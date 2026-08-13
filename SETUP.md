# SETUP —— 把這套系統換成「你的」環境

這份文件逐項列出所有**與原作者環境綁定**的東西:它原本是什麼、
為什麼會綁定、你要怎麼換成自己的。照順序做完,整條線就是你的了。

> 執行順序建議:§7(SEC UA,兩分鐘)→ §1–§3(Google 側)→ §4(claude)
> → §8(路徑)→ 手動試跑 → §5、§6(podcast 與 LINE,皆可後補)→ §9(排程)。

---

## §1 Google OAuth client(必要)

**原作環境**:作者自建的 Google Cloud 專案 + 桌面應用程式 OAuth client。

**你要做的**(詳細步驟見 [automation/README.md](automation/README.md) 的「一次性設定」):

1. https://console.cloud.google.com/ 建專案,啟用 **Drive API、Sheets API、Docs API**
   (要用 Gmail 告警再加 Gmail API)。
2. OAuth 同意畫面 → 外部 → 測試使用者加入你自己的 Gmail。
3. 建立「桌面應用程式」OAuth 用戶端,下載 JSON 改名 `client_secret.json`,放到
   secrets 目錄(見下)。
4. `python automation/tools/setup_oauth.py` 走一次瀏覽器授權。
5. `python automation/tools/verify_infra.py` 驗收(唯讀,安全)。

**secrets 目錄在哪**:`config.json` 的 `local.secrets_dir` 留空 =
`%LOCALAPPDATA%\meigu-automation\secrets`。若你的工作排程器讀不到那裡
(作者踩過 AppData 沙箱 ACL 重導向的坑),就填一個 OneDrive **之外**的絕對路徑。
**不要放 OneDrive / Dropbox 同步範圍內** —— refresh token 等同長期密碼。

**7 天地雷(強烈建議處理)**:OAuth 同意畫面停在「測試」狀態時,refresh token
**7 天就會被 Google 作廢**。到 Cloud Console 把發布狀態改成「正式版」
(不需送審,授權時多點一次「進階 → 前往」),地雷即拆除。

## §2 追蹤表 Google Sheet(必要)

**原作環境**:一張 5 人共用的追蹤表,一季一個分頁,表頭在第 4 列,
欄名是中文(`ticker`、`日期`、`時間(台灣)`、`Podcast`、`報告`⋯⋯)。

**你要做的**:

1. 建一張自己的 Google Sheet,一列一檔股票。最少要有:代號、日期、時間、
   Podcast 勾選框、報告勾選框、產業 這幾欄。
2. 把 `spreadsheet_id`(網址中 `/d/` 後那串)與分頁 `gid` 填進
   `config.json` 的 `sheet` 段。
3. 欄名不必跟原作一樣 —— `sheet.columns` 就是「程式概念 → 你的表頭文字」
   的對映表,把右邊改成你的欄名即可。程式靠表頭文字定位,不吃固定欄序。
4. 程式會在表的最右邊自動新增 4 個 `auto_*` 狀態欄(`auto_status` 等)。

**時區慣例(重要)**:「日期」「時間」欄填的是**你 `config.timezone` 時區的
法說會開始時刻**(原作是 `Asia/Taipei`)。填美東日期會讓盤後公司的整列
提早一天,原作者實際踩過這個坑。

## §3 Google Drive 資料夾樹(必要)

**原作環境**:兩棵**別人擁有**的共用樹(作者僅是協作者)——
`美股財報 Word`(長期單一樹,每 ticker 一份 Google Doc)與
`{季度} 美股財報podcast`(每季新建)。這也是整個系統用 OAuth 而非
service account 的原因。

**你要做的**:

1. 在自己的 Drive 建兩個資料夾(自己擁有,一切更單純):
   - Word 樹:底下依產業分類,每分類底下每 ticker 一個資料夾
   - Podcast 樹:底下依產業分類放 `.m4a`
2. 把三個 folder ID(網址最後那串)填進 `config.json` 的 `drive` 段:
   `grandparent_id`(兩棵樹的上層,自動換季掃描用)、`word_tree_id`、
   `podcast_tree_id`。
