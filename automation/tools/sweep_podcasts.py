"""掃 PODCAST/{季度}/ 把人工做好的音檔自動歸檔。

這是過渡方案 B 的另一半：headless CLI 沒有瀏覽器 MCP，所以 podcast 由人在
桌面 App 用 NotebookLM 網頁版批次產出，丟進 `PODCAST\\{季度}\\` 就好 ——
剩下的（判斷該進哪個 Drive 分類、上傳、補分頁連結、回寫表、更新檢查點）
這支自動完成。

本機檔名用 `{TICKER} {n}Q{yy}`（VST 2Q26.m4a），
Drive 上統一改成分頁名 `{TICKER} {YYYY}Q{n}`，兩邊各自維持自己的慣例。

分頁季度**不從檔名推**：檔名是人取的，非曆年財政年度的公司會取成曆季，
而分頁是財政季度命名的。以追蹤表的財報日期 + SEC 的 fiscalYearEnd 為準，
讀不到才退回檔名。

用法：
    python tools/sweep_podcasts.py --quarter 2Q26            # 只看有哪些待處理
    python tools/sweep_podcasts.py --quarter 2Q26 --publish  # 真的上傳
"""

import _bootstrap  # noqa: F401

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

from core import checkpoint as cp
from core import notify, quarters
from core.gauth import load_config

# VST 2Q26.m4a / NRG_2Q26.m4a / ENPH 2026Q2.m4a 都要能認
NAME_RE = re.compile(r"^([A-Z][A-Z0-9.\-]{0,6})[ _]+(\d)Q(\d{2})|^([A-Z][A-Z0-9.\-]{0,6})[ _]+(\d{4})Q(\d)", re.I)


def parse_name(stem: str) -> tuple[str, str] | None:
    """回傳 (ticker, 檔名裡的季度 'YYYYQn')。認不出來回 None。

    注意：檔名裡的季度**只是退路**。它是人取的，非曆年財政年度的公司會取成曆季，
    而分頁是用財政季度命名的 —— 兩者對不上就會指到別一季的分頁。
    正式的推導在 resolve_quarter()。
    """
    m = NAME_RE.match(stem.strip())
    if not m:
        return None
    if m.group(1):
        tk, q, yy = m.group(1), int(m.group(2)), int(m.group(3))
        return tk.upper(), f"20{yy:02d}Q{q}"
    return m.group(4).upper(), f"{m.group(5)}Q{m.group(6)}"


def _first_existing(*paths: Path) -> Path | None:
    """回傳第一個真的存在的路徑；都不存在回 None。"""
    return next((p for p in paths if p.exists()), None)


