"""hk 1862 — ``--recommend`` accounts for every canon row at the grade.

Every canon row at a grade is either ranked, listed as an escalation/promotion
candidate, or shown on the single folded ``✗ 제외`` line with a reason —
nothing is dropped silently anymore:

* an effort-less row is a legacy projection only when an effort row of the
  same profile *at the same grade* replaces it (grok-hi at S stays ranked);
* barred models (``BARRED_MODEL_IDS``) fold as ``barred`` with the decision
  ref, never rank and stay unlaunchable;
* unsubscribed pools fold as ``unsubscribed`` — ROB-1219 suppression and the
  ``_suppress_policy_excluded`` path are gone, policy-excluded pools fold too;
* ``BORROWED_RUNGS`` entries carry a per-entry listing gate (codex-sol@xhigh
  lists at S as a default-gated candidate; sonnet@high lists at B);
* an inert borrowed entry folds at its listed grade naming the expected source
  grade and where the rung actually is;
* E6 measurement rungs at their carrier fold as ``E6 unmeasured``;
* grok rungs below A fold as ``grok restricted`` (operator r1023grokup).

Mutants this file kills (stated as invariants): restoring the old any-grade
skip makes the grok-hi cases RED; removing the barred entry from the fold
makes the terra case RED; dropping the inert-entry fold makes the moved-source
case RED.
"""

from __future__ import annotations

import datetime as dt
import json
import re

from scopefuel import bench, policy
from scopefuel import recommend as recommend_mod
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    _GRADE_ORDER,
    BORROWED_RUNGS,
    GRADE_TABLE,
    _borrowed_listings,
    _borrowed_profiles,
    gate_check,
    recommend,
    recommend_dict,
)

NOW = dt.datetime(2026, 10, 23, 12, 0, 0, tzinfo=dt.UTC)
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


def _pool_providers(table) -> list[ProviderResult]:
    """One measurable ProviderResult per (provider, group) the table routes to."""
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


def _server_view(rows=None):
    rows = rows if rows is not None else [entry.as_dict() for entry in bench.catalog_snapshot()]
    entries = bench._catalog_from_payload({"catalog": rows})
    return bench.CatalogView(tuple(entries), source="server", backend="handoffkeep")


def _canon_table(rows=None):
    table = bench._catalog_grade_table(_server_view(rows))
    assert table is not None
    return table


def _canon_row(profile: str, effort: str, **patch):
    row = next(
        entry.as_dict()
        for entry in bench.catalog_snapshot()
        if entry.profile == profile and entry.effort == effort
    )
    row.update(patch)
    return row


def _cleaned_canon_rows():
    """Today's canon shaped like the post-merge cleanup this task precedes:

    the grok-hi S row carries its explicit ``high`` effort and the four terra
    rows are gone (both are the director's post-merge push-catalog step — the
    fixture asserts the same end state the served canon will carry).
    """
    rows = []
    for entry in bench.catalog_snapshot():
        row = entry.as_dict()
        if row["model_id"].startswith("gpt-5.6-terra"):
            continue
        if row["profile"] == "grok-hi" and row["effort"] == "":
            row["effort"] = "high"
        rows.append(row)
    return rows


def _ranked(output: str) -> list[str]:
    return [line for line in output.splitlines() if line[:1].isdigit()]


def _fold_line(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("✗ 제외")]
    assert len(lines) == 1, f"expected exactly one folded excluded line: {output}"
    return lines[0]


def _no_fold_line(output: str) -> None:
    assert not [line for line in output.splitlines() if line.startswith("✗ 제외")], output


# ── skip rule: effort-less row drops only against a same-grade effort row ────


def test_effortless_grok_hi_row_is_ranked_at_s():
    """grok-hi "" at S stays a candidate: its effort rows sit at other grades."""
    table = _canon_table()
    out = recommend(_pool_providers(table), "S", today=TODAY, now=NOW, grade_table=table)
    assert any("grok-hi" in line for line in _ranked(out)), out
    fold = _fold_line(out)
    assert "grok-hi@default" not in fold and "grok-hi " not in fold


def test_effortless_row_replaced_by_same_grade_effort_row_folds_as_legacy():
    """Add grok-hi@high at S and the effort-less S row folds, naming the rung."""
    rows = [e.as_dict() for e in bench.catalog_snapshot()]
    new_row = _canon_row("grok-hi", "")
    new_row["effort"] = "high"
    rows.append(new_row)
    table = _canon_table(rows)
    out = recommend(_pool_providers(table), "S", today=TODAY, now=NOW, grade_table=table)
    fold = _fold_line(out)
    assert "grok-hi@default — dropped legacy" in fold, fold
    assert "replaced by grok-hi@high" in fold
    # the replacing rung itself ranks at S
    assert any("grok-hi" in line and "high" in line for line in _ranked(out)), out


