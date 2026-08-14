---
name: "earnings-memo-auto"
description: "[HEADLESS/AUTOMATION COPY - used by the scheduled dispatcher via `claude -p`. The desktop app uses the plugin copy named `earnings-memo`.] Research a US-listed company's quarterly earnings and produce a Traditional-Chinese earnings memo (.docx) in this user's standard template — the same format as {專案根目錄}\\MEMO\\{quarter}\\{TICKER} {quarter}.docx (e.g. \"VZ 1Q26\") — AND generate the accompanying podcast audio via NotebookLM, downloaded and renamed to `{TICKER} {quarter}.m4a` in the user's Downloads folder. Use this whenever the user asks to \"整理財報\", \"做財報memo\", \"generate an earnings memo\", asks for a summary/writeup of a specific company's quarterly results (earnings release, 10-Q/10-K, investor presentation, conference call / call transcript), or names a ticker plus a quarter (e.g. \"VZ 1Q26\", \"幫我看一下 GEV 這季財報\"), even if they don't explicitly say \"memo\", \"podcast\", or \"skill\". Always use this skill for this kind of request instead of writing an ad hoc summary — the output format is strict and pre-defined, and the podcast step is now a standing part of the workflow, not optional."
---

# Earnings Memo Builder

Produce a Traditional-Chinese quarterly earnings memo for a US-listed company, matching an exact
pre-existing template used for every past memo in `{專案根目錄}\MEMO\`. The template's layout (fonts, bullet
styles, table borders, section order) is fully handled by a bundled script
(`scripts/build_memo.py`) — your job is research and writing the Chinese-language content that
goes into it, not fiddling with docx formatting.

**This skill covers two deliverables per ticker/quarter, in order: the Word memo (Steps 0-5
below), then the podcast audio via NotebookLM (Step 6).** Both are standing parts of the
workflow — don't stop after the memo and wait to be asked for the podcast.

**Division of labor:** you gather facts and write text into a JSON file; `build_memo.py` turns
that JSON into a pixel-faithful `.docx`. Never hand-build the docx yourself (no docx-js, no
manual XML) — the script already reproduces the exact fonts/margins/bullets/table styling found
in the reference memos, and reinventing it by hand is how small formatting drifts creep in.

The whole document (title, headings, body bullets, table, Q&A) renders in **Microsoft JhengHei
(微軟正黑體)** — set this once in the script, not per-section.

## Step 0: environment check

`build_memo.py` depends on `python-docx`; the income-statement screenshot step depends on
`pymupdf` and `pillow`. Check once per session:

```
python -c "import docx" || python -m pip install python-docx
python -c "import fitz, PIL" || python -m pip install pymupdf pillow
```

**If this run was launched by a scheduled task, rename the *previous* earnings-memo session
now.** A scheduled run's session gets an auto-derived title that is just the task ID prettified
(`earnings-memo-stld-noc-2q26-7am` -> `Earnings memo stld noc 2q26 7am`). Among a sidebar of
Chinese titles those all look alike, and the user cannot find the conversation again once the
one-time task auto-disables and drops out of the ROUTINE panel.

`set_session_title` refuses the *current* session, so a run can never fix its own title — but it
can fix the last one. At the start of every run: `list_sessions`, find the most recent session
whose title looks like a prettified `earnings-memo-*` task ID, and rename it to
`{季度} 財報memo・{代號}（{月/日}）`, e.g. `2Q26 財報memo・STLD+NOC（7/22）`. Confirm the linkage
with `get_session` (`scheduledTaskId`) if more than one candidate matches.

## Step 1: figure out what's being asked for

Parse the ticker and quarter from the request (e.g. "VZ 1Q26" -> ticker VZ, calendar Q1 2026).
Some companies report on a non-calendar fiscal year (e.g. existing memos use labels like
"1Q26FY" or "FY26Q3") — check how that company's own filings label the quarter and follow their
convention, matching the labeling style seen in `{專案根目錄}\MEMO\` (browse existing filenames there if
unsure — e.g. `Emerson (EMR) 1Q26FY.docx`, `SMCI 2Q26FY.docx`).

**If the user says "最新一季" / "latest quarter" instead of naming one, resolve it by checking
the company's actual most recent earnings release — don't infer it from today's date.** Fiscal
calendars vary enough (Microsoft's FY ends June, Nvidia's ends late January, Apple's ends late
September, Walmart's ends January...) that "it's July, so this must be Q2" is wrong often enough
to not be a shortcut. Search for "{ticker} latest earnings" / check the IR site's most recent
press release, confirm the specific period it covers and how the company itself labels it (e.g.
"fiscal 2026 third quarter"), and state that resolved label back to the user in your reply (e.g.
"NVDA 最新一季是 FY26 Q1，截至 2026/4/27，財報於 2026/5/28 公布") so a wrong guess is obvious
and correctable immediately rather than buried in the memo. Default to the most recently
*reported* quarter (has actual results), not the quarter that's currently in progress — if the
next report looks imminent (within a few days), flag that ambiguity to the user instead of
silently picking one.

If the quarter isn't finished reporting yet, or you can't find a press release for that specific
quarter, say so rather than guessing — don't fabricate financials.

## Step 2: gather source material

Collect these, in this priority order, using WebSearch/WebFetch:

1. **Earnings press release** — the company's own IR site (investor.{company}.com or similar),
   under "News & Events" / "Financial Results" / "Quarterly Results". This has the headline
   numbers, segment tables, and guidance. This is your primary numeric source — prefer it over
   any third-party recap. Companies often publish two separate PDFs: a short "highlights" release
   (narrative only, no statement tables) and the full exhibit (highlights *plus* the Condensed
   Consolidated Statements of Income/Balance Sheet/Cash Flows and segment schedules, usually
   10-15+ pages). If the PDF you fetched is only 3-5 pages, you have the highlights-only version —
   look for the fuller one (it's what actually gets filed as the 8-K Exhibit 99.1; a copy is
   sometimes mirrored on financial news aggregators when SEC.gov blocks the fetch) before
   concluding the income statement isn't available. You need the full version anyway for
   Step 3's income statement screenshot.
2. **SEC filing for the same period** — the 8-K (which usually attaches the press release as
   Exhibit 99.1) and/or the 10-Q/10-K, via SEC EDGAR full text search
   (`https://www.sec.gov/cgi-bin/browse-edgar` or `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={name}`,
   or `https://efts.sec.gov/LATEST/search-index?q=...`). Use this to cross-check GAAP figures,
   segment detail, and any numbers not in the press release.
3. **Investor presentation / earnings call slide deck** — usually a PDF linked next to the press
   release on the IR site ("Earnings Presentation", "Supplemental Materials", "Investor Deck").
   Useful for guidance detail, KPIs, and forward-looking initiatives that don't appear in the
   press release.
4. **Earnings call transcript** — search the web for "{ticker} {quarter} earnings call
   transcript". Try in this order:
   - Free sources first: Motley Fool's "Earnings Call Transcript" series, investing.com, and the
     company's own IR site webcast/transcript PDF are usually free and fetchable directly via
     WebFetch.
   - **Seeking Alpha requires login, but the user has an account** — WebFetch won't have their
     session, so it'll look paywalled/blocked even though the user can actually read it. Use the
     browser tools instead (`mcp__claude-in-chrome__*` or `mcp__Claude_Browser__*`, whichever is
     available) to `navigate` to the Seeking Alpha transcript URL in a real browser tab, which
     carries the user's already-logged-in session — then read the page content from there. This
     is usually the most complete and fastest-to-post source once it's up, so check it even when
     a free alternative already worked, if the free one seems thin (e.g. paraphrased highlights
     rather than a full Q&A).
   - This is where Q&A content and management color come from — don't skip straight to inventing
     question/answer content if the first source you try is thin; try the next one down this list
     before concluding no transcript is available. If truly nothing is up yet anywhere, tell the
     user what's missing (e.g. section 七 Q&A) rather than fabricating it.
