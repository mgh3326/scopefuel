"""리댁션된 실제 응답 픽스처로 각 provider 파서를 검증한다 (네트워크 없음)."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message

import pytest

from scopefuel import cache, render
from scopefuel.model import Bucket, ProviderResult
from scopefuel.providers import BUILTIN, agy, claude, clinepass, codex, grok


def test_claude_marks_weekly_scoped_as_model_scope(fixture_json, monkeypatch, tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "subscriptionType": "max"}}))
    monkeypatch.setattr(claude, "CREDENTIALS", creds)
    monkeypatch.setattr(claude, "request_json", lambda *a, **k: fixture_json("claude_usage"))

    result = claude.fetch()
    assert result.plan == "max"
    kinds = {b.label: (b.scope.kind, b.horizon) for b in result.buckets}
    assert kinds["5h"] == ("account", "now")
    assert kinds["7d all"] == ("account", "week")
    assert kinds["7d Fable"] == ("model", "week")
    assert result.verdict.blocking_pct == 97


def test_claude_missing_credentials_is_error_with_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(claude, "CREDENTIALS", tmp_path / "nope.json")
    result = claude.fetch()
    assert result.error and result.hint
    assert result.buckets == []


def test_codex_separates_additional_rate_limits(fixture_json, monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "x", "account_id": "acc"}}))
    monkeypatch.setattr(codex, "AUTH", auth)
    monkeypatch.setattr(codex, "request_json", lambda *a, **k: fixture_json("codex_usage"))

    result = codex.fetch()
    assert result.plan == "pro"
    primary = next(b for b in result.buckets if b.label == "7d")
    spark = next(b for b in result.buckets if b.label.startswith("GPT-5.3"))
    assert (primary.scope.kind, primary.used_pct, primary.horizon) == ("account", 82.0, "week")
    assert spark.scope == type(spark.scope)("model", "GPT-5.3-Codex-Spark")
    assert primary.resets_at and primary.resets_at.startswith("2026-")


def test_agy_local_groups_weekly_and_5h(fixture_json, monkeypatch):
    monkeypatch.setattr(agy, "_fetch_local", lambda: fixture_json("agy_local"))
    result = agy.fetch()
    assert result.source == "local-server"
    labels = {b.label: (b.scope.kind, b.scope.name, b.horizon, b.used_pct) for b in result.buckets}
    assert labels["gemini 5h"] == ("group", "gemini", "now", 0.2)
    assert labels["3p 5h"] == ("group", "3p", "now", 57.3)
    assert labels["gemini weekly"][2] == "week"
    assert result.verdict.basis == "group"


def test_agy_local_quota_note_identifies_shared_antigravity_pool(fixture_json, monkeypatch):
    monkeypatch.setattr(agy, "_fetch_local", lambda: fixture_json("agy_local"))

    result = agy.fetch()

    assert result.note == (
        "Antigravity 계정 풀 — CLIProxy 경유 oc-gflash·oc-sonnet46·oc-oss도 같은 pool/group을 소모"
    )


def test_agy_local_quota_keeps_existing_group_values_and_verdict(fixture_json):
    result = agy._from_local(fixture_json("agy_local"))

    values = {(bucket.scope.name, bucket.window): bucket.used_pct for bucket in result.buckets}
    assert values == {
        ("gemini", "5h"): 0.2,
        ("gemini", "7d"): 0.9,
        ("3p", "5h"): 57.3,
        ("3p", "7d"): 19.1,
    }
    assert result.verdict.groups == {"gemini": 0.9, "3p": 57.3}


def test_agy_cloud_fallback_collapses_models_into_groups(fixture_json, monkeypatch):
    monkeypatch.setattr(agy, "_fetch_local", lambda: None)
    monkeypatch.setattr(agy, "_cloud_token", lambda: "token")

    def fake_request(url, **kwargs):
        if "loadCodeAssist" in url:
            return {"cloudaicompanionProject": "proj-1"}
        return fixture_json("agy_cloud")

    monkeypatch.setattr(agy, "request_json", fake_request)
    result = agy.fetch()

    assert result.source == "cloud"
    # 모델별 행이 와도 값이 그룹 공유이므로 그룹 2개로 접힌다 (비-쿼타 항목은 제외).
    assert sorted(b.scope.name for b in result.buckets) == ["3p", "gemini"]
    assert all(b.horizon == "now" for b in result.buckets)
    assert "5h" in (result.note or "")


def test_agy_reports_error_when_both_paths_fail(monkeypatch):
    monkeypatch.setattr(agy, "_fetch_local", lambda: None)
    monkeypatch.setattr(agy, "_cloud_token", lambda: (_ for _ in ()).throw(RuntimeError("no token")))
    result = agy.fetch()
    assert result.error and "no token" in result.error


def test_agy_absent_beacon_error_explains_oc_workers_may_consume_pool(monkeypatch):
    monkeypatch.setattr(agy, "_fetch_local", lambda: None)
    monkeypatch.setattr(agy, "_cloud_token", lambda: (_ for _ in ()).throw(RuntimeError("no token")))

    result = agy.fetch()

    assert result.error == (
        "agy 세션이 실행 중이 아님 — CLIProxy 경유 oc-gflash·oc-sonnet46·oc-oss가 같은 "
        "Antigravity pool/group을 소모 중일 수 있으나 quota beacon이 없어 읽지 못함 / cloud: no token"
    )


def test_clinepass_key_precedence(monkeypatch, tmp_path):
    opencode = tmp_path / "auth.json"
    opencode.write_text(json.dumps({"cline-pass": {"key": "file-secret"}}))
    cline_file = tmp_path / "api-key"
    cline_file.write_text("fallback-secret")
    monkeypatch.setattr(clinepass, "OPENCODE_AUTH", opencode)
    monkeypatch.setattr(clinepass, "CLINE_API_KEY_FILE", cline_file)
    monkeypatch.setenv("CLINE_API_KEY", "env-secret")

    assert clinepass._api_key() == ("env-secret", "env:CLINE_API_KEY")

    monkeypatch.delenv("CLINE_API_KEY")
    assert clinepass._api_key() == ("file-secret", "opencode:cline-pass.key")

    opencode.write_text("{}")
    assert clinepass._api_key() == ("fallback-secret", "file:~/.config/cline/api-key")


def test_clinepass_usage_request_is_token_free_get(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"data":{"limits":[]},"success":true}'

    def fake_urlopen(request, **kwargs):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = request.data
        captured["authorization"] = request.get_header("Authorization")
        return Response()

    monkeypatch.setattr(clinepass.urllib.request, "urlopen", fake_urlopen)

    status, _body = clinepass._request_usage("synthetic-key")

    assert status == 200
    assert captured == {
        "url": clinepass.USAGE_URL,
        "method": "GET",
        "body": None,
        "authorization": "Bearer synthetic-key",
    }


def test_clinepass_applies_rate_headers_from_http_error(monkeypatch):
    headers = Message()
    headers["x-ratelimit-limit-requests"] = "100"
    headers["x-ratelimit-remaining-requests"] = "75"
    headers["x-ratelimit-reset-requests"] = "5h"
    error = urllib.error.HTTPError(
        clinepass.COMPLETIONS_URL, 429, "rate limited", headers, io.BytesIO(b'{"error":"limited"}')
    )

    def raise_http_error(*args, **kwargs):
        raise error

    monkeypatch.setattr(clinepass.urllib.request, "urlopen", raise_http_error)

    status, actual = clinepass._probe("not-a-real-key")
    assert status == 429
    assert actual["x-ratelimit-remaining-requests"] == "75"


def test_clinepass_usage_limits_maps_all_windows_and_tz_aware_resets(monkeypatch):
    payload = {
        "data": {
            "limits": [
                {"type": "monthly", "percentUsed": 30, "resetsAt": "2099-08-30T05:00:00Z"},
                {"type": "five_hour", "percentUsed": 10, "resetsAt": "2099-07-30T10:00:00Z"},
                {"type": "weekly", "percentUsed": 20, "resetsAt": "2099-08-06T05:00:00+00:00"},
            ]
        },
        "success": True,
        "accountId": "must-not-enter-raw",
    }
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("synthetic-key", "env:CLINE_API_KEY"))
    monkeypatch.setattr(
        clinepass,
        "_request_usage",
        lambda _key: (200, json.dumps(payload).encode()),
    )
    monkeypatch.setattr(
        clinepass,
        "_probe",
        lambda _key: (_ for _ in ()).throw(AssertionError("fallback called on usage success")),
    )

    result = clinepass.fetch()

    assert result.source == "primary(usage-limits)"
    assert result.note is None
    assert [(b.label, b.window, b.horizon, b.used_pct) for b in result.buckets] == [
        ("five_hour", "5h", "now", 10.0),
        ("weekly", "7d", "week", 20.0),
        ("monthly", "30d", "month", 30.0),
    ]
    assert all(b.resets_at and b.resets_at.endswith("+00:00") for b in result.buckets)
    assert result.verdict.month_pct == 30.0
    assert result.raw and result.raw["credential"] == {
        "source": "env:CLINE_API_KEY",
        "present": True,
    }
    assert "accountId" not in json.dumps(result.raw)
    output = render.table([result], color=False)
    assert "now  five_hour" in output
    assert "week weekly" in output
    assert "month monthly" in output
    assert "이번달 사용 30%" in output


def test_clinepass_server_error_does_not_spend_fallback_token(monkeypatch):
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("synthetic-key", "env:CLINE_API_KEY"))
    monkeypatch.setattr(clinepass, "_request_usage", lambda _key: (500, b""))
    monkeypatch.setattr(
        clinepass,
        "_probe",
        lambda _key: (_ for _ in ()).throw(AssertionError("fallback called on HTTP 500")),
    )

    result = clinepass.fetch()

    assert result.error == "usage-limits HTTP 500"
    assert result.source == "primary(usage-limits)"


def test_clinepass_fallback_parses_headers_without_exposing_key(monkeypatch):
    headers = Message()
    headers["x-ratelimit-limit-requests"] = "100"
    headers["x-ratelimit-remaining-requests"] = "75"
    headers["x-ratelimit-reset-requests"] = "5h"
    headers["x-ratelimit-limit-tokens"] = "1000"
    headers["x-ratelimit-remaining-tokens"] = "250"
    headers["x-ratelimit-reset-tokens"] = "1d"
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("super-secret", "env:CLINE_API_KEY"))
    monkeypatch.setattr(clinepass, "_request_usage", lambda _key: (404, b""))
    monkeypatch.setattr(clinepass, "_probe", lambda _key: (400, headers))

    result = clinepass.fetch()

    assert result.error is None
    assert result.status == "warning"
    assert result.note == "usage-limits HTTP 404; fallback 응답 실패"
    assert result.source == "fallback(probe)"
    assert [(b.label, b.used_pct) for b in result.buckets] == [
        ("requests", 25.0),
        ("tokens", 75.0),
    ]
    assert result.raw and result.raw["credential"] == {
        "source": "env:CLINE_API_KEY",
        "present": True,
    }
    assert "super-secret" not in json.dumps(result.raw)


def test_clinepass_empty_limits_is_graceful_no_data(monkeypatch):
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("super-secret", "env:CLINE_API_KEY"))
    monkeypatch.setattr(
        clinepass,
        "_request_usage",
        lambda _key: (200, b'{"data":{"limits":[]},"success":true}'),
    )

    result = clinepass.fetch()

    assert result.status == "ok"
    assert result.error is None
    assert result.buckets == []
    assert result.note == "no data — usage-limits limits 비어 있음"
    assert result.raw and result.raw["data"] == {"limits": []}
    assert "super-secret" not in json.dumps(result.as_dict(include_raw=True))


def test_clinepass_partial_limits_stays_usable_and_marks_invalid_reset(monkeypatch):
    payload = {
        "data": {
            "limits": [
                {"type": "weekly", "percentUsed": 42.5, "resetsAt": "2099-08-06T05:00:00Z"},
                {"type": "monthly", "percentUsed": 55, "resetsAt": "2099-08-30T05:00:00"},
            ]
        },
        "success": True,
    }
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("synthetic-key", "env:CLINE_API_KEY"))
    monkeypatch.setattr(clinepass, "_request_usage", lambda _key: (200, json.dumps(payload).encode()))

    result = clinepass.fetch()

    assert [(b.label, b.used_pct) for b in result.buckets] == [("weekly", 42.5), ("monthly", 55.0)]
    assert result.note == "partial data — limits 누락: five_hour"
    monthly = next(bucket for bucket in result.buckets if bucket.label == "monthly")
    assert monthly.resets_at is None
    assert monthly.note and "비-tz ISO8601" in monthly.note
    assert result.error is None


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        (101, "percentUsed 100 초과 (101)"),
        ("12.5", "percentUsed 숫자 문자열 허용 안 함"),
        (-1, "percentUsed 음수 (-1)"),
        ("not-a-number", "percentUsed 숫자 문자열 허용 안 함"),
        (10**400, "percentUsed 숫자 한도 초과"),
    ],
)
def test_clinepass_percent_used_anomalies_are_explicit_warnings(monkeypatch, value, problem):
    payload = {
        "data": {
            "limits": [
                {
                    "type": "five_hour",
                    "percentUsed": value,
                    "resetsAt": "2099-07-30T10:00:00Z",
                }
            ]
        },
        "success": True,
    }
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("synthetic-key", "env:CLINE_API_KEY"))
    monkeypatch.setattr(clinepass, "_request_usage", lambda _key: (200, json.dumps(payload).encode()))

    result = clinepass.fetch()
    output = render.table([result], color=False)

    assert result.status == "warning"
    assert result.error is None
    assert result.warning == f"데이터 이상 — five_hour: {problem}"
    assert result.buckets[0].used_pct is None
    assert result.buckets[0].note == problem
    assert result.raw and result.raw["data"]["limits"][0]["percentUsed"] is None
    assert problem in output


def test_clinepass_duplicate_type_warns_and_keeps_first_value(monkeypatch):
    payload = {
        "data": {
            "limits": [
                {
                    "type": "weekly",
                    "percentUsed": 11,
                    "resetsAt": "2099-08-06T05:00:00Z",
                },
                {
                    "type": "weekly",
                    "percentUsed": 99,
                    "resetsAt": "2099-08-07T05:00:00Z",
                },
            ]
        },
        "success": True,
    }
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("synthetic-key", "env:CLINE_API_KEY"))
    monkeypatch.setattr(clinepass, "_request_usage", lambda _key: (200, json.dumps(payload).encode()))

    result = clinepass.fetch()
    output = render.table([result], color=False)

    assert result.status == "warning"
    assert result.error is None
    assert result.warning == "데이터 이상 — weekly: duplicate type (첫 값 유지)"
    assert [(bucket.label, bucket.used_pct) for bucket in result.buckets] == [("weekly", 11.0)]
    assert "duplicate type (첫 값 유지)" in output


def test_clinepass_past_reset_is_preserved_and_warns_as_stale(monkeypatch):
    payload = {
        "data": {
            "limits": [
                {
                    "type": "monthly",
                    "percentUsed": 25,
                    "resetsAt": "2000-01-01T00:00:00Z",
                }
            ]
        },
        "success": True,
    }
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("synthetic-key", "env:CLINE_API_KEY"))
    monkeypatch.setattr(clinepass, "_request_usage", lambda _key: (200, json.dumps(payload).encode()))

    result = clinepass.fetch()
    output = render.table([result], color=False)
    (bucket,) = result.buckets

    assert result.status == "warning"
    assert result.error is None
    assert result.warning == "데이터 이상 — monthly: resetsAt 과거(stale)"
    assert bucket.resets_at == "2000-01-01T00:00:00+00:00"
    assert bucket.note == "resetsAt 과거(stale)"
    assert "resetsAt 과거(stale)" in output


def test_clinepass_exact_now_reset_is_not_stale(monkeypatch):
    import datetime as dt

    fixed_now = dt.datetime(2026, 7, 30, 10, 0, 0, tzinfo=dt.UTC)
    now_iso = fixed_now.isoformat()
    payload = {
        "data": {
            "limits": [
                {
                    "type": "monthly",
                    "percentUsed": 25,
                    "resetsAt": now_iso,
                }
            ]
        },
        "success": True,
    }
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("synthetic-key", "env:CLINE_API_KEY"))
    monkeypatch.setattr(clinepass, "_request_usage", lambda _key: (200, json.dumps(payload).encode()))

    class MockDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(clinepass.dt, "datetime", MockDatetime)

    result = clinepass.fetch()

    assert result.status == "ok"
    assert result.warning is None
    assert result.buckets[0].note is None


def test_clinepass_auth_failures_warn_but_empty_limits_is_no_data(monkeypatch):
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("synthetic-key", "env:CLINE_API_KEY"))

    outputs = {}
    for status in (401, 403, 200):
        body = b'{"data":{"limits":[]},"success":true}' if status == 200 else b""
        monkeypatch.setattr(
            clinepass,
            "_request_usage",
            lambda _key, status=status, body=body: (status, body),
        )
        result = clinepass.fetch()
        outputs[status] = render.table([result], color=False)

    assert outputs[401] == (
        "clinepass [primary(usage-limits)]  [WARN] 인증 실패 (HTTP 401) — 키를 확인하세요\n"
    )
    assert outputs[403] == (
        "clinepass [primary(usage-limits)]  [WARN] 인증 실패 (HTTP 403) — 키를 확인하세요\n"
    )
    assert outputs[200].startswith("clinepass [primary(usage-limits)]  [ok] 한도 정보 없음")
    assert "no data — usage-limits limits 비어 있음" in outputs[200]
    assert len(set(outputs.values())) == 3


def test_clinepass_fallback_auth_failure_is_warning(monkeypatch):
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("synthetic-key", "env:CLINE_API_KEY"))
    monkeypatch.setattr(clinepass, "_request_usage", lambda _key: (404, b""))
    monkeypatch.setattr(clinepass, "_probe", lambda _key: (403, Message()))

    result = clinepass.fetch()

    assert result.status == "warning"
    assert result.error is None
    assert result.warning == "인증 실패 (HTTP 403) — 키를 확인하세요"
    assert result.source == "fallback(probe)"


def test_clinepass_fallback_http_failures_are_warnings_not_false_ok(monkeypatch):
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("synthetic-key", "env:CLINE_API_KEY"))
    monkeypatch.setattr(clinepass, "_request_usage", lambda _key: (404, b""))

    outputs = {}
    for status in (301, 302, 304, 399, 400, 429, 500, 503):
        monkeypatch.setattr(
            clinepass,
            "_probe",
            lambda _key, status=status: (status, Message()),
        )
        result = clinepass.fetch()
        outputs[status] = render.table([result], color=False)
        assert result.status == "warning"
        assert result.error is None
        assert result.warning == f"completions fallback HTTP {status} — 정상으로 간주하지 않음"
        assert result.source == "fallback(probe)"
        assert result.raw and result.raw["status"] == 404
        assert result.raw["fallback_status"] == status

    assert outputs[301].startswith(
        "clinepass [fallback(probe)]  [WARN] completions fallback HTTP 301 — 정상으로 간주하지 않음"
    )
    assert outputs[400].startswith(
        "clinepass [fallback(probe)]  [WARN] completions fallback HTTP 400 — 정상으로 간주하지 않음"
    )
    assert outputs[429].startswith(
        "clinepass [fallback(probe)]  [WARN] completions fallback HTTP 429 — 정상으로 간주하지 않음"
    )
    assert outputs[500].startswith(
        "clinepass [fallback(probe)]  [WARN] completions fallback HTTP 500 — 정상으로 간주하지 않음"
    )


def test_clinepass_sanitizes_primary_value_error(monkeypatch):
    marker = "ZZ-SYNTHETIC-MARKER-F1-NOT-A-REAL-KEY"
    monkeypatch.setattr(clinepass, "_api_key", lambda: (marker, "env:CLINE_API_KEY"))

    def leak_if_unsanitized(_key):
        raise ValueError(f"Invalid header value b'Bearer {marker}'")

    monkeypatch.setattr(clinepass, "_request_usage", leak_if_unsanitized)
    result = clinepass.fetch()
    output = render.table([result], color=False) + render.brief([result], color=False)
    serialized = json.dumps(result.as_dict(include_raw=True), ensure_ascii=False)

    assert result.error == "invalid credential format"
    assert marker not in output
    assert marker not in serialized


def test_clinepass_sanitizes_fallback_value_error(monkeypatch):
    marker = "ZZ-SYNTHETIC-MARKER-F1-NOT-A-REAL-KEY"
    monkeypatch.setattr(clinepass, "_api_key", lambda: (marker, "env:CLINE_API_KEY"))
    monkeypatch.setattr(clinepass, "_request_usage", lambda _key: (404, b""))

    def leak_if_unsanitized(_key):
        raise ValueError(f"Invalid header value b'Bearer {marker}'")

    monkeypatch.setattr(clinepass, "_probe", leak_if_unsanitized)
    result = clinepass.fetch()
    output = render.table([result], color=False) + render.brief([result], color=False)
    serialized = json.dumps(result.as_dict(include_raw=True), ensure_ascii=False)

    assert result.error == "invalid credential format"
    assert marker not in output
    assert marker not in serialized


def test_clinepass_fallback_reflection_is_absent_from_all_surfaces(monkeypatch, tmp_path, capsys):
    marker = "ZZSYNTHETICF1MARKERNOTAREALKEY"
    headers = Message()
    headers["x-ratelimit-limit-requests"] = "100"
    headers["x-ratelimit-remaining-requests"] = "75"
    headers["x-ratelimit-reset-requests"] = marker
    headers[f"x-ratelimit-limit-{marker}"] = "100"
    headers[f"x-ratelimit-remaining-{marker}"] = "75"
    headers[f"x-ratelimit-reset-{marker}"] = "5h"
    monkeypatch.setattr(clinepass, "_api_key", lambda: (marker, "env:CLINE_API_KEY"))
    monkeypatch.setattr(clinepass, "_request_usage", lambda _key: (404, b""))
    monkeypatch.setattr(clinepass, "_probe", lambda _key: (200, headers))
    cache_file = tmp_path / "snapshots.json"
    monkeypatch.setenv("SCOPEFUEL_CACHE", str(cache_file))

    (result,) = cache.collect(
        {"clinepass": clinepass.fetch},
        ["clinepass"],
        ttl_s=60,
        use_cache=True,
        now=1,
    )
    surfaces = [
        render.table([result], color=False),
        render.brief([result], color=False),
        json.dumps(result.as_dict(), ensure_ascii=False),
        json.dumps(result.as_dict(include_raw=True), ensure_ascii=False),
        cache_file.read_text(),
        capsys.readouterr().err,
    ]

    assert result.status == "ok"
    assert [bucket.label for bucket in result.buckets] == ["requests"]
    assert all(marker not in surface for surface in surfaces)
    assert all(marker.lower() not in surface for surface in surfaces)


def test_clinepass_sanitizes_unexpected_primary_and_fallback_errors(monkeypatch):
    marker = "ZZ-SYNTHETIC-MARKER-F1-NOT-A-REAL-KEY"
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("synthetic-key", "env:CLINE_API_KEY"))

    def primary_failure(_key):
        raise RuntimeError(f"request/URL/body detail: {marker}")

    monkeypatch.setattr(clinepass, "_request_usage", primary_failure)
    primary_result = clinepass.fetch()

    class BadHeaders:
        def items(self):
            raise RuntimeError(f"response header detail: {marker}")

    monkeypatch.setattr(clinepass, "_request_usage", lambda _key: (404, b""))
    monkeypatch.setattr(clinepass, "_probe", lambda _key: (200, BadHeaders()))
    fallback_result = clinepass.fetch()

    for result in (primary_result, fallback_result):
        output = render.table([result], color=False) + render.brief([result], color=False)
        serialized = json.dumps(result.as_dict(include_raw=True), ensure_ascii=False)
        assert marker not in output
        assert marker not in serialized


def test_clinepass_rejects_unsafe_synthetic_keys_without_exposure(monkeypatch):
    marker = "ZZ-SYNTHETIC-MARKER-F1-NOT-A-REAL-KEY"
    keys = [
        f"{marker}\nINJECTED",
        f"{marker}\tINJECTED",
        f"{marker} INJECTED",
        f"{marker}한글",
    ]

    for key in keys:
        monkeypatch.setattr(clinepass, "_api_key", lambda key=key: (key, "env:CLINE_API_KEY"))
        monkeypatch.setattr(
            clinepass,
            "_request_usage",
            lambda _key: (_ for _ in ()).throw(AssertionError("unsafe key reached probe")),
        )
        result = clinepass.fetch()
        output = render.table([result], color=False) + render.brief([result], color=False)
        serialized = json.dumps(result.as_dict(include_raw=True), ensure_ascii=False)

        assert result.error == "invalid credential format"
        assert marker not in output
        assert marker not in serialized


def test_clinepass_ignores_zero_limit():
    headers = {
        "x-ratelimit-limit-requests": "0",
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-reset-requests": "5h",
    }

    assert clinepass._buckets(headers) == []


def test_clinepass_blank_env_falls_through_to_opencode(monkeypatch, tmp_path):
    opencode = tmp_path / "auth.json"
    opencode.write_text(json.dumps({"cline-pass": {"key": "fallback-synthetic-key"}}))
    monkeypatch.setattr(clinepass, "OPENCODE_AUTH", opencode)
    monkeypatch.setattr(clinepass, "CLINE_API_KEY_FILE", tmp_path / "missing")
    monkeypatch.setenv("CLINE_API_KEY", " \t ")

    assert clinepass._api_key() == ("fallback-synthetic-key", "opencode:cline-pass.key")


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        ("reflection", "ok"),
        ("fallback-auth", "warning"),
        ("fallback-400", "warning"),
        ("fallback-429", "warning"),
        ("fallback-500", "warning"),
        ("percent-over-100", "warning"),
        ("percent-string", "warning"),
        ("percent-negative", "warning"),
        ("duplicate-type", "warning"),
        ("past-reset", "warning"),
    ],
)
def test_clinepass_bad_inputs_never_stop_other_provider(monkeypatch, scenario, expected_status):
    marker = "ZZSYNTHETICAC4MARKER"
    monkeypatch.setattr(clinepass, "_api_key", lambda: (marker, "env:CLINE_API_KEY"))

    if scenario.startswith("fallback") or scenario == "reflection":
        monkeypatch.setattr(clinepass, "_request_usage", lambda _key: (404, b""))
        headers = Message()
        if scenario == "reflection":
            headers["x-ratelimit-limit-requests"] = "100"
            headers["x-ratelimit-remaining-requests"] = "75"
            headers["x-ratelimit-reset-requests"] = marker
            fallback_status = 200
        elif scenario == "fallback-auth":
            fallback_status = 403
        else:
            fallback_status = int(scenario.removeprefix("fallback-"))
        monkeypatch.setattr(
            clinepass,
            "_probe",
            lambda _key, fallback_status=fallback_status, headers=headers: (
                fallback_status,
                headers,
            ),
        )
    else:
        values = {
            "percent-over-100": [
                {"type": "five_hour", "percentUsed": 101, "resetsAt": "2099-01-01T00:00:00Z"}
            ],
            "percent-string": [
                {
                    "type": "five_hour",
                    "percentUsed": "12.5",
                    "resetsAt": "2099-01-01T00:00:00Z",
                }
            ],
            "percent-negative": [
                {"type": "five_hour", "percentUsed": -1, "resetsAt": "2099-01-01T00:00:00Z"}
            ],
            "duplicate-type": [
                {"type": "weekly", "percentUsed": 11, "resetsAt": "2099-01-01T00:00:00Z"},
                {"type": "weekly", "percentUsed": 99, "resetsAt": "2099-01-02T00:00:00Z"},
            ],
            "past-reset": [{"type": "monthly", "percentUsed": 25, "resetsAt": "2000-01-01T00:00:00Z"}],
        }
        payload = {"data": {"limits": values[scenario]}, "success": True}
        monkeypatch.setattr(
            clinepass,
            "_request_usage",
            lambda _key, payload=payload: (200, json.dumps(payload).encode()),
        )

    healthy = ProviderResult(
        id="healthy",
        buckets=[Bucket(label="5h", window="5h", used_pct=10, horizon="now")],
    )
    results = cache.collect(
        {"healthy": lambda: healthy, "clinepass": clinepass.fetch},
        ["healthy", "clinepass"],
        use_cache=False,
    )

    assert [result.id for result in results] == ["healthy", "clinepass"]
    assert results[0].status == "ok"
    assert results[0].buckets[0].used_pct == 10
    assert results[1].status == expected_status
    assert "healthy" in render.table(results, color=False)


# --------------------------------------------------------- OverflowError in provider converters (Fix 4)


class _OverflowNum:
    def __float__(self):
        raise OverflowError("too large")


def test_claude_num_catches_overflow():
    assert claude._num(10**1000) is None
    assert claude._num(2**1024) is None
    assert claude._num(_OverflowNum()) is None
    assert claude._num(True) is None
    assert claude._num(50.0) == 50.0
    assert claude._num(0) == 0.0
    assert claude._num(100) == 100.0


def test_codex_num_catches_overflow():
    assert codex._num(10**1000) is None
    assert codex._num(2**1024) is None
    assert codex._num(_OverflowNum()) is None
    assert codex._num(True) is None
    assert codex._num(50.0) == 50.0
    assert codex._num(0) == 0.0
    assert codex._num(100) == 100.0


def test_agy_from_local_catches_overflow():
    raw = {
        "response": {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [{"window": "5h", "remainingFraction": 10**1000}],
                }
            ]
        }
    }
    result = agy._from_local(raw)
    assert result.buckets[0].used_pct is None


def test_agy_cloud_buckets_catches_overflow():
    raw = {
        "models": {
            "gemini-2.5-pro": {
                "quotaInfo": {"remainingFraction": 10**1000, "resetTime": "2099-01-01T00:00:00Z"}
            }
        }
    }
    buckets = agy._cloud_buckets(raw)
    assert len(buckets) == 0


# ------------------------------------------------------------------ grok (ROB-1228)


SAMPLE_GROK_USAGE = """\
Grok
❯
Session usage is unavailable until the session starts.

