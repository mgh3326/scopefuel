"""Event-driven, single-pool cache refresh with kernel-backed locks."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import os
import pathlib
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

from . import cache, proctrack, quota_share, quota_v2, recommend
from .http import classify_error
from .model import PROBE_IN_PROGRESS, SWE2_BACKOFF, SWE2_IN_FLIGHT_TIMEOUT, ProviderResult
from .providers import BUILTIN

# Sorted so argparse choices stay stable in `scopefuel refresh --help`.
REFRESH_POOLS = tuple(sorted(BUILTIN))
DEFAULT_TIMEOUT_S = 60.0
LOCK_DIR_NAME = "refresh-locks"
LOG_DIR_NAME = "refresh-logs"

# hk 1754 — the --_swe2 worker reports why it skipped on these exit codes so
# the parent can tell a skip apart from a run without parsing free text.
# Plain `refresh` (swe2=False) keeps rc 0 for every skip.
SWE2_RC_LOCK_HELD = 10
SWE2_RC_PROBE_IN_PROGRESS = 11
SWE2_RC_BACKOFF = 12
SWE2_WAIT_SKIP_RCS = frozenset({SWE2_RC_LOCK_HELD, SWE2_RC_PROBE_IN_PROGRESS})

# Bound for waiting on a probe another worker already holds in flight: the
# in-flight worker's own hard cap is _timeout_seconds() (a devin fetch runs
# two sequential probe phases under one lock, so the provider TIMEOUT_S alone
# under-covers it), and the margin covers teardown plus the cache write after
# the probe exits.
SWE2_IN_FLIGHT_WAIT_MARGIN_S = 5.0
SWE2_IN_FLIGHT_POLL_S = 0.25  # cache-file poll only — never a network call


def _in_flight_wait_s() -> float:
    return _timeout_seconds() + SWE2_IN_FLIGHT_WAIT_MARGIN_S


def _refresh_dir(name: str) -> pathlib.Path:
    return cache.cache_dir() / name


def lock_path(pool: str) -> pathlib.Path:
    return _refresh_dir(LOCK_DIR_NAME) / f"{pool}.lock"


def log_path(pool: str) -> pathlib.Path:
    return _refresh_dir(LOG_DIR_NAME) / f"{pool}.log"


@contextmanager
def pool_lock(pool: str) -> Iterator[bool]:
    """Try an exclusive advisory lock; a dead owner releases it automatically."""

    path = lock_path(pool)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _timeout_seconds() -> float:
    raw = os.environ.get("SCOPEFUEL_REFRESH_TIMEOUT_S")
    if raw is None:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


def _kill_process_group_on_timeout(_signum: int, _frame: object) -> None:
    """Kill registered probe child groups, then the worker's own group.

    Probe children run in dedicated sessions (start_new_session), so killpg on
    this process's group cannot reach them; providers register each child pgid
    in proctrack and this handler signals those groups' members first — only
    members whose cwd is still inside the registered probe dir at signal time.
    """

    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    proctrack.kill_registered()
    pgid = os.getpgrp()
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    time.sleep(0.2)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
    os._exit(124)


def _v2_capture(pool: str, now: float) -> dict:
    """task #578 shadow 기록 — 실패해도 refresh 결과·rc 에 영향 0."""
    try:
        return quota_v2.capture_identities([pool], now)
    except Exception:
        return {}


def _v2_record(pool: str, result, started_at: float, completed_at: float, identities: dict) -> None:
    if not identities:
        return
    with contextlib.suppress(Exception):
        quota_v2.record_attempts({pool: (result, completed_at)}, started_at=started_at, identities=identities)


