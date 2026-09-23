"""task #578 round 5 — the design table (hk review/2026-09-24/578-ac-review, "라운드 5
설계 고정") pinned by one fixture per row, every r1–r4 counterexample, and
fixed-seed property tests against an independent oracle of the same table.

Seeds: ``QUOTA_V2_PROP_SEEDS=start:stop`` overrides the default range so a
verifier can rerun the properties on other seeds.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random

import pytest

from scopefuel import cache, quota_v2
from scopefuel.model import Bucket, ProviderResult, Scope

T0 = dt.datetime(2026, 9, 24, 1, 0, 0, tzinfo=dt.UTC)
ACCOUNT = "acct_7k2m9q4x"
OTHER_ACCOUNT = "acct_p3n8w5r1"
OWN = ("node-a", 1)
OTHER = ("node-b", 2)
TTL = cache.PROVIDER_TTL_S["claude"]
RESET_5H = "2026-09-24T04:00:00Z"
RESET_7D = "2026-09-28T01:00:00Z"
DUPLICATE_REF = "parse_error:duplicate_limit"


def _seeds() -> range:
    raw = os.environ.get("QUOTA_V2_PROP_SEEDS", "0:300")
    start, stop = (int(part) for part in raw.split(":"))
    return range(start, stop)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _identity(machine: str = OWN[0], revision: int = OWN[1], *, valid: bool = True) -> dict:
    return {
        "provider": "claude",
        "account_ref": ACCOUNT,
        "entitlement_ref": "unknown",
        "binding_revision": revision,
        "machine_id": machine,
        "local_slot_ref": "slot-0000000000000000",
        "verified_at": _iso(T0 - dt.timedelta(days=1)),
        "valid_until": _iso(T0 + (dt.timedelta(days=1) if valid else -dt.timedelta(seconds=1))),
        "label": None,
    }


def _bucket_row(
    label: str, kind: str, ref: str | None, window: str, used: float, *, old_id: bool = False
) -> dict:
    horizon = "now" if window == "5h" else "week"
    reset = RESET_5H if window == "5h" else RESET_7D
    bucket = Bucket(label, window, used, reset, Scope(kind, ref), horizon)
    limit = quota_v2._base_limit_id(bucket) if old_id else quota_v2.limit_id(bucket)
    return {
        "limit_id": limit,
        "label": label,
        "scope": {"kind": kind, "ref": ref},
        "horizon": horizon,
        "window": window,
        "window_instance": reset,
        "used_pct": used,
        "reset_at": reset,
        "observed_at": None,
    }


def _standard(
    five: float = 10.0, week: float = 20.0, opus: float = 30.0, *, old_id: bool = False
) -> list[dict]:
    return [
        _bucket_row("5h", "account", None, "5h", five, old_id=old_id),
        _bucket_row("7d all", "account", None, "7d", week, old_id=old_id),
        _bucket_row("7d Opus", "model", "Opus", "7d", opus, old_id=old_id),
    ]


def _obs(
    oid: str,
    seconds: float,
    status: str = "success",
    *,
    slot: tuple[str, int] = OWN,
    buckets: list[dict] | None = None,
    account: str = ACCOUNT,
    error_ref: str | None = None,
) -> dict:
    measuring = status in quota_v2.MEASURING
    return {
        "schema": quota_v2.OBSERVATION_SCHEMA,
        "observation_id": oid,
        "provider": "claude",
        "account_ref": account,
        "entitlement_ref": "unknown",
        "source_machine": slot[0],
        "source_binding_revision": slot[1],
        "collector_version": quota_v2.COLLECTOR_VERSION,
        "measured_at": _iso(T0 + dt.timedelta(seconds=seconds)),
        "received_at": None,
        "status": status,
        "buckets": (buckets if buckets is not None else _standard()) if measuring else [],
        "error_ref": error_ref if error_ref is not None else (None if measuring else status),
        "lease_epoch": None,
    }


def _dup(oid: str, seconds: float, *, slot: tuple[str, int] = OWN) -> dict:
    return _obs(oid, seconds, "parse_error", slot=slot, error_ref=DUPLICATE_REF)


def _select(rows: list[dict], *, now_s: float = 30.0, identity: dict | None = None):
    return quota_v2._select(
        "claude",
        identity if identity is not None else _identity(),
        rows,
        T0 + dt.timedelta(seconds=now_s),
        "preserve",
    )


def _evaluate(rows: list[dict], *, now_s: float = 30.0, identity: dict | None = None) -> quota_v2.Evaluation:
    snapshot = {
        "schema": quota_v2.SNAPSHOT_SCHEMA,
        "identities": {"claude": identity if identity is not None else _identity()},
        "observations": rows,
    }
    return quota_v2.evaluate(snapshot, "opus", now=T0 + dt.timedelta(seconds=now_s))


# ------------------------------------------------------------------ (b) one fixture per row

ROWS = {
    "I-no-identity": ([_obs("s", 0)], {"identity": _identity(valid=False)}, "IDENTITY_UNKNOWN"),
    "X-other-account-only": ([_obs("s", 0, account=OTHER_ACCOUNT)], {}, "NO_SAMPLE"),
    "O-other-slot-auth-ignored": ([_obs("s", 0), _obs("a", 10, "auth_error", slot=OTHER)], {}, "FRESH"),
    "A-own-auth-latest": (
        [_obs("s", 0, slot=OTHER), _obs("a", 5, "auth_error"), _obs("s2", 10, slot=OTHER)],
        {},
        "AUTH_BLOCKED",
    ),
    "A-own-auth-then-own-429": (
        [_obs("s", 0), _obs("a", 5, "auth_error"), _obs("r", 10, "rate_limited")],
        {},
        "AUTH_BLOCKED",
    ),
    "A-own-auth-cleared-by-own-success": ([_obs("a", 0, "auth_error"), _obs("s", 10)], {}, "FRESH"),
    "A-tie-auth-wins": ([_obs("s", 10), _obs("a", 10, "auth_error")], {}, "AUTH_BLOCKED"),
    "N-nothing": ([], {}, "NO_SAMPLE"),
    "N-only-429": ([_obs("r", 0, "rate_limited")], {}, "NO_SAMPLE"),
    "C-duplicate-newest": ([_obs("s", 0), _dup("d", 10)], {}, "CONFLICT"),
    "C-values-differ-same-instant": (
        [_obs("s1", 10, buckets=_standard(10)), _obs("s2", 10, slot=OTHER, buckets=_standard(90))],
        {},
        "CONFLICT",
    ),
    "P-parse-newest": ([_obs("s", 0), _obs("p", 10, "parse_error")], {}, "PARSE_FAILED"),
    "Q-partial-newest": (
        [_obs("s", 0), _obs("q", 10, "partial", buckets=_standard(99, 99, 99))],
        {},
        "PARTIAL",
    ),
    "F-fresh": ([_obs("s", 0)], {}, "FRESH"),
    "F-union-same-instant": (
        [_obs("s1", 10, buckets=_standard()[:2]), _obs("s2", 10, slot=OTHER, buckets=_standard()[1:])],
        {},
        "FRESH",
    ),
    "E-expired": ([_obs("s", 0)], {"now_s": TTL + 1}, "STALE_EXPIRED"),
    "S-stale-after-429": ([_obs("s", 0), _obs("r", 300, "rate_limited")], {"now_s": 400}, "STALE"),
    "S-tie-success-and-429": ([_obs("s", 10), _obs("r", 10, "transport_error", slot=OTHER)], {}, "STALE"),
    "S'-parse-between": (
        [_obs("s", 0), _obs("p", 100, "parse_error", slot=OTHER), _obs("r", 300, "rate_limited")],
        {"now_s": 400},
        "PARSE_FAILED",
    ),
    "S'-duplicate-between": (
        [_obs("s", 0), _dup("d", 100), _obs("r", 300, "rate_limited")],
        {"now_s": 400},
        "CONFLICT",
    ),
    "S'-partial-between": (
        [_obs("s", 0), _obs("q", 100, "partial"), _obs("r", 300, "transport_error")],
        {"now_s": 400},
        "PARTIAL",
    ),
    "T-parse-beats-success": ([_obs("s", 10), _obs("p", 10, "parse_error", slot=OTHER)], {}, "PARSE_FAILED"),
    "T-conflict-beats-parse": ([_dup("d", 10), _obs("p", 10, "parse_error", slot=OTHER)], {}, "CONFLICT"),
    "T-partial-beats-429": (
        [_obs("q", 10, "partial"), _obs("r", 10, "rate_limited", slot=OTHER)],
        {},
        "PARTIAL",
    ),
}


@pytest.mark.parametrize("name", sorted(ROWS))
def test_design_table_row(name):
    rows, kwargs, expected = ROWS[name]
    assert _select(rows, **kwargs).code == expected
    assert _oracle(rows, **kwargs) == expected


def test_old_success_never_covers_a_newer_error_end_to_end():
    """OLD-1 through the full evaluator: only the explicit stale path may use
    an older success, and it is marked stale_accepted."""
    for rows, now_s in (
        ([_obs("s", 0), _dup("d", 10)], 30),
        ([_obs("s", 0), _obs("p", 10, "parse_error")], 30),
        ([_obs("s", 0), _obs("q", 10, "partial", buckets=_standard(99, 99, 99))], 30),
        ([_obs("s", 0), _obs("p", 100, "parse_error"), _obs("r", 300, "rate_limited")], 400),
    ):
        assert not _evaluate(rows, now_s=now_s).gate.ok
    stale = _evaluate([_obs("s", 0), _obs("r", 300, "rate_limited")], now_s=400)
    assert stale.gate.ok and stale.gate.stale_accepted and stale.code == "STALE_ACCEPTED"


# ------------------------------------------------------------------ r1–r4 counterexamples


def test_r4_sf1_conflicting_twins_do_not_fall_back_to_old_low_value():
    """R4-SF-1: newest measurement reports one limit as 10% and 99%."""
    identity = quota_v2.Identity("claude", ACCOUNT, "unknown", 1, "node-a", "slot-x", 0.0, 1e12)
    twins = ProviderResult(
        id="claude",
        buckets=[
            Bucket("5h", "5h", 10.0, RESET_5H, Scope("account"), "now"),
            Bucket("7d all", "7d", 10.0, RESET_7D, Scope("account"), "week"),
            Bucket("7d all", "7d", 99.0, RESET_7D, Scope("account"), "week"),
        ],
    )
    newest = quota_v2.observation_from_result(
        twins, identity, measured_at=(T0 + dt.timedelta(seconds=30)).timestamp(), observation_id="obs-twins"
    )
    assert newest["error_ref"] == DUPLICATE_REF
    v2 = _evaluate([_obs("obs-prior", 0, buckets=_standard(10, 10, 10)), newest], now_s=40)
    assert v2.code == "CONFLICT" and not v2.gate.ok


def test_r4_sf2_old_and_new_limit_id_formats_name_the_same_limit():
    """R4-SF-2: the hub golden fixture's ids and hashed ids at one instant."""
    old = _obs("obs-old", 10, buckets=_standard(old_id=True))
    new = _obs("obs-new", 10, slot=OTHER, buckets=_standard())
    assert {b["limit_id"] for b in old["buckets"]}.isdisjoint({b["limit_id"] for b in new["buckets"]})
    v2 = _evaluate([old, new])
    assert v2.code == "OK" and v2.gate.ok


