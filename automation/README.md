# 美股財報自動化 — 階段一：Google API 基建

在寫任何 agent 之前，先證明四件事做得到。做不到的話，後面全部白工。

## 為什麼要先做這個

現有的 Drive 連接器（MCP）能力不足以完成歸檔：

| 需求 | MCP 連接器 | 原生 API |
|---|---|---|
| 上傳 10–13MB podcast | **做不到**（只能吃 base64，13MB → ~17MB 字串塞不進工具呼叫） | resumable upload |
| 回寫追蹤表 TRUE/FALSE | **沒有寫入能力** | Sheets API |
| 在既有 Google Doc 新增分頁 | 沒有 | Docs API（能力待實測，見下） |
| 找既有分類資料夾 | 可讀 | 可讀 |

## 一次性設定

### 步驟 1：建立 OAuth client

1. 開 https://console.cloud.google.com/ → 建立一個專案（名稱隨意，例如 `meigu-automation`）
2. 「API 和服務」→「已啟用的 API」→ 啟用這三個：
   - Google Drive API
   - Google Sheets API
   - Google Docs API
3. 「OAuth 同意畫面」→ 選 **外部** → 填應用程式名稱與你的信箱 → 儲存
   - 「測試使用者」加入 `you@example.com`
   - 保持在「測試」狀態即可，不需要送審
4. 「憑證」→「建立憑證」→「OAuth 用戶端 ID」→ 應用程式類型選 **桌面應用程式**
5. 下載 JSON，改名為 `client_secret.json`，放到：

   ```
   %LOCALAPPDATA%\meigu-automation\secrets\client_secret.json
   ```

> **為什麼放在 OneDrive 外面**：這個資料夾在 OneDrive 底下，會同步到雲端。
> refresh token 等同長期密碼，不該被同步。

> **為什麼不能用 service account**：Drive 上的目標資料夾由 `owner@example.com`
> 擁有，你是協作者。service account 是一個獨立身分，不在那棵樹的共享名單裡，
> 拿不到存取權。必須用你本人的 OAuth。

### 步驟 2：安裝套件並授權

```bash
pip install -r "{專案根目錄}\automation\requirements.txt"
```

```bash
python "{專案根目錄}\automation\tools\setup_oauth.py"
```

會開瀏覽器，用**你自己的** Google 帳號登入並同意。

### 步驟 3：驗收

```bash
python "{專案根目錄}\automation\tools\verify_infra.py"
```

預設是唯讀＋只在自建的測試資料夾裡動作（測完丟垃圾桶，可還原）。

要一併驗證「回寫追蹤表」再加旗標 —— **注意這會在團隊共用的那張表最右邊新增 4 個欄位**：

```bash
python "{專案根目錄}\automation\tools\verify_infra.py" --write-sheet
```

### 步驟 4：產生 Drive 對照表 → 端到端試跑一檔

```bash
python "{專案根目錄}\automation\tools\build_drive_map.py"
```

**每季開季都要重跑一次。**

然後拿已經跑完的 VST 2Q26 當白老鼠，先 dry-run：

```bash
python "{專案根目錄}\automation\tools\publish_memo.py" --ticker VST --tab-name "VST 2026Q2" --json "{專案根目錄}\MEMO\_work\vst_2q26.json" --m4a "{專案根目錄}\PODCAST\2Q26\VST 2Q26.m4a" --report-date 2026/08/07 --dry-run
```

確認目的地正確後拿掉 `--dry-run` 真的跑一次，去 Drive 上肉眼檢查分頁長相。

---

## 設計決策（以及為什麼）

### 只認 folder ID，不認資料夾名稱

實際掃描 Drive 的結果：

- podcast 樹**每季新建一棵**，命名毫無規則：
  `2026Q1 美股財報 Podcast` / `2026Q2 美股財報podcast` / `2026Q2 美股財報`
- 分類名每季會漂：`金融業`→`金融`、`AI硬體`↔`AI 硬體`（空格）、
  `實體AI/機器人`→`實體AI/機器人/醫療`
- 兩棵樹的分類集合根本不同：podcast 樹 12 個頂層分類、Word 樹 9 個；
  podcast 樹的 `生技醫療`/`地熱`/`貴金屬` 在 Word 樹是次層資料夾

用名稱比對已經害人工歸檔錯過一次（`MEMO/2Q26/_Drive投遞清單.md` 有記錄）。
自動化只會把這個錯放大成每季 100 次。

### 追蹤表欄位靠表頭文字定位，不用固定索引

那張表有 4 個分頁（4 個季度），欄位排列會漂 —— 某個分頁少一欄，
導致「產業」欄裡裝的是人名。固定索引必然讀錯。

### 代號正規化

`CITI` → `C`（`CITI` 不是有效美股代號）、`C (Citi)` → `C`、`Unity` → `U`。
沒有日期的 7 檔（IREN、CRDO、AVAV、ORCL、SMTC、MU、KEYS）標 `no_date` 跳過，不猜。

### 冪等

- `ensure_folder`：同名資料夾存在就沿用，不建第二個
- `upload`：同名檔存在就**更新同一個 file ID**，不建第二份
  （Drive 允許同名並存，不做這件事重跑一次就有兩份）
- 冪等 key：`{TICKER} {季度}`

### Google Docs 新增分頁：已確認可行

查 Docs v1 discovery document（非憑記憶）確認：

| 能力 | API | 狀態 |
|---|---|---|
| 新增分頁 | `addDocumentTab`（回傳新 `tabId`） | 支援 |
| 把寫入導向指定分頁 | `Location.tabId` / `EndOfSegmentLocation.tabId` | 支援 |
| 讀出既有分頁清單 | `documents.get(includeTabsContent=True)` | 支援 |
| 改分頁標題／順序 | `updateDocumentTabProperties` | 支援 |

所以歸檔慣例（每個 ticker 一份 `{TICKER} 財報`，每季新增分頁，不產生新 .docx）
可以完全程式化，不需要備案。

### 順序依賴：podcast 必須先上傳

分頁內容的第 3、4 行就是 podcast 連結（照 `NVDA 財報` 現況）：

```
NVDA 2025Q3
20251120
podcast link：
https://drive.google.com/file/d/…
重點摘要
…
```

所以 run 內順序是寫死的：**產 podcast → 上傳 Drive 取得連結 → 才寫 memo 分頁**。
`tools/publish_memo.py` 就是照這個順序實作的。

### earnings-memo skill 的能力邊界（已查證）