5. **Stock price reaction + valuation** — check what time the release actually came out relative
   to US market hours (pre-market/intraday vs. after-hours) so you pull the *right* reaction
   window — see the `tldr` point 5 rules below, this trips people up because "the day after"
   isn't always correct. Also get the current price and the stock's **3-5 year historical trailing
   P/E range** (e.g. macrotrends.net's `/stocks/charts/{TICKER}/{name}/pe-ratio`, or
   stockanalysis.com/wsj's historical valuation pages) — needed for the `tldr` P/E line and
   Section 六.

If any of these is genuinely unavailable after a real search, proceed with what you have and
flag the gap explicitly in your reply to the user (don't silently drop a section, and don't
invent numbers to fill it).

### When a source blocks the fetch (403 / bot detection)

SEC.gov and some IR sites block WebFetch fairly often. Work down this list instead of giving up
after one try:

1. **Try a mirror first.** Press releases are usually syndicated through GlobeNewswire,
   Business Wire, or PR Newswire (the original distribution wire — rarely blocked), and often
   get mirrored on stocktitan.net, fortune.com (search `site:fortune.com company-assets
   {ticker}`), quartr.com, or sec.report. A `WebSearch` for the release headline plus one of
   these site names usually surfaces a fetchable copy. Also check whether the IR site exposes a
   direct PDF link for the exact release (e.g. a `/node/{id}/pdf` path next to the HTML page) —
   fetching that PDF URL directly often succeeds even when the HTML page or a generic crawl of
   the same domain gets blocked.
2. **Switch from WebFetch to the browser tools.** WebFetch is more likely to trip bot detection
   than an actual rendered browser session. If the browser tools (`mcp__Claude_Browser__*`) are
   available, `navigate` to the blocked URL and read it with `get_page_text`, or `screenshot`/
   `zoom` it directly — this also solves the income-statement-screenshot step below without
   needing a fetchable PDF at all, since you can screenshot the table straight out of the
   rendered page or an in-browser PDF viewer.
3. **Try the Wayback Machine** (`web.archive.org/web/2*/{url}`) as a last resort — earnings
   press releases usually get crawled within hours of publication.

If a source is still unreachable after this, that's a legitimate gap — disclose it in the log
(Step 5) rather than spending unbounded effort or fabricating the missing figures.

**If the whole execution environment has restricted general internet access** (a sandboxed shell
that can reach a handful of allowlisted domains but not the open web, so `is_shot.py` /
`pdf_page_to_image.py` and any script-driven fetch fail with connection errors even though
`WebFetch`/`WebSearch` still work): fall back to reconstructing the income-statement table as a
PNG from the exact figures you already pulled via `WebFetch`, using a local script (matplotlib or
similar) styled to look like a clean financial statement table (title block, three-line header,
right-aligned numeric columns, ruled subtotal/total rows). This is not "hand-typing a
reproduction" in the sense the rule above warns against — the numbers still come verbatim from
the official release, not from memory or estimation — but caption it with the same source
citation and treat it as a fallback, not the default: always try the real screenshot route first
in every environment where the network access actually supports it.

## Step 3: write the content into JSON

Build a JSON file matching the schema in [references/schema.md](references/schema.md), then run:

```
python scripts/build_memo.py <data.json> "{專案根目錄}\MEMO\{calendar_quarter}\{TICKER} {fiscal_label}.docx"
```

**The folder and the filename use different labels — this is deliberate.**

- **Folder = the calendar quarter being reported on**, so everything from one earnings season files
  together and can be read side by side (e.g. `{專案根目錄}\MEMO\2Q26\`). Determine it from the *period the
  report covers*, not the period-end date and not today's date: a 52/53-week fiscal quarter that
  ends a few days into the next calendar quarter still belongs to the one it actually covers.
  Skyworks' FY26 Q3 ran to 2026/7/3 but covers April–June, so it files under `2Q26`, not `3Q26`.
  KLA's FY26 Q4 ended 2026/6/30 and likewise files under `2Q26`.
- **Filename = the company's own fiscal label** for that quarter, exactly as resolved in Step 1
  (`SWKS 3Q26FY.docx`, `KLAC 4Q26FY.docx`, `NUE 2Q26.docx`). Never rewrite a company's fiscal
  quarter into a calendar one in the filename — the label is how the user identifies which of that
  company's quarters it is.

So a single `{專案根目錄}\MEMO\2Q26\` folder legitimately contains `NUE 2Q26.docx`, `SWKS 3Q26FY.docx` and
`KLAC 4Q26FY.docx` together. Create the calendar-quarter folder if it doesn't exist; do **not**
create a separate folder per fiscal label. Also pass the fiscal label (not the calendar quarter)
to `log_run.py --quarter`, so the log row matches the filename.

### Income statement screenshot (page 1, right below the dashboard)

The memo embeds an actual screenshot of the income statement table — not a hand-typed
reproduction — right after the `dashboard` block (see below) and before section 一. To produce
it:

1. Get the full earnings-release PDF (see the note in Step 2 about the highlights-only vs. full
   version — you need the full one here).
2. Find which page holds the income statement: open the PDF (the Read tool renders PDF pages) or
   check with `python -c "import fitz; [print(i+1, p.get_text()[:60]) for i,p in enumerate(fitz.open(r'<pdf>'))]"`
   and look for the page whose text starts with "Condensed Consolidated Statements of Income".
3. Render and auto-crop that page to a PNG:
   ```
   python scripts/pdf_page_to_image.py "<pdf_path>" <page_number> "<scratchpad>/income_statement.png"
   ```
4. Add a top-level `income_statement_image` field to the JSON:
   ```json
   "income_statement_image": {
     "path": "<absolute path to the PNG>",
     "caption": "資料來源：Verizon 1Q26 Earnings Release, Condensed Consolidated Statements of Income"
   }
   ```
5. After building, open the PNG yourself (the Read tool displays images) and confirm it's the
   right table, legible, and not awkwardly cropped — the autocrop trims whitespace but doesn't
   know which table you meant, so a wrong page number silently produces a wrong screenshot.

**When the source is HTML, not a PDF — which is the common case — use
`scripts/is_shot.py` instead.** Most issuers publish the release as HTML only, and `WebFetch`
gets a 403 from `sec.gov`, so the reliable path is the SEC 8-K exhibit rendered locally by
headless Chrome:

1. Find the 8-K and its EX-99.1. `data.sec.gov` and `www.sec.gov/Archives` both answer plain
   `Invoke-RestMethod`/`requests` calls as long as you send a `User-Agent` with a contact address:
   ```
   https://data.sec.gov/submissions/CIK<10-digit-zero-padded>.json   -> filings.recent, pick the 8-K
   https://www.sec.gov/Archives/edgar/data/<cik>/<accession-no-dashes>/index.json  -> pick the ex99 .htm
   ```
2. Render it:
   ```
   python scripts/is_shot.py "<exhibit url>" "<scratchpad>/income_statement.png"
   ```
   It downloads the HTML, keeps the issuer's own table markup and heading (nothing is retyped),
   picks the table that has both a per-share line and a revenue/net-sales line, screenshots it via
   headless Chrome at 2x, and auto-crops. It prints every candidate table it considered — **read
   that list.** The largest match is usually right but not always: L3Harris's biggest match was
   the segment table with guidance columns, and the real income statement was the smaller one.
   Re-run with `--index N` to override, `--width` to change the layout width.
3. This also works on a press-release URL when the 8-K exhibit lacks the financial schedules
   (Flex, for example — its schedules are on PR Newswire, not in the 8-K).

Prefer the PDF-page route when a full-release PDF genuinely exists; reach for `is_shot.py`
otherwise. **If neither can run because the shell has no general internet access** (see the
fallback note at the end of Step 2's blocked-fetch section), reconstruct the table as a PNG from
the verified figures instead of skipping the image.

**Retro-fitting the screenshot into a memo that already exists** — e.g. the user has since
hand-edited the text and rebuilding from JSON would destroy their edits:

```
python scripts/insert_is.py "<docx>" "<png>" "<caption>"
```

It inserts the image + caption immediately before the `一、` heading, matching `add_image()`
exactly (centred, 17 cm, 9 pt caption), and touches nothing else. It refuses and exits 2 if the
docx already contains an inline image, so re-running is safe. Word holds a write lock on an open
file — a `PermissionError` plus a `~$<name>.docx` in the folder means the user has it open; ask
them to close it rather than trying to force the write.

### Content structure (fixed — do not reorder or rename sections)

**Title**: `"{中文公司名} {English name} ({TICKER}) {quarter}"` — e.g. `"威訊通訊 Verizon (VZ) 1Q26"`.
Use the Chinese name only if there's a commonly-used one; otherwise English name + ticker is fine.

**Release date**: the earnings release date, `YYYY/MM/DD`.

**TL;DR (`tldr`)**: 6-8 numbered highlight points — this is the section that gets read most
carefully, so it needs to actually be dense with substance, not a thinned-out preview of the
sections below it. **Dense means facts-per-character, not length**: the failure mode isn't a
short `tldr`, it's a long one padded with framing and interpretation — see the ceiling and the
"states facts, does not argue" rule below. Write each point as flowing content, not a labeled sub-point — no
`"{Label}："` prefixes here (the one fixed exception is the guidance point, which always starts
`"{年份} 指引：..."`, e.g. `"2026 指引：..."`). Lean hard on English financial shorthand (`rev.`,
`EPS`, `GM`/`OPM`/`NM` for gross/operating/net margin, `YoY`, `QoQ`, `cons.`, `adj.`, `beat`/
`miss`, `bps`) to keep it dense rather than wordy — this is a research note for someone who
already reads in this shorthand, not prose for a general reader. Bold the standout figures within
each point (not whole sentences) using `segments`, the same way as elsewhere.

**tldr point 1 is fixed: it always leads with the Q&A takeaway.** Start the first `tldr` point
with the literal prefix `"Q&A 的核心結論是："`, followed by the single most decision-relevant
conclusion from the earnings call Q&A (same "what's actually a highlight" judgment used for
point 3's criteria below and for section 七's `themes`). Every other point then shifts down one
slot from the ordering described next (headline financials becomes point 2, segment context
becomes point 3, and so on) — the *content* required of each point doesn't change, only where it
sits in the list.

**Hard ceiling: each point is at most 2 sentences and roughly 120 Chinese characters.** Not a
target — a ceiling. If a point won't fit, that's the signal it's carrying material that belongs in
a body section, not that the ceiling is wrong. Trim the *argument*, never the *facts* (see the
must-not-cut list below).

**`tldr` states facts; it does not argue.** This is the biggest source of bloat and it survives
every filler-word pass because each sentence reads useful in isolation. The interpretation — why
it matters, what it implies for the model, what to watch, how the market read it — lives in
section 五 and the `Investment Thesis` line of 六, which is where the user goes for it. In `tldr`
the facts have to carry themselves. Specifically, do not write:

- **Causal/narrative closers**: `"這才是盤後由 +4.7% 反轉至 -7.8% 的真正原因"`, `"市場在細讀後選擇賣出"`,
  `"營收創高但獲利腰斬——這是本季全部矛盾的所在"`. State the two facts adjacently and stop; a reader
  who sees `beat 3.4%` and `OPM 指引由 16% 砍到 12.5%–14%` next to a `+4.7% → -7.8%` reaction does
  not need the causal sentence written out.
- **Implication clauses**: `"這對估值模型的意涵是…"`, `"代表…"`, `"換言之…"`, `"值得注意的是…"`,
  `"是理解 X 的關鍵"`.
- **Long management quotes.** A quote earns its place only when the *wording itself* is the news
  (an admission, a correction, an unusually blunt commitment) — and then it's one short clause, not
  three lines. Otherwise paraphrase to the fact: `CEO 說「Kidney is what gets us from 10 to
  20, and that's by 2030 and 2032」` → `腎臟貢獻定位在 2030–2032`.

**One point = one idea.** Don't chain a second thought after a full stop to avoid adding a point —
if there are two ideas, they're two points (that's what the 6-8 range is for), or the weaker one
drops.

**Then cut filler words and connectives.** Same content, fewer words — density comes from the
wording, not just the facts. Concretely:

- **Drop framing clauses that add no fact.** `"訂單與在手訂單才是本季真正的亮點："` → `"訂單為本季亮點："`.
  `"管理層在法說會給出的關鍵增量資訊："` → `"法說會關鍵增量："`. `"這是一份「營收平淡、利潤率爆發」的
  財報。"` → `"典型的「rev. 平淡、利潤率爆發」。"`
- **Drop redundant verbs around numbers.** `"rev. 達 103.97 億 USD"` → `"rev. 103.97 億 USD"`;
  `"beat cons. 的 101.5 億 USD"` → `"beat cons. 101.5 億 USD"` (的 before a consensus figure
  is always droppable); `"當日正常盤股價上漲 +4.00% 收 124.97 USD"` → `"當日正常盤 +4.00% 收 124.97 USD"`.
- **Don't repeat a unit or label the reader already has.** In a margin list, write
  `"GAAP OPM 13.0% (-180bps)"` rather than repeating `YoY` on every entry once the first one
  established it.
- **Lead with the fact, not the setup.** `"法說會上市場最在意的是「全年 organic 從 2%–4% 收斂到
  2%–3% 為什麼」：管理層明確指出 X 是唯一原因"` → `"全年 organic 由 2%–4% 收斂至 2%–3%，唯一原因為 X"`.
- **What must NOT be cut**: any number, any YoY/QoQ/beat-miss comparison, any guidance figure, any
  named driver, the stock-reaction window, or the valuation line. Shortening means removing
  connective tissue, never removing a data point or collapsing two facts into one vaguer one. If a
  trim would cost information, keep the longer phrasing. A short point that dropped the QoQ figure
  is a worse point than a long one that kept it — the ceiling loses to this rule, not the other way
  round.

Worked example — same facts, 191 characters down to 88, nothing lost:

> **Before**: `法說會揭露了 press release 沒寫的關鍵一句：全年 operating margin 指引由原本的 16%
> 下修至 12.5%–14%，且 2H26 GM 預期約 59% (低於 2Q 的 60%)。這才是盤後由 +4.7% 反轉至 -7.8% 的真正
> 原因——營收指引上調下緣、利潤率指引卻大幅下修，市場在細讀後選擇賣出。`
>
> **After**: `法說會 (非 press release) 下修全年 OPM 指引至 12.5%–14% (原 16%)、2H26 GM 約 59%
> (2Q 為 60%)。`
>
> What went: the `法說會揭露了…關鍵一句` framing, the causal closer, the restatement of the stock
> reaction (already its own point). What stayed: every number, the source distinction, and the
> comparison bases.

Cover all of these, each as its own point (this is 6 required points plus the fixed Q&A lead
described above, so 7 total; add 1-2 more if there's a second thing genuinely important enough to
flag at this level):

1. **This quarter's headline financials**: rev. (YoY%, QoQ%), EPS, and — this is often
   under-covered — **gross margin, operating margin, AND net margin**, not just one of them.
   State plainly whether each beat/missed/matched consensus, and separately whether it beat/
   missed the company's own prior guidance midpoint in bps where the company gives margin/EPS
   guidance (these are two different comparisons — "優於市場預期" and "優於財測中點" are not the
   same claim, don't conflate them).
2. **Business/segment context**: how revenue breaks down across segments or geographies, what's
   actually growing vs. shrinking, and any new strategic moves or growth drivers management
   flagged this quarter (a new product, a partnership, a capacity buildout, an M&A integration).
3. **What the market is most focused on in Q&A, stated as a conclusion, not a topic list.**
   Don't write "management discussed pricing pressure" — write what they actually said, with the
   timeline and numbers: e.g. "management guided margin recovery to complete by Q3, citing $XM in
   cost actions already booked." This is frequently missing and it's one of the most useful
   points in the whole memo — pull it from your Q&A research even before writing section 七.

   **How to actually tell what's a highlight vs. a routine answer** — this is the part that goes
   generic most easily, so apply real judgment rather than defaulting to "the first question
   asked" or "whichever one has the most words." An exchange earns a place in `tldr` (and a
   `themes` bullet in 七) when it does at least one of these:
   - **Discloses something incremental** — a number, timeline, or admission that wasn't already
     in the press release or deck. If the answer just restates a figure the reader already saw in
     section 一/四, it's not a highlight no matter how confidently it's phrased.
   - **Commits to something forward and specific** — a date, a dollar figure, a threshold — not a
     vague reassurance ("we feel good about the trajectory").
   - **Directly explains the stock's actual reaction** (tldr point 5) — if the memo says the stock
     fell on capex/FCF concerns, the Q&A exchange where management addressed that concern is
     almost certainly the highlight, more so than an unrelated question that happened to come
     first.
   - **Multiple analysts pushed on the same topic from different angles.** That convergence is
     itself a signal of what the market is anxious about, even if any single answer sounds routine
     in isolation — notice the pattern across questions, not just the content of one.

   A polite non-answer, a restatement of already-public numbers, or a single narrow follow-up
   nobody else asked about is not a highlight, even if it happens to be the first or longest
   exchange in the transcript.
4. **Forward guidance**, fixed format: `"{年份} 指引：{content}"`.
5. **Stock reaction — which window depends on when the release actually happened relative to US
   market hours:**
   - **Pre-market or during regular trading hours (盤前/盤中)**: use *that same US trading day's*
     regular-session reaction (close vs. prior close).
   - **After market close (盤後)** — the common case, since most large caps report after the
     close: use *that same day's after-hours session* reaction, not the next day's regular
     session. These can tell very different stories (a stock can pop after-hours on the headline
     numbers, then reverse once the call itself reveals something the numbers didn't — reacting
     to next-day close alone would report the pop and miss the reversal, or vice versa). If a
     clean after-hours closing print isn't available, the initial after-hours move right after
     the release, plus how it moved during/after the call if that's reported (e.g. "popped 4%
     initially, gave it back after the CFO's capex comments"), is the best available substitute —
     just don't substitute the *next day's regular session* number and label it as the after-hours
     reaction.
   - **Always convert the date to the Taiwan calendar date for display**, not the US date. US
     after-hours sessions on the release evening (US Eastern time) routinely fall in the early
     morning hours of the *next* calendar day in Taiwan (e.g. a 5:30pm ET call is ~5:30am Taiwan
     time the next day) — write whichever Taiwan date the reaction you're citing actually falls
     on, and note it's the after-hours session so it's unambiguous which reaction window you mean
     (e.g. `"財報盤後公布(台灣時間2026/4/23清晨)，當天盤後一度上漲4%，隨後...回吐至下跌約1%"`).
6. **Valuation**: `近 3-5 年歷史 P/E 區間落在 XX-XX 倍，以目前股價約 XX USD 計算，對應當前 TTM
   EPS 約 XX USD，current P/E 約 XX；以 {FY} EPS mid XX USD 推算，forward P/E 約 XX。` — the
   historical range is what makes the current multiple mean anything; don't drop it even though
   it takes an extra search to find (see Step 2's blocked-source note if the usual historical P/E
   sources don't load).

   **When P/E doesn't work as the anchor** (loss-making years leave gaps in the historical range,
   or a one-off gain/charge distorts TTM EPS), switch the anchor to **P/S** — or P/B where the
   business is asset-based — and **just state those numbers directly, in the same shape as the P/E
   line**: `近 5 年 (FY21–FY25) 歷史 P/S 區間 1.71–2.04 倍，以目前股價 221.56 USD、TTM rev. 939.95
   億 USD 計算，current P/S 約 1.85 倍、位於區間中位。` **Do not spend a sentence explaining that
   P/E is unusable** — no `"P/E 對 X 無參考價值"`, no `"沒有可用的近 5 年 P/E 區間"`, no
   `"較有意義的錨是 P/S"`. Lead with the multiple you're actually using. Still report current and
   forward P/E afterwards as secondary figures with a short parenthetical on what distorts them
   (e.g. `"current P/E 約 82.7 (TTM 含 4Q25 一次性巨額利益)"`), but the P/S line comes first and
   carries the argument. Apply the same rule in Section 六's `PE Consensus / PB Consensus` line.

**Bold the key figures in every bullet, not just `tldr`.** A bullet item can be either a plain
`{"text": "..."}` or, like `tldr`, a `{"segments": [{"text","bold"}, ...]}` array — use `segments`
whenever a bullet contains a standout number (the actual figure, a YoY/QoQ swing, a beat/miss
delta, a guidance change) so a reader skimming the page catches it without reading every word.
Bold the number and its immediate label, not the whole sentence — e.g. bold `"總營收：344.0億美元"`
and leave `"，YoY 2.9%，低於市場預期的348.0億美元。"` plain. Reserve full-sentence bold for the one
or two things in each section that are genuinely the headline (a record metric, a guidance raise,
a material one-off item) — if everything is bold, nothing reads as emphasized. Q&A `q`/`a` text
can take the same treatment via optional `q_segments`/`a_segments` when management cited a number
worth highlighting.

**Red is a second, scarcer tier of emphasis on top of bold** — add `"color": "red"` to a segment
(alongside `"bold": true`) for the single most decision-critical fact per section, not for every
bolded figure. Think of it as: bold = "worth noticing while skimming", red = "the one thing a
reader must not miss even skimming past everything else" — the headline EPS/revenue beat-or-miss
in `tldr`, a guidance cut or raise, a genuine red-flag risk (leverage covenant risk, a going-concern
note, a material one-off charge), or a record/all-time-high metric. Budget roughly 1-3 red spans
for the whole memo; if more than a handful of things are red, none of them reads as urgent anymore.

### 速覽表 (`dashboard`, page 1 — between `tldr` and the income statement image)

Every memo needs a page-1 skim block for a reader who only has 30 seconds and wants the answer to
three questions without reading prose: **(1) which numbers beat/missed and by how much, (2) did
the quarterly guidance beat expectations and was the full-year outlook raised, (3) what is the
market actually fixated on in this report.** `tldr` carries the narrative; this block carries the
same underlying facts as short, scannable tables so the reader doesn't have to hold a whole `tldr`
paragraph in their head to answer "was this a beat or a miss." This is a required part of every
memo, not optional polish — build it every time, the same way `tldr` and the seven sections are
built every time.

Add a top-level `dashboard` field to the JSON (see `references/schema.md`'s `dashboard` array —
each entry is a table block with `heading`, `columns`, `rows`, and an optional `note`, rendered by
`add_dashboard()` right after `tldr`). Four tables, in this exact order:

1. **`一、財務結果 vs 市場預期`** — columns `["項目", "實際", "市場預期/財測中點", "差異", "結果"]`.
   One row per headline metric: rev., GAAP 毛利率, 非GAAP 毛利率, GAAP 營運利潤率 (or 非GAAP if
   that's the company's preferred guided metric), GAAP EPS, plus any other metric the company
   itself guides to (Adjusted EBITDA, FCF, etc.). `差異` is the actual computed gap in bps or %
   — reuse the exact figures already computed for section 一, never a vaguer restatement. `結果`
   is `Beat` / `Miss` / `Inline`, with a magnitude qualifier where useful (`大幅 Beat`, `小幅
   Miss`) — bold this cell so it's the first thing a skimming eye catches per row.
2. **`二、財測與展望`** — columns `["項目", "本次財測", "vs 市場預期/前次財測", "結果"]`. Next-
   quarter guidance line by line vs. consensus, or vs. this quarter's actual where that's the more
   informative comparison (e.g. margin guidance stepping down from actual is itself the finding).
   Always include one explicit row answering whether full-year guidance was raised / cut /
   maintained / not provided — this is one of the most commonly asked follow-up questions and is
   easy to lose inside a wall of bullets in section 四.
3. **`三、市場關注焦點`** — 2-column, `["焦點", "結論"]`, 3-5 rows. Same "what's actually a
   highlight" judgment as the `tldr` Q&A lead point and section 七's `themes` (incremental
   disclosure, specific forward commitment, ties to the actual stock reaction, multi-analyst
   convergence), condensed to one row each. This table and `tldr` point 1 should point at the same
   underlying issues — one as a table row, one as a sentence — not diverge.