def run_worker(fetchers: dict[str, object], pool: str, *, swe2: bool = False) -> int:
    """Fetch one pool and merge only that pool into the cache."""

    if pool not in REFRESH_POOLS:
        print(f"refresh: unknown pool: {pool}", file=sys.stderr)
        return 2

    fetcher = fetchers[pool]
    # hk 2114 — refresh is another registry-fetcher caller, so it must honour
    # the same never-probe rule as collect/gate/recommend/status.
    if reason := cache._pool_skip_reason(pool, cache._effective_class(pool, fetcher), None):
        print(f"refresh: pool={pool} {reason} — skipped")
        return 0

    with pool_lock(pool) as acquired:
        if not acquired:
            print(f"refresh: pool={pool} already in progress; skipped")
            return SWE2_RC_LOCK_HELD if swe2 else 0

        signal.signal(signal.SIGALRM, _kill_process_group_on_timeout)
        signal.setitimer(signal.ITIMER_REAL, _timeout_seconds())
        try:
            now = time.time()
            remaining = cache.backoff_remaining(pool, now)
            if remaining > 0:
                print(f"refresh: pool={pool} backoff 중 — {remaining:.0f}s 뒤 허용, 네트워크 호출 생략")
                return SWE2_RC_BACKOFF if swe2 else 0
            v2_identities = _v2_capture(pool, now)
            started_at = now
            result = _fetch(fetcher, pool)
            completed_at = time.time()
            if result.error_kind == PROBE_IN_PROGRESS:
                # provider 의 single-probe 잠금을 다른 프로브가 쥐고 있다 —
                # 측정 실패가 아니므로 캐시·v2 감사도 남기지 않는다(#639).
                print(f"refresh: pool={pool} probe in progress; skipped")
                return SWE2_RC_PROBE_IN_PROGRESS if swe2 else 0
            usable_models = (
                swe2
                and pool == "devin"
                and recommend._swe2_probe_is_current(result, dt.datetime.now(dt.UTC))
                and any(
                    recommend._swe2_model_bucket(result, rung) is not None
                    for rung in ("swe-2-high", "swe-2-medium", "swe-2-max")
                )
            )
            if result.error or (result.warning and not usable_models):
                detail = result.error or result.warning
                cache.record_failure(pool, result, now)
                _v2_record(pool, result, started_at, completed_at, v2_identities)
                print(
                    f"refresh: pool={pool} failed: status={result.http_status or '-'} "
                    f"kind={result.error_kind or '-'} detail={detail}",
                    file=sys.stderr,
                )
                return 1
            now = time.time()
            result.id = pool
            if pool_class := getattr(fetcher, "pool_class", None):
                result.pool_class = pool_class
            result.fetched_at = now
            result.age_s = 0.0
            result.stale = False
            cache.update_entry(pool, result, now)
            _v2_record(pool, result, started_at, completed_at, v2_identities)
            # task #654 — 정상 측정의 정제 스냅샷을 hk 에 게시한다(fail-open).
            with contextlib.suppress(Exception):
                quota_share.publish_result(pool, result, now=now)
            print(f"refresh: pool={pool} updated fetched_at={now:.6f}", flush=True)
            return 0
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)


def _fetch(fetcher: object, pool: str) -> ProviderResult:
    try:
        result = fetcher()  # type: ignore[operator]
    except Exception as exc:
        kind, status, retry_after = classify_error(exc)
        return ProviderResult(
            id=pool,
            error=str(exc),
            error_kind=kind,
            http_status=status,
            retry_after_s=retry_after,
        )
    if not isinstance(result, ProviderResult):
        return ProviderResult(id=pool, error="fetcher returned an invalid result", error_kind="unknown")
    return result


def spawn(pool: str, *, background: bool) -> int:
    """Run the worker in a dedicated session; optionally return immediately."""

    command = [sys.executable, "-m", "scopefuel.cli", "refresh", pool, "--_worker"]
    if background:
        path = log_path(pool)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as log_file:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                close_fds=True,
                start_new_session=True,
            )
        print(f"refresh: pool={pool} started in background")
        return 0

    return subprocess.run(command, check=False).returncode


