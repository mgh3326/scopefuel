"""Task 579: local manual quota observation and fail-closed gate integration."""

from __future__ import annotations

import datetime as dt
import json
import os
import time

import pytest

from scopefuel import cache, cli, manual, recommend
from scopefuel.model import Bucket, ProviderResult, Scope


@pytest.fixture(autouse=True)
def stable_audit_context(monkeypatch):
    monkeypatch.setattr(
        manual,
        "_execution_context",
        lambda: {
            "parent": {"pid": 4242, "name": "zsh"},
            "ancestors": [{"pid": 41, "name": "Terminal"}],
            "tty": {"stdin": False, "stdout": False},
            "agent_environment_signals": {"CODEX_HOME": True},
        },
    )
    monkeypatch.setattr(
        manual,
        "_author",
        lambda: {
            "os_user": "local-user",
            "host": "local-host",
            "verification": "unverified",
            "label": manual.SOURCE_LABEL,
        },
    )


@pytest.fixture
def claude_error_registry(monkeypatch):
    def install(error: str = "HTTP 429 rate limit") -> None:
        monkeypatch.setattr(
            cli,
            "registry",
            lambda: {"claude": lambda: ProviderResult(id="claude", error=error, pool_class="preserve")},
        )

    install()
    return install


def _set_manual(
    capsys,
    *,
    pool: str = "claude",
    window: str = "5h",
    used: str = "10",
    measured_at: str = "now",
    ttl: str = "15m",
) -> None:
    rc = cli.main(
        [
            "manual",
            "set",
            "--pool",
            pool,
            "--window",
            window,
            "--used",
            used,
            "--measured-at",
            measured_at,
            "--reason",
            "automatic measurement outage",
            "--ttl",
            ttl,
        ]
    )
    assert rc == 0
    capsys.readouterr()


def _set_claude_pair(capsys, *, used: str = "10", measured_at: str = "now", ttl: str = "15m") -> None:
    _set_manual(capsys, window="5h", used=used, measured_at=measured_at, ttl=ttl)
    _set_manual(capsys, window="7d", used=used, measured_at=measured_at, ttl=ttl)


def _automatic_claude(used: float, *, stale: bool = False, last_error: str | None = None) -> ProviderResult:
    return ProviderResult(
        id="claude",
        buckets=[
            Bucket(label="5h", window="5h", used_pct=used, scope=Scope("account"), horizon="now"),
            Bucket(label="7d", window="7d", used_pct=used, scope=Scope("account"), horizon="week"),
        ],
        fetched_at=time.time() - 60,
        stale=stale,
        last_error=last_error,
        pool_class="preserve",
    )


