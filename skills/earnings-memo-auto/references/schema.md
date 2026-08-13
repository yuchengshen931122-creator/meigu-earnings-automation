# earnings-memo JSON schema

Input file for `scripts/build_memo.py`. Top level keys: `title`, `release_date`, `tldr`, `sections`.

```
{
  "title": string,            // "{中文公司名} {English name} ({TICKER}) {quarter}"
  "release_date": string,     // "YYYY/MM/DD"
  "tldr": [                   // 6-8 numbered highlight points, no "{Label}：" prefixes except
                               // the guidance point ("{年份} 指引：...") -- see SKILL.md for the
                               // required content list (margins x3, segment context, Q&A
                               // takeaway, guidance, day-after stock reaction, historical P/E)
    [ {"text": string, "bold": boolean, "color": "red"?}, ... ],  // one array of segments per point
    ...
  ],
  "dashboard": [                       // optional but expected on every memo; rendered between
                                       // tldr and the income statement image (i.e. page 1).
                                       // See SKILL.md "速覽表" for the four required tables.
    {
      "heading": string,               // e.g. "一、財務結果 vs 市場預期"
      "columns": [string, ...],
      "col_ratio": [number, ...]?,     // one entry per column; normalised, so [20,20,22,14,24]
                                       // and [0.2,0.2,0.22,0.14,0.24] are the same thing.
                                       // Omit and the first column takes ~22-28% and the rest
                                       // split the remainder evenly.
      "rows": [ [<cell>, ...], ... ],  // <cell> = string | {"text","bold","color"} | [segments]
      "note": string?                  // 9pt footnote rendered under the table
    }, ...
  ],
  "income_statement_image": {          // optional; rendered after the dashboard, before section 一
    "path": string,                    // absolute path to a PNG (see pdf_page_to_image.py)
    "caption": string?                 // optional source citation shown centered under the image
  },
  "sections": [ <Section>, ... ]   // exactly 7, in the fixed order described in SKILL.md
}
```

Every `{"text","bold"}` segment anywhere in this schema (tldr, bullet items/children, qa
`*_segments`) also accepts an optional `"color": "red"` alongside `"bold": true` — see "Red is a
second, scarcer tier of emphasis" in SKILL.md for when to use it.

## Section variants

Every section has `heading` (the fixed Chinese heading text) and `type`.

**`type: "bullets"`** — used for sections 一, 二, 三, 五, 六.
```
{
  "heading": "一、整體財務表現",
  "type": "bullets",
  "items": [
    {"text": string},                                        // plain bullet
    {"text": string, "bold": boolean},                       // whole bullet, bold or not
    {"segments": [{"text": string, "bold": boolean}, ...]},   // mixed bold/plain, same shape as tldr
    {"text": string, "children": [{"text": string}, ...]}     // bullet with sub-bullets (level 2);
                                                                // children accept the same text/bold/segments forms
  ]
}
```
Use `segments` to bold the specific figure/label in a bullet without bolding the whole sentence
(see the "Bold the key figures" guidance in SKILL.md) — this is the same mechanism `tldr` uses.

**`type: "table"`** — used for section 四 only.
```
{
  "heading": "四、(下一個季度/全年)財務預測",
  "type": "table",
  "columns": ["項目", "內容"],
  "col_ratio": [number, ...]?,        // same meaning as in `dashboard`; omit for the 2-column default
  "rows": [ [<cell>, ...], ... ]      // one row per guided metric; column 1 is bolded automatically.
                                      // <cell> = string | {"text","bold","color"} | [segments]
}
```
When the company gives both next-quarter and full-year guidance for the same subject, use two
rows and mark the period in the label (`"總營收(次季)"` / `"總營收(全年)"`) rather than merging
them. See SKILL.md for the full list of subjects to cover (margins, per-segment guidance, channel
inventory, capex, etc.) — this section is forward guidance only, don't repeat section 一's actuals.

