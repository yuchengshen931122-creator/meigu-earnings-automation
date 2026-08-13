"""印出本機資料夾用的季度標籤（例如 2Q26）。

給 .bat 用的小工具 —— 排程腳本原本把季度寫死（`set QUARTER=2Q26`），
跨季就會去掃一個空的舊資料夾，新一季的音檔永遠等不到人來上傳，
而且不會有任何錯誤（sweep 只會說「0 個音檔」，rc=0）。

輸出兩個標籤：**本季**與**上一季**，空白分隔。
上一季要一起掃，因為季度切換那幾天還會有前一波的落後檔案進來
（法說會拖到十月才開的公司，音檔仍屬 3Q26 那個桶子）。

    python tools/quarter_label.py            # 2Q26 1Q26
    python tools/quarter_label.py --current  # 2Q26
"""

import _bootstrap  # noqa: F401

import argparse
import datetime as dt

from core.quarters import local_folder_label


def previous(label: str) -> str:
    """`2Q26` → `1Q26`。從標籤本身遞減，不用日期回推 ——
    十月初 today-95 天會落回 Q2 而算出 1Q26，整整跳過一季。"""
    q, y = int(label[0]), int(label[2:])
    return f"{q - 1}Q{y:02d}" if q > 1 else f"4Q{(y - 1) % 100:02d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--current", action="store_true", help="只印本季")
    ap.add_argument("--date", help="改用指定日期推導（YYYY-MM-DD），測試用")
    args = ap.parse_args()

    d = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    cur = local_folder_label(d)
    print(cur if args.current else f"{cur} {previous(cur)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
