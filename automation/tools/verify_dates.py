"""財報日期核對 agent —— 用 Nasdaq 打底，再派 agent 去公司官網核對／補漏。

## 為什麼是「agent」而不是爬蟲（實測結論，不是偏好）

用純 HTTP 抓 15 個 IR 頁面：3 檔表上無網址、4 檔連線失敗、8 檔抓到頁面，
其中只有 1 檔給出可用日期（而且是舊的），NVDA 抓到「September 09, 2021」的垃圾。
成功率約 12%。

換 headless Chrome 再測：VST 的 DOM 有 183KB，但**連 "Earnings" 字樣都沒有**
——內容在二次 XHR 之後才載入。MU 直接抓不到。

同時確認 EDGAR 全文檢索對這件事無效：AMAT / WMT / DELL / PANW 在財報前
都**沒有**為了預告日期而發 8-K（total=0）。那只是 IR 網站上的新聞稿。

⇒ 110 個異質 IR 網站，regex 不可能通吃。但這正是 LLM 擅長的：
   讀一頁人看得懂、機器難解析的頁面，然後回報結構化答案。
   所以官網這一段交給 agent，Nasdaq 那段維持確定性程式。

## 分工

    Nasdaq calendar（確定性）  →  多數 ticker 的日期 + 盤前/盤後
            ↓
    agent（claude -p，每檔一個）
      ├─ Nasdaq 有 → 去官網核對，確認／推翻，並補上精確的法說會時刻
      └─ Nasdaq 無 → 去官網找有沒有公告

    → 三方比對（表上現值 / Nasdaq / 官網）。兩者不同一律以官網為主，直接寫回表，
      並在報表上列出被推翻的項目供事後覆核。

用法：
    python tools/verify_dates.py --days 45                  # 只核對，不寫表
    python tools/verify_dates.py --days 45 --limit 5        # 先試幾檔
    python tools/verify_dates.py --days 45 --write          # 核對後寫回表
    python tools/verify_dates.py --tickers VST,AMAT --no-agent   # 只跑 Nasdaq
"""

import _bootstrap  # noqa: F401

import argparse
import datetime as dt
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

from core import calendar_source as cal
from core.gauth import load_config

# 查證規則、輸出格式、鐵則都寫在 agent 定義裡
# （.claude/agents/earnings-date-verifier.md），這裡只給這一次的任務資料。
AGENT_NAME = "earnings-date-verifier"
AGENT_PROMPT = """\
查證標的：{ticker}

- 公司 IR 頁面：{ir_url}
- Nasdaq 行事曆：{nasdaq_hint}
"""


@dataclass
class Verdict:
    ticker: str
    sheet_date: str | None
    nasdaq_date: str | None
    nasdaq_session: str | None
    ir_found: bool = False
    ir_date: str | None = None
    ir_time_et: str | None = None
    ir_source: str | None = None
    ir_quote: str | None = None
    ir_confidence: str | None = None
    agreement: str = ""        # agree / conflict / nasdaq_only / ir_only / none
    final_date: str | None = None      # 美東日期（公司/行事曆講的那天）
    final_call_tw: str | None = None   # 台灣時間的法說會開始時刻
    final_date_tw: str | None = None   # 台灣日期 —— 寫回表的就是這個，不是 final_date
    note: str = ""


# 使用者指定全面 skip_permissions —— 無人值守時不能停在權限詢問。
# 實質範圍仍受 agent 定義的 tools 欄位限制（本 agent 只有 WebFetch, WebSearch）。
AGENT_TOOLS = "WebFetch,WebSearch"

# agent 定義在專案的 .claude/agents/，所以必須在該目錄下執行才找得到
PROJECT_DIR = str(Path(__file__).resolve().parent.parent.parent)