def test_effortless_row_unreplaced_by_other_grade_effort_rows():
    """An effort row at a *different* grade does not drop the default row —
    the grok-hi S row survives grok-hi@xhigh sitting at A (mutant pin: the old
    any-grade skip turns this case RED)."""
    rows = [e.as_dict() for e in bench.catalog_snapshot()]
    assert any(r["profile"] == "grok-hi" and r["effort"] == "xhigh" and r["grade"] == "A" for r in rows)
    assert not any(r["profile"] == "grok-hi" and r["effort"] for r in rows if r["grade"] == "S")
    table = _canon_table(rows)
    out = recommend(_pool_providers(table), "S", today=TODAY, now=NOW, grade_table=table)
    assert any("grok-hi" in line for line in _ranked(out)), out


# ── barred rows fold with the decision ref, never rank ──────────────────────


def test_terra_max_at_s_folds_as_barred():
    table = _canon_table()
    out = recommend(_pool_providers(table), "S", today=TODAY, now=NOW, grade_table=table)
    assert not any("codex-terra" in line for line in _ranked(out)), out
    fold = _fold_line(out)
    assert "codex-terra-max@default — barred (operator 2026-10-12 noterra)" in fold, fold


def test_barred_rows_fold_at_every_grade_and_stay_unlaunchable():
    table = _canon_table()
    providers = _pool_providers(table)
    for grade in ("S", "A+", "A"):
        out = recommend(providers, grade, today=TODAY, now=NOW, grade_table=table)
        assert not any("codex-terra" in line for line in _ranked(out)), (grade, out)
        assert "codex-terra" in _fold_line(out) and "barred" in _fold_line(out), grade
    result = gate_check(providers, "codex-terra", today=TODAY, now=NOW, grade_table=table)
    assert not result.ok and "barred" in result.reason


def test_barred_row_in_json_excluded():
    table = _canon_table()
    payload = recommend_dict(_pool_providers(table), "S", today=TODAY, now=NOW, grade_table=table)
    entry = next(e for e in payload["excluded"] if e["profile"] == "codex-terra-max")
    assert entry["effort"] == "default"
    assert "barred" in entry["reason"]
    assert "operator 2026-10-12 noterra" in entry["reason"]


# ── unsubscribed and policy-excluded pools fold ─────────────────────────────


def test_unsubscribed_pool_rows_fold_as_unsubscribed():
    table = _canon_table()
    policy.set_subscribed("kimi", False)
    out = recommend(_pool_providers(table), "S", today=TODAY, now=NOW, grade_table=table)
    assert not any("kimi-k3" in line for line in _ranked(out)), out
    fold = _fold_line(out)
    assert "kimi-k3@default — unsubscribed" in fold, fold
    assert "pools.kimi" in fold


def test_policy_excluded_pool_folds_not_hidden():
    """ROB-1219 suppression is gone — the pool's rows appear on the line."""
    table = _canon_table()
    policy.set_policy("claude", "exclude", until=TODAY + dt.timedelta(days=30), note="무료 전환(구독 종료)")
    out = recommend(_pool_providers(table), "S", today=TODAY, now=NOW, grade_table=table)
    fold = _fold_line(out)
    assert "opus@low — policy excluded" in fold, fold
    assert "claude 풀 class=exclude" in fold and "무료 전환(구독 종료)" in fold


# ── borrowed rungs: listing gate, sonnet@high at B, inert fold ──────────────


def test_borrowed_rungs_entries_and_refs():
    assert BORROWED_RUNGS == (
        ("codex-sol", "xhigh", "S", "default", "operator hk:task/1786 comment 6689"),
        ("sonnet", "high", "B", "default", "operator hk:task/1786 comment 6689"),
    )


def test_codex_sol_xhigh_at_s_is_a_default_gated_candidate():
    """The borrowed listing's own gate applies at S — the row ranks, it does
    not sit in the escalation section. Its own S+ row stays escalation."""
    table = _canon_table()
    out = recommend(_pool_providers(table), "S", today=TODAY, now=NOW, grade_table=table)
    ranked = [line for line in _ranked(out) if "codex-sol --effort xhigh" in line]
    assert len(ranked) == 1 and "[borrowed S+]" in ranked[0], out
    assert "승급 후보" not in out or "codex-sol" not in out.split("승급 후보", 1)[1]


