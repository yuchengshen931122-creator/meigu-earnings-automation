"""兩棵 Drive 樹的分類對映。

問題：Word 樹（長期單一棵）有 337 個 ticker 的歸檔位置，
但 podcast 樹每季重建，本季（2026Q2）只長出 141 個 —— 追蹤清單 112 檔裡
有 44 檔在 podcast 樹沒有落點，包括已經做完的 VST / OKLO / RKLB。

所以歸檔時必須能從「Word 樹的分類」推出「podcast 樹的對應分類」，
不存在就補建。而兩棵樹的分類名有系統性漂移（實際掃描結果）：

    Word 樹                  Podcast 樹
    AI 硬體（有空格）    →    AI硬體
    實體AI/機器人/醫療   →    實體AI/機器人
    記憶體/硬體          →    記憶體/存儲
    能源儲存及潔淨能源   →    能源儲存與潔淨能源
    衣 / 食              →    衣著 / 飲食
    FinTech              →    Fintech
    散熱 / 水資源 /
    半導體/工業設計      →    （無，需補建）
    生技醫療（次層）     →    生技醫療（頂層）
    （無）               →    地熱 / 貴金屬（頂層）

策略：先用正規化後的**葉節點名**比對（多數直接命中），
再套別名表，最後才在對應的頂層分類底下補建。
比對一律不用原始字串 —— 空格與全形斜線是這裡最常見的地雷。
"""

from __future__ import annotations

import re

# Word 樹葉節點 → podcast 樹葉節點（皆以正規化後的鍵儲存）
_RAW_ALIASES = {
    "AI 硬體": "AI硬體",
    "實體AI/機器人/醫療": "實體AI/機器人",
    "記憶體/硬體": "記憶體/存儲",
    "能源儲存及潔淨能源": "能源儲存與潔淨能源",
    "衣": "衣著",
    "食": "飲食",
    "FinTech": "Fintech",
}


def normalize(name: str) -> str:
    """去空格、統一全形斜線與大小寫。分類名本身含 '/'，不能當路徑分隔處理。"""
    s = name.replace("／", "/").replace("　", "")
    s = re.sub(r"\s+", "", s)
    return s.casefold()


ALIASES = {normalize(k): normalize(v) for k, v in _RAW_ALIASES.items()}


def build_index(folders: list[dict]) -> dict[str, list[dict]]:
    """{正規化葉名: [folder, ...]}。同名可能出現在不同層，故存 list。"""
    idx: dict[str, list[dict]] = {}
    for f in folders:
        idx.setdefault(normalize(f["name"]), []).append(f)
    return idx


def find_counterpart(
    word_path: list[str], podcast_folders: list[dict]
) -> tuple[dict | None, str]:
    """給 Word 樹的路徑，找 podcast 樹的對應資料夾。

    回傳 (folder | None, 說明)。找不到時說明會指出該在哪個頂層底下補建。
    """
    idx = build_index(podcast_folders)

    # 由葉往根找：先試最深的分類，再退回上層
    for depth_from_leaf, name in enumerate(reversed(word_path)):
        key = normalize(name)
        for candidate in (key, ALIASES.get(key)):
            if not candidate:
                continue
            hits = idx.get(candidate)
            if hits:
                how = "葉名直接命中" if candidate == key else f"別名對映（{name}）"
                if depth_from_leaf:
                    how += f"；退回上層第 {depth_from_leaf} 級"
                return hits[0], how

    top = word_path[0] if word_path else ""
    return None, f"podcast 樹無對應分類，需在頂層『{top}』底下補建『{word_path[-1]}』"


def top_level_for(word_path: list[str], podcast_folders: list[dict]) -> dict | None:
    """找 podcast 樹裡對應的頂層分類，供補建子資料夾時當 parent。"""
    if not word_path:
        return None
    idx = build_index([f for f in podcast_folders if f["depth"] == 1])
    key = normalize(word_path[0])
    for candidate in (key, ALIASES.get(key)):
        if candidate and idx.get(candidate):
            return idx[candidate][0]
    return None
