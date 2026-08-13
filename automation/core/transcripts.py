"""逐字稿偵測 —— 來源不限，誰先上稿就用誰。

原本這一層只查 Motley Fool，而 Fool 的覆蓋率不夠：2026-08-12 早上 CRWV／SMCI／
LITE 三檔財報都發了、逐字稿也都出來了，Fool 上一篇都沒有，三檔全卡在 waiting。

改成「全網搜尋 + 標題判讀」之後，同一時間點三檔都抓得到（2026-08-12 07:45 實測）：

    CRWV  MarketBeat    CoreWeave Q2 Earnings Call Highlights
    SMCI  Yahoo Finance SUPER MICRO COMPUTER INC (SMCI.MX) Q4 FY2026 earnings call transcript
    LITE  Investing.com Earnings call transcript: Lumentum tops Q4 2026 forecasts …

實測可用的來源（2026-08-12）：

    Google News RSS   ✅ 全網、免登入、0.5 秒，涵蓋 Investing／Yahoo／SA／Fool／MarketBeat
    Yahoo headline RSS ✅ 綁代號、回真實文章網址（Google News 回的是轉址）
    Motley Fool        ✅ 唯一能解析出「季度＋發佈日」的來源，保留為高信心訊號
    Nasdaq API 404 ／ discountingcashflows 403 ／ Seeking Alpha 403 ／ Investing.com 直連是 JS 殼

判讀規則（precision 比 recall 重要 —— 誤判會讓 memo 引到別家公司的電話會議）：

  1. 標題要有**強訊號**字樣：transcript／call highlights／prepared remarks。
     只寫 "earnings call" 不算 —— 「…Ahead Of Earnings Call」是預告不是逐字稿。
  2. 標題要指到本公司：代號獨立字詞，或 SEC 公司名的關鍵詞。
     （只比代號會漏掉「Lumentum tops Q4…」這種只寫公司名的標題。）
  3. 排除預告字樣（ahead of／preview／will report…）。
  4. 發佈日不得早於法說會前一天。

Google News 的連結是 news.google.com 轉址，內容是 JS 殼 —— 給 agent 當線索可以，
**不要**餵給 NotebookLM（core/sources.py 會濾掉）。
"""

from __future__ import annotations

import datetime as dt
import email.utils
import re
import urllib.parse
from dataclasses import dataclass

from core.net import BROWSER_UA, fetch

GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={tk}&region=US&lang=en-US"

_ITEM = re.compile(r"<item>(.*?)</item>", re.S)

# 強訊號：確定是「會議內容」而不是「會議預告」
STRONG = re.compile(r"(transcript|call highlights|prepared remarks|earnings call recap)", re.I)
# 弱訊號：只當線索列出，不足以判定就緒
WEAK = re.compile(r"(earnings call|conference call|call presentation)", re.I)
# 預告稿：出現這些字一律不算
PREVIEW = re.compile(
    r"(ahead of|preview|imminent|what to expect|will report|to report|to announce"
    r"|before the (bell|open)|expectations for|forecasts? ahead|set to report)", re.I)

# 公司名去掉法人尾綴後才拿來比對標題
_STOP = {"inc", "inc.", "corp", "corp.", "corporation", "company", "co", "co.", "ltd",
         "ltd.", "limited", "plc", "holdings", "holding", "group", "the", "sa", "nv",
         "ag", "se", "class", "common", "stock", "technologies", "technology"}


@dataclass
class Hit:
    date: dt.date | None
    title: str
    url: str
    domain: str
    strong: bool

    def line(self) -> str:
        return f"{self.date or '?'} [{self.domain}] {self.title}"


def name_keywords(company: str) -> list[str]:
    """'Lumentum Holdings Inc.' → ['Lumentum']；'Super Micro Computer, Inc.' → ['Super','Micro']"""
    words = [w for w in re.sub(r"[^A-Za-z0-9 &]", " ", company or "").split()
             if w.lower() not in _STOP and len(w) > 1]
    return words[:3]


