"""task #1280: handoffkeep behind Cloudflare Access — service-token headers.

Every server here is a fake on loopback; no request leaves the machine. The
token, CF id and CF secret are fixture strings, and the fake handlers never log
a request line. Two loopback servers on different ports play two origins; a
redirect to ``localhost`` on the first server's port plays a different host
that would still land on the same socket — so a wrongly followed redirect is
recorded, not lost.
"""

from __future__ import annotations

import http.server
import json
import os
import pathlib
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import traceback

import pytest

from scopefuel import __version__, bench, quota_share, served, stale_build
from scopefuel import http as sf_http
from scopefuel.http import HttpError, request_json

FIX_TOKEN = "hk-token-fixture-7Qx"
FIX_ID = "cf-id-fixture-9Lm.access"
FIX_SECRET = "cf-secret-fixture-3Zp"
SECRETS = (FIX_TOKEN, FIX_ID, FIX_SECRET)
UA = f"scopefuel/{__version__}"
CF_ID_KEY = "HANDOFFKEEP_CF_ACCESS_CLIENT_ID"
CF_SECRET_KEY = "HANDOFFKEEP_CF_ACCESS_CLIENT_SECRET"
REDIRECT_CODES = (301, 302, 303, 307, 308)


# ------------------------------------------------------------ fake servers


class _Handler(http.server.BaseHTTPRequestHandler):
    """Records every request, then answers with the server's ``action``."""

    def _respond(self):
        length = self.headers.get("Content-Length")
        if length:
            self.rfile.read(int(length))
        headers = {k.lower(): v for k, v in self.headers.items()}
        self.server.received.append((self.command, self.path, headers))
        action = self.server.action
        if action is not None and (
            self.server.action_path is None or self.path.startswith(self.server.action_path)
        ):
            action(self, headers)
            return
        scope = self.path.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if self.command == "GET" and scope in ("scores", "reps", "grades", "catalog"):
            payload = {scope: []}
        elif self.path.startswith("/aa"):
            payload = {"data": []}
        else:
            payload = {"ok": True, "model": "probe-model"}
        self.send_json(200, payload)

    def send_json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_body(self, status, content_type, body: str, extra=()):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in extra:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _respond
    do_POST = _respond
    do_PUT = _respond

    def log_message(self, *args):  # request lines and headers stay out of test logs
        pass


def _echo(headers) -> str:
    """Everything credential-shaped the client sent, echoed back verbatim."""
    return " ".join(
        f"{name}={headers.get(name, '')}"
        for name in ("authorization", "cf-access-client-id", "cf-access-client-secret")
    )


def redirect(code, location):
    def _action(handler, headers):
        handler.send_body(
            code, "text/html", f'<a href="{location}">moved</a> {_echo(headers)}', [("Location", location)]
        )

    return _action


def html(content_type="text/html; charset=utf-8", body="<html><body>Sign in</body></html>"):
    def _action(handler, headers):
        handler.send_body(200, content_type, body)

    return _action


def echo_status(status, content_type="text/plain"):
    def _action(handler, headers):
        handler.send_body(status, content_type, f"denied {_echo(headers)}")

    return _action


class _CountingServer(http.server.ThreadingHTTPServer):
    """Counts accepted connections, including ones that never form a request.

    A redirect followed into a scheme change (http -> https on the same port)
    dies in the TLS handshake before any request line exists, so ``received``
    alone cannot see it; the connection count can. Every urllib request opens
    its own connection, so one connection means nothing was re-sent.
    """

    def process_request(self, request, client_address):
        self.connections += 1
        super().process_request(request, client_address)


@pytest.fixture
def make_server():
    servers = []

    def _make(tls_context=None):
        httpd = _CountingServer(("127.0.0.1", 0), _Handler)
        httpd.connections = 0
        httpd.received = []
        httpd.action = None
        httpd.action_path = None
        scheme = "http"
        if tls_context is not None:
            httpd.socket = tls_context.wrap_socket(httpd.socket, server_side=True)
            scheme = "https"
        httpd.port = httpd.server_address[1]
        httpd.base_url = f"{scheme}://127.0.0.1:{httpd.port}"
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        servers.append(httpd)
        return httpd

    yield _make
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def hk(make_server):
    return make_server()


@pytest.fixture(scope="session")
def tls_server_context(tmp_path_factory):
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


def _env(monkeypatch, url, *, cf=True):
    monkeypatch.setenv("HANDOFFKEEP_URL", url)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", FIX_TOKEN)
    if cf:
        monkeypatch.setenv(CF_ID_KEY, FIX_ID)
        monkeypatch.setenv(CF_SECRET_KEY, FIX_SECRET)
    else:
        monkeypatch.delenv(CF_ID_KEY, raising=False)
        monkeypatch.delenv(CF_SECRET_KEY, raising=False)


def _no_secret(text: str) -> None:
    for value in SECRETS:
        assert value not in text


def _exception_graph(exc: BaseException) -> str:
    """Everything reachable from ``exc``, printed or not (#1280 B2).

    ``raise ... from None`` only hides ``__context__`` from the traceback
    printer; the original stays attached. Walk every nested exception through
    ``__cause__``, ``__context__``, ``args`` and instance attributes, and render
    each value both ways.
    """
    seen: set[int] = set()
    parts: list[str] = []

    def walk(obj, depth=0):
        if obj is None or id(obj) in seen or depth > 12:
            return
        seen.add(id(obj))
        if isinstance(obj, (str, bytes, int, float, bool)):
            parts.append(repr(obj))
            return
        try:
            parts.append(repr(obj))
            parts.append(str(obj))
        except Exception:
            pass
        if isinstance(obj, BaseException):
            for arg in obj.args:
                walk(arg, depth + 1)
            walk(obj.__cause__, depth + 1)
            walk(obj.__context__, depth + 1)
        if isinstance(obj, (list, tuple, set, frozenset)):
            for item in obj:
                walk(item, depth + 1)
        elif isinstance(obj, dict):
            for key, value in obj.items():
                walk(key, depth + 1)
                walk(value, depth + 1)
        if isinstance(obj, BaseException):
            for value in vars(obj).values():
                walk(value, depth + 1)

    walk(exc)
    return "\n".join(parts)


def _exc_text(exc: BaseException) -> str:
    """The message, the printable traceback chain and the whole exception graph."""
    return str(exc) + "\n" + "".join(traceback.format_exception(exc)) + "\n" + _exception_graph(exc)


def _assert_no_retained_exception(exc: BaseException) -> None:
    assert exc.__cause__ is None
    assert exc.__context__ is None


def _catalog_backend():
    backend = bench.bench_backend(use="catalog")
    assert backend.name == bench.BENCH_BACKEND_HANDOFFKEEP
    return backend


def _reps_backend():
    backend = bench.bench_backend(use="reps")
    assert backend.name == bench.BENCH_BACKEND_HANDOFFKEEP
    return backend


