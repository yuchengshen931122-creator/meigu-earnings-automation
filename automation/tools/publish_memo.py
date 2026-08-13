"""歸檔：把一份跑完的 memo + podcast 送上 Drive。

這支腳本補的是 earnings-memo skill **沒有**的能力。
skill 自己的 SKILL.md 寫明：批次執行「end at save-to-MEMO plus the renamed .m4a
sitting in the Downloads folder」——上傳 Drive、開 Google Doc 分頁、回填 podcast
連結，這三件 skill 都不做，scripts/ 底下也沒有任何 Google API 程式碼。

順序是寫死的，因為分頁內容第 3 行就是 podcast 連結：

    ① 上傳 .m4a 到 podcast 樹的分類資料夾  →  取得 Drive 連結
    ② 找到／建立 Word 樹裡的『{TICKER} 財報』Google Doc
    ③ 新增（或沿用）分頁『{TICKER} {year}Q{q}』
    ④ 把 memo JSON 渲染進該分頁，第一行就帶上 ① 的連結

全程冪等：重跑同一個 ticker 不會產生第二份音檔、第二個分頁。

用法：
    python tools/publish_memo.py --ticker VST --tab-name "VST 2026Q2" \
        --json  "...\\MEMO\\_work\\vst_2q26.json" \
        --m4a   "...\\PODCAST\\2Q26\\VST 2Q26.m4a" \
        --report-date 2026/08/07
    加 --dry-run 只檢查不寫入。
"""

import _bootstrap  # noqa: F401

import argparse
import json
import re
from pathlib import Path

from core import checkpoint as cp
from core import docx_reader, tree_match
from core.docs_client import MEMO_DOC_SUFFIX, DocsClient
from core.drive_client import DriveClient
from core.gauth import load_config, load_credentials


class PublishError(RuntimeError):
    pass


def load_drive_map(cfg: dict) -> dict:
    p = Path(cfg["local"]["state_dir"]) / "drive_map.json"
    if not p.exists():
        raise PublishError(f"找不到 {p}，請先跑 tools/build_drive_map.py")
    return json.loads(p.read_text(encoding="utf-8"))


def _ymd(text: str | None) -> str | None:
    """從 memo 的 release_date 或分頁前幾行挖出報告日期，正規化成 YYYYMMDD。

    格式在各分頁間並不統一：2026/02/04、2026.05.06、20250806 都有。
    只看前 3 行 —— 報告日期固定在標題的下一行，再往下就會挖到內文裡的數字。
    """
    if not text:
        return None
    head = "\n".join(str(text).split("\n")[:3])
    m = re.search(r"(20\d{2})[./-]?(\d{2})[./-]?(\d{2})", head)
    return "".join(m.groups()) if m else None


def resolve_folder(dmap: dict, ticker: str, tree: str) -> tuple[str, str]:
    """回傳 (folder_id, 說明)。

    podcast 樹每季重建，多數 ticker 本季還沒有落點（實測：追蹤清單 112 檔有 44 檔
    沒有），所以找不到時要從 Word 樹的分類推對應位置，而不是直接放棄。
    """
    entry = dmap["ticker_index"].get(ticker) or {}
    if entry.get(tree):
        return entry[tree], "對照表既有位置"

    if tree == "podcast" and entry.get("word"):
        wf = {f["id"]: f for f in dmap["trees"]["word"]["folders"]}.get(entry["word"])
        if wf:
            pf = dmap["trees"]["podcast"]["folders"]
            hit, how = tree_match.find_counterpart(wf["path"], pf)
            if hit:
                return hit["id"], f"由 Word 樹分類『{'/'.join(wf['path'])}』對映（{how}）"
            top = tree_match.top_level_for(wf["path"], pf)
            if top:
                return f"__CREATE__{top['id']}__{wf['path'][-1]}", (
                    f"podcast 樹無對應分類，將在『{top['name']}』底下補建『{wf['path'][-1]}』"
                )

    raise PublishError(
        f"{ticker} 在 {tree} 樹裡找不到歸檔位置，也無法從另一棵樹推導。"
        f"不猜分類 —— 請先在 Drive 上建好該分類，再重跑 build_drive_map.py。"
        f"（派工時帶 --industry 可用追蹤表「產業」欄自動建檔）"
    )