skill **沒有**任何 Drive／Docs 能力。SKILL.md 第 747–749 行自述：批次執行
「end at save-to-`MEMO\` plus the renamed `.m4a` sitting in the Downloads folder」。
`scripts/` 底下 5 支腳本（build_memo / is_shot / pdf_page_to_image / insert_is /
log_run）沒有一行 Google API 程式碼。

| 步驟 | 誰做 |
|---|---|
| 抓資料、產結構化 JSON、產 .docx、產 .m4a | earnings-memo skill |
| 上傳 podcast → 取連結 | **本專案** `publish_memo.py` |
| 找／建 `{TICKER} 財報` Doc、新增分頁、寫入內容 | **本專案** `publish_memo.py` |
| 回寫追蹤表狀態 | **本專案** `sheets_client.py` |

> **對 skill 的一個要求**：目前 memo 的結構化 JSON 是臨時檔
> （`MEMO/_work/vst_2q26.json`），命名不固定。自動化需要它在固定路徑，
> 因為分頁內容是從 JSON 渲染的，不是從 .docx 反解。

---

## 已知風險

| 風險 | 影響 | 緩解 |
|---|---|---|
| Drive 樹不是你的（`owner@example.com`） | 對方改共享設定 → 歸檔全斷 | 每次 run 前檢查 `canAddChildren`，失敗即告警 |
| Google OAuth token 過期 | 無人時段靜默停擺 | `MeiguAuthMon` 每 10 分鐘檢查＋refresh；修不動立刻推 LINE（見「授權的即時監控」一節） |
| NotebookLM cookie 過期 | podcast 產不出來 | `MeiguAuthMon` 每 10 分鐘 `auth refresh` 保溫＋檢查（原 MeiguNlmWarm 已併入） |
| 本機關機／休眠 | 整條線停止 | 法說會多落在台灣時間凌晨 4:30–6:00，機器必須 24/7 不休眠 |
| 外國發行人沒有 10-Q | ASML、SAP、STM、NOK、NVO、TEVA、ARM、SE、GRAB、MELI、CPNG、NU、CSIQ 等的 SEC 取數步驟會失敗 | 就緒判準已改為「新聞稿＋逐字稿」，不把 10-Q 當阻塞條件 |

---

## 階段二：日期更新、就緒偵測、排程器（已完成，可離線試跑）

```
[Windows 工作排程器]
   ├─ 每日 08:00  → MeiguDates      verify_dates.py    掃 Nasdaq + 官網 agent 核對日期
   ├─ 每 15 分鐘  → MeiguDispatch   dispatcher.py      讀表 → 就緒偵測 → 派工
   ├─ 每 20 分鐘  → MeiguSweep      sweep_podcasts.py  上傳落地的音檔 → 補分頁連結 → 推 LINE
   ├─ 每 30 分鐘  → MeiguBackfill   backfill_podcasts.py  補做缺席的 podcast
   ├─ 每 10 分鐘  → MeiguAuthMon    auth_monitor.py    授權快迴圈：檢查＋自動修復＋看板燈號
   ├─ 每 1 小時   → MeiguWatchdog   watchdog.py        健康檢查 + 告警
   └─ 每 5 分鐘   → MeiguDashboard  serve_dashboard.py 看板存活監督
                        ↓
        就緒偵測（新聞稿 ✓ + 逐字稿 ✓）
                        ↓ dispatchable
        每檔各自獨立、就緒即開工（同時最多 run.max_parallel 檔，預設 3）：
        claude CLI 產 memo → notebooklm CLI 產 podcast
        → publish_memo.py 上傳＋開分頁 → 回寫表 → 推 LINE
