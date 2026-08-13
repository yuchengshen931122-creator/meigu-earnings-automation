"""把 memo .docx 一比一重建進 Google Doc 分頁。

**為什麼不從 JSON 渲染。**
`docs_client._render()` 只認得 `tldr` 與 `sections[].items`。memo JSON 的
`dashboard`（4 張表）、`type=table` 的 `rows`、`type=qa` 的 `themes`/`qa` 全都落在
它看不到的 key 上，靜靜被丟掉——LITE 2026Q4 因此少了 4,204 字、5 張表與損益表
圖片（分頁 4,848 字 vs 本機 docx 8,339 字）。補 JSON 渲染器只會讓兩套格式繼續
分岔；`.docx` 才是使用者眼中的 memo 本體，直接照它重建，「分頁＝memo」就是結構
保證，不是靠兩邊同步維護出來的。

**寫入分批，順序不能改：**

    A  insertText 整份骨架（表格／圖片先放佔位符；巢狀項目前綴 \\t）
    B  createParagraphBullets（會吃掉 \\t —— 文字長度只在這一批改變）
    C  段落樣式 → 文字樣式（namedStyleType 會重設 run 樣式，必須先段落後文字）
    D  佔位符換成真表格／真圖片
    E  填表格內容
    F  欄寬

B 之後長度就固定了。C／D／E／F 之前各重讀一次文件拿真實 index，不用算的——
算 index 是這支東西唯一會出錯的地方，能用讀的就不要用算的。
同一批之內凡是會位移 index 的請求，一律**由後往前**下，前面的 index 才不會失效。

**已知會不同的地方**（Google Docs 沒有對應能力，非缺漏）：
  - 字型 Microsoft JhengHei 不下，Docs 沒有這個字型，硬塞只會 fallback 得更難看
  - 項目符號縮排用 Docs 的預設值（18/36pt）而非 Word 的 36/72pt，符號本身
    ● ○ ■ 是對的
"""

from __future__ import annotations

import contextlib
import re
import shutil
import tempfile
from pathlib import Path

import docx
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph

EMU_PER_PT = 12700
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# 佔位符：必須是 memo 內文絕不會出現的字串。實際會 assert 檢查，撞到就換一組。
TBL_MARK = "«TBL{}»"
IMG_MARK = "«IMG{}»"

# Word numbering.xml 的 abstractNum 900 是 ● ○ ■ 三層，正好對上 Docs 這個 preset
BULLET_PRESET = "BULLET_DISC_CIRCLE_SQUARE"
NUMBER_PRESET = "NUMBERED_DECIMAL_ALPHA_ROMAN"


# ---------------------------------------------------------------- 解析 .docx


def _pt(v) -> float | None:
    return None if v is None else v / EMU_PER_PT


def _numbering(par: DocxParagraph) -> tuple[str, int] | None:
    """回傳 (kind, level)。kind 為 'bullet' / 'number'，都不是就 None。

    兩種來源要分開看：
      - 段落自帶 numPr（本 memo 的章節內文，numId=900 → ● ○ ■）
      - 樣式帶的 numPr（重點摘要用的 'List Number' 樣式 → 1. 2. 3.）
    只看段落會漏掉後者，只看樣式會漏掉前者。
    """
    pr = par._p.find(W + "pPr")
    if pr is not None:
        num = pr.find(W + "numPr")
        if num is not None:
            ilvl = num.find(W + "ilvl")
            lvl = int(ilvl.get(W + "val")) if ilvl is not None else 0
            return "bullet", lvl
    if (par.style.name or "").startswith("List Number"):
        return "number", 0
    return None


