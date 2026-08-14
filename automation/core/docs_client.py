"""把 memo 寫進既有 Google Doc 的「新分頁」。

歸檔慣例（使用者確認）：
  Word 樹（config.drive.word_tree_id）底下每個 ticker 一份 Google Doc『{TICKER} 財報』，
  每一季**新增一個分頁**，不產生新的 .docx。

分頁內容格式（照 NVDA 財報現況）：
    NVDA 2025Q3          ← 標題
    20251120             ← 財報日期
    podcast link：
    <Drive 連結>
    重點摘要
    ...

⇒ **順序依賴**：podcast 必須先上傳拿到 Drive 連結，memo 分頁才寫得出來。

API 能力已實測確認（查 Docs v1 discovery document，非憑記憶）：
  - `addDocumentTab`：可程式化新增分頁，回傳新分頁的 tabId
  - `Location.tabId` / `EndOfSegmentLocation.tabId`：可把寫入導向指定分頁
  - `documents.get(includeTabsContent=True)`：可讀出既有分頁清單
"""

from __future__ import annotations

import re

from googleapiclient.discovery import build

from core import docx_to_docs

MEMO_DOC_SUFFIX = "財報"  # 文件命名慣例：『{TICKER} 財報』


def _u16(s: str) -> int:
    """Docs API 的 index 以 UTF-16 code unit 計。中文是 1 單位，emoji 是 2。"""
    return len(s.encode("utf-16-le")) // 2


def _norm_name(s: str) -> str:
    """檔名正規化：所有空白（含全形空格、NBSP、tab）折成一個半形空格，去頭尾。"""
    return re.sub(r"\s+", " ", s).strip()


