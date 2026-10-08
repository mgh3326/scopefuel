"""task #750 — rep -> catalog rung (profile@effort) resolution tests.

Every rep resolves to a real rung through one printed basis — direct,
builder map, alias, or the profile default with ``effort inferred`` — and
anything that still cannot resolve stays unrung, never silently counted.

The unmapped-builder guard reads a fixture copy of wrk's builder-spelling
list, the same pattern ``tests/test_wrk_contract_guard.py`` uses: when wrk
adds a builder spelling and the fixture is refreshed, the equality check
fails until ``grades._BUILDER_RUNGS`` gains the mapping.
"""

from __future__ import annotations

import pytest
from test_wrk_contract_guard import WRK_CATALOG_EXEMPT, WRK_CATALOG_SPELLINGS

from scopefuel import bench, grades

HOST = "test-host"

# --- checked-in bin/wrk contract --------------------------------------------
# The complete rep-visible builder-spelling set, derived — not hand-maintained:
# every builder/captain spelling wrk recognizes lives in the contract guard's
# mirror tables, either as a ``resolve_catalog_profile()`` arm
# (WRK_CATALOG_SPELLINGS) or as a catalog-exempt argv spelling
# (WRK_CATALOG_EXEMPT — devin-*/kimi-* builder spellings take no effort flag,
# so the catalog resolver has no arm for them, but a rep still records the
# spelling). When wrk adds a builder spelling the mirror-side guard fails
# first; refreshing those tables then fails the equality below until
# ``grades._BUILDER_RUNGS`` maps it.
WRK_RECOGNIZED_BUILDER_SPELLINGS: frozenset[str] = frozenset(
    spelling
    for spelling in (*WRK_CATALOG_SPELLINGS, *WRK_CATALOG_EXEMPT)
    if spelling.startswith(("builder-", "captain-"))
)

# Expected (catalog profile, effort pin or "") per spelling — the value half
# of the guard. Mirrors the builder rows of wrk's
# ``resolve_catalog_profile()`` plus the catalog-exempt builder spellings the
# launcher carries no effort flag for (devin-*/kimi-k3 base spellings — their
# rung is the profile's default row, so the pin is "").
#
# Deliberately absent: ``builder-astra``/``captain-astra`` — wrk removed those
# spellings (task #526); they die on use, and a rep spelled that way must not
# silently re-admit the profile.
WRK_BUILDER_SPELLINGS: dict[str, tuple[str, str]] = {
    "builder-opus": ("opus", ""),
    "captain-opus": ("opus", ""),
    "builder-opus-low": ("opus", "low"),
    "builder-opus-medium": ("opus", "medium"),
    "builder-sonnet-xhigh": ("sonnet", "xhigh"),
    "builder-sonnet-max": ("sonnet", "max"),
    # #921: bare builder-sonnet consults sonnet@xhigh (EFFORT:-xhigh) —
    # accepts high|xhigh|max; a recorded rep effort still wins over the pin.
    "builder-sonnet": ("sonnet", "xhigh"),
    "builder-sol": ("codex-sol", "high"),
    "captain-sol": ("codex-sol", "high"),
    "builder-sol-high": ("codex-sol", "high"),
    "builder-sol-max": ("codex-sol", "max"),
    "builder-sol-medium": ("codex-sol", "medium"),
    # #1026: the gpt-6-sol rollback builder seat — catalog-exempt on the wrk
    # side (literal gpt-6-sol argv) but it gates at codex-sol@high, so its
    # reps resolve there and read as model mismatches.
    "builder-sol6": ("codex-sol", "high"),
    "builder-luna": ("codex-luna", "xhigh"),
    "builder-luna-max": ("codex-luna", "max"),
    "builder-terra-high": ("codex-terra", "high"),
    "builder-terra-xhigh": ("codex-terra", "xhigh"),
    "builder-terra-max": ("codex-terra", "max"),
    "builder-grok": ("grok-hi", "xhigh"),
    "builder-grok-low": ("grok-hi", "low"),
    "builder-grok-medium": ("grok-hi", "medium"),
    "builder-grok-xhigh": ("grok-hi", "xhigh"),
    "builder-devin": ("devin-swe2", ""),
    "builder-devin-medium": ("devin-swe2-medium", ""),
    "builder-devin-max": ("devin-swe2-max", ""),
    "builder-ds41": ("devin-ds41", ""),
    "builder-ds41-max": ("devin-ds41-max", ""),
    "builder-kimi": ("kimi-k3", ""),
    "builder-kimi-high": ("kimi-k3", "high"),
    "builder-kimi-max": ("kimi-k3", "max"),
}


def _rep(task_ref: str, **overrides) -> dict:
    row = {
        "profile": "opus",
        "model_id": "claude-opus-5-5",
        "task_ref": task_ref,
        "tier": "T1",
        "role": "impl",
        "rounds": 1,
        "blockers_found": 0,
        "completed": 1,
        "recorded_at": "2026-09-20T10:00:00Z",
    }
    row.update(overrides)
    return row


def _seed(rows: list[dict]) -> None:
    for row in rows:
        bench.add_rep(**row)


def _entry(profile: str, effort: str, grade: str, **overrides) -> bench.CatalogEntry:
    fields = {
        "profile": profile,
        "effort": effort,
        # Fixture reps record model_id="claude-opus-5-5" — the rung's catalog
        # model defaults to the same so v1.1 model-match admits them; tests
        # seeding other-model reps pass the entry model explicitly.
        "model_id": "claude-opus-5-5",
        "pool": "test",
        "grade": grade,
    }
    fields.update(overrides)
    return bench.CatalogEntry(**fields)


def _view(*entries: bench.CatalogEntry) -> bench.CatalogView:
    return bench.CatalogView(
        entries=tuple(entries),
        source=bench.CATALOG_SOURCE_SNAPSHOT,
        backend=bench.BENCH_BACKEND_LOCAL,
        reason="auto-local",
    )


