"""Drive 操作：掃樹、建資料夾、resumable 上傳、冪等覆蓋。

設計原則：**只認 folder ID，不認名稱。**
理由（實測）：
  - podcast 樹每季新建一棵，名稱無規則
    （「2026Q1 美股財報 Podcast」/「2026Q2 美股財報podcast」/「2026Q2 美股財報」）
  - 分類名每季會漂（「金融業」→「金融」、「AI硬體」↔「AI 硬體」、
    「實體AI/機器人」→「實體AI/機器人/醫療」）
  - 兩棵樹的分類集合根本不同（podcast 樹 12 個頂層、Word 樹 9 個）
名稱比對已經害人工歸檔錯過一次，自動化不重蹈覆轍。
"""

from __future__ import annotations

import io
import time
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

FOLDER_MIME = "application/vnd.google-apps.folder"
GDOC_MIME = "application/vnd.google-apps.document"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# 大檔用 resumable。podcast m4a 約 10–13MB；
# Drive 連接器（MCP）只能吃 base64，13MB 檔會變成 ~17MB 字串，物理上塞不進去，
# 這正是必須走原生 API 的原因。
RESUMABLE_THRESHOLD_BYTES = 5 * 1024 * 1024
CHUNK_SIZE = 4 * 1024 * 1024


class DriveClient:
    def __init__(self, creds):
        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    # ---------- 讀 ----------

    def list_children(self, parent_id: str, *, folders_only: bool = False) -> list[dict]:
        q = f"'{parent_id}' in parents and trashed = false"
        if folders_only:
            q += f" and mimeType = '{FOLDER_MIME}'"
        out, token = [], None
        while True:
            resp = self._retry(
                lambda: self.svc.files()
                .list(
                    q=q,
                    fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, owners(emailAddress))",
                    pageSize=1000,
                    pageToken=token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            out.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def scan_tree(self, root_id: str, *, max_depth: int = 4) -> list[dict]:
        """遞迴掃出所有子資料夾，回傳 [{path: [...], id, name, depth}]。

        path 用 list 而非字串串接 —— 分類名本身就含 '/'
        （「國防/戰略物資/太空/無人機」），用 '/' 當分隔會直接歧義。
        """
        result: list[dict] = []

        def walk(fid: str, path: tuple[str, ...], depth: int) -> None:
            if depth > max_depth:
                return
            for f in self.list_children(fid, folders_only=True):
                new_path = path + (f["name"],)
                result.append(
                    {"path": list(new_path), "id": f["id"], "name": f["name"], "depth": depth}
                )
                walk(f["id"], new_path, depth + 1)

        walk(root_id, (), 1)
        return result

    def find_child(self, parent_id: str, name: str, *, mime: str | None = None) -> dict | None:
        safe = name.replace("'", "\\'")
        q = f"'{parent_id}' in parents and name = '{safe}' and trashed = false"
        if mime:
            q += f" and mimeType = '{mime}'"
        resp = self._retry(
            lambda: self.svc.files()
            .list(
                q=q,
                fields="files(id, name, mimeType, size)",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = resp.get("files", [])
        return files[0] if files else None

    # ---------- 寫 ----------

    def ensure_folder(self, parent_id: str, name: str) -> str:
        """存在就回傳既有 ID，不存在才建。冪等。"""
        existing = self.find_child(parent_id, name, mime=FOLDER_MIME)
        if existing:
            return existing["id"]
        meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        created = self._retry(
            lambda: self.svc.files()
            .create(body=meta, fields="id", supportsAllDrives=True)
            .execute()
        )
        return created["id"]

    def upload(
        self,
        local_path: str | Path,
        parent_id: str,
        *,
        name: str | None = None,
        mime_type: str | None = None,
        convert_to_gdoc: bool = False,
        overwrite: bool = True,
    ) -> dict:
        """上傳檔案。

        冪等：同名檔已存在時預設 **更新同一個 file ID**，而不是再建一份。
        Drive 允許同名並存，不做這件事重跑一次就會有兩份。
        """
        p = Path(local_path)
        if not p.exists():
            raise FileNotFoundError(p)
        name = name or p.name
        size = p.stat().st_size
        mime_type = mime_type or _guess_mime(p)

        resumable = size >= RESUMABLE_THRESHOLD_BYTES
        media = MediaFileUpload(
            str(p), mimetype=mime_type, resumable=resumable, chunksize=CHUNK_SIZE
        )

        existing = self.find_child(parent_id, name) if overwrite else None
        if existing:
            req = self.svc.files().update(
                fileId=existing["id"],
                media_body=media,
                fields="id, name, size, webViewLink",
                supportsAllDrives=True,
            )
            action = "updated"
        else:
            body = {"name": name, "parents": [parent_id]}
            if convert_to_gdoc:
                body["mimeType"] = GDOC_MIME
            req = self.svc.files().create(
                body=body,
                media_body=media,
                fields="id, name, size, webViewLink",
                supportsAllDrives=True,
            )
            action = "created"

        resp = self._execute(req, resumable=resumable)
        resp["_action"] = action
        return resp

    # ---------- 內部 ----------

    def _execute(self, req, *, resumable: bool):
        if not resumable:
            return self._retry(req.execute)

        resp = None
        consecutive_errors = 0
        while resp is None:
            try:
                _status, resp = req.next_chunk()
                consecutive_errors = 0
            except HttpError as exc:
                # resumable 上傳可以從中斷處續傳，重試整個 chunk 即可
                if exc.resp.status in (500, 502, 503, 504, 429):
                    consecutive_errors += 1
                    if consecutive_errors > 5:
                        raise
                    time.sleep(2**consecutive_errors)
                    continue
                raise
        return resp

    @staticmethod
    def _retry(fn, attempts: int = 5):
        delay = 2
        for i in range(attempts):
            try:
                return fn()
            except HttpError as exc:
                if exc.resp.status in (500, 502, 503, 504, 429) and i < attempts - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise RuntimeError("unreachable")


def _guess_mime(p: Path) -> str:
    return {
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".docx": DOCX_MIME,
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".json": "application/json",
        ".md": "text/markdown",
    }.get(p.suffix.lower(), "application/octet-stream")
