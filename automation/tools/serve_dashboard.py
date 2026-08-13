"""常駐看板服務 —— 試算表一改，看板就跟著改。

為什麼要這支，而不是把靜態 HTML 貼上去：

  · 線上 Artifact 受 CSP 限制不能打 Google API，而且憑證也不該放進網頁；
    這個 session 也沒有可用的 claude.ai 連接器，所以線上頁做不到即時。
  · 憑證在本機，所以由本機服務現讀試算表最直接。
  · 不論是人在 Google Sheets 上打勾，或是 dispatcher 回寫 auto_status，
    下一次輪詢就會反映出來 —— 兩種來源一視同仁，因為讀的是同一份真相。

架構：
    GET /            外殼頁面 + 輪詢用的 JS
    GET /fragment    現讀資料、伺服器端算好的看板 HTML（有快取）
    GET /health      給監控用的簡短狀態

前端只換 innerHTML，不整頁重載 —— 捲動位置與深淺色主題不會被打斷。

快取策略（避免每 15 秒打爆 Sheets API 與 schtasks）：
    試算表     15 秒
    排程器狀態  120 秒（schtasks 是三個 subprocess，很慢）
"""

import _bootstrap  # noqa: F401

import argparse
import datetime as dt
import json
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import checkpoint as cp
from core.gauth import load_config, load_credentials
from core.sheets_client import SheetsClient
from tools.dashboard import CSS, build, load_auth_state, scheduler_state

SHEET_TTL = 15
SCHED_TTL = 120


class State:
    """快取 + 執行緒安全。多個瀏覽器分頁同時輪詢也只會打一次 API。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self._sheet = None
        self._sheet_at = 0.0
        self._sched = None
        self._sched_at = 0.0
        self._client = None
        self.last_error: str | None = None
        self.refreshes = 0

    def client(self) -> SheetsClient:
        if self._client is None:
            self._client = SheetsClient(load_credentials(interactive=False), self.cfg)
        return self._client

    def sheet(self):
        now = time.time()
        with self.lock:
            if self._sheet is None or now - self._sheet_at > SHEET_TTL:
                c = self.client()
                self._sheet = (c.load_tasks(year=dt.datetime.now().year), c.resolve_tab())
                self._sheet_at = now
                self.refreshes += 1
            return self._sheet

    def sched(self):
        now = time.time()
        with self.lock:
            if self._sched is None or now - self._sched_at > SCHED_TTL:
                self._sched = scheduler_state()
                self._sched_at = now
            return self._sched

    def fragment(self) -> str:
        tasks, quarter = self.sheet()
        # 檢查點與授權狀態都是本機小檔，每次都重讀 ——
        # dispatcher / auth_monitor 寫入後要立刻看得到
        cks = {(c.ticker, c.quarter): c for c in cp.load_all(self.cfg["local"]["state_dir"])}
        return build(tasks, cks, self.sched(), quarter, dt.datetime.now(),
                     auth=load_auth_state(self.cfg))


SHELL = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>美股財報流程看板</title><style>__CSS__
.live{position:fixed;right:14px;bottom:14px;display:flex;align-items:center;gap:7px;
  background:var(--surface);border:1px solid var(--line);border-radius:99px;
  padding:6px 13px 6px 10px;font-size:12px;color:var(--ink-2);
  box-shadow:0 2px 10px rgba(0,0,0,.07);font-variant-numeric:tabular-nums;z-index:9}
.live .dot{width:7px;height:7px;border-radius:50%;background:var(--done);flex:none}
.live.stale .dot{background:var(--active)}
.live.err .dot{background:var(--attn)}
@media (prefers-reduced-motion:no-preference){
  .live .dot{animation:p 2.4s ease-in-out infinite}
  @keyframes p{0%,100%{opacity:1}50%{opacity:.35}}
}
</style></head><body>
<div id="root">__INITIAL__</div>
<div class="live" id="live"><span class="dot"></span><span id="ago">連線中</span></div>
<script>
const POLL = __POLL__ * 1000;
let last = Date.now(), fails = 0;
const live = document.getElementById('live'), ago = document.getElementById('ago');

async function tick(){
  try{
    const r = await fetch('/fragment', {cache:'no-store'});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    document.getElementById('root').innerHTML = await r.text();
    last = Date.now(); fails = 0;
    live.className = 'live';
  }catch(e){
    fails++;
    live.className = 'live err';
    ago.textContent = '連線失敗 ×' + fails;
    return;
  }
}
function label(){
  if(fails) return;
  const s = Math.round((Date.now()-last)/1000);
  ago.textContent = s < 5 ? '即時' : s + ' 秒前更新';
  live.className = s > POLL/1000*3 ? 'live stale' : 'live';
}
setInterval(tick, POLL);
setInterval(label, 1000);
tick();
</script></body></html>"""


def make_handler(state: State, poll: int):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, body: str, ctype="text/html; charset=utf-8", code=200):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path == "/fragment":
                    self._send(state.fragment())
                elif path == "/health":
                    self._send(json.dumps({
                        "ok": state.last_error is None,
                        "refreshes": state.refreshes,
                        "error": state.last_error,
                    }, ensure_ascii=False), "application/json; charset=utf-8")
                elif path in ("/", "/index.html"):
                    page = (SHELL.replace("__CSS__", CSS)
                                 .replace("__INITIAL__", state.fragment())
                                 .replace("__POLL__", str(poll)))
                    self._send(page)
                else:
                    self._send("404", code=404)
                state.last_error = None
            except Exception as exc:
                state.last_error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
                self._send(
                    f"<div style='font-family:system-ui;padding:20px;color:#b3402f'>"
                    f"<b>取資料失敗</b><br>{state.last_error}</div>", code=500)

        def log_message(self, fmt, *a):
            if "/fragment" not in (a[0] if a else ""):
                print(f"  {self.address_string()} {fmt % a}")

    return H


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--poll", type=int, default=15, help="前端輪詢秒數")
    ap.add_argument("--open", dest="do_open", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    state = State(cfg)
    print("預熱中…", end=" ", flush=True)
    tasks, quarter = state.sheet()
    print(f"{quarter} {len(tasks)} 檔")

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state, args.poll))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"看板：{url}（每 {args.poll} 秒自動更新，Ctrl+C 停止）")
    print("試算表快取 15 秒；人工打勾或 dispatcher 回寫，下一輪就會反映")
    if args.do_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
