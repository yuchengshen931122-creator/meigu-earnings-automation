"""自動找出本季的 podcast 樹，並更新 config.json 的 podcast_tree_id。

## 為什麼需要這支

podcast 樹每季新建一棵，命名毫無規則。實際掃出來的歷季名稱：

    2026Q2 美股財報podcast      2026Q1 美股財報 Podcast
    2025 Q4 美股財報 podcast    2025 Q3 美股財報podcast
    2025Q2 美股財報 podcast     2025Q1美股財報 錄音      ← 連關鍵字都不同

季度與「podcast」之間有沒有空格、大小寫、甚至用不用 podcast 這個字，全都會漂。
原本的作法是每季開季由人重跑 `build_drive_map.py` 並手動把新的 folder ID
貼進 config.json —— 忘了做的話，整季的音檔會全部上傳到**上一季**那棵樹底下，
而且不會有任何錯誤：資料夾是存在的、上傳會成功、表也會打勾。

## 為什麼不硬猜

同一層底下還有**沒有** podcast 字樣的誘餌：`2025 Q3 美股財報`、`2025Q2 美股財報`
——那些是別的東西。所以比對要求兩個條件同時成立：季度符合，且帶音檔關鍵字。

命中剛好一個才寫入。**零個或多個一律不猜**，回非零讓 watchdog 告警，
由人決定 —— 猜錯的代價（整季歸檔到錯的地方）遠高於停下來問一句。

用法：
    python tools/resolve_podcast_tree.py            # 只檢查，印出結論
    python tools/resolve_podcast_tree.py --write    # 命中就更新 config.json
    python tools/resolve_podcast_tree.py --quarter 2026Q3 --write
"""

import _bootstrap  # noqa: F401

import argparse
import datetime as dt
import re
from pathlib import Path

from core import quarters
from core.drive_client import FOLDER_MIME, DriveClient
from core.gauth import load_config, load_credentials

#: 音檔樹的關鍵字。`錄音` 是 2025Q1 的用法，留著才認得回頭補的舊季度。
AUDIO_HINTS = ("podcast", "錄音", "音檔")


def _norm(s: str) -> str:
    """去掉所有空白與全形空白後 casefold —— 空格位置每季都在漂。"""
    return re.sub(r"[\s　]+", "", s).casefold()


def quarter_token(when: dt.date) -> str:
    """本季桶子的標籤，例如 2026Q2（與資料夾命名同一套，非財政季度）。"""
    y, q = quarters.calendar_quarter_for(when)
    return f"{y}Q{q}"


def candidates(client: DriveClient, grandparent_id: str, token: str,
               *, exclude: set[str]) -> list[dict]:
    want = _norm(token)
    out = []
    for ch in client.list_children(grandparent_id):
        if ch["mimeType"] != FOLDER_MIME or ch["id"] in exclude:
            continue
        n = _norm(ch["name"])
        if want in n and any(h in n for h in AUDIO_HINTS):
            out.append(ch)
    return out


def update_config(path: Path, folder_id: str, folder_name: str) -> None:
    """只替換那兩個值，不重寫整份檔 —— config.json 裡有大量 `_comment` 說明，
    整份 json.dump 出去會把格式與註解位置洗掉。"""
    text = path.read_text(encoding="utf-8")
    for key, val in (("podcast_tree_id", folder_id),
                     ("podcast_tree_name", folder_name)):
        new, n = re.subn(rf'("{key}"\s*:\s*)"[^"]*"',
                         lambda m: m.group(1) + '"' + val.replace('"', '\\"') + '"',
                         text, count=1)
        if n != 1:
            raise RuntimeError(f"config.json 裡找不到唯一的 {key}，沒有寫入任何東西")
        text = new
    path.write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="命中就更新 config.json")
    ap.add_argument("--quarter", help="指定季度（YYYYQn），預設由今天推導")
    args = ap.parse_args()

    cfg = load_config()
    d = cfg["drive"]
    token = args.quarter or quarter_token(dt.date.today())
    cfg_path = Path(__file__).resolve().parent.parent / "config.json"

    client = DriveClient(load_credentials(interactive=False))
    hits = candidates(client, d["grandparent_id"], token,
                      exclude={d["word_tree_id"]})

    print(f"本季桶子：{token}")
    print(f"目前設定：{d['podcast_tree_name']!r}  {d['podcast_tree_id']}")

    if len(hits) != 1:
        print(f"\n[X] 找到 {len(hits)} 個符合的資料夾，不猜。")
        for h in hits:
            print(f"    {h['name']!r}  {h['id']}")
        print("    請人工確認後更新 config.json 的 podcast_tree_id／podcast_tree_name，")
        print("    然後重跑 tools/build_drive_map.py。")
        return 1

    hit = hits[0]
    if hit["id"] == d["podcast_tree_id"]:
        print(f"\n[OK] 已經是本季那一棵：{hit['name']!r}")
        return 0

    print(f"\n[!] 本季應該用：{hit['name']!r}  {hit['id']}")
    if not args.write:
        print("    （僅檢查。要更新請加 --write）")
        return 1

    update_config(cfg_path, hit["id"], hit["name"])
    print(f"[OK] 已更新 {cfg_path}")
    print("     接著請重跑 tools/build_drive_map.py（歸檔位置索引要跟著換樹重建）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