def resolve_quarter(ticker: str, from_name: str, rows: dict, edgar, tmpl: str) -> tuple[str, str]:
    """回傳 (分頁季度, 說明)。以追蹤表的財報日期 + SEC 財政年度推導為準。

    這支原本直接拿檔名裡的季度當分頁季度，對非曆年財政年度的公司是錯的：
    SMCI 今天這份是 FY26 Q4，檔名寫 2Q26 → 推出『SMCI 2026Q2』，
    那卻是今年二月那份 FY26 Q2 的分頁。（2026-08-12 實際踩到。）

    追蹤表或 SEC 讀不到時退回檔名 —— 只列出、不上傳的用法不該被網路或授權擋住。
    """
    task = rows.get(ticker)
    if not task or edgar is None:
        return from_name, "檔名（讀不到追蹤表，退回檔名）"
    tab = quarters.tab_name(ticker, task.call_at.date(), tmpl,
                            fy_end=edgar.fiscal_year_end(ticker))
    q = tab.split(" ", 1)[-1]
    if q != from_name:
        return q, f"財政季度推導（檔名寫 {from_name}，已更正）"
    return q, "財政季度推導"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarter", required=True, help="本機資料夾名，例如 2Q26")
    ap.add_argument("--publish", action="store_true", help="真的執行上傳（否則只列出）")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config()
    sd = cfg["local"]["state_dir"]
    pod_dir = Path(cfg["local"]["podcast_dir"]) / args.quarter
    memo_dir = Path(cfg["local"]["memo_dir"])

    if not pod_dir.exists():
        print(f"[X] 找不到 {pod_dir}")
        return 1

    files = sorted(pod_dir.glob("*.m4a")) + sorted(pod_dir.glob("*.mp3"))
    print(f"{pod_dir} 底下 {len(files)} 個音檔\n")

    # 追蹤表與 SEC 要在掃檔之前就備妥 —— 分頁季度靠它們推導，不是靠檔名。
    # 讀不到就退回檔名，只列出的用法照樣能離線跑。
    sheets, rows, edgar = None, {}, None
    tmpl = cfg["memo_output"]["tab_name_template"]
    try:
        from core.edgar import Edgar
        from core.gauth import load_credentials
        from core.sheets_client import SheetsClient

        sheets = SheetsClient(load_credentials(interactive=False), cfg)
        rows = {t.ticker: t for t in sheets.load_tasks(year=dt.datetime.now().year)}
        edgar = Edgar(sd)
    except Exception as exc:
        print(f"[!] 讀不到追蹤表，分頁季度改用檔名、也不會回寫："
              f"{type(exc).__name__}: {exc}\n")

    pending, done, unknown, no_memo = [], [], [], []

    for f in files:
        parsed = parse_name(f.stem)
        if not parsed:
            unknown.append(f.name)
            continue
        ticker, from_name = parsed
        tab_q, how = resolve_quarter(ticker, from_name, rows, edgar, tmpl)
        if tab_q != from_name:
            print(f"   [季度更正] {f.name}：{from_name} → {tab_q}（{how}）")
        ck = cp.load(sd, ticker, tab_q)

        if ck.is_done("podcast_uploaded"):
            done.append(f"{ticker} {tab_q}")
            # 自癒：dispatcher 在 run 內一條龍上傳的檔，過去沒有人會打
            # Podcast 勾（2026-08-13 COHR/CSCO 實證）。dispatcher 已改為
            # 自己勾，但這裡留一道保險 —— 已上傳、報告已勾、Podcast 未勾
            # 就補勾。只在報告也勾了才動：報告沒勾代表整檔還沒完成，
            # 不該由這裡把 status 改成 done。
            t = rows.get(ticker)
            if args.publish and t and t.report_done and not t.podcast_done:
                try:
                    # error=None：只補勾，別把 auto_error 裡的「缺漏」註記洗掉
                    sheets.write_status(t, status="done", podcast_done=True,
                                        owner="agent", error=None)
                    print(f"   [補勾] {ticker}：音檔已上傳但表上 Podcast 未勾，已補上")
                except Exception as exc:
                    print(f"   [!] {ticker} 補勾失敗：{type(exc).__name__}: {exc}")
            continue

        # memo 來源：**.docx 優先** —— 分頁要跟使用者手上那份逐字一致，
        # JSON 渲染器看不到 dashboard／table／Q&A，會少掉一大半內容。
        # 正規檔名用分頁季度（SMCI 2026Q4.docx）。舊季度的檔案是曆年短標籤
        # （SMCI 2Q26.docx）命名的，一併找，免得回頭補舊資料時抓不到 memo。
        js = _first_existing(
            memo_dir / "_work" / f"{ticker.lower()}_{tab_q.lower()}.json",
            memo_dir / "_work" / f"{ticker.lower()}_{args.quarter.lower()}.json",
        )
        dx = _first_existing(
            memo_dir / args.quarter / f"{ticker} {tab_q}.docx",
            memo_dir / args.quarter / f"{ticker} {args.quarter}.docx",
        )
        if dx:
            src = ("--docx", str(dx))
        elif js:
            src = ("--json", str(js))
        else:
            # 本機沒有 memo，不代表不能歸檔 —— memo 很可能是同事直接寫在
            # Google Doc 分頁裡的。那種情況只要上傳音檔並補連結即可。
            src = None
            no_memo.append(f"{ticker} {tab_q}（本機無 memo，改為只補音檔與連結）")

        pending.append((ticker, tab_q, f, src))

    print(f"== 待上傳（{len(pending)}）")
    for tk, q, f, src in pending:
        mode = src[0][2:] if src else "只補音檔"
        print(f"   {tk:6s} {q}  {f.name}  ({f.stat().st_size / 1024 / 1024:.1f} MB, {mode})")
    if done:
        print(f"\n== 已上傳過，跳過（{len(done)}）：{', '.join(done)}")
    if no_memo:
        print(f"\n== 本機無 memo，改走「只補音檔」（{len(no_memo)}）")
        for x in no_memo:
            print(f"   {x}")
    if unknown:
        print(f"\n== 檔名認不出 ticker/季度（{len(unknown)}）")
        for x in unknown:
            print(f"   {x}")

    if not args.publish:
        print("\n（僅列出。要真的上傳請加 --publish）")
        return 0

    todo = pending[: args.limit] if args.limit else pending
    ok = fail = 0

    for tk, q, f, src in todo:
        print(f"\n>>> {tk} {q}")
        cmd = [
            sys.executable, str(Path(__file__).with_name("publish_memo.py")),
            "--ticker", tk, "--tab-name", f"{tk} {q}",
            "--m4a", str(f), "--on-existing-tab", "skip",
        ]
        if src:
            cmd += [src[0], src[1]]
        r = subprocess.run(cmd, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            fail += 1
            print(f"    [X] rc={r.returncode}")
            continue

        ok += 1
        # memo 先行的那條路在這裡才補齊 podcast，所以 LINE 通知也在這裡發 ——
        # dispatcher 當時音檔還沒好，刻意沒推，避免同一檔通知兩次。
        if src and src[0] == "--docx":
            notify.push(tk, q, src[1], f, cwd=Path(__file__).resolve().parent.parent)

        # 音檔上傳成功才勾 Podcast —— 不然表上看起來做完了、Drive 上卻沒有東西。
        # error=None：memo 先行的檔，auto_error 裡是 dispatcher 寫的「缺漏」註記，
        # 補勾不該把它洗掉。
        task = rows.get(tk)
        if sheets and task:
            sheets.write_status(task, status="done", podcast_done=True, owner="agent",
                                error=None)
            cp.load(sd, tk, q).mark("sheet_updated", row=task.row,
                                    podcast=True, by="sweep")
            print(f"    追蹤表第 {task.row} 列：Podcast 已勾選")
        elif sheets:
            print(f"    [!] 追蹤表找不到 {tk}，未回寫")

    print(f"\n完成 {ok}／失敗 {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
