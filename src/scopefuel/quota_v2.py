"""Account-scoped quota v2 — task #578 stage 1 (shadow only).

This module adds a second, account-keyed view of automatic measurements next to
the legacy provider-keyed ``snapshots.json``. It never changes what the legacy
gate decides: the v2 decision is computed after the fact and only written to a
shadow log for comparison.

Contract (advice hk 2558, Q1–Q3):

- ``account_ref`` is an opaque id enrolled by the operator on the hub. It is
  never derived from an email, a path or a token. A node learns its bindings
  from the hub (``GET /v2/quota/bindings``) and mirrors them locally with
  ``scopefuel quota-v2 bindings import``; scopefuel itself never calls the hub.
- A binding names a *login slot* (``local_slot_ref``). Locally the slot is
  located by a hash of the provider's config-dir environment — a locator, not
  an identity. Zero or several matching bindings mean the identity is unknown.
- Only values fetched in the current call are recorded, stamped with the
  measurement time (``measured_at``), and only when the binding captured before
  the fetch is still the same binding afterwards and began before the value was
  measured. Cache hits never become new observations and the legacy cache is
  never migrated into this store.
- ``evaluate(snapshot, profile)`` is pure: it reads only the snapshot it is
  given and reuses :func:`recommend.gate_check` for the quota meaning, so there
  is no second judge.
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
from dataclasses import dataclass, field

from . import __version__, cache, recommend
from .model import Bucket, PoolClass, ProviderResult, Scope, _is_valid_used_pct
from .policy import get_policy

OBSERVATION_SCHEMA = "quota-observation/v2"
OBSERVATIONS_SCHEMA = "quota-observations/v2"  # hub GET /v2/quota/observations
BINDINGS_SCHEMA = "quota-bindings/v2"  # hub GET /v2/quota/bindings
SNAPSHOT_SCHEMA = "quota-snapshot/v2"  # evaluate() input
LOCAL_SCHEMA = "scopefuel.quota-v2-local.v1"
MIRROR_SCHEMA = "scopefuel.quota-v2-bindings.v1"
SHADOW_SCHEMA = "scopefuel.quota-v2-shadow.v1"
COLLECTOR_VERSION = f"scopefuel/{__version__}+quota-v2.1"

STATUSES = frozenset({"success", "partial", "rate_limited", "auth_error", "parse_error", "transport_error"})
MEASURING = frozenset({"success", "partial"})
HORIZONS = frozenset({"now", "week", "month"})
MAX_LOCAL_PER_ACCOUNT = 32
MAX_FUTURE_SKEW_S = 300.0
SHADOW_MAX_BYTES = 1 << 20
DISABLE_ENV = "SCOPEFUEL_QUOTA_V2"

ACCOUNT_REF_RE = re.compile(r"^acct_[0-9a-z]{8,40}$")
REF_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
MACHINE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
PROVIDER_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
WINDOW_RE = re.compile(r"^(?:[a-z0-9][a-z0-9._-]{0,31}|\?)$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/:-]{0,127}$")
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")

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
    return cache.cache_dir() / "quota-v2"


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


# --------------------------------------------------------------------------- time


def _parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC)


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat().replace("+00:00", "Z")


def _as_utc(now: dt.datetime) -> dt.datetime:
    return now.replace(tzinfo=dt.UTC) if now.tzinfo is None else now.astimezone(dt.UTC)


# --------------------------------------------------------------------------- identity


@dataclass(frozen=True)
class Identity:
    provider: str
    account_ref: str
    entitlement_ref: str
    binding_revision: int
    machine_id: str
    local_slot_ref: str
    verified_at: float
    valid_until: float
    label: str | None = None

    def same_binding(self, other: Identity | None) -> bool:
        return (
            other is not None
            and self.account_ref == other.account_ref
            and self.binding_revision == other.binding_revision
            and self.machine_id == other.machine_id
            and self.local_slot_ref == other.local_slot_ref
            and self.entitlement_ref == other.entitlement_ref
        )

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "account_ref": self.account_ref,
            "entitlement_ref": self.entitlement_ref,
            "binding_revision": self.binding_revision,
            "machine_id": self.machine_id,
            "local_slot_ref": self.local_slot_ref,
            "verified_at": _iso(self.verified_at),
            "valid_until": _iso(self.valid_until),
            "label": self.label,
        }


def slot_locator(provider: str, env: Mapping[str, str] | None = None) -> str | None:
    """Locate the login slot this process would use for ``provider``.

    This is a hash of config-dir *paths*: it tells two slots on one machine
    apart, it says nothing about which account is logged in there.
    """
    env = os.environ if env is None else env
    parts = [f"{key}={env[key]}" for key in _SLOT_KEYS.get(provider, ("HOME",)) if env.get(key)]
    if not parts:
        return None
    return "slot-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _valid_binding(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    revision = entry.get("binding_revision")
    label = entry.get("label")
    return (
        isinstance(entry.get("account_ref"), str)
        and bool(ACCOUNT_REF_RE.match(entry["account_ref"]))
        and isinstance(entry.get("provider"), str)
        and bool(PROVIDER_RE.match(entry["provider"]))
        and isinstance(entry.get("machine_id"), str)
        and bool(MACHINE_RE.match(entry["machine_id"]))
        and isinstance(entry.get("local_slot_ref"), str)
        and bool(REF_RE.match(entry["local_slot_ref"]))
        and isinstance(entry.get("entitlement_ref"), str)
        and bool(REF_RE.match(entry["entitlement_ref"]))
        and isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision > 0
        and _parse_time(entry.get("verified_at")) is not None
        and _parse_time(entry.get("valid_until")) is not None
        and (label is None or (isinstance(label, str) and 0 < len(label) <= 64))
    )


def import_bindings(document: object) -> dict:
    """Mirror a hub ``GET /v2/quota/bindings`` response taken with this node's token.

    The response must be a node view (``machine_id`` set) and every binding
    must belong to that machine. The mirror is replaced wholesale, so a binding
    removed or rebound on the hub disappears locally at the next import.
    """
    if not isinstance(document, dict) or document.get("schema") != BINDINGS_SCHEMA:
        raise QuotaV2Error(f"schema 가 {BINDINGS_SCHEMA} 가 아니다")
    machine_id = document.get("machine_id")
    if not isinstance(machine_id, str) or not MACHINE_RE.match(machine_id):
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
    machine_id = data.get("machine_id")
    bindings = data.get("bindings")
    if not isinstance(machine_id, str) or not isinstance(bindings, list):
        return None
    usable = [b for b in bindings if _valid_binding(b) and b["machine_id"] == machine_id]
    return {"schema": MIRROR_SCHEMA, "machine_id": machine_id, "bindings": usable}


def resolve_identity(
    provider: str,
    now: float,
    *,
    mirror: dict | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Identity | None, str]:
    """Step 1 of the read order: fix the execution identity or say why not.

    Returns ``(identity, "ok")`` or ``(None, reason)`` with reason one of
    ``not_enrolled`` · ``slot_unknown`` · ``no_binding`` · ``ambiguous`` · ``expired``.
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
    verified = _parse_time(entry["verified_at"])
    valid_until = _parse_time(entry["valid_until"])
    assert verified is not None and valid_until is not None
    if now >= valid_until.timestamp():
        return None, "expired"
    return (
        Identity(
            provider=provider,
            account_ref=entry["account_ref"],
            entitlement_ref=entry["entitlement_ref"],
            binding_revision=entry["binding_revision"],
            machine_id=entry["machine_id"],
            local_slot_ref=slot,
            verified_at=verified.timestamp(),
            valid_until=valid_until.timestamp(),
            label=entry.get("label"),
        ),
        "ok",
    )


