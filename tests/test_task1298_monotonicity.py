"""task #1298 — per-profile effort monotonicity (bench_catalog_not_monotonic).

handoffkeep's ``checkBenchCatalogMonotonicity`` (internal/store/store.go,
answered 400 at internal/api/api.go) checks every profile a catalog write
touches over the merged state: live, known-effort rungs sorted
low<medium<high<xhigh<max must carry non-worsening grades as effort rises;
retired rows, effort "" rows and unknown effort strings never participate;
the first rung is compared against C. ``catalog_monotonicity_violations`` is
the scopefuel-side mirror — propose marks what the rule would refuse as
blocked-by-monotonicity (never applyable) and apply re-checks the exact
stamped set before anything is written, so an --only subset that drops one
half of a valid pair is refused locally.
"""

from __future__ import annotations

import json

import pytest

from scopefuel import bench, cli, grades

HOST = "test-host"
RET = "2026-09-24T00:00:00Z"  # a retired_at stamp


def _entry(profile: str, effort: str, grade: str, **overrides) -> bench.CatalogEntry:
    fields = {
        "profile": profile,
        "effort": effort,
        "model_id": "kimi-k3",
        "pool": "test",
        "grade": grade,
    }
    fields.update(overrides)
    return bench.CatalogEntry(**fields)


def _row(spec: tuple) -> bench.CatalogEntry:
    profile, effort, grade = spec[:3]
    retired = spec[3] if len(spec) > 3 else None
    return _entry(profile, effort, grade, retired_at=retired)


def _canon_view(*entries: bench.CatalogEntry) -> bench.CatalogView:
    """A healthy canon read — the view apply accepts without an override."""
    return bench.CatalogView(
        entries=tuple(entries),
        source=bench.CATALOG_SOURCE_SERVER,
        backend=bench.BENCH_BACKEND_HANDOFFKEEP,
        reason="configured",
        age_s=0.0,
    )


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


def _kimi_rep(task_ref: str, effort: str, grade: str, **overrides) -> dict:
    return _rep(task_ref, profile="kimi-k3", model_id="kimi-k3", effort=effort, grade=grade, **overrides)


def _seed(rows: list[dict]) -> None:
    for row in rows:
        bench.add_rep(**row)


def _propose(view) -> grades.Proposal:
    return grades.evaluate(grades.gather_reps(view=view, host=HOST), view)


def _result(proposal: grades.Proposal, profile: str, effort: str) -> grades.RungResult:
    return next(r for r in proposal.results if r.key == (profile, effort))


# ---------------------------------------------------------------------------
# AC1 — the check mirrors the server's rule, case for case.
#
# Every row is (name, catalog rows, write set, expected violations). The
# comment names the outcome handoffkeep's checkBenchCatalogMonotonicity —
# read from store.go, not re-derived — gives the same write: 200 when the
# merged ladder stays non-worsening, 400 bench_catalog_not_monotonic naming
# the first rung that sits below a lower one.
# ---------------------------------------------------------------------------

