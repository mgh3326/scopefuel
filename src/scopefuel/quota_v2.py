"""Account-scoped quota v2 — task #578, contract r3 (shadow only).

Contract: hk `review/2026-09-24/578-contract-r3.1`. This module is the I/O
side: the local binding mirror, the account-scoped observation store, the
recorder (§5.7) and the shadow comparison (§1.2). The contract's data and
forms live in `quota_v2_contract`, the decision table in `quota_v2_eval`.

- ``account_ref`` is an opaque id enrolled by the operator on the hub. A node
  mirrors its own bindings (``scopefuel quota-v2 bindings import``); scopefuel
  never calls the hub.
- A binding names a login slot (``local_slot_ref``), located locally by a hash
  of the provider's config-dir environment — a locator, not an identity.
- Only real attempts are recorded, with ``measured_at`` = the moment the
  response completed, and only when the same binding was valid at both the
  start and the completion of the fetch. Cache hits and backoff are not
  attempts. The legacy cache is never migrated into this store.
- The real gate never reads anything from here; every v2 entry point is
  isolated at the legacy call site.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from . import __version__, recommend
from .model import PoolClass, ProviderResult
from .quota_v2_contract import (
    CONTRACT_REV,
    OBSERVATION_SCHEMA,
    SUPPORT_LIST,
    Attempt,
    iso,
    parse_time,
    valid_post,
    valid_stored,
)
from .quota_v2_eval import SNAPSHOT_SCHEMA, UNKNOWN_CLOCK, Clock, Evaluation, evaluate

__all__ = [
    "BINDINGS_SCHEMA",
    "OBSERVATIONS_SCHEMA",
    "SNAPSHOT_SCHEMA",
    "Clock",
    "Evaluation",
    "Identity",
    "QuotaV2Error",
    "evaluate",
    "import_bindings",
    "import_observations",
    "record_attempts",
    "shadow_gate",
]

OBSERVATIONS_SCHEMA = "quota-observations/v2"  # hub GET /v2/quota/observations
BINDINGS_SCHEMA = "quota-bindings/v2"  # hub GET /v2/quota/bindings
LOCAL_SCHEMA = "scopefuel.quota-v2-local.v3"
MIRROR_SCHEMA = "scopefuel.quota-v2-bindings.v1"
SHADOW_SCHEMA = "scopefuel.quota-v2-shadow.v2"
COLLECTOR_VERSION = f"scopefuel/{__version__}+{CONTRACT_REV}"
MAX_LOCAL_PER_ACCOUNT = 32
SHADOW_MAX_BYTES = 1 << 20
DISABLE_ENV = "SCOPEFUEL_QUOTA_V2"

_ACCOUNT_REF_RE = re.compile(r"acct_[0-9a-z]{8,40}")
_REF_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
_MACHINE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
_PROVIDER_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")

# Same selector choice as the hub's legacy fingerprint: which environment names
# locate a provider's login context on one machine.
_SLOT_KEYS = {
    "claude": ("CLAUDE_CONFIG_DIR", "HOME"),
    "codex": ("CODEX_HOME", "HOME"),
    "grok": ("GROK_HOME", "HOME"),
}


class QuotaV2Error(ValueError):
    """A v2 document is unusable."""


def enabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() not in {"0", "off", "false", "no"}


# --------------------------------------------------------------------------- paths


def v2_dir() -> pathlib.Path:
    from .cache import cache_dir

    return cache_dir() / "quota-v2"


def bindings_path() -> pathlib.Path:
    return v2_dir() / "bindings.json"


def observations_path() -> pathlib.Path:
    return v2_dir() / "observations.json"


def shadow_path() -> pathlib.Path:
    return v2_dir() / "shadow.jsonl"


@contextmanager
def _lock() -> Iterator[None]:
    path = v2_dir() / "v2.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_json(path: pathlib.Path) -> object | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: pathlib.Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False))
    tmp.chmod(0o600)
    tmp.replace(path)


def _match(pattern: re.Pattern, value: object) -> bool:
    return isinstance(value, str) and value.isascii() and pattern.fullmatch(value) is not None


# --------------------------------------------------------------------------- identity


@dataclass(frozen=True)
class Identity:
    """Execution identity (§5.4): machine, slot, provider, account, entitlement, revision."""

    provider: str
    account_ref: str
    entitlement_ref: str
    binding_revision: int
    machine_id: str
    local_slot_ref: str
    valid_from: float
    valid_until: float
    label: str | None = None

    def _key(self) -> tuple:
        return (
            self.provider,
            self.account_ref,
            self.entitlement_ref,
            self.binding_revision,
            self.machine_id,
            self.local_slot_ref,
        )

    def same_binding(self, other: Identity | None) -> bool:
        return other is not None and self._key() == other._key()

    def valid_at(self, t: float) -> bool:
        return self.valid_from <= t < self.valid_until

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "account_ref": self.account_ref,
            "entitlement_ref": self.entitlement_ref,
            "binding_revision": self.binding_revision,
            "machine_id": self.machine_id,
            "local_slot_ref": self.local_slot_ref,
            "valid_from": iso(self.valid_from),
            "valid_until": iso(self.valid_until),
            "label": self.label,
        }


def slot_locator(provider: str, env: Mapping[str, str] | None = None) -> str | None:
    """Locate the login slot this process would use for ``provider`` (a locator, not an identity)."""
    env = os.environ if env is None else env
    parts = [f"{key}={env[key]}" for key in _SLOT_KEYS.get(provider, ("HOME",)) if env.get(key)]
    if not parts:
        return None
    return "slot-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _valid_binding(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    revision, label = entry.get("binding_revision"), entry.get("label")
    start, end = parse_time(entry.get("verified_at")), parse_time(entry.get("valid_until"))
    return (
        _match(_ACCOUNT_REF_RE, entry.get("account_ref"))
        and _match(_PROVIDER_RE, entry.get("provider"))
        and _match(_MACHINE_RE, entry.get("machine_id"))
        and _match(_REF_RE, entry.get("local_slot_ref"))
        and _match(_REF_RE, entry.get("entitlement_ref"))
        and isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision > 0
        and start is not None
        and end is not None
        and start < end  # §5.4: a degenerate window is a format error
        and (label is None or (isinstance(label, str) and 0 < len(label) <= 64))
    )


def import_bindings(document: object) -> dict:
    """Mirror a hub ``GET /v2/quota/bindings`` response taken with this node's token."""
    if not isinstance(document, dict) or document.get("schema") != BINDINGS_SCHEMA:
        raise QuotaV2Error(f"schema 가 {BINDINGS_SCHEMA} 가 아니다")
    machine_id = document.get("machine_id")
    if not _match(_MACHINE_RE, machine_id):
        raise QuotaV2Error("node 토큰으로 받은 응답이 아니다 (machine_id 없음)")
    bindings = document.get("bindings")
    if not isinstance(bindings, list):
        raise QuotaV2Error("bindings 가 목록이 아니다")
    for entry in bindings:
        if not _valid_binding(entry):
            raise QuotaV2Error("binding 형식 오류")
        if entry["machine_id"] != machine_id:
            raise QuotaV2Error("다른 machine 의 binding 이 섞였다")
    mirror = {"schema": MIRROR_SCHEMA, "machine_id": machine_id, "bindings": bindings}
    with _lock():
        _write_json(bindings_path(), mirror)
    return mirror