def test_warning_only_auth_failure_is_auth_error():
    """CodeRabbit Minor: an auth warning without an error blocks the slot."""
    result = ProviderResult(id="claude", warning="HTTP 401 unauthorized: token expired")
    assert quota_v2.status_for(result) == "auth_error"
    with_values = ProviderResult(
        id="claude",
        warning="login required",
        buckets=[Bucket("5h", "5h", 5.0, RESET_5H, Scope("account"), "now")],
    )
    assert quota_v2.status_for(with_values) == "auth_error"
    plain = ProviderResult(
        id="claude",
        warning="some field missing",
        buckets=[Bucket("5h", "5h", 5.0, RESET_5H, Scope("account"), "now")],
    )
    assert quota_v2.status_for(plain) == "partial"


EARLIER_COUNTEREXAMPLES = {
    # r1 SF-1: own auth, then another node's later success / own later 429
    "r1-sf1-other-success": (
        [_obs("s", 0), _obs("a", 10, "auth_error"), _obs("s2", 20, slot=OTHER)],
        "AUTH_BLOCKED",
    ),
    "r1-sf1-own-429": (
        [_obs("s", 0), _obs("a", 10, "auth_error"), _obs("r", 20, "rate_limited")],
        "AUTH_BLOCKED",
    ),
    # r1 SF-2: newest partial 99% after an older 10%
    "r1-sf2-partial": (
        [_obs("s", 0, buckets=_standard(10)), _obs("q", 20, "partial", buckets=_standard(99, 99, 99))],
        "PARTIAL",
    ),
    # r2 R2-SF-2: same-instant auth/success in both orders
    "r2-tie-auth-first": ([_obs("a", 10, "auth_error"), _obs("s", 10)], "AUTH_BLOCKED"),
    "r2-tie-success-first": ([_obs("s", 10), _obs("a", 10, "auth_error")], "AUTH_BLOCKED"),
}


