"""#615: request_json 은 같은 origin(scheme·host·port) 안의 redirect 만 따라간다.

CPython 기본 HTTPRedirectHandler 는 redirect 시 Authorization 을 포함한 헤더를
그대로 복사해 다른 origin·https→http 로 bearer 가 샌다(CWE-319). 여기서 두
서버는 모두 loopback 에 뜨지만 포트가 달라 서로 다른 origin 역할을 한다.
토큰 값은 모두 가짜이고, handler 는 request line 을 로그에 남기지 않는다.
"""

from __future__ import annotations

import http.server
import json
import shutil
import ssl
import subprocess
import threading

import pytest

from scopefuel.http import HttpError, _origin, request_json

FAKE_TOKEN = "test-token-not-real"


class _Handler(http.server.BaseHTTPRequestHandler):
    """routes 규칙대로 redirect 또는 JSON 을 돌려주고 받은 헤더를 기록한다."""

    def _respond(self):
        length = self.headers.get("Content-Length")
        if length:
            self.rfile.read(int(length))  # 안 읽은 본문이 있으면 close 가 RST 를 내 응답이 끊긴다
        self.server.received.append(
            (self.command, self.path, {k.lower(): v for k, v in self.headers.items()})
        )
        action = self.server.routes.get(self.path, ("json", {"ok": True}))
        if action[0] in ("redirect", "redirect307"):
            status = 302 if action[0] == "redirect" else 307
            # 본문에 목적지 URL 을 싣는다 — 거부 경로가 URL 쿼리를 오류 메시지에
            # 새지 않는지 검증하는 재료다.
            self.send_response(status)
            self.send_header("Location", action[1])
            self.end_headers()
            self.wfile.write(f'<a href="{action[1]}">moved</a>'.encode())
            return
        payload = json.dumps(action[1]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _respond
    do_POST = _respond
    do_PUT = _respond

    def log_message(self, *args):  # URL·헤더가 테스트 로그에 새지 않게 한다.
        pass


def _auths(received):
    """기록된 (method, path, headers) 에서 Authorization 값만 모은다."""
    return [headers.get("authorization") for _, _, headers in received]


def _paths(received):
    return [path for _, path, _ in received]


@pytest.fixture
def make_server():
    servers = []

    def _make(tls_context=None):
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        httpd.routes = {}
        httpd.received = []
        scheme = "http"
        if tls_context is not None:
            httpd.socket = tls_context.wrap_socket(httpd.socket, server_side=True)
            scheme = "https"
        httpd.base_url = f"{scheme}://127.0.0.1:{httpd.server_address[1]}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return httpd

    yield _make
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(scope="session")
def tls_server_context(tmp_path_factory):
    """loopback 자체서명 인증서 — openssl 이 없는 환경에서는 https 케이스를 건너뛴다."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl binary not found")
    out = tmp_path_factory.mktemp("tls")
    cert, key = out / "cert.pem", out / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=localhost",
            "-days",
            "1",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


def _run(url, **kwargs):
    """request_json 을 돌리고 (응답 또는 None, 예외 또는 None) 을 돌려준다.

    거부된 redirect 는 HttpError(3xx) 다. mutant(가드 제거)가 다른 예외를
    던지더라도 테스트는 assertion 으로 RED 가 되도록 예외를 통째로 돌려준다.
    """
    try:
        return request_json(url, **kwargs), None
    except Exception as exc:
        return None, exc


def _refused(err):
    return isinstance(err, HttpError) and 300 <= err.status < 400


# ------------------------------------------------------------ same origin


def test_same_origin_redirect_keeps_authorization(make_server):
    a = make_server()
    a.routes["/start"] = ("redirect", f"{a.base_url}/final")
    result, err = _run(f"{a.base_url}/start", headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    assert err is None
    assert result == {"ok": True}
    assert _paths(a.received) == ["/start", "/final"]
    assert _auths(a.received) == [f"Bearer {FAKE_TOKEN}", f"Bearer {FAKE_TOKEN}"]


def test_relative_redirect_same_origin(make_server):
    a = make_server()
    a.routes["/start"] = ("redirect", "/final")
    result, err = _run(f"{a.base_url}/start", headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    assert err is None
    assert result == {"ok": True}
    assert _auths(a.received) == [f"Bearer {FAKE_TOKEN}", f"Bearer {FAKE_TOKEN}"]


def test_same_origin_post_302_followed_as_get(make_server):
    """기존 urllib 규칙 유지: POST+302 는 GET 으로 변환돼 따라가고 Authorization 은 남는다."""
    a = make_server()
    a.routes["/start"] = ("redirect", f"{a.base_url}/final")
    result, err = _run(
        f"{a.base_url}/start",
        method="POST",
        headers={"Authorization": f"Bearer {FAKE_TOKEN}"},
        body={"x": 1},
    )
    assert err is None
    assert result == {"ok": True}
    assert [method for method, _, _ in a.received] == ["POST", "GET"]
    assert _auths(a.received) == [f"Bearer {FAKE_TOKEN}", f"Bearer {FAKE_TOKEN}"]


def test_same_origin_post_307_raises(make_server):
    """urllib 자체가 본문 있는 메서드의 307/308 자동 redirect 를 거부한다 — 기존 동작."""
    a = make_server()
    a.routes["/start"] = ("redirect307", f"{a.base_url}/final")
    result, err = _run(
        f"{a.base_url}/start",
        method="POST",
        headers={"Authorization": f"Bearer {FAKE_TOKEN}"},
        body={"x": 1},
    )
    assert result is None
    assert _refused(err)
    assert err.body == ""  # 거부된 게 아니라 urllib 자체 거부 — 사유 표식 없음
    assert len(a.received) == 1


def test_same_origin_redirect_loop_has_no_marker(make_server):
    """같은 origin 자기 루프는 상한 종료 — cross-origin 표식이 붙으면 안 된다."""
    a = make_server()
    a.routes["/start"] = ("redirect", "/start")
    result, err = _run(f"{a.base_url}/start", headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    assert result is None
    assert _refused(err)
    assert err.body == ""
    assert len(a.received) > 1


# ---------------------------------------------------------- cross origin


def test_cross_origin_port_redirect_refused(make_server):
    """같은 host 라도 port 가 다르면 다른 origin — 두 번째 요청은 나가지 않는다."""
    a, b = make_server(), make_server()
    a.routes["/start"] = ("redirect", f"{b.base_url}/final?code=secret-query")
    result, err = _run(f"{a.base_url}/start", headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    assert result is None
    assert _refused(err)
    assert b.received == []
    assert len(a.received) == 1


def test_cross_origin_host_redirect_refused(make_server):
    """같은 소켓이어도 hostname 이 다르면(localhost vs 127.0.0.1) 다른 origin."""
    a = make_server()
    a.routes["/start"] = ("redirect", a.base_url.replace("127.0.0.1", "localhost") + "/final")
    result, err = _run(f"{a.base_url}/start", headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    assert result is None
    assert _refused(err)
    assert len(a.received) == 1  # localhost 명의 요청은 아예 만들어지지 않는다


def test_https_to_http_downgrade_refused(make_server, tls_server_context):
    a = make_server(tls_context=tls_server_context)
    b = make_server()
    a.routes["/start"] = ("redirect", f"{b.base_url}/final")
    result, err = _run(
        f"{a.base_url}/start",
        headers={"Authorization": f"Bearer {FAKE_TOKEN}"},
        insecure=True,
    )
    assert result is None
    assert _refused(err)
    assert b.received == []


def test_http_to_https_upgrade_refused(make_server):
    """http→https 업그레이드도 scheme 이 다르면 다른 origin — 엄격하게 거부한다."""
    a = make_server()
    a.routes["/start"] = ("redirect", f"https://127.0.0.1:{a.server_address[1]}/final")
    result, err = _run(f"{a.base_url}/start", headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    assert result is None
    assert _refused(err)
    assert len(a.received) == 1


def test_exotic_port_caller_url_refuses_redirect(make_server):
    """`:+PORT` — http.client int() 는 받지만 urlsplit 은 거부한다. origin 해석 불가 → 거부.

    독립 tester 가 찾아낸 커버리지 공백(NICE-1/M4): `_origin(req.full_url)` 이
    None 을 돌려주는 호출자 URL 로는 가드의 `origin is None` 분기만이 막는다.
    """
    a, b = make_server(), make_server()
    # Location 도 `:+PORT` — 양쪽 origin 이 모두 None 이어도 따라가면 안 된다.
    a.routes["/start"] = ("redirect", f"http://127.0.0.1:+{b.server_address[1]}/final")
    result, err = _run(
        f"http://127.0.0.1:+{a.server_address[1]}/start",
        headers={"Authorization": f"Bearer {FAKE_TOKEN}"},
    )
    assert result is None
    assert _refused(err)
    assert "cross-origin" in err.body
    assert b.received == []


def test_multi_hop_redirect_stops_at_boundary(make_server):
    """A→A 는 따라가고(Authorization 유지), A→B hop 에서 멈춘다."""
    a, b = make_server(), make_server()
    a.routes["/start"] = ("redirect", f"{a.base_url}/middle")
    a.routes["/middle"] = ("redirect", f"{b.base_url}/final")
    result, err = _run(f"{a.base_url}/start", headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    assert result is None
    assert _refused(err)
    assert _paths(a.received) == ["/start", "/middle"]
    assert _auths(a.received) == [f"Bearer {FAKE_TOKEN}", f"Bearer {FAKE_TOKEN}"]
    assert b.received == []


def test_refused_redirect_does_not_leak_location_in_error(make_server):
    """거부된 302 본문의 목적지 URL(쿼리 포함)이 HttpError 에 실리지 않는다."""
    a, b = make_server(), make_server()
    a.routes["/start"] = ("redirect", f"{b.base_url}/final?code=secret-query")
    result, err = _run(f"{a.base_url}/start", headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    assert result is None
    assert _refused(err)
    assert "secret-query" not in str(err)
    assert "secret-query" not in err.body
    assert "cross-origin" in err.body  # 사유는 남고 목적지는 안 샌다


# --------------------------------------------------------- origin 판정 단위


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("https://a.com/x", "https://a.com/y", True),
        ("https://a.com", "https://a.com:443", True),  # 기본 포트 생략 정규화
        ("http://a.com", "http://a.com:80", True),
        ("http://EXAMPLE.com/x", "http://example.com/y", True),  # host 대소문자
        ("http://u:p@a.com/", "http://a.com/", True),  # userinfo 는 origin 이 아니다
        ("http://a.com", "http://a.com:8080", False),  # 포트만 다름
        ("http://a.com:0", "http://a.com", False),  # 포트 0 은 기본 포트가 아니다
        ("https://a.com", "http://a.com", False),  # downgrade
        ("http://a.com@evil.com/", "http://a.com/", False),  # userinfo 로 host 위장
        ("http://a.com.", "http://a.com", False),  # trailing dot — fail-closed
        ("http://bücher.ch", "http://xn--bcher-kva.ch", False),  # IDN — fail-closed
        ("https://a.com", "https://b.com", False),
    ],
)
def test_origin_comparison(left, right, same):
    assert (_origin(left) == _origin(right)) is same


def test_origin_unparseable_is_none():
    assert _origin("http://a.com:not-a-port/") is None
    assert _origin("http://a.com:+8080/") is None  # http.client int() 는 받는 꼴
