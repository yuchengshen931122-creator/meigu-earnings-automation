"""替 podcast 蒐集免費來源 —— 對齊人工流程的選材廣度。

skill 的人工流程會挑：IR 新聞稿、PR Newswire/GlobeNewswire/Business Wire 鏡像、
Yahoo Finance、Motley Fool、公司 webcast 頁。自動化要能做到同一件事。

實測哪些真的可用（2026-08-11，對 CRWV/VST/OKLO 測）：

    StockTitan 新聞頁      ✅ 可抓，且能挖出**具體文章網址**（新聞稿鏡像）
    Motley Fool quote 頁   ✅ 可抓，能挖出具體逐字稿網址
    SEC EDGAR             ✅ 申報文件本身
    PR Newswire 搜尋頁     ⚠️ 抓得到，但是清單頁
    Yahoo /press-releases ❌ 404
    Yahoo quote 頁         ❌ JS 渲染，純文字裡沒有內容
    GlobeNewswire 搜尋     ❌ 逾時
    Business Wire 搜尋     ❌ 403

**清單頁不要餵給 NotebookLM。** 實測 CEG：IR 首頁那種清單頁會被標成
status=error 而靜默排除，等於白佔一個來源位。只送具體文章網址。

StockTitan 會 429，所以請求之間強制節流。
"""

from __future__ import annotations

import datetime as dt
import re
import threading
import time

from core.net import BROWSER_UA, fetch

_ST_LOCK = threading.Lock()
_ST_LAST = [0.0]
ST_MIN_GAP = 3.0          # 實測連續 3 次就 429

STOCKTITAN_NEWS = "https://www.stocktitan.net/news/{ticker}/"
ST_ARTICLE_RE = re.compile(r'href="(/news/{tk}/([a-z0-9\-]{{10,140}})\.html)"')

# 只認人類可讀的部分；網址尾端的雜湊（…-q35vjxogw6q9）會誤中 q1–q4
EARNINGS_WORDS = re.compile(
    r"(earning|results|first-quarter|second-quarter|third-quarter|fourth-quarter"
    r"|q[1-4]-20\d\d|20\d\d-q[1-4]|financial-results|reports-(first|second|third|fourth))",
    re.I,
)

# 財報「預告」稿：只說哪天要發，沒有任何數字。餵進去是白佔來源位。
ANNOUNCEMENT = re.compile(r"(to-report|to-announce|to-host|announces-date|will-report|schedule)", re.I)

ORDINAL = {1: "first", 2: "second", 3: "third", 4: "fourth"}


def _quarter_ok(slug: str, want_q: int | None, want_y: int | None) -> bool:
    """slug 是否對得上目標季度。

    沒有這道檢查，StockTitan 會把**上一季**的財報稿也撈進來
    （實測 VST 2Q26 撈到 vistra-reports-first-quarter-2026），
    餵給 NotebookLM 會讓節目內容錯季。
    """
    if not want_q:
        return True
    s = slug.lower()
    mine = [f"{ORDINAL[want_q]}-quarter", f"q{want_q}-{want_y}", f"{want_y}-q{want_q}"]
    others = [f"{ORDINAL[q]}-quarter" for q in ORDINAL if q != want_q]
    others += [f"q{q}-{want_y}" for q in ORDINAL if q != want_q]
    if any(o in s for o in others):
        return False              # 明確是別季 → 排除
    if any(m in s for m in mine):
        return True               # 明確是本季 → 收
    return want_y is None or str(want_y) in s or True  # 沒標季度的泛用稿，保留


def _throttle() -> None:
    with _ST_LOCK:
        gap = time.time() - _ST_LAST[0]
        if gap < ST_MIN_GAP:
            time.sleep(ST_MIN_GAP - gap)
        _ST_LAST[0] = time.time()