4. **`四、評價快照`** — 2-column, `["指標", "數值"]`. 現價 (with the date), the current multiple
   (P/E, or the P/S/P/B fallback per the "P/E calculation" rules above — same anchor choice as
   `tldr` point 6 and section 六), the 3-5yr historical range, EPS/rev. consensus for the current
   or next fiscal year, and analyst average price target/rating if available. Same figures as
   section 六's valuation bullets, just surfaced to the front so a reader doesn't have to scroll to
   the bottom to see whether the stock looks cheap or expensive right now.

Keep each table to what fits without scrolling on a printed page — 4-7 rows is the sweet spot. If
a table wants more than ~8 rows, that's a signal the extra detail belongs in the body section
instead, not that the table should grow to hold it. **Every number in the dashboard must match
the number written out later in `sections` exactly** — this is the same "the TL;DR must match the
detail in the sections below it" check from Step 4, just extended to cover the dashboard too.

**Sections (`sections`, in this exact order and these exact Chinese headings):**

1. `一、整體財務表現` (bullets) — one line per subject, each written as a single flowing
   sentence/short paragraph (not fragmented into many small bullets), covering, **in this order**:
   - **總營收**: `"總營收：{rev}，YoY {x}%，QoQ {y}%，(高於/符合/低於)財測中點{n}bps，(高於/
     符合/低於)市場預期。"` — note this is *two separate comparisons* (vs. the company's own prior
     guidance midpoint in bps, and vs. analyst consensus); include whichever of the two you have
     data for, and both if you have both.
   - **毛利率**: GAAP and 非GAAP as sub-bullets (`children`), same vs.-guidance/vs.-consensus
     pattern where available.
   - **營運利潤率**: GAAP and 非GAAP as sub-bullets.
   - **稀釋後每股淨收益**: GAAP and 非GAAP as sub-bullets.
   - **營運現金流**
   - **淨資本支出**
   - **非GAAP 自由現金流**
   - **資本回報** (dividends + buybacks)
   - **淨負債與槓桿比率**

   **If a subject genuinely has no disclosed figure, omit that line entirely rather than writing
   a "來源資料未說明" placeholder** — unlike section 六 below, these are core financials that are
   almost always disclosed, so a missing one is rare enough not to need a placeholder cluttering
   the page.
