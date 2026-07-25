"""stdlib-only HTTP 헬퍼. 의존성 0을 유지하는 이유 = `uvx scopefuel` 콜드스타트."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str = ""):
        super().__init__(f"HTTP {status}{f': {body[:200]}' if body else ''}")
        self.status = status
        self.body = body


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict | None = None,
    timeout: float = 20.0,
    insecure: bool = False,
) -> dict:
    """JSON 요청/응답. insecure=True 는 localhost 자체서명 인증서 전용."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as exc:  # 상태코드를 보존해 401/429를 구분한다
        raise HttpError(exc.code, exc.read().decode("utf-8", "replace")) from exc
    text = payload.decode("utf-8", "replace").strip()
    if not text:
        return {}
    return json.loads(text)


def dig(data: object, path: list[str | int] | None) -> object:
    """['tokens', 'access_token'] 같은 경로로 중첩 값을 꺼낸다. 없으면 None."""
    if not path:
        return None
    cur: object = data
    for key in path:
        if isinstance(key, int) and isinstance(cur, list):
            if key >= len(cur):
                return None
            cur = cur[key]
        elif isinstance(cur, dict):
            if key not in cur:
                return None
            cur = cur[key]
        else:
            return None
    return cur
