"""task #653/#654 — 계정당 측정 1곳 → hk 문서 저장 → 다른 호스트 읽기.

hk 는 dict-backed fake 이다 — ``quota_share.request_json`` 을 바꿔 실 네트워크
호출이 없다. usage API 도 fetcher 스텁이다. conftest 가 HANDOFFKEEP_URL/TOKEN 을
지워주므로 자격을 심은 테스트에서만 기능이 켜진다.

뮤턴트 계약(파일에서 세는 대상 — 각 테스트 주석에 'M#' 표기):
- M1  페이로드에 token/자격 필드 추가 → 정확한 키 집합 검사가 RED
- M2  REMOTE_MAX_AGE_S 상향(16분 수용) → 낡은 스냅샷 테스트가 RED
- M3  계정 지문 일치 검사 제거 → mismatch 테스트가 RED
- M4  신선 로컬 결과도 원격으로 덮기 → not-eligible 테스트가 RED
- M5  expiresAt 사전 검사 제거 → 만료 테스트가 RED(usage 호출이 일어남)
- M6  publish 가 usage API 를 추가 호출 → 호출 카운터 테스트가 RED
"""

from __future__ import annotations

import datetime as dt
import json
import socket
import urllib.parse

import pytest

from scopefuel import cache, cli, quota_share, refresh
from scopefuel.http import HttpError
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.providers import claude
from scopefuel.quota_v2_contract import Attempt
from scopefuel.recommend import gate_check

NOW = dt.datetime(2026, 9, 24, 12, 0, 0, tzinfo=dt.UTC)
EPOCH = NOW.timestamp()
TODAY = NOW.date()
FP_A = "fp-account-aaaa1111"
FP_B = "fp-account-bbbb2222"
HK_URL = "https://hk.invalid"
HOST = socket.gethostname()