# Every request kind scopefuel sends to handoffkeep. Reads go through the real
# fetchers; writes through _handoffkeep_request (what push-catalog, reps add,
# grades set, scores sync and push-local all call) and quota_share._request.
REQUEST_KINDS = {
    "catalog GET": lambda: bench._fetch_catalog(_catalog_backend()),
    "scores GET": lambda: bench._fetch_scores(_catalog_backend()),
    "grades GET": lambda: bench._fetch_grades(_catalog_backend()),
    "reps GET (list)": lambda: bench._fetch_reps(_reps_backend(), query={"limit": 5}),
    "reps PUT (add)": lambda: bench._handoffkeep_request(
        _reps_backend(), "reps", method="PUT", body={"reps": []}
    ),
    "catalog PUT (push-catalog)": lambda: bench._handoffkeep_request(
        _catalog_backend(), "catalog", method="PUT", body={"catalog": []}
    ),
    "grades PUT": lambda: bench._handoffkeep_request(
        _catalog_backend(), "grades", method="PUT", body={"grades": []}
    ),
    "scores PUT": lambda: bench._handoffkeep_request(
        _catalog_backend(), "scores", method="PUT", body={"scores": []}
    ),
    "quota share GET": lambda: quota_share._request("GET", "fixture-key"),
    "quota share PUT": lambda: quota_share._request("PUT", "fixture-key", {"document": {}}),
}
HK_ONLY_KINDS = [k for k in REQUEST_KINDS if not k.startswith("quota")]


# ------------------------------------------------------------ AC1 headers


def test_every_hk_request_kind_carries_both_cf_headers_and_the_user_agent(hk, monkeypatch):
    _env(monkeypatch, hk.base_url)
    for run in REQUEST_KINDS.values():
        run()
    assert len(hk.received) == len(REQUEST_KINDS)
    for _method, _path, headers in hk.received:
        assert headers["cf-access-client-id"] == FIX_ID
        assert headers["cf-access-client-secret"] == FIX_SECRET
        assert headers["user-agent"] == UA
        assert headers["authorization"] == f"Bearer {FIX_TOKEN}"


def test_without_keys_no_hk_request_carries_a_cf_header(hk, monkeypatch):
    _env(monkeypatch, hk.base_url, cf=False)
    for run in REQUEST_KINDS.values():
        run()
    assert len(hk.received) == len(REQUEST_KINDS)
    for method, _path, headers in hk.received:
        assert not [name for name in headers if name.startswith("cf-")]
        assert headers["user-agent"] == UA
        # The only difference to the pre-#1280 request is the User-Agent.
        expected = {"authorization", "user-agent", "host", "accept-encoding", "connection"}
        if method == "PUT":
            expected |= {"content-type", "content-length"}
        assert set(headers) == expected


def test_cf_header_values_are_trimmed_on_the_wire(hk, monkeypatch):
    _env(monkeypatch, hk.base_url)
    monkeypatch.setenv(CF_ID_KEY, f"  {FIX_ID}\t")
    monkeypatch.setenv(CF_SECRET_KEY, f" {FIX_SECRET} ")
    bench._fetch_catalog(_catalog_backend())
    ((_, _, headers),) = hk.received
    assert headers["cf-access-client-id"] == FIX_ID
    assert headers["cf-access-client-secret"] == FIX_SECRET


def test_provider_and_non_hk_calls_never_carry_cf_headers(make_server, tmp_path, monkeypatch):
    hk = make_server()
    provider = make_server()
    # Keys present in both sources — nothing about them may reach a non-hk URL.
    _env(monkeypatch, hk.base_url)
    dotenv = tmp_path / "hk.env"
    dotenv.write_text(f"{CF_ID_KEY}={FIX_ID}\n{CF_SECRET_KEY}={FIX_SECRET}\n", encoding="utf-8")
    monkeypatch.setenv("HANDOFFKEEP_CONFIG", str(dotenv))

    assert (
        served.probe_upstream("provider-key", "probe-model", endpoint=f"{provider.base_url}/v1/chat")
        == "probe-model"
    )
    stale_build._get_json(f"{provider.base_url}/repos/x/y/commits/main", 5.0)
    request_json(f"{provider.base_url}/usage", headers={"Authorization": "Bearer provider-token"})
    # bench sync: the Artificial Analysis fetch is a provider call through
    # bench's own transport; the handoffkeep write that follows is not.
    monkeypatch.setattr(bench, "AA_API_URL", f"{provider.base_url}/aa")
    bench.sync_scores(api_key="aa-fixture-key")

    assert len(provider.received) == 4
    for _method, _path, headers in provider.received:
        assert not [name for name in headers if name.startswith("cf-")]
        _no_secret(json.dumps(headers))
    # The hk side of the same run did carry them — the headers are scoped, not lost.
    for _method, _path, headers in hk.received:
        assert headers["cf-access-client-id"] == FIX_ID


def test_cf_header_names_live_only_in_the_one_hk_helper():
    """Structural guard: no other module can attach the CF service token."""
    src = pathlib.Path(bench.__file__).parent
    hits = sorted(
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if "cf-access-client" in path.read_text(encoding="utf-8").lower()
    )
    assert hits == ["bench.py"]
    bench_src = pathlib.Path(bench.__file__).read_text(encoding="utf-8")
    assert bench_src.count('headers["CF-Access-Client-Id"]') == 1
    # quota share sends through the same helper rather than its own headers.
    qs_src = pathlib.Path(quota_share.__file__).read_text(encoding="utf-8")
    assert "bench._handoffkeep_send(" in qs_src
    assert "Authorization" not in qs_src


# ------------------------------------------------------------ AC2 config


