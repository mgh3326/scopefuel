"""hk 2136: ``scopefuel --json`` lists every manual-only pool as a provider.

The arbiter reads the ``providers`` ids of ``scopefuel --json`` (schema
scopefuel.v1) as the pool canon.  Before this change the json path appended
no manual-only result, so ``anthropic-api`` (the pi credit pool, hk 2079) was
missing there even though ``scopefuel gate`` prints ``pool=anthropic-api``.

Each test docstring names the mutant it kills where the brief lists one.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from scopefuel import cli, manual, providers, recommend
from scopefuel.model import Bucket, ProviderResult, Scope

NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)
TODAY = NOW.date()
CREDIT_EXPIRES = (TODAY + dt.timedelta(days=30)).isoformat()

# Pools that gate can name in ``pool=`` but that --json does not list.  oc-omni
# (a grade-table row) bills ``omniroute``, which has no registered fetcher and
# no manual route, so gate can never answer it ok and --json has no entry for
# it.  The operator retired omniroute on 2026-09-26 (config profiles.oc-omni
# subscribed=false, "omniroute 미사용"), so it is not live.  Pinned here so a
# new unlisted pool still fails the drift test below; remove this exception
# together with oc-omni.
UNLISTED_GATE_POOLS = frozenset({"omniroute"})


def _record_credit(
    *,
    remaining: float = 150.0,
    total: float = 200.0,
    expires_on: str = CREDIT_EXPIRES,
    measured_at: dt.datetime = NOW,
    ttl_s: float | None = None,
    now: dt.datetime = NOW,
) -> dict:
    return manual.record_observation(
        pool="anthropic-api",
        window="credit",
        remaining_usd=remaining,
        total_usd=total,
        credit_expires_on=expires_on,
        measured_at=measured_at,
        reason="Anthropic console credit balance",
        ttl_s=ttl_s,
        now=now,
    )


def _claude_healthy() -> ProviderResult:
    return ProviderResult(
        id="claude",
        pool_class="preserve",
        buckets=[Bucket(label="7d", window="7d", used_pct=10.0, scope=Scope("account"), horizon="week")],
    )


def _claude_crit() -> ProviderResult:
    return ProviderResult(
        id="claude",
        pool_class="preserve",
        buckets=[Bucket(label="7d", window="7d", used_pct=100.0, scope=Scope("account"), horizon="week")],
    )


def _stub_registry(monkeypatch, fetcher=_claude_healthy) -> None:
    monkeypatch.setattr(cli, "registry", lambda: {"claude": fetcher})


def _json_payload(capsys, argv: list[str]) -> tuple[int, dict]:
    rc = cli.main(["--json", "--no-cache", *argv])
    return rc, json.loads(capsys.readouterr().out)


def _entry(payload: dict, pool: str = "anthropic-api") -> dict:
    (entry,) = [item for item in payload["providers"] if item["id"] == pool]
    return entry


def _no_usage_shown(entry: dict) -> None:
    """Fail-closed: the entry carries no used_pct, so nothing reads as availability."""

    assert all(bucket["used_pct"] is None for bucket in entry["buckets"])
    assert entry["verdict"]["now_pct"] is None
    assert entry["verdict"]["week_pct"] is None


def test_json_lists_anthropic_api_with_no_observation_fail_closed(capsys, monkeypatch):
    """Mutant: the --json path stops appending manual-only results -> this goes RED."""

    _stub_registry(monkeypatch)
    rc, payload = _json_payload(capsys, [])

    assert rc == 0
    assert [item["id"] for item in payload["providers"]] == ["claude", "anthropic-api", "commandcode"]
    entry = _entry(payload)
    assert entry["status"] == "error"
    assert entry["error_kind"] == "manual_only"
    assert entry["pool_class"] == "spend"
    _no_usage_shown(entry)


def test_json_anthropic_api_shows_a_valid_credit_observation(capsys, monkeypatch):
    _record_credit(remaining=150.0, total=200.0)
    _stub_registry(monkeypatch)
    rc, payload = _json_payload(capsys, [])

    assert rc == 0
    entry = _entry(payload)
    assert entry["error"] is None
    assert entry["status"] == "ok"
    (bucket,) = entry["buckets"]
    assert bucket["used_pct"] == pytest.approx(25.0)


def test_json_anthropic_api_stale_observation_fails_closed(capsys, monkeypatch):
    _record_credit(measured_at=NOW - dt.timedelta(hours=2), ttl_s=900.0)
    _stub_registry(monkeypatch)
    rc, payload = _json_payload(capsys, [])

    assert rc == 0
    entry = _entry(payload)
    assert entry["status"] == "error"
    _no_usage_shown(entry)


def test_json_anthropic_api_expired_observation_fails_closed():
    """The expiry boundary is the midnight UTC of ``credit_expires_on``; json must fail closed there."""

    expires_on = TODAY + dt.timedelta(days=1)
    _record_credit(expires_on=expires_on.isoformat())
    expiry_start = dt.datetime.combine(expires_on, dt.time.min, tzinfo=dt.UTC)
    listed = cli._listed_with_manual_only([], now=expiry_start)

    (entry,) = [result for result in listed if result.id == "anthropic-api"]
    assert entry.error is not None
    assert entry.buckets == []


def test_unobserved_manual_pool_does_not_change_summary_or_exit_code(capsys, monkeypatch):
    """A degraded manual entry is listed; the host mark and exit code come from the automatic results only."""

    _stub_registry(monkeypatch, fetcher=_claude_crit)
    rc, payload = _json_payload(capsys, [])
    assert payload["summary"] == {"mark": "crit", "usage_mark": "crit"}
    assert _entry(payload)["verdict"]["mark"] == "degraded"
    assert rc == 0

    _stub_registry(monkeypatch, fetcher=_claude_healthy)
    rc, payload = _json_payload(capsys, [])
    assert payload["summary"] == {"mark": "ok", "usage_mark": "ok"}
    assert rc == 0
    assert _entry(payload)["verdict"]["mark"] == "degraded"


def test_human_readable_output_still_omits_the_manual_only_pool(capsys, monkeypatch):
    _stub_registry(monkeypatch)
    assert cli.main(["--no-cache", "--no-color"]) == 0
    assert "anthropic-api" not in capsys.readouterr().out


def _gate_printable_pools() -> set[str]:
    """Every ``pool=`` a placed profile resolves to (grade table plus the E6 arm rungs)."""

    placed = {profile.name for rows in recommend.GRADE_TABLE.values() for profile in rows}
    placed |= {profile.name for profile in recommend.E6_ARM_RUNGS}
    return {recommend.profile_pool(name)[0] for name in placed} - {""}


def test_json_provider_ids_equal_the_pools_gate_can_print(capsys, monkeypatch):
    """Mutant: drop the manual-only append from the json path -> anthropic-api leaves the set -> RED."""

    real_names = set(providers.registry())
    stubs = {name: (lambda name=name: ProviderResult(id=name)) for name in real_names}
    monkeypatch.setattr(cli, "registry", lambda: stubs)
    rc, payload = _json_payload(capsys, [])

    assert rc == 0
    json_ids = {item["id"] for item in payload["providers"]}
    gate_pools = _gate_printable_pools()
    assert "anthropic-api" in json_ids
    assert json_ids - gate_pools == set()
    assert gate_pools - json_ids == UNLISTED_GATE_POOLS
    assert "omniroute" not in real_names