3. `python automation/tools/build_drive_map.py` 產生 ticker → 資料夾對照表
   (每天的排程也會自動重跑)。

分類名不必照原作 —— 樹是掃出來的,不是寫死的。兩棵樹分類名有出入時的
對映規則在 `automation/core/tree_match.py`(內含原作團隊的別名表,可改可刪)。

## §4 claude CLI + skill + agents(必要)

**原作環境**:claude CLI 已登入,`earnings-memo-auto` skill 與兩個 agent
已就位。

**你要做的**:

1. 安裝並登入 claude CLI(桌面 App 的登入與 CLI **是分開的**):
   互動式終端機跑 `claude`,執行 `/login`。
2. 把 skill 複製到使用者 skill 目錄:
   ```
   xcopy /e /i skills\earnings-memo-auto %USERPROFILE%\.claude\skills\earnings-memo-auto
   ```
3. 兩個 agent 定義在 repo 的 `.claude/agents/`,**從 repo 根目錄執行**
   claude 相關工具就找得到(dispatcher 已處理 cwd),不用另外安裝。
4. (選用,無人值守加固)`claude setup-token` 產生長效 token 後
   `python automation/tools/store_claude_token.py` 存入 secrets,
   主登入失效時 auth_monitor 會自動切換備援。

**額度提醒**:每檔 memo 是一次 20 分鐘上下的 agent 執行,吃你的 Claude
訂閱額度;`run.max_parallel` 開越大燒越快。

## §5 NotebookLM podcast(選用)

**原作環境**:`notebooklm-py[headless]` 已用作者帳號完成 master-token 登入,
`config.json` 的 `run.podcast_mode` 是 `"cli"`。

**你要做的**(要 podcast 才需要):

1. `pip install "notebooklm-py[headless]"`
2. 一次性登入:`notebooklm login --master-token --account 你的@gmail.com`
   (master token 讓 session 過期後能無頭重鑄,無人值守必備)
3. 之後 auth_monitor 會自動保溫與修復;只有改 Google 密碼才需重新登入。

**不想要 podcast**:`run.podcast_mode` 設 `"manual"` —— 排程只產 memo,
音檔你自己丟進 `PODCAST/{季度}/`(或永遠不丟),`sweep` 會自動接手上傳。
memo 分頁照樣會寫,只是少 podcast 連結。

## §6 LINE 推播(選用)

**原作環境**:作者的 LINE 官方帳號推到一個 4 人工作群組;告警走作者自己的
1:1 私訊。channel token / groupId / userId 全部放在 secrets 目錄的
`line.json`,**不在 repo 裡**。

**你要做的**(要推播才需要):

1. 建 LINE 官方帳號 + Messaging API channel。
2. `python automation/tools/setup_line.py` —— 它會一步步引導:
   填 token、把 bot 拉進群組、用 webhook.site 抓 groupId、寫入 `line.json`。
3. **關掉 OA 的四項自動回應**(Chat / Greeting / Webhooks / Auto-response),
   否則 bot 會在群組裡插嘴 —— 步驟見 automation/README.md「一定要關掉
   OA 的自動回覆」一節。
4. 額度:免費方案每月 200 則,**按接收人數計**(推一次 4 人群組= 4 則)。
   `notify_line.py` 送出前會自查餘額,不夠就跳過不送。

**不設定 LINE**:memo / podcast / 歸檔全部照常,只是沒有群組推播;
告警退回 Gmail(要 gmail.send scope)或 Gmail 草稿。

**語音訊息的代價(要知道再開)**:LINE 語音訊息要求音檔放在**公開** HTTPS
網址上且長期有效。系統只會公開你自己 My Drive 底下專用資料夾裡的檔案,
`python automation/tools/line_public.py list / revoke` 可隨時稽核與收回。

## §7 SEC User-Agent(必要,兩分鐘)

**原作環境**:`automation/core/net.py` 的 `SEC_UA` 帶作者 email。

**你要做的**:把 `SEC_UA = "meigu-automation you@example.com"` 改成
**你的真實 email**。SEC 明文要求可識別、可聯絡的 UA,匿名 UA 會被封 IP
—— 無人值守下被封是災難。速率限制(≤10 req/s)程式已寫死,不用動。