2. `二、各部門財務表現` (bullets) — one bullet per reporting segment, segment name **bolded**
   with a full-width colon, then substance: revenue (YoY%, QoQ%), how the segment actually
   performed this quarter (list this out in real detail — margin, volume/KPI trends, what drove
   the move, not just the headline number), and near-term growth outlook for that segment
   specifically (also detailed, not one throwaway clause).
3. `三、產品營運表現/計畫` (bullets) — one bullet per major product/initiative, bolded label +
   colon, then detailed substance backed by numbers. Cover supply/demand dynamics, pricing
   actions, and tariff impacts where the company discussed them, alongside the usual
   product-launch/capacity/partnership items — these are often where the real quarter-over-quarter
   story is, not just in the headline financials.
4. `四、(下一個季度/全年)財務預測` (table, columns `["項目", "內容"]`) — every forward-looking
   number the company gave, organized by subject, covering **both the next quarter and the full
   year separately** where the company guides both (don't merge them into one row — when both
   exist for the same subject, use two rows and mark the period in the label, e.g.
   `"總營收(次季)"` / `"總營收(全年)"`). Don't repeat this quarter's actuals or anything already
   covered in section 一 — this section is exclusively forward guidance. Rows to include wherever
   disclosed: 總營收, 非GAAP毛利率, 非GAAP營運利潤率, 非GAAP稀釋後每股淨收益, 各部門預期 (one row
   per segment the company guided separately), 通路庫存 (channel inventory, where relevant e.g.
   hardware/semis), 現金支出 (capex).
