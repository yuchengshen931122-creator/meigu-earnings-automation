"""掃描兩棵 Drive 樹，產生 state/drive_map.json（ticker/分類 → folder ID）。

**每季開季必須重跑一次**，因為 podcast 樹每季新建且命名無規則。

輸出結構：
{
  "generated_at": "...",
  "trees": {
    "word":    {"root_id": ..., "root_name": ..., "folders": [{"path": [...], "id": ...}]},
    "podcast": {...}
  },
  "ticker_index": { "VST": {"word": "<folder_id>", "podcast": "<folder_id>"} , ... },
  "unmapped": ["<ticker>", ...]
}

ticker_index 的建法：掃出每棵樹底下已存在的 {TICKER} 子資料夾 / 檔名，
反推 ticker 屬於哪個分類。這樣就不必維護那份會過期的手寫歸檔地圖。
"""

import _bootstrap  # noqa: F401

import argparse
import datetime as dt
import json
import re
from pathlib import Path

from core.drive_client import FOLDER_MIME, DriveClient
from core.gauth import load_config, load_credentials

TICKER_RE = re.compile(r"^([A-Z][A-Z0-9.\-]{0,6})\b")


def infer_ticker(name: str) -> str | None:
    """從資料夾/檔名推 ticker：'VST'、'VST 2Q26.docx'、'RKLB 財報' → VST / RKLB。"""
    m = TICKER_RE.match(name.strip())
    if not m:
        return None
    t = m.group(1).rstrip(".")
    if len(t) < 1 or t in {"AI", "TRUE", "FALSE", "Q"}:
        return None
    return t


def scan(client: DriveClient, root_id: str, root_name: str) -> dict:
    folders = client.scan_tree(root_id, max_depth=3)
    print(f"  [{root_name}] 掃到 {len(folders)} 個子資料夾")

    # ticker → 它所在的「父資料夾」ID（也就是該檔要歸進去的分類資料夾）
    index: dict[str, str] = {}

    # (a) 子資料夾本身就是 ticker（例如 …/傳統能源／核能／再生能源/VST/）
    for f in folders:
        t = infer_ticker(f["name"])
        if t and t == f["name"].strip().upper():
            index.setdefault(t, f["id"])

    # (b) 分類資料夾底下直接放檔案（例如 …/太空概念股/RKLB 2Q26.docx）
    for f in folders:
        for child in client.list_children(f["id"]):
            if child["mimeType"] == FOLDER_MIME:
                continue
            t = infer_ticker(child["name"])
            if t:
                index.setdefault(t, f["id"])

    print(f"  [{root_name}] 推出 {len(index)} 個 ticker 的歸檔位置")
    return {
        "root_id": root_id,
        "root_name": root_name,
        "folders": folders,
        "ticker_to_folder": index,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = load_config()
    creds = load_credentials(interactive=False)
    client = DriveClient(creds)

    d = cfg["drive"]
    print("掃描 Drive（只認 ID，不認名稱）…")
    word = scan(client, d["word_tree_id"], d["word_tree_name"])
    podcast = scan(client, d["podcast_tree_id"], d["podcast_tree_name"])

    all_tickers = sorted(set(word["ticker_to_folder"]) | set(podcast["ticker_to_folder"]))
    ticker_index = {
        t: {
            "word": word["ticker_to_folder"].get(t),
            "podcast": podcast["ticker_to_folder"].get(t),
        }
        for t in all_tickers
    }
    partial = [t for t, v in ticker_index.items() if not (v["word"] and v["podcast"])]

    out = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "trees": {"word": word, "podcast": podcast},
        "ticker_index": ticker_index,
        "only_in_one_tree": partial,
    }

    state_dir = Path(cfg["local"]["state_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    path = Path(args.out) if args.out else state_dir / "drive_map.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[OK] 寫出 {path}")
    print(f"     ticker 總數：{len(ticker_index)}")
    print(f"     只在單一棵樹有位置（歸檔時需補建）：{len(partial)}")
    if partial:
        print(f"     例如：{', '.join(partial[:15])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