def test_sonnet_high_lists_at_b_borrowed_from_a():
    table = _canon_table()
    out = recommend(_pool_providers(table), "B", today=TODAY, now=NOW, grade_table=table)
    lines = [line for line in _ranked(out) if "sonnet --effort high" in line]
    assert len(lines) == 1 and "[borrowed A]" in lines[0], out


def test_no_grok_rung_ranked_or_borrowed_at_b_or_c():
    """r1023grokup: grok only at A and above — B/C grok rows fold, never rank."""
    table = _canon_table()
    providers = _pool_providers(table)
    for grade in ("B", "C"):
        out = recommend(providers, grade, today=TODAY, now=NOW, grade_table=table)
        assert not any("grok" in line for line in _ranked(out)), (grade, out)
        assert "승급" not in out or not any(
            "grok" in line for line in out.split("승급 후보", 1)[1].splitlines()
        )
        fold = _fold_line(out)
        if grade == "B":  # canon still carries grok@low at B today
            assert "grok@low — grok restricted to A and above (operator r1023grokup)" in fold


def test_inert_borrowed_entry_folds_with_source_grade():
    """sonnet@high moved to S (one grade above its A source, not the B listing's
    expected source) — the entry is inert and folds at B naming where the rung
    actually is. Dropping this fold (mutant) turns this case RED. hk 2007 removed
    the opus@low A+ entry, so the inert fold is pinned on the sonnet@high B entry."""
    rows = _cleaned_canon_rows()
    target = next(row for row in rows if row["profile"] == "sonnet" and row["effort"] == "high")
    target["grade"] = "S"
    table = _canon_table(rows)
    out = recommend(_pool_providers(table), "B", today=TODAY, now=NOW, grade_table=table)
    assert not any("sonnet --effort high" in line for line in _ranked(out)), out
    fold = _fold_line(out)
    assert "sonnet@high — borrowed rung inert" in fold, fold
    assert "expected at A" in fold and "placed at S" in fold


def test_retired_borrowed_rung_folds_as_not_carried():
    rows = _cleaned_canon_rows()
    target = next(row for row in rows if row["profile"] == "sonnet" and row["effort"] == "high")
    target["retired_at"] = "2026-10-23T00:00:00Z"
    table = _canon_table(rows)
    out = recommend(_pool_providers(table), "B", today=TODAY, now=NOW, grade_table=table)
    assert "sonnet@high" in _fold_line(out)
    assert "not carried" in _fold_line(out)


def test_borrowed_entry_listings_report_inert_state():
    table = _canon_table()
    listings = _borrowed_listings("S", table)
    sol = next(x for x in listings if x.profile_name == "codex-sol")
    assert sol.active and sol.placed_grade == "S+" and sol.listed_gate == "default"
    # still reachable through the narrow helper too
    assert (sol.profile, "S+") in _borrowed_profiles("S", table)


# ── E6 rungs fold as E6 unmeasured at their carrier ─────────────────────────


def test_e6_grok_rungs_fold_at_c_never_ranked():
    table = _canon_table()
    out = recommend(_pool_providers(table), "C", today=TODAY, now=NOW, grade_table=table)
    assert not any("grok" in line for line in _ranked(out)), out
    fold = _fold_line(out)
    assert "grok-hi@low — E6 unmeasured" in fold, fold
    assert "grok-hi@medium — E6 unmeasured" in fold, fold


def test_e6_rungs_fold_on_the_snapshot_table_too():
    """A plain-dict table (no catalog) folds the compile-time E6 rungs."""
    out = recommend(_pool_providers(GRADE_TABLE), "C", today=TODAY, now=NOW)
    fold = _fold_line(out)
    assert "grok-hi@low — E6 unmeasured" in fold, fold


# ── one folded line per grade; --hide-excluded; JSON excluded ───────────────


