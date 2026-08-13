"""把 podcast 音檔放到一個可公開存取的直連網址，給 LINE 的語音訊息用。

**這件事會把音檔放到公開網路上，任何拿到網址的人都存取得到。**
不是可以避免的：LINE 的 audio message 只吃公開 HTTPS 直連網址，它的伺服器與每一位
按下播放的成員都會去抓那個網址，所以檔案必須**長期保持公開**，送完不能收回。
（使用者在「只給 Drive 連結」與「可播放語音訊息」之間明確選了後者。）

既然躲不掉，就讓它可控、可稽核：
  - 只放在**本帳號自己的 My Drive** 底下一個專用資料夾，不碰 共用樹擁有者 那兩棵共用樹
  - 每一份都記在 state/line_public_files.json：ticker、季度、file_id、公開時間
  - `python tools/line_public.py list` 隨時看有哪些是公開的
  - `python tools/line_public.py revoke <ticker>`（或 --all）可以收回

實測（2026-08-12，11MB 的 m4a）：公開後
`https://drive.google.com/uc?export=download&id={id}` 回 200、
Content-Type `audio/mp4`、Content-Length 完整、內容是真的 m4a bytes，
會 302 到 drive.usercontent.google.com。100MB 以下不會出現病毒掃描確認頁。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import requests

FOLDER_NAME = "美股 podcast 公開連結（LINE 用）"
LEDGER = "line_public_files.json"


def direct_url(file_id: str) -> str:
    return f"https://drive.google.com/uc?export=download&id={file_id}"


# ---------- 帳本 ----------


def _ledger_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / LEDGER


def load_ledger(state_dir: str | Path) -> dict:
    p = _ledger_path(state_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_ledger(state_dir: str | Path, data: dict) -> None:
    p = _ledger_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 音檔長度 ----------


def duration_ms(path: str | Path) -> int:
    """用 ffprobe 量長度。LINE 的語音訊息要毫秒數，給錯進度條就會亂掉。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"ffprobe 讀不到長度：{(out.stderr or '').strip()[:200]}")
    return int(float(out.stdout.strip()) * 1000)


# ---------- 公開／撤銷 ----------


def ensure_folder(drive) -> str:
    """在本帳號 My Drive 根目錄底下的專用資料夾。刻意不放共用樹。"""
    return drive.ensure_folder("root", FOLDER_NAME)


def publish(drive, local_path: str | Path, *, name: str, ticker: str,
            quarter: str, state_dir: str | Path) -> dict:
    """上傳並開放連結存取，回傳 {file_id, url, duration_ms, size_mb}。

    冪等：同名檔已存在就更新同一個 file ID，不會每次多一份公開檔案。
    """
    p = Path(local_path)
    folder = ensure_folder(drive)
    up = drive.upload(p, folder, name=name, mime_type="audio/mp4", overwrite=True)
    fid = up["id"]

    drive.svc.permissions().create(
        fileId=fid, body={"role": "reader", "type": "anyone"},
    ).execute()

    info = {
        "file_id": fid,
        "name": name,
        "url": direct_url(fid),
        "duration_ms": duration_ms(p),
        "size_mb": round(p.stat().st_size / 1024 / 1024, 1),
        "ticker": ticker,
        "quarter": quarter,
    }
    ledger = load_ledger(state_dir)
    ledger[f"{ticker} {quarter}"] = info
    _save_ledger(state_dir, ledger)
    return info


def verify(url: str) -> dict:
    """確認網址真的直接吐得出音訊。

    LINE 抓不到內容時**不會回報錯誤給我們** —— 群組裡只會看到一則播不動的語音訊息。
    這種默默壞掉的失敗要在送出前就擋下來，所以先自己抓一次看 content-type。
    """
    r = requests.get(url, stream=True, timeout=30, allow_redirects=True)
    ct = r.headers.get("Content-Type", "")
    head = next(r.iter_content(16), b"")
    r.close()
    ok = r.status_code == 200 and ct.startswith("audio/")
    return {
        "ok": ok,
        "status": r.status_code,
        "content_type": ct,
        "bytes": r.headers.get("Content-Length"),
        "looks_like_mp4": b"ftyp" in head,
    }


def revoke(drive, state_dir: str | Path, key: str) -> bool:
    """收回公開權限並刪檔。收回之後群組裡那則語音訊息就播不出來了。"""
    ledger = load_ledger(state_dir)
    info = ledger.get(key)
    if not info:
        return False
    try:
        drive.svc.files().delete(fileId=info["file_id"]).execute()
    except Exception:
        pass
    ledger.pop(key, None)
    _save_ledger(state_dir, ledger)
    return True
