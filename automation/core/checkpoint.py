"""每檔每季的檢查點 —— 讓流程可以從中斷處續跑，而不是每次重頭。

問題：整條線有 7 個階段，跨越網路查詢、LLM 產出、Drive 上傳。
任何一步失敗或人為中斷，重跑時若沒有狀態，就會把已完成的昂貴步驟再做一次
（重新產 memo、重新產 podcast），而且可能造成重複資料
（實戰教訓：VST 分頁被重複寫入過一次）。

狀態存成 state/checkpoints/{TICKER}_{QUARTER}.json，一檔一季一份。
每個階段記 done / at / detail，detail 存下游需要的產物
（例如 podcast_uploaded 存 file_id 與 url，memo 分頁要用）。

為什麼不只靠追蹤表的 auto_status：
  - 表上一列只能表達「整體到哪」，表達不了「7 個階段各自的狀態」
  - 表是共用的，5 個人也在改
  - 表上存不了 file_id / tab_id 這些續跑必需的產物
追蹤表仍是給人看的單一真相；檢查點是給程式續跑用的細節。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

# 階段定義：順序即依賴順序
STAGES = [
    ("date_confirmed", "財報日期已確認", "Nasdaq + 官網 agent 核對"),
    ("readiness", "資料已就緒", "新聞稿 + 逐字稿"),
    ("memo_started", "開始製作 memo", "dispatcher 開工標記；同時只允許一檔"),
    ("memo_built", "memo 已產出", "earnings-memo skill → JSON + .docx"),
    ("podcast_built", "podcast 已產出", "NotebookLM → .m4a"),
    ("podcast_uploaded", "podcast 已上傳", "Drive；產出 url 供分頁引用"),
    ("memo_tab_written", "memo 分頁已寫入", "Google Doc 新分頁"),
    ("sheet_updated", "追蹤表已回寫", "TRUE/TRUE + auto_status"),
    ("line_notified", "已推播 LINE 群組", "memo 連結 + 摘要 + podcast 語音，單次 push"),
]
STAGE_KEYS = [s[0] for s in STAGES]
STAGE_LABEL = {k: label for k, label, _ in STAGES}
# 整條線的終點。complete 以它為準（理由見 Checkpoint.complete）。
# **刻意不改成 line_notified**：改了會讓所有既有 checkpoint 在同一秒變成未完成，
# 也會讓「LINE 還沒設定好」這件事把整條線判成沒跑完。推播是通知，不是產出。
FINAL_STAGE = "sheet_updated"


@dataclass
class Checkpoint:
    ticker: str
    quarter: str
    path: Path
    data: dict

    # ---------- 讀 ----------

    def is_done(self, stage: str) -> bool:
        self._check(stage)
        return bool(self.data["stages"].get(stage, {}).get("done"))

    def detail(self, stage: str) -> dict:
        self._check(stage)
        return self.data["stages"].get(stage, {}).get("detail") or {}

    def at(self, stage: str) -> str:
        """該階段被標記的時間（ISO 字串）。沒標過回空字串。"""
        self._check(stage)
        return self.data["stages"].get(stage, {}).get("at") or ""

    def next_stage(self) -> str | None:
        """第一個還沒完成的階段。整條線完成回 None。"""
        if self.complete:
            return None
        for k in STAGE_KEYS:
            if not self.is_done(k):
                return k
        return None

    @property
    def done_count(self) -> int:
        return sum(1 for k in STAGE_KEYS if self.is_done(k))

    @property
    def complete(self) -> bool:
        """整條線走完 —— 以最後一個階段為準，不是「八格全滿」。

        刻意不用 done_count == len(STAGE_KEYS)。分母是執行時算的，
        所以每次新增階段，所有既有的 checkpoint 檔都會在同一秒變成未完成，
        dispatcher 的「已完成就不再打 SEC/Fool」保險會整批失效
        （memo_started 上線時就會踩到：OKLO/RKLB/VST 三檔本來是滿格）。
        改看終點階段之後，既有檔案不必遷移也不必改寫。

        副作用是好的：人工用 publish_memo.py 直接發佈、沒有走過前面階段的
        （例如 CEG），現在也會被正確認定為完成而跳過。
        """
        return self.is_done(FINAL_STAGE)

    @property
    def in_progress(self) -> bool:
        """製作中 —— 已開工但 memo 還沒產出。全線只允許一檔處於這個狀態。"""
        return self.is_done("memo_started") and not self.is_done("memo_built")

    # ---------- 寫 ----------

    def mark(self, stage: str, *, done: bool = True, **detail) -> "Checkpoint":
        self._check(stage)
        self.data["stages"][stage] = {
            "done": done,
            "at": dt.datetime.now().isoformat(timespec="seconds"),
            "detail": detail or None,
        }
        self.save()
        return self

    def fail(self, stage: str, reason: str) -> "Checkpoint":
        return self.mark(stage, done=False, error=reason)

    def reset(self, stage: str | None = None) -> "Checkpoint":
        """reset(None) 清掉全部；reset(stage) 只清該階段**及其之後**的階段。

        清掉之後的階段是刻意的：前面重做了，後面的產物就不該還算數。
        """
        if stage is None:
            self.data["stages"] = {}
        else:
            self._check(stage)
            for k in STAGE_KEYS[STAGE_KEYS.index(stage):]:
                self.data["stages"].pop(k, None)
        # 唯一該真的刪掉階段的地方：不能合併，否則剛清掉的階段會從磁碟長回來。
        self.save(merge=False)
        return self

    def save(self, *, merge: bool = True) -> None:
        """寫回檔案。預設先合併磁碟上的版本，不整份覆寫。

        同一個檢查點檔會被多個進程寫：dispatcher 持著自己的 Checkpoint 物件跑完
        一整檔（動輒 20 分鐘），期間 make_podcast / publish_memo 這兩個子進程也在
        mark 同一個檔。整份覆寫會讓父進程最後那一筆 mark 把子進程寫的階段全部吃掉。

        2026-08-12 SMCI 實際發生：podcast 產出、上傳、分頁寫入三個階段都成功，
        但父進程收尾標 sheet_updated 時用的是 09:15 載入的舊快照，三個階段被抹掉。
        後果不只是看板顯示錯 —— sweep_podcasts 看到 podcast_uploaded 沒完成，
        會把已經在 Drive 上的音檔再傳一次。

        合併規則：同一階段以 `at` 較新的為準；磁碟上有、記憶體沒有的一律保留。
        reset() 是唯一要真的刪掉階段的情境，它會傳 merge=False 繞過。
        """
        if merge and self.path.exists():
            try:
                disk = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                disk = {}
            for k, v in (disk.get("stages") or {}).items():
                mine = self.data["stages"].get(k)
                if mine is None or (v.get("at") or "") > (mine.get("at") or ""):
                    self.data["stages"][k] = v

        self.data["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _check(stage: str) -> None:
        if stage not in STAGE_KEYS:
            raise KeyError(f"未知階段 {stage!r}；合法值：{STAGE_KEYS}")

    # ---------- 顯示 ----------

    def bar(self) -> str:
        # 完成的一律全綠：舊檔沒有 memo_started，逐格畫會在中間開一個洞
        if self.complete:
            return "●" * len(STAGE_KEYS)
        return "".join("●" if self.is_done(k) else "○" for k in STAGE_KEYS)

    def summary(self) -> str:
        nxt = self.next_stage()
        tail = "完成" if nxt is None else f"待辦：{STAGE_LABEL[nxt]}"
        n = len(STAGE_KEYS) if self.complete else self.done_count
        return f"{self.ticker:6s} {self.quarter:8s} {self.bar()} {n}/{len(STAGE_KEYS)}  {tail}"


def _path(state_dir: str | Path, ticker: str, quarter: str) -> Path:
    safe = quarter.replace(" ", "_").replace("/", "-")
    return Path(state_dir) / "checkpoints" / f"{ticker.upper()}_{safe}.json"


def load(state_dir: str | Path, ticker: str, quarter: str) -> Checkpoint:
    p = _path(state_dir, ticker, quarter)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        data = {"ticker": ticker.upper(), "quarter": quarter, "stages": {}}
    return Checkpoint(ticker.upper(), quarter, p, data)


def load_all(state_dir: str | Path) -> list[Checkpoint]:
    d = Path(state_dir) / "checkpoints"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        out.append(Checkpoint(data["ticker"], data["quarter"], p, data))
    return out