class FakeHk:
    """PUT/GET /v1/documents/{key} 를 dict 로 흉내 낸다 (hk 서버 계약 모사)."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.puts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    def __call__(self, url, *, method="GET", headers=None, body=None, timeout=None, **_kw):
        assert (headers or {}).get("Authorization") == "Bearer hk-test-token"
        assert str(url).startswith(f"{HK_URL}/v1/documents/")
        key = urllib.parse.unquote(str(url).split("/v1/documents/", 1)[1])
        if method == "PUT":
            assert isinstance(body, dict)
            self.docs[key] = dict(body)
            self.puts.append((key, dict(body)))
            return {"document": {"key": key, **body}, "changed": True}
        assert method == "GET"
        self.gets.append(key)
        doc = self.docs.get(key)
        if doc is None:
            raise HttpError(404, "not_found")
        return {"key": key, "kind": doc["kind"], "session": doc["session"], "body": doc["body"]}


def _enable(monkeypatch) -> FakeHk:
    hk = FakeHk()
    monkeypatch.setenv("HANDOFFKEEP_URL", HK_URL)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", "hk-test-token")
    monkeypatch.setattr(quota_share, "request_json", hk)
    return hk


def _bucket(window: str, used: float, *, hours_ahead: float = 4.0, base: dt.datetime = NOW) -> Bucket:
    resets = (base + dt.timedelta(hours=hours_ahead)).isoformat()
    return Bucket(
        label=window,
        window=window,
        used_pct=used,
        resets_at=resets,
        scope=Scope("account"),
        horizon="now" if window == "5h" else "week",
    )


def _ok(fp: str | None = FP_A, *, session_fp: str | None = "sess-writer-1") -> ProviderResult:
    return ProviderResult(
        id="claude",
        plan="claude_max",
        buckets=[_bucket("5h", 10.0), _bucket("7d", 30.0, hours_ahead=160.0)],
        source="oauth-usage-api",
        http_status=200,
        account_fp=fp,
        session_fp=session_fp,
    )


def _failing(error: str, *, kind: str, status: int | None = None, fp: str | None = FP_A):
    """claude.fetch 형태의 실패 fetcher — probe·pool_class 메타데이터 포함."""

    def fetch() -> ProviderResult:
        return ProviderResult(
            id="claude",
            error=error,
            error_kind=kind,
            http_status=status,
            account_fp=fp,
        )

    fetch.current_account_fp = lambda: fp  # noqa: B023 — 로컬 전용 probe
    fetch.pool_class = "spend"  # noqa: B023 — BUILTIN 래핑과 같은 메타데이터
    return fetch


def _seed_remote(
    hk: FakeHk,
    *,
    pool: str = "claude",
    fp: str = FP_A,
    measured_at: float,
    host: str = "mbp-server",
    session_fp: str = "sess-publisher",
    used_5h: float = 10.0,
    used_7d: float = 30.0,
) -> str:
    """게시된 형태 그대로의 원격 스냅샷 문서를 fake 에 직접 심는다."""
    key = quota_share.key_for(pool, fp)

    def _entry(label, window, horizon, used, hours_ahead):
        return {
            "label": label,
            "window": window,
            "horizon": horizon,
            "used_pct": used,
            "resets_at": (NOW + dt.timedelta(hours=hours_ahead)).isoformat(),
            "scope": {"kind": "account", "name": None},
            "note": None,
            "measured_by": {"host": host, "session_fp": session_fp},
        }

    body = json.dumps(
        {
            "schema": quota_share.SCHEMA,
            "pool": pool,
            "account_fp": fp,
            "measured_at": dt.datetime.fromtimestamp(measured_at, dt.UTC).isoformat(),
            "measured_at_epoch": measured_at,
            "measured_by": {"host": host, "session_fp": session_fp},
            "source": "oauth-usage-api",
            "plan": "claude_max",
            "buckets": [
                _entry("5h", "5h", "now", used_5h, 4.0),
                _entry("7d", "7d", "week", used_7d, 160.0),
            ],
        }
    )
    hk.docs[key] = {
        "key": key,
        "kind": quota_share.DOC_KIND,
        "session": quota_share.DOC_SESSION,
        "job": "",
        "body": body,
    }
    return key


def _read_entries() -> dict:
    return json.loads(cache.cache_path().read_text(encoding="utf-8"))


def _walk_keys(obj: object):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


# ------------------------------------------------------------------ AC1: 게시


def test_collect_publishes_sanitized_snapshot(monkeypatch):
    """M1 — 토큰 형 필드를 싣는 뮤턴트는 정확한 키 집합 검사로 RED."""
    hk = _enable(monkeypatch)
    cache.collect({"claude": lambda: _ok()}, ["claude"], now=EPOCH)

    assert [key for key, _ in hk.puts] == [f"quota/claude/{FP_A}/latest"]
    _key, doc = hk.puts[0]
    assert doc["kind"] == quota_share.DOC_KIND
    assert doc["session"] == quota_share.DOC_SESSION
    payload = json.loads(doc["body"])
    assert set(payload) == {
        "schema",
        "pool",
        "account_fp",
        "measured_at",
        "measured_at_epoch",
        "measured_by",
        "source",
        "plan",
        "buckets",
    }
    assert payload["schema"] == quota_share.SCHEMA
    assert payload["pool"] == "claude"
    assert payload["account_fp"] == FP_A
    assert payload["measured_at_epoch"] == pytest.approx(EPOCH)
    assert payload["measured_by"] == {"host": HOST, "session_fp": "sess-writer-1"}
    # 값 단위 provenance(AC5): 어느 호스트·세션이 측정했는지가 버킷마다 붙는다.
    assert len(payload["buckets"]) == 2
    for bucket in payload["buckets"]:
        assert set(bucket) == {
            "label",
            "window",
            "horizon",
            "used_pct",
            "resets_at",
            "scope",
            "note",
            "measured_by",
        }
        assert set(bucket["scope"]) == {"kind", "name"}
        assert bucket["measured_by"] == {"host": HOST, "session_fp": "sess-writer-1"}
    used = {bucket["label"]: bucket["used_pct"] for bucket in payload["buckets"]}
    assert used == {"5h": 10.0, "7d": 30.0}
    # 어떤 키·값에도 토큰/자격 흔적이 없다 — 비가역 지문만 허용된다.
    forbidden = {
        "token",
        "access_token",
        "accesstoken",
        "authorization",
        "secret",
        "credential",
        "api_key",
        "apikey",
    }
    assert {str(key).lower() for key in _walk_keys(payload)}.isdisjoint(forbidden)
    assert "Bearer" not in doc["body"]


def test_refresh_worker_publishes(monkeypatch, capsys):
    """refresh 경로도 같은 게시를 한다 — publish 는 정상 실행의 부산물."""
    hk = _enable(monkeypatch)
    fetcher = lambda: _ok()  # noqa: E731
    fetcher.pool_class = "spend"
    assert refresh.run_worker({"claude": fetcher}, "claude") == 0
    assert [key for key, _ in hk.puts] == [f"quota/claude/{FP_A}/latest"]
    capsys.readouterr()


def test_publish_skipped_without_account_fp(monkeypatch):
    """지문 없는 성공은 게시하지 않는다 — 읽는 쪽이 계정 일치를 증명할 수 없다."""
    hk = _enable(monkeypatch)
    cache.collect({"claude": lambda: _ok(fp=None)}, ["claude"], now=EPOCH)
    assert hk.puts == []


def test_publish_failure_is_fail_open(monkeypatch):
    """hk 장애가 측정 자체를 망가뜨리지 않는다 — 결과는 그대로 정상."""

    def _boom(*_a, **_k):
        raise OSError("hk unreachable")

    monkeypatch.setenv("HANDOFFKEEP_URL", HK_URL)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", "hk-test-token")
    monkeypatch.setattr(quota_share, "request_json", _boom)
    results = cache.collect({"claude": lambda: _ok()}, ["claude"], now=EPOCH)
    assert results[0].error is None
    assert results[0].buckets


def test_quota_share_disabled_env(monkeypatch):
    """SCOPEFUEL_QUOTA_SHARE=off 면 읽기·쓰기 모두 조용히 꺼진다(롤백 스위치)."""
    hk = _enable(monkeypatch)
    monkeypatch.setenv("SCOPEFUEL_QUOTA_SHARE", "off")
    cache.collect({"claude": lambda: _ok()}, ["claude"], now=EPOCH)
    assert hk.puts == []
    _seed_remote(hk, measured_at=EPOCH - 60.0)
    results = cache.collect(
        {"claude": _failing("HTTP 429", kind="rate_limited", status=429)},
        ["claude"],
        now=EPOCH,
        use_cache=False,
    )
    assert results[0].source != quota_share.REMOTE_SOURCE
    assert hk.gets == []


def test_no_hk_credentials_disables_share(monkeypatch):
    """자격이 없으면 기능이 꺼진다 — request_json 까지 가지도 않는다."""
    calls: list = []
    monkeypatch.setattr(quota_share, "request_json", lambda *a, **k: calls.append(1) or {})
    cache.collect({"claude": lambda: _ok()}, ["claude"], now=EPOCH)
    results = cache.collect(
        {"claude": _failing("HTTP 429", kind="rate_limited", status=429)},
        ["claude"],
        now=EPOCH + 400,
        use_cache=False,
    )
    assert results[0].source != quota_share.REMOTE_SOURCE
    assert calls == []


# ------------------------------------------------------------------ AC2: 읽기


def test_remote_snapshot_used_when_local_429(monkeypatch):
    """AC2 — 로컬 429 시 신선한 원격 스냅샷이 답한다. 라벨·class 검증."""
    hk = _enable(monkeypatch)
    _seed_remote(hk, measured_at=EPOCH - 60.0)
    results = cache.collect(
        {"claude": _failing("HTTP 429: rate limit", kind="rate_limited", status=429)},
        ["claude"],
        now=EPOCH,
        use_cache=False,
    )
    remote = results[0]
    assert remote.error is None
    assert remote.stale is False
    assert remote.source == quota_share.REMOTE_SOURCE
    assert remote.note == "remote measured (mbp-server)"  # 자기신고 라벨이 아니다
    assert remote.pool_class == "spend"  # pace 와 같은 경로 — preserve 고정 아님
    assert remote.account_fp == FP_A
    assert remote.account_fp_match is True
    assert remote.session_fp == "sess-publisher"
    assert remote.age_s == pytest.approx(60.0)
    assert remote.last_error == "HTTP 429: rate limit"  # 대체된 로컬 실패는 감사로 남는다
    assert {b.label: b.used_pct for b in remote.buckets} == {"5h": 10.0, "7d": 30.0}
    # pace 기반 판정이 로컬 측정과 같다 — 게이트가 spend 로 통과한다.
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is True
    assert res.pool_class == "spend"
    assert res.unmeasurable is False


def test_remote_snapshot_refused_at_16_minutes(monkeypatch):
    """M2 — 15분을 넘은 스냅샷은 거부한다 (16분은 이미 낡음)."""
    hk = _enable(monkeypatch)
    _seed_remote(hk, measured_at=EPOCH - 16 * 60.0)
    results = cache.collect(
        {"claude": _failing("HTTP 429: rate limit", kind="rate_limited", status=429)},
        ["claude"],
        now=EPOCH,
        use_cache=False,
    )
    assert results[0].source != quota_share.REMOTE_SOURCE
    assert results[0].error is not None
    assert hk.gets == [f"quota/claude/{FP_A}/latest"]  # 조회는 했으나 거부


def test_remote_snapshot_refused_on_account_mismatch(monkeypatch):
    """M3 — 다른 계정의 스냅샷은 지문이 달라 거부한다.

    키 경로에 지문이 박혀 있어 다른 계정 문서는 보통 조회조차 되지 않는다 —
    방어 심화로 본문 지문도 대조하는 두 경우를 다 검증한다.
    """
    hk = _enable(monkeypatch)
    # 다른 계정의 문서가 올바른 위치에 있음 → 조회 키가 달라 아예 미스
    _seed_remote(hk, fp=FP_B, measured_at=EPOCH - 60.0)
    # 같은 키 아래 본문 지문이 어긋난 문서 → 본문 대조에서 거부
    key = quota_share.key_for("claude", FP_A)
    _seed_remote(hk, fp=FP_A, measured_at=EPOCH - 60.0)
    tampered = json.loads(hk.docs[key]["body"])
    tampered["account_fp"] = FP_B
    hk.docs[key]["body"] = json.dumps(tampered)

    results = cache.collect(
        {"claude": _failing("HTTP 401", kind="auth", status=401)},
        ["claude"],
        now=EPOCH,
        use_cache=False,
    )
    assert results[0].source != quota_share.REMOTE_SOURCE
    assert results[0].error_kind == "auth"


def test_remote_refused_without_local_fp(monkeypatch):
    """이 호스트의 계정을 증명할 수 없으면(probe 가 None) 읽지 않는다."""
    hk = _enable(monkeypatch)
    _seed_remote(hk, measured_at=EPOCH - 60.0)
    results = cache.collect(
        {"claude": _failing("자격증명 없음", kind="credentials", fp=None)},
        ["claude"],
        now=EPOCH,
        use_cache=False,
    )
    assert results[0].source != quota_share.REMOTE_SOURCE
    assert hk.gets == []


def test_remote_not_read_when_local_fresh(monkeypatch):
    """M4 — 신선한 로컬 성공은 원격을 조회조차 하지 않는다."""
    hk = _enable(monkeypatch)
    _seed_remote(hk, measured_at=EPOCH - 60.0, used_5h=99.0)
    results = cache.collect({"claude": lambda: _ok()}, ["claude"], now=EPOCH)
    assert results[0].source == "oauth-usage-api"
    assert results[0].note != "remote measured (mbp-server)"
    assert hk.gets == []
    assert len(hk.puts) == 1  # 로컬 성공은 게시만 한다


def test_remote_read_does_not_write_local_cache_or_backoff(monkeypatch):
    """AC5 — 원격 값은 로컬 측정이 아니다: 캐시 스냅샷·backoff 를 오염시키지 않는다."""
    hk = _enable(monkeypatch)
    _seed_remote(hk, measured_at=EPOCH - 60.0)
    results = cache.collect(
        {"claude": _failing("HTTP 429", kind="rate_limited", status=429)},
        ["claude"],
        now=EPOCH,
        use_cache=False,
    )
    assert results[0].source == quota_share.REMOTE_SOURCE
    entries = _read_entries()
    # 실패 감사는 남지만 스냅샷 본문(result)은 원격 값으로 덮이지 않는다.
    assert entries["claude"].get("result", {}).get("source") != quota_share.REMOTE_SOURCE
    assert entries["claude"].get("fetched_at") == 0


def test_remote_label_distinct_from_manual(monkeypatch, capsys):
    """AC2 라벨 — 게이트 첫 줄이 'remote measured (host)' 를 보인다."""
    hk = _enable(monkeypatch)
    _seed_remote(hk, measured_at=dt.datetime.now(dt.UTC).timestamp() - 60.0)
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"claude": _failing("HTTP 429", kind="rate_limited", status=429)},
    )
    # bench 가 실 hk 를 두드리지 않게 스텁 — quota_share 만 테스트 대상이다.
    import urllib.error

    def _offline(*_a, **_k):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("scopefuel.bench.request_json", _offline)
    rc = cli.main(["gate", "-m", "opus", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    first = out.out.splitlines()[0]
    assert "source=remote" in first
    assert 'source_label="remote measured (mbp-server)"' in first
    assert "source=operator" not in out.out  # 자기신고와 구분된다
    assert "자기신고" not in first


# ------------------------------------------------------------------ AC5/AC6


def test_publisher_429_does_not_follow_snapshot(monkeypatch):
    """AC5 — 한 호스트의 429 는 스냅샷에 실리지 않는다: 문서에는 오류·backoff 없음.

    읽는 쪽은 발행자의 실패를 보지 않는다 — measured_by provenance 만 남는다.
    """
    hk = _enable(monkeypatch)
    cache.collect({"claude": lambda: _ok()}, ["claude"], now=EPOCH)
    _key, doc = hk.puts[0]
    assert "error" not in doc["body"]
    assert "rate_limited" not in doc["body"]
    assert "backoff" not in doc["body"]


def test_usage_call_count_unchanged_by_sharing(monkeypatch):
    """M6 — 게시·읽기가 usage API 호출 수를 늘리지 않는다(카운팅 스텁)."""
    hk = _enable(monkeypatch)
    _seed_remote(hk, measured_at=EPOCH - 60.0)
    calls = {"n": 0}

    def fetch() -> ProviderResult:
        calls["n"] += 1
        return _ok()

    fetch.current_account_fp = lambda: FP_A
    fetch.pool_class = "spend"

    cache.collect({"claude": fetch}, ["claude"], now=EPOCH)  # 측정 + 게시
    cache.collect({"claude": fetch}, ["claude"], now=EPOCH + 60)  # TTL 안 → 캐시 히트
    assert calls["n"] == 1  # usage API 는 1회 — 게시는 hk PUT 만 더한다
    assert len(hk.puts) == 1
    assert hk.gets == []  # 성공·캐시 히트는 원격을 읽지 않는다


# ------------------------------------------------------------------ AC3: 지문


def test_claude_account_fp_matches_across_tokens():
    """AC3 — 다른 호스트의 다른 토큰, 같은 계정 uuid → 같은 지문."""
    a = {"accessToken": "token-host-a", "subscriptionType": "max", "oauthAccount": {"accountUuid": "u-1"}}
    b = {"accessToken": "token-host-b", "subscriptionType": "max", "oauthAccount": {"accountUuid": "u-1"}}
    assert claude._account_fp(a) == claude._account_fp(b)
    # 세션 지문은 다르다 — 토큰을 따라 바뀌는 게 목적이다.
    assert claude._session_fp(a) != claude._session_fp(b)


def test_claude_account_fp_differs_across_accounts():
    a = {"accessToken": "t", "subscriptionType": "max", "oauthAccount": {"accountUuid": "u-1"}}
    b = {"accessToken": "t", "subscriptionType": "max", "oauthAccount": {"accountUuid": "u-2"}}
    assert claude._account_fp(a) != claude._account_fp(b)


def test_claude_account_fp_is_none_without_account_uuid():
    """uuid 가 없으면 토큰 해시로 폴백하지 않고 None — 호스트마다 다른 지문 방지."""
    assert claude._account_fp({"accessToken": "t"}) is None
    assert claude._account_fp({"accessToken": "t", "oauthAccount": {"accountUuid": "  "}}) is None


def test_session_fp_not_serialized():
    """세션 지문은 hk measured_by provenance 전용 — 출력·캐시에 새지 않는다."""
    result = _ok()
    assert "session_fp" not in result.as_dict()
    assert "session_fp" not in json.dumps(result.as_dict(include_raw=True))


# ------------------------------------------------------------------ AC4: 만료


def _claude_creds(monkeypatch, tmp_path, oauth: dict) -> None:
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": oauth}))
    monkeypatch.setattr(claude, "CREDENTIALS", creds)
    monkeypatch.setattr(claude, "_read_keychain", lambda: None)


def test_claude_expired_token_fails_before_any_call(monkeypatch, tmp_path):
    """M5 — expiresAt 과거면 usage API 를 치지 않고 'token expired' 로 보고한다."""
    _claude_creds(
        monkeypatch,
        tmp_path,
        {
            "accessToken": "expired-token",
            "expiresAt": 1000,  # 1970 — 밀리초가 아니라 초 단위도 과거면 만료
            "oauthAccount": {"accountUuid": "u-1"},
        },
    )
    calls: list = []
    monkeypatch.setattr(claude, "request_json", lambda *a, **k: calls.append(1) or {})

    result = claude.fetch()
    assert calls == []  # 호출 전 판정 — 401/429 가 될 기회 자체가 없다
    assert result.error_kind == "token_expired"
    assert "expired" in (result.error or "").lower()
    assert result.account_fp == claude._account_fp(
        {"accessToken": "expired-token", "oauthAccount": {"accountUuid": "u-1"}}
    )
    assert isinstance(result.v2_attempt, Attempt)
    assert result.v2_attempt.error_ref == "auth_error:token_expired"


def test_claude_valid_expiry_proceeds_to_usage_call(monkeypatch, tmp_path):
    """expiresAt 미래면 정상 호출한다 — 만료 검사가 정상 경로를 막지 않는다."""
    _claude_creds(
        monkeypatch,
        tmp_path,
        {
            "accessToken": "live-token",
            "expiresAt": 4_000_000_000_000,  # ms — 먼 미래
            "oauthAccount": {"accountUuid": "u-1"},
            "subscriptionType": "max",
        },
    )
    calls: list = []

    def _usage(*a, **k):
        calls.append(1)
        return {"five_hour": {"utilization": 10.0, "resets_at": "2099-01-01T00:00:00Z"}}

    monkeypatch.setattr(claude, "request_json", _usage)
    result = claude.fetch()
    assert calls == [1]
    assert result.error is None
    assert result.session_fp is not None
    assert result.account_fp is not None


def test_claude_missing_expiry_proceeds(monkeypatch, tmp_path):
    """expiresAt 부재는 '모름' — 부재를 만료로 오판해 측정을 막지 않는다."""
    _claude_creds(monkeypatch, tmp_path, {"accessToken": "live-token"})
    calls: list = []

    def _usage(*a, **k):
        calls.append(1)
        return {"five_hour": {"utilization": 5.0, "resets_at": "2099-01-01T00:00:00Z"}}

    monkeypatch.setattr(claude, "request_json", _usage)
    result = claude.fetch()
    assert calls == [1]
    assert result.error is None
    assert result.account_fp is None  # uuid 없음 → 지문 없음(fail closed)


def test_claude_401_expired_body_reports_token_expired(monkeypatch, tmp_path):
    """서버가 401 + 'expired' 본문을 돌려주면 만료로 분류 — 속도제한이 아니다."""
    _claude_creds(
        monkeypatch,
        tmp_path,
        {"accessToken": "live-token", "expiresAt": 4_000_000_000_000},
    )

    def _boom(*_a, **_k):
        raise HttpError(401, "token expired")

    monkeypatch.setattr(claude, "request_json", _boom)
    result = claude.fetch()
    assert result.error_kind == "token_expired"
    assert result.http_status == 401
    assert isinstance(result.v2_attempt, Attempt)
    assert result.v2_attempt.error_ref == "auth_error:token_expired"


def test_gate_reason_reports_token_expired():
    """AC4 게이트 표면 — 만료는 '속도 제한'·'측정 불가'가 아니라 'token expired'."""
    results = cache.collect(
        {"claude": _failing("token expired — claude access token 만료", kind="token_expired")},
        ["claude"],
        now=EPOCH,
        use_cache=False,
    )
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True
    assert "token expired" in res.reason
    assert "속도 제한" not in res.reason


def test_claude_401_without_expired_stays_auth(monkeypatch, tmp_path):
    """관계없는 401 은 만료로 오분류하지 않는다 — 'auth' 로 남는다."""
    _claude_creds(
        monkeypatch,
        tmp_path,
        {"accessToken": "live-token", "expiresAt": 4_000_000_000_000},
    )

    def _boom(*_a, **_k):
        raise HttpError(401, "unauthorized")

    monkeypatch.setattr(claude, "request_json", _boom)
    result = claude.fetch()
    assert result.error_kind == "auth"
    assert isinstance(result.v2_attempt, Attempt)
    assert result.v2_attempt.error_ref == "auth_error:http_401"