def _match_category(folders: list[dict], industry: str) -> dict | None:
    """用「產業」欄找分類資料夾。

    兩邊都是複合詞：資料夾名如『傳統能源/核能/再生能源』（斜線是名字的一部分，
    不是層級），產業欄如『能源/鈾鋰礦』『國防/太空』。所以三級比對：
      0 葉名正規化後完全相等（含 tree_match 別名表，雙向）
      1 任一產業段落 == 任一葉名段落（『國防/太空』命中『國防/戰略物資/太空/無人機』）
      2 產業段落包含於葉名段落（『能源』⊂『傳統能源』）。刻意只做這個方向 ——
        反向（葉名⊂產業）會讓樹上叫『測試』之類的短名資料夾吸走
        『量子運算測試』這種長產業名（實測踩過）。寬鬆匹配是最後手段，
        只在 ticker 完全沒有落點時走到；寧可漏配去樹根建新資料夾
        （看得見、搬得動），也不要錯進不相干的分類
    同級多個命中取最淺層（頂層分類優先），全部落空回 None。
    """
    key = tree_match.normalize(industry)
    ind_segs = [tree_match.normalize(s) for s in industry.split("/") if s.strip()]
    best: tuple | None = None
    for f in folders:
        leaf_raw = f["path"][-1]
        leaf = tree_match.normalize(leaf_raw)
        leaf_segs = [tree_match.normalize(s) for s in leaf_raw.split("/") if s.strip()]
        if leaf == key or tree_match.ALIASES.get(key) == leaf \
                or tree_match.ALIASES.get(leaf) == key:
            rank = 0
        elif key in leaf_segs or any(s in leaf_segs for s in ind_segs):
            rank = 1
        elif any(len(a) >= 2 and a in b for a in ind_segs for b in leaf_segs):
            rank = 2
        else:
            continue
        cand = (rank, f.get("depth", 9))
        if best is None or cand < best[:2]:
            best = (*cand, f)
    return best[2] if best else None


