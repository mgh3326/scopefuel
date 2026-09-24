"""quota v2 contract r3 (`quota-v2.r3`) — definitions, wire validation, claude adapter.

Contract: hk `review/2026-09-24/578-contract-r3.1`. This module holds what the
contract fixes as data and form: the limit definition tables (§3), the wire
envelope in POST and stored form (§2), and the claude adapter's typed attempt
outcome (§4). The evaluator (§6) lives in `quota_v2_eval`.

Nothing here reads warning/last_error/error/hint text to decide a status
(§4.1): the claude adapter decides from the HTTP status, the classified
transport exception, and the parsed body only.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import socket
import urllib.error
from collections.abc import Mapping
from dataclasses import dataclass, field

CONTRACT_REV = "quota-v2.r3"
OBSERVATION_SCHEMA = "quota-observation/v2"
STATUSES = frozenset({"success", "partial", "rate_limited", "auth_error", "parse_error", "transport_error"})
MEASURING = frozenset({"success", "partial"})
HORIZONS = frozenset({"now", "week", "month"})
ERROR_REFS = frozenset(
    {
        "rate_limited:http_429",
        "auth_error:http_401",
        "auth_error:http_403",
        "transport_error:http_5xx",
        "transport_error:network",
        "transport_error:timeout",
        "parse_error:http_4xx",
        "parse_error:schema",
        "parse_error:required_missing",
        "parse_error:duplicate_limit",
        "parse_error:outcome_missing",
        "parse_error:http_unexpected",
        "partial:required_missing",
    }
)
STALE_MAX_S = 21600.0  # #576 acceptance bound and the §6 D window

_ID_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
_PROVIDER_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_ACCOUNT_RE = re.compile(r"acct_[0-9a-z]{8,40}")
_MACHINE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/:-]{0,127}")
_WINDOW_RE = re.compile(r"(?:[a-z0-9][a-z0-9._-]{0,31}|\?)")
_INSTANCE_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_RFC3339_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)")

ENVELOPE_FIELDS = (
    "schema",
    "contract_rev",
    "observation_id",
    "provider",
    "account_ref",
    "entitlement_ref",
    "source_machine",
    "source_slot_ref",
    "source_binding_revision",
    "collector_version",
    "measured_at",
    "received_at",
    "status",
    "error_ref",
    "unshared_limit_count",
    "buckets",
    "lease_epoch",
)
BUCKET_FIELDS = (
    "limit_id",
    "label",
    "scope",
    "horizon",
    "window",
    "window_instance",
    "used_pct",
    "reset_at",
    "observed_at",
)


# --------------------------------------------------------------------------- §3 definitions


@dataclass(frozen=True)
class LimitDef:
    limit_id: str
    kind: str
    ref: str | None
    window: str
    horizon: str
    window_length_s: float
    instance_rule: str  # "none" | "fixed"
    required: bool
    label: str | None = None


@dataclass(frozen=True)
class ProviderContract:
    provider: str
    ttl_s: float
    limits: Mapping[str, LimitDef]
    has_gate: bool = True

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(limit_id for limit_id, d in self.limits.items() if d.required)


CLAUDE = ProviderContract(
    provider="claude",
    ttl_s=180.0,
    limits={
        "claude.five_hour": LimitDef(
            "claude.five_hour", "account", None, "5h", "now", 18000.0, "none", True, "5h"
        ),
        "claude.seven_day": LimitDef(
            "claude.seven_day", "account", None, "7d", "week", 604800.0, "none", True, "7d all"
        ),
    },
)
DEFINITIONS: Mapping[str, ProviderContract] = {"claude": CLAUDE}
SUPPORT_LIST: tuple[str, ...] = ("claude",)  # §1.1 production value


# --------------------------------------------------------------------------- time


def parse_time(value: object) -> dt.datetime | None:
    """RFC3339 with an explicit offset → aware UTC datetime; anything else → None."""
    if not isinstance(value, str) or not value.isascii() or not _RFC3339_RE.fullmatch(value):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(dt.UTC) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):  # out of range, or year 1/9999 pushed out by the offset
        return None


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- §2 wire


def _is_control(char: str) -> bool:
    code = ord(char)
    return code <= 0x1F or code == 0x7F or 0x80 <= code <= 0x9F


def _text_ok(value: object) -> bool:
    """UTF-8 1..128 bytes, no C0/DEL/C1 control characters (§2)."""
    if not isinstance(value, str) or not value:
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate (JSON "\ud800") is not UTF-8: reject, never repair
        return False
    if len(encoded) > 128:
        return False
    return not any(_is_control(c) for c in value)


def _ascii_match(pattern: re.Pattern, value: object) -> bool:
    return isinstance(value, str) and value.isascii() and pattern.fullmatch(value) is not None


_INT_MAX = 2**63 - 1  # the Go hub decodes these into int64; both sides share the bound


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value <= _INT_MAX


def _member(value: object, allowed: frozenset[str]) -> bool:
    """Set membership that is False (not TypeError) for a list/dict value."""
    return isinstance(value, str) and value in allowed


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def identity_fields_ok(identity: Mapping) -> bool:
    """§6 I: the execution identity's own fields have the §2 shapes of the fields they must match."""
    return (
        _ascii_match(_PROVIDER_RE, identity.get("provider"))
        and _ascii_match(_ACCOUNT_RE, identity.get("account_ref"))
        and _ascii_match(_ID_RE, identity.get("entitlement_ref"))
        and _ascii_match(_MACHINE_RE, identity.get("machine_id"))
        and _ascii_match(_ID_RE, identity.get("local_slot_ref"))
        and _is_int(identity.get("binding_revision"))
        and identity["binding_revision"] >= 1
    )