def capture_identities(names: Iterable[str], now: float) -> dict[str, Identity]:
    """Identities in force *before* a fetch. Never raises; empty when not enrolled."""
    if not enabled():
        return {}
    try:
        mirror = load_bindings()
        if not mirror:
            return {}
        out: dict[str, Identity] = {}
        for name in names:
            identity, _reason = resolve_identity(name, now, mirror=mirror)
            if identity is not None:
                out[name] = identity
        return out
    except Exception:
        return {}


# --------------------------------------------------------------------------- envelope


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-.")
    return slug or "x"


def _normalize_reset(value: str | None) -> str | None:
    parsed = _parse_time(value)
    return None if parsed is None else parsed.isoformat().replace("+00:00", "Z")


def buckets_to_v2(buckets: list[Bucket]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for bucket in buckets:
        name = bucket.scope.name if bucket.scope.kind != "account" else None
        base = f"{bucket.scope.kind}:{_slug(name) if name else '-'}:{_slug(bucket.window)}"
        limit_id, suffix = base, 2
        while limit_id in seen:
            limit_id, suffix = f"{base}-{suffix}", suffix + 1
        seen.add(limit_id)
        reset = _normalize_reset(bucket.resets_at)
        window = bucket.window if WINDOW_RE.match(bucket.window or "") else "?"
        out.append(
            {
                "limit_id": limit_id,
                "label": bucket.label[:128] if bucket.label else None,
                "scope": {"kind": bucket.scope.kind, "ref": name},
                "horizon": bucket.horizon if bucket.horizon in HORIZONS else "week",
                "window": window,
                "window_instance": reset or "unknown",
                "used_pct": float(bucket.used_pct) if _is_valid_used_pct(bucket.used_pct) else None,
                "reset_at": reset,
                "observed_at": None,
            }
        )
    return out


_RATE_TEXT = re.compile(r"(?<!\d)429(?!\d)|rate[ _-]?limit|too many requests", re.I)


def status_for(result: ProviderResult) -> str:
    """Six-way v2 status of one automatic attempt."""
    if result.error is None and not result.stale:
        if not any(_is_valid_used_pct(b.used_pct) for b in result.buckets):
            return "parse_error"
        return "partial" if result.warning else "success"
    kind = result.error_kind
    text = result.last_error or result.error or ""
    if kind == "rate_limited":
        return "rate_limited"
    if kind in ("auth", "credentials"):
        return "auth_error"
    if kind in ("server", "network", "transport"):
        return "rate_limited" if _RATE_TEXT.search(text) else "transport_error"
    if kind == "http" and result.http_status in (401, 403):
        return "auth_error"
    if kind is None:
        from . import manual

        classified, _ = manual.classify_automatic_failure(result)
        if classified == "auth":
            return "auth_error"
        if classified == "transport":
            return "rate_limited" if _RATE_TEXT.search(text) else "transport_error"
    # http 4xx, unknown and unclassified failures are never stale-acceptable.
    return "parse_error"


def observation_from_result(
    result: ProviderResult,
    identity: Identity,
    *,
    measured_at: float,
    observation_id: str | None = None,
) -> dict:
    """Build a v2 envelope. ``measured_at`` is the fetch time, never render time."""
    status = status_for(result)
    error_ref = None
    if status not in MEASURING:
        error_ref = status + (f":http_{result.http_status}" if isinstance(result.http_status, int) else "")
    return {
        "schema": OBSERVATION_SCHEMA,
        "observation_id": observation_id or f"obs-{uuid.uuid4().hex}",
        "provider": identity.provider,
        "account_ref": identity.account_ref,
        "entitlement_ref": identity.entitlement_ref,
        "source_machine": identity.machine_id,
        "source_binding_revision": identity.binding_revision,
        "collector_version": COLLECTOR_VERSION,
        "measured_at": _iso(measured_at),
        "received_at": None,
        "status": status,
        "buckets": buckets_to_v2(result.buckets) if status in MEASURING else [],
        "error_ref": error_ref,
        "lease_epoch": None,
    }


def _valid_bucket(bucket: object) -> bool:
    if not isinstance(bucket, dict):
        return False
    scope = bucket.get("scope")
    if not isinstance(scope, dict) or scope.get("kind") not in ("account", "model", "group"):
        return False
    ref = scope.get("ref")
    if (scope["kind"] == "account") != (ref is None):
        return False
    if ref is not None and (not isinstance(ref, str) or not ref):
        return False
    used = bucket.get("used_pct")
    if used is not None and not _is_valid_used_pct(used):
        return False
    label = bucket.get("label")
    return (
        isinstance(bucket.get("limit_id"), str)
        and bool(REF_RE.match(bucket["limit_id"]))
        and bucket.get("horizon") in HORIZONS
        and isinstance(bucket.get("window"), str)
        and bool(WINDOW_RE.match(bucket["window"]))
        and isinstance(bucket.get("window_instance"), str)
        and bool(INSTANCE_RE.match(bucket["window_instance"]))
        and (bucket.get("reset_at") is None or _parse_time(bucket.get("reset_at")) is not None)
        and (bucket.get("observed_at") is None or _parse_time(bucket.get("observed_at")) is not None)
        and (label is None or isinstance(label, str))
    )


def validate_observation(obs: object) -> bool:
    """Shape check mirroring the hub's validateQuotaV2Envelope."""
    if not isinstance(obs, dict) or obs.get("schema") != OBSERVATION_SCHEMA:
        return False
    revision = obs.get("source_binding_revision")
    buckets = obs.get("buckets")
    status = obs.get("status")
    if not (
        isinstance(obs.get("observation_id"), str)
        and REF_RE.match(obs["observation_id"])
        and isinstance(obs.get("provider"), str)
        and PROVIDER_RE.match(obs["provider"])
        and isinstance(obs.get("account_ref"), str)
        and ACCOUNT_REF_RE.match(obs["account_ref"])
        and isinstance(obs.get("entitlement_ref"), str)
        and REF_RE.match(obs["entitlement_ref"])
        and isinstance(obs.get("source_machine"), str)
        and MACHINE_RE.match(obs["source_machine"])
        and isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision > 0
        and isinstance(obs.get("collector_version"), str)
        and VERSION_RE.match(obs["collector_version"])
        and _parse_time(obs.get("measured_at")) is not None
        and status in STATUSES
        and isinstance(buckets, list)
    ):
        return False
    if obs.get("received_at") is not None and _parse_time(obs.get("received_at")) is None:
        return False
    if (status in MEASURING) != bool(buckets):
        return False
    if not all(_valid_bucket(b) for b in buckets):
        return False
    limit_ids = [b["limit_id"] for b in buckets]
    return len(limit_ids) == len(set(limit_ids))


# --------------------------------------------------------------------------- local store


_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


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
    accounts = store["accounts"]
    for obs in observations:
        by_provider = accounts.setdefault(obs["provider"], {})
        rows = by_provider.setdefault(obs["account_ref"], [])
        if any(isinstance(row, dict) and row.get("observation_id") == obs["observation_id"] for row in rows):
            continue
        rows.append(obs)
        rows.sort(
            key=lambda row: _parse_time(row.get("measured_at")) or dt.datetime.min.replace(tzinfo=dt.UTC)
        )
        del rows[:-MAX_LOCAL_PER_ACCOUNT]
        added += 1
    return added


def append_observations(observations: list[dict]) -> int:
    valid = [obs for obs in observations if validate_observation(obs)]
    if not valid:
        return 0
    with _lock():
        store = _load_store()
        added = _merge_into(store, valid)
        if added:
            _write_json(observations_path(), store)
    return added


def import_observations(document: object) -> int:
    """Store a hub ``GET /v2/quota/observations`` response for a bound account.

    The response is filtered again locally: only observations whose provider
    and account_ref equal the response header *and* a current local binding
    are kept, so a wrong file cannot plant another account's values.
    """
    if not isinstance(document, dict) or document.get("schema") != OBSERVATIONS_SCHEMA:
        raise QuotaV2Error(f"schema 가 {OBSERVATIONS_SCHEMA} 가 아니다")
    provider, account_ref = document.get("provider"), document.get("account_ref")
    rows = document.get("observations")
    if not isinstance(provider, str) or not isinstance(account_ref, str) or not isinstance(rows, list):
        raise QuotaV2Error("provider/account_ref/observations 형식 오류")
    mirror = load_bindings()
    bound = bool(mirror) and any(
        b["provider"] == provider and b["account_ref"] == account_ref for b in mirror["bindings"]
    )
    if not bound:
        raise QuotaV2Error("이 node 에 binding 이 없는 계정이다")
    accepted = [
        obs
        for obs in rows
        if validate_observation(obs)
        and obs["provider"] == provider
        and obs["account_ref"] == account_ref
        and obs.get("received_at") is not None
    ]
    return append_observations(accepted)


def account_observations(provider: str, account_ref: str) -> list[dict]:
    store = _load_store()
    rows = (store["accounts"].get(provider) or {}).get(account_ref) or []
    return [row for row in rows if validate_observation(row)]


def record_fetch(
    successes: Mapping[str, ProviderResult],
    failures: Mapping[str, ProviderResult],
    *,
    measured_at: float,
    identities: Mapping[str, Identity],
) -> int:
    """Record this call's automatic attempts under the account that made them.

    ``identities`` must have been captured before the fetch. The binding is
    re-resolved now; if it changed in between, or began after the value was
    measured, nothing is recorded — a value measured under account A never
    becomes account B's observation. Never raises.
    """
    if not identities or not enabled():
        return 0
    try:
        mirror = load_bindings()
        observations: list[dict] = []
        for name, result in [*successes.items(), *failures.items()]:
            before = identities.get(name)
            if before is None:
                continue
            after, _reason = resolve_identity(name, measured_at, mirror=mirror)
            if not before.same_binding(after) or measured_at < before.verified_at:
                continue
            if result.backoff_until is not None:
                continue  # no attempt was made inside a backoff window
            observations.append(observation_from_result(result, before, measured_at=measured_at))
        return append_observations(observations)
    except Exception:
        return 0


def snapshot_for(
    identities: Mapping[str, Identity | None],
    *,
    reasons: Mapping[str, str] | None = None,
) -> dict:
    """Assemble the pure evaluate() input from the local account-scoped store."""
    observations: list[dict] = []
    for identity in identities.values():
        if identity is not None:
            observations.extend(account_observations(identity.provider, identity.account_ref))
    return {
        "schema": SNAPSHOT_SCHEMA,
        "identities": {name: (i.as_dict() if i is not None else None) for name, i in identities.items()},
        "identity_reasons": dict(reasons or {}),
        "observations": observations,
    }


# --------------------------------------------------------------------------- evaluate

_FAILURE_KIND = {
    "rate_limited": "rate_limited",
    "transport_error": "network",
    "auth_error": "auth",
    "parse_error": "parse",
}


@dataclass(frozen=True)
class Evaluation:
    gate: recommend.GateResult
    exit_code: int
    code: str
    account_ref: str | None
    binding_revision: int | None
    observation_ids: tuple[str, ...] = ()
    measured_at: str | None = None
    age_s: float | None = None
    freshness: str | None = None  # fresh | stale | None
    excluded: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "ok": self.gate.ok,
            "exit_code": self.exit_code,
            "unmeasurable": self.gate.unmeasurable,
            "stale_accepted": self.gate.stale_accepted,
            "role_denied": self.gate.role_denied,
            "used_pct": self.gate.used_pct,
            "account_ref": self.account_ref,
            "binding_revision": self.binding_revision,
            "observation_ids": list(self.observation_ids),
            "measured_at": self.measured_at,
            "age_s": None if self.age_s is None else round(self.age_s, 1),
            "freshness": self.freshness,
            "excluded": self.excluded,
            "reason": self.gate.reason,
        }


