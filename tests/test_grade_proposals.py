"""task #735 — measured-rep grade promotion/demotion proposal tests.

Every rule in ``scopefuel.grades`` is pinned both directions: promotion needs
two *clean graded* passes (an ungraded pass, a pass-with-blockers, or one pass
alone never promotes), demotion needs FAIL evidence at-or-below the placement
(and conflicts with a measured pass body at the placement), superseded reps
never count twice, and missing exclusion targets are reported, never silent.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse

import pytest

from scopefuel import bench, cli, grades

HOST = "test-host"


def _rep(task_ref: str, **overrides) -> dict:
    row = {
        "profile": "builder-grok",
        "model_id": "grok-4.7",
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
        # Fixture reps record model_id="grok-4.7" — the rung's catalog model
        # defaults to the same so v1.1 model-match admits them; tests probing
        # the match itself override either side explicitly.
        "model_id": "grok-4.7",
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


def _canon_view(*entries: bench.CatalogEntry, source: str = bench.CATALOG_SOURCE_SERVER) -> bench.CatalogView:
    """A healthy canon read — the view apply accepts without an override."""
    return bench.CatalogView(
        entries=tuple(entries),
        source=source,
        backend=bench.BENCH_BACKEND_HANDOFFKEEP,
        reason="configured",
        age_s=0.0,
    )


def _propose(view, *, min_passes: int = 2, exclusions=(), host: str = HOST) -> grades.Proposal:
    evidence = grades.gather_reps(view=view, exclusions=list(exclusions), host=host)
    return grades.evaluate(evidence, view, min_passes=min_passes)


def _result(proposal: grades.Proposal, profile: str, effort: str = "") -> grades.RungResult:
    return next(r for r in proposal.results if r.key == (profile, effort))


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def test_promote_two_clean_passes(tmp_path, isolated_cache):
    """Two clean PASSes at grade A+ promote the rung C -> A+."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A+"),
            _rep("t2", effort="xhigh", grade="A+"),
            _rep("t3", effort="xhigh", grade="A"),
        ]
    )
    proposal = _propose(view)
    result = _result(proposal, "grok-hi", "xhigh")
    assert result.action == "promote"
    assert result.row.grade == "C"
    assert result.target == "A+"
    assert len(result.evidence_refs) == 2
    assert all(ref.startswith("local:") for ref in result.evidence_refs)


def test_promote_needs_min_passes_not_one(tmp_path, isolated_cache):
    """Mutant guard: a single clean PASS must not promote (threshold is N=2)."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed([_rep("t1", effort="xhigh", grade="A+")])
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "insufficient"
    assert result.target == "C"


def test_promote_picks_highest_qualifying_grade(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A"),
            _rep("t2", effort="xhigh", grade="A"),
            _rep("t3", effort="xhigh", grade="A+"),
            _rep("t4", effort="xhigh", grade="A+"),
            _rep("t5", effort="xhigh", grade="S"),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    # Only A+ has >=2 graded passes — one S pass alone cannot promote to S.
    assert result.action == "promote"
    assert result.target == "A+"


def test_ungraded_pass_never_promotes(tmp_path, isolated_cache):
    """Mutant guard: ungraded reps show but cannot establish a grade claim."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed([_rep(f"t{i}", effort="xhigh") for i in range(3)])
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "insufficient"
    assert len(result.ungraded_passes) == 3
    assert result.passes_at == {}


def test_pass_with_blockers_never_promotes(tmp_path, isolated_cache):
    """Mutant guard: completed-with-blockers is not a clean PASS."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A+", blockers_found=2),
            _rep("t2", effort="xhigh", grade="A+", blockers_found=1),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "insufficient"
    assert len(result.unclean_passes) == 2


def test_promote_does_not_move_when_evidence_below_current(tmp_path, isolated_cache):
    """Passes at a grade at-or-below the placement confirm, never demote."""
    view = _view(_entry("grok-hi", "xhigh", "S"))
    _seed([_rep(f"t{i}", effort="xhigh", grade="A") for i in range(2)])
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "hold"
    assert result.target == "S"


# ---------------------------------------------------------------------------
# Demotion
# ---------------------------------------------------------------------------


def test_single_capout_at_placement_blocks_not_demotes(tmp_path, isolated_cache):
    """Rule v1: one cap-out FAIL at placement is one short of demotion —
    it blocks promotion instead."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A", completed=1),  # 1 clean A pass (< 2)
            _rep("t2", effort="xhigh", grade="A+", completed=0, blockers_found=1),  # FAIL at placement
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "blocked"
    assert result.target == "A+"
    assert "demotion needs" in result.note


def test_demote_two_fails_at_placement(tmp_path, isolated_cache):
    """Two FAIL reps at-or-below the placement demote one step below the
    weakest failed grade."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A+", completed=0, blockers_found=1),
            _rep("t2", effort="xhigh", grade="A", completed=0, blockers_found=1),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"
    assert result.target == "B"  # one below the weakest failed claim (A)


def test_demote_below_placement_drops_below_failed_grade(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "xhigh", "S"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="B", completed=0, blockers_found=1),
            _rep("t2", effort="xhigh", grade="A", completed=0, blockers_found=1),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"
    assert result.target == "C"  # can't do B work -> below B


def test_demote_ungraded_fails_step_below_current(tmp_path, isolated_cache):
    """Ungraded FAILs are fail-closed: they count at the placement, and two
    of them demote one step below it."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed(
        [
            _rep("t1", effort="xhigh", completed=0, blockers_found=1),
            _rep("t2", effort="xhigh", completed=0, blockers_found=1),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"
    assert result.target == "A"


def test_demote_floors_at_c(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="C", completed=0, blockers_found=1),
            _rep("t2", effort="xhigh", completed=0, blockers_found=1),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"
    assert result.target == "C"
    assert "floor" in result.note
    assert _propose(view).changes() == []


def test_overreach_fail_blocks_promote_never_demotes(tmp_path, isolated_cache):
    """Mutant guard: a FAIL above the placement is overreach, not a demote —
    but it must still block promotion."""
    view = _view(_entry("grok-hi", "xhigh", "A"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A+", completed=1),
            _rep("t2", effort="xhigh", grade="A+", completed=1),
            _rep("t3", effort="xhigh", grade="S", completed=0, blockers_found=1),  # overreach fail
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "blocked"
    assert result.target == "A"


def test_conflict_holds_when_at_grade_passes_exist(tmp_path, isolated_cache):
    """A demote trigger plus >=N clean passes at the placement = conflicted."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A+"),
            _rep("t2", effort="xhigh", grade="A+"),
            _rep("t3", effort="xhigh", grade="A+", completed=0, blockers_found=1),
            _rep("t4", effort="xhigh", grade="A", completed=0, blockers_found=1),  # 2nd demote-grade fail
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "conflicted"
    assert result.target == "A+"
    assert _propose(view).changes() == []


def test_single_marker_fail_still_conflicts(tmp_path, isolated_cache):
    """One post-merge marker at-or-below is a full demote trigger on its own —
    the conflict rule still applies against measured at-grade passes."""
    view = _view(_entry("grok-hi", "xhigh", "A"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A"),
            _rep("t2", effort="xhigh", grade="A"),
            _rep("t3", effort="xhigh", grade="B", completed=0, notes="[rollback] merge"),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "conflicted"
    assert result.target == "A"
    assert _propose(view).changes() == []


def test_rollback_marker_is_fail_evidence(tmp_path, isolated_cache):
    """A [rollback] notes marker is FAIL evidence even on a completed rep —
    and under rule v1 one marker at-or-below demotes alone."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed([_rep("t1", effort="xhigh", completed=1, notes="merged then reverted [rollback]")])
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"
    assert result.evidence_refs


def test_post_merge_blocker_marker_is_fail(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed([_rep("t1", effort="xhigh", completed=1, notes="[post-merge-blocker] found by ops")])
    assert _result(_propose(view), "grok-hi", "xhigh").action == "demote"


def test_marker_fail_above_placement_blocks_not_demotes(tmp_path, isolated_cache):
    """Rule v1 scoping: a marker FAIL on a task *above* the placement is
    overreach evidence — it blocks promotion but does not demote, because
    the placement never claimed that level."""
    view = _view(_entry("grok-hi", "xhigh", "A"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="B"),
            _rep("t2", effort="xhigh", grade="S", completed=0, notes="[rollback]"),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "blocked"
    assert result.target == "A"


def test_one_at_below_plus_one_above_fail_not_demote(tmp_path, isolated_cache):
    """Counting trap: one at-or-below FAIL plus one above-placement FAIL must
    not add up to a two-FAIL demotion."""
    view = _view(_entry("grok-hi", "xhigh", "A"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="B", completed=0, blockers_found=1),
            _rep("t2", effort="xhigh", grade="S", completed=0, blockers_found=1),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "blocked"
    assert result.target == "A"


# ---------------------------------------------------------------------------
# Duplicates — static pairs, CLI pairs, notes-declared supersedes
# ---------------------------------------------------------------------------


def test_static_supersede_pair_excludes_old_rep(tmp_path, isolated_cache):
    """993 superseded by 995: without exclusion the pair counts as two."""
    view = _view(_entry("codex-sol", "high", "C"))
    rows = [
        _rep("t1", profile="builder-sol-high", effort="high", grade="S"),
        _rep("t2", profile="builder-sol-high", effort="high", grade="S"),
    ]
    _seed(rows)
    # Local ids are 1 and 2 on a fresh store — rename to the cited pair via a
    # CLI exclusion to exercise the same path the static list drives.
    proposal = _propose(view, exclusions=[("local:1", "local:2")])
    result = _result(proposal, "codex-sol", "high")
    assert result.action == "insufficient"  # one pass left — never double-counted
    cli_ex = next(e for e in proposal.evidence.exclusions if e.origin == "cli")
    assert cli_ex.old_refs == ("local:1",) and cli_ex.new_ref == "local:2"


def test_notes_supersede_excludes_cited_id(tmp_path, isolated_cache):
    """A rep's own ``supersedes id=N`` note excludes the cited row."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A+"),  # local:1
            _rep("t1-r2", effort="xhigh", grade="A+", notes="supersedes id=1"),  # local:2
        ]
    )
    proposal = _propose(view)
    result = _result(proposal, "grok-hi", "xhigh")
    assert result.action == "insufficient"  # only the survivor counts
    excluded = {item.ref for item in result.excluded}
    assert "local:1" in excluded


def test_missing_exclusion_target_is_reported(tmp_path, isolated_cache):
    """AC3/mutant: an exclusion naming a rep that is not in evidence shows up."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed([_rep("t1", effort="xhigh", grade="A")])
    proposal = _propose(view, exclusions=[("local:999", "local:1")])
    text = grades.render_proposal(proposal, view)
    assert "local:999" in text
    assert "not in evidence" in text
    assert "missing exclusion targets" in text


def test_host_qualified_exclusion_only_binds_on_that_host(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A+"),
            _rep("t2", effort="xhigh", grade="A+"),
        ]
    )
    proposal = _propose(view, exclusions=[("local@desktop:1", "local@desktop:2")], host="mbp")
    result = _result(proposal, "grok-hi", "xhigh")
    # The pair binds only on host "desktop" — here both reps still count.
    assert result.action == "promote"
    cli_ex = next(e for e in proposal.evidence.exclusions if e.origin == "cli")
    assert "unresolvable" in cli_ex.status({r.ref for r in proposal.evidence.rows})
    # On the desktop host the same pair does exclude local:1.
    proposal2 = _propose(view, exclusions=[("local@desktop:1", "local@desktop:2")], host="desktop")
    assert _result(proposal2, "grok-hi", "xhigh").action == "insufficient"


def test_known_static_pairs_present_in_output(tmp_path, isolated_cache):
    """The operator-named pairs are listed with hit/miss status each run."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed([_rep("t1", effort="xhigh", grade="A")])
    text = grades.render_proposal(_propose(view), view)
    assert "993" in text and "995" in text
    assert "996" in text and "997" in text
    assert "home-desktop:80" in text


# ---------------------------------------------------------------------------
# Rung mapping / missing catalog rows
# ---------------------------------------------------------------------------


def test_rep_effort_wins_over_spelling_pin(tmp_path, isolated_cache):
    """builder-grok pins xhigh, but a rep recorded at effort=high measured
    high — the recorded effort still wins, and with no grok-hi@high row the
    rep is unrung, never re-judged by the profile's other rungs (v1.2)."""
    view = _view(_entry("grok-hi", "", "S"), _entry("grok-hi", "xhigh", "C"))
    _seed([_rep("t1", effort="high", grade="S")])
    proposal = _propose(view)
    assert not proposal.results
    row = next(r for r in proposal.evidence.rows if r.rep.task_ref == "t1")
    assert row.rung == ("grok-hi", "high")  # recorded effort wins over the xhigh pin
    assert row.row_key is None
    assert [r.rep.task_ref for r in proposal.unrung] == ["t1"]


def test_spelling_pin_used_when_effort_unrecorded(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "", "S"), _entry("grok-hi", "xhigh", "C"))
    _seed([_rep("t1", grade="S")])  # builder-grok, no effort -> pin xhigh
    proposal = _propose(view)
    assert _result(proposal, "grok-hi", "xhigh").counted
    assert all(r.key != ("grok-hi", "") for r in proposal.results)


def test_builder_sol_pins_the_high_rung(tmp_path, isolated_cache):
    """builder-sol consults codex-sol@high — wrk's catalog pin (decision
    4088B), not the profile's max default. An effort the rep recorded would
    still win over the pin."""
    view = _view(_entry("codex-sol", "max", "S+"), _entry("codex-sol", "high", "C"))
    _seed([_rep("t1", profile="builder-sol", grade="S")])
    proposal = _propose(view)
    keys = {r.key for r in proposal.results}
    assert ("codex-sol", "high") in keys
    assert ("codex-sol", "max") not in keys
    row = next(r for r in proposal.evidence.rows if r.rep.task_ref == "t1")
    assert row.resolution == "builder map" and not row.effort_inferred


def test_unrung_reps_are_reported_not_counted(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A+"),
            _rep("t2", effort="xhigh", grade="A+"),
            _rep("t3", profile="no-such-profile", grade="S"),
        ]
    )
    proposal = _propose(view)
    assert [r.rep.task_ref for r in proposal.unrung] == ["t3"]
    text = grades.render_proposal(proposal, view)
    assert "unrung evidence" in text and "no-such-profile" not in text.split("unrung evidence")[0]


def test_retired_row_never_proposed(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "xhigh", "C", retired_at="2026-09-01T00:00:00Z"))
    _seed([_rep("t1", effort="xhigh", grade="A"), _rep("t2", effort="xhigh", grade="A")])
    proposal = _propose(view)
    # The retired row is not a proposal target; its reps surface as unrung.
    assert all(r.key != ("grok-hi", "xhigh") for r in proposal.results)
    assert len(proposal.unrung) == 2


# ---------------------------------------------------------------------------
# CLI: read-only propose, sources disclosure, apply safety
# ---------------------------------------------------------------------------


def _cli_view(monkeypatch, entries) -> bench.CatalogView:
    view = _view(*entries)
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: view)
    return view


def _cli_canon_view(monkeypatch, entries, *, source: str = bench.CATALOG_SOURCE_SERVER) -> bench.CatalogView:
    view = _canon_view(*entries, source=source)
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: view)
    return view


