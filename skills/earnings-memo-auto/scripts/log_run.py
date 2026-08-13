#!/usr/bin/env python3
"""Append a row to the shared earnings-memo run log, so the user can see what's been generated
without needing a separate dashboard.

Usage:
    python log_run.py --ticker VZ --quarter 1Q26 --status "完成" --notes "無" \
        --path "{專案根目錄}\\MEMO\\1Q26\\VZ 1Q26.docx" [--log {專案根目錄}\\MEMO\\_generated_log.md] [--date 2026-07-13]

New entries are inserted directly under the table header, so the most recent run is always at
the top -- the user can just open the file and read the first few rows, no scrolling needed.
Creates the log file (with header) on first use if it doesn't exist yet.
"""
import argparse
import datetime
from pathlib import Path

HEADER = "| 產生日期 | 代號 | 季度 | 狀態 | 缺漏/備註 | 檔案路徑 |\n"
SEPARATOR = "|---|---|---|---|---|---|\n"
TITLE = "# 財報 Memo 產出紀錄\n\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--quarter", required=True)
    parser.add_argument("--status", required=True, help='e.g. "完成" or "部分完成(缺逐字稿Q&A)"')
    parser.add_argument("--notes", default="無")
    parser.add_argument("--path", required=True)
    parser.add_argument("--log", default=r"MEMO\_generated_log.md")  # 相對於工作目錄；建議明確傳 --log
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today")
    args = parser.parse_args()

    date = args.date or datetime.date.today().isoformat()
    # escape pipe characters so a stray "|" in notes/path doesn't break the table
    def esc(s):
        return str(s).replace("|", "\\|")

    row = f"| {esc(date)} | {esc(args.ticker)} | {esc(args.quarter)} | {esc(args.status)} | {esc(args.notes)} | {esc(args.path)} |\n"

    log_path = Path(args.log)

    if not log_path.exists():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(TITLE + HEADER + SEPARATOR + row, encoding="utf-8")
        print(f"Created {log_path} with first entry")
        return

    text = log_path.read_text(encoding="utf-8")
    if HEADER not in text:
        # existing file predates the table format -- keep old content, add a fresh table on top
        text = TITLE + HEADER + SEPARATOR + text
    lines = text.splitlines(keepends=True)
    header_idx = next(i for i, line in enumerate(lines) if line == HEADER)
    insert_at = header_idx + 2  # after header + separator row
    lines.insert(insert_at, row)
    log_path.write_text("".join(lines), encoding="utf-8")
    print(f"Logged {args.ticker} {args.quarter} -> {log_path}")


if __name__ == "__main__":
    main()