@dataclass
class _Selection:
    result: ProviderResult | None
    code: str
    identity: dict | None = None
    observation_ids: tuple[str, ...] = ()
    measured_at: str | None = None
    age_s: float | None = None
    freshness: str | None = None
    excluded: dict = field(default_factory=dict)


def _snapshot_identity(raw: object, provider: str, now: dt.datetime) -> dict | None:
    if not isinstance(raw, dict) or raw.get("provider") != provider or not _valid_binding(raw):
        return None
    valid_until = _parse_time(raw["valid_until"])
    if valid_until is None or valid_until <= now:
        return None
    return raw


def _buckets_from_v2(rows: list[dict]) -> list[Bucket]:
    return [
        Bucket(
            label=row.get("label") or row["limit_id"],
            window=row["window"],
            used_pct=row.get("used_pct"),
            resets_at=row.get("reset_at"),
            scope=Scope(row["scope"]["kind"], row["scope"].get("ref")),
            horizon=row["horizon"],
        )
        for row in rows
    ]


def _values(obs: dict) -> list:
    return sorted((b["limit_id"], b.get("used_pct"), b.get("reset_at")) for b in obs["buckets"])


def _unusable(provider: str, code: str, error_kind: str | None, pool_class: PoolClass) -> ProviderResult:
    return ProviderResult(id=provider, error=f"quota-v2 {code}", error_kind=error_kind, pool_class=pool_class)


