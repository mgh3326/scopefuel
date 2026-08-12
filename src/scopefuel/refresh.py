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

from . import cache
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
    """Kill the worker and every CLI/PTY child in its dedicated session."""

    pgid = os.getpgrp()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    time.sleep(0.2)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
    os._exit(124)


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
            fetcher = fetchers[pool]
            result = _fetch(fetcher, pool)
            if result.error or result.warning:
                detail = result.error or result.warning
                print(f"refresh: pool={pool} failed: {detail}", file=sys.stderr)
                return 1
            now = time.time()
            result.id = pool
            if pool_class := getattr(fetcher, "pool_class", None):
                result.pool_class = pool_class
            result.fetched_at = now
            result.age_s = 0.0
            result.stale = False
            cache.update_entry(pool, result, now)
            print(f"refresh: pool={pool} updated fetched_at={now:.6f}", flush=True)
            return 0
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)


def _fetch(fetcher: object, pool: str) -> ProviderResult:
    try:
        result = fetcher()  # type: ignore[operator]
    except Exception as exc:
        return ProviderResult(id=pool, error=f"fetch failed ({type(exc).__name__})")
    if not isinstance(result, ProviderResult):
        return ProviderResult(id=pool, error="fetcher returned an invalid result")
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
