"""追蹤表讀寫。

這張表現在是給人看的，不是給機器讀的。實測到的問題：
  - 4 個分頁（= 4 個季度），欄位排列會漂：某個分頁少一欄，
    導致「產業」欄裡裝的是人名（Brian、子宸）
  - ticker 寫法不一致：'CITI' vs 'C (Citi)'；'CITI' 不是有效代號（應為 C）
  - 日期沒有年份，只有 '07/14  週二'
  - 7 檔完全沒有日期（IREN、CRDO、AVAV、ORCL、SMTC、MU、KEYS）

因此：**欄位一律靠表頭文字定位，不用固定索引。**
找不到表頭就明確報錯，不猜。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from googleapiclient.discovery import build

# 代號正規化：把人類寫法對回真正的美股代號
TICKER_ALIASES = {
    "CITI": "C",
    "C (CITI)": "C",
    "C(CITI)": "C",
    "UNITY": "U",
    "GOOGL": "GOOG",
}

_DATE_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})")
_TIME_RE = re.compile(r"(\d{1,2})\s*:\s*(\d{2})")


@dataclass
class Task:
    row: int                      # 1-based sheet row
    ticker: str
    raw_ticker: str
    call_at: dt.datetime | None   # 台灣時間，法說會「開始」時間
    ir_url: str = ""
    industry: str = ""
    owner: str = ""
    status: str = ""
    retry: int = 0
    error: str = ""
    skip_reason: str = ""
    report_done: bool = False
    podcast_done: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return not self.skip_reason and self.call_at is not None

    @property
    def needs_memo(self) -> bool:
        """人工已經把「報告」打 TRUE 的就不該再派工 —— 這是團隊 5 個人共用的欄位。"""
        return not self.report_done

    @property
    def needs_podcast(self) -> bool:
        return not self.podcast_done


class SheetsClient:
    def __init__(self, creds, config: dict):
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.cfg = config["sheet"]
        self.sid = self.cfg["spreadsheet_id"]
        self._tab_title: str | None = None
        self._header: dict[str, int] = {}
        self._header_row: int = self.cfg.get("header_row", 4)

    # ---------- 分頁定位 ----------

    def resolve_tab(self) -> str:
        """用 gid 定位分頁，不用名稱 —— 分頁名每季會改。"""
        if self._tab_title:
            return self._tab_title
        meta = self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        gid = self.cfg["active_tab_gid"]
        for sh in meta["sheets"]:
            if sh["properties"]["sheetId"] == gid:
                self._tab_title = sh["properties"]["title"]
                return self._tab_title
        raise LookupError(
            f"找不到 gid={gid} 的分頁。現有分頁："
            + ", ".join(
                f"{s['properties']['title']}(gid={s['properties']['sheetId']})"
                for s in meta["sheets"]
            )
        )

    def _values(self, rng: str) -> list[list[str]]:
        resp = (
            self.svc.spreadsheets()
            .values()
            .get(spreadsheetId=self.sid, range=rng, valueRenderOption="FORMATTED_VALUE")
            .execute()
        )
        return resp.get("values", [])

    def load_header(self) -> dict[str, int]:
        """靠表頭文字建立 {欄位鍵: 0-based 欄索引}。找不到的必填欄位直接報錯。"""
        if self._header:
            return self._header
        tab = self.resolve_tab()
        rows = self._values(f"'{tab}'!A1:Z20")

        wanted = dict(self.cfg["columns"])
        wanted.update(self.cfg["status_columns"])

        best_row, best_map = None, {}
        for idx, row in enumerate(rows, start=1):
            cells = [c.strip() for c in row]
            m = {
                key: cells.index(label)
                for key, label in wanted.items()
                if label in cells
            }
            if len(m) > len(best_map):
                best_row, best_map = idx, m

        required = ["ticker", "date", "time_tw"]
        missing = [r for r in required if r not in best_map]
        if missing:
            raise LookupError(
                f"分頁『{tab}』找不到必要欄位 {missing}（掃描前 20 列）。"
                f"表頭可能又漂了，請確認後更新 config.json 的 sheet.columns。"
            )

        self._header_row = best_row
        self._header = best_map
        return best_map

    def ensure_status_columns(self) -> dict[str, int]:
        """auto_status / auto_checked_at / auto_retry / auto_error 不存在就補在最右邊。"""
        hdr = self.load_header()
        tab = self.resolve_tab()
        missing = {k: v for k, v in self.cfg["status_columns"].items() if k not in hdr}
        if not missing:
            return hdr

        header_vals = self._values(f"'{tab}'!A{self._header_row}:Z{self._header_row}")
        cur = header_vals[0] if header_vals else []
        next_col = len(cur)

        updates = []
        for key, label in missing.items():
            hdr[key] = next_col
            updates.append(
                {
                    "range": f"'{tab}'!{_a1(next_col)}{self._header_row}",
                    "values": [[label]],
                }
            )
            next_col += 1

        self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sid,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()
        self._header = hdr
        return hdr

    # ---------- 讀任務 ----------

    def load_tasks(self, *, year: int) -> list[Task]:
        hdr = self.load_header()
        tab = self.resolve_tab()
        rows = self._values(f"'{tab}'!A{self._header_row + 1}:Z500")

        tasks: list[Task] = []
        for offset, row in enumerate(rows):
            rownum = self._header_row + 1 + offset
            get = lambda k: (row[hdr[k]].strip() if k in hdr and hdr[k] < len(row) else "")

            raw = get("ticker")
            if not raw or raw.lower() in ("ticker", "d"):
                continue

            ticker = normalize_ticker(raw)
            call_at, skip = parse_call_datetime(get("date"), get("time_tw"), year=year)

            tasks.append(
                Task(
                    row=rownum,
                    ticker=ticker,
                    raw_ticker=raw,
                    call_at=call_at,
                    ir_url=get("ir_url"),
                    industry=get("industry"),
                    owner=get("owner"),
                    status=get("status"),
                    retry=int(get("retry") or 0),
                    error=get("error"),
                    skip_reason=skip,
                    report_done=get("report_done").upper() == "TRUE",
                    podcast_done=get("podcast_done").upper() == "TRUE",
                )
            )
        return tasks

    # ---------- 超連結 ----------

    def load_hyperlinks(self, column_key: str = "ir_url") -> dict[int, str]:
        """取回某一欄的**超連結目標**，回傳 {列號: url}。

        為什麼需要這個：追蹤表的 `con-call 官網連結` 欄有 42% 不是純文字網址，
        而是用 Ctrl+K 貼的超連結（顯示成「GM Investor Relations」這種標題文字）。
        values.get 只拿得到顯示文字，URL 會掉。真正的網址藏在
        cellData.hyperlink 或 textFormatRuns[].format.link.uri。
        """
        hdr = self.load_header()
        if column_key not in hdr:
            return {}
        col = hdr[column_key]
        tab = self.resolve_tab()

        resp = (
            self.svc.spreadsheets()
            .get(
                spreadsheetId=self.sid,
                ranges=[f"'{tab}'!{_a1(col)}1:{_a1(col)}500"],
                includeGridData=True,
                fields=(
                    "sheets/data/rowData/values/hyperlink,"
                    "sheets/data/rowData/values/textFormatRuns/format/link/uri,"
                    "sheets/data/rowData/values/userEnteredValue/formulaValue"
                ),
            )
            .execute()
        )

        out: dict[int, str] = {}
        rows = resp["sheets"][0]["data"][0].get("rowData", [])
        for i, row in enumerate(rows, start=1):
            vals = row.get("values") or []
            if not vals:
                continue
            cell = vals[0]
            url = cell.get("hyperlink")
            if not url:
                for run in cell.get("textFormatRuns", []) or []:
                    uri = (run.get("format") or {}).get("link", {}).get("uri")
                    if uri:
                        url = uri
                        break
            if not url:
                # =HYPERLINK("url","顯示文字") 的情況
                f = (cell.get("userEnteredValue") or {}).get("formulaValue") or ""
                m = re.search(r'HYPERLINK\(\s*"([^"]+)"', f, re.I)
                if m:
                    url = m.group(1)
            if url:
                out[i] = url
        return out

    # ---------- 寫狀態 ----------

    def write_status(
        self,
        task: Task,
        *,
        status: str,
        error: str | None = "",
        retry: int | None = None,
        podcast_done: bool | None = None,
        report_done: bool | None = None,
        owner: str | None = None,
    ) -> None:
        """error 的三態：字串＝寫入（含空字串＝清除）；None＝完全不碰該欄。

        auto_error 欄除了錯誤訊息，還承載成功歸檔時的「缺漏」註記（逐字稿未公開
        之類）。只想動勾選框的呼叫（sweep 補勾、事後補資料）務必傳 error=None ——
        2026-08-13 踩過：補 Podcast 勾時用預設值，把三檔的缺漏註記全洗掉了。
        """
        hdr = self.ensure_status_columns()
        tab = self.resolve_tab()
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

        cells: dict[str, object] = {"status": status, "checked_at": now}
        if error is not None:
            cells["error"] = error[:500]
        if retry is not None:
            cells["retry"] = str(retry)
        if owner is not None:
            cells["owner"] = owner

        # 「Podcast」與「報告」是**核取方塊**（dataValidation = BOOLEAN），
        # 儲存格內容是 boolValue，不是字串。寫入字串 "TRUE" 雖然一樣顯示 TRUE，
        # 但會把該格從勾選框變成文字，破壞原表格形式。
        # 所以送真正的 JSON boolean，並用 USER_ENTERED 讓 Sheets 照型別解讀。
        for key, val in (("podcast_done", podcast_done), ("report_done", report_done)):
            if val is not None:
                cells[key] = bool(val)

        data = [
            {"range": f"'{tab}'!{_a1(hdr[k])}{task.row}", "values": [[v]]}
            for k, v in cells.items()
            if k in hdr
        ]
        if data:
            self.svc.spreadsheets().values().batchUpdate(
                spreadsheetId=self.sid,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            ).execute()


# ---------- 純函式（可單元測試，不需要網路） ----------


def normalize_ticker(raw: str) -> str:
    t = raw.strip().upper()
    t = re.sub(r"\s*\(.*?\)\s*", "", t).strip() or raw.strip().upper()
    return TICKER_ALIASES.get(raw.strip().upper(), TICKER_ALIASES.get(t, t))


def parse_call_datetime(
    date_cell: str, time_cell: str, *, year: int
) -> tuple[dt.datetime | None, str]:
    """回傳 (台灣時間的 datetime, skip_reason)。

    表上沒有年份，由呼叫端指定季度所屬年份。
    12 月→1 月跨年的分頁必須分開處理，不要在這裡猜。
    """
    if not date_cell.strip():
        return None, "no_date"

    dm = _DATE_RE.search(date_cell)
    if not dm:
        return None, f"bad_date:{date_cell[:20]}"

    tm = _TIME_RE.search(time_cell)
    if not tm:
        return None, "no_time"

    month, day = int(dm.group(1)), int(dm.group(2))
    hour, minute = int(tm.group(1)), int(tm.group(2))
    try:
        return dt.datetime(year, month, day, hour, minute), ""
    except ValueError as exc:
        return None, f"bad_datetime:{exc}"


def _a1(col_idx: int) -> str:
    """0-based 欄索引 → A1 欄名（支援超過 Z）。"""
    s, n = "", col_idx + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
