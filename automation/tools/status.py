"""看板：一眼看出每檔每季卡在哪個階段。

    python tools/status.py                    # 全部
    python tools/status.py --quarter 2026Q2   # 只看某季
    python tools/status.py --stuck            # 只看沒完成的
    python tools/status.py --detail VST       # 看單一檔的細節
    python tools/status.py --reset VST 2026Q2 --from memo_built   # 從某階段重跑
"""

import _bootstrap  # noqa: F401

import argparse

from core import checkpoint as cp
from core.gauth import load_config


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarter", default=None)
    ap.add_argument("--stuck", action="store_true", help="只列出未完成的")
    ap.add_argument("--detail", default=None, metavar="TICKER")
    ap.add_argument("--reset", nargs=2, metavar=("TICKER", "QUARTER"))
    ap.add_argument("--from", dest="from_stage", default=None,
                    help="搭配 --reset：從哪個階段起重跑（含該階段）；省略則全清")
    args = ap.parse_args()

    cfg = load_config()
    sd = cfg["local"]["state_dir"]

    # ---------- reset ----------
    if args.reset:
        tk, q = args.reset
        c = cp.load(sd, tk, q)
        before = c.bar()
        c.reset(args.from_stage)
        print(f"{tk} {q}\n  before {before}\n  after  {c.bar()}")
        if args.from_stage:
            print(f"  已清除『{cp.STAGE_LABEL[args.from_stage]}』及其之後的階段")
        else:
            print("  已清除全部階段")
        return 0

    # ---------- detail ----------
    if args.detail:
        found = [c for c in cp.load_all(sd) if c.ticker == args.detail.upper()]
        if not found:
            print(f"查無 {args.detail} 的檢查點")
            return 1
        for c in found:
            print(f"\n=== {c.ticker} {c.quarter} ===  {c.bar()}  {c.done_count}/{len(cp.STAGE_KEYS)}")
            for k, label, hint in cp.STAGES:
                st = c.data["stages"].get(k, {})
                mark = "●" if st.get("done") else ("✗" if st else "○")
                at = st.get("at", "")[:16].replace("T", " ")
                print(f"  {mark} {label:16s} {at:17s} {hint}")
                d = st.get("detail") or {}
                for dk, dv in d.items():
                    print(f"       {dk} = {str(dv)[:96]}")
        return 0

    # ---------- 總覽 ----------
    rows = cp.load_all(sd)
    if args.quarter:
        rows = [c for c in rows if c.quarter == args.quarter]
    if args.stuck:
        rows = [c for c in rows if not c.complete]

    if not rows:
        print("尚無檢查點。跑過 dispatcher / publish_memo 之後就會產生。")
        return 0

    print("階段：" + "  ".join(f"{i+1}.{label}" for i, (_, label, _) in enumerate(cp.STAGES)))
    print()
    for c in sorted(rows, key=lambda x: (x.quarter, x.done_count, x.ticker)):
        print("  " + c.summary())

    done = sum(1 for c in rows if c.complete)
    print(f"\n共 {len(rows)} 檔；完成 {done}；未完成 {len(rows) - done}")

    # 卡在哪一關的統計 —— 看出瓶頸在哪
    stuck: dict[str, int] = {}
    for c in rows:
        n = c.next_stage()
        if n:
            stuck[n] = stuck.get(n, 0) + 1
    if stuck:
        print("\n卡關分布：")
        for k, _l, _h in cp.STAGES:
            if k in stuck:
                print(f"  {cp.STAGE_LABEL[k]:16s} {stuck[k]:3d} 檔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
