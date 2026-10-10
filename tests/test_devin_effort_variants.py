"""#635: catalog rows for the Devin effort variants.

devin keys effort into the model id (``--model swe-2-max``), so each rung is its
own profile. The variants start unmeasured: the high rung's grade is a
reference, never an inherited placement — #594 E6 settles them. #787 moved
``devin-swe2-medium`` to A on the operator-approved reps measurement
(hk:doc 5177 item 2, 2026-09-27); #1296 promoted ``devin-swe2-max``'s max rung
to A+ (hk:task/1296, 2026-10-08) while its profile-default row stays
unmeasured C; devin-ds41-max stays unmeasured C.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from scopefuel import cli, launch
from scopefuel.providers import devin
from scopefuel.recommend import (
    DEVIN_EFFORT_VARIANT_ANNOTATION,
    DEVIN_SWE2_MAX_GRADE_ANNOTATION,
    DEVIN_SWE2_MEDIUM_GRADE_ANNOTATION,
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

# #787: placement + annotation per variant. devin-swe2-medium carries the
# reps-measured A row (hk:doc 5177 item 2); the others keep the #635
# unmeasured-variant row at C — except devin-swe2-max, whose #1296-promoted
# max rung is an ordinary A+ row alongside the C default row.
PLACEMENT: dict[str, tuple[str, str]] = {
    "devin-swe2-medium": ("A", DEVIN_SWE2_MEDIUM_GRADE_ANNOTATION),
    "devin-swe2-max": ("C", DEVIN_EFFORT_VARIANT_ANNOTATION),
    "devin-ds41-max": ("C", DEVIN_EFFORT_VARIANT_ANNOTATION),
}


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_variant_row_is_unmeasured_with_high_reference(name):
    grade, annotation = PLACEMENT[name]
    placements = _placements(name)
    if name == "devin-swe2-max":
        # #1296: the promoted max rung is an A+ placement; the effort-less
        # default row keeps the unmeasured-variant row at C.
        assert placements == ["A+", "C"]
    else:
        assert placements == [grade]
    (profile,) = [p for p in GRADE_TABLE[grade] if p.name == name]
    assert profile.benchmark is None
    assert profile.benchmark_source is None
    assert profile.launcher_effort is None  # devin takes no effort flag
    assert profile.gate == "default"
    assert profile.benchmark_annotation == annotation
    assert profile_pool(name) == ("devin", None)


def test_swe2_max_rung_row_is_the_promoted_placement():
    """#1296: devin-swe2-max@max is an ordinary A+ row with the 1296 provenance."""

    (rung,) = [p for p in GRADE_TABLE["A+"] if p.name == "devin-swe2-max"]
    assert rung.launcher_effort == "max"
    assert rung.benchmark is None
    assert rung.benchmark_source is None
    assert rung.gate == "default"
    assert rung.benchmark_annotation == DEVIN_SWE2_MAX_GRADE_ANNOTATION
    assert "hk:task/1296" in DEVIN_SWE2_MAX_GRADE_ANNOTATION
    override = launch.ARM_GRADE_OVERRIDES[("devin-swe2-max", "max")]
    assert override.grade == "A+"


def test_variant_annotation_is_unmeasured_and_cites_high_only_as_reference():
    assert DEVIN_EFFORT_VARIANT_ANNOTATION.startswith("미측정")
    assert "high A+ 참조" in DEVIN_EFFORT_VARIANT_ANNOTATION
    assert "#594 E6" in DEVIN_EFFORT_VARIANT_ANNOTATION