def test_cli_propose_is_read_only(tmp_path, monkeypatch, capsys):
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    db = bench.db_path()
    before = sqlite3.connect(db).execute("SELECT * FROM reps ORDER BY id").fetchall()
    assert cli.main(["grades", "propose"]) == 0
    after = sqlite3.connect(db).execute("SELECT * FROM reps ORDER BY id").fetchall()
    assert before == after
    out = capsys.readouterr().out
    assert "promote grok-hi@xhigh C -> A+" in out
    assert "local:1" in out and "local:2" in out  # evidence ids print


def test_cli_propose_rung_focus(tmp_path, monkeypatch, capsys):
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    _seed([_rep("700", effort="xhigh")])
    assert cli.main(["grades", "propose", "--rung", "grok-hi@xhigh"]) == 0
    out = capsys.readouterr().out
    assert "focused rung" in out
    assert "local:1 task=700" in out
    assert "insufficient" in out


def test_cli_propose_json_artifact(tmp_path, monkeypatch, capsys):
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = tmp_path / "proposal.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    payload = json.loads(artifact.read_text())
    assert payload["min_passes"] == 2
    assert payload["digest"]
    results = {(r["profile"], r["effort"]): r for r in payload["results"]}
    assert results[("grok-hi", "xhigh")]["action"] == "promote"
    assert sorted(results[("grok-hi", "xhigh")]["evidence"]) == ["local:1", "local:2"]


def test_cli_apply_writes_catalog_and_stamps(tmp_path, monkeypatch, capsys):
    _cli_canon_view(
        monkeypatch,
        [_entry("grok-hi", "xhigh", "C"), _entry("opus", "", "S")],
    )
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = tmp_path / "proposal.json"
    out_file = tmp_path / "catalog.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    assert (
        cli.main(
            [
                "grades",
                "apply",
                "--proposal",
                str(artifact),
                "--out",
                str(out_file),
                "--decided-by",
                "operator:test",
            ]
        )
        == 0
    )
    payload = json.loads(out_file.read_text())
    # "catalog" is the pushable delta — changed rows only, all stamped.
    rows = {(r["profile"], r["effort"]): r for r in payload["catalog"]}
    changed = rows[("grok-hi", "xhigh")]
    assert len(rows) == 1
    assert changed["grade"] == "A+"
    assert changed["decided_by"] == "operator:test"
    assert "local:1" in changed["deviation_ref"] and "local:2" in changed["deviation_ref"]
    assert changed["decided_at"]
    # The full post-apply catalog rides along for the audit record; rows
    # without evidence are untouched.
    snap = {(r["profile"], r["effort"]): r for r in payload["snapshot"]}
    assert snap[("opus", "")]["grade"] == "S"
    assert snap[("opus", "")]["decided_by"] is None
    assert snap[("grok-hi", "xhigh")]["grade"] == "A+"
    assert "degraded_override" not in payload


def test_cli_apply_refuses_stale_proposal(tmp_path, monkeypatch, capsys):
    """apply can never write without the matching propose evidence: a rep
    recorded after propose makes the artifact stale and apply refuses."""
    _cli_canon_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = tmp_path / "proposal.json"
    out_file = tmp_path / "catalog.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    _seed([_rep("t3", effort="xhigh", grade="B")])  # evidence moved
    assert (
        cli.main(
            [
                "grades",
                "apply",
                "--proposal",
                str(artifact),
                "--out",
                str(out_file),
                "--decided-by",
                "operator:test",
            ]
        )
        == 2
    )
    assert "digest does not match" in capsys.readouterr().err
    assert not out_file.exists()


def test_cli_apply_refuses_forged_proposal(tmp_path, monkeypatch, capsys):
    """A hand-written proposal naming an evidence-less change fails the
    digest check — apply only ever writes evaluated changes."""
    _cli_canon_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    _seed([_rep("t1", effort="xhigh", grade="A")])
    forged = tmp_path / "forged.json"
    forged.write_text(
        json.dumps(
            {
                "rule_version": 1,
                "min_passes": 2,
                "digest": "forged",
                "results": [
                    {
                        "profile": "grok-hi",
                        "effort": "xhigh",
                        "action": "promote",
                        "target": "S",
                        "evidence": ["local:1"],
                    }
                ],
            }
        )
    )
    assert (
        cli.main(
            [
                "grades",
                "apply",
                "--proposal",
                str(forged),
                "--out",
                str(tmp_path / "catalog.json"),
                "--decided-by",
                "operator:test",
            ]
        )
        == 2
    )


def test_cli_apply_no_changes_writes_nothing(tmp_path, monkeypatch, capsys):
    _cli_canon_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    _seed([_rep("t1", effort="xhigh", grade="A")])  # one pass — insufficient
    artifact = tmp_path / "proposal.json"
    out_file = tmp_path / "catalog.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    assert (
        cli.main(
            [
                "grades",
                "apply",
                "--proposal",
                str(artifact),
                "--out",
                str(out_file),
                "--decided-by",
                "operator:test",
            ]
        )
        == 0
    )
    assert not out_file.exists()
    assert "no grade changes" in capsys.readouterr().out


def test_cli_propose_min_passes_zero_rejected(tmp_path, monkeypatch, capsys):
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    assert cli.main(["grades", "propose", "--min-passes", "0"]) == 2
    assert "1 이상" in capsys.readouterr().err


def test_cli_propose_bad_exclude_rejected(tmp_path, monkeypatch, capsys):
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    assert cli.main(["grades", "propose", "--exclude", "bogus"]) == 2


# ---------------------------------------------------------------------------
# task #741 — apply refuses degraded input unless --allow-degraded says why
# ---------------------------------------------------------------------------


def _apply_args(artifact, out_file, *extra) -> list[str]:
    return [
        "grades",
        "apply",
        "--proposal",
        str(artifact),
        "--out",
        str(out_file),
        "--decided-by",
        "operator:test",
        *extra,
    ]


def _insecure_url_reps_backend(monkeypatch) -> None:
    """Per-use split: credentials exist but the reps use stays local."""
    real = bench.bench_backend

    def fake(*, use, stderr=None, allow_plaintext_http=False):
        if use == "reps":
            return bench.BenchBackend(
                name=bench.BENCH_BACKEND_LOCAL,
                cache_ttl_s=60.0,
                url=None,
                token=None,
                endpoint_id="",
                reason="auto-local-insecure-url",
                plaintext_use=use,
            )
        return real(use=use, stderr=stderr, allow_plaintext_http=allow_plaintext_http)

    monkeypatch.setattr(bench, "bench_backend", fake)


def test_cli_apply_refuses_snapshot_catalog(tmp_path, monkeypatch, capsys):
    """Mutant guard: a snapshot catalog must stop apply cold — a row stamped
    from the bundled snapshot must never reach push-catalog."""
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])  # snapshot source
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = tmp_path / "proposal.json"
    out_file = tmp_path / "catalog.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    assert cli.main(_apply_args(artifact, out_file)) == 2
    err = capsys.readouterr().err
    assert "refuses degraded input" in err and "snapshot" in err
    assert not out_file.exists()