Weekly limit: 37%
Next reset: August 14, 04:58
"""


def test_grok_parse_is_single_weekly_bucket_and_kst_reset():
    result = grok.parse(
        SAMPLE_GROK_USAGE,
        now=__import__("datetime").datetime(2026, 8, 7, 13, 0, tzinfo=__import__("datetime").timezone.utc),
    )

    assert result.error is None
    assert [(b.label, b.window, b.horizon, b.used_pct) for b in result.buckets] == [
        ("weekly", "7d", "week", 37.0)
    ]
    assert result.buckets[0].resets_at == "2026-08-13T19:58:00Z"
    assert "KST" in (result.note or "")


def test_grok_parse_accepts_box_and_control_residue_around_usage_lines():
    text = "\x1b[2K│  Weekly limit: 37%  │\x1b[0m\n│ Next reset: August 14, 04:58 │\n"
    result = grok.parse(text)
    assert result.error is None
    assert result.buckets[0].used_pct == 37.0
    assert result.buckets[0].resets_at.endswith("19:58:00Z")


def test_grok_parse_infers_next_year_at_december_boundary():
    import datetime as dt

    result = grok.parse(
        "Weekly limit: 12%\nNext reset: January 2, 04:58\n",
        now=dt.datetime(2026, 12, 31, 20, tzinfo=dt.UTC),
    )
    assert result.buckets[0].resets_at == "2027-01-01T19:58:00Z"


def test_grok_parse_keeps_same_year_when_reset_is_ahead():
    import datetime as dt

    result = grok.parse(
        "Weekly limit: 12%\nNext reset: December 31, 23:00\n",
        now=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
    )
    assert result.buckets[0].resets_at == "2026-12-31T14:00:00Z"


@pytest.mark.parametrize(
    "text",
    [
        "Weekly limit: 12%\nNext reset: not-a-date\n",
        "Session usage is unavailable until the session starts.\n",
        "monthlyLimit: 15000 used: 2916\n",
    ],
)
def test_grok_parse_failure_is_degraded_without_monthly_fallback(text):
    result = grok.parse(text)
    assert result.error
    assert result.buckets == []
    assert result.verdict.mark == "degraded"
    assert "monthly" not in json.dumps(result.as_dict())


def test_grok_normal_session_message_does_not_block_success():
    result = grok.parse(SAMPLE_GROK_USAGE)
    assert result.error is None
    assert result.buckets[0].used_pct == 37.0


def test_grok_raw_masks_credential_and_does_not_expose_token(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    secret = "super-secret-token-value"
    auth.write_text(json.dumps({"https://auth.x.ai::client": {"key": secret, "email": "x@y.test"}}))
    monkeypatch.setattr(grok, "BINARY", "/bin/printf")
    monkeypatch.setattr(grok.shutil, "which", lambda _binary: "/bin/printf")
    monkeypatch.setattr(grok, "_credential_meta", lambda: {"credential_present": True})
    monkeypatch.setattr(grok, "_probe_once", lambda: "Weekly limit: 1%\nNext reset: August 14, 04:58\n")
    result = grok.fetch()
    raw = json.dumps(result.raw)
    assert result.error is None
    assert secret not in raw
    assert result.raw["credential"] == {"credential_present": True}
    assert all(isinstance(value, bool) for value in result.raw["credential"].values())


def test_grok_fetch_uses_pty_prompt_and_fixed_workdir(tmp_path, monkeypatch):
    binary = tmp_path / "fake-grok"
    binary.write_text(
        "#!/bin/sh\n"
        "printf '╭────────╮\\r\\n│ ❯      │\\r\\n╰────────╯\\r\\n'\n"
        'printf \'CWD=%s LINES=%s COLUMNS=%s\\r\\n\' "$PWD" "$LINES" "$COLUMNS"\n'
        "printf 'Shift+Tab:mode │ Ctrl+.:shortcuts\\r\\n'\n"
        "IFS= read -r command\n"
        "[ \"$command\" = '/usage' ] || exit 9\n"
        "printf 'Session usage is unavailable until the session starts.\\r\\n'\n"
        "printf 'Weekly limit: 42%%\\r\\nNext reset: August 14, 04:58\\r\\n'\n"
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    workdir = tmp_path / "probe-workdir"
    monkeypatch.setattr(grok, "BINARY", str(binary))
    monkeypatch.setattr(grok, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(grok, "_credential_meta", lambda: {"credential_present": True})
    ioctl_calls = []
    original_ioctl = grok.fcntl.ioctl

    def recording_ioctl(fd, request, argument):
        ioctl_calls.append((fd, request, argument))
        return original_ioctl(fd, request, argument)

    monkeypatch.setattr(grok.fcntl, "ioctl", recording_ioctl)

    result = grok.fetch()

    assert result.error is None
    assert result.buckets[0].used_pct == 42.0
    assert f"CWD={workdir}" in result.raw["stdout"]
    assert ioctl_calls
    assert ioctl_calls[0][1] == grok.termios.TIOCSWINSZ
    assert grok.struct.unpack("HHHH", ioctl_calls[0][2]) == (50, 200, 0, 0)
    assert "LINES=50" in result.raw["stdout"] or "COLUMNS=200" in result.raw["stdout"]


def test_grok_prompt_marker_matches_when_tui_draws_after_prompt():
    screen = "╭────────╮\n│ ❯      │\n╰────────╯\nShift+Tab:mode │ Ctrl+.:shortcuts\n"
    assert grok._PROMPT.search(screen)


def test_grok_timeout_has_hard_limit_and_degraded(tmp_path, monkeypatch):
    binary = tmp_path / "fake-grok-timeout"
    binary.write_text("#!/bin/sh\nprintf 'starting\\r\\n'\nsleep 2\n")
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(grok, "BINARY", str(binary))
    monkeypatch.setattr(grok, "PROBE_WORKDIR", tmp_path / "probe-timeout")
    monkeypatch.setattr(grok, "TIMEOUT_S", 0.1)
    monkeypatch.setattr(grok, "STARTUP_DELAY_S", 0.0)
    monkeypatch.setattr(grok, "_credential_meta", lambda: {"credential_present": False})

    result = grok.fetch()

    assert result.error and "안에 끝나지 않음" in result.error
    assert result.verdict.mark == "degraded"


def test_grok_registry_and_ttl_are_unchanged():
    from scopefuel.cache import PROVIDER_TTL_S

    assert BUILTIN["grok"].pool_class == "spend"
    assert PROVIDER_TTL_S["grok"] == 1800.0
