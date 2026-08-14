"""健康檢查 + Gmail 告警。掛在排程器上，每小時跑一次。

檢查的是「無人值守時會靜默死掉」的那些點 —— 每一項都是實際踩過或確認存在的風險：

  auth_expired        Google OAuth token 失效 → 歸檔、讀表全停
  claude_cli_down     claude CLI 未登入或無法執行 → 派工與日期核對全停
  notebooklm_expired  NotebookLM 授權過期 → podcast 產不出來 → 也就不會推 LINE
  drive_access_lost   Drive 樹是 共用樹擁有者 的，共享權限被撤就寫不進去
  line_unavailable    LINE token 失效／額度用完 → 做完了但沒人收到通知
  stuck_readiness     財報早就發了，卻一直派不出工（就緒偵測卡住）
  no_activity_24h     排程器自己死了 —— 沒有任何檢查點在 24 小時內更新
  run_failed_final    有 ticker 重試到上限仍失敗
  authmon_stale       授權監控（MeiguAuthMon）自己停了 —— 看板的授權燈號
                      會一直停在最後一次的綠燈，比沒有監控更糟

用法：
    python tools/watchdog.py            # 檢查並在有問題時寄信
    python tools/watchdog.py --dry-run  # 只檢查不寄信
    python tools/watchdog.py --test     # 寄一封測試信，驗證管道通不通
"""

import _bootstrap  # noqa: F401

import argparse
import datetime as dt
import json
import re
import subprocess
from pathlib import Path

from core import checkpoint as cp
from core.alerts import Alerter, format_report
from core.gauth import auth_status, load_config, load_credentials

CLI = "claude"
NLM = "notebooklm"


def check_claude_cli(timeout: int = 120) -> tuple[bool, str]:
    try:
        # env=cli_env()：備援 token 啟用時，這裡量的是「實際派工會用的能力」，
        # 不是主登入的帳面狀態（那是 auth_monitor 的事）。
        # --model haiku：這裡只驗「登入有效＋端到端能跑」。授權錯誤發生在
        # 模型路由之前、session limit 也是帳號級跨模型，用哪個模型測結果都一樣；
        # haiku 便宜一個量級，且 Opus 專屬額度用完時不會誤報成「CLI 掛了」
        # （配額用完不是失敗，那是 dispatcher 的 QUOTA 分支自己處理的事）。
        from core.claude_cli import cli_env
        r = subprocess.run([CLI, "-p", "ok", "--model", "haiku"],
                           capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace",
                           env=cli_env())
    except FileNotFoundError:
        return False, "找不到 claude 指令"
    except subprocess.TimeoutExpired:
        return False, f"claude -p 逾時（{timeout}s）"
    if r.returncode != 0:
        return False, f"rc={r.returncode}: {(r.stdout or r.stderr or '')[:200].strip()}"
    return True, "正常"


