"""無人值守產出 podcast —— 方案 A。

為什麼不能沿用 earnings-memo skill 的 Step 6：
skill 是用 `mcp__claude-in-chrome__*` 操作 NotebookLM 網頁版，
但排程器叫起來的 headless CLI 沒有任何瀏覽器 MCP
（實測 `claude mcp list` 只有 Drive / Gmail / Calendar / Slack）。

改走 notebooklm-py CLI。參數與網頁版一對一對得上（皆為實測 --help 確認）：

    網頁版                     CLI
    格式＝深入探索              --format deep-dive（預設）
    長度＝短                   --length short
    語言＝中文（繁體）           --language zh_Hant   ← CLI 語言清單確認
    Focus prompt              位置參數
    等生成                     --wait --timeout
    下載                       notebooklm download audio <path>

流程：建 notebook → 加來源網址 → 生成 → 等待 → 下載並更名。

**付費牆來源會失敗**：NotebookLM 是伺服器端抓取，沒有使用者 session，
Seeking Alpha 這類需登入的頁面會被標紅並靜默排除。所以只餵免費來源
（IR 新聞稿、PR Newswire/GlobeNewswire 鏡像、Fool 逐字稿）。

用法：
    python tools/make_podcast.py --ticker CRWV --quarter 2Q26 \
        --source https://... --source https://... [--focus-from MEMO.json]
"""

import _bootstrap  # noqa: F401

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from core import checkpoint as cp
from core.gauth import load_config

CLI = "notebooklm"
LANG = "zh_Hant"          # 繁體中文（CLI `language list` 確認）
FORMAT = "deep-dive"      # 深入探索
LENGTH = "short"          # 短


class PodcastError(RuntimeError):
    pass


