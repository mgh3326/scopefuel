"""Upstage Solar Pro 4 — fixed spend-class pool and oc-solar4 profile placement."""

from __future__ import annotations

import pytest

from scopefuel import cli
from scopefuel.providers import BUILTIN, upstage
from scopefuel.recommend import (
    GRADE_TABLE,
    UPSTAGE_SOLAR4_PLACEMENT_NOTE,
    profile_pool,
)

NOTE = "Large Trial free until 2026-09-20, 무제한 사용, 소진/전환은 운영자 정리(#211)"


def test_registry_exposes_upstage_as_spend():
    assert BUILTIN["upstage"].pool_class == "spend"


def test_fetch_is_fixed_zero_spend():
    result = upstage.fetch()
    assert result.error is None
    assert result.id == "upstage"
    assert result.pool_class == "spend"
    assert result.note == NOTE
    assert len(result.buckets) == 1
    bucket = result.buckets[0]
    assert bucket.used_pct == 0.0
    assert bucket.scope.kind == "account"
    assert bucket.note == NOTE


def test_profile_pool_oc_solar4():
    assert profile_pool("oc-solar4") == ("upstage", None)


def test_oc_solar4_grade_exposure_and_placement_note():
    names = {grade: [p.name for p in profiles] for grade, profiles in GRADE_TABLE.items()}
    assert "oc-solar4" in names["B"]
    assert "oc-solar4" not in names["S+"]
    assert "oc-solar4" not in names["S"]
    assert "oc-solar4" not in names["A+"]
    assert "oc-solar4" not in names["A"]
    assert "oc-solar4" not in names["C"]

    profile = next(p for p in GRADE_TABLE["B"] if p.name == "oc-solar4")
    assert profile.placement_note == UPSTAGE_SOLAR4_PLACEMENT_NOTE
    assert "T1 한정" in profile.placement_note
    assert "tester 금지" in profile.placement_note


def test_gate_cli_ok_on_fixed_zero(monkeypatch, capsys):
    result = upstage.fetch()
    monkeypatch.setattr(cli, "registry", lambda: {"upstage": lambda: result})
    rc = cli.main(["gate", "-m", "oc-solar4", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    assert "pool=upstage" in out.out
    assert "used_pct=0.0" in out.out or "used_pct=0" in out.out
    assert "class=spend" in out.out


def test_list_and_argparse_include_oc_solar4(capsys):
    assert cli.main(["--list-recommend-profiles"]) == 0
    names = capsys.readouterr().out.splitlines()
    assert "oc-solar4" in names

    with pytest.raises(SystemExit) as exc:
        cli.main(["gate", "-m", "not-oc-solar4"])
    assert exc.value.code == 2
    parser = cli.build_parser(["upstage"])
    gate = parser._subparsers._group_actions[0].choices["gate"]
    profile_action = next(action for action in gate._actions if action.dest == "profile")
    assert "oc-solar4" in profile_action.choices
