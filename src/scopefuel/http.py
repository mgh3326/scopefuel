"""stdlib-only HTTP 헬퍼. 의존성 0을 유지하는 이유 = `uvx scopefuel` 콜드스타트."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str = "", retry_after: float | None = None):
        super().__init__(f"HTTP {status}{f': {body[:200]}' if body else ''}")
        self.status = status
        self.body = body
        # Retry-After 의 초 값(헤더가 숫자일 때만). backoff 계산은 호출자가 한다.
        self.retry_after = retry_after


def classify_error(exc: BaseException) -> tuple[str, int | None, float | None]:
    """예외 → (error_kind, http_status, retry_after_s) 분류.

    게이트의 stale 수용 사유는 rate_limited(429)/server(5xx)/network 뿐이다.
    auth·http·unknown 은 수용 사유가 될 수 없다.
    """
    if isinstance(exc, HttpError):
        status = exc.status
        if status == 429:
            return "rate_limited", status, exc.retry_after
        if status in (401, 403):
            return "auth", status, exc.retry_after
        if 500 <= status <= 599:
            return "server", status, exc.retry_after
        return "http", status, exc.retry_after
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return "network", None, None
    return "unknown", None, None


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    """Retry-After 헤더의 초 값. HTTP-date·비수치·음수는 해석하지 않는다."""
    try:
        raw = exc.headers.get("Retry-After") if exc.headers is not None else None
    except Exception:
        return None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict | None = None,
    timeout: float = 20.0,
    insecure: bool = False,
    status_out: list[int] | None = None,
) -> dict:
    """JSON 요청/응답. insecure=True 는 localhost 자체서명 인증서 전용.

    ``status_out`` (task #578): 주어지면 응답의 HTTP status 를 덧붙인다 — 200 이 아닌
    2xx 도 구분해야 하는 호출자용이며, 반환값·예외는 그대로다.
    """
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if status_out is not None:
                status_out.append(resp.status)
            payload = resp.read()
    except urllib.error.HTTPError as exc:  # 상태코드를 보존해 401/429를 구분한다
        if status_out is not None:
            status_out.append(exc.code)
        raise HttpError(exc.code, exc.read().decode("utf-8", "replace"), _retry_after_seconds(exc)) from exc
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