def industry_plan(dmap: dict, ticker: str, tree: str, industry: str
                  ) -> tuple[str, list[str], str]:
    """兩棵樹都推不出落點時的最後手段：用追蹤表「產業」欄決定分類。

    回傳 (起點資料夾 id, 待補建的資料夾名鏈, 說明)。呼叫端沿著鏈逐層
    ensure_folder，最後一層就是落點。設計取捨：

    - 先在該樹既有分類裡找「產業」的正規化命中（含兩棵樹的別名表，雙向查）。
      命中的話：word 樹在分類底下補建 {TICKER} 子資料夾（樹上兩種慣例並存，
      建子資料夾的那種下次 build_drive_map 掃得回來）；podcast 樹直接用
      分類資料夾（該樹慣例是檔案直接放分類底下）。
    - 全樹都沒有這個分類 → 在樹根補建以「產業」欄原文命名的分類資料夾。
      名字照抄不改寫 —— 這會出現在團隊共用的 Drive 上，寧可名字與現況
      重複醜一點（人看得懂、搬得動），也不要猜錯類把檔案藏到沒人找的地方。
    - 多個同名分類命中時挑最淺的那個（頂層優先），與 tree_match 的取向一致。
    """
    if not industry:
        raise PublishError(
            f"{ticker} 沒有落點，追蹤表「產業」欄也是空的 —— 無法自動建檔。"
            f"請在表上填產業，或先在 Drive 建好資料夾再重跑 build_drive_map.py。")

    cat = _match_category(dmap["trees"][tree]["folders"], industry)
    if cat:
        if tree == "word":
            return cat["id"], [ticker], (
                f"產業『{industry}』→ 既有分類『{'/'.join(cat['path'])}』，補建 {ticker}/")
        return cat["id"], [], f"產業『{industry}』→ 既有分類『{'/'.join(cat['path'])}』"

    root = dmap["trees"][tree]["root_id"]
    chain = [industry, ticker] if tree == "word" else [industry]
    return root, chain, f"產業『{industry}』全樹無對應分類，於樹根補建『{industry}』"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--tab-name", required=True, help="分頁名，例如 'VST 2026Q2'")
    # 兩個都不給也可以：分頁若已由人工寫好（memo 在 Google Doc、本機沒有檔案），
    # 就只需要上傳音檔並把連結插進既有分頁 —— 那種情況沒有 memo 來源可給。
    # 兩個都給也可以，而且是建議做法：.docx 負責內容，JSON 只用來取 metadata。
    ap.add_argument("--docx", dest="docx_path",
                    help="memo 本體。**優先來源** —— 分頁要跟它逐字一致")
    ap.add_argument("--json", dest="json_path",
                    help="退路：找不到 .docx 時才用。JSON 渲染器看不到 dashboard 表、"
                         "type=table 的 rows 與 type=qa 的問答，會少掉一大半內容")
    ap.add_argument("--m4a", dest="m4a_path", default=None,
                    help="音檔。manual 模式下 memo 先歸檔、音檔之後由 sweep_podcasts 補上，"
                         "此時可省略——分頁會先不帶 podcast 連結，補上傳時再插入")
    ap.add_argument("--report-date", default=None, help="顯示在分頁第 2 行，例如 2026/08/07")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="忽略檢查點，強制重做（會受 --on-existing-tab 保護）")
    ap.add_argument("--drive-name", default=None,
                    help="音檔在 Drive 上的檔名，預設用分頁名（對齊 Drive 既有慣例）")
    ap.add_argument("--on-existing-tab", choices=["abort", "skip", "overwrite"],
                    default="abort",
                    help="分頁已存在且有內容時怎麼辦。預設 abort（安全優先，不動既有內容）")
    ap.add_argument("--industry", default=None,
                    help="追蹤表「產業」欄。新 ticker 兩棵樹都沒有落點時，"
                         "用它找（或補建）分類資料夾，不再直接失敗")
    args = ap.parse_args()

    cfg = load_config()
    ticker = args.ticker.strip().upper()
    dmap = load_drive_map(cfg)

    # 檢查點：已完成的階段直接跳過，不重做昂貴步驟。也是 .docx 路徑的來源之一。
    ck = cp.load(cfg["local"]["state_dir"], ticker, args.tab_name.split(" ", 1)[-1])

    podcast_only = not args.json_path and not args.docx_path
    docx_path = Path(args.docx_path) if args.docx_path else None
    if docx_path is None and args.json_path:
        # 只給了 JSON：把同一次產出的 .docx 找回來。分頁要跟使用者手上那份逐字
        # 一致，而 JSON 渲染器做不到這件事（LITE 2026Q4 因此少了 4,204 字與 5 張表）。
        hint = ck.detail("memo_built").get("docx") if ck.is_done("memo_built") else None
        if hint and Path(hint).exists():
            docx_path = Path(hint)
            podcast_only = False

    memo: dict = {}
    if podcast_only:
        if not args.m4a_path:
            raise PublishError("沒有給 --json/--docx 也沒有 --m4a，沒有東西可以歸檔")
        print("  模式：只補音檔（memo 分頁已由人工寫好，本機無檔案）")
    elif docx_path:
        if not docx_path.exists():
            raise PublishError(f"找不到 memo：{docx_path}")
        memo = docx_reader.read(docx_path)      # 只為了拿 release_date 做撞名檢查
        print(f"  來源：{docx_path.name}（逐字重建，含表格與圖片）")
    else:
        memo = json.loads(Path(args.json_path).read_text(encoding="utf-8"))
        print("  [!] 只有 JSON、找不到 .docx —— 分頁會少掉表格、Q&A 與損益表圖片")

    m4a = Path(args.m4a_path) if args.m4a_path else None
    if m4a and not m4a.exists():
        raise PublishError(f"找不到音檔：{m4a}")

    industry = (args.industry or "").strip()

    def _landing(tree: str):
        """回傳 (folder_id, 補建計畫, 說明)。folder_id=None 表示稍後照計畫補建。"""
        try:
            fid, how = resolve_folder(dmap, ticker, tree)
            return fid, None, how
        except PublishError:
            if not industry:
                raise
            start, chain, how = industry_plan(dmap, ticker, tree, industry)
            if not chain:
                return start, None, how
            return None, (start, chain), how

    podcast_folder, pod_plan, pod_how = ("", None, "（本次不處理音檔）")
    if m4a:
        podcast_folder, pod_plan, pod_how = _landing("podcast")
    word_folder, word_plan, word_how = _landing("word")

    print(f"ticker={ticker}  分頁={args.tab_name}")
    print(f"  podcast 目的地: {podcast_folder or ('（待補建）' if pod_plan else '—')}  ({pod_how})")
    print(f"  memo    目的地: {word_folder or '（待補建）'}  ({word_how})")
    print(f"  音檔: {f'{m4a.name} ({m4a.stat().st_size / 1024 / 1024:.1f} MB)' if m4a else '本次無（memo 先行）'}")

    print(f"  檢查點: {ck.bar()} {ck.done_count}/{len(cp.STAGE_KEYS)}"
          + (f"（--force 可忽略）" if ck.done_count else ""))

    if args.dry_run:
        print("\n[dry-run] 以上檢查通過，未寫入任何東西。")
        return 0

    creds = load_credentials(interactive=False)
    drive = DriveClient(creds)
    docs = DocsClient(creds, drive)

    # 需要補建分類資料夾的情況
    if podcast_folder and podcast_folder.startswith("__CREATE__"):
        _, parent_id, leaf = podcast_folder.split("__", 3)[1:]
        podcast_folder = drive.ensure_folder(parent_id, leaf)
        print(f"  已補建 podcast 分類資料夾『{leaf}』{podcast_folder}")

    # 產業欄自動建檔：新 ticker 兩棵樹都沒有落點時，照 industry_plan 的鏈逐層補建
    def _build_chain(plan) -> str:
        pid, chain = plan
        for leaf in chain:
            pid = drive.ensure_folder(pid, leaf)
            print(f"  已補建資料夾『{leaf}』{pid}")
        return pid

    if word_plan:
        word_folder = _build_chain(word_plan)
    if pod_plan:
        podcast_folder = _build_chain(pod_plan)
    if word_plan or pod_plan:
        # 立刻記回 drive_map：同一輪稍後的呼叫與下一輪 sweep 直接查得到，
        # 不必等每日 build_drive_map 重掃（重掃會以 Drive 現況覆蓋，結果一致）。
        entry = dmap["ticker_index"].setdefault(ticker, {"word": None, "podcast": None})
        if word_plan:
            entry["word"] = word_folder
        if pod_plan:
            entry["podcast"] = podcast_folder
        (Path(cfg["local"]["state_dir"]) / "drive_map.json").write_text(
            json.dumps(dmap, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  drive_map.json 已更新（含新建落點）")

    # ① podcast 先上傳 —— 分頁內容需要它的連結
    # Drive 上的多數慣例是 "{TICKER} {YYYY}Q{n}.m4a"（LNG 2026Q2.m4a、CEG 2026Q2.m4a…），
    # 與分頁名一致；本機檔名用的是 "{TICKER} {n}Q{yy}"（VST 2Q26.m4a），屬少數派。
    # 上傳時統一改用分頁名，避免同一資料夾裡三種格式並存。
    print("\n① 上傳 podcast…")
    podcast_url = None
    up = None   # 只有「這次真的上傳了音檔」才會被賦值；其餘兩條路都不會
    if m4a is None:
        # manual 模式：音檔還沒產。分頁先寫，等 sweep_podcasts 補上傳時
        # 再用 ensure_podcast_link 把連結插進既有分頁。
        d = ck.detail("podcast_uploaded")
        podcast_url = d.get("url") if ck.is_done("podcast_uploaded") else None
        print(f"   {'沿用既有連結' if podcast_url else '本次略過（音檔尚未產出，之後由 sweep_podcasts 補）'}")
    elif ck.is_done("podcast_uploaded") and not args.force:
        d = ck.detail("podcast_uploaded")
        podcast_url = d["url"]
        print(f"   [checkpoint] 已上傳過，跳過（{d.get('file_id')}）")
    else:
        drive_name = args.drive_name or f"{args.tab_name}{m4a.suffix}"
        if drive_name != m4a.name:
            print(f"   本機 {m4a.name} → Drive 上命名為 {drive_name}（對齊 Drive 既有慣例）")
        up = drive.upload(m4a, podcast_folder, name=drive_name)
        podcast_url = up.get("webViewLink") or f"https://drive.google.com/file/d/{up['id']}/view"
        print(f"   {up['_action']}  {podcast_url}")
        ck.mark("podcast_uploaded", file_id=up["id"], url=podcast_url,
                name=drive_name, folder=podcast_folder)

    # ② 找／建 Google Doc
    print("② 定位 Google Doc…")
    doc = docs.find_memo_doc(ticker, cfg["drive"]["word_tree_id"])
    if doc and doc.get("_ambiguous"):
        raise PublishError(
            f"Drive 上有多份『{ticker} {MEMO_DOC_SUFFIX}』：{doc['_ambiguous']}。"
            f"不猜要寫哪一份，請先人工合併。"
        )
    if not doc:
        created = drive.svc.files().create(
            body={
                "name": f"{ticker} {MEMO_DOC_SUFFIX}",
                "mimeType": "application/vnd.google-apps.document",
                "parents": [word_folder],
            },
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        ).execute()
        doc = created
        print(f"   建立新文件『{doc['name']}』{doc['id']}")
    else:
        print(f"   沿用既有文件『{doc['name']}』{doc['id']}")

    # ③ 分頁（冪等）
    print("③ 建立分頁…")
    tab_id, created = docs.ensure_tab(doc["id"], args.tab_name)
    print(f"   {'新增' if created else '沿用既有'} 分頁 tabId={tab_id}")

    if podcast_only:
        # 只補音檔：分頁內容一個字都不動，只插入 podcast 連結
        if created:
            raise PublishError(
                f"分頁『{args.tab_name}』原本不存在，但本次沒有 memo 來源可寫入。"
                f"請改用 --json/--docx，或確認分頁名稱正確。"
            )
        res = docs.ensure_podcast_link(doc["id"], tab_id, podcast_url)
        print(f"   podcast 連結：{'已補上' if res['inserted'] else res['reason']}")
        ck.mark("memo_tab_written", doc_id=doc["id"], tab_id=tab_id,
                tab_name=args.tab_name, source="人工既有", podcast_link_added=res["inserted"])
        print(f"\n[OK] 音檔已上傳、連結已補入既有分頁。")
        print(f"     https://docs.google.com/document/d/{doc['id']}/edit?tab={tab_id}")
        return 0

    # 分頁已存在且已有內容時，絕不盲目寫入 —— 那會變成前後兩份並存。
    # （這個防護是實戰教訓：第一次跑 VST 時就把內容疊加到既有人工版本上了。）
    keep_existing = False
    if not created:
        existing = docs.tab_text(doc["id"], tab_id).strip()
        if existing:
            preview = existing[:60].replace("\n", " / ")
            if args.on_existing_tab == "abort":
                raise PublishError(
                    f"分頁『{args.tab_name}』已存在且已有 {len(existing)} 字內容"
                    f"（開頭：{preview}…）。\n"
                    f"    未指定處理方式，為避免內容重複已中止。\n"
                    f"    --on-existing-tab skip      → 保留既有，不寫入\n"
                    f"    --on-existing-tab overwrite → 清空後以本次內容取代"
                )
            if args.on_existing_tab == "skip":
                # 名稱撞車偵測。分頁名是用**曆年**季度推出來的（config 的
                # tab_name_template + 財報日期），但這些 Doc 的分頁其實是用**財政**
                # 季度命名的。非曆年財政年度的公司（SMCI/LITE/NVDA/AVGO/MU/ORCL…）
                # 兩者會對不上：今天的 FY4Q26 算出來的分頁名『2026Q2』，指到的是
                # 今年二月那份 FY2Q26。撞上還往下走的話，本季 memo 一個字都沒寫進去，
                # 卻把 podcast 連結插進別季的分頁、再回報成功讓追蹤表標成完成。
                # （2026-08-12 SMCI 實際發生，二月那份分頁被插進八月的連結。）
                d_tab, d_memo = _ymd(existing), _ymd(memo.get("release_date"))
                if d_tab and d_memo and d_tab != d_memo:
                    raise PublishError(
                        f"分頁『{args.tab_name}』裡是 {d_tab} 那一季的 memo，"
                        f"本次是 {d_memo} —— 分頁名撞車。\n"
                        f"    分頁名由曆年季度推得，但這份 Doc 是用財政季度命名。\n"
                        f"    請用正確的分頁名重跑，例如："
                        f"--tab-name '{ticker} 2026Q4'（先確認該 Doc 的既有慣例）。"
                    )
                print(f"   [skip] 既有內容 {len(existing)} 字，保留不動（開頭：{preview}…）")
                # 人工寫的分頁通常沒有 podcast 連結。補一行是純新增，不動既有內容。
                if podcast_url:
                    res = docs.ensure_podcast_link(doc["id"], tab_id, podcast_url)
                    print(f"   podcast 連結：{'已補上' if res['inserted'] else res['reason']}")
                else:
                    res = {"inserted": False}
                    print("   podcast 連結：本次無（音檔尚未產出，之後由 sweep_podcasts 補）")
                # 這個分頁若是本流程自己寫的（檢查點有紀錄且 tabId 對得上），就不要把
                # 來源改標成「人工既有」—— 那會把真實的寫入統計蓋掉。
                mine = (ck.is_done("memo_tab_written")
                        and ck.detail("memo_tab_written").get("tab_id") == tab_id)
                if not mine:
                    ck.mark("memo_tab_written", doc_id=doc["id"], tab_id=tab_id,
                            tab_name=args.tab_name, source="人工既有",
                            podcast_link_added=res["inserted"])
                # 不在這裡 return —— 早退會跳過 manifest，讓「分頁其實已完成」的這次
                # 執行留不下任何紀錄。往下走，④ 會因為檢查點／本旗標而不重寫內容。
                keep_existing = True
            else:
                # 這個 else 是必要的：skip 分支原本靠 return 早退，順帶擋住了下面的
                # clear_tab。改成往下走之後若沒有 else，skip 會直接掉進清空分頁。
                # （2026-08-12 實際踩到，CRWV 分頁被清掉 4149 字。）
                n = docs.clear_tab(doc["id"], tab_id)
                print(f"   [overwrite] 已清空既有內容 {n} 字")

    # ④ 寫內容（第 3、4 行帶 podcast 連結）
    print("④ 寫入 memo 內容…")
    if keep_existing:
        print("   [skip] 既有內容保留，不寫入")
        stat = {"skipped": "kept-existing"}
    elif ck.is_done("memo_tab_written") and not args.force:
        print(f"   [checkpoint] 已寫入過，跳過（{ck.detail('memo_tab_written').get('tab_id')}）")
        stat = {"skipped": True}
    elif docx_path:
        stat = docs.write_docx_tab(
            doc["id"], tab_id, docx_path,
            podcast_url=podcast_url,
            image_parent=word_folder,   # 損益表圖片的暫存落點，插完就刪
        )
        print(f"   {stat}")
        # 寫進去了 ≠ 內容一致。這次的缺漏正是前者成立、後者不成立卻回報成功，
        # 所以一定要讀回來逐字對過才算數。
        v = docs.verify_tab_matches(doc["id"], tab_id, docx_path)
        if v["match"]:
            print(f"   ✓ 逐字比對通過（分頁 {v['tab_chars']} 字 ⊇ memo {v['docx_chars']} 字）")
        else:
            raise PublishError(
                f"分頁內容與 {docx_path.name} 不一致，少了 {len(v['missing'])} 段：\n"
                + "\n".join(f"      · {m[:60]}" for m in v["missing"][:5])
                + "\n    分頁已寫入但不完整，請修正後以 --force --on-existing-tab overwrite 重跑。"
            )
        stat["verified"] = True
        ck.mark("memo_tab_written", doc_id=doc["id"], tab_id=tab_id,
                tab_name=args.tab_name, **stat)
    else:
        stat = docs.write_memo_tab(
            doc["id"], tab_id, memo,
            podcast_url=podcast_url, report_date=args.report_date,
            title=args.tab_name,   # 標題用分頁名，不用 memo 裡的公司全名
        )
        print(f"   {stat}")
        ck.mark("memo_tab_written", doc_id=doc["id"], tab_id=tab_id,
                tab_name=args.tab_name, **stat)

    manifest = {
        "ticker": ticker,
        "tab_name": args.tab_name,
        "doc_id": doc["id"],
        "tab_id": tab_id,
        # 音檔未產出時 up 是 None —— 直接寫 up["id"] 會 NameError，而且因為它不是
        # PublishError，不會被下面的攔截器接住，整支以 exit 1 收場：分頁明明已經
        # 寫好了，上層卻判定歸檔失敗。（2026-08-12 CRWV 實際踩到。）
        "podcast_file_id": (up or {}).get("id") or ck.detail("podcast_uploaded").get("file_id"),
        "podcast_url": podcast_url,
        "doc_url": f"https://docs.google.com/document/d/{doc['id']}/edit?tab={tab_id}",
        "tab_was_new": created,
    }
    out = Path(cfg["local"]["state_dir"]) / f"published_{ticker}_{args.tab_name.replace(' ', '_')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[OK] 完成。manifest: {out}")
    print(f"     {manifest['doc_url']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublishError as exc:
        print(f"\n[X] {exc}")
        raise SystemExit(2)