def _admitted_landing(entry: dict, before_fetched: float | None, now: float) -> ProviderResult | None:
    """An in-flight landing counts only when strictly newer than the pre-wait entry."""
    fetched = entry.get("fetched_at")
    if not isinstance(fetched, (int, float)) or fetched > now:
        return None
    # The entry the gate saw before waiting never admits; the landing must be
    # a genuinely newer fetch.
    if isinstance(before_fetched, (int, float)) and fetched <= before_fetched:
        return None
    failed = entry.get("last_error_at")
    if isinstance(failed, (int, float)) and failed >= fetched:
        return None
    result = cache._from_entry(entry, "devin", now)
    if result.error:
        return None
    ttl = cache.PROVIDER_TTL_S.get("devin", cache.DEFAULT_TTL_S)
    if result.age_s is None or result.age_s > ttl:
        return None
    result.stale = False
    return result


def _wait_for_in_flight(before_fetched: float | None, now: float | None) -> ProviderResult:
    """Poll the cache for the in-flight probe's landing; bounded, no network calls."""
    wait_s = _in_flight_wait_s()
    deadline = time.monotonic() + wait_s
    while True:
        at = time.time() if now is None else now
        # Backoff wins over waiting — whether it was already on disk when the
        # worker raced the lock, or the in-flight probe just recorded a 429.
        if cache.backoff_remaining("devin", at) > 0:
            return _backoff_refusal()
        entry = cache._load().get("devin")
        if isinstance(entry, dict):
            result = _admitted_landing(entry, before_fetched, at)
            if result is not None:
                return result
        if time.monotonic() >= deadline:
            return ProviderResult(
                id="devin",
                error=(f"in-flight probe did not land in time (waited up to {wait_s:.0f}s)"),
                error_kind=SWE2_IN_FLIGHT_TIMEOUT,
            )
        time.sleep(SWE2_IN_FLIGHT_POLL_S)


def _backoff_refusal() -> ProviderResult:
    """backoff 스킵은 기다리지 않는다 — named 사유에 남은 초를 싣는다."""
    remaining = cache.backoff_remaining("devin", time.time())
    return ProviderResult(
        id="devin",
        error=f"backoff 중, {remaining:.0f}s 뒤 재시도 가능",
        error_kind=SWE2_BACKOFF,
    )


def refresh_swe2_result(now: float | None = None) -> ProviderResult | None:
    """Run the bounded worker once, wait for an in-flight probe, then read the cache."""
    command = [sys.executable, "-m", "scopefuel.cli", "refresh", "devin", "--_worker", "--_swe2"]
    started = time.time()
    before = cache._load().get("devin")
    before_fetched = before.get("fetched_at") if isinstance(before, dict) else None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        code = process.wait(timeout=_timeout_seconds() + 5)
    except subprocess.TimeoutExpired:
        # Only signal the session we created; never a name/pattern-matched process.
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            process.wait(timeout=5)
        return None
    if code == SWE2_RC_BACKOFF:
        return _backoff_refusal()
    if code in SWE2_WAIT_SKIP_RCS:
        # A probe is already in flight and will write a fresh entry shortly —
        # waiting for it is the bounded synchronous refresh, not a retry.
        return _wait_for_in_flight(before_fetched, now)
    if code != 0:
        return None
    now = time.time() if now is None else now
    entry = cache._load().get("devin")
    if not isinstance(entry, dict):
        return None
    # An older cache hit is not evidence that this refresh measured the models
    # list — the swe2 worker's skip exit codes above are how a skip is told
    # apart from a run.
    fetched = entry.get("fetched_at")
    if not isinstance(fetched, (int, float)) or not started <= fetched <= now:
        return None
    failed = entry.get("last_error_at")
    if isinstance(failed, (int, float)) and failed >= fetched:
        return None
    result = cache._from_entry(entry, "devin", now)
    if result.error:
        return None
    ttl = cache.PROVIDER_TTL_S.get("devin", cache.DEFAULT_TTL_S)
    if result.age_s is None or result.age_s > ttl:
        return None
    result.stale = False
    return result