def _canon_view(*entries: bench.CatalogEntry) -> bench.CatalogView:
    return bench.CatalogView(
        entries=tuple(entries),
        source=bench.CATALOG_SOURCE_SERVER,
        backend=bench.BENCH_BACKEND_HANDOFFKEEP,
        reason="configured",
        age_s=0.0,
    )


def _propose(view, *, min_passes: int = 2) -> grades.Proposal:
    evidence = grades.gather_reps(view=view, host=HOST)
    return grades.evaluate(evidence, view, min_passes=min_passes)


def _counted(proposal: grades.Proposal, task_ref: str) -> grades.EvidenceRep:
    return next(r for r in proposal.evidence.rows if r.rep.task_ref == task_ref)


def _keys(proposal: grades.Proposal) -> set[tuple[str, str]]:
    return {r.key for r in proposal.results}


# ---------------------------------------------------------------------------
# The unmapped-builder guard (AC1a)
# ---------------------------------------------------------------------------


def test_every_wrk_builder_spelling_is_mapped():
    """Guard: when wrk adds a builder spelling and the contract-guard tables
    are refreshed, the equality fails until _BUILDER_RUNGS maps it — no
    silent drops, no stale extras."""
    assert set(grades._BUILDER_RUNGS) == WRK_RECOGNIZED_BUILDER_SPELLINGS


def test_builder_mapping_values_match_wrk():
    """The mapped (profile, effort pin) values mirror wrk's resolver/gate
    tables — a drifted value silently lands reps on the wrong rung."""
    assert grades._BUILDER_RUNGS == WRK_BUILDER_SPELLINGS


@pytest.mark.parametrize("spelling", sorted(WRK_BUILDER_SPELLINGS))
def test_builder_mapping_resolves_to_a_catalog_profile(spelling):
    """Every mapped builder lands on a real catalog profile — a mapping that
    pointed at nothing would strand its reps as unrung."""
    profile, _ = WRK_BUILDER_SPELLINGS[spelling]
    profiles = {entry.profile for entry in bench.catalog_snapshot()}
    assert profile in profiles


def test_removed_builder_astra_stays_unmapped():
    """builder-astra was deleted from wrk (#526): mapping it would quietly
    re-admit a spelling the launcher now refuses."""
    assert "builder-astra" not in grades._BUILDER_RUNGS
    assert "captain-astra" not in grades._BUILDER_RUNGS


# ---------------------------------------------------------------------------
# direct (AC2 kind)
# ---------------------------------------------------------------------------