AC1_CASES = [
    # --- the server's own cases (tests/bench_test.go TestBenchCatalogAPI)
    (
        "server-mono-base",
        [("bench-cat-mono", "low", "A")],
        {("bench-cat-mono", "high"): "B"},
        [("bench-cat-mono", "high", "B")],
        # server: 400 bench_catalog_not_monotonic (high B below low A)
    ),
    (
        "server-mono-improved",
        [("bench-cat-mono", "low", "A"), ("bench-cat-mono", "high", "B")],
        {("bench-cat-mono", "high"): "A+"},
        [],
        # server: 200 — high A+ stays at-or-above low A
    ),
    (
        "server-retired-rung",
        [("bench-cat-retmono", "high", "B", RET)],
        {("bench-cat-retmono", "low"): "A"},
        [],
        # server: 200 — a retired high=B never constrains a live low=A
    ),
    # --- the 10-08 incident and required additions
    (
        "kimi-k3-high-S-with-max-C",
        [("kimi-k3", "high", "C"), ("kimi-k3", "max", "C")],
        {("kimi-k3", "high"): "S"},
        [("kimi-k3", "max", "C")],
        # server: 400 — max C below the raised high S (the refused write)
    ),
    (
        "lower-rung-promoted-above-higher",
        [("p", "low", "C"), ("p", "medium", "C")],
        {("p", "low"): "S"},
        [("p", "medium", "C")],
        # server: 400 — medium C now below low S
    ),
    (
        "equal-grades-rise",
        [("p", "high", "C"), ("p", "max", "B")],
        {("p", "high"): "B"},
        [],
        # server: 200 — equal grades are non-worsening
    ),
    (
        "effort-empty-row-ignored",
        [("p", "", "A"), ("p", "high", "C"), ("p", "max", "C")],
        {("p", ""): "S+"},
        [],
        # server: 200 — the profile-default rung never participates
    ),
    (
        "effort-empty-write-still-checks-profile",
        [("p", "", "C"), ("p", "high", "A"), ("p", "max", "B")],
        {("p", ""): "S+"},
        [("p", "max", "B")],
        # server: 400 — any write touching the profile runs its ladder check
    ),
    (
        "unknown-effort-row-ignored",
        [("p", "high", "C"), ("p", "turbo", "A")],
        {("p", "high"): "S"},
        [],
        # server: 200 — "turbo" is not a ladder rung and never participates
    ),
    (
        "unknown-effort-write-still-checks-profile",
        [("p", "high", "A"), ("p", "max", "B")],
        {("p", "turbo"): "S"},
        [("p", "max", "B")],
        # server: 400 — the unknown row does not participate but the touched
        # profile's ladder is still checked
    ),
    (
        "gap-rung-promotion-ok",
        [("p", "high", "C"), ("p", "max", "C")],
        {("p", "max"): "S"},
        [],
        # server: 200 — C,S over the gap is non-worsening
    ),
    (
        "gap-rung-demotion-violates",
        [("p", "high", "S"), ("p", "max", "A")],
        {("p", "max"): "C"},
        [("p", "max", "C")],
        # server: 400 — max C below high S across the rung gap
    ),
    (
        "pair-fine-together",
        [("p", "high", "C"), ("p", "max", "C")],
        {("p", "high"): "S", ("p", "max"): "S"},
        [],
        # server: 200 — the batch is checked merged, S,S is monotonic
    ),
    (
        "pair-half-alone",
        [("p", "high", "C"), ("p", "max", "C")],
        {("p", "high"): "S"},
        [("p", "max", "C")],
        # server: 400 — dropping the max half breaks the pair
    ),
    (
        "demote-pair-fine-together",
        [("p", "high", "A"), ("p", "max", "A")],
        {("p", "high"): "C", ("p", "max"): "C"},
        [],
        # server: 200 — C,C stays monotonic
    ),
    (
        "demote-higher-half-alone",
        [("p", "high", "A"), ("p", "max", "A")],
        {("p", "max"): "C"},
        [("p", "max", "C")],
        # server: 400 — demoting only the higher rung puts C below A
    ),
    (
        "untouched-profile-not-checked",
        [("p", "high", "A"), ("p", "max", "B"), ("q", "high", "C")],
        {("q", "high"): "S"},
        [],
        # server: 200 — p's canon is already non-monotonic but the write
        # never touches p, so p's ladder is not consulted
    ),
    (
        "lower-worse-than-higher-ok",
        [("p", "high", "S")],
        {("p", "low"): "C"},
        [],
        # server: 200 — grades may improve as effort rises
    ),
]


@pytest.mark.parametrize(
    ("name", "rows", "write", "want"),
    [pytest.param(*case, id=case[0]) for case in AC1_CASES],
)
def test_catalog_monotonicity_parity(name, rows, write, want, isolated_cache):
    entries = [_row(spec) for spec in rows]
    got = grades.catalog_monotonicity_violations(entries, write)
    assert [(v.profile, v.effort, v.grade) for v in got] == want, name


def test_violation_names_the_binding_lower_rung(isolated_cache):
    """The server's message names the violating rung; ours also records the
    lower rung whose grade binds it, so a blocked demotion can point down."""
    got = grades.catalog_monotonicity_violations(
        [_entry("p", "high", "S"), _entry("p", "max", "A")],
        {("p", "max"): "C"},
    )
    assert got == [
        grades.MonotonicityViolation(
            profile="p", effort="max", grade="C", lower_effort="high", lower_grade="S"
        )
    ]


# ---------------------------------------------------------------------------
# AC2 — propose marks the kimi-k3@high change blocked-by-monotonicity.
# ---------------------------------------------------------------------------


def _kimi_view() -> bench.CatalogView:
    return _canon_view(
        _entry("kimi-k3", "high", "C"),
        _entry("kimi-k3", "max", "C"),
        _entry("grok-hi", "xhigh", "C", model_id="grok-4.7"),
    )