5. `五、其他重點` (bullets) — anything notable that doesn't fit sections 一-三, bolded label +
   colon, backed by numbers.
6. `六、評價` (bullets) — fixed lines, in order: `EPS Consensus`, `PE Consensus / PB Consensus`,
   `PE Industry Avg.`, `Market News` (post-earnings sentiment/news, plus how the stock has moved
   since if there's a notable further move), `Investment Thesis` (a real synthesis, not a
   restatement of section 一 — the case for/against, and what to watch). Also restate the 3-5yr
   historical P/E range from `tldr` here for context — or, where `tldr` point 6 fell back to P/S
   because P/E was unusable, restate the P/S range and lead the `PE Consensus / PB Consensus` line
   with the P/S figures, same as in `tldr` (no sentence explaining why P/E was dropped).
   Unlike section 一, these valuation metrics
   frequently aren't cleanly available — write `"來源資料未說明。"` for any of these six you
   couldn't source rather than omitting the line, since a reader should be able to tell "not
   disclosed" apart from "I forgot to check."
7. `七、Q&A Session` (type `qa`) — first `themes`: 4-6 one-line bullets summarizing the main
   takeaways, **stated as conclusions with the specific numbers/timelines**, not just topic names.
   Pick these using the same "what's actually a highlight" criteria as `tldr` point 3 above
   (incremental disclosure, specific forward commitments, ties to the stock reaction, or a topic
   multiple analysts converged on) — `themes` and the `tldr` Q&A point should agree with each
   other, since they're drawing on the same judgment about what mattered in the call.
   Then `qa`: **cover every substantive question from the transcript**, not a curated handful —
   this section's whole value is completeness. For each pair:
   - `q`: translate to Traditional Chinese, numbered from 1, preserving key terms and any numbers
     cited in the question; leave very basic/universally-known proper nouns in English (company
     names, well-known product names) rather than force a translation nobody uses.
   - `a`: rewrite in **first person**, as if the executive is speaking directly (`"我們認為..."`,
     not `"CFO表示..."` or naming who said it) — no names, no throat-clearing/pleasantries, no
     internal bullet points (one flowing paragraph, not a list), but **do bold the specific
     data/figures/timelines within it** via `a_segments` — the answer should read like a direct,
     confident quote, not a third-person meeting summary.
   - `q` and `a` always render as two separate paragraphs (handled by the script), so don't try
     to merge them into one block yourself.

