"""claude CLI 的備援憑證（長效 token）注入。

正常情況下，claude CLI 用自己的登入（~/.claude 憑證檔）。主登入失效時
（手動登出、Anthropic 端撤銷 session、憑證檔損毀），auth_monitor 會確認
secrets 裡的長效 token 可用，然後放一個旗標檔 —— 此後所有派工／告警的
claude 子行程都帶 CLAUDE_CODE_OAUTH_TOKEN 環境變數，整條線不中斷。
主登入恢復後旗標自動移除，回到正常路徑。

為什麼用旗標而不是永遠注入：env token 的優先權高於本機登入，永遠注入
等於平常也繞過主登入 —— 備援 token 哪天被撤銷，好端端的主登入也會被
它拖垮。備援只在主登入死掉時上場。

長效 token 的來源：使用者在自己的終端機跑一次 `claude setup-token`，
把產出的 token 用 tools/store_claude_token.py 存進 secrets
（token 不經過對話、不進 OneDrive）。
"""

from __future__ import annotations

import os
from pathlib import Path

from core.gauth import SECRET_DIR, load_config

TOKEN_PATH = SECRET_DIR / "claude_token.txt"


def flag_path(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    return Path(cfg["local"]["state_dir"]) / "claude_cli_fallback.json"


def cli_env() -> dict:
    """給 claude 子行程用的環境。備援旗標啟用且 token 存在時注入。

    任何一步失敗都退回原環境 —— 備援機制自己壞掉不能反過來害死派工。
    """
    env = os.environ.copy()
    try:
        if flag_path().exists() and TOKEN_PATH.exists():
            tok = TOKEN_PATH.read_text(encoding="utf-8").strip()
            if tok:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    except Exception:
        pass
    return env