def load_bindings() -> dict | None:
    data = _read_json(bindings_path())
    if not isinstance(data, dict) or data.get("schema") != MIRROR_SCHEMA:
        return None
    machine_id, bindings = data.get("machine_id"), data.get("bindings")
    if not isinstance(machine_id, str) or not isinstance(bindings, list):
        return None
    usable = [b for b in bindings if _valid_binding(b) and b["machine_id"] == machine_id]
    return {"schema": MIRROR_SCHEMA, "machine_id": machine_id, "bindings": usable}


def resolve_identity(
    provider: str, now: float, *, mirror: dict | None = None, env: Mapping[str, str] | None = None
) -> tuple[Identity | None, str]:
    """Fix the execution identity or say why not.

    Reasons: ``not_enrolled`` · ``slot_unknown`` · ``no_binding`` · ``ambiguous`` ·
    ``not_started`` · ``expired``.
    """
    mirror = load_bindings() if mirror is None else mirror
    if not mirror:
        return None, "not_enrolled"
    slot = slot_locator(provider, env)
    if slot is None:
        return None, "slot_unknown"
    matches = [b for b in mirror["bindings"] if b["provider"] == provider and b["local_slot_ref"] == slot]
    if not matches:
        return None, "no_binding"
    if len(matches) > 1:
        return None, "ambiguous"
    entry = matches[0]
    start, end = parse_time(entry["verified_at"]), parse_time(entry["valid_until"])
    assert start is not None and end is not None
    if now < start.timestamp():
        return None, "not_started"
    if now >= end.timestamp():
        return None, "expired"
    return (
        Identity(
            provider,
            entry["account_ref"],
            entry["entitlement_ref"],
            entry["binding_revision"],
            entry["machine_id"],
            slot,
            start.timestamp(),
            end.timestamp(),
            entry.get("label"),
        ),
        "ok",
    )


