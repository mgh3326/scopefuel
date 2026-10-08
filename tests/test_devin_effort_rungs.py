"""#1284 — devin-swe2-max@max and devin-swe2@high rung rows.

builder-devin-max / builder-devin reps that recorded an effort (max / high)
resolve to those exact rungs; under rule v1.2 (effort exactness) a rung with
no catalog row leaves the rep unrung. The two rows are E6 measurement rows —
grade C, unmeasured, marker-gated, never placements — so they let the reps
count without moving any grade: the grade moves only through ``grades
propose`` / ``apply``.

The reps here mirror the shapes recorded in the fleet store (profile spelling,
model id spelling, effort, role, task grade). Each one is a distinct task so
the per-rung task aggregation never hides a row.
"""

from __future__ import annotations

import datetime as dt

import pytest

from scopefuel import bench, grades, launch
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import E6_ARM_GRADE, E6_ARM_KEYS, GRADE_TABLE, gate_check, recommend

HOST = "test-host"
TODAY = dt.date(2026, 10, 8)
NOW = dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=dt.UTC)

MAX_RUNG = ("devin-swe2-max", "max")
HIGH_RUNG = ("devin-swe2", "high")

COUNTED = "counted"
MISMATCH = "model-mismatch"

# (task, profile, model_id, effort, role, grade) -> (status, judging row key)
GROUPS: dict[tuple[str, str, str, str, str, str], tuple[str, tuple[str, str]]] = {
    # builder-devin-max: effort-less reps land on the profile's default row
    # (effort inferred); max-recorded reps land on the new max row.
    ("g1", "builder-devin-max", "swe-2-max", "", "impl", "A+"): (COUNTED, ("devin-swe2-max", "")),
    ("g2", "builder-devin-max", "swe-2-max", "", "orch", "A+"): (COUNTED, ("devin-swe2-max", "")),
    ("g3", "builder-devin-max", "swe-2-max", "max", "impl", "A+"): (COUNTED, MAX_RUNG),
    ("g4", "builder-devin-max", "swe-2-max", "max", "orch", "A+"): (COUNTED, MAX_RUNG),
    ("g5", "builder-devin-max", "devin-swe2", "", "impl", "A+"): (MISMATCH, ("devin-swe2-max", "")),
    ("g6", "builder-devin-max", "devin-swe2", "max", "impl", "A+"): (MISMATCH, MAX_RUNG),
    ("g7", "builder-devin-max", "swe-2", "max", "impl", "A"): (MISMATCH, MAX_RUNG),
    # builder-devin
    ("g8", "builder-devin", "swe-2", "", "impl", "A"): (COUNTED, ("devin-swe2", "")),
    ("g9", "builder-devin", "swe-2", "", "orch", "A"): (COUNTED, ("devin-swe2", "")),
    ("g10", "builder-devin", "swe-2", "high", "impl", "A"): (COUNTED, HIGH_RUNG),
    ("g11", "builder-devin", "swe-2", "high", "orch", "A"): (COUNTED, HIGH_RUNG),
    ("g12", "builder-devin", "devin-swe2", "", "orch", "A"): (COUNTED, ("devin-swe2", "")),
    ("g13", "builder-devin", "devin-swe2", "high", "impl", "A"): (COUNTED, HIGH_RUNG),
    ("g14", "builder-devin", "devin-swe2", "high", "orch", "A"): (COUNTED, HIGH_RUNG),
    # worker spellings — the profile name itself (direct, default effort)
    ("g15", "devin-swe2-max", "swe-2-max", "", "impl", "A+"): (COUNTED, ("devin-swe2-max", "")),
    ("g16", "devin-swe2-max", "swe-2-max", "", "verify", "B"): (COUNTED, ("devin-swe2-max", "")),
    ("g17", "devin-swe2", "swe-2", "", "impl", "A"): (COUNTED, ("devin-swe2", "")),
}


def _add(task: str, profile: str, model_id: str, effort: str, role: str, grade: str) -> None:
    bench.add_rep(
        profile=profile,
        model_id=model_id,
        task_ref=f"T1284-{task}",
        tier="T2",
        role=role,
        effort=effort or None,
        grade=grade or None,
        rounds=1,
        blockers_found=0,
        completed=1,
        recorded_at="2026-10-08T10:00:00Z",
    )


def _propose() -> grades.Proposal:
    view = bench.read_catalog()
    evidence = grades.gather_reps(view=view, host=HOST)
    return grades.evaluate(evidence, view)