def run(args: list[str], *, timeout: int = 300, check: bool = True) -> str:
    r = subprocess.run([CLI, *args], capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise PodcastError(f"notebooklm {' '.join(args[:2])} 失敗 rc={r.returncode}: "
                           f"{(r.stderr or r.stdout or '')[:400]}")
    return r.stdout or ""


def ensure_auth() -> None:
    """非互動模式不能開瀏覽器等人登入 —— 沒授權就明確報錯，不要靜默卡住。"""
    out = run(["auth", "check", "--test", "--json"], timeout=120, check=False)
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        raise PodcastError(f"auth check 回傳非 JSON：{out[:200]}")
    if d.get("status") != "ok" or not (d.get("checks") or {}).get("token_fetch"):
        raise PodcastError(
            "NotebookLM 尚未授權（或 cookie 已失效）。\n"
            "    請在有畫面的環境執行一次：notebooklm login\n"
            f"    目前狀態：{d.get('status')}  checks={d.get('checks')}"
        )


def existing_titles(nb: str | None) -> set[str]:
    """已在 notebook 裡的來源標題（memo 這種上傳檔沒有 URL，只能靠標題比對）。"""
    if not nb:
        return set()
    out = run(["source", "list", "-n", nb, "--json"], timeout=180, check=False)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return set()
    try:
        return {s.get("title", "") for s in json.loads(m.group(0)).get("sources", [])}
    except (json.JSONDecodeError, TypeError):
        return set()


def existing_sources(nb: str | None) -> tuple[set[str], set[str]]:
    """回傳 (可用的來源網址, 失敗的來源網址)。

    只有 status='ready' 才算可用。抓取失敗的來源會以 status='error' 留在
    notebook 裡（實測 CEG：IR 首頁抓不到內容，但那筆仍列在 source list），
    若把它算成「已存在」就會高估素材數 —— 極端情況下所有來源都失敗，
    卻仍判定有料可生成，產出一集空洞的節目。
    """
    if not nb:
        return set(), set()
    out = run(["source", "list", "-n", nb, "--json"], timeout=180, check=False)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return set(), set()
    try:
        srcs = json.loads(m.group(0)).get("sources", [])
    except (json.JSONDecodeError, TypeError):
        return set(), set()
    ok = {s["url"] for s in srcs if s.get("url") and s.get("status") == "ready"}
    bad = {s["url"] for s in srcs if s.get("url") and s.get("status") != "ready"}
    return ok, bad


def find_notebook(name: str) -> str | None:
    """依名稱找既有 notebook。重跑時沿用，避免每次都在帳號裡堆一份重複的。"""
    out = run(["list", "--json"], timeout=180, check=False)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    items = data if isinstance(data, list) else (data.get("notebooks") or data.get("items") or [])
    for it in items:
        if isinstance(it, dict) and (it.get("title") or it.get("name") or "").strip() == name:
            return it.get("id") or it.get("notebook_id")
    return None


def _segs_text(item) -> str:
    segs = item.get("segments", []) if isinstance(item, dict) else item
    return re.sub(r"\s+", " ", "".join(s.get("text", "") for s in segs)).strip()


def memo_to_text(memo: dict, ticker: str, quarter: str) -> str:
    """把 memo 攤平成純文字，當作 NotebookLM 的一份來源。

    這是繞過付費牆的關鍵：memo 的 Q&A 段落是 agent 用**已登入的瀏覽器**
    從 Seeking Alpha 抓來的，而 NotebookLM 伺服器端抓取進不去那些頁面。
    memo 本身沒有這個限制 —— 它就是一份本機檔案。
    所以餵 memo 等於把付費牆內容合法地帶進 NotebookLM。

    附帶效果：memo 是已經查證、去蕪存菁過的內容，比原始網頁更適合當素材。
    """
    lines = [f"{ticker} {quarter} 財報 memo（已查證整理，含法說會 Q&A）",
             memo.get("title", ""), memo.get("release_date", ""), ""]
    if memo.get("tldr"):
        lines += ["【重點摘要】"]
        lines += [f"{i}. {_segs_text(x)}" for i, x in enumerate(memo["tldr"], 1)]
        lines.append("")
    for sec in memo.get("sections", []):
        lines.append(f"【{sec.get('heading','')}】")
        lines += [f"- {_segs_text(x)}" for x in sec.get("items", [])]
        lines.append("")
    return "\n".join(l for l in lines if l is not None)


# 反廢話指令。NotebookLM 的 Deep Dive 預設很愛寒暄、繞圈、互相附和，
# 短長度只縮短總時長、不會提高密度，所以必須在 focus prompt 裡明講。
NO_FILLER = (
    "風格要求：直接進入內容，不要開場寒暄、不要自我介紹、不要「這真是個好問題」"
    "這類客套與互相附和；不要重複同一件事、不要為了過渡而說廢話、不要空泛的"
    "感嘆與總結性套話。每一句都要帶資訊：數字、對比、原因或影響。"
    "寧可短，也不要用填充語句撐長度。"
)


def focus_prompt(memo: dict, ticker: str, quarter: str) -> str:
    """用 memo 的 tldr 造 focus prompt，並明確要求高資訊密度。"""
    bits = [t for t in (_segs_text(x) for x in (memo.get("tldr") or [])[:5]) if t]
    if bits:
        body = (f"請聚焦 {ticker} {quarter} 這一季的以下重點，並用具體數字展開："
                + "；".join(b[:180] for b in bits))
    else:
        body = (f"請聚焦 {ticker} {quarter} 這一季的營收、獲利、各部門表現、"
                f"指引變動與市場反應，一律用具體數字說明。")
    return body + "\n\n" + NO_FILLER


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--quarter", required=True, help="本機資料夾季度標籤，例如 2Q26")
    ap.add_argument("--source", action="append", default=[], help="來源網址，可重複")
    ap.add_argument("--focus-from", default=None,
                    help="memo JSON 或 .docx：既用來生成 focus prompt，也會整份當成一份來源")
    ap.add_argument("--no-memo-source", action="store_true",
                    help="不要把 memo 當來源（只用網址）")
    ap.add_argument("--tab-quarter", default=None,
                    help="分頁季度（如 2026Q4）。checkpoint key 與輸出檔名都用它；"
                         "不給則由 --quarter 機械式轉換（2Q26→2026Q2，只對曆年制"
                         "公司正確——非曆年財年公司請務必傳入，否則 podcast_built "
                         "會寫進別季的 checkpoint 檔）")
    ap.add_argument("--out", default=None, help="覆寫輸出路徑（測試比較用）")
    ap.add_argument("--focus", default=None, help="直接指定 focus prompt")
    ap.add_argument("--timeout", type=int, default=1500, help="等待生成的秒數")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    ticker, quarter = args.ticker.upper(), args.quarter
    tab_q = args.tab_quarter or _tab_quarter(quarter)
    # 檔名用分頁季度（財政）：本機檔名、Drive 檔名、Doc 分頁三邊同一個標籤。
    # 資料夾仍用曆年短標籤（2Q26）—— 那是「這一波財報季」的桶子。
    name = f"{ticker} {tab_q}"
    out_dir = Path(cfg["local"]["podcast_dir"]) / quarter
    out_path = Path(args.out) if args.out else out_dir / f"{name}.m4a"
    legacy_path = out_dir / f"{ticker} {quarter}.m4a"

    if out_path.exists():
        print(f"[skip] 已存在 {out_path}")
        return 0
    if not args.out and legacy_path.exists():
        print(f"[skip] 已存在（舊命名）{legacy_path}")
        return 0

    free_sources = [s for s in args.source if "seekingalpha.com" not in s.lower()]
    dropped = [s for s in args.source if s not in free_sources]

    # memo 同時有兩個用途：造 focus prompt，以及自己當一份來源
    memo_obj: dict = {}
    if args.focus_from and Path(args.focus_from).exists():
        p = Path(args.focus_from)
        if p.suffix.lower() == ".docx":
            from core import docx_reader

            memo_obj = docx_reader.read(p)
        else:
            memo_obj = json.loads(p.read_text(encoding="utf-8"))
    if args.no_memo_source:
        memo_obj = {}
    focus = args.focus or focus_prompt(memo_obj, ticker, quarter)

    print(f"標的 {name}")
    print(f"  memo 當來源：{'是（含 Q&A，可繞過付費牆）' if memo_obj else '否'}")
    print(f"  網址來源 {len(free_sources)} 個" + (f"（排除付費牆 {len(dropped)} 個）" if dropped else ""))
    for s in free_sources:
        print(f"    {s[:96]}")
    print(f"  參數 format={FORMAT} length={LENGTH} language={LANG}")
    print(f"  focus {focus[:110]}…")
    print(f"  輸出 {out_path}")

    if args.dry_run:
        print("\n[dry-run] 未實際生成。")
        return 0
    if not free_sources:
        raise PodcastError("沒有可用的免費來源網址 —— NotebookLM 抓不到付費牆頁面")

    ensure_auth()

    print("\n① 建立 notebook…")
    nb = find_notebook(name)
    if nb:
        run(["use", nb], timeout=120)
        print(f"   沿用既有 notebook（重跑不會建出第二個）  id={nb}")
    else:
        run(["create", name, "--use"], timeout=180)
        nb = find_notebook(name)
        print(f"   已建立  id={nb}")

    # 之後每一個子指令都明講 -n。`use` 設的是 CLI profile 裡的「當前 notebook」，
    # 那是跨進程的全域狀態：兩支 make_podcast 同時跑（排程器做完一檔就接著下一檔，
    # 人工補跑又插進來）時，後者的 `use` 會把前者的當前 notebook 換掉，
    # generate/download 就會作用在別檔的 notebook 上，音檔張冠李戴。
    N = ["-n", nb]

    print("② 加入來源…")
    ready_urls, error_urls = existing_sources(nb)
    added, failed = 0, []
    for s in free_sources:
        if s in ready_urls:
            added += 1          # 已在 notebook 裡且狀態正常，算可用素材
            print(f"   = 已存在（ready），略過 {s[:66]}")
            continue
        if s in error_urls:
            failed.append(s)
            print(f"   ! 先前加入已失敗，不重試 {s[:60]}")
            continue
        # 單一來源失敗**不能**中止整個流程 —— 無人值守時一定會遇到
        # 付費牆、JS 清單頁、死連結。用得到的來源繼續，全部失敗才放棄。
        try:
            run(["source", "add", s, "--type", "url", "--timeout", "120", *N], timeout=180)
            added += 1
            print(f"   + {s[:80]}")
        except PodcastError as exc:
            failed.append(s)
            reason = "抓不到內容（付費牆／JS 頁面／無效連結）"
            print(f"   ! 跳過 {s[:70]} — {reason}")

    # memo 本身當一份來源 —— 它含有 NotebookLM 抓不到的付費牆逐字稿內容
    if memo_obj:
        # 上傳檔案時 --title 不生效（實測：來源標題就是檔名），
        # 所以直接把暫存檔命名成我們要的標題，去重才比對得到。
        stem = f"{ticker} {quarter} 財報 memo"
        titles = existing_titles(nb)
        if stem in titles or f"{stem}.txt" in titles:
            print("   = memo 已在來源清單，略過")
            added += 1
        else:
            txt = memo_to_text(memo_obj, ticker, quarter)
            tmp = Path(cfg["local"]["state_dir"]) / f"{stem}.txt"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(txt, encoding="utf-8")
            try:
                # 走檔案而非命令列字串：memo 動輒上萬字，Windows 命令列會截斷
                run(["source", "add", str(tmp), "--type", "file",
                     "--mime-type", "text/plain", *N], timeout=240)
                added += 1
                print(f"   + memo 全文（{len(txt)} 字，含 Q&A）")
            except PodcastError as exc:
                failed.append("memo")
                print(f"   ! memo 加入失敗：{str(exc)[:100]}")
            finally:
                tmp.unlink(missing_ok=True)

    if added == 0:
        raise PodcastError(
            f"{len(failed)} 個來源全部加入失敗，沒有素材可生成。\n"
            + "\n".join(f"    {s}" for s in failed)
        )
    if failed:
        print(f"   （{added} 成功／{len(failed)} 失敗，以成功的繼續）")

    print(f"③ 生成語音摘要（等待上限 {args.timeout}s）…")
    run(["generate", "audio", focus, *N,
         "--format", FORMAT, "--length", LENGTH, "--language", LANG,
         "--wait", "--timeout", str(args.timeout)], timeout=args.timeout + 120)

    print("④ 下載…")
    out_dir.mkdir(parents=True, exist_ok=True)
    run(["download", "audio", str(out_path), *N], timeout=600)

    if not out_path.exists():
        raise PodcastError(f"下載後找不到檔案：{out_path}")
    mb = out_path.stat().st_size / 1024 / 1024
    print(f"   {out_path.name}  {mb:.1f} MB")

    ck = cp.load(cfg["local"]["state_dir"], ticker, tab_q)
    ck.mark("podcast_built", file=str(out_path), size_mb=round(mb, 1),
            via="notebooklm-cli", sources=len(free_sources))
    print(f"\n[OK] {name} podcast 完成；sweep_podcasts 會在下一輪上傳歸檔")
    return 0


def _tab_quarter(local_q: str) -> str:
    """2Q26 → 2026Q2，對齊檢查點與分頁的季度標籤。"""
    m = re.match(r"(\d)Q(\d{2})$", local_q.strip(), re.I)
    return f"20{m.group(2)}Q{m.group(1)}" if m else local_q


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PodcastError as exc:
        print(f"\n[X] {exc}", file=sys.stderr)
        raise SystemExit(2)