def capture_identities(names: Iterable[str], now: float) -> dict[str, Identity]:
    """Identities in force at fetch start, for supported providers only. Never raises."""
    if not enabled():
        return {}
    try:
        mirror = load_bindings()
        if not mirror:
            return {}
        out: dict[str, Identity] = {}
        for name in names:
            if name not in SUPPORT_LIST:
                continue
            identity, _reason = resolve_identity(name, now, mirror=mirror)
            if identity is not None:
                out[name] = identity
        return out
    except Exception:
        return {}


# --------------------------------------------------------------------------- observations


def attempt_of(result: ProviderResult) -> Attempt:
    """The typed attempt the adapter attached, or outcome_missing (§4.1)."""
    attempt = getattr(result, "v2_attempt", None)
    return attempt if isinstance(attempt, Attempt) else Attempt("parse_error", "parse_error:outcome_missing")


def observation_from_attempt(
    attempt: Attempt, identity: Identity, *, measured_at: float, observation_id: str | None = None
) -> dict:
    return {
        "schema": OBSERVATION_SCHEMA,
        "contract_rev": CONTRACT_REV,
        "observation_id": observation_id or f"obs-{uuid.uuid4().hex}",
        "provider": identity.provider,
        "account_ref": identity.account_ref,
        "entitlement_ref": identity.entitlement_ref,
        "source_machine": identity.machine_id,
        "source_slot_ref": identity.local_slot_ref,
        "source_binding_revision": identity.binding_revision,
        "collector_version": COLLECTOR_VERSION,
        "measured_at": iso(measured_at),
        "received_at": None,
        "status": attempt.status,
        "error_ref": attempt.error_ref,
        "unshared_limit_count": attempt.unshared_limit_count,
        "buckets": [dict(b) for b in attempt.buckets],
        "lease_epoch": None,
    }


def _load_store() -> dict:
    data = _read_json(observations_path())
    if (
        not isinstance(data, dict)
        or data.get("schema") != LOCAL_SCHEMA
        or not isinstance(data.get("accounts"), dict)
    ):
        return {"schema": LOCAL_SCHEMA, "accounts": {}}
    return data


def _merge_into(store: dict, observations: Iterable[dict]) -> int:
    added = 0
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    for obs in observations:
        rows = store["accounts"].setdefault(obs["provider"], {}).setdefault(obs["account_ref"], [])
        if any(row == obs for row in rows):
            continue
        rows.append(obs)
        rows.sort(key=lambda row: parse_time(row.get("measured_at")) or epoch)
        del rows[:-MAX_LOCAL_PER_ACCOUNT]
        added += 1
    return added


def append_observations(observations: list[dict]) -> int:
    valid = [obs for obs in observations if valid_stored(obs)]
    if not valid:
        return 0
    with _lock():
        store = _load_store()
        added = _merge_into(store, valid)
        if added:
            _write_json(observations_path(), store)
    return added


def import_observations(document: object) -> int:
    """Store a hub ``GET /v2/quota/observations`` response for an account this node is bound to."""
    if not isinstance(document, dict) or document.get("schema") != OBSERVATIONS_SCHEMA:
        raise QuotaV2Error(f"schema 가 {OBSERVATIONS_SCHEMA} 가 아니다")
    provider, account_ref, rows = (
        document.get("provider"),
        document.get("account_ref"),
        document.get("observations"),
    )
    if not isinstance(provider, str) or not isinstance(account_ref, str) or not isinstance(rows, list):
        raise QuotaV2Error("provider/account_ref/observations 형식 오류")
    mirror = load_bindings()
    if not (
        mirror
        and any(b["provider"] == provider and b["account_ref"] == account_ref for b in mirror["bindings"])
    ):
        raise QuotaV2Error("이 node 에 binding 이 없는 계정이다")
    accepted = [
        obs
        for obs in rows
        if valid_stored(obs)
        and obs["received_at"] is not None
        and obs["provider"] == provider
        and obs["account_ref"] == account_ref
    ]
    return append_observations(accepted)


def account_observations(provider: str, account_ref: str) -> list[dict]:
    rows = (_load_store()["accounts"].get(provider) or {}).get(account_ref) or []
    return [row for row in rows if isinstance(row, dict)]


def export_observations(identity: Identity) -> list[dict]:
    """This slot's own observations not yet received by the hub, in POST form."""
    return [
        obs
        for obs in account_observations(identity.provider, identity.account_ref)
        if obs.get("source_machine") == identity.machine_id
        and obs.get("received_at") is None
        and valid_post(obs)
    ]