def test_cli_apply_refuses_unsupported_catalog(tmp_path, monkeypatch, capsys):
    """Mutant guard: UNSUPPORTED also serves the bundled snapshot — a guard
    that only names SNAPSHOT leaves a degraded path that still applies."""
    _cli_canon_view(
        monkeypatch,
        [_entry("grok-hi", "xhigh", "C")],
        source=bench.CATALOG_SOURCE_UNSUPPORTED,
    )
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = tmp_path / "proposal.json"
    out_file = tmp_path / "catalog.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    assert cli.main(_apply_args(artifact, out_file)) == 2
    assert "refuses degraded input" in capsys.readouterr().err
    assert not out_file.exists()


def test_cli_apply_allows_cache_catalog(tmp_path, monkeypatch, capsys):
    """Mutant guard: a server cache inside the staleness budget is canon —
    refusing every non-server source would brick normal applies."""
    _cli_canon_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")], source=bench.CATALOG_SOURCE_CACHE)
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = tmp_path / "proposal.json"
    out_file = tmp_path / "catalog.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    assert cli.main(_apply_args(artifact, out_file)) == 0
    assert out_file.exists()


def test_cli_apply_refuses_insecure_url_reps(tmp_path, monkeypatch, capsys):
    """The insecure-url fallback: the catalog is canon but the reps canon
    exists and was never read — apply refuses even with a matching digest."""
    _cli_canon_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    _insecure_url_reps_backend(monkeypatch)
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = tmp_path / "proposal.json"
    out_file = tmp_path / "catalog.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    assert cli.main(_apply_args(artifact, out_file)) == 2
    err = capsys.readouterr().err
    assert "refuses degraded input" in err and "reps" in err
    assert not out_file.exists()


def test_cli_apply_refuses_partial_rep_window(tmp_path, monkeypatch, capsys):
    """A full server rep window can hide older FAIL evidence — apply refuses."""
    _cli_canon_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    fake = _remote_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(bench, "_MIGRATE_REP_WINDOW", 1)
    fake.reps = [
        _remote_row(
            bench.RepRecord(
                id=900,
                profile="builder-grok",
                model_id="grok-4.7",
                task_ref="srv-1",
                tier="T1",
                role="impl",
                rounds=1,
                blockers_found=0,
                completed=1,
                input_tokens=None,
                output_tokens=None,
                notes=None,
                recorded_at="2026-09-25T00:00:00Z",
                effort="xhigh",
                grade="A+",
                table_grade=None,
            ),
            601,
            host=None,
        )
    ]
    artifact = tmp_path / "proposal.json"
    out_file = tmp_path / "catalog.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    assert cli.main(_apply_args(artifact, out_file)) == 2
    err = capsys.readouterr().err
    assert "refuses degraded input" in err and "window" in err
    assert not out_file.exists()


def test_cli_apply_allow_degraded_records_reason(tmp_path, monkeypatch, capsys):
    """The explicit override passes — and the reason lands in the artifact,
    the console, and every changed row's deviation_ref."""
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])  # snapshot = degraded
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = tmp_path / "proposal.json"
    out_file = tmp_path / "catalog.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    reason = "canon unreachable; snapshot rows are the reviewed copy"
    assert cli.main(_apply_args(artifact, out_file, "--allow-degraded", reason)) == 0
    payload = json.loads(out_file.read_text())
    override_record = payload.get("degraded_override") or {}
    assert override_record.get("reason") == reason
    assert override_record.get("inputs")
    changed = payload["catalog"][0]
    assert f"degraded-override: {reason}" in changed["deviation_ref"]
    out = capsys.readouterr().out
    assert "degraded input applied" in out and reason in out


def test_cli_apply_allow_degraded_no_changes_still_records_reason(tmp_path, monkeypatch, capsys):
    """A degraded apply that changes nothing writes no artifact — the console
    is the only trace, so the override reason must land there."""
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])  # snapshot = degraded
    _seed([_rep("t1", effort="xhigh", grade="A")])  # one pass — insufficient
    artifact = tmp_path / "proposal.json"
    out_file = tmp_path / "catalog.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    reason = "canon unreachable; reviewed the bundled snapshot"
    assert cli.main(_apply_args(artifact, out_file, "--allow-degraded", reason)) == 0
    assert not out_file.exists()
    out = capsys.readouterr().out
    assert "proceeded over degraded input" in out and reason in out


def test_cli_apply_allow_degraded_requires_a_reason(tmp_path, monkeypatch, capsys):
    """A blank --allow-degraded is not an override — refuse before any write."""
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = tmp_path / "proposal.json"
    out_file = tmp_path / "catalog.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    assert cli.main(_apply_args(artifact, out_file, "--allow-degraded", "  ")) == 2
    assert not out_file.exists()


def test_cli_propose_marks_degraded_output(tmp_path, monkeypatch, capsys):
    """Propose stays read-only but labels degraded input in both outputs."""
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = tmp_path / "proposal.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    assert json.loads(artifact.read_text())["degraded"]
    assert cli.main(["grades", "propose"]) == 0
    assert "degraded input" in capsys.readouterr().out


def test_cli_propose_clean_input_marks_nothing(tmp_path, monkeypatch, capsys):
    _cli_canon_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = tmp_path / "proposal.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    assert json.loads(artifact.read_text())["degraded"] == []
    assert cli.main(["grades", "propose"]) == 0
    assert "degraded input" not in capsys.readouterr().out


def test_cli_apply_malformed_artifact_is_a_clean_error(tmp_path, monkeypatch, capsys):
    """CodeRabbit minor on #100: params/results of the wrong shape used to
    crash with AttributeError/KeyError — now a BenchError with exit 2."""
    _cli_canon_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    bad_params = tmp_path / "bad-params.json"
    bad_params.write_text(
        json.dumps({"rule_version": grades.RULE_VERSION, "min_passes": 2, "params": ["cli_exclusions"]})
    )
    assert cli.main(_apply_args(bad_params, tmp_path / "o1.json")) == 2
    assert "params" in capsys.readouterr().err
    # A truthy non-array cli_exclusions or results used to TypeError before the
    # comprehension — both are clean exit-2 errors now.
    bad_collections = tmp_path / "bad-collections.json"
    bad_collections.write_text(
        json.dumps({"rule_version": grades.RULE_VERSION, "min_passes": 2, "params": {"cli_exclusions": 42}})
    )
    assert cli.main(_apply_args(bad_collections, tmp_path / "o1b.json")) == 2
    assert "cli_exclusions" in capsys.readouterr().err
    bad_collections.write_text(
        json.dumps({"rule_version": grades.RULE_VERSION, "min_passes": 2, "results": 42})
    )
    assert cli.main(_apply_args(bad_collections, tmp_path / "o1c.json")) == 2
    assert "results" in capsys.readouterr().err
    # A results row missing profile/effort must not KeyError — it loses the
    # recorded-changes comparison as a plain BenchError.
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = tmp_path / "proposal.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    payload = json.loads(artifact.read_text())
    del payload["results"][0]["profile"]
    artifact.write_text(json.dumps(payload))
    assert cli.main(_apply_args(artifact, tmp_path / "o2.json")) == 2
    assert "disagrees" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# handoffkeep backend: server + host-local merge, migration dedup
# ---------------------------------------------------------------------------


class _FakeRepStore:
    """GET /v1/bench/reps only — enough for the merge path."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.reps: list[dict] = []

    def __call__(self, url, *, method="GET", headers=None, body=None, timeout=None, **_kw):
        assert method == "GET"
        split = urllib.parse.urlsplit(str(url))
        assert split.path == "/v1/bench/reps"
        rows = sorted(self.reps, key=lambda r: r["id"], reverse=True)
        return {"reps": [dict(r) for r in rows]}


def _remote_row(rep: bench.RepRecord, server_id: int, *, host: str | None) -> dict:
    if host is None:
        notes, origin_id = rep.notes, rep.id
    else:
        notes = f"{rep.notes} [src:{host}]" if rep.notes else f"[src:{host}]"
        origin_id = bench._migrate_origin_id(host, rep.profile, rep.id)
    return {
        "id": server_id,
        "origin_id": origin_id,
        "created_by": "ops",
        "created_at": "2026-09-25T00:00:00Z",
        "profile": rep.profile,
        "model_id": rep.model_id,
        "task_ref": rep.task_ref,
        "tier": rep.tier,
        "role": rep.role,
        "rounds": rep.rounds,
        "blockers_found": rep.blockers_found,
        "completed": rep.completed,
        "input_tokens": rep.input_tokens,
        "output_tokens": rep.output_tokens,
        "notes": notes,
        "recorded_at": rep.recorded_at,
        "effort": rep.effort,
        "grade": rep.grade,
        "table_grade": rep.table_grade,
    }


def _remote_backend(tmp_path, monkeypatch) -> _FakeRepStore:
    config = tmp_path / "config" / "scopefuel" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[bench]\nbackend = "handoffkeep"\n', encoding="utf-8")
    monkeypatch.setenv("HANDOFFKEEP_URL", "https://hk.invalid")
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", "hk-test-token")
    fake = _FakeRepStore("https://hk.invalid")
    monkeypatch.setattr(bench, "request_json", fake)
    return fake


def test_server_and_local_merge_dedups_migrated(tmp_path, monkeypatch):
    """AC3: server rows carry srv refs; local rows not yet migrated keep their
    local refs; a local row already on the server counts once via the server."""
    migrated = bench.add_rep(**_rep("old", effort="xhigh", grade="A+"))  # local:1
    bench.add_rep(**_rep("new", effort="xhigh", grade="A+"))  # local:2
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps = [
        _remote_row(migrated, 501, host=HOST),
        _remote_row(
            bench.RepRecord(
                id=900,
                profile="builder-grok",
                model_id="grok-4.7",
                task_ref="srv-only",
                tier="T1",
                role="impl",
                rounds=1,
                blockers_found=0,
                completed=1,
                input_tokens=None,
                output_tokens=None,
                notes=None,
                recorded_at="2026-09-25T00:00:00Z",
                effort="xhigh",
                grade="A+",
                table_grade=None,
            ),
            502,
            host=None,
        ),
    ]
    view = _view(_entry("grok-hi", "xhigh", "C"))
    evidence = grades.gather_reps(view=view, host=HOST)
    assert evidence.backend == "handoffkeep"
    assert evidence.remote_count == 2 and evidence.local_count == 2
    counted_refs = sorted(r.ref for r in evidence.rows if not r.excluded)
    assert counted_refs == ["local:2", "srv:501", "srv:502"]
    migrated_row = next(r for r in evidence.rows if r.ref == f"local:{migrated.id}")
    assert "already migrated" in migrated_row.excluded
    # And the merged evidence promotes the rung (3 graded passes incl. server-only).
    proposal = grades.evaluate(evidence, view)
    assert _result(proposal, "grok-hi", "xhigh").action == "promote"


def test_bare_exclusion_id_binds_both_namespaces(tmp_path, monkeypatch):
    """Tester blocker: under handoffkeep a bare exclusion id is the fleet-cited
    rep id — it must bind the server row AND an unmigrated local duplicate
    sharing the rowid, so the pair cannot double-count."""
    rep = bench.add_rep(**_rep("dup", effort="xhigh", grade="A+"))  # local:1
    bench.add_rep(**_rep("survivor", effort="xhigh", grade="A+"))  # local:2
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps = [
        _remote_row(
            bench.RepRecord(
                id=1,
                profile="builder-grok",
                model_id="grok-4.7",
                task_ref="srv-dup",
                tier="T1",
                role="impl",
                rounds=1,
                blockers_found=0,
                completed=1,
                input_tokens=None,
                output_tokens=None,
                notes=None,
                recorded_at="2026-09-25T00:00:00Z",
                effort="xhigh",
                grade="A+",
                table_grade=None,
            ),
            1,  # srv:1 — same rowid as local:1, a different rep
            host=None,
        ),
    ]
    view = _view(_entry("grok-hi", "xhigh", "C"))
    evidence = grades.gather_reps(view=view, exclusions=[("1", "2")], host=HOST)
    cli_ex = next(e for e in evidence.exclusions if e.origin == "cli")
    assert set(cli_ex.old_refs) == {"srv:1", "local:1"}
    excluded = {r.ref for r in evidence.rows if r.excluded}
    assert {"srv:1", "local:1"} <= excluded
    assert "local:2" in {r.ref for r in evidence.rows if not r.excluded}
    # A namespace-pinned spec does not reach across: srv:1 leaves local:1 alone.
    evidence2 = grades.gather_reps(view=view, exclusions=[("srv:1", "srv:2")], host=HOST)
    pinned = next(e for e in evidence2.exclusions if e.origin == "cli")
    assert pinned.old_refs == ("srv:1",)
    assert f"local:{rep.id}" in {r.ref for r in evidence2.rows if not r.excluded}


def test_local_backend_discloses_server_unread(tmp_path, monkeypatch, capsys):
    """AC3: under the local backend the proposal says the server was not read."""
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    _seed([_rep("t1", effort="xhigh", grade="A")])
    assert cli.main(["grades", "propose"]) == 0
    out = capsys.readouterr().out
    assert "source=local" in out
    assert "server reps were not read" in out


# ---------------------------------------------------------------------------
# Tester round-1 blocker regressions
# ---------------------------------------------------------------------------


def _seed_at_id(rep_id: int, **overrides) -> None:
    """Insert a rep at an explicit rowid — static pairs cite fixed ids."""
    task_ref = overrides.pop("task_ref", f"t{rep_id}")
    row = _rep(task_ref, **overrides)
    conn = bench.connect()
    try:
        conn.execute(
            "INSERT INTO reps "
            "(id, profile, model_id, task_ref, tier, role, rounds, blockers_found, completed, "
            "input_tokens, output_tokens, notes, recorded_at, effort, grade, table_grade) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rep_id,
                row["profile"],
                row["model_id"],
                row["task_ref"],
                row["tier"],
                row["role"],
                row["rounds"],
                row["blockers_found"],
                row["completed"],
                None,
                None,
                row.get("notes"),
                row["recorded_at"],
                row.get("effort"),
                row.get("grade"),
                bench.derive_table_grade(row["profile"], row.get("effort")),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_static_desktop_pair_binds_home_desktop(tmp_path, isolated_cache):
    """Tester blocker: the desktop pair is local@home-desktop — the earlier
    'desktop' hostname bound nothing anywhere."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed_at_id(80, effort="xhigh", grade="A+")
    _seed_at_id(81, effort="xhigh", grade="A+")
    evidence = grades.gather_reps(view=view, host="home-desktop")
    ex = next(e for e in evidence.exclusions if e.old_spec == "local@home-desktop:80")
    assert ex.old_refs == ("local:80",) and ex.new_ref == "local:81"
    excluded = {r.ref for r in evidence.rows if r.excluded}
    assert "local:80" in excluded and "local:81" not in excluded
    proposal = grades.evaluate(evidence, view)
    assert _result(proposal, "grok-hi", "xhigh").action == "insufficient"
    # Anywhere else the pair binds nothing — an unrelated local:80 still counts.
    proposal2 = _propose(view, host="mbp")
    assert _result(proposal2, "grok-hi", "xhigh").action == "promote"