```

**為什麼改成併行（2026-08-12）**：原本一檔做完才做下一檔。實測 08-12 那輪
CRWV / SMCI / LITE / ASTS 序列跑了 1 小時 45 分，而每檔的瓶頸都是在等外部服務
（agent 約 20 分、NotebookLM 約 7 分），CPU 幾乎沒動 —— 排隊純屬浪費。
用執行緒（每檔本來就是一連串阻塞的 subprocess，GIL 不是瓶頸），
共用的 sheets / journal / stdout 各上一把鎖，log 每行加上 `[TICKER]` 前綴。
上限存在的理由是 claude 用量額度全帳號共用，開越多消耗越快；`max_parallel: 1`
即回到序列行為。一檔炸掉不影響其他檔；配額用完時，尚未開始的直接跳過留到下一輪。

追蹤表 = 唯一狀態源（`auto_status`：waiting / ready / running / done / failed）。
任何一步掛掉就停在 failed + 原因，下一輪自動重試；斷電關機都能接續。

### 資料源實測結果（2026-08-11，對真實資料驗證）

| 用途 | 來源 | 結果 |
|---|---|---|
| ticker→CIK | `sec.gov/files/company_tickers.json` | 10,387 筆，全數命中 |
| 新聞稿（美國） | SEC 8-K item 2.02 | **高**。VST 08-07、RKLB 08-10 日期精準 |
| 新聞稿（無 8-K） | SEC 10-Q / 10-K | **高**。OKLO 不發 8-K 2.02，只交 10-Q |
| 新聞稿（外國） | SEC 6-K 三層判讀 | **低～中**。6-K 無 item 代碼：① reportDate 標成期末（中）② 檔名關鍵字（低）③ 抓內文比對 INDEX TO EXHIBITS（低）。NBIS 08-12 實測只有③命中 —— 它一年發 28 份 6-K，檔名全是 `tm2622968d1_6k.htm` 這種代編號 |
| 逐字稿 | `fool.com/quote/{ex}/{ticker}/` | **高**但覆蓋不完整。唯一能解析出季度＋發佈日的來源 |
| 逐字稿 | Google News RSS（全網） | **中**。0.5 秒、免登入，涵蓋 Investing／Yahoo／SA／MarketBeat |
| 逐字稿 | Yahoo headline RSS（綁代號） | **中**。回真實文章網址（Google News 回的是轉址殼） |
| 逐字稿 | investing.com／Seeking Alpha 直連 | ✗ JS 渲染／HTTP 403，純 HTTP 進不去 |
| 逐字稿 | Nasdaq API、discountingcashflows | ✗ 404 / 403 |
| 財報日期 | Nasdaq calendar API | 可用。給日期 + 盤前/盤後，**給不出精確時刻** |

### 逐字稿為什麼「來源不限」（2026-08-12 改）

原本這一層只查 Motley Fool，覆蓋率不夠。實測：CRWV／SMCI／LITE 三檔 08-11 盤後發財報，
逐字稿當晚就在 Yahoo Finance、Investing.com、MarketBeat 上線，Fool 一篇都沒有 ——
三檔在本層全卡成 `waiting`，白等了一整晚。

改成 Fool + 全網搜尋（`core/transcripts.py`）之後，同一時間點三檔都判得出來。
判讀規則是**標題判讀**，precision 優先（誤判會讓 memo 引到別家公司的電話會議）：
標題要有 transcript／call highlights／prepared remarks 這類**強訊號**字樣、
要指到本公司（代號獨立字詞或 SEC 公司名關鍵詞）、不能是預告稿、發佈日不早於法說會前一天。
只寫 "earnings call" 不算 —— 「…Ahead Of Earnings Call」是預告不是逐字稿。
這些規則有單元測試釘住（`tests/test_pure.py`，用真實抓到的標題）。

Google News 給的是 `news.google.com` 轉址殼（內容靠 JS 還原），只能當線索給 agent，
**不會**餵進 NotebookLM（`core/sources.py` 會濾掉）。同強度下真實網址排前面。

### 逐字稿逃生門：已改為「不等到逐字稿絕不開工」（2026-08-13 定案）

`transcript_soft_cutoff_hours` 現在是 **null ＝ 逃生門關閉**：新聞稿確認了也不派工，
一定等到逐字稿被偵測到才開始做。

使用者定案的理由：品質優先 —— 逾時先做的 memo，Q&A 整段是用新聞報導與管理層
公開發言重建的，不是真問答（COHR/CSCO/CBRS 三檔實證，缺漏註記都標了）。
大盤股逐字稿多在會後 2–12 小時上線（台灣時間當天中午～傍晚），代價是產出晚幾小時。

關閉逃生門的配套（不能無聲空等）：

- 某檔等逐字稿**滿 24 小時**的那一輪，dispatcher 推一次 LINE
  （alert key `transcript_overdue`，跨過門檻才發、天然去重）——
  冷門股可能永遠沒有免費逐字稿，要不要放行是人的決定
- 等待中的檔在看板「等待資料就緒」泳道可見，dispatch log 每輪印出已等時數
- 要對個別檔放行：把 `transcript_soft_cutoff_hours` 暫時設回數字（小時），
  跑完再改回 null；或人工在桌面 App 產出後丟進 MEMO/PODCAST 資料夾，
  sweep 會自動接手歸檔

舊行為（歷史紀錄）：0.75 小時軟性逾時 → `ready_partial` 派工、由 agent 自行找
逐字稿。2026-08-12 曾靠它讓 CRWV/SMCI/LITE 當晚開工；如今偵測已是全網來源，
熱門股通常等不了多久，逃生門的價值只剩冷門股 —— 而那正是重建 Q&A 品質最差的一群。

另一個相關設計不變：**新聞稿一旦確認就永不放棄**。數字都公開了，memo 一定做得出來；
give_up 只適用於「連財報都還沒發」。這也讓系統停機數天後回來仍會補跑。

### 為什麼「時間不準」影響有限

Nasdaq 只給盤前/盤後，而且財報**發布**時間 ≠ 法說會**開始**時間（常差 30–60 分鐘）。
但在本架構下影響有限：**派工由就緒偵測把關，不是由時間把關。**
時間只決定何時開始輪詢，早晚半小時不影響最終產出。
所以日期要準，分鐘不必準。

`update_dates.py` 因此只更新日期欄，時間欄僅在**原本空白**時填推估值，
人手填的精確時間不覆蓋。

### 試跑

```bash
python "{專案根目錄}\automation\tools\update_dates.py" --days 45 --offline
```

```bash
python "{專案根目錄}\automation\tools\dispatcher.py" --once --offline --queue state/manual_queue.json --now 2026-08-11T12:00 --dry-run
```

### 財報日期核對（verify_dates.py）：為什麼官網那段要用 agent

用純 HTTP 抓 15 個 IR 頁面實測：3 檔表上無網址、4 檔連線失敗、8 檔抓到頁面，
其中**只有 1 檔給出可用日期**（而且是舊的），NVDA 抓到「September 09, 2021」的垃圾。
成功率約 12%。

換 headless Chrome 再測：VST 的 DOM 有 183KB，但連 "Earnings" 字樣都沒有
——內容在二次 XHR 之後才載入。MU 直接抓不到。

也確認 EDGAR 全文檢索對這件事無效：AMAT / WMT / DELL / PANW 在財報前
**都沒有**為了預告日期而發 8-K（total=0）。那只是 IR 網站上的新聞稿。

⇒ 110 個異質 IR 網站，regex 不可能通吃。所以分兩層：

| 層 | 負責 | 方式 |
|---|---|---|
| Nasdaq calendar | 多數 ticker 的日期 + 盤前/盤後 | 確定性程式 |
| agent（`claude -p`，每檔一個） | 官網核對、Nasdaq 漏掉的補查、精確法說會時刻 | LLM 讀頁面 |

三方比對（表上現值 / Nasdaq / 官網）：
- **一致** → 寫回
- **不一致** → 採官網（公司自述優先）並**直接寫回**，同時在報表列出被推翻的項目供事後覆核
  （2026-08-12 改：原本卡人工確認，但實測 Nasdaq 常慢半拍，卡著只會讓該檔永遠沒日期）
- 只有一邊有 → 寫回並註明來源
- 兩邊都無 → 標記

另外，追蹤表的 `con-call 官網連結` 欄有 **42% 不是純文字網址**，
而是 Ctrl+K 貼的超連結（顯示成「GM Investor Relations」這種標題文字）。
`values.get` 只拿得到顯示文字，URL 會掉 —— 已用 `sheets_client.load_hyperlinks()`
從 `cellData.hyperlink` / `textFormatRuns` 取回。

### 兩棵樹的分類對映（core/tree_match.py）

podcast 樹每季重建，本季只長出 141 個 ticker 落點，而 Word 樹有 337 個
——追蹤清單 112 檔裡有 **44 檔在 podcast 樹沒有位置**（含已做完的 VST/OKLO/RKLB）。

所以歸檔時要能從 Word 樹的分類推出 podcast 樹的對應分類。兩棵樹的名稱漂移：

| Word 樹 | Podcast 樹 |
|---|---|
| `AI 硬體`（有空格） | `AI硬體` |
| `實體AI/機器人/醫療` | `實體AI/機器人` |
| `記憶體/硬體` | `記憶體/存儲` |
| `能源儲存及潔淨能源` | `能源儲存與潔淨能源` |
| `衣` / `食` | `衣著` / `飲食` |
| `FinTech` | `Fintech` |
| `散熱`、`水資源`、`半導體/工業設計` | 無 → 退回上層或補建 |

作法：正規化（去空格、統一全形斜線、casefold）→ 葉名比對 → 別名表 → 退回上層 →
仍無則在對應頂層底下補建。抽測 16 檔全數對映成功。

### 分頁重複寫入的防護（實戰教訓）

第一次跑 VST 2Q26 時，`ensure_tab` 正確地「沿用既有分頁」，但接著仍把內容寫了進去
——該分頁本來就有人工版本，結果變成前後兩份並存（8718 → 16355 字）。
已用 `deleteContentRange` 精準還原，並補上防護：

`publish_memo.py --on-existing-tab {abort,skip,overwrite}`，**預設 `abort`**。
分頁已存在且有內容就中止，不動既有資料。要覆寫必須明講（會先 `clear_tab` 清空再寫，
不是疊加）。

### 待確認：memo 分頁的格式不一致

現有 `VST 財報 / VST 2026Q2` 分頁（人工寫的）與本工具的輸出格式**不同**：

| | 既有人工版 | 本工具輸出（照 NVDA 分頁的樣式） |
|---|---|---|
| 標題 | `維斯特拉 Vistra (VST) 2Q26` | `Vistra (VST) 2Q26` |
| 條列 | `1. 2. 3.` 數字 | `•` 項目符號 |
| podcast 連結 | **沒有** | 第 3–4 行 |

`NVDA 財報` 的分頁有 podcast 連結、VST 的沒有 —— 團隊格式本身不一致。
需要指定哪一種是標準，`core/docs_client.py` 的 `_render()` 再照做。

### 兩個尚未解除的登入阻斷

**① Google OAuth（歸檔用）** —— `%LOCALAPPDATA%\meigu-automation\secrets\client_secret.json`
尚未放置。啟用 API 只是第一步，還要建立「桌面應用程式」OAuth 用戶端、下載 JSON、
再跑 `setup_oauth.py`。

**② `claude` CLI 未登入（派工用）** —— 實測：

```
$ claude -p "回答一個字：hi"
rc=1
Not logged in · Please run /login
```

目前這個工作階段跑在 Claude 桌面 App 裡，跟 npm 裝的獨立 CLI **憑證是分開的**。
排程器的 `--execute` 與 `verify_dates.py` 的 agent 核對都靠這支 CLI，
沒登入就整條線不會動。請在終端機開一次互動式 `claude` 並執行 `/login`。

**附帶問題（已處理）**：`~/.claude/skills` 原本沒有 `earnings-memo`
——它只存在於桌面 App 的 plugin 沙箱路徑（含 session GUID，不穩定）。
已複製一份到 `~/.claude/skills/earnings-memo/`，獨立 CLI 才看得到。
代價是變成兩份副本，plugin 端更新時要記得同步。

### headless 呼叫 claude CLI 的兩個地雷

**① 參數順序**：`--allowedTools` 是變參數旗標，放在 `-p` 之後會把 prompt 吃掉，
結果是 rc=1 且完全沒有輸出（很難查）。

```
✗ claude -p "prompt" --allowedTools WebFetch,WebSearch      → rc=1，無輸出
✗ claude -p --allowedTools WebFetch,WebSearch "prompt"      → rc=1，無輸出
✓ claude --allowedTools WebFetch,WebSearch -p "prompt"      → rc=0
```

**② headless 下沒有權限就什麼都做不了**：不給白名單時 agent 會回
「網路工具（WebFetch / WebSearch / curl）目前都需要您在 Claude Code 的…」。

日期核對 agent 只需要唯讀網路工具，所以用**窄白名單** `WebFetch,WebSearch`，
而不是 `--dangerously-skip-permissions`。產 memo 的 agent 需要的權限大得多
（Bash / Write / 瀏覽器），那才是 `run.skip_permissions` 要處理的範圍。

### 命名慣例（2026-08-12 統一）

**單一標籤：`{TICKER} {YYYY}Q{n}`，季度用財政季度。** 本機檔名、Drive 檔名、
Doc 分頁三邊同一個字串：

| | 例（SMCI，財政年度 6/30 結束） |
|---|---|
| Doc 分頁 | `SMCI 2026Q4` |
| Drive 音檔 | `SMCI 2026Q4.m4a` |
| 本機音檔 | `PODCAST/2Q26/SMCI 2026Q4.m4a` |
| 本機 docx | `MEMO/2Q26/SMCI 2026Q4.docx` |
| 本機 JSON | `MEMO/_work/smci_2026q4.json` |

**資料夾維持曆年季度**（`2Q26`）。它是「這一波財報季」的桶子 ——
改成財政會把同一個晚上發的 CRWV（曆年 Q2）和 SMCI（財政 Q4）拆到兩個資料夾。

財政季度由 `core/quarters.fiscal_quarter_for()` 推導，財政年度結束月份取自
SEC submissions 的 `fiscalYearEnd`（`Edgar.fiscal_year_end()`），不手維護清單。

檔名不由 agent 決定 —— `dispatcher` 在 prompt 裡寫死兩個輸出路徑，
agent 若還是自己取名（曾產出 `SMCI 4Q26FY.docx`），收尾時會搬回正規檔名。

改動之前的舊檔是 `{TICKER} {n}Q{yy}`（`VST 2Q26.docx`）。2Q26 的既有檔案已全部
改名，`sweep_podcasts` 仍會同時尋找舊格式，回頭補更早的季度不會抓不到。

## 兩個具名 agent（.claude/agents/）

| Agent | 何時派工 | 工具 | 為什麼要用 agent |
|---|---|---|---|
| `earnings-date-verifier` | `verify_dates.py`，Nasdaq 打底後 | `WebFetch, WebSearch` | IR 頁面 JS 渲染，regex 實測只有 12% 成功率；LLM 能像人一樣讀懂 |
| `earnings-memo-runner` | `dispatcher.py --execute`，就緒偵測通過後 | `Skill, Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch` | 產 memo 本來就需要判斷與研究 |

**歸檔刻意不做成 agent** —— 上傳檔案、開分頁、回寫表格是純機械操作，
`publish_memo.py` 這種程式比 LLM 可靠、便宜、而且結果可重現。

呼叫方式（`--agent` 要在 `-p` 之前，且必須在專案目錄下執行才找得到定義）：

```bash
claude --agent earnings-date-verifier --allowedTools WebFetch,WebSearch -p "查證標的：WMT ..."
```

實測輸出（agent 找到官方頁面、抓原文、換算時區、附出處）：

```json
{"found": true, "date": "2026-08-20", "time_et": "08:00", "session": "BMO",
 "source_url": "https://corporate.walmart.com/news/events/fy2027-q2-earnings-release",
 "quote": "a live conference call with the investment community will begin at 7 a.m. CT",
 "confidence": "high"}