def _select(
    provider: str,
    identity_raw: object,
    observations: list,
    now: dt.datetime,
    pool_class: PoolClass,
) -> _Selection:
    identity = _snapshot_identity(identity_raw, provider, now)
    if identity is None:
        return _Selection(None, "IDENTITY_UNKNOWN")
    excluded = {"invalid": 0, "other_account": 0, "future": 0}
    candidates: dict[str, dict] = {}
    for obs in observations:
        if not isinstance(obs, dict) or obs.get("provider") != provider:
            continue
        if not validate_observation(obs):
            excluded["invalid"] += 1
            continue
        if (
            obs["account_ref"] != identity["account_ref"]
            or obs["entitlement_ref"] != identity["entitlement_ref"]
        ):
            excluded["other_account"] += 1
            continue
        measured = _parse_time(obs["measured_at"])
        assert measured is not None
        if (measured - now).total_seconds() > MAX_FUTURE_SKEW_S:
            excluded["future"] += 1
            continue
        candidates.setdefault(obs["observation_id"], obs)
    rows = sorted(candidates.values(), key=lambda o: _parse_time(o["measured_at"]))  # type: ignore[arg-type,return-value]
    successes = [o for o in rows if o["status"] == "success"]
    base = {"identity": identity, "excluded": excluded}

    def failure_after(point: dt.datetime | None, *, own_only: bool) -> dict | None:
        found = None
        for obs in rows:
            if obs["status"] in MEASURING:
                continue
            at = _parse_time(obs["measured_at"])
            if point is not None and at is not None and at <= point:
                continue
            if own_only and (
                obs["source_machine"] != identity["machine_id"]
                or obs["source_binding_revision"] != identity["binding_revision"]
            ):
                continue
            found = obs
        return found

    if not successes:
        failure = failure_after(None, own_only=False)
        own = failure_after(None, own_only=True)
        code = "AUTH_BLOCKED" if own is not None and own["status"] == "auth_error" else "NO_SAMPLE"
        error_kind = _FAILURE_KIND.get(failure["status"]) if failure else None
        return _Selection(
            ProviderResult(
                id=provider, error=f"quota-v2 {code}", error_kind=error_kind, pool_class=pool_class
            ),
            code,
            **base,
        )
    latest = successes[-1]
    latest_at = _parse_time(latest["measured_at"])
    assert latest_at is not None
    twins = [o for o in successes if _parse_time(o["measured_at"]) == latest_at]
    if any(_values(o) != _values(latest) for o in twins):
        # Same instant, different values, no way to order them: never pick
        # the friendlier one.
        return _Selection(
            _unusable(provider, "CONFLICT", "parse", pool_class),
            "CONFLICT",
            observation_ids=tuple(o["observation_id"] for o in twins),
            **base,
        )
    # The execution slot's own auth failure is never covered by a value
    # measured elsewhere; another node's expired credential does not block
    # this one.
    own_auth = failure_after(latest_at, own_only=True)
    if own_auth is not None and own_auth["status"] == "auth_error":
        return _Selection(
            ProviderResult(
                id=provider, error="quota-v2 AUTH_BLOCKED", error_kind="auth", pool_class=pool_class
            ),
            "AUTH_BLOCKED",
            observation_ids=(own_auth["observation_id"],),
            **base,
        )
    age = (now - latest_at).total_seconds()
    ttl = cache.PROVIDER_TTL_S.get(provider, cache.DEFAULT_TTL_S)
    result = ProviderResult(
        id=provider,
        buckets=_buckets_from_v2(latest["buckets"]),
        source="quota-v2",
        fetched_at=latest_at.timestamp(),
        age_s=age,
        pool_class=pool_class,
    )
    ids = (latest["observation_id"],)
    if age <= ttl:
        return _Selection(
            result,
            "FRESH",
            observation_ids=ids,
            measured_at=latest["measured_at"],
            age_s=age,
            freshness="fresh",
            **base,
        )
    failure = failure_after(latest_at, own_only=False)
    if failure is None or failure["status"] not in ("rate_limited", "transport_error"):
        # Old value and no evidence the measurement itself is failing: stage 1
        # has no refresh intent, so this is not admission evidence.
        return _Selection(
            ProviderResult(
                id=provider, error="quota-v2 STALE_EXPIRED", error_kind="parse", pool_class=pool_class
            ),
            "STALE_EXPIRED",
            observation_ids=ids,
            measured_at=latest["measured_at"],
            age_s=age,
            freshness="stale",
            **base,
        )
    result.stale = True
    result.error_kind = _FAILURE_KIND[failure["status"]]
    result.last_error = failure.get("error_ref") or failure["status"]
    # Identity is proven by the account binding, not by a credential hash.
    result.account_fp_match = True
    return _Selection(
        result,
        "STALE",
        observation_ids=(*ids, failure["observation_id"]),
        measured_at=latest["measured_at"],
        age_s=age,
        freshness="stale",
        **base,
    )


