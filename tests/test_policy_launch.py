"""#593: ``scopefuel policy launch`` — the launcher's single source of truth.

The interesting cases are not the happy path but the refusals: a consult-only
profile, a rung the catalog does not carry, and a gate that a *stale* catalog is
not allowed to relax. hk:doc 2558 — a server being down is not free dispatch.
"""

from __future__ import annotations

import json

import pytest
from test_bench_backend import FakeHandoffkeep, _set_backend
from test_bench_catalog import _row

from scopefuel import bench, cli, launch


@pytest.fixture
def stale_catalog(tmp_path, monkeypatch):
    """A host configured for the canon that cannot reach it any more."""

    _set_backend(tmp_path, monkeypatch)
    fake = FakeHandoffkeep()
    fake.catalog = [
        _row("opus", "high", "claude-opus-5-5", "claude", "S+"),
        _row("opus", "max", "claude-opus-5-5", "claude", "S+", gate="escalation"),
        _row("fable", "", "claude-fable-5-1", "claude", "S+", gate="consult_only"),
    ]
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    bench.reset_catalog_memo()
    bench.read_catalog()

    import datetime as dt
    import sqlite3

    stamp = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=200000)).isoformat()
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute("UPDATE bench_cache_meta SET fetched_at = ? WHERE scope = 'catalog'", (stamp,))
        conn.commit()
    finally:
        conn.close()
    fake.offline = True
    bench.reset_catalog_memo()
    return fake


# --- AC3: consult_only ------------------------------------------------------


def test_fable_needs_an_operator_request(capsys):
    assert cli.main(["policy", "launch", "fable"]) == 3
    assert "consult_only" in capsys.readouterr().err


def test_fable_with_an_operator_request_returns_the_5_1_model_id(capsys):
    assert cli.main(["policy", "launch", "fable", "--operator-request", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["model_id"] == "claude-fable-5-1"
    assert payload["gate"] == "consult_only"


def test_astra_is_consult_only_and_defaults_to_xhigh(capsys):
    """#527 overlap: codex-astra's default effort is xhigh, not max."""

    assert cli.main(["policy", "launch", "codex-astra", "--operator-request", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["model_id"] == "gpt-6-astra"
    assert payload["effort"] == "xhigh"


# --- the launcher contract --------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "effort", "model_id", "resolved_effort"),
    [
        ("opus", None, "claude-opus-5-5", "high"),
        ("codex-sol", None, "gpt-6-sol", "max"),
        ("codex-max", None, "gpt-6-sol", "max"),  # alias resolves to codex-sol
        ("codex-luna", None, "gpt-6-luna", "medium"),
        ("codex-luna", "high", "gpt-6-luna", "high"),
        ("codex-terra", None, "gpt-5.6-terra", "medium"),
        ("grok-hi", None, "grok-4.7", "high"),
        ("grok", None, "grok-4.7", "medium"),
        ("kiro-opus", None, "claude-opus-5", "xhigh"),
        ("kiro-sonnet", None, "claude-sonnet-5", "high"),
        ("kiro-cheap", None, "qwen3-coder-next", "medium"),
        ("haiku", None, "", "low"),
    ],
)
def test_snapshot_defaults_match_the_launcher_case_table(profile, effort, model_id, resolved_effort):
    """Every value bin/wrk hardcodes today must come back identical from the
    snapshot — otherwise switching wrk to the catalog silently re-points a
    profile at a different model or rung."""

    decision = launch.resolve_launch(profile, effort=effort)
    assert decision.model_id == model_id
    assert decision.effort == resolved_effort


def test_an_unknown_profile_is_refused_not_guessed(capsys):
    assert cli.main(["policy", "launch", "no-such-profile"]) == 3
    assert "not in the catalog" in capsys.readouterr().err


def test_a_rung_the_catalog_never_placed_inherits_the_default_placement():
    """``wrk -m codex`` runs Sol at effort high — a rung the grade table has never
    placed. The catalog enumerates rungs to say where each is placed, not to list
    which rungs the CLI accepts, so an unplaced rung is answered from the
    profile's default placement instead of blocking the spawn."""

    decision = launch.resolve_launch("codex-sol", effort="high")
    assert decision.model_id == "gpt-6-sol"
    assert decision.effort == "high"
    # It inherits the *default* rung's gate (max, default), never a higher
    # rung's escalation gate — falling back must not widen and must not narrow.
    assert decision.gate == "default"


def test_an_enumerated_gated_rung_keeps_its_gate():
    """The fallback above must not become a way around a gate the operator set:
    a rung the catalog *did* place wins exactly."""

    with pytest.raises(launch.LaunchError, match="consult_only|stale"):
        launch.resolve_launch("fable")
    assert launch.resolve_launch("opus", effort="max").gate == "escalation"
    assert launch.resolve_launch("codex-sol", effort="xhigh").gate == "escalation"


def test_a_default_row_only_profile_accepts_a_pinned_rung():
    """kiro-opus is keyed on one row; the rung is the launcher's, not a claim the
    catalog made, so pinning it is answered rather than refused."""

    assert launch.resolve_launch("kiro-opus", effort="max").model_id == "claude-opus-5"


# --- ④(c): a stale catalog must not widen anything --------------------------


def test_stale_refuses_an_escalation_gate_without_an_operator_request(stale_catalog, capsys):
    view = bench.read_catalog()
    assert view.stale is True
    with pytest.raises(launch.LaunchError, match="stale"):
        launch.resolve_launch("opus", effort="max")


def test_stale_still_allows_ordinary_default_gate_launches(stale_catalog):
    decision = launch.resolve_launch("opus", effort="high")
    assert decision.model_id == "claude-opus-5-5"
    assert decision.catalog_stale is True


def test_stale_escalation_is_allowed_with_an_explicit_operator_request(stale_catalog):
    decision = launch.resolve_launch("opus", effort="max", operator_request=True)
    assert decision.gate == "escalation"
    assert decision.catalog_stale is True


def test_a_local_only_host_is_not_treated_as_stale():
    """A host that never read the canon has none to be behind; refusing its
    escalation launches would break every un-migrated machine for no safety gain."""

    view = bench.read_catalog()
    assert view.local_only is True and view.stale is False
    assert launch.resolve_launch("opus", effort="max").gate == "escalation"


def test_stale_json_discloses_the_source(stale_catalog, capsys):
    assert cli.main(["policy", "launch", "opus", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["catalog"]["stale"] is True
    assert payload["catalog"]["source"] == "snapshot"
    assert "catalog=stale" in captured.err
