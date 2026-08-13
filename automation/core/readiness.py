"""就緒偵測 —— 整套系統的關鍵閘門。

使用者定的規則：表上的時間是法說會**開始**時間，那時候什麼資料都還沒有，
所以不能一到時間就派工，必須先確認資料到位。判準：新聞稿 + 逐字稿。
10-Q 刻意不列入阻塞條件（常延後數日，外國發行人根本不發 10-Q）。

各訊號的實測可靠度（2026-08-11 對真實資料測的，不是猜的）：

  新聞稿  SEC 8-K item 2.02          高  美國發行人，日期精準命中
          SEC 10-Q/10-K              高  不發 8-K 的公司（如 OKLO）
          SEC 6-K 檔名關鍵字          低  外國發行人無 item 代碼，只能推測
  逐字稿  fool.com /quote/{ex}/{tk}/  高  能解析出季度與發佈日，但**覆蓋不完整**
          全網搜尋（core/transcripts） 中  Google News RSS + Yahoo RSS，來源不限

逐字稿改成「來源不限」的理由（2026-08-12 實測）：Fool 只是眾多上稿者之一，
且往往不是最快的。CRWV／SMCI／LITE 三檔財報 08-11 盤後就發、逐字稿當晚也都出來了
（Yahoo Finance、Investing.com、MarketBeat），Fool 一篇都沒有，三檔在本層全卡成
waiting。改成全網搜尋後同一時間點三檔都判得出來。細節見 core/transcripts.py。

軟性逾時（ready_partial 逃生門）可用 transcript_soft_cutoff_hours 控制：
數字＝新聞稿確認後等這麼多小時仍無逐字稿就先派工；**null＝逃生門關閉，
逐字稿沒確認就永不派工**（使用者 2026-08-13 定案：品質優先，Q&A 必須來自
真正的逐字稿，不再用新聞報導重建）。關閉時的配套：dispatcher 會在等超過
24 小時的當口推一次 LINE 提醒 —— 冷門股可能永遠沒有免費逐字稿，
要不要放行改用新聞稿先做，是人的決定，不該無聲空等。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from core import transcripts
from core.edgar import Edgar, Release
from core.net import BROWSER_UA, fetch

FOOL_QUOTE = "https://www.fool.com/quote/{exchange}/{ticker}/"
FOOL_EXCHANGES = ("nyse", "nasdaq")
FOOL_TRANSCRIPT_RE = re.compile(
    r"/earnings/call-transcripts/(\d{4})/(\d{2})/(\d{2})/"
    r"([a-z0-9\-]+-q(\d)-(\d{4})-earnings-call-transcript)/"
)

# 狀態
WAIT = "wait"
READY = "ready"
READY_PARTIAL = "ready_partial"
GIVE_UP = "give_up"
NOT_YET = "not_yet"


@dataclass
class Signal:
    ok: bool
    source: str = ""
    detail: str = ""
    confidence: str = ""


@dataclass
class Readiness:
    ticker: str
    state: str
    press_release: Signal = field(default_factory=lambda: Signal(False))
    transcript: Signal = field(default_factory=lambda: Signal(False))
    reason: str = ""

    @property
    def dispatchable(self) -> bool:
        return self.state in (READY, READY_PARTIAL)

    def as_row(self) -> str:
        pr = "✓" if self.press_release.ok else "✗"
        tr = "✓" if self.transcript.ok else "✗"
        return f"{self.ticker:6s} 新聞稿{pr} 逐字稿{tr}  {self.state:13s} {self.reason}"


class ReadinessChecker:
    def __init__(self, edgar: Edgar, cfg: dict):
        self.edgar = edgar
        rc = cfg["readiness"]
        self.min_wait = dt.timedelta(minutes=rc["earliest_check_after_call_minutes"])
        cutoff = rc.get("transcript_soft_cutoff_hours", 6)
        #: None＝逃生門關閉：逐字稿未確認就永不派工（見模組 docstring）
        self.soft_cutoff = None if cutoff is None else dt.timedelta(hours=cutoff)
        self.give_up_after = dt.timedelta(hours=rc["give_up_after_hours"])

    def check(
        self, ticker: str, call_at: dt.datetime, *, now: dt.datetime | None = None
    ) -> Readiness:
        now = now or dt.datetime.now()
        elapsed = now - call_at

        if elapsed < dt.timedelta(0):
            mins = int(-elapsed.total_seconds() // 60)
            return Readiness(ticker, NOT_YET, reason=f"法說會還沒開始（{mins} 分鐘後）")

        if elapsed < self.min_wait:
            return Readiness(
                ticker, WAIT,
                reason=f"法說會才過 {int(elapsed.total_seconds() // 60)} 分鐘，"
                       f"未達最短等待 {int(self.min_wait.total_seconds() // 60)} 分鐘",
            )

        pr = self._check_press_release(ticker, call_at.date())
        if not pr.ok:
            state = GIVE_UP if elapsed > self.give_up_after else WAIT
            hrs = self.give_up_after.total_seconds() / 3600
            reason = (f"等超過 {hrs:.0f} 小時仍無財報申報，放棄"
                      if state == GIVE_UP else f"等待新聞稿：{pr.detail[:70]}")
            return Readiness(ticker, state, pr, Signal(False, detail="新聞稿未出，未查逐字稿"), reason)

        tr = self._check_transcript(ticker, call_at.date())

        if tr.ok:
            return Readiness(ticker, READY, pr, tr, "新聞稿＋逐字稿齊備，可派工")

        # 新聞稿已確認就永不放棄 —— 數字已經公開，memo 一定做得出來。
        # give_up 只適用於「連財報都還沒發」的情況（上面已處理）。
        # 這也讓系統停機數天後回來，仍會補跑而不是直接丟掉。
        if self.soft_cutoff is not None and elapsed > self.soft_cutoff:
            mins = self.soft_cutoff.total_seconds() / 60
            return Readiness(
                ticker, READY_PARTIAL, pr, tr,
                f"新聞稿已確認、逾 {mins:.0f} 分鐘全網仍搜不到逐字稿 → 立即派工，"
                f"由 agent 自行尋找（本層偵測：{tr.detail[:90]}；"
                f"agent 有瀏覽器與搜尋，進得去 Seeking Alpha 等本層進不去的站）",
            )

        hrs = elapsed.total_seconds() / 3600
        return Readiness(ticker, WAIT, pr, tr,
                         f"新聞稿已出，等逐字稿（已等 {hrs:.1f} 小時；{tr.detail[:70]}）")

    # ---------- 個別訊號 ----------

    def _check_press_release(self, ticker: str, call_date: dt.date) -> Signal:
        try:
            # 盤前財報的 8-K 可能前一天就報備，往前留一天
            rel: Release = self.edgar.find_earnings_release(
                ticker, on_or_after=call_date - dt.timedelta(days=1)
            )
        except Exception as exc:
            return Signal(False, "edgar", f"查詢失敗：{type(exc).__name__}: {exc}")
        if not rel.found:
            return Signal(False, "edgar", rel.detail)
        return Signal(
            True, f"edgar/{rel.form}",
            f"{rel.filing_date} {rel.detail} {rel.url}", confidence=rel.confidence,
        )

    def _check_transcript(self, ticker: str, call_date: dt.date) -> Signal:
        """只回答「這一季的逐字稿出現了沒」，不抓內容 —— 內容由 skill 抓。

        來源不限：先問 Fool（唯一能解析出季度＋發佈日的來源，信心最高），
        沒有就全網搜尋（core/transcripts）。誰先上稿就用誰 —— 目標是最快開工。
        """
        fool_note = ""
        try:
            hits = self._fool_transcripts(ticker)
        except Exception as exc:
            fool_note = f"Fool 查詢失敗（{type(exc).__name__}）"
            hits = []

        for pub, url, quarter, year in hits:
            if pub >= call_date - dt.timedelta(days=1):
                return Signal(True, "fool", f"{pub} Q{quarter} {year} {url}", confidence="high")
        if hits and not fool_note:
            fool_note = f"Fool 最新只到 {hits[0][0]}（Q{hits[0][2]} {hits[0][3]}）"
        elif not fool_note:
            fool_note = "Fool 查無此代號"

        # ---- 全網搜尋 ----
        try:
            company = self.edgar.company_name(ticker)
        except Exception:
            company = ""
        try:
            found = transcripts.find(ticker, company=company, on_or_after=call_date)
        except Exception as exc:
            return Signal(False, "web", f"{fool_note}；全網搜尋失敗：{type(exc).__name__}: {exc}")

        strong = transcripts.confirmed(found)
        if strong:
            detail = " ｜ ".join(f"{h.line()} {h.url}" for h in strong[:3])
            return Signal(True, "web", detail, confidence="medium")

        if found:
            return Signal(
                False, "web",
                f"{fool_note}；全網只找到相關報導、沒有逐字稿："
                + " ｜ ".join(h.line() for h in found[:2]),
            )
        return Signal(False, "web", f"{fool_note}；全網也查無本季逐字稿")

    @staticmethod
    def _fool_transcripts(ticker: str) -> list[tuple[dt.date, str, str, str]]:
        """回傳 [(發佈日, url, 季, 年)]，新到舊。"""
        for ex in FOOL_EXCHANGES:
            try:
                html = fetch(
                    FOOL_QUOTE.format(exchange=ex, ticker=ticker.lower()),
                    ua=BROWSER_UA, timeout=25, retries=2,
                ).decode("utf-8", "ignore")
            except Exception:
                continue
            out = []
            for y, m, d, slug, q, fy in FOOL_TRANSCRIPT_RE.findall(html):
                try:
                    pub = dt.date(int(y), int(m), int(d))
                except ValueError:
                    continue
                out.append((pub, f"https://www.fool.com/earnings/call-transcripts/{y}/{m}/{d}/{slug}/", q, fy))
            if out:
                return sorted(set(out), key=lambda r: r[0], reverse=True)
        return []