def test_propose_marks_high_blocked_when_max_stays_c(tmp_path, isolated_cache):
    """The 10-08 case: kimi-k3@high measured C -> S while @max stays C —
    proposed, shown, recorded, never applyable."""
    view = _kimi_view()
    _seed(
        [
            _kimi_rep("k1", "high", "S"),
            _kimi_rep("k2", "high", "S"),
            _rep("g1", effort="xhigh", grade="A+"),
            _rep("g2", effort="xhigh", grade="A+"),
        ]
    )
    proposal = _propose(view)

    high = _result(proposal, "kimi-k3", "high")
    assert (high.action, high.row.grade, high.target) == ("promote", "C", "S")
    block = high.monotonicity_block
    assert block is not None
    assert (block.profile, block.effort, block.grade) == ("kimi-k3", "max", "C")
    assert block.direction == "higher"
    assert "kimi-k3@max" in block.reason and "C" in block.reason

    assert [r.key for r in proposal.blocked_changes()] == [("kimi-k3", "high")]
    assert [r.key for r in proposal.applyable_changes()] == [("grok-hi", "xhigh")]

    text = grades.render_proposal(proposal, view)
    assert "blocked-by-monotonicity" in text
    assert "promote kimi-k3@high C -> S" in text
    assert "kimi-k3@max grade C" in text
    # The other proposal is unchanged byte for byte versus main's rendering.
    assert "  promote grok-hi@xhigh C -> A+\n    rule: >=2 clean PASSes at grade A+" in text

    payload = grades.proposal_to_json(proposal, view)
    blocked = payload["monotonicity"]["blocked"]
    assert blocked == [
        {
            "profile": "kimi-k3",
            "effort": "high",
            "action": "promote",
            "current": "C",
            "target": "S",
            "status": "blocked-by-monotonicity",
            "conflict": {"profile": "kimi-k3", "effort": "max", "grade": "C"},
            "direction": "higher",
            "reason": "would leave kimi-k3@max grade C below lower effort",
        }
    ]
    # The recorded per-rung result keeps its shape — the block disclosure is
    # additive so old readers and the digest see the same artifact.
    recorded = next(r for r in payload["results"] if (r["profile"], r["effort"]) == ("kimi-k3", "high"))
    assert recorded["action"] == "promote" and "monotonicity" not in recorded


def test_propose_pair_offered_when_each_rung_has_evidence(tmp_path, isolated_cache):
    """Both halves of a monotonic pair are applyable — each on its own
    evidence; no change is invented for the unmeasured rung."""
    view = _canon_view(
        _entry("kimi-k3", "high", "C"),
        _entry("kimi-k3", "max", "C"),
    )
    _seed(
        [
            _kimi_rep("kh1", "high", "S"),
            _kimi_rep("kh2", "high", "S"),
            _kimi_rep("km1", "max", "S"),
            _kimi_rep("km2", "max", "S"),
        ]
    )
    proposal = _propose(view)
    assert {r.key for r in proposal.applyable_changes()} == {
        ("kimi-k3", "high"),
        ("kimi-k3", "max"),
    }
    assert proposal.blocked_changes() == []


def test_propose_demote_blocked_below_lower_rung(tmp_path, isolated_cache):
    """The mirrored case: a demotion that would sit below the lower rung's
    grade is blocked naming that lower rung."""
    view = _canon_view(
        _entry("kimi-k3", "high", "S"),
        _entry("kimi-k3", "max", "S"),
    )
    _seed(
        [
            _kimi_rep("km1", "max", "B", completed=0, blockers_found=1),
            _kimi_rep("km2", "max", "B", completed=0, blockers_found=1),
        ]
    )
    proposal = _propose(view)
    maxed = _result(proposal, "kimi-k3", "max")
    assert (maxed.action, maxed.target) == ("demote", "C")
    block = maxed.monotonicity_block
    assert block is not None
    assert (block.profile, block.effort, block.grade) == ("kimi-k3", "high", "S")
    assert block.direction == "lower"
    assert "kimi-k3@high" in block.reason and "S" in block.reason
    assert proposal.applyable_changes() == []


# ---------------------------------------------------------------------------
# AC3 — apply re-checks the exact stamped set; an --only half-pair is
# refused locally, before anything reaches the wire.
# ---------------------------------------------------------------------------


