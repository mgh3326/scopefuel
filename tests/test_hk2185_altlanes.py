"""hk 2185 — utilization-conditioned promotion of the alternative lanes.

Operator desk op-1028-altlanes 1-4 FINAL, and the director-1 10-28 grade
placement (op-1028-grades) as corrected after the first per-effort inheritance
attempt:

* the canon catalog already carries the pi-* and cmd-dsflash41 rows. The bundled
  table equals those canon rows EXACTLY — grade, gate and gate_reason per effort
  (frozen in ``fixtures/hk2185/canon_rows.json``). cmd-dsflash41 is @high only:
  @low/@max are deliberately unplaced (a C max below an A+ high would break the
  canon's effort monotonicity), and the gate refuses them as not placed.
* a primary pool (claude or codex) triggers promotion when its WEEKLY (7d) pace
  multiple — exactly the per-bucket field scopefuel already prints — is at or
  above ``[settings] altlane_pace_multiple`` (default 1.2x), with the weekly
  pace-warning as the secondary trigger. The 5h pace never triggers alone.
* promotion is a RANKING step applied after every eligibility filter: the alt
  row ranks above the triggering primary for the grade it is placed in, the
  primary stays listed, and the row carries ``promoted: <pool> 7d pace N.NNx``.
* limits: never at S+, never for a tester/consult role, never above the placed
  grade, never for a rung with a recorded FAIL/BOUNCE at that grade, and never
  for a row its own pool/host cannot measure (cmd-dsflash41 stays desktop-only
  until hk 2180 — the host filter still applies after promotion).

Each test docstring names the mutant it kills.
"""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tarfile

import pytest

from scopefuel import bench, cli, grades, launch, policy
from scopefuel import recommend as recommend_mod
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    ALTLANE_CANON_ROWS,
    ALTLANE_PROFILES,
    GRADE_TABLE,
    altlane_canon_grade,
    altlane_promotions,
    gate_check,
    provider_family,
    recommend_dict,
)

NOW = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
# The head this branch was cut from — the "today" order the golden compares to.
BASE_COMMIT = "70d29e87700f59a088ef1745e49e25bb1273364c"
FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "hk2185"
CANON_ROWS = json.loads((FIXTURES / "canon_rows.json").read_text(encoding="utf-8"))


def _bucket(window: str, used: float, reset_in_hours: float | None) -> Bucket:
    reset = None if reset_in_hours is None else (NOW + dt.timedelta(hours=reset_in_hours)).isoformat()
    return Bucket(
        label=window,
        window=window,
        used_pct=used,
        resets_at=reset,
        scope=Scope("account"),
        horizon="now" if window == "5h" else "week",
    )


def _provider(pool: str, *buckets: Bucket, pool_class: str = "spend") -> ProviderResult:
    return ProviderResult(id=pool, pool_class=pool_class, buckets=list(buckets))  # type: ignore[arg-type]


def _weekly(used: float, *, reset_in_hours: float = 84.0) -> ProviderResult:
    """A primary measured on the weekly window.

    ``reset_in_hours=84`` is half of the 7d window, so the pace multiple is
    exactly ``used / 50`` — 60% used is 1.2x, 50% is 1.0x, 75% is 1.5x.
    """

    return _provider("claude", _bucket("7d", used, reset_in_hours))


def _codex_weekly(used: float, *, reset_in_hours: float = 84.0) -> ProviderResult:
    return _provider("codex", _bucket("7d", used, reset_in_hours), pool_class="preserve")


def _credit(pool: str = "anthropic-api", *, used: float = 25.0) -> ProviderResult:
    return _provider(pool, _bucket("7d", used, 144.0))


def _healthy(*extra: ProviderResult) -> list[ProviderResult]:
    return [
        _provider("claude", _bucket("7d", 40, 84.0)),
        _codex_weekly(20.0),
        _credit("anthropic-api"),
        _credit("commandcode"),
        *extra,
    ]


def _candidates(payload: dict) -> list[dict]:
    return [row for row in payload["rows"] if row["kind"] == "candidate"]


def _names(payload: dict) -> list[str]:
    return [row["profile"] for row in _candidates(payload)]


