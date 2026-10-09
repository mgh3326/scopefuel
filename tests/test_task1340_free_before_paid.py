"""#1340 (hk 1340, dr-1340-1 decision A): free rows before paid rows in one pool.

Pool devin mixes Free-tag launches (the swe-2 family, glm-5-2, swe-1-7) with
paid devin-ds41, which draws the Devin Pro budget. The single total sort key
in ``_recommend_rank_key`` gained a paid-within-pool dimension — after the
quota and boost (grade-standing) dimensions, before value order — so inside
one pool a free row ranks above a paid row at equal quota and boost standing.
``unknown`` sorts neutral, the same as ``free``, so rows without data never
move; one tuple key, no comparator.
"""

from __future__ import annotations

import datetime as dt
import itertools
import random
import re
from dataclasses import replace
from pathlib import Path

from scopefuel import bench, launch
from scopefuel import recommend as recommend_mod
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    _GRADE_ORDER,
    GRADE_TABLE,
    Profile,
    _profile_has_benchmark_score,
    _recommend_rank_key,
    profile_pool,
    recommend,
    recommend_dict,
)

NOW = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
GRADES = _GRADE_ORDER

FIXTURE = Path(__file__).parent / "fixtures" / "devin_models_list.txt"

# dr-1340-1 pinned enumeration: free = swe-2 / swe-2-medium / swe-2-max /
# glm-5-2 / swe-1-7 launch rows; paid = devin-ds41; everything else unknown.
PINNED_BILLING = {
    "devin-swe2": "free",
    "devin-swe2-medium": "free",
    "devin-swe2-max": "free",
    "devin-glm52": "free",
    "devin-swe17": "free",
    "devin-ds41": "paid",
}


def _bucket(window: str = "7d", scope: Scope | None = None, used: float = 10.0) -> Bucket:
    return Bucket(
        label=window,
        window=window,
        used_pct=used,
        resets_at=(NOW + dt.timedelta(days=6)).isoformat(),
        scope=scope or Scope("account"),
        horizon="week",  # type: ignore[arg-type]
    )


def _pool_providers() -> list[ProviderResult]:
    """One measurable ProviderResult per (provider, group) the table routes to."""
    pool_scopes: dict[str, set[str | None]] = {}
    for profiles in GRADE_TABLE.values():
        for profile in profiles:
            provider_id, group_name = profile_pool(profile.name)
            assert provider_id, profile.name
            pool_scopes.setdefault(provider_id, set()).add(group_name)
    providers = []
    for provider_id, group_names in pool_scopes.items():
        providers.append(
            ProviderResult(
                id=provider_id,
                pool_class="preserve",
                buckets=[
                    _bucket(scope=Scope("account") if g is None else Scope("group", g)) for g in group_names
                ],
            )
        )
    return providers


def _ranked(output: str) -> list[str]:
    """Ranked lines (``N. …``) as raw text."""
    return [line for line in output.splitlines() if line[:1].isdigit()]


def _rank_of(output: str, needle: str) -> int:
    for index, line in enumerate(_ranked(output)):
        if needle in line:
            return index
    raise AssertionError(f"{needle} not ranked:\n{output}")


# ── billing source (dr-1340-1): bundled flags vs the recorded models list ────