def test_retired_rung_evidence_never_flows_to_live_sibling(tmp_path, isolated_cache):
    """Tester blocker: reps on retired grok-hi@xhigh must not judge the live
    grok-hi default row through the effort fallback."""
    view = _view(
        _entry("grok-hi", "xhigh", "C", retired_at="2026-09-01T00:00:00Z"),
        _entry("grok-hi", "", "S"),
    )
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    proposal = _propose(view)
    assert all(r.key != ("grok-hi", "") for r in proposal.results)
    assert {r.rep.task_ref for r in proposal.unrung} == {"t1", "t2"}
    text = grades.render_proposal(proposal, view)
    assert "rung retired" in text


def test_notes_supersede_follows_cited_id_across_migration(tmp_path, monkeypatch):
    """Tester blocker: a migrated carrier's ``supersedes id=N`` cites a local
    id on the source host — the exclusion must bind the cited row's migrated
    server copy, not an unrelated server row N."""
    cited = bench.add_rep(**_rep("old", effort="xhigh", grade="A+"))  # local:1
    carrier = bench.add_rep(**_rep("new", effort="xhigh", grade="A+", notes="supersedes id=1"))  # local:2
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps = [
        _remote_row(cited, 501, host=HOST),  # srv:501 = migrated local:1
        _remote_row(carrier, 502, host=HOST),  # srv:502 carries the note
    ]
    view = _view(_entry("grok-hi", "xhigh", "C"))
    evidence = grades.gather_reps(view=view, host=HOST)
    excluded = {r.ref: r.excluded for r in evidence.rows if r.excluded}
    assert "superseded" in excluded.get("srv:501", "")
    proposal = grades.evaluate(evidence, view)
    # Only the carrier's server copy counts — 1 pass < 2.
    assert _result(proposal, "grok-hi", "xhigh").action == "insufficient"


def test_notes_supersede_binds_migrated_copy_for_local_carrier(tmp_path, monkeypatch):
    """The mirror case: a still-local carrier cites a local id that has
    already migrated — its live server copy is bound too."""
    cited = bench.add_rep(**_rep("old", effort="xhigh", grade="A+"))  # local:1
    bench.add_rep(**_rep("new", effort="xhigh", grade="A+", notes="supersedes id=1"))  # local:2, unmigrated
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps = [_remote_row(cited, 501, host=HOST)]  # only the cited row migrated
    view = _view(_entry("grok-hi", "xhigh", "C"))
    evidence = grades.gather_reps(view=view, host=HOST)
    excluded = {r.ref: r.excluded for r in evidence.rows if r.excluded}
    assert "superseded" in excluded.get("srv:501", "")
    proposal = grades.evaluate(evidence, view)
    assert _result(proposal, "grok-hi", "xhigh").action == "insufficient"


def test_read_catalog_commit_cache_false_never_writes(tmp_path, monkeypatch):
    """Tester blocker: the strictly read-only catalog path must neither
    persist the server response nor create the cache DB/schema."""
    _remote_backend(tmp_path, monkeypatch)
    monkeypatch.setattr(
        bench,
        "request_json",
        lambda url, **kw: {
            "catalog": [{"profile": "grok-hi", "effort": "xhigh", "grade": "A", "gate": "default"}]
        },
    )
    target = bench.db_path()
    assert not target.exists()
    view = bench.read_catalog(commit_cache=False)
    assert view.source == bench.CATALOG_SOURCE_SERVER
    assert not target.exists()  # nothing created, nothing written
    bench.reset_catalog_memo()
    view2 = bench.read_catalog()
    assert view2.source == bench.CATALOG_SOURCE_SERVER
    assert target.exists()  # the default path still maintains the cache


def test_null_blockers_never_counts_as_clean(tmp_path, isolated_cache):
    """Tester SHOULD: blockers_found=None is 'not recorded', not zero — an
    unmeasured pass cannot establish a clean PASS."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed_at_id(1, effort="xhigh", grade="A+", blockers_found=None)
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "insufficient"
    assert len(result.unclean_passes) == 1


def test_off_ladder_catalog_grade_raises_bench_error(tmp_path, isolated_cache):
    """Tester SHOULD: a catalog row whose grade is off the ladder must fail
    with a BenchError naming the rung — not an AttributeError on row.label."""
    view = _view(_entry("grok-hi", "xhigh", "Q"))
    _seed([_rep("t1", effort="xhigh", grade="A+")])
    with pytest.raises(bench.BenchError, match="grok-hi@xhigh"):
        _propose(view)


# ---------------------------------------------------------------------------
# task #743 — rule v1 supplements: required --grade, non-mutating backfill
# ---------------------------------------------------------------------------


def _reps_add_argv(**extra) -> list[str]:
    argv = [
        "reps",
        "add",
        "--profile",
        "builder-x",
        "--model",
        "model-x",
        "--task",
        "743",
        "--tier",
        "T2",
        "--role",
        "impl",
        "--rounds",
        "1",
        "--blockers-found",
        "0",
        "--completed",
        "1",
    ]
    for flag, value in extra.items():
        argv += [f"--{flag.replace('_', '-')}", str(value)]
    return argv


def test_reps_add_requires_grade(tmp_path, isolated_cache, capsys):
    """Missing --grade refuses at the parser, writes nothing, names the flag."""
    with pytest.raises(SystemExit) as exc:
        cli.main(_reps_add_argv())
    assert exc.value.code == 2
    assert "--grade" in capsys.readouterr().err
    assert bench.read_reps() == []


def test_reps_add_with_grade_still_records(tmp_path, isolated_cache):
    assert cli.main(_reps_add_argv(grade="A+")) == 0
    reps = bench.read_reps()
    assert len(reps) == 1 and reps[0].grade == "A+"


def _mapping_file(tmp_path, payload) -> str:
    path = tmp_path / "grade-map.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _annotations() -> dict:
    return bench.read_rep_grade_annotations()


def test_backfill_dry_run_writes_nothing(tmp_path, isolated_cache, capsys):
    _seed([_rep("743", effort="xhigh")])
    rc = cli.main(["reps", "backfill", "--mapping", _mapping_file(tmp_path, {"743": "A+"})])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out
    assert "annotate local:1 grade=A+ task=743" in out
    assert _annotations() == {}


def test_backfill_apply_annotates_without_mutating_originals(tmp_path, isolated_cache, capsys):
    _seed([_rep("743", effort="xhigh")])
    before = [rep.as_dict() for rep in bench.read_reps()]
    rc = cli.main(["reps", "backfill", "--mapping", _mapping_file(tmp_path, {"743": "A+"}), "--apply"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "wrote 1 annotation row(s)" in out
    after = [rep.as_dict() for rep in bench.read_reps()]
    assert after == before  # the original row is byte-identical
    annotations = _annotations()
    assert list(annotations) == ["local:1"]
    assert annotations["local:1"].grade == "A+"


def test_backfill_proposal_consumes_annotated_grade(tmp_path, isolated_cache):
    """After --apply, propose evaluates the annotated reps at their filled
    grade — and discloses the overlay."""
    view = _view(_entry("grok-hi", "xhigh", "B"))
    # Two distinct tasks — v1.1 counts one task once per rung.
    _seed([_rep("743", effort="xhigh"), _rep("744", effort="xhigh")])
    report = grades.backfill_rep_grades(mapping={"743": "A+", "744": "A+"}, apply=True)
    assert report.applied == 2
    proposal = _propose(view)
    result = _result(proposal, "grok-hi", "xhigh")
    assert result.action == "promote"
    assert result.target == "A+"
    assert proposal.evidence.annotations_applied
    text = grades.render_proposal(proposal, view)
    assert "(backfilled)" in text
    assert "backfilled grades applied" in text


def test_backfill_without_apply_changes_no_proposal(tmp_path, isolated_cache):
    """Dry-run plans but never persists — propose still sees ungraded reps."""
    view = _view(_entry("grok-hi", "xhigh", "B"))
    _seed([_rep("743", effort="xhigh"), _rep("744", effort="xhigh")])
    report = grades.backfill_rep_grades(mapping={"743": "A+", "744": "A+"})
    assert len(report.planned) == 2 and report.applied == 0
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert len(result.ungraded_passes) == 2
    assert result.action != "promote"


def test_backfill_never_overwrites_existing_grade(tmp_path, isolated_cache):
    """An already-graded rep keeps its recorded grade; the annotation is
    reported as skipped, not written."""
    _seed(
        [
            _rep("743", effort="xhigh", grade="B"),  # recorded grade wins
            _rep("743", effort="xhigh"),  # the only annotatable row
        ]
    )
    report = grades.backfill_rep_grades(mapping={"743": "A+"}, apply=True)
    assert report.applied == 1
    assert report.skipped_graded == [("local:1", "743", "B")]
    annotations = _annotations()
    assert "local:1" not in annotations
    assert annotations["local:2"].grade == "A+"


def test_backfill_rerun_is_idempotent(tmp_path, isolated_cache):
    _seed([_rep("743", effort="xhigh")])
    assert grades.backfill_rep_grades(mapping={"743": "A+"}, apply=True).applied == 1
    second = grades.backfill_rep_grades(mapping={"743": "A+"}, apply=True)
    assert second.applied == 0
    assert second.already_annotated == [("local:1", "A+")]
    assert len(_annotations()) == 1


def test_backfill_conflicting_annotation_keeps_original(tmp_path, isolated_cache):
    """A second mapping that names a different grade for an annotated rep is a
    conflict — reported, never rewritten."""
    _seed([_rep("743", effort="xhigh")])
    grades.backfill_rep_grades(mapping={"743": "A+"}, apply=True)
    second = grades.backfill_rep_grades(mapping={"743": "S"}, apply=True)
    assert second.applied == 0
    assert second.conflicting_annotations == [("local:1", "A+", "S")]
    assert _annotations()["local:1"].grade == "A+"


def test_backfill_missing_task_reported(tmp_path, isolated_cache, capsys):
    _seed([_rep("742", effort="xhigh")])
    rc = cli.main(["reps", "backfill", "--mapping", _mapping_file(tmp_path, {"999": "A"})])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no counted reps for task 999" in out
    assert _annotations() == {}


def test_backfill_rejects_bad_mapping(tmp_path, isolated_cache, capsys):
    _seed([_rep("743", effort="xhigh")])
    for payload in (["743"], {"743": "Q"}, {"": "A+"}, {}):
        rc = cli.main(["reps", "backfill", "--mapping", _mapping_file(tmp_path, payload)])
        assert rc == 2
        assert "error:" in capsys.readouterr().err
    assert _annotations() == {}


def test_backfill_annotated_fail_counts_as_demote_evidence(tmp_path, isolated_cache):
    """The overlay feeds every classifier, not just passes: a backfilled
    post-merge marker FAIL demotes at-or-below alone."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed([_rep("743", effort="xhigh", completed=0, notes="[rollback]")])
    grades.backfill_rep_grades(mapping={"743": "A+"}, apply=True)
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"
    assert result.target == "A"


