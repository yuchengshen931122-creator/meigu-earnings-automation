"""叫用 tools/notify_line.py 的共用小工具。

dispatcher 與 sweep_podcasts 兩條路都會走到「這一檔完成了」，兩邊都要推播，
所以把「怎麼叫」收在同一處，免得兩邊的參數與失敗處理各長各的。

**推播失敗不能影響產出線**：memo 與 podcast 都已經產好、歸檔完成，
通知送不出去是通知的問題，不該讓整檔被判定失敗、更不該觸發重做。
所以這裡只回報，不拋例外。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "notify_line.py"
TIMEOUT = 600


def push(ticker: str, quarter: str, docx_path: str | Path,
         m4a_path: str | Path | None, *, cwd: str | Path | None = None) -> bool:
    """推播一檔。回傳有沒有成功送出（跳過也算 False，但不是錯誤）。"""
    if not m4a_path or not Path(m4a_path).exists():
        # memo 與 podcast 齊了才推 —— 音檔還沒好就交給 sweep 那條路
        return False

    cmd = [sys.executable, str(TOOL),
           "--ticker", ticker, "--quarter", quarter,
           "--docx", str(docx_path), "--m4a", str(m4a_path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd,
                           encoding="utf-8", errors="replace", timeout=TIMEOUT)
    except Exception as exc:
        print(f"    [!] LINE 推播沒跑成：{type(exc).__name__}: {exc}")
        return False

    out = (r.stdout or "") + (r.stderr or "")
    for line in out.strip().splitlines()[-3:]:
        print(f"      {line}")
    if r.returncode != 0:
        print("    [!] LINE 推播失敗（memo 與 podcast 已完成，不影響本檔結果）")
        return False
    # rc=0 有兩種：真的送出，以及刻意跳過（尚未設定、已推播過、音檔未到）。
    # 兩者都不是錯誤，但只有前者算「送出了」—— 不看輸出就分不出來。
    return "[OK] 已推播" in out
