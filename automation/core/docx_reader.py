"""從 memo .docx 反推出結構化內容，讓沒有 JSON 的舊 memo 也能寫進 Google Doc 分頁。

背景：`publish_memo.py` 是從 earnings-memo skill 的結構化 JSON 渲染分頁的，
但 JSON 是後來才有的慣例 —— `MEMO/2Q26/` 底下 OKLO 與 RKLB 只有 .docx，
只有 VST 有 JSON。

輸出刻意與 JSON 同一個 shape，`docs_client._render()` 兩者通吃：

    {title, release_date, tldr: [[{text,bold}]], sections: [{heading, items:[{segments}]}]}

.docx 的結構（實測 OKLO / RKLB 2Q26）：
    段落 0        標題（粗體）
    段落 1        日期
    List Number   重點摘要的編號項目
    Normal(粗體)  表格的標籤，例如「財務結果 vs 市場預期」
    表格          數字明細，緊接在上面那個標籤之後
    Heading 1     章節標題（一、整體財務表現…）
    Normal        章節內文

兩個必須按順序走訪 body 才能處理對的地方：
  1. 表格標籤是 Normal 段落，若只看段落會被誤當成重點摘要項目
     （OKLO 因此一度變成 13 項，實際只有 7 項）
  2. 「四、財務預測」這類章節的內容整個在表格裡，只讀段落會得到 0 項
python-docx 的 `document.paragraphs` 不含表格，且拿不到兩者的相對順序，
所以這裡直接走 `document.element.body`。
"""

from __future__ import annotations

from pathlib import Path

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph

TLDR_STYLES = {"List Number", "List Paragraph"}


def _iter_body(doc):
    """按文件順序產出 Paragraph 與 Table。"""
    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            yield Paragraph(child, doc)
        elif tag == "tbl":
            yield Table(child, doc)


def _segments(par: Paragraph) -> list[dict]:
    """把 runs 併成 {text, bold} 片段，相鄰同粗細的合併。"""
    out: list[dict] = []
    for r in par.runs:
        if not r.text:
            continue
        bold = bool(r.bold)
        if out and out[-1]["bold"] == bold:
            out[-1]["text"] += r.text
        else:
            out.append({"text": r.text, "bold": bold})
    if not out and par.text.strip():
        out = [{"text": par.text, "bold": False}]
    return out


def _table_lines(tbl: Table) -> list[str]:
    """表格轉成每列一行，欄以 ' | ' 分隔。空列略過。"""
    lines = []
    for row in tbl.rows:
        cells = [c.text.strip().replace("\n", " ") for c in row.cells]
        # 合併儲存格會讓同一格重複出現，去掉連續重複
        dedup = [c for i, c in enumerate(cells) if i == 0 or c != cells[i - 1]]
        line = " | ".join(c for c in dedup if c)
        if line:
            lines.append(line)
    return lines


def read(path: str | Path) -> dict:
    doc = docx.Document(str(path))

    title = ""
    release_date = ""
    tldr: list[list[dict]] = []
    sections: list[dict] = []
    current: dict | None = None
    pending_label: str | None = None   # 表格前面那個粗體標籤
    seen = 0
    n_tables = 0

    for item in _iter_body(doc):
        if isinstance(item, Table):
            n_tables += 1
            lines = _table_lines(item)
            if not lines:
                continue
            target = current["items"] if current else None
            if target is None:
                # 還在重點摘要區：表格獨立成一個章節，用它的標籤當標題
                current = {"heading": pending_label or "資料表", "type": "table", "items": []}
                sections.append(current)
                target = current["items"]
                current = None if False else current
            if pending_label:
                target.append({"segments": [{"text": pending_label, "bold": True}]})
                pending_label = None
            for ln in lines:
                target.append({"segments": [{"text": ln, "bold": False}]})
            continue

        text = item.text.strip()
        if not text:
            continue
        style = item.style.name or ""
        seen += 1

        if seen == 1:
            title = text
            continue
        if seen == 2:
            release_date = text
            continue

        if style.startswith("Heading"):
            current = {"heading": text, "type": "bullets", "items": []}
            sections.append(current)
            pending_label = None
            continue

        if current is None:
            if style in TLDR_STYLES:
                tldr.append(_segments(item))
            else:
                # 重點摘要區裡的 Normal 段落 = 後面那張表格的標籤，不是摘要項目
                pending_label = text
            continue

        current["items"].append({"segments": _segments(item)})

    return {
        "title": title,
        "release_date": release_date,
        "tldr": tldr,
        "sections": sections,
        "_source": "docx",
        "_dropped": {"images": len(doc.inline_shapes), "tables_read": n_tables},
    }
