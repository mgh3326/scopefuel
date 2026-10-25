"""hk 1795 — d1786ap borrowed rungs: a cross-grade listing, not a placement.

``recommend.BORROWED_RUNGS`` names the operator-directive rows (hk:task/1786):
``codex-sol@xhigh`` lists at ``S`` while the table places it at S+ keeping its
escalation gate, and ``sonnet@high`` lists at ``B`` while the table places it at
A. (``opus@low``'s former A+ listing was removed by r1025p1apply, hk 2007.) An
entry lists only while the live table (canon view or
snapshot) carries the rung exactly one grade above its listed grade — anywhere
else or absent, it is inert. A borrowed row is a ranked candidate ordered like
a one-up row, tagged ``[borrowed <placed grade>]``; a row qualifying as both
one-up and borrowed lists once, as borrowed.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
from dataclasses import replace

import pytest

from scopefuel import bench, cli, launch
from scopefuel import recommend as recommend_mod
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    _GRADE_ORDER,
    BORROWED_RUNGS,
    GRADE_TABLE,
    Profile,
    _alt_candidates,
    _borrowed_profiles,
    _recommend_rank_key,
    gate_check,
    recommend,
    recommend_dict,
)

NOW = dt.datetime(2026, 10, 21, 12, 0, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
GRADES = _GRADE_ORDER


def _bucket(window: str = "7d", scope: Scope | None = None, used: float = 10.0) -> Bucket:
    return Bucket(
        label=window,
        window=window,
        used_pct=used,
        resets_at=(NOW + dt.timedelta(days=6)).isoformat(),
        scope=scope or Scope("account"),
        horizon="week",  # type: ignore[arg-type]
    )


def _pool_providers(table=None) -> list[ProviderResult]:
    """One measurable ProviderResult per (provider, group) the table routes to."""
    table = GRADE_TABLE if table is None else table
    pool_scopes: dict[str, set[str | None]] = {}
    for profiles in table.values():
        for profile in profiles:
            provider_id, group_name = recommend_mod.resolved_pool(profile)
            if provider_id:
                pool_scopes.setdefault(provider_id, set()).add(group_name)
    return [
        ProviderResult(
            id=provider_id,
            pool_class="preserve",
            buckets=[
                _bucket(scope=Scope("account") if g is None else Scope("group", g)) for g in group_names
            ],
        )
        for provider_id, group_names in pool_scopes.items()
    ]


def _ranked(output: str) -> list[str]:
    return [line for line in output.splitlines() if line[:1].isdigit()]


def _line_label(line: str) -> str:
    tokens = line.split(".", 1)[1].strip().split()
    while tokens and tokens[0].startswith("🔥"):
        tokens.pop(0)
    label = tokens[0]
    if len(tokens) >= 3 and tokens[1] == "--effort":
        label += f" --effort {tokens[2]}"
    elif len(tokens) >= 3 and tokens[1] == "(effort":
        label += f" (effort {tokens[2]}"
    return label


def _ranked_labels(output: str) -> list[str]:
    return [_line_label(line) for line in _ranked(output)]


def _server_view(rows=None):
    """A server-source CatalogView built from catalog-row dicts."""
    rows = rows or [entry.as_dict() for entry in bench.catalog_snapshot()]
    entries = bench._catalog_from_payload({"catalog": rows})
    return bench.CatalogView(tuple(entries), source="server", backend="handoffkeep")


def _canon_table(rows=None):
    table = bench._catalog_grade_table(_server_view(rows))
    assert table is not None
    return table


def _seed_rows_with(profile: str, effort: str, **patch):
    """Bundled-seed catalog rows with one (profile, effort) row patched."""
    rows = [entry.as_dict() for entry in bench.catalog_snapshot()]
    target = next(row for row in rows if row["profile"] == profile and row["effort"] == effort)
    target.update(patch)
    return rows


# ── constant shape ────────────────────────────────────────────────────────────


def test_borrowed_rungs_is_exactly_the_directive_entries():
    assert BORROWED_RUNGS == (
        ("codex-sol", "xhigh", "S", "default", "operator hk:task/1786 comment 6689"),
        ("sonnet", "high", "B", "default", "operator hk:task/1786 comment 6689"),
    )


def test_borrowed_entries_list_at_their_named_grade_only():
    for grade in GRADES:
        listed = {(p.name, p.launcher_effort) for p, _ in _borrowed_profiles(grade, GRADE_TABLE)}
        expected = (
            {("codex-sol", "xhigh")} if grade == "S" else ({("sonnet", "high")} if grade == "B" else set())
        )
        assert listed == expected, grade


# ── AC1: snapshot fixture ─────────────────────────────────────────────────────


def test_recommend_s_lists_codex_sol_xhigh_tagged_borrowed_splus_candidate():
    out = recommend(_pool_providers(), "S", today=TODAY, now=NOW)
    # hk 1862 (comment 6689): the entry's listed gate is default — at S it
    # ranks as a normal candidate, tagged borrowed. Its own S+ row keeps
    # gate=escalation.
    lines = [line for line in _ranked(out) if "codex-sol --effort xhigh" in line]
    assert len(lines) == 1 and "[borrowed S+]" in lines[0], out


def test_recommend_dict_marks_borrowed_rows():
    s = recommend_dict(_pool_providers(), "S", today=TODAY, now=NOW)
    sol = next(row for row in s["rows"] if row["profile"] == "codex-sol" and "xhigh" in str(row["display"]))
    assert sol["kind"] == "candidate"
    assert sol["borrowed"] is True
    assert sol["placed_grade"] == "S+"
    assert sol["one_up"] is False

    b = recommend_dict(_pool_providers(), "B", today=TODAY, now=NOW)
    sonnet = next(row for row in b["rows"] if row["profile"] == "sonnet" and "high" in str(row["display"]))
    assert sonnet["kind"] == "candidate"
    assert sonnet["borrowed"] is True
    assert sonnet["placed_grade"] == "A"
    assert sonnet["one_up"] is False


# ── AC1: canon fixture ────────────────────────────────────────────────────────


def test_canon_fixture_lists_both_borrowed_rows():
    table = _canon_table()
    providers = _pool_providers(table)
    out = recommend(providers, "S", today=TODAY, now=NOW, grade_table=table)
    entry_line = next(line for line in out.splitlines() if "codex-sol --effort xhigh" in line)
    assert "[borrowed S+]" in entry_line

    out = recommend(providers, "B", today=TODAY, now=NOW, grade_table=table)
    lines = [line for line in _ranked(out) if "sonnet --effort high" in line]
    assert len(lines) == 1 and "[borrowed A]" in lines[0], out


def test_canon_fixture_json_marks_borrowed_rows():
    table = _canon_table()
    providers = _pool_providers(table)
    s = recommend_dict(providers, "S", today=TODAY, now=NOW, grade_table=table)
    sol = next(row for row in s["rows"] if row.get("borrowed") is True and row["profile"] == "codex-sol")
    assert sol["placed_grade"] == "S+" and sol["kind"] == "candidate"
    b = recommend_dict(providers, "B", today=TODAY, now=NOW, grade_table=table)
    sonnet = next(row for row in b["rows"] if row.get("borrowed") is True and row["profile"] == "sonnet")
    assert sonnet["placed_grade"] == "A" and sonnet["kind"] == "candidate"


def test_recommend_json_cli_marks_borrowed(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {
            name: (lambda provider_id=name: ProviderResult(id=provider_id, buckets=[_bucket()]))
            for name in {recommend_mod.resolved_pool(p)[0] for ps in GRADE_TABLE.values() for p in ps}
        },
    )
    assert cli.main(["--recommend", "S", "--json", "--no-cache"]) == 0
    payload = json.loads(capsys.readouterr().out)
    sol = next(
        row for row in payload["rows"] if row["profile"] == "codex-sol" and "xhigh" in str(row["display"])
    )
    assert sol["borrowed"] is True and sol["placed_grade"] == "S+"


# ── AC2: inert cases, one each ────────────────────────────────────────────────
# hk 2007 removed the opus@low A+ entry; the inert cases below run on the
# sonnet@high B entry (listed at B, expected to be placed at A).


def test_inert_when_canon_places_sonnet_high_at_b_itself():
    """A canon move to the listed grade makes it an exact row, not borrowed."""
    table = _canon_table(_seed_rows_with("sonnet", "high", grade="B"))
    out = recommend(_pool_providers(table), "B", today=TODAY, now=NOW, grade_table=table)
    lines = [line for line in _ranked(out) if "sonnet --effort high" in line]
    assert len(lines) == 1 and "[borrowed" not in lines[0] and "[one-up" not in lines[0]
    assert "[borrowed" not in out


def test_inert_when_canon_places_sonnet_high_at_s():
    """Placed two grades above the listed grade the entry stays silent."""
    table = _canon_table(_seed_rows_with("sonnet", "high", grade="S"))
    for grade in GRADES:
        out = recommend(_pool_providers(table), grade, today=TODAY, now=NOW, grade_table=table)
        assert "[borrowed A]" not in out, grade


def test_inert_when_canon_retires_sonnet_high():
    """A retired rung is not carried — the entry finds nothing to list."""
    table = _canon_table(_seed_rows_with("sonnet", "high", retired_at="2026-10-21T00:00:00Z"))
    for grade in GRADES:
        out = recommend(_pool_providers(table), grade, today=TODAY, now=NOW, grade_table=table)
        assert "sonnet --effort high" not in out and "[borrowed A]" not in out, grade


def test_inert_when_codex_sol_xhigh_placed_at_s():
    """codex-sol@xhigh one grade below its listed grade is not borrowed.

    The canon itself can never say this — _catalog_grade_table refuses a Sol
    row outside S+ (the _SOL_PROFILES placement rule stands). The table-level
    fixture pins the inert outcome directly.
    """
    rows = _seed_rows_with("codex-sol", "xhigh", grade="S")
    assert bench._catalog_grade_table(_server_view(rows)) is None  # Sol rule refuses

    table = {grade: list(profiles) for grade, profiles in GRADE_TABLE.items()}
    sol = next(p for p in table["S+"] if p.name == "codex-sol" and p.launcher_effort == "xhigh")
    table["S+"] = [p for p in table["S+"] if p is not sol]
    table["S"].append(sol)
    out = recommend(_pool_providers(table), "S", today=TODAY, now=NOW, grade_table=table)
    assert "[borrowed" not in out
    # it still lists — as its own exact-grade escalation row, untagged
    entry_line = next(line for line in out.splitlines() if "codex-sol --effort xhigh" in line)
    assert "[one-up" not in entry_line and "[borrowed" not in entry_line


# ── AC3: differential over all six grades, snapshot + canon ──────────────────


def _added_lines(without: str, with_: str) -> list[str]:
    return [
        line[2:] for line in difflib.ndiff(without.splitlines(), with_.splitlines()) if line.startswith("+ ")
    ]


@pytest.mark.parametrize("fixture", ["snapshot", "canon"])
def test_everything_else_is_byte_identical_text_and_json(fixture, monkeypatch):
    """The whole change adds exactly the two borrowed rows — every other grade
    and every other row stays byte-identical to the run with no BORROWED_RUNGS."""
    table = GRADE_TABLE if fixture == "snapshot" else _canon_table()
    providers = _pool_providers(table)
    with_rows = {
        grade: (
            recommend(providers, grade, today=TODAY, now=NOW, grade_table=table),
            recommend_dict(providers, grade, today=TODAY, now=NOW, grade_table=table)["rows"],
        )
        for grade in GRADES
    }
    monkeypatch.setattr(recommend_mod, "BORROWED_RUNGS", ())
    base_rows = {
        grade: (
            recommend(providers, grade, today=TODAY, now=NOW, grade_table=table),
            recommend_dict(providers, grade, today=TODAY, now=NOW, grade_table=table)["rows"],
        )
        for grade in GRADES
    }
    for grade in GRADES:
        text_with, json_with = with_rows[grade]
        text_base, json_base = base_rows[grade]
        # hk 2007: A+ carries no borrowed entry any more, so A+ is byte-identical
        # to the run with BORROWED_RUNGS emptied, like every other grade but S, B.
        if grade not in ("S", "B"):
            assert text_with == text_base, grade
            assert json_with == json_base, grade
            continue
        # JSON: the borrowed listing is the only added row — at B the same
        # rung flips one_up -> borrowed (dedupe), so the row sets match once
        # that rung is set aside either way.
        borrowed_rows = [row for row in json_with if row.get("borrowed") is True]
        assert len(borrowed_rows) == 1, (grade, json_with)
        rung = (borrowed_rows[0]["profile"], borrowed_rows[0]["display"])

        def key(r):
            return (r["profile"], r["display"], r["kind"])

        without = [key(r) for r in json_with if (r["profile"], r["display"]) != rung]
        base = [key(r) for r in json_base if (r["profile"], r["display"]) != rung]
        assert sorted(without) == sorted(base), grade

        # Text: every row is byte-identical up to the rank renumbering an
        # inserted row causes — stripping the "N." prefix leaves the borrowed
        # listing as the only diff.
        def strip(text: str) -> list[str]:
            return [line.split(".", 1)[1] if line[:1].isdigit() else line for line in text.splitlines()]

        added = _added_lines("\n".join(strip(text_base)), "\n".join(strip(text_with)))
        assert added, grade
        if grade == "B":
            joined = "\n".join(added)
            assert "sonnet --effort high" in joined and "[borrowed A]" in joined
        else:
            joined = "\n".join(added)
            assert "codex-sol --effort xhigh" in joined and "[borrowed S+]" in joined


# ── ordering: borrowed sorts like one-up ─────────────────────────────────────


def test_better_value_borrowed_outranks_exact_exact_first_only_on_full_tie():
    """Same pool, same quota, no boost: the value-order slot precedes the
    exact/borrowed tie-break, so a better-value borrowed row lists first — and
    a full tie restores exact-first."""
    table = {grade: [] for grade in GRADES}
    table["A"].append(
        Profile("sonnet", "Sonnet (high)", 62.0, launcher_effort="high", aa_model_id="m-borrow")
    )
    table["B"].append(Profile("haiku", "Exact", 62.0, aa_model_id="m-exact"))
    providers = [ProviderResult(id="claude", pool_class="preserve", buckets=[_bucket()])]

    def price(model_id: str, blended: float) -> bench.ModelPrice:
        return bench.ModelPrice(
            model_id=model_id,
            price_1m_blended_3_to_1=blended,
            price_1m_input_tokens=1.0,
            price_1m_output_tokens=4.0,
            captured_at="2026-10-21T00:00:00+00:00",
        )

    ranked = _ranked_labels(
        recommend(
            providers,
            "B",
            today=TODAY,
            now=NOW,
            grade_table=table,
            model_prices={"m-borrow": price("m-borrow", 5.0), "m-exact": price("m-exact", 10.0)},
        )
    )
    assert ranked == ["sonnet --effort high", "haiku"], ranked
    tied = _ranked_labels(
        recommend(
            providers,
            "B",
            today=TODAY,
            now=NOW,
            grade_table=table,
            model_prices={"m-borrow": price("m-borrow", 10.0), "m-exact": price("m-exact", 10.0)},
        )
    )
    assert tied == ["haiku", "sonnet --effort high"], tied


def test_rank_key_treats_borrowed_like_one_up(monkeypatch):
    """The non-exact element separates a full tie the same way for borrowed:
    drive the key directly with the borrowed candidate holding the earlier
    slot — drop it and the slot wrongly lists borrowed first."""
    monkeypatch.setattr(recommend_mod, "get_policy", lambda *a, **k: ("preserve", None))
    monkeypatch.setattr(recommend_mod, "get_boost", lambda *a, **k: (None, None))
    table = {grade: [] for grade in GRADES}
    table["A"].append(Profile("sonnet", "Exact", 50.0))
    base = recommend_mod._evaluate(
        _pool_providers(),
        "A",
        TODAY,
        NOW,
        urgency_hours=24,
        bench_scores=[],
        normalized_prices={},
        table=table,
    ).included[0]
    exact = replace(base, profile=replace(base.profile, name="exact-row"))
    borrowed = replace(base, profile=replace(base.profile, name="borrowed-row"), borrowed=True)
    value_order = {id(exact.profile): 0, id(borrowed.profile): 0}
    intra_pool_rank = {id(exact.profile): 0, id(borrowed.profile): 0}
    ranked = sorted(
        (borrowed, exact),
        key=lambda c: _recommend_rank_key(c, [], value_order, intra_pool_rank, 0 if c.borrowed else 1),
    )
    assert [c.profile.name for c in ranked] == ["exact-row", "borrowed-row"]


# ── AC5 mutants: one-up + borrowed lists once, as borrowed ────────────────────


def test_a_row_qualifying_as_both_one_up_and_borrowed_lists_once_as_borrowed():
    """Mutant pin for the dedup: make sonnet@high rep-measured at its placed
    grade A (one-up eligible at B, default gate, not Sol) — it must still list
    exactly once, tagged [borrowed A], never [one-up A]. (hk 2007: the opus@low
    A+ entry is gone, so the dedup is pinned on the sonnet@high B entry.)"""
    table = {grade: list(profiles) for grade, profiles in GRADE_TABLE.items()}
    rep = Profile(
        "sonnet",
        "Sonnet (high)",
        43.0,
        launcher_effort="high",
        benchmark_annotation="급 실측(A; reps 3/3)",
        aa_agent_model_id="claude-sonnet-5-5",
        aa_model_id="claude-sonnet-5-5",
    )
    table["A"] = [rep if (p.name, p.launcher_effort) == ("sonnet", "high") else p for p in table["A"]]
    one_up = [p for p, _ in recommend_mod._one_up_profiles("B", table)]
    borrowed = [p for p, _ in _borrowed_profiles("B", table)]
    assert rep in one_up and rep in borrowed  # both paths independently qualify

    out = recommend(_pool_providers(table), "B", today=TODAY, now=NOW, grade_table=table)
    lines = [line for line in _ranked(out) if "sonnet --effort high" in line]
    assert len(lines) == 1 and "[borrowed A]" in lines[0] and "[one-up" not in lines[0], out

    payload = recommend_dict(_pool_providers(table), "B", today=TODAY, now=NOW, grade_table=table)
    rows = [row for row in payload["rows"] if row["profile"] == "sonnet" and "high" in str(row["display"])]
    assert len(rows) == 1
    assert rows[0]["borrowed"] is True and rows[0]["one_up"] is False


# ── AC4: gate and policy launch are untouched ────────────────────────────────


def test_gate_and_launch_for_the_borrowed_rungs_are_unchanged():
    providers = _pool_providers()
    result = gate_check(providers, "opus", effort="low", today=TODAY, now=NOW)
    assert result.ok is True and result.grade == "S"
    assert "opus@low" in result.reason and "escalation" not in result.reason
    decision = launch.resolve_launch("opus", effort="low")
    assert (decision.effort, decision.grade, decision.gate, decision.model_id) == (
        "low",
        "S",
        "default",
        "claude-opus-5-5",
    )

    result = gate_check(providers, "codex-sol", effort="xhigh", today=TODAY, now=NOW)
    assert result.ok is True and result.grade == "S+"
    assert "codex-sol@xhigh, escalation-gated" in result.reason
    decision = launch.resolve_launch("codex-sol", effort="xhigh")
    assert (decision.effort, decision.grade, decision.gate, decision.model_id) == (
        "xhigh",
        "S+",
        "escalation",
        "gpt-6.1-sol",
    )


def test_borrowed_rows_never_enter_gate_alternatives():
    """A borrowed listing is a display row one grade up, never a same-grade
    alternative — _alt_candidates skips it like a one-up line."""
    providers = _pool_providers()
    alternatives = _alt_candidates(providers, "A+", "sonnet", TODAY, NOW, 24.0)
    assert "opus" not in alternatives
