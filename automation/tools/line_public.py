"""管理「為了 LINE 語音訊息而公開」的 podcast 音檔。

LINE 的語音訊息只吃公開直連網址，所以推播過的 podcast 會有一份放在
本帳號 My Drive 的專用資料夾裡、且開放任何人以連結存取。
這支就是那批檔案的清單與撤銷工具 —— 公開這件事要看得見、收得回。

    python tools/line_public.py list
    python tools/line_public.py revoke "LITE 2026Q4"
    python tools/line_public.py revoke --all

撤銷之後，群組裡那則語音訊息就播不出來了（訊息本身還在，只是抓不到內容）。
"""

import _bootstrap  # noqa: F401

import argparse

from core import public_link
from core.drive_client import DriveClient
from core.gauth import load_config, load_credentials


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["list", "revoke"])
    ap.add_argument("key", nargs="?", help="例如 'LITE 2026Q4'")
    ap.add_argument("--all", action="store_true", help="撤銷全部")
    args = ap.parse_args()

    cfg = load_config()
    state_dir = cfg["local"]["state_dir"]
    ledger = public_link.load_ledger(state_dir)

    if args.action == "list":
        if not ledger:
            print("目前沒有任何公開的音檔。")
            return 0
        print(f"公開中的 podcast 音檔（{len(ledger)} 份）——"
              f"任何拿到網址的人都存取得到：\n")
        for key, info in sorted(ledger.items()):
            print(f"  {key:16s} {info['size_mb']:>5} MB  {info['url']}")
        print(f"\n資料夾：我的雲端硬碟 /「{public_link.FOLDER_NAME}」")
        print("撤銷：python tools/line_public.py revoke \"<key>\"  或  --all")
        return 0

    if not args.all and not args.key:
        print("要撤銷哪一份？給 key（例如 \"LITE 2026Q4\"）或 --all")
        return 2

    drive = DriveClient(load_credentials(interactive=False))
    keys = list(ledger) if args.all else [args.key]
    done = 0
    for k in keys:
        if public_link.revoke(drive, state_dir, k):
            print(f"  已撤銷並刪除 {k}")
            done += 1
        else:
            print(f"  找不到 {k}")
    print(f"\n共撤銷 {done} 份。群組裡對應的語音訊息從現在起播不出來。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