def _runs(par: DocxParagraph) -> list[dict]:
    """runs 併成 {text, bold, italic, color, size} 片段，相鄰同格式的合併。

    bold 要往樣式回溯：Heading 1 的 run 多半 bold=None（繼承樣式的 True），
    只看 run 會把所有標題寫成不粗體。
    """
    style_bold = bool(par.style.font.bold) if par.style is not None else False
    out: list[dict] = []
    for r in par.runs:
        if not r.text:
            continue
        color = None
        if r.font.color is not None and r.font.color.type is not None:
            rgb = r.font.color.rgb
            if rgb is not None:
                color = str(rgb)
        seg = {
            "text": r.text,
            "bold": style_bold if r.bold is None else bool(r.bold),
            "italic": bool(r.italic),
            "color": color,
            "size": r.font.size.pt if r.font.size else None,
        }
        if out and all(out[-1][k] == seg[k] for k in ("bold", "italic", "color", "size")):
            out[-1]["text"] += seg["text"]
        else:
            out.append(seg)
    if not out and par.text:
        out = [{"text": par.text, "bold": style_bold, "italic": False,
                "color": None, "size": None}]
    return out


def _para_block(par: DocxParagraph) -> dict:
    pf = par.paragraph_format
    style = par.style.name or "Normal"
    return {
        "kind": "p",
        "runs": _runs(par),
        "named": "HEADING_1" if style.startswith("Heading 1")
        else "HEADING_2" if style.startswith("Heading 2")
        else "NORMAL_TEXT",
        "align": "CENTER" if str(pf.alignment).startswith("CENTER") else None,
        "list": _numbering(par),
        "space_above": _pt(pf.space_before),
        "space_below": _pt(pf.space_after),
    }


def _cell_paras(cell) -> list[dict]:
    out = [_para_block(p) for p in cell.paragraphs]
    return [p for p in out if p["runs"]] or [{"kind": "p", "runs": [], "named": "NORMAL_TEXT",
                                              "align": None, "list": None,
                                              "space_above": None, "space_below": None}]


def _image_blocks(par: DocxParagraph, doc) -> list[dict]:
    """段落裡的內嵌圖，回傳 [{kind:'img', blob, w_pt, h_pt}]。"""
    out = []
    for blip in par._p.iter(
        "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
    ):
        rid = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        if not rid:
            continue
        part = doc.part.related_parts[rid]
        ext = par._p.find(
            ".//{http://schemas.openxmlformats.org/drawingml/2006/main}ext"
        )
        w = h = None
        if ext is not None:
            w, h = _pt(int(ext.get("cx"))), _pt(int(ext.get("cy")))
        out.append({"kind": "img", "blob": part.blob, "w_pt": w, "h_pt": h,
                    "align": "CENTER" if str(par.paragraph_format.alignment).startswith("CENTER") else None})
    return out


def parse(path: str | Path) -> list[dict]:
    """.docx → 區塊序列（p / table / img）。"""
    doc = docx.Document(str(path))
    blocks: list[dict] = []

    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "tbl":
            t = DocxTable(child, doc)
            blocks.append({
                "kind": "table",
                "rows": [[_cell_paras(c) for c in row.cells] for row in t.rows],
                "widths": [_pt(c.width) for c in t.columns],
            })
            continue
        if tag != "p":
            continue

        par = DocxParagraph(child, doc)
        imgs = _image_blocks(par, doc)
        if imgs:
            # 圖片段落在本 memo 一律只有圖、沒有文字；真有文字也先出圖再出字
            blocks.extend(imgs)
            if par.text.strip():
                blocks.append(_para_block(par))
            continue
        if not par.text.strip():
            continue   # Word 的空段落多半是版面殘留，不帶進 Docs
        blocks.append(_para_block(par))

    return blocks


# ---------------------------------------------------------------- 寫進 Docs


def _u16(s: str) -> int:
    """Docs 的 index 以 UTF-16 code unit 計。中文 1 單位，emoji 2 單位。"""
    return len(s.encode("utf-16-le")) // 2


def _text_style(seg: dict) -> tuple[dict, str]:
    style: dict = {"bold": seg.get("bold", False), "italic": seg.get("italic", False)}
    fields = ["bold", "italic"]
    if seg.get("size"):
        style["fontSize"] = {"magnitude": seg["size"], "unit": "PT"}
        fields.append("fontSize")
    if seg.get("color"):
        c = seg["color"]
        style["foregroundColor"] = {"color": {"rgbColor": {
            "red": int(c[0:2], 16) / 255,
            "green": int(c[2:4], 16) / 255,
            "blue": int(c[4:6], 16) / 255,
        }}}
        fields.append("foregroundColor")
    if seg.get("link"):
        style["link"] = {"url": seg["link"]}
        fields.append("link")
    return style, ",".join(fields)