**`type: "qa"`** — used for section 七 only.
```
{
  "heading": "七、Q&A Session",
  "type": "qa",
  "themes": [ {"text": string}, ... ],       // 4-6 takeaway bullets stated as conclusions with
                                              // numbers/timelines, not topic labels (also accepts "segments")
  "qa": [
    {"q": string, "a": string},                                    // plain Q&A
    {"q_segments": [{"text","bold"}, ...], "a": string},           // bold spans in the question
    {"q": string, "a_segments": [{"text","bold"}, ...]}            // bold spans in the answer
  ]   // Q and A render as two separate paragraphs ("Q1：{q}" then "A：{a}"), auto-numbered from 1;
      // *_segments takes priority over the plain field. Cover every substantive question in the
      // transcript, not a curated subset. "a" must be a first-person rewrite (no names, no
      // pleasantries, one flowing paragraph, key figures bolded via a_segments) -- see SKILL.md.
}
```

## Worked example (VZ 1Q26)

**Note:** this worked example predates both the `Q&A 的核心結論是` rule for `tldr` point 1 and the
`dashboard` block — it is still accurate for bullet/table/qa shapes and formatting conventions, but
follow SKILL.md where the two differ.

This is the JSON that reproduces the reference memo `{專案根目錄}\MEMO\1Q26\VZ 1Q26.docx`. Use it as a
concrete template for formatting conventions (billion→億 conversion, bold placement in `tldr`,
label + full-width-colon pattern in bullets, etc).

