"""告警：LINE 私訊 + Gmail。

設計原則：**告警不能依賴 agent 堆疊。**
如果告警要透過 `claude -p` + Gmail MCP 才送得出去，那麼「claude CLI 掛了」
這件事就永遠通知不出去 —— 而那正是最需要被通知的情況。
所以這裡直接用 API，純 Python。

**為什麼加 LINE 私訊（2026-08-12）**：本帳號的 OAuth 目前沒有 gmail.send 範圍，
Gmail 只能走降級管道 —— 叫 claude CLI 的 Gmail MCP 建一封**草稿**。
草稿不會推播到手機，等於無人值守時出事根本不會有人知道
（實測：NotebookLM 授權過期了好幾個小時，沒有任何一則通知送出）。
LINE 那條路已經在推播財報時證明可用，而且是唯一真的會讓手機響的管道，
所以拿它當告警主路徑，推到使用者自己的 1:1（`user_id`），不是財報那個群組。

三條路互相獨立，任一條成功就算送到：
    LINE 私訊（會推播，主路徑）→ Gmail API（有 gmail.send 才走）→ Gmail 草稿（聊勝於無）

**額度保護**：LINE 免費方案每月 200 則，告警與財報推播共用。
剩餘額度低於 QUOTA_FLOOR 時不再送告警 —— 財報推播才是這條線的產出，
不能被告警洗掉。此時仍會走 Gmail 那兩條路。

去重：同一種告警在 cooldown 內只送一次，避免每小時的排程把手機洗爆。
狀態存在 state/alerts_sent.json。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import traceback
from email.mime.text import MIMEText
from pathlib import Path

from googleapiclient.discovery import build

DEFAULT_COOLDOWN_MINUTES = 180

#: LINE 剩餘額度低於這個數字就不再用 LINE 送告警，把額度留給財報推播。
#: 一檔財報推播吃「群組人數」則（目前 4），留 40 約等於還推得動 10 檔。
QUOTA_FLOOR = 40


class Alerter:
    def __init__(self, creds, cfg: dict):
        self.cfg = cfg
        self.to = cfg["alerts"]["email_to"]
        self.enabled = set(cfg["alerts"].get("on") or [])
        self.cooldown = dt.timedelta(
            minutes=cfg["alerts"].get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)
        )
        self.state_path = Path(cfg["local"]["state_dir"]) / "alerts_sent.json"
        self._svc = None
        self._creds = creds
        # 最近一次送出走了哪些路。"line"／"api" 是真的送達，"draft" 只是草稿不會推播。
        self.last_channel: str | None = None
        self.channels: list[str] = []

    #: 真的會讓使用者看到的管道（草稿不算）
    PUSH_CHANNELS = frozenset({"line", "api"})

    @property
    def pushed(self) -> bool:
        """最近一次 send() 有沒有走到真的會推播的管道。"""
        return bool(self.PUSH_CHANNELS & set(self.channels))

    @property
    def available(self) -> bool:
        """憑證有沒有 gmail.send 範圍。沒有就不能寄信，但不該讓其他功能跟著壞。

        creds 允許是 None —— auth_monitor 在「Google OAuth 本身掛掉」時仍要能
        用 LINE 送告警，那正是最需要告警的時刻，不能因為沒憑證就整個不能用。
        """
        if self._creds is None:
            return False
        from core.gauth import GMAIL_SCOPE, has_scope

        return has_scope(self._creds, GMAIL_SCOPE)

    @property
    def svc(self):
        if self._svc is None:
            self._svc = build("gmail", "v1", credentials=self._creds, cache_discovery=False)
        return self._svc

    # ---------- 去重 ----------

    def _load(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self, d: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    def _recently_sent(self, key: str) -> bool:
        sent = self._load().get(key)
        if not sent:
            return False
        try:
            return dt.datetime.now() - dt.datetime.fromisoformat(sent) < self.cooldown
        except ValueError:
            return False

    def was_alerted(self, key: str) -> bool:
        """這個 key 是否有「發出後尚未 clear」的告警紀錄。

        給恢復通知用：只有真的吵過人的問題，恢復時才值得再推一則「好了」；
        沒吵過人的小抖動安靜復原就好。
        """
        return key in self._load()

    def clear(self, key: str) -> None:
        """問題解除時呼叫，讓下次再發生時能立刻通知。"""
        d = self._load()
        if d.pop(key, None) is not None:
            self._save(d)

    # ---------- 送信 ----------

    def send(self, key: str, subject: str, body: str, *, force: bool = False) -> bool:
        if key not in self.enabled and not force:
            return False
        if not force and self._recently_sent(key):
            return False

        channels: list[str] = []

        # 1) LINE 私訊 —— 唯一真的會讓手機響的管道，所以排第一
        if self._send_via_line(subject, body):
            channels.append("line")

        # 2) Gmail API（憑證要有 gmail.send 範圍）
        if self.available:
            try:
                self._send_via_gmail(subject, body)
                channels.append("api")
            except Exception as exc:
                print(f"    [Gmail API 寄送失敗] {type(exc).__name__}: {exc}")
        elif not channels:
            # 3) 降級管道：透過 claude CLI 的 Gmail MCP 建草稿。
            # **這比前兩條都差**——它依賴 claude CLI 本身，而「CLI 掛了」正是
            # 最需要被通知的情況之一，那時這條路也會一起死；而且草稿不會推播。
            # 只在前面全都沒送成時才走：它要叫起一個 claude 子進程，慢又不可靠，
            # LINE 已經送到就不值得再花這個成本。
            if self._send_via_cli(subject, body):
                channels.append("draft")

        self.channels = channels
        self.last_channel = channels[0] if channels else None
        if not channels:
            return False

        d = self._load()
        d[key] = dt.datetime.now().isoformat(timespec="seconds")
        self._save(d)
        return True

    def _send_via_gmail(self, subject: str, body: str) -> None:
        msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = self.to
        msg["subject"] = f"[美股自動化] {subject}"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self.svc.users().messages().send(userId="me", body={"raw": raw}).execute()

    def _send_via_line(self, subject: str, body: str) -> bool:
        """推到使用者自己的 1:1 聊天室。

        失敗只回報不拋 —— 告警管道壞掉不該讓 watchdog 自己也掛掉。
        """
        try:
            from core import line_client as lc
        except Exception as exc:      # requests 沒裝之類
            print(f"    [LINE 告警略過] 載入失敗：{type(exc).__name__}: {exc}")
            return False

        try:
            s = lc.read_settings()
        except Exception as exc:
            print(f"    [LINE 告警略過] 設定讀不到：{exc}")
            return False

        token, uid = s.get("channel_access_token"), s.get("user_id")
        if not token or not uid:
            # 沒有 user_id 就不要退而求其次推到群組 —— 系統壞掉是維運的事，
            # 不該洗版到 5 個人共用的工作群組。
            print("    [LINE 告警略過] line.json 沒有 user_id（刻意不改推群組）")
            return False

        try:
            cli = lc.LineClient(token)
            left = cli.remaining()
            if left is not None and left < QUOTA_FLOOR:
                print(f"    [LINE 告警略過] 剩餘額度 {left} < {QUOTA_FLOOR}，"
                      f"額度留給財報推播")
                return False
            cli.push(uid, [lc.text_message(f"[美股自動化] {subject}\n\n{body}")])
        except Exception as exc:
            print(f"    [LINE 告警失敗] {type(exc).__name__}: {exc}")
            return False
        return True


    def _send_via_cli(self, subject: str, body: str) -> bool:
        """備援：叫 claude CLI 用它的 Gmail MCP 寄信。"""
        import subprocess

        # 實測：這個 Gmail MCP 只有 create_draft / update_draft，**沒有寄送工具**。
        # 所以退而求其次建草稿 —— 手機上的 Gmail 至少看得到草稿，
        # 但它**不會推播**，不能取代真正的告警。真正的解法是拿到 gmail.send。
        prompt = (
            f"用 Gmail 建立一封『草稿』（這個 MCP 只有草稿工具，沒有寄送工具）。\n"
            f"收件者：{self.to}\n"
            f"主旨：[美股自動化][草稿告警] {subject}\n"
            f"內文（原樣照抄，不要改寫、不要加註解）：\n{body}\n\n"
            f"建立後只回一行 SENT 或 FAILED:<原因>。"
        )
        try:
            from core.claude_cli import cli_env

            r = subprocess.run(
                ["claude", "--dangerously-skip-permissions", "-p", prompt],
                capture_output=True, text=True, timeout=300,
                encoding="utf-8", errors="replace", env=cli_env(),
            )
        except Exception as exc:
            print(f"    [備援管道失敗] {type(exc).__name__}: {exc}")
            return False
        out = (r.stdout or "").strip()
        if r.returncode == 0 and "SENT" in out.upper():
            print("    [備援管道] 已建立 Gmail 草稿（注意：草稿不會推播）")
            return True
        print(f"    [備援管道失敗] rc={r.returncode} out={out[:200]}")
        return False


def format_report(title: str, lines: list[str], *, footer: str = "") -> str:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [title, f"時間：{ts}（台灣）", ""]
    out += lines
    if footer:
        out += ["", footer]
    return "\n".join(out)


def exception_body(context: str, exc: BaseException) -> str:
    return format_report(
        f"{context} 發生例外",
        [f"{type(exc).__name__}: {exc}", "", "".join(traceback.format_exception(exc))[:3000]],
    )
