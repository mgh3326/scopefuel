"""stdlib-only HTTP 헬퍼. 의존성 0을 유지하는 이유 = `uvx scopefuel` 콜드스타트."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

_DEFAULT_PORTS = {"http": 80, "https": 443}


# Cloudflare Access 로그인 페이지의 호스트(<team>.cloudflareaccess.com).
_CF_ACCESS_LOGIN_DOMAIN = "cloudflareaccess.com"
# 2xx 로 와도 JSON 이 아닌 HTML 문서 — reject_html=True 호출자가 성공으로 받지 않는다.
_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def _norm_host(host: str | None) -> str:
    """비교용 호스트: 소문자, 끝의 점(FQDN 표기) 제거."""
    return (host or "").lower().rstrip(".")


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """redirect 판정용 (scheme, host, port). 기본 포트는 정규화하고, 해석 불가면 None.

    host 는 대소문자 무시·끝 점 제거로 정규화한다 — ``API.example.com.`` 과
    ``api.example.com`` 은 같은 origin 이다. scheme 은 정규화 외에 동일시하지
    않는다: http→https 승격도 자격이 설정된 scheme 밖으로 나가는 것이라 거부된다.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
        if port is None:
            port = _DEFAULT_PORTS.get(parsed.scheme.lower())
        return parsed.scheme.lower(), _norm_host(parsed.hostname), port
    except ValueError:
        return None


def is_cf_access_login_host(host: str | None) -> bool:
    """``cloudflareaccess.com`` 또는 그 하위 도메인(대소문자·끝 점 무시)인가."""
    norm = _norm_host(host)
    return norm == _CF_ACCESS_LOGIN_DOMAIN or norm.endswith("." + _CF_ACCESS_LOGIN_DOMAIN)


def _location_target(base: str, location: str) -> tuple[str | None, str | None]:
    """redirect Location → (해석된 절대 URL 또는 None, 호스트 또는 None).

    포트가 깨진 URL 처럼 origin 판정은 불가해도 호스트는 읽히는 경우가 있어 둘을
    따로 돌려준다. 어떤 해석 실패도 예외가 아니라 None 이다 — 파서 예외 메시지는
    Location 조각을 그대로 담을 수 있다.
    """
    try:
        target = urllib.parse.urljoin(base, location.strip())
        parts = urllib.parse.urlsplit(target)
        host = parts.hostname
    except ValueError:
        return None, None
    try:
        _ = parts.port  # 포트 형식 검증 — 실패하면 origin 판정 불가
    except ValueError:
        return None, host
    return target, host


class _RedirectRefused(urllib.error.HTTPError):
    """_SameOriginRedirectHandler 가 거부한 redirect — HttpError 변환 시 사유 표식으로 쓴다."""


class _CfAccessRedirectRefused(_RedirectRefused):
    """Cloudflare Access 로그인 호스트로의 redirect — 별도 이름의 오류로 변환된다."""


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """같은 origin(scheme·host·port) 안의 redirect 만 따라간다.

    CPython 기본 HTTPRedirectHandler 는 Content-Length/Content-Type 외의 헤더를
    redirect 요청에 그대로 복사한다 — Authorization bearer 가 다른 origin 이나
    https→http 다운그레이드로 새는 CWE-319. spec.py 처럼 자격이 Authorization 이
    아닌 헤더에 실리는 호출자도 있어, 헤더를 지우는 대신 cross-origin redirect
    자체를 거부한다. 거부하면 호출자는 3xx 를 HttpError 로 받는다.

    #1280: Location 은 CPython 이 처리하기 전에 먼저 판정한다. CPython 은 허용되지
    않는 scheme 의 redirect 를 Location 전체가 든 메시지로 거부하고, 파서는 깨진
    호스트 조각을 예외 메시지에 싣는다 — 둘 다 거치기 전에 고정 문구로 거부한다.
    redirect_request 는 CPython 이 최종 해석한 URL 로 한 번 더 판정한다.
    """

    def http_error_302(self, req, fp, code, msg, headers):
        location = headers.get("location") or headers.get("uri")
        if location is not None:
            target, host = _location_target(req.full_url, location)
            if is_cf_access_login_host(host):
                raise _CfAccessRedirectRefused(
                    req.full_url, code, "redirect to a Cloudflare Access login refused", headers, fp
                )
            origin = _origin(req.full_url)
            if target is None or origin is None or origin != _origin(target):
                raise _RedirectRefused(req.full_url, code, "cross-origin redirect refused", headers, fp)
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        origin = _origin(req.full_url)
        if origin is None or origin != _origin(newurl):
            raise _RedirectRefused(req.full_url, code, "cross-origin redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str = "", retry_after: float | None = None):
        super().__init__(f"HTTP {status}{f': {body[:200]}' if body else ''}")
        self.status = status
        self.body = body
        # Retry-After 의 초 값(헤더가 숫자일 때만). backoff 계산은 호출자가 한다.
        self.retry_after = retry_after


class CfAccessLoginRedirect(HttpError):
    """응답이 Cloudflare Access 로그인으로 redirect 했다 — 서비스 토큰이 없거나 거부됐다.

    메시지는 고정 문구다: Location·쿼리·헤더 값은 싣지 않는다.
    """

    reason = "cf_access_login_redirect"

    def __init__(self, status: int):
        super().__init__(status, "redirect to a Cloudflare Access login refused")


class UnexpectedHtmlResponse(HttpError):
    """JSON 호출에 2xx HTML 문서가 왔다 — 대개 Cloudflare Access 로그인 페이지다.

    본문은 읽지도 싣지도 않는다.
    """

    reason = "unexpected_html_response"

    def __init__(self, status: int):
        super().__init__(status, "HTML document where JSON was expected")


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
    reject_html: bool = False,
) -> dict:
    """JSON 요청/응답. insecure=True 는 localhost 자체서명 인증서 전용.

    ``status_out`` (task #578): 주어지면 응답의 HTTP status 를 덧붙인다 — 200 이 아닌
    2xx 도 구분해야 하는 호출자용이며, 반환값·예외는 그대로다.

    ``reject_html`` (task #1280): 2xx 응답의 Content-Type 이 HTML 이면 본문을 읽지
    않고 UnexpectedHtmlResponse 를 던진다 — 빈 HTML 본문이 ``{}`` 성공으로 접히지
    않게 한다. 응답 헤더를 엄격히 지키지 않는 provider 가 있어 기본은 끈다.
    """
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    handlers: list[urllib.request.BaseHandler] = [_SameOriginRedirectHandler()]
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            if status_out is not None:
                status_out.append(resp.status)
            if reject_html and resp.headers.get_content_type() in _HTML_CONTENT_TYPES:
                raise UnexpectedHtmlResponse(resp.status)
            payload = resp.read()
    except urllib.error.HTTPError as exc:  # 상태코드를 보존해 401/429를 구분한다
        if status_out is not None:
            status_out.append(exc.code)
        body_text = exc.read().decode("utf-8", "replace")
        if isinstance(exc, _CfAccessRedirectRefused):
            raise CfAccessLoginRedirect(exc.code) from exc
        if 300 <= exc.code < 400:
            # 거부된 redirect 의 본문에는 Location 목적지(URL 쿼리 포함)가 들어갈 수 있다.
            # 사유 표식은 거부 판정과 1:1 인 _RedirectRefused 에만 붙는다 — 같은
            # origin 루프 상한 등 다른 3xx 는 사유 없이 본문만 비운다.
            body_text = "cross-origin redirect refused" if isinstance(exc, _RedirectRefused) else ""
        raise HttpError(exc.code, body_text, _retry_after_seconds(exc)) from exc
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