def test_superseded_fail_never_double_counts_demote(tmp_path, isolated_cache):
    """Directed surface: a superseded FAIL row and its replacement must not
    add up to the two-FAIL demotion threshold."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed_at_id(11, effort="xhigh", grade="A+", completed=0, blockers_found=1)
    _seed_at_id(12, effort="xhigh", grade="A+", completed=0, blockers_found=1, notes="supersedes id=11")
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "blocked"  # only rep 12 counts — one FAIL short
    assert result.target == "A+"


def test_two_distinct_fails_still_demote_after_exclusion(tmp_path, isolated_cache):
    """…but two genuinely distinct FAILs still reach the threshold."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed_at_id(11, effort="xhigh", grade="A+", completed=0, blockers_found=1)
    _seed_at_id(12, effort="xhigh", grade="A+", completed=0, blockers_found=1, notes="supersedes id=11")
    _seed_at_id(13, effort="xhigh", grade="A", completed=0, blockers_found=1)
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"  # reps 12 + 13 count
    assert result.target == "B"  # one step below the weaker fail (A)


def test_backfill_conflict_follows_annotation_across_migration(tmp_path, monkeypatch):
    """Round-1 BLOCKER: an annotation that reached a rep through migration
    fan-out is still an existing annotation — re-backfilling the task at a
    different grade is a reported conflict on the canonical ref, never a
    second annotation that splits the rep's grade."""
    migrated = bench.add_rep(**_rep("743", effort="xhigh"))  # local:1, ungraded
    assert grades.backfill_rep_grades(mapping={"743": "A+"}, apply=True, host=HOST).applied == 1
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps = [_remote_row(migrated, 501, host=HOST)]  # local:1 -> srv:501

    second = grades.backfill_rep_grades(mapping={"743": "S"}, apply=True, host=HOST)
    assert second.applied == 0
    assert second.conflicting_annotations == [("srv:501", "A+", "S")]
    annotations = _annotations()
    assert list(annotations) == ["local:1"]
    assert annotations["local:1"].grade == "A+"


def test_backfill_same_grade_after_migration_is_idempotent(tmp_path, monkeypatch):
    """Same-grade re-backfill against the migrated canonical ref reports
    already-annotated and writes nothing."""
    migrated = bench.add_rep(**_rep("743", effort="xhigh"))
    grades.backfill_rep_grades(mapping={"743": "A+"}, apply=True, host=HOST)
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps = [_remote_row(migrated, 501, host=HOST)]

    second = grades.backfill_rep_grades(mapping={"743": "A+"}, apply=True, host=HOST)
    assert second.applied == 0
    assert second.already_annotated == [("srv:501", "A+")]
    assert len(_annotations()) == 1


# ---------------------------------------------------------------------------
# rule v1.1 — evidence hygiene (operator decision 2026-09-27, task #759)
#
# Every gate is pinned both directions: a rep counts only when its recorded
# model matches the rung's catalog model (declared renames aside), non-coding
# task refs and anomalous rows are excluded and reported, one task counts once
# per rung (latest recorded rep), and demotion is a FAIL rate over the most
# recent N counted tasks — never a raw FAIL count. Removing any gate turns
# the matching assertion RED.
# ---------------------------------------------------------------------------


def _excluded(proposal: grades.Proposal, ref: str) -> grades.EvidenceRep:
    row = next(r for r in proposal.evidence.rows if r.ref == ref)
    assert row.excluded
    return row


# --- AC1: model match -----------------------------------------------------


def test_rep_model_matches_rung_model_counts(tmp_path, isolated_cache):
    """Baseline: rep.model == rung catalog model -> the rep counts."""
    view = _view(_entry("codex-sol", "high", "C", model_id="gpt-6-sol"))
    _seed(
        [
            _rep("t1", profile="codex-sol", model_id="gpt-6-sol", effort="high", grade="A+"),
            _rep("t2", profile="codex-sol", model_id="gpt-6-sol", effort="high", grade="A+"),
        ]
    )
    result = _result(_propose(view), "codex-sol", "high")
    assert result.action == "promote"
    assert result.target == "A+"


def test_model_mismatch_reported_never_counted(tmp_path, isolated_cache):
    """The held-proposal defect: a rep recorded as `codex` resolves to the
    codex-sol rung by spelling, but its recorded model is not the rung's
    gpt-6-sol — mismatch is reported, never counted, so pre-09-22 evidence
    cannot promote the new-generation rung."""
    view = _view(_entry("codex-sol", "high", "C", model_id="gpt-6-sol"))
    _seed(
        [
            _rep("t1", profile="codex", model_id="codex", effort="high", grade="S"),
            _rep("t2", profile="codex", model_id="gpt-5", effort="high", grade="S"),
        ]
    )
    proposal = _propose(view)
    result = _result(proposal, "codex-sol", "high")
    assert result.action == "insufficient" and not result.counted
    assert len(result.excluded) == 2
    assert all("model mismatch" in r.excluded for r in result.excluded)
    text = grades.render_proposal(proposal, view)
    assert "model mismatch" in text
    assert "model-mismatch=2" in text


def test_older_generation_never_equivalent(tmp_path, isolated_cache):
    """gpt-5.6-sol is an older generation of the same line — not equivalent."""
    view = _view(_entry("codex-sol", "high", "C", model_id="gpt-6-sol"))
    _seed([_rep("t1", profile="codex-sol", model_id="gpt-5.6-sol", effort="high", grade="S")])
    row = _excluded(_propose(view), "local:1")
    assert "model mismatch" in row.excluded and "model-mismatch" in row.exclusion_tags


def test_model_equivalence_declared_rename_counts(tmp_path, isolated_cache):
    """The declared list covers provable same-model spellings: a rep recorded
    as `devin-swe2-max` — the launcher profile that pins exactly that model —
    counts on the swe-2-max rung."""
    view = _view(_entry("devin-swe2-max", "", "C", model_id="swe-2-max"))
    _seed(
        [
            _rep("t1", profile="builder-devin-max", model_id="devin-swe2-max", grade="A+"),
            _rep("t2", profile="builder-devin-max", model_id="devin-swe2-max", grade="A+"),
        ]
    )
    result = _result(_propose(view), "devin-swe2-max", "")
    assert result.action == "promote"
    assert result.target == "A+"


