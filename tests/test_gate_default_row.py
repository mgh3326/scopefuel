"""Bare gate placement is independent of catalog effort ordering (#724)."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from scopefuel import bench, cli
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import GRADE_TABLE, Profile, _find_profile, gate_check


def _healthy_provider(provider_id: str) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class="spend",
        buckets=[
            Bucket(
                label="7d",
                window="7d",
                used_pct=10.0,
                resets_at="2026-10-02T12:00:00+00:00",
                scope=Scope("account"),
                horizon="week",
            )
        ],
    )


def _canon_table():
    view = bench.CatalogView(entries=bench.catalog_snapshot(), source="server", backend="handoffkeep")
    table = bench._catalog_grade_table(view)
    assert table is not None
    return table


@pytest.mark.parametrize("spelling", ["codex-max", "codex-sol"])
def test_bare_codex_gate_uses_canon_default_row(monkeypatch, capsys, tmp_path, spelling):
    table = _canon_table()
    codex_rows = [p for p in table["S+"] if p.name == "codex-sol"]
    assert [(p.launcher_effort, p.gate) for p in codex_rows] == [
        ("xhigh", "escalation"),
        ("max", "default"),
    ]
    monkeypatch.setattr(cli.bench, "runtime_grade_table", lambda: table)
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {pid: (lambda pid=pid: _healthy_provider(pid)) for pid in ("codex", "claude")},
    )
    output = tmp_path / "gate.json"
    rc = cli.main(["gate", "-m", spelling, "--no-cache", "--gate-output", str(output)])
    captured = capsys.readouterr()
    record = json.loads(output.read_text())
    assert rc == 0, captured.err
    assert record["ok"] is True
    assert record["grade"] == "S+"
    assert "escalation 후보" not in record["reason"]


@pytest.mark.parametrize("spelling", ["codex-max", "codex-sol"])
def test_explicit_canon_escalation_rung_keeps_716_admission(spelling):
    result = gate_check(
        [_healthy_provider("codex"), _healthy_provider("claude")],
        spelling,
        effort="xhigh",
        grade_table=_canon_table(),
        today=dt.date(2026, 9, 25),
    )
    assert result.ok is True
    assert result.grade == "S+"
    assert "codex-sol@xhigh" in result.reason


def test_escalation_only_profile_still_uses_escalation_gate():
    profile = Profile("codex-sol", "Test", 60, gate="escalation", gate_reason="test")
    lower_grade = Profile("codex-sol", "Lower grade", 55, gate="escalation")
    table = {
        "S+": [profile, Profile("opus", "Ordinary", 60)],
        "S": [lower_grade],
    }
    assert _find_profile("codex-sol", grade_table=table) == ("S+", profile)
    result = gate_check(
        [_healthy_provider("codex"), _healthy_provider("claude")],
        "codex-sol",
        grade_table=table,
        today=dt.date(2026, 9, 25),
    )
    assert result.ok is False
    assert "escalation 후보" in result.reason


def test_local_table_selection_keeps_first_grade_placement():
    for spelling in (
        "opus",
        "sonnet",
        "codex-sol",
        "codex-terra",
        "codex-luna",
        "grok",
        "devin-swe2",
        "haiku",
    ):
        original = next(
            (grade, profile)
            for grade, profiles in GRADE_TABLE.items()
            for profile in profiles
            if profile.name == spelling
        )
        assert _find_profile(spelling, grade_table=GRADE_TABLE) == original


@pytest.mark.parametrize("family", ["claude", "codex", "grok", "devin", "kimi"])
def test_selection_skips_an_escalation_row_for_each_family(family):
    escalation = Profile(family, "Test", 60, launcher_effort="xhigh", gate="escalation")
    ordinary = Profile(family, "Test", 60, launcher_effort="max", gate="default")
    assert _find_profile(family, grade_table={"S+": [escalation, ordinary]}) == (
        "S+",
        ordinary,
    )