@pytest.mark.parametrize("name", sorted(EARLIER_COUNTEREXAMPLES))
def test_earlier_round_counterexamples(name):
    rows, expected = EARLIER_COUNTEREXAMPLES[name]
    assert _select(rows).code == expected
    assert _select(list(reversed(rows))).code == expected


def test_r3_counterexamples_keep_distinct_ids_and_agree_across_nodes():
    """R3-SF-1: 123-char refs sharing 122 chars, and the 40-bit collision pair."""
    long_a = _bucket_row("model A", "model", "x" * 122 + "a", "7d", 10.0)
    long_b = _bucket_row("model B", "model", "x" * 122 + "b", "7d", 80.0)
    pair_a = _bucket_row("limit-763944", "account", None, "7d", 10.0)
    pair_b = _bucket_row("limit-1182207", "account", None, "7d", 20.0)
    for extra in ([long_a, long_b], [pair_a, pair_b]):
        assert extra[0]["limit_id"] != extra[1]["limit_id"] and all(len(b["limit_id"]) <= 128 for b in extra)
        first = _obs("obs-first", 10, buckets=_standard() + extra)
        second = _obs("obs-second", 10, slot=OTHER, buckets=_standard() + list(reversed(extra)))
        assert quota_v2.validate_observation(first) and quota_v2.validate_observation(second)
        assert _select([first, second]).code == "FRESH"


