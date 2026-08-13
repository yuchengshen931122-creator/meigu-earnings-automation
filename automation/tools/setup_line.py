"""LINE 推播的設定精靈與自我檢查。

    python tools/setup_line.py                        # 印步驟 + 檢查目前狀態
    python tools/setup_line.py --check                # 只檢查（token/群組/額度）
    python tools/setup_line.py --send-test            # 測試訊息，私訊自己
    python tools/setup_line.py --send-test --to-group # 測試訊息，送群組

--send-test 預設送給設定檔裡的 user_id（你自己），不是群組 —— 群組裡有其他真人，
驗證設定不該吵到他們。要驗真正的推播對象才加 --to-group。
兩者都刻意做成獨立旗標，不會在 --check 時順便送出。
"""

import _bootstrap  # noqa: F401

import argparse
import json

from core import line_client
from core.line_client import LineClient, LineError, LineNotConfigured

GUIDE = """
LINE 群組推播 —— 設定步驟（約 15 分鐘，只需做一次）

前提：LINE Notify 已於 2025-03-31 終止服務，只能走「LINE 官方帳號 + Messaging API」。

1) 建立 LINE 官方帳號（OA）
   ※ 2026-08 實測：**已經不能直接在 Developers Console 建 Messaging API channel 了**。
     Console 的「Create a new channel → Messaging API」只會顯示一段說明，要你先去建 OA。
     順序是「先有 OA → 啟用 Messaging API → channel 才會自己出現」。
   - 到 https://manager.line.biz/ 建立官方帳號
   - **帳號名稱就是群組成員會看到的名字**，想清楚再填
   - 類別、email 照實填即可

2) 在 OA Manager 啟用 Messaging API（channel 在這一步才誕生）
   - https://manager.line.biz/ → 選剛建的帳號
   - 右上「設定」→ 左側「Messaging API」→「啟用 Messaging API」
   - 會要你選 provider —— **選既有的那個**，不要另開新的
     （user ID 是跟著 provider 走的，換 provider 等於換一組 ID）
   - 完成後回 Developers Console，該 provider 底下就會出現這個 channel，
     而且它**有** Messaging API 分頁

3) 取得 channel access token
   - Developers Console → 該 channel → Messaging API 分頁
   - 最下方「Channel access token (long-lived)」→ Issue
   - 複製那串 token（很長，之後看不到第二次，先貼在記事本）

4) 把 bot 加進群組
   - OA Manager → 設定 → 帳號設定 → **「允許加入群組／多人聊天室」開啟**
     （預設是關的，不開的話邀請會直接失敗）
   - 用手機加該官方帳號為好友，再到目標群組邀請它進群

5) 取得 groupId（這一步需要 webhook，但不必自己架伺服器）
   - 開 https://webhook.site/ ，它會給你一個唯一網址，複製它
   - 回到 developers console 的 Messaging API 分頁：
       Webhook URL 貼上那個網址 → Update → 把 "Use webhook" 打開
   - 在群組裡隨便發一句話
   - 回到 webhook.site，最新那筆請求的 JSON 裡會有：
         "source": { "type": "group", "groupId": "Cxxxxxxxx..." }
     複製那串 groupId
   - 抓到之後可以把 "Use webhook" 關掉，webhook.site 的網址也可以丟掉

6) 寫進設定檔（**不要放進專案資料夾**，那裡會同步到 OneDrive）
   路徑：__PATH__
   內容：
       {
         "channel_access_token": "第 3 步那串",
         "group_id": "第 5 步那串"
       }

7) 驗證（token 一填好就能做，不必等 groupId）
       python tools/setup_line.py --check
       python tools/setup_line.py --send-test            # 私訊你自己
       python tools/setup_line.py --send-test --to-group # 確認群組本身

計費要知道的事：
  - 推播是按「**接收人數**」計，推一次到 10 人的群組扣 10 則
  - 一次 push 帶多則訊息只算一次（本流程的文字+語音就是打包成一個請求送的）
  - 免費方案每月 200 則。群組人數 × 每季檔數若會超過，要考慮升級方案
    （中用量 NT$800/月 3,000 則、高用量 NT$1,200/月 6,000 則）
  - `--check` 會顯示目前剩餘額度
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只檢查現況，不印步驟")
    ap.add_argument("--send-test", action="store_true",
                    help="送一則測試訊息。預設**私訊你自己**，不吵群組")
    ap.add_argument("--to-group", action="store_true",
                    help="搭配 --send-test：改送到群組（群組裡的人會看到）")
    args = ap.parse_args()

    if not args.check and not args.send_test:
        # 用 replace 不用 format —— 指引裡有 JSON 範例，大括號會被 format 當成欄位
        print(GUIDE.replace("__PATH__", str(line_client.secrets_path())))

    print("=" * 60)
    print("目前狀態")
    print("=" * 60)
    settings = line_client.read_settings()
    path = line_client.secrets_path()
    if not settings:
        print(f"  [X] 還沒有設定檔：{path}")
        return 1

    # 逐項回報，不要一句「尚未設定」把已經填好的部分也蓋掉 ——
    # 這支的用途就是讓人知道「還差哪一項」。
    print(f"  設定檔：{path}")
    for key, label, needed in [
        ("channel_id", "Channel ID", False),
        ("channel_secret", "Channel secret", False),
        ("user_id", "你的 user ID（測試用）", False),
        ("channel_access_token", "Channel access token", True),
        ("group_id", "群組 groupId", True),
    ]:
        v = settings.get(key) or ""
        mark = "[OK]" if v else ("[X] " if needed else "[--]")
        shown = (v[:8] + "…") if len(v) > 12 else (v or ("必填，尚缺" if needed else "未填（非必要）"))
        print(f"    {mark} {label:24s} {shown}")

    token = settings.get("channel_access_token")
    group_id = settings.get("group_id")
    if not token:
        print("\n  還差 channel access token —— 沒有它什麼都做不了。")
        print("    → 步驟 3：Messaging API 分頁最下方 Issue 一組 long-lived token")
        return 1

    client = LineClient(token)
    try:
        info = client.bot_info()
        print(f"\n  [OK] token 有效 — 官方帳號『{info.get('displayName')}』"
              f"（basicId {info.get('basicId')}）")
    except LineError as exc:
        print(f"\n  [X] token 無效：{exc}")
        return 1

    count = None
    if group_id:
        try:
            summary = client.group_summary(group_id)
            count = client.group_member_count(group_id)
            print(f"  [OK] 群組『{summary.get('groupName')}』，成員 {count} 人")
            print(f"       每推一次會用掉 {count} 則額度")
        except LineError as exc:
            print(f"  [X] 讀不到群組（bot 可能還沒被邀請進去，或 groupId 不對）：{exc}")
            return 1
    else:
        print("  [--] 還沒有 groupId（步驟 4、5）—— 但 token 已可用，"
              "可以先 --send-test 私訊自己驗證")

    try:
        q = client.quota()
        if q.get("type") == "limited":
            used = client.consumption().get("totalUsage", 0)
            left = int(q["value"]) - int(used)
            tail = f"（約還能推 {left // count} 檔）" if count else ""
            print(f"  [OK] 本月額度 {q['value']} 則，已用 {used}，剩 {left}{tail}")
        else:
            print(f"  [OK] 方案無則數上限（{q.get('type')}）")
    except LineError as exc:
        print(f"  [!] 額度查詢失敗：{exc}")

    if args.send_test:
        # 預設**私訊你自己**，不是群組 —— 群組裡有真人，驗證設定不需要吵到他們。
        # 要驗真正的推播對象再加 --to-group。
        to_group = args.to_group
        target = group_id if to_group else (settings.get("user_id") or group_id)
        if not target:
            print("\n[X] 沒有可送的對象（user_id 與 group_id 都是空的）")
            return 1
        where = "群組" if target == group_id else "你本人（私訊）"
        print(f"\n送出測試訊息 → {where}…")
        client.push(target, [line_client.text_message(
            "✅ 美股財報自動化：LINE 推播設定完成。\n"
            "之後每檔 memo 與 podcast 做完，會自動推一則"
            "（重點摘要 + memo 連結 + 可播放的 podcast）。"
        )])
        print(f"  已送出，請到{where}確認。")
        if not to_group and group_id:
            print("  要驗證群組本身：python tools/setup_line.py --send-test --to-group")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LineError as exc:
        print(f"\n[X] {exc}")
        raise SystemExit(2)
