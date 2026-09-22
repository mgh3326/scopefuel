"""Local, append-only manual quota observations.

Manual values are measurement evidence, not a policy bypass.  They live in a
file separate from automatic snapshots and carry an explicitly unverified
operator-source claim plus enough process context for later audit.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import getpass
import json
import math
import os
import pathlib
import re
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace

from .model import Bucket, ProviderResult, Scope, _is_valid_used_pct

SCHEMA = "scopefuel.manual.v1"
SOURCE = "operator"
SOURCE_VERIFICATION = "unverified"
SOURCE_LABEL = "자기신고 · 미검증"
DEFAULT_TTL_S = 15 * 60.0
MAX_TTL_S = 2 * 3600.0
MAX_FUTURE_SKEW_S = 300.0
MAX_REASON_CHARS = 1000

WINDOW_ALIASES = {
    "5h": "5h",
    "7d": "7d",
    "30d": "30d",
    "daily": "1d",
    "weekly": "7d",
}
DEFAULT_WINDOWS = {
    "devin": "1d",
    "grok": "7d",
    "kiro": "30d",
    "upstage": "30d",
}
# These are admission-coverage floors, not synthetic zero-valued buckets.
# Devin intentionally requires daily only; its weekly value is not available
# from the current provider and must not be invented.
REQUIRED_WINDOWS = {
    "claude": frozenset({"5h", "7d"}),
    "codex": frozenset({"5h", "7d"}),
    "devin": frozenset({"1d"}),
    "grok": frozenset({"7d"}),
    "kimi": frozenset({"5h", "7d"}),
    "kiro": frozenset({"30d"}),
    "clinepass": frozenset({"5h", "7d", "30d"}),
    "upstage": frozenset({"30d"}),
}

_DURATION_PART = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([smhdw])", re.IGNORECASE)
_AUTH_FAILURE = re.compile(
    r"(?<!\d)(?:401|403)(?!\d)|auth(?:entication)?\s+fail|unauthori[sz]ed|forbidden|"
    r"(?:credential|token).*(?:invalid|expired)|인증\s*실패|자격증명",
    re.I,
)
_RATE_OR_TRANSPORT = re.compile(
    r"(?<!\d)429(?!\d)|rate[ -]?limit|too many requests|timeout|timed out|"
    r"connection|transport|network|dns|unreachable|circuit open|urlopen|errno|"
    r"temporarily unavailable|nodename|remote end|ssl|http\s+5\d\d",
    re.I,
)
_PARSE_FAILURE = re.compile(
    r"parse|parser|decode|malformed|json|format|expecting value|extra data|unterminated|"
    r"파싱|형식|찾지 못|유효 범위|no data|bucket",
    re.I,
)

_AGENT_ENV_SIGNALS = (
    "AGENT_NAME",
    "CLAUDECODE",
    "CLAUDE_CODE",
    "CODEX_HOME",
    "CODEX_THREAD_ID",
    "DEVIN_AGENT",
    "HERDR_SESSION",
    "KIMI_CODE_HOME",
    "OPENCODE",
    "PANEWIRE_MACHINE_ID",
)


class ManualError(ValueError):
    """Manual store or input is unusable."""


@dataclass(frozen=True)
class Resolution:
    result: ProviderResult
    applied: bool
    summary: dict | None
    selected_entries: tuple[dict, ...] = ()
    last_auto_error: str | None = None
    failure_kind: str | None = None
    failure_reason: str | None = None


def manual_path() -> pathlib.Path:
    if override := os.environ.get("SCOPEFUEL_MANUAL"):
        return pathlib.Path(os.path.expanduser(override))
    from .cache import cache_path

    return cache_path().parent / "manual.json"


def _lock_path() -> pathlib.Path:
    return manual_path().with_name("manual.lock")


@contextmanager
def _store_lock(*, exclusive: bool) -> Iterator[None]:
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        os.fchmod(lock_file.fileno(), 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _empty_store() -> dict:
    return {"schema": SCHEMA, "history": [], "latest": {}}


def _load_unlocked() -> dict:
    path = manual_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_store()
    except (OSError, json.JSONDecodeError) as exc:
        raise ManualError(f"manual store 읽기 실패: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise ManualError("manual store schema 불일치")
    if not isinstance(raw.get("history"), list) or not isinstance(raw.get("latest"), dict):
        raise ManualError("manual store 구조 불일치")
    _validate_store(raw)
    return raw


def _validate_store(store: dict) -> None:
    known_entries: dict[str, dict] = {}
    for index, event in enumerate(store["history"]):
        if not isinstance(event, dict):
            raise ManualError(f"manual history {index} 구조 불일치")
        kind = event.get("event")
        if kind == "set":
            observation_id = event.get("manual_observation_id")
            if not isinstance(observation_id, str) or not observation_id or observation_id in known_entries:
                raise ManualError(f"manual history {index} observation id 불일치")
            if not isinstance(event.get("pool"), str) or not event["pool"]:
                raise ManualError(f"manual history {index} pool 불일치")
            if event.get("window") not in {"5h", "1d", "7d", "30d"}:
                raise ManualError(f"manual history {index} window 불일치")
            if not _is_valid_used_pct(event.get("used_pct")):
                raise ManualError(f"manual history {index} used_pct 불일치")
            measured = _parse_iso(event.get("measured_at"))
            entered = _parse_iso(event.get("entered_at"))
            expires = _parse_iso(event.get("expires_at"))
            if measured is None or entered is None or expires is None:
                raise ManualError(f"manual history {index} timestamp 불일치")
            if measured > entered + dt.timedelta(seconds=MAX_FUTURE_SKEW_S):
                raise ManualError(f"manual history {index} future measurement 불일치")
            if expires > measured + dt.timedelta(seconds=MAX_TTL_S) or expires > entered + dt.timedelta(
                seconds=MAX_TTL_S
            ):
                raise ManualError(f"manual history {index} expiry 불일치")
            if not isinstance(event.get("reason"), str) or not event["reason"].strip():
                raise ManualError(f"manual history {index} reason 불일치")
            if len(event["reason"]) > MAX_REASON_CHARS:
                raise ManualError(f"manual history {index} reason 길이 불일치")
            _validate_audit_fields(event, index)
            limits = event.get("entitlement_and_limits")
            if not isinstance(limits, dict) or (
                limits.get("window"),
                limits.get("used_pct"),
                limits.get("resets_at"),
            ) != (event.get("window"), event.get("used_pct"), event.get("resets_at")):
                raise ManualError(f"manual history {index} limit coverage 불일치")
            resets_at = event.get("resets_at")
            if resets_at is not None and _parse_iso(resets_at) is None:
                raise ManualError(f"manual history {index} reset timestamp 불일치")
            if event.get("local_only") is not True or event.get("usage_mode") != "admission_evidence":
                raise ManualError(f"manual history {index} usage mode 불일치")
            supersedes = event.get("supersedes")
            if supersedes is not None:
                prior = known_entries.get(str(supersedes))
                if prior is None or (prior.get("pool"), prior.get("window")) != (
                    event.get("pool"),
                    event.get("window"),
                ):
                    raise ManualError(f"manual history {index} supersedes ref 불일치")
            known_entries[observation_id] = event
        elif kind == "clear":
            clear_id = event.get("manual_clear_id")
            if not isinstance(clear_id, str) or not clear_id:
                raise ManualError(f"manual history {index} clear id 불일치")
            if not isinstance(event.get("pool"), str) or not event["pool"]:
                raise ManualError(f"manual history {index} clear pool 불일치")
            if _parse_iso(event.get("entered_at")) is None:
                raise ManualError(f"manual history {index} clear timestamp 불일치")
            _validate_audit_fields(event, index)
            cleared_ids = event.get("cleared_observation_ids")
            if not isinstance(cleared_ids, list) or any(
                not isinstance(value, str) or value not in known_entries for value in cleared_ids
            ):
                raise ManualError(f"manual history {index} cleared refs 불일치")
        else:
            raise ManualError(f"manual history {index} event 불일치")
    for key, observation_id in store["latest"].items():
        if (
            not isinstance(key, str)
            or not isinstance(observation_id, str)
            or observation_id not in known_entries
        ):
            raise ManualError("manual latest projection 불일치")


def _validate_audit_fields(event: dict, index: int) -> None:
    if (
        event.get("source") != SOURCE
        or event.get("source_verification") != SOURCE_VERIFICATION
        or event.get("source_label") != SOURCE_LABEL
    ):
        raise ManualError(f"manual history {index} source claim 불일치")
    author = event.get("author")
    if (
        not isinstance(author, dict)
        or not isinstance(author.get("os_user"), str)
        or not isinstance(author.get("host"), str)
        or author.get("verification") != SOURCE_VERIFICATION
        or author.get("label") != SOURCE_LABEL
    ):
        raise ManualError(f"manual history {index} author 불일치")
    context = event.get("execution_context")
    if not isinstance(context, dict):
        raise ManualError(f"manual history {index} audit context 불일치")
    parent = context.get("parent")
    tty = context.get("tty")
    ancestors = context.get("ancestors")
    signals = context.get("agent_environment_signals")
    if (
        not isinstance(parent, dict)
        or not isinstance(parent.get("pid"), int)
        or parent.get("name") is not None
        and not isinstance(parent.get("name"), str)
        or not isinstance(tty, dict)
        or not isinstance(tty.get("stdin"), bool)
        or not isinstance(tty.get("stdout"), bool)
        or not isinstance(ancestors, list)
        or not isinstance(signals, dict)
        or any(key not in _AGENT_ENV_SIGNALS or value is not True for key, value in signals.items())
    ):
        raise ManualError(f"manual history {index} execution context 불일치")


def load_store() -> dict:
    with _store_lock(exclusive=False):
        return _load_unlocked()


def _save_unlocked(store: dict) -> None:
    path = manual_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(store, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        tmp.chmod(0o600)
        tmp.replace(path)
    except OSError as exc:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise ManualError(f"manual store 쓰기 실패: {exc}") from exc


def parse_duration(value: str) -> float:
    text = value.strip()
    if not text:
        raise ManualError("duration 이 비어 있습니다")
    total = 0.0
    position = 0
    units = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}
    while position < len(text):
        match = _DURATION_PART.match(text, position)
        if match is None:
            raise ManualError(f"duration 형식 오류: {value!r}")
        total += float(match.group(1)) * units[match.group(2).lower()]
        position = match.end()
    if not math.isfinite(total) or total <= 0:
        raise ManualError("duration 은 0보다 큰 유한값이어야 합니다")
    return total


def parse_timestamp(value: str, *, now: dt.datetime | None = None) -> dt.datetime:
    current = _as_utc(now or dt.datetime.now(dt.UTC))
    if value.strip().lower() == "now":
        return current
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManualError(f"timestamp 형식 오류: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(dt.UTC)


def normalize_window(pool: str, window: str | None) -> str:
    if window is None:
        inferred = DEFAULT_WINDOWS.get(pool)
        if inferred is None:
            raise ManualError(f"{pool} 은 --window 지정이 필요합니다")
        return inferred
    try:
        return WINDOW_ALIASES[window]
    except KeyError as exc:
        raise ManualError(f"지원하지 않는 window: {window}") from exc


def parse_used_pct(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ManualError("--used 는 0..100 유한 숫자여야 합니다") from exc
    if not _is_valid_used_pct(parsed):
        raise ManualError("--used 는 0..100 유한 숫자여야 합니다")
    return parsed


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _iso(value: dt.datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _process_info(pid: int) -> tuple[int | None, str | None]:
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    line = proc.stdout.strip().splitlines()
    if not line:
        return None, None
    parts = line[0].strip().split(maxsplit=1)
    try:
        parent_pid = int(parts[0])
    except (IndexError, ValueError):
        parent_pid = None
    name = pathlib.Path(parts[1]).name if len(parts) > 1 else None
    return parent_pid, name


def _execution_context() -> dict:
    parent_pid = os.getppid()
    next_pid, parent_name = _process_info(parent_pid)
    ancestors: list[dict] = []
    seen = {os.getpid(), parent_pid}
    current = next_pid
    for _ in range(4):
        if current is None or current <= 0 or current in seen:
            break
        seen.add(current)
        next_parent, name = _process_info(current)
        ancestors.append({"pid": current, "name": name})
        current = next_parent
    return {
        "parent": {"pid": parent_pid, "name": parent_name},
        "ancestors": ancestors,
        "tty": {"stdin": bool(sys.stdin.isatty()), "stdout": bool(sys.stdout.isatty())},
        # Names and booleans only.  Environment values are deliberately never read into the record.
        "agent_environment_signals": {name: True for name in _AGENT_ENV_SIGNALS if name in os.environ},
    }


def _author() -> dict:
    try:
        os_user = getpass.getuser()
    except (OSError, KeyError):
        os_user = "unknown"
    try:
        host = socket.gethostname()
    except OSError:
        host = "unknown"
    return {
        "os_user": os_user,
        "host": host,
        "verification": SOURCE_VERIFICATION,
        "label": SOURCE_LABEL,
    }


def _latest_set_id(history: list[dict], pool: str, window: str) -> str | None:
    for event in reversed(history):
        if event.get("event") == "set" and event.get("pool") == pool and event.get("window") == window:
            value = event.get("manual_observation_id")
            return str(value) if value else None
    return None


def record_observation(
    *,
    pool: str,
    used_pct: float,
    window: str | None,
    measured_at: dt.datetime,
    reason: str,
    ttl_s: float = DEFAULT_TTL_S,
    resets_in_s: float | None = None,
    resets_at: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> dict:
    entered_at = _as_utc(now or dt.datetime.now(dt.UTC))
    measured_at = _as_utc(measured_at)
    if not _is_valid_used_pct(used_pct):
        raise ManualError("used_pct 는 0..100 유한 숫자여야 합니다")
    if not math.isfinite(ttl_s) or ttl_s <= 0 or ttl_s > MAX_TTL_S:
        raise ManualError("--ttl 은 0보다 크고 2h 이하여야 합니다")
    if measured_at > entered_at + dt.timedelta(seconds=MAX_FUTURE_SKEW_S):
        raise ManualError("--measured-at 은 미래 시각일 수 없습니다")
    reason = reason.strip()
    if not reason:
        raise ManualError("--reason 은 비어 있을 수 없습니다")
    if len(reason) > MAX_REASON_CHARS:
        raise ManualError(f"--reason 은 {MAX_REASON_CHARS}자 이하여야 합니다")
    normalized_window = normalize_window(pool, window)
    if resets_in_s is not None and resets_at is not None:
        raise ManualError("--resets-in 과 --resets-at 은 함께 쓸 수 없습니다")
    if resets_in_s is not None:
        resets_at = measured_at + dt.timedelta(seconds=resets_in_s)
    expires_at = min(
        measured_at + dt.timedelta(seconds=ttl_s),
        entered_at + dt.timedelta(seconds=MAX_TTL_S),
    )
    if resets_at is not None:
        expires_at = min(expires_at, _as_utc(resets_at))

    with _store_lock(exclusive=True):
        store = _load_unlocked()
        history: list[dict] = store["history"]
        observation_id = str(uuid.uuid4())
        supersedes = _latest_set_id(history, pool, normalized_window)
        entry = {
            "event": "set",
            "manual_observation_id": observation_id,
            "pool": pool,
            "window": normalized_window,
            "used_pct": float(used_pct),
            "resets_at": _iso(resets_at) if resets_at is not None else None,
            "entitlement_and_limits": {
                "window": normalized_window,
                "used_pct": float(used_pct),
                "resets_at": _iso(resets_at) if resets_at is not None else None,
            },
            "measured_at": _iso(measured_at),
            "entered_at": _iso(entered_at),
            "author": _author(),
            "reason": reason,
            "evidence_ref": "explicit-local-observation",
            "expires_at": _iso(expires_at),
            "supersedes": supersedes,
            "source": SOURCE,
            "source_verification": SOURCE_VERIFICATION,
            "source_label": SOURCE_LABEL,
            "usage_mode": "admission_evidence",
            "local_only": True,
            "execution_context": _execution_context(),
        }
        history.append(entry)
        store["latest"][f"{pool}:{normalized_window}"] = observation_id
        _save_unlocked(store)
    return entry


def clear_pool(pool: str, *, now: dt.datetime | None = None) -> dict:
    entered_at = _as_utc(now or dt.datetime.now(dt.UTC))
    with _store_lock(exclusive=True):
        store = _load_unlocked()
        history: list[dict] = store["history"]
        cleared_ids = [
            str(event["manual_observation_id"])
            for event in history
            if event.get("event") == "set"
            and event.get("pool") == pool
            and event.get("manual_observation_id")
        ]
        event = {
            "event": "clear",
            "manual_clear_id": str(uuid.uuid4()),
            "pool": pool,
            "entered_at": _iso(entered_at),
            "cleared_observation_ids": cleared_ids,
            "source": SOURCE,
            "source_verification": SOURCE_VERIFICATION,
            "source_label": SOURCE_LABEL,
            "author": _author(),
            "execution_context": _execution_context(),
        }
        history.append(event)
        for key in list(store["latest"]):
            if key.startswith(f"{pool}:"):
                del store["latest"][key]
        _save_unlocked(store)
    return event


def automatic_error_text(result: ProviderResult) -> str | None:
    if result.last_error:
        return result.last_error
    if result.error:
        return result.error
    if result.warning:
        return result.warning
    return None


def classify_automatic_failure(result: ProviderResult) -> tuple[str | None, str | None]:
    text = automatic_error_text(result)
    if text is None:
        return None, None
    safe_text = " ".join(text.split())[:200]
    if _AUTH_FAILURE.search(safe_text):
        return "auth", safe_text
    if _RATE_OR_TRANSPORT.search(safe_text):
        return "transport", safe_text
    if _PARSE_FAILURE.search(safe_text):
        return "parse_error", safe_text
    return "unsupported", safe_text


def _auto_state(result: ProviderResult) -> tuple[float | None, bool]:
    return result.fetched_at, result.status == "ok" and not result.stale


def _entry_views(
    store: dict,
    *,
    now: dt.datetime,
    auto_states: dict[str, tuple[float | None, bool]] | None = None,
) -> list[dict]:
    history = store.get("history") or []
    auto_states = auto_states or {}
    later_replacement: dict[int, tuple[str, str | None]] = {}
    latest_by_key: dict[tuple[str, str], int] = {}
    for index, event in enumerate(history):
        if not isinstance(event, dict):
            continue
        pool = str(event.get("pool") or "")
        if event.get("event") == "set":
            key = (pool, str(event.get("window") or ""))
            if key in latest_by_key:
                replacement_id = str(event.get("manual_observation_id") or "") or None
                later_replacement[latest_by_key[key]] = ("superseded", replacement_id)
            latest_by_key[key] = index
        elif event.get("event") == "clear":
            clear_id = str(event.get("manual_clear_id") or "") or None
            for key, set_index in list(latest_by_key.items()):
                if key[0] == pool:
                    later_replacement[set_index] = ("cleared", clear_id)
                    del latest_by_key[key]

    current = _as_utc(now)
    views: list[dict] = []
    for index, event in enumerate(history):
        if not isinstance(event, dict) or event.get("event") != "set":
            continue
        view = dict(event)
        measured = _parse_iso(event.get("measured_at"))
        expires = _parse_iso(event.get("expires_at"))
        auto_fetched, auto_fresh = auto_states.get(str(event.get("pool") or ""), (None, False))
        auto_time = dt.datetime.fromtimestamp(auto_fetched, tz=dt.UTC) if auto_fetched is not None else None
        replacement = later_replacement.get(index)
        if replacement is not None:
            status, replacement_id = replacement
            view["replacement_ref"] = replacement_id
        elif measured is not None and auto_time is not None and auto_time > measured:
            status = "superseded_by_auto"
        elif expires is None or expires <= current:
            status = "expired"
        elif auto_fresh:
            status = "shadowed_by_fresh_auto"
        else:
            status = "active"
        view["status"] = status
        view["effective"] = status == "active"
        view["age_s"] = None if measured is None else max(0.0, (current - measured).total_seconds())
        view["remaining_effect_s"] = (
            None if expires is None else max(0.0, (expires - current).total_seconds())
        )
        views.append(view)
    return views


def _counts(views: list[dict]) -> dict:
    replaced_statuses = {"superseded", "cleared", "superseded_by_auto"}
    return {
        "input": len(views),
        "expired": sum(1 for view in views if view.get("status") == "expired"),
        "replaced": sum(1 for view in views if view.get("status") in replaced_statuses),
    }


def _summary_for_pool(
    store: dict,
    pool: str,
    *,
    now: dt.datetime,
    auto_result: ProviderResult | None = None,
) -> dict | None:
    auto_states = {pool: _auto_state(auto_result)} if auto_result is not None else {}
    entries = [view for view in _entry_views(store, now=now, auto_states=auto_states) if view["pool"] == pool]
    if not entries:
        return None
    return {
        "schema": SCHEMA,
        "source": SOURCE,
        "source_verification": SOURCE_VERIFICATION,
        "source_label": SOURCE_LABEL,
        "local_only": True,
        "entries": entries,
        "latest_valid": [entry for entry in entries if entry["effective"]],
        "counts": _counts(entries),
    }


def _required_windows(pool: str) -> frozenset[str]:
    return REQUIRED_WINDOWS.get(pool, frozenset())


def _confirmed_account_cutoff(result: ProviderResult, *, now: dt.datetime) -> tuple[float, float] | None:
    """Return (used, cutoff) only for a persisted successful automatic snapshot."""
    if result.fetched_at is None:
        return None
    from .policy import get_policy
    from .recommend import PRESERVE_EXCLUDE_PCT, SPEND_EXCLUDE_PCT

    fallback_class = result.pool_class if result.pool_class in {"preserve", "spend"} else "preserve"
    effective_class = get_policy(result.id, fallback_class, today=_as_utc(now).date())[0]
    if effective_class == "exclude":
        return None
    cutoff = SPEND_EXCLUDE_PCT if effective_class == "spend" else PRESERVE_EXCLUDE_PCT
    confirmed = [
        float(bucket.used_pct)
        for bucket in result.buckets
        if bucket.scope.kind == "account" and _is_valid_used_pct(bucket.used_pct)
    ]
    over = [used for used in confirmed if used >= cutoff]
    return (max(over), cutoff) if over else None


def _horizon(window: str) -> str:
    if window in {"5h", "1d"}:
        return "now"
    if window == "30d":
        return "month"
    return "week"


def resolve_result(
    result: ProviderResult,
    *,
    now: dt.datetime,
    store: dict | None = None,
    group_name: str | None = None,
) -> Resolution:
    store = load_store() if store is None else store
    summary = _summary_for_pool(store, result.id, now=now, auto_result=result)
    annotated = replace(result, manual=summary) if summary is not None else result
    if summary is None:
        return Resolution(annotated, False, None, failure_reason="manual observation 없음")
    if result.status == "ok" and not result.stale:
        summary["selection"] = "automatic_fresh"
        return Resolution(annotated, False, summary, failure_reason="fresh automatic measurement 우선")
    if group_name is not None:
        summary["selection"] = "unsupported_group_scope"
        return Resolution(
            annotated,
            False,
            summary,
            failure_reason="manual CLI는 group scope를 증명하지 않음",
        )

    confirmed_cutoff = _confirmed_account_cutoff(result, now=now)
    if confirmed_cutoff is not None:
        used_pct, cutoff = confirmed_cutoff
        summary["selection"] = "automatic_cutoff"
        summary["automatic_cutoff"] = {"used_pct": used_pct, "cutoff": cutoff}
        return Resolution(
            annotated,
            False,
            summary,
            failure_reason="자동 측정이 cutoff 초과를 확정하여 manual fallback 불가",
        )

    failure_kind, error_text = classify_automatic_failure(result)
    summary["last_auto_error"] = error_text
    summary["last_auto_error_kind"] = failure_kind
    if failure_kind == "auth":
        summary["selection"] = "auth_failure"
        return Resolution(
            annotated,
            False,
            summary,
            last_auto_error=error_text,
            failure_kind=failure_kind,
            failure_reason="검증된 auth 실패는 manual observation으로 숨길 수 없음",
        )
    if failure_kind not in {"transport", "parse_error"}:
        summary["selection"] = "unsupported_failure"
        return Resolution(
            annotated,
            False,
            summary,
            last_auto_error=error_text,
            failure_kind=failure_kind,
            failure_reason="manual fallback 허용 오류가 아님",
        )

    active = {entry["window"]: entry for entry in summary["latest_valid"]}
    required = _required_windows(result.id)
    missing = sorted(required - active.keys())
    summary["required_windows"] = sorted(required)
    summary["covered_windows"] = sorted(active)
    summary["missing_windows"] = missing
    if missing or not active:
        summary["selection"] = "incomplete_bucket_coverage"
        return Resolution(
            annotated,
            False,
            summary,
            last_auto_error=error_text,
            failure_kind=failure_kind,
            failure_reason=f"필수 manual bucket 누락: {', '.join(missing) or 'all'}",
        )

    selected = tuple(active[key] for key in sorted(active))
    buckets = [
        Bucket(
            label=f"manual {entry['window']}",
            window=entry["window"],
            used_pct=entry["used_pct"],
            resets_at=entry.get("resets_at"),
            scope=Scope("account"),
            horizon=_horizon(entry["window"]),  # type: ignore[arg-type]
            note=f"source={SOURCE}; {SOURCE_LABEL}",
        )
        for entry in selected
    ]
    measured = [_parse_iso(entry.get("measured_at")) for entry in selected]
    expires = [_parse_iso(entry.get("expires_at")) for entry in selected]
    measured_valid = [value for value in measured if value is not None]
    expires_valid = [value for value in expires if value is not None]
    weakest_measured = min(measured_valid) if measured_valid else now
    earliest_expiry = min(expires_valid) if expires_valid else now
    summary["selection"] = "manual_fallback"
    summary["selected_observation_ids"] = [entry["manual_observation_id"] for entry in selected]
    summary["selected_measured_at"] = _iso(weakest_measured)
    summary["selected_expires_at"] = _iso(earliest_expiry)
    manual_result = ProviderResult(
        id=result.id,
        plan=result.plan,
        buckets=buckets,
        note=f"source={SOURCE}; {SOURCE_LABEL}; 자동 측정 마지막 오류={error_text}",
        source=SOURCE,
        fetched_at=weakest_measured.timestamp(),
        age_s=max(0.0, (_as_utc(now) - weakest_measured).total_seconds()),
        stale=False,
        pool_class=result.pool_class,
        last_error=error_text,
        manual=summary,
    )
    return Resolution(
        manual_result,
        True,
        summary,
        selected_entries=selected,
        last_auto_error=error_text,
        failure_kind=failure_kind,
    )


def apply_for_display(results: list[ProviderResult], *, now: dt.datetime) -> list[ProviderResult]:
    try:
        store = load_store()
    except ManualError as exc:
        audit = {"schema": SCHEMA, "store_error": str(exc), "source_verification": SOURCE_VERIFICATION}
        return [replace(result, manual=audit) for result in results]
    return [resolve_result(result, now=now, store=store).result for result in results]


def _automatic_states_from_cache(now: dt.datetime) -> dict[str, tuple[float | None, bool]]:
    from .cache import DEFAULT_TTL_S as AUTO_DEFAULT_TTL_S
    from .cache import PROVIDER_TTL_S, cache_path

    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    states: dict[str, tuple[float | None, bool]] = {}
    current_epoch = _as_utc(now).timestamp()
    if not isinstance(raw, dict):
        return states
    for pool, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        try:
            fetched = float(entry.get("fetched_at"))
        except (TypeError, ValueError):
            continue
        ttl = PROVIDER_TTL_S.get(str(pool), AUTO_DEFAULT_TTL_S)
        states[str(pool)] = (fetched, current_epoch - fetched <= ttl)
    return states


def list_payload(*, pool: str | None = None, now: dt.datetime | None = None) -> dict:
    current = _as_utc(now or dt.datetime.now(dt.UTC))
    store = load_store()
    views = _entry_views(store, now=current, auto_states=_automatic_states_from_cache(current))
    if pool is not None:
        views = [view for view in views if view.get("pool") == pool]
    clears = [
        event
        for event in store["history"]
        if isinstance(event, dict)
        and event.get("event") == "clear"
        and (pool is None or event.get("pool") == pool)
    ]
    return {
        "schema": SCHEMA,
        "source": SOURCE,
        "source_verification": SOURCE_VERIFICATION,
        "source_label": SOURCE_LABEL,
        "local_only": True,
        "entries": views,
        "clear_events": clears,
        "latest_valid": [view for view in views if view.get("effective")],
        "counts": _counts(views),
    }


def format_list(payload: dict) -> str:
    lines = [
        f"manual source={SOURCE} source_verification={SOURCE_VERIFICATION} label={SOURCE_LABEL} "
        f"input={payload['counts']['input']} expired={payload['counts']['expired']} "
        f"replaced={payload['counts']['replaced']} local_only=true"
    ]
    for entry in payload["entries"]:
        lines.append(
            f"{entry['pool']} window={entry['window']} used_pct={entry['used_pct']:g} "
            f"status={entry['status']} measured_at={entry['measured_at']} "
            f"expires_at={entry['expires_at']} source={SOURCE} "
            f"source_verification={SOURCE_VERIFICATION} label={SOURCE_LABEL} "
            f"id={entry['manual_observation_id']}"
        )
    for event in payload["clear_events"]:
        lines.append(
            f"{event['pool']} clear entered_at={event['entered_at']} source={SOURCE} "
            f"source_verification={SOURCE_VERIFICATION} label={SOURCE_LABEL} "
            f"id={event['manual_clear_id']}"
        )
    return "\n".join(lines)


def format_age_seconds(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}초"
    if seconds < 5400:
        return f"{int(seconds // 60)}분"
    return f"{seconds / 3600:.1f}시간"