def _row(proposal: grades.Proposal, task: str) -> grades.EvidenceRep:
    return next(r for r in proposal.evidence.rows if r.rep.task_ref == f"T1284-{task}")


def _result(proposal: grades.Proposal, key: tuple[str, str]) -> grades.RungResult:
    return next(r for r in proposal.results if r.key == key)


def _provider(provider_id: str) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class="spend",
        buckets=[
            Bucket(
                label="7d",
                window="7d",
                used_pct=10.0,
                resets_at=(dt.datetime.now(dt.UTC) + dt.timedelta(hours=100)).isoformat(),
                scope=Scope("account"),
                horizon="week",  # type: ignore[arg-type]
            )
        ],
    )


# --- the rows ----------------------------------------------------------------


@pytest.mark.parametrize(("key", "model_id"), [(MAX_RUNG, "swe-2-max"), (HIGH_RUNG, "swe-2")])
def test_each_rung_is_an_unmeasured_e6_row_with_the_launch_model_id(key, model_id):
    assert key in E6_ARM_KEYS
    rows = [entry for entry in bench.catalog_snapshot() if entry.key == key]
    assert len(rows) == 1, rows
    (row,) = rows
    assert row.grade == E6_ARM_GRADE == "C"
    assert row.score is None
    assert row.pool == "devin"
    assert row.gate == "default"
    # the id wrk hands the devin CLI for this profile — what reps record
    assert row.model_id == launch.LAUNCH_MODEL_IDS[key[0]] == model_id


def test_the_rows_are_not_placements():
    """No profile moves grade bucket: the rows stay out of the placement canon."""

    placement_keys = {entry.key for entry in launch.snapshot_entries()}
    assert MAX_RUNG not in placement_keys
    assert HIGH_RUNG not in placement_keys
    placed = {(p.name, p.launcher_effort or "") for profiles in GRADE_TABLE.values() for p in profiles}
    assert MAX_RUNG not in placed
    assert HIGH_RUNG not in placed
    rows = {entry.key: entry for entry in launch.snapshot_entries()}
    assert rows[("devin-swe2", "")].grade == "A+"
    assert rows[("devin-swe2-max", "")].grade == "C"
    devin_buckets = {
        grade: sorted(p.name for p in profiles if p.name in ("devin-swe2", "devin-swe2-max"))
        for grade, profiles in GRADE_TABLE.items()
    }
    assert devin_buckets == {
        "S+": [],
        "S": [],
        "A+": ["devin-swe2"],
        "A": ["devin-swe2"],
        "B": ["devin-swe2"],
        "C": ["devin-swe2-max"],
    }


def test_recommend_never_lists_the_new_rungs():
    providers = [_provider("devin"), _provider("claude"), _provider("codex")]
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        out = recommend(providers, grade, today=TODAY, now=NOW)
        for line in out.splitlines():
            if line[:1].isdigit() and line.split()[1].startswith("devin-swe2"):
                assert "--effort" not in line, (grade, line)


# --- AC1: every recorded rep shape, counted or excluded with its reason -------


def test_every_recorded_shape_resolves_as_documented(isolated_cache):
    for shape in GROUPS:
        _add(*shape)
    proposal = _propose()
    for shape, (status, row_key) in GROUPS.items():
        row = _row(proposal, shape[0])
        assert row.row_key == row_key, (shape, row.resolution_detail)
        if status == COUNTED:
            assert not row.excluded, (shape, row.excluded)
            assert row.kind == "pass"
        else:
            assert "model-mismatch" in row.exclusion_tags, (shape, row.excluded)
    assert proposal.unrung == []


# --- AC5 (a): a max-recorded builder-devin-max A+ PASS counts on the max row ---


def test_a_max_recorded_builder_devin_max_pass_counts_on_the_max_rung(isolated_cache):
    _add("a1", "builder-devin-max", "swe-2-max", "max", "impl", "A+")
    proposal = _propose()
    row = _row(proposal, "a1")
    assert row.row_key == MAX_RUNG, row.resolution_detail
    assert row.excluded == ""
    assert row.resolution == "builder map"
    assert row.effort_inferred is False
    result = _result(proposal, MAX_RUNG)
    assert [item.ref for item in result.counted] == [row.ref]
    assert result.passes_at.get("A+") == [row.ref]


