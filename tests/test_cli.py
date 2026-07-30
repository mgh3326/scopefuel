"""CLI 계약: JSON 스키마, 종료코드, brief 표현."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from scopefuel import cli, render
from scopefuel.model import SCHEMA, Bucket, ProviderResult, Scope, pace_for

CLAUDE = ProviderResult(
    id="claude",
    plan="max",
    buckets=[
        Bucket(label="5h", window="5h", used_pct=6.0, scope=Scope("account"), horizon="now"),
        Bucket(label="7d all", window="7d", used_pct=97.0, scope=Scope("account"), horizon="week"),
        Bucket(label="7d Fable", window="7d", used_pct=100.0, scope=Scope("model", "Fable"), horizon="week"),
    ],
)
AGY = ProviderResult(
    id="agy",
    buckets=[
        Bucket(label="gemini 5h", window="5h", used_pct=7.4, scope=Scope("group", "gemini"), horizon="now"),
        Bucket(label="3p 5h", window="5h", used_pct=57.3, scope=Scope("group", "3p"), horizon="now"),
    ],
)


@pytest.fixture(autouse=True)
def stub_registry(monkeypatch):
    monkeypatch.setattr(cli, "registry", lambda: {"claude": lambda: CLAUDE, "agy": lambda: AGY})


def test_json_contract(capsys):
    assert cli.main(["--json", "--no-cache"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == SCHEMA
    assert payload["summary"]["mark"] == "crit"
    claude = next(p for p in payload["providers"] if p["id"] == "claude")
    assert claude["verdict"]["now_pct"] == 6.0
    assert claude["verdict"]["week_pct"] == 97.0
    assert claude["verdict"]["blocking_pct"] == 97.0
    assert [b["scope"]["kind"] for b in claude["buckets"]] == ["account", "account", "model"]
    agy = next(p for p in payload["providers"] if p["id"] == "agy")
    assert agy["verdict"]["basis"] == "group"


def test_brief_shows_both_axes_and_exhausted_scope():
    line = render.brief([CLAUDE, AGY], color=False)
    assert "now 사용 6%" in line and "week 사용 97%" in line
    assert "Fable소진" in line
    assert "gemini 사용 7.4%" in line and "3p 사용 57.3%" in line
    assert line.startswith("[CRIT]")


def test_brief_horizon_now_only():
    line = render.brief([CLAUDE], color=False, horizon="now")
    assert "now 사용 6%" in line and "week" not in line


def test_exit_code_on_threshold(capsys):
    assert cli.main(["--brief", "--no-cache", "--exit-code-on", "crit"]) == 2
    assert cli.main(["--brief", "--no-cache", "--only", "agy", "--exit-code-on", "crit"]) == 0
    capsys.readouterr()


def test_unknown_provider_is_rejected(capsys):
    assert cli.main(["--only", "nope"]) == 2
    assert "알 수 없는 provider" in capsys.readouterr().err


def test_error_provider_yields_exit_1(capsys, monkeypatch):
    monkeypatch.setattr(cli, "registry", lambda: {"x": lambda: ProviderResult(id="x", error="boom")})
    assert cli.main(["--no-cache"]) == 1
    assert "boom" in capsys.readouterr().out


def test_table_marks_model_scope(capsys):
    cli.main(["--no-cache", "--no-color", "--only", "claude"])
    out = capsys.readouterr().out
    assert "이 모델만" in out
    assert "Fable 소진" in out
    assert "지금(5h급) 사용 6%" in out


def test_brief_shows_error_reason_and_degraded_mark():
    err_res = ProviderResult(id="codex", error="HTTP 503 circuit open")
    line = render.brief([err_res], color=False)
    assert line == "[DEGRADED] codex n/a(HTTP 503 circuit open)"


def test_errored_provider_summary_mark_json(capsys, monkeypatch):
    monkeypatch.setattr(
        cli, "registry", lambda: {"codex": lambda: ProviderResult(id="codex", error="HTTP 503")}
    )
    assert cli.main(["--json", "--no-cache"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["mark"] == "degraded"
    assert payload["providers"][0]["status"] == "error"
    assert payload["providers"][0]["verdict"]["mark"] == "degraded"


def test_exit_code_on_warn_triggers_exit_2_on_errored_provider(capsys, monkeypatch):
    monkeypatch.setattr(
        cli, "registry", lambda: {"codex": lambda: ProviderResult(id="codex", error="HTTP 503")}
    )
    assert cli.main(["--brief", "--no-cache", "--exit-code-on", "warn"]) == 2
    capsys.readouterr()


def test_warning_provider_stays_alive_but_is_not_false_ok(capsys, monkeypatch):
    auth_warning = ProviderResult(id="clinepass", warning="인증 실패 (HTTP 401) — 키를 확인하세요")
    healthy = ProviderResult(
        id="healthy",
        buckets=[Bucket(label="5h", window="5h", used_pct=10, horizon="now")],
    )
    monkeypatch.setattr(
        cli, "registry", lambda: {"healthy": lambda: healthy, "clinepass": lambda: auth_warning}
    )

    assert cli.main(["--no-cache", "--no-color"]) == 0
    output = capsys.readouterr().out
    assert "healthy" in output
    assert "clinepass  [WARN] 인증 실패 (HTTP 401) — 키를 확인하세요" in output


NOW = dt.datetime(2026, 7, 26, 12, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("window", "reset_at", "expected_pace", "expected_rate", "unit"),
    [
        ("7d", "2026-07-29T12:00:00Z", 0.7, 20.0, "%/일"),
        ("5h", "2026-07-26T14:30:00Z", 0.8, 24.0, "%/h"),
    ],
)
def test_pace_supports_day_and_hour_windows(window, reset_at, expected_pace, expected_rate, unit):
    pace = pace_for(Bucket(label=window, window=window, used_pct=40, resets_at=reset_at), now=NOW)
    assert pace.ratio == pytest.approx(expected_pace)
    assert pace.full_use_rate == pytest.approx(expected_rate)
    assert pace.full_use_rate_unit == unit


@pytest.mark.parametrize(
    "window,reset_at",
    [("nonsense", "2026-07-29T12:00:00Z"), ("7d", None), ("7d", "not-a-date")],
)
def test_pace_is_blank_when_window_or_reset_is_unparseable(window, reset_at):
    pace = pace_for(Bucket(label="x", window=window, used_pct=40, resets_at=reset_at), now=NOW)
    assert pace.ratio is pace.full_use_rate is pace.full_use_rate_unit is None


@pytest.mark.parametrize(
    "reset_at",
    ["2026-08-02T12:00:00Z", "2026-07-19T12:00:00Z"],
)
def test_pace_is_blank_outside_active_window(reset_at):
    pace = pace_for(Bucket(label="x", window="7d", used_pct=40, resets_at=reset_at), now=NOW)
    assert pace.ratio is pace.full_use_rate is None


def test_pace_is_blank_at_reset_and_after_reset():
    for reset_at in ("2026-07-26T12:00:00Z", "2026-07-26T11:59:59Z"):
        pace = pace_for(Bucket(label="x", window="7d", used_pct=40, resets_at=reset_at), now=NOW)
        assert pace.ratio is pace.full_use_rate is None


def test_json_adds_null_pace_instead_of_misleading_zero(capsys):
    assert cli.main(["--json", "--no-cache", "--only", "claude"]) == 0
    payload = json.loads(capsys.readouterr().out)
    bucket = payload["providers"][0]["buckets"][0]
    assert bucket["pace"] is None
    assert bucket["full_use_rate"] is None