def test_model_unrecorded_never_counts(tmp_path, isolated_cache):
    """A rep with no recorded model cannot prove it ran the rung's model —
    fail-closed: reported, never counted."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed_at_id(9, model_id=None, effort="xhigh", grade="A+")
    row = _excluded(_propose(view), "local:9")
    assert "model not recorded" in row.excluded
    assert "model-mismatch" in row.exclusion_tags


def test_rung_model_unrecorded_never_counts(tmp_path, isolated_cache):
    """A catalog row with no model id gives the rep nothing to match —
    fail-closed rather than counting an unverifiable rep."""
    view = _view(_entry("grok-hi", "xhigh", "C", model_id=""))
    _seed([_rep("t1", effort="xhigh", grade="A+")])
    row = _excluded(_propose(view), "local:1")
    assert "rung model not recorded" in row.excluded


def test_model_disposition_unit_edges(tmp_path, isolated_cache):
    """assertion-RED: deleting the equivalence/mismatch logic flips these."""
    entry = _entry("grok-hi", "xhigh", "C", model_id="grok-4.7")
    rep = bench.RepRecord(
        id=1,
        profile="builder-grok",
        model_id="grok-4.7",
        task_ref="t",
        tier="T1",
        role="impl",
        rounds=1,
        blockers_found=0,
        completed=1,
        input_tokens=None,
        output_tokens=None,
        notes=None,
        recorded_at="2026-09-20T10:00:00Z",
        effort="xhigh",
        grade="A+",
    )
    assert grades._model_disposition(rep, entry) == ""
    assert "mismatch" in grades._model_disposition(dataclasses_replace_model(rep, "grok-4.6"), entry)
    equiv_entry = _entry("devin-swe2-max", "", "C", model_id="swe-2-max")
    assert grades._model_disposition(dataclasses_replace_model(rep, "devin-swe2-max"), equiv_entry) == ""
    assert "mismatch" in grades._model_disposition(dataclasses_replace_model(rep, "devin-swe2"), equiv_entry)


def dataclasses_replace_model(rep: bench.RepRecord, model_id: str | None) -> bench.RepRecord:
    import dataclasses

    return dataclasses.replace(rep, model_id=model_id)


# --- AC2: task-level aggregation ------------------------------------------


def test_duplicate_task_cannot_reach_min_passes(tmp_path, isolated_cache):
    """The #677/#568 defect: two reps of the SAME task on the same rung are
    one measurement — they cannot add up to min_passes."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("677", effort="xhigh", grade="A+"),
            _rep("677", effort="xhigh", grade="A+", recorded_at="2026-09-20T11:00:00Z"),
        ]
    )
    proposal = _propose(view)
    result = _result(proposal, "grok-hi", "xhigh")
    assert result.action == "insufficient"
    assert len(result.counted) == 1
    text = grades.render_proposal(proposal, view)
    assert "same task '677' counted once" in text
    assert "final rep is local:2" in text


def test_latest_final_rep_wins_fail_then_pass(tmp_path, isolated_cache):
    """A task re-run counts once by its final disposition: an early FAIL
    superseded by a later PASS of the same task leaves the PASS."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep(
                "retry",
                effort="xhigh",
                grade="A+",
                completed=0,
                blockers_found=1,
            ),
            _rep(
                "retry",
                effort="xhigh",
                grade="A+",
                completed=1,
                recorded_at="2026-09-20T12:00:00Z",
            ),
            _rep("other", effort="xhigh", grade="A+"),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "promote"
    assert len(result.fails) == 0  # the early FAIL is not the task's final rep


def test_latest_final_rep_wins_pass_then_fail(tmp_path, isolated_cache):
    """…and the inverse: an early PASS superseded by a later FAIL counts the
    FAIL — one task can flip its final verdict either direction."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed(
        [
            _rep("flip", effort="xhigh", grade="A+", completed=1),
            _rep(
                "flip",
                effort="xhigh",
                grade="A+",
                completed=0,
                blockers_found=1,
                recorded_at="2026-09-20T12:00:00Z",
            ),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "blocked"
    assert len(result.fails) == 1


def test_anomalous_latest_rep_cannot_suppress_valid_earlier(tmp_path, isolated_cache):
    """Only counted candidates compete for the task's final slot: an anomalous
    latest row is excluded by the hygiene gate before aggregation, so it
    cannot silence an earlier valid measurement."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("ghost", effort="xhigh", grade="A+", completed=1),
            _rep(
                "ghost",
                effort="xhigh",
                grade="A+",
                completed=0,
                rounds=0,
                blockers_found=0,
                recorded_at="2026-09-20T12:00:00Z",
            ),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert len(result.counted) == 1
    assert result.counted[0].rep.completed == 1
    ghost = next(r for r in result.excluded if r.anomaly)
    assert ghost.anomaly == "zero-round-fail"


def test_task_final_tie_breaks_to_later_store_row(tmp_path, isolated_cache):
    """Same recorded_at on duplicate task rows: the higher store id is the
    later record — deterministic, printed."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed([_rep("tie", effort="xhigh", grade="A"), _rep("tie", effort="xhigh", grade="A")])
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert [r.ref for r in result.counted] == ["local:2"]
    assert "final rep is local:2" in grades.render_proposal(_propose(view), view)


# --- AC3: non-coding exclusion --------------------------------------------


def test_b0x_trading_slot_excluded_and_reported(tmp_path, isolated_cache):
    """The default list: a B0X-* trading-slot rep is not coding evidence —
    excluded with the pattern named, never counted."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("B0X-CRYPTO-SLOT9-A", effort="xhigh", grade="A+"),
            _rep("B0X-US-SLOT4-VERIFY", effort="xhigh", grade="A+"),
        ]
    )
    proposal = _propose(view)
    result = _result(proposal, "grok-hi", "xhigh")
    assert not result.counted
    assert all("non-coding task" in r.excluded for r in result.excluded)
    text = grades.render_proposal(proposal, view)
    assert "non-coding task patterns" in text and "(?i)^B0X-" in text
    assert "non-coding=2" in text


def test_coding_task_unaffected_by_b0x_pattern(tmp_path, isolated_cache):
    """Negative: the anchored pattern does not eat ordinary task refs."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("task-B0X-rework", effort="xhigh", grade="A+"),
            _rep("677", effort="xhigh", grade="A+"),
        ]
    )
    assert len(_result(_propose(view), "grok-hi", "xhigh").counted) == 2


def test_config_non_coding_patterns_honored(tmp_path, isolated_cache):
    """``[grades].non_coding_task_patterns`` in config.toml extends the
    default list — the operator's analysis/research refs."""
    config = tmp_path / "config" / "scopefuel" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[grades]\nnon_coding_task_patterns = ["^analysis-", "^research-"]\n')
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("analysis-12", effort="xhigh", grade="A+"),
            _rep("research-doc", effort="xhigh", grade="A+"),
            _rep("568", effort="xhigh", grade="A+"),
        ]
    )
    proposal = _propose(view)
    result = _result(proposal, "grok-hi", "xhigh")
    assert [r.rep.task_ref for r in result.counted] == ["568"]
    assert set(proposal.evidence.non_coding_patterns) >= {"(?i)^B0X-", "^analysis-", "^research-"}


