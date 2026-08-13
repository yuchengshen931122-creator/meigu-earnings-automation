"""把做完的 memo 與 podcast 推到 LINE 群組。

一檔一季**只推一次、只發一個請求**：
  訊息①（文字）標題 + 日期 + 重點摘要 + memo 分頁連結 + podcast 連結
  訊息②（語音）podcast 本體，可直接在群組裡播

兩則打包在同一個 push 裡，是因為計費看的是**接收人數 × 請求次數** ——
分兩次呼叫會讓同一檔扣掉兩倍額度。

**時機**：memo 與 podcast 都齊了才推。
  - dispatcher 跑完且音檔已產出 → 當下推
  - 音檔還沒好（memo 先行）→ 不推，等 sweep_podcasts 補上傳時再推
這樣每檔只會有一則通知，不會先來一則「memo 好了」再來一則「podcast 好了」。

用法：
    python tools/notify_line.py --ticker LITE --quarter 2026Q4 \
        --docx "...\\MEMO\\2Q26\\LITE 2026Q4.docx" \
        --m4a  "...\\PODCAST\\2Q26\\LITE 2026Q4.m4a"
    加 --dry-run 只組訊息並印出來，不送、也不公開任何檔案。
"""

import _bootstrap  # noqa: F401

import argparse
import json
from pathlib import Path

from core import checkpoint as cp
from core import docx_to_docs, line_client, public_link
from core.drive_client import DriveClient
from core.gauth import load_config, load_credentials
from core.line_client import LineClient, LineError, LineNotConfigured

STAGE = "line_notified"


def _tldr(blocks: list[dict]) -> list[str]:
    """memo 的重點摘要 = .docx 裡 'List Number' 樣式那幾段（1. 2. 3. …）。

    章節內文用的是段落自帶的 numPr（● ○ ■），型別不同，不會混進來。
    """
    return [docx_to_docs._plain(b).strip()
            for b in blocks
            if b["kind"] == "p" and (b.get("list") or ("", 0))[0] == "number"]


def compose(docx_path: Path, *, doc_url: str | None, podcast_url: str | None) -> str:
    blocks = docx_to_docs.parse(docx_path)
    paras = [b for b in blocks if b["kind"] == "p"]
    title = docx_to_docs._plain(paras[0]).strip() if paras else docx_path.stem
    date = docx_to_docs._plain(paras[1]).strip() if len(paras) > 1 else ""

    lines = [f"📊 {title}", date, ""]
    tldr = _tldr(blocks)
    if tldr:
        lines.append("【重點摘要】")
        lines += [f"{i}. {t}" for i, t in enumerate(tldr, 1)]
        lines.append("")
    if doc_url:
        lines += ["📄 完整 memo（含表格與損益表）", doc_url, ""]
    if podcast_url:
        # 語音訊息之外**再附一次連結**：LINE 抓不到音檔時不會回報錯誤給我們，
        # 群組裡只會出現一則播不動的語音。附上連結，那種情況還有得救。
        lines += ["🎧 Podcast", podcast_url]
    return "\n".join(lines).strip()


