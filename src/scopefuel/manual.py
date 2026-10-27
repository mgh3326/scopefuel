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
CREDIT_V1_MAX_TTL_S = 24 * 3600.0
CREDIT_V1_TTL_POLICY = "credit-24h-v1"
CREDIT_V2_TTL_POLICY = "credit-until-expiry-v2"
MAX_FUTURE_SKEW_S = 300.0
MAX_REASON_CHARS = 1000

WINDOW_ALIASES = {
    "5h": "5h",
    "7d": "7d",
    "30d": "30d",
    "daily": "1d",
    "weekly": "7d",
    # hk 2079: prepaid credit has no rolling limit window. The operator enters
    # the console balance as remaining/total USD and the expiry date; used_pct
    # is derived from those two numbers (see credit_used_pct).
    "credit": "credit",
}
DEFAULT_WINDOWS = {
    "devin": "1d",
    "grok": "7d",
    "kiro": "30d",
    "upstage": "30d",
    "anthropic-api": "credit",
}
# hk 2079: pools with no automatic probe. Their admission evidence is a manual
# observation only, and a missing/stale observation fails closed exactly like
# every other manual fallback (REQUIRED_WINDOWS coverage + TTL).
MANUAL_ONLY_POOLS = frozenset({"anthropic-api"})
MANUAL_ONLY_ERROR = "no automatic probe — manual observation only"
CREDIT_WINDOW = "credit"
# These are admission-coverage floors, not synthetic zero-valued buckets.
# Devin requires only the weekly window: the startup banner always proves the
# weekly remainder, while Daily exists only when /usage is readable — requiring
# it would refuse stale/manual snapshots that a fresh probe would admit.
REQUIRED_WINDOWS = {
    "claude": frozenset({"5h", "7d"}),
    "codex": frozenset({"5h", "7d"}),
    "devin": frozenset({"7d"}),
    "grok": frozenset({"7d"}),
    "kimi": frozenset({"5h", "7d"}),
    "kiro": frozenset({"30d"}),
    "clinepass": frozenset({"5h", "7d", "30d"}),
    "upstage": frozenset({"30d"}),
    "anthropic-api": frozenset({"credit"}),
}
# hk 1769 (b953) — codex required windows follow plan_type, which
# providers/codex.py already stores as ``plan``. 2026-10-20 wham usage API
# fleet evidence (hk 1767 report): plus (fp 18b9acc4) returns {5h, 7d},
# prolite (fc823b1d) returns {7d} only, and free (af2c560b) returns a single
# 30-day window (~2592000 s, the "30d" band in model.window_label). A plan
# absent from this table keeps the REQUIRED_WINDOWS {5h, 7d} floor and the
# caller warns so the table gets extended.
CODEX_PLAN_REQUIRED_WINDOWS = {
    "plus": frozenset({"5h", "7d"}),
    "prolite": frozenset({"7d"}),
    "free": frozenset({"30d"}),
}