def bucket_errors(bucket: object) -> list[str]:
    if not isinstance(bucket, dict) or set(bucket) != set(BUCKET_FIELDS):
        return ["bucket fields"]
    errors = []
    if not _ascii_match(_ID_RE, bucket["limit_id"]):
        errors.append("limit_id")
    if bucket["label"] is not None and not _text_ok(bucket["label"]):
        errors.append("label")
    scope = bucket["scope"]
    if (
        not isinstance(scope, dict)
        or set(scope) != {"kind", "ref"}
        or scope["kind"] not in ("account", "model", "group")
    ):
        errors.append("scope")
    elif (scope["kind"] == "account") != (scope["ref"] is None) or (
        scope["ref"] is not None and not _text_ok(scope["ref"])
    ):
        errors.append("scope.ref")
    if not _member(bucket["horizon"], HORIZONS):
        errors.append("horizon")
    if not _ascii_match(_WINDOW_RE, bucket["window"]):
        errors.append("window")
    instance = bucket["window_instance"]
    if instance != "unknown" and (not _ascii_match(_INSTANCE_RE, instance) or parse_time(instance) is None):
        errors.append("window_instance")
    used = bucket["used_pct"]
    if used is not None and not (_is_number(used) and 0 <= used <= 100):
        errors.append("used_pct")
    if bucket["reset_at"] is not None and parse_time(bucket["reset_at"]) is None:
        errors.append("reset_at")
    if bucket["observed_at"] is not None and parse_time(bucket["observed_at"]) is None:
        errors.append("observed_at")
    return errors


def envelope_errors(obs: object, form: str) -> list[str]:
    """§2 violations for the POST form or the stored form (empty list = valid)."""
    if not isinstance(obs, dict) or set(obs) != set(ENVELOPE_FIELDS):
        return ["envelope fields"]
    errors = []
    if obs["schema"] != OBSERVATION_SCHEMA:
        errors.append("schema")
    if obs["contract_rev"] != CONTRACT_REV:
        errors.append("contract_rev")
    for key, pattern in (
        ("observation_id", _ID_RE),
        ("provider", _PROVIDER_RE),
        ("account_ref", _ACCOUNT_RE),
        ("entitlement_ref", _ID_RE),
        ("source_machine", _MACHINE_RE),
        ("source_slot_ref", _ID_RE),
        ("collector_version", _VERSION_RE),
    ):
        if not _ascii_match(pattern, obs[key]):
            errors.append(key)
    if not _is_int(obs["source_binding_revision"]) or obs["source_binding_revision"] < 1:
        errors.append("source_binding_revision")
    measured = parse_time(obs["measured_at"])
    if measured is None:
        errors.append("measured_at")
    if form == "post":
        if obs["received_at"] is not None:
            errors.append("received_at (POST)")
    elif obs["received_at"] is not None and parse_time(obs["received_at"]) is None:
        errors.append("received_at")
    status = obs["status"] if _member(obs["status"], STATUSES) else None
    if status is None:
        errors.append("status")
    ref = obs["error_ref"]
    if status == "success":
        if ref is not None:
            errors.append("error_ref (success)")
    elif not _member(ref, ERROR_REFS) or ref.split(":", 1)[0] != status:
        errors.append("error_ref")
    if not _is_int(obs["unshared_limit_count"]) or obs["unshared_limit_count"] < 0:
        errors.append("unshared_limit_count")
    if obs["lease_epoch"] is not None:
        errors.append("lease_epoch")
    buckets = obs["buckets"]
    if not isinstance(buckets, list) or (status in MEASURING) != bool(buckets):
        errors.append("buckets")
        return errors
    ids = []
    for bucket in buckets:
        problems = bucket_errors(bucket)
        errors.extend(f"bucket.{p}" for p in problems)
        if not problems:
            ids.append(bucket["limit_id"])
            observed = parse_time(bucket["observed_at"]) if bucket["observed_at"] is not None else None
            if observed is not None and measured is not None and observed > measured:
                errors.append("bucket.observed_at > measured_at")
    if len(ids) != len(set(ids)):
        errors.append("duplicate limit_id")
    return errors