### Number formatting conventions (match these — inconsistency is the easiest way for a memo to
look wrong even when the facts are right)

- **Currency in body sections** (everything except `tldr`): convert to 億 (hundred-million) units
  regardless of the original currency, 2 decimal places, no space: `344.40億美元` (= $34.44bn).
  This applies throughout sections 一-六 and the `dashboard` tables.
- **Currency in `tldr`**: bilingual shorthand, 1 decimal place, space before the unit:
  `344.4 億 USD`.
- **Percentages**: 1-2 decimal places. In `tldr`, prefix an explicit `+` on positive growth
  figures (`YoY +2.9%`); in body sections, no `+` prefix, just the number (`YoY 2.9%`), but always
  keep an explicit `-` for negative figures.
- **Full-width colon `：`** (not `:`) after every bullet label, e.g. `總營收：`.
- **Parentheses are always half-width `()`, never full-width `（）`** — even around pure-Chinese
  content (`(去年同期16.3%)`, not `（去年同期16.3%）`). This is the one place the reference
  memos consistently break from "full-width for Chinese punctuation" — double check this
  specifically, since it's easy to default to full-width parens out of habit when everything
  else around them is full-width.
- **Basis points**: write `bps`, not `個基點`.
- Keep the original filing's precision when quoting a specific reported figure (don't round a
  reported `264.53億美元` down to `265億美元`).
- Before saving, do one pass over the whole JSON checking for any other full-width/half-width
  slips (e.g. a stray `:` where it should be `：`, or vice versa) — inconsistency within a single
  memo reads as sloppy even when every individual figure is correct.

### P/E calculation for `tldr` and Section 六

`current P/E = 現價 / TTM EPS`（trailing twelve months，用最近四季已公布的 GAAP 或
non-GAAP EPS，依該公司慣用口徑）; `forward P/E = 現價 / 全年財測 EPS 中位數`. State both the
current price and the two EPS figures used so the multiples are reproducible, matching the style
of `目前股價約 47.10 USD 計算...current P/E 約 11.16；以 FY26 EPS mid 4.97 USD 推算，
forward P/E 約 9.48。` in past memos. Precede this with the 3-5yr historical trailing P/E range
(`近 3-5 年歷史 P/E 區間落在 XX-XX 倍`, see Step 2's stock-price-reaction note for where to find
it) — the historical range is what tells a reader whether the current multiple is cheap, average,
or expensive for this specific stock, which a bare current/forward P/E can't convey on its own.

**Falling back to P/S (or P/B).** Two situations make P/E useless as the anchor: the company was
loss-making in some of the last 3-5 years (so there's no continuous historical range — Boeing
FY21-FY24 is the worked example), or TTM EPS contains a large one-off (a tax-valuation release, a
divestiture gain, an impairment) that makes the current multiple meaningless. In either case
compute `current P/S = 現價 / TTM 營收每股` — in practice, `市值 / 近四季營收合計` — pull the
historical P/S range from the same `stockanalysis.com/stocks/{ticker}/financials/ratios/` page that
gives P/E, and **write it in exactly the same shape as the P/E line, with no preamble about why
P/E was dropped**:

```
近 5 年 (FY21–FY25) 歷史 P/S 區間 1.71–2.04 倍，以目前股價 221.56 USD、TTM rev. 939.95 億 USD
計算，current P/S 約 1.85 倍、位於區間中位。
```

Then report current/forward P/E after it as secondary figures, each with a brief parenthetical
naming the distortion (`"current P/E 約 82.7 (TTM 含 4Q25 一次性巨額利益)"`). The user has said
explicitly: if you're going to use P/S, just write the P/S numbers — don't write a sentence saying
P/E doesn't apply. The same applies to P/B for asset-heavy or book-value-driven names, and note
that P/B is itself unusable when shareholders' equity was negative in the comparison years (say so
in a parenthetical, not a sentence). When TTM net income has been negative every year (so P/E is
not just distorted but genuinely never available), skip the "secondary P/E" figures entirely
rather than reporting an N/A line with a full sentence of explanation — a short parenthetical
(`"current/forward P/E 因連年虧損無意義"`) is enough.

## Step 4: 逐數字回源驗證（硬性關卡——通過前不得執行 build_memo.py）

一次生成的長文摘要，「數字本身正確、但掛載錯誤」是**預期行為而不是意外**：數值記得住，
它掛在哪個主詞/期間/口徑上是壓縮時最先斷的環節；而寫錯與寫對的行文流暢度完全相同，
所以「重讀一遍看順不順」抓不到任何一類這種錯。驗證必須是逐條回源比對，不是自我審查。

寫完 JSON 後，逐條處理裡面**每一個數字、專有名詞、因果歸因**（tldr、dashboard、
sections 一～七全部），對每一條核對五個綁定，並把結果寫進
`{專案根目錄}\MEMO\_work\{ticker小寫}_{季度小寫}_check.md`：

| 綁定 | 要核對什麼（每一項都對應 2026-08-13 CSCO/Cerebras 兩份 memo 的真實翻車案例） |
|---|---|
| 數值+單位 | 75,000「個」800G 模組 ≠「75,800 Gbps」——兩個相鄰 token 融接成一個假數字假單位 |
| 主詞 | +95% 是「服務供應商＋雲端」客戶別合計，不是電信商；8,600+ 是 Cisco IQ 的客戶數，不是 RIS |
| 期間 | 280+ 是 Q4 單季、1,000+ 才是全年；35.5-36.5% 是 Q1 指引、全年隱含約 35%；「累計至今出貨」≠「本季出貨」 |
| 口徑 | 地理別 +18% 是營收、+44% 是產品訂單，差一倍以上；ARR/訂閱占比是季末時點數，不是全年數 |
| 歸因 | 原因用公司自己的字眼：逐字稿說 memory cost 就寫記憶體成本，不得代換成更有名的敘事（如關稅） |

check 檔每條的格式：`[狀態] memo 寫法 → 來源原文片段（英文原句，不是你的轉述）`。
**貼不出原文片段的條目就是未通過**——「貼出原文」這個動作本身就是驗證，它強迫你回到
來源文字而不是回到自己的記憶（記憶裡的版本就是當初寫錯的版本）。狀態三選一：

- `相符` — 五個綁定全對。
- `已修正` — 任一綁定錯了：回頭改 JSON，把修正後的寫法記在這條。`已修正 > 0` 是
  正常狀態不是失敗——驗證站存在的目的就是接住這些。
- `無法核對(外部：{來源})` — 只允許出現在本來就不出自逐字稿/新聞稿的資料
  （市場共識、目標價、歷史 P/E 區間、股價），必須註明實際來源。**逐字稿章節
  （tldr 的 Q&A 點、三、五、七、dashboard 焦點表）不允許有無法核對的條目**——
  找不到原文就刪掉或降級改寫。外部來源可抓取時應回源核對後改標
  `相符(外部：{來源})`；真的抓不到才留無法核對——**外部 ≠ 免驗**。

驗證時同步套用這八條（每條都來自真實錯誤，不是假設性防範）：

1. **來源隔離**：「管理層表示／法說會內容」語境只能出自本次逐字稿。WebSearch 撈到的
   新聞（尤其幾個月前的舊聞）不得混進管理層語境——CSCO memo 的「關稅漲價」就是舊新聞
   敘事滲染，逐字稿全篇沒有 tariff 一字，公司明講原因是記憶體成本。另注意**機器轉錄
   逐字稿的「數量＋規格」壓縮陷阱**（"850,400 gig" 實為 850,000 個 400G、"75,800 gig"
   實為 75,000 個 800G）——怪異的數字＋單位組合必須回新聞稿或第二來源核對。