def test_cli_non_coding_pattern_honored(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed([_rep("audit-4", effort="xhigh", grade="A+")])
    evidence = grades.gather_reps(view=view, non_coding=["^audit-"], host=HOST)
    row = _excluded(grades.evaluate(evidence, view), "local:1")
    assert "non-coding task audit-4" in row.excluded
    assert evidence.cli_non_coding == ["^audit-"]


def test_invalid_non_coding_pattern_refused():
    """A bad regex fails loud at gather time, never silently matches."""
    with pytest.raises(bench.BenchError, match="non-coding task pattern"):
        grades._non_coding_patterns(["(unclosed"])


# --- AC4: demotion = FAIL rate over the most recent N counted tasks --------


def test_demotion_needs_fail_rate_in_recent_window(tmp_path, isolated_cache):
    """Two at-or-below FAILs inside the last-5 window at >=40% demote — the
    rule prints the window and the rate."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed(
        [
            _rep("f1", effort="xhigh", grade="A+", completed=0, blockers_found=1),
            _rep(
                "f2",
                effort="xhigh",
                grade="A",
                completed=0,
                blockers_found=1,
                recorded_at="2026-09-20T11:00:00Z",
            ),
            _rep("p1", effort="xhigh", grade="A", recorded_at="2026-09-20T12:00:00Z"),
            _rep("p2", effort="xhigh", grade="A", recorded_at="2026-09-20T13:00:00Z"),
            _rep("p3", effort="xhigh", grade="A", recorded_at="2026-09-20T14:00:00Z"),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"
    assert result.target == "B"  # one below the weakest window FAIL (A -> B)
    assert "2/5" in result.note and "window 5" in result.note


def test_fail_rate_below_threshold_blocks(tmp_path, isolated_cache):
    """Two FAILs in a six-task window is 33% — below the 40% rate, so the
    rung is blocked, not demoted."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed(
        [
            _rep("f1", effort="xhigh", grade="A+", completed=0, blockers_found=1),
            _rep(
                "f2",
                effort="xhigh",
                grade="A+",
                completed=0,
                blockers_found=1,
                recorded_at="2026-09-20T11:00:00Z",
            ),
            _rep("p1", effort="xhigh", grade="A", recorded_at="2026-09-20T12:00:00Z"),
            _rep("p2", effort="xhigh", grade="A", recorded_at="2026-09-20T13:00:00Z"),
            _rep("p3", effort="xhigh", grade="A", recorded_at="2026-09-20T14:00:00Z"),
            _rep("p4", effort="xhigh", grade="A", recorded_at="2026-09-20T15:00:00Z"),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "blocked"
    assert "demotion needs" in result.note


def test_single_fail_never_demotes_even_at_full_rate(tmp_path, isolated_cache):
    """The floor: one FAIL in a thin window is 100% but still one FAIL —
    demotion needs at least two, so the rung blocks instead."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed([_rep("lone", effort="xhigh", grade="A+", completed=0, blockers_found=1)])
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "blocked"


def test_fails_older_than_window_never_demote(tmp_path, isolated_cache):
    """Recency: a FAIL older than the most recent N counted tasks is stale —
    it still blocks promotion but no longer demotes, and says why."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed(
        [
            _rep(
                "old-fail",
                effort="xhigh",
                grade="A+",
                completed=0,
                blockers_found=1,
                recorded_at="2026-09-20T09:00:00Z",
            ),
            _rep(
                "f2",
                effort="xhigh",
                grade="A+",
                completed=0,
                blockers_found=1,
                recorded_at="2026-09-20T11:00:00Z",
            ),
            _rep("p1", effort="xhigh", grade="A", recorded_at="2026-09-20T12:00:00Z"),
            _rep("p2", effort="xhigh", grade="A", recorded_at="2026-09-20T13:00:00Z"),
            _rep("p3", effort="xhigh", grade="A", recorded_at="2026-09-20T14:00:00Z"),
            _rep("p4", effort="xhigh", grade="A", recorded_at="2026-09-20T15:00:00Z"),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    # Window is the newest 5 of 6: f2 + four passes inside -> 1 FAIL, 20% ->
    # blocked; the oldest FAIL is reported as outside the window.
    assert result.action == "blocked"
    assert "older than" in result.note


def test_marker_fail_outside_window_never_demotes(tmp_path, isolated_cache):
    """A post-merge marker demotes alone only inside the window — an old
    rollback is stale evidence, blocking but not demoting."""
    view = _view(_entry("grok-hi", "xhigh", "A"))
    _seed(
        [
            _rep(
                "old-rollback",
                effort="xhigh",
                grade="B",
                completed=0,
                notes="[rollback]",
                recorded_at="2026-09-20T08:00:00Z",
            ),
            *[
                _rep(f"p{i}", effort="xhigh", grade="A", recorded_at=f"2026-09-20T1{i}:00:00Z")
                for i in range(5)
            ],
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "blocked"


def test_marker_fail_inside_window_demotes(tmp_path, isolated_cache):
    """…and inside the window a single marker is still a full demote trigger."""
    view = _view(_entry("grok-hi", "xhigh", "A"))
    _seed(
        [
            _rep("p1", effort="xhigh", grade="A", recorded_at="2026-09-20T10:00:00Z"),
            _rep(
                "rollback",
                effort="xhigh",
                grade="B",
                completed=0,
                notes="[rollback]",
                recorded_at="2026-09-20T11:00:00Z",
            ),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"


def test_cli_demote_window_flag_validated(tmp_path, monkeypatch, capsys):
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    assert cli.main(["grades", "propose", "--demote-window", "0"]) == 2
    assert "demote-window" in capsys.readouterr().err
    assert cli.main(["grades", "propose", "--demote-rate", "0"]) == 2
    assert "demote-rate" in capsys.readouterr().err
    assert cli.main(["grades", "propose", "--demote-rate", "1.5"]) == 2


def test_cli_demote_window_flag_narrows_the_window(tmp_path, monkeypatch, capsys):
    """--demote-window 2 makes the newest two counted tasks the slice: two
    FAILs there demote even with older passes alongside."""
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "A+")])
    _seed(
        [
            _rep("p1", effort="xhigh", grade="A", recorded_at="2026-09-20T10:00:00Z"),
            _rep(
                "f1",
                effort="xhigh",
                grade="A+",
                completed=0,
                blockers_found=1,
                recorded_at="2026-09-20T11:00:00Z",
            ),
            _rep(
                "f2",
                effort="xhigh",
                grade="A+",
                completed=0,
                blockers_found=1,
                recorded_at="2026-09-20T12:00:00Z",
            ),
        ]
    )
    assert cli.main(["grades", "propose", "--demote-window", "2"]) == 0
    out = capsys.readouterr().out
    assert "demote grok-hi@xhigh" in out
    assert "2/2" in out


# --- AC5: anomalous reps ---------------------------------------------------


def test_zero_round_fail_is_anomaly_never_counts(tmp_path, isolated_cache):
    """The srv:756 shape: FAIL with 0 rounds and 0 blockers measured nothing
    — listed as needing review, excluded, so it cannot drive a demotion."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A+", completed=0, blockers_found=1),
            _rep("t2", effort="xhigh", grade="A+", completed=0, rounds=0, blockers_found=0),
        ]
    )
    proposal = _propose(view)
    result = _result(proposal, "grok-hi", "xhigh")
    assert result.action == "blocked"  # one real FAIL remains — no demote
    ghost = _excluded(proposal, "local:2")
    assert ghost.anomaly == "zero-round-fail"
    assert "needs review" in ghost.excluded
    text = grades.render_proposal(proposal, view)
    assert "anomalous reps (1" in text and "zero-round-fail" in text


def test_pass_shaped_fail_is_anomaly(tmp_path, isolated_cache):
    """completed=0 with zero blockers recorded is a FAIL that found nothing —
    the completed=0-with-PASS contradiction the AC names."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed([_rep("t1", effort="xhigh", completed=0, rounds=2, blockers_found=0)])
    row = _excluded(_propose(view), "local:1")
    assert row.anomaly == "pass-shaped-fail"


def test_no_completion_flag_is_anomaly(tmp_path, isolated_cache):
    """A rep with no completed flag never recorded an outcome."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed_at_id(7, effort="xhigh", completed=None)
    row = _excluded(_propose(view), "local:7")
    assert row.anomaly == "no-completion-flag"


def test_marker_fail_exempt_from_anomaly(tmp_path, isolated_cache):
    """A rollback legitimately records 0 rounds/0 blockers — the failure was
    found after merge. Marker FAILs are exempt and still demote alone."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed(
        [
            _rep(
                "t1",
                effort="xhigh",
                grade="A+",
                completed=0,
                rounds=0,
                blockers_found=0,
                notes="[rollback] post-merge revert",
            )
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"
    assert all(not r.anomaly for r in _propose(view).evidence.rows)


def test_legit_fail_with_blockers_is_not_anomaly(tmp_path, isolated_cache):
    """Negative: a FAIL that recorded blockers is a measurement, not an
    anomaly — every real fleet FAIL carries them."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed([_rep("t1", effort="xhigh", completed=0, blockers_found=3)])
    proposal = _propose(view)
    row = next(r for r in proposal.evidence.rows if r.ref == "local:1")
    assert not row.anomaly and not row.excluded


def test_apply_refuses_proposal_built_on_anomaly(tmp_path, isolated_cache, monkeypatch):
    """AC6: an artifact whose recorded results name an anomalous rep as
    evidence is refused — anomalies are excluded until reviewed, and apply
    cannot launder one through."""
    canon = _canon_view(_entry("grok-hi", "xhigh", "C"))
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: canon)
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A+"),
            _rep("t2", effort="xhigh", grade="A+"),
            _rep("t3", effort="xhigh", grade="A+", completed=0, rounds=0, blockers_found=0),
        ]
    )
    proposal = _propose(canon)
    artifact = grades.proposal_to_json(proposal, canon)
    # Forge the promote result to cite the anomalous rep as evidence.
    promote = next(r for r in artifact["results"] if r["action"] == "promote")
    promote["evidence"].append("local:3")
    with pytest.raises(bench.BenchError, match="anomalous rep"):
        grades.apply_proposals(artifact, decided_by="operator:test", deviation_ref="task-759")


def test_apply_accepts_clean_proposal_with_anomalies_present(tmp_path, isolated_cache, monkeypatch):
    """Negative: an artifact built on clean evidence still applies when the
    store merely *contains* an anomaly the proposal did not cite."""
    canon = _canon_view(_entry("grok-hi", "xhigh", "C"))
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: canon)
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A+"),
            _rep("t2", effort="xhigh", grade="A+"),
            _rep("t3", effort="xhigh", grade="A+", completed=0, rounds=0, blockers_found=0),
        ]
    )
    proposal = _propose(canon)
    artifact = grades.proposal_to_json(proposal, canon)
    entries, live, _view = grades.apply_proposals(
        artifact, decided_by="operator:test", deviation_ref="task-759"
    )
    rows = {e.key: e for e in entries}
    assert rows[("grok-hi", "xhigh")].grade == "A+"


# --- AC6: printed summary + artifact params --------------------------------


def test_render_prints_evidence_summary_and_patterns(tmp_path, isolated_cache):
    """Every proposal ends with the counted/excluded tally by reason — the
    audit summary the operator asked for."""
    view = _view(_entry("codex-sol", "high", "C", model_id="gpt-6-sol"))
    _seed(
        [
            _rep("B0X-US-SLOT1", profile="codex-sol", model_id="gpt-6-sol", effort="high", grade="S"),
            _rep("mismatch", profile="codex-sol", model_id="gpt-5", effort="high", grade="S"),
            _rep("ok", profile="codex-sol", model_id="gpt-6-sol", effort="high", grade="S"),
        ]
    )
    text = grades.render_proposal(_propose(view), view)
    assert "non-coding task patterns" in text
    assert "evidence summary:" in text
    assert "non-coding=1" in text and "model-mismatch=1" in text
    assert "1 counted" in text


def test_json_carries_demote_params_and_anomalies(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed([_rep("t1", effort="xhigh", grade="A+", completed=0, rounds=0, blockers_found=0)])
    evidence = grades.gather_reps(view=view, non_coding=["^audit-"], host=HOST)
    payload = grades.proposal_to_json(grades.evaluate(evidence, view), view)
    assert payload["params"]["demote_window"] == grades.DEMOTE_WINDOW
    assert payload["params"]["demote_fail_rate"] == grades.DEMOTE_FAIL_RATE
    assert payload["params"]["cli_non_coding"] == ["^audit-"]
    assert payload["anomalies"] == [{"ref": "local:1", "rule": "zero-round-fail", "task_ref": "t1"}]
    assert "(?i)^B0X-" in payload["non_coding_patterns"]


def test_apply_replays_artifact_non_coding_and_window(tmp_path, isolated_cache, monkeypatch):
    """The artifact params drive apply's re-evaluation: same cli patterns and
    demote window -> the digest matches; forged params fail shape checks."""
    canon = _canon_view(_entry("grok-hi", "xhigh", "C"))
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: canon)
    _seed([_rep("audit-9", effort="xhigh", grade="A+")])
    evidence = grades.gather_reps(view=canon, non_coding=["^audit-"], host=HOST)
    artifact = grades.proposal_to_json(
        grades.evaluate(evidence, canon, demote_window=3, demote_fail_rate=0.5), canon
    )
    entries, live, _ = grades.apply_proposals(artifact, decided_by="operator:test", deviation_ref="task-759")
    assert live.demote_window == 3 and live.demote_fail_rate == 0.5
    assert "audit-9" in live.evidence.rows[0].excluded or any(
        "non-coding" in r.excluded for r in live.evidence.rows
    )
    broken = dict(artifact, params=dict(artifact["params"], demote_window=0))
    with pytest.raises(bench.BenchError, match="demote_window"):
        grades.apply_proposals(broken, decided_by="operator:test", deviation_ref="task-759")
    broken2 = dict(artifact, params=dict(artifact["params"], demote_fail_rate=1.5))
    with pytest.raises(bench.BenchError, match="demote_fail_rate"):
        grades.apply_proposals(broken2, decided_by="operator:test", deviation_ref="task-759")


def test_apply_refuses_artifact_naming_excluded_anomaly_twin(tmp_path, monkeypatch):
    """Directed E-surface regression (codex-luna-max finding): a migrated local
    row whose server twin is the anomaly carries the anomaly *shape* label even
    though the migration already excludes it — a forged artifact naming the
    quiet local ref as promote evidence must be refused. The label is a shape
    property of every row; needs-review stays gated on the exclusion tag."""
    migrated = bench.add_rep(
        **_rep(
            "t-a",
            completed=0,
            rounds=0,
            blockers_found=0,
            recorded_at="2026-09-20T10:00:00Z",
        )
    )
    bench.add_rep(**_rep("t-b", completed=1, grade="A+", effort="xhigh", recorded_at="2026-09-21T10:00:00Z"))
    bench.add_rep(**_rep("t-c", completed=1, grade="A+", effort="xhigh", recorded_at="2026-09-22T10:00:00Z"))
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps = [_remote_row(migrated, 501, host=HOST)]
    view = _view(_entry("grok-hi", "xhigh", "A"))
    evidence = grades.gather_reps(view=view, host=HOST)
    rows = {row.ref: row for row in evidence.rows}
    assert rows["srv:501"].anomaly == "zero-round-fail"
    assert rows["local:1"].anomaly == "zero-round-fail"  # shape recorded despite migration exclusion
    assert "migrated" in rows["local:1"].exclusion_tags and "anomaly" not in rows["local:1"].exclusion_tags
    artifact = grades.proposal_to_json(grades.evaluate(evidence, view), view)
    for item in artifact["results"]:
        if item.get("profile") == "grok-hi" and item.get("action") == "promote":
            item["evidence"].append("local:1")
    canon = _canon_view(_entry("grok-hi", "xhigh", "A"))
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: canon)
    with pytest.raises(bench.BenchError, match="anomalous rep"):
        grades.apply_proposals(artifact, decided_by="operator:test", deviation_ref="task-759")


def test_kimi_code_floating_spelling_is_not_kimi_k3(tmp_path, isolated_cache):
    """CR Major: bare 'kimi-code' is a floating launcher spelling (generation
    ambiguous — same class as 'codex'), never equivalent to kimi-k3; only the
    generation-namespaced 'kimi-code/k3' rename is declared."""
    view = _view(_entry("kimi-k3", "high", "C", model_id="kimi-k3"))
    _seed(
        [
            _rep("t1", profile="kimi-k3", model_id="kimi-code", effort="high", grade="A+"),
            _rep("t2", profile="kimi-k3", model_id="kimi-code", effort="high", grade="A+"),
            _rep("t3", profile="kimi-k3", model_id="kimi-code/k3", effort="high", grade="A+"),
        ]
    )
    proposal = _propose(view)
    result = _result(proposal, "kimi-k3", "high")
    assert result.action == "insufficient"  # one counted PASS, never three
    rows = {row.rep.model_id: row for row in proposal.evidence.rows}
    assert "model mismatch" in rows["kimi-code"].excluded
    assert rows["kimi-code/k3"].excluded == ""


def test_same_task_collapse_orders_by_true_instant_not_wall_clock(tmp_path, isolated_cache):
    """CR Minor: recorded_at with a non-UTC offset must order by the instant,
    not the local wall-clock fields — 10:00+09:00 (01:00Z) predates 02:00Z."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A", recorded_at="2026-09-20T10:00:00+09:00"),
            _rep("t1", effort="xhigh", grade="A+", recorded_at="2026-09-20T02:00:00Z"),
        ]
    )
    proposal = _propose(view)
    rows = sorted(proposal.evidence.rows, key=lambda r: r.ref)
    assert rows[0].rep.grade == "A" and "same task" in rows[0].excluded  # 01:00Z loses
    assert rows[1].rep.grade == "A+" and rows[1].excluded == ""  # 02:00Z is the final rep


def test_alias_duplicate_exclusion_tagged(tmp_path, isolated_cache):
    """CR Minor: alias-collapse exclusions carry the alias-duplicate tag so the
    evidence summary's per-tag breakdown adds up."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed(
        [
            _rep("t1", profile="grok", effort="xhigh", grade="A+"),
            _rep("t1", profile="grok-hi", effort="xhigh", grade="A+"),
        ]
    )
    proposal = _propose(view)
    dupes = [row for row in proposal.evidence.rows if "alias-duplicate" in row.exclusion_tags]
    assert len(dupes) == 1 and dupes[0].excluded


def test_reps_without_task_ref_never_count_independently(tmp_path, isolated_cache):
    """Tester BLOCKER: AC2's one-task-one-count needs a task identity — reps
    with None/blank task_ref cannot prove they are distinct measurements, so
    they are reported, never counted."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    for i, task in enumerate([None, "   ", None, None], start=700):
        _seed_at_id(i, task_ref=task, effort="xhigh", grade="A+")
    proposal = _propose(view)
    result = _result(proposal, "grok-hi", "xhigh")
    assert result.action == "insufficient"  # zero counted rows — four before the fix
    assert not proposal.evidence.counted
    for row in proposal.evidence.rows:
        assert "task ref not recorded" in row.excluded
        assert "no-task-ref" in row.exclusion_tags


def test_default_non_coding_pattern_matches_lowercase_b0x(tmp_path, isolated_cache):
    """Tester SHOULD: slot names are a generated class — the default regex
    matches b0x- as well as B0X-."""
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed([_rep("b0x-live", effort="xhigh", grade="A+")])
    proposal = _propose(view)
    row = proposal.evidence.rows[0]
    assert "non-coding" in row.exclusion_tags and row.excluded


def test_model_match_is_exact_case_sensitive_identity(tmp_path, isolated_cache):
    """Tester BLOCKER r2: case-fold is an undeclared equivalence — a rep
    'vendor/model-a' must not count on a distinct 'vendor/Model-A' rung.
    Only exact identity or a declared MODEL_EQUIVALENCE spelling counts."""
    view = _view(_entry("grok-hi", "xhigh", "C", model_id="vendor/Model-A"))
    _seed(
        [
            _rep("t1", effort="xhigh", model_id="vendor/model-a", grade="A+"),
            _rep("t2", effort="xhigh", model_id="vendor/model-a", grade="A+"),
            _rep("t3", effort="xhigh", model_id="vendor/Model-A", grade="A+"),
        ]
    )
    proposal = _propose(view)
    result = _result(proposal, "grok-hi", "xhigh")
    assert result.action == "insufficient"  # one counted PASS, never promote
    rows = {row.rep.model_id: row for row in proposal.evidence.rows}
    assert "model mismatch" in rows["vendor/model-a"].excluded
    assert rows["vendor/Model-A"].excluded == ""


def test_apply_refuses_artifact_naming_ghost_ref(tmp_path, isolated_cache, monkeypatch):
    """Tester BLOCKER r3: the stored digest binds live evidence, not the
    artifact's claimed refs — a result forged to cite a rep that never existed
    (local:999) or a ref the live result does not rest on is refused."""
    canon = _canon_view(_entry("grok-hi", "xhigh", "C"))
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: canon)
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    proposal = _propose(canon)
    artifact = grades.proposal_to_json(proposal, canon)
    promote = next(r for r in artifact["results"] if r["action"] == "promote")
    promote["evidence"].append("local:999")
    with pytest.raises(bench.BenchError, match="absent from its live evidence"):
        grades.apply_proposals(artifact, decided_by="operator:test", deviation_ref="task-759")


# ---------------------------------------------------------------------------
# task #777 — artifact result-key integrity (v1.2)
# ---------------------------------------------------------------------------


def test_apply_refuses_duplicate_rung_results(tmp_path, isolated_cache, monkeypatch):
    """AC1 (#777): an artifact that lists the same (profile, effort) result
    more than once is refused BEFORE the recorded map is built — the r4
    smuggle was an earlier duplicate carrying local:999 hidden behind a
    later clean duplicate. Both orders refuse, and a duplicate carrying a
    different action refuses too: every item that claims a rung key counts."""
    canon = _canon_view(_entry("grok-hi", "xhigh", "C"))
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: canon)
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    proposal = _propose(canon)
    artifact = grades.proposal_to_json(proposal, canon)
    promote = next(r for r in artifact["results"] if r["action"] == "promote")
    forged = dict(promote, evidence=["local:999"])  # the hidden ghost carrier
    for results in ([forged, promote], [promote, forged]):
        bad = dict(artifact, results=[*artifact["results"], *results])
        with pytest.raises(bench.BenchError, match="more than once"):
            grades.apply_proposals(bad, decided_by="operator:test", deviation_ref="task-777")
    hold_twin = dict(promote, action="hold", target=None)
    bad = dict(artifact, results=[*artifact["results"], hold_twin])
    with pytest.raises(bench.BenchError, match="more than once"):
        grades.apply_proposals(bad, decided_by="operator:test", deviation_ref="task-777")