def test_a_high_recorded_builder_devin_pass_counts_on_the_high_rung(isolated_cache):
    _add("a2", "builder-devin", "swe-2", "high", "impl", "A")
    proposal = _propose()
    row = _row(proposal, "a2")
    assert row.row_key == HIGH_RUNG, row.resolution_detail
    assert row.excluded == ""
    assert _result(proposal, HIGH_RUNG).passes_at.get("A") == [row.ref]


# --- AC5 (b): an effort-less rep keeps the documented default-effort path ------


@pytest.mark.parametrize(
    ("profile", "model_id", "default_row", "new_row"),
    [
        ("builder-devin-max", "swe-2-max", ("devin-swe2-max", ""), MAX_RUNG),
        ("builder-devin", "swe-2", ("devin-swe2", ""), HIGH_RUNG),
    ],
)
def test_an_effortless_rep_counts_on_the_default_row_never_the_new_rung(
    isolated_cache, profile, model_id, default_row, new_row
):
    """v1.2: an inferred effort lands on the profile's default rung (effort "").

    The new rungs are unmeasured E6 rows, which ``_catalog_default_effort``
    skips — an effort-less rep is never re-read as max/high evidence.
    """

    _add("b1", profile, model_id, "", "impl", "A+")
    proposal = _propose()
    row = _row(proposal, "b1")
    assert row.row_key == default_row, row.resolution_detail
    assert row.effort_inferred is True
    assert row.excluded == ""
    assert all(r.key != new_row for r in proposal.results)


# --- AC5 (c): a non-matching model id stays excluded (no widening) ------------


@pytest.mark.parametrize(
    ("profile", "model_id", "effort", "key"),
    [
        ("builder-devin-max", "devin-swe2", "max", MAX_RUNG),
        ("builder-devin-max", "swe-2", "max", MAX_RUNG),
        ("builder-devin", "swe-2-max", "high", HIGH_RUNG),
    ],
)
def test_a_rep_with_another_model_stays_excluded_on_the_new_rung(
    isolated_cache, profile, model_id, effort, key
):
    _add("c1", profile, model_id, effort, "impl", "A+")
    proposal = _propose()
    row = _row(proposal, "c1")
    assert row.row_key == key, row.resolution_detail
    assert "model-mismatch" in row.exclusion_tags
    assert row.excluded.startswith("model mismatch")
    assert _result(proposal, key).counted == []


def test_the_devin_model_equivalence_is_unchanged():
    assert grades.MODEL_EQUIVALENCE["swe-2-max"] == frozenset({"devin-swe2-max"})
    assert grades.MODEL_EQUIVALENCE["swe-2"] == frozenset({"devin-swe2"})


# --- AC3: launch -------------------------------------------------------------


@pytest.mark.parametrize("profile", ["devin-swe2-max", "devin-swe2"])
def test_default_launches_are_unchanged(profile):
    decision = launch.resolve_launch(profile)
    assert decision.model_id == launch.LAUNCH_MODEL_IDS[profile]
    assert decision.effort == ""
    assert decision.grade == {"devin-swe2-max": "C", "devin-swe2": "A+"}[profile]
    assert decision.e6_arm is None


@pytest.mark.parametrize(("key", "default_grade"), [(MAX_RUNG, "C"), (HIGH_RUNG, "A+")])
def test_an_unmarked_effort_launch_keeps_the_default_placement(key, default_grade):
    profile, effort = key
    decision = launch.resolve_launch(profile, effort=effort)
    assert decision.model_id == launch.LAUNCH_MODEL_IDS[profile]
    assert decision.effort == effort
    assert decision.grade == default_grade
    assert decision.e6_arm is None


@pytest.mark.parametrize("key", [MAX_RUNG, HIGH_RUNG])
def test_a_marked_effort_launch_resolves_the_rung(key):
    profile, effort = key
    decision = launch.resolve_launch(profile, effort=effort, e6_arm=f"{profile}@{effort}")
    assert decision.model_id == launch.LAUNCH_MODEL_IDS[profile]
    assert decision.effort == effort
    assert decision.grade == "C"
    assert decision.e6_arm == f"{profile}@{effort}"


@pytest.mark.parametrize(("profile", "grade"), [("devin-swe2-max", "C"), ("devin-swe2", "A+")])
def test_the_bare_gate_is_unchanged(profile, grade):
    """wrk passes no --effort to the gate — the bare spawn judges the default row."""

    result = gate_check([_provider("devin")], profile, today=TODAY, now=NOW)
    assert result.ok is True, result.reason
    assert result.grade == grade
    assert result.e6_arm is None