def _fixture_model_tags(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """model uid -> raw tag bracket, family slug -> joined row tags."""
    uid_tags: dict[str, str] = {}
    family_tags: dict[str, list[str]] = {}
    family: str | None = None
    for line in text.splitlines():
        header = re.match(r"^(\S.*) \(([^)]+)\)\s*$", line)
        if header:
            family = header.group(2)
            continue
        row = re.match(r"^\s+(\S+)\s{2,}.*\[([^]]+)\]", line)
        if row:
            uid_tags.setdefault(row.group(1), row.group(2))
            if family:
                family_tags.setdefault(family, []).append(row.group(2))
    return uid_tags, {slug: " ".join(tags) for slug, tags in family_tags.items()}


def _launch_free_in_fixture(launch_id: str) -> bool:
    """A launch id is Free-backed iff its model row — or every row of its
    family — carries the Free tag in the recorded ``devin models list``."""
    uid_tags, family_tags = _fixture_model_tags(FIXTURE.read_text())
    tags = uid_tags.get(launch_id, family_tags.get(launch_id, ""))
    return "Free" in tags


def test_bundled_devin_billing_matches_pinned_enumeration():
    """Every devin-pool row's billing equals the dr-1340-1 pin — nothing else
    marked free or paid."""
    for profiles in GRADE_TABLE.values():
        for profile in profiles:
            if profile_pool(profile.name)[0] != "devin":
                continue
            expected = PINNED_BILLING.get(profile.name, "unknown")
            assert profile.billing == expected, (profile.name, profile.billing, expected)


def test_free_marks_are_backed_by_a_free_tag_in_the_recorded_models_list():
    """No free marking without data: each free profile's launch id resolves to
    a Free-tagged model row or an all-Free family in the recorded sample."""
    for profiles in GRADE_TABLE.values():
        for profile in profiles:
            if profile.billing != "free":
                continue
            launch_id = launch.LAUNCH_MODEL_IDS.get(profile.name)
            assert launch_id, (profile.name, "free without a launch id")
            assert _launch_free_in_fixture(launch_id), (
                profile.name,
                launch_id,
                "free mark lacks a Free tag in devin_models_list.txt",
            )


def test_paid_pin_is_ds41_and_its_launch_id_has_no_free_tag():
    """devin-ds41 is the only paid row; its launch id is absent from the
    recorded Free tags (it draws the Devin Pro budget)."""
    paid = {p.name for profiles in GRADE_TABLE.values() for p in profiles if p.billing == "paid"}
    assert paid == {"devin-ds41"}
    assert not _launch_free_in_fixture(launch.LAUNCH_MODEL_IDS["devin-ds41"])


def _server_view(rows=None):
    """A server-source CatalogView built from catalog-row dicts."""
    rows = rows or [entry.as_dict() for entry in bench.catalog_snapshot()]
    entries = bench._catalog_from_payload({"catalog": rows})
    return bench.CatalogView(tuple(entries), source="server", backend="handoffkeep")


def test_catalog_only_rows_default_to_unknown_billing():
    """dr-1340-1: catalog-built rows without a wire column default to unknown.

    The wire carries no billing field, so a profile the canon adds — one with
    no bundled template to inherit — is unknown and sorts neutral. Rows the
    catalog merely re-places keep their bundled row's billing instead: they
    are built by ``replace(template, …)`` and the bundled GRADE_TABLE row is
    the single source the decision named.
    """
    assert not hasattr(bench.CatalogEntry, "billing")
    rows = [entry.as_dict() for entry in bench.catalog_snapshot()]
    rows.append(
        bench.CatalogEntry(
            profile="devin-serveronly",
            effort="",
            grade="A",
            model_id="swe-9",
            pool="devin",
        ).as_dict()
    )
    table = bench._catalog_grade_table(_server_view(rows))
    assert table is not None
    server_only = next(p for p in table["A"] if p.name == "devin-serveronly")
    assert server_only.billing == "unknown"
    # …while a canon-covered bundled row keeps the bundled flag — GRADE_TABLE
    # is the source, the wire just does not carry it.
    covered = next(p for p in table["A+"] if p.name == "devin-ds41")
    assert covered.billing == "paid"


# ── AC1-level ordering on the bundled table ──────────────────────────────────


def test_recommend_a_ranks_free_swe2_one_ups_above_paid_ds41():
    """At A the paid devin-ds41 one-up row used to sort above the free SWE-2
    one-up rows at equal quota standing; now every free devin row leads it."""
    out = recommend(_pool_providers(), "A", today=TODAY, now=NOW)
    ds41 = _rank_of(out, "devin-ds41")
    for free_row in ("devin-swe2", "devin-swe2-medium", "devin-swe2-max"):
        assert _rank_of(out, free_row) < ds41, out


def test_recommend_aplus_ranks_all_free_devin_rows_above_paid_ds41():
    """At A+ all four devin rows are exact-grade candidates sharing one quota
    line: the three free rows (swe-2 family) must all lead paid ds41."""
    out = recommend(_pool_providers(), "A+", today=TODAY, now=NOW)
    ds41 = _rank_of(out, "devin-ds41")
    for free_row in ("devin-swe2", "devin-swe2 (effort high)", "devin-swe2-max"):
        assert _rank_of(out, free_row) < ds41, out


def test_non_devin_pools_hold_one_cost_class_so_their_order_is_mains():
    """Every non-devin bundled row is unknown — a single (neutral) cost class —
    so this change cannot reorder any other pool."""
    for profiles in GRADE_TABLE.values():
        for profile in profiles:
            provider_id = profile_pool(profile.name)[0]
            if provider_id == "devin":
                continue
            assert profile.billing == "unknown", profile.name


# ── JSON marker ──────────────────────────────────────────────────────────────


def test_recommend_dict_rows_carry_the_billing_marker():
    payload = recommend_dict(_pool_providers(), "A", today=TODAY, now=NOW)
    assert payload["rows"], "expected ranked rows"
    assert all("billing" in row for row in payload["rows"])
    by_name = {}
    for row in payload["rows"]:
        by_name.setdefault(row["profile"], []).append(row["billing"])
    assert "free" in by_name["devin-swe2"]
    assert "free" in by_name["devin-swe2-medium"]
    assert "free" in by_name["devin-swe2-max"]
    assert by_name["devin-ds41"] == ["paid"]
    # neutral default: every non-devin row stays unknown
    for row in payload["rows"]:
        if profile_pool(row["profile"])[0] != "devin":
            assert row["billing"] == "unknown", row


# ── AC2: property tests over generated candidate sets ────────────────────────


def _candidate_template(monkeypatch) -> recommend_mod._Candidate:
    """A real _Candidate built by _evaluate, used as the replace() base so the
    property tests exercise genuine production candidate fields."""
    monkeypatch.setattr(recommend_mod, "get_policy", lambda *a, **k: ("preserve", None))
    monkeypatch.setattr(recommend_mod, "get_boost", lambda *a, **k: (None, None))
    table = {grade: [] for grade in GRADES}
    table["A"].append(Profile("sonnet", "Exact", 50.0))
    evaluation = recommend_mod._evaluate(
        _pool_providers(),
        "A",
        TODAY,
        NOW,
        urgency_hours=24,
        bench_scores=[],
        normalized_prices={},
        table=table,
    )
    return evaluation.included[0]


def _main_key(candidate, bench_scores, value_order, intra_pool_rank, slot):
    """main's (ead1794) sort key, verbatim — the oracle single-cost pools and
    same-billing groups must keep sorting by."""
    return (
        0 if _profile_has_benchmark_score(candidate.profile, bench_scores) else 1,
        0 if candidate.imminent_exhaustion else 1,
        0 if candidate.boost is not None else 1,
        candidate.boost if candidate.boost is not None else 0,
        -candidate.score,
        value_order[id(candidate.profile)],
        intra_pool_rank[id(candidate.profile)],
        1 if candidate.one_up else 0,
        slot,
    )


def _generated_set(rng, base, seed, equal_quota_dims=False):
    """A randomized candidate set: name carries the pool, billing is
    randomized, and quota/boost dims may be pinned equal on request."""
    candidates = []
    value_order: dict[int, int] = {}
    intra_pool_rank: dict[int, int] = {}
    pools = ["devin", "claude", "codex"]
    for index in range(rng.randint(3, 12)):
        pool = rng.choice(pools)
        billing = rng.choice(["free", "paid", "unknown", "unknown"])
        profile = replace(
            base.profile,
            name=f"{pool}-s{seed}-r{index}",
            benchmark=None if equal_quota_dims else rng.choice([None, 20.0, 80.0]),
            billing=billing,
        )
        candidates.append(
            replace(
                base,
                profile=profile,
                provider_id=pool,
                one_up=bool(rng.getrandbits(1)),
                imminent_exhaustion=False if equal_quota_dims else rng.choice([False, False, True]),
                boost=None if equal_quota_dims else rng.choice([None, None, 1, 2, 5]),
                score=0.0 if equal_quota_dims else rng.choice([0.0, 50.0, 112.5, 250.0]),
            )
        )
        value_order[id(profile)] = index
        intra_pool_rank[id(profile)] = index
    slot = {id(candidate): index for index, candidate in enumerate(candidates)}
    return candidates, value_order, intra_pool_rank, slot


def test_rank_key_is_a_total_order_under_permutation(monkeypatch):
    """The key is a total order: sorting is identical across input
    permutations and no intransitive triple exists — with billing varied."""
    base = _candidate_template(monkeypatch)
    rng = random.Random(1340)
    intransitive = 0
    order_dependent = 0
    for seed in range(1500):
        candidates, value_order, intra_pool_rank, slot = _generated_set(rng, base, seed)

        def key_of(c, vo=value_order, rank=intra_pool_rank, slots=slot):
            return _recommend_rank_key(c, [], vo, rank, slots[id(c)])

        orders = set()
        for perm_index in range(6):
            perm = list(candidates)
            if perm_index == 1:
                perm.reverse()
            elif perm_index > 1:
                rng.shuffle(perm)
            orders.add(tuple(c.profile.name for c in sorted(perm, key=key_of)))
        if len(orders) != 1:
            order_dependent += 1
        for a, b, c in itertools.permutations(candidates, 3):
            if key_of(a) < key_of(b) < key_of(c) and not key_of(a) < key_of(c):
                intransitive += 1
    assert intransitive == 0 and order_dependent == 0


def test_single_cost_and_all_unknown_pools_keep_mains_order(monkeypatch):
    """Invariant (b): "Single-cost pools keep main's order."

    Generated sets, grouped by pool: any pool whose rows share one billing
    class — free-only, paid-only, or all-unknown — must sort byte-identically
    to main's key. A dimension that varied inside a same-billing group
    (e.g. keyed on one_up, or on something other than the profile datum)
    reorders such a pool and fails here.
    """
    base = _candidate_template(monkeypatch)
    rng = random.Random(13401)
    checked_pools = 0
    for seed in range(1500):
        candidates, value_order, intra_pool_rank, slot = _generated_set(rng, base, seed)
        new_order = sorted(
            candidates,
            key=lambda c: _recommend_rank_key(c, [], value_order, intra_pool_rank, slot[id(c)]),
        )
        main_order = sorted(
            candidates,
            key=lambda c: _main_key(c, [], value_order, intra_pool_rank, slot[id(c)]),
        )
        by_pool: dict[str, list] = {}
        for c in candidates:
            by_pool.setdefault(c.provider_id, []).append(c)
        for pool, members in by_pool.items():
            if len({m.profile.billing for m in members}) != 1:
                continue
            checked_pools += 1
            new_pool = [c.profile.name for c in new_order if c.provider_id == pool]
            main_pool = [c.profile.name for c in main_order if c.provider_id == pool]
            assert new_pool == main_pool, (pool, members, new_pool, main_pool)
        # Stronger form: every same-billing slice, pooled or not, keeps main's
        # relative order — the billing term is a constant inside the slice.
        for billing in ("free", "paid", "unknown"):
            assert [c.profile.name for c in new_order if c.profile.billing == billing] == [
                c.profile.name for c in main_order if c.profile.billing == billing
            ]
    assert checked_pools > 0


def test_mixed_pool_free_never_below_paid_at_equal_quota_and_boost(monkeypatch):
    """Invariant (a): "A paid row never outranks a free row of the same pool
    at equal quota and boost standing."

    With quota/boost/score pinned equal inside a generated pool, every free
    and every unknown row sorts ahead of every paid row of that pool.
    """
    base = _candidate_template(monkeypatch)
    rng = random.Random(13402)
    mixed_pools = 0
    for seed in range(1500):
        candidates, value_order, intra_pool_rank, slot = _generated_set(
            rng, base, seed, equal_quota_dims=True
        )
        order = sorted(
            candidates,
            key=lambda c: _recommend_rank_key(c, [], value_order, intra_pool_rank, slot[id(c)]),
        )
        rank_of = {id(c): index for index, c in enumerate(order)}
        by_pool: dict[str, list] = {}
        for c in candidates:
            by_pool.setdefault(c.provider_id, []).append(c)
        for pool, members in by_pool.items():
            billings = {m.profile.billing for m in members}
            if "paid" not in billings or not billings & {"free", "unknown"}:
                continue
            mixed_pools += 1
            worst_free = max(rank_of[id(m)] for m in members if m.profile.billing != "paid")
            best_paid = min(rank_of[id(m)] for m in members if m.profile.billing == "paid")
            assert worst_free < best_paid, (pool, order)
    assert mixed_pools > 0
