"""Google OAuth 憑證管理。

重點：Drive 上的目標資料夾由 owner@example.com 擁有，本帳號僅是協作者。
因此 **不能用 service account**（service account 不是那棵樹的協作者），
必須用使用者本人的 OAuth 憑證。

祕密檔案刻意不放在 OneDrive 底下 —— OneDrive 會把 refresh token 同步到雲端。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# 需要完整 drive scope：drive.file 只看得到本程式建立的檔案，
# 找不到既有的分類資料夾，無法歸檔。
CORE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]
# 告警用。刻意走 Gmail API 而不是透過 claude CLI 的 Gmail MCP ——
# 告警不能依賴 agent 堆疊，否則「agent 掛了」這件事永遠通知不出去。
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"

# 授權時要的完整範圍
SCOPES = CORE_SCOPES + [GMAIL_SCOPE]

def _resolve_secret_dir() -> Path:
    """決定祕密目錄。**不能只依賴環境變數** ——

    Windows 工作排程器給的環境是精簡過的，LOCALAPPDATA 可能不存在或指向別處
    （實測：排程跑 watchdog 時回報 token_missing，但同一個使用者在互動式
    shell 底下該檔明明存在）。無人值守的東西不該因為環境變數而失效。

    順序：config.json 的 local.secrets_dir → LOCALAPPDATA → USERPROFILE 推導。
    """
    global SECRET_DIR_SOURCE
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        explicit = json.loads(cfg_path.read_text(encoding="utf-8"))["local"].get("secrets_dir")
        if explicit:
            SECRET_DIR_SOURCE = f"config.json（{cfg_path}）"
            return Path(explicit)
        SECRET_DIR_SOURCE = "config.json 沒有 local.secrets_dir"
    except Exception as exc:
        # 不要靜默吞掉 —— 排程環境下這裡失敗會退回環境變數，
        # 而環境變數正是排程環境不可靠的東西，錯誤會變成難查的 token_missing。
        SECRET_DIR_SOURCE = f"讀 config.json 失敗：{type(exc).__name__}: {exc}"

    base = os.environ.get("LOCALAPPDATA")
    if not base:
        profile = os.environ.get("USERPROFILE") or str(Path.home())
        base = str(Path(profile) / "AppData" / "Local")
    return Path(base) / "meigu-automation" / "secrets"


SECRET_DIR_SOURCE = "未初始化"
SECRET_DIR = _resolve_secret_dir()
CLIENT_SECRET_PATH = SECRET_DIR / "client_secret.json"
TOKEN_PATH = SECRET_DIR / "token.json"


class AuthError(RuntimeError):
    pass


def ensure_secret_dir() -> Path:
    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    return SECRET_DIR


def load_credentials(*, interactive: bool = False) -> Credentials:
    """回傳可用的憑證。

    interactive=False（排程模式）時絕不開瀏覽器：token 失效就直接拋錯，
    讓上層送告警。無人值守時默默卡在瀏覽器等待是最糟的失敗方式。
    """
    ensure_secret_dir()

    creds: Credentials | None = None
    if TOKEN_PATH.exists():
        # 刻意**不**傳入 SCOPES —— 用 token 自己記錄的範圍。
        # 傳入比 token 實際擁有的更大的範圍，refresh 會被 Google 以
        # invalid_scope 打回，等於「新增一個告警用 scope」就會把整條線弄掛。
        # 授權時才用完整的 SCOPES（見下方 InstalledAppFlow）。
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds)
            return creds
        except Exception as exc:  # refresh token 被撤銷或過期
            if not interactive:
                raise AuthError(
                    f"憑證刷新失敗且處於非互動模式：{exc}\n"
                    f"請手動執行：python tools/setup_oauth.py"
                ) from exc

    if not interactive:
        raise AuthError(
            "沒有可用憑證。請先執行一次：python tools/setup_oauth.py"
        )

    if not CLIENT_SECRET_PATH.exists():
        raise AuthError(
            f"找不到 OAuth client 檔案：{CLIENT_SECRET_PATH}\n"
            f"請依 README 的步驟從 Google Cloud Console 下載後放到該路徑。"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    # 預設等待太短：實測使用者還在瀏覽器上點同意，本機接收端就已經
    # WSGITimeoutError 收攤了，結果是「使用者以為授權成功、token 卻沒更新」。
    creds = flow.run_local_server(port=0, prompt="consent", timeout_seconds=600)
    _save(creds)
    return creds


def _save(creds: Credentials) -> None:
    ensure_secret_dir()
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass  # Windows 上不一定生效，不阻斷


def auth_status() -> dict:
    """給告警用的健康檢查，不會拋錯。"""
    if not TOKEN_PATH.exists():
        return {"ok": False, "reason": "token_missing"}
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    except Exception as exc:
        return {"ok": False, "reason": f"token_unreadable: {exc}"}

    if creds.valid:
        return {"ok": True, "reason": "valid"}
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds)
            return {"ok": True, "reason": "refreshed"}
        except Exception as exc:
            return {"ok": False, "reason": f"refresh_failed: {exc}"}
    return {"ok": False, "reason": "no_refresh_token"}


def has_scope(creds: Credentials, scope: str) -> bool:
    return scope in (creds.scopes or [])


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else Path(__file__).resolve().parent.parent / "config.json"
    return json.loads(p.read_text(encoding="utf-8"))
