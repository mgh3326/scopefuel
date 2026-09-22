"""Task 579: local manual quota observation and fail-closed gate integration."""

from __future__ import annotations

import datetime as dt
import json
import os
import time

import pytest

from scopefuel import cache, cli, manual, recommend
from scopefuel.model import Bucket, ProviderResult, Scope

_REAL_EXECUTION_CONTEXT = manual._execution_context


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
    monkeypatch.setenv("SCOPEFUEL_MANUAL", str(snapshot))
    monkeypatch.setenv("CODEX_HOME", "secret-value-must-not-be-recorded")

    _set_manual(capsys)

    assert snapshot.read_text(encoding="utf-8") == original
    assert manual.manual_path() == snapshot.parent / "manual.json"
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
    assert entry["author_principal"] == entry["author"]
    assert entry["account_ref"] == {
        "pool": "claude",
        "host": "local-host",
        "verification": "unverified",
        "local_only": True,
    }
    assert entry["supersedes_ref"] is None
    assert "execution_context" in entry
    assert entry["execution_context"]["parent"] == {"pid": 4242, "name": "zsh"}
    assert entry["execution_context"]["tty"] == {"stdin": False, "stdout": False}
    assert entry["execution_context"]["agent_environment_signals"] == {"CODEX_HOME": True}
    assert "secret-value-must-not-be-recorded" not in manual.manual_path().read_text(encoding="utf-8")
    assert oct(os.stat(manual.manual_path()).st_mode & 0o777) == "0o600"


def test_real_execution_context_records_signal_presence_without_its_value(monkeypatch):
    secret = "secret-value-must-never-reach-manual-store"
    monkeypatch.setattr(manual, "_execution_context", _REAL_EXECUTION_CONTEXT)
    monkeypatch.setenv("CODEX_HOME", secret)

    entry = manual.record_observation(
        pool="devin",
        used_pct=8.0,
        window="daily",
        measured_at=dt.datetime.now(dt.UTC),
        reason="real audit context check",
    )

    signals = entry["execution_context"]["agent_environment_signals"]
    assert signals["CODEX_HOME"] is True
    assert secret not in manual.manual_path().read_text(encoding="utf-8")


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
    assert "manual_windows=5h,7d" in captured.out
    assert "manual_supersedes=false" in captured.out
    assert "자동 측정 마지막 오류 HTTP 429 rate limit" in captured.out
    record = json.loads(gate_output.read_text(encoding="utf-8"))
    assert record["source"] == "operator"
    assert record["source_verification"] == "unverified"
    assert record["source_label"] == "자기신고 · 미검증"
    assert len(record["manual_observation_ids"]) == 2
    assert {item["window"] for item in record["manual_observations"]} == {"5h", "7d"}
    assert {item["status"] for item in record["manual_observations"]} == {"active"}
    assert all(item["supersedes_ref"] is None for item in record["manual_observations"])
    assert all(item["source_verification"] == "unverified" for item in record["manual_observations"])
    assert record["observed_age_s"] >= 0
    assert record["remaining_effect_s"] > 0
    assert record["last_auto_error"] == "HTTP 429 rate limit"


def test_gate_receipt_exposes_manual_supersedes_reference(claude_error_registry, capsys, tmp_path):
    _set_manual(capsys, window="5h", used="9")
    _set_manual(capsys, window="5h", used="10")
    _set_manual(capsys, window="7d", used="10")
    gate_output = tmp_path / "gate.json"

    assert cli.main(["gate", "-m", "opus", "--no-cache", "--gate-output", str(gate_output)]) == 0

    captured = capsys.readouterr()
    assert "manual_supersedes=true" in captured.out
    assert "supersedes yes" in captured.out
    observations = json.loads(gate_output.read_text(encoding="utf-8"))["manual_observations"]
    corrected = next(item for item in observations if item["window"] == "5h")
    assert corrected["supersedes_ref"] is not None


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


