"""Host-local observations of Devin's free short-window limit.

The observer never probes Devin. It stores only the limit reported in an
already-read worker pane. The ordinary quota-share channel carries provider
quota snapshots through a remote document API; it has no record type or
gate-time read for pane observations, so this signal stays in the XDG cache.
When a credential-free account fingerprint is already present in the local
scopefuel snapshot cache, it scopes the record. Otherwise the fallback key is
local to the current host and Unix user.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import getpass
import hashlib
import json
import logging
import os
import pathlib
import re
import socket
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass

from . import cache

SCHEMA = "scopefuel.devin-free-limit.v1"
STATE_NAME = "devin-free-limit.json"
LOCK_NAME = "devin-free-limit.lock"
MAX_WINDOW = dt.timedelta(hours=24)
# Membership means a live exhausted free SWE-2 window stalls this profile.
# The Fusion-Sol rows joined after the confirmed stall recorded in hk 1729 r2.
FREE_PROFILES = frozenset(
    {
        "devin-swe2",
        "devin-swe2-medium",
        "devin-swe2-max",
        "builder-devin",
        "builder-devin-medium",
        "builder-devin-max",
        "devin-fusion-sol61",
        "devin-fusion-sol61-xhigh",
    }
)
# Observation is selected by provider kind, so paid Devin workers can report
# the same free-pool refusal text without being refused by this signal. Keep
# this input allowlist separate from FREE_PROFILES, which controls gate and
# recommendation denial membership.
OBSERVED_RUNG_IDS = frozenset(
    FREE_PROFILES
    | {
        "devin-glm52",
        "devin-swe17",
        "devin-ds41",
        "devin-ds41-max",
        "builder-ds41",
        "builder-ds41-max",
        "devin-fusion-opus55",
        "devin-fusion-sonnet55",
        "devin-argon",
        "devin-argon-medium",
        "devin-argon-low",
    }
)
RUNG_IDS = OBSERVED_RUNG_IDS
SOURCE_RE = re.compile(r"job:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
IDENTITY_KEY_RE = re.compile(r"(?:account|host-user)-[0-9a-f]{32}\Z")
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class FreeLimitRecord:
    reset_at: dt.datetime
    observed_at: dt.datetime
    rung: str
    source: str
    unparsed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "reset_at": format_utc(self.reset_at),
            "observed_at": format_utc(self.observed_at),
            "rung": self.rung,
            "source": self.source,
            "unparsed": self.unparsed,
        }


def parse_aware(value: str) -> dt.datetime:
    """Parse ISO-8601 and reject naive timestamps instead of assuming a zone."""

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp must include a timezone: {value!r}")
    try:
        return parsed.astimezone(dt.UTC)
    except OverflowError as exc:
        raise ValueError(f"timestamp is outside the UTC datetime range: {value!r}") from exc


def format_utc(value: dt.datetime) -> str:
    moment = value.astimezone(dt.UTC)
    return moment.isoformat().replace("+00:00", "Z")


def is_free_profile(profile: str) -> bool:
    return profile in FREE_PROFILES


def reason(record: FreeLimitRecord) -> str:
    return (
        f"free_rate_limited until {format_utc(record.reset_at)} "
        f"(observed {format_utc(record.observed_at)} via {record.source}; rung {record.rung})"
    )


def display_value(record: FreeLimitRecord | None) -> str:
    if record is None:
        return "not observed"
    return f"limited until {format_utc(record.reset_at)} via {record.source}"


def state_path() -> pathlib.Path:
    return cache.cache_dir() / STATE_NAME


def lock_path() -> pathlib.Path:
    return cache.cache_dir() / LOCK_NAME


def _identity_key() -> str:
    """Use only an already-cached credential-free identity; never probe a CLI."""

    snapshots = cache._load()
    entry = snapshots.get("devin") if isinstance(snapshots, dict) else None
    result = entry.get("result") if isinstance(entry, dict) else None
    if isinstance(result, dict):
        fingerprint = result.get("account_fp")
        kind = result.get("account_fp_kind")
        if kind == "account" and isinstance(fingerprint, str) and fingerprint.strip():
            digest = hashlib.sha256(("account\0" + fingerprint).encode("utf-8")).hexdigest()[:32]
            return "account-" + digest

    host = socket.gethostname()
    user = str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
    digest = hashlib.sha256((host + "\0" + user).encode("utf-8")).hexdigest()[:32]
    return "host-user-" + digest


def _record_from_dict(value: object) -> FreeLimitRecord | None:
    if not isinstance(value, dict):
        return None
    try:
        reset_at = parse_aware(str(value["reset_at"]))
        observed_at = parse_aware(str(value["observed_at"]))
        rung = value["rung"]
        source = value["source"]
        unparsed = value.get("unparsed", False)
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(rung, str) or rung not in OBSERVED_RUNG_IDS:
        return None
    if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
        return None
    if not isinstance(unparsed, bool):
        return None
    if reset_at <= observed_at or reset_at - observed_at > MAX_WINDOW:
        return None
    return FreeLimitRecord(reset_at, observed_at, rung, source, unparsed)


def _load_records(*, warn: bool = True) -> dict[str, FreeLimitRecord]:
    path = state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError, RecursionError, MemoryError) as exc:
        if warn:
            _LOG.warning("ignoring corrupt Devin free-window state: %s", exc)
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != SCHEMA
        or not isinstance(payload.get("records"), dict)
    ):
        if warn:
            _LOG.warning("ignoring corrupt Devin free-window state: invalid schema")
        return {}
    records: dict[str, FreeLimitRecord] = {}
    for account, raw in payload["records"].items():
        if not isinstance(account, str) or not account.startswith(("account-", "host-user-")):
            if warn:
                _LOG.warning("ignoring corrupt Devin free-window state: invalid identity key")
            return {}
        record = _record_from_dict(raw)
        if record is None:
            if warn:
                _LOG.warning("ignoring corrupt Devin free-window state: invalid record")
            return {}
        records[account] = record
    return records


@contextmanager
def _exclusive_lock() -> Iterator[None]:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _save_records(records: dict[str, FreeLimitRecord]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA,
        "records": {key: record.as_dict() for key, record in sorted(records.items())},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        with suppress(OSError):
            os.close(fd)
        with suppress(OSError):
            os.unlink(temporary)
        raise


def record_observation(
    *,
    reset: str,
    observed: str,
    rung: str,
    source: str,
    unparsed: bool = False,
    now: dt.datetime | None = None,
) -> bool:
    """Persist a non-expired observation; return False when expired/older."""

    reset_at = parse_aware(reset)
    observed_at = parse_aware(observed)
    if rung not in OBSERVED_RUNG_IDS:
        raise ValueError(f"unsupported Devin profile rung: {rung!r}")
    if not SOURCE_RE.fullmatch(source):
        raise ValueError("source must be job:<job>")
    if not isinstance(unparsed, bool):
        raise ValueError("unparsed must be boolean")
    if reset_at <= observed_at:
        return False
    if reset_at - observed_at > MAX_WINDOW:
        raise ValueError("reset must be within 24 hours of observed time")
    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(dt.UTC)
    if observed_at > current:
        raise ValueError("observed time cannot be in the future")
    if reset_at <= current:
        return False

    key = _identity_key()
    new_record = FreeLimitRecord(reset_at, observed_at, rung, source, unparsed)
    with _exclusive_lock():
        records = _load_records()
        records = {
            identity: item
            for identity, item in records.items()
            if item.observed_at <= current < item.reset_at
        }
        previous = records.get(key)
        if previous is not None:
            old_order = (previous.observed_at, previous.reset_at, previous.source, previous.rung)
            new_order = (new_record.observed_at, new_record.reset_at, new_record.source, new_record.rung)
            if old_order >= new_order:
                return False
        records[key] = new_record
        _save_records(records)
    return True


def clear_hold(*, account_key: str | None = None, now: dt.datetime | None = None) -> FreeLimitRecord | None:
    """Remove the live hold under one identity key; return it, or None.

    Runs under the same exclusive lock and atomic replace as the writer, so a
    clear racing an observe lands cleanly: whichever commits last wins. Only a
    live record counts; an absent or already-expired record is nothing to
    clear (expired entries are still pruned on the way out).
    """

    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(dt.UTC)
    key = _identity_key() if account_key is None else account_key
    if not isinstance(key, str) or not IDENTITY_KEY_RE.fullmatch(key):
        raise ValueError("account key must be account-<hex> or host-user-<hex>")
    removed: FreeLimitRecord | None = None
    with _exclusive_lock():
        records = _load_records()
        live = {
            identity: item
            for identity, item in records.items()
            if item.observed_at <= current < item.reset_at
        }
        removed = live.pop(key, None)
        if live != records:
            _save_records(live)
    return removed


def active_record(*, now: dt.datetime | None = None) -> FreeLimitRecord | None:
    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(dt.UTC)
    record = _load_records().get(_identity_key())
    return record if record is not None and record.observed_at <= current < record.reset_at else None