# ------------------------------------------------------------------ independent oracle of table (b)

_RANK = ["success", "availability", "partial", "parse", "conflict"]


def _oracle(rows: list[dict], *, now_s: float = 30.0, identity: dict | None = None) -> str:
    """The table, written as independent predicates (not the implementation)."""
    identity = identity if identity is not None else _identity()
    now = T0 + dt.timedelta(seconds=now_s)
    if dt.datetime.fromisoformat(identity["valid_until"].replace("Z", "+00:00")) <= now:
        return "IDENTITY_UNKNOWN"
    kept = {}
    for row in rows:
        when = dt.datetime.fromisoformat(row["measured_at"].replace("Z", "+00:00"))
        if row["account_ref"] == ACCOUNT and (when - now).total_seconds() <= quota_v2.MAX_FUTURE_SKEW_S:
            kept[json.dumps(row, sort_keys=True)] = (when, row)
    events = list(kept.values())

    def own(row: dict) -> bool:
        return (row["source_machine"], row["source_binding_revision"]) == (
            identity["machine_id"],
            identity["binding_revision"],
        )

    own_events = [(w, r) for w, r in events if own(r) and r["status"] in ("auth_error", "success", "partial")]
    if own_events:
        latest = max(w for w, _ in own_events)
        last_auth = max((w for w, r in own_events if r["status"] == "auth_error"), default=None)
        after = [
            w
            for w, r in own_events
            if r["status"] != "auth_error" and last_auth is not None and w > last_auth
        ]
        if last_auth is not None and not after and last_auth >= latest:
            return "AUTH_BLOCKED"
    events = [(w, r) for w, r in events if r["status"] != "auth_error"]
    if not events:
        return "NO_SAMPLE"

    def klass(moment: dt.datetime) -> str:
        group = [r for w, r in events if w == moment]
        seen: dict = {}
        conflict = False
        for r in group:
            if r["status"] == "success":
                for b in r["buckets"]:
                    key = (
                        b["scope"]["kind"],
                        b["scope"]["ref"] or "",
                        b["window"],
                        b["horizon"],
                        b["label"] or "",
                    )
                    value = (b["used_pct"], b["reset_at"])
                    conflict |= key in seen and seen[key] != value
                    seen.setdefault(key, value)
        names = {
            "success"
            if r["status"] == "success"
            else "availability"
            if r["status"] in ("rate_limited", "transport_error")
            else "partial"
            if r["status"] == "partial"
            else "conflict"
            if r.get("error_ref") == DUPLICATE_REF
            else "parse"
            for r in group
        }
        if conflict:
            names.add("conflict")
        return max(names, key=_RANK.index)

    stop = {"conflict": "CONFLICT", "parse": "PARSE_FAILED", "partial": "PARTIAL"}
    moments = sorted({w for w, _ in events}, reverse=True)
    newest = klass(moments[0])
    if newest in stop:
        return stop[newest]
    if newest == "success":
        return "FRESH" if (now - moments[0]).total_seconds() <= TTL else "STALE_EXPIRED"
    for moment in moments:
        group_class = klass(moment)
        if group_class in stop:
            return stop[group_class]
        if any(r["status"] == "success" for w, r in events if w == moment):
            return "STALE"
    return "NO_SAMPLE"