def _para_style(b: dict) -> tuple[dict, list[str]]:
    style: dict = {"namedStyleType": b["named"]}
    fields = ["namedStyleType"]
    style["alignment"] = b.get("align") or "START"
    fields.append("alignment")
    if b.get("space_above") is not None:
        style["spaceAbove"] = {"magnitude": b["space_above"], "unit": "PT"}
        fields.append("spaceAbove")
    if b.get("space_below") is not None:
        style["spaceBelow"] = {"magnitude": b["space_below"], "unit": "PT"}
        fields.append("spaceBelow")
    return style, fields


def _plain(b: dict) -> str:
    return "".join(s.get("text", "") for s in b["runs"])


class TabWriter:
    """把區塊序列寫進某個分頁。分頁必須是空的（呼叫端負責先清）。"""

    def __init__(self, docs_svc, doc_id: str, tab_id: str):
        self.svc = docs_svc
        self.doc_id = doc_id
        self.tab_id = tab_id

    # ---- 讀回文件結構 ----

    def _tab_body(self) -> list[dict]:
        doc = self.svc.documents().get(
            documentId=self.doc_id, includeTabsContent=True
        ).execute()
        stack = list(doc.get("tabs") or [])
        while stack:
            t = stack.pop(0)
            if t.get("tabProperties", {}).get("tabId") == self.tab_id:
                return t.get("documentTab", {}).get("body", {}).get("content", [])
            stack.extend(t.get("childTabs") or [])
        raise RuntimeError(f"分頁 {self.tab_id} 不存在")

    def _paragraphs(self) -> list[dict]:
        return [e for e in self._tab_body() if "paragraph" in e]

    def _tables(self) -> list[dict]:
        return [e for e in self._tab_body() if "table" in e]

    def _batch(self, requests: list[dict]) -> None:
        if not requests:
            return
        self.svc.documents().batchUpdate(
            documentId=self.doc_id, body={"requests": requests}
        ).execute()

    # ---- 主流程 ----

    def write(self, blocks: list[dict], *, image_uri=None) -> dict:
        marks = self._check_marks(blocks)

        # --- A：骨架 ---
        skeleton, para_blocks = self._skeleton(blocks, marks)
        self._batch([{"insertText": {
            "location": {"tabId": self.tab_id, "index": 1},
            "text": skeleton,
        }}])

        # --- B：清單（會吃掉 \t，長度在這裡改變；由後往前才不會位移前面的群組）---
        self._batch(list(reversed(self._bullet_requests(blocks, skeleton))))

        # --- C：段落樣式 → 文字樣式 ---
        live = self._paragraphs()
        if len(live) != len(para_blocks):
            raise RuntimeError(
                f"段落數對不上：文件 {len(live)}、來源 {len(para_blocks)}。"
                f"沒把握對齊就不亂套樣式。"
            )
        self._batch(self._style_requests(live, para_blocks))

        # --- D：佔位符 → 真表格／真圖片（由後往前）---
        self._batch(self._object_requests(blocks, marks, image_uri))

        # --- E：填表格（由後往前）---
        n_cells = self._fill_tables(blocks)

        # --- F：欄寬 ---
        self._batch(self._width_requests(blocks))

        # --- G：清掉表格前後的空段落 ---
        blanks = self._tidy_blanks()

        return {
            "blank_lines_removed": blanks,
            "chars": _u16(skeleton),
            "paragraphs": len(para_blocks),
            "tables": sum(1 for b in blocks if b["kind"] == "table"),
            "cells": n_cells,
            "images": sum(1 for b in blocks if b["kind"] == "img"),
        }

    # ---- 各批的組裝 ----

    def _check_marks(self, blocks) -> dict[int, str]:
        """替每個表格／圖片配一個佔位符，並確認它不會撞到內文。"""
        body = "\n".join(_plain(b) for b in blocks if b["kind"] == "p")
        marks: dict[int, str] = {}
        for i, b in enumerate(blocks):
            if b["kind"] not in ("table", "img"):
                continue
            tmpl = TBL_MARK if b["kind"] == "table" else IMG_MARK
            mark = tmpl.format(i)
            if mark in body:
                raise RuntimeError(f"佔位符 {mark} 撞到內文，請換一組符號")
            marks[i] = mark
        return marks

    def _skeleton(self, blocks, marks) -> tuple[str, list[dict]]:
        """整份純文字。表格／圖片各佔一個段落（之後被替換掉）。

        巢狀項目前綴 \\t —— Docs 的 createParagraphBullets 就是數前導 tab 決定層級的，
        並在建立清單時把 tab 吃掉。這是官方唯一支援的設定巢狀層級的方式。
        """
        parts: list[str] = []
        para_blocks: list[dict] = []
        for i, b in enumerate(blocks):
            if b["kind"] == "p":
                lst = b.get("list")
                lvl = lst[1] if lst else 0
                parts.append("\t" * lvl + _plain(b))
                para_blocks.append(b)
            else:
                parts.append(marks[i])
                # 佔位符本身也是段落，樣式階段要跟著對齊
                para_blocks.append({"kind": "p", "runs": [], "named": "NORMAL_TEXT",
                                    "align": b.get("align"), "list": None,
                                    "space_above": None, "space_below": None,
                                    "_placeholder": True})
        # 最後一段不補換行：Docs 的 body 本來就以一個換行結尾，補了會多出空段落
        return "\n".join(parts), para_blocks

    def _bullet_requests(self, blocks, skeleton) -> list[dict]:
        """連續同類的清單段落併成一個 createParagraphBullets。"""
        # 先算出每個段落在 skeleton 裡的 offset（此時還沒有任何長度變化）
        offsets: list[tuple[int, int, dict]] = []
        pos = 0
        for i, b in enumerate(blocks):
            if b["kind"] == "p":
                lst = b.get("list")
                text = "\t" * (lst[1] if lst else 0) + _plain(b)
            else:
                text = ""   # 佔位符不會是清單
            offsets.append((pos, pos + _u16(text), b))
            pos += _u16(text) + 1   # +1 是段落之間的換行

        reqs: list[dict] = []
        run: list[tuple[int, int, dict]] = []

        def flush() -> None:
            if not run:
                return
            kind = run[0][2]["list"][0]
            reqs.append({"createParagraphBullets": {
                "range": {"tabId": self.tab_id,
                          "startIndex": 1 + run[0][0],
                          "endIndex": 1 + run[-1][1]},
                "bulletPreset": BULLET_PRESET if kind == "bullet" else NUMBER_PRESET,
            }})
            run.clear()

        for start, end, b in offsets:
            lst = b.get("list") if b["kind"] == "p" else None
            if lst and (not run or run[0][2]["list"][0] == lst[0]):
                run.append((start, end, b))
            else:
                flush()
                if lst:
                    run.append((start, end, b))
        flush()
        return reqs

    def _style_requests(self, live, para_blocks) -> list[dict]:
        """段落樣式全部排在文字樣式前面 —— namedStyleType 會重設 run 樣式。"""
        para_reqs: list[dict] = []
        text_reqs: list[dict] = []
        for el, b in zip(live, para_blocks):
            start = el["startIndex"]
            end = el["endIndex"]
            if b.get("_placeholder"):
                continue
            style, fields = _para_style(b)
            if b.get("list"):
                # 清單的縮排交給 Docs 的 preset，這裡只管對齊與段距
                style.pop("namedStyleType", None)
                fields = [f for f in fields if f != "namedStyleType"]
            if fields:
                para_reqs.append({"updateParagraphStyle": {
                    "range": {"tabId": self.tab_id, "startIndex": start, "endIndex": end},
                    "paragraphStyle": style,
                    "fields": ",".join(fields),
                }})
            text_reqs.extend(self._run_styles(start, b["runs"]))
        return para_reqs + text_reqs

    def _run_styles(self, start: int, runs: list[dict]) -> list[dict]:
        out: list[dict] = []
        pos = start
        for seg in runs:
            n = _u16(seg.get("text", ""))
            if n == 0:
                continue
            style, fields = _text_style(seg)
            out.append({"updateTextStyle": {
                "range": {"tabId": self.tab_id, "startIndex": pos, "endIndex": pos + n},
                "textStyle": style,
                "fields": fields,
            }})
            pos += n
        return out

    def _object_requests(self, blocks, marks, image_uri) -> list[dict]:
        """把佔位符換成真的表格／圖片。由後往前，前面的 index 才不會被位移。"""
        live = self._paragraphs()
        text_of = {}
        for el in live:
            txt = "".join(
                r.get("textRun", {}).get("content", "")
                for r in el.get("paragraph", {}).get("elements", [])
            )
            text_of[txt.strip()] = el

        reqs: list[dict] = []
        for i in sorted(marks, reverse=True):
            b = blocks[i]
            el = text_of.get(marks[i])
            if el is None:
                raise RuntimeError(f"找不到佔位符 {marks[i]}")
            idx = el["startIndex"]
            reqs.append({"deleteContentRange": {"range": {
                "tabId": self.tab_id,
                "startIndex": idx,
                "endIndex": idx + _u16(marks[i]),
            }}})
            if b["kind"] == "table":
                # insertTable 一定會在表格前面補一個換行，而那個換行是 Docs 用來錨定
                # 表格的、事後**刪不掉**（實測：before-table 的空段落 5 次全部
                # 400 Invalid deletion range，after-table 的 5 次全部成功）。
                # 所以不要讓它另外生一個 —— 插在前一段自己的換行上，讓那一段的換行
                # 直接當錨點，多出來的空段落就會落在表格後面，那個是刪得掉的。
                reqs.append({"insertTable": {
                    "location": {"tabId": self.tab_id, "index": max(1, idx - 1)},
                    "rows": len(b["rows"]),
                    "columns": len(b["rows"][0]),
                }})
            else:
                uri = image_uri(b) if image_uri else None
                if not uri:
                    continue
                req: dict = {"insertInlineImage": {
                    "location": {"tabId": self.tab_id, "index": idx},
                    "uri": uri,
                }}
                if b.get("w_pt") and b.get("h_pt"):
                    req["insertInlineImage"]["objectSize"] = {
                        "width": {"magnitude": b["w_pt"], "unit": "PT"},
                        "height": {"magnitude": b["h_pt"], "unit": "PT"},
                    }
                reqs.append(req)
        return reqs

    def _fill_tables(self, blocks) -> int:
        """填格子。由最後一個表的最後一格往前填，index 才不會邊填邊歪。"""
        srcs = [b for b in blocks if b["kind"] == "table"]
        live = self._tables()
        if len(live) != len(srcs):
            raise RuntimeError(f"表格數對不上：文件 {len(live)}、來源 {len(srcs)}")

        reqs: list[dict] = []
        n_cells = 0
        for src, tbl in reversed(list(zip(srcs, live))):
            rows = tbl["table"]["tableRows"]
            for r in range(len(rows) - 1, -1, -1):
                cells = rows[r]["tableCells"]
                for c in range(len(cells) - 1, -1, -1):
                    paras = src["rows"][r][c] if r < len(src["rows"]) and c < len(src["rows"][r]) else []
                    text = "\n".join(_plain(p) for p in paras)
                    if not text:
                        continue
                    idx = cells[c]["content"][0]["startIndex"]
                    reqs.append({"insertText": {
                        "location": {"tabId": self.tab_id, "index": idx},
                        "text": text,
                    }})
                    flat: list[dict] = []
                    for k, p in enumerate(paras):
                        if k:
                            flat.append({"text": "\n"})
                        flat.extend(p["runs"])
                    reqs.extend(self._run_styles(idx, flat))
                    n_cells += 1
        self._batch(reqs)
        return n_cells

    def _tidy_blanks(self) -> int:
        """刪掉表格前後的空段落。

        `insertTable` 一定會在表格前面補一個換行，被頂到表格後面的佔位符段落又留下
        一個 —— 每張表白白多兩行空白，.docx 裡沒有這些。骨架本身不產生空段落
        （來源的空段落在 parse() 就濾掉了），所以分頁裡的空段落必然是這樣來的，
        全數刪除是安全的。

        兩個例外不能刪：刪了會讓兩張表直接相鄰，或刪掉 body 的最後一個元素 ——
        Docs 兩者都不允許。
        """
        content = self._tab_body()
        drop: list[dict] = []
        for i, e in enumerate(content):
            if "paragraph" not in e:
                continue
            els = e["paragraph"].get("elements", [])
            text = "".join(r.get("textRun", {}).get("content", "") for r in els)
            if text.strip():
                continue
            # 圖片段落沒有文字，但**不是**空段落 —— 只看文字會把損益表整張刪掉
            if any(k != "textRun" for el in els for k in el
                   if k not in ("startIndex", "endIndex")):
                continue
            prev_is_table = i > 0 and "table" in content[i - 1]
            next_is_table = i + 1 < len(content) and "table" in content[i + 1]
            if next_is_table:
                continue                      # 表格前面那個換行是錨點，刪不掉
            if prev_is_table and next_is_table:
                continue                      # 刪了兩張表就黏在一起
            if i == len(content) - 1:
                continue                      # body 必須以段落結尾
            drop.append(e)

        self._batch([
            {"deleteContentRange": {"range": {
                "tabId": self.tab_id,
                "startIndex": e["startIndex"],
                "endIndex": e["endIndex"],
            }}}
            for e in reversed(drop)            # 由後往前，前面的 index 才不會失效
        ])
        return len(drop)

    def _width_requests(self, blocks) -> list[dict]:
        srcs = [b for b in blocks if b["kind"] == "table"]
        live = self._tables()
        reqs: list[dict] = []
        for src, tbl in zip(srcs, live):
            widths = src.get("widths") or []
            for ci, w in enumerate(widths):
                if not w:
                    continue
                reqs.append({"updateTableColumnProperties": {
                    "tableStartLocation": {"tabId": self.tab_id,
                                           "index": tbl["startIndex"]},
                    "columnIndices": [ci],
                    "tableColumnProperties": {
                        "widthType": "FIXED_WIDTH",
                        "width": {"magnitude": w, "unit": "PT"},
                    },
                    "fields": "widthType,width",
                }})
        return reqs


