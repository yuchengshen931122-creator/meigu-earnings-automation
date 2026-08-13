"""基建驗收 —— 在寫任何 agent 之前，先證明這四件事做得到。

    1. OAuth 憑證能用，且能在非互動模式下自動刷新
    2. 能讀到兩棵 Drive 樹
    3. 能程式化上傳一個 13MB 檔案到指定資料夾（resumable），且重跑不會產生第二份
    4. 能讀追蹤表、能回寫狀態欄
    5. Google Docs 能不能「新增分頁」—— 實測，不靠記憶下結論

預設是唯讀 / 只在自建的測試資料夾裡動作。
要真的寫追蹤表請加 --write-sheet（會在那張『團隊共用』的表最右邊新增 4 個欄位）。
"""

import _bootstrap  # noqa: F401

import argparse
import datetime as dt
import os
import tempfile
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from core.drive_client import DriveClient
from core.gauth import load_config, load_credentials
from core.sheets_client import SheetsClient

TEST_FOLDER = "_自動化驗證_可刪除"
TEST_BLOB_MB = 13


def ok(msg):
    print(f"  [OK] {msg}")


def fail(msg):
    print(f"  [X]  {msg}")


def step(n, title):
    print(f"\n=== {n}. {title} ===")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-sheet", action="store_true",
                    help="真的在共用追蹤表新增 auto_* 欄位並回寫一列（預設不做）")
    ap.add_argument("--keep", action="store_true", help="保留測試檔不丟進垃圾桶")
    args = ap.parse_args()

    cfg = load_config()
    failures = []

    step(1, "OAuth 憑證（非互動模式）")
    try:
        creds = load_credentials(interactive=False)
        ok(f"憑證可用，scopes={len(creds.scopes or [])} 個")
    except Exception as exc:
        fail(f"{exc}")
        print("\n先跑：python tools/setup_oauth.py")
        return 1

    drive = DriveClient(creds)

    step(2, "讀取兩棵 Drive 樹")
    for key, name in (("word_tree_id", "Word 樹"), ("podcast_tree_id", "Podcast 樹")):
        try:
            kids = drive.list_children(cfg["drive"][key], folders_only=True)
            ok(f"{name}：{len(kids)} 個頂層分類 → {', '.join(k['name'] for k in kids[:5])} …")
        except Exception as exc:
            fail(f"{name}: {exc}")
            failures.append(name)

    step(3, f"上傳 {TEST_BLOB_MB}MB 檔案（resumable）＋ 冪等覆蓋")
    tmp_path = None
    try:
        test_parent = drive.ensure_folder(cfg["drive"]["podcast_tree_id"], TEST_FOLDER)
        ok(f"測試資料夾 ready: {test_parent}")

        again = drive.ensure_folder(cfg["drive"]["podcast_tree_id"], TEST_FOLDER)
        if again == test_parent:
            ok("ensure_folder 冪等（重跑沒有建出第二個同名資料夾）")
        else:
            fail("ensure_folder 不冪等！重跑建出了新資料夾")
            failures.append("ensure_folder")

        fd, tmp_path = tempfile.mkstemp(suffix=".m4a")
        with os.fdopen(fd, "wb") as fh:
            fh.write(os.urandom(TEST_BLOB_MB * 1024 * 1024))

        r1 = drive.upload(tmp_path, test_parent, name="_verify_13mb.m4a")
        ok(f"第一次上傳：{r1['_action']} id={r1['id']} size={r1.get('size')}")

        r2 = drive.upload(tmp_path, test_parent, name="_verify_13mb.m4a")
        if r2["id"] == r1["id"] and r2["_action"] == "updated":
            ok("第二次上傳沿用同一個 file ID（不會出現兩份重複檔）")
        else:
            fail(f"冪等失敗：第二次 action={r2['_action']} id={r2['id']}")
            failures.append("upload idempotency")

        if not args.keep:
            drive.svc.files().update(
                fileId=r1["id"], body={"trashed": True}, supportsAllDrives=True
            ).execute()
            drive.svc.files().update(
                fileId=test_parent, body={"trashed": True}, supportsAllDrives=True
            ).execute()
            ok("測試檔與測試資料夾已移到垃圾桶（可還原，非永久刪除）")
    except Exception as exc:
        fail(f"上傳測試失敗：{exc}")
        failures.append("upload")
    finally:
        if tmp_path and Path(tmp_path).exists():
            os.unlink(tmp_path)

    step(4, "追蹤表讀寫")
    try:
        sheets = SheetsClient(creds, cfg)
        tab = sheets.resolve_tab()
        ok(f"用 gid 定位到分頁：『{tab}』")

        hdr = sheets.load_header()
        ok(f"表頭定位成功（第 {sheets._header_row} 列）：{sorted(hdr)}")

        tasks = sheets.load_tasks(year=dt.date.today().year)
        actionable = [t for t in tasks if t.actionable]
        skipped = [t for t in tasks if t.skip_reason]
        ok(f"讀到 {len(tasks)} 列；可排程 {len(actionable)}；需跳過 {len(skipped)}")
        if skipped:
            print("       跳過原因："
                  + ", ".join(f"{t.ticker}({t.skip_reason})" for t in skipped[:10]))
        renamed = [t for t in tasks if t.ticker != t.raw_ticker.strip().upper()]
        if renamed:
            ok("代號正規化："
               + ", ".join(f"{t.raw_ticker}→{t.ticker}" for t in renamed[:10]))

        if args.write_sheet:
            sheets.ensure_status_columns()
            ok("auto_status / auto_checked_at / auto_retry / auto_error 欄位已就緒")
            if actionable:
                sheets.write_status(actionable[0], status="verify_ok", error="")
                ok(f"已回寫第 {actionable[0].row} 列（{actionable[0].ticker}）")
        else:
            print("       （唯讀模式，未寫入。要驗證回寫請加 --write-sheet）")
    except Exception as exc:
        fail(f"追蹤表失敗：{exc}")
        failures.append("sheets")

    step(5, "Google Docs 能不能新增分頁？（實測，不靠記憶）")
    try:
        docs = build("docs", "v1", credentials=creds, cache_discovery=False)
        probe = drive.svc.files().create(
            body={"name": "_verify_tabs_probe", "mimeType": "application/vnd.google-apps.document"},
            fields="id",
        ).execute()
        doc = docs.documents().get(documentId=probe["id"], includeTabsContent=True).execute()
        has_tabs = "tabs" in doc
        ok(f"Docs API 讀得到 tabs 欄位：{has_tabs}")
        try:
            # 請求名是 addDocumentTab（discovery document 確認過），不是 createTab
            resp = docs.documents().batchUpdate(
                documentId=probe["id"],
                body={"requests": [{"addDocumentTab": {"tabProperties": {"title": "驗證分頁"}}}]},
            ).execute()
            tab_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
            ok(f"addDocumentTab 可用，新分頁 tabId={tab_id}")

            docs.documents().batchUpdate(
                documentId=probe["id"],
                body={"requests": [{"insertText": {
                    "location": {"tabId": tab_id, "index": 1},
                    "text": "寫入指定分頁測試\n",
                }}]},
            ).execute()
            after = docs.documents().get(
                documentId=probe["id"], includeTabsContent=False
            ).execute()
            titles = [t.get("tabProperties", {}).get("title") for t in after.get("tabs", [])]
            ok(f"寫入指定分頁成功；文件現有分頁：{titles}")
        except HttpError as exc:
            fail(f"分頁操作失敗（{exc.resp.status}）：{exc}")
            failures.append("docs tabs")
        drive.svc.files().update(
            fileId=probe["id"], body={"trashed": True}
        ).execute()
    except Exception as exc:
        fail(f"Docs 測試失敗：{exc}")
        failures.append("docs")

    print("\n" + "=" * 50)
    if failures:
        print(f"驗收未通過，失敗項目：{failures}")
        return 1
    print("驗收全數通過。基建可用，可以進入下一階段（就緒偵測 + 排程器）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
