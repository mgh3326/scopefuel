"""hk 1788 — hk 1786 phase 0: measurement rows + the sonnet@high promote (b953).

Four new E6 measurement rungs (codex-sol@low, haiku@xhigh, haiku@max,
sonnet@max) join E6_ARM_RUNGS; sonnet@high promotes B -> A. The three rungs
that were bundled placements (sonnet@max S, haiku@xhigh/max C) de-graduate out
of GRADE_TABLE — the E6 row replaces the placement row, and its reference text
carries the old score (director-1 addendum r1).

The canon-monotonicity trap: handoffkeep checkBenchCatalogMonotonicity refuses
a higher effort below any lower one, with no E6 exemption. sonnet@xhigh is A+,
so sonnet@max's carrier is A+ — not the global C carrier the other rungs use.
Every "is this row an unmeasured arm" check compares against the row's OWN
carrier (e6_arm_carrier_grade), never the bare E6_ARM_GRADE constant.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib

import pytest

from scopefuel import bench, grades, launch
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    E6_ARM_ANNOTATION,
    E6_ARM_KEYS,
    E6_ARM_RUNGS,
    GRADE_TABLE,
    Profile,
    e6_arm_carrier_grade,
    e6_arm_tag,
    gate_check,
    recommend,
    validate_grade_table,
)

TODAY = dt.date(2026, 10, 21)
NOW = dt.datetime(2026, 10, 21, 12, 0, 0, tzinfo=dt.UTC)
C1_FILE = pathlib.Path(__file__).parent.parent / "docs" / "catalog" / "hk1788.json"

# The phase-0 rungs: (profile, effort) -> (carrier, catalog model id).
RUNGS: dict[tuple[str, str], tuple[str, str]] = {
    ("codex-sol", "low"): ("C", "gpt-6.1-sol"),
    ("haiku", "xhigh"): ("C", "claude-haiku-5-5"),
    ("haiku", "max"): ("C", "claude-haiku-5-5"),
    ("sonnet", "max"): ("A+", "claude-sonnet-5-5"),
}
# The rungs that used to be GRADE_TABLE placements — the E6 row replaces them.
DEGRADUATED: dict[tuple[str, str], float] = {
    ("sonnet", "max"): 70.6,
    ("haiku", "xhigh"): 31.5,
    ("haiku", "max"): 39.2,
}
POOL = {"codex-sol": "codex", "haiku": "claude", "sonnet": "claude"}


def _provider(provider_id: str) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class="spend",  # type: ignore[arg-type]
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


# --- AC1: the four rungs are E6 rows, marker-gated, never candidates ----------


def test_the_four_rungs_are_e6_arm_rungs():
    assert set(RUNGS) <= set(E6_ARM_KEYS)
    rows = {entry.key: entry for entry in launch.e6_arm_entries()}
    for key, (carrier, model_id) in RUNGS.items():
        assert e6_arm_carrier_grade(*key) == carrier
        entry = rows[key]
        assert entry.grade == carrier
        assert entry.model_id == model_id
        assert entry.score is None
        assert entry.gate == "default"
        assert entry.benchmark_annotation.startswith(E6_ARM_ANNOTATION)


def test_no_e6_key_is_also_a_grade_table_key():
    """Director addendum r1: the E6 row replaces the placement row — a rung
    must never sit in both tables."""
    placed = {(p.name, p.launcher_effort or "") for profiles in GRADE_TABLE.values() for p in profiles}
    assert not (set(E6_ARM_KEYS) & placed)
    for key in DEGRADUATED:
        assert key not in placed
    # snapshot_entries mirrors the placement canon — no dup keys in the view.
    keys = [entry.key for entry in bench.catalog_snapshot()]
    assert len(keys) == len(set(keys))


def test_the_old_scores_live_in_the_e6_reference():
    rungs = {(row.name, row.launcher_effort): row for row in E6_ARM_RUNGS}
    for key, score in DEGRADUATED.items():
        assert f"{key[1]} {score}" in rungs[key].benchmark_annotation


@pytest.mark.parametrize(("key", "expected"), sorted(RUNGS.items()))
def test_each_rung_opens_only_with_its_exact_marker(key, expected):
    carrier, model_id = expected
    profile, effort = key
    provider = _provider(POOL[profile])
    # No marker: refused, naming the rung and the marker remedy.
    result = gate_check([provider], profile, effort=effort, today=TODAY, now=NOW)
    assert result.ok is False
    assert result.grade == carrier
    assert "e6_arm_required" in result.reason
    # A wrong marker opens nothing — different rung, wrong case-fold target,
    # a different profile's real rung, and a placed sibling rung.
    for marker in ("sonnet@xhigh", "kimi-k3@max", f"{profile}@zzz"):
        wrong = gate_check([provider], profile, effort=effort, e6_arm=marker, today=TODAY, now=NOW)
        assert not (wrong.ok and wrong.e6_arm == f"{profile}@{effort}")
    # The exact marker opens the arm at its carrier grade.
    result = gate_check(
        [provider], profile, effort=effort, e6_arm=f"{profile}@{effort}", today=TODAY, now=NOW
    )
    assert result.ok is True, result.reason
    assert result.grade == carrier
    assert result.e6_arm == f"{profile}@{effort}"
    assert e6_arm_tag(f"{profile}@{effort}") in result.reason
    decision = launch.resolve_launch(profile, effort=effort, e6_arm=f"{profile}@{effort}")
    assert decision.e6_arm == f"{profile}@{effort}"
    assert decision.model_id == model_id
    assert decision.grade == carrier


@pytest.mark.parametrize("grade", ["S+", "S", "A+", "A", "B", "C"])
def test_no_new_rung_is_a_recommendation_candidate(grade):
    out = recommend([_provider("claude"), _provider("codex")], grade, today=TODAY, now=NOW)
    for profile, effort in RUNGS:
        for line in out.splitlines():
            if line[:1].isdigit():
                assert not (line.split()[1] == profile and f"--effort {effort}" in line), (grade, line)
            assert f"{profile}@{effort}" not in line or not line[:1].isdigit()
    assert "E6 arm" not in out


def test_a_new_rung_never_becomes_a_candidate_on_a_canon_view():
    """At its carrier the canon row is filtered; moved off it, ordinary."""
    entries = [
        bench.CatalogEntry(profile=p, effort=e, model_id=RUNGS[(p, e)][1], pool=POOL[p], grade=c)
        for (p, e), (c, _) in RUNGS.items()
    ]
    table = bench._catalog_grade_table(
        bench.CatalogView(entries=tuple(entries), source="server", backend="handoffkeep")
    )
    assert table is not None
    placed = {(p.name, p.launcher_effort or "") for ps in table.values() for p in ps}
    assert not (set(RUNGS) & placed)
    # A canon row moved off the carrier is an ordinary candidate again.
    moved = [
        bench.CatalogEntry(
            profile="sonnet",
            effort="max",
            model_id="claude-sonnet-5-5",
            pool="claude",
            grade="S",
        )
    ]
    table = bench._catalog_grade_table(
        bench.CatalogView(entries=tuple(moved), source="server", backend="handoffkeep")
    )
    assert ("sonnet", "max") in {(p.name, p.launcher_effort or "") for ps in table.values() for p in ps}


# --- AC2: sonnet@high at A ----------------------------------------------------


def test_sonnet_high_is_a_and_the_table_validates():
    placed = [(p.name, p.launcher_effort or "") for p in GRADE_TABLE["A"]]
    assert ("sonnet", "high") in placed
    assert not any((p.name, p.launcher_effort) == ("sonnet", "high") for p in GRADE_TABLE["B"])
    validate_grade_table()  # raises on a boundary violation
    row = next(p for p in GRADE_TABLE["A"] if p.name == "sonnet")
    assert row.benchmark == 43.0
    assert row.benchmark_annotation.startswith("급 실측(A;")
    assert "hk1786-propose-sonnet-high" in row.benchmark_annotation
    entry = {e.key: e for e in launch.snapshot_entries()}[("sonnet", "high")]
    assert entry.grade == "A"
    assert entry.decided_by == "b953-hk1786"
    assert "hk1786-propose-sonnet-high" in (entry.deviation_ref or "")


# --- AC3: the C1 file ----------------------------------------------------------


def _c1_rows() -> list[bench.CatalogEntry]:
    return bench._catalog_rows_from_json(json.loads(C1_FILE.read_text()))


def test_the_c1_file_parses_and_holds_exactly_the_five_rows():
    rows = _c1_rows()
    assert {(r.profile, r.effort) for r in rows} == {
        ("codex-sol", "low"),
        ("haiku", "xhigh"),
        ("haiku", "max"),
        ("sonnet", "max"),
        ("sonnet", "high"),
    }
    by_key = {r.key: r for r in rows}
    for key, (carrier, _model) in RUNGS.items():
        assert by_key[key].grade == carrier
        assert by_key[key].decided_by == "b953-hk1786"
        assert by_key[key].deviation_ref == "hk:doc/briefs/b953/tier-coverage"
    promote = by_key[("sonnet", "high")]
    assert promote.grade == "A"
    assert promote.decided_by == "b953-hk1786"
    assert promote.deviation_ref == "hk:doc/reports/b953/hk1786-propose-sonnet-high"


def test_the_c1_merge_is_monotonic_over_the_bundled_seed():
    merged = {entry.key: entry for entry in bench.catalog_snapshot()}
    rows = _c1_rows()
    for entry in rows:
        merged[entry.key] = entry
    write = {entry.key: entry.grade for entry in rows}
    assert grades.catalog_monotonicity_violations(list(merged.values()), write) == []


def test_a_c_carrier_sonnet_max_would_have_been_a_violation():
    """The mutant pin: sonnet@max at C sits below sonnet@xhigh's A+.

    The merged state is built from the real bundled snapshot — forcing
    e6_arm_carrier_grade back to the global C turns the baseline
    zero-violations assertion red."""
    merged = {entry.key: entry for entry in bench.catalog_snapshot()}
    # The seed's own sonnet@max row rides at its carrier — forcing the carrier
    # back to global C makes this assert (and the zero-violations baseline on
    # the write below) fail, which is what pins the mutant.
    assert merged[("sonnet", "max")].grade == "A+" == e6_arm_carrier_grade("sonnet", "max")
    rows = _c1_rows()
    for entry in rows:
        merged[entry.key] = entry
    write = {entry.key: entry.grade for entry in rows}
    assert grades.catalog_monotonicity_violations(list(merged.values()), write) == []
    # The pre-#1788 global-carrier mutant: sonnet@max pushed at C.
    merged[("sonnet", "max")] = dataclasses.replace(merged[("sonnet", "max")], grade="C")
    write[("sonnet", "max")] = "C"
    violations = grades.catalog_monotonicity_violations(list(merged.values()), write)
    assert violations
    assert violations[0].profile == "sonnet"
    assert violations[0].effort == "max"
    assert violations[0].grade == "C"
    assert violations[0].lower_grade == "A+"


def test_gate_check_refuses_a_carrier_grade_rung_without_marker():
    """Mutant pin for the gate's arm check: a grade table that carries
    sonnet@max at exactly its A+ carrier (the post-push canon) must still
    gate it — the row is the unmeasured arm, not a measured placement.
    Comparing the row to the global C instead of its carrier opens it
    unmarked."""
    table = {
        "A+": [
            Profile(
                "sonnet",
                "Sonnet 5.5 (max)",
                70.6,
                launcher_effort="max",
                benchmark_effort="max",
                aa_model_id="claude-sonnet-5-5",
            )
        ],
        "A": [
            Profile(
                "sonnet",
                "Sonnet 5.5 (high)",
                43.0,
                launcher_effort="high",
                benchmark_effort="high",
                aa_model_id="claude-sonnet-5-5",
            )
        ],
    }
    result = gate_check(
        [_provider("claude")],
        "sonnet",
        effort="max",
        grade_table=table,
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert "e6_arm_required" in result.reason
    assert result.grade == "A+"
    result = gate_check(
        [_provider("claude")],
        "sonnet",
        effort="max",
        grade_table=table,
        e6_arm="sonnet@max",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True
    assert result.grade == "A+"
    assert result.e6_arm == "sonnet@max"


# --- AC4: byte-identical elsewhere ---------------------------------------------


def test_every_other_launch_decision_is_byte_identical_to_base():
    """The untouched spellings resolve exactly as base 5be221b did — the
    de-graduated rungs' new answers are pinned explicitly."""
    expected = {
        # profile: (effort arg, resolved effort, grade, gate, model_id, e6_arm)
        "sonnet": (None, "high", "A", "default", "claude-sonnet-5-5", None),
        "sonnet@xhigh": ("xhigh", "xhigh", "A+", "escalation", "claude-sonnet-5-5", None),
        "sonnet@medium": ("medium", "medium", "C", "default", "claude-sonnet-5-5", None),
        "haiku": (None, "low", "C", "default", "claude-haiku-5-5", None),
        "haiku@medium": ("medium", "medium", "C", "default", "claude-haiku-5-5", None),
        "haiku@high": ("high", "high", "C", "default", "claude-haiku-5-5", None),
        "codex-sol": (None, "max", "S+", "default", "gpt-6.1-sol", None),
        "codex-sol@xhigh": ("xhigh", "xhigh", "S+", "escalation", "gpt-6.1-sol", None),
        "opus": (None, "high", "S+", "default", "claude-opus-5-5", None),
        "opus@low": ("low", "low", "S", "default", "claude-opus-5-5", None),
        "grok-hi": (None, "high", "S", "default", "grok-4.7", None),
        "kimi-k3": (None, "", "S", "default", "kimi-k3", None),
    }
    for key, (arg, effort, grade, gate, model_id, e6) in expected.items():
        profile = key.split("@")[0]
        decision = launch.resolve_launch(profile, effort=arg)
        assert (decision.effort, decision.grade, decision.gate, decision.model_id, decision.e6_arm) == (
            effort,
            grade,
            gate,
            model_id,
            e6,
        ), key


def test_existing_e6_rungs_are_byte_identical():
    """Every pre-#1788 arm rung: same carrier C, same marker-gated answers."""
    existing = [
        ("codex-sol", "high"),
        ("codex-sol", "medium"),
        ("kimi-k3", "high"),
        ("kimi-k3", "max"),
        ("grok-hi", "low"),
        ("grok-hi", "medium"),
    ]
    for profile, effort in existing:
        assert e6_arm_carrier_grade(profile, effort) == "C"
        provider = _provider({"kimi-k3": "kimi", "grok-hi": "grok"}.get(profile) or POOL[profile])
        result = gate_check([provider], profile, effort=effort, today=TODAY, now=NOW)
        assert result.ok is False and result.grade == "C"
        result = gate_check(
            [provider],
            profile,
            effort=effort,
            e6_arm=f"{profile}@{effort}",
            today=TODAY,
            now=NOW,
        )
        assert result.ok is True
        assert result.grade == "C"
        assert result.e6_arm == f"{profile}@{effort}"