2. **先驗知識禁令**：來源沒出現的競品型號（B200）、效能數字（750 tokens/sec）、
   比較對象，無論多合理都不得出現。逐字稿說「Helios 優於 355s」就寫 MI355X
   （AMD 自家前一代），不升級成「優於 NVIDIA」。
3. **限定句是一級事實，跟著數字走**：excludes / not including / cumulative since /
   "we haven't done X yet" 這類限定，數字走到哪（tldr、dashboard、內文）它跟到哪。
   例：RPO $25B「不含 AWS 或任何 hyperscaler 訂單」；「NVIDIA 存量 GPU 我們還沒做過」。
   壓縮時模型會把數字視為高價值、限定句視為低價值——對投資判讀恰好相反。
4. **禁止縫合**：來源分開講的兩件事不得合併成一個宣稱。「Q2 簽 6 筆 >$30M」與
   「新客戶名單 Figma/Cognition/…」在逐字稿是兩段話，不得寫成「6 筆大單（Figma、
   Cognition 等）」。要並列就照來源的分法寫成兩句。
5. **內部算術自檢**：分項加總＝合計（16.59+15.02=31.61 就不能寫 32.0）；可推導值
   驗算一次（回購金額 ÷ 均價 ＝ 股數）；dashboard 與 sections 數字逐一相同；
   tldr 與內文不互相矛盾；7 個 section 齊全。
6. **稿內矛盾**：準備稿與 Q&A 數字打架時（1,500+ vs 1,600），以準備稿為準、
   括號註記 Q&A 說法。
7. **術語**：技術名詞先查 [references/glossary.md](references/glossary.md)；不在表上的，
   中譯後**首次出現必附英文原文括號**（`分離式推論 (disaggregated inference)`——
   「非結構化推理」這個錯譯就是因為沒帶原文，全篇 15 處無人能發現），並把新詞補進
   glossary。
8. **反向掃描抓遺漏**：memo→來源方向驗完後，反過來把準備稿再掃一次：每個數字、
   每個新產品/路線圖宣告（CS4/CS5 這種等級）、每個總體口徑（如「產品訂單 +35%、
   排除超大規模 +25%」），要嘛在 memo 裡、要嘛列進 check 檔末尾的「未收錄」清單。
   逐條驗證只能抓「寫錯的」，抓不到「沒寫的」——本季最重要的訂單口徑曾整個消失，
   靠的就是這一步。
9. **衍生數必須重算，不得以「外部」豁免**：TTM＝近四季逐季相加、FY 估算＝已公布各季＋
   指引中值——各季數字回**各季自己的新聞稿**核對（公司 IR／GlobeNewswire 都抓得到）；
   並跑恆等式 `FY估算 − TTM ＝ Q4指引中值 − 去年同季實際`，兩邊對不上＝其中一個合成數
   必錯（AMAT 曾寫 13.0−11.5=1.5 但 4.02−2.17=1.85，兩者不可能同時對）。統計網站的
   「EPS (ttm)」是 GAAP 稀釋口徑——標成 non-GAAP 前必先驗口徑（AMAT 曾把 GAAP TTM 11.6
   標成 non-GAAP TTM 11.5，current P/E 少算 2 倍多）；P/E 分母口徑要與標籤及歷史區間
   口徑一致。
10. **評級/目標價用彙整頁＋標擷取日**：優先 `stockanalysis.com/stocks/{ticker}/forecast/`
    （含分析師人數與買賣分佈）；marketbeat instant-alert 這類單一新聞頁不得作為評級唯一
    依據（曾據其把正確的 Strong Buy 改成錯的 Moderate Buy——單一劣質來源的「修正」比
    不修正更危險）。外部 ≠ 免驗：來源可抓取就抓來核對後標 `相符(外部：{來源})`，
    真的抓不到才准標 `無法核對(外部)`。

check 檔開頭寫統計行：`相符 N／已修正 N／無法核對 N／未收錄 N`。全部條目處理完、
JSON 修正完，才執行 build_memo.py。

## Step 5: after generating

Report back to the user: the output path, and a short list of anything you couldn't source
(e.g. "沒有找到公開的逐字稿，Q&A 部分是從法說會簡報的 talking points 整理，不是逐字稿問答").
That gap disclosure matters more than a complete-looking-but-partly-fabricated memo.

Then log the run so there's a standing record of what's been generated without needing to ask —
the user's way of checking "what have you made" is opening this file, not re-asking in chat:

```
python scripts/log_run.py --ticker VZ --quarter 1Q26 --status "完成" --notes "無" --path "{專案根目錄}\MEMO\1Q26\VZ 1Q26.docx"
```

Use `--status "部分完成(缺逐字稿Q&A)"` (or similar) and put the specifics in `--notes` whenever
Step 4 turned up a gap — the log should surface the same caveats you'd say in chat, not a
sanitized "完成" that hides them. `--notes` always leads with Step 4's verification stats line
(`驗證：相符 N／已修正 N／無法核對 N／未收錄 N`) before any other caveats. This writes to `{專案根目錄}\MEMO\_generated_log.md` by default, newest
run on top.

**Do not stop here — continue straight to Step 6 (podcast) for the same ticker/quarter.** The
memo alone is not a complete run.

## Step 6: generate the podcast audio via NotebookLM

**Two execution contexts — pick by what tools you actually have.**

**(a) Interactive (Claude desktop app):** you have `mcp__claude-in-chrome__*`, so drive the
NotebookLM web UI as described below. This is the original path, confirmed working end-to-end
on VST 2Q26, 2026-08-10.

**(b) Headless (`claude -p`, e.g. the scheduled dispatcher):** you have **no browser MCP** —
`claude mcp list` shows only Drive / Gmail / Calendar / Slack. The web-UI steps below are
impossible; do not attempt them, and do not fake the output. **Skip this step entirely.**
Podcast generation is handled after you by
`美股\automation\tools\make_podcast.py`, which uses the `notebooklm` CLI with settings
equivalent to the web UI (`--format deep-dive --length short --language zh_Hant`) and derives
the focus prompt from your memo JSON's `tldr`. Verified end-to-end on CEG 2Q26, 2026-08-11
(5.3 min / 9.7 MB, Traditional Chinese, title 「CEG併購Calpine的財務障眼法」).

One consequence worth knowing in both contexts: NotebookLM fetches sources server-side with no
user session, so **paywalled transcripts (Seeking Alpha) cannot be used as sources** — the CLI
path drops them automatically. When only the press release is available the episode leans on
reported figures and covers the Q&A thinly.

**This is a standing part of every run, not an optional extra — produce the actual podcast audio
yourself, don't just note where it should eventually go.** (In context (b), "produce" means
leaving a correct memo JSON for the CLI step; the audio itself is not yours to make.)

