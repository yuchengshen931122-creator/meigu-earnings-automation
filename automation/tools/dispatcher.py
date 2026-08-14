"""排程器 —— 讀表、比時間、跑就緒偵測、派工。

架構刻意不是「三個 agent 互相通知」：

    [Windows 工作排程器 每 30 分鐘] → dispatcher --once
         ↓ 讀追蹤表（唯一狀態源）
    挑出「法說會時間已過、狀態未完成」的 ticker
         ↓
    就緒偵測（新聞稿 ✓ + 逐字稿 ✓）
         ↓ dispatchable
    同一個 run 內循序做完：產 memo/podcast → 上傳 → 開分頁 → 回寫表

跨 agent 通知在這裡沒有好處，只多一種掉訊息的失敗模式。
任何一步掛掉，表上那一列就停在 failed + 原因，下一輪自動重試；
斷電、關機、當機都能自動接續。

用法：
    python tools/dispatcher.py --once --dry-run        # 只看就緒狀態，不派工
    python tools/dispatcher.py --once                  # 跑一輪
    python tools/dispatcher.py --once --offline --queue state/manual_queue.json
"""

import _bootstrap  # noqa: F401

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from core import checkpoint as cp
from core import notify, quarters, sources
from core.claude_cli import cli_env
from core.edgar import Edgar
from core.gauth import load_config
from core.readiness import GIVE_UP, NOT_YET, READY, READY_PARTIAL, WAIT, ReadinessChecker

# 追蹤表 auto_status 欄的值
ST_PENDING = ""
ST_WAITING = "waiting"
ST_READY = "ready"
ST_RUNNING = "running"
ST_DONE = "done"
ST_FAILED = "failed"
ST_SKIPPED = "skipped"
TERMINAL = {ST_DONE, ST_SKIPPED}


@dataclass
class Job:
    ticker: str
    row: int
    call_at: str
    tab_name: str
    quarter_label: str
    readiness_state: str
    readiness_reason: str
    press_release: str
    transcript: str
    fy_warning: str | None = None
    ready_at: str = ""       # 判定就緒的時間 —— 排序主鍵
    resuming: bool = False   # 上一輪開工到一半沒做完，這輪接手
    industry: str = ""       # 追蹤表「產業」欄 —— 新 ticker 建 Drive 落點的分類依據


def _make_job(t, tab_name, quarters_mod, state, reason, pr, tr, ready_at="") -> Job:
    return Job(
        ticker=t.ticker, row=t.row, call_at=t.call_at.isoformat(),
        tab_name=tab_name,
        quarter_label=quarters_mod.local_folder_label(t.call_at.date()),
        readiness_state=state, readiness_reason=reason,
        press_release=pr, transcript=tr,
        fy_warning=quarters_mod.fy_warning(t.ticker),
        ready_at=ready_at,
        industry=(getattr(t, "industry", "") or "").strip(),
    )


# 多檔並行後，這三樣是唯一的共用資源，各上一把鎖：
#   journal   同一個檔案多執行緒 append，交錯會寫出壞掉的 JSON 行
#   sheets    googleapiclient 的 service 物件（底下是 httplib2）不保證執行緒安全
#   stdout    不鎖的話三檔的輸出會逐字交錯，log 直接沒法看
_JOURNAL_LOCK = threading.Lock()
_SHEET_LOCK = threading.Lock()


def journal(cfg: dict, event: dict) -> None:
    p = Path(cfg["local"]["state_dir"]) / "dispatch_journal.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    event["ts"] = dt.datetime.now().isoformat(timespec="seconds")
    with _JOURNAL_LOCK, p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def write_status(sheets, *args, **kwargs) -> None:
    """追蹤表寫入的唯一入口 —— 併行時必須序列化。"""
    if not sheets:
        return
    with _SHEET_LOCK:
        sheets.write_status(*args, **kwargs)


class _TaggedStdout:
    """把每一行標上 ticker，並確保一行不會被另一條執行緒插斷。

    刻意不緩衝到工作結束才吐 —— 一檔要跑 20 到 40 分鐘，緩衝的話
    log 會有半小時完全空白，出事時看不出跑到哪裡。逐行標記才能一邊跑一邊追。
    """

    def __init__(self, base):
        self._base = base
        self._lock = threading.Lock()
        self._local = threading.local()
        self._buf = {}

    def set_tag(self, tag: str | None) -> None:
        self._local.tag = tag

    @property
    def _tag(self) -> str | None:
        return getattr(self._local, "tag", None)

    def write(self, s: str) -> int:
        tag = self._tag
        if not tag:
            with self._lock:
                return self._base.write(s)
        key = threading.get_ident()
        with self._lock:
            pending = self._buf.get(key, "") + s
            lines = pending.split("\n")
            self._buf[key] = lines.pop()          # 最後一段還沒收到換行，留著
            for ln in lines:
                self._base.write(f"[{tag}] {ln}\n")
            return len(s)

    def flush(self) -> None:
        with self._lock:
            self._base.flush()

    def __getattr__(self, name):
        return getattr(self._base, name)