```

規則寫在 agent 定義裡，派工程式只給該次的任務資料 —— 這樣改規則不必動程式。

## 檢查點（core/checkpoint.py + tools/status.py）

整條線有 7 個階段，跨越網路查詢、LLM 產出、Drive 上傳。沒有檢查點時，
任何一步失敗或中斷，重跑就會把已完成的昂貴步驟再做一次
（重新產 memo、重新產 podcast、重傳 11MB），而且可能造成重複資料。

狀態存在 `state/checkpoints/{TICKER}_{QUARTER}.json`，一檔一季一份：

| # | 階段 | 誰做 |
|---|---|---|
| 1 | 財報日期已確認 | `verify_dates.py`（Nasdaq + 官網 agent） |
| 2 | 資料已就緒 | `dispatcher.py`（SEC 新聞稿 + 逐字稿全網搜尋） |
| 3 | memo 已產出 | earnings-memo skill → JSON + .docx |
| 4 | podcast 已產出 | NotebookLM → .m4a |
| 5 | podcast 已上傳 | `publish_memo.py` → Drive，產出 url |
| 6 | memo 分頁已寫入 | `publish_memo.py` → Google Doc 新分頁 |
| 7 | 追蹤表已回寫 | TRUE/TRUE + auto_status |

**為什麼不只靠追蹤表的 `auto_status`**：表上一列只能表達「整體到哪」，
表達不了 7 個階段各自的狀態；表是 5 個人共用的；而且表存不了
`file_id` / `tab_id` 這些續跑必需的產物。
追蹤表仍是給人看的單一真相，檢查點是給程式續跑用的細節。

```bash
python "{專案根目錄}\automation\tools\status.py"
```

```
  OKLO   2026Q2   ●●●●○○○ 4/7  待辦：podcast 已上傳
  RKLB   2026Q2   ●●●●○○○ 4/7  待辦：podcast 已上傳
  VST    2026Q2   ●●●●●○○ 5/7  待辦：memo 分頁已寫入