def check_notebooklm(timeout: int = 180) -> tuple[bool, str]:
    """NotebookLM CLI 授權。

    **為什麼非查不可**：podcast 全靠這支 CLI 產。授權一過期，make_podcast 就會
    在 ensure_auth() 丟錯，但那個錯被 dispatcher 吞掉（memo 照樣歸檔、排程 rc=0），
    而沒有音檔就不會推 LINE —— 整條線看起來風平浪靜，實際上產出少了一半。
    2026-08-12 就是這樣靜默停擺了好幾個小時，五項檢查全綠。
    """
    try:
        r = subprocess.run([NLM, "auth", "check", "--test", "--json"],
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False, "找不到 notebooklm 指令"
    except subprocess.TimeoutExpired:
        return False, f"auth check 逾時（{timeout}s）"

    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return False, f"auth check 回傳非 JSON：{out[:160].strip()}"
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False, f"auth check JSON 解析失敗：{out[:160].strip()}"

    checks = d.get("checks") or {}
    if d.get("status") == "ok" and checks.get("token_fetch"):
        return True, "已授權"

    err = ((d.get("details") or {}).get("error") or "").splitlines()
    detail = f"status={d.get('status')}／token_fetch={checks.get('token_fetch')}"
    return False, detail + (f"；{err[0][:140]}" if err else "")


def check_line(cfg) -> tuple[bool, str]:
    """LINE 推播管道：token 有效、群組讀得到、額度還夠推。

    額度是按「接收人數」計的，推一次群組吃掉成員數那麼多則。剩餘量若不到
    兩檔的份，等於接下來做完也送不出去 —— 那要提早知道，不是等到當下才發現。
    """
    try:
        from core import line_client as lc
    except Exception as exc:
        return False, f"載入失敗：{type(exc).__name__}: {exc}"

    try:
        s = lc.load_settings()
    except lc.LineNotConfigured as exc:
        return False, f"尚未設定：{str(exc).splitlines()[0]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    try:
        cli = lc.LineClient(s["channel_access_token"])
        cli.bot_info()                       # token 無效這裡就會炸
        members = cli.group_member_count(s["group_id"])
        left = cli.remaining()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:180]}"

    if left is None:
        return True, f"群組 {members} 人，方案無額度上限"
    pushes = left // max(members, 1)
    if pushes < 2:
        return False, f"剩餘額度 {left} 則／群組 {members} 人 → 只夠再推 {pushes} 檔"
    return True, f"剩餘額度 {left} 則／群組 {members} 人 → 約還能推 {pushes} 檔"


def check_podcast_tree(creds, cfg) -> tuple[bool, str]:
    """config.json 指的 podcast 樹是不是本季那一棵。

    podcast 樹每季新建、命名無規則。指到上一季那棵不會有任何錯誤浮現 ——
    資料夾存在、上傳成功、表照樣打勾 —— 但整季的音檔都歸錯地方。
    `resolve_podcast_tree.py` 每天會自動更正；認不出來時它拒絕猜，
    那個情況要靠這裡叫人。
    """
    import datetime as _dt

    from resolve_podcast_tree import candidates, quarter_token

    from core.drive_client import DriveClient

    d = cfg["drive"]
    token = quarter_token(_dt.date.today())
    try:
        hits = candidates(DriveClient(creds), d["grandparent_id"], token,
                          exclude={d["word_tree_id"]})
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    if len(hits) == 1 and hits[0]["id"] == d["podcast_tree_id"]:
        return True, f"{token} → {hits[0]['name']!r}"
    if not hits:
        return False, (f"{token} 找不到對應的 podcast 樹（本季那棵可能還沒建）。"
                       f"目前仍指著 {d['podcast_tree_name']!r}")
    if len(hits) > 1:
        return False, (f"{token} 有 {len(hits)} 個候選，無法自動判定："
                       + "、".join(repr(h["name"]) for h in hits))
    return False, (f"{token} 應該用 {hits[0]['name']!r}，"
                   f"但 config.json 仍指著 {d['podcast_tree_name']!r}")


def check_drive(creds, cfg) -> tuple[bool, str]:
    from core.drive_client import DriveClient

    d = DriveClient(creds)
    try:
        for key, name in (("word_tree_id", "Word 樹"), ("podcast_tree_id", "Podcast 樹")):
            fid = cfg["drive"][key]
            meta = d.svc.files().get(
                fileId=fid, fields="id,name,capabilities/canAddChildren",
                supportsAllDrives=True,
            ).execute()
            if not meta.get("capabilities", {}).get("canAddChildren"):
                return False, f"{name}『{meta.get('name')}』已無寫入權限"
        return True, "兩棵樹皆可寫入"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def check_pipeline(cfg) -> tuple[list[str], list[str], str | None]:
    """回傳 (卡住的, 失敗的, 最後活動時間)。"""
    rows = cp.load_all(cfg["local"]["state_dir"])
    stuck, failed, latest = [], [], None

    threshold = dt.timedelta(hours=cfg["readiness"].get("give_up_after_hours", 72))
    now = dt.datetime.now()

    for c in rows:
        upd = c.data.get("updated_at")
        if upd and (latest is None or upd > latest):
            latest = upd
        if c.complete:
            continue

        # 就緒過了卻遲遲沒完成 → 卡住
        st = c.data["stages"].get("readiness") or {}
        if st.get("done") and st.get("at"):
            try:
                age = now - dt.datetime.fromisoformat(st["at"])
                if age > threshold:
                    nxt = c.next_stage()
                    stuck.append(f"{c.ticker} {c.quarter}：已就緒 {age.days} 天仍卡在"
                                 f"『{cp.STAGE_LABEL[nxt]}』" if nxt else f"{c.ticker}")
            except ValueError:
                pass

        for k, s in c.data["stages"].items():
            if s.get("done") is False and (s.get("detail") or {}).get("error"):
                failed.append(f"{c.ticker} {c.quarter} / {cp.STAGE_LABEL.get(k, k)}："
                              f"{(s['detail']['error'])[:120]}")
    return stuck, failed, latest