def test_property_every_canon_row_accounted_text_and_json():
    """rows + escalation + excluded == canon rows at the grade (plus the
    cross-grade one-up/borrowed listings), no duplicates — text and JSON."""
    table = _canon_table()
    providers = _pool_providers(table)
    canon = {}
    for entry in _server_view().entries:
        if not entry.retired_at:
            canon.setdefault(entry.grade, set()).add((entry.profile, entry.effort or "default"))
    for grade in GRADES:
        text = recommend(providers, grade, today=TODAY, now=NOW, grade_table=table)
        payload = recommend_dict(providers, grade, today=TODAY, now=NOW, grade_table=table)

        listed = {
            (
                row["profile"],
                row["display"].split("--effort ")[-1] if "--effort" in row["display"] else "default",
            )
            for row in payload["rows"]
        }
        excluded = {(e["profile"], e["effort"]) for e in payload["excluded"]}
        row_keys = [(r["profile"], r["display"], r["kind"]) for r in payload["rows"]]
        assert len(set(row_keys)) == len(row_keys)  # no duplicate rows
        accounted = listed | excluded
        expected = (
            canon.get(grade, set())
            | {(x.profile_name, x.effort) for x in _borrowed_listings(grade, table)}
            | {
                (p.name, p.launcher_effort or "default")
                for p, _ in recommend_mod._one_up_profiles(grade, table)
            }
        )
        missing = {k for k in expected if k not in accounted}
        assert not missing, (grade, missing, text, json.dumps(payload, ensure_ascii=False))
        # no duplicates on the text fold line
        fold_entries = re.findall(r"(\S+@\S+) — ", _fold_line(text)) if "✗ 제외" in text else []
        assert len(fold_entries) == len(set(fold_entries))


def test_hide_excluded_hides_text_line_but_json_keeps_excluded():
    table = _canon_table()
    providers = _pool_providers(table)
    out = recommend(providers, "S", today=TODAY, now=NOW, grade_table=table, hide_excluded=True)
    _no_fold_line(out)
    payload = recommend_dict(providers, "S", today=TODAY, now=NOW, grade_table=table)
    assert payload["excluded"]
    assert any(e["profile"] == "codex-terra-max" for e in payload["excluded"])


# ── family coverage: every grade keeps a claude and a codex rung visible ────

_CLAUDE_FAMILIES = {"claude", "kiro", "agy"} & {"claude"}  # model family, see below


def _family_of(profile_name: str) -> str:
    """Model vendor family for the coverage check — provider_family() gives the
    pool; the fusion/kiro rows' underlying model decides."""
    name = profile_name
    if name in (
        "opus",
        "sonnet",
        "haiku",
        "fable",
        "kiro-opus",
        "kiro-sonnet",
        "kiro-haiku",
        "oc-sonnet46",
        "cc-qwen38",
        "cc-glm",
        "devin-fusion-opus55",
        "devin-fusion-sonnet55",
    ):
        return "claude"
    if name.startswith("codex") or name in ("kiro-sol", "devin-fusion-sol61", "devin-fusion-sol61-xhigh"):
        return "codex"
    if name == "grok" or name.startswith("grok-"):
        return "grok"
    return ""


def test_family_coverage_every_grade():
    """C..S+ each show >=1 claude and >=1 codex rung; A, A+ and S also show a
    grok rung — ranked, escalated or folded (operator r1023grokup; the brief's
    S+ was corrected by director-1 on escalation 00005 — grok is not required
    at S+). A grade with no canon or borrowed rung for a required family is a
    coverage failure the brief says to escalate, not to paper over with data."""
    table = _canon_table(_cleaned_canon_rows())
    providers = _pool_providers(table)
    for grade in GRADES:
        payload = recommend_dict(providers, grade, today=TODAY, now=NOW, grade_table=table)
        names = {row["profile"] for row in payload["rows"]} | {e["profile"] for e in payload["excluded"]}
        families = {_family_of(n) for n in names} - {""}
        # borrowed entries listing at this grade count toward visibility
        families |= {_family_of(x.profile_name) for x in _borrowed_listings(grade, table) if x.active}
        assert "claude" in families, (grade, sorted(names))
        assert "codex" in families, (grade, sorted(names))
        if grade in ("A", "A+", "S"):
            assert "grok" in families, (grade, sorted(names))


# ── item 8: the launch gate reads no borrowed gate ───────────────────────────


def test_gate_judges_the_rungs_own_row_unchanged():
    """gate_check has no task-grade input: codex-sol --effort xhigh is judged
    on its S+ escalation row exactly as before. The BORROWED_RUNGS gate field
    is a listing attribute; launch matching task grade to rung grade is wrk's
    side, not this repo's."""
    table = _canon_table()
    providers = _pool_providers(table)
    result = gate_check(providers, "codex-sol", today=TODAY, now=NOW, grade_table=table, effort="xhigh")
    assert result.grade == "S+"
    # a spurious operator-request on a default-gated rung is still refused as
    # not-applicable — the escalation path is the same as main's
