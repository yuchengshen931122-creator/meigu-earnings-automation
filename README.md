# 美股財報自動化(meigu-earnings-automation)

無人值守的美股財報處理管線:從「法說會日期確認」到「繁體中文財報 memo +
AI podcast 產出、歸檔到 Google Drive/Docs、回寫追蹤表、推播到 LINE 群組」,
全程自動。

一檔股票的完整旅程:

```
Nasdaq 行事曆 + IR 官網 agent 核對法說會日期
        ↓  (寫回 Google Sheet 追蹤表)
就緒偵測:SEC 新聞稿 ✓ + 法說會逐字稿 ✓
        ↓  (每 15 分鐘輪詢,就緒即開工,最多 3 檔並行)
claude CLI + earnings-memo skill → 繁中 memo(.docx + 結構化 JSON)
        ↓
notebooklm CLI → 中文 deep-dive podcast(.m4a)
        ↓
publish_memo.py → podcast 上傳 Drive、Google Doc 開新分頁寫入 memo
        ↓
追蹤表打勾 + LINE 群組推播(文字摘要 + 可直接播放的語音訊息)
```

支撐這條線的還有:7 個 Windows 排程任務、7 階段檢查點(斷電可續跑)、
授權即時監控與自動修復(Google OAuth / NotebookLM / claude CLI / LINE)、
LINE 私訊告警、以及一個本機即時看板。

**設計文件**:[automation/README.md](automation/README.md) 詳細記錄了每一個
設計決策與踩過的坑(為什麼只認 folder ID 不認名稱、為什麼逐字稿不等到就不開工、
OAuth 測試模式的 7 天地雷⋯⋯),強烈建議先讀。

## 這個 repo 的內容

| 路徑 | 內容 |
|---|---|
| `automation/` | 整條管線的程式:`core/`(API 封裝)、`tools/`(排程進入點)、`run/`(排程 .bat)、`tests/` |
| `automation/config.example.json` | 設定範本 → 複製成 `config.json` 後填入你自己的值 |
| `skills/earnings-memo-auto/` | 產 memo 的 Claude skill(嚴格的輸出格式定義)→ 安裝到 `~/.claude/skills/` |
| `.claude/agents/` | 兩個具名 agent:財報日期查證員、memo 產出員 |
| `MEMO/`、`PODCAST/` | 產出落地目錄(空,由管線填入) |

## 快速開始

```
git clone <this-repo>
cd meigu-earnings-automation
pip install -r automation/requirements.txt
copy automation\config.example.json automation\config.json
```

然後 —— **重要** —— 這套系統原本跑在作者自己的環境裡,有一批東西
**你一定要換成自己的**才跑得起來。完整清單與每一項的解法在
**[SETUP.md](SETUP.md)**,摘要如下:

> **原團隊成員**:追蹤表、Drive 樹、OAuth client 都**直接重用共用的那一份**,
> 不用自建 —— 跟作者拿 `config.colleagues.json`(私下傳,不在本 repo),
> 照 SETUP 各節開頭的「原團隊成員」框走即可。下表是給外部使用者的。

| # | 項目 | 原作環境 | 你需要做的 |
|---|---|---|---|
| 1 | Google OAuth client | 作者自建的 Cloud 專案 | 自建專案啟用 Drive/Sheets/Docs API,下載 `client_secret.json`(見 SETUP §1);**團隊成員**:共用作者那份 |
| 2 | 追蹤表 Google Sheet | 團隊共用表,特定欄位名 | 建自己的表,`spreadsheet_id` 與欄名對映填進 `config.json`(SETUP §2);**團隊成員**:重用共用表 |
| 3 | Drive 資料夾樹 | 別人擁有、作者是協作者 | 換成你自己的資料夾 ID(自己擁有更單純)(SETUP §3);**團隊成員**:重用共用樹 |
| 4 | claude CLI + skill | 已登入、skill 已安裝 | `npm i -g @anthropic-ai/claude-code`、登入、把 `skills/` 複製到 `~/.claude/skills/`(SETUP §4) |
| 5 | NotebookLM(podcast) | notebooklm-py 已授權 | `pip install notebooklm-py[headless]` + 登入(SETUP §5);不要 podcast 也能跑 |
| 6 | LINE 推播(選用) | 作者的官方帳號 + 群組 | 建自己的 LINE OA + Messaging API channel(SETUP §6);不設定只是不推播 |
| 7 | SEC User-Agent | 作者的 email | **必改** `automation/core/net.py` 的 `SEC_UA`,SEC 要求真實聯絡方式 (SETUP §7) |
| 8 | 本機路徑 | 作者的資料夾 | `config.json` 的 `local.*` 填你的路徑;文件中 `{專案根目錄}` = 你 clone 下來的位置(SETUP §8) |
| 9 | Windows 排程 | 作者機器上的 7 個排程 | 用 `automation/run/*.bat` 自行註冊排程(SETUP §9);非 Windows 需自行移植 |
| 10 | 語言與格式慣例 | 繁中 memo、團隊範本 | 想改語言/格式 → 改 skill 與 `core/docs_client.py`(SETUP §10) |

## 誠實的邊界

- **平台**:只在 Windows 11 + Python 3.13 實測過。核心 Python 大多可攜,
  但排程(.bat + 工作排程器)、路徑處理都是 Windows 假設。
- **無人值守的前提**:機器 24/7 接電、不休眠、保持登入(美股法說會多在
  台灣時間凌晨)。
- **費用與額度**:claude CLI 吃你的 Claude 訂閱額度;LINE 免費方案每月
  200 則(按接收人數計);Google API 在正常用量下免費。
- **`skip_permissions`**:全自動模式會用 `--dangerously-skip-permissions`
  呼叫 claude CLI(無人值守時停在權限詢問等於整條線掛掉)。風險與緩解見
  `config.example.json` 內的註解,建議先手動觀察幾檔再打開。

## License

[MIT](LICENSE)。本 repo 由私人工作環境去識別化匯出:文件裡的
`{專案根目錄}`、`you@example.com`、`owner@example.com` 都是佔位符,
歷史敘述中的日期與實測數據保留原樣供參考。