def _tag(name: str, blob: str) -> str:
    m = re.search(rf"<{name}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", blob, re.S)
    return (m.group(1) if m else "").strip()


def _unescape(s: str) -> str:
    for a, b in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">")):
        s = s.replace(a, b)
    return s


def _rss(url: str, *, timeout: int = 20) -> list[tuple[dt.date | None, str, str, str]]:
    body = fetch(url, ua=BROWSER_UA, timeout=timeout, retries=2).decode("utf-8", "ignore")
    out = []
    for blob in _ITEM.findall(body):
        raw = _tag("pubDate", blob)
        try:
            when = email.utils.parsedate_to_datetime(raw).date()
        except Exception:
            when = None
        link = _tag("link", blob)
        src = re.search(r'<source url="([^"]+)"', blob)
        domain = urllib.parse.urlparse(src.group(1) if src else link).netloc
        out.append((when, _unescape(_tag("title", blob)), link, domain.replace("www.", "")))
    return out


def _relevant(title: str, ticker: str, kw: list[str]) -> bool:
    """標題指的是不是本公司。"""
    if re.search(rf"(?<![A-Za-z]){re.escape(ticker)}(?![A-Za-z])", title):
        return True
    if kw and re.search(rf"\b{re.escape(' '.join(kw[:2]))}\b", title, re.I):
        return True
    return bool(kw) and bool(re.search(rf"\b{re.escape(kw[0])}\b", title, re.I))


def _leading_token_ok(title: str, ticker: str, kw: list[str]) -> bool:
    """擋掉「BLZE Q2 Earnings Call Focuses on CoreWeave…」這種別家公司的會議。

    標題開頭若是另一個全大寫代號（且不是我們的代號、也不是公司名的字），
    那篇講的是別人的電話會議，只是內文提到我們。
    """
    m = re.match(r"([A-Z]{2,5})\b", title.strip())
    if not m:
        return True
    lead = m.group(1)
    if lead == ticker.upper():
        return True
    return any(lead.lower() == w.lower() for w in kw)


def find(ticker: str, *, company: str = "", on_or_after: dt.date | None = None,
         limit: int = 6) -> list[Hit]:
    """回傳逐字稿線索，強訊號排前面。任何一個來源掛掉都不影響其他來源。"""
    tk = ticker.strip().upper()
    kw = name_keywords(company)

    queries = [f'"{tk}" earnings call transcript when:7d']
    if kw:
        queries.append(f'"{" ".join(kw[:2])}" earnings call transcript when:7d')

    raw: list[tuple[dt.date | None, str, str, str]] = []
    for q in queries:
        try:
            raw += _rss(GOOGLE_NEWS.format(q=urllib.parse.quote(q)))
        except Exception:
            pass
    try:
        raw += _rss(YAHOO_RSS.format(tk=tk))
    except Exception:
        pass

    hits: list[Hit] = []
    seen: set[str] = set()
    for when, title, link, domain in raw:
        if not title or title in seen:
            continue
        if on_or_after and when and when < on_or_after - dt.timedelta(days=1):
            continue
        if PREVIEW.search(title):
            continue
        if not _relevant(title, tk, kw) or not _leading_token_ok(title, tk, kw):
            continue
        strong = bool(STRONG.search(title))
        if not strong and not WEAK.search(title):
            continue
        seen.add(title)
        hits.append(Hit(when, title, link, domain, strong))

    # 強訊號優先；同強度下**真實網址**優先於 news.google.com 轉址殼
    # （轉址殼要 JS 才還原得出內容，agent 與 NotebookLM 都讀不到）；最後才比新舊。
    hits.sort(key=lambda h: (not h.strong,
                             "news.google.com" in h.url,
                             -(h.date or dt.date.min).toordinal()))
    return hits[:limit]


def confirmed(hits: list[Hit]) -> list[Hit]:
    return [h for h in hits if h.strong]