# ---------------------------------------------------------------- 圖片託管


@contextlib.contextmanager
def hosted_image(drive, blob: bytes, parent_id: str, name: str):
    """把圖片暫時放上 Drive 並開連結權限，交出可公開存取的 URI。

    `insertInlineImage` 只吃公開 URI，本機 PNG 不能直接插。Docs 在插入當下會把
    圖片位元組**複製進文件自己的儲存空間**，所以插完就能把暫存檔連同權限一起收掉
    —— 對外可見的時間只有這幾秒，事後 Drive 上不留任何東西。
    """
    # 暫存檔放系統暫存區，不要落在專案目錄 —— 同時跑兩檔會互相蓋掉，
    # 中途失敗也會在使用者的資料夾裡留下垃圾。
    workdir = Path(tempfile.mkdtemp(prefix="memo_img_"))
    tmp = workdir / name
    tmp.write_bytes(blob)
    file_id = None
    try:
        up = drive.upload(tmp, parent_id, name=tmp.name, mime_type="image/png",
                          overwrite=True)
        file_id = up["id"]
        drive.svc.permissions().create(
            fileId=file_id, body={"role": "reader", "type": "anyone"},
            supportsAllDrives=True,
        ).execute()
        yield f"https://drive.google.com/uc?export=view&id={file_id}"
    finally:
        if file_id:
            with contextlib.suppress(Exception):
                drive.svc.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        shutil.rmtree(workdir, ignore_errors=True)


def plain_text(blocks: list[dict]) -> str:
    """區塊序列的純文字，用來跟分頁讀回來的文字做逐字比對。"""
    out: list[str] = []
    for b in blocks:
        if b["kind"] == "p":
            out.append(_plain(b))
        elif b["kind"] == "table":
            for row in b["rows"]:
                for cell in row:
                    out.extend(_plain(p) for p in cell if _plain(p))
    return "\n".join(x for x in out if x.strip())


def normalize(s: str) -> str:
    """比對用：吃掉空白差異與 Docs 自動加的清單符號。"""
    s = re.sub(r"[​﻿]", "", s)
    return re.sub(r"\s+", "", s)