class DocsClient:
    def __init__(self, creds, drive_client):
        self.svc = build("docs", "v1", credentials=creds, cache_discovery=False)
        self.drive = drive_client

    # ---------- 找文件 ----------

    def find_memo_doc(self, ticker: str, tree_root_id: str) -> dict | None:
        """在 Word 樹裡找『{TICKER} 財報』。找不到回 None（由呼叫端決定要不要建）。

        比對不用 Drive 的 `name =` 精確查詢，改抓候選後在本地做「去空白全等」——
        人工命名的檔案有空白變體（同一份 Doc 裡就有『AMAT  2025Q4』這種雙空格
        分頁名）。2026-08-14 AMAT 實際踩到：原有『AMAT 財報』名稱比對落空，
        流程靜靜建了第二份同名文件，memo 寫進新檔，使用者原有的檔案一字未動。
        """
        want = _norm_name(f"{ticker} {MEMO_DOC_SUFFIX}").replace(" ", "")
        safe = ticker.replace("'", "\\'")
        files, token = [], None
        while True:
            resp = self.drive._retry(
                lambda: self.drive.svc.files()
                .list(
                    q=(
                        f"name contains '{safe}' and trashed = false "
                        f"and mimeType = 'application/vnd.google-apps.document'"
                    ),
                    fields="nextPageToken, files(id, name, parents)",
                    pageSize=100,
                    pageToken=token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            files.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        hits = [f for f in files if _norm_name(f["name"]).replace(" ", "") == want]
        if not hits:
            return None
        if len(hits) > 1:
            # 同名多份是既有資料問題，不猜 —— 交給呼叫端報錯
            return {"_ambiguous": [f["id"] for f in hits], **hits[0]}
        return hits[0]

    # ---------- 分頁 ----------

    def list_tabs(self, doc_id: str) -> list[dict]:
        # 必須 includeTabsContent=True，否則回應根本不會帶 tabs 欄位，
        # list_tabs 會永遠回空 → ensure_tab 每次都當成「不存在」而重複建分頁。
        doc = self.svc.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute()
        out = []

        def walk(tabs):
            for t in tabs or []:
                p = t.get("tabProperties", {})
                out.append({"tabId": p.get("tabId"), "title": p.get("title"),
                            "index": p.get("index")})
                walk(t.get("childTabs"))

        walk(doc.get("tabs"))
        return out

    # ---------- 內容 ----------

    def ensure_tab(self, doc_id: str, title: str) -> tuple[str, bool]:
        """回傳 (tabId, created)。同名分頁已存在就沿用 —— 冪等，重跑不會建第二個。

        新分頁一律插在**最前面**（index 0）：這些 Doc 一檔累積十幾季，最新一季堆在
        最下面等於每次都要捲到底。使用者要求最新的在最上面。
        （API 保證：`addDocumentTab` 在指定 index 插入時，其後所有分頁的 index 自動 +1。）
        """
        for t in self.list_tabs(doc_id):
            if (t["title"] or "").strip() == title.strip():
                return t["tabId"], False

        resp = self.svc.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"addDocumentTab": {
                "tabProperties": {"title": title, "index": 0},
            }}]},
        ).execute()
        new_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
        return new_id, True

    def rename_tab(self, doc_id: str, tab_id: str, title: str) -> None:
        """改分頁標題。用在「採用既有單季檔」時：檔名降格成分頁名後，
        原本那個預設標題（Tab 1）的分頁要冠回原檔名，季度才認得出來。"""
        self.svc.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"updateDocumentTabProperties": {
                "tabProperties": {"tabId": tab_id, "title": title},
                "fields": "title",
            }}]},
        ).execute()

    def move_tab(self, doc_id: str, tab_id: str, index: int = 0) -> None:
        """把既有分頁搬到指定位置（預設最上面）。

        用來修正 2026-08-12 之前建立的分頁 —— 那時 addDocumentTab 沒帶 index，
        新分頁一律接在最後面，把「最新在最上面」的既有慣例弄反了。
        """
        self.svc.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"updateDocumentTabProperties": {
                "tabProperties": {"tabId": tab_id, "index": index},
                "fields": "index",
            }}]},
        ).execute()

    def tab_text(self, doc_id: str, tab_id: str) -> str:
        """讀出某分頁的純文字。用來判斷『這個分頁是不是已經有人寫過了』。"""
        doc = self.svc.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute()
        for t in _walk_tabs(doc.get("tabs", [])):
            if t.get("tabProperties", {}).get("tabId") == tab_id:
                content = t.get("documentTab", {}).get("body", {}).get("content", [])
                return "".join(
                    r.get("textRun", {}).get("content", "")
                    for e in content
                    for r in e.get("paragraph", {}).get("elements", [])
                )
        return ""

    def tab_content(self, doc_id: str, tab_id: str) -> list[dict]:
        """分頁的 body content（段落／表格／圖片都在裡面）。"""
        doc = self.svc.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute()
        for t in _walk_tabs(doc.get("tabs", [])):
            if t.get("tabProperties", {}).get("tabId") == tab_id:
                return t.get("documentTab", {}).get("body", {}).get("content", [])
        return []

    def clear_tab(self, doc_id: str, tab_id: str) -> int:
        """清空分頁內容，回傳刪掉的長度。覆寫前必須先清，否則會變成前後兩份並存。

        刪除範圍取自 body 最後一個元素的 endIndex，**不是**純文字長度 ——
        表格與圖片在 index 上佔位但不出現在 tab_text() 的字串裡，用文字長度當
        endIndex 會刪不乾淨，剩下的表格會跟新內容並存。
        """
        content = self.tab_content(doc_id, tab_id)
        if not content:
            return 0
        end = max(e.get("endIndex", 0) for e in content)
        n = end - 1        # body 結尾那個換行刪不得
        if n <= 1:
            return 0
        self.svc.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"deleteContentRange": {
                "range": {"tabId": tab_id, "startIndex": 1, "endIndex": end - 1},
            }}]},
        ).execute()
        return n

    # ---------- 寫內容：.docx 一比一 ----------

    def write_docx_tab(
        self,
        doc_id: str,
        tab_id: str,
        docx_path,
        *,
        podcast_url: str | None = None,
        image_parent: str | None = None,
    ) -> dict:
        """把 memo .docx 原封不動重建進分頁，只在標題／日期之後多插 podcast 連結。

        跟 write_memo_tab()（從 JSON 渲染）的差別是**內容完整**：JSON 渲染器看不到
        dashboard 表、type=table 的 rows、type=qa 的問答，會靜靜少掉一大半。
        細節見 core/docx_to_docs.py 的模組說明。
        """
        blocks = docx_to_docs.parse(docx_path)
        if podcast_url:
            blocks = _with_podcast_link(blocks, podcast_url)

        writer = docx_to_docs.TabWriter(self.svc, doc_id, tab_id)

        img_blocks = [b for b in blocks if b["kind"] == "img"]
        if img_blocks and image_parent:
            with docx_to_docs.hosted_image(
                self.drive, img_blocks[0]["blob"], image_parent, "_memo_image_tmp.png"
            ) as uri:
                stat = writer.write(blocks, image_uri=lambda b: uri)
        else:
            if img_blocks:
                print("   [!] 沒有可用的暫存資料夾，圖片本次不插入")
            stat = writer.write(blocks, image_uri=None)

        stat["source"] = "docx"
        return stat

    def verify_tab_matches(self, doc_id: str, tab_id: str, docx_path) -> dict:
        """讀回分頁，跟 .docx 逐字比對。回傳 {match, missing, tab_chars, docx_chars}。

        「寫進去了」跟「內容一致」是兩件事 —— 這次的缺漏就是前者成立、後者不成立
        卻被回報成功。所以歸檔完一定要讀回來對過。
        """
        blocks = docx_to_docs.parse(docx_path)
        want = docx_to_docs.normalize(docx_to_docs.plain_text(blocks))
        got = docx_to_docs.normalize(self.tab_all_text(doc_id, tab_id))
        missing = [
            line for line in
            (docx_to_docs.plain_text(blocks).split("\n"))
            if line.strip() and docx_to_docs.normalize(line) not in got
        ]

        # 只比文字會漏掉「圖沒進去」這種缺漏 —— 第一次跑就是文字全過但 0 張圖。
        content = self.tab_content(doc_id, tab_id)
        n_tab_tables = sum(1 for e in content if "table" in e)
        n_want_tables = sum(1 for b in blocks if b["kind"] == "table")
        n_want_images = sum(1 for b in blocks if b["kind"] == "img")
        n_tab_images = len(self._tab_inline_objects(doc_id, tab_id))

        if n_tab_tables != n_want_tables:
            missing.append(f"[表格數不符：分頁 {n_tab_tables}、memo {n_want_tables}]")
        if n_tab_images != n_want_images:
            missing.append(f"[圖片數不符：分頁 {n_tab_images}、memo {n_want_images}]")

        return {
            "match": not missing,
            "missing": missing,
            "docx_chars": len(want),
            "tab_chars": len(got),
            "tables": f"{n_tab_tables}/{n_want_tables}",
            "images": f"{n_tab_images}/{n_want_images}",
        }

    def _tab_inline_objects(self, doc_id: str, tab_id: str) -> dict:
        """分頁的內嵌物件。**注意在 documentTab 底下**，不是回應最上層的
        `inlineObjects` —— 看最上層永遠是 0，會把「圖沒插進去」誤判成正常。"""
        doc = self.svc.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute()
        for t in _walk_tabs(doc.get("tabs", [])):
            if t.get("tabProperties", {}).get("tabId") == tab_id:
                return t.get("documentTab", {}).get("inlineObjects") or {}
        return {}

    def tab_all_text(self, doc_id: str, tab_id: str) -> str:
        """分頁全文，含表格內的文字（tab_text() 只讀得到段落）。"""
        def walk(content) -> list[str]:
            out = []
            for e in content:
                if "paragraph" in e:
                    out.append("".join(
                        r.get("textRun", {}).get("content", "")
                        for r in e["paragraph"].get("elements", [])
                    ))
                elif "table" in e:
                    for row in e["table"].get("tableRows", []):
                        for cell in row.get("tableCells", []):
                            out.extend(walk(cell.get("content", [])))
            return out

        return "".join(walk(self.tab_content(doc_id, tab_id)))

    def ensure_podcast_link(self, doc_id: str, tab_id: str, url: str) -> dict:
        """在既有分頁補上 podcast 連結，不動其他任何內容。

        用於人工已經寫好 memo、只缺連結的分頁（實際上 VST/OKLO/RKLB 都是這種情形）。
        插在第 2 行（日期）之後，與 NVDA 分頁的版面一致。
        已經有連結就不重複插入。
        """
        # 音檔還沒產出時上層會傳 None（memo 先行歸檔的那條路）。不擋掉的話
        # 下面的 `url in text` 會丟 TypeError，而且它不是 PublishError、不會被
        # 攔截器接住，整支以 exit 1 收場。（2026-08-12 CRWV 實際踩到。）
        if not url:
            return {"inserted": False, "reason": "尚無 podcast 連結，本次不插入"}

        # 比對要不分大小寫，也要認得 "Podcast:" 這種舊寫法 —— 人工寫的分頁用的是
        # 『Podcast: https://notebooklm...』，只比對小寫的 "podcast link" 會判成
        # 沒有連結，於是又插一行，同一個分頁出現兩個 podcast 連結。
        # （2026-08-12 SMCI 二月分頁實際發生。）
        text = self.tab_text(doc_id, tab_id)
        low = text.lower()
        if "podcast link" in low or "podcast:" in low or "podcast：" in low or url in text:
            return {"inserted": False, "reason": "已有 podcast 連結"}

        lines = text.split("\n")
        if len(lines) < 2:
            return {"inserted": False, "reason": "分頁內容太短，找不到插入點"}

        # 第 1 行標題 + 第 2 行日期之後 = 兩行的長度（含各自的換行）
        offset = _u16(lines[0] + "\n" + lines[1] + "\n")
        payload = f"podcast link：\n{url}\n"

        self.svc.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {
                "location": {"tabId": tab_id, "index": 1 + offset},
                "text": payload,
            }}]},
        ).execute()
        return {"inserted": True, "at_index": 1 + offset, "chars": _u16(payload)}

    # ---------- 寫內容 ----------

    def write_memo_tab(
        self,
        doc_id: str,
        tab_id: str,
        memo: dict,
        *,
        podcast_url: str | None = None,
        report_date: str | None = None,
        title: str | None = None,
    ) -> dict:
        """把 earnings-memo 的 JSON 渲染進指定分頁。**退路，不是主路。**

        只認得 `tldr` 與 `sections[].items`：JSON 的 `dashboard`、`type=table` 的
        `rows`、`type=qa` 的 `themes`/`qa` 都不在這兩個 key 底下，會被靜靜丟掉，
        圖片也只留一行佔位文字。LITE 2026Q4 因此少了 4,204 字與 5 張表。
        有 .docx 就走 write_docx_tab()，那條路是逐字重建且寫完會驗。
        這支只在完全找不到 .docx 時使用。

        memo 結構（來自 MEMO/_work/*.json）：
          title, release_date,
          tldr: [[{text, bold}, ...], ...]
          sections: [{heading, type, items: [{segments: [{text, bold}]}]}]
        """
        text, bolds, headings = _render(memo, podcast_url, report_date, title=title)

        requests = [
            {
                "insertText": {
                    "location": {"tabId": tab_id, "index": 1},
                    "text": text,
                }
            }
        ]
        # 樣式必須在文字插入之後套用；index 以插入起點 1 為基準
        for start, end in bolds:
            requests.append(
                {
                    "updateTextStyle": {
                        "range": {"tabId": tab_id, "startIndex": 1 + start, "endIndex": 1 + end},
                        "textStyle": {"bold": True},
                        "fields": "bold",
                    }
                }
            )
        for start, end, style in headings:
            requests.append(
                {
                    "updateParagraphStyle": {
                        "range": {"tabId": tab_id, "startIndex": 1 + start, "endIndex": 1 + end},
                        "paragraphStyle": {"namedStyleType": style},
                        "fields": "namedStyleType",
                    }
                }
            )

        self.svc.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute()
        return {"chars": _u16(text), "bold_ranges": len(bolds), "headings": len(headings)}


