"""季度標籤推導。

規則：財報日期所屬的季度 = **該日期之前最近一個已結束的曆季**。
  VST 2026-08-07 發表 → 最近結束的曆季是 2026 Q2 → 「VST 2026Q2」 ✓

已知未解問題（config 裡也記了一筆）：
非曆年財政年度的公司，公司自己的季度標籤和曆季不同。
現有 Google Doc 分頁命名本身就不一致：
    NVDA 2027Q1 / NVDA 2026Q4FY / NVDA 2025Q3
其中「NVDA 2025Q3」的日期是 20251120，對應的其實是 NVDA 的 FY2026 Q3。
在使用者定下規則前，一律用曆季，並把推導結果印出來讓人看得見。
"""

from __future__ import annotations

import datetime as dt

# 這些公司財政年度非曆年，曆季標籤會與公司自報季度不符，派工時提醒
NON_CALENDAR_FY = {
    "NVDA", "AVGO", "MU", "ORCL", "CRM", "ADBE", "COST", "WMT", "HPQ", "HPE",
    "DELL", "MRVL", "SNOW", "CRWD", "ZS", "OKTA", "WDAY", "PANW", "INTU",
    "SWKS", "AEHR", "JBL", "TTMI", "NKE", "FDX", "LULU", "DOCU", "GTLB",
    "S", "PATH", "MDB", "SEZL", "TOST",
}


def calendar_quarter_for(report_date: dt.date) -> tuple[int, int]:
    """回傳 (年, 季)：report_date 之前最近一個『已結束』的曆季。"""
    q = (report_date.month - 1) // 3 + 1
    y = report_date.year
    q -= 1
    if q == 0:
        q, y = 4, y - 1
    return y, q


def _shift_months(d: dt.date, months: int) -> dt.date:
    """往前／後移 n 個月，日超過該月天數就取當月最後一天。"""
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    last = [31, 29 if (y % 4 == 0 and y % 100 != 0) or y % 400 == 0 else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return dt.date(y, m, min(d.day, last))


def fiscal_quarter_for(report_date: dt.date, fy_end: str) -> tuple[int, int]:
    """回傳 (財政年, 財政季)：report_date 之前最近一個『已結束』的財政季。

    fy_end 是 SEC submissions 的 fiscalYearEnd，格式 MMDD（SMCI='0630'）。
    財政年標籤採公司自己的講法 —— 財政年度**結束**的那個曆年，
    這也是既有分頁的慣例（SMCI FY26 Q4 → 分頁『SMCI 2026Q4』）。

    做法是從財政年度結束日往回每 3 個月抓一個季末，挑出報告日之前最近的那個。
    不用「月份除以 3」那種算法：MU（0903）、AVGO（1101）這種季末落在月中的公司，
    財報常常跟季末同一個曆月發布，純月份運算會整整差一季。
    """
    m, d = int(fy_end[:2]), int(fy_end[2:])
    best: tuple[dt.date, int, int] | None = None
    for fy in range(report_date.year - 2, report_date.year + 3):
        year_end = _shift_months(dt.date(fy, m, 1), 0).replace(
            day=min(d, [31, 29 if (fy % 4 == 0 and fy % 100 != 0) or fy % 400 == 0
                        else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]))
        for q in (1, 2, 3, 4):
            qe = _shift_months(year_end, -3 * (4 - q))
            if qe < report_date and (best is None or qe > best[0]):
                best = (qe, fy, q)
    assert best is not None
    return best[1], best[2]


def tab_name(ticker: str, report_date: dt.date, template: str = "{ticker} {year}Q{q}",
             fy_end: str | None = None) -> str:
    """分頁名。給了 fy_end 就用財政季度，否則退回曆季。

    使用者 2026-08-12 定案：非曆年財政年度的公司一律用財政季度，跟既有分頁一致。
    曆季會撞車 —— SMCI 今天這份 FY26 Q4 用曆季算出來是『SMCI 2026Q2』，
    指到的卻是今年二月那份 FY26 Q2。
    財政年度結束月份取自 SEC submissions 的 fiscalYearEnd，不手維護清單。
    """
    if fy_end:
        y, q = fiscal_quarter_for(report_date, fy_end)
    else:
        y, q = calendar_quarter_for(report_date)
    return template.format(ticker=ticker, year=y, q=q)


def local_folder_label(report_date: dt.date) -> str:
    """本機 MEMO 資料夾用的短標籤，例如 2Q26（沿用既有慣例）。"""
    y, q = calendar_quarter_for(report_date)
    return f"{q}Q{y % 100:02d}"


def fy_warning(ticker: str) -> str | None:
    if ticker.upper() in NON_CALENDAR_FY:
        return (f"{ticker} 財政年度非曆年，曆季標籤可能與公司自報季度不符"
                f"（例：NVDA 11 月發的是公司 FY Q3，曆季算 Q3，但既有分頁寫過 2027Q1）")
    return None
