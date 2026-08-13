"""LINE 官方帳號推播（Messaging API）。

**為什麼不是 LINE Notify：** LINE Notify 已於 2025-03-31 終止服務，2025-04-01 起
全部功能停用（含 API 與 token 發行）。要推到群組只剩官方帳號 + Messaging API 一條路。

**為什麼不透過 agent／MCP：** 與 core/alerts.py 同一個原則 —— 通知不能依賴 agent
堆疊。純 requests，只依賴一組 channel access token。

**計費（會真正扣到額度的是這個，不是「送了幾則」）：**
  以**實際接收人數**計算 —— 推一次到 N 人的群組就扣 N 則。
  但**一次 push 帶多個訊息物件只算一則**，所以文字與音訊一定要打包在同一個請求裡，
  分兩次呼叫會變成 2N。免費方案每月 200 則，額度用完 push 回 429。
  送之前先問 quota，不要等 429 才知道。

**LINE 沒有「檔案」訊息類型**（只有 text/sticker/image/video/audio/location/
imagemap/template/flex），所以 memo 的 .docx 傳不進群組，只能給連結。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

API = "https://api.line.me/v2/bot"
TIMEOUT = 30

# 單則文字訊息上限。超過會 400，所以在送出前就先截斷。
TEXT_LIMIT = 5000
# 一次 push 最多帶幾個訊息物件
MAX_MESSAGES = 5


class LineError(RuntimeError):
    pass


class LineNotConfigured(LineError):
    """還沒設定 token／group_id。呼叫端據此決定是「跳過」而不是「失敗」。"""


def secrets_path() -> Path:
    from core.gauth import SECRET_DIR

    return SECRET_DIR / "line.json"


#: push 真正需要的兩項。channel_id / channel_secret / user_id 都不是 ——
#: channel secret 只用在驗證 webhook 簽章，本流程只推不收，用不到。
REQUIRED = ("channel_access_token", "group_id")


def read_settings() -> dict:
    """把設定檔讀出來，**不驗證**。給 setup 精靈逐項回報用。

    刻意與 Google 的 token 放在同一個專案外的 secrets 目錄 ——
    config.json 在 OneDrive 底下，channel access token 放進去會被同步到雲端。
    """
    p = secrets_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LineError(f"{p} 不是合法 JSON：{exc}") from exc


def load_settings() -> dict:
    """讀出並確認 push 需要的欄位都在。缺就丟 LineNotConfigured。"""
    p = secrets_path()
    data = read_settings()
    if not data:
        raise LineNotConfigured(
            f"找不到 {p}。\n"
            f"    設定步驟：python tools/setup_line.py"
        )
    missing = [k for k in REQUIRED if not data.get(k)]
    if missing:
        raise LineNotConfigured(f"{p} 還缺 {'、'.join(missing)}")
    return data


class LineClient:
    def __init__(self, token: str):
        self.token = token

    # ---------- 底層 ----------

    def _call(self, method: str, path: str, **kw) -> dict:
        url = f"{API}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        if method == "POST":
            headers["Content-Type"] = "application/json"

        delay = 2
        for attempt in range(4):
            r = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kw)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                # 429 有兩種：額度用完（重試沒用）與流量限制（重試有用）。
                # 訊息裡看得出來，額度用完就不要白等。
                if r.status_code == 429 and "monthly limit" in r.text.lower():
                    break
                time.sleep(delay)
                delay *= 2
                continue
            break

        if r.status_code >= 400:
            raise LineError(f"LINE API {method} {path} → {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    # ---------- 查詢 ----------

    def bot_info(self) -> dict:
        """自我檢查用：token 對不對、是哪一個官方帳號。"""
        return self._call("GET", "/info")

    def quota(self) -> dict:
        return self._call("GET", "/message/quota")

    def consumption(self) -> dict:
        return self._call("GET", "/message/quota/consumption")

    def remaining(self) -> int | None:
        """本月還剩幾則。無上限方案回 None。"""
        q = self.quota()
        if q.get("type") != "limited":
            return None
        return int(q.get("value", 0)) - int(self.consumption().get("totalUsage", 0))

    def group_summary(self, group_id: str) -> dict:
        return self._call("GET", f"/group/{group_id}/summary")

    def group_member_count(self, group_id: str) -> int:
        return int(self._call("GET", f"/group/{group_id}/members/count").get("count", 0))

    # ---------- 推播 ----------

    def push(self, to: str, messages: list[dict]) -> dict:
        """一次送出。**所有訊息物件要放在同一個請求裡** —— 分次送會按次數扣額度。"""
        if not messages:
            raise LineError("沒有訊息可送")
        if len(messages) > MAX_MESSAGES:
            raise LineError(f"一次最多 {MAX_MESSAGES} 個訊息物件，收到 {len(messages)}")
        return self._call("POST", "/message/push",
                          data=json.dumps({"to": to, "messages": messages},
                                          ensure_ascii=False).encode("utf-8"))


# ---------- 訊息物件 ----------


def text_message(body: str, *, tail: str = "") -> dict:
    """文字訊息。超過上限就從中間截斷，**保留結尾**（連結都在那裡）。"""
    if len(body) > TEXT_LIMIT:
        keep = TEXT_LIMIT - len(tail) - 20
        body = body[:keep] + "\n…（略）\n" + tail
    return {"type": "text", "text": body}


def audio_message(url: str, duration_ms: int) -> dict:
    """語音訊息。

    `originalContentUrl` 必須是 **HTTPS（TLS 1.2+）且公開可存取**的直連網址 ——
    LINE 的伺服器與每一位按播放的成員都會去抓它，所以來源必須長期保持公開。
    """
    if not url.startswith("https://"):
        raise LineError(f"語音訊息的網址必須是 https：{url}")
    return {
        "type": "audio",
        "originalContentUrl": url,
        "duration": int(duration_ms),
    }