1. **Create a notebook.** Navigate to `https://notebooklm.google.com/`, click 建立新的筆記本, then
   rename the notebook (click the title, type, Enter) to `{TICKER} {quarter}` — same label used
   for the memo filename (Step 3's fiscal label), e.g. `VST 2Q26`.
2. **Add sources by URL**, not file upload — click 新增來源 → 網站 (the "網站與 YouTube 網址"
   option), paste one URL per line (space/newline-separated, multiple at once), then 插入.
   Reuse the same source URLs already found in Step 2 (press release, transcript, presentation).
   **Paywalled sources fail here** — NotebookLM fetches server-side with no user session, so
   Seeking Alpha and similar logged-in-only pages get flagged with a red error icon and silently
   excluded from generation, even though WebFetch/browser-tools could read them earlier in Step 2.
   Stick to the free/mirror sources: the IR press release, a PR Newswire/GlobeNewswire/Business
   Wire mirror, and free transcript sources (Yahoo Finance, Motley Fool, the company's own webcast
   page). After inserting, check the source list in the left panel for any red error icon before
   moving on — don't assume every pasted URL made it in.
3. **Open the Audio Overview panel** — click 語音摘要 in the 工作室 (Studio) panel on the right,
   which opens the 自訂語音摘要 dialog. Set, in order:
   - **格式 (format) = 深入探索 (Deep Dive)** — this is the default selection, but confirm it;
     never Brief/摘要, Critique/評論, or Debate/辯論 unless the user asks otherwise.
   - **選擇語言 (language) = 中文（繁體）** — the dropdown list is sorted with CJK languages
     (中文（簡體）, 中文（繁體）, 日本語) grouped **above** the Latin-alphabet languages (open
     the dropdown, it lands scrolled near "English" — scroll UP a little to find Chinese, not
     down toward Korean; scrolling down leads away from it).
   - **長度 (length) = 短**.
   - **Focus prompt** (the "在本集中，AI 主持人應著重哪些部分？" box) — write 2-4 sentences in
     Chinese naming this quarter's specific headline financials, segment/strategic highlights, and
     guidance, so the episode isn't generic. Base it on the same facts already gathered for the
     memo's `tldr`.
   - Click 生成.
4. **Wait for generation** — this takes roughly 5 minutes for 深入探索 even at 短 length. Poll
   with periodic screenshots (every 30-60s) rather than assuming a short fixed wait; the Studio
   panel shows "正在生成語音摘要...請稍候幾分鐘" while running and the finished episode card
   (title, duration, format) once done.
5. **Download it** — click the finished episode's ⋮ (more) menu in the Studio panel → 下載.
   **This downloads through the user's real Chrome browser, landing in their actual Windows
   Downloads folder (`%USERPROFILE%\Downloads`) — not the sandbox outputs folder.**
6. **Rename it in place — and leave it in Downloads.** NotebookLM names the file after the
   Chinese episode title with spaces replaced by underscores (e.g.
   `Vistra_攜手_NVIDIA_掌握_AI_電力命脈.m4a`), which is unrecognisable next to the memo. Rename it
   to `{TICKER} {fiscal_label}.m4a` — the same fiscal label used for the memo filename in Step 3,
   e.g. `VST 2Q26.m4a`, `SWKS 3Q26FY.m4a`.

   **Do not move the file anywhere.** It stays in the Downloads folder; the user files it
   themselves. There is no `PODCAST\` folder step any more — renaming is the whole of this step.

   Prefer `mcp__Windows-MCP__FileSystem` (load via ToolSearch if deferred): `mode: search` with a
   pattern matching the ticker or Chinese company name to locate the just-downloaded file, then
   `mode: move` with the **same Downloads directory** and the new filename — a same-folder move is
   a rename.

   **If Windows-MCP isn't available in this session**, fall back to the `mcp__computer-use__*`
   tools (request access to "File Explorer" once per session): select the file in Downloads, press
   `F2`, type the new name, Enter. Explorer's rename box selects the extension along with the
   name, so typing over it silently drops `.m4a` unless you re-append it — if Explorer prompts
   "the file might become unusable", you dropped the extension; cancel and redo it keeping `.m4a`.
   No cut/paste is involved any more, so the old clipboard-overwrite hazard no longer applies.
7. Confirm the rename succeeded (`mode: list` on the Downloads folder, or a fresh screenshot)
   before reporting back, and give the user the full renamed path
   (`%USERPROFILE%\Downloads\{TICKER} {fiscal_label}.m4a`) so they can find it immediately.

If a source genuinely has no free/fetchable equivalent (e.g. a transcript exists only behind
Seeking Alpha's paywall), proceed with whatever sources did load rather than blocking the whole
podcast step on it — a Deep Dive with 2 solid sources beats no podcast at all — but mention the
gap when reporting back, the same way Step 4/5 discloses memo gaps.

## Running unattended (scheduled/batch mode)

This skill is sometimes invoked from a scheduled task the night before a batch of earnings calls,
with nobody watching when it actually runs — e.g. "these 5 companies report tomorrow before the
open, build all 5 once transcripts are out." A few things change when there's no one to redirect
you mid-run:

- **The scheduled task's prompt must be fully self-contained** — it starts with zero memory of
  the conversation that set it up. Spell out every ticker, its quarter label, and where to save
  each file; don't reference "the companies we discussed."
- **Transcripts don't land on a predictable clock.** The press release is usually on time, but a
  clean call transcript from a free source can lag the call by anywhere from ~30 minutes to a few
  hours depending on the outlet. A single fire-once task risks running before transcripts exist.
  Prefer a short retry window over a single shot: e.g. a recurring cron pinned to one specific
  date (`*/15 6-9 <day> <month> *`) that, on each fire, builds whichever of the N memos aren't
  done yet and skips ones already completed, then calls `update_scheduled_task` (or
  `delete_scheduled_task`) on itself to stop once all N are done or a stated cutoff (e.g. 9am)
  passes.
- **At the cutoff, finish with what you have rather than stall.** If a transcript still isn't up
  by the deadline, build the memo without section 七's full Q&A (themes only, or omit with a
  clear note) instead of holding up the ones that are ready. Same principle for Step 6's podcast:
  generate it from whatever free sources did load rather than blocking on a transcript that never
  landed.
- **Nobody's there to read a chat reply, so Step 5's report needs a durable home.** Call
  `log_run.py` for each of the N tickers as it finishes (same as any interactive run) — the
  shared `{專案根目錄}\MEMO\_generated_log.md` is what makes a batch run visible after the fact, so there's
  no separate batch-only summary file to maintain. Send one `PushNotification` at the very end
  (after the last ticker is done or the cutoff passes) so the user knows to go check the log —
  don't rely on conversational output nobody will read until later.
- **Where a run ends depends on who started it.** An interactive batch you drive yourself ends at
  save-to-`{專案根目錄}\MEMO\` plus the renamed `.m4a` in Downloads, plus a
  notification. A run started by the scheduled dispatcher does **not** end there: after you
  finish the memo, `美股\automation\` takes over and
  (1) generates the podcast via the `notebooklm` CLI into `美股\PODCAST\{quarter}\`,
  (2) uploads both to Google Drive, (3) writes the memo into a new tab of the
  `{TICKER} 財報` Google Doc with the podcast link, and (4) ticks 報告/Podcast in the
  tracking sheet. So in that context, do not treat "file saved locally" as the finish line and do
  not duplicate any of those steps — just leave correct files at the conventional paths.

## Reference

- [references/schema.md](references/schema.md) — full JSON schema for `build_memo.py`, with a
  complete worked example (VZ 1Q26).
- [scripts/pdf_page_to_image.py](scripts/pdf_page_to_image.py) — renders one PDF page to a
  cropped PNG for the income statement screenshot.
- [scripts/is_shot.py](scripts/is_shot.py) — pulls the income-statement table straight out of an
  HTML release (SEC EX-99.1, PR Newswire, …) and renders it to a cropped PNG via headless Chrome.
  Prints the candidate tables it considered; `--index N` overrides the pick.
- [scripts/insert_is.py](scripts/insert_is.py) — drops an income-statement PNG + caption into an
  already-built .docx without altering a single character of text. Use when the memo has been
  hand-edited since it was generated. No-ops if the file already has an image.
- [scripts/log_run.py](scripts/log_run.py) — appends a row to `{專案根目錄}\MEMO\_generated_log.md` after
  every run (see Step 5).

