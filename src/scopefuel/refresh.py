"""Event-driven, single-pool cache refresh with kernel-backed locks."""

from __future__ import annotations

import contextlib
import fcntl
import os
import pathlib
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

from . import cache, proctrack, quota_v2
from .http import classify_error
from .model import ProviderResult
from .providers import BUILTIN

# Sorted so argparse choices stay stable in `scopefuel refresh --help`.
REFRESH_POOLS = tuple(sorted(BUILTIN))
DEFAULT_TIMEOUT_S = 60.0
LOCK_DIR_NAME = "refresh-locks"
LOG_DIR_NAME = "refresh-logs"


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


def run_worker(fetchers: dict[str, object], pool: str) -> int:
    """Fetch one pool and merge only that pool into the cache."""

    if pool not in REFRESH_POOLS:
        print(f"refresh: unknown pool: {pool}", file=sys.stderr)
        return 2

    with pool_lock(pool) as acquired:
        if not acquired:
            print(f"refresh: pool={pool} already in progress; skipped")
            return 0

        signal.signal(signal.SIGALRM, _kill_process_group_on_timeout)
        signal.setitimer(signal.ITIMER_REAL, _timeout_seconds())
        try:
            now = time.time()
            remaining = cache.backoff_remaining(pool, now)
            if remaining > 0:
                print(f"refresh: pool={pool} backoff 중 — {remaining:.0f}s 뒤 허용, 네트워크 호출 생략")
                return 0
            fetcher = fetchers[pool]
            v2_identities = _v2_capture(pool, now)
            started_at = now
            result = _fetch(fetcher, pool)
            completed_at = time.time()
            if result.error or result.warning:
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