def _pair_artifact(monkeypatch):
    """A proposal artifact offering the monotonic kimi pair, with the canon
    read faked for both propose and apply."""
    view = _canon_view(
        _entry("kimi-k3", "high", "C"),
        _entry("kimi-k3", "max", "C"),
    )
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: view)
    _seed(
        [
            _kimi_rep("kh1", "high", "S"),
            _kimi_rep("kh2", "high", "S"),
            _kimi_rep("km1", "max", "S"),
            _kimi_rep("km2", "max", "S"),
        ]
    )
    proposal = _propose(view)
    return proposal, grades.proposal_to_json(proposal, view)


def _no_http(monkeypatch) -> list:
    """A fake transport that records every request — zero must be made."""
    calls: list = []
    monkeypatch.setattr(bench, "request_json", lambda *a, **kw: calls.append((a, kw)) or {})
    return calls


def test_apply_only_half_of_pair_refused_locally(tmp_path, monkeypatch, isolated_cache):
    """AC3: --only kimi-k3@high drops the max half the pair needs — refused
    with the server's own error name and zero HTTP requests."""
    proposal, payload = _pair_artifact(monkeypatch)
    assert {r.key for r in proposal.applyable_changes()} == {("kimi-k3", "high"), ("kimi-k3", "max")}
    calls = _no_http(monkeypatch)
    with pytest.raises(bench.BenchError, match="bench_catalog_not_monotonic"):
        grades.apply_proposals(
            payload,
            decided_by="operator:test",
            deviation_ref="hk:task/1298",
            only=[("kimi-k3", "high")],
        )
    assert calls == []


def test_apply_only_max_half_of_pair_proceeds(tmp_path, monkeypatch, isolated_cache):
    """The max half needs nothing below it — a valid subset proceeds."""
    proposal, payload = _pair_artifact(monkeypatch)
    entries, live, _view = grades.apply_proposals(
        payload,
        decided_by="operator:test",
        deviation_ref="hk:task/1298",
        only=[("kimi-k3", "max")],
    )
    stamped = {e.key: e for e in entries}
    assert stamped[("kimi-k3", "max")].grade == "S"
    assert stamped[("kimi-k3", "high")].grade == "C"  # untouched
    assert live.applyable_changes() == proposal.applyable_changes()


def test_apply_full_pair_proceeds(tmp_path, monkeypatch, isolated_cache):
    """The whole approved set is monotonic — stamped unchanged."""
    proposal, payload = _pair_artifact(monkeypatch)
    assert {r.key for r in proposal.applyable_changes()} == {("kimi-k3", "high"), ("kimi-k3", "max")}
    calls = _no_http(monkeypatch)
    entries, _live, _view = grades.apply_proposals(
        payload, decided_by="operator:test", deviation_ref="hk:task/1298"
    )
    stamped = {e.key: e for e in entries}
    assert stamped[("kimi-k3", "high")].grade == "S"
    assert stamped[("kimi-k3", "max")].grade == "S"
    assert stamped[("kimi-k3", "high")].decided_by == "operator:test"
    assert calls == []


def test_apply_only_names_blocked_change_refused(tmp_path, monkeypatch, isolated_cache):
    """--only naming a blocked-by-monotonicity change is a clean refusal."""
    view = _canon_view(
        _entry("kimi-k3", "high", "C"),
        _entry("kimi-k3", "max", "C"),
    )
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: view)
    _seed([_kimi_rep("kh1", "high", "S"), _kimi_rep("kh2", "high", "S")])
    payload = grades.proposal_to_json(_propose(view), view)
    with pytest.raises(bench.BenchError, match="blocked-by-monotonicity"):
        grades.apply_proposals(
            payload,
            decided_by="operator:test",
            deviation_ref="hk:task/1298",
            only=[("kimi-k3", "high")],
        )


def test_cli_apply_only_half_pair_refused(tmp_path, monkeypatch, capsys, isolated_cache):
    """The CLI path: exit 2, the named error, no output file."""
    view = _canon_view(
        _entry("kimi-k3", "high", "C"),
        _entry("kimi-k3", "max", "C"),
    )
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: view)
    _seed(
        [
            _kimi_rep("kh1", "high", "S"),
            _kimi_rep("kh2", "high", "S"),
            _kimi_rep("km1", "max", "S"),
            _kimi_rep("km2", "max", "S"),
        ]
    )
    artifact = tmp_path / "proposal.json"
    out_file = tmp_path / "catalog.json"
    assert cli.main(["grades", "propose", "--json", "--out", str(artifact)]) == 0
    rc = cli.main(
        [
            "grades",
            "apply",
            "--proposal",
            str(artifact),
            "--out",
            str(out_file),
            "--decided-by",
            "operator:test",
            "--only",
            "kimi-k3@high",
        ]
    )
    assert rc == 2
    assert "bench_catalog_not_monotonic" in capsys.readouterr().err
    assert not out_file.exists()


