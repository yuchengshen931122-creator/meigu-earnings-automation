"""產生流程看板 HTML —— 一眼看出每檔卡在哪個階段。

資料源全部是活的：
  追蹤表      ticker / 日期 / 台灣時間 / 報告勾選 / Podcast 勾選 / 分工 / 產業
  檢查點      七階段各自的完成狀態（state/checkpoints/*.json）
  工作排程器  三個任務的下次執行時間

用法：
    python tools/dashboard.py                 # 產出 state/dashboard.html
    python tools/dashboard.py --open          # 產完直接開啟
"""

import _bootstrap  # noqa: F401

import argparse
import datetime as dt
import html
import json
import subprocess
import webbrowser
from pathlib import Path

from core import checkpoint as cp
from core.gauth import load_config, load_credentials
from core.sheets_client import SheetsClient

# 階段定義：key, 標題, 使用者語彙的說明, 語意色
LANES = [
    ("confirming", "確認財報日期中", "公司尚未公告，每天重查", "wait"),
    ("scheduled", "日期已確認・等待開會", "時間到了才會進入下一關", "idle"),
    ("awaiting", "等待資料就緒", "已開會，等新聞稿與逐字稿", "wait"),
    ("ready", "可製作", "資料齊備，排隊等開工", "idle"),
    ("producing", "製作中", "agent 正在做（可並行，上限 run.max_parallel）", "active"),
    ("archiving", "待歸檔", "memo 好了，等上傳與分頁", "active"),
    ("podcast", "缺 podcast", "memo 完成，音檔待人工產出", "attn"),
    ("done", "已完成", "memo + podcast 皆完成", "done"),
]


def classify(task, ck, now: dt.datetime) -> str:
    if task.report_done and task.podcast_done:
        return "done"
    if task.report_done and not task.podcast_done:
        return "podcast"
    if task.call_at is None:
        return "confirming"
    if task.call_at > now:
        return "scheduled"
    if ck and ck.is_done("memo_built"):
        return "archiving"
    # 製作中 = 已開工但 memo 還沒產出。全線同時只會有一檔。
    if ck and ck.in_progress:
        return "producing"
    if ck and ck.is_done("readiness"):
        return "ready"
    return "awaiting"