# #781+#787+#1296: decided rungs carry their approved grade; effort-less
# default rows keep the unmeasured-variant C row.
CATALOG_GRADE: dict[str, str] = {
    "devin-swe2-medium": "A",
    "devin-swe2-max": "C",
    "devin-ds41-max": "C",
}


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_variant_snapshot_and_launch_carry_the_devin_model_id(name):
    rows = [entry for entry in launch.snapshot_entries() if entry.profile == name]
    if name == "devin-swe2-max":
        # #1296: two rows — the promoted A+ max rung and the C default row.
        assert {(row.effort, row.grade) for row in rows} == {("max", "A+"), ("", "C")}
        (row,) = [row for row in rows if row.effort == ""]
    else:
        assert len(rows) == 1
        (row,) = rows
    assert row.model_id == VARIANTS[name]
    assert row.pool == "devin"
    assert row.grade == CATALOG_GRADE[name]
    assert row.score is None
    assert row.benchmark_annotation == PLACEMENT[name][1]

    decision = launch.resolve_launch(name)
    assert decision.model_id == VARIANTS[name]
    assert decision.pool == "devin"
    # #1296: devin-swe2-max's catalog default followed the canon onto the A+
    # max rung; the other variants still launch their "" row.
    assert decision.effort == ("max" if name == "devin-swe2-max" else "")
    assert decision.grade == ("A+" if name == "devin-swe2-max" else CATALOG_GRADE[name])


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
    # Golden originally captured at af2233f (main, before #635) with this same
    # helper; re-captured under #1381's quota semantics — devin requires the
    # weekly (7d) window only and swe-2 is model-scoped, so models-list-only
    # input gates fail-closed with the 7d window missing.
    expected = json.loads(GATE_GOLDEN.read_text())
    actual = gate_outputs(monkeypatch, capsys, tmp_path, fixture_text)
    assert json.dumps(actual, ensure_ascii=False, sort_keys=True) == json.dumps(
        expected, ensure_ascii=False, sort_keys=True
    )


# -- AC3: recommend lists each variant only at its measured placement --------


def _measured_devin(fixture_text):
    """#1381: models list 는 쿼타를 안 주므로 측정된 devin 풀은 PTY 세션 파서로 만든다."""
    return devin.parse_session(fixture_text("devin_usage"))


def test_recommend_outside_the_measured_placements_never_lists_variants(fixture_text):
    providers = [_measured_devin(fixture_text)]
    # Measured placements are A (medium), A+ (swe2-max's promoted max rung) and
    # C (the effort-less max rungs) — nowhere else. #1318-2: the A rep-measured
    # row (medium) also lists at B, tagged one-up; the unmeasured C rungs do not
    # move (their variant annotation is not rep evidence).
    for grade in ("S+", "S"):
        out = recommend(providers, grade, explain=True)
        for name in VARIANTS:
            assert name not in out, (grade, name, out)
    b_out = recommend(providers, "B", explain=True)
    medium_b = [line for line in b_out.splitlines() if "devin-swe2-medium" in line]
    assert medium_b and all("[one-up A]" in line for line in medium_b)
    for name in ("devin-swe2-max", "devin-ds41-max"):
        assert name not in b_out, (name, b_out)
    aplus = recommend(providers, "A+", explain=True)
    assert "devin-swe2-medium" not in aplus
    assert "devin-ds41-max" not in aplus
    # #1331: the rung prints as a non-command annotation — devin takes no
    # --effort flag, so the accepted spelling is the bare profile name.
    assert any(line[:1].isdigit() and "devin-swe2-max (effort max)" in line for line in aplus.splitlines())


def test_recommend_c_lists_only_the_still_unmeasured_variants(fixture_text):
    providers = [_measured_devin(fixture_text)]
    out = recommend(providers, "C")
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "devin-swe2-medium" not in out
    for name in ("devin-swe2-max", "devin-ds41-max"):
        rows = [line for line in ranked if name in line.split()]
        assert len(rows) == 1, (name, out)
        assert "미측정" in rows[0], rows[0]


def test_recommend_a_lists_only_the_measured_variant(fixture_text):
    """#787: devin-swe2-medium is the A candidate; the C variants stay out.

    #1318-2: the A+ rep-measured rungs (devin-swe2@high, devin-swe2-max@max,
    devin-ds41) now list at A as one-up rows — tagged, never untagged.
    """
    providers = [_measured_devin(fixture_text)]
    out = recommend(providers, "A")
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    rows = [line for line in ranked if "devin-swe2-medium" in line.split()]
    assert len(rows) == 1, out
    assert "[one-up" not in rows[0]
    # The promoted A+ max rung lists here only as a tagged one-up row; the
    # unmeasured C max rungs stay at C.
    swe2_max_a = [line for line in ranked if "devin-swe2-max" in line]
    assert swe2_max_a and all("[one-up A+]" in line for line in swe2_max_a), out
    assert "devin-ds41-max" not in out, out