# ------------------------------------------------------------------ property tests (fixed seeds)

_LABELS = [
    "requests",
    "Requests",
    "requests-1",
    "A B",
    "A-B",
    "limit-763944",
    "limit-1182207",
    "x" * 121 + "a",
]
_REFS = ["Opus", "Sonnet", "x" * 122 + "a", "x" * 122 + "b"]


def _random_bucket(rng: random.Random) -> Bucket:
    kind = rng.choice(["account", "model", "group"])
    window = rng.choice(["5h", "7d"])
    return Bucket(
        rng.choice(_LABELS),
        window,
        rng.choice([0.0, 10.0, 55.5, 99.0]),
        RESET_5H if window == "5h" else RESET_7D,
        Scope(kind, None if kind == "account" else rng.choice(_REFS)),
        "now" if window == "5h" else "week",
    )


def _identity_tuple(bucket: Bucket) -> tuple:
    return (bucket.scope.kind, bucket.scope.name or "", bucket.window, bucket.horizon, bucket.label)


@pytest.mark.parametrize("seed", _seeds())
def test_property_limit_id_is_a_pure_injective_function_of_identity(seed):
    """ID-1/ID-2/ID-3 on random bucket sets, shuffles and injected twins."""
    rng = random.Random(seed)
    buckets = [_random_bucket(rng) for _ in range(rng.randint(1, 8))]
    by_identity: dict[tuple, str] = {}
    for bucket in buckets:
        lid = quota_v2.limit_id(bucket)
        assert len(lid) <= quota_v2.LIMIT_ID_MAX
        moved = Bucket(
            bucket.label, bucket.window, 42.0, "2026-10-01T00:00:00Z", bucket.scope, bucket.horizon
        )
        assert quota_v2.limit_id(moved) == lid  # ID-1: value-independent
        previous = by_identity.setdefault(_identity_tuple(bucket), lid)
        assert previous == lid
    assert len(set(by_identity.values())) == len(by_identity)  # ID-2
    distinct = list({_identity_tuple(b): b for b in buckets}.values())
    shuffled = distinct[:]
    rng.shuffle(shuffled)
    forward = {r["limit_id"] for r in quota_v2.buckets_to_v2(distinct)}
    backward = {r["limit_id"] for r in quota_v2.buckets_to_v2(shuffled)}
    alone = {quota_v2.buckets_to_v2([b])[0]["limit_id"] for b in distinct}
    assert forward == backward == alone  # ID-1: order- and set-independent
    identity = quota_v2.Identity("claude", ACCOUNT, "unknown", 1, "node-a", "slot-x", 0.0, 1e12)
    twin = rng.choice(distinct)
    same = quota_v2.observation_from_result(
        ProviderResult(id="claude", buckets=[*distinct, twin]), identity, measured_at=T0.timestamp()
    )
    assert quota_v2.validate_observation(same) and len(same["buckets"]) == len(distinct)  # ID-3 equal twins
    other_value = 1.0 if twin.used_pct != 1.0 else 2.0
    clash = Bucket(twin.label, twin.window, other_value, twin.resets_at, twin.scope, twin.horizon)
    conflicting = quota_v2.observation_from_result(
        ProviderResult(id="claude", buckets=[*distinct, clash]), identity, measured_at=T0.timestamp()
    )
    assert conflicting["status"] == "parse_error" and conflicting["error_ref"] == DUPLICATE_REF  # ID-3


