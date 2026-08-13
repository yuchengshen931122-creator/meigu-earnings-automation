"""財報日期來源。

實測（2026-08-11）：Nasdaq 的公開 calendar endpoint 可用，單日約 240 筆，
提供 symbol 與 `time-pre-market` / `time-after-hours`。

**但它給不出精確的法說會時間。** 而且要注意兩者不是同一件事：

    財報「發布」時間  ≠  法說會「開始」時間     常差 30–60 分鐘

追蹤表填的是法說會開始時間（使用者確認過）。所以 Nasdaq 抓來的東西
不能直接當成表上的時間，只能給「日期 + 盤前/盤後」。

不過這在本架構下影響有限：**派工由就緒偵測把關，不是由時間把關。**
時間只決定「從什麼時候開始輪詢」，早半小時或晚半小時開始輪詢，
不影響最終產出——因為要等新聞稿真的出現才會派工。
所以日期要準，分鐘不必準。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from core.net import BROWSER_UA, fetch_json

NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings?date={date}"
ET = ZoneInfo("America/New_York")
TW = ZoneInfo("Asia/Taipei")

# 美東當地的典型法說會時間（用來把 BMO/AMC 換算成台灣時間的估計值）
TYPICAL_BMO_ET = dt.time(8, 30)
TYPICAL_AMC_ET = dt.time(17, 0)


@dataclass
class CalendarEntry:
    ticker: str
    date: dt.date
    session: str            # "BMO" | "AMC" | "?"
    est_call_tw: dt.datetime | None
    source: str = "nasdaq"

    @property
    def time_is_estimate(self) -> bool:
        return True  # Nasdaq 永遠只給盤前/盤後，時刻一律是推估


def _session_of(raw: str) -> str:
    r = (raw or "").lower()
    if "pre-market" in r or "before" in r:
        return "BMO"
    if "after-hours" in r or "after" in r:
        return "AMC"
    return "?"


def estimate_call_time_tw(date: dt.date, session: str) -> dt.datetime | None:
    """把「日期 + 盤前/盤後」換算成台灣時間的估計值（含美東日光節約時間）。"""
    if session == "BMO":
        local = dt.datetime.combine(date, TYPICAL_BMO_ET, tzinfo=ET)
    elif session == "AMC":
        local = dt.datetime.combine(date, TYPICAL_AMC_ET, tzinfo=ET)
    else:
        return None
    return local.astimezone(TW).replace(tzinfo=None)


def fetch_day(date: dt.date) -> list[CalendarEntry]:
    data = fetch_json(
        NASDAQ_URL.format(date=date.isoformat()),
        ua=BROWSER_UA,
        headers={"Accept": "application/json"},
        timeout=25,
        retries=2,
    )
    rows = ((data or {}).get("data") or {}).get("rows") or []
    out = []
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        sess = _session_of(r.get("time", ""))
        out.append(CalendarEntry(sym, date, sess, estimate_call_time_tw(date, sess)))
    return out


def fetch_range(start: dt.date, days: int, *, tickers: set[str] | None = None,
                on_progress=None) -> dict[str, CalendarEntry]:
    """掃一段日期，回傳 {ticker: CalendarEntry}。只保留最早出現的一筆。"""
    found: dict[str, CalendarEntry] = {}
    for i in range(days):
        d = start + dt.timedelta(days=i)
        if d.weekday() >= 5:      # 週末不會有財報
            continue
        try:
            entries = fetch_day(d)
        except Exception as exc:
            if on_progress:
                on_progress(d, None, f"{type(exc).__name__}: {exc}")
            continue
        hit = 0
        for e in entries:
            if tickers is not None and e.ticker not in tickers:
                continue
            if e.ticker not in found:
                found[e.ticker] = e
                hit += 1
        if on_progress:
            on_progress(d, len(entries), f"命中 {hit}")
    return found
