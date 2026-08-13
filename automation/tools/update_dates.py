"""日期更新 agent —— 取代人工填寫追蹤表的日期欄。

流程：
  ① 從追蹤表讀出要追蹤的 ticker 清單
  ② 掃 Nasdaq earnings calendar 未來 N 天
  ③ 跟表上現有日期比對，產出差異報告
  ④ --write 時才真的寫回表（需要 OAuth）

**刻意的限制**：Nasdaq 只給「日期 + 盤前/盤後」，給不出精確的法說會時間，
而且財報發布時間 ≠ 法說會開始時間（常差 30–60 分鐘）。
所以本工具：
  - 日期欄：直接更新（這是它能給的），但**一律換算成台灣日期再寫**——表上日期欄跟
    「時間（台灣）」欄是一組的，寫美東日期會讓所有盤後的公司早一天
  - 時間欄：只在「原本是空的」時候填推估值，並在備註標明 est.
    已經有人手填精確時間的，**不覆蓋** —— 人填的比推估的準。

這在本架構下是可接受的，因為派工是由就緒偵測把關，不是由時間把關。
時間只決定何時開始輪詢，早晚半小時不影響產出。

用法：
    python tools/update_dates.py --days 45              # 只看差異，不寫
    python tools/update_dates.py --days 45 --write      # 真的寫回表
    python tools/update_dates.py --days 45 --offline    # 不碰 Google，只掃 Nasdaq
"""

import _bootstrap  # noqa: F401

import argparse
import datetime as dt
import json
from pathlib import Path

from core import calendar_source as cal
from core.gauth import load_config


def as_tw(e: cal.CalendarEntry) -> cal.CalendarEntry | None:
    """把 Nasdaq 的美東日期換算成台灣日期。

    表上「日期」欄跟「時間（台灣）」欄是一組的（見 core.sheets_client.parse_call_datetime），
    填的必須是台灣日期。盤後（AMC）的公司美東日期比台灣日期少一天，直接寫 Nasdaq 給的日期
    會讓整列早一天。Nasdaq 連盤前/盤後都沒給的（session "?"）換算不出來，回 None 讓呼叫端跳過。
    """
    if not e.est_call_tw:
        return None
    return cal.CalendarEntry(e.ticker, e.est_call_tw.date(), e.session, e.est_call_tw, e.source)