def test_cli_apply_reports_blocked_change(tmp_path, monkeypatch, capsys, isolated_cache):
    """A proposal whose only change is blocked writes an audit artifact —
    nothing is stamped, the block is disclosed."""
    view = _canon_view(
        _entry("kimi-k3", "high", "C"),
        _entry("kimi-k3", "max", "C"),
    )
    monkeypatch.setattr(bench, "read_catalog", lambda **kw: view)
    _seed([_kimi_rep("kh1", "high", "S"), _kimi_rep("kh2", "high", "S")])
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
    assert payload["catalog"] == []
    withheld = payload["not_applied"]
    assert len(withheld) == 1 and withheld[0]["effort"] == "high"
    assert "blocked-by-monotonicity" in withheld[0]["status"]
    out = capsys.readouterr().out
    assert "not applied (blocked-by-monotonicity)" in out


# ---------------------------------------------------------------------------
# AC4 — the invariants, stated as sentences the tests enforce.
# ---------------------------------------------------------------------------


def test_invariant_propose_never_offers_an_applyable_change_the_server_rejects(tmp_path, isolated_cache):
    """(a) A propose output never contains an applyable change that the
    server rule rejects in the merged state."""
    scenarios = [
        (
            _canon_view(
                _entry("kimi-k3", "high", "C"),
                _entry("kimi-k3", "max", "C"),
                _entry("grok-hi", "xhigh", "C", model_id="grok-4.7"),
            ),
            [
                _kimi_rep("k1", "high", "S"),
                _kimi_rep("k2", "high", "S"),
                _rep("g1", effort="xhigh", grade="A+"),
                _rep("g2", effort="xhigh", grade="A+"),
            ],
        ),
        (
            _canon_view(_entry("kimi-k3", "high", "S"), _entry("kimi-k3", "max", "S")),
            [
                _kimi_rep("m1", "max", "B", completed=0, blockers_found=1),
                _kimi_rep("m2", "max", "B", completed=0, blockers_found=1),
            ],
        ),
    ]
    for view, reps in scenarios:
        _seed(reps)
        proposal = _propose(view)
        offered = {r.key: r.target for r in proposal.applyable_changes()}
        canon = [e for e in view.entries]
        assert grades.catalog_monotonicity_violations(canon, offered) == []


def test_invariant_apply_never_stamps_a_write_the_server_rejects(tmp_path, monkeypatch, isolated_cache):
    """(b) Apply never sends a write the server rule rejects — a subset that
    would break the merged state is refused, and whatever is approved stamps
    monotonic."""
    proposal, payload = _pair_artifact(monkeypatch)
    with pytest.raises(bench.BenchError, match="bench_catalog_not_monotonic"):
        grades.apply_proposals(
            payload,
            decided_by="operator:test",
            deviation_ref="hk:task/1298",
            only=[("kimi-k3", "high")],
        )
    for only in (None, [("kimi-k3", "max")], [("kimi-k3", "high"), ("kimi-k3", "max")]):
        entries, live, _view = grades.apply_proposals(
            payload,
            decided_by="operator:test",
            deviation_ref="hk:task/1298",
            only=only,
        )
        canon = {e.key: e.grade for e in _view.entries if e.profile not in live.snapshot_profiles}
        stamped = {
            key: entry.grade for key, entry in ((e.key, e) for e in entries) if canon.get(key) != entry.grade
        }
        assert grades.catalog_monotonicity_violations([e for e in _view.entries], stamped) == []


def test_invariant_exempt_rows_never_participate(isolated_cache):
    """(c) Retired, effort '' and unknown-effort rows never participate —
    a promotion beside them cannot be blocked by them."""
    entries = [
        _entry("p", "", "S"),
        _entry("p", "turbo", "S+"),
        _entry("p", "low", "A"),
        _entry("p", "high", "B", retired_at=RET),
    ]
    assert grades.catalog_monotonicity_violations(entries, {("p", "low"): "S"}) == []
