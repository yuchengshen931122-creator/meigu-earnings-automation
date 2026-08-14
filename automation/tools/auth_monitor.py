"""授權快迴圈：檢查 → 能修的當場修 → 狀態寫給看板 → 修不好的才叫人。

回答的是使用者的問題：「我要怎麼**即時**知道現在有沒有在授權，
而不是出錯了才發現？」答案分三層：

  1. 每 10 分鐘（排程 MeiguAuthMon）把五條授權線的現況寫進
     state/auth_status.json —— 看板（serve_dashboard，每 15 秒輪詢）
     直接顯示五盞燈，隨時打開 http://127.0.0.1:8787/ 就看得到。
  2. 查到掛的先**自動修復**再說：Google access token → refresh；
     NotebookLM session → auth refresh（保溫與修復是同一件事，
     原 MeiguNlmWarm 的工作已併入這裡）。修好了就不吵人。
  3. 修不好的（refresh token 被撤銷、NotebookLM session 整個過期、
     claude CLI 登出）**立刻推 LINE**，訊息帶著修復指令。
     恢復時若先前吵過人，再推一則「已恢復」收尾。

與 watchdog 的分工：watchdog 每小時做深度檢查（真的叫 claude 跑一輪、
查流程卡關），本工具每 10 分鐘只做授權面的快檢查。兩邊共用同一組
alert key 與 alerts_sent.json 冷卻，同一個問題不會被兩邊各轟一次。
watchdog 反過來監督本工具：auth_status.json 超過 65 分鐘沒更新
就發 authmon_stale —— 監控自己停了卻還掛著綠燈，比沒有監控更糟。

自動修復的誠實邊界（修不回來的只能叫人，訊息會帶指令）：
  Google access token 過期     → refresh 修得回來（全自動）
  NotebookLM session 變冷      → auth refresh 修得回來（全自動）
  NotebookLM session 已過期    → master-token-refresh 修得回來（全自動；
                                 master token 於 2026-08-12 bootstrap）
  claude CLI 主登入失效        → 切換 secrets 的長效備援 token（全自動，
                                 看板轉黃提醒主登入未修）
  Google refresh token 失效    → 要人跑 tools/setup_oauth.py
  NotebookLM master token 被撤銷（改 Google 密碼）→ 要人重跑 bootstrap
  claude 備援 token 也失效     → 要人跑 claude auth login

另外一個「不是出錯才知道」的預警：OAuth 同意畫面若還在**測試**狀態，
refresh token 七天就會被 Google 作廢（README 的設定步驟就是測試狀態）。
本工具記錄 refresh token 的首見時間，滿 6 天推一次預警，讓你在斷之前
有一天的時間處理。一勞永逸的解法是把同意畫面發布成**正式版**，
refresh token 就不再有七天限期 —— 見 README「授權的即時監控」一節。

用法：
    python tools/auth_monitor.py            # 檢查＋修復＋寫狀態＋必要時告警
    python tools/auth_monitor.py --dry-run  # 檢查＋修復＋寫狀態，不送告警
"""

import _bootstrap  # noqa: F401

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from core import claude_cli
from core.alerts import Alerter, format_report
from core.gauth import TOKEN_PATH, auth_status, load_config, load_credentials
from watchdog import check_drive, check_line, check_notebooklm

STATE_NAME = "auth_status.json"
INTERVAL_MINUTES = 10

#: 測試模式 refresh token 的壽命是 7 天，滿這個天數就預警（留一天緩衝）
RENEW_WARN_DAYS = 6

SETUP_OAUTH = f'python "{Path(__file__).resolve().with_name("setup_oauth.py")}"'


