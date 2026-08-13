"""把 `claude setup-token` 鑄出的長效 token 存進 secrets（一次性設定）。

用法（在你自己的終端機跑，token 不經過任何對話或雲端）：
    1. claude setup-token          # 跟著瀏覽器完成授權，最後會印出 sk-ant-oat... token
    2. python tools/store_claude_token.py   # 貼上 token（輸入不顯示），驗證後存檔

存到 %SECRET_DIR%\\claude_token.txt（與 Google token 同一個 OneDrive 外目錄）。
之後 claude CLI 主登入若失效，auth_monitor 會自動切換到這顆 token，
派工不中斷 —— 見 core/claude_cli.py。
"""

import _bootstrap  # noqa: F401

import os
import subprocess
from getpass import getpass

from core.claude_cli import TOKEN_PATH


def main() -> int:
    tok = getpass("貼上 claude setup-token 產出的 token（輸入不會顯示）：").strip()
    if not tok.startswith("sk-ant-"):
        print("[X] 這不像 Anthropic 長效 token（應以 sk-ant- 開頭），未存檔。")
        return 1

    print("驗證中（用這顆 token 真的呼叫一次 claude）…")
    env = os.environ.copy()
    env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    try:
        r = subprocess.run(["claude", "-p", "ok"], capture_output=True, text=True,
                           timeout=180, env=env, encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"[X] 驗證失敗：{type(exc).__name__}: {exc}，未存檔。")
        return 1
    if r.returncode != 0:
        print(f"[X] token 無法使用（rc={r.returncode}）："
              f"{(r.stdout or r.stderr or '')[:200].strip()}，未存檔。")
        return 1

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(tok, encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    print(f"[OK] 備援 token 已驗證並存到 {TOKEN_PATH}")
    print("     主登入失效時 auth_monitor 會自動切換過來，不需要再做任何事。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
