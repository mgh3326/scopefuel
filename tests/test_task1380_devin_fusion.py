"""#1380 (director-1 10-10, operator decision 15:0x via desk): the paid Devin
fusion lane.

Two catalog rows join the devin pool — ``devin-fusion-opus55`` (launch id
``fusion-claude-opus-5-5-high-sidekick-swe-2-medium``, $4 / 1M input) and
``devin-fusion-sonnet55`` (``fusion-claude-sonnet-5-5-high-sidekick-swe-2-medium``,
$2 / 1M input) — superseding the 09-14 no-paid-Devin line. They serve Claude
models under the hood, so their tester-separation provider family is the same
``claude`` value the claude rows get (NOT cognition), while the quota pool
stays devin (the spend is Devin credits) and billing is paid (#1340's
free-before-paid rule sinks them below the Free-tag SWE-2 rows). No rep
evidence exists — hk 1382 runs the first reps — so both rows are unmeasured:
no 급 실측 annotation, no ArmGradeOverride, no promote-evidence stamp, and
``is_rep_measured`` stays False.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from scopefuel import bench, cli, launch
from scopefuel.providers import devin
from scopefuel.recommend import (
    DEVIN_FUSION_ANNOTATION,
    GRADE_TABLE,
    PROFILE_ALIASES,
    _profile_has_benchmark_score,
    is_rep_measured,
    profile_pool,
    provider_family,
    recommend,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

NOW = dt.datetime(2026, 10, 10, 12, 0, 0, tzinfo=dt.UTC)
TODAY = NOW.date()

FUSION_PROFILES = ("devin-fusion-opus55", "devin-fusion-sonnet55")
# profile -> devin model id (the launcher's --model argument), per the
# recorded ``devin models list`` (Fusion family, SWE-2 Medium sidekick).
FUSION_IDS = {
    "devin-fusion-opus55": "fusion-claude-opus-5-5-high-sidekick-swe-2-medium",
    "devin-fusion-sonnet55": "fusion-claude-sonnet-5-5-high-sidekick-swe-2-medium",
}
FUSION_INPUT_PRICE = {
    "devin-fusion-opus55": 4.0,
    "devin-fusion-sonnet55": 2.0,
}
FUSION_PLACEMENTS = {"devin-fusion-opus55": ["A", "B"], "devin-fusion-sonnet55": ["A", "B"]}


def _ranked(output: str) -> list[str]:
    return [line for line in output.splitlines() if line[:1].isdigit()]


def _devin_ranked_names(output: str) -> list[str]:
    """The devin-pool ranked rows in output order (ranked-line name token)."""
    return [line.split()[1] for line in _ranked(output) if profile_pool(line.split()[1])[0] == "devin"]


def _placements(name: str) -> list[str]:
    return [grade for grade, profiles in GRADE_TABLE.items() if any(p.name == name for p in profiles)]


# ── AC1: policy launch --json ────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(FUSION_PROFILES))
def test_policy_launch_json_reports_fusion_id_paid_family_devin_pool(capsys, name):
    """The launch verdict prints the fusion model id, billing paid, family
    claude and pool devin — catalog from the bundled template."""
    assert cli.main(["policy", "launch", name, "--json"]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    decision = json.loads(out.out)
    assert decision["profile"] == name
    assert decision["model_id"] == FUSION_IDS[name]
    assert decision["billing"] == "paid"
    assert decision["family"] == "claude"
    assert decision["pool"] == "devin"
    assert decision["gate"] == "default"
    assert decision["effort"] == ""


def test_policy_launch_text_surface_carries_the_same_fields(capsys):
    """The non-JSON render prints the same billing/family/pool lines."""
    assert cli.main(["policy", "launch", "devin-fusion-opus55"]) == 0
    out = capsys.readouterr().out
    assert "model_id fusion-claude-opus-5-5-high-sidekick-swe-2-medium" in out
    assert "pool devin" in out
    assert "family claude" in out
    assert "billing paid" in out


# ── rows: placements, spelling, billing, unmeasured ──────────────────────────


def test_fusion_rows_are_placed_at_a_and_b_nowhere_else():
    for name in FUSION_PROFILES:
        assert _placements(name) == FUSION_PLACEMENTS[name]


@pytest.mark.parametrize("name", sorted(FUSION_PROFILES))
def test_fusion_row_is_unmeasured_paid_default_gate(name):
    for grade in FUSION_PLACEMENTS[name]:
        rows = [p for p in GRADE_TABLE[grade] if p.name == name]
        assert len(rows) == 1
        (profile,) = rows
        assert profile.benchmark is None
        assert profile.benchmark_source is None
        assert profile.launcher_effort is None  # devin takes no effort flag
        assert profile.benchmark_effort is None
        assert profile.gate == "default"
        assert profile.billing == "paid"
        assert profile.benchmark_annotation == DEVIN_FUSION_ANNOTATION
        assert profile.benchmark_annotation.startswith("미측정")
        assert not is_rep_measured(profile)
        assert not _profile_has_benchmark_score(profile, [])
        assert profile_pool(name) == ("devin", None)


def test_fusion_annotation_claims_no_rep_evidence():
    """The annotation names the paid lane + Claude family + hk 1382 wait, and
    never carries the rep-measured prefix the one-up rule reads."""
    assert DEVIN_FUSION_ANNOTATION.startswith("미측정")
    assert "급 실측(" not in DEVIN_FUSION_ANNOTATION
    assert "1380" in DEVIN_FUSION_ANNOTATION


# ── AC3: family is the claude value, by equality ─────────────────────────────


@pytest.mark.parametrize("name", sorted(FUSION_PROFILES))
def test_fusion_family_equals_the_claude_family_value(name):
    """The fusion family is *the same value* the claude rows get — asserted by
    equality, not substring: a "cognition" mutant goes red here."""
    assert provider_family(name) == provider_family("opus")
    assert provider_family(name) == provider_family("sonnet")
    assert provider_family(name) == "claude"
    assert profile_pool(name)[0] == "devin"  # pool stays devin — family diverges


@pytest.mark.parametrize("name", sorted(FUSION_PROFILES))
def test_launch_decision_family_is_claude_while_pool_is_devin(name):
    decision = launch.resolve_launch(name)
    assert decision.family == "claude"
    assert decision.pool == "devin"
    assert decision.billing == "paid"


# ── launch spelling ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(FUSION_PROFILES))
def test_fusion_launch_spelling_is_the_bare_profile(name):
    """bin/wrk rejects --effort for devin spellings — the accepted fragment is
    the bare profile name, same shape as devin-ds41."""
    assert launch.launch_spelling(name, None) == name
    assert launch.launch_spelling(name, "high") == name  # rung stays non-command
    assert not launch.launcher_accepts_effort_flag(name)


def test_no_fusion_line_anywhere_carries_the_effort_flag(fixture_text):
    providers = [devin.parse(fixture_text("devin_models_list"))]
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        out = recommend(providers, grade, today=TODAY, now=NOW)
        for line in out.splitlines():
            if "devin-fusion" in line:
                assert "--effort" not in line, (grade, line)


# ── model ids: launch map, equivalence, snapshot, fixture ────────────────────


def test_launch_model_ids_carry_the_fusion_uids():
    for name, uid in FUSION_IDS.items():
        assert launch.LAUNCH_MODEL_IDS[name] == uid


def test_grades_model_equivalence_maps_fusion_ids_to_profiles():
    from scopefuel.grades import MODEL_EQUIVALENCE

    for name, uid in FUSION_IDS.items():
        assert MODEL_EQUIVALENCE[uid] == frozenset({name})


def test_fusion_snapshot_rows_collapse_to_the_best_placement():
    """Both A/B listings emit one (profile, "") catalog row at the best grade A
    with the fusion model id and the devin pool — same shape as devin-swe2."""
    for name, uid in FUSION_IDS.items():
        rows = [entry for entry in launch.snapshot_entries() if entry.profile == name]
        assert len(rows) == 1
        (row,) = rows
        assert row.effort == ""
        assert row.grade == "A"
        assert row.model_id == uid
        assert row.pool == "devin"
        assert row.score is None
        assert row.gate == "default"


def test_fusion_model_ids_are_real_devin_model_uids(fixture_text):
    """The launch ids resolve to priced rows in the recorded models list — the
    "Sidekick: Free" tag on each row is the swe-2-medium helper's, not the
    rung's, which is why billing=paid is honest."""
    listed = {}
    for line in fixture_text("devin_models_list").splitlines():
        if line.startswith("  ") and "[" in line:
            listed[line.split()[0]] = line
    for name, uid in FUSION_IDS.items():
        assert uid in listed
        assert f"${int(FUSION_INPUT_PRICE[name])} / 1M Input" in listed[uid]