def test_fresh_automatic_low_value_wins_over_manual_high_value(claude_error_registry, capsys, monkeypatch):
    _set_claude_pair(capsys, used="95")
    fresh = _automatic_claude(10.0)
    monkeypatch.setattr(cli, "registry", lambda: {"claude": lambda: fresh})

    rc = cli.main(["gate", "-m", "opus", "--no-cache"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "used_pct=10.0" in captured.out
    assert "source=operator" not in captured.out
    assert cli.main(["--json", "--no-cache", "--only", "claude"]) == 0
    provider = json.loads(capsys.readouterr().out)["providers"][0]
    assert provider["source"] is None
    assert provider["manual"]["selection"] == "automatic_fresh"


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
    assert "manual fallback 불가: 검증된 auth 실패" in captured.err
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
    assert "manual fallback 불가: 필수 manual bucket 누락: 5h" in capsys.readouterr().err
    assert cli.main(["--json", "--no-cache", "--only", "claude"]) == 1
    provider = json.loads(capsys.readouterr().out)["providers"][0]
    assert provider["status"] == "error"
    assert provider["manual"]["selection"] == "incomplete_bucket_coverage"
    assert provider["manual"]["missing_windows"] == ["5h"]


def test_devin_weekly_observation_cannot_replace_required_daily_bucket(capsys, monkeypatch):
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {
            "devin": lambda: ProviderResult(
                id="devin",
                error="startup banner parse failure",
                pool_class="spend",
            )
        },
    )
    _set_manual(capsys, pool="devin", window="weekly", used="10")

    rc = cli.main(["gate", "-m", "devin-swe2", "--no-cache"])

    assert rc == 4
    assert "측정 불가" in capsys.readouterr().err
    assert cli.main(["--json", "--no-cache", "--only", "devin"]) == 1
    provider = json.loads(capsys.readouterr().out)["providers"][0]
    assert provider["manual"]["missing_windows"] == ["1d"]


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


def test_reentering_same_measurement_cannot_extend_effect():
    now = dt.datetime(2026, 9, 22, 6, 0, tzinfo=dt.UTC)
    measured = now
    first = manual.record_observation(
        pool="devin",
        used_pct=8.0,
        window="daily",
        measured_at=measured,
        reason="console reading",
        ttl_s=manual.DEFAULT_TTL_S,
        now=now,
    )
    with pytest.raises(manual.ManualError, match="새 --measured-at"):
        manual.record_observation(
            pool="devin",
            used_pct=8.0,
            window="daily",
            measured_at=measured,
            reason="same observation entered with a longer ttl",
            ttl_s=manual.MAX_TTL_S,
            now=now + dt.timedelta(minutes=10),
        )

    payload = manual.list_payload(pool="devin", now=now + dt.timedelta(minutes=16))
    assert first["expires_at"] == "2026-09-22T06:15:00Z"
    assert payload["latest_valid"] == []
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["status"] == "expired"


@pytest.mark.parametrize(
    "error",
    [
        "devin 기동 배너가 30초 안에 쿼타 줄을 그리지 않음",
        "devin 배너 프로브 실행 실패: resource temporarily unavailable",
        "kiro-cli /usage 가 30초 안에 끝나지 않음",
        "usage-limits 조회 실패 (URLError)",
        "usage-limits HTTP 408",
    ],
)
def test_known_timeout_and_transport_failures_are_manual_eligible(error):
    kind, _ = manual.classify_automatic_failure(ProviderResult(id="provider", error=error))
    assert kind == "transport"


def test_devin_banner_timeout_can_use_a_complete_daily_manual_observation(capsys, monkeypatch):
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {
            "devin": lambda: ProviderResult(
                id="devin",
                error="devin 기동 배너가 30초 안에 쿼타 줄을 그리지 않음",
                pool_class="spend",
            )
        },
    )
    _set_manual(capsys, pool="devin", window="daily", used="10")

    rc = cli.main(["gate", "-m", "devin-swe2", "--no-cache"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "source=operator" in captured.out
    assert "자기신고 · 미검증" in captured.out


def test_fresh_no_data_result_is_parse_failure_and_can_use_complete_manual(
    claude_error_registry, capsys, monkeypatch
):
    _set_claude_pair(capsys)
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {
            "claude": lambda: ProviderResult(
                id="claude",
                note="no data — automatic response had no quota buckets",
                pool_class="preserve",
            )
        },
    )

    rc = cli.main(["gate", "-m", "opus", "--no-cache"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "source=operator" in captured.out
    assert "no data" in captured.out
    payload = manual.list_payload(pool="claude")
    assert len(payload["latest_valid"]) == 2
    assert {entry["status"] for entry in payload["entries"]} == {"active"}


def test_login_hint_is_auth_failure_even_when_primary_error_looks_like_parse_failure():
    result = ProviderResult(
        id="kiro",
        error="/usage 출력에서 크레딧 줄을 찾지 못함",
        hint="kiro-cli 로그인이 필요해 보입니다 (kiro-cli login)",
    )

    kind, _ = manual.classify_automatic_failure(result)

    assert kind == "auth"


def test_stale_fallback_preserves_auth_hint_for_fail_closed_classification():
    healthy = _automatic_claude(10.0)
    cache.collect({"claude": lambda: healthy}, ["claude"], now=1000.0)

    stale = cache.collect(
        {
            "claude": lambda: ProviderResult(
                id="claude",
                error="usage output could not be parsed",
                hint="login required",
            )
        },
        ["claude"],
        now=1100.0,
        ttl_s=0.0,
    )[0]

    assert stale.last_error == "usage output could not be parsed — login required"
    assert manual.classify_automatic_failure(stale)[0] == "auth"


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


def test_future_dated_structurally_valid_store_fails_closed(claude_error_registry, capsys):
    _set_claude_pair(capsys)
    path = manual.manual_path()
    store = json.loads(path.read_text(encoding="utf-8"))
    future = dt.datetime.now(dt.UTC) + dt.timedelta(hours=12)
    for entry in store["history"]:
        entry["measured_at"] = future.isoformat().replace("+00:00", "Z")
        entry["entered_at"] = future.isoformat().replace("+00:00", "Z")
        entry["expires_at"] = (future + dt.timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
    original = json.dumps(store)
    path.write_text(original, encoding="utf-8")

    assert cli.main(["gate", "-m", "opus", "--no-cache"]) == 4
    assert "manual store 오류" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == original