def ask_agent(ticker: str, ir_url: str, nasdaq_hint: str, timeout: int) -> dict:
    prompt = AGENT_PROMPT.format(ticker=ticker, ir_url=ir_url or "（表上沒有網址，請自行找官方 IR 頁）",
                                 nasdaq_hint=nasdaq_hint)
    # 參數順序有講究：--allowedTools 是變參數旗標，放在 -p 之前才不會把 prompt 吃掉。
    # 寫成 ['claude','-p',prompt,'--allowedTools',...] 或
    #     ['claude','-p','--allowedTools',...,prompt] 都會 rc=1 且無輸出。
    cmd = ["claude", "--agent", AGENT_NAME, "--allowedTools", AGENT_TOOLS,
           "--dangerously-skip-permissions", "-p", prompt]
    try:
        # env=cli_env()：claude 主登入失效時自動帶備援 token（core/claude_cli.py）
        from core.claude_cli import cli_env
        proc = subprocess.run(cmd, capture_output=True, cwd=PROJECT_DIR, env=cli_env(),
                              text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"found": False, "_error": "agent 逾時"}
    if proc.returncode != 0:
        return {"found": False, "_error": f"agent rc={proc.returncode}: {(proc.stderr or '')[:200]}"}

    out = proc.stdout or ""
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return {"found": False, "_error": f"agent 沒有回傳 JSON：{out[:200]}"}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        return {"found": False, "_error": f"JSON 解析失敗 {exc}: {m.group(0)[:200]}"}