def _strip_hash(slug: str) -> str:
    """`vistra-reports-second-quarter-2026-x3f6lqo6ne3k` → 去掉尾端雜湊段。"""
    parts = slug.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) >= 8 and any(c.isdigit() for c in parts[1]):
        return parts[0]
    return slug


def stocktitan_articles(ticker: str, *, limit: int = 2,
                        want_q: int | None = None, want_y: int | None = None) -> list[str]:
    """公司新聞稿的免費鏡像。回傳看起來是財報的具體文章網址。"""
    _throttle()
    try:
        html = fetch(STOCKTITAN_NEWS.format(ticker=ticker.upper()),
                     ua=BROWSER_UA, timeout=25, retries=1).decode("utf-8", "ignore")
    except Exception:
        return []

    pat = re.compile(ST_ARTICLE_RE.pattern.format(tk=re.escape(ticker.upper())))
    out, seen = [], set()
    for path, slug in pat.findall(html):
        if path in seen:
            continue
        seen.add(path)
        clean = _strip_hash(slug)
        if not EARNINGS_WORDS.search(clean):
            continue
        if ANNOUNCEMENT.search(clean):
            continue              # 只是預告哪天發財報，沒有數字
        if not _quarter_ok(clean, want_q, want_y):
            continue              # 別季的財報稿
        out.append(f"https://www.stocktitan.net{path}")
        if len(out) >= limit:
            break
    return out


def fool_transcript(ticker: str, *, on_or_after: dt.date | None = None) -> list[str]:
    """Motley Fool 逐字稿的具體文章網址（沿用就緒偵測已驗證過的解析）。"""
    from core.readiness import ReadinessChecker

    try:
        hits = ReadinessChecker._fool_transcripts(ticker)
    except Exception:
        return []
    out = []
    for pub, url, _q, _y in hits:
        if on_or_after and pub < on_or_after - dt.timedelta(days=1):
            continue
        out.append(url)
        break                      # 只要最新那一篇
    return out


def discover(ticker: str, *, call_date: dt.date | None = None,
             extra: list[str] | None = None, ir_url: str = "",
             want_q: int | None = None, want_y: int | None = None,
             ) -> tuple[list[str], list[str]]:
    """回傳 (可送出的來源, 說明行)。

    extra 是上游已知的網址（就緒偵測抓到的 SEC 申報與逐字稿）—— 一律保留。
    ir_url 刻意**不**加入：追蹤表上那欄多半是 IR 首頁或活動清單頁，
    實測那種頁面 NotebookLM 抓不到內容。
    """
    notes: list[str] = []
    urls: list[str] = []

    for u in extra or []:
        if u not in urls:
            urls.append(u)
    if urls:
        notes.append(f"上游既有 {len(urls)} 個（SEC 申報／逐字稿）")

    st = stocktitan_articles(ticker, want_q=want_q, want_y=want_y)
    for u in st:
        if u not in urls:
            urls.append(u)
    notes.append(f"StockTitan 新聞稿鏡像 {len(st)} 篇" if st else "StockTitan 查無財報新聞稿")

    if not any("fool.com" in u for u in urls):
        fl = fool_transcript(ticker, on_or_after=call_date)
        for u in fl:
            if u not in urls:
                urls.append(u)
        notes.append(f"Fool 逐字稿 {len(fl)} 篇" if fl else "Fool 查無逐字稿")

    if ir_url:
        notes.append("IR 網址未採用（多為清單頁，NotebookLM 抓不到內容）")

    # 付費牆一律排除 —— NotebookLM 伺服器端抓取沒有登入態。
    # news.google.com 是轉址殼（內容靠 JS 還原），抓下來是空的，同樣排除；
    # 它只在就緒偵測裡當「逐字稿存在」的線索用，不是可讀來源。
    blocked = [u for u in urls
               if "seekingalpha.com" in u.lower() or "news.google.com" in u.lower()]
    urls = [u for u in urls if u not in blocked]
    if blocked:
        notes.append(f"排除付費牆／轉址殼 {len(blocked)} 個")

    return urls, notes