def test_price_seeds_carry_the_fusion_input_prices(tmp_path):
    prices = bench.read_prices(path=tmp_path / "missing.db")
    for name, uid in FUSION_IDS.items():
        price = prices[uid]
        assert price.price_1m_input_tokens == FUSION_INPUT_PRICE[name]
        assert price.price_1m_output_tokens == FUSION_INPUT_PRICE[name] * 5


# ── AC2: --recommend B and A — unmeasured-last, paid after free ─────────────


def test_recommend_lists_fusion_rows_only_at_a_and_b(fixture_text):
    providers = [devin.parse(fixture_text("devin_models_list"))]
    for grade in ("S+", "S", "A+", "C"):
        out = recommend(providers, grade, explain=True)
        for name in FUSION_PROFILES:
            assert name not in out, (grade, name, out)


@pytest.mark.parametrize("grade", ["A", "B"])
def test_recommend_fusion_rows_follow_every_measured_and_free_swe2_row(fixture_text, grade):
    """At A and B the fusion rows rank after every measured row (unmeasured-
    last) and after the free SWE-2 rows (paid sinks below free at equal quota
    standing) — as exact rows, never [one-up] tagged."""
    providers = [devin.parse(fixture_text("devin_models_list"))]
    out = recommend(providers, grade)
    ranked = _ranked(out)
    for name in FUSION_PROFILES:
        lines = [line for line in ranked if line.split()[1] == name]
        assert len(lines) == 1, (grade, name, out)
        assert "[one-up" not in lines[0], (grade, name, lines[0])
        assert "미측정" in lines[0], (grade, name, lines[0])
    # Every free devin row precedes every paid fusion row in the pool order.
    devin_names = _devin_ranked_names(out)
    free_devin = [
        n
        for n in devin_names
        if n in {"devin-swe2", "devin-swe2-medium", "devin-swe2-max", "devin-glm52", "devin-swe17"}
    ]
    for free_name in free_devin:
        for name in FUSION_PROFILES:
            assert devin_names.index(free_name) < devin_names.index(name), (grade, free_name, name, out)