def test_apply_refuses_result_for_rung_absent_from_live(tmp_path, isolated_cache, monkeypatch):
    """AC2 (#777): an extra recorded result for a rung the live evaluation
    never produced is refused even when every evidence list is empty — the
    live result's existence, not the artifact's claimed refs, is the gate."""
    canon = _canon_view(_entry("grok-hi", "xhigh", "C"))
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: canon)
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    proposal = _propose(canon)
    artifact = grades.proposal_to_json(proposal, canon)
    # opus@high is a bundled-snapshot rung the live evaluation never produced
    # (no evidence at all): a forged promote on it must fail closed.
    artifact["results"].append(
        {
            "profile": "opus",
            "effort": "high",
            "action": "promote",
            "target": "S",
            "current": "A",
            "passes_at": {},
            "ungraded_passes": [],
            "unclean_passes": [],
            "fails": [],
            "evidence": [],
            "note": "forged",
        }
    )
    with pytest.raises(bench.BenchError, match="absent from the live results"):
        grades.apply_proposals(artifact, decided_by="operator:test", deviation_ref="task-777")


def test_apply_refuses_recorded_action_disagreeing_with_live(tmp_path, isolated_cache, monkeypatch):
    """AC2 (#777): a forged promote on a rung live evaluates as insufficient
    is refused even when it borrows the live result's own refs — the digest
    binds live evidence, never the artifact's claimed action/target."""
    canon = _canon_view(_entry("opus", "high", "C"))
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: canon)
    _seed([_rep("t1", profile="opus", effort="high", grade="A")])
    proposal = _propose(canon)
    artifact = grades.proposal_to_json(proposal, canon)
    held = next(r for r in artifact["results"] if r["action"] == "insufficient")
    forged = dict(held, action="promote", target="S")
    artifact["results"] = [forged if r is held else r for r in artifact["results"]]
    with pytest.raises(bench.BenchError, match="disagrees with live evaluation"):
        grades.apply_proposals(artifact, decided_by="operator:test", deviation_ref="task-777")


def test_apply_refuses_artifact_written_under_another_rule_version(tmp_path, isolated_cache, monkeypatch):
    """Tester r1 BLOCKER: rule_version pins the artifact to the semantics
    that wrote it — a v1.1 artifact must not apply under v1.2 even with a
    matching digest, and a missing field refuses the same way."""
    canon = _canon_view(_entry("grok-hi", "xhigh", "C"))
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: canon)
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = grades.proposal_to_json(_propose(canon), canon)
    artifact["rule_version"] = "1.1"
    with pytest.raises(bench.BenchError, match="rule v"):
        grades.apply_proposals(artifact, decided_by="operator:test", deviation_ref="task-777")
    artifact["rule_version"] = None
    with pytest.raises(bench.BenchError, match="rule v"):
        grades.apply_proposals(artifact, decided_by="operator:test", deviation_ref="task-777")


def test_apply_refuses_non_string_claimed_ref(tmp_path, isolated_cache, monkeypatch):
    """Tester r1 BLOCKER: a claimed ref that is not a string (nested list,
    number) must be refused — silently filtering it would pass a corrupt
    artifact through the same gate a ghost string fails."""
    canon = _canon_view(_entry("grok-hi", "xhigh", "C"))
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: canon)
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = grades.proposal_to_json(_propose(canon), canon)
    promote = next(r for r in artifact["results"] if r["action"] == "promote")
    for bad_ref in (["local:999"], 999):
        forged = dict(
            artifact,
            results=[
                dict(r, evidence=[*r["evidence"], bad_ref]) if r is promote else r
                for r in artifact["results"]
            ],
        )
        with pytest.raises(bench.BenchError, match="non-string"):
            grades.apply_proposals(forged, decided_by="operator:test", deviation_ref="task-777")


def test_apply_refuses_any_action_result_for_absent_rung(tmp_path, isolated_cache, monkeypatch):
    """Tester r1 SHOULD: the live-result gate covers every keyed item — a
    hold/insufficient/blocked entry for a rung live never produced is
    refused, not only promote/demote."""
    canon = _canon_view(_entry("grok-hi", "xhigh", "C"))
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: canon)
    _seed([_rep("t1", effort="xhigh", grade="A+"), _rep("t2", effort="xhigh", grade="A+")])
    artifact = grades.proposal_to_json(_propose(canon), canon)
    for action in ("hold", "insufficient", "blocked"):
        forged = dict(
            artifact,
            results=[
                *artifact["results"],
                {
                    "profile": "opus",
                    "effort": "high",
                    "action": action,
                    "target": "A",
                    "current": "A",
                    "passes_at": {},
                    "ungraded_passes": [],
                    "unclean_passes": [],
                    "fails": [],
                    "evidence": [],
                    "note": "forged",
                },
            ],
        )
        with pytest.raises(bench.BenchError, match="absent from the live results"):
            grades.apply_proposals(forged, decided_by="operator:test", deviation_ref="task-777")
