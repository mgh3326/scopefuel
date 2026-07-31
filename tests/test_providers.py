"""리댁션된 실제 응답 픽스처로 각 provider 파서를 검증한다 (네트워크 없음)."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message

import pytest

from scopefuel import cache, render
from scopefuel.model import Bucket, ProviderResult
from scopefuel.providers import BUILTIN, agy, claude, clinepass, codex, grok, kiro


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


def test_kiro_parses_plan_credits(fixture_text):
    result = kiro.parse(fixture_text("kiro_usage"))

    assert result.plan == "pro max"
    assert result.source == "cli:/usage"
    (bucket,) = result.buckets
    assert (bucket.scope.kind, bucket.horizon, bucket.window) == ("account", "week", "30d")
    assert bucket.used_pct == 0.01  # 0.50 / 5000
    # CLI 는 날짜만 준다 → 로컬 자정으로 고정(오프셋은 실행 환경에 따라 다르다).
    assert bucket.resets_at and bucket.resets_at.startswith("2026-08-01T00:00:00")
    assert bucket.note is None


def test_kiro_folds_addon_credits_into_one_account_bucket(fixture_text):
    """플랜이 100% 여도 애드온이 남으면 막힌 게 아니다 — 합산 한 줄로 낸다."""
    result = kiro.parse(fixture_text("kiro_usage_bonus"))

    assert result.plan == "free"
    (bucket,) = result.buckets
    assert bucket.scope.kind == "account"
    # 플랜 50/50 + 애드온 122.54/500 → 합산 172.54/550
    assert round(bucket.used_pct, 2) == 31.37
    assert result.verdict.blocking_pct < 90  # 플랜만 보면 100% 라 crit 으로 오판했을 값
    assert bucket.note and "애드온" in bucket.note
    assert bucket.resets_at and bucket.resets_at.startswith("20")


def test_kiro_unparsable_output_is_error_not_zero(fixture_text):
    result = kiro.parse("Please log in to continue\n")
    assert result.error and result.buckets == []
    assert result.hint and "login" in result.hint
    assert result.verdict.blocking_pct == 0  # 값을 지어내지 않는다


def test_kiro_missing_binary_is_error_with_hint(monkeypatch):
    monkeypatch.setattr(kiro.shutil, "which", lambda _name: None)
    result = kiro.fetch()
    assert result.error and result.hint
    assert result.buckets == []


def test_kiro_strips_ansi_before_parsing():
    noisy = "\x1b[38;5;1mCredits (1.00 of 10 covered in plan)\x1b[0m\n"
    result = kiro.parse(noisy)
    assert result.buckets[0].used_pct == 10.0


def test_kiro_retries_once_on_expired_token(fixture_text, monkeypatch):
    """만료 토큰은 CLI 호출이 갱신한다 — 1회 재시도 후 성공하면 그 값을 쓴다."""
    outputs = iter(
        [
            "⚠️  Warning: Could not retrieve usage information from backend\n"
            "Error: AccessDeniedError [AccessDeniedException]: Token expired\n",
            fixture_text("kiro_usage"),
        ]
    )
    monkeypatch.setattr(kiro.shutil, "which", lambda _name: "/usr/local/bin/kiro-cli")
    monkeypatch.setattr(kiro, "_probe_once", lambda: kiro.parse(next(outputs)))

    result = kiro.fetch()
    assert result.error is None
    assert result.buckets[0].used_pct == 0.01


def test_kiro_expired_token_twice_keeps_error_with_login_hint(monkeypatch):
    expired = "Error: AccessDeniedError [AccessDeniedException]: Token expired\n"
    monkeypatch.setattr(kiro.shutil, "which", lambda _name: "/usr/local/bin/kiro-cli")
    monkeypatch.setattr(kiro, "_probe_once", lambda: kiro.parse(expired))

    result = kiro.fetch()
    assert result.error and "login" in (result.hint or "")
    assert result.buckets == []


def test_clinepass_is_appended_after_existing_builtin_providers():
    assert list(BUILTIN) == ["claude", "codex", "agy", "kiro", "clinepass", "grok"]


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


# ------------------------------------------------------------------ grok (ROB-1179)


def _grok_auth(tmp_path, monkeypatch, *, key="synthetic-jwt-not-real", with_ids=True):
    auth = tmp_path / "auth.json"
    entry = {"key": key}
    if with_ids:
        entry["user_id"] = "00000000-0000-4000-8000-000000000001"
        entry["team_id"] = "00000000-0000-4000-8000-000000000002"
        entry["email"] = "redacted@example.com"
    auth.write_text(json.dumps({f"{grok.AUTH_KEY_PREFIX}synthetic-client": entry}))
    monkeypatch.setattr(grok, "AUTH", auth)
    return auth


def test_grok_auth_selects_auth_x_ai_prefix_and_redacts_identifiers(tmp_path, monkeypatch):
    other = tmp_path / "auth.json"
    other.write_text(
        json.dumps(
            {
                "https://other.example/sign-in": {"key": "wrong-token"},
                f"{grok.AUTH_KEY_PREFIX}abc": {
                    "key": "correct-token",
                    "user_id": "uid-must-not-leak",
                    "team_id": "tid-must-not-leak",
                    "email": "must-not-leak@example.com",
                },
            }
        )
    )
    monkeypatch.setattr(grok, "AUTH", other)
    token, meta = grok._load_token()
    assert token == "correct-token"
    assert meta["auth_key_prefix_match"] is True
    assert meta["key_field_present"] is True
    assert meta["user_id_field_present"] is True
    assert meta["team_id_field_present"] is True
    assert "uid-must-not-leak" not in json.dumps(meta)
    assert "tid-must-not-leak" not in json.dumps(meta)


def test_grok_missing_auth_is_error_with_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(grok, "AUTH", tmp_path / "missing.json")
    result = grok.fetch()
    assert result.error and result.hint
    assert result.buckets == []
    assert "auth.x.ai" in (result.hint or "")


def test_grok_billing_success_maps_weekly_account_and_product_breakdown(fixture_json, tmp_path, monkeypatch):
    _grok_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(
        grok,
        "_request_billing",
        lambda _token: (200, fixture_json("grok_billing")),
    )
    monkeypatch.setattr(grok, "_fetch_plan", lambda _token: "grok pro")

    result = grok.fetch()

    assert result.error is None
    assert result.plan == "grok pro"
    assert result.source == "cli-billing"
    assert result.pool_class == "preserve"  # class is applied by registry, not fetch()

    by_label = {b.label: b for b in result.buckets}
    weekly = by_label["weekly"]
    assert weekly.used_pct == 8.0
    assert weekly.window == "7d"
    assert weekly.horizon == "week"
    assert weekly.scope.kind == "account"
    assert weekly.resets_at and weekly.resets_at.endswith("Z")
    assert weekly.note and "단일 주간 풀" in weekly.note

    assert by_label["GrokBuild"].used_pct == 5.0
    assert by_label["GrokBuild"].scope.kind == "group"
    assert by_label["GrokChat"].used_pct == 3.0

    raw_dump = json.dumps(result.raw)
    assert result.raw and "synthetic-jwt" not in raw_dump
    assert "00000000-0000-4000-8000-000000000001" not in raw_dump
    assert "redacted@example.com" not in raw_dump
    assert result.raw["creditUsagePercent"] == 8.0
    assert result.raw["isUnifiedBillingUser"] is True

    # registry class spend — text/JSON agree when pool_class applied
    result.pool_class = "spend"
    assert result.verdict.mark == "ok"
    table = render.table([result], color=False)
    brief = render.brief([result], color=False)
    payload = result.as_dict()
    assert payload["buckets"][0]["used_pct"] == 8.0
    assert "8%" in table
    assert "GrokBuild" in table
    assert "GrokChat" in table
    assert "grok" in brief
    assert "00000000-0000-4000-8000-000000000001" not in table
    assert "redacted@example.com" not in table


def test_grok_billing_missing_config_is_graceful_no_data(tmp_path, monkeypatch):
    _grok_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(grok, "_request_billing", lambda _token: (200, {}))
    monkeypatch.setattr(grok, "_fetch_plan", lambda _t: None)

    result = grok.fetch()
    assert result.buckets == []
    assert result.error
    assert "config" in result.error
    # used_pct 를 0으로 채우지 않는다
    assert result.verdict.blocking_pct == 0
    assert result.verdict.mark == "degraded"


def test_grok_billing_unexpected_period_type_warns(tmp_path, monkeypatch):
    _grok_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(
        grok,
        "_request_billing",
        lambda _token: (
            200,
            {
                "config": {
                    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_MONTHLY", "end": "2099-08-06T19:58:08Z"},
                    "creditUsagePercent": 8.0,
                }
            },
        ),
    )
    monkeypatch.setattr(grok, "_fetch_plan", lambda _t: None)

    result = grok.fetch()
    assert result.warning and "주기 유형" in result.warning
    assert result.buckets == []


def test_grok_billing_partial_product_usage_keeps_account_bucket(tmp_path, monkeypatch):
    _grok_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(
        grok,
        "_request_billing",
        lambda _token: (
            200,
            {
                "config": {
                    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": "2099-08-06T19:58:08Z"},
                    "creditUsagePercent": 8.0,
                    "isUnifiedBillingUser": True,
                    "productUsage": [
                        {"product": "GrokBuild", "usagePercent": 5.0},
                        {"product": "GrokBuild", "usagePercent": 99.0},
                        {"product": "GrokChat", "usagePercent": "not-a-number"},
                    ],
                }
            },
        ),
    )
    monkeypatch.setattr(grok, "_fetch_plan", lambda _t: None)

    result = grok.fetch()
    assert result.error is None
    assert result.note and "partial data" in result.note
    assert [b.label for b in result.buckets] == ["weekly", "GrokBuild"]
    by_label = {b.label: b for b in result.buckets}
    assert by_label["weekly"].used_pct == 8.0
    assert by_label["GrokBuild"].used_pct == 5.0
    assert "GrokChat" not in by_label
    assert "duplicate product" in result.note


def test_grok_billing_missing_product_usage_is_partial_data(tmp_path, monkeypatch):
    _grok_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(
        grok,
        "_request_billing",
        lambda _token: (
            200,
            {
                "config": {
                    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": "2099-08-06T19:58:08Z"},
                    "creditUsagePercent": 8.0,
                    "isUnifiedBillingUser": True,
                    # productUsage key intentionally missing
                }
            },
        ),
    )
    monkeypatch.setattr(grok, "_fetch_plan", lambda _t: None)

    result = grok.fetch()
    assert result.error is None
    assert result.warning is None
    assert result.note and "productUsage" in result.note and "누락" in result.note
    assert [b.label for b in result.buckets] == ["weekly"]
    assert result.buckets[0].used_pct == 8.0


def test_grok_billing_empty_product_usage_is_normal(tmp_path, monkeypatch):
    _grok_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(
        grok,
        "_request_billing",
        lambda _token: (
            200,
            {
                "config": {
                    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": "2099-08-06T19:58:08Z"},
                    "creditUsagePercent": 8.0,
                    "isUnifiedBillingUser": True,
                    "productUsage": [],
                }
            },
        ),
    )
    monkeypatch.setattr(grok, "_fetch_plan", lambda _t: None)

    result = grok.fetch()
    assert result.error is None
    assert result.warning is None
    assert result.note is None
    assert [b.label for b in result.buckets] == ["weekly"]


def test_grok_billing_percent_anomalies_are_explicit_warnings(tmp_path, monkeypatch):
    _grok_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(
        grok,
        "_request_billing",
        lambda _token: (
            200,
            {
                "config": {
                    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": "2099-08-06T19:58:08Z"},
                    "creditUsagePercent": 101.0,
                    "isUnifiedBillingUser": True,
                }
            },
        ),
    )
    monkeypatch.setattr(grok, "_fetch_plan", lambda _t: None)

    result = grok.fetch()
    assert result.warning and "creditUsagePercent" in result.warning
    assert result.buckets == []


def test_grok_http_429_is_error(tmp_path, monkeypatch):
    _grok_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(grok, "_request_billing", lambda *_a, **_k: (429, None))
    monkeypatch.setattr(grok, "_fetch_plan", lambda _t: None)
    result = grok.fetch()
    assert result.error and "429" in result.error
    assert result.buckets == []


def test_grok_auth_401_is_warning(tmp_path, monkeypatch):
    _grok_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(
        grok,
        "_request_billing",
        lambda *_a, **_k: (401, {"message": "unauthorized"}),
    )
    monkeypatch.setattr(grok, "_fetch_plan", lambda _t: None)
    result = grok.fetch()
    assert result.warning and "401" in result.warning
    assert result.error is None
    assert result.buckets == []


def test_grok_plan_from_subscriptions_redacts_account_fields(fixture_json, tmp_path, monkeypatch):
    _grok_auth(tmp_path, monkeypatch)
    monkeypatch.setattr(
        grok,
        "_request_billing",
        lambda *_a, **_k: (200, fixture_json("grok_billing")),
    )

    def fake_get(_token, url):
        if url == grok.SUBSCRIPTIONS_URL:
            # live-shaped but with synthetic ids that must not leak into provider raw
            dirty = fixture_json("grok_subscriptions")
            dirty = json.loads(json.dumps(dirty))
            dirty["subscriptions"][0]["xaiUserId"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            return 200, dirty
        return 404, None

    monkeypatch.setattr(grok, "_get_json", fake_get)
    result = grok.fetch()
    assert result.plan == "grok pro"
    dumped = json.dumps(result.as_dict(include_raw=True))
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" not in dumped
    assert "xaiUserId" not in dumped


def test_grok_registry_spend_class_and_list_order():
    assert "grok" in BUILTIN
    assert BUILTIN["grok"].pool_class == "spend"
    assert list(BUILTIN)[-1] == "grok"


def test_grok_cache_json_rendering_secret_free(fixture_json, tmp_path, monkeypatch):
    _grok_auth(tmp_path, monkeypatch, key="super-secret-token-value")

    monkeypatch.setattr(
        grok,
        "_request_billing",
        lambda _token: (200, fixture_json("grok_billing")),
    )
    monkeypatch.setattr(grok, "_fetch_plan", lambda _t: "grok pro")

    results = cache.collect({"grok": grok.fetch}, ["grok"], ttl_s=60, use_cache=True)
    results[0].pool_class = "spend"
    assert results[0].buckets[0].used_pct == 8.0

    # second collect hits cache
    cached = cache.collect(
        {"grok": lambda: (_ for _ in ()).throw(AssertionError("should use cache"))},
        ["grok"],
        ttl_s=60,
        use_cache=True,
    )
    assert cached[0].buckets[0].used_pct == 8.0

    table = render.table(results, color=False)
    brief = render.brief(results, color=False)
    payload = json.dumps(results[0].as_dict(include_raw=True))
    for surface in (table, brief, payload):
        assert "super-secret-token-value" not in surface
        assert "00000000-0000-4000-8000" not in surface