def _with_podcast_link(blocks: list[dict], url: str) -> list[dict]:
    """在標題、日期之後插入兩行 podcast 連結，其餘一個字都不動。

    插在第 3、4 行是既有慣例（NVDA 分頁、ensure_podcast_link 都是這個位置）。
    """
    def para(text: str, *, link: str | None = None) -> dict:
        seg = {"text": text, "bold": False, "italic": False, "color": None, "size": 11.0}
        if link:
            seg["link"] = link
        return {"kind": "p", "runs": [seg], "named": "NORMAL_TEXT", "align": None,
                "list": None, "space_above": 0.0, "space_below": 0.0}

    # 標題與日期是 .docx 的前兩個段落；表格／圖片不可能出現在它們之前
    at = 0
    seen = 0
    for i, b in enumerate(blocks):
        if b["kind"] == "p":
            seen += 1
            if seen == 2:
                at = i + 1
                break
    return blocks[:at] + [para("podcast link："), para(url, link=url)] + blocks[at:]


def _fmt_date(raw: str) -> str:
    """統一成 YYYYMMDD（NVDA 分頁用的格式）。認不出來就原樣保留。"""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits if len(digits) == 8 else raw


def _render(
    memo: dict,
    podcast_url: str | None,
    report_date: str | None,
    *,
    title: str | None = None,
) -> tuple[str, list[tuple[int, int]], list[tuple[int, int, str]]]:
    """把 memo dict 攤平成 (純文字, 粗體區間, 段落樣式區間)，index 以 UTF-16 計。

    版面依 `NVDA 財報` 分頁：

        {TICKER} {YYYY}Q{n}     ← 用分頁名，不是 memo JSON 裡的公司全名
        {YYYYMMDD}
        podcast link：
        {Drive 連結}
        重點摘要
        1. …

    註：既有分頁其實六個季度六種寫法（標題有的用中文公司名、日期有 3 種格式、
    podcast 連結有的指向 notebooklm 有的沒有），並無既存標準。
    這裡統一採 NVDA 那版 —— 它是唯一用 Drive 連結的，而 Drive 連結正是本流程的產物。
    """
    parts: list[str] = []
    bolds: list[tuple[int, int]] = []
    headings: list[tuple[int, int, str]] = []
    pos = 0

    def emit(s: str, *, bold: bool = False, style: str | None = None) -> None:
        nonlocal pos
        n = _u16(s)
        if bold and s.strip():
            bolds.append((pos, pos + n))
        if style and s.strip():
            headings.append((pos, pos + n, style))
        parts.append(s)
        pos += n

    emit((title or memo.get("title", "")) + "\n", style="HEADING_1")
    emit(_fmt_date(report_date or memo.get("release_date", "")) + "\n")

    if podcast_url:
        emit("podcast link：\n")
        emit(podcast_url + "\n")

    if memo.get("tldr"):
        emit("重點摘要\n", style="HEADING_2")
        for i, item in enumerate(memo["tldr"], 1):
            emit(f"{i}.  ")
            for seg in _segments(item):
                emit(seg.get("text", ""), bold=seg.get("bold", False))
            emit("\n")

    for sec in memo.get("sections", []):
        emit(sec.get("heading", "") + "\n", style="HEADING_2")
        for i, item in enumerate(sec.get("items", []), 1):
            emit(f"{i}.  ")
            for seg in _segments(item):
                emit(seg.get("text", ""), bold=seg.get("bold", False))
            emit("\n")

    img = memo.get("income_statement_image") or {}
    if img.get("caption"):
        # 圖片本身需另外處理：insertInlineImage 只吃「公開可存取的 URI」，
        # 本機 PNG 不能直接插入。要插圖必須先上傳 Drive 並暫時開放連結權限，
        # 那會改動檔案的共享設定 —— 刻意不預設執行，留給 phase 2 明確決定。
        emit(f"[損益表圖片待插入] {img['caption']}\n")

    return "".join(parts), bolds, headings


def _walk_tabs(tabs):
    for t in tabs or []:
        yield t
        yield from _walk_tabs(t.get("childTabs"))


def _segments(item) -> list[dict]:
    if isinstance(item, dict):
        return item.get("segments", [])
    if isinstance(item, list):
        return item
    return [{"text": str(item), "bold": False}]