## §8 本機路徑與 config(必要)

1. `copy automation\config.example.json automation\config.json`
   (`config.json` 已在 .gitignore,你的 ID 不會被 commit)。
2. `local.memo_dir` / `local.podcast_dir` / `local.state_dir` 填你 clone
   位置底下的 `MEMO`、`PODCAST`、`automation\state` 絕對路徑。
3. 文件與 skill 裡的 `{專案根目錄}` 一律指「你 clone 下來的 repo 根目錄」。
4. `timezone` 填你的時區(影響追蹤表日期時間的解讀,見 §2)。

**路徑有中文/非 ASCII?** `run/*.bat` 用 `%~dp0..` 自動定位,一般沒事;
若 cmd 處理不了你的路徑,改用 8.3 短路徑(`dir /x` 查),.bat 內有註解。

## §9 Windows 工作排程(必要)

**原作環境**:7 個排程任務,全部 `LogonType=Interactive` + `StartWhenAvailable`。

**你要做的**:在工作排程器逐一建立(程式 = `automation\run\` 對應的 .bat):

| 任務 | 頻率 | .bat | 作用 |
|---|---|---|---|
| Dates | 每日 08:00 | dates.bat | 核對法說會日期 + 重建 drive map + 換季偵測 |
| Dispatch | 每 15 分 | dispatch.bat | 就緒偵測 → 派工產 memo/podcast → 歸檔 |
| Sweep | 每 20 分 | sweep.bat | 上傳落地音檔、補連結、推 LINE |
| Backfill | 每 30 分 | backfill.bat | 補做失敗的 podcast |
| AuthMon | 每 10 分 | authmon.bat | 授權檢查 + 自動修復 |
| Watchdog | 每 1 小時 | watchdog.bat | 健康檢查 + 告警 |
| Dashboard | 每 5 分 | serve.bat | 看板(http://127.0.0.1:8787/)存活監督 |

每個任務勾:「不論使用者是否登入均執行」**不要**勾(claude / notebooklm 需要
互動式 session)、「錯過排程後盡快啟動」要勾、「喚醒電腦執行」要勾、
ExecutionTimeLimit 給 Dispatch 至少 5 小時。

**機器前提**:24/7 接電、AC 永不休眠、保持登入。

**非 Windows**:.bat 換成 cron + shell script 即可,Python 端大多可攜,
但路徑處理與 `%USERPROFILE%` 假設需要自行修,未在 macOS/Linux 實測過。

## §10 語言與團隊格式慣例(想改才動)

這套系統的產出格式綁定原作團隊的慣例:

| 慣例 | 定義在哪 | 想改就 |
|---|---|---|
| memo 是繁體中文、固定章節結構 | `skills/earnings-memo-auto/SKILL.md` + `references/schema.md` | 改 skill 的格式規則 |
| .docx 版面(字型、縮排、IS 截圖) | `skills/earnings-memo-auto/scripts/build_memo.py` | 改該腳本 |
| Google Doc 分頁版式(標題/日期/podcast 連結/摘要) | `automation/core/docs_client.py` 的 `_render()` | 改渲染函式 |
| Doc 命名 `{TICKER} 財報`、分頁 `{TICKER} {YYYY}Q{n}` | `config.json` 的 `memo_output` | 改 template |
| podcast 語言/長度(繁中 deep-dive short) | `automation/tools/make_podcast.py` 的 CLI 參數 | 改參數 |
| 代號正規化特例(`CITI→C` 等) | `automation/core/sheets_client.py` | 改對映表 |

## 常見試跑指令

```bash
# 全部不碰網路的單元測試
python automation/tests/test_pure.py

# 日期更新 dry-run(離線)
python automation/tools/update_dates.py --days 45 --offline

# 排程主迴圈 dry-run
python automation/tools/dispatcher.py --once --offline --dry-run

# 進度總覽(7 階段檢查點)
python automation/tools/status.py

# 授權五盞燈
python automation/tools/auth_monitor.py --dry-run
```