def _random_history(rng: random.Random) -> list[dict]:
    rows = []
    for index in range(rng.randint(0, 7)):
        status = rng.choice(
            ["success"] * 4
            + ["rate_limited", "transport_error", "parse_error", "partial", "auth_error", "duplicate"]
        )
        slot = rng.choice([OWN, OWN, OTHER])
        seconds = rng.choice([0, 60, 120, 120, 250, 300])  # repeated instants create ties
        values = rng.choice([(10, 20, 30), (10, 20, 30), (90, 95, 99)])
        subset = rng.choice([slice(0, 3), slice(0, 2), slice(1, 3)])
        buckets = _standard(*values, old_id=rng.random() < 0.3)[subset]
        oid = f"obs-{index}" if rng.random() > 0.1 else "obs-shared"  # same id, different content
        if status == "duplicate":
            rows.append(_dup(oid, seconds, slot=slot))
        else:
            rows.append(_obs(oid, seconds, status, slot=slot, buckets=buckets))
    return rows


@pytest.mark.parametrize("seed", _seeds())
def test_property_history_matches_table_and_invariants(seed):
    """Oracle agreement, PERM-1, OLD-1 and MONO-1 on random histories."""
    rng = random.Random(seed)
    rows = _random_history(rng)
    now_s = rng.choice([130.0, 310.0, 500.0, 1000.0])
    selection = _select(rows, now_s=now_s)
    assert selection.code == _oracle(rows, now_s=now_s)
    for _ in range(3):  # PERM-1
        shuffled = rows[:]
        rng.shuffle(shuffled)
        again = _select(shuffled, now_s=now_s)
        assert (again.code, again.observation_ids) == (selection.code, selection.observation_ids)
    verdict = _evaluate(rows, now_s=now_s)
    if verdict.gate.ok:  # OLD-1
        chosen = {o["observation_id"] for o in rows if o["observation_id"] in verdict.observation_ids}
        chosen_at = max(
            o["measured_at"] for o in rows if o["observation_id"] in chosen and o["status"] == "success"
        )
        later = [
            o for o in rows if o["measured_at"] >= chosen_at and o["status"] not in ("success", "auth_error")
        ]
        if verdict.gate.stale_accepted:
            assert all(o["status"] in ("rate_limited", "transport_error") for o in later)
        else:
            assert not later
    for bad in ("parse_error", "partial", "duplicate", "auth_error"):  # MONO-1
        extra = _dup("obs-late", 305) if bad == "duplicate" else _obs("obs-late", 305, bad)
        assert not _evaluate([*rows, extra], now_s=max(now_s, 310.0)).gate.ok