def test_manual_set_is_separate_append_only_and_records_unverified_execution_context(
    claude_error_registry, capsys, monkeypatch
):
    snapshot = cache.cache_path()
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    original = '{"sentinel":"automatic snapshot unchanged"}\n'
    snapshot.write_text(original, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", "secret-value-must-not-be-recorded")

    _set_manual(capsys)

    assert snapshot.read_text(encoding="utf-8") == original
    store = json.loads(manual.manual_path().read_text(encoding="utf-8"))
    assert store["schema"] == manual.SCHEMA
    assert len(store["history"]) == 1
    entry = store["history"][0]
    assert entry["source"] == "operator"
    assert entry["source_verification"] == "unverified"
    assert entry["source_label"] == "자기신고 · 미검증"
    assert entry["author"] == {
        "os_user": "local-user",
        "host": "local-host",
        "verification": "unverified",
        "label": "자기신고 · 미검증",
    }
    assert entry["execution_context"]["parent"] == {"pid": 4242, "name": "zsh"}
    assert entry["execution_context"]["tty"] == {"stdin": False, "stdout": False}
    assert entry["execution_context"]["agent_environment_signals"] == {"CODEX_HOME": True}
    assert "secret-value-must-not-be-recorded" not in manual.manual_path().read_text(encoding="utf-8")
    assert oct(os.stat(manual.manual_path()).st_mode & 0o777) == "0o600"


def test_manual_list_and_json_show_unverified_source_and_counts(claude_error_registry, capsys):
    _set_manual(capsys)

    assert cli.main(["manual", "list"]) == 0
    text = capsys.readouterr().out
    assert "source=operator" in text
    assert "source_verification=unverified" in text
    assert "자기신고 · 미검증" in text
    assert "input=1" in text

    assert cli.main(["--json", "manual", "list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_verification"] == "unverified"
    assert payload["entries"][0]["execution_context"]["parent"]["pid"] == 4242
    assert payload["counts"] == {"input": 1, "expired": 0, "replaced": 0}


def test_json_uses_complete_manual_fallback_with_audit_metadata(claude_error_registry, capsys):
    _set_claude_pair(capsys)

    assert cli.main(["--json", "--no-cache", "--only", "claude"]) == 0
    payload = json.loads(capsys.readouterr().out)
    provider = payload["providers"][0]
    assert provider["status"] == "ok"
    assert provider["source"] == "operator"
    assert provider["last_error"] == "HTTP 429 rate limit"
    assert provider["manual"]["source_verification"] == "unverified"
    assert provider["manual"]["source_label"] == "자기신고 · 미검증"
    assert provider["manual"]["selection"] == "manual_fallback"
    assert provider["manual"]["missing_windows"] == []
    assert {bucket["window"] for bucket in provider["buckets"]} == {"5h", "7d"}


def test_gate_uses_manual_on_retryable_failure_and_emits_receipt(claude_error_registry, capsys, tmp_path):
    _set_claude_pair(capsys)
    gate_output = tmp_path / "gate.json"

    rc = cli.main(["gate", "-m", "opus", "--no-cache", "--gate-output", str(gate_output)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "source=operator" in captured.out
    assert "source_verification=unverified" in captured.out
    assert "자기신고 · 미검증" in captured.out
    assert "자동 측정 마지막 오류 HTTP 429 rate limit" in captured.out
    record = json.loads(gate_output.read_text(encoding="utf-8"))
    assert record["source"] == "operator"
    assert record["source_verification"] == "unverified"
    assert record["source_label"] == "자기신고 · 미검증"
    assert len(record["manual_observation_ids"]) == 2
    assert record["observed_age_s"] >= 0
    assert record["remaining_effect_s"] > 0
    assert record["last_auto_error"] == "HTTP 429 rate limit"


def test_fresh_automatic_cutoff_cannot_be_hidden_by_manual_low_value(
    claude_error_registry, capsys, monkeypatch
):
    _set_claude_pair(capsys, used="1")
    fresh = _automatic_claude(95.0)
    monkeypatch.setattr(cli, "registry", lambda: {"claude": lambda: fresh})

    rc = cli.main(["gate", "-m", "opus", "--no-cache"])
    captured = capsys.readouterr()
    assert rc == 3
    assert "95% 소진" in captured.err
    assert "source=operator" not in captured.err


def test_stale_automatic_cutoff_confirmation_cannot_be_hidden(claude_error_registry, capsys, monkeypatch):
    _set_claude_pair(capsys, used="1")
    stale = _automatic_claude(95.0, stale=True, last_error="HTTP 429 rate limit")
    monkeypatch.setattr(cli, "registry", lambda: {"claude": lambda: stale})

    rc = cli.main(["gate", "-m", "opus", "--no-cache"])
    captured = capsys.readouterr()
    assert rc == 3
    assert "95% 소진" in captured.err
    assert "source=operator" not in captured.err

    assert cli.main(["--json", "--no-cache", "--only", "claude"]) == 0
    provider = json.loads(capsys.readouterr().out)["providers"][0]
    assert provider["source"] is None
    assert provider["manual"]["selection"] == "automatic_cutoff"
    assert provider["manual"]["automatic_cutoff"] == {"used_pct": 95.0, "cutoff": 90.0}


def test_measured_at_older_than_two_hours_is_expired(claude_error_registry, capsys):
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2, seconds=10)).isoformat()
    _set_claude_pair(capsys, measured_at=old, ttl="2h")

    rc = cli.main(["gate", "-m", "opus", "--no-cache"])
    assert rc == 4
    assert "측정 불가" in capsys.readouterr().err

    assert cli.main(["--json", "manual", "list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {entry["status"] for entry in payload["entries"]} == {"expired"}
    assert payload["latest_valid"] == []
    assert payload["counts"]["expired"] == 2


@pytest.mark.parametrize("status", [401, 403])
def test_verified_auth_failure_cannot_be_hidden_by_manual(claude_error_registry, capsys, status):
    _set_claude_pair(capsys)
    claude_error_registry(f"HTTP {status} authentication failed")

    rc = cli.main(["gate", "-m", "opus", "--no-cache"])
    captured = capsys.readouterr()
    assert rc == 4
    assert "측정 불가" in captured.err
    assert "source=operator" not in captured.err


def test_newer_automatic_success_supersedes_manual_even_after_probe_failure(
    claude_error_registry, capsys, monkeypatch
):
    _set_claude_pair(capsys)
    stale = _automatic_claude(10.0, stale=True, last_error="HTTP 429 rate limit")
    stale.fetched_at = time.time() + 1
    monkeypatch.setattr(cli, "registry", lambda: {"claude": lambda: stale})

    assert cli.main(["gate", "-m", "opus", "--no-cache"]) == 4
    capsys.readouterr()
    assert cli.main(["--json", "--no-cache", "--only", "claude"]) == 0
    provider = json.loads(capsys.readouterr().out)["providers"][0]
    assert {entry["status"] for entry in provider["manual"]["entries"]} == {"superseded_by_auto"}
    assert provider["manual"]["latest_valid"] == []


def test_missing_required_five_hour_bucket_stays_fail_closed(claude_error_registry, capsys):
    _set_manual(capsys, window="7d")

    assert cli.main(["gate", "-m", "opus", "--no-cache"]) == 4
    capsys.readouterr()
    assert cli.main(["--json", "--no-cache", "--only", "claude"]) == 1
    provider = json.loads(capsys.readouterr().out)["providers"][0]
    assert provider["status"] == "error"
    assert provider["manual"]["selection"] == "incomplete_bucket_coverage"
    assert provider["manual"]["missing_windows"] == ["5h"]


def test_manual_used_zero_still_runs_normal_cutoff_check(claude_error_registry, capsys, monkeypatch):
    _set_claude_pair(capsys, used="0")
    monkeypatch.setattr(recommend, "PRESERVE_EXCLUDE_PCT", 0.0)

    rc = cli.main(["gate", "-m", "opus", "--no-cache"])
    captured = capsys.readouterr()
    assert rc == 3
    assert "0% 소진 (cutoff 0%" in captured.err
    assert "source=operator" in captured.err


def test_clear_is_append_only_and_disables_all_pool_entries(claude_error_registry, capsys):
    _set_claude_pair(capsys)
    assert cli.main(["manual", "clear", "--pool", "claude"]) == 0
    clear_output = capsys.readouterr().out
    assert "source_verification=unverified" in clear_output

    store = json.loads(manual.manual_path().read_text(encoding="utf-8"))
    assert [event["event"] for event in store["history"]] == ["set", "set", "clear"]
    assert cli.main(["gate", "-m", "opus", "--no-cache"]) == 4
    capsys.readouterr()
    payload = manual.list_payload(pool="claude")
    assert payload["latest_valid"] == []
    assert {entry["status"] for entry in payload["entries"]} == {"cleared"}


def test_reentering_same_old_measurement_does_not_extend_effect(monkeypatch):
    now = dt.datetime(2026, 9, 22, 6, 0, tzinfo=dt.UTC)
    measured = now - dt.timedelta(hours=3)
    first = manual.record_observation(
        pool="devin",
        used_pct=8.0,
        window="daily",
        measured_at=measured,
        reason="old console reading",
        ttl_s=manual.MAX_TTL_S,
        now=now,
    )
    second = manual.record_observation(
        pool="devin",
        used_pct=8.0,
        window="daily",
        measured_at=measured,
        reason="same old console reading entered again",
        ttl_s=manual.MAX_TTL_S,
        now=now + dt.timedelta(minutes=5),
    )

    assert second["supersedes"] == first["manual_observation_id"]
    assert second["expires_at"] == first["expires_at"]
    payload = manual.list_payload(pool="devin", now=now + dt.timedelta(minutes=5))
    assert payload["latest_valid"] == []
    assert payload["entries"][0]["status"] == "superseded"
    assert payload["entries"][1]["status"] == "expired"


def test_ttl_above_two_hours_is_rejected_without_writing(claude_error_registry, capsys):
    rc = cli.main(
        [
            "manual",
            "set",
            "--pool",
            "claude",
            "--window",
            "5h",
            "--used",
            "10",
            "--measured-at",
            "now",
            "--reason",
            "outage",
            "--ttl",
            "2h1s",
        ]
    )
    assert rc == 2
    assert "2h 이하" in capsys.readouterr().err
    assert not manual.manual_path().exists()


def test_reset_boundary_shortens_effect_before_ttl():
    now = dt.datetime(2026, 9, 22, 6, 0, tzinfo=dt.UTC)
    entry = manual.record_observation(
        pool="devin",
        used_pct=8.0,
        window=None,
        measured_at=now,
        reason="daily console reading",
        ttl_s=manual.DEFAULT_TTL_S,
        resets_in_s=5 * 60,
        now=now,
    )
    assert entry["window"] == "1d"
    assert entry["expires_at"] == "2026-09-22T06:05:00Z"
    assert manual.list_payload(pool="devin", now=now + dt.timedelta(minutes=5))["latest_valid"] == []


def test_cache_stale_fallback_preserves_structured_last_error_without_persisting_it():
    healthy = _automatic_claude(10.0)
    first = cache.collect({"claude": lambda: healthy}, ["claude"], now=1000.0)
    assert first[0].status == "ok"

    second = cache.collect(
        {"claude": lambda: ProviderResult(id="claude", error="HTTP 429 rate limit")},
        ["claude"],
        now=1100.0,
        ttl_s=0.0,
    )
    assert second[0].stale is True
    assert second[0].last_error == "HTTP 429 rate limit"
    snapshot_text = cache.cache_path().read_text(encoding="utf-8")
    assert "last_error" not in snapshot_text


def test_corrupt_or_verified_claim_store_fails_closed_without_overwrite(claude_error_registry, capsys):
    path = manual.manual_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = {
        "schema": manual.SCHEMA,
        "history": [
            {
                "event": "set",
                "manual_observation_id": "forged",
                "pool": "claude",
                "window": "5h",
                "used_pct": 0,
                "measured_at": "2026-09-22T06:00:00Z",
                "entered_at": "2026-09-22T06:00:00Z",
                "expires_at": "2026-09-22T06:15:00Z",
                "reason": "forged",
                "source": "operator",
                "source_verification": "verified",
            }
        ],
        "latest": {"claude:5h": "forged"},
    }
    original = json.dumps(corrupt)
    path.write_text(original, encoding="utf-8")

    assert cli.main(["gate", "-m", "opus", "--no-cache"]) == 4
    assert "manual store 오류" in capsys.readouterr().err
    rc = cli.main(
        [
            "manual",
            "set",
            "--pool",
            "claude",
            "--window",
            "5h",
            "--used",
            "10",
            "--measured-at",
            "now",
            "--reason",
            "must not overwrite corrupt history",
        ]
    )
    assert rc == 2
    assert "manual history" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == original
