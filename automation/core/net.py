"""共用 HTTP 取用，含重試與速率限制。

SEC 明文要求：帶可識別的 User-Agent，且每秒不超過 10 次請求。
違反會被封 IP —— 無人值守下被封是災難，所以節流寫死在這裡。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

SEC_UA = "meigu-automation you@example.com"  # ←務必改成你的真實 email：SEC 要求可識別、可聯絡的 UA，否則會被封 IP
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_lock = threading.Lock()
_last_call = {"sec": 0.0}
SEC_MIN_INTERVAL = 0.15  # 約 6.7 req/s，留安全邊際


class FetchError(RuntimeError):
    def __init__(self, url: str, status: int | None, detail: str):
        super().__init__(f"{status or '-'} {url}: {detail}")
        self.url, self.status, self.detail = url, status, detail


def fetch(
    url: str,
    *,
    ua: str = BROWSER_UA,
    headers: dict | None = None,
    timeout: int = 30,
    retries: int = 3,
    sec_throttle: bool = False,
) -> bytes:
    h = {"User-Agent": ua}
    if headers:
        h.update(headers)

    last = None
    for attempt in range(retries):
        if sec_throttle:
            with _lock:
                gap = time.time() - _last_call["sec"]
                if gap < SEC_MIN_INTERVAL:
                    time.sleep(SEC_MIN_INTERVAL - gap)
                _last_call["sec"] = time.time()
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = FetchError(url, exc.code, exc.reason)
            # 4xx 除了 429 之外重試沒意義
            if exc.code != 429 and 400 <= exc.code < 500:
                raise last
        except Exception as exc:
            last = FetchError(url, None, f"{type(exc).__name__}: {exc}")
        time.sleep(2 ** attempt)
    raise last  # type: ignore[misc]


def fetch_json(url: str, **kw) -> dict:
    return json.loads(fetch(url, **kw))


def head_ok(url: str, *, timeout: int = 15) -> bool:
    """只判斷「存在嗎」，不下載內容。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
