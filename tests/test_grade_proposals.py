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
    assert cli_ex.old_ref == "local:1" and cli_ex.new_ref == "local:2"


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
    assert "desktop:80" in text


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
    _cli_view(
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


def test_cli_apply_refuses_stale_proposal(tmp_path, monkeypatch, capsys):
    """apply can never write without the matching propose evidence: a rep
    recorded after propose makes the artifact stale and apply refuses."""
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
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
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
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
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
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


def test_server_ref_syntax_in_exclusions(tmp_path, monkeypatch):
    rep = bench.add_rep(**_rep("t1", effort="xhigh", grade="A+"))
    _remote_backend(tmp_path, monkeypatch)
    # Nothing remote; the bare id under a handoffkeep backend names the server
    # side, so a local row needs an explicit local: prefix to be excluded.
    view = _view(_entry("grok-hi", "xhigh", "C"))
    evidence = grades.gather_reps(view=view, exclusions=[("1", "2")], host=HOST)
    cli_ex = next(e for e in evidence.exclusions if e.origin == "cli")
    assert cli_ex.old_ref == "srv:1"  # bare -> shared store
    counted = {r.ref for r in evidence.rows if not r.excluded}
    assert f"local:{rep.id}" in counted
    evidence2 = grades.gather_reps(view=view, exclusions=[("local:1", "local:2")], host=HOST)
    assert f"local:{rep.id}" not in {r.ref for r in evidence2.rows if not r.excluded}


def test_local_backend_discloses_server_unread(tmp_path, monkeypatch, capsys):
    """AC3: under the local backend the proposal says the server was not read."""
    _cli_view(monkeypatch, [_entry("grok-hi", "xhigh", "C")])
    _seed([_rep("t1", effort="xhigh", grade="A")])
    assert cli.main(["grades", "propose"]) == 0
    out = capsys.readouterr().out
    assert "source=local" in out
    assert "server reps were not read" in out