_DURATION_PART = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([smhdw])", re.IGNORECASE)
_AUTH_FAILURE = re.compile(
    r"(?<!\d)(?:401|403)(?!\d)|auth(?:entication)?\s+fail|unauthori[sz]ed|forbidden|"
    r"(?:credential|token|api[ _-]?key).*(?:invalid|expired|missing|없음|유효하지)|"
    r"login.*(?:required|needed|필요)|인증\s*실패|자격증명|로그인이?\s*필요",
    re.I,
)
_RATE_OR_TRANSPORT = re.compile(
    r"(?<!\d)429(?!\d)|rate[ -]?limit|too many requests|timeout|timed out|"
    r"connection|transport|network|dns|unreachable|circuit open|urlopen|urlerror|errno|"
    r"temporarily unavailable|nodename|remote end|ssl|http\s+(?:408|5\d\d)|"
    r"시간\s*초과|끝나지\s*않음|그리지\s*않음|연결|네트워크|전송|"
    r"조회\s*실패|프로브\s*실행\s*실패",
    re.I,
)
_PARSE_FAILURE = re.compile(
    r"parse|parser|decode|malformed|json|format|expecting value|extra data|unterminated|"
    r"파싱|형식|찾지 못|유효 범위|no data|bucket|버킷|데이터\s*이상",
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
    from .cache import cache_path

    snapshot = cache_path()
    candidate = snapshot.parent / "manual.json"
    if candidate == snapshot:
        return snapshot.parent / "manual-observations.json"
    return candidate


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
    latest_by_key: dict[tuple[str, str], dict] = {}
    seen_observations: set[tuple[str, str, float, dt.datetime]] = set()
    current = dt.datetime.now(dt.UTC)
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
            if event.get("window") not in {"5h", "1d", "7d", "30d", CREDIT_WINDOW}:
                raise ManualError(f"manual history {index} window 불일치")
            if not _is_valid_used_pct(event.get("used_pct")):
                raise ManualError(f"manual history {index} used_pct 불일치")
            _validate_credit(event, index)
            measured = _parse_iso(event.get("measured_at"))
            entered = _parse_iso(event.get("entered_at"))
            expires = _parse_iso(event.get("expires_at"))
            if measured is None or entered is None or expires is None:
                raise ManualError(f"manual history {index} timestamp 불일치")
            if measured > current + dt.timedelta(seconds=MAX_FUTURE_SKEW_S) or entered > (
                current + dt.timedelta(seconds=MAX_FUTURE_SKEW_S)
            ):
                raise ManualError(f"manual history {index} future timestamp 불일치")
            observation_key = (
                event["pool"],
                event["window"],
                float(event["used_pct"]),
                measured,
            )
            if observation_key in seen_observations:
                raise ManualError(f"manual history {index} 동일 관측 재입력")
            seen_observations.add(observation_key)
            if measured > entered + dt.timedelta(seconds=MAX_FUTURE_SKEW_S):
                raise ManualError(f"manual history {index} future measurement 불일치")
            credit_policy = event.get("credit_ttl_policy")
            if event["window"] == CREDIT_WINDOW:
                if credit_policy is not None and credit_policy not in (
                    CREDIT_V1_TTL_POLICY,
                    CREDIT_V2_TTL_POLICY,
                ):
                    raise ManualError(f"manual history {index} credit TTL policy 불일치")
                # Pre-hk-2106 events had the general two-hour cap. Keep those
                # append-only records readable when their only new-policy
                # overrun is expires_on midnight; clamp their read view below.
                if credit_policy == CREDIT_V2_TTL_POLICY:
                    credit = event["credit"]
                    expires_on = _parse_date(credit["expires_on"])
                    assert expires_on is not None
                    expiry_bounds = [dt.datetime.combine(expires_on, dt.time.min, tzinfo=dt.UTC)]
                else:
                    ttl_limit_s = CREDIT_V1_MAX_TTL_S if credit_policy == CREDIT_V1_TTL_POLICY else MAX_TTL_S
                    expiry_bounds = [
                        measured + dt.timedelta(seconds=ttl_limit_s),
                        entered + dt.timedelta(seconds=ttl_limit_s),
                    ]
                    if credit_policy == CREDIT_V1_TTL_POLICY:
                        credit = event["credit"]
                        expires_on = _parse_date(credit["expires_on"])
                        assert expires_on is not None
                        expiry_bounds.append(dt.datetime.combine(expires_on, dt.time.min, tzinfo=dt.UTC))
            else:
                if "credit_ttl_policy" in event:
                    raise ManualError(f"manual history {index} credit TTL policy 불일치")
                ttl_limit_s = MAX_TTL_S
                expiry_bounds = [
                    measured + dt.timedelta(seconds=ttl_limit_s),
                    entered + dt.timedelta(seconds=ttl_limit_s),
                ]
            if expires > min(expiry_bounds):
                raise ManualError(f"manual history {index} expiry 불일치")
            if not isinstance(event.get("reason"), str) or not event["reason"].strip():
                raise ManualError(f"manual history {index} reason 불일치")
            if len(event["reason"]) > MAX_REASON_CHARS:
                raise ManualError(f"manual history {index} reason 길이 불일치")
            _validate_audit_fields(event, index)
            account_ref = event.get("account_ref")
            author_principal = event.get("author_principal")
            if (
                not isinstance(account_ref, dict)
                or account_ref.get("pool") != event.get("pool")
                or account_ref.get("host") != event["author"].get("host")
                or account_ref.get("verification") != SOURCE_VERIFICATION
                or account_ref.get("local_only") is not True
                or author_principal != event.get("author")
            ):
                raise ManualError(f"manual history {index} Q5 identity fields 불일치")
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
            if (
                event.get("local_only") is not True
                or event.get("usage_mode") != "admission_evidence"
                or event.get("evidence_ref") != "explicit-local-observation"
            ):
                raise ManualError(f"manual history {index} usage mode 불일치")
            supersedes = event.get("supersedes")
            if "supersedes_ref" not in event or event.get("supersedes_ref") != supersedes:
                raise ManualError(f"manual history {index} supersedes ref alias 불일치")
            key = (event["pool"], event["window"])
            prior = latest_by_key.get(key)
            expected_supersedes = None if prior is None else prior["manual_observation_id"]
            if supersedes != expected_supersedes:
                raise ManualError(f"manual history {index} supersedes ref 불일치")
            if prior is not None:
                prior_measured = _parse_iso(prior.get("measured_at"))
                prior_expires = _parse_iso(prior.get("expires_at"))
                if prior_measured is None or prior_expires is None:
                    raise ManualError(f"manual history {index} prior timestamp 불일치")
                if measured <= prior_measured and float(event["used_pct"]) == float(prior["used_pct"]):
                    raise ManualError(f"manual history {index} 동일 관측 재입력")
                if measured <= prior_measured and expires > prior_expires:
                    raise ManualError(f"manual history {index} correction expiry 불일치")
            known_entries[observation_id] = event
            latest_by_key[key] = event
        elif kind == "clear":
            clear_id = event.get("manual_clear_id")
            if not isinstance(clear_id, str) or not clear_id:
                raise ManualError(f"manual history {index} clear id 불일치")
            if not isinstance(event.get("pool"), str) or not event["pool"]:
                raise ManualError(f"manual history {index} clear pool 불일치")
            clear_entered = _parse_iso(event.get("entered_at"))
            if clear_entered is None:
                raise ManualError(f"manual history {index} clear timestamp 불일치")
            if clear_entered > current + dt.timedelta(seconds=MAX_FUTURE_SKEW_S):
                raise ManualError(f"manual history {index} clear future timestamp 불일치")
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
        normalized = WINDOW_ALIASES[window]
    except KeyError as exc:
        raise ManualError(f"지원하지 않는 window: {window}") from exc
    # hk 2079: the credit window is the manual-only pool's window and nothing
    # else's — a credit entry on a probed pool (or a rolling window on the
    # credit pool) could never satisfy that pool's REQUIRED_WINDOWS.
    if (normalized == CREDIT_WINDOW) != (pool in MANUAL_ONLY_POOLS):
        raise ManualError(
            f"{pool} 에는 {normalized} window 를 쓸 수 없습니다 "
            f"(credit window 는 {', '.join(sorted(MANUAL_ONLY_POOLS))} 전용)"
        )
    return normalized


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


def _parse_date(value: object) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def credit_used_pct(remaining_usd: float, total_usd: float) -> float:
    """Used share of a prepaid credit balance — remaining USD against total USD.

    This is the only derived number the credit window stores: the manual record
    keeps the two USD amounts and the expiry, and ``used_pct`` follows from them
    so the existing admission/cutoff path (which speaks percentages) needs no
    second shape. A zero remaining balance is 100% used (fail closed).
    """

    if not math.isfinite(remaining_usd) or not math.isfinite(total_usd):
        raise ManualError("credit 금액은 유한 숫자여야 합니다")
    if remaining_usd < 0 or total_usd <= 0 or remaining_usd > total_usd:
        raise ManualError("credit 은 0 이상 remaining <= total > 0 이어야 합니다")
    return (1.0 - remaining_usd / total_usd) * 100.0


def _credit(event: dict) -> dict | None:
    credit = event.get("credit")
    return credit if isinstance(credit, dict) else None


def _validate_credit(event: dict, index: int) -> None:
    """Validate the optional ``credit`` block (hk 2079).

    A credit window must carry it and the stored used_pct must be the value the
    USD numbers derive — a hand-edited percentage cannot disagree with the
    balance the operator entered. Every other window must not carry one.
    """

    credit = event.get("credit")
    # The credit window belongs to the manual-only pool and nothing else, in the
    # reader as in the writer (normalize_window): a hand-edited store cannot move it.
    if (event.get("window") == CREDIT_WINDOW) != (event.get("pool") in MANUAL_ONLY_POOLS):
        raise ManualError(f"manual history {index} credit pool 불일치")
    if event.get("window") != CREDIT_WINDOW:
        if credit is not None:
            raise ManualError(f"manual history {index} credit window 불일치")
        return
    if not isinstance(credit, dict):
        raise ManualError(f"manual history {index} credit 구조 불일치")
    remaining = credit.get("remaining_usd")
    total = credit.get("total_usd")
    expires = credit.get("expires_on")
    if (
        isinstance(remaining, bool)
        or not isinstance(remaining, (int, float))
        or isinstance(total, bool)
        or not isinstance(total, (int, float))
    ):
        raise ManualError(f"manual history {index} credit 금액 불일치")
    if _parse_date(expires) is None:
        raise ManualError(f"manual history {index} credit 만료일 불일치")
    expected = credit_used_pct(float(remaining), float(total))
    if abs(float(event.get("used_pct")) - expected) > 1e-9:
        raise ManualError(f"manual history {index} credit used_pct 불일치")


def credit_display(summary: dict | None, *, now: dt.datetime) -> str | None:
    """One human line for a fresh credit observation: balance and validity."""

    if not summary:
        return None
    entries = summary.get("latest_valid")
    if entries is None:
        entries = summary.get("entries") or []
    for entry in entries:
        credit = _credit(entry)
        if entry.get("window") != CREDIT_WINDOW or credit is None:
            continue
        expires = _parse_date(credit.get("expires_on"))
        if expires is None:
            continue
        valid_until = _parse_iso(entry.get("expires_at"))
        if valid_until is None:
            continue
        days = (expires - _as_utc(now).date()).days
        left = f"{days}일 남음" if days >= 0 else f"{-days}일 지남(만료)"
        remaining_s = (valid_until - _as_utc(now)).total_seconds()
        remaining = _format_remaining_validity(remaining_s)
        pool = str(entry.get("pool") or "")
        return (
            f"{pool} credit ${float(credit['remaining_usd']):.2f}/"
            f"${float(credit['total_usd']):.2f} · {left} (만료 {expires.isoformat()}) · "
            f"valid_until={_iso(valid_until)} ({remaining})"
            f"{' · legacy-clamped' if entry.get('legacy_clamped') else ''}"
        )
    return None


def _format_remaining_validity(seconds: float) -> str:
    if seconds <= 0:
        return "expired"
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    if hours:
        suffix = f" {minutes}m" if minutes else ""
        return f"in {hours}h{suffix}"
    return f"in {max(1, math.ceil(seconds / 60))}m"


def manual_only_result(pool: str) -> ProviderResult:
    """The synthetic automatic result for a pool that has no probe (hk 2079).

    ``resolve_result`` treats this as a retryable measurement failure so the
    manual observation is the only admission evidence; a missing or stale
    observation leaves the pool unmeasurable (fail closed), exactly like every
    other manual fallback.
    """

    if pool not in MANUAL_ONLY_POOLS:
        raise ManualError(f"{pool} 은 manual-only pool 이 아닙니다")
    return ProviderResult(
        id=pool,
        error=MANUAL_ONLY_ERROR,
        error_kind="manual_only",
        note="자동 프로브 없음 — 운영자가 console 값을 manual 로 입력한다",
        pool_class="spend",
    )


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


def _latest_set(history: list[dict], pool: str, window: str) -> dict | None:
    for event in reversed(history):
        if event.get("event") == "set" and event.get("pool") == pool and event.get("window") == window:
            return event
    return None


def record_observation(
    *,
    pool: str,
    used_pct: float | None = None,
    window: str | None,
    measured_at: dt.datetime,
    reason: str,
    ttl_s: float | None = None,
    resets_in_s: float | None = None,
    resets_at: dt.datetime | None = None,
    remaining_usd: float | None = None,
    total_usd: float | None = None,
    credit_expires_on: str | None = None,
    now: dt.datetime | None = None,
) -> dict:
    entered_at = _as_utc(now or dt.datetime.now(dt.UTC))
    measured_at = _as_utc(measured_at)
    if measured_at > entered_at + dt.timedelta(seconds=MAX_FUTURE_SKEW_S):
        raise ManualError("--measured-at 은 미래 시각일 수 없습니다")
    reason = reason.strip()
    if not reason:
        raise ManualError("--reason 은 비어 있을 수 없습니다")
    if len(reason) > MAX_REASON_CHARS:
        raise ManualError(f"--reason 은 {MAX_REASON_CHARS}자 이하여야 합니다")
    normalized_window = normalize_window(pool, window)
    is_credit = normalized_window == CREDIT_WINDOW
    if is_credit:
        if ttl_s is not None and (not math.isfinite(ttl_s) or ttl_s <= 0):
            raise ManualError("credit window --ttl 은 0보다 큰 유한값이어야 합니다")
    else:
        if ttl_s is None:
            ttl_s = DEFAULT_TTL_S
        if not math.isfinite(ttl_s) or ttl_s <= 0 or ttl_s > MAX_TTL_S:
            raise ManualError("--ttl 은 0보다 크고 2h 이하여야 합니다")
    credit: dict | None = None
    credit_expiry_start: dt.datetime | None = None
    if normalized_window == CREDIT_WINDOW:
        if used_pct is not None:
            raise ManualError("credit window 는 --used 대신 --remaining/--total 을 씁니다")
        if remaining_usd is None or total_usd is None or credit_expires_on is None:
            raise ManualError("credit window 는 --remaining·--total·--expires-on 이 모두 필요합니다")
        if resets_in_s is not None or resets_at is not None:
            raise ManualError(
                "credit window 는 --resets-in/--resets-at 을 쓰지 않습니다 (만료일은 --expires-on)"
            )
        expires = _parse_date(credit_expires_on)
        if expires is None:
            raise ManualError("--expires-on 은 YYYY-MM-DD 형식이어야 합니다")
        credit_expiry_start = dt.datetime.combine(expires, dt.time.min, tzinfo=dt.UTC)
        if entered_at >= credit_expiry_start:
            raise ManualError("--expires-on credit balance 는 입력 시각에 이미 만료됐습니다")
        used_pct = credit_used_pct(float(remaining_usd), float(total_usd))
        credit = {
            "remaining_usd": float(remaining_usd),
            "total_usd": float(total_usd),
            "expires_on": expires.isoformat(),
        }
    elif used_pct is None:
        raise ManualError("--used 는 필수입니다 (credit window 는 --remaining/--total)")
    elif any(value is not None for value in (remaining_usd, total_usd, credit_expires_on)):
        raise ManualError("--remaining/--total/--expires-on 은 --window credit 에서만 씁니다")
    if used_pct is None or not _is_valid_used_pct(used_pct):
        raise ManualError("used_pct 는 0..100 유한 숫자여야 합니다")
    if resets_in_s is not None and resets_at is not None:
        raise ManualError("--resets-in 과 --resets-at 은 함께 쓸 수 없습니다")
    if resets_in_s is not None:
        resets_at = measured_at + dt.timedelta(seconds=resets_in_s)
    if credit_expiry_start is not None:
        if ttl_s is None:
            expires_at = credit_expiry_start
        else:
            until_expiry_s = max(0.0, (credit_expiry_start - measured_at).total_seconds())
            expires_at = (
                credit_expiry_start if ttl_s >= until_expiry_s else measured_at + dt.timedelta(seconds=ttl_s)
            )
    else:
        assert ttl_s is not None
        expires_at = min(
            measured_at + dt.timedelta(seconds=ttl_s),
            entered_at + dt.timedelta(seconds=MAX_TTL_S),
        )
    if resets_at is not None:
        expires_at = min(expires_at, _as_utc(resets_at))

    with _store_lock(exclusive=True):
        store = _load_unlocked()
        history: list[dict] = store["history"]
        duplicate = any(
            event.get("event") == "set"
            and event.get("pool") == pool
            and event.get("window") == normalized_window
            and float(event.get("used_pct")) == float(used_pct)
            and _parse_iso(event.get("measured_at")) == measured_at
            for event in history
        )
        prior = _latest_set(history, pool, normalized_window)
        if prior is not None:
            prior_measured = _parse_iso(prior.get("measured_at"))
            prior_expires = _parse_iso(prior.get("expires_at"))
            if prior_measured is None or prior_expires is None:
                raise ManualError("이전 manual observation timestamp 불일치")
            if duplicate or (measured_at <= prior_measured and float(prior["used_pct"]) == float(used_pct)):
                raise ManualError("동일 관측값을 다시 넣으려면 이전 값보다 새 --measured-at 이 필요합니다")
            if measured_at <= prior_measured:
                expires_at = min(expires_at, prior_expires)
        observation_id = str(uuid.uuid4())
        supersedes = None if prior is None else str(prior["manual_observation_id"])
        author = _author()
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
            "author": author,
            "author_principal": author,
            "account_ref": {
                "pool": pool,
                "host": author["host"],
                "verification": SOURCE_VERIFICATION,
                "local_only": True,
            },
            "reason": reason,
            "evidence_ref": "explicit-local-observation",
            "expires_at": _iso(expires_at),
            "supersedes": supersedes,
            "supersedes_ref": supersedes,
            "source": SOURCE,
            "source_verification": SOURCE_VERIFICATION,
            "source_label": SOURCE_LABEL,
            "usage_mode": "admission_evidence",
            "local_only": True,
            "execution_context": _execution_context(),
        }
        if credit is not None:
            entry["credit"] = credit
            entry["credit_ttl_policy"] = CREDIT_V2_TTL_POLICY
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
    if not _has_usable_automatic_bucket(result):
        return result.note or "no data — automatic measurement has no usable bucket"
    return None