def load_offline_queue(path: str) -> list:
    """離線測試用：[{ticker, call_at:'YYYY-MM-DDTHH:MM', row}]"""
    from core.sheets_client import Task

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Task(row=r.get("row", i + 1), ticker=r["ticker"].upper(), raw_ticker=r["ticker"],
             call_at=dt.datetime.fromisoformat(r["call_at"]), status=r.get("status", ""))
        for i, r in enumerate(raw)
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", default=True)
    ap.add_argument("--dry-run", action="store_true", help="只做就緒偵測，不派工不寫表")
    ap.add_argument("--offline", action="store_true", help="不連 Google")
    ap.add_argument("--queue", default=None, help="離線佇列 JSON")
    ap.add_argument("--now", default=None, help="覆寫現在時間（測試用）YYYY-MM-DDTHH:MM")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--execute", action="store_true",
                    help="真的呼叫 claude CLI 產 memo/podcast（否則只產任務單）")
    args = ap.parse_args()

    cfg = load_config()
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now()
    edgar = Edgar(cfg["local"]["state_dir"])
    checker = ReadinessChecker(edgar, cfg)

    # ---------- 讀任務 ----------
    sheets = None
    if args.offline or args.queue:
        if not args.queue:
            print("[X] --offline 需要搭配 --queue")
            return 1
        tasks = load_offline_queue(args.queue)
    else:
        from core.gauth import load_credentials
        from core.sheets_client import SheetsClient

        sheets = SheetsClient(load_credentials(interactive=False), cfg)
        tasks = sheets.load_tasks(year=now.year)

    print(f"現在時間 {now:%Y-%m-%d %H:%M}（台灣）；讀到 {len(tasks)} 列\n")

    # ---------- 篩出候選 ----------
    candidates = []
    counts = {"no_date": 0, "terminal": 0, "future": 0, "human_done": 0}
    for t in tasks:
        if t.skip_reason or t.call_at is None:
            counts["no_date"] += 1
            continue
        if (t.status or "") in TERMINAL:
            counts["terminal"] += 1
            continue
        # 團隊 5 個人也在做同一張表。「報告」欄已是 TRUE 就不該再派工，
        # 否則會重做別人做完的東西（實測：81 檔候選裡絕大多數屬於此類）。
        if not t.needs_memo:
            counts["human_done"] += 1
            continue
        if t.call_at > now:
            counts["future"] += 1
            continue
        candidates.append(t)

    print(f"候選 {len(candidates)} 檔"
          f"（跳過：無日期 {counts['no_date']}、報告已完成 {counts['human_done']}、"
          f"auto_status 已終結 {counts['terminal']}、尚未開始 {counts['future']}）\n")
    if args.limit:
        candidates = candidates[: args.limit]

    # ---------- 就緒偵測 ----------
    jobs: list[Job] = []
    sd = cfg["local"]["state_dir"]
    tmpl = cfg["memo_output"]["tab_name_template"]
    skipped_by_ckpt = 0
    gave_up: list[str] = []
    transcript_overdue: list[str] = []

    def push_status(t, status: str, error: str = "") -> None:
        """狀態欄回寫。表是 5 個人共看的，狀態一有變化就要當下寫回，不能等下一輪；
        本輪看過但狀態沒變的也要寫，auto_checked_at 才代表『真的在這個時間看過』。"""
        if sheets and not args.dry_run:
            write_status(sheets, t, status=status, error=error)

    for t in candidates:
        tab = quarters.tab_name(t.ticker, t.call_at.date(), tmpl,
                               fy_end=edgar.fiscal_year_end(t.ticker))
        ck = cp.load(sd, t.ticker, tab.split(" ", 1)[-1])

        # 檢查點：整條線走完的直接跳過，不必再打 SEC / Fool
        if ck.complete:
            skipped_by_ckpt += 1
            continue
        # 重試到上限後放棄的：不再自動撿起來，否則會每輪重試一次直到永遠。
        # 要它重來就把該檔的 checkpoint 刪掉。
        if ck.detail("memo_started").get("gave_up"):
            gave_up.append(t.ticker)
            push_status(t, ST_FAILED, (ck.detail("memo_started").get("error") or "")[:400])
            continue
        # 已確認就緒過就不重查 —— 就緒是單向的，出現過的資料不會消失
        if ck.is_done("readiness"):
            d = ck.detail("readiness")
            mark = "（製作中，本輪接手）" if ck.in_progress else ""
            print(f"{t.ticker:6s} [checkpoint] 已就緒（{d.get('state')}），跳過重查{mark}")
            # 這條路徑原本直接 continue，狀態欄完全沒寫 —— 一檔判到就緒之後，
            # auto_checked_at 就停在那一刻，看起來像是沒人在管它。
            push_status(t, ST_RUNNING if ck.in_progress else ST_READY)
            j = _make_job(t, tab, quarters, d.get("state", READY),
                          d.get("reason", ""), d.get("press_release", ""),
                          d.get("transcript", ""), ready_at=ck.at("readiness"))
            j.resuming = ck.in_progress
            jobs.append(j)
            continue

        # 走到這裡表示表上已經有可用的日期時間 —— 那就是「日期已確認」。
        # 沒有這一步，date_confirmed 永遠不會被標記，看板會把每一檔都算成
        # 卡在階段 1，而且 ck.complete 永遠不成立（整條線的跳過邏輯失效）。
        if not ck.is_done("date_confirmed"):
            ck.mark("date_confirmed", call_at=t.call_at.isoformat(), source="追蹤表")

        r = checker.check(t.ticker, t.call_at, now=now)
        print(r.as_row())
        # 逐字稿等超過 24 小時 → 收集起來推一次 LINE。逃生門關閉後
        # （transcript_soft_cutoff_hours=null）這是唯一的「等太久」訊號：
        # 冷門股可能永遠沒有免費逐字稿，要不要放行是人的決定，不能無聲空等。
        # 只在「跨過門檻的那半小時」內收集，天然去重，不會每輪都轟。
        if (r.state == WAIT and r.press_release.ok
                and dt.timedelta(hours=24) <= now - t.call_at < dt.timedelta(hours=24.5)):
            transcript_overdue.append(f"{t.ticker}（{tab}）：{r.reason[:100]}")
        if r.dispatchable:
            ck.mark("readiness", state=r.state, reason=r.reason,
                    press_release=r.press_release.detail[:200],
                    transcript=r.transcript.detail[:200])

        new_status = {
            READY: ST_READY, READY_PARTIAL: ST_READY,
            WAIT: ST_WAITING, NOT_YET: ST_WAITING, GIVE_UP: ST_FAILED,
        }[r.state]

        if r.dispatchable:
            jobs.append(_make_job(t, tab, quarters, r.state, r.reason,
                                  r.press_release.detail[:200],
                                  r.transcript.detail[:200],
                                  ready_at=ck.at("readiness") or now.isoformat()))

        push_status(t, new_status, "" if r.state != GIVE_UP else r.reason)
        journal(cfg, {"ticker": t.ticker, "state": r.state, "reason": r.reason})

    # ---------- 逐字稿等太久的提醒 ----------
    if transcript_overdue and not args.dry_run:
        try:
            from core.alerts import Alerter, format_report
            from core.gauth import load_credentials as _lc
            try:
                _creds = _lc(interactive=False)
            except Exception:
                _creds = None
            Alerter(_creds, cfg).send(
                "transcript_overdue", f"{len(transcript_overdue)} 檔等逐字稿超過 24 小時",
                format_report("以下財報的新聞稿早已確認，但逐字稿仍未公開", [
                    *transcript_overdue, "",
                    "目前政策：逐字稿沒出來就不開工（transcript_soft_cutoff_hours=null）。",
                    "冷門股可能永遠沒有免費逐字稿。若要對個別檔放行改用新聞稿先做，",
                    "把 config.json 的 transcript_soft_cutoff_hours 暫時設回數字（小時），",
                    "跑完該檔再改回 null；或人工在桌面 App 產出後丟進 MEMO/PODCAST 資料夾。",
                ]))
        except Exception as exc:
            print(f"[!] 逐字稿逾時提醒送出失敗：{type(exc).__name__}: {exc}")

    # ---------- 排序：就緒時間早的先 ----------
    # 同一輪一起判到就緒的會差幾秒（迴圈順序），用表上列序打破平手，
    # 讓順序在不同輪次之間穩定 —— 否則同分項目的先後會隨機漂移。
    jobs.sort(key=lambda j: (j.ready_at or "9999", j.row))

    # ---------- 接手：未收尾的照樣排進來，不再擋住其他檔 ----------
    # 2026-08-12 改：原本只要有一檔處於「製作中」，整輪就只做它（jobs = stuck[:1]），
    # 別檔就算資料早就齊了也得排隊。使用者要的是每檔各自獨立、就緒即開工，
    # 所以接手的只是「排在前面」，不再是「其他人都不准動」。
    stuck = [j for j in jobs if j.resuming]
    if stuck:
        print(f"\n[接手] {len(stuck)} 檔上一輪未收尾，本輪一併接手："
              f"{'、'.join(j.ticker for j in stuck)}")

    # ---------- 派工 ----------
    if skipped_by_ckpt:
        print(f"\n[checkpoint] {skipped_by_ckpt} 檔已整條線完成，未重查")
    if gave_up:
        print(f"[checkpoint] {len(gave_up)} 檔重試到上限後放棄，需人工處理："
              f"{'、'.join(gave_up)}")
    print(f"\n可派工 {len(jobs)} 檔（依就緒時間排序，就緒即開工）")
    for j in jobs:
        print(f"   {j.ticker:6s} → 分頁『{j.tab_name}』  本機季度資料夾 {j.quarter_label}"
              f"  [{j.readiness_state}]  就緒於 {j.ready_at or '—'}"
              + ("  ← 接手" if j.resuming else ""))
        if j.fy_warning:
            print(f"          [!] {j.fy_warning}")

    qpath = Path(cfg["local"]["state_dir"]) / "jobs.json"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_text(
        json.dumps([asdict(j) for j in jobs], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n任務單：{qpath}")

    if args.dry_run or not args.execute:
        print("（未執行。要真的產 memo/podcast 請加 --execute）")
        return 0

    # ---------- 併行執行：每檔各自獨立，就緒即開工 ----------
    # 2026-08-12 改（原本是一檔做完才做下一檔）。實測 08-12 那輪：CRWV / SMCI /
    # LITE / ASTS 四檔序列跑了 1 小時 45 分，而每一檔的瓶頸都是在等外部服務
    # （claude agent 約 20 分、NotebookLM 約 7 分），CPU 幾乎沒在動 —— 排隊純屬浪費。
    #
    # 用執行緒而非多程序：每一檔的實作本來就是一連串 subprocess.run，
    # 阻塞在 I/O 上，GIL 不構成瓶頸；共用的 sheets / journal / stdout 上鎖即可。
    #
    # 為什麼仍要設上限：claude CLI 的用量額度是全帳號共用的，開越多消耗越快
    # （08-12 早上就撞過 session limit）；NotebookLM 同時生成多集也會變慢。
    # max_parallel 就是這個節流閥，設 1 即回到原本的序列行為。
    if not jobs:
        # 絕大多數輪次都是這條路（一天 96 輪，有東西可做的沒幾輪）。
        # 不早退的話，每一輪都會印出併行標題與空的「本輪結果：」洗版。
        return 0

    budget = dt.timedelta(minutes=cfg["run"].get("run_budget_minutes", 240))
    per_job = dt.timedelta(seconds=cfg["run"].get("timeout_seconds", 5400))
    parallel = max(1, int(cfg["run"].get("max_parallel", 3)))
    started = dt.datetime.now()
    quota_hit = threading.Event()

    tagged = _TaggedStdout(sys.stdout)
    sys.stdout = tagged

    def run_one(j: Job) -> tuple[str, str]:
        tagged.set_tag(j.ticker)
        try:
            # 配額用完之後才輪到的：不必白跑，下一輪自然會重來
            if quota_hit.is_set():
                print("Claude 額度已用完，本檔留到下一輪")
                return j.ticker, "quota_skipped"
            if dt.datetime.now() - started + per_job > budget:
                print("本輪預算放不下一整檔，留到下一輪")
                return j.ticker, "budget_skipped"
            _execute(cfg, j, sheets)
            return j.ticker, "done"
        except QuotaExhausted as exc:
            quota_hit.set()
            print(f"[!] Claude 用量上限（{exc}）—— 本檔與尚未開始的都留到下一輪")
            return j.ticker, "quota"
        except Exception as exc:      # 一檔炸掉不能拖垮其他檔
            journal(cfg, {"ticker": j.ticker, "event": "worker_crash",
                          "error": f"{type(exc).__name__}: {exc}"[:300]})
            print(f"[X] 未預期的例外：{type(exc).__name__}: {exc}")
            return j.ticker, "crash"
        finally:
            tagged.set_tag(None)

    print(f"\n>>> 併行執行 {len(jobs)} 檔（同時最多 {parallel} 檔）…\n")
    try:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            results = list(pool.map(run_one, jobs))
    finally:
        sys.stdout = tagged._base

    tally: dict[str, list[str]] = {}
    for tk, outcome in results:
        tally.setdefault(outcome, []).append(tk)
    print("\n本輪結果：" + "　".join(
        f"{k} {len(v)} 檔（{'、'.join(v)}）" for k, v in sorted(tally.items())))
    return 0


MEMO_AGENT = "earnings-memo-runner"
VERIFY_AGENT = "earnings-memo-verifier"
BUILD_MEMO = r"%USERPROFILE%\.claude\skills\earnings-memo-auto\scripts\build_memo.py"
PROJECT_DIR = str(Path(__file__).resolve().parent.parent.parent)

# Claude 用量上限：agent 兩三秒就 rc=1 退出，訊息長這樣
#   "You've hit your session limit · resets 10:20am (Asia/Taipei)"
# 這**不是失敗**，是「還不能開始」。分開處理的三個理由：
#   1. 表是 5 個人共用的，寫 failed 會讓人以為這檔做壞了，其實一行都還沒跑
#   2. 檢查點不該把 memo_built 記成失敗 —— 失敗的不是 memo，是根本沒開始
#   3. 配額用完時同一輪剩下的 job 必定也失敗，白跑還多洗一次表
QUOTA = re.compile(r"(session limit|usage limit|rate limit|quota|resets \d{1,2}[:.]?\d{0,2}\s*[ap]m)", re.I)
RESET_AT = re.compile(r"resets\s+([^\n·]{1,40})", re.I)


class QuotaExhausted(RuntimeError):
    """配額用完 —— 本輪別再派了，下一輪自動重試。"""


def _execute(cfg: dict, job: Job, sheets) -> None:
    """派給 earnings-memo-runner agent 產出，再呼叫 publish_memo 歸檔。

    產出規則、無人值守準則、輸出契約都寫在 agent 定義裡
    （.claude/agents/earnings-memo-runner.md），這裡只給這一次的任務資料。
    """
    json_out, docx_out = _memo_paths(cfg, job)
    prompt = (
        f"標的：{job.ticker}  季度：{job.quarter_label}\n"
        f"就緒狀態：{job.readiness_state}（{job.readiness_reason}）\n"
        f"財報新聞稿依據：{job.press_release}\n"
        f"逐字稿線索：{job.transcript or '本層未確認，請自行取得'}\n"
        # 檔名寫死給它。放著讓 agent 自己取，非曆年財政年度的公司會取成
        # 「SMCI 4Q26FY.docx」而同一次的 JSON 卻是 smci_2q26.json，兩套混用。
        f"\n輸出檔名請完全照這兩個路徑，不要自行改用其他季度標籤：\n"
        f"  JSON：{json_out}\n"
        f"  docx：{docx_out}\n"
        + (f"注意：{job.fy_warning}\n" if job.fy_warning else "")
        + ("\npodcast 模式：manual —— 只產 memo，跳過 skill 的 Step 6。\n"
           if cfg.get("run", {}).get("podcast_mode", "manual") == "manual" else "")
    )
    cmd = ["claude", "--agent", MEMO_AGENT, "-p", prompt]
    if cfg.get("run", {}).get("skip_permissions"):
        # 全自動的必要條件：無人值守時不能停在權限詢問。
        # 這個 agent 需要 Bash / Write / 瀏覽器，範圍比日期核對 agent 大得多，
        # 開啟前請確認你接受這個範圍。
        cmd.append("--dangerously-skip-permissions")

    ck = cp.load(cfg["local"]["state_dir"], job.ticker, job.tab_name.split(" ", 1)[-1])

    def finish(status: str, err: str = "", report_done=None, retry=None,
               podcast_done=None) -> None:
        if not sheets:
            return
        from core.sheets_client import Task
        t = Task(row=job.row, ticker=job.ticker, raw_ticker=job.ticker,
                 call_at=dt.datetime.fromisoformat(job.call_at))
        # podcast_done 只在「這個 run 已把音檔上傳 Drive」時才設 True（cli 模式
        # 一條龍那條路）。manual 模式音檔還沒產，維持 None 不動勾 ——
        # 先勾會讓 sweep_podcasts 以為做完而永遠跳過，音檔就再也不會上傳。
        # 2026-08-13 教訓：cli 模式下這裡不勾就沒有人會勾（sweep 看到
        # podcast_uploaded 已 done 直接跳過），COHR/CSCO 表上因此漏勾。
        write_status(sheets, t, status=status, error=err[:400],
                     report_done=report_done, podcast_done=podcast_done,
                     owner="agent", retry=retry)

    # ---- 0. 歸檔續跑短路 ----
    # memo 已產出且檔案還在，就不再叫 agent —— 歸檔失敗的重試只該重跑歸檔。
    # 實戰教訓（2026-08-13 CBRS）：publish 因 Drive 落點失敗，下一輪把 20 分鐘的
    # memo agent 整檔重跑了一次，額度白燒、內容還變版。
    quarter = job.quarter_label
    json_path, docx_path = _memo_paths(cfg, job)
    resume_archive = ck.is_done("memo_built") and (docx_path.exists() or json_path.exists())

    if resume_archive:
        gaps = list(ck.detail("memo_built").get("gaps") or [])
        print(f"\n>>> {job.ticker}：memo 已產出（{ck.at('memo_built')}），跳過 agent、直接歸檔")
        journal(cfg, {"ticker": job.ticker, "event": "resume_archive"})
        finish(ST_RUNNING, "")
    else:
        # ---- 0b. 開工標記 ----
        # 標了才動手。之後每一輪只要看到「memo_started 已標、memo_built 未完成」，
        # 就知道有一檔開到一半沒收尾，該接手的是它而不是隊列裡的新人。
        # 配額用完那條路徑不會走到這裡（在下面才判），所以不會白佔一次重試。
        attempts = int(ck.detail("memo_started").get("attempts") or 0) + 1
        max_retries = int(cfg["run"].get("max_retries", 2))
        ck.mark("memo_started", attempts=attempts, pid=os.getpid(),
                started_at=dt.datetime.now().isoformat(timespec="seconds"))

        def fail_memo(msg: str) -> None:
            """memo 沒產出來。沒到重試上限就維持製作中，下一輪接手；到了就放行。"""
            ck.fail("memo_built", msg)
            if attempts >= max_retries:
                # 解除製作中，否則這一檔會永遠擋住後面所有人。
                # gave_up 讓候選階段直接跳過它 —— 不然下一輪又會被撿起來重試。
                ck.mark("memo_started", done=False, attempts=attempts,
                        gave_up=True, error=msg)
                print(f"    [X] {msg}（第 {attempts} 次，已達上限 {max_retries}，放棄並放行）")
                print("        要讓它重來：刪掉 "
                      f"state/checkpoints/{job.ticker}_{job.tab_name.split(' ', 1)[-1]}.json")
                finish(ST_FAILED, f"{msg}（重試 {attempts} 次後放棄）", retry=attempts)
            else:
                print(f"    [X] {msg}（第 {attempts} 次，維持製作中，下一輪接手）")
                finish(ST_FAILED, f"{msg}（第 {attempts} 次，下一輪自動接手）", retry=attempts)

        print(f"\n>>> 執行 {job.ticker}：agent={MEMO_AGENT} …"
              + (f"（接手，第 {attempts} 次）" if job.resuming else ""))
        journal(cfg, {"ticker": job.ticker, "event": "execute_start", "attempt": attempts})
        # 開工是一次真實的狀態變化，當下就要寫回。memo agent 最久要跑 90 分鐘，
        # 不寫的話這段期間表上一直顯示 ready，別人會以為沒人接手而重複做。
        # error 給空字串：這裡是開工不是出錯，順便把上一次失敗留下的訊息清掉。
        finish(ST_RUNNING, "", retry=attempts if attempts > 1 else None)

        # ---- 1. 產 memo ----
        try:
            # env=cli_env()：claude 主登入失效時自動帶備援 token（core/claude_cli.py）
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_DIR,
                                  encoding="utf-8", errors="replace", env=cli_env(),
                                  timeout=cfg["run"].get("timeout_seconds", 5400))
        except subprocess.TimeoutExpired:
            journal(cfg, {"ticker": job.ticker, "event": "timeout", "attempt": attempts})
            fail_memo("memo agent 逾時")
            return

        out = (proc.stdout or "")
        journal(cfg, {"ticker": job.ticker, "event": "agent_done",
                      "rc": proc.returncode, "tail": out[-1200:]})
        print(f"    agent rc={proc.returncode}")

        if proc.returncode != 0:
            blob = f"{out}\n{proc.stderr or ''}"
            if QUOTA.search(blob):
                m = RESET_AT.search(blob)
                when = f"，額度恢復時間 {m.group(1).strip()}" if m else ""
                journal(cfg, {"ticker": job.ticker, "event": "quota_blocked", "detail": blob[-200:]})
                print(f"    [!] Claude 用量上限{when} —— 不算失敗，維持 waiting")
                # 撤銷開工標記並退回這次的計次：配額用完跟這一檔的品質無關，
                # 不撤的話它會在 10 分鐘內把重試額度燒光，然後被永久標成 failed，
                # 而且會一直掛在「製作中」擋住所有人。
                ck.mark("memo_started", done=False, attempts=attempts - 1, quota_blocked=True)
                finish(ST_WAITING, f"Claude 用量上限，尚未開始{when}；配額恢復後自動重跑")
                raise QuotaExhausted(when.lstrip("，") or "額度恢復時間不明")
            fail_memo(f"memo agent rc={proc.returncode}: {(proc.stderr or out)[:200]}")
            return

        # ---- 2. 驗證產出真的存在 ----
        # rc==0 不代表有東西產出。agent 可能什麼都沒做就正常結束，
        # 若只憑 returncode 就標 done，表上會出現「完成但沒有檔案」的假訊號。
        res = _parse_agent_json(out)

        # agent 有時會自作主張用自己算的季度標籤命名（SMCI 產出過『SMCI 4Q26FY.docx』，
        # 同一次執行的 JSON 卻叫 smci_2q26.json）。命名一致由這裡保證，不靠 agent 自律：
        # 它報回來的檔案若不在正規路徑上，搬過去。
        for reported, canon in ((res.get("json_path"), json_path),
                                (res.get("docx_path"), docx_path)):
            if not reported:
                continue
            p = Path(reported)
            if p.exists() and p != canon:
                canon.parent.mkdir(parents=True, exist_ok=True)
                p.replace(canon)
                print(f"    [命名統一] {p.name} → {canon.name}")

        if not json_path.exists() and not docx_path.exists():
            fail_memo(f"agent 回報完成但找不到產出：{json_path.name} / {docx_path.name}")
            return

        gaps = res.get("gaps") or []
        ck.mark("memo_built", json=str(json_path) if json_path.exists() else None,
                docx=str(docx_path) if docx_path.exists() else None, gaps=gaps)
        print(f"    memo 產出確認：{json_path.name if json_path.exists() else docx_path.name}"
              + (f"　缺漏 {gaps}" if gaps else ""))
        finish(ST_RUNNING)   # 心跳：memo 已產出，還在跑 podcast/歸檔

    # ---- 2.4 驗證關卡：獨立 clean-context 逐數字回源，通過才准往下 ----
    # 2026-08-13 起。同 context 的自我檢查抓不到「數字對、掛載錯」——寫錯的那個
    # context 會再次認可自己的錯（CSCO/CBRS 人工抽查出七類系統性錯誤，Step 4
    # 自我重讀全數漏接）。改由另一個乾淨 context 的 agent 拿逐字稿逐條比對，
    # 錯的直接修 JSON 並重建 docx。必須排在 podcast 之前：podcast 的 focus
    # prompt 來自 JSON 的 tldr，驗證後的 JSON 才能餵給它。
    verify_mode = cfg.get("run", {}).get("verify_mode", "gate")
    if verify_mode != "off" and not ck.is_done("memo_verified"):
        vd = ck.detail("memo_verified")
        if vd.get("gave_up"):
            finish(ST_FAILED, f"驗證重試已達上限，需人工處理：{(vd.get('error') or '')[:200]}")
            return
        v_att = int(vd.get("attempts") or 0) + 1
        check_path = json_path.with_name(json_path.stem + "_check.md")
        # 靜態預檢（純程式）：發現的問題塞進 verifier 的派工單，讓它一併修。
        import verify_memo_static as vms
        pre_hard, pre_warn = vms.check(json_path, docx_path, check_path)
        for w in (pre_hard + pre_warn)[:6]:
            print(f"      [靜態預檢] {w}")
        static_note = ""
        if pre_hard or pre_warn:
            static_note = ("靜態檢查發現以下問題，請一併處理：\n  - "
                           + "\n  - ".join(pre_hard + pre_warn) + "\n")
        vprompt = (
            f"標的：{job.ticker}  分頁季度：{job.tab_name.split(' ', 1)[-1]}\n"
            f"memo JSON：{json_path}\n"
            f"memo docx：{docx_path}\n"
            f"驗證檔輸出：{check_path}\n"
            f"來源線索：\n  新聞稿：{job.press_release}\n  逐字稿：{job.transcript or '未確認，請自行取得'}\n"
            f"docx 重建指令：python \"{BUILD_MEMO}\" \"{json_path}\" \"{docx_path}\"\n"
            + static_note
        )
        vcmd = ["claude", "--agent", VERIFY_AGENT, "-p", vprompt]
        if cfg.get("run", {}).get("skip_permissions"):
            vcmd.append("--dangerously-skip-permissions")
        print(f"    驗證關卡（clean-context 逐數字回源，第 {v_att} 次）…")
        journal(cfg, {"ticker": job.ticker, "event": "verify_start", "attempt": v_att})
        finish(ST_RUNNING)
        try:
            vp = subprocess.run(vcmd, capture_output=True, text=True, cwd=PROJECT_DIR,
                                encoding="utf-8", errors="replace", env=cli_env(),
                                timeout=cfg["run"].get("verify_timeout_seconds", 2700))
        except subprocess.TimeoutExpired:
            journal(cfg, {"ticker": job.ticker, "event": "verify_timeout", "attempt": v_att})
            ck.mark("memo_verified", done=False, attempts=v_att, error="驗證 agent 逾時")
            finish(ST_FAILED, f"驗證 agent 逾時（第 {v_att} 次，下一輪重試）", retry=v_att)
            return
        vout = vp.stdout or ""
        vres = _parse_agent_json(vout)
        journal(cfg, {"ticker": job.ticker, "event": "verify_done", "rc": vp.returncode,
                      "ok": vres.get("ok"), "tail": vout[-600:]})
        if vp.returncode != 0 and QUOTA.search(f"{vout}\n{vp.stderr or ''}"):
            m = RESET_AT.search(f"{vout}\n{vp.stderr or ''}")
            when = f"，額度恢復時間 {m.group(1).strip()}" if m else ""
            print(f"    [!] Claude 用量上限（驗證階段）{when} —— 不算失敗，維持 waiting")
            # 不計入重試次數：配額跟這一檔的品質無關（同 memo 階段的處理）。
            ck.mark("memo_verified", done=False, attempts=v_att - 1, quota_blocked=True)
            finish(ST_WAITING, f"Claude 用量上限（驗證階段）{when}；恢復後自動重跑")
            raise QuotaExhausted(when.lstrip("，") or "額度恢復時間不明")
        # 靜態後檢（硬性）：verifier 說 ok 也要過這關 —— 例如它改了 JSON 卻忘了
        # 重建 docx、或沒寫驗證檔，模型騙不過確定性檢查。
        post_hard: list[str] = []
        if vp.returncode == 0 and vres.get("ok"):
            post_hard, _ = vms.check(json_path, docx_path, check_path)
            for h in post_hard:
                print(f"      [靜態後檢·FAIL] {h}")

        # ---- 多重獨立覆核（verify_passes > 1 時）----
        # 每一輪都是全新 context 的 verifier 重驗上一輪的產物，全數通過才放行。
        # 錯誤率按輪數相乘下降；代價是 Claude 用量按倍數增加，由 config 決定。
        # 注意：第 2 輪之後的配額耗盡走一般失敗路徑（計一次 attempt，下一輪重試），
        # 不走 QuotaExhausted 快停 —— 罕見情境，重試語意仍正確。
        passes = max(1, int(cfg["run"].get("verify_passes", 1)))
        fixed_total = int(vres.get("fixed") or 0) if vres.get("ok") else 0
        vpass_done = 1
        while (vpass_done < passes and vp.returncode == 0 and vres.get("ok")
               and not post_hard):
            vpass_done += 1
            print(f"    多重覆核：第 {vpass_done}/{passes} 輪（獨立 context 重驗）…")
            finish(ST_RUNNING)
            try:
                vp = subprocess.run(vcmd, capture_output=True, text=True, cwd=PROJECT_DIR,
                                    encoding="utf-8", errors="replace", env=cli_env(),
                                    timeout=cfg["run"].get("verify_timeout_seconds", 2700))
            except subprocess.TimeoutExpired:
                ck.mark("memo_verified", done=False, attempts=v_att,
                        error=f"第 {vpass_done} 輪覆核逾時")
                finish(ST_FAILED, f"第 {vpass_done} 輪覆核逾時（下一輪重試）", retry=v_att)
                return
            vres = _parse_agent_json(vp.stdout or "")
            post_hard = []
            if vp.returncode == 0 and vres.get("ok"):
                post_hard, _ = vms.check(json_path, docx_path, check_path)
                fixed_total += int(vres.get("fixed") or 0)
            journal(cfg, {"ticker": job.ticker, "event": "verify_pass",
                          "n": vpass_done, "rc": vp.returncode, "ok": vres.get("ok")})

        if vp.returncode == 0 and vres.get("ok") and not post_hard:
            ck.mark("memo_verified", attempts=v_att, passes=vpass_done,
                    fixed=fixed_total, unverifiable=vres.get("unverifiable"),
                    missing_added=vres.get("missing_added"),
                    check=str(vres.get("check_path") or check_path))
            print(f"    驗證通過（{vpass_done} 輪）：累計修正 {fixed_total} 條"
                  f"／無法核對 {vres.get('unverifiable', '?')} 條"
                  f"／補遺漏 {vres.get('missing_added', '?')} 條")
        else:
            msg = ("驗證未通過："
                   + ("；".join(post_hard)
                      or "；".join(vres.get("issues") or [])
                      or f"rc={vp.returncode} {(vp.stderr or vout)[-200:]}")[:250])
            if verify_mode == "warn":
                print(f"    [!] {msg}（verify_mode=warn，放行不擋）")
                ck.mark("memo_verified", attempts=v_att, waived=True, error=msg[:200])
            else:
                if v_att >= int(cfg["run"].get("max_retries", 2)):
                    # fail-closed 的終點：不 gave_up 整條線（memo 本身可能沒問題），
                    # 只把驗證標成放棄並停在 failed，等人工判斷。要重來：刪 checkpoint
                    # 的 memo_verified 階段或整檔 checkpoint。
                    ck.mark("memo_verified", done=False, attempts=v_att,
                            gave_up=True, error=msg[:300])
                    print(f"    [X] {msg}（第 {v_att} 次，達上限，停下等人工）")
                    finish(ST_FAILED, f"{msg}（驗證重試 {v_att} 次後停止，需人工）", retry=v_att)
                else:
                    ck.mark("memo_verified", done=False, attempts=v_att, error=msg[:300])
                    print(f"    [X] {msg}（第 {v_att} 次，下一輪重試）")
                    finish(ST_FAILED, msg, retry=v_att)
                return

    # ---- 2.5 podcast（方案 A：notebooklm CLI）----
    # 就緒偵測已經抓到新聞稿與逐字稿的網址，正好是 NotebookLM 能伺服器端抓取的
    # 免費來源。付費牆（Seeking Alpha）由 make_podcast 自行排除。
    # 檔名用分頁季度（財政），對齊「本機檔名、Drive 檔名、Doc 分頁三邊同標籤」的
    # 慣例（見 _memo_paths docstring）；資料夾仍是曆年桶。舊命名（{TICKER} 2Q26.m4a）
    # 的既有檔案照樣沿用，不重產。
    tab_q = job.tab_name.split(" ", 1)[-1]
    pod_dir = Path(cfg["local"]["podcast_dir"]) / quarter
    m4a = pod_dir / f"{job.ticker} {tab_q}.m4a"
    legacy_m4a = pod_dir / f"{job.ticker} {quarter}.m4a"
    if not m4a.exists() and legacy_m4a.exists():
        m4a = legacy_m4a
    if cfg.get("run", {}).get("podcast_mode") == "cli" and not m4a.exists():
        # 就緒偵測只給 SEC 申報與 Fool 逐字稿；再補上新聞稿鏡像，
        # 讓自動化的選材廣度貼近人工流程。
        call_d = dt.datetime.fromisoformat(job.call_at).date()
        wy, wq = quarters.calendar_quarter_for(call_d)
        srcs, notes = sources.discover(
            job.ticker, call_date=call_d, want_q=wq, want_y=wy,
            extra=_urls(job.press_release) + _urls(job.transcript),
        )
        for n in notes:
            print(f"      · {n}")
        if srcs:
            print(f"    產 podcast（notebooklm CLI，{len(srcs)} 個來源）…")
            # --tab-quarter：checkpoint 與檔名都要用分頁季度。不傳的話
            # make_podcast 會退回「曆年季度」，非曆年財年公司（COHR/SMCI…）
            # 的 podcast_built 會寫進別季的 checkpoint 檔（2026-08-13 實證）。
            pod = [sys.executable, str(Path(__file__).with_name("make_podcast.py")),
                   "--ticker", job.ticker, "--quarter", quarter,
                   "--tab-quarter", tab_q]
            for s in srcs:
                pod += ["--source", s]
            if json_path.exists():
                pod += ["--focus-from", str(json_path)]
            r3 = subprocess.run(pod, capture_output=True, text=True, cwd=PROJECT_DIR,
                                encoding="utf-8", errors="replace", timeout=2400)
            tail = (r3.stdout or r3.stderr or "").strip().splitlines()[-3:]
            print("      " + "\n      ".join(tail))
            journal(cfg, {"ticker": job.ticker, "event": "podcast", "rc": r3.returncode})
            # podcast 失敗不擋 memo 歸檔 —— memo 本身是獨立的成果，
            # 音檔之後可由 sweep 或人工補上。
            if r3.returncode != 0:
                print("      [!] podcast 產出失敗，memo 仍會歸檔")
        else:
            print("    [!] 沒有可用的來源網址，跳過 podcast")

    # ---- 3. 歸檔（音檔若已產出就一併上傳；否則分頁先寫、連結之後補）----
    pub = [
        sys.executable, str(Path(__file__).with_name("publish_memo.py")),
        "--ticker", job.ticker, "--tab-name", job.tab_name,
        "--on-existing-tab", "skip",
    ]
    if job.industry:
        # 新 ticker 兩棵樹都沒有落點時，publish_memo 會用「產業」欄自動建檔
        # （2026-08-13 CBRS 教訓：以前直接失敗，還連帶整檔重跑）。
        pub += ["--industry", job.industry]
    # .docx 是優先來源 —— 分頁要跟使用者手上那份 memo 逐字一致。
    # JSON 只在沒有 .docx 時當退路（它的渲染器看不到 dashboard／table／Q&A）。
    if docx_path.exists():
        pub += ["--docx", str(docx_path)]
    elif json_path.exists():
        pub += ["--json", str(json_path)]
    if m4a.exists():
        pub += ["--m4a", str(m4a)]

    print(f"    歸檔中（{'含音檔' if m4a.exists() else 'memo 先行，音檔待 sweep 補'}）…")
    finish(ST_RUNNING)   # 心跳：進入歸檔（上傳 Drive + 寫 Google Doc 分頁）
    r2 = subprocess.run(pub, capture_output=True, text=True, cwd=PROJECT_DIR,
                        encoding="utf-8", errors="replace", timeout=1800)
    print("      " + "\n      ".join((r2.stdout or "").strip().splitlines()[-4:]))
    journal(cfg, {"ticker": job.ticker, "event": "publish_done", "rc": r2.returncode})

    if r2.returncode != 0:
        msg = f"memo 已產出但歸檔失敗：{(r2.stdout or r2.stderr or '')[-300:]}"
        # 歸檔失敗只算歸檔的帳：記在 memo_tab_written（watchdog 的
        # run_failed_final 會撿到並告警），memo/podcast 的 checkpoint 原封不動
        # —— 下一輪走「歸檔續跑短路」只重跑歸檔，不再重產 memo。
        ck.fail("memo_tab_written", msg[:300])
        finish(ST_FAILED, msg)
        return

    # 報告完成；音檔若已在本 run 內上傳（--m4a 有帶），Podcast 一併打勾 ——
    # 這條路 sweep 會因 podcast_uploaded 已 done 而跳過，沒人會再勾。
    # memo 先行（音檔未產）維持 None，勾留給 sweep 上傳成功時打。
    note = f"（缺漏：{'、'.join(gaps)}）" if gaps else ""
    finish(ST_DONE, note, report_done=True,
           podcast_done=True if m4a.exists() else None)
    ck.mark("sheet_updated", row=job.row, report=True, by="dispatcher")
    print(f"    [OK] {job.ticker} memo 已歸檔{note}")

    # LINE 推播 —— 音檔已在才推。memo 先行的那條路留給 sweep_podcasts，
    # 這樣每檔只會有一則通知，不會「memo 好了」「podcast 好了」各來一則。
    if m4a.exists():
        print("    推播 LINE 群組…")
        # 季度用分頁季度（tab_name 的後半，例如 2026Q4）—— 要跟檢查點同一把 key，
        # 不是資料夾用的曆年標籤 quarter_label（2Q26），否則會另開一份檢查點、
        # 冪等就失效，同一檔每輪都會重推一次。
        notify.push(job.ticker, job.tab_name.split(" ", 1)[-1],
                    docx_path, m4a, cwd=PROJECT_DIR)


def _memo_paths(cfg: dict, job: "Job") -> tuple[Path, Path]:
    """本機 memo 的正規路徑 (JSON, docx)。

    命名一律用分頁名（`{TICKER} {YYYY}Q{n}`，財政季度）—— 本機檔名、Drive 檔名、
    Doc 分頁三邊同一個標籤，不再一邊曆年一邊財政。

    資料夾維持曆年季度（MEMO/2Q26/）：那是「這一波財報季」的桶子，
    改成財政會把同一個晚上發的 CRWV（曆年 Q2）和 SMCI（財政 Q4）拆到兩個資料夾。
    """
    memo_dir = Path(cfg["local"]["memo_dir"])
    stem = job.tab_name
    return (memo_dir / "_work" / f"{stem.lower().replace(' ', '_')}.json",
            memo_dir / job.quarter_label / f"{stem}.docx")


def _urls(text: str) -> list[str]:
    """從就緒偵測的說明字串裡挖出來源網址（SEC 申報、Fool 逐字稿）。

    要 unescape：這些字串是從 RSS 標題／摘要來的，網址裡的 & 是 HTML 實體 &amp;。
    直接餵給 NotebookLM 會 404。（CRWV 的 marketbeat 逐字稿實際踩到。）
    """
    return [html.unescape(u)
            for u in re.findall(r"https?://[^\s一-鿿）)]+", text or "")]


def _parse_agent_json(out: str) -> dict:
    """從 agent 的輸出裡挖出最後一個 JSON 物件。

    刻意不用逐行比對：實測 agent 可能把 JSON 印成多行（pretty print），
    也可能包在 markdown 圍欄裡，逐行比對兩種都抓不到。
    改成掃描成對大括號，並忽略字串內的括號與跳脫字元。
    抓不到就回空 dict，由檔案是否存在來判斷成敗 —— 不因為格式沒對上就誤判失敗。
    """
    spans, depth, start = [], 0, -1
    in_str = esc = False
    for i, ch in enumerate(out):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append((start, i + 1))

    for a, b in reversed(spans):
        blob = out[a:b]
        v = None
        try:
            v = json.loads(blob)
        except json.JSONDecodeError:
            # 常見的真實失敗：agent 直接吐未跳脫的 Windows 路徑
            #   "json_path": "C:\Users\..."   ← \U \b 之類都是無效的 JSON escape
            # 把不是合法 JSON 跳脫的單一反斜線補成雙反斜線再試一次。
            repaired = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", blob)
            try:
                v = json.loads(repaired)
            except json.JSONDecodeError:
                continue
        if isinstance(v, dict) and ("ok" in v or "json_path" in v or "docx_path" in v):
            return v
    return {}


if __name__ == "__main__":
    raise SystemExit(main())