def load_auth_state(cfg) -> dict | None:
    """auth_monitor（排程 MeiguAuthMon，每 10 分鐘）寫入的授權狀態。

    讀不到就回 None，面板會顯示「監控尚未啟動」—— 不拋錯，
    授權監控壞掉不該把整個看板拖下水。
    """
    p = Path(cfg["local"]["state_dir"]) / "auth_status.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _auth_section(auth: dict | None, now: dt.datetime) -> str:
    """五盞授權燈 + 資料新鮮度。正常綠、預警黃、異常紅；狀態檔太舊整排提醒。"""
    if not auth or not auth.get("items"):
        return ('<div class="scroll"><div class="empty">尚無授權監控資料 —— '
                '排程 MeiguAuthMon 每 10 分鐘寫入一次，或手動執行 '
                '<code>python tools/auth_monitor.py</code></div></div>')

    interval = int(auth.get("interval_minutes") or 10)
    age_txt, stale = "檢查時間不明", True
    try:
        mins = int((now - dt.datetime.fromisoformat(auth["checked_at"]))
                   .total_seconds() // 60)
        age_txt = "剛剛檢查" if mins < 1 else f"{mins} 分鐘前檢查"
        stale = mins > interval * 2 + 5
    except (KeyError, ValueError):
        pass

    cards = []
    for it in (auth["items"] or {}).values():
        if not it.get("ok"):
            c, st = "var(--attn)", "異常"
        elif it.get("warn"):
            c, st = "var(--active)", "注意"
        else:
            c, st = "var(--done)", "正常"
        note = "（這一輪剛自動修復）" if it.get("repaired") else ""
        cards.append(
            f'<div class="auth" style="--c:{c}">'
            f'<div class="row"><span class="dot"></span><b>{esc(it.get("label"))}</b>'
            f'<span class="st">{st}</span></div>'
            f'<div class="d">{esc(it.get("detail"))}{esc(note)}</div></div>'
        )

    if stale:
        meta = (f'<p class="auth-meta warn">最後一次檢查是 {esc(age_txt)}'
                f'（排程是每 {interval} 分鐘）—— 授權監控本身可能停了，'
                f'上面的燈號不可信</p>')
    else:
        meta = (f'<p class="auth-meta">{esc(age_txt)} · 每 {interval} 分鐘'
                f'自動檢查，可修復的（token refresh／session 保溫）當場修，'
                f'修不好的即時推 LINE</p>')
    return f'<div class="auths">{"".join(cards)}</div>{meta}'


def scheduler_state() -> list[dict]:
    out = []
    for name, label, cadence in (
        ("MeiguDispatch", "派工", "每 30 分鐘"),
        ("MeiguDates", "日期更新", "每天 08:00"),
        ("MeiguWatchdog", "健康檢查", "每小時"),
    ):
        nxt, status = "—", "未註冊"
        try:
            r = subprocess.run(["schtasks", "/query", "/tn", name, "/fo", "list"],
                               capture_output=True, text=True, timeout=25,
                               encoding="utf-8", errors="replace")
            for line in (r.stdout or "").splitlines():
                if "Next Run Time:" in line:
                    nxt = line.split(":", 1)[1].strip()
                elif line.strip().startswith("Status:"):
                    status = line.split(":", 1)[1].strip()
        except Exception:
            pass
        out.append({"label": label, "cadence": cadence, "next": nxt, "status": status})
    return out


CSS = """
:root{
  /* 中性色刻意帶一點冷偏，不用純灰 —— 純中性讀起來像沒選過 */
  --bg:#f7f8fa; --surface:#ffffff; --surface-2:#f0f3f6;
  --ink:#16202b; --ink-2:#4a5666; --ink-3:#7b8797;
  --line:#dde3ea; --line-2:#eaeef3;
  --accent:#1f5f8b;
  --done:#2f7d5a; --active:#b06f10; --wait:#5b6674; --idle:#8894a3; --attn:#b3402f;
  --radius:10px;
  --mono:ui-monospace,"SF Mono","Cascadia Mono","JetBrains Mono",Consolas,monospace;
  --cjk:"PingFang TC","Microsoft JhengHei","Noto Sans TC","Hiragino Sans TC",sans-serif;
  --sans:system-ui,-apple-system,"Segoe UI",var(--cjk);
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0f151c; --surface:#161e27; --surface-2:#1c2630;
    --ink:#e6ecf3; --ink-2:#a8b4c2; --ink-3:#76838f;
    --line:#26313d; --line-2:#1e2832;
    --accent:#63a6d4;
    --done:#5eb187; --active:#d9a54a; --wait:#8b98a8; --idle:#67737f; --attn:#e0765f;
  }
}
:root[data-theme="dark"]{
  --bg:#0f151c; --surface:#161e27; --surface-2:#1c2630;
  --ink:#e6ecf3; --ink-2:#a8b4c2; --ink-3:#76838f;
  --line:#26313d; --line-2:#1e2832;
  --accent:#63a6d4;
  --done:#5eb187; --active:#d9a54a; --wait:#8b98a8; --idle:#67737f; --attn:#e0765f;
}
:root[data-theme="light"]{
  --bg:#f7f8fa; --surface:#ffffff; --surface-2:#f0f3f6;
  --ink:#16202b; --ink-2:#4a5666; --ink-3:#7b8797;
  --line:#dde3ea; --line-2:#eaeef3;
  --accent:#1f5f8b;
  --done:#2f7d5a; --active:#b06f10; --wait:#5b6674; --idle:#8894a3; --attn:#b3402f;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:var(--sans);font-size:15px;line-height:1.55;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:28px 22px 64px}
.num{font-variant-numeric:tabular-nums}

header.page{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end;
  justify-content:space-between;padding-bottom:18px;border-bottom:2px solid var(--ink);
  margin-bottom:22px}
h1{margin:0;font-size:25px;letter-spacing:-.015em;text-wrap:balance;font-weight:650}
.sub{margin:4px 0 0;color:var(--ink-2);font-size:13.5px}
.stamp{text-align:right;font-size:12px;color:var(--ink-3);
  font-family:var(--mono);font-variant-numeric:tabular-nums}

.summary{display:grid;gap:1px;background:var(--line);
  grid-template-columns:repeat(auto-fit,minmax(132px,1fr));
  border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;margin-bottom:26px}
.stat{background:var(--surface);padding:13px 15px}
.stat .k{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-3)}
.stat .v{font-size:27px;font-weight:600;line-height:1.15;margin-top:3px;
  font-variant-numeric:tabular-nums}
.stat .n{font-size:11.5px;color:var(--ink-3);margin-top:1px}

h2{font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:var(--ink-3);
  margin:32px 0 12px;font-weight:600}

.lanes{display:grid;gap:12px}
.lane{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  overflow:hidden}
.lane-head{display:flex;align-items:baseline;gap:10px;padding:11px 14px;
  background:var(--surface-2);border-bottom:1px solid var(--line);border-left:4px solid var(--c)}
.lane-head .t{font-weight:600;font-size:14.5px}
.lane-head .c{font-family:var(--mono);font-size:13px;color:var(--c);font-weight:600;
  font-variant-numeric:tabular-nums}
.lane-head .d{color:var(--ink-3);font-size:12.5px;margin-left:auto;text-align:right}
.chips{display:flex;flex-wrap:wrap;gap:7px;padding:13px 14px}
.chip{display:inline-flex;align-items:center;gap:7px;background:var(--surface-2);
  border:1px solid var(--line);border-left:3px solid var(--c);
  border-radius:6px;padding:4px 9px 4px 8px;font-size:13px}
.chip b{font-family:var(--mono);font-weight:600;letter-spacing:.01em}
.chip span{color:var(--ink-3);font-size:11.5px;font-variant-numeric:tabular-nums}
.empty{padding:13px 14px;color:var(--ink-3);font-size:13px;font-style:italic}

table{width:100%;border-collapse:collapse;font-size:13.5px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:var(--radius);
  background:var(--surface)}
th{text-align:left;font-size:11px;letter-spacing:.07em;text-transform:uppercase;
  color:var(--ink-3);font-weight:600;padding:10px 13px;border-bottom:1px solid var(--line);
  background:var(--surface-2);white-space:nowrap}
td{padding:9px 13px;border-bottom:1px solid var(--line-2);vertical-align:top}
tr:last-child td{border-bottom:none}
td.t{font-family:var(--mono);font-weight:600;white-space:nowrap}
td.n{font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--ink-2)}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;
  border:1px solid currentColor;font-weight:500}

.auths{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(215px,1fr))}
.auth{display:flex;flex-direction:column;gap:4px;background:var(--surface);
  border:1px solid var(--line);border-left:4px solid var(--c);
  border-radius:var(--radius);padding:10px 13px}
.auth .row{display:flex;align-items:center;gap:8px}
.auth .dot{width:8px;height:8px;border-radius:50%;background:var(--c);flex:none}
.auth b{font-size:13.5px}
.auth .st{margin-left:auto;font-size:11px;font-weight:600;color:var(--c);
  letter-spacing:.06em}
.auth .d{color:var(--ink-2);font-size:12px;line-height:1.5;overflow-wrap:anywhere}
.auth-meta{margin:9px 2px 0;font-size:12px;color:var(--ink-3)}
.auth-meta.warn{color:var(--attn);font-weight:600}

.bars{display:flex;flex-direction:column;gap:7px}
.bar{display:grid;grid-template-columns:112px 1fr 40px;gap:11px;align-items:center;
  font-size:13px}
.bar .track{height:8px;background:var(--surface-2);border-radius:99px;overflow:hidden;
  border:1px solid var(--line-2)}
.bar .fill{height:100%;background:var(--c);border-radius:99px}
.bar .v{text-align:right;font-variant-numeric:tabular-nums;color:var(--ink-2);
  font-family:var(--mono);font-size:12px}

footer{margin-top:38px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--ink-3);font-size:12.5px}
footer code{font-family:var(--mono);font-size:12px;background:var(--surface-2);
  padding:1px 5px;border-radius:4px;border:1px solid var(--line-2)}
@media (max-width:640px){
  .wrap{padding:20px 14px 48px}
  h1{font-size:21px}
  .bar{grid-template-columns:84px 1fr 36px}
}
"""

SEM = {"done": "var(--done)", "active": "var(--active)", "wait": "var(--wait)",
       "idle": "var(--idle)", "attn": "var(--attn)"}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _chip_note(ck, lane: str) -> str:
    """卡片上的小註記 —— 只標會影響判讀的事，不是每檔都掛東西。"""
    if not ck:
        return ""
    if lane == "ready":
        d = ck.detail("memo_started")
        if d.get("gave_up"):
            return '<span style="color:var(--attn)">已放棄 · 需人工</span>'
        if d.get("quota_blocked"):
            return '<span>等額度</span>'
        if ck.detail("readiness").get("state") == "ready_partial":
            return '<span>逐字稿未確認</span>'
    if lane == "producing":
        n = int(ck.detail("memo_started").get("attempts") or 1)
        if n > 1:
            return f'<span>第 {n} 次</span>'
    return ""


def build(tasks, cks, sched, quarter: str, now: dt.datetime,
          auth: dict | None = None) -> str:
    groups = {k: [] for k, *_ in LANES}

    # 檢查點配對：先精確配（ticker, 看板分頁季度），配不到再用 call_at 相同的
    # 任一季度檔案退路 —— 非曆年財年公司（COHR/SMCI…）的檢查點叫 2026Q4，
    # 用曆年分頁名 2026Q2 永遠配不到，會把「製作中」誤判成「等待資料就緒」
    # （2026-08-13 實測：三檔並行只顯示一檔）。call_at 相同 = 同一場法說會，
    # 不會誤抓到同 ticker 上一季的舊檔案。
    by_ticker: dict[str, list] = {}
    for (tk, _q), c in cks.items():
        by_ticker.setdefault(tk, []).append(c)

    def _find_ck(t):
        exact = cks.get((t.ticker, quarter))
        if exact:
            return exact
        if not t.call_at:
            return None
        want = t.call_at.isoformat()
        for c in by_ticker.get(t.ticker, []):
            if (c.detail("date_confirmed") or {}).get("call_at") == want:
                return c
        return None

    for t in tasks:
        ck = _find_ck(t)
        groups[classify(t, ck, now)].append((t, ck))

    total = len(tasks)
    done_n = len(groups["done"])
    active_n = len(groups["producing"]) + len(groups["archiving"])
    pending_n = total - done_n

    # ---- 摘要 ----
    stats = [
        ("追蹤總數", total, f"{quarter} 這一季"),
        ("已完成", done_n, f"{done_n * 100 // total if total else 0}% 的追蹤清單"),
        ("處理中", active_n, "正在產出或歸檔"),
        ("排隊可製作", len(groups["ready"]), "資料齊備，等前一檔做完"),
        ("待處理", pending_n, "尚未走完流程"),
        ("缺 podcast", len(groups["podcast"]), "memo 好了、音檔待補"),
        ("待確認日期", len(groups["confirming"]), "公司尚未公告"),
    ]
    summary = "".join(
        f'<div class="stat"><div class="k">{esc(k)}</div>'
        f'<div class="v">{v}</div><div class="n">{esc(n)}</div></div>'
        for k, v, n in stats
    )

    # ---- 泳道 ----
    lanes = []
    for key, title, desc, sem in LANES:
        items = groups[key]
        c = SEM[sem]
        if key in ("scheduled", "confirming", "awaiting"):
            items.sort(key=lambda x: (x[0].call_at or dt.datetime.max))
        elif key == "ready":
            # 依派工順序排 —— 就緒時間早的先，同時間用列序，跟 dispatcher 一致。
            # 這一欄由上而下就是接下來會被做的順序。
            items.sort(key=lambda x: (x[1].at("readiness") if x[1] else "9999", x[0].row))
        else:
            items.sort(key=lambda x: x[0].ticker)

        if items:
            chips = "".join(
                f'<span class="chip" style="--c:{c}"><b>{esc(t.ticker)}</b>'
                + (f'<span>{t.call_at:%m/%d %H:%M}</span>' if t.call_at else "")
                + _chip_note(ck, key)
                + "</span>"
                for t, ck in items
            )
            body = f'<div class="chips">{chips}</div>'
        else:
            body = '<div class="empty">目前沒有</div>'

        lanes.append(
            f'<section class="lane"><div class="lane-head" style="--c:{c}">'
            f'<span class="t">{esc(title)}</span>'
            f'<span class="c" style="--c:{c}">{len(items)}</span>'
            f'<span class="d">{esc(desc)}</span></div>{body}</section>'
        )

    # ---- 未來 7 日 ----
    soon = sorted(
        [t for t in tasks if t.call_at and now <= t.call_at <= now + dt.timedelta(days=7)],
        key=lambda t: t.call_at,
    )
    if soon:
        rows = "".join(
            f'<tr><td class="t">{esc(t.ticker)}</td>'
            f'<td class="n">{t.call_at:%m/%d}（{"一二三四五六日"[t.call_at.weekday()]}）</td>'
            f'<td class="n">{t.call_at:%H:%M}</td>'
            f'<td class="n">{_countdown(t.call_at - now)}</td>'
            f'<td>{esc(t.industry)}</td>'
            f'<td>{esc(t.owner) or "<span style=\'color:var(--ink-3)\'>未指派</span>"}</td></tr>'
            for t in soon
        )
        upcoming = (
            '<div class="scroll"><table><thead><tr>'
            "<th>代號</th><th>日期</th><th>台灣時間</th><th>倒數</th>"
            "<th>產業</th><th>分工</th></tr></thead><tbody>"
            f"{rows}</tbody></table></div>"
        )
    else:
        upcoming = '<div class="scroll"><div class="empty">未來 7 日沒有排定的財報</div></div>'

    # ---- 七階段進度 ----
    stage_rows = []
    n_ck = len(cks) or 1
    for k, label, _hint in cp.STAGES:
        n = sum(1 for c in cks.values() if c.is_done(k))
        pct = n * 100 // n_ck
        stage_rows.append(
            f'<div class="bar" style="--c:var(--accent)"><span>{esc(label)}</span>'
            f'<span class="track"><span class="fill" style="width:{pct}%"></span></span>'
            f'<span class="v">{n}/{n_ck}</span></div>'
        )

    # ---- 排程 ----
    sched_rows = "".join(
        f'<tr><td class="t">{esc(s["label"])}</td><td>{esc(s["cadence"])}</td>'
        f'<td class="n">{esc(s["next"])}</td>'
        f'<td><span class="pill" style="color:'
        f'{"var(--done)" if s["status"] == "Ready" else "var(--attn)"}">'
        f'{esc(s["status"])}</span></td></tr>'
        for s in sched
    )

    return f"""<div class="wrap">
<header class="page">
  <div>
    <h1>美股財報流程看板</h1>
    <p class="sub">{esc(quarter)}　·　追蹤 {total} 檔　·　資料取自追蹤表、檢查點與工作排程器</p>
  </div>
  <div class="stamp">產生於 {now:%Y-%m-%d %H:%M}<br>台灣時間</div>
</header>

<div class="summary">{summary}</div>

<h2>授權狀態</h2>
{_auth_section(auth, now)}

<h2>流程階段</h2>
<div class="lanes">{"".join(lanes)}</div>

<h2>未來 7 日財報</h2>
{upcoming}

<h2>七階段完成度（{n_ck} 檔有檢查點）</h2>
<div class="bars">{"".join(stage_rows)}</div>

<h2>排程狀態</h2>
<div class="scroll"><table><thead><tr>
<th>任務</th><th>頻率</th><th>下次執行</th><th>狀態</th>
</tr></thead><tbody>{sched_rows}</tbody></table></div>

<footer>
  派工每 30 分鐘重讀追蹤表，新增或變更的日期會在下一輪自動納入 —— 不需要為每檔股票各設一個排程。<br>
  重新產生此頁：<code>python tools/dashboard.py</code>　·
  文字版看板：<code>python tools/status.py --stuck</code>
</footer>
</div>"""


def _countdown(delta: dt.timedelta) -> str:
    h = int(delta.total_seconds() // 3600)
    if h < 1:
        return f"{int(delta.total_seconds() // 60)} 分"
    if h < 48:
        return f"{h} 小時"
    return f"{h // 24} 天"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--open", dest="do_open", action="store_true")
    ap.add_argument("--fragment", action="store_true",
                    help="只輸出 body 內容（給 Artifact 用，它會自己包 doctype/head）")
    args = ap.parse_args()

    cfg = load_config()
    now = dt.datetime.now()
    s = SheetsClient(load_credentials(interactive=False), cfg)
    tasks = s.load_tasks(year=now.year)
    quarter = s.resolve_tab()
    cks = {(c.ticker, c.quarter): c for c in cp.load_all(cfg["local"]["state_dir"])}

    body = build(tasks, cks, scheduler_state(), quarter, now,
                 auth=load_auth_state(cfg))
    if args.fragment:
        page = f"<style>{CSS}</style>{body}"
        out = Path(args.out) if args.out else Path(cfg["local"]["state_dir"]) / "dashboard_fragment.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, encoding="utf-8")
        print(f"[OK] {out}  ({len(page) // 1024} KB，{len(tasks)} 檔)")
        return 0

    page = (
        "<!doctype html><html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>美股財報流程看板 · {quarter}</title><style>{CSS}</style></head>"
        f"<body>{body}</body></html>"
    )

    out = Path(args.out) if args.out else Path(cfg["local"]["state_dir"]) / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"[OK] {out}  ({len(page) // 1024} KB，{len(tasks)} 檔)")
    if args.do_open:
        webbrowser.open(out.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