def classify_automatic_failure(result: ProviderResult) -> tuple[str | None, str | None]:
    if result.error_kind == "manual_only" or result.error == MANUAL_ONLY_ERROR:
        # hk 2079: there is no probe to fail — the manual observation is the
        # only measurement path, and the usual TTL/coverage rules still gate it.
        return "manual_only", MANUAL_ONLY_ERROR
    text = automatic_error_text(result)
    if text is None:
        return None, None
    safe_text = " ".join(text.split())[:200]
    evidence = safe_text
    if result.hint:
        evidence = f"{evidence} {' '.join(result.hint.split())[:200]}"
    if _AUTH_FAILURE.search(evidence):
        return "auth", safe_text
    if _RATE_OR_TRANSPORT.search(evidence):
        return "transport", safe_text
    if _PARSE_FAILURE.search(evidence):
        return "parse_error", safe_text
    return "unsupported", safe_text


def _auto_state(result: ProviderResult) -> tuple[float | None, bool, bool]:
    successful = result.error is None and result.warning is None and _has_usable_automatic_bucket(result)
    return (
        result.fetched_at,
        successful and not result.stale,
        successful,
    )


def _has_usable_automatic_bucket(result: ProviderResult) -> bool:
    return any(_is_valid_used_pct(bucket.used_pct) for bucket in result.buckets)