def test_direct_resolution_recorded_effort(tmp_path, isolated_cache):
    """A rep whose spelling IS the catalog profile lands on its recorded rung
    — no mapping, no inference, kind 'direct'."""
    view = _view(_entry("opus", "xhigh", "S"))
    _seed([_rep("t1", profile="opus", effort="xhigh", grade="A")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("opus", "xhigh")
    assert row.row_key == ("opus", "xhigh")
    assert row.resolution == "direct"
    assert row.effort_inferred is False


# ---------------------------------------------------------------------------
# builder map (AC1a)
# ---------------------------------------------------------------------------


def test_builder_map_pinned_rung(tmp_path, isolated_cache):
    """builder-grok pins grok-hi@xhigh — the rung wrk's gate consults, not the
    rep profile's literal name."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed([_rep("t1", profile="builder-grok", effort="xhigh", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("grok-hi", "xhigh")
    assert row.resolution == "builder map"
    assert row.effort_inferred is False


def test_builder_map_supplies_effort_when_rep_records_none(tmp_path, isolated_cache):
    """builder-grok-low with no recorded effort lands on the pinned
    grok-hi@low — the pin is the launch's rung, not an inference."""
    view = _view(_entry("grok-hi", "low", "C"), _entry("grok-hi", "", "S"))
    _seed([_rep("t1", profile="builder-grok-low", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("grok-hi", "low")
    assert row.row_key == ("grok-hi", "low")
    assert row.effort_inferred is False


def test_builder_map_profile_without_pin_infers_effort(tmp_path, isolated_cache):
    """builder-devin-max names the devin-swe2-max profile and passes no effort
    flag — the rung is the profile's default row, marked inferred."""
    view = _view(_entry("devin-swe2-max", "", "A+"))
    _seed([_rep("t1", profile="builder-devin-max", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("devin-swe2-max", "")
    assert row.resolution == "builder map"
    assert row.effort_inferred is True


def test_recorded_effort_wins_over_builder_pin(tmp_path, isolated_cache):
    """A builder rep that recorded a different effort measured that rung —
    the rep's record outranks the spelling's pin."""
    view = _view(_entry("grok-hi", "high", "C"), _entry("grok-hi", "", "S"))
    _seed([_rep("t1", profile="builder-grok", effort="high", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("grok-hi", "high")
    assert row.row_key == ("grok-hi", "high")


# ---------------------------------------------------------------------------
# default effort — marked, never measured (AC1b)
# ---------------------------------------------------------------------------


def test_default_effort_inferred_and_marked(tmp_path, isolated_cache):
    """An effort-less rep on a catalog profile lands on the profile's catalog
    default rung (opus -> high), kind 'default effort', effort inferred."""
    view = _view(
        _entry("opus", "low", "A"),
        _entry("opus", "medium", "A"),
        _entry("opus", "high", "S"),
        _entry("opus", "xhigh", "S+"),
    )
    _seed([_rep("t1", profile="opus", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("opus", "high")
    assert row.row_key == ("opus", "high")
    assert row.resolution == "default effort"
    assert row.effort_inferred is True
    text = grades.render_proposal(proposal, view)
    assert "(effort inferred)" in text
    # Mutant guard: the marker must read "inferred", never "measured".
    assert "effort inferred" in text and "measured effort" not in text


def test_default_effort_empty_rung_profile(tmp_path, isolated_cache):
    """For a profile the catalog keys only on the default row the inferred
    effort is "" — grok-hi's launcher default (high) is not a rung."""
    view = _view(_entry("grok-hi", "", "S"))
    _seed([_rep("t1", profile="grok-hi", grade="A+")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("grok-hi", "")
    assert row.effort_inferred is True


# ---------------------------------------------------------------------------
# alias (AC1c)
# ---------------------------------------------------------------------------


def test_legacy_alias_claude_opus(tmp_path, isolated_cache):
    """Legacy spelling -> opus through the alias table; effort inferred from
    the profile default."""
    view = _view(_entry("opus", "high", "S"), _entry("opus", "xhigh", "S+"))
    _seed([_rep("t1", profile="claude-opus", grade="S+")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("opus", "high")
    assert row.resolution == "alias"
    assert row.effort_inferred is True


def test_alias_table_codex_max(tmp_path, isolated_cache):
    """codex-max resolves through recommend.PROFILE_ALIASES to codex-sol —
    the existing alias table, exactly where legacy spellings live."""
    view = _view(_entry("codex-sol", "max", "S+"))
    _seed([_rep("t1", profile="codex-max", effort="max", grade="S+")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("codex-sol", "max")
    assert row.resolution == "alias"


def test_wrk_spelling_alias_with_pin(tmp_path, isolated_cache):
    """`codex` reps measure codex-sol@high — the pin is part of the mapping,
    not the profile default (max)."""
    view = _view(_entry("codex-sol", "high", "C"), _entry("codex-sol", "max", "S+"))
    _seed([_rep("t1", profile="codex", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("codex-sol", "high")
    assert row.row_key == ("codex-sol", "high")
    assert row.resolution == "alias"


def test_grok_spelling_maps_to_grok_hi_not_grok(tmp_path, isolated_cache):
    """`grok` is itself a catalog profile (grok@low), but the launcher
    spelling consults grok-hi — the map wins over the literal name."""
    view = _view(_entry("grok", "low", "B"), _entry("grok-hi", "low", "C"))
    _seed([_rep("t1", profile="grok", effort="low", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("grok-hi", "low")
    assert row.row_key == ("grok-hi", "low")


def test_kimi_k3_low_is_its_own_profile(tmp_path, isolated_cache):
    """Regression: kimi-k3-low is a catalog profile (KIMI_CODE_HOME variant),
    not a rung of kimi-k3 — a rep on it must not judge kimi-k3@low."""
    view = _view(_entry("kimi-k3-low", "", "A"), _entry("kimi-k3", "", "S"))
    _seed([_rep("t1", profile="kimi-k3-low", grade="A")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("kimi-k3-low", "")
    assert row.row_key == ("kimi-k3-low", "")


# ---------------------------------------------------------------------------
# #1026 rollback spellings — resolve to the live rungs, read as mismatches
# ---------------------------------------------------------------------------


def test_rollback_spellings_resolve_and_read_as_model_mismatches(tmp_path, isolated_cache):
    """#1026 AC2: the codex-sol rungs carry gpt-6.1-sol, so the rollback
    spellings' reps (pinned literal gpt-6-sol / gpt-5.6-sol) resolve onto the
    live rungs and report as model mismatches — never unrung, never counted."""
    view = _view(
        _entry("codex-sol", "high", "C", model_id="gpt-6.1-sol"),
        _entry("codex-sol", "max", "S+", model_id="gpt-6.1-sol"),
    )
    _seed(
        [
            _rep("b6", profile="builder-sol6", model_id="gpt-6-sol", grade="S"),
            _rep("s6", profile="codex-sol6", model_id="gpt-6-sol", grade="S"),
            _rep("s56", profile="codex-sol56", model_id="gpt-5.6-sol", grade="S"),
        ]
    )
    proposal = _propose(view)
    assert proposal.unrung == []

    builder_row = _counted(proposal, "b6")
    # builder-sol6 consults the same rung builder-sol does (codex-sol@high).
    assert builder_row.rung == ("codex-sol", "high")
    assert builder_row.resolution == "builder map"
    assert "model mismatch" in builder_row.excluded
    assert "model-mismatch" in builder_row.exclusion_tags

    sol6_row = _counted(proposal, "s6")
    assert sol6_row.rung == ("codex-sol", "max")
    assert sol6_row.resolution == "alias"
    assert "model mismatch" in sol6_row.excluded
    assert "model-mismatch" in sol6_row.exclusion_tags

    # codex-sol56 gets the same treatment: the older Sol generation resolves
    # onto the live rows and mismatches there too.
    sol56_row = _counted(proposal, "s56")
    assert sol56_row.rung == ("codex-sol", "max")
    assert sol56_row.resolution == "alias"
    assert "model mismatch" in sol56_row.excluded


def test_rollback_rep_recorded_effort_still_wins(tmp_path, isolated_cache):
    """A codex-sol6 rep recorded at a named effort resolves that exact rung —
    the pin is the fallback for reps that recorded none."""
    view = _view(
        _entry("codex-sol", "high", "C", model_id="gpt-6.1-sol"),
        _entry("codex-sol", "max", "S+", model_id="gpt-6.1-sol"),
    )
    _seed([_rep("t1", profile="codex-sol6", model_id="gpt-6-sol", effort="high", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("codex-sol", "high")
    assert "model mismatch" in row.excluded


def test_current_model_rep_on_codex_sol_still_counts(tmp_path, isolated_cache):
    """Positive control: a gpt-6.1-sol rep on codex-sol@max counts — only the
    superseded ids read as mismatches."""
    view = _view(_entry("codex-sol", "max", "S+", model_id="gpt-6.1-sol"))
    _seed([_rep("t1", profile="codex-sol", model_id="gpt-6.1-sol", effort="max", grade="S+")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("codex-sol", "max")
    assert row.row_key == ("codex-sol", "max")
    assert row.excluded == ""
    assert row.resolution == "direct"


def test_pre_refresh_haiku45_rep_stays_off_the_haiku55_rungs(tmp_path, isolated_cache):
    """#1269 AC4: the haiku rows carry claude-haiku-5-5, so a pre-refresh
    claude-haiku-4.5 rep resolves to the rung but reads as a model mismatch —
    exact match keeps old-model reps attributed to the old id, never counted."""
    view = _view(
        _entry("haiku", "low", "C", model_id="claude-haiku-5-5"),
        _entry("haiku", "high", "C", model_id="claude-haiku-5-5"),
    )
    _seed(
        [
            _rep("t1", profile="haiku", model_id="claude-haiku-4.5", effort="high", grade="B"),
            _rep("t2", profile="haiku", model_id="claude-haiku-5-5", effort="high", grade="B"),
        ]
    )
    proposal = _propose(view)
    assert proposal.unrung == []

    old_row = _counted(proposal, "t1")
    assert old_row.rung == ("haiku", "high")
    assert old_row.resolution == "direct"
    assert "model mismatch" in old_row.excluded
    assert "model-mismatch" in old_row.exclusion_tags

    # Positive control: the same rung counts a rep recorded on the new id.
    new_row = _counted(proposal, "t2")
    assert new_row.rung == ("haiku", "high")
    assert new_row.excluded == ""
    result = next(r for r in proposal.results if r.key == ("haiku", "high"))
    assert [item.rep.task_ref for item in result.counted] == ["t2"]


# ---------------------------------------------------------------------------
# unrung (AC1d) — reported, never counted
# ---------------------------------------------------------------------------


def test_unknown_spelling_stays_unrung(tmp_path, isolated_cache):
    """An unmapped spelling reports as unrung — never silently attached to a
    lookalike profile."""
    view = _view(_entry("opus", "high", "S"))
    _seed([_rep("t1", profile="orch-mock", effort="high", grade="S")])
    proposal = _propose(view)
    assert not proposal.results
    assert [r.rep.profile for r in proposal.unrung] == ["orch-mock"]
    row = _counted(proposal, "t1")  # row exists in evidence but not in results
    assert row.row_key is None
    assert row.resolution == ""
    text = grades.render_proposal(proposal, view)
    assert "unrung evidence" in text
    assert "no catalog profile for 'orch-mock'" in text


def test_unrung_never_counts_into_results(tmp_path, isolated_cache):
    """Mutant guard: an unrung rep's pass must not promote or hold any rung —
    dropping it into the wrong group would miscount evidence."""
    view = _view(_entry("opus", "high", "S"))
    _seed(
        [
            _rep("t1", profile="opus", effort="high", grade="S+"),
            _rep("t2", profile="opus", effort="high", grade="S+"),
            _rep("t3", profile="claude-code-direct", effort="high", grade="S+"),
        ]
    )
    proposal = _propose(view)
    result = next(r for r in proposal.results if r.key == ("opus", "high"))
    # claude-code-direct names no model — ambiguous, unrung, not counted.
    assert len(result.counted) == 2
    assert all(item.rep.profile != "claude-code-direct" for item in result.counted)
    assert [r.rep.profile for r in proposal.unrung] == ["claude-code-direct"]


# ---------------------------------------------------------------------------
# snapshot stand-ins — the partially seeded canon (AC1d, merged universe)
# ---------------------------------------------------------------------------


def test_uncovered_profile_resolves_via_snapshot_and_is_marked(tmp_path, isolated_cache):
    """The live canon today covers devin-ds41 only. A rep on an uncovered
    profile resolves to the bundled snapshot's reviewed placement — labelled
    [snapshot-placement], never indistinguishable from a canon row."""
    view = _canon_view(_entry("devin-ds41", "", "A+"))
    _seed(
        [
            _rep("t1", profile="opus", effort="high", grade="S+"),
            _rep("t2", profile="opus", effort="high", grade="S+"),
        ]
    )
    proposal = _propose(view)
    keys = _keys(proposal)
    assert ("opus", "high") in keys
    result = next(r for r in proposal.results if r.key == ("opus", "high"))
    assert result.snapshot_row is True
    assert "opus" in proposal.snapshot_profiles
    assert proposal.unrung == []
    text = grades.render_proposal(proposal, view)
    assert "[snapshot-placement]" in text


def test_covered_profile_never_reads_snapshot_rungs(tmp_path, isolated_cache):
    """A profile the canon mentions is canon's to define — snapshot rungs of
    that profile must not leak back into the evaluated universe, and a rep
    recorded at an effort with no canon row is unrung rather than judged by
    the profile's live default or a snapshot stand-in (v1.2)."""
    view = _canon_view(_entry("opus", "high", "S"))  # canon carries only opus@high
    _seed([_rep("t1", profile="opus", effort="low", grade="S")])
    proposal = _propose(view)
    # opus@low has no canon row: unrung — a measured effort counts only on its
    # own rung, never on opus@high and never on a snapshot stand-in.
    assert _keys(proposal) == set()
    row = _counted(proposal, "t1")
    assert row.rung == ("opus", "low")
    assert row.row_key is None
    assert [r.rep.task_ref for r in proposal.unrung] == ["t1"]


def test_empty_canon_evaluates_the_full_snapshot_universe(tmp_path, isolated_cache):
    """A canon that has said nothing cannot hide evidence: every bundled
    rung is eligible."""
    view = _canon_view()
    _seed([_rep("t1", profile="opus", effort="high", grade="S")])
    proposal = _propose(view)
    assert ("opus", "high") in _keys(proposal)
    assert "opus" in proposal.snapshot_profiles


# ---------------------------------------------------------------------------
# printed basis + summary (AC2)
# ---------------------------------------------------------------------------


def test_render_shows_per_rep_resolution_basis_and_summary(tmp_path, isolated_cache):
    """Every counted rep prints how its rung resolved, and the summary counts
    each kind — the operator can audit exactly what was counted on what."""
    view = _view(
        _entry("opus", "high", "S"),
        _entry("opus", "low", "A"),
        _entry("grok-hi", "xhigh", "C"),
    )
    _seed(
        [
            _rep("direct", profile="opus", effort="high", grade="S"),
            _rep("builder", profile="builder-grok", effort="xhigh", grade="S"),
            _rep("default", profile="opus", grade="S"),  # -> opus@high inferred
            _rep("alias", profile="claude-opus", grade="S"),  # alias + inferred -> opus@high
        ]
    )
    proposal = _propose(view)
    text = grades.render_proposal(proposal, view)
    assert "resolved=direct:" in text
    assert "resolved=builder map:" in text
    assert "resolved=default effort:" in text
    assert "resolved=alias:" in text
    assert "resolution basis (4 counted reps)" in text
    assert "direct=1" in text
    assert "builder map=1" in text
    assert "default effort=1" in text
    assert "alias=1" in text
    # builder (pinned) + alias (recorded) are not inferred; direct is not;
    # only the default-effort rep is.
    assert "effort inferred on 2" in text  # opus-default + claude-opus alias


def test_json_carries_resolution_counts(tmp_path, isolated_cache):
    view = _view(_entry("opus", "high", "S"))
    _seed([_rep("t1", profile="opus", grade="S")])
    proposal = _propose(view)
    payload = grades.proposal_to_json(proposal, view)
    assert payload["resolution_counts"] == {"default effort": 1}
    assert payload["effort_inferred"] == 1
    assert payload["snapshot_fill_profiles"] == []


def test_apply_writes_promoted_snapshot_row(tmp_path, isolated_cache, monkeypatch):
    """A promote on a snapshot stand-in must reach the apply artifact —
    push-catalog then writes the row into the canon."""
    canon = _canon_view(_entry("devin-ds41", "", "A+"))
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: canon)
    _seed(
        [
            # The snapshot's sonnet rows carry model claude-sonnet-5-5 — the
            # reps must record that model to count (v1.1 model match).
            _rep("t1", profile="sonnet", model_id="claude-sonnet-5-5", effort="medium", grade="B"),
            _rep("t2", profile="sonnet", model_id="claude-sonnet-5-5", effort="medium", grade="B"),
        ]
    )
    proposal = _propose(canon)
    artifact = {
        "rule_version": grades.RULE_VERSION,
        "min_passes": 2,
        "digest": proposal.digest,
        "results": [r.as_dict() for r in proposal.results if r.action in ("promote", "demote")],
        "params": {"cli_exclusions": []},
    }
    entries, live, _ = grades.apply_proposals(artifact, decided_by="operator:test", deviation_ref="task-750")
    rows = {e.key: e for e in entries}
    # sonnet@medium is a snapshot stand-in (canon covers only devin-ds41): its
    # promote C -> B must reach the artifact with the decision stamps.
    assert rows[("sonnet", "medium")].grade == "B"
    assert rows[("sonnet", "medium")].decided_by == "operator:test"
    assert rows[("sonnet", "medium")].deviation_ref.startswith("task-750")
    assert rows[("devin-ds41", "")].grade == "A+"  # untouched canon row


# ---------------------------------------------------------------------------
# assertion-RED mutants — the checks are actually wired (AC4)
# ---------------------------------------------------------------------------


def test_mutant_inverted_inferred_flag_would_fail(tmp_path, isolated_cache):
    """If effort inference stopped being flagged, this test is assertion-RED:
    the exact bool must be True here and False on the pinned sibling."""
    view = _view(_entry("opus", "high", "S"), _entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("inferred", profile="opus", grade="S"),
            _rep("pinned", profile="builder-grok", effort="xhigh", grade="S"),
        ]
    )
    proposal = _propose(view)
    assert _counted(proposal, "inferred").effort_inferred is True
    assert _counted(proposal, "pinned").effort_inferred is False


def test_mutant_wrong_base_or_effort_would_fail(tmp_path, isolated_cache):
    """Each mapping asserts profile AND effort — a mutant sliding either
    (builder-grok -> grok@xhigh, or -> grok-hi@high) fails the exact pair."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed([_rep("t1", profile="builder-grok", effort="xhigh", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("grok-hi", "xhigh")
    assert row.rung != ("grok", "xhigh")
    assert row.rung != ("grok-hi", "high")


def test_mutant_builder_map_kind_would_fail(tmp_path, isolated_cache):
    """Kind labels are asserted verbatim: renaming 'builder map' or counting
    the rep as 'direct' fails this test."""
    view = _view(_entry("devin-swe2-max", "", "A+"))
    _seed([_rep("t1", profile="builder-devin-max", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.resolution == "builder map"
    assert row.resolution != "direct"
    text = grades.render_proposal(proposal, view)
    assert "builder map=1" in text


# ---------------------------------------------------------------------------
# same-run collapse — one measured run under two spellings counts once
# ---------------------------------------------------------------------------


def _remote_rep_row(server_id: int, **fields) -> bench.RepRecord:
    base = dict(
        id=server_id,
        profile="codex",
        model_id="gpt-5-sol",
        task_ref="dup-run",
        tier="T1",
        role="impl",
        rounds=1,
        blockers_found=0,
        completed=1,
        input_tokens=None,
        output_tokens=None,
        notes=None,
        recorded_at="2026-09-20T10:00:00Z",
        effort=None,
        grade="A",
        table_grade=None,
    )
    base.update(fields)
    return bench.RepRecord(**base)


def test_alias_canonical_same_run_counts_once(tmp_path, monkeypatch, isolated_cache):
    """srv stored the run as 'codex', local stored it as 'codex-sol' — same
    model, task, instant and outcome under two spellings. Raw dedup keys on
    the recorded spelling, so only the resolved-rung layer can collapse
    them: the row carrying the recorded effort is kept, the derived one is
    excluded with the reason."""
    from test_grade_proposals import _remote_backend, _remote_row

    local = bench.add_rep(
        **_rep("dup-run", profile="codex-sol", model_id="gpt-5-sol", effort="high", grade="A")
    )
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps = [
        _remote_row(_remote_rep_row(1200, profile="codex", effort=None), 1200, host=None),
    ]
    view = _view(_entry("codex-sol", "high", "C", model_id="gpt-5-sol"))
    proposal = _propose(view)
    counted = proposal.evidence.counted
    # The local row recorded effort=high — it measured the rung; the server
    # row only reached it through the 'codex' spelling pin.
    assert [r.ref for r in counted] == [f"local:{local.id}"]
    loser = next(r for r in proposal.evidence.rows if r.ref == "srv:1200")
    assert loser.excluded
    assert "alias-duplicate" in loser.excluded and f"local:{local.id}" in loser.excluded
    # And the survivor cannot drive a promotion alone (min_passes=2).
    from test_grade_proposals import _result

    assert _result(proposal, "codex-sol", "high").action != "promote"


def test_same_run_different_rung_keeps_recorded_effort(tmp_path, monkeypatch, isolated_cache):
    """Same identity, conflicting rung claims: the row whose effort was
    recorded — not inferred — is the one that measured the run."""
    from test_grade_proposals import _remote_backend, _remote_row

    local = bench.add_rep(
        **_rep("dup-rung", profile="codex-sol", model_id="gpt-5-sol", effort="high", grade="A")
    )
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps = [
        _remote_row(
            _remote_rep_row(1201, profile="codex", effort="low", task_ref="dup-rung"),
            1201,
            host=None,
        ),
    ]
    view = _view(
        _entry("codex-sol", "high", "C", model_id="gpt-5-sol"),
        _entry("codex-sol", "low", "C", model_id="gpt-5-sol"),
    )
    proposal = _propose(view)
    counted = proposal.evidence.counted
    assert [r.ref for r in counted] == ["srv:1201"]
    assert counted[0].rung == ("codex-sol", "low")
    loser = next(r for r in proposal.evidence.rows if r.ref == f"local:{local.id}")
    assert "duplicate" in loser.excluded and "codex-sol@low" in loser.excluded


def test_recorded_effort_beats_spelling_pin_in_collapse(tmp_path, monkeypatch, isolated_cache):
    """Tester r2: a rep-recorded effort measured the rung; a spelling pin only
    says which rung the launcher consulted. srv 'codex' effort=- pins to high;
    local 'codex-sol@max' recorded max — the recorded row wins even though the
    pin lands on the server."""
    from test_grade_proposals import _remote_backend, _remote_row

    local = bench.add_rep(
        **_rep("dup-pin", profile="codex-sol", model_id="gpt-5-sol", effort="max", grade="A")
    )
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps = [
        _remote_row(
            _remote_rep_row(1600, profile="codex", effort=None, task_ref="dup-pin"),
            1600,
            host=None,
        ),
    ]
    view = _view(
        _entry("codex-sol", "high", "C", model_id="gpt-5-sol"),
        _entry("codex-sol", "max", "C", model_id="gpt-5-sol"),
    )
    proposal = _propose(view)
    counted = proposal.evidence.counted
    assert [r.ref for r in counted] == [f"local:{local.id}"]
    assert counted[0].rung == ("codex-sol", "max")
    loser = next(r for r in proposal.evidence.rows if r.ref == "srv:1600")
    assert "duplicate" in loser.excluded and "codex-sol@max" in loser.excluded


def test_missing_model_id_never_collapses(tmp_path, monkeypatch, isolated_cache):
    """model_id is optional — None == None is not identity. Two rows with
    different spellings and no model must never merge on task+instant.
    (add_rep requires a model, so the pair arrives as server rows, whose
    schema tolerates the empty field.) Rule v1.1 adds the sharper check:
    a rep with no recorded model can never prove it ran the rung's model —
    both rows are excluded as unmatchable, still two distinct rows."""
    from test_grade_proposals import _remote_backend, _remote_row

    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps = [
        _remote_row(
            _remote_rep_row(1701, profile="opus", model_id=None, effort="high", task_ref="run-x"),
            1701,
            host=None,
        ),
        _remote_row(
            _remote_rep_row(1702, profile="codex-sol", model_id=None, effort="high", task_ref="run-x"),
            1702,
            host=None,
        ),
    ]
    view = _view(
        _entry("opus", "high", "C"),
        _entry("codex-sol", "high", "C"),
    )
    proposal = _propose(view)
    rows = [r for r in proposal.evidence.rows if r.rep.task_ref == "run-x"]
    assert len(rows) == 2  # both tracked — never merged
    assert proposal.evidence.counted == []
    assert {r.rung for r in rows} == {("opus", "high"), ("codex-sol", "high")}
    assert all("model not recorded" in r.excluded for r in rows)


def test_same_run_key_requires_model_id():
    """None == None is not model identity — never collapse on it."""
    assert grades._same_run_key(_remote_rep_row(9, model_id=None)) is None
    assert grades._same_run_key(_remote_rep_row(9, model_id="m")) is not None


def test_different_runs_same_task_aggregate_on_a_rung(tmp_path, isolated_cache):
    """v1.1 AC2: identical spelling+task on the same rung at different
    instants are re-runs of one task — only the latest recorded rep counts;
    the earlier one is excluded with the printed rule."""
    _seed(
        [
            _rep("run-a", profile="codex-sol", effort="high", grade="A"),
            _rep(
                "run-a",
                profile="codex-sol",
                effort="high",
                grade="A",
                recorded_at="2026-09-20T11:00:00Z",
            ),
        ]
    )
    view = _view(_entry("codex-sol", "high", "C"))
    proposal = _propose(view)
    counted = proposal.evidence.counted
    assert len(counted) == 1
    assert counted[0].rep.recorded_at == "2026-09-20T11:00:00Z"
    loser = next(r for r in proposal.evidence.rows if r.rep.recorded_at == "2026-09-20T10:00:00Z")
    assert "same task 'run-a'" in loser.excluded and "final rep" in loser.excluded


def test_same_task_different_rung_keeps_both(tmp_path, isolated_cache):
    """Guard against over-merge: the same task measured on two different
    rungs (two distinct runs) is two pieces of evidence — aggregation is
    per (task, rung)."""
    _seed(
        [
            _rep("run-a", profile="opus", effort="high", grade="A"),
            _rep(
                "run-a",
                profile="opus",
                effort="xhigh",
                grade="A",
                recorded_at="2026-09-20T11:00:00Z",
            ),
        ]
    )
    view = _view(_entry("opus", "high", "C"), _entry("opus", "xhigh", "C"))
    proposal = _propose(view)
    assert len(proposal.evidence.counted) == 2
    assert {r.row_key for r in proposal.evidence.counted} == {("opus", "high"), ("opus", "xhigh")}


def test_same_run_key_requires_task_ref():
    """task_ref empty -> no same-run key; timestamp alone never merges.
    (add_rep itself requires a task_ref — this guards remote rows, whose
    schema tolerates the empty field.)"""
    rep = _remote_rep_row(9, task_ref=None)
    assert grades._same_run_key(rep) is None
    assert grades._same_run_key(_remote_rep_row(9, task_ref="t")) is not None


# ---------------------------------------------------------------------------
# render: every counted rep prints its resolution basis (AC2, incl. changes)
# ---------------------------------------------------------------------------


def test_changed_rung_renders_every_counted_rep(tmp_path, isolated_cache):
    """A promoted rung previously printed only the refs that drove the rule —
    the other counted reps (e.g. a FAIL above the placement) went silent.
    Every counted rep must print resolved=."""
    _seed(
        [
            _rep("p1", profile="codex-sol", effort="high", grade="A"),
            _rep("p2", profile="codex-sol", effort="high", grade="A"),
            _rep("p3", profile="codex-sol", effort="high", grade="A"),
            _rep("below-target", profile="codex-sol", effort="high", grade="B"),
        ]
    )
    view = _view(_entry("codex-sol", "high", "C"))
    proposal = _propose(view)
    result = next(r for r in proposal.results if r.key == ("codex-sol", "high"))
    assert result.action == "promote"
    text = grades.render_proposal(proposal, view)
    counted = proposal.evidence.counted
    assert len(counted) == 4
    assert text.count("resolved=") == len(counted)
    assert "evidence local:" in text
    for item in counted:
        assert f"evidence {item.ref}" in text


def test_mutant_evidence_refs_filter_would_fail(tmp_path, isolated_cache):
    """assertion-RED: restoring the old evidence_refs filter leaves a counted
    rep unprinted — this exact-count assertion goes red."""
    _seed(
        [
            _rep("p1", profile="codex-sol", effort="high", grade="A"),
            _rep("p2", profile="codex-sol", effort="high", grade="A"),
            _rep("silent", profile="codex-sol", effort="high", grade="B"),
        ]
    )
    view = _view(_entry("codex-sol", "high", "C"))
    proposal = _propose(view)
    text = grades.render_proposal(proposal, view)
    silent = _counted(proposal, "silent")
    assert f"evidence {silent.ref}" in text
    assert text.count("resolved=") == len(proposal.evidence.counted)


def test_mutant_alias_dedup_would_fail(tmp_path, monkeypatch, isolated_cache):
    """assertion-RED: removing the collapse makes the same-run pair count
    twice — len==1 fails."""
    from test_grade_proposals import _remote_backend, _remote_row

    bench.add_rep(**_rep("dup-run", profile="codex-sol", model_id="gpt-5-sol", effort="high", grade="A"))
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps = [
        _remote_row(_remote_rep_row(1202, profile="codex", effort=None), 1202, host=None),
    ]
    view = _view(_entry("codex-sol", "high", "C", model_id="gpt-5-sol"))
    proposal = _propose(view)
    assert len(proposal.evidence.counted) == 1


# ---------------------------------------------------------------------------
# effort exactness (task #777 / rule v1.2) — a rep counts on its measured rung
# only; the resolved (profile, effort) is judged by the exact-effort catalog
# row or nothing. Affected paths: direct (recorded effort), builder map and
# alias (the pin, or a recorded effort overriding it). Unaffected: a rep with
# no effort at all still lands on the profile default via ``effort inferred``.
# ---------------------------------------------------------------------------


def test_recorded_max_reps_never_promote_the_medium_rung(tmp_path, isolated_cache):
    """The live regression (hk:doc 5177, #777 AC3, direct path): reps recorded
    codex-terra@max must not promote codex-terra@medium — a measured effort
    with no exact-effort row is unrung, never judged-by-medium."""
    view = _view(_entry("codex-terra", "medium", "A"), _entry("codex-terra", "high", "A+"))
    _seed(
        [
            _rep("t1", profile="codex-terra", effort="max", grade="S"),
            _rep("t2", profile="codex-terra", effort="max", grade="S"),
            _rep("t3", profile="codex-terra", effort="max", grade="S"),
        ]
    )
    proposal = _propose(view)
    assert _keys(proposal) == set()  # medium keeps its A — zero counted evidence
    assert {r.rep.task_ref for r in proposal.unrung} == {"t1", "t2", "t3"}
    row = _counted(proposal, "t1")
    assert row.rung == ("codex-terra", "max")
    assert row.row_key is None
    assert "counts only on that effort's rung" in row.resolution_detail
    text = grades.render_proposal(proposal, view)
    assert "judged-by" not in text  # the rung-effort=max->judged-by-medium print is gone


def test_builder_pin_unrung_without_exact_effort_row(tmp_path, isolated_cache):
    """Builder-map path: builder-terra-max pins codex-terra@max — with no max
    row the rep is unrung; the pin cannot borrow the medium default."""
    view = _view(_entry("codex-terra", "medium", "A"), _entry("codex-terra", "high", "A+"))
    _seed([_rep("t1", profile="builder-terra-max", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("codex-terra", "max")
    assert row.row_key is None
    assert [r.rep.task_ref for r in proposal.unrung] == ["t1"]


def test_builder_pin_recorded_effort_override_unrung(tmp_path, isolated_cache):
    """Builder-map + recorded override: a builder-terra-max rep that recorded
    effort=xhigh measured xhigh — unrung without an xhigh row, not adopted by
    the pinned max's sibling rungs."""
    view = _view(_entry("codex-terra", "medium", "A"), _entry("codex-terra", "max", "C"))
    _seed([_rep("t1", profile="builder-terra-max", effort="xhigh", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("codex-terra", "xhigh")
    assert row.row_key is None


def test_alias_recorded_effort_unrung_without_exact_row(tmp_path, isolated_cache):
    """Alias path: codex-med aliases to codex-terra@medium — a rep recorded at
    xhigh on that spelling measured xhigh, and with no xhigh row it is unrung:
    the alias's medium pin cannot adopt it."""
    view = _view(_entry("codex-terra", "medium", "A"))
    _seed([_rep("t1", profile="codex-med", effort="xhigh", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("codex-terra", "xhigh")
    assert row.row_key is None
    assert [r.rep.task_ref for r in proposal.unrung] == ["t1"]


def test_profile_alias_recorded_effort_unrung(tmp_path, isolated_cache):
    """PROFILE_ALIASES path: codex-max folds to codex-sol — a rep recorded at
    effort=max on that spelling is unrung when only codex-sol@high exists."""
    view = _view(_entry("codex-sol", "high", "C"))
    _seed([_rep("t1", profile="codex-max", effort="max", grade="S")])
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("codex-sol", "max")
    assert row.row_key is None


def test_effort_less_rung_judges_only_effort_less_reps(tmp_path, isolated_cache):
    """Effort-less rungs are explicit: a rep recorded at effort=max on an
    effort-less profile measured a rung that does not exist and is unrung —
    while a rep that recorded no effort still lands on the "" row flagged
    'effort inferred'."""
    view = _view(_entry("codex-terra-max", "", "S"))
    _seed(
        [
            _rep("measured", profile="codex-terra-max", effort="max", grade="S"),
            _rep("quiet", profile="codex-terra-max", grade="A+"),
        ]
    )
    proposal = _propose(view)
    measured = _counted(proposal, "measured")
    assert measured.rung == ("codex-terra-max", "max")
    assert measured.row_key is None
    quiet = _counted(proposal, "quiet")
    assert quiet.rung == ("codex-terra-max", "")
    assert quiet.row_key == ("codex-terra-max", "")
    assert quiet.effort_inferred is True


def test_inferred_effort_still_lands_on_profile_default(tmp_path, isolated_cache):
    """Unaffected path: an effort-less rep still lands on the profile's
    default rung marked 'effort inferred' — inference IS the resolved rung,
    so an exact-effort row exists by construction."""
    view = _view(_entry("opus", "medium", "A"), _entry("opus", "high", "S"))
    _seed([_rep("t1", profile="opus", grade="S")])  # no effort recorded
    proposal = _propose(view)
    row = _counted(proposal, "t1")
    assert row.rung == ("opus", "high")  # launcher's default for opus
    assert row.row_key == ("opus", "high")
    assert row.effort_inferred is True
    assert row.resolution == "default effort"


def test_mutant_exact_effort_boundary_would_fail(tmp_path, isolated_cache):
    """assertion-RED: same effort counts, adjacent effort doesn't — a mutant
    that re-admits a different-effort row flips exactly one of the two
    assertions."""
    view = _view(_entry("codex-terra", "medium", "A"), _entry("codex-terra", "max", "C"))
    _seed(
        [
            _rep("kept", profile="codex-terra", effort="max", grade="S"),
            _rep("lost", profile="codex-terra", effort="high", grade="S"),
        ]
    )
    proposal = _propose(view)
    assert _counted(proposal, "kept").row_key == ("codex-terra", "max")
    lost = _counted(proposal, "lost")
    assert lost.rung == ("codex-terra", "high")
    assert lost.row_key is None
    assert [r.rep.task_ref for r in proposal.unrung] == ["lost"]


def test_mutant_cross_effort_fallback_would_promote_medium(tmp_path, isolated_cache):
    """assertion-RED: restoring judged-by-default fallback re-counts max
    evidence on the medium rung — the 'no result' and unrung assertions both
    go red."""
    view = _view(_entry("codex-terra", "medium", "C"), _entry("codex-terra", "high", "A+"))
    _seed(
        [
            _rep("t1", profile="codex-terra", effort="max", grade="S"),
            _rep("t2", profile="codex-terra", effort="max", grade="S"),
        ]
    )
    proposal = _propose(view)
    assert ("codex-terra", "medium") not in _keys(proposal)  # fallback would produce it
    assert _keys(proposal) == set()
    assert {r.rep.task_ref for r in proposal.unrung} == {"t1", "t2"}
