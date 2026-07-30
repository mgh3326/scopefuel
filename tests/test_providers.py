"""리댁션된 실제 응답 픽스처로 각 provider 파서를 검증한다 (네트워크 없음)."""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message

from scopefuel import render
from scopefuel.providers import BUILTIN, agy, claude, clinepass, codex, kiro


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
    assert list(BUILTIN) == ["claude", "codex", "agy", "kiro", "clinepass"]


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


def test_clinepass_applies_rate_headers_from_http_error(monkeypatch):
    headers = Message()
    headers["x-ratelimit-limit-requests"] = "100"
    headers["x-ratelimit-remaining-requests"] = "75"
    headers["x-ratelimit-reset-requests"] = "5h"
    error = urllib.error.HTTPError(
        clinepass.USAGE_URL, 429, "rate limited", headers, io.BytesIO(b'{"error":"limited"}')
    )

    def raise_http_error(*args, **kwargs):
        raise error

    monkeypatch.setattr(clinepass.urllib.request, "urlopen", raise_http_error)

    status, actual = clinepass._probe("not-a-real-key")
    assert status == 429
    assert actual["x-ratelimit-remaining-requests"] == "75"


def test_clinepass_parses_headers_without_exposing_key(monkeypatch):
    headers = Message()
    headers["x-ratelimit-limit-requests"] = "100"
    headers["x-ratelimit-remaining-requests"] = "75"
    headers["x-ratelimit-reset-requests"] = "5h"
    headers["x-ratelimit-limit-tokens"] = "1000"
    headers["x-ratelimit-remaining-tokens"] = "250"
    headers["x-ratelimit-reset-tokens"] = "1d"
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("super-secret", "env:CLINE_API_KEY"))
    monkeypatch.setattr(clinepass, "_probe", lambda _key: (400, headers))

    result = clinepass.fetch()

    assert result.error is None
    assert result.note == "HTTP 400 응답의 rate-limit 헤더 적용"
    assert [(b.label, b.used_pct) for b in result.buckets] == [
        ("requests", 25.0),
        ("tokens", 75.0),
    ]
    assert result.raw and result.raw["credential"] == {
        "source": "env:CLINE_API_KEY",
        "present": True,
    }
    assert "super-secret" not in json.dumps(result.raw)


def test_clinepass_missing_rate_headers_is_graceful_no_data(monkeypatch):
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("super-secret", "env:CLINE_API_KEY"))
    monkeypatch.setattr(clinepass, "_probe", lambda _key: (200, Message()))

    result = clinepass.fetch()

    assert result.status == "ok"
    assert result.error is None
    assert result.buckets == []
    assert result.note and result.note.startswith("no data — rate-limit 헤더 없음")
    assert result.raw and result.raw["rate_limit_headers"] == {}
    assert "super-secret" not in json.dumps(result.as_dict(include_raw=True))


def test_clinepass_auth_failures_warn_but_200_without_headers_is_no_data(monkeypatch):
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("synthetic-key", "env:CLINE_API_KEY"))

    outputs = {}
    for status in (401, 403, 200):
        monkeypatch.setattr(clinepass, "_probe", lambda _key, status=status: (status, Message()))
        result = clinepass.fetch()
        outputs[status] = render.table([result], color=False)

    assert outputs[401] == "clinepass  [WARN] 인증 실패 (HTTP 401) — 키를 확인하세요\n"
    assert outputs[403] == "clinepass  [WARN] 인증 실패 (HTTP 403) — 키를 확인하세요\n"
    assert outputs[200].startswith("clinepass  [ok] 한도 정보 없음")
    assert "no data — rate-limit 헤더 없음, HTTP 200" in outputs[200]
    assert len(set(outputs.values())) == 3


def test_clinepass_sanitizes_probe_value_error(monkeypatch):
    marker = "ZZ-SYNTHETIC-MARKER-F1-NOT-A-REAL-KEY"
    monkeypatch.setattr(clinepass, "_api_key", lambda: (marker, "env:CLINE_API_KEY"))

    def leak_if_unsanitized(_key):
        raise ValueError(f"Invalid header value b'Bearer {marker}'")

    monkeypatch.setattr(clinepass, "_probe", leak_if_unsanitized)
    result = clinepass.fetch()
    output = render.table([result], color=False) + render.brief([result], color=False)
    serialized = json.dumps(result.as_dict(include_raw=True), ensure_ascii=False)

    assert result.error == "invalid credential format"
    assert marker not in output
    assert marker not in serialized


def test_clinepass_sanitizes_unexpected_probe_and_header_errors(monkeypatch):
    marker = "ZZ-SYNTHETIC-MARKER-F1-NOT-A-REAL-KEY"
    monkeypatch.setattr(clinepass, "_api_key", lambda: ("synthetic-key", "env:CLINE_API_KEY"))

    def probe_failure(_key):
        raise RuntimeError(f"request/URL/body detail: {marker}")

    monkeypatch.setattr(clinepass, "_probe", probe_failure)
    probe_result = clinepass.fetch()

    class BadHeaders:
        def items(self):
            raise RuntimeError(f"response header detail: {marker}")

    monkeypatch.setattr(clinepass, "_probe", lambda _key: (200, BadHeaders()))
    header_result = clinepass.fetch()

    for result in (probe_result, header_result):
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
            "_probe",
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