def evaluate(
    snapshot: Mapping,
    profile: str,
    *,
    now: dt.datetime,
    pool_classes: Mapping[str, PoolClass] | None = None,
    bench_scores: list | None = None,
    model_prices: Mapping | None = None,
    grade_table: dict | None = None,
    operator_request: str | None = None,
    requested_by: str | None = None,
    purpose: str | None = None,
) -> Evaluation:
    """Gate decision from a v2 snapshot alone — no network, no cache, no fetch.

    Providers present in ``snapshot["identities"]`` get one selected
    observation each; everything else is absent (unmeasurable), exactly as a
    provider with no data is for the legacy gate.
    """
    if not isinstance(snapshot, Mapping) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise QuotaV2Error(f"schema 가 {SNAPSHOT_SCHEMA} 가 아니다")
    now = _as_utc(now)
    identities = snapshot.get("identities") or {}
    observations = snapshot.get("observations") or []
    if not isinstance(identities, Mapping) or not isinstance(observations, list):
        raise QuotaV2Error("identities/observations 형식 오류")
    provider_id, _group = recommend.profile_pool(profile)
    selections: dict[str, _Selection] = {}
    for provider in sorted({*identities, provider_id} - {""}):
        explicit = (pool_classes or {}).get(provider)
        pool_class = explicit if explicit is not None else get_policy(provider, "preserve")[0]
        selections[provider] = _select(provider, identities.get(provider), observations, now, pool_class)
    results = [s.result for s in selections.values() if s.result is not None]
    gate = recommend.gate_check(
        results,
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
    exit_code = 0 if gate.ok else (5 if gate.role_denied else (4 if gate.unmeasurable else 3))
    chosen = selections.get(provider_id) or _Selection(None, "IDENTITY_UNKNOWN")
    if gate.role_denied:
        code = "ROLE_DENIED"
    elif gate.ok:
        code = "STALE_ACCEPTED" if gate.stale_accepted else "OK"
    elif gate.unmeasurable:
        code = chosen.code if chosen.code not in ("FRESH", "STALE") else "UNMEASURABLE"
    else:
        code = "QUOTA_DENIED"
    identity = chosen.identity or {}
    return Evaluation(
        gate=gate,
        exit_code=exit_code,
        code=code,
        account_ref=identity.get("account_ref"),
        binding_revision=identity.get("binding_revision"),
        observation_ids=chosen.observation_ids,
        measured_at=chosen.measured_at,
        age_s=chosen.age_s,
        freshness=chosen.freshness,
        excluded=chosen.excluded,
    )


# --------------------------------------------------------------------------- shadow

# Decision fields: a difference here means the v2 view would admit or refuse
# differently. used_pct is reported separately as a value delta, because a
# newer measurement from another node of the same account legitimately moves
# the number without changing the decision.
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
    delta = None
    a, b = legacy.used_pct, v2.gate.used_pct
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        delta = round(float(b) - float(a), 6)
    elif a is not None or b is not None:
        diff.append("used_pct")
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
    names: Iterable[str],
    pool_classes: Mapping[str, PoolClass] | None = None,
    bench_scores: list | None = None,
    model_prices: Mapping | None = None,
    grade_table: dict | None = None,
    gate_kwargs: Mapping | None = None,
) -> dict | None:
    """Evaluate the v2 view and log the comparison. Never raises, never decides.

    Returns the logged record (for tests) or None when not enrolled/disabled.
    """
    if not enabled():
        return None
    try:
        mirror = load_bindings()
        if not mirror:
            return None
        epoch = _as_utc(now).timestamp()
        identities: dict[str, Identity | None] = {}
        reasons: dict[str, str] = {}
        for name in names:
            identity, reason = resolve_identity(name, epoch, mirror=mirror)
            identities[name] = identity
            reasons[name] = reason
        snapshot = snapshot_for(identities, reasons=reasons)
        v2 = evaluate(
            snapshot,
            profile,
            now=now,
            pool_classes=pool_classes,
            bench_scores=bench_scores,
            model_prices=model_prices,
            grade_table=grade_table,
            **dict(gate_kwargs or {}),
        )
        provider_id, _ = recommend.profile_pool(profile)
        diff, delta = compare(legacy, legacy_exit, v2)
        record = {
            "schema": SHADOW_SCHEMA,
            "at": _as_utc(now).isoformat().replace("+00:00", "Z"),
            "profile": profile,
            "provider": provider_id,
            "identity_reason": reasons.get(provider_id, "not_requested"),
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
    except Exception:
        return None
