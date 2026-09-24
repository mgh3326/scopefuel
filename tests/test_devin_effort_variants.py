"""#635: catalog rows for the Devin effort variants.

devin keys effort into the model id (``--model swe-2-max``), so each rung is its
own profile. The variants are unmeasured: the high rung's grade is a reference,
never an inherited placement — #594 E6 settles them.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from scopefuel import cli, launch
from scopefuel.providers import devin
from scopefuel.recommend import (
    DEVIN_EFFORT_VARIANT_ANNOTATION,
    GRADE_TABLE,
    profile_pool,
    recommend,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# profile -> devin model id (the launcher's --model argument)
VARIANTS: dict[str, str] = {
    "devin-swe2-medium": "swe-2-medium",
    "devin-swe2-max": "swe-2-max",
    "devin-ds41-max": "deepseek-v4-1-flash-max",
}
EXISTING_DEVIN = ("devin-swe2", "devin-ds41")
GATE_GOLDEN = FIXTURES / "devin_gate_golden_635.json"


def _models_list(fixture_text) -> str:
    return fixture_text("devin_models_list")


def _placements(name: str) -> list[str]:
    return [grade for grade, profiles in GRADE_TABLE.items() if any(p.name == name for p in profiles)]


# -- AC1: rows ----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_variant_row_is_unmeasured_with_high_reference(name):
    assert _placements(name) == ["C"]
    (profile,) = [p for p in GRADE_TABLE["C"] if p.name == name]
    assert profile.benchmark is None
    assert profile.benchmark_source is None
    assert profile.launcher_effort is None  # devin takes no effort flag
    assert profile.gate == "default"
    assert profile.benchmark_annotation == DEVIN_EFFORT_VARIANT_ANNOTATION
    assert profile_pool(name) == ("devin", None)


def test_variant_annotation_is_unmeasured_and_cites_high_only_as_reference():
    assert DEVIN_EFFORT_VARIANT_ANNOTATION.startswith("미측정")
    assert "high A+ 참조" in DEVIN_EFFORT_VARIANT_ANNOTATION
    assert "#594 E6" in DEVIN_EFFORT_VARIANT_ANNOTATION


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_variant_snapshot_and_launch_carry_the_devin_model_id(name):
    rows = [entry for entry in launch.snapshot_entries() if entry.profile == name]
    assert len(rows) == 1
    (row,) = rows
    assert row.model_id == VARIANTS[name]
    assert row.pool == "devin"
    assert row.grade == "C"
    assert row.score is None
    assert row.benchmark_annotation == DEVIN_EFFORT_VARIANT_ANNOTATION

    decision = launch.resolve_launch(name)
    assert decision.model_id == VARIANTS[name]
    assert decision.pool == "devin"
    assert decision.effort == ""


def test_swe2_variant_model_ids_are_real_devin_model_uids(fixture_text):
    listed = {line.split()[0] for line in _models_list(fixture_text).splitlines() if line.startswith("  ")}
    for name in ("devin-swe2-medium", "devin-swe2-max"):
        assert VARIANTS[name] in listed


def test_existing_devin_model_ids_unchanged():
    ids = {entry.profile: entry.model_id for entry in launch.snapshot_entries()}
    assert ids["devin-swe2"] == "swe-2"
    assert ids["devin-ds41"] == "deepseek-v4-1-flash-high"


# -- AC2: existing profiles' gate output is byte-identical -------------------


def _gate_capture(monkeypatch, capsys, tmp_path, result, profile: str) -> dict:
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: result})
    record_path = tmp_path / f"{profile}.json"
    rc = cli.main(["gate", "-m", profile, "--no-cache", "--gate-output", str(record_path)])
    out = capsys.readouterr()
    record = json.loads(record_path.read_text())
    record.pop("generated_at")  # wall clock, the only non-deterministic field
    return {"rc": rc, "stdout": out.out, "stderr": out.err, "record": record}


def _without_swe2_free(text: str) -> str:
    return text.replace("[262K context, Free]", "[262K context, $1 / 1M Input]")


def gate_outputs(monkeypatch, capsys, tmp_path, fixture_text) -> dict:
    text = _models_list(fixture_text)
    cases = {"free": devin.parse(text), "swe2_not_free": devin.parse(_without_swe2_free(text))}
    return {
        f"{profile}/{case}": _gate_capture(monkeypatch, capsys, tmp_path, result, profile)
        for profile in EXISTING_DEVIN
        for case, result in cases.items()
    }


def test_existing_devin_gate_output_matches_pre_635_golden(monkeypatch, capsys, tmp_path, fixture_text):
    # Golden captured at af2233f (main, before #635) with this same helper.
    expected = json.loads(GATE_GOLDEN.read_text())
    actual = gate_outputs(monkeypatch, capsys, tmp_path, fixture_text)
    assert json.dumps(actual, ensure_ascii=False, sort_keys=True) == json.dumps(
        expected, ensure_ascii=False, sort_keys=True
    )


# -- AC3: never a confirmed A+ candidate -------------------------------------


def test_recommend_above_c_never_lists_variants(fixture_text):
    providers = [devin.parse(_models_list(fixture_text))]
    for grade in ("S+", "S", "A+", "A", "B"):
        out = recommend(providers, grade, explain=True)
        for name in VARIANTS:
            assert name not in out, (grade, name, out)


def test_recommend_c_lists_variants_only_as_unmeasured(fixture_text):
    providers = [devin.parse(_models_list(fixture_text))]
    out = recommend(providers, "C")
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    for name in VARIANTS:
        rows = [line for line in ranked if line.split()[1] == name]
        assert len(rows) == 1, (name, out)
        assert "미측정" in rows[0], rows[0]