def check_claude_auth(timeout: int = 90) -> tuple[bool, str]:
    """claude CLI 登入狀態。

    刻意用 `claude auth status`（本機查詢，免費、秒回）而不是 watchdog 的
    `claude -p ok`（一次真的 LLM 呼叫）—— 放進 10 分鐘一輪的迴圈裡，
    後者一天要多燒 144 次。端到端能不能跑仍由 watchdog 每小時驗一次。
    """
    try:
        r = subprocess.run(["claude", "auth", "status"], capture_output=True,
                           text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False, "找不到 claude 指令"
    except subprocess.TimeoutExpired:
        return False, f"auth status 逾時（{timeout}s）"

    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return False, f"回傳非 JSON：{out[:120].strip()}"
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False, f"JSON 解析失敗：{out[:120].strip()}"

    if d.get("loggedIn"):
        return True, f"已登入（{d.get('authMethod')}／{d.get('subscriptionType')}）"
    return False, "未登入"


def verify_claude_fallback(timeout: int = 180) -> tuple[bool, str]:
    """備援 token 要真的能跑一輪才算修復。

    這是一次真的 LLM 呼叫 —— 只在主登入失效時才走到，且旗標檔記錄
    verified_at，6 小時內不重驗，不會在故障期間每 10 分鐘燒一次。
    """
    try:
        tok = claude_cli.TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return False, f"token 檔讀取失敗：{exc}"
    if not tok:
        return False, "token 檔是空的"
    env = os.environ.copy()
    env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    try:
        # --model haiku：只驗 token 有效性（授權錯誤擋在模型路由之前，
        # 用哪個模型測都一樣），沒必要為一句 ok 燒預設模型的量。
        r = subprocess.run(["claude", "-p", "ok", "--model", "haiku"],
                           capture_output=True, text=True,
                           timeout=timeout, env=env,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False, "找不到 claude 指令"
    except subprocess.TimeoutExpired:
        return False, f"驗證逾時（{timeout}s）"
    if r.returncode != 0:
        return False, f"rc={r.returncode}: {(r.stdout or r.stderr or '')[:120].strip()}"
    return True, "備援 token 實測可跑"


def nlm_refresh(timeout: int = 240) -> tuple[bool, str]:
    """NotebookLM 保溫＝第一段修復：session 變冷時 refresh 一次就救得回來。"""
    try:
        r = subprocess.run(["notebooklm", "auth", "refresh"], capture_output=True,
                           text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False, "找不到 notebooklm 指令"
    except subprocess.TimeoutExpired:
        return False, f"auth refresh 逾時（{timeout}s）"
    return r.returncode == 0, ((r.stdout or r.stderr or "").strip()[:160])


def nlm_master_refresh(timeout: int = 300) -> tuple[bool, str]:
    """NotebookLM 第二段修復：用 master token 無頭重鑄整套 session cookies。

    auth refresh 只救得回「變冷」的 session；session 整個過期就要靠這個
    （實測 rc=0、無瀏覽器、無人工）。master token 本身只在使用者改
    Google 密碼／撤銷授權時才會死 —— 那才真的需要人重跑一次
    notebooklm login --master-token。
    """
    try:
        r = subprocess.run(["notebooklm", "login", "--master-token-refresh"],
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False, "找不到 notebooklm 指令"
    except subprocess.TimeoutExpired:
        return False, f"master-token-refresh 逾時（{timeout}s）"
    return r.returncode == 0, ((r.stdout or r.stderr or "").strip()[:160])


def _refresh_token_fingerprint() -> str | None:
    """refresh token 換了沒（重新授權會換一顆）。只存指紋，不存 token 本身。"""
    try:
        tok = json.loads(TOKEN_PATH.read_text(encoding="utf-8")).get("refresh_token")
    except Exception:
        return None
    if not tok:
        return None
    return hashlib.sha256(tok.encode()).hexdigest()[:12]


def _item(label: str, ok: bool, detail: str, *,
          warn: bool = False, repaired: bool = False) -> dict:
    d = {"label": label, "ok": bool(ok), "detail": str(detail)}
    if warn:
        d["warn"] = True
    if repaired:
        d["repaired"] = True
    return d


#: (狀態檔 key, alert key —— 與 watchdog 共用, 修復指令說明)
ALERT_MAP = {
    "google_oauth": ("auth_expired", [
        "影響：歸檔、讀寫追蹤表全部停擺。",
        "自動修復（refresh）已試過且失敗，需要人工重新授權：",
        f"  {SETUP_OAUTH}",
    ]),
    "drive": ("drive_access_lost", [
        "影響：memo 分頁與 podcast 上傳失敗。",
        "兩棵樹的擁有者是 owner@example.com，本帳號僅為協作者，",
        "多半是對方調整了共享設定，需要請對方恢復。",
    ]),
    "notebooklm": ("notebooklm_expired", [
        "影響：podcast 產不出來，LINE 也不會推。",
        "兩段自動修復（auth refresh、master-token-refresh）都已試過且失敗，",
        "master token 可能已被撤銷（改過 Google 密碼？）。請在有畫面的環境重跑：",
        "  notebooklm login --master-token --account you@example.com",
    ]),
    "claude_cli": ("claude_cli_down", [
        "影響：日期核對 agent 與 memo 派工全部停擺（歸檔不受影響）。",
        "處理：在終端機執行 claude auth login（或互動式 claude 裡 /login）。",
        "重建備援 token：claude setup-token，再跑 python tools/store_claude_token.py。",
    ]),
    "line": ("line_unavailable", [
        "影響：memo 與 podcast 照常產出歸檔，但群組收不到通知。",
        "處理：python tools/setup_line.py --check",
    ]),
}


def run_checks(cfg, prev: dict, now: dt.datetime) -> tuple[dict, dict]:
    """回傳 (items, refresh_token 追蹤狀態)。"""
    items: dict[str, dict] = {}

    # ---- 1. Google OAuth（auth_status 內建 refresh ＝ 自動修復）----
    st = auth_status()
    g_ok, g_detail = st["ok"], st["reason"]
    repaired = g_detail == "refreshed"

    # refresh token 年齡追蹤：測試模式下 7 天必死，滿 6 天就該預警。
    fp = _refresh_token_fingerprint()
    rt_prev = prev.get("refresh_token") or {}
    if fp and rt_prev.get("fingerprint") == fp:
        rt = dict(rt_prev)
    else:
        rt = {"fingerprint": fp, "first_seen": now.isoformat(timespec="seconds"),
              "renew_warned": False}

    warn = False
    if g_ok and fp:
        try:
            age = now - dt.datetime.fromisoformat(rt["first_seen"])
            if age.days >= RENEW_WARN_DAYS:
                warn = True
                g_detail += (f"；refresh token 已使用 {age.days} 天 —— 同意畫面若仍是"
                             f"「測試」狀態，7 天即失效，請盡快處理（見 README）")
        except ValueError:
            pass

    items["google_oauth"] = _item("Google OAuth", g_ok, g_detail,
                                  warn=warn, repaired=repaired)

    creds = None
    if g_ok:
        try:
            creds = load_credentials(interactive=False)
        except Exception as exc:
            items["google_oauth"] = _item(
                "Google OAuth", False, f"load_credentials：{type(exc).__name__}: {exc}")

    # ---- 2. Drive 寫入（真的打 API —— 證明授權不只帳面有效）----
    if creds:
        ok, detail = check_drive(creds, cfg)
        items["drive"] = _item("Drive 寫入", ok, detail)
    else:
        items["drive"] = _item("Drive 寫入", False, "Google OAuth 不可用，未檢查")

    # ---- 3. NotebookLM：refresh（保溫）→ check → master-token-refresh → 再 check ----
    if cfg.get("run", {}).get("podcast_mode") == "cli":
        was_bad = not ((prev.get("items") or {}).get("notebooklm") or {}).get("ok", True)
        r_ok, _ = nlm_refresh()
        ok, detail = check_notebooklm()
        repaired = ok and was_bad and r_ok
        if not ok:
            m_ok, m_out = nlm_master_refresh()
            if m_ok:
                ok, detail = check_notebooklm()
                repaired = ok
                if not ok:
                    detail += "（master-token-refresh 成功但 check 仍失敗）"
            else:
                detail += f"（master-token-refresh 失敗：{m_out[:80]}）"
        items["notebooklm"] = _item("NotebookLM", ok, detail, repaired=repaired)
    else:
        items["notebooklm"] = _item(
            "NotebookLM", True,
            f"略過（podcast_mode={cfg.get('run', {}).get('podcast_mode')}）")

    # ---- 4. claude CLI：主登入 → 失效就切換 secrets 的長效備援 token ----
    ok, detail = check_claude_auth()
    fpath = claude_cli.flag_path(cfg)
    if ok:
        if fpath.exists():
            fpath.unlink(missing_ok=True)
            detail += "；主登入已恢復，備援 token 退場"
        items["claude_cli"] = _item("claude CLI", True, detail)
    elif claude_cli.TOKEN_PATH.exists():
        newly = not fpath.exists()
        flag = {}
        if not newly:
            try:
                flag = json.loads(fpath.read_text(encoding="utf-8"))
            except Exception:
                flag = {}
        need_verify = True
        if flag.get("verified_at"):
            try:
                need_verify = (now - dt.datetime.fromisoformat(flag["verified_at"])
                               ) > dt.timedelta(hours=6)
            except ValueError:
                pass
        v_ok, v_detail = (verify_claude_fallback() if need_verify
                          else (True, "6 小時內驗證過"))
        if v_ok:
            fpath.write_text(json.dumps({
                "activated_at": flag.get("activated_at")
                                or now.isoformat(timespec="seconds"),
                "verified_at": now.isoformat(timespec="seconds")
                               if need_verify else flag.get("verified_at"),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            it = _item("claude CLI", True,
                       f"主登入失效（{detail}）→ 備援 token 運作中（{v_detail}）。"
                       f"有空時跑 claude auth login 恢復主登入",
                       warn=True, repaired=newly)
            if newly:
                it["_notify"] = True   # 首次切換推一則通知，main 會取走這個鍵
            items["claude_cli"] = it
        else:
            fpath.unlink(missing_ok=True)
            items["claude_cli"] = _item(
                "claude CLI", False, f"{detail}；備援 token 也無效（{v_detail}）")
    else:
        items["claude_cli"] = _item(
            "claude CLI", False,
            detail + "；secrets 沒有備援 token（設定：claude setup-token + "
                     "python tools/store_claude_token.py）")

    # ---- 5. LINE 推播 ----
    ok, detail = check_line(cfg)
    items["line"] = _item("LINE 推播", ok, detail)

    return items, rt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不送告警，只檢查與寫狀態檔")
    args = ap.parse_args()

    cfg = load_config()
    now = dt.datetime.now()
    state_path = Path(cfg["local"]["state_dir"]) / STATE_NAME
    try:
        prev = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        prev = {}

    items, rt = run_checks(cfg, prev, now)
    fallback_notice = bool(items.get("claude_cli", {}).pop("_notify", False))

    # ---- 關機／休眠空窗偵測 ----
    # 本工具每 10 分鐘一輪。上一輪與這一輪之間隔太久，代表機器離線過 ——
    # 排程都設了 StartWhenAvailable（錯過補跑），所以功能會自己恢復，
    # 但「曾經斷線多久」這件事要讓使用者知道，而不是無聲吞掉。
    gap_minutes = None
    if prev.get("checked_at"):
        try:
            gap = now - dt.datetime.fromisoformat(prev["checked_at"])
            if gap > dt.timedelta(minutes=45):
                gap_minutes = int(gap.total_seconds() // 60)
        except ValueError:
            pass

    # ---- 狀態檔：看板的即時資料源，先寫再告警（告警炸了狀態也要在）----
    state_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"checked_at": now.isoformat(timespec="seconds"),
            "interval_minutes": INTERVAL_MINUTES,
            "refresh_token": rt,
            "items": items}
    state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    for it in items.values():
        flag = "OK " if it["ok"] else "FAIL"
        if it.get("warn"):
            flag = "WARN"
        extra = "（這輪已自動修復）" if it.get("repaired") else ""
        print(f"{it['label']:<14}{flag}  {it['detail']}{extra}")

    if args.dry_run:
        print("[dry-run] 不送告警")
        return 0 if all(i["ok"] for i in items.values()) else 1

    # ---- 告警與恢復通知（與 watchdog 共用 key 與冷卻）----
    creds = None
    if items["google_oauth"]["ok"]:
        try:
            creds = load_credentials(interactive=False)
        except Exception:
            pass
    alerter = Alerter(creds, cfg)

    if gap_minutes:
        h, m = divmod(gap_minutes, 60)
        span = f"{h} 小時 {m} 分" if h else f"{m} 分鐘"
        alerter.send("machine_gap", f"機器離線約 {span}，已恢復", format_report(
            f"偵測到監控空窗約 {span}", [
                f"上一輪：{prev.get('checked_at')}／本輪：{now.isoformat(timespec='seconds')}",
                "",
                "排程已自動恢復並補跑（StartWhenAvailable）。",
                "空窗期間發布的財報不會漏掉 —— 就緒偵測每輪重掃，",
                "新聞稿一旦確認就永不放棄，回線後會自動接續產出。",
            ]), force=True)

    if fallback_notice:
        # 首次切換備援：算「修好了」所以不走失敗告警，但使用者必須知道
        # 主登入死了 —— 掛在 claude_cli_down 這個 key 下，之後主登入恢復
        # 會自動收到「已恢復」收尾。
        alerter.send("claude_cli_down", "claude CLI 已切換備援 token", format_report(
            "claude CLI 主登入失效，已自動切換長效備援 token", [
                items["claude_cli"]["detail"], "",
                "派工與告警不中斷；看板該項顯示黃色「注意」。",
                "恢復主登入：在終端機執行 claude auth login，",
                "恢復後備援自動退場，會再收到一則「已恢復」。"]), force=True)

    for key, it in items.items():
        alert_key, fix_lines = ALERT_MAP[key]
        if it["ok"]:
            if alerter.was_alerted(alert_key):
                # 之前吵過人，恢復也要說一聲，不然使用者不知道還要不要處理
                alerter.send(alert_key + "_recovered", f"{it['label']} 已恢復",
                             format_report(f"{it['label']} 已恢復正常", [
                                 f"目前狀態：{it['detail']}",
                                 "（先前的告警不需要再處理）"]), force=True)
                alerter.clear(alert_key + "_recovered")
                alerter.clear(alert_key)
            continue
        if key == "drive" and not items["google_oauth"]["ok"]:
            # Drive 的紅燈只是 Google OAuth 掛掉的下游症狀，
            # 同一個根因不推兩則 —— auth_expired 那則已經在路上了。
            continue
        body = format_report(f"{it['label']} 授權異常", [f"detail：{it['detail']}", ""] + fix_lines)
        sent = alerter.send(alert_key, f"{it['label']} 授權異常", body)
        print(f"  {'已推送告警' if sent else '冷卻中未重複推送'}：{it['label']}")

    # refresh token 屆滿預警：每顆 token 只推一次（不進冷卻循環，避免天天轟）
    if items["google_oauth"].get("warn") and not rt.get("renew_warned"):
        sent = alerter.send("auth_renew_soon", "Google 授權即將到期", format_report(
            "refresh token 可能在一天內失效", [
                items["google_oauth"]["detail"], "",
                "若 OAuth 同意畫面仍是「測試」狀態，refresh token 滿 7 天會被作廢。",
                "一勞永逸：Cloud Console → OAuth 同意畫面 → 發布為「正式版」，",
                f"然後重新授權一次：{SETUP_OAUTH}",
                "（已是正式版的話，這則預警可忽略，token 沒有 7 天限期）",
            ]), force=True)
        if sent:
            rt["renew_warned"] = True
            data["refresh_token"] = rt
            state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")

    return 0 if all(i["ok"] for i in items.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