卡關分布：
  podcast 已上傳        2 檔
  memo 分頁已寫入         1 檔
```

其他用法：

```bash
python "{專案根目錄}\automation\tools\status.py" --detail VST
```

```bash
python "{專案根目錄}\automation\tools\status.py" --stuck --quarter 2026Q2
```

從某階段重跑（會**連同其後的階段一起清掉** —— 前面重做了，後面的產物就不該還算數）：

```bash
python "{專案根目錄}\automation\tools\status.py" --reset VST 2026Q2 --from memo_built
```

`dispatcher.py` 會跳過已整條線完成的 ticker（不再打 SEC / Fool），
`publish_memo.py` 會跳過已完成的上傳與分頁寫入（`--force` 可忽略檢查點）。

### 全自動的最後一道開關

`config.json` 的 `run.skip_permissions` 預設 **false**。設為 true 時，dispatcher
會用 `--dangerously-skip-permissions` 呼叫 claude CLI ——這是「不需要任何 approval」
的必要條件（無人值守時停在權限詢問等於整條線掛掉），但它會讓該次執行跳過所有
工具權限確認。建議先用 false 手動觀察幾檔，確認行為符合預期再打開。

## 階段三：做完即時推到 LINE 群組

memo 與 podcast 都齊了之後，自動推一則訊息到指定的 LINE 群組：
文字（標題＋日期＋重點摘要＋memo 分頁連結＋podcast 連結）＋一則可直接播放的語音訊息。

```bash
python tools/setup_line.py              # 設定步驟 + 現況檢查
python tools/setup_line.py --check      # 只檢查 token/群組/剩餘額度
python tools/setup_line.py --send-test  # 送一則測試訊息（群組裡的人看得到）

python tools/notify_line.py --ticker LITE --quarter 2026Q4 \
    --docx "...\MEMO\2Q26\LITE 2026Q4.docx" \
    --m4a  "...\PODCAST\2Q26\LITE 2026Q4.m4a" --dry-run