def _print_scope_fix() -> None:
    print("    取得 gmail.send 的步驟（啟用 Gmail API 只是其中一步，不夠）：")
    print("      1. Cloud Console →『OAuth 同意畫面』／新版『Google 驗證平台 → 資料存取』")
    print("      2. 點『新增或移除範圍』，搜尋 gmail.send，勾選")
    print("         https://www.googleapis.com/auth/gmail.send")
    print("      3. 儲存後重跑 tools/setup_oauth.py，同意畫面上四項權限都要勾")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true", help="寄一封測試信驗證管道")
    args = ap.parse_args()

    cfg = load_config()
    problems: list[tuple[str, str, str]] = []   # (key, subject, body)
    lines: list[str] = []

    # ---------- 0. 環境自述 ----------
    # 排程環境與互動式 shell 的差異踩過一次（LOCALAPPDATA 不同導致 token_missing），
    # 所以每次都把實際用到的路徑印出來，出事一眼看得到。
    from core.gauth import SECRET_DIR, SECRET_DIR_SOURCE, TOKEN_PATH
    import os as _os
    lines.append(f"祕密目錄            {SECRET_DIR}")
    lines.append(f"  來源              {SECRET_DIR_SOURCE}")
    lines.append(f"  token 存在        {TOKEN_PATH.exists()}")
    lines.append(f"  LOCALAPPDATA      {_os.environ.get('LOCALAPPDATA', '(未設定)')}")
    lines.append(f"  cwd               {_os.getcwd()}")

    # ---------- 1. Google OAuth ----------
    st = auth_status()
    lines.append(f"Google OAuth        {'OK' if st['ok'] else 'FAIL'}  {st['reason']}")
    if not st["ok"]:
        problems.append((
            "auth_expired", "Google 憑證失效",
            format_report("Google OAuth 憑證無法使用", [
                f"原因：{st['reason']}", "",
                "影響：歸檔、讀寫追蹤表、Gmail 告警全部停擺。",
                "處理：在有畫面的環境執行",
                f'  python "{Path(__file__).resolve().with_name("setup_oauth.py")}"',
            ])))
        # 憑證掛了就寄不出信，只能記錄
        print("\n".join(lines))
        print("\n[X] Google 憑證失效，無法寄送告警。")
        return 1

    creds = load_credentials(interactive=False)
    alerter = Alerter(creds, cfg)
    lines.append(f"Gmail 寄信權限       "
                 f"{'OK（Gmail API 直送）' if alerter.available else '降級（走 claude CLI + Gmail MCP，CLI 掛掉時一起失效）'}")

    if args.test:
        sent = alerter.send("test", "測試通知", format_report(
            "這是一則測試通知", ["若你收到這則通知，代表告警管道正常。"]), force=True)
        if sent and alerter.pushed:
            print(f"[OK] 測試通知已**送達** → 管道：{'、'.join(alerter.channels)}")
            return 0
        if sent:
            print(f"[!] 只建立了 Gmail **草稿**（收件者 {cfg['alerts']['email_to']}）。")
            print("    這個 Gmail MCP 只有 create_draft／update_draft，沒有寄送工具，")
            print("    草稿**不會推播到手機**，不能當成真正的告警。")
            print("    要真的收得到通知，仍須取得 gmail.send（見下方）。")
            _print_scope_fix()
            return 1
        print("[X] 測試信**沒有**送出，草稿也沒建成。")
        _print_scope_fix()
        return 1

    alerter.clear("auth_expired")

    # ---------- 2. claude CLI ----------
    ok, detail = check_claude_cli()
    lines.append(f"claude CLI          {'OK' if ok else 'FAIL'}  {detail}")
    if ok:
        alerter.clear("claude_cli_down")
    else:
        problems.append((
            "claude_cli_down", "claude CLI 無法使用",
            format_report("claude CLI 無法執行", [
                f"detail：{detail}", "",
                "影響：日期核對 agent 與 memo 派工全部停擺（歸檔不受影響）。",
                "處理：在互動式終端機執行 claude 並 /login。",
            ])))

    # ---------- 2b. NotebookLM 授權（podcast 產製） ----------
    # 只有走 CLI 產 podcast 時才是阻塞條件；manual 模式是人工做，不必查。
    if cfg.get("run", {}).get("podcast_mode") == "cli":
        ok, detail = check_notebooklm()
        lines.append(f"NotebookLM 授權      {'OK' if ok else 'FAIL'}  {detail}")
        if ok:
            alerter.clear("notebooklm_expired")
        else:
            problems.append((
                "notebooklm_expired", "NotebookLM 授權失效",
                format_report("NotebookLM CLI 無法取得 token", [
                    f"detail：{detail}", "",
                    "影響：podcast 產不出來。memo 仍會照常產出與歸檔，",
                    "但沒有音檔就不會推 LINE —— 這個失敗不會讓排程回傳非零，",
                    "所以不查就完全看不出來。",
                    "",
                    "處理：在有畫面的環境執行一次（會開瀏覽器登入 Google）",
                    "  notebooklm login",
                    "登入後 MeiguAuthMon 排程會每 10 分鐘保溫一次，正常不會再過期。",
                ])))
    else:
        lines.append(f"NotebookLM 授權      略過（podcast_mode="
                     f"{cfg.get('run', {}).get('podcast_mode')}）")

    # ---------- 3. Drive 權限 ----------
    ok, detail = check_drive(creds, cfg)
    lines.append(f"Drive 寫入權限       {'OK' if ok else 'FAIL'}  {detail}")
    if ok:
        alerter.clear("drive_access_lost")
    else:
        problems.append((
            "drive_access_lost", "Drive 寫入權限異常",
            format_report("Drive 樹無法寫入", [
                f"detail：{detail}", "",
                "影響：memo 分頁與 podcast 上傳失敗。",
                "備註：兩棵樹的擁有者是 owner@example.com，本帳號僅為協作者，"
                "對方調整共享設定就會發生這件事。",
            ])))

    # ---------- 3a. 本季的 podcast 樹 ----------
    ok, detail = check_podcast_tree(creds, cfg)
    lines.append(f"本季 podcast 樹      {'OK' if ok else 'FAIL'}  {detail}")
    if ok:
        alerter.clear("podcast_tree_stale")
    else:
        problems.append((
            "podcast_tree_stale", "podcast 樹指向本季以外",
            format_report("config.json 的 podcast_tree_id 不是本季那一棵", [
                f"detail：{detail}", "",
                "影響：音檔會被歸檔到上一季的樹底下。不會有任何錯誤 ——",
                "資料夾存在、上傳成功、追蹤表照樣打勾。",
                "",
                "處理：確認本季那棵樹的名稱後，執行",
                "  python tools/resolve_podcast_tree.py --write",
                "  python tools/build_drive_map.py",
                "命名認得出來的話，每日的 MeiguDates 會自己更正，不必人工介入。",
            ])))

    # ---------- 3b. LINE 推播管道 ----------
    ok, detail = check_line(cfg)
    lines.append(f"LINE 推播           {'OK' if ok else 'FAIL'}  {detail}")
    if ok:
        alerter.clear("line_unavailable")
    else:
        problems.append((
            "line_unavailable", "LINE 推播管道異常",
            format_report("LINE 推播送不出去", [
                f"detail：{detail}", "",
                "影響：memo 與 podcast 仍會照常產出歸檔，但群組收不到通知。",
                "額度不足的話要等下個月重置，或到 LINE Official Account Manager 升級方案。",
                "處理：python tools/setup_line.py --check",
            ])))

    # ---------- 3c. 授權監控本身還活著嗎 ----------
    # auth_monitor 是「即時知道授權狀態」的資料源。它自己停了的話，
    # 看板會一直顯示最後一次的綠燈 —— 必須有人監督監控者。
    am_path = Path(cfg["local"]["state_dir"]) / "auth_status.json"
    am_problem = None
    if not am_path.exists():
        am_problem = "state/auth_status.json 不存在（MeiguAuthMon 沒跑過？）"
    else:
        try:
            checked = dt.datetime.fromisoformat(
                json.loads(am_path.read_text(encoding="utf-8"))["checked_at"])
            idle_m = int((dt.datetime.now() - checked).total_seconds() // 60)
            if idle_m > 65:
                am_problem = f"最後更新是 {idle_m} 分鐘前（排程是每 10 分鐘）"
        except Exception as exc:
            am_problem = f"讀取失敗：{type(exc).__name__}: {exc}"
    lines.append(f"授權監控            {'FAIL  ' + am_problem if am_problem else 'OK  10 分鐘一輪更新中'}")
    if am_problem:
        problems.append((
            "authmon_stale", "授權監控停擺",
            format_report("auth_monitor 沒有在更新狀態", [
                f"detail：{am_problem}", "",
                "影響：看板上的授權燈號停留在最後一次的結果，授權掛掉不會被發現。",
                "處理：確認工作排程器的 MeiguAuthMon 存在且為 Ready；",
                "手動驗證：python tools/auth_monitor.py --dry-run",
            ])))
    else:
        alerter.clear("authmon_stale")

    # ---------- 4. 流程健康 ----------
    stuck, failed, latest = check_pipeline(cfg)
    lines.append(f"檢查點最後更新       {latest or '（無任何檢查點）'}")
    lines.append(f"卡住 {len(stuck)} 檔／失敗 {len(failed)} 檔")

    if stuck:
        problems.append(("stuck_readiness", f"{len(stuck)} 檔卡在流程中",
                         format_report("以下 ticker 已就緒但長時間未完成", stuck)))
    else:
        alerter.clear("stuck_readiness")

    if failed:
        problems.append(("run_failed_final", f"{len(failed)} 檔執行失敗",
                         format_report("以下階段失敗", failed)))
    else:
        alerter.clear("run_failed_final")

    if latest:
        try:
            idle = dt.datetime.now() - dt.datetime.fromisoformat(latest)
            if idle > dt.timedelta(hours=24):
                problems.append((
                    "no_activity_24h", "24 小時沒有任何動靜",
                    format_report("排程器可能已停止", [
                        f"最後一次檢查點更新：{latest}（{idle.days} 天 {idle.seconds // 3600} 小時前）",
                        "", "影響：整條線停擺且無人察覺。",
                        "處理：確認 Windows 工作排程器的兩個任務仍在執行。",
                    ])))
            else:
                alerter.clear("no_activity_24h")
        except ValueError:
            pass

    print("\n".join(lines))

    if not problems:
        print("\n[OK] 全部正常，未寄送告警。")
        return 0

    print(f"\n發現 {len(problems)} 項問題：")
    for key, subject, body in problems:
        if args.dry_run:
            print(f"  [dry-run] 會寄：{subject}")
            continue
        try:
            sent = alerter.send(key, subject, body)
            print(f"  {'已寄出' if sent else '冷卻中未重複寄送'}：{subject}")
        except PermissionError as exc:
            # 寄不出去也要把問題印出來，不要因為告警管道壞了就整個安靜下來
            print(f"  [X] 無法寄送：{subject}\n      {exc}")
            print(f"      內容：\n{body}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
