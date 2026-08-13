"""SEC EDGAR：財報新聞稿的權威訊號。

實測結果（2026-08-11 抓的真實資料）：

    VST  2026-08-07  8-K  items='2.02,9.01'   ← 財報新聞稿，日期精準命中
    RKLB 2026-08-10  8-K  items='2.02,9.01'   ← 同上
    ASML 2026-07-15  6-K  items=''            ← 外國發行人「沒有 item 代碼」

⇒ 美國發行人用 8-K item 2.02 判斷，精準。
⇒ 外國發行人（ASML、SAP、STM、NOK、NVO、TEVA、ARM、SE、GRAB、MELI、CPNG、NU…）
   只有 6-K 且無 item 分類，只能靠「日期接近 + 主文件名關鍵字」推測，
   信心度較低，必須標記出來，不能假裝跟 8-K 一樣可靠。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

import re

from core.net import SEC_UA, fetch, fetch_json

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

EARNINGS_ITEM = "2.02"  # Results of Operations and Financial Condition
FOREIGN_FORMS = {"6-K"}
DOMESTIC_FORMS = {"8-K"}
# 有些公司不發 8-K item 2.02，新聞稿只放自家網站，直接交 10-Q。
# 實測：OKLO 2Q26 只在 2026-08-07 交 10-Q，近期沒有任何 item 2.02 的 8-K。
# 10-Q/10-K 本身就代表數字已公開，是同等強度的訊號。
PERIODIC_FORMS = {"10-Q", "10-K"}

# 6-K 主文件名裡出現這些字，才當成「可能是財報」
FOREIGN_DOC_HINTS = ("quarterly", "results", "earnings", "interim", "q1", "q2", "q3", "q4")

# 6-K 沒有 item 代碼，判斷「這份是不是財報」只能推測。三層訊號由強到弱，任一命中就採信：
#   ① reportDate 標成季末（2026-03-31）而非申報日 —— 只有財報 6-K 會標所屬期間
#   ② 主文件名帶財報字樣（nbis-20260331x6k.htm）
#   ③ 抓內文的 INDEX TO EXHIBITS 看附件描述
#
# 為什麼③非有不可（2026-08-12 實測 NBIS）：NBIS 一年發 28 份 6-K，檔名幾乎都是
# tm2621374d1_6k.htm 這種代編號，②永遠不會命中；①只有財報那份成立，但那是觀察到的
# 慣例、不是規定。沒有③的話 NBIS 這種外國發行人會等滿 72 小時後被放棄。
#
# 附件描述的鑑別力（實測同一家三份 6-K）：
#   財報       → "Operating and Financial Review"、"Unaudited Condensed Consolidated
#                 Financial Statements"、"Three Months Ended March 31"
#   股東會通知  → "Notice, Agenda and Explanatory Notes"
#   併購新聞稿  → "Press release ... announcing the closing of the acquisition"
# 所以 "press release" **不能**當關鍵字 —— 併購那份也是 press release。
EARNINGS_TEXT_HINTS = (
    "operating and financial review",
    "financial statements",
    "financial results",
    "results of operations",
    "unaudited condensed",
    "unaudited interim",
    "interim financial",
    "earnings release",
    "quarter ended",
    "quarter and",
    "three months ended",
    "six months ended",
    "nine months ended",
    "twelve months ended",
    "half year",
    "first quarter",
    "second quarter",
    "third quarter",
    "fourth quarter",
)
# 只中一個字就採信太鬆（"financial statements" 在一堆併購文件裡也會出現），要兩個以上
MIN_TEXT_HITS = 2
# 6-K 內文判讀的快取：每 15 分鐘一輪，同一份不該重抓
SNIFF_CACHE = "sec_6k_sniff.json"
_CONFIDENCE_RANK = {"": 0, "low": 1, "medium": 2, "high": 3}


@dataclass
class Release:
    found: bool
    form: str = ""
    filing_date: str = ""
    accession: str = ""
    url: str = ""
    confidence: str = ""      # "high" = 8-K/2.02；"low" = 6-K 推測
    detail: str = ""


class Edgar:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._map: dict[str, int] | None = None
        self._subs: dict[int, dict] = {}
        self._names: dict[str, str] | None = None

    # ---------- ticker → CIK ----------

    def ticker_map(self, *, max_age_days: int = 7) -> dict[str, int]:
        if self._map is not None:
            return self._map
        cache = self.state_dir / "sec_ticker_cik.json"
        fresh = (
            cache.exists()
            and (dt.datetime.now().timestamp() - cache.stat().st_mtime) < max_age_days * 86400
        )
        if fresh:
            self._map = json.loads(cache.read_text(encoding="utf-8"))
        else:
            raw = fetch_json(TICKER_MAP_URL, ua=SEC_UA, sec_throttle=True)
            self._map = {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}
            cache.write_text(json.dumps(self._map, indent=0), encoding="utf-8")
        return self._map

    def cik(self, ticker: str) -> int | None:
        return self.ticker_map().get(ticker.strip().upper())

    # ---------- submissions（同一輪內共用，別重打 SEC）----------

    def submissions(self, cik: int) -> dict:
        if cik not in self._subs:
            self._subs[cik] = fetch_json(
                SUBMISSIONS_URL.format(cik=cik), ua=SEC_UA, sec_throttle=True
            )
        return self._subs[cik]

    def fiscal_year_end(self, ticker: str) -> str | None:
        """公司財政年度結束日，MMDD（SMCI='0630'、NVDA='0131'）。查不到回 None。

        分頁季度要用財政季度算（使用者 2026-08-12 定案），這是唯一的權威來源 ——
        比手維護一份非曆年公司清單可靠，那種清單今天就漏掉了 SMCI 和 LITE。

        查不到不能擋住流程：外國發行人、剛上市的、SEC 對照表還沒更新的都可能查無，
        回 None 讓上層退回曆季，行為跟導入財政季度之前一樣。
        """
        try:
            cik = self.cik(ticker)
            if not cik:
                return None
            fy = (self.submissions(cik) or {}).get("fiscalYearEnd") or ""
            return fy if len(fy) == 4 and fy.isdigit() else None
        except Exception:
            return None

    def company_name(self, ticker: str) -> str:
        """SEC 登記的公司全名 —— 逐字稿偵測要用它比對新聞標題。

        新聞標題寫的是公司名不是代號（「Lumentum tops Q4…」而非 LITE），
        只比對代號會漏掉大半。名稱幾乎不變，永久快取。
        """
        tk = ticker.strip().upper()
        cache = self.state_dir / "sec_company_names.json"
        if self._names is None:
            try:
                self._names = json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                self._names = {}
        if tk in self._names:
            return self._names[tk]

        cik = self.cik(tk)
        if cik is None:
            return ""
        try:
            name = self.submissions(cik).get("name", "") or ""
        except Exception:
            return ""
        self._names[tk] = name
        try:
            cache.write_text(json.dumps(self._names, ensure_ascii=False, indent=0), encoding="utf-8")
        except Exception:
            pass
        return name

    # ---------- 財報新聞稿偵測 ----------

    def find_earnings_release(
        self, ticker: str, *, on_or_after: dt.date, window_days: int = 5
    ) -> Release:
        cik = self.cik(ticker)
        if cik is None:
            return Release(False, detail=f"{ticker} 不在 SEC ticker 清單（可能是 OTC/ADR 或代號有誤）")

        data = self.submissions(cik)
        recent = data["filings"]["recent"]
        cutoff = on_or_after + dt.timedelta(days=window_days)

        best_foreign: Release | None = None
        best_periodic: Release | None = None
        all_forms = DOMESTIC_FORMS | FOREIGN_FORMS | PERIODIC_FORMS

        for i in range(len(recent["form"])):
            form = recent["form"][i]
            if form not in all_forms:
                continue
            try:
                fdate = dt.date.fromisoformat(recent["filingDate"][i])
            except ValueError:
                continue
            if fdate < on_or_after or fdate > cutoff:
                continue

            acc = recent["accessionNumber"][i].replace("-", "")
            doc = recent["primaryDocument"][i]
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

            if form in DOMESTIC_FORMS:
                items = (recent["items"][i] or "").split(",")
                if EARNINGS_ITEM in [x.strip() for x in items]:
                    return Release(
                        True, form, fdate.isoformat(), recent["accessionNumber"][i], url,
                        confidence="high",
                        detail=f"8-K item {EARNINGS_ITEM}（Results of Operations）",
                    )
            elif form in PERIODIC_FORMS:
                if best_periodic is None:
                    best_periodic = Release(
                        True, form, fdate.isoformat(), recent["accessionNumber"][i], url,
                        confidence="high",
                        detail=f"{form} 已申報（數字已公開；此公司未發 8-K item {EARNINGS_ITEM}）",
                    )
            else:
                rdate = (recent.get("reportDate") or [""] * len(recent["form"]))[i]
                cand = self._foreign_release(
                    form, fdate, rdate, doc, url, recent["accessionNumber"][i]
                )
                if cand and (
                    best_foreign is None
                    or _CONFIDENCE_RANK[cand.confidence] > _CONFIDENCE_RANK[best_foreign.confidence]
                ):
                    best_foreign = cand

        # 優先序：8-K/2.02（上面直接 return）> 10-Q/10-K > 6-K 推測
        return best_periodic or best_foreign or Release(
            False, detail=f"{on_or_after}~{cutoff} 之間找不到財報申報"
        )

    # ---------- 6-K 判讀（外國發行人專用）----------

    @staticmethod
    def _is_period_end(rdate: str, fdate: dt.date) -> bool:
        """reportDate 標的是「所屬期間結束日」而不是申報日 → 這份 6-K 帶財務期間。

        財報 6-K：filingDate 2026-05-20 / reportDate 2026-03-31
        其他 6-K：兩者相同（或差一兩天）
        """
        try:
            d = dt.date.fromisoformat(rdate)
        except (ValueError, TypeError):
            return False
        if (fdate - d).days < 7:      # 跟申報日同期 → 只是「發生日」，不是財務期間
            return False
        nxt = d + dt.timedelta(days=1)
        return nxt.day == 1 and nxt.month in (1, 2, 4, 5, 7, 8, 10, 11)

    def _sniff_cache(self) -> dict:
        p = self.state_dir / SNIFF_CACHE
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _doc_hits(self, url: str, accession: str) -> list[str]:
        """抓 6-K 內文，回傳命中的財報用語。抓不到就回空陣列（不能讓它擋住流程）。"""
        cache = self._sniff_cache()
        if accession in cache:
            return cache[accession].get("hits", [])
        try:
            raw = fetch(url, ua=SEC_UA, sec_throttle=True, timeout=30)[:400_000]
        except Exception:
            return []      # 抓不到不記快取 —— 下一輪還要再試
        text = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "replace"))
        text = re.sub(r"&#?[a-zA-Z0-9]+;", " ", text)
        text = re.sub(r"\s+", " ", text).lower()
        hits = sorted({h for h in EARNINGS_TEXT_HINTS if h in text})
        cache[accession] = {"hits": hits, "url": url}
        try:
            (self.state_dir / SNIFF_CACHE).write_text(
                json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
        except Exception:
            pass
        return hits

    def _foreign_release(self, form: str, fdate: dt.date, rdate: str,
                         doc: str, url: str, accession: str) -> Release | None:
        """一份 6-K 是不是財報。三層訊號，命中最強的那層。"""
        def rel(conf: str, why: str) -> Release:
            return Release(True, form, fdate.isoformat(), accession, url,
                           confidence=conf,
                           detail=f"6-K 無 item 代碼，{why}——外國發行人只能到這個程度")

        if self._is_period_end(rdate, fdate):
            return rel("medium", f"reportDate 標為期末 {rdate}（非申報日）")
        if any(h in doc.lower() for h in FOREIGN_DOC_HINTS):
            return rel("low", f"主文件名帶財報字樣（{doc}）")
        hits = self._doc_hits(url, accession)
        if len(hits) >= MIN_TEXT_HITS:
            return rel("low", f"內文附件描述命中 {len(hits)} 個財報用語（{'、'.join(hits[:3])}）")
        return None

    def is_foreign_filer(self, ticker: str) -> bool | None:
        """看最近的申報是 6-K/20-F 還是 8-K/10-Q。None = 查不到。"""
        cik = self.cik(ticker)
        if cik is None:
            return None
        data = self.submissions(cik)
        forms = set(data["filings"]["recent"]["form"][:60])
        if forms & {"10-Q", "10-K", "8-K"}:
            return False
        if forms & {"6-K", "20-F", "40-F"}:
            return True
        return None
