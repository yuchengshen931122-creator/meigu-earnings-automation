"""補做「memo 已完成、podcast 卻缺席」的那些檔。

## 為什麼需要這支

dispatcher 把 podcast 當成**不阻塞**的步驟：產不出來也照樣歸檔 memo，
然後 `finish(ST_DONE, report_done=True)` 把該檔標成「報告已完成」。
下一輪的候選過濾會直接跳過「報告已完成」的列 —— 於是那檔再也不會被碰。

而 `sweep_podcasts.py` 只掃**已經躺在磁碟上**的音檔，它不會產生任何東西。

兩件事合起來就是一個洞：NotebookLM 授權過期（或來源暫時抓不到）的那段期間，
每一檔跑完都會**永久**少掉 podcast，連帶 LINE 也永遠不會推
（`dispatcher` 與 `sweep` 都只在音檔存在時才推），而且全程沒有任何東西變紅：
memo 有了、表上打勾了、排程 rc=0。2026-08-12 授權過期時就是這個狀態。

這支就是那條回填路徑：授權修好之後，積欠的檔會自己補齊，不必人工回頭找。
它只負責**產出音檔**；上傳、補分頁連結、勾追蹤表、推 LINE 全部照舊交給
下一輪的 `sweep_podcasts.py` —— 那條路已經驗證過，不重複實作。

## 選材與 dispatcher 完全一致

來源不重新研究，直接沿用檢查點裡 `readiness` 階段存下來的新聞稿／逐字稿網址，
再過一次 `sources.discover()` 補鏡像 —— 跟 dispatcher 當下用的是同一套邏輯，
所以補出來的節目跟當初該產出的那集是同一份素材。

用法：
    python tools/backfill_podcasts.py                # 只列出缺哪些
    python tools/backfill_podcasts.py --run          # 真的補（預設一次 1 檔）
    python tools/backfill_podcasts.py --run --limit 3
"""

import _bootstrap  # noqa: F401

import argparse
import datetime as dt
import html
import re
import subprocess
import sys
from pathlib import Path

from core import checkpoint as cp
from core import quarters, sources
from core.gauth import load_config

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _urls(text: str) -> list[str]:
    """從就緒偵測的說明字串裡挖出來源網址。

    要 unescape：那些字串來自 RSS 標題／摘要，網址裡的 & 是 HTML 實體 &amp;，
    直接餵給 NotebookLM 會 404。（與 dispatcher._urls 同一份邏輯。）
    """
    return [html.unescape(u)
            for u in re.findall(r"https?://[^\s一-鿿）)]+", text or "")]


def missing(cfg: dict) -> list[dict]:
    """列出「memo 有了、音檔還沒有」的檔，附上補做需要的全部資料。"""
    sd = cfg["local"]["state_dir"]
    pod_root = Path(cfg["local"]["podcast_dir"])
    out: list[dict] = []

    for ck in cp.load_all(sd):
        # memo 還沒產出的不算積欠 —— 那是 dispatcher 還沒輪到，不是漏掉。
        if not ck.is_done("memo_built"):
            continue
        if ck.is_done("podcast_built") or ck.is_done("podcast_uploaded"):
            continue

        call_at = (ck.detail("date_confirmed") or {}).get("call_at")
        if not call_at:
            # 沒有法說會日期就推不出資料夾季度，也挑不了來源時間窗
            out.append({"ck": ck, "skip": "檢查點沒有 call_at，無法推導資料夾季度"})
            continue
        call_d = dt.datetime.fromisoformat(call_at).date()
        folder = quarters.local_folder_label(call_d)
        m4a = pod_root / folder / f"{ck.ticker} {ck.quarter}.m4a"
        legacy = pod_root / folder / f"{ck.ticker} {folder}.m4a"

        # 音檔已經在磁碟上（新舊命名都認）→ 不是這支的事，sweep 下一輪就會上傳
        if m4a.exists() or legacy.exists():
            continue

        rd = ck.detail("readiness") or {}
        out.append({
            "ck": ck, "call_date": call_d, "folder": folder, "m4a": m4a,
            "extra": _urls(rd.get("press_release", "")) + _urls(rd.get("transcript", "")),
            "json": (ck.detail("memo_built") or {}).get("json"),
            "skip": None,
        })
    return out


def build_one(item: dict, *, timeout: int = 2400) -> bool:
    ck = item["ck"]
    call_d = item["call_date"]
    wy, wq = quarters.calendar_quarter_for(call_d)
    srcs, notes = sources.discover(ck.ticker, call_date=call_d,
                                   want_q=wq, want_y=wy, extra=item["extra"])
    for n in notes:
        print(f"      · {n}")
    if not srcs:
        print(f"    [!] {ck.ticker}：沒有可用的來源網址，跳過")
        return False

    # --tab-quarter=ck.quarter：checkpoint key 與檔名都用分頁季度（財政），
    # 不傳的話 make_podcast 會退回曆年季度，非曆年財年公司會寫錯 checkpoint 檔。
    cmd = [sys.executable, str(Path(__file__).with_name("make_podcast.py")),
           "--ticker", ck.ticker, "--quarter", item["folder"],
           "--tab-quarter", ck.quarter]
    for s in srcs:
        cmd += ["--source", s]
    if item["json"] and Path(item["json"]).exists():
        cmd += ["--focus-from", item["json"]]

    print(f"    產 podcast（notebooklm CLI，{len(srcs)} 個來源）…")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_DIR,
                       encoding="utf-8", errors="replace", timeout=timeout)
    for line in (r.stdout or r.stderr or "").strip().splitlines()[-3:]:
        print(f"      {line}")
    if r.returncode != 0:
        print(f"    [X] {ck.ticker} 補做失敗 rc={r.returncode}")
        return False
    print(f"    [OK] {ck.ticker} {ck.quarter} podcast 補齊；"
          f"sweep_podcasts 下一輪會上傳並推 LINE")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="真的補做（否則只列出）")
    ap.add_argument("--limit", type=int, default=1,
                    help="一次最多補幾檔。預設 1 —— 一集動輒十幾分鐘，"
                         "排程每 20 分鐘一輪，補太多會讓下一輪一直被 IgnoreNew 擋掉")
    ap.add_argument("--ticker", help="只補指定代號")
    args = ap.parse_args()

    cfg = load_config()
    items = missing(cfg)
    if args.ticker:
        items = [i for i in items if i["ck"].ticker.upper() == args.ticker.upper()]

    blocked = [i for i in items if i.get("skip")]
    todo = [i for i in items if not i.get("skip")]

    print(f"積欠 podcast：{len(todo)} 檔")
    for i in todo:
        print(f"   {i['ck'].ticker:6s} {i['ck'].quarter}  → {i['m4a'].name}"
              f"（既有來源 {len(i['extra'])} 個）")
    if blocked:
        print(f"\n無法補做（{len(blocked)}）")
        for i in blocked:
            print(f"   {i['ck'].ticker:6s} {i['ck'].quarter}：{i['skip']}")

    if not args.run:
        if todo:
            print("\n（僅列出。要真的補做請加 --run）")
        return 0
    if not todo:
        return 0

    ok = 0
    for i in todo[: args.limit]:
        print(f"\n>>> {i['ck'].ticker} {i['ck'].quarter}")
        if build_one(i):
            ok += 1
    left = max(0, len(todo) - args.limit)
    print(f"\n補齊 {ok} 檔" + (f"，還有 {left} 檔待下一輪" if left else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
