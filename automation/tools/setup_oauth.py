"""一次性 OAuth 授權。需要有畫面的環境（會開瀏覽器）。

執行前提：%LOCALAPPDATA%\\meigu-automation\\secrets\\client_secret.json 已放好。
取得方式見 automation/README.md。
"""

import _bootstrap  # noqa: F401

from core.gauth import CLIENT_SECRET_PATH, TOKEN_PATH, auth_status, ensure_secret_dir, load_credentials


def main() -> int:
    ensure_secret_dir()
    print(f"祕密目錄：{CLIENT_SECRET_PATH.parent}")
    print("（刻意放在 OneDrive 之外 —— 不要讓 refresh token 被同步到雲端）\n")

    if not CLIENT_SECRET_PATH.exists():
        print(f"[X] 找不到 {CLIENT_SECRET_PATH}")
        print("    請先依 README「步驟 1」在 Google Cloud Console 建立 OAuth client，")
        print("    下載 JSON 後改名為 client_secret.json 放到上面的路徑。")
        return 1

    print("[*] 即將開啟瀏覽器進行 Google 授權。")
    print("    請用『你自己的』Google 帳號登入（you@example.com）——")
    print("    Drive 上的目標資料夾由 owner@example.com 擁有，你是協作者，")
    print("    所以必須用你的帳號授權，service account 不會有存取權。\n")

    creds = load_credentials(interactive=True)
    print(f"[OK] 授權完成，token 已寫入：{TOKEN_PATH}")
    print(f"     scopes: {', '.join(creds.scopes or [])}")

    st = auth_status()
    print(f"[OK] 健康檢查：{st}")
    print("\n下一步：python tools/build_drive_map.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