def _dotenv(tmp_path, monkeypatch, body: str, name="hk.env"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("HANDOFFKEEP_CONFIG", str(path))
    return path


def test_env_keys_are_read(monkeypatch):
    monkeypatch.setenv(CF_ID_KEY, FIX_ID)
    monkeypatch.setenv(CF_SECRET_KEY, FIX_SECRET)
    assert bench._handoffkeep_cf_access() == (FIX_ID, FIX_SECRET)


def test_config_env_keys_are_read_with_the_endpoint(tmp_path, monkeypatch):
    _dotenv(
        tmp_path,
        monkeypatch,
        f"HANDOFFKEEP_URL=https://hk.example.invalid\nHANDOFFKEEP_TOKEN={FIX_TOKEN}\n"
        f"{CF_ID_KEY}='{FIX_ID}'\n{CF_SECRET_KEY}=\"{FIX_SECRET}\"\n",
    )
    assert bench._handoffkeep_cf_access() == (FIX_ID, FIX_SECRET)


def test_env_wins_per_key_over_config_env(tmp_path, monkeypatch):
    _dotenv(tmp_path, monkeypatch, f"{CF_ID_KEY}=stored-id\n{CF_SECRET_KEY}=stored-secret\n")
    monkeypatch.setenv(CF_ID_KEY, FIX_ID)
    assert bench._handoffkeep_cf_access() == (FIX_ID, "stored-secret")
    monkeypatch.delenv(CF_ID_KEY)
    monkeypatch.setenv(CF_SECRET_KEY, FIX_SECRET)
    assert bench._handoffkeep_cf_access() == ("stored-id", FIX_SECRET)


def test_env_endpoint_override_never_completes_cf_keys_from_config_env(tmp_path, monkeypatch):
    """Same rule as the token: an env URL must not pull the stored secret to its host."""
    _dotenv(tmp_path, monkeypatch, f"{CF_ID_KEY}=stored-id\n{CF_SECRET_KEY}=stored-secret\n")
    monkeypatch.setenv("HANDOFFKEEP_URL", "https://elsewhere.example.invalid")
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", FIX_TOKEN)
    assert bench._handoffkeep_cf_access() is None
    monkeypatch.setenv(CF_ID_KEY, FIX_ID)
    monkeypatch.setenv(CF_SECRET_KEY, FIX_SECRET)
    assert bench._handoffkeep_cf_access() == (FIX_ID, FIX_SECRET)


def test_handoffkeep_config_override_replaces_the_default_config_env(tmp_path, monkeypatch):
    default = tmp_path / "config" / "handoffkeep" / "config.env"
    default.parent.mkdir(parents=True)
    default.write_text(f"{CF_ID_KEY}=default-id\n{CF_SECRET_KEY}=default-secret\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("HANDOFFKEEP_CONFIG")
    assert bench._handoffkeep_cf_access() == ("default-id", "default-secret")
    _dotenv(
        tmp_path, monkeypatch, f"{CF_ID_KEY}={FIX_ID}\n{CF_SECRET_KEY}={FIX_SECRET}\n", name="override.env"
    )
    assert bench._handoffkeep_cf_access() == (FIX_ID, FIX_SECRET)
    monkeypatch.setenv("HANDOFFKEEP_CONFIG", str(tmp_path / "missing.env"))
    assert bench._handoffkeep_cf_access() is None  # an override is honoured as written


@pytest.mark.parametrize("blank", ["", "   ", "\t \t"])
def test_whitespace_only_is_absent(tmp_path, monkeypatch, blank):
    monkeypatch.setenv(CF_ID_KEY, blank)
    monkeypatch.setenv(CF_SECRET_KEY, blank)
    assert bench._handoffkeep_cf_access() is None
    # A blank env value falls through to config.env, like an unset one.
    _dotenv(tmp_path, monkeypatch, f'{CF_ID_KEY}= {FIX_ID} \n{CF_SECRET_KEY}=" {FIX_SECRET} "\n')
    assert bench._handoffkeep_cf_access() == (FIX_ID, FIX_SECRET)
    _dotenv(tmp_path, monkeypatch, f"{CF_ID_KEY}={blank}\n{CF_SECRET_KEY}='{blank}'\n")
    assert bench._handoffkeep_cf_access() is None


@pytest.mark.parametrize(
    ("present", "value", "missing"),
    [(CF_ID_KEY, FIX_ID, CF_SECRET_KEY), (CF_SECRET_KEY, FIX_SECRET, CF_ID_KEY)],
)
@pytest.mark.parametrize("source", ["env", "config.env"])
def test_one_key_only_is_a_config_error_naming_the_missing_key(
    tmp_path, monkeypatch, present, value, missing, source
):
    if source == "env":
        monkeypatch.setenv(present, value)
    else:
        _dotenv(tmp_path, monkeypatch, f"{present}={value}\n")
    with pytest.raises(bench.BenchCfAccessError) as info:
        bench._handoffkeep_cf_access()
    assert info.value.reason == "cf_access_config_incomplete"
    message = str(info.value)
    assert message.startswith(f"{missing} is missing")
    assert value not in message
    _no_secret(_exc_text(info.value))


def test_one_key_only_fails_the_hk_request_without_sending_anything(hk, monkeypatch):
    _env(monkeypatch, hk.base_url)
    monkeypatch.delenv(CF_SECRET_KEY)
    with pytest.raises(bench.BenchCfAccessError) as info:
        bench._fetch_catalog(_catalog_backend())
    assert CF_SECRET_KEY in str(info.value)
    _no_secret(_exc_text(info.value))
    assert hk.received == []


@pytest.mark.parametrize("bad", [f"{FIX_SECRET}\r\nX-Injected: 1", f"{FIX_SECRET} tail", f"{FIX_SECRET}é"])
def test_a_value_no_header_can_carry_is_refused_by_key_name(hk, monkeypatch, bad):
    _env(monkeypatch, hk.base_url)
    monkeypatch.setenv(CF_SECRET_KEY, bad)
    with pytest.raises(bench.BenchCfAccessError) as info:
        bench._fetch_catalog(_catalog_backend())
    assert info.value.reason == "cf_access_config_invalid"
    assert CF_SECRET_KEY in str(info.value)
    _no_secret(_exc_text(info.value))
    assert hk.received == []


# ------------------------------------------------------------ AC3 redirects


def _catalog_call():
    try:
        return bench._handoffkeep_request(_catalog_backend(), "catalog"), None
    except Exception as exc:  # the assertion, not the exception type, decides RED
        return None, exc


@pytest.mark.parametrize("code", REDIRECT_CODES)
def test_same_origin_redirect_is_followed_with_credentials(hk, monkeypatch, code):
    _env(monkeypatch, hk.base_url)
    hk.action, hk.action_path = redirect(code, f"{hk.base_url}/v1/bench/moved/catalog"), "/v1/bench/catalog"
    result, err = _catalog_call()
    assert err is None
    assert result == {"catalog": []}
    assert [path for _, path, _ in hk.received] == ["/v1/bench/catalog", "/v1/bench/moved/catalog"]
    assert hk.received[1][2]["cf-access-client-secret"] == FIX_SECRET


@pytest.mark.parametrize("code", REDIRECT_CODES)
def test_same_origin_redirect_with_case_and_trailing_dot_is_followed(hk, monkeypatch, code):
    _env(monkeypatch, hk.base_url)
    # 127.0.0.1 has no case; the host compare is exercised through localhost.
    base = f"http://localhost:{hk.port}"
    monkeypatch.setenv("HANDOFFKEEP_URL", base)
    hk.action = redirect(code, f"http://LOCALHOST.:{hk.port}/v1/bench/moved/catalog")
    hk.action_path = "/v1/bench/catalog"
    result, err = _catalog_call()
    assert err is None
    assert len(hk.received) == 2


def _refused_targets(hk, other):
    return {
        "cross-host": f"http://localhost:{hk.port}/x",
        "port change": f"http://127.0.0.1:{other.port}/x",
        "http->https": f"https://127.0.0.1:{hk.port}/x",
        "scheme-relative foreign port": f"//127.0.0.1:{other.port}/x",
        "scheme-relative foreign host": f"//localhost:{hk.port}/x",
        "lookalike login host": f"http://cloudflareaccess.com.127.0.0.1.nip.invalid:{hk.port}/x",
        "ftp": f"ftp://127.0.0.1:{hk.port}/x",
        "javascript": "javascript:alert(1)",
    }


@pytest.mark.parametrize("code", REDIRECT_CODES)
@pytest.mark.parametrize(
    "case",
    [
        "cross-host",
        "port change",
        "http->https",
        "scheme-relative foreign port",
        "scheme-relative foreign host",
        "lookalike login host",
        "ftp",
        "javascript",
    ],
)
def test_foreign_redirect_is_refused_and_the_target_gets_nothing(make_server, monkeypatch, code, case):
    hk, other = make_server(), make_server()
    _env(monkeypatch, hk.base_url)
    hk.action = redirect(code, _refused_targets(hk, other)[case])
    result, err = _catalog_call()
    assert result is None
    assert isinstance(err, bench.BenchBackendError)
    assert not isinstance(err, bench.BenchCfAccessError)
    # Only the original request reached any socket: nothing was re-sent, not
    # even a connection attempt that died before its request line.
    assert len(hk.received) == 1
    assert other.received == []
    assert (hk.connections, other.connections) == (1, 0)
    _no_secret(_exc_text(err))


@pytest.mark.parametrize("code", REDIRECT_CODES)
@pytest.mark.parametrize(
    "case",
    [
        "cross-host",
        "port change",
        "http->https",
        "scheme-relative foreign port",
        "scheme-relative foreign host",
    ],
)
def test_foreign_redirect_is_a_refusal_at_the_transport(make_server, code, case):
    hk, other = make_server(), make_server()
    hk.action = redirect(code, _refused_targets(hk, other)[case])
    with pytest.raises(HttpError) as info:
        request_json(f"{hk.base_url}/x", headers={"CF-Access-Client-Secret": FIX_SECRET})
    assert info.value.status == code
    assert str(info.value) == f"HTTP {code}: cross-origin redirect refused"
    assert other.received == [] and len(hk.received) == 1


@pytest.mark.parametrize("code", REDIRECT_CODES)
def test_https_to_http_downgrade_is_refused(make_server, tls_server_context, code):
    tls, plain = make_server(tls_server_context), make_server()
    tls.action = redirect(code, f"http://127.0.0.1:{plain.port}/x")
    with pytest.raises(HttpError) as info:
        request_json(
            f"{tls.base_url}/x",
            headers={"CF-Access-Client-Id": FIX_ID, "CF-Access-Client-Secret": FIX_SECRET},
            insecure=True,
        )
    assert info.value.status == code
    assert plain.received == []
    # Same host and port, scheme down: refused too.
    tls.action = redirect(code, f"http://127.0.0.1:{tls.port}/x")
    with pytest.raises(HttpError):
        request_json(f"{tls.base_url}/x", headers={"CF-Access-Client-Secret": FIX_SECRET}, insecure=True)


LOGIN_LOCATIONS = [
    "https://team.cloudflareaccess.com/cdn-cgi/access/login/hk?kid=1&redirect_url=%2Fv1",
    "https://TEAM.CloudFlareAccess.COM/cdn-cgi/access/login",
    "https://team.cloudflareaccess.com./cdn-cgi/access/login",
    "https://Team.CLOUDFLAREACCESS.com.:443/login",
    "https://cloudflareaccess.com/login",
    "https://a.b.team.cloudflareaccess.com/login",
    "//team.cloudflareaccess.com/login",
    "http://team.cloudflareaccess.com/login",
    "https://team.cloudflareaccess.com:notaport/login",
]


@pytest.mark.parametrize("code", REDIRECT_CODES)
@pytest.mark.parametrize("location", LOGIN_LOCATIONS)
@pytest.mark.parametrize("cf", [True, False])
def test_cf_access_login_redirect_is_a_named_error(hk, monkeypatch, code, location, cf):
    _env(monkeypatch, hk.base_url, cf=cf)
    hk.action = redirect(code, location)
    result, err = _catalog_call()
    assert result is None
    assert isinstance(err, bench.BenchCfAccessError)
    assert err.reason == "cf_access_login_redirect"
    message = str(err)
    assert "cf_access_login_redirect" in message
    assert "service token is missing or not allowed" in message
    assert "cloudflareaccess" not in message.lower()
    assert "redirect_url" not in message and "?" not in message
    _no_secret(_exc_text(err))
    assert len(hk.received) == 1


@pytest.mark.parametrize(
    "host",
    [
        "team.cloudflareaccess.com",
        "TEAM.CLOUDFLAREACCESS.COM",
        "x.CloudflareAccess.com.",
        "cloudflareaccess.com",
    ],
)
def test_login_host_match_is_case_and_dot_insensitive(host):
    assert sf_http.is_cf_access_login_host(host)


@pytest.mark.parametrize(
    "host", ["cloudflareaccess.com.evil.example", "notcloudflareaccess.com", "cloudflareaccess.co", "", None]
)
def test_login_host_match_is_anchored(host):
    assert not sf_http.is_cf_access_login_host(host)


MALFORMED_LOCATIONS = [
    f"http://[{FIX_SECRET}]/x",
    f"http://[::1{FIX_ID}]/x",
    f"http://127.0.0.1:{FIX_SECRET}/x",
    f"http://{FIX_TOKEN}:{FIX_SECRET}@[{FIX_ID}/x",
    f"https://team.cloudflareaccess.com:{FIX_SECRET}/login",
    f"http://exa mple/{FIX_SECRET}",
]


@pytest.mark.parametrize("location", MALFORMED_LOCATIONS)
def test_malformed_location_is_refused_without_echo(hk, monkeypatch, location):
    _env(monkeypatch, hk.base_url)
    hk.action = redirect(302, location)
    result, err = _catalog_call()
    assert result is None
    assert isinstance(err, bench.BenchBackendError)
    _no_secret(_exc_text(err))
    assert len(hk.received) == 1
    # The transport itself refuses it with fixed text, before any parser speaks.
    # (Shared request_json keeps main's printable-only contract and its chain;
    # the full-graph guarantee is the hk helper's, asserted above.)
    with pytest.raises(HttpError) as info:
        request_json(f"{hk.base_url}/x", classify_cf_login=True)
    _no_secret(str(info.value) + "".join(traceback.format_exception(info.value)))


# ------------------------------------------------------------ AC4 HTML


HTML_TYPES = ["text/html; charset=utf-8", "text/html", "TEXT/HTML; Charset=UTF-8", "application/xhtml+xml"]


@pytest.mark.parametrize("kind", HK_ONLY_KINDS)
@pytest.mark.parametrize("content_type", HTML_TYPES)
@pytest.mark.parametrize("cf", [True, False])
def test_html_answer_on_a_json_route_is_a_named_error(hk, monkeypatch, kind, content_type, cf):
    _env(monkeypatch, hk.base_url, cf=cf)
    hk.action = html(content_type)
    with pytest.raises(bench.BenchCfAccessError) as info:
        REQUEST_KINDS[kind]()
    assert info.value.reason == "unexpected_html_response"
    assert "service token is missing or not allowed" in str(info.value)
    _no_secret(_exc_text(info.value))


@pytest.mark.parametrize("body", ["", "{}", '{"catalog": []}'])
def test_html_is_refused_even_when_the_body_would_parse(hk, monkeypatch, body):
    """An empty HTML body used to fold into ``{}`` — a silent success."""
    _env(monkeypatch, hk.base_url)
    hk.action = html("text/html; charset=utf-8", body)
    with pytest.raises(bench.BenchCfAccessError):
        bench._handoffkeep_request(_catalog_backend(), "catalog")


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("cf", [True, False])
def test_quota_share_html_answer_is_named_then_folded_fail_open(hk, monkeypatch, method, cf):
    _env(monkeypatch, hk.base_url, cf=cf)
    hk.action = html()
    body = {"document": {}} if method == "PUT" else None
    with pytest.raises(bench.BenchCfAccessError) as info:
        bench._handoffkeep_send(
            f"{hk.base_url}/v1/documents/k",
            token=FIX_TOKEN,
            method=method,
            body=body,
            fetch=quota_share.request_json,
        )
    assert info.value.reason == "unexpected_html_response"
    # quota share stays fail-open: the named error becomes "no remote snapshot".
    assert quota_share._request(method, "k", body) is None


def test_json_routes_still_succeed_and_providers_still_accept_html_labelled_json(hk, monkeypatch):
    _env(monkeypatch, hk.base_url)
    assert bench._handoffkeep_request(_catalog_backend(), "catalog") == {"catalog": []}
    hk.action = html("text/html", '{"model": "x"}')
    # reject_html is opt-in: a provider mislabelling JSON keeps working as before.
    assert request_json(f"{hk.base_url}/provider") == {"model": "x"}


# ------------------------------------------------------------ AC5 leak (in-process)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_send_reraises_http_errors_without_body_or_chain(hk, monkeypatch, status):
    """The hk helper's own HttpError: status only, no echoed body, no chain to one."""
    _env(monkeypatch, hk.base_url)
    hk.action = echo_status(status)
    with pytest.raises(HttpError) as info:
        bench._handoffkeep_send(f"{hk.base_url}/v1/documents/k", token=FIX_TOKEN)
    exc = info.value
    assert (exc.status, exc.body, str(exc)) == (status, "", f"HTTP {status}")
    _assert_no_retained_exception(exc)
    _no_secret(_exc_text(exc))


LEAK_ACTIONS = {
    "500 echo": lambda hk, other: echo_status(500),
    "401 echo": lambda hk, other: echo_status(401),
    "200 html echo": lambda hk, other: echo_status(200, "text/html"),
    "302 foreign with values in query": lambda hk, other: redirect(
        302, f"http://127.0.0.1:{other.port}/steal?t={FIX_TOKEN}&i={FIX_ID}&s={FIX_SECRET}"
    ),
    "302 login with values in query": lambda hk, other: redirect(
        302, f"https://team.cloudflareaccess.com/login?t={FIX_TOKEN}&i={FIX_ID}&s={FIX_SECRET}"
    ),
    "302 malformed": lambda hk, other: redirect(302, f"http://[{FIX_TOKEN}{FIX_ID}{FIX_SECRET}]/x"),
    "302 malformed port": lambda hk, other: redirect(302, f"http://127.0.0.1:{FIX_SECRET}/x"),
}


@pytest.mark.parametrize("case", sorted(LEAK_ACTIONS))
def test_echoed_credentials_never_reach_error_text(make_server, monkeypatch, capsys, case):
    hk, other = make_server(), make_server()
    _env(monkeypatch, hk.base_url)
    hk.action = LEAK_ACTIONS[case](hk, other)
    for run in REQUEST_KINDS.values():
        try:
            run()
        except Exception as exc:
            _no_secret(_exc_text(exc))
    # read_catalog swallows and warns — the warning must be clean too.
    bench.read_catalog()
    captured = capsys.readouterr()
    _no_secret(captured.out + captured.err)
    assert other.received == []


# ------------------------------------------------------------ AC6 status line


def _status_cf_lines(report: str) -> list[str]:
    return [line for line in report.splitlines() if "cf_access" in line or "CF_ACCESS" in line]


def test_status_says_configured_and_nothing_else(hk, monkeypatch):
    _env(monkeypatch, hk.base_url)
    report = bench.catalog_status_report()
    lines = _status_cf_lines(report)
    assert len(lines) == 1 and " cf_access=configured " in lines[0]
    _no_secret(report)


def test_status_says_absent(hk, monkeypatch):
    _env(monkeypatch, hk.base_url, cf=False)
    report = bench.catalog_status_report()
    lines = _status_cf_lines(report)
    assert len(lines) == 1 and " cf_access=absent " in lines[0]


def test_status_names_the_missing_half_without_values(hk, monkeypatch):
    _env(monkeypatch, hk.base_url)
    monkeypatch.delenv(CF_ID_KEY)
    report = bench.catalog_status_report()
    assert " cf_access=absent " in report
    assert f"blocked: {CF_ID_KEY} is missing" in report
    _no_secret(report)


# ------------------------------------------------------------ round 2: broken HTTP framing


class _RawServer:
    """A loopback socket that answers each connection with fixed raw bytes.

    http.server cannot emit a malformed status line or lie about
    Content-Length, so this one writes the bytes itself. It still reads the
    request head first, and records it, so the test can tell a request was made.
    """

    def __init__(self, response: bytes):
        self.response = response
        self.requests: list[bytes] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(2.0)
                head = b""
                try:
                    while b"\r\n\r\n" not in head:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        head += chunk
                    self.requests.append(head)
                    conn.sendall(self.response)
                except OSError:
                    pass

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2.0)
        self.sock.close()


@pytest.fixture
def raw_server():
    servers = []

    def _make(response: bytes):
        server = _RawServer(response)
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.close()


_ECHO = f"{FIX_TOKEN} {FIX_ID} {FIX_SECRET}"
BROKEN_FRAMING = {
    # http.client.BadStatusLine carries a repr of the line the server sent.
    "malformed status line": f"HTTP/1.1 2x0 {_ECHO}\r\nContent-Length: 0\r\n\r\n".encode(),
    "non-HTTP status line": f"SSH-2.0 {_ECHO}\r\n\r\n".encode(),
    "bad HTTP version": f"HTTP/9 200 {_ECHO}\r\n\r\n".encode(),
    # http.client.IncompleteRead: Content-Length promises more than arrives.
    "truncated body": (
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 4096\r\n"
        f'Connection: close\r\n\r\n{{"catalog": "{_ECHO}'
    ).encode(),
    "truncated chunked body": (
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
        f"Connection: close\r\n\r\nzz{_ECHO}\r\n"
    ).encode(),
}


@pytest.mark.parametrize("case", sorted(BROKEN_FRAMING))
@pytest.mark.parametrize("kind", HK_ONLY_KINDS)
def test_broken_http_framing_is_a_fixed_backend_error(raw_server, monkeypatch, case, kind):
    server = raw_server(BROKEN_FRAMING[case])
    _env(monkeypatch, server.base_url)
    with pytest.raises(bench.BenchBackendError) as info:
        REQUEST_KINDS[kind]()
    assert str(info.value) == "handoffkeep request failed"
    _assert_no_retained_exception(info.value)
    _no_secret(_exc_text(info.value))
    assert len(server.requests) == 1


@pytest.mark.parametrize("case", sorted(BROKEN_FRAMING))
def test_quota_share_folds_broken_framing_fail_open(raw_server, monkeypatch, case):
    server = raw_server(BROKEN_FRAMING[case])
    _env(monkeypatch, server.base_url)
    assert quota_share._request("GET", "fixture-key") is None
    assert quota_share._request("PUT", "fixture-key", {"document": {}}) is None
    assert len(server.requests) == 2


@pytest.mark.parametrize("case", sorted(BROKEN_FRAMING))
def test_read_catalog_falls_back_to_the_snapshot_on_broken_framing(raw_server, monkeypatch, capsys, case):
    server = raw_server(BROKEN_FRAMING[case])
    _env(monkeypatch, server.base_url)
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert view.source == bench.CATALOG_SOURCE_SNAPSHOT
    assert view.stale
    captured = capsys.readouterr()
    _no_secret(captured.out + captured.err)
    assert len(server.requests) == 1


@pytest.mark.parametrize("case", sorted(BROKEN_FRAMING))
def test_read_catalog_falls_back_to_the_cache_on_broken_framing(
    raw_server, tmp_path, monkeypatch, capsys, case
):
    server = raw_server(BROKEN_FRAMING[case])
    _env(monkeypatch, server.base_url)
    config = tmp_path / "config" / "scopefuel" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[bench]\ncatalog_ttl_s = 0\n", encoding="utf-8")  # always re-read the server
    bench._commit_catalog_cache(path=None, entries=list(bench.catalog_snapshot()), backend=_catalog_backend())
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert view.source == bench.CATALOG_SOURCE_CACHE
    assert len(view.entries) == len(bench.catalog_snapshot())
    captured = capsys.readouterr()
    _no_secret(captured.out + captured.err)
    assert len(server.requests) == 1


@pytest.mark.parametrize("case", sorted(BROKEN_FRAMING))
def test_real_cli_survives_broken_framing_without_echo(raw_server, cli_env, case):
    server = raw_server(BROKEN_FRAMING[case])
    for name, argv in CLI_COMMANDS.items():
        before = len(server.requests)
        proc = _run_cli(cli_env, server.base_url, argv)
        _no_secret(proc.stdout)
        _no_secret(proc.stderr)
        assert "Traceback" not in proc.stderr, (name, proc.stderr[-400:])
        assert len(server.requests) > before, name
    proc = _run_cli(cli_env, server.base_url, ["bench", "catalog", "status"])
    assert proc.returncode == 0, proc.stderr[-400:]


def test_read_catalog_warns_once_with_the_named_reason(hk, monkeypatch, capsys):
    _env(monkeypatch, hk.base_url)
    hk.action = redirect(302, "https://team.cloudflareaccess.com/login")
    view = bench.read_catalog()
    assert view.source == bench.CATALOG_SOURCE_SNAPSHOT
    err = capsys.readouterr().err
    assert err.count("cf_access_login_redirect") == 1


# ------------------------------------------------------------ AC5 leak (real CLI)


STUB_BINARIES = (
    "claude",
    "codex",
    "devin",
    "agy",
    "kimi",
    "grok",
    "kiro",
    "kiro-cli",
    "gemini",
    "cline",
    "opencode",
    "droid",
    "amp",
    "cursor-agent",
    "qwen",
    "security",
    "handoffkeep",
    "wrk",
    "herdr",
)


@pytest.fixture
def cli_env(tmp_path):
    """A throwaway HOME/XDG world; the parent environment is not inherited."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in STUB_BINARIES:
        stub = stubs / name
        stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.pathsep.join([str(stubs), str(pathlib.Path(sys.executable).parent), "/usr/bin", "/bin"]),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "HANDOFFKEEP_CONFIG": str(tmp_path / "hk-fixture.env"),
        "SCOPEFUEL_CACHE": str(tmp_path / "snapshots.json"),
        "SCOPEFUEL_SPEC_DIR": str(tmp_path / "specs"),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
    }
    return env


def _run_cli(env, url, argv):
    pathlib.Path(env["HANDOFFKEEP_CONFIG"]).write_text(
        f"HANDOFFKEEP_URL={url}\nHANDOFFKEEP_TOKEN={FIX_TOKEN}\n{CF_ID_KEY}={FIX_ID}\n{CF_SECRET_KEY}={FIX_SECRET}\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, "-c", "import sys; from scopefuel.cli import main; sys.exit(main())", *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


CLI_COMMANDS = {
    "bench catalog status": ["bench", "catalog", "status"],
    "--recommend": ["--recommend", "A", "--only", "codex"],
    "reps list": ["reps", "list"],
}


@pytest.mark.parametrize("case", sorted(LEAK_ACTIONS))
def test_real_cli_output_never_carries_the_credentials(make_server, cli_env, case):
    hk, other = make_server(), make_server()
    hk.action = LEAK_ACTIONS[case](hk, other)
    for name, argv in CLI_COMMANDS.items():
        before = len(hk.received)
        proc = _run_cli(cli_env, hk.base_url, argv)
        _no_secret(proc.stdout)
        _no_secret(proc.stderr)
        assert "Traceback" not in proc.stderr, (name, proc.stderr[-400:])
        assert len(hk.received) > before, name  # the hk path was exercised
    for _method, _path, headers in hk.received:
        assert headers["cf-access-client-secret"] == FIX_SECRET
    assert other.received == []


def test_real_cli_status_reports_cf_access_configured(make_server, cli_env):
    hk = make_server()
    proc = _run_cli(cli_env, hk.base_url, ["bench", "catalog", "status"])
    assert proc.returncode == 0, proc.stderr
    assert " cf_access=configured " in proc.stdout
    _no_secret(proc.stdout + proc.stderr)
    assert hk.received and hk.received[0][2]["user-agent"] == UA


# ------------------------------------------------------------ round 3 (tester BLOCKERs B1-B5)

IGNORED_NOTE = (
    "cf_access=absent (config.env CF keys ignored because HANDOFFKEEP_URL/TOKEN come from the environment)"
)


@pytest.fixture
def stored_cf(tmp_path, monkeypatch):
    """A config.env holding the full endpoint and the CF pair; the env starts clean."""

    def _write(url):
        _dotenv(
            tmp_path,
            monkeypatch,
            f"HANDOFFKEEP_URL={url}\nHANDOFFKEEP_TOKEN={FIX_TOKEN}\n{CF_ID_KEY}={FIX_ID}\n{CF_SECRET_KEY}={FIX_SECRET}\n",
        )

    monkeypatch.setattr(bench, "_WARNED_CF_ACCESS_STORED_IGNORED", False)
    return _write


# B1 (a): a whitespace-only env URL or TOKEN is unset, for the endpoint and the CF rule alike.
@pytest.mark.parametrize(
    "blank_keys", [("HANDOFFKEEP_URL",), ("HANDOFFKEEP_TOKEN",), ("HANDOFFKEEP_URL", "HANDOFFKEEP_TOKEN")]
)
def test_b1_whitespace_env_endpoint_is_unset(hk, stored_cf, monkeypatch, capsys, blank_keys):
    stored_cf(hk.base_url)
    for key in blank_keys:
        monkeypatch.setenv(key, " \t ")
    assert bench._handoffkeep_credentials() == (hk.base_url, FIX_TOKEN)
    assert bench._handoffkeep_cf_access_resolve() == ((FIX_ID, FIX_SECRET), False)
    bench._fetch_catalog(_catalog_backend())
    ((_, _, headers),) = hk.received
    assert headers["cf-access-client-secret"] == FIX_SECRET
    assert headers["authorization"] == f"Bearer {FIX_TOKEN}"
    assert "ignored" not in capsys.readouterr().err


# B1 (b): the rule skipping a stored CF pair is said, once, never silently.
# Supersedes the tester's test_contract_mixed_endpoint_does_not_discard_file_cf
# (builder ruling, round 3): the stored pair stays unused, and now says so.
@pytest.mark.parametrize(
    "env_keys", [("HANDOFFKEEP_URL",), ("HANDOFFKEEP_TOKEN",), ("HANDOFFKEEP_URL", "HANDOFFKEEP_TOKEN")]
)
def test_b1_status_says_stored_cf_keys_are_ignored(hk, stored_cf, monkeypatch, env_keys):
    stored_cf(hk.base_url)
    values = {"HANDOFFKEEP_URL": hk.base_url, "HANDOFFKEEP_TOKEN": FIX_TOKEN}
    for key in env_keys:
        monkeypatch.setenv(key, values[key])
    assert bench._handoffkeep_cf_access_resolve() == (None, True)
    report = bench.catalog_status_report()
    assert f" {IGNORED_NOTE} " in report
    assert report.count("CF keys ignored") == 1
    _no_secret(report)


def test_b1_first_hk_request_says_it_once_on_stderr(hk, stored_cf, monkeypatch, capsys):
    """Same endpoint in env and file — the tester's mixed case: no CF headers, one note."""
    stored_cf(hk.base_url)
    monkeypatch.setenv("HANDOFFKEEP_URL", hk.base_url)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", FIX_TOKEN)
    bench._fetch_catalog(_catalog_backend())
    bench._fetch_grades(_catalog_backend())
    quota_share._request("GET", "fixture-key")
    err = capsys.readouterr().err
    assert err.count(IGNORED_NOTE) == 1
    assert err.strip() == f"note: {IGNORED_NOTE}"
    _no_secret(err)
    assert len(hk.received) == 3
    for _method, _path, headers in hk.received:
        assert not [name for name in headers if name.startswith("cf-")]


def test_b1_no_note_when_nothing_was_skipped(hk, stored_cf, tmp_path, monkeypatch, capsys):
    # env carries the whole CF pair: config.env's pair would lose anyway.
    stored_cf(hk.base_url)
    _env(monkeypatch, hk.base_url)
    assert bench._handoffkeep_cf_access_resolve() == ((FIX_ID, FIX_SECRET), False)
    assert "ignored" not in bench.catalog_status_report()
    # config.env has no CF keys at all.
    _dotenv(tmp_path, monkeypatch, f"HANDOFFKEEP_URL={hk.base_url}\n", name="plain.env")
    monkeypatch.delenv(CF_ID_KEY)
    monkeypatch.delenv(CF_SECRET_KEY)
    assert bench._handoffkeep_cf_access_resolve() == (None, False)
    bench._fetch_catalog(_catalog_backend())
    assert "ignored" not in capsys.readouterr().err


# B1 (c): an env id with the secret only in the ignored config.env is the incomplete-pair error.
@pytest.mark.parametrize(
    ("env_key", "env_value", "missing"),
    [(CF_ID_KEY, FIX_ID, CF_SECRET_KEY), (CF_SECRET_KEY, FIX_SECRET, CF_ID_KEY)],
)
def test_b1_env_half_with_stored_half_is_incomplete(hk, stored_cf, monkeypatch, env_key, env_value, missing):
    stored_cf(hk.base_url)
    monkeypatch.setenv("HANDOFFKEEP_URL", hk.base_url)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", FIX_TOKEN)
    monkeypatch.setenv(env_key, env_value)
    with pytest.raises(bench.BenchCfAccessError) as info:
        bench._fetch_catalog(_catalog_backend())
    assert info.value.reason == "cf_access_config_incomplete"
    message = str(info.value)
    assert message.startswith(f"{missing} is missing")
    assert message.endswith(
        "(config.env CF keys ignored because HANDOFFKEEP_URL/TOKEN come from the environment)"
    )
    _no_secret(_exc_text(info.value))
    assert hk.received == []
    assert f"blocked: {missing} is missing" in bench.catalog_status_report()


# B1 (d) and the visible-ASCII note: the README states both.
def test_b1_readme_states_the_isolation_rule_and_the_charset():
    readme = (pathlib.Path(bench.__file__).parents[2] / "README.md").read_text(encoding="utf-8")
    assert "**격리 규칙.**" in readme
    assert IGNORED_NOTE in readme
    assert "보이는 ASCII(0x21–0x7E" in readme


# B2: no retained exception anywhere on the hk path, not merely a hidden one.
B2_RAW = {
    "malformed status line": BROKEN_FRAMING["malformed status line"],
    "invalid chunk size": BROKEN_FRAMING["truncated chunked body"],
    "truncated body": BROKEN_FRAMING["truncated body"],
}


@pytest.mark.parametrize("case", sorted(B2_RAW))
def test_b2_send_failure_graph_is_empty(raw_server, monkeypatch, case):
    server = raw_server(B2_RAW[case])
    _env(monkeypatch, server.base_url)
    with pytest.raises(bench.BenchBackendError) as info:
        bench._handoffkeep_send(server.base_url, token=FIX_TOKEN)
    assert str(info.value) == "handoffkeep request failed"
    _assert_no_retained_exception(info.value)
    _no_secret(_exception_graph(info.value))


@pytest.mark.parametrize("kind", HK_ONLY_KINDS)
def test_b2_hostile_500_body_graph_is_empty_at_every_site(hk, monkeypatch, kind):
    _env(monkeypatch, hk.base_url)
    hk.action = echo_status(500)
    with pytest.raises(bench.BenchBackendError) as info:
        REQUEST_KINDS[kind]()
    _assert_no_retained_exception(info.value)
    _no_secret(_exc_text(info.value))


@pytest.mark.parametrize(
    "action",
    [
        lambda: redirect(302, f"https://TEAM.CLOUDFLAREACCESS.COM./login?s={FIX_SECRET}"),
        lambda: echo_status(200, "text/html"),
        lambda: echo_status(404),
    ],
    ids=["login redirect", "html", "404"],
)
def test_b2_named_errors_retain_nothing(hk, monkeypatch, action):
    _env(monkeypatch, hk.base_url)
    hk.action = action()
    with pytest.raises(bench.BenchBackendError) as info:
        bench._handoffkeep_request(_catalog_backend(), "catalog")
    _assert_no_retained_exception(info.value)
    _no_secret(_exc_text(info.value))


# B3: a valid JSON answer whose fields carry the canaries.
def json_answer(payload):
    def _action(handler, headers):
        handler.send_json(200, payload)

    return _action


_STAMP = "2026-10-08T00:00:00Z"
B3_ROWS = {
    "scores source": (
        "scores GET",
        {
            "scores": [
                {
                    "model_id": "m",
                    "source": FIX_SECRET,
                    "metric": "intelligence",
                    "score": 1,
                    "captured_at": _STAMP,
                }
            ]
        },
        "handoffkeep returned invalid score data (row 0 field source)",
    ),
    "scores captured_at": (
        "scores GET",
        {
            "scores": [
                {
                    "model_id": "m",
                    "source": "AA-model",
                    "metric": "intelligence",
                    "score": 1,
                    "captured_at": _STAMP,
                },
                {
                    "model_id": "m",
                    "source": "AA-model",
                    "metric": "intelligence",
                    "score": 1,
                    "captured_at": FIX_TOKEN,
                },
            ]
        },
        "handoffkeep returned invalid score data (row 1 field captured_at)",
    ),
    "grades grade": (
        "grades GET",
        {"grades": [{"profile": FIX_ID, "grade": FIX_SECRET, "deviation_ref": FIX_TOKEN}]},
        "handoffkeep returned invalid grade data (row 0 field grade)",
    ),
    "catalog gate": (
        "catalog GET",
        {"catalog": [{"profile": "p", "grade": "A"}, {"profile": FIX_ID, "grade": "A", "gate": FIX_SECRET}]},
        "handoffkeep returned invalid catalog data (row 1 field gate)",
    ),
    "catalog score": (
        "catalog GET",
        {"catalog": [{"profile": FIX_ID, "grade": "A", "score": FIX_TOKEN}]},
        "handoffkeep returned invalid catalog data (row 0 field score)",
    ),
    "reps recorded_at": (
        "reps GET (list)",
        {"reps": [{"id": 1, "origin_id": 1, "profile": FIX_ID, "recorded_at": FIX_SECRET}]},
        "handoffkeep returned invalid rep data (row 0 field recorded_at)",
    ),
    "reps rounds": (
        "reps GET (list)",
        {"reps": [{"id": 1, "origin_id": 1, "profile": FIX_ID, "recorded_at": _STAMP, "rounds": FIX_TOKEN}]},
        "handoffkeep returned invalid rep data (row 0 field rounds)",
    ),
    "reps row not an object": (
        "reps GET (list)",
        {"reps": [FIX_SECRET]},
        "handoffkeep returned invalid rep data (row 0)",
    ),
}


@pytest.mark.parametrize("case", sorted(B3_ROWS))
def test_b3_decoder_errors_name_the_field_never_the_value(hk, monkeypatch, case):
    kind, payload, expected = B3_ROWS[case]
    _env(monkeypatch, hk.base_url)
    hk.action = json_answer(payload)
    with pytest.raises(bench.BenchBackendError) as info:
        REQUEST_KINDS[kind]()
    assert str(info.value) == expected
    _assert_no_retained_exception(info.value)
    _no_secret(_exc_text(info.value))


def test_b3_captured_at_retains_no_parser_error():
    with pytest.raises(bench.BenchError) as info:
        bench._captured_at(f"not-a-date-{FIX_SECRET}")
    assert str(info.value) == "captured_at must be ISO-8601"
    _assert_no_retained_exception(info.value)
    _no_secret(_exc_text(info.value))


def test_b3_read_catalog_falls_back_on_a_rejected_row(hk, monkeypatch, capsys):
    _env(monkeypatch, hk.base_url)
    hk.action = json_answer({"catalog": [{"profile": FIX_ID, "grade": FIX_SECRET}]})
    bench.reset_catalog_memo()
    assert bench.read_catalog().source == bench.CATALOG_SOURCE_SNAPSHOT
    captured = capsys.readouterr()
    _no_secret(captured.out + captured.err)


# B4: the login diagnosis is made before (and without) reading a broken body.
_LOGIN = "https://TEAM.CLOUDFLAREACCESS.COM./login"
B4_RAW = {
    "truncated Content-Length": (
        f"HTTP/1.1 302 Found\r\nLocation: {_LOGIN}\r\nContent-Length: 4096\r\n"
        f"Connection: close\r\n\r\n{_ECHO}"
    ).encode(),
    "invalid chunk framing": (
        f"HTTP/1.1 302 Found\r\nLocation: {_LOGIN}\r\nTransfer-Encoding: chunked\r\n"
        f"Connection: close\r\n\r\nzz{_ECHO}\r\n"
    ).encode(),
}


@pytest.mark.parametrize("case", sorted(B4_RAW))
def test_b4_login_redirect_with_a_broken_body_keeps_its_name(raw_server, monkeypatch, case):
    server = raw_server(B4_RAW[case])
    _env(monkeypatch, server.base_url)
    with pytest.raises(bench.BenchCfAccessError) as info:
        bench._handoffkeep_send(server.base_url, token=FIX_TOKEN)
    assert info.value.reason == "cf_access_login_redirect"
    _assert_no_retained_exception(info.value)
    _no_secret(_exc_text(info.value))
    with pytest.raises(bench.BenchCfAccessError) as info:
        bench._fetch_catalog(_catalog_backend())
    assert info.value.reason == "cf_access_login_redirect"


@pytest.mark.parametrize("framing", ["truncated", "chunked"])
def test_b4_generic_refused_redirect_with_a_broken_body_is_classified_first(raw_server, framing):
    location = "http://127.0.0.1:9/elsewhere"
    if framing == "truncated":
        wire = (
            f"HTTP/1.1 307 Temporary Redirect\r\nLocation: {location}\r\nContent-Length: 4096\r\n\r\n{_ECHO}"
        )
    else:
        wire = (
            f"HTTP/1.1 307 Temporary Redirect\r\nLocation: {location}\r\nTransfer-Encoding: chunked\r\n"
            f"\r\nzz{_ECHO}\r\n"
        )
    server = raw_server(wire.encode())
    with pytest.raises(HttpError) as info:
        request_json(server.base_url, classify_cf_login=True)
    assert type(info.value) is HttpError
    assert str(info.value) == "HTTP 307: cross-origin redirect refused"


# B5: providers see main's refusal for a login-host Location, unchanged.
MAIN_REFUSAL = ("HttpError", "HTTP 302: cross-origin redirect refused")


@pytest.mark.parametrize(
    "location",
    [
        "https://TEAM.CLOUDFLAREACCESS.COM/login",
        "https://TEAM.CLOUDFLAREACCESS.COM./login",
        "https://team.cloudflareaccess.com/cdn-cgi/access/login",
        "//team.cloudflareaccess.com/login",
    ],
)
def test_b5_provider_login_redirect_matches_main(hk, tmp_path, monkeypatch, location):
    from scopefuel.providers import codex

    _env(monkeypatch, hk.base_url)  # CF keys present: still nothing CF-specific for a provider
    auth = tmp_path / "codex-home" / "auth.json"
    auth.parent.mkdir()
    auth.write_text('{"tokens": {"access_token": "provider-token"}}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(auth.parent))
    monkeypatch.setattr(codex, "USAGE_URL", f"{hk.base_url}/provider")
    hk.action = redirect(302, location)
    try:
        codex.fetch()
        outcome = ("ok", "")
    except Exception as exc:
        outcome = (type(exc).__name__, str(exc))
    assert outcome == MAIN_REFUSAL
    # The shared transport without the hk flag, directly.
    with pytest.raises(HttpError) as info:
        request_json(f"{hk.base_url}/provider")
    assert (type(info.value).__name__, str(info.value)) == MAIN_REFUSAL
    for _method, _path, headers in hk.received:
        assert not [name for name in headers if name.startswith("cf-")]


# ------------------------------------------------------------ round 4: provider pre-check gated


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("same-origin", ("ok", {"ok": True, "model": "probe-model"}, 2)),
        ("javascript", ("HttpError", "HTTP 302", 1)),
        ("data", ("HttpError", "HTTP 302", 1)),
        ("file", ("HttpError", "HTTP 302", 1)),
    ],
)
def test_unflagged_handler_follows_provider_redirects_as_main(hk, case, expected):
    # Without the hk flag the raw Location pre-check is skipped: CPython's own
    # redirect handling (main's) decides, so a followed redirect is followed and a
    # disallowed scheme is main's bare 3xx, not the hk refusal text.
    base = f"http://localhost:{hk.port}"
    location = {
        "same-origin": f"http://LOCALHOST:{hk.port}/moved",
        "javascript": "javascript:alert(1)",
        "data": "data:text/plain,x",
        "file": "file:///etc/hosts",
    }[case]
    hk.action = redirect(302, location)
    hk.action_path = "/provider"
    try:
        outcome = ("ok", request_json(f"{base}/provider"))
    except HttpError as exc:
        outcome = (type(exc).__name__, str(exc))
    assert (*outcome, len(hk.received)) == expected
