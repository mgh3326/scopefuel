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
        "model_id": "model-x",
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


def test_demote_on_fail_at_placement(tmp_path, isolated_cache):
    """A completed=0 rep on an at-placement task demotes one step below it."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed(
        [
            _rep("t1", effort="xhigh", grade="A", completed=1),  # 1 clean A pass (< 2)
            _rep("t2", effort="xhigh", grade="A+", completed=0),  # FAIL at placement
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"
    assert result.target == "A"  # one below the failed A+ claim


def test_demote_below_placement_drops_below_failed_grade(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "xhigh", "S"))
    _seed([_rep("t1", effort="xhigh", grade="B", completed=0)])
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"
    assert result.target == "C"  # can't do B work -> below B


def test_demote_ungraded_fail_steps_below_current(tmp_path, isolated_cache):
    """An ungraded FAIL is fail-closed: treated as failing at the placement."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed([_rep("t1", effort="xhigh", completed=0)])
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"
    assert result.target == "A"


def test_demote_floors_at_c(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "xhigh", "C"))
    _seed([_rep("t1", effort="xhigh", grade="C", completed=0)])
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
            _rep("t3", effort="xhigh", grade="S", completed=0),  # overreach fail
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
            _rep("t3", effort="xhigh", grade="A+", completed=0),
        ]
    )
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "conflicted"
    assert result.target == "A+"
    assert _propose(view).changes() == []


def test_rollback_marker_is_fail_evidence(tmp_path, isolated_cache):
    """A [rollback] notes marker is FAIL evidence even on a completed rep."""
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed([_rep("t1", effort="xhigh", completed=1, notes="merged then reverted [rollback]")])
    result = _result(_propose(view), "grok-hi", "xhigh")
    assert result.action == "demote"
    assert result.evidence_refs


def test_post_merge_blocker_marker_is_fail(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "xhigh", "A+"))
    _seed([_rep("t1", effort="xhigh", completed=1, notes="[post-merge-blocker] found by ops")])
    assert _result(_propose(view), "grok-hi", "xhigh").action == "demote"


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
    """builder-grok pins xhigh, but a rep recorded at effort=high measured high."""
    view = _view(_entry("grok-hi", "", "S"), _entry("grok-hi", "xhigh", "C"))
    _seed([_rep("t1", effort="high", grade="S")])
    proposal = _propose(view)
    # (grok-hi, high) has no row -> judged by the profile default row "".
    result = _result(proposal, "grok-hi", "")
    assert any(item.rep.task_ref == "t1" for item in result.counted)


def test_spelling_pin_used_when_effort_unrecorded(tmp_path, isolated_cache):
    view = _view(_entry("grok-hi", "", "S"), _entry("grok-hi", "xhigh", "C"))
    _seed([_rep("t1", grade="S")])  # builder-grok, no effort -> pin xhigh
    proposal = _propose(view)
    assert _result(proposal, "grok-hi", "xhigh").counted
    assert all(r.key != ("grok-hi", "") for r in proposal.results)


def test_launcher_default_effort_fills_unpinned(tmp_path, isolated_cache):
    """builder-sol pins nothing — the launcher default (codex-sol@max) applies."""
    view = _view(_entry("codex-sol", "max", "S+"), _entry("codex-sol", "high", "C"))
    _seed([_rep("t1", profile="builder-sol", grade="S")])
    proposal = _propose(view)
    assert _result(proposal, "codex-sol", "max").counted


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
    bad_params.write_text(json.dumps({"min_passes": 2, "params": ["cli_exclusions"]}))
    assert cli.main(_apply_args(bad_params, tmp_path / "o1.json")) == 2
    assert "params" in capsys.readouterr().err
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
    row = _rep(f"t{rep_id}", **overrides)
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