```

設定檔放 `%SECRET_DIR%\line.json`（與 Google token 同一個專案外目錄，
不放 config.json —— 那個檔在 OneDrive 底下，token 會被同步到雲端）。

### 為什麼是 Messaging API，不是 LINE Notify

LINE Notify 已於 **2025-03-31 終止服務**，2025-04-01 起 API 與 token 發行全部停用。
要推到群組只剩「LINE 官方帳號 + Messaging API」一條路，需要 channel access token
與群組的 groupId（groupId 只能從 webhook 事件取得，`setup_line.py` 的步驟 4 用
webhook.site 取，不必自己架伺服器）。

### memo 為什麼只給連結

**Messaging API 沒有「檔案」訊息類型** —— 只有 text / sticker / image / video /
audio / location / imagemap / template / flex。所以 .docx 傳不進群組。
訊息帶的是重點摘要（.docx 裡 `List Number` 那幾段）＋ Google Doc 分頁連結，
分頁本身已經與 memo 逐字一致（見 `core/docx_to_docs.py`）。

### 額度：按「接收人數」計，不是按「送幾則」

推一次到 N 人的群組會扣 **N 則**額度。免費方案每月 200 則。
但**一次 push 帶多個訊息物件只算一次**，所以文字與語音一定要打包在同一個請求裡送 ——
分兩次呼叫會變成 2N。`notify_line.py` 送出前會先查剩餘額度，不夠就不送並記錯誤，
不會等 429 才發現。群組人數 × 每季檔數若接近 200，要考慮升級方案。

### 語音訊息的代價：音檔會被放到公開網路上

LINE 的 audio message 只吃**公開的 HTTPS 直連網址**，而且 LINE 伺服器與每一位按下
播放的成員都會去抓那個網址 —— 所以音檔必須**長期保持公開**，送完不能收回。
這是使用者在「只給 Drive 連結」與「可播放語音訊息」之間明確選擇後者的結果。

範圍已盡量縮小、且可稽核：

- 只放在**本帳號自己的 My Drive** 底下的「美股 podcast 公開連結（LINE 用）」，
  不碰 共用樹擁有者 擁有的那兩棵共用樹
- 每一份記在 `state/line_public_files.json`
- `python tools/line_public.py list` 看目前有哪些是公開的；
  `revoke "<TICKER> <季度>"` 或 `--all` 收回（收回後群組裡那則語音就播不出來）

送出前會先自己抓一次那個網址確認 `Content-Type: audio/*`，不通過就中止並撤銷 ——
LINE 抓不到內容時**不會回報錯誤給我們**，群組裡只會出現一則播不動的語音。
訊息本文另外附一次 podcast 連結，就是為了這種情況還有得救。

### 觸發點與冪等

一檔一季只推一次，由檢查點的 `line_notified` 階段擋住重複：

- `dispatcher.py` 跑完且音檔已產出 → 當下推
- 音檔還沒好（memo 先行）→ 不推，等 `sweep_podcasts.py` 補上傳時才推

所以不會「memo 好了」一則、「podcast 好了」再一則。
`line_notified` **不是** `FINAL_STAGE` —— 推播是通知不是產出，
LINE 沒設定好不該讓整條線被判成沒跑完，既有的 checkpoint 也不必遷移。

### 分頁順序：最新的一律在最上面

`addDocumentTab` 不帶 `index` 時，新分頁會接在**最後面**。這些 Doc 一檔累積十幾季，
最新一季堆在最底等於每次都要捲到底 —— 而且使用者原本的人工慣例本來就是新→舊
（VST：2026Q2 → 2026Q1 → 2025Q4 → …），自動建的分頁反而把它弄反了。

所以 `ensure_tab()` 固定帶 `tabProperties.index = 0`。
API 保證：在指定 index 插入時，其後所有分頁的 index 自動 +1。

搬既有分頁用 `DocsClient.move_tab(doc_id, tab_id, index=0)`（走
`updateDocumentTabProperties`）。2026-08-12 已用它把先前接在最後面的
LITE 2026Q4、CRWV 2026Q2、SMCI 2026Q4 三個分頁歸位。

**NVDA 不要照字面排序**：它的分頁是 2027Q1 → 2026Q4FY → 2025Q3 → 2026Q2 → 2026Q1，
標籤混用了財政與曆年兩種命名（README 前面的「分頁命名不一致」就是講這件事），
按標籤排序會排錯。那些是人工建的，先不要動。

### 一定要關掉 OA 的自動回覆（預設是開的）

新建的 LINE 官方帳號**預設會自動回覆**。群組裡有人講話，bot 就會插嘴：

    感謝您的訊息！
    很抱歉，本帳號無法個別回覆用戶的訊息。
    敬請期待我們下次發送的內容喔

這個 bot 的職責只有「財報與 podcast 產出完成時推一則」，不該對群組對話有任何反應。
到 OA Manager →「Response settings」把這四個全部關掉：

    Chat                    Off
    Greeting message        Off   ← bot 被加好友／被邀進群組時會發問候語
    Webhooks                Off   ← 只在抓 groupId 那幾分鐘開，抓完就關
    Auto-response messages  Off   ← 就是上面那句自動回覆

關掉之後，能對群組發話的路徑只剩本流程的 push API。
Developers Console 的 Messaging API 分頁會同步顯示 Auto-reply / Greeting 為 Disabled，
可以拿來交叉確認。

註：自動回應訊息屬於 LINE 的免付費訊息，不吃每月額度 —— 所以先前那些自動回覆
沒有消耗掉推播額度（實測：關掉前後都是已用 5 則）。

## 無人值守的三個補丁（2026-08-12 稽核）

針對「接下來所有財報，從頭到 LINE 推播全自動」做了一次逐段查證。
排程、憑證、Drive、追蹤表都是通的，但查出兩個**會靜默吃掉產出**的洞，
以及一批查得到卻不會通知的告警。

### 洞 ①：podcast 失敗後永遠不會補做

`dispatcher` 刻意把 podcast 當成不阻塞的步驟 —— 產不出來也照樣歸檔 memo，
然後 `finish(ST_DONE, report_done=True)`。但候選過濾會跳過「報告已完成」的列，
所以那一檔再也不會被碰；而 `sweep_podcasts` 只上傳**已經在磁碟上**的音檔，
它不產生任何東西。

⇒ NotebookLM 授權過期那段期間跑掉的每一檔，都會**永久**少掉 podcast，
連帶 LINE 永遠不會推（兩條推播路徑都要求音檔存在），而且沒有任何東西變紅：
memo 有了、表上打勾了、排程 rc=0。

補法是 `tools/backfill_podcasts.py`（排程 `MeiguBackfill`，每 30 分鐘一檔）：
掃檢查點找出「`memo_built` 完成、`podcast_built` 未完成、磁碟上也沒有音檔」的檔，
沿用檢查點 `readiness` 階段存下來的新聞稿／逐字稿網址重新產出。
它**只產音檔**；上傳、補分頁連結、勾表、推 LINE 全部照舊由下一輪 `sweep` 處理。

給獨立排程而不是塞進 `sweep.bat`：一集要 15–40 分鐘，共用 sweep 的時段會讓
`MultipleInstances=IgnoreNew` 擋掉後續輪次，把它自己該做的上傳與推播一起卡住。

### 洞 ②：NotebookLM 授權過期沒有任何人知道

watchdog 原本查 Google OAuth / claude CLI / Drive / 流程健康四項，
唯獨不查 podcast 產製那條線 —— 而那正是當天實際壞掉的東西。
已加 `check_notebooklm()`（`notebooklm auth check --test --json`）與
`check_line()`（token 有效性 + 剩餘額度，不足兩檔的份就告警）。

另外加了 `MeiguNlmWarm`（每 15 分鐘 `notebooklm auth refresh`）保溫
（2026-08-12 晚間已併入 `MeiguAuthMon`，原排程停用 —— 見「授權的即時監控」一節）。
**保溫救不回已經過期的 session** —— 那要人工在有畫面的環境跑一次 `notebooklm login`。
保溫的作用是登入之後不要再過期。

### 告警：主路徑改成 LINE 私訊

`alerts.on` 原本只列 3 種，等於 `claude_cli_down`、`drive_access_lost`、
`stuck_readiness` 這三項查得到也會被 `Alerter.send()` 直接丟掉。已全部補上。

管道本身也是壞的：本帳號的 OAuth 沒有 `gmail.send`，Gmail 只能走降級路徑
——叫 claude CLI 的 Gmail MCP 建一封**草稿**，而草稿不會推播。
所以無人值守時出事根本不會有人知道。

改成三條獨立管道，任一條成功就算送到：

    LINE 私訊（會推播，主路徑）→ Gmail API（要 gmail.send）→ Gmail 草稿（聊勝於無）

推到 `line.json` 的 `user_id`（使用者自己的 1:1），**不是**財報那個群組 ——
系統壞掉是維運的事，不該洗版到 5 個人的工作群組。
剩餘額度低於 `QUOTA_FLOOR`（40）時不再用 LINE 送告警，把額度留給財報推播。

### 順帶修掉的定時炸彈：sweep 的季度寫死

`sweep.bat` 原本 `set QUARTER=2Q26`。季度一換，它會繼續掃那個空的舊資料夾，
新一季的音檔全部躺在磁碟上等一個永遠不會來的上傳，而且不會有任何錯誤
（sweep 只會說「0 個音檔」，rc=0）。

改成由 `tools/quarter_label.py` 推導，並且**連上一季一起掃** ——
換季那幾天還會有前一波的落後檔案進來（法說會拖到十月才開的公司，
音檔仍屬 3Q26 那個桶子）。上一季資料夾不存在會回 1，那不是錯誤，照樣往下走。

邊界都驗過：`2026-10-01 → 3Q26 2Q26`、`2027-01-05 → 4Q26 3Q26`（跨年）、
`2026-04-30 → 1Q26 4Q25`。

### 每季換 podcast 樹：已自動化（2026-08-12）

原本每季開季要人工重跑 `build_drive_map.py` 並把新的 folder ID 貼進 config.json。
忘了做不會有任何錯誤浮現 —— 資料夾存在、上傳成功、追蹤表照樣打勾 ——
但整季的音檔都歸進**上一季**那棵樹。

`tools/resolve_podcast_tree.py` 把這件事自動化：掃 grandparent 底下的資料夾，
比對「季度符合」且「帶音檔關鍵字」兩個條件。實測認得出所有歷季變體：

| 資料夾名 | 難點 |
|---|---|
| `2026Q2 美股財報podcast` | 無空格 |
| `2026Q1 美股財報 Podcast` | 有空格、大寫 P |
| `2025 Q4 美股財報 podcast` | 季度中間有空格 |
| `2025Q1美股財報 錄音` | 關鍵字根本不是 podcast |

同一層還有 `2025 Q3 美股財報`、`2025Q2 美股財報` 這種**沒有**音檔字樣的誘餌，
所以兩個條件缺一不可。**命中剛好一個才寫入**；零個或多個一律不猜，
回非零並由 watchdog 的 `podcast_tree_stale` 告警叫人 ——
猜錯的代價（整季歸錯地方）遠高於停下來問一句。

寫回 config.json 用的是針對那兩個值的字串替換，不是整份 `json.dump`：
這個檔裡有大量 `_comment` 說明，整份重寫會把格式與註解位置洗掉。

掛在每日的 `MeiguDates` 上（`run/dates.bat`），順便每天重建一次 drive_map ——
新的 ticker 資料夾整季都在長出來，索引過期會讓某檔被歸到上一層而不是自己的資料夾。

### 剩下唯一需要人的判斷：LINE 額度

免費方案每月 200 則，按**接收人數**計。群組 4 人 → 一檔吃 4 則 → 一個月最多 50 檔。
這一季剩 19 檔沒問題，但下一波財報季（10–11 月）單月可能 60–80 檔，會撞上限。

偵測已自動化（watchdog 的 `check_line()`，剩餘不到兩檔的份就告警），
但**要不要升級方案**是花錢的決定，程式不該自己做。
撞到上限時的行為是安全的：`notify_line.py` 送出前先查額度，不夠就不送並記錯誤，
不會等 429 才發現，也不會影響 memo 與 podcast 的產出與歸檔。

## 授權的即時監控與自動修復（2026-08-12）

回答的問題：「怎麼**即時**知道現在有沒有在授權，而不是出錯了才發現？」

### 三層架構

| 層 | 元件 | 作用 |
|---|---|---|
| 看得到 | 看板最上方「授權狀態」面板（http://127.0.0.1:8787/） | 五盞燈：正常綠／預警黃／異常紅，含最後檢查時間；資料太舊會整排提醒「監控本身可能停了」 |
| 修得動 | `tools/auth_monitor.py`（排程 `MeiguAuthMon`，每 10 分鐘） | 查到掛的先自動修復再說，修好就不吵人；結果寫 `state/auth_status.json` 給看板 |
| 叫得到人 | LINE 私訊（`core/alerts.py`） | 修不好的立刻推播、訊息帶修復指令；恢復時補一則「已恢復」收尾 |

五條授權線與自動修復的誠實邊界：

| 授權線 | 檢查方式 | 修得回來（全自動） | 修不回來（告警帶指令） |
|---|---|---|---|
| Google OAuth | `auth_status()`（內建 refresh） | access token 過期 | refresh token 失效 → `setup_oauth.py` |
| Drive 寫入 | `files.get` 的 `canAddChildren` | —（權限在對方手上） | 請 共用樹擁有者 恢復共享 |
| NotebookLM | `auth refresh` → `auth check --test` | session 變冷 → refresh；整個過期 → `master-token-refresh` 無頭重鑄 | master token 被撤銷（改 Google 密碼）→ 重跑 bootstrap |
| claude CLI | `claude auth status` | 主登入失效 → 自動切換 secrets 長效備援 token（看板轉黃） | 備援 token 也失效 → `claude auth login` |
| LINE 推播 | `bot_info` + 剩餘額度 | — | `setup_line.py --check`／升級方案 |

設計要點：

- **claude 檢查刻意用 `claude auth status` 而不是 `claude -p ok`** —— 前者是本機
  查詢，免費、秒回；後者是一次真的 LLM 呼叫，放進 10 分鐘迴圈一天要燒 144 次。
  端到端能不能跑仍由 watchdog 每小時用 `-p` 驗一次。
- **與 watchdog 共用 alert key 與 `alerts_sent.json` 冷卻**（180 分鐘），
  同一個問題不會被兩邊各轟一次。
- **互相監督**：watchdog 檢查 `auth_status.json` 的新鮮度，超過 65 分鐘沒更新
  就發 `authmon_stale` —— 監控自己停了卻還掛著綠燈，比沒有監控更糟。
- **`authmon.bat` 永遠 exit 0** —— 紅燈用 LINE 與看板表達，不靠排程器的
  LastTaskResult；腳本自己炸掉由 `authmon_stale` 兜底。
- **原 `MeiguNlmWarm` 已停用** —— NotebookLM 的保溫（`auth refresh`）與檢查
  在同一輪裡做掉，兩個排程同時碰 notebooklm CLI 反而有並發風險。

### 二段修復與備援憑證（2026-08-12 深夜，故障演練驗證）

原本 NotebookLM 過期、claude CLI 登出都得叫人。兩邊都補上了無人修復路徑：

**NotebookLM master token**（`notebooklm-py[headless]`，gpsoauth）：

- 一次性 bootstrap（已完成，實測連視窗都沒開就換到了）：
  `notebooklm login --master-token --account you@example.com`
- master token 存在 `~\.notebooklm\profiles\default\master_token.json`，
  與 cookies（`storage_state.json`）分離 —— session 全滅不影響它。
- auth_monitor 的修復鏈：`auth refresh`（變冷）→ 失敗則
  `notebooklm login --master-token-refresh`（無頭重鑄整套 cookies）→ 再驗。
- 故障演練：竄改全部 32 個 cookies（`status: error`）→ 下一輪排程
  自動「已授權（這輪已自動修復）」，無人介入。
- master token 只在改 Google 密碼／撤銷授權時才死 —— 那種情況才叫人。

**claude CLI 長效備援 token**（`core/claude_cli.py`）：

- 一次性設定：使用者自己終端機跑 `claude setup-token`，
  再 `python tools/store_claude_token.py` 存進 secrets（token 不經過對話）。
- auth_monitor 偵測主登入失效 → 驗證備援 token 真的能跑（`claude -p ok`，
  6 小時內不重驗）→ 放旗標 `state/claude_cli_fallback.json` → 派工／告警／
  watchdog 的所有 claude 子行程自動帶 `CLAUDE_CODE_OAUTH_TOKEN`。
- 看板顯示黃色「注意」提醒主登入未修；恢復主登入後旗標自動退場並推「已恢復」。
- 刻意**不**永遠注入 env token：env 優先權高於本機登入，永遠注入等於平常就
  繞過主登入，備援 token 哪天被撤銷會反過來拖垮好端端的主登入。

### 測試模式的 7 天地雷（最常見的「突然沒授權」原因）

OAuth 同意畫面停在「**測試**」狀態時，refresh token **7 天就會被 Google 作廢**
—— 不是 bug，是 Google 對測試狀態應用程式的既定政策。本 README 前面的設定
步驟就是測試狀態，等於每週都要人工重新授權一次，而且斷點以「同意當下」起算，
無人時段斷掉就是靜默停擺。

- **緩解（已自動化）**：auth_monitor 記錄 refresh token 的首見時間（只存指紋
  不存 token），滿 6 天推一次 LINE 預警（每顆 token 只推一次，不洗版），
  讓你在斷線**之前**還有一天可以處理。
- **根治（一次性人工，強烈建議）**：Cloud Console →「OAuth 同意畫面」→
  發布狀態改成「**正式版**」。不需要送審 —— 之後授權時會出現「未經驗證的
  應用程式」警告，點「進階」→「前往」即可。改完重跑一次 `setup_oauth.py`，
  refresh token 從此沒有 7 天限期，這顆地雷整個消失。

### 驗證與排錯

```bash
python tools/auth_monitor.py --dry-run
```

看板面板：http://127.0.0.1:8787/ 最上方。狀態檔：`state/auth_status.json`。
日誌：`logs/authmon.log`。watchdog 的輸出也多了一行「授權監控」。

## 歸檔線的三個修復（2026-08-13）

當天實測暴露的問題，全部修在根因，不是打補丁：

### ① 新 ticker 沒有 Drive 落點 → 用「產業」欄自動建檔

CBRS 實測：memo、podcast 都做完，歸檔卡死在「podcast 樹裡找不到歸檔位置…不猜分類」。
舊邏輯只會從 Word 樹推 podcast 樹，兩棵樹都沒有的全新 ticker 直接失敗等人工。

現在 dispatcher 派工時帶上追蹤表「產業」欄（`--industry`），publish_memo 在兩棵樹
都推不出落點時用三級比對找分類資料夾（`_match_category`）：

    0 葉名正規化後完全相等（含 tree_match 別名表，雙向）
    1 段落相等：『國防/太空』→『國防/戰略物資/太空/無人機』
    2 產業段落 ⊂ 葉名段落：『能源』→『傳統能源/核能/再生能源』
      （刻意只做這個方向 —— 反向會讓樹上叫『測試』的資料夾
        吸走『量子運算測試』這種長產業名，實測踩過）

命中後：word 樹在分類底下補建 `{TICKER}/`、podcast 樹直接用分類資料夾
（兩棵樹各自的既有慣例）。全樹無對應 → 在樹根補建以產業原文命名的資料夾 ——
寧可名字醜（人看得懂、搬得動），也不猜錯類把檔案藏到沒人找的地方。
建完立刻寫回 drive_map.json，同輪與下一輪 sweep 直接可查；每日 build_drive_map
重掃會以 Drive 現況校正。

### ② 歸檔失敗只重跑歸檔，不再整檔重來

CBRS 第一輪歸檔失敗後，第二輪把 20 分鐘的 memo agent 整個重跑（額度白燒、
內容變版），而且 publish 失敗沒有重試上限，放著會每輪重燒一次。兩個修法：

- `_execute` 開頭加**歸檔續跑短路**：`memo_built` 已 done 且檔案還在
  → 跳過 agent、跳過 podcast（m4a 已在），直接進歸檔
- publish 失敗改記 `ck.fail("memo_tab_written", …)` —— watchdog 的
  `run_failed_final` 每小時會撿到並推 LINE（以前只寫 sheet=failed，
  checkpoint 無痕、輪結還回報「done」，失敗完全隱形）

### ③ Podcast 勾選漏打＋checkpoint 落錯季

cli 模式 dispatcher 一條龍上傳後，「打 Podcast 勾」沒有任何人做：sweep 看到
checkpoint 已上傳就跳過（打勾只在它自己上傳成功那條路），dispatcher 又明文
不碰 podcast_done（那是 manual 模式時代的假設）。COHR/CSCO/CBRS 三檔實證漏勾。

- dispatcher 完成時音檔若已在本 run 上傳 → **一併勾 Podcast**；
  memo 先行（音檔未產）維持不動，勾留給 sweep
- sweep 加自癒：已上傳＋報告已勾＋Podcast 未勾 → 補勾（僅 `--publish` 模式動表）

另一個根因：make_podcast 的 checkpoint key 用曆年季度（`2Q26→2026Q2` 機械轉換），
非曆年財年公司（COHR、SMCI…）的 `podcast_built` 會寫進**別季**的 checkpoint 檔
（COHR 實證：主線在 2026Q4，podcast_built 落在 2026Q2，而那可能是已完成的舊季檔案）。
dispatcher 與 backfill 現在都傳 `--tab-quarter`（分頁季度），checkpoint 與 m4a 檔名
都用它（`COHR 2026Q4.m4a`，對齊三邊同標籤慣例）；舊命名的既有檔案照樣沿用不重產。

### ④ 關機空窗（同日一併處理）

- `MeiguAuthMon` 補齊 `StartWhenAvailable`／`WakeToRun`／電池供電可跑
  （其他六個任務本來就有，唯獨它漏了）
- auth_monitor 加**空窗偵測**：兩輪間隔超過 45 分鐘 → 推一則 LINE 說明
  離線多久、排程已自動恢復補跑、空窗期間的財報不會漏（就緒偵測每輪重掃）
- 喚醒計時器 AC 端已確認啟用（`powercfg RTCWAKE` AC=1）。徹底關機仍是
  軟體無解 —— 但開機登入後 `StartWhenAvailable` 會立刻補跑所有錯過的排程，
  「新聞稿一旦確認就永不放棄」保證產出會補齊，空窗通知讓你知道發生過。

### 機器層面的前提（軟體解不掉）

- **必須保持接電**。AC 已設為永不待機／永不休眠（實測 `STANDBYIDLE`／`HIBERNATEIDLE`
  的 AC index 都是 0）。但這是一台筆電，拔掉電源 180 秒就睡；
  把電池模式也改成永不睡只會讓它更快耗盡電力後硬關機，不是解法。
- **必須保持登入**。五個排程都是 `LogonType=Interactive`，使用者登出就不會執行。
  改成 S4U 可以「不論登入與否都執行」，但那會讓 claude CLI 與 notebooklm
  跑在非互動 session，風險比它解決的問題大，維持現狀並靠 `no_activity_24h` 告警。
