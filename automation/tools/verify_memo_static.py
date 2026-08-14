"""memo 的純程式靜態檢查 —— 驗證鏈的第三道防線（非模型）。

三道防線的分工：
  1. runner 產出時的 Step 4 自我驗證（同 context，最弱）
  2. earnings-memo-verifier 獨立 clean-context 逐數字回源（最強，但仍是模型）
  3. 本檔：確定性的結構不變量，模型說什麼都騙不過它

跑兩次：verifier 之前（發現的問題塞進 verifier 的派工單讓它修）、
verifier 之後（硬性關卡——verifier 自己改完東西也要過這關，例如改了 JSON
卻忘了重建 docx，就是這裡抓）。

hard（不過就擋）：
  - JSON 解析失敗
  - 七個章節缺漏或標題不對
  - 驗證檔不存在，或裡面沒有「相符 N／已修正 N」統計行
  - docx 不存在，或比 JSON 舊（改了內容沒重建）
  - dashboard 差異欄算術不合（實際−預期 ≠ 差異；能解析的列做硬性驗算，
    解析不了的列自動跳過交給 verifier）
warn（印出來給 verifier / 人看，不擋）：
  - dashboard 的數字 token 在 tldr+sections 全文找不到（可能是掛載不一致）
  - 全形括號（）（格式慣例違規）

用法：python tools/verify_memo_static.py <json> <docx> <check.md>
exit 0 = 通過；1 = hard 失敗。
"""

import _bootstrap  # noqa: F401

import json
import re
import sys
from pathlib import Path

SECTION_HEADS = ["一、", "二、", "三、", "四、", "五、", "六、", "七、"]
STATS_RE = re.compile(r"相符\s*\d+.{0,12}已修正\s*\d+")
NUM_TOKEN = re.compile(r"\d[\d,]*\.\d+%?|\d[\d,]{2,}%?|\d+\.?\d*%")
_NUM = re.compile(r"([+-])?\s*\$?(\d[\d,]*(?:\.\d+)?)\s*(億|萬|%|bps)?")


def _first_num(text: str):
    """抓字串裡第一個數字，回 (值, 單位, 顯示粒度)。抓不到回 None。

    跳過 Q2/FY26/H1 這類「代號裡的數字」——它們前面貼著字母，不是量值。
    """
    for m in _NUM.finditer(text):
        i = m.start(2)
        if i > 0 and (text[i - 1].isalpha() or text[i - 1] in "/_"):
            continue
        s = m.group(2)
        val = float(s.replace(",", ""))
        if m.group(1) == "-":
            val = -val
        gran = 10 ** -(len(s.split(".")[1]) if "." in s else 0)
        return val, (m.group(3) or ""), gran
    return None


def _dash_arith(memo: dict) -> list[str]:
    """dashboard「實際／預期／差異」三欄的確定性驗算。

    只驗能無歧義解析的列（實際與預期同單位、差異欄有數字）；
    區間指引、N/A、純文字比較欄自動跳過，交給 verifier 的模型層。
    容差 = 兩邊顯示粒度之和 + 2%（四捨五入的合法誤差），超過就是真的算錯。
    """
    errs: list[str] = []
    for tbl in memo.get("dashboard", []):
        cols = tbl.get("columns", [])
        # 只驗「實際／預期／差異」語意的表（表頭有「差異」欄）；
        # 財測與展望那張表是（財測/比較/結果）語意，不能套算術。
        try:
            i_a = next(i for i, c in enumerate(cols) if "實際" in c)
            i_e = next(i for i, c in enumerate(cols) if "預期" in c or "中點" in c)
            i_d = next(i for i, c in enumerate(cols) if "差異" in c)
        except StopIteration:
            continue
        for row in tbl.get("rows", []):
            if not isinstance(row, list) or len(row) <= max(i_a, i_e, i_d):
                continue
            label = _all_text(row[0]).strip()[:20]
            pa, pe, pd = (_first_num(_all_text(row[i])) for i in (i_a, i_e, i_d))
            if not (pa and pe and pd):
                continue
            (a, ua, ga), (e, ue, ge), (d, ud, gd) = pa, pe, pd
            if ua != ue:
                continue
            diff = a - e
            if ud == "bps" and ua == "%":
                got, tol = diff * 100, (ga + ge) * 100 + 0.02 * abs(d) + 1
            elif ud == "萬" and ua == "億":
                got, tol = diff * 10000, (ga + ge) * 10000 + 0.02 * abs(d)
            elif ud == ua or ud == "":
                got, tol = diff, ga + ge + 0.02 * abs(d)
            else:
                continue
            if abs(abs(got) - abs(d)) > tol:
                errs.append(f"dashboard 算術不合：「{label}」實際−預期={got:+.2f} "
                            f"與差異欄 {d:+g}{ud} 對不上")
    return errs


