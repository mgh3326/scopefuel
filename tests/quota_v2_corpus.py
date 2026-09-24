"""Mechanical expansion of the quota v2 contract corpus (tests/fixtures/quota_v2_contract_r3.json).

Only fills defaults, turns second offsets into RFC3339 and copies definition
fields from the contract §3 tables. Expected results are read from the corpus
and never computed here. The `ctest` provider exists only in the corpus
(contract §3.3), so its definitions live here, not in production code.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib

from scopefuel.quota_v2_contract import CLAUDE, LimitDef, ProviderContract
from scopefuel.quota_v2_eval import SNAPSHOT_SCHEMA, Clock

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CORPUS = json.loads((FIXTURES / "quota_v2_contract_r3.json").read_text())
CONV = CORPUS["conventions"]
T0 = dt.datetime(2026, 9, 24, 1, 0, 0, tzinfo=dt.UTC)

CTEST = ProviderContract(
    provider="ctest",
    ttl_s=60.0,
    limits={
        "ctest.fixed_5h": LimitDef("ctest.fixed_5h", "account", None, "5h", "now", 18000.0, "fixed", True),
        "ctest.fixed_7d": LimitDef("ctest.fixed_7d", "account", None, "7d", "week", 604800.0, "fixed", True),
        "ctest.rolling_1d": LimitDef(
            "ctest.rolling_1d", "group", "pool", "1d", "week", 86400.0, "none", False
        ),
    },
    has_gate=False,
)
CORPUS_DEFINITIONS = {"claude": CLAUDE, "ctest": CTEST}


def at(offset: float) -> str:
    return (T0 + dt.timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")


def definition(limit_id: str) -> tuple:
    for contract in CORPUS_DEFINITIONS.values():
        if limit_id in contract.limits:
            d = contract.limits[limit_id]
            return d.kind, d.ref, d.window, d.horizon, d.instance_rule
    if limit_id.startswith("claude.weekly_scoped.model."):
        return "model", limit_id.rsplit(".", 1)[1], "7d", "week", "none"
    parts = limit_id.split(":")
    if len(parts) >= 3 and parts[0] in ("account", "model", "group"):  # old experimental ids
        return (
            parts[0],
            None if parts[1] == "-" else parts[1],
            parts[2],
            "now" if parts[2] == "5h" else "week",
            "none",
        )
    return "account", None, "7d", "week", "none"  # undefined ids: shape only (§3.6 decides)


def _vals(vals) -> dict:
    sets = CONV["value_sets"]
    if isinstance(vals, str):
        return copy.deepcopy(sets[vals])
    out = copy.deepcopy(sets[vals["extend"]]) if "extend" in vals else {}
    out.update({k: v for k, v in vals.items() if k != "extend"})
    return out


def _bucket(limit_id: str, spec, overrides: dict, labels: dict) -> dict:
    if isinstance(spec, list):
        spec = {"used": spec[0], "reset": spec[1], "instance": spec[2] if len(spec) > 2 else None}
    kind, ref, window, horizon, rule = definition(limit_id)
    override = overrides.get(limit_id, {})
    reset = spec.get("reset_str") or (None if spec.get("reset") is None else at(spec["reset"]))
    instance = spec.get("instance")
    if instance is None:
        instance = at(spec["reset"]) if rule == "fixed" and spec.get("reset") is not None else "unknown"
    return {
        "limit_id": limit_id,
        "label": labels.get(limit_id),
        "scope": {"kind": kind, "ref": ref},
        "horizon": override.get("horizon", horizon),
        "window": override.get("window", window),
        "window_instance": instance,
        "used_pct": spec["used"],
        "reset_at": reset,
        "observed_at": at(spec["observed"]) if "observed" in spec else None,
    }


def src_of(spec) -> dict:
    src = spec.get("src", "own")
    return CONV["src"][src] if isinstance(src, str) else src


def observation(case: dict, spec: dict) -> dict:
    src = src_of(spec)
    status = spec.get("status", "success")
    buckets = []
    if status in ("success", "partial"):
        buckets = [
            _bucket(k, v, spec.get("defs", {}), spec.get("labels", {}))
            for k, v in _vals(spec["vals"]).items()
        ]
    own = CONV["identity"]["machine"]
    received = (
        spec["received_at"] if "received_at" in spec else (None if src["machine"] == own else spec["t"] + 1)
    )
    obs = {
        "schema": "quota-observation/v2",
        "contract_rev": spec.get("contract_rev", CONV["defaults"]["contract_rev"]),
        "observation_id": spec["id"],
        "provider": spec.get("provider", case.get("provider", "claude")),
        "account_ref": spec.get("account", CONV["identity"]["account"]),
        "entitlement_ref": spec.get("entitlement", "unknown"),
        "source_machine": src["machine"],
        "source_slot_ref": src["slot"],
        "source_binding_revision": src["revision"],
        "collector_version": "scopefuel/0.1.0+quota-v2.r3",
        "measured_at": at(spec["t"]),
        "received_at": None if received is None else at(received),
        "status": status,
        "error_ref": spec.get("error_ref"),
        "unshared_limit_count": 0,
        "buckets": buckets,
        "lease_epoch": None,
    }
    if obs["contract_rev"] is None:
        del obs["contract_rev"]
    return obs


def identity(case: dict) -> dict | None:
    if "identity" in case and case["identity"] is None:
        return None
    ident = dict(CONV["identity"])
    ident.update(case.get("identity") or {})
    if "provider" not in (case.get("identity") or {}):
        ident["provider"] = case.get("provider", "claude")
    return {
        "provider": ident["provider"],
        "account_ref": ident["account"],
        "entitlement_ref": ident["entitlement"],
        "binding_revision": ident["revision"],
        "machine_id": ident["machine"],
        "local_slot_ref": ident["slot"],
        "valid_from": at(ident["valid_from"]),
        "valid_until": at(ident["valid_until"]),
        "label": None,
    }


def snapshot(case: dict, observations: list[dict]) -> dict:
    ident = identity(case)
    provider = case.get("provider") or (ident["provider"] if ident else "claude")
    return {
        "schema": SNAPSHOT_SCHEMA,
        "provider": provider,
        "identities": {provider: ident} if ident else {},
        "observations": observations,
    }


def clock(case: dict) -> Clock:
    raw = case.get("clock", CONV["clock"])
    return Clock(raw["skew_bound_s"], raw["source"])


def now(case: dict) -> dt.datetime:
    return T0 + dt.timedelta(seconds=case.get("now", CONV["now"]))


def profile(case: dict) -> str:
    return case.get("profile", "opus")


def support_list(case: dict) -> list[str]:
    return case.get("support_list", CONV["support_list"])


def _token(value):
    if not isinstance(value, str) or "*" not in value or len(value) >= 16:
        return value
    head, _, rest = value.partition("*")
    count, _, tail = rest.partition("+")
    return head * int(count) + tail if count.isdigit() else value


def wire_envelope(case: dict) -> dict:
    env = observation({"provider": "claude"}, {"id": "obs-wire", "t": 500, "vals": "claude.std"})
    env["received_at"] = None
    for path, value in case["patch"].items():
        if path.startswith("-"):
            env.pop(path[1:], None)
            continue
        value = {k: _token(v) for k, v in value.items()} if isinstance(value, dict) else _token(value)
        if isinstance(value, str) and value.startswith("+") and value.endswith("s"):
            value = at(500 + float(value[1:-1]))
        target, key = env, path
        if path.startswith("buckets["):
            index = int(path[8 : path.index("]")])
            target, key = env["buckets"][index], path[path.index("].") + 2 :]
        target[key] = value
    return env


def expanded_wire() -> list[dict]:
    """The exact envelopes both Go and Python validate (tests/fixtures/quota_v2_wire_r3.json)."""
    return [
        {"id": c["id"], "form": c["form"], "envelope": wire_envelope(c), "expect": c["expect"]}
        for c in CORPUS["wire"]
    ]