def test_recommend_b_devin_pool_order_is_free_then_paid(fixture_text):
    """The pinned devin-pool order at B: the free SWE-2 rows (including the
    tagged one-up swe2-medium) lead the two paid fusion rungs."""
    providers = [devin.parse(fixture_text("devin_models_list"))]
    out = recommend(providers, "B")
    devin_names = _devin_ranked_names(out)
    assert devin_names[-2:] == ["devin-fusion-opus55", "devin-fusion-sonnet55"], out
    assert devin_names[0] == "devin-swe2", out


def test_recommend_a_devin_pool_order_is_free_then_paid_fusion(fixture_text):
    """The pinned devin-pool order at A: free rows first, the two paid fusion
    rungs before the paid ds41 one-up (one-up rows take appended slots)."""
    providers = [devin.parse(fixture_text("devin_models_list"))]
    out = recommend(providers, "A")
    devin_names = _devin_ranked_names(out)
    assert devin_names[-3:] == ["devin-fusion-opus55", "devin-fusion-sonnet55", "devin-ds41"], out


@pytest.mark.parametrize("grade", ["A", "B"])
def test_fusion_rows_are_not_rep_measured_and_not_one_up_anywhere(fixture_text, grade):
    payload = recommend_dict_rows(fixture_text, grade)
    for name in FUSION_PROFILES:
        rows = [row for row in payload["rows"] if row["profile"] == name]
        assert len(rows) == 1
        row = rows[0]
        assert row["billing"] == "paid"
        assert row["one_up"] is False
        assert row["placed_grade"] == grade


def recommend_dict_rows(fixture_text, grade):
    from scopefuel.recommend import recommend_dict

    providers = [devin.parse(fixture_text("devin_models_list"))]
    return recommend_dict(providers, grade, today=TODAY, now=NOW)


# ── wrk contract (part B mirror) ────────────────────────────────────────────


def test_wrk_contract_exempt_carries_the_fusion_spellings():
    """agent-skills' bin/wrk keeps the fusion spellings fixed-argv like every
    devin row — the exempt table is what part B must match by name."""
    import test_wrk_contract_guard

    for name in FUSION_PROFILES:
        assert name in test_wrk_contract_guard.WRK_CATALOG_EXEMPT
        assert "fusion" in test_wrk_contract_guard.WRK_CATALOG_EXEMPT[name]


def test_no_builder_alias_exists_for_the_fusion_rows():
    """#1380: no builder-* aliases — builder use is decided after hk 1382."""
    from scopefuel.grades import _BUILDER_RUNGS

    for name in FUSION_PROFILES:
        assert not any(target[0] == name for target in _BUILDER_RUNGS.values())
    assert not any(name.startswith("builder-fusion") for name in _BUILDER_RUNGS)
    assert not any(PROFILE_ALIASES.get(name, "").startswith("builder") for name in FUSION_PROFILES)