def record_attempts(
    attempts: Mapping[str, tuple[ProviderResult, float]],
    *,
    started_at: float,
    identities: Mapping[str, Identity],
) -> int:
    """§5.7: record each real attempt under the binding that was in force for all of it.

    ``attempts`` maps a provider to (result, completion time). ``identities``
    were captured at ``started_at``. A value is recorded only if that same
    binding is still the resolved binding at completion and was valid at both
    ends; ``measured_at`` is the completion time. Never raises.
    """
    if not identities or not enabled():
        return 0
    try:
        mirror = load_bindings()
        observations = []
        for name, (result, completed_at) in attempts.items():
            before = identities.get(name)
            if before is None or result.backoff_until is not None:
                continue
            after, _reason = resolve_identity(name, completed_at, mirror=mirror)
            if (
                not before.same_binding(after)
                or not before.valid_at(started_at)
                or not before.valid_at(completed_at)
            ):
                continue
            observations.append(
                observation_from_attempt(attempt_of(result), before, measured_at=completed_at)
            )
        return append_observations(observations)
    except Exception:
        return 0


def snapshot_for(provider: str, identity: Identity | None) -> dict:
    """The pure evaluate() input for one provider, from the local store."""
    observations = account_observations(identity.provider, identity.account_ref) if identity else []
    return {
        "schema": SNAPSHOT_SCHEMA,
        "provider": provider,
        "identities": {provider: identity.as_dict()} if identity else {},
        "observations": observations,
    }


# --------------------------------------------------------------------------- shadow

# Decision fields: a difference here means the v2 view would admit or refuse
# differently. used_pct is reported separately as a value delta.
DECISION_FIELDS = ("ok", "exit_code", "unmeasurable", "stale_accepted", "role_denied")


def compare(legacy: recommend.GateResult, legacy_exit: int, v2: Evaluation) -> tuple[list[str], float | None]:
    left = {
        "ok": legacy.ok,
        "exit_code": legacy_exit,
        "unmeasurable": legacy.unmeasurable,
        "stale_accepted": legacy.stale_accepted,
        "role_denied": legacy.role_denied,
    }
    right = v2.as_dict()
    diff = [key for key in DECISION_FIELDS if left[key] != right[key]]
    a, b = legacy.used_pct, right["used_pct"]
    delta = (
        round(float(b) - float(a), 6) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
    )
    return diff, delta


def _append_shadow(record: dict) -> None:
    path = shadow_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock():
        try:
            if path.stat().st_size > SHADOW_MAX_BYTES:
                path.replace(path.with_suffix(".jsonl.1"))
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


def shadow_gate(
    *,
    profile: str,
    legacy: recommend.GateResult,
    legacy_exit: int,
    now: dt.datetime,
    pool_classes: Mapping[str, PoolClass] | None = None,
    bench_scores: list | None = None,
    model_prices: Mapping | None = None,
    grade_table: dict | None = None,
    gate_kwargs: Mapping | None = None,
    clock: Clock = UNKNOWN_CLOCK,
) -> dict | None:
    """Evaluate the v2 view and log the comparison. Never decides anything.

    Returns the logged record (for tests), or None when not enrolled/disabled.
    Callers still isolate it (§1.2): an exception here must not reach the gate.
    """
    if not enabled():
        return None
    mirror = load_bindings()
    if not mirror:
        return None
    now = now.replace(tzinfo=dt.UTC) if now.tzinfo is None else now.astimezone(dt.UTC)
    provider, _ = recommend.profile_pool(profile)
    if provider in SUPPORT_LIST:
        identity, reason = resolve_identity(provider, now.timestamp(), mirror=mirror)
    else:
        identity, reason = None, "unsupported"
    v2 = evaluate(
        snapshot_for(provider, identity),
        profile,
        now=now,
        clock=clock,
        pool_classes=pool_classes,
        bench_scores=bench_scores,
        model_prices=model_prices,
        grade_table=grade_table,
        **dict(gate_kwargs or {}),
    )
    diff, delta = compare(legacy, legacy_exit, v2)
    record = {
        "schema": SHADOW_SCHEMA,
        "contract_rev": CONTRACT_REV,
        "at": iso(now.timestamp()),
        "profile": profile,
        "provider": provider,
        "identity_reason": reason,
        "clock": {"skew_bound_s": clock.skew_bound_s, "source": clock.source},
        "legacy": {
            "ok": legacy.ok,
            "exit_code": legacy_exit,
            "unmeasurable": legacy.unmeasurable,
            "stale_accepted": legacy.stale_accepted,
            "role_denied": legacy.role_denied,
            "used_pct": legacy.used_pct,
            "source": legacy.source,
        },
        "v2": v2.as_dict(),
        "diff": diff,
        "used_pct_delta": delta,
        "match": not diff,
    }
    _append_shadow(record)
    return record
