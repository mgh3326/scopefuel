"""#1284 — devin-swe2-max@max and devin-swe2@high rung rows.

builder-devin-max / builder-devin reps that recorded an effort (max / high)
resolve to those exact rungs; under rule v1.2 (effort exactness) a rung with
no catalog row leaves the rep unrung. The rows began as E6 measurement rows —
grade C, unmeasured, marker-gated, never placements — so they let the reps
count without moving any grade.

#1296 (10-08 operator decision, applied by desk): both rungs were promoted to
A+ and are now ordinary GRADE_TABLE placements stamped by
``launch.ARM_GRADE_OVERRIDES`` — the same graduation path #920 used for
sonnet@max. The E6 marker is inert for them, and a bare
``policy launch devin-swe2-max`` follows the canon onto the A+ max rung.

The reps here mirror the shapes recorded in the fleet store (profile spelling,
model id spelling, effort, role, task grade). Each one is a distinct task so
the per-rung task aggregation never hides a row.
"""

from __future__ import annotations

import datetime as dt

import pytest

from scopefuel import bench, grades, launch
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import E6_ARM_KEYS, GRADE_TABLE, gate_check, recommend

HOST = "test-host"
TODAY = dt.date(2026, 10, 8)
NOW = dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=dt.UTC)

MAX_RUNG = ("devin-swe2-max", "max")
HIGH_RUNG = ("devin-swe2", "high")

COUNTED = "counted"
MISMATCH = "model-mismatch"

# (task, profile, model_id, effort, role, grade) -> (status, judging row key)
GROUPS: dict[tuple[str, str, str, str, str, str], tuple[str, tuple[str, str]]] = {
    # builder-devin-max: effort-less reps land on the profile's catalog default
    # — since #1296 that is the A+ max rung, not the C default row; recorded
    # max reps keep landing on it directly.
    ("g1", "builder-devin-max", "swe-2-max", "", "impl", "A+"): (COUNTED, MAX_RUNG),
    ("g2", "builder-devin-max", "swe-2-max", "", "orch", "A+"): (COUNTED, MAX_RUNG),
    ("g3", "builder-devin-max", "swe-2-max", "max", "impl", "A+"): (COUNTED, MAX_RUNG),
    ("g4", "builder-devin-max", "swe-2-max", "max", "orch", "A+"): (COUNTED, MAX_RUNG),
    ("g5", "builder-devin-max", "devin-swe2", "", "impl", "A+"): (MISMATCH, MAX_RUNG),
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
    # worker spellings — the profile name itself (direct, catalog default)
    ("g15", "devin-swe2-max", "swe-2-max", "", "impl", "A+"): (COUNTED, MAX_RUNG),
    ("g16", "devin-swe2-max", "swe-2-max", "", "verify", "B"): (COUNTED, MAX_RUNG),
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
def test_each_rung_is_a_promoted_ordinary_row_with_the_launch_model_id(key, model_id):
    # #1296: graduated from E6 arm to an ordinary A+ placement.
    assert key not in E6_ARM_KEYS
    rows = [entry for entry in bench.catalog_snapshot() if entry.key == key]
    assert len(rows) == 1, rows
    (row,) = rows
    assert row.grade == "A+"
    assert row.score is None
    assert row.pool == "devin"
    assert row.gate == "default"
    assert row.decided_by == "operator:2026-10-08 via operator-desk"
    # the id wrk hands the devin CLI for this profile — what reps record
    assert row.model_id == launch.LAUNCH_MODEL_IDS[key[0]] == model_id


def test_the_rows_are_now_placements():
    """#1296: the promoted rungs joined the placement canon at A+."""

    placement_keys = {entry.key for entry in launch.snapshot_entries()}
    assert MAX_RUNG in placement_keys
    assert HIGH_RUNG in placement_keys
    placed = {(p.name, p.launcher_effort or "") for profiles in GRADE_TABLE.values() for p in profiles}
    assert MAX_RUNG in placed
    assert HIGH_RUNG in placed
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
        # #1296: devin-swe2 twice — the "" row and the promoted @high rung.
        "A+": ["devin-swe2", "devin-swe2", "devin-swe2-max"],
        "A": ["devin-swe2"],
        "B": ["devin-swe2"],
        "C": ["devin-swe2-max"],
    }