def _all_text(node) -> str:
    """把 JSON 任意子樹攤平成純文字。"""
    if isinstance(node, str):
        return node + "\n"
    if isinstance(node, dict):
        return "".join(_all_text(v) for v in node.values())
    if isinstance(node, list):
        return "".join(_all_text(v) for v in node)
    return ""


def check(json_path: Path, docx_path: Path, check_path: Path
          ) -> tuple[list[str], list[str]]:
    """回傳 (hard 問題, warn 警告)。"""
    hard: list[str] = []
    warn: list[str] = []

    # ---- JSON 解析與章節 ----
    try:
        memo = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"JSON 解析失敗：{type(exc).__name__}: {exc}"], []

    heads = [s.get("heading", "") for s in memo.get("sections", [])]
    for h in SECTION_HEADS:
        if not any(x.startswith(h) for x in heads):
            hard.append(f"缺章節 {h}")
    if not memo.get("tldr"):
        hard.append("缺 tldr")
    if not memo.get("dashboard"):
        hard.append("缺 dashboard")

    # ---- 驗證檔 ----
    cp = Path(check_path)
    if not cp.exists():
        hard.append(f"驗證檔不存在：{cp.name}")
    else:
        head = cp.read_text(encoding="utf-8", errors="replace")[:2000]
        if not STATS_RE.search(head):
            hard.append(f"驗證檔缺統計行（相符 N／已修正 N …）：{cp.name}")

    # ---- docx 存在且不比 JSON 舊 ----
    dp = Path(docx_path)
    if not dp.exists():
        hard.append(f"docx 不存在：{dp.name}")
    elif dp.stat().st_mtime + 2 < Path(json_path).stat().st_mtime:
        hard.append("docx 比 JSON 舊 —— 內容改了沒重建（rebuild 後再來）")

    # ---- dashboard 差異欄算術（hard）----
    hard.extend(_dash_arith(memo))

    # ---- dashboard 數字回查（warn）----
    body_text = _all_text(memo.get("tldr")) + _all_text(memo.get("sections"))
    missing = []
    for tbl in memo.get("dashboard", []):
        for row in tbl.get("rows", []):
            for tok in NUM_TOKEN.findall(_all_text(row)):
                if len(tok) < 3:
                    continue
                if tok not in body_text and tok not in missing:
                    missing.append(tok)
    if missing:
        warn.append(f"dashboard 有 {len(missing)} 個數字在內文找不到（抽樣：{'、'.join(missing[:8])}）"
                    "——可能是計算欄，也可能是掛載不一致，verifier 應逐一確認")

    # ---- 全形括號（warn）----
    n_full = body_text.count("（") + body_text.count("）")
    if n_full:
        warn.append(f"全形括號出現 {n_full} 處（慣例是半形）")

    return hard, warn


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 1
    hard, warn = check(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    for w in warn:
        print(f"[warn] {w}")
    if hard:
        for h in hard:
            print(f"[FAIL] {h}")
        return 1
    print("[OK] 靜態檢查通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
