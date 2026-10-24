"""hk 1795 — d1786ap borrowed rungs: a cross-grade listing, not a placement.

``recommend.BORROWED_RUNGS`` names the two operator-directive rows
(hk:task/1786): ``opus@low`` lists at ``A+`` while the table places it at S,
``codex-sol@xhigh`` lists at ``S`` while the table places it at S+ keeping its
escalation gate. An entry lists only while the live table (canon view or
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
        ("opus", "low", "A+", "default", "operator d1786ap hk:task/1786"),
        ("codex-sol", "xhigh", "S", "default", "operator hk:task/1786 comment 6689"),
        ("sonnet", "high", "B", "default", "operator hk:task/1786 comment 6689"),
    )


def test_borrowed_entries_list_at_their_named_grade_only():
    for grade in GRADES:
        listed = {(p.name, p.launcher_effort) for p, _ in _borrowed_profiles(grade, GRADE_TABLE)}
        expected = (
            {("opus", "low")}
            if grade == "A+"
            else (
                {("codex-sol", "xhigh")}
                if grade == "S"
                else ({("sonnet", "high")} if grade == "B" else set())
            )
        )
        assert listed == expected, grade


# ── AC1: snapshot fixture ─────────────────────────────────────────────────────


def test_recommend_aplus_lists_opus_low_tagged_borrowed_s():
    out = recommend(_pool_providers(), "A+", today=TODAY, now=NOW)
    lines = [line for line in _ranked(out) if "opus --effort low" in line]
    assert len(lines) == 1 and "[borrowed S]" in lines[0], out
    assert "[one-up" not in lines[0]


def test_recommend_s_lists_codex_sol_xhigh_tagged_borrowed_splus_candidate():
    out = recommend(_pool_providers(), "S", today=TODAY, now=NOW)
    # hk 1862 (comment 6689): the entry's listed gate is default — at S it
    # ranks as a normal candidate, tagged borrowed. Its own S+ row keeps
    # gate=escalation.
    lines = [line for line in _ranked(out) if "codex-sol --effort xhigh" in line]
    assert len(lines) == 1 and "[borrowed S+]" in lines[0], out


def test_recommend_dict_marks_borrowed_rows():
    aplus = recommend_dict(_pool_providers(), "A+", today=TODAY, now=NOW)
    opus = next(row for row in aplus["rows"] if row["profile"] == "opus" and "low" in str(row["display"]))
    assert opus["kind"] == "candidate"
    assert opus["borrowed"] is True
    assert opus["placed_grade"] == "S"
    assert opus["one_up"] is False

    s = recommend_dict(_pool_providers(), "S", today=TODAY, now=NOW)
    sol = next(row for row in s["rows"] if row["profile"] == "codex-sol" and "xhigh" in str(row["display"]))
    assert sol["kind"] == "candidate"
    assert sol["borrowed"] is True
    assert sol["placed_grade"] == "S+"
    assert sol["one_up"] is False


# ── AC1: canon fixture ────────────────────────────────────────────────────────


def test_canon_fixture_lists_both_borrowed_rows():
    table = _canon_table()
    providers = _pool_providers(table)
    out = recommend(providers, "A+", today=TODAY, now=NOW, grade_table=table)
    lines = [line for line in _ranked(out) if "opus --effort low" in line]
    assert len(lines) == 1 and "[borrowed S]" in lines[0], out

    out = recommend(providers, "S", today=TODAY, now=NOW, grade_table=table)
    entry_line = next(line for line in out.splitlines() if "codex-sol --effort xhigh" in line)
    assert "[borrowed S+]" in entry_line


def test_canon_fixture_json_marks_borrowed_rows():
    table = _canon_table()
    providers = _pool_providers(table)
    aplus = recommend_dict(providers, "A+", today=TODAY, now=NOW, grade_table=table)
    opus = next(row for row in aplus["rows"] if row.get("borrowed") is True)
    assert opus["profile"] == "opus" and opus["placed_grade"] == "S" and opus["kind"] == "candidate"
    s = recommend_dict(providers, "S", today=TODAY, now=NOW, grade_table=table)
    sol = next(row for row in s["rows"] if row.get("borrowed") is True and row["profile"] == "codex-sol")
    assert sol["placed_grade"] == "S+" and sol["kind"] == "candidate"


def test_recommend_json_cli_marks_borrowed(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {
            name: (lambda provider_id=name: ProviderResult(id=provider_id, buckets=[_bucket()]))
            for name in {recommend_mod.resolved_pool(p)[0] for ps in GRADE_TABLE.values() for p in ps}
        },
    )
    assert cli.main(["--recommend", "A+", "--json", "--no-cache"]) == 0
    payload = json.loads(capsys.readouterr().out)
    opus = next(row for row in payload["rows"] if row["profile"] == "opus" and "low" in str(row["display"]))
    assert opus["borrowed"] is True and opus["placed_grade"] == "S"


# ── AC2: inert cases, one each ────────────────────────────────────────────────


def test_inert_when_canon_places_opus_low_at_aplus_itself():
    """A canon move to the listed grade makes it an exact row, not borrowed."""
    table = _canon_table(_seed_rows_with("opus", "low", grade="A+"))
    out = recommend(_pool_providers(table), "A+", today=TODAY, now=NOW, grade_table=table)
    lines = [line for line in _ranked(out) if "opus --effort low" in line]
    assert len(lines) == 1 and "[borrowed" not in lines[0] and "[one-up" not in lines[0]
    assert "[borrowed" not in out


def test_inert_when_canon_places_opus_low_at_splus():
    """Placed two grades above the listed grade the entry stays silent."""
    table = _canon_table(_seed_rows_with("opus", "low", grade="S+"))
    for grade in GRADES:
        out = recommend(_pool_providers(table), grade, today=TODAY, now=NOW, grade_table=table)
        assert "[borrowed S]" not in out, grade  # the codex-sol [borrowed S+] row may still list


def test_inert_when_canon_retires_opus_low():
    """A retired rung is not carried — the entry finds nothing to list."""
    table = _canon_table(_seed_rows_with("opus", "low", retired_at="2026-10-21T00:00:00Z"))
    for grade in GRADES:
        out = recommend(_pool_providers(table), grade, today=TODAY, now=NOW, grade_table=table)
        assert "opus --effort low" not in out and "[borrowed S]" not in out, grade


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
        if grade not in ("A+", "S", "B"):
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
        if grade == "A+":
            assert len(added) == 1 and "opus --effort low" in added[0] and "[borrowed S]" in added[0]
        elif grade == "B":
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
    table["S"].append(Profile("opus", "Opus (low)", 62.0, launcher_effort="low", aa_model_id="m-opus"))
    table["A+"].append(Profile("sonnet", "Exact", 62.0, aa_model_id="m-exact"))
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
            "A+",
            today=TODAY,
            now=NOW,
            grade_table=table,
            model_prices={"m-opus": price("m-opus", 5.0), "m-exact": price("m-exact", 10.0)},
        )
    )
    assert ranked == ["opus --effort low", "sonnet"], ranked
    tied = _ranked_labels(
        recommend(
            providers,
            "A+",
            today=TODAY,
            now=NOW,
            grade_table=table,
            model_prices={"m-opus": price("m-opus", 10.0), "m-exact": price("m-exact", 10.0)},
        )
    )
    assert tied == ["sonnet", "opus --effort low"], tied


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
    """Mutant pin for the dedup: make opus@low rep-measured (one-up eligible,
    default gate, not Sol) — it must still list exactly once, tagged
    [borrowed S], never [one-up S]."""
    table = {grade: list(profiles) for grade, profiles in GRADE_TABLE.items()}
    rep = Profile(
        "opus",
        "Opus (low)",
        62.0,
        launcher_effort="low",
        benchmark_annotation="급 실측(S; reps 3/3)",
        aa_agent_model_id="claude-opus-5-5",
        aa_model_id="claude-opus-5-5",
    )
    table["S"] = [rep if (p.name, p.launcher_effort) == ("opus", "low") else p for p in table["S"]]
    one_up = [p for p, _ in recommend_mod._one_up_profiles("A+", table)]
    borrowed = [p for p, _ in _borrowed_profiles("A+", table)]
    assert rep in one_up and rep in borrowed  # both paths independently qualify

    out = recommend(_pool_providers(table), "A+", today=TODAY, now=NOW, grade_table=table)
    lines = [line for line in _ranked(out) if "opus --effort low" in line]
    assert len(lines) == 1 and "[borrowed S]" in lines[0] and "[one-up" not in lines[0], out

    payload = recommend_dict(_pool_providers(table), "A+", today=TODAY, now=NOW, grade_table=table)
    rows = [row for row in payload["rows"] if row["profile"] == "opus" and "low" in str(row["display"])]
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