def _entry_views(
    store: dict,
    *,
    now: dt.datetime,
    auto_states: dict[str, tuple[float | None, bool, bool]] | None = None,
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
        credit = _credit(event)
        if credit is not None and expires is not None:
            expires_on = _parse_date(credit.get("expires_on"))
            if expires_on is not None:
                credit_expiry_start = dt.datetime.combine(expires_on, dt.time.min, tzinfo=dt.UTC)
                if expires > credit_expiry_start:
                    expires = credit_expiry_start
                    view["expires_at"] = _iso(expires)
                    view["legacy_clamped"] = True
        auto_fetched, auto_fresh, auto_success = auto_states.get(
            str(event.get("pool") or ""), (None, False, False)
        )
        auto_time = dt.datetime.fromtimestamp(auto_fetched, tz=dt.UTC) if auto_fetched is not None else None
        replacement = later_replacement.get(index)
        if replacement is not None:
            status, replacement_id = replacement
            view["replacement_ref"] = replacement_id
        elif auto_success and measured is not None and auto_time is not None and auto_time > measured:
            status = "superseded_by_auto"
        elif expires is None or expires <= current:
            status = "expired"
        elif (credit := _credit(event)) is not None and (
            (expires_on := _parse_date(credit.get("expires_on"))) is None or expires_on < current.date()
        ):
            # hk 2079: an expired credit balance is not admission evidence even
            # while the manual TTL is still running.
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


def normalize_codex_plan(plan: object) -> str | None:
    """plan_type as the wham API spells it — equality after normalising
    (strip + casefold), never substring."""
    if not isinstance(plan, str):
        return None
    normalized = plan.strip().casefold()
    return normalized or None


def unknown_codex_plan(result: ProviderResult | None) -> str | None:
    """The judged codex plan when the table does not know it (hk 1769).

    Returns the normalised plan value, ``"<absent>"`` for a missing or
    non-string plan_type, or None when the result is not codex or the plan
    has a table row. Naming the value lets the gate surface one warning so
    ``CODEX_PLAN_REQUIRED_WINDOWS`` gets extended.
    """
    if result is None or result.id != "codex":
        return None
    plan = normalize_codex_plan(result.plan)
    if plan in CODEX_PLAN_REQUIRED_WINDOWS:
        return None
    return plan if plan is not None else "<absent>"


def required_windows_for(result: ProviderResult | None) -> frozenset[str]:
    """Admission-coverage floor for one result (hk 1769).

    codex follows the stored plan_type: a plan in
    ``CODEX_PLAN_REQUIRED_WINDOWS`` uses its own row — a plan can never be
    asked for a window the API does not return for it, and a missing window
    that the plan does return is not waved through. Any absent or unlisted
    plan keeps the ``REQUIRED_WINDOWS`` {5h, 7d} floor byte-identically.
    """
    if result is None:
        return frozenset()
    if result.id == "codex":
        plan = normalize_codex_plan(result.plan)
        if plan in CODEX_PLAN_REQUIRED_WINDOWS:
            return CODEX_PLAN_REQUIRED_WINDOWS[plan]
    return REQUIRED_WINDOWS.get(result.id, frozenset())


def confirmed_automatic_cutoff(result: ProviderResult, *, now: dt.datetime) -> tuple[float, float] | None:
    """Return an automatic account-bucket breach, including warning results."""
    from .policy import get_cutoff, get_policy
    from .recommend import PRESERVE_EXCLUDE_PCT, SPEND_EXCLUDE_PCT

    fallback_class = result.pool_class if result.pool_class in {"preserve", "spend"} else "preserve"
    effective_class = get_policy(result.id, fallback_class, today=_as_utc(now).date())[0]
    if effective_class == "exclude":
        return None
    # task #638: pool-level ``cutoff`` config applies here too — the manual
    # fallback guard must honor the same threshold the gate enforces.
    cutoff, _cutoff_status = get_cutoff(
        result.id, SPEND_EXCLUDE_PCT if effective_class == "spend" else PRESERVE_EXCLUDE_PCT
    )
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
    if window in {"30d", CREDIT_WINDOW}:
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
    if result.status == "ok" and not result.stale and _has_usable_automatic_bucket(result):
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

    confirmed_cutoff = confirmed_automatic_cutoff(result, now=now)
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
    allowed_failures = {"transport", "parse_error"}
    if failure_kind == "manual_only" and result.id in MANUAL_ONLY_POOLS:
        allowed_failures.add("manual_only")
    if failure_kind not in allowed_failures:
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
    required = required_windows_for(result)
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
    credit = credit_display({"latest_valid": list(selected)}, now=now)
    buckets = [
        Bucket(
            label=f"manual {entry['window']}",
            window=entry["window"],
            used_pct=entry["used_pct"],
            resets_at=entry.get("resets_at"),
            scope=Scope("account"),
            horizon=_horizon(entry["window"]),  # type: ignore[arg-type]
            note=f"source={SOURCE}; {SOURCE_LABEL}" + (f"; {credit}" if credit else ""),
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


def _automatic_states_from_cache(now: dt.datetime) -> dict[str, tuple[float | None, bool, bool]]:
    from .cache import DEFAULT_TTL_S as AUTO_DEFAULT_TTL_S
    from .cache import PROVIDER_TTL_S, cache_path

    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    states: dict[str, tuple[float | None, bool, bool]] = {}
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
        payload = entry.get("result")
        buckets = payload.get("buckets") if isinstance(payload, dict) else None
        successful = (
            isinstance(payload, dict)
            and not payload.get("error")
            and not payload.get("warning")
            and isinstance(buckets, list)
            and any(
                isinstance(bucket, dict) and _is_valid_used_pct(bucket.get("used_pct")) for bucket in buckets
            )
        )
        ttl = PROVIDER_TTL_S.get(str(pool), AUTO_DEFAULT_TTL_S)
        states[str(pool)] = (fetched, successful and current_epoch - fetched <= ttl, successful)
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
        credit = _credit(entry)
        credit_s = (
            f" credit_remaining_usd={credit['remaining_usd']:g}"
            f" credit_total_usd={credit['total_usd']:g}"
            f" credit_expires_on={credit['expires_on']}"
            if credit is not None
            else ""
        )
        remaining_effect_s = entry.get("remaining_effect_s")
        validity_s = (
            f" valid_until={entry['expires_at']} ({_format_remaining_validity(float(remaining_effect_s))})"
            if credit is not None and remaining_effect_s is not None
            else ""
        )
        lines.append(
            f"{entry['pool']} window={entry['window']} used_pct={entry['used_pct']:g} "
            f"status={entry['status']} measured_at={entry['measured_at']} "
            f"expires_at={entry['expires_at']}{credit_s}{validity_s} "
            f"{'legacy-clamped=true ' if entry.get('legacy_clamped') else ''}source={SOURCE} "
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