def test_recommend_lists_the_new_rungs_only_at_their_placement():
    providers = [_provider("devin"), _provider("claude"), _provider("codex")]
    for grade in ("S+", "S", "A", "B", "C"):
        out = recommend(providers, grade, today=TODAY, now=NOW)
        for line in out.splitlines():
            if line[:1].isdigit() and line.split()[1].startswith("devin-swe2"):
                assert "--effort" not in line, (grade, line)
    out = recommend(providers, "A+", today=TODAY, now=NOW)
    assert any(line[:1].isdigit() and "devin-swe2 --effort high" in line for line in out.splitlines())
    assert any(line[:1].isdigit() and "devin-swe2-max --effort max" in line for line in out.splitlines())


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
    ("profile", "model_id", "default_row"),
    [
        # #1296: the promoted max rung is devin-swe2-max's best-graded ordinary
        # row, so the catalog default IS the max rung now.
        ("builder-devin-max", "swe-2-max", MAX_RUNG),
        ("builder-devin", "swe-2", ("devin-swe2", "")),
    ],
)
def test_an_effortless_rep_counts_on_the_catalog_default_rung(isolated_cache, profile, model_id, default_row):
    """v1.2: an inferred effort lands on the profile's catalog default rung.

    The rungs are ordinary placements now, so ``_catalog_default_effort``
    follows the canon: devin-swe2-max's default is the A+ max rung;
    devin-swe2 keeps its effort-"" row (tied at A+, the default rung wins).
    """

    _add("b1", profile, model_id, "", "impl", "A+")
    proposal = _propose()
    row = _row(proposal, "b1")
    assert row.row_key == default_row, row.resolution_detail
    assert row.effort_inferred is True
    assert row.excluded == ""


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


@pytest.mark.parametrize(
    ("profile", "effort", "grade"),
    [
        # #1296: the bare devin-swe2-max launch follows the canon onto the A+
        # max rung (its best-graded ordinary row); devin-swe2 keeps its "" row.
        ("devin-swe2-max", "max", "A+"),
        ("devin-swe2", "", "A+"),
    ],
)
def test_default_launches_follow_the_promoted_canon(profile, effort, grade):
    decision = launch.resolve_launch(profile)
    assert decision.model_id == launch.LAUNCH_MODEL_IDS[profile]
    assert decision.effort == effort
    assert decision.grade == grade
    assert decision.e6_arm is None


@pytest.mark.parametrize("key", [MAX_RUNG, HIGH_RUNG])
def test_an_unmarked_effort_launch_resolves_the_promoted_rung(key):
    profile, effort = key
    decision = launch.resolve_launch(profile, effort=effort)
    assert decision.model_id == launch.LAUNCH_MODEL_IDS[profile]
    assert decision.effort == effort
    assert decision.grade == "A+"
    assert decision.e6_arm is None


@pytest.mark.parametrize("key", [MAX_RUNG, HIGH_RUNG])
def test_a_marker_on_a_graduated_rung_is_inert(key):
    """#1296: the marker once admitted the E6 arm; the rung is an ordinary
    placement now, so it resolves the same with or without the marker."""

    profile, effort = key
    decision = launch.resolve_launch(profile, effort=effort, e6_arm=f"{profile}@{effort}")
    assert decision.model_id == launch.LAUNCH_MODEL_IDS[profile]
    assert decision.effort == effort
    assert decision.grade == "A+"
    assert decision.e6_arm is None


@pytest.mark.parametrize(("profile", "grade"), [("devin-swe2-max", "A+"), ("devin-swe2", "A+")])
def test_the_bare_gate_judges_the_default_rung(profile, grade):
    """wrk passes no --effort to the gate — the bare spawn judges the default row."""

    result = gate_check([_provider("devin")], profile, today=TODAY, now=NOW)
    assert result.ok is True, result.reason
    assert result.grade == grade
    assert result.e6_arm is None