def valid_post(obs: object) -> bool:
    return not envelope_errors(obs, "post")


def valid_stored(obs: object) -> bool:
    return not envelope_errors(obs, "stored")


# --------------------------------------------------------------------------- §4 claude adapter


@dataclass
class Attempt:
    """Typed outcome of one claude usage attempt (§4.1)."""

    status: str
    error_ref: str | None
    buckets: list[dict] = field(default_factory=list)
    unshared_limit_count: int = 0


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (TimeoutError, socket.timeout))
    return False


def _is_network(exc: BaseException) -> bool:
    return isinstance(exc, (urllib.error.URLError, ConnectionError, OSError)) and not _is_timeout(exc)


def _http_ref(status: int) -> tuple[str, str]:
    if status == 401:
        return "auth_error", "auth_error:http_401"
    if status == 403:
        return "auth_error", "auth_error:http_403"
    if status == 429:
        return "rate_limited", "rate_limited:http_429"
    if 500 <= status <= 599:
        return "transport_error", "transport_error:http_5xx"
    if 400 <= status <= 499:
        return "parse_error", "parse_error:http_4xx"
    return "parse_error", "parse_error:http_unexpected"


class _Schema(Exception):
    pass


def _claude_limit(section: object, definition: LimitDef) -> dict | None:
    """One required claude limit, or None if missing/invalid. Wrong types → _Schema."""
    if section is None:
        return None
    if not isinstance(section, dict):
        raise _Schema
    used, reset = section.get("utilization"), section.get("resets_at")
    if used is not None and (isinstance(used, bool) or not isinstance(used, (int, float))):
        raise _Schema
    if reset is not None and not isinstance(reset, str):
        raise _Schema
    if (
        used is None
        or reset is None
        or not math.isfinite(used)
        or not 0 <= used <= 100
        or parse_time(reset) is None
    ):
        return None
    return {
        "limit_id": definition.limit_id,
        "label": definition.label,
        "scope": {"kind": definition.kind, "ref": definition.ref},
        "horizon": definition.horizon,
        "window": definition.window,
        "window_instance": "unknown",
        "used_pct": float(used),
        "reset_at": reset,
        "observed_at": None,
    }


def claude_attempt(
    *, http_status: int | None, body: object = None, exc: BaseException | None = None
) -> Attempt:
    """Typed attempt outcome from the transport result only (§3.2, §4.1)."""
    if http_status is not None and http_status != 200:
        status, ref = _http_ref(http_status)
        return Attempt(status, ref)
    if http_status is None:
        if exc is None:
            return Attempt("parse_error", "parse_error:outcome_missing")
        if _is_timeout(exc):
            return Attempt("transport_error", "transport_error:timeout")
        if _is_network(exc):
            return Attempt("transport_error", "transport_error:network")
        return Attempt("parse_error", "parse_error:outcome_missing")
    # HTTP 200
    if exc is not None or not isinstance(body, dict):
        return Attempt("parse_error", "parse_error:schema")
    limits = body.get("limits")
    unshared = (
        sum(1 for item in limits if isinstance(item, dict) and item.get("kind") == "weekly_scoped")
        if isinstance(limits, list)
        else 0
    )
    try:
        buckets = [
            b
            for b in (
                _claude_limit(body.get("five_hour"), CLAUDE.limits["claude.five_hour"]),
                _claude_limit(body.get("seven_day"), CLAUDE.limits["claude.seven_day"]),
            )
            if b is not None
        ]
    except _Schema:
        return Attempt("parse_error", "parse_error:schema", unshared_limit_count=unshared)
    if len(buckets) == len(CLAUDE.required):
        return Attempt("success", None, buckets, unshared)
    if buckets:
        return Attempt("partial", "partial:required_missing", buckets, unshared)
    return Attempt("parse_error", "parse_error:required_missing", [], unshared)
