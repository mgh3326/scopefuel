"""Host-local observations of a Command Code (cmd) usage/rate limit.

hk 2141 (operator r1027cmdcode, plan GOAT per r1027cmdgoat): the commandcode
pool is manual-only — the Command Code credit has no usage API, and the
operator's credit observation is the only admission evidence. When a cmd
run's screen or exit text shows a usage or
rate limit, the observer (wrk side, hk 2140) records the reset the text names
through ``scopefuel observe commandcode-limit``; while that record is live the
pool reads as exhausted until the named reset — fail closed, same direction
as a stale manual observation.

The observer never logs in, never reads cmd's credential/config files, and
never runs cmd itself: it stores only the limit already seen on a worker's
screen. The record is host-local (XDG cache), keyed on host+user like the
Devin free-limit store — no account fingerprint exists for a probe-less pool.

The cmd limit wording is not yet known: ``cmd --help`` documents ``/usage``
but no limit text, and no capture names one. Until a desk capture lands, the
observer writes only records whose reset it was explicitly handed; the tests
exercise the store through typed values, never a real cmd run.
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
from .free_window import format_utc, parse_aware

SCHEMA = "scopefuel.commandcode-limit.v1"
STATE_NAME = "commandcode-limit.json"
LOCK_NAME = "commandcode-limit.lock"
# The cmd launcher spellings a limit observation may be recorded against.
# Membership means a live record stalls this rung's pool.
OBSERVED_RUNG_IDS = frozenset({"cmd-dsflash41"})
RUNG_IDS = OBSERVED_RUNG_IDS
SOURCE_RE = re.compile(r"job:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
IDENTITY_KEY_RE = re.compile(r"(?:account|host-user)-[0-9a-f]{32}\Z")
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandcodeLimitRecord:
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


def reason(record: CommandcodeLimitRecord) -> str:
    return (
        f"commandcode rate_limited until {format_utc(record.reset_at)} "
        f"(observed {format_utc(record.observed_at)} via {record.source}; rung {record.rung})"
    )


def display_value(record: CommandcodeLimitRecord | None) -> str:
    if record is None:
        return "not observed"
    return (
        f"last observed commandcode rate_limited until {format_utc(record.reset_at)} "
        f"(observed {format_utc(record.observed_at)} via {record.source})"
    )


def state_path() -> pathlib.Path:
    return cache.cache_dir() / STATE_NAME


def lock_path() -> pathlib.Path:
    return cache.cache_dir() / LOCK_NAME


def _identity_key() -> str:
    """No probe ever caches a commandcode account fingerprint — host+user only."""

    host = socket.gethostname()
    user = str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
    digest = hashlib.sha256((host + "\0" + user).encode("utf-8")).hexdigest()[:32]
    return "host-user-" + digest


def _record_from_dict(value: object) -> CommandcodeLimitRecord | None:
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
    if reset_at <= observed_at:
        return None
    return CommandcodeLimitRecord(reset_at, observed_at, rung, source, unparsed)


def _load_records(*, warn: bool = True) -> dict[str, CommandcodeLimitRecord]:
    path = state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError, RecursionError, MemoryError) as exc:
        if warn:
            _LOG.warning("ignoring corrupt commandcode limit state: %s", exc)
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != SCHEMA
        or not isinstance(payload.get("records"), dict)
    ):
        if warn:
            _LOG.warning("ignoring corrupt commandcode limit state: invalid schema")
        return {}
    records: dict[str, CommandcodeLimitRecord] = {}
    for account, raw in payload["records"].items():
        if not isinstance(account, str) or not account.startswith(("account-", "host-user-")):
            if warn:
                _LOG.warning("ignoring corrupt commandcode limit state: invalid identity key")
            return {}
        record = _record_from_dict(raw)
        if record is None:
            if warn:
                _LOG.warning("ignoring corrupt commandcode limit state: invalid record")
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


def _save_records(records: dict[str, CommandcodeLimitRecord]) -> None:
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
        raise ValueError(f"unsupported commandcode profile rung: {rung!r}")
    if not SOURCE_RE.fullmatch(source):
        raise ValueError("source must be job:<job>")
    if not isinstance(unparsed, bool):
        raise ValueError("unparsed must be boolean")
    if reset_at <= observed_at:
        return False
    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(dt.UTC)
    if observed_at > current:
        raise ValueError("observed time cannot be in the future")
    if reset_at <= current:
        return False

    key = _identity_key()
    new_record = CommandcodeLimitRecord(reset_at, observed_at, rung, source, unparsed)
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


def clear_hold(
    *, account_key: str | None = None, now: dt.datetime | None = None
) -> CommandcodeLimitRecord | None:
    """Remove the live hold under one identity key; return it, or None."""

    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(dt.UTC)
    key = _identity_key() if account_key is None else account_key
    if not isinstance(key, str) or not IDENTITY_KEY_RE.fullmatch(key):
        raise ValueError("account key must be account-<hex> or host-user-<hex>")
    removed: CommandcodeLimitRecord | None = None
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


def active_record(*, now: dt.datetime | None = None) -> CommandcodeLimitRecord | None:
    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(dt.UTC)
    record = _load_records().get(_identity_key())
    return record if record is not None and record.observed_at <= current < record.reset_at else None