def _base_recommend(tmp_path):
    """The pre-change recommend module, loaded from ``BASE_COMMIT``."""

    repo = pathlib.Path(__file__).resolve().parents[1]
    if subprocess.run(["git", "cat-file", "-e", BASE_COMMIT], cwd=repo, capture_output=True).returncode:
        subprocess.run(
            ["git", "fetch", "--depth=1", "origin", BASE_COMMIT],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    archive = subprocess.run(
        ["git", "archive", BASE_COMMIT, "src"], cwd=repo, check=True, capture_output=True
    ).stdout
    target = tmp_path / "base"
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(target, filter="data")
    package = target / "src" / "scopefuel"
    spec = importlib.util.spec_from_file_location(
        "scopefuel_hk2185_base", package / "__init__.py", submodule_search_locations=[str(package)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return importlib.import_module("scopefuel_hk2185_base.recommend")


# ---------------------------------------------------------------------------
# FINAL grade placement: the bundled rows equal the canon rows exactly
# ---------------------------------------------------------------------------


def test_bundled_rows_equal_the_canon_rows_exactly():
    """Mutant: hand-copy or re-derive a row -> this goes RED.

    The canon is the source (desk pushed these rows 2026-10-28, ref desk
    op-1028-grades); the bundled table must agree on grade, gate, gate_reason,
    model id, pool and annotation, or a seed push / a canon-serving host would
    disagree with the bundle.
    """

    rows = {entry.key: entry for entry in bench.catalog_snapshot() if entry.profile in ALTLANE_PROFILES}
    assert len(rows) == len(CANON_ROWS) == len(ALTLANE_CANON_ROWS)
    for canon in CANON_ROWS:
        key = (canon["profile"], canon["effort"])
        assert key in rows, key
        entry = rows[key]
        assert entry.grade == canon["grade"], key
        assert entry.gate == canon["gate"], key
        assert (entry.gate_reason or None) == (canon["gate_reason"] or None), key
        assert entry.model_id == canon["model_id"], key
        assert entry.pool == canon["pool"], key
        assert entry.score is None, key
        assert entry.benchmark_annotation == canon["benchmark_annotation"], key


def test_every_placed_rung_is_a_grade_table_row():
    """A canon row with no bundled placement would vanish from --recommend."""

    placed = {
        (profile.name, profile.launcher_effort)
        for rows in GRADE_TABLE.values()
        for profile in rows
        if profile.name in ALTLANE_PROFILES
    }
    assert placed == {(row["profile"], row["effort"]) for row in CANON_ROWS}


def test_cmd_is_high_only_and_low_max_are_unplaced():
    """Mutant: copy devin-ds41-max (C) onto cmd@max -> the canon refuses the seed."""

    assert recommend_mod.worker_tester_rungs("cmd-dsflash41") == ("high",)
    assert altlane_canon_grade("cmd-dsflash41", "high") == "A+"
    keys = {(entry.profile, entry.effort) for entry in bench.catalog_snapshot()}
    assert ("cmd-dsflash41", "high") in keys
    assert ("cmd-dsflash41", "low") not in keys
    assert ("cmd-dsflash41", "max") not in keys


def test_the_alt_lane_ladders_are_monotone_so_a_seed_can_never_400():
    """The canon's checkBenchCatalogMonotonicity refuses a worse higher rung."""

    write = {
        (profile.name, profile.launcher_effort or ""): grade
        for grade, rows in GRADE_TABLE.items()
        for profile in rows
        if profile.name in ALTLANE_PROFILES
    }
    assert grades.catalog_monotonicity_violations(list(bench.catalog_snapshot()), write) == []


def test_pi_grades_match_their_source_rows_where_the_canon_inherits():
    """Mutant: a fixed grade for every pi rung -> this goes RED."""

    diverged = set()
    for profile, rung, grade, _gate, _reason in ALTLANE_CANON_ROWS:
        if profile not in recommend_mod.PI_CREDIT_MODELS:
            continue
        source = recommend_mod.ALTLANE_CANON_SOURCES[(profile, rung)]
        source_profile, _, source_effort = source.partition("@")
        if (profile, rung) in recommend_mod.ALTLANE_CANON_DIVERGENCES:
            diverged.add((profile, rung))
            continue
        assert grade == recommend_mod.source_placement_grade(source_profile, source_effort), (
            profile,
            rung,
        )
    # The canon's own re-placements are named, not silently accepted.
    assert diverged == set(recommend_mod.ALTLANE_CANON_DIVERGENCES)
    assert recommend_mod.altlane_canon_grade("pi-opus55", "xhigh") == "S+"
    assert recommend_mod.altlane_canon_grade("pi-sonnet55", "high") == "A"


def test_pi_opus55_is_listed_at_s_plus_with_the_same_order():
    """FINAL AC: pi-opus55 at S+ appears, and the S+ order is unchanged under pace."""

    hot = [_weekly(75.0), _credit("anthropic-api"), _credit("commandcode")]
    payload = recommend_dict(hot, "S+", today=TODAY, now=NOW)
    names = _names(payload)
    assert "pi-opus55" in names
    # No promotion at S+: the credit rows stay after the standard lanes and no
    # row carries a promotion reason.
    assert not any(row.get("promotion") for row in _candidates(payload))
    assert max(i for i, name in enumerate(names) if name == "pi-opus55") > min(
        i for i, name in enumerate(names) if name != "pi-opus55"
    )
    # The escalation-gated opus@max rung sits in the escalation section, not as
    # a ranked candidate.
    assert "pi-opus55" in {row["profile"] for row in payload["rows"] if row["kind"] == "escalation"}


# ---------------------------------------------------------------------------
# AC1: a hot primary promotes the eligible alt above it, reason names the multiple
# ---------------------------------------------------------------------------


def test_hot_primary_promotes_the_eligible_alt_above_it():
    """Mutant: ignore the multiple (never promote) -> this goes RED."""

    providers = _healthy()
    providers[0] = _weekly(75.0)  # 1.5x
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    names = _names(payload)
    assert "pi-sonnet55" in names and "sonnet" in names
    assert names.index("pi-sonnet55") < names.index("sonnet")
    promoted = next(row for row in _candidates(payload) if row["profile"] == "pi-sonnet55")
    assert promoted["promotion"] == "promoted: claude 7d pace 1.50x"
    # The primary stays listed as a fallback.
    assert "sonnet" in names
    # The row is only listed for the rung the canon places at A.
    assert promoted["launch"] == "pi-sonnet55 --effort high"


def test_the_hottest_triggering_primary_names_the_reason():
    providers = _healthy()
    providers[0] = _weekly(60.0)  # 1.2x
    providers[1] = _codex_weekly(75.0)  # 1.5x — hotter
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    promoted = next(row for row in _candidates(payload) if row["profile"] == "pi-sonnet55")
    assert promoted["promotion"] == "promoted: codex 7d pace 1.50x"


def test_promotion_never_lifts_a_rung_above_its_placed_grade():
    """Mutant: promote above the placed grade -> this goes RED."""

    providers = _healthy()
    providers[0] = _weekly(75.0)
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        payload = recommend_dict(providers, grade, today=TODAY, now=NOW)
        placed_here = {
            profile
            for profile, rung, row_grade, gate, _r in ALTLANE_CANON_ROWS
            if row_grade == grade and gate == "default"
        }
        listed = {name for name in _names(payload) if name in ALTLANE_PROFILES}
        assert listed <= placed_here, (grade, listed, placed_here)


# ---------------------------------------------------------------------------
# AC2/AC3/AC4: threshold semantics and the golden
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("used", [40.0, 50.0, 59.5])
def test_at_or_below_the_threshold_keeps_todays_order(used, tmp_path):
    """Mutant: promote when healthy / at 1.19x -> this goes RED."""

    providers = _healthy()
    providers[0] = _weekly(used)
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    names = _names(payload)
    assert not any(row.get("promotion") for row in _candidates(payload))
    # Today's exact order: the pre-change module lists the same non-alt names in
    # the same order for the same fixture.
    base = _base_recommend(tmp_path)
    base_out = base.recommend(providers, "A", today=TODAY, now=NOW, bench_scores=None, model_prices={})
    base_names = []
    for line in base_out.splitlines():
        if line[:1].isdigit():
            tokens = line.split(". ", 1)[1].split()
            while tokens and tokens[0].startswith("🔥"):
                tokens.pop(0)
            base_names.append(tokens[0])
    assert [name for name in names if name not in ALTLANE_PROFILES] == base_names
    # The credit rows stay after every standard lane.
    standard = [i for i, name in enumerate(names) if name not in ALTLANE_PROFILES]
    credit = [i for i, name in enumerate(names) if name in ALTLANE_PROFILES]
    assert standard and credit and max(standard) < min(credit)


def test_the_threshold_is_inclusive():
    """Mutant: use > instead of >= -> 1.2x exactly would not promote (RED)."""

    providers = _healthy()
    providers[0] = _weekly(60.0)  # exactly 1.2x
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    promoted = next(row for row in _candidates(payload) if row["profile"] == "pi-sonnet55")
    assert promoted["promotion"] == "promoted: claude 7d pace 1.20x"


def test_the_threshold_is_a_config_key_read_at_runtime(monkeypatch):
    """Mutant: hard-code 1.2 -> a 1.5 config would still promote at 1.3x (RED)."""

    assert policy.get_altlane_pace_multiple() == pytest.approx(1.2)
    providers = _healthy()
    providers[0] = _weekly(65.0)  # 1.3x — above the default, below 1.5
    promoted_at_default = next(
        row
        for row in _candidates(recommend_dict(providers, "A", today=TODAY, now=NOW))
        if row["profile"] == "pi-sonnet55"
    )
    assert promoted_at_default.get("promotion")

    # No code edit — just the config key.
    assert cli.main(["policy", "setting", "altlane_pace_multiple", "1.5"]) == 0
    assert policy.get_altlane_pace_multiple() == pytest.approx(1.5)
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    assert not any(row.get("promotion") for row in _candidates(payload))
    assert cli.main(["policy", "setting", "altlane_pace_multiple", "none"]) == 0
    assert policy.get_altlane_pace_multiple() == pytest.approx(1.2)


def test_a_malformed_setting_falls_back_to_the_default():
    policy.config_path().parent.mkdir(parents=True, exist_ok=True)
    policy.config_path().write_text('[settings]\naltlane_pace_multiple = "nope"\n')
    assert policy.get_altlane_pace_multiple() == pytest.approx(1.2)
    policy.config_path().write_text("[settings]\naltlane_pace_multiple = -1\n")
    assert policy.get_altlane_pace_multiple() == pytest.approx(1.2)


# ---------------------------------------------------------------------------
# AC5: the 5h pace never triggers alone; the weekly percent is not the trigger
# ---------------------------------------------------------------------------


def test_the_5h_pace_never_triggers_alone():
    """Mutant: read the 5h window -> this goes RED."""

    providers = _healthy()
    providers[0] = _provider(
        "claude",
        _bucket("5h", 95.0, 1.0),  # a very hot 5h window
        _bucket("7d", 40.0, 84.0),  # 0.8x weekly
    )
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    assert not any(row.get("promotion") for row in _candidates(payload))
    assert altlane_promotions(providers, NOW, grade="A") == {}


def test_the_trigger_is_the_pace_multiple_not_the_weekly_percent():
    """Mutant: compare used% to the threshold -> this goes RED.

    Both fixtures use 60% of the weekly window; only the first has a 1.2x pace
    (the second is late in the window, so 60% used is 0.7x).
    """

    early = [_weekly(60.0, reset_in_hours=84.0)]
    late = [_weekly(60.0, reset_in_hours=24.0)]
    assert altlane_promotions(early, NOW, grade="A")
    assert altlane_promotions(late, NOW, grade="A") == {}


def test_the_secondary_weekly_pace_warning_triggers(monkeypatch):
    """time_to_exhaust < hours_to_reset is the secondary trigger.

    It is the fallback for a weekly window whose multiple cannot be computed;
    a measurable multiple decides on its own (otherwise a 1.19x window's
    warning would silently defeat the 1.2x threshold).
    """

    from types import SimpleNamespace

    providers = [_weekly(40.0)]
    monkeypatch.setattr(recommend_mod, "pace_for", lambda *a, **k: SimpleNamespace(ratio=None))
    monkeypatch.setattr(
        recommend_mod, "_pace_window_verdicts", lambda *a, **k: [SimpleNamespace(exhausts_before_reset=True)]
    )
    assert altlane_promotions(providers, NOW, grade="A") == {"claude": "claude 7d pace-warning"}


# ---------------------------------------------------------------------------
# AC6: the gate rank follows the same order
# ---------------------------------------------------------------------------


def test_the_gate_alternatives_follow_the_promoted_order():
    """Mutant: rank the gate on a different order -> this goes RED."""

    providers = _healthy()
    # 100% used: sonnet is refused (exhausted) and its alternatives are ranked.
    providers[0] = _provider("claude", _bucket("7d", 100.0, 84.0))
    result = gate_check(providers, "sonnet", role="worker", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.alternatives
    recommended = _names(recommend_dict(providers, "A", today=TODAY, now=NOW))
    # The gate's list is the recommend order with the requested row removed —
    # the promoted alt ranks where recommend ranks it.
    expected = [name for name in recommended if name != "sonnet"]
    assert list(result.alternatives) == expected[: len(result.alternatives)]
    assert "pi-sonnet55" in result.alternatives


# ---------------------------------------------------------------------------
# AC7: cmd-dsflash41 stays desktop-only until hk 2180 (the host filter applies)
# ---------------------------------------------------------------------------


def test_the_cmd_row_is_not_offered_where_the_host_has_no_cmd_measurement():
    """Mutant: promote a row into view regardless of its own pool -> RED.

    The commandcode pool is host-local (manual observation / local /usage
    probe); while hk 2180 is open cmd-dsflash41 may only run on the desktop.
    scopefuel never offers the row on a host that cannot measure its pool, even
    when a primary is hot and the row would otherwise be promoted.
    """

    providers = [_weekly(75.0), _credit("anthropic-api")]  # no commandcode result
    payload = recommend_dict(providers, "A+", today=TODAY, now=NOW)
    assert "cmd-dsflash41" not in _names(payload)
    folded = [row for row in payload["excluded"] if row["profile"] == "cmd-dsflash41"]
    assert folded, payload["excluded"]
    # With a local commandcode measurement the row is listed and promoted.
    providers.append(_credit("commandcode"))
    payload = recommend_dict(providers, "A+", today=TODAY, now=NOW)
    promoted = next(row for row in _candidates(payload) if row["profile"] == "cmd-dsflash41")
    assert promoted.get("promotion") == "promoted: claude 7d pace 1.50x"
    assert "desktop-only until hk 2180" in (
        next(p for p in GRADE_TABLE["A+"] if p.name == "cmd-dsflash41").placement_note or ""
    )


# ---------------------------------------------------------------------------
# AC8: S+ never promotes
# ---------------------------------------------------------------------------


def test_s_plus_never_promotes():
    """Mutant: promote at S+ -> this goes RED."""

    providers = _healthy()
    providers[0] = _weekly(100.0)
    assert altlane_promotions(providers, NOW, grade="S+") == {}
    payload = recommend_dict(providers, "S+", today=TODAY, now=NOW)
    assert not any(row.get("promotion") for row in _candidates(payload))


# ---------------------------------------------------------------------------
# AC9: tester/consult roles keep the family split
# ---------------------------------------------------------------------------


def test_a_tester_role_keeps_the_family_split_and_never_promotes():
    """Mutant: promote for a tester seat -> this goes RED."""

    providers = _healthy()
    providers[0] = _weekly(100.0)
    assert altlane_promotions(providers, NOW, grade="A", role="tester") == {}
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW, role="tester")
    assert not any(row.get("promotion") for row in _candidates(payload))
    result = gate_check(providers, "sonnet", role="tester", today=TODAY, now=NOW)
    if result.alternatives:
        # The same-grade alternatives keep today's order (no promoted first row).
        assert result.alternatives[0] != "pi-sonnet55"


def test_a_pi_tester_for_a_claude_builder_is_same_family():
    """FINAL AC: pi-* are the Anthropic family, cmd is the DeepSeek family."""

    for profile in ("pi-opus55", "pi-sonnet55", "pi-haiku55"):
        assert provider_family(profile) == provider_family("sonnet") == "claude"
    assert provider_family("cmd-dsflash41") == provider_family("devin-ds41") == "devin"
    # The family is the tester-separation axis, not the quota pool.
    assert recommend_mod.profile_pool("pi-opus55")[0] == "anthropic-api"
    assert recommend_mod.profile_pool("cmd-dsflash41")[0] == "commandcode"


# ---------------------------------------------------------------------------
# AC10: a recorded FAIL/BOUNCE blocks promotion for that grade
# ---------------------------------------------------------------------------


def test_a_recorded_failure_blocks_promotion_for_that_grade():
    """Mutant: ignore rep evidence -> this goes RED."""

    providers = _healthy()
    providers[0] = _weekly(75.0)
    blocked = frozenset({("pi-sonnet55", "A")})
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW, promotion_blocks=blocked)
    row = next(row for row in _candidates(payload) if row["profile"] == "pi-sonnet55")
    assert "promotion" not in row
    # A different grade is unaffected.
    other = recommend_dict(providers, "A+", today=TODAY, now=NOW, promotion_blocks=blocked)
    assert next(row for row in _candidates(other) if row["profile"] == "pi-sonnet55")["promotion"].startswith(
        "promoted:"
    )


def test_a_lazy_promotion_block_is_only_read_when_a_promotion_applies():
    calls = {"n": 0}

    def blocks() -> frozenset[tuple[str, str]]:
        calls["n"] += 1
        return frozenset({("pi-sonnet55", "A")})

    healthy = _healthy()
    recommend_dict(healthy, "A", today=TODAY, now=NOW, promotion_blocks=blocks)
    assert calls["n"] == 0  # nothing triggered — no reps read
    hot = _healthy()
    hot[0] = _weekly(75.0)
    recommend_dict(hot, "A", today=TODAY, now=NOW, promotion_blocks=blocks)
    assert calls["n"] == 1


def test_the_cli_reads_failures_from_the_reps_store(monkeypatch):
    """The CLI's block set comes from reps: completed=0 or a recorded blocker."""

    class Rep:
        def __init__(self, profile, grade, completed, blockers):
            self.profile = profile
            self.grade = grade
            self.completed = completed
            self.blockers_found = blockers

    reps = [
        Rep("pi-sonnet55", "A", 0, 0),  # FAIL
        Rep("pi-haiku55", "C", 1, 2),  # BOUNCE
        Rep("pi-opus55", "S+", 1, 0),  # clean pass — no block
        Rep("sonnet", "A", 0, 0),  # not an alt lane — ignored
    ]
    monkeypatch.setattr(bench, "read_reps", lambda **kwargs: reps)
    assert cli._promotion_blocks() == frozenset({("pi-sonnet55", "A"), ("pi-haiku55", "C")})


# ---------------------------------------------------------------------------
# AC11 / limits: eligibility before promotion
# ---------------------------------------------------------------------------


def test_an_ineligible_alt_is_never_promoted_into_view():
    """Mutant: apply promotion before the eligibility filters -> RED."""

    # The anthropic-api pool is unmeasurable (error): the alt rows fold, and a
    # hot claude cannot promote what is not listed.
    broken = ProviderResult(id="anthropic-api", pool_class="spend", buckets=[], error="probe failed")
    providers = [_weekly(75.0), broken, _credit("commandcode")]
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    assert "pi-sonnet55" not in _names(payload)
    assert not any(row.get("promotion") for row in _candidates(payload))


def test_an_alt_with_an_unsubscribed_or_excluded_pool_is_not_promoted():
    policy.set_policy("anthropic-api", "exclude", until=dt.date(2099, 12, 31))
    providers = [_weekly(75.0), _credit("anthropic-api")]
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    assert "pi-sonnet55" not in _names(payload)


def test_consult_only_and_escalation_rungs_are_never_promoted():
    """A consult_only row is not a candidate; an escalation row is not ranked."""

    providers = _healthy()
    providers[0] = _weekly(100.0)
    payload = recommend_dict(providers, "A+", today=TODAY, now=NOW)
    # pi-sonnet55@max is consult_only at A+; only the xhigh rung is a candidate.
    assert not any(
        row["profile"] == "pi-sonnet55" and row["launch"].endswith("max") for row in _candidates(payload)
    )
    s_plus = recommend_dict(providers, "S+", today=TODAY, now=NOW)
    assert not any(row.get("promotion") for row in _candidates(s_plus))


def test_the_bundled_table_still_launches_and_gates_every_placed_rung():
    view = launch.CatalogView(
        entries=launch.snapshot_entries(), source=bench.CATALOG_SOURCE_SNAPSHOT, age_s=None
    )
    for profile, rung, grade, gate, _reason in ALTLANE_CANON_ROWS:
        request = "hk:task/2185" if gate in ("consult_only", "escalation") else None
        decision = launch.resolve_launch(
            profile, effort=rung, role="worker", operator_request=request, view=view
        )
        assert (decision.grade, decision.gate) == (grade, gate), (profile, rung)
        result = gate_check(
            [_credit(recommend_mod.profile_pool(profile)[0])],
            profile,
            role="worker",
            effort=rung,
            operator_request=request,
            today=TODAY,
            now=NOW,
        )
        assert result.ok is True, (profile, rung, result.reason)


def test_unplaced_cmd_rungs_are_refused_as_not_placed():
    for rung in ("low", "max"):
        result = gate_check(
            [_credit("commandcode")], "cmd-dsflash41", role="worker", effort=rung, today=TODAY, now=NOW
        )
        assert result.ok is False
        assert "not_placed" in result.reason


def test_manual_credit_rows_still_need_an_explicit_effort():
    for profile in ALTLANE_PROFILES:
        result = gate_check([_credit(recommend_mod.profile_pool(profile)[0])], profile, role="worker")
        assert result.ok is False
        assert result.reason.startswith("effort_required:")