def find_doc_url(cfg: dict, ticker: str, tab_name: str) -> str | None:
    """從 publish_memo 留下的 manifest 拿分頁網址。"""
    p = (Path(cfg["local"]["state_dir"])
         / f"published_{ticker}_{tab_name.replace(' ', '_')}.json")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("doc_url")
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--quarter", required=True, help="例如 2026Q4")
    ap.add_argument("--docx", dest="docx_path", required=True)
    ap.add_argument("--m4a", dest="m4a_path", default=None,
                    help="沒給就不推 —— memo 與 podcast 齊了才通知，避免同一檔通知兩次")
    ap.add_argument("--doc-url", default=None, help="預設從 publish 的 manifest 讀")
    ap.add_argument("--tab-name", default=None, help="預設為 '{ticker} {quarter}'")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略檢查點，重推一次")
    args = ap.parse_args()

    cfg = load_config()
    ticker = args.ticker.strip().upper()
    quarter = args.quarter.strip()
    tab_name = args.tab_name or f"{ticker} {quarter}"
    state_dir = cfg["local"]["state_dir"]
    docx_path = Path(args.docx_path)

    if not docx_path.exists():
        print(f"[X] 找不到 memo：{docx_path}")
        return 2

    ck = cp.load(state_dir, ticker, quarter)
    if ck.is_done(STAGE) and not args.force and not args.dry_run:
        print(f"[skip] {ticker} {quarter} 已推播過（--force 可重推）")
        return 0

    doc_url = args.doc_url or find_doc_url(cfg, ticker, tab_name)
    if not doc_url:
        print(f"   [!] 找不到 {tab_name} 的分頁網址，訊息不會帶 memo 連結")

    # dry-run 刻意排在憑證與檔案檢查之前：預覽訊息長什麼樣不該需要 token，
    # 也不該需要音檔已經產好 —— 那正是還沒設定完的人最想先看到的東西。
    if args.dry_run:
        body = compose(docx_path, doc_url=doc_url,
                       podcast_url="（音檔公開連結：實際推播時才產生）")
        print(f"--- dry-run：{ticker} {quarter} 會送出的文字（{len(body)} 字）---")
        print(body)
        print("\n--- 同一個 push 還會帶一則語音訊息（podcast 本體）---")
        print("未公開任何檔案、未送出任何訊息、未消耗任何額度。")
        return 0

    if not args.m4a_path:
        print(f"[skip] {ticker} {quarter} podcast 尚未產出，等音檔到齊再一次推播")
        return 0
    m4a = Path(args.m4a_path)
    if not m4a.exists():
        print(f"[X] 找不到音檔：{m4a}")
        return 2

    # 設定不全時「跳過」而不是「失敗」—— 通知沒設好不該讓整條產出線判定失敗
    try:
        settings = line_client.load_settings()
    except LineNotConfigured as exc:
        print(f"[skip] LINE 尚未設定：{exc}")
        return 0

    client = LineClient(settings["channel_access_token"])
    group_id = settings["group_id"]

    # 額度預檢：群組推播是「人數 × 次數」計費，用完會 429
    try:
        members = client.group_member_count(group_id)
        left = client.remaining()
        if left is not None:
            print(f"  群組 {members} 人；本月剩餘額度 {left} 則（本次會用掉 {members} 則）")
            if left < members:
                print(f"[X] 額度不足（剩 {left}、需要 {members}），本次不推播")
                ck.mark(STAGE, done=False, error=f"quota {left} < {members}")
                return 2
    except LineError as exc:
        print(f"   [!] 額度查詢失敗，仍照常推播：{exc}")
        members = None

    # 公開音檔 → 這一步會讓檔案在網路上可存取，帳本記在 state/line_public_files.json
    drive = DriveClient(load_credentials(interactive=False))
    print(f"  公開音檔 {m4a.name}…")
    info = public_link.publish(drive, m4a, name=f"{ticker} {quarter}.m4a",
                               ticker=ticker, quarter=quarter, state_dir=state_dir)
    print(f"   {info['size_mb']} MB、{info['duration_ms'] // 1000} 秒 → {info['url']}")

    check = public_link.verify(info["url"])
    if not check["ok"]:
        print(f"[X] 公開網址驗證失敗：{check} —— 送出去會是一則播不動的語音，中止")
        public_link.revoke(drive, state_dir, f"{ticker} {quarter}")
        return 2
    print(f"   ✓ 直連可用（{check['content_type']}、{check['bytes']} bytes）")

    body = compose(docx_path, doc_url=doc_url, podcast_url=info["url"])
    messages = [
        line_client.text_message(body, tail=doc_url or ""),
        line_client.audio_message(info["url"], info["duration_ms"]),
    ]

    print(f"  推播中（1 個請求、{len(messages)} 則訊息）…")
    client.push(group_id, messages)
    ck.mark(STAGE, group_id=group_id, members=members, chars=len(body),
            audio_file_id=info["file_id"], audio_url=info["url"],
            doc_url=doc_url)
    print(f"\n[OK] 已推播 {ticker} {quarter} 到 LINE 群組。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LineError as exc:
        print(f"\n[X] {exc}")
        raise SystemExit(2)