def reconcile(v: Verdict) -> Verdict:
    n, i = v.nasdaq_date, (v.ir_date if v.ir_found else None)
    if n and i:
        if n == i:
            v.agreement, v.final_date = "agree", n
            v.note = "Nasdaq 與官網一致"
        else:
            # 官網是公司自己講的，權威性高於第三方彙整 —— 兩者不同一律以官網為準，
            # 並且直接寫回表（2026-08-12 定案：不再卡人工確認，實測 Nasdaq 常慢半拍）。
            v.agreement, v.final_date = "conflict", i
            v.note = f"不一致：Nasdaq {n} vs 官網 {i} → 採官網（公司自述優先）並寫回，請事後覆核"
    elif n:
        v.agreement, v.final_date = "nasdaq_only", n
        v.note = "官網查無，僅有 Nasdaq"
    elif i:
        v.agreement, v.final_date = "ir_only", i
        v.note = "Nasdaq 未收錄，官網有"
    else:
        v.agreement = "none"
        v.note = "兩邊都查不到"

    if v.final_date:
        d = dt.date.fromisoformat(v.final_date)
        if v.ir_found and v.ir_time_et:
            try:
                hh, mm = (int(x) for x in v.ir_time_et.split(":"))
                tw = dt.datetime.combine(d, dt.time(hh, mm), tzinfo=cal.ET).astimezone(cal.TW)
                v.final_call_tw = tw.replace(tzinfo=None).isoformat(timespec="minutes")
                v.final_date_tw = v.final_call_tw[:10]
                v.note += "；時刻為官網實際載明（非推估）"
                return v
            except Exception:
                pass
        est = cal.estimate_call_time_tw(d, v.nasdaq_session or "?")
        if est:
            v.final_call_tw = est.isoformat(timespec="minutes")
            v.final_date_tw = v.final_call_tw[:10]
            v.note += "；時刻為盤前/盤後推估"
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--start", default=None)
    ap.add_argument("--tickers", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-agent", action="store_true", help="只跑 Nasdaq，不派 agent")
    ap.add_argument("--only-missing", action="store_true",
                    help="只處理表上沒有日期/時間的那幾檔（省下大量 agent 呼叫）")
    ap.add_argument("--rolling", action="store_true",
                    help="滾動模式（排程用）：只處理『還沒做完』且『日期缺漏或近期內』的檔，"
                         "其餘不浪費 agent 呼叫")
    ap.add_argument("--horizon-days", type=int, default=10,
                    help="滾動模式下，日期落在幾天內的才重新核對（預設 10）")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--agent-timeout", type=int, default=420)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    start = dt.date.fromisoformat(args.start) if args.start else dt.date.today()

    # ---------- ticker 清單 + IR 網址 ----------
    sheets, tasks, ir_urls = None, [], {}
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        from core.gauth import load_credentials
        from core.sheets_client import SheetsClient

        sheets = SheetsClient(load_credentials(interactive=False), cfg)
        tasks = sheets.load_tasks(year=start.year)
        tickers = [t.ticker for t in tasks]
        # 純文字欄只拿得到顯示文字，真正的網址要從超連結取
        links = sheets.load_hyperlinks("ir_url")
        for t in tasks:
            ir_urls[t.ticker] = (
                t.ir_url if t.ir_url.startswith("http") else links.get(t.row, "")
            )
        got = sum(1 for u in ir_urls.values() if u)
        print(f"IR 網址：{got}/{len(tickers)} 檔有（含從超連結救回的）")
        if args.only_missing:
            missing = [t.ticker for t in tasks if t.skip_reason or t.call_at is None]
            tickers = [t for t in tickers if t in missing]
            print(f"--only-missing：縮小到 {len(tickers)} 檔（{', '.join(tickers)}）")

        elif args.rolling:
            # 滾動模式的取捨：每天對 112 檔全部派 agent 太貴，而且大多數不會變。
            # 只查兩種：
            #   (a) 還沒有日期的 —— 可能是公司剛公告，這正是「發現新日期」的入口
            #   (b) 日期就在近期、且還沒做完的 —— 日期常在財報前一兩週被公司改
            # 已經打 TRUE 的一律不查，對別人做完的事重新核對沒有意義。
            now = dt.datetime.now()
            horizon = dt.timedelta(days=args.horizon_days)
            pick, why = [], {}
            for t in tasks:
                if not t.needs_memo and not t.needs_podcast:
                    continue
                if t.call_at is None:
                    pick.append(t.ticker)
                    why[t.ticker] = "尚無日期"
                elif dt.timedelta(0) <= (t.call_at - now) <= horizon:
                    pick.append(t.ticker)
                    why[t.ticker] = f"{t.call_at:%m/%d} 在 {args.horizon_days} 天內"
            tickers = [t for t in tickers if t in pick]
            print(f"--rolling：縮小到 {len(tickers)} 檔")
            for t in tickers:
                print(f"    {t:6s} {why.get(t, '')}")

    if args.limit:
        tickers = tickers[: args.limit]
    print(f"核對 {len(tickers)} 檔\n")

    # ---------- Nasdaq ----------
    print("① 掃 Nasdaq 行事曆…")
    found = cal.fetch_range(
        start, args.days, tickers=set(tickers),
        on_progress=lambda d, n, m: print(f"   {d} {'-' if n is None else f'{n:3d}'} {m}"),
    )
    print(f"   Nasdaq 命中 {len(found)}/{len(tickers)}\n")

    by_ticker = {t.ticker: t for t in tasks}
    verdicts = {
        tk: Verdict(
            ticker=tk,
            sheet_date=(by_ticker[tk].call_at.date().isoformat()
                        if tk in by_ticker and by_ticker[tk].call_at else None),
            nasdaq_date=found[tk].date.isoformat() if tk in found else None,
            nasdaq_session=found[tk].session if tk in found else None,
        )
        for tk in tickers
    }

    # ---------- agent 核對 ----------
    if not args.no_agent:
        print(f"② 派 agent 核對官網（併發 {args.concurrency}，逾時 {args.agent_timeout}s）…")

        def work(tk: str):
            v = verdicts[tk]
            hint = (f"{v.nasdaq_date}（{v.nasdaq_session}）" if v.nasdaq_date
                    else "行事曆上查無，請確認是否有公告")
            return tk, ask_agent(tk, ir_urls.get(tk, ""), hint, args.agent_timeout)

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futs = {pool.submit(work, tk): tk for tk in tickers}
            for fut in as_completed(futs):
                tk, res = fut.result()
                v = verdicts[tk]
                if res.get("_error"):
                    v.note = res["_error"]
                    print(f"   {tk:6s} [X] {res['_error'][:70]}")
                    continue
                v.ir_found = bool(res.get("found"))
                v.ir_date = res.get("date") or None
                v.ir_time_et = res.get("time_et") or None
                v.ir_source = res.get("source_url") or None
                v.ir_quote = res.get("quote") or None
                v.ir_confidence = res.get("confidence") or None
                print(f"   {tk:6s} {'找到 ' + str(v.ir_date) if v.ir_found else '官網查無'}"
                      f"{' ' + (v.ir_time_et or '') if v.ir_found else ''}"
                      f" [{v.ir_confidence or '-'}]")
        print()

    # ---------- 比對 ----------
    for v in verdicts.values():
        reconcile(v)

    groups: dict[str, list[Verdict]] = {}
    for v in verdicts.values():
        groups.setdefault(v.agreement, []).append(v)

    labels = {
        "agree": "Nasdaq 與官網一致",
        "conflict": "**不一致 → 採官網並寫回（請事後覆核）**",
        "nasdaq_only": "只有 Nasdaq",
        "ir_only": "只有官網（Nasdaq 未收錄）",
        "none": "兩邊都查不到",
    }
    print("③ 比對結果")
    for key in ("conflict", "ir_only", "agree", "nasdaq_only", "none"):
        rows = groups.get(key, [])
        if not rows:
            continue
        print(f"\n  == {labels[key]}（{len(rows)}）")
        for v in sorted(rows, key=lambda x: x.ticker):
            # 表上是台灣日期，所以左右兩邊都用台灣日期比，美東日期只當佐證附在後面
            tw = f" {v.final_call_tw[11:16]}" if v.final_call_tw else ""
            print(f"     {v.ticker:6s} 表上 {v.sheet_date or '空':10s} → "
                  f"台灣 {v.final_date_tw or '換算不出':10s}{tw}   （美東 {v.final_date or '-'}）")
            if key == "conflict":
                print(f"            {v.note}")
                if v.ir_quote:
                    print(f"            官網原文：{v.ir_quote[:70]}  {v.ir_source or ''}")

    out = Path(cfg["local"]["state_dir"]) / "date_verify.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"verified_at": dt.datetime.now().isoformat(timespec="seconds"),
         "results": [asdict(v) for v in verdicts.values()]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完整結果：{out}")

    # ---------- 寫回 ----------
    if not args.write:
        print("（唯讀模式。確認無誤後加 --write 才會寫回追蹤表）")
        return 0
    if sheets is None:
        print("[X] --write 不能跟 --tickers 併用（需要表上的列號）")
        return 1

    from tools.update_dates import _write_date  # noqa: E402

    # 表上的「日期」欄跟「時間（台灣）」欄是一組的，會被 core.sheets_client.parse_call_datetime
    # 直接組成台灣時間，所以日期欄填的必須是**台灣日期**。盤後（AMC）的公司美東日期比台灣日期
    # 少一天，把 final_date（美東）寫進去會讓整列早一天 —— 2026-08-12 那次就是這樣寫壞 6 列的。
    n, no_tw, overridden = 0, [], []
    for v in verdicts.values():
        # 兩者不同就以官網為主（公司自述 > 第三方彙整）—— conflict 照寫，寫完列出來供覆核。
        if not v.final_date:
            continue
        if not v.final_date_tw:
            no_tw.append(v.ticker)   # 只有日期、查不到時刻 → 換算不出台灣日期，寧可不寫
            continue
        if v.sheet_date == v.final_date_tw:
            continue
        d = dt.date.fromisoformat(v.final_date_tw)
        entry = cal.CalendarEntry(
            v.ticker, d, v.nasdaq_session or "?",
            dt.datetime.fromisoformat(v.final_call_tw) if v.final_call_tw else None,
        )
        _write_date(sheets, by_ticker[v.ticker], entry, fill_time=not by_ticker[v.ticker].call_at)
        n += 1
        if v.agreement == "conflict":
            overridden.append(v)
    print(f"[OK] 寫回 {n} 列")
    if overridden:
        print(f"     其中 {len(overridden)} 檔 Nasdaq 與官網不一致，已採官網寫回，請事後覆核：")
        for v in sorted(overridden, key=lambda x: x.ticker):
            print(f"       {v.ticker:6s} Nasdaq {v.nasdaq_date} → 官網 {v.final_date}"
                  f"（台灣 {v.final_date_tw} {(v.final_call_tw or '')[11:16]}）"
                  f" 信心 {v.ir_confidence or '-'}　{v.ir_source or ''}")
    if no_tw:
        print(f"     另有 {len(no_tw)} 檔只有美東日期、查不到盤前盤後或時刻，換算不出台灣日期，"
              f"未寫回，請人工確認：{', '.join(sorted(no_tw))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