def read_tickers_offline(cfg: dict) -> set[str]:
    """沒有 OAuth 時，用 build_drive_map 產的對照表當 ticker 清單。"""
    p = Path(cfg["local"]["state_dir"]) / "drive_map.json"
    if p.exists():
        return set(json.loads(p.read_text(encoding="utf-8"))["ticker_index"])
    return set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--start", default=None, help="YYYY-MM-DD，預設今天")
    ap.add_argument("--write", action="store_true", help="真的寫回追蹤表")
    ap.add_argument("--offline", action="store_true", help="不連 Google，只掃 Nasdaq")
    ap.add_argument("--tickers", default=None, help="逗號分隔，覆蓋自動取得的清單")
    args = ap.parse_args()

    cfg = load_config()
    start = dt.date.fromisoformat(args.start) if args.start else dt.date.today()

    # ---------- ① ticker 清單 ----------
    sheets = None
    tasks = []
    if args.tickers:
        tickers = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}
    elif args.offline:
        tickers = read_tickers_offline(cfg)
    else:
        from core.gauth import load_credentials
        from core.sheets_client import SheetsClient

        creds = load_credentials(interactive=False)
        sheets = SheetsClient(creds, cfg)
        tasks = sheets.load_tasks(year=start.year)
        tickers = {t.ticker for t in tasks}

    if not tickers:
        print("[X] 拿不到 ticker 清單。先跑 build_drive_map.py，或用 --tickers 指定。")
        return 1
    print(f"追蹤 {len(tickers)} 檔；掃 {start} 起 {args.days} 天的 Nasdaq 財報行事曆\n")

    # ---------- ② 掃行事曆 ----------
    def progress(d, n, msg):
        print(f"  {d}  {'-' if n is None else f'{n:3d} 筆'}  {msg}")

    found = cal.fetch_range(start, args.days, tickers=tickers, on_progress=progress)
    print(f"\n在行事曆上找到 {len(found)} / {len(tickers)} 檔\n")

    # ---------- ③ 比對 ----------
    by_ticker = {t.ticker: t for t in tasks}
    new_rows, changed, unchanged, not_found, no_session = [], [], [], [], []

    for tk in sorted(tickers):
        e = found.get(tk)
        if not e:
            not_found.append(tk)
            continue
        e = as_tw(e)          # 之後一律用台灣日期，跟表上同一個時區
        if e is None:
            no_session.append(tk)
            continue
        cur = by_ticker.get(tk)
        cur_date = cur.call_at.date() if (cur and cur.call_at) else None
        if cur_date is None:
            new_rows.append((tk, e))
        elif cur_date != e.date:
            changed.append((tk, cur_date, e))
        else:
            unchanged.append(tk)

    print(f"== 表上原本沒有日期，行事曆有（{len(new_rows)}）")
    for tk, e in new_rows:
        est = e.est_call_tw.strftime("%m/%d %H:%M") if e.est_call_tw else "??"
        print(f"   {tk:6s} → 台灣 {e.date} {e.session:3s}  時刻推估 {est}")

    print(f"\n== 日期有變動（{len(changed)}）")
    for tk, old, e in changed:
        print(f"   {tk:6s} 台灣 {old} → {e.date} {e.session}")

    if no_session:
        print(f"\n== 行事曆有、但沒給盤前/盤後（{len(no_session)}）換算不出台灣日期，未處理")
        print(f"   {', '.join(no_session)}")

    print(f"\n== 未變動（{len(unchanged)}）  == 行事曆查無（{len(not_found)}）")
    if not_found:
        print(f"   查無：{', '.join(not_found[:30])}{' …' if len(not_found) > 30 else ''}")
        print("   （多為本區間內不發財報，或 Nasdaq 未收錄的 OTC/ADR）")

    out = Path(cfg["local"]["state_dir"]) / "date_diff.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "scanned_at": dt.datetime.now().isoformat(timespec="seconds"),
        "range": [start.isoformat(), args.days],
        "new": [{"ticker": t, "date": e.date.isoformat(), "session": e.session,
                 "est_call_tw": e.est_call_tw.isoformat() if e.est_call_tw else None}
                for t, e in new_rows],
        "changed": [{"ticker": t, "old": o.isoformat(), "date": e.date.isoformat(),
                     "session": e.session} for t, o, e in changed],
        "not_found": not_found,
        "no_session": no_session,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n差異已寫出：{out}")

    # ---------- ④ 寫回 ----------
    if not args.write:
        print("\n（唯讀模式。確認差異無誤後加 --write 才會寫回追蹤表）")
        return 0
    if sheets is None:
        print("\n[X] --write 需要連線模式（不能跟 --offline / --tickers 併用）")
        return 1

    print(f"\n寫回追蹤表：{len(new_rows) + len(changed)} 列…")
    for tk, e in new_rows:
        _write_date(sheets, by_ticker[tk], e, fill_time=True)
    for tk, _old, e in changed:
        _write_date(sheets, by_ticker[tk], e, fill_time=False)
    print("[OK] 完成")
    return 0


def _write_date(sheets, task, entry, *, fill_time: bool) -> None:
    """只動日期（與空白的時間欄）。人手填的精確時間不覆蓋。

    `entry.date` 必須已經是**台灣日期**（呼叫端負責換算）——這格會跟「時間（台灣）」欄
    一起被讀成台灣時間。
    """
    hdr = sheets.load_header()
    tab = sheets.resolve_tab()
    from core.sheets_client import _a1

    weekday = "一二三四五六日"[entry.date.weekday()]
    data = [{
        "range": f"'{tab}'!{_a1(hdr['date'])}{task.row}",
        "values": [[f"  {entry.date.strftime('%m/%d')}   週{weekday}"]],
    }]
    if fill_time and entry.est_call_tw and "time_tw" in hdr:
        data.append({
            "range": f"'{tab}'!{_a1(hdr['time_tw'])}{task.row}",
            "values": [[f" {entry.est_call_tw.strftime('%H:%M')}"]],
        })
    sheets.svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheets.sid,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


if __name__ == "__main__":
    raise SystemExit(main())
