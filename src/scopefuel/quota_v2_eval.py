"""quota v2 evaluator — contract r3 §6 (hk `review/2026-09-24/578-contract-r3.1`).

Pure: reads only the snapshot and arguments it is given. The table rows are
kept in the contract's order; each helper names the row it implements. The
final allow/deny for claude is the existing `recommend.gate_check` (§6G), so
there is one judge of quota meaning.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from . import recommend
from .model import Bucket, PoolClass, ProviderResult, Scope
from .quota_v2_contract import (
    CONTRACT_REV,
    DEFINITIONS,
    MEASURING,
    STALE_MAX_S,
    SUPPORT_LIST,
    ProviderContract,
    parse_time,
    valid_stored,
)

SNAPSHOT_SCHEMA = "quota-snapshot/v2"
_RANK = {"success": 0, "avail": 1, "partial": 2, "parse": 3, "conflict": 4}


@dataclass(frozen=True)
class Clock:
    """§5.5: the bound on cross-machine clock error and where it comes from."""

    skew_bound_s: float | None
    source: str


UNKNOWN_CLOCK = Clock(None, "unknown")


@dataclass(frozen=True)
class Evaluation:
    code: str
    ok: bool
    reason: str | None = None
    selected: tuple[str, ...] = ()
    excluded: Mapping[str, int] = field(default_factory=dict)
    gate: recommend.GateResult | None = None
    account_ref: str | None = None
    binding_revision: int | None = None
    measured_at: str | None = None
    age_s: float | None = None
    freshness: str | None = None

    @property
    def exit_code(self) -> int:
        if self.ok:
            return 0
        if self.gate is not None and self.gate.role_denied:
            return 5
        return 3 if self.code == "QUOTA_DENIED" else 4

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "reason": self.reason,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "unmeasurable": not self.ok and self.code != "QUOTA_DENIED",
            "stale_accepted": self.code == "STALE_ACCEPTED",
            "role_denied": bool(self.gate and self.gate.role_denied),
            "used_pct": self.gate.used_pct if self.gate else None,
            "account_ref": self.account_ref,
            "binding_revision": self.binding_revision,
            "selected": list(self.selected),
            "excluded": dict(self.excluded),
            "measured_at": self.measured_at,
            "age_s": None if self.age_s is None else round(self.age_s, 3),
            "freshness": self.freshness,
        }


@dataclass
class _Obs:
    raw: dict
    tau: dt.datetime
    times: list[dt.datetime]
    machine: str
    own_machine: bool
    own_slot: bool
    klass: str
    violation: bool

    @property
    def id(self) -> str:
        return self.raw["observation_id"]

    @property
    def sort_key(self) -> tuple:
        return (self.tau, self.id, json.dumps(self.raw, sort_keys=True))


def _identity(raw: object, provider: str, now: dt.datetime) -> dict | None:
    """§6 I: a well-formed execution identity whose window contains now."""
    if not isinstance(raw, Mapping):
        return None
    keys = (
        "provider",
        "account_ref",
        "entitlement_ref",
        "binding_revision",
        "machine_id",
        "local_slot_ref",
        "valid_from",
        "valid_until",
    )
    if any(k not in raw for k in keys) or raw["provider"] != provider:
        return None
    start, end = parse_time(raw["valid_from"]), parse_time(raw["valid_until"])
    if start is None or end is None or not start < end or not start <= now < end:
        return None
    return dict(raw, _start=start, _end=end)


def _complete_success(raw: dict, contract: ProviderContract) -> bool:
    if raw["status"] != "success":
        return False
    values = {b["limit_id"]: b for b in raw["buckets"]}
    for limit_id in contract.required:
        bucket = values.get(limit_id)
        if bucket is None or bucket["used_pct"] is None or bucket["reset_at"] is None:
            return False
    return True


def _class_of(raw: dict, contract: ProviderContract) -> str:
    status = raw["status"]
    if status == "success":
        return "success" if _complete_success(raw, contract) else "partial"
    if status == "partial":
        return "partial"
    if status in ("rate_limited", "transport_error"):
        return "avail"
    if raw["error_ref"] == "parse_error:duplicate_limit":
        return "conflict"
    return "parse"  # parse_error (auth_error is handled before classes are used)


def _violation(raw: dict, contract: ProviderContract) -> bool:
    """§3.6 definition violation of any bucket."""
    measured = parse_time(raw["measured_at"])
    for bucket in raw["buckets"]:
        definition = contract.limits.get(bucket["limit_id"])
        if definition is None:
            return True
        scope = bucket["scope"]
        if (scope["kind"], scope["ref"], bucket["window"], bucket["horizon"]) != (
            definition.kind,
            definition.ref,
            definition.window,
            definition.horizon,
        ):
            return True
        instance = bucket["window_instance"]
        if definition.instance_rule == "none":
            if instance != "unknown":
                return True
            continue
        if instance == "unknown":
            continue
        end = parse_time(instance)
        reset = parse_time(bucket["reset_at"]) if bucket["reset_at"] is not None else None
        observed = parse_time(bucket["observed_at"]) if bucket["observed_at"] is not None else measured
        if end is None or reset != end or observed is None:
            return True
        start = end - dt.timedelta(seconds=definition.window_length_s)
        if not start <= observed < end:
            return True
    return False


def _tau_and_times(raw: dict) -> tuple[dt.datetime, list[dt.datetime]]:
    measured = parse_time(raw["measured_at"])
    assert measured is not None
    times = [measured]
    bucket_times = []
    for bucket in raw["buckets"]:
        observed = parse_time(bucket["observed_at"]) if bucket["observed_at"] is not None else None
        if observed is not None:
            times.append(observed)
        bucket_times.append(observed or measured)
    tau = min(bucket_times) if raw["status"] in MEASURING and bucket_times else measured
    return tau, times


def _values_equal(a: dict, b: dict) -> bool:
    """§6 V for one limit."""
    ua, ub = a["used_pct"], b["used_pct"]
    if (ua is None) != (ub is None) or (ua is not None and float(ua) != float(ub)):
        return False
    ra = parse_time(a["reset_at"]) if a["reset_at"] is not None else None
    rb = parse_time(b["reset_at"]) if b["reset_at"] is not None else None
    return ra == rb and a["window_instance"] == b["window_instance"]


def _merge(successes: Sequence[_Obs]) -> dict[str, dict] | None:
    merged: dict[str, dict] = {}
    for obs in sorted(successes, key=lambda o: o.sort_key):
        for bucket in obs.raw["buckets"]:
            seen = merged.get(bucket["limit_id"])
            if seen is not None and not _values_equal(seen, bucket):
                return None
            merged.setdefault(bucket["limit_id"], bucket)
    return merged


class _Order:
    def __init__(self, skew: float):
        self.skew = skew

    def delta(self, a: _Obs, b: _Obs) -> float:
        if a.machine == b.machine:
            return 0.0
        if a.own_machine or b.own_machine:
            return self.skew
        return 2 * self.skew

    def before(self, a: _Obs, b: _Obs) -> bool:
        """a ≺ b: a is provably earlier than b (§5.5)."""
        return (b.tau - a.tau).total_seconds() > self.delta(a, b)

    def frontier(self, items: Sequence[_Obs]) -> list[_Obs]:
        return [o for o in items if not any(self.before(o, p) for p in items if p is not o)]


def _ids(items: Sequence[_Obs]) -> tuple[str, ...]:
    return tuple(sorted(o.id for o in items))


def _max_age(items: Sequence[_Obs], now: dt.datetime, skew: float) -> float:
    return max((now - o.tau).total_seconds() + (0.0 if o.own_machine else skew) for o in items)


@dataclass
class _Outcome:
    code: str
    reason: str | None = None
    selected: tuple[str, ...] = ()
    kind: str | None = None  # "fresh" | "stale"
    values: dict[str, dict] | None = None
    age_s: float | None = None
    failure_kind: str | None = None
    measured_at: str | None = None


def _check_k(
    k: Sequence[_Obs],
    earlier: Sequence[_Obs],
    order: _Order,
    contract: ProviderContract,
    now: dt.datetime,
    skew: float,
) -> _Outcome | None:
    """§6 K1–K3 on the candidate success set K."""
    values = _merge(k)
    assert values is not None
    for obs in k:
        for bucket in obs.raw["buckets"]:
            definition = contract.limits[bucket["limit_id"]]
            if definition.instance_rule != "fixed" or bucket["window_instance"] == "unknown":
                continue
            new_end = parse_time(bucket["window_instance"])
            new_start = new_end - dt.timedelta(seconds=definition.window_length_s)
            for old in earlier:
                if not order.before(old, obs) or _max_age([old], now, skew) > STALE_MAX_S:
                    continue
                for old_bucket in old.raw["buckets"]:
                    if (
                        old_bucket["limit_id"] != bucket["limit_id"]
                        or old_bucket["window_instance"] == "unknown"
                    ):
                        continue
                    old_end = parse_time(old_bucket["window_instance"])
                    if new_end < old_end or (new_end > old_end and new_start < old_end):
                        return _Outcome("CONFLICT", "INSTANCE", _ids([*k, old]))
    for limit_id in contract.required:
        definition = contract.limits[limit_id]
        if definition.instance_rule == "fixed" and values[limit_id]["window_instance"] == "unknown":
            return _Outcome("WINDOW_UNKNOWN", selected=_ids(k))
    for limit_id in contract.required:
        if parse_time(values[limit_id]["reset_at"]) <= now:
            return _Outcome("WINDOW_PASSED", selected=_ids(k))
    return None


def select(
    provider: str,
    identity_raw: object,
    observations: Sequence,
    now: dt.datetime,
    clock: Clock,
    contract: ProviderContract,
) -> tuple[_Outcome, dict, dict | None]:
    """§6 rows I … S for one provider. Returns (outcome, excluded counts, identity)."""
    excluded = {
        k: 0
        for k in (
            "contract_unsupported",
            "invalid",
            "other_account",
            "future",
            "out_of_binding",
            "other_slot_auth",
            "cleared_own_auth",
        )
    }
    identity = _identity(identity_raw, provider, now)
    if identity is None:
        return _Outcome("IDENTITY_UNKNOWN"), excluded, None
    skew = clock.skew_bound_s if clock.skew_bound_s is not None else 0.0
    own_machine = identity["machine_id"]
    kept: list[_Obs] = []
    for raw in observations:
        if not isinstance(raw, dict) or raw.get("contract_rev") != CONTRACT_REV:  # X1
            excluded["contract_unsupported"] += 1
            continue
        if not valid_stored(raw) or (
            raw["source_machine"] != own_machine and raw["received_at"] is None
        ):  # X2
            excluded["invalid"] += 1
            continue
        if (raw["provider"], raw["account_ref"], raw["entitlement_ref"]) != (
            provider,
            identity["account_ref"],
            identity["entitlement_ref"],
        ):  # X3
            excluded["other_account"] += 1
            continue
        tau, times = _tau_and_times(raw)
        is_own_machine = raw["source_machine"] == own_machine
        allowance = 0.0 if is_own_machine else skew
        if any((t - now).total_seconds() > allowance for t in times):  # X4 future
            excluded["future"] += 1
            continue
        own_slot = (
            is_own_machine
            and raw["source_slot_ref"] == identity["local_slot_ref"]
            and (raw["source_binding_revision"] == identity["binding_revision"])
        )
        if own_slot and any(not identity["_start"] <= t < identity["_end"] for t in times):  # X4 window
            excluded["out_of_binding"] += 1
            continue
        kept.append(
            _Obs(
                raw,
                tau,
                times,
                raw["source_machine"],
                is_own_machine,
                own_slot,
                _class_of(raw, contract) if raw["status"] != "auth_error" else "auth",
                _violation(raw, contract),
            )
        )
    kept.sort(key=lambda o: o.sort_key)

    # O: another slot's auth failure is not evidence for this slot.
    for obs in [o for o in kept if o.klass == "auth" and not o.own_slot]:
        excluded["other_slot_auth"] += 1
        kept.remove(obs)
    # A / A′
    own_auth = [o for o in kept if o.klass == "auth"]
    if own_auth:
        latest = max(o.tau for o in own_auth)
        amax = [o for o in own_auth if o.tau == latest]
        released = any(
            o.own_slot and o.klass == "success" and not o.violation and o.tau > latest for o in kept
        )
        if not released:
            return _Outcome("AUTH_BLOCKED", selected=_ids(amax)), excluded, identity
        excluded["cleared_own_auth"] += len(own_auth)
        kept = [o for o in kept if o.klass != "auth"]
    # T
    if clock.skew_bound_s is None and any(not o.own_machine for o in kept):
        return _Outcome("TIME_UNCERTAIN"), excluded, identity
    # D
    violating = [o for o in kept if o.violation and _max_age([o], now, skew) <= STALE_MAX_S]
    if violating:
        return _Outcome("CONFLICT", "CONTRACT", _ids(violating)), excluded, identity
    kept = [o for o in kept if not o.violation]
    if not kept:  # N0
        return _Outcome("NO_SAMPLE"), excluded, identity

    order = _Order(skew)
    frontier = order.frontier(kept)
    f_success = [o for o in frontier if o.klass == "success"]
    worst = max((o.klass for o in frontier), key=_RANK.__getitem__)
    if f_success and _merge(f_success) is None:
        return _Outcome("CONFLICT", "VALUE", _ids(f_success)), excluded, identity
    stop = {
        "conflict": ("CONFLICT", "DUPLICATE_LIMIT"),
        "parse": ("PARSE_FAILED", None),
        "partial": ("PARTIAL", None),
    }
    if worst in stop:
        code, reason = stop[worst]
        return _Outcome(code, reason, _ids([o for o in frontier if o.klass == worst])), excluded, identity

    successes = [o for o in kept if o.klass == "success"]
    if worst == "success":  # F / E
        blocked = _check_k(f_success, successes, order, contract, now, skew)
        if blocked:
            return blocked, excluded, identity
        age = _max_age(f_success, now, skew)
        measured = min(f_success, key=lambda o: o.tau).raw["measured_at"]
        if age <= contract.ttl_s:
            return (
                _Outcome(
                    "FRESH", None, _ids(f_success), "fresh", _merge(f_success), age, measured_at=measured
                ),
                excluded,
                identity,
            )
        return _Outcome("STALE_EXPIRED", selected=_ids(f_success), age_s=age), excluded, identity

    # worst == avail: N / S′ / S
    if not successes:
        return _Outcome("NO_SAMPLE"), excluded, identity
    f_s = order.frontier(successes)
    if _merge(f_s) is None:
        return _Outcome("CONFLICT", "VALUE", _ids(f_s)), excluded, identity
    b = [o for o in kept if o.klass != "success" and any(not order.before(o, s) for s in f_s)]
    b_worst = max((o.klass for o in b), key=_RANK.__getitem__)
    if b_worst in stop:
        code, reason = stop[b_worst]
        return _Outcome(code, reason, _ids([o for o in b if o.klass == b_worst])), excluded, identity
    blocked = _check_k(f_s, successes, order, contract, now, skew)
    if blocked:
        return blocked, excluded, identity
    failure = "rate_limited" if any(o.raw["status"] == "rate_limited" for o in b) else "network"
    measured = min(f_s, key=lambda o: o.tau).raw["measured_at"]
    return (
        _Outcome(
            "STALE", None, _ids([*f_s, *b]), "stale", _merge(f_s), _max_age(f_s, now, skew), failure, measured
        ),
        excluded,
        identity,
    )


def _result_for(provider: str, outcome: _Outcome, pool_class: PoolClass, now: dt.datetime) -> ProviderResult:
    values = outcome.values or {}
    buckets = [
        Bucket(
            label=row["label"] or row["limit_id"],
            window=row["window"],
            used_pct=row["used_pct"],
            resets_at=row["reset_at"],
            scope=Scope(row["scope"]["kind"], row["scope"]["ref"]),
            horizon=row["horizon"],
        )
        for _, row in sorted(values.items())
    ]
    age = outcome.age_s or 0.0
    result = ProviderResult(
        id=provider,
        buckets=buckets,
        source="quota-v2",
        pool_class=pool_class,
        fetched_at=(now - dt.timedelta(seconds=age)).timestamp(),
        age_s=age,
    )
    if outcome.kind == "stale":
        result.stale = True
        result.error_kind = outcome.failure_kind
        result.account_fp_match = True  # identity comes from the binding, not a credential hash
    return result


def evaluate(
    snapshot: Mapping,
    profile: str,
    *,
    now: dt.datetime,
    clock: Clock = UNKNOWN_CLOCK,
    support_list: Sequence[str] = SUPPORT_LIST,
    definitions: Mapping[str, ProviderContract] = DEFINITIONS,
    pool_classes: Mapping[str, PoolClass] | None = None,
    bench_scores: list | None = None,
    model_prices: Mapping | None = None,
    grade_table: dict | None = None,
    operator_request: str | None = None,
    requested_by: str | None = None,
    purpose: str | None = None,
) -> Evaluation:
    """§6 + §6G for the provider of `profile` (or the snapshot's single provider)."""
    if not isinstance(snapshot, Mapping) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError(f"schema 가 {SNAPSHOT_SCHEMA} 가 아니다")
    now = now.replace(tzinfo=dt.UTC) if now.tzinfo is None else now.astimezone(dt.UTC)
    provider = snapshot.get("provider") or recommend.profile_pool(profile)[0]
    if provider not in support_list or provider not in definitions:  # U
        return Evaluation("CONTRACT_UNSUPPORTED", False)
    contract = definitions[provider]
    identities = snapshot.get("identities") or {}
    outcome, excluded, identity = select(
        provider, identities.get(provider), snapshot.get("observations") or [], now, clock, contract
    )
    common = {
        "excluded": excluded,
        "account_ref": identity["account_ref"] if identity else None,
        "binding_revision": identity["binding_revision"] if identity else None,
        "measured_at": outcome.measured_at,
        "age_s": outcome.age_s,
        "freshness": outcome.kind,
    }
    if outcome.kind is None:
        return Evaluation(outcome.code, False, outcome.reason, outcome.selected, **common)
    if outcome.kind == "stale" and outcome.age_s > STALE_MAX_S:
        return Evaluation("STALE_REJECTED", False, None, outcome.selected, **common)
    if not contract.has_gate:  # corpus-only providers (§6G note)
        return Evaluation(
            "FRESH" if outcome.kind == "fresh" else "STALE", True, None, outcome.selected, **common
        )
    pool_class = (pool_classes or {}).get(provider, "preserve")
    result = _result_for(provider, outcome, pool_class, now)
    gate = recommend.gate_check(
        [result],
        profile,
        today=now.date(),
        now=now,
        bench_scores=bench_scores,
        model_prices=model_prices,
        grade_table=grade_table,
        operator_request=operator_request,
        requested_by=requested_by,
        purpose=purpose,
    )
    if gate.ok:
        code = "STALE_ACCEPTED" if outcome.kind == "stale" else "OK"
    elif gate.role_denied:
        code = "ROLE_DENIED"
    elif gate.unmeasurable:
        code = "UNMEASURABLE"
    else:
        code = "QUOTA_DENIED"
    return Evaluation(code, gate.ok, None, outcome.selected, gate=gate, **common)
