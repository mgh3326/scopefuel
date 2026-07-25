"""리댁션된 실제 응답 픽스처로 각 provider 파서를 검증한다 (네트워크 없음)."""

from __future__ import annotations

import json

from scopefuel.providers import agy, claude, codex


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