```json
{
  "title": "威訊通訊 Verizon (VZ) 1Q26",
  "release_date": "2026/04/28",
  "income_statement_image": {
    "path": "C:/scratch/vz_income_statement.png",
    "caption": "資料來源：Verizon 1Q26 Earnings Release, Condensed Consolidated Statements of Income"
  },
  "tldr": [
    [
      {"text": "1Q26 rev. 達 344.4 億 USD", "bold": true},
      {"text": " (YoY +2.9% ，QoQ-5.3%) 略低於 cons. 的 348.6 億 USD； ", "bold": false},
      {"text": "adj. EPS 達 1.28 USD ", "bold": true, "color": "red"},
      {"text": "(YoY+7.6%) beat cons. 的 1.21 USD；", "bold": false},
      {"text": "adj. EBITDA margin 提升至 38.9%", "bold": true},
      {"text": " 創歷史新高，獲利能力表現穩健且優於市場預期。", "bold": false}
    ],
    [
      {"text": "後付費手機淨增 5.5 萬戶", "bold": true},
      {"text": "創下 2013 年以來首次第一季正成長，且", "bold": false},
      {"text": "流失率降至 0.90%", "bold": true},
      {"text": " 展現客戶導向與精準微細分行銷策略成效，大幅降低 35% 的獲客與留客成本。", "bold": false}
    ],
    [
      {"text": "寬頻網路持續擴張且順利整併 Frontier", "bold": true},
      {"text": "，預計 2028 年帶來 10 億 USD 營運綜效，內部已透過 AI 解決 85% 網路問題並省下 2 億 USD 能源成本；外部正與雲端巨頭積極接洽利用光纖與 5G 資產支援 AI 基礎設施以創造數十億 USD 潛在 rev. 成長動能。", "bold": false}
    ],
    [
      {"text": "2026 指引：", "bold": false},
      {"text": "adj. EPS 成長上調至 5% 到 6% 區間達 4.95 至 4.99 USD", "bold": true},
      {"text": "，行動與寬頻服務 rev. 預計成長 2% 到 3% 總額約 930 億 USD；資本支出維持 160 億至 165 億 USD、FCF 目標為 215 億 USD 以上；後付費手機淨增數上調至 75 萬至 100 萬區間的高標。", "bold": false}
    ],
    [
      {"text": "財報發布當日 2026 年 4 月 27 日股價上漲 1.55% 收 47.10 US，", "bold": true},
      {"text": " 盤中一度漲 4.5%，主因為第一季獲利 beat 及全年財測上調。", "bold": false}
    ],
    [
      {"text": "目前股價約 ", "bold": false},
      {"text": "47.10", "bold": true},
      {"text": " USD 計算，對應當前 TTM EPS 約 ", "bold": false},
      {"text": "4.22", "bold": true},
      {"text": " USD，current P/E 約 ", "bold": false},
      {"text": "11.16", "bold": true},
      {"text": "；以 FY26 EPS mid ", "bold": false},
      {"text": "4.97", "bold": true},
      {"text": " USD 推算，forward P/E 約 ", "bold": false},
      {"text": "9.48", "bold": true},
      {"text": "。", "bold": false}
    ]
  ],
  "sections": [
    {
      "heading": "一、整體財務表現",
      "type": "bullets",
      "items": [
        {"segments": [
          {"text": "總營收：344.40億美元", "bold": true},
          {"text": "，YoY 2.9%，QoQ -5.3%，低於市場預期的348.60億美元。", "bold": false}
        ]},
        {"text": "營運利潤率：", "children": [
          {"text": "GAAP：23.93%，YoY成長 (根據營業利益82.42億美元推算)。"},
          {"segments": [
            {"text": "非GAAP：38.90% (Adjusted EBITDA Margin)", "bold": true},
            {"text": "，YoY提升140bps，創下公司歷史新高。", "bold": false}
          ]}
        ]},
        {"text": "稀釋後每股淨收益：", "children": [
          {"text": "GAAP：1.20美元，YoY 4.3%。"},
          {"text": "非GAAP：1.28美元，YoY 7.6%，高於市場預期的1.21美元。"}
        ]},
        {"text": "營運現金流：80.00億美元，YoY 2.6%。"},
        {"text": "淨資本支出：42.01億美元。"},
        {"text": "非GAAP 自由現金流：38.00億美元，YoY 4.0%。"},
        {"text": "資本回報：支付29.10億美元股息，完成25.00億美元股票回購。"},
        {"text": "淨負債與槓桿比率：無擔保淨債務1,300.53億美元，非GAAP淨無擔保債務對EBITDA槓桿比率增至2.60倍。"}
      ]
    },
    {
      "heading": "二、各部門財務表現",
      "type": "bullets",
      "items": [
        {"text": "消費者部門：營收264.53億美元、年增3.3%、季增-7.0%。部門表現方面，後付費手機淨流失縮減至3.50萬戶，較去年同期大幅改善32.10萬戶；後付費手機流失率降至0.90%；核心預付費連續七個季度成長，淨增11.50萬戶。未來成長將透過AI精準的微細分(Micro-segmentation)行銷降低獲客成本，不再過度依賴免費手機補貼，轉向客製化服務以提升顧客終身價值(LTV)。"},
        {"text": "企業部門：營收74.19億美元、年增1.8%、季增0.7%。部門表現方面，營業利益達8.84億美元(年增33.1%)，EBITDA達19.65億美元(年增16.7%)，EBITDA利潤率大幅提升至26.50%。未來成長潛力在於公司正與大型雲端供應商及企業密切洽談整合光纖與5G資產，以支援資料中心連線及AI基礎設施，未來數月有望帶來數十億美元的潛在營收。"}
      ]
    },
    {
      "heading": "三、產品營運表現/計畫",
      "type": "bullets",
      "items": [
        {"text": "寬頻與光纖擴張計畫：本季寬頻淨增34.10萬戶(包含21.40萬FWA固定無線接入與12.70萬光纖)。全年目標將光纖覆蓋擴張至超過3,200.00萬戶，並透過完成Frontier的整合來加速寬頻與融合服務佈局。數據顯示，結合行動網路與寬頻的融合搭售方案，能使客戶流失率較單一產品低近30%。"},
        {"text": "網路優化與成本控制計畫：已成功完成1.30萬人的裁員，有效降低外包與營運成本。預計到2028年，Frontier的營運整合將創造超過10.00億美元的營運成本協同效應，穩步邁向2026年節省50.00億美元OpEx的目標。"},
        {"text": "AI技術全面導入計畫：正在建立由數據、開發引擎、運行代理及控制平面組成的四層AI技術堆疊(預計7月大致就緒、11月全面完成)。目前85%的網路問題已可由AI自動解決，不僅為公司節省超過2.00億美元能源成本，導入AI語音客服更使客戶滿意度年增1,280bps，軟體交付速度提升超過40%。"}
      ]
    },
    {
      "heading": "四、(下一個季度/全年)財務預測",
      "type": "table",
      "columns": ["項目", "全年預測數值/內容"],
      "rows": [
        ["總營收", "預期行動與寬頻服務營收成長2.0%至3.0%（總額約930.00億美元）；無線服務營收持平"],
        ["非GAAP 稀釋後每股淨收益", "預估上調至4.95至4.99美元 (年增5.0%至6.0%)"],
        ["現金支出", "資本支出預估160.00億至165.00億美元；自由現金流(FCF)預估215.00億美元或以上"],
        ["後付費手機淨增", "預期將落在75.00萬至100.00萬區間的「高標」(Upper half)"]
      ]
    },
    {
      "heading": "五、其他重點",
      "type": "bullets",
      "items": [
        {"text": "微細分與行銷轉型：公司推出「每位顧客都有名字(Every Customer Has a Name)」計畫，透過精細的微細分(Micro-segmentation)策略提供專屬客製化報價，取代過去無差別的免費硬體補貼與盲目漲價。這項轉型使3月份獲客與留客成本(COA/COR)較去年底大幅下降35%。"},
        {"text": "一月網路斷訊事件影響：1月份發生的網路當機事件導致公司直接發放客戶補償，此舉對Q1無線服務營收成長造成了約80bps的單次性負面影響(ARPA亦短暫承壓)。但管理層強調這屬短期衝擊，3月營收成長動能已順利回升至財測區間中點。"}
      ]
    },
    {
      "heading": "六、評價",
      "type": "bullets",
      "items": [
        {"text": "EPS Consensus: Q1預期為1.21美元；全年預期為4.91美元。"},
        {"text": "PE Consensus / PB Consensus: 來源資料未說明。"},
        {"text": "PE Industry Avg.: 來源資料未說明。"},
        {"text": "Market News: 財報發布後，零售投資人情緒由「看跌」強勢轉為「看漲」，社群平台討論聲量激增三倍，市場預期股價有望突破50美元；自新任CEO Schulman去年十月上任以來，VZ股價已上漲12%，表現優於S&P 500指數。"},
        {"text": "Investment Thesis: VZ正在減少對低利潤免費手機促銷的過度依賴，轉向強調客戶生命週期價值(LTV)。憑藉破紀錄的EBITDA獲利能力、連續20年增加股息的強大現金流穩定性，以及導入AI與整合Frontier所帶來的結構性營運槓桿，顯示公司轉型已見成效，基本面具備長期的投資吸引力。"}
      ]
    },
    {
      "heading": "七、Q&A Session",
      "type": "qa",
      "themes": [
        {"text": "帳戶導向策略：公司營運重心由「線路」轉向「帳戶」，隨著融合產品推廣，ARPA將逐漸改善。"},
        {"text": "精準補貼取代免費手機：透過微細分進行客戶留存，不再盲目發放免費手機，Q2升級量已開始放緩。"},
        {"text": "成本撙節進展超前：50億美元OpEx節約進度良好，包含裁員1.3萬人及Frontier整合帶來的後續綜效。"},
        {"text": "寬頻與資本配置：維持強勢FWA推廣及光纖擴展；全年維持至少30億美元股票回購目標。"},
        {"text": "AI商業變現：正積極與雲端巨頭接洽，有望透過提供光纖與5G基礎設施支援AI獲得數十億美元營收。"},
        {"text": "客戶留存率成增長主力：預期未來超過一半的新增淨用戶將來自於客戶流失率(Churn)的實質下降。"}
      ],
      "qa": [
        {"q": "請討論本季帳戶(Accounts)和 ARPA 的表現，以及目前的促銷環境和定價策略將如何影響未來的展望？", "a": "我們現在已經將重心從單看「線路數(Lines)」轉向以「帳戶(Accounts)」為中心。這帶動了帳戶淨增數在消費者和總零售後付費的年成長。第一季ARPA的下降主要來自於我們針對1月網路斷訊所提供的一次性客戶補償，但這只是單次事件。隨著我們在獲客與留客成本上更加紀律化，減少對促銷的依賴，促銷攤銷的逆風將會轉為順風，預計ARPA在2026年及2027年會持續改善。"}
      ]
    }
  ]
}
```
