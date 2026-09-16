"""Probe child process-group registry, cwd-based leftover sweep, and orphan reaper.

프로브는 CLI/PTY 자식을 전용 세션(start_new_session=True)에 띄우므로 refresh
워커 자기 그룹에 대한 killpg 가 그 자식에 닿지 않는다. 세 가지 장치로 고아를 막는다:

- 레지스트리: provider 가 띄운 자식의 pgid 를 모듈 수준에 등록한다.
  refresh 타임아웃 핸들러가 os._exit 전에 등록된 그룹을 killpg 한다.
- 선제 스윕: 프로브 시작 전 workdir 을 cwd 로 쓰는 잔존 프로세스를 정리한다.
- 리퍼: 분리된 감시 프로세스가 부모 사망(ppid→1)을 폴링으로 감지해 workdir 을
  스윕한다 — 부모가 SIGKILL/SIGHUP 로 죽어 파이썬 정리 경로가 전혀 못 도는 경우의
  최종 방어선이다.

판별자는 오직 cwd 다. 나이·CPU·이름으로 고르지 않는다 — 장수 정상 워커를
죽이지 않기 위해서다(2026-09-16 실측: 정상 작업 세션이 이틀 이상 도는 경우가 있다).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path

# 프로브 자식(전용 세션 리더)의 pgid 들. 같은 프로세스 안에서만 유효하다 —
# 부모 사망 시 이 테이블도 같이 죽으므로 그 경우는 리퍼/스윕이 담당한다.
_PGROUPS: set[int] = set()

_LSOF_TIMEOUT_S = 10.0
_REAPER_SWEEP_PASSES = 4
_REAPER_SWEEP_GAP_S = 0.4


def register(pgid: int) -> None:
    """Register a probe child's process group for timeout-path cleanup."""

    _PGROUPS.add(pgid)


def unregister(pgid: int) -> None:
    """Drop a pgid once the child has been reaped by the normal finally path."""

    _PGROUPS.discard(pgid)


def registered() -> frozenset[int]:
    return frozenset(_PGROUPS)


def kill_registered(grace_s: float = 0.2) -> None:
    """SIGTERM then SIGKILL every registered process group. Best effort."""

    pgids = list(_PGROUPS)
    for pgid in pgids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
    if grace_s > 0:
        time.sleep(grace_s)
    for pgid in pgids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)


def pids_with_cwd(workdir: Path) -> list[int]:
    """Pids whose current working directory is exactly ``workdir`` (lsof)."""

    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv; path is a directory we own
            [lsof, "-a", "-d", "cwd", "-t", "--", str(workdir)],
            capture_output=True,
            text=True,
            timeout=_LSOF_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(token) for token in out.stdout.split() if token.strip().isdigit()]


def kill_leftovers_at_cwd(workdir: Path, *, exclude: Iterable[int] = ()) -> list[int]:
    """SIGKILL every process whose cwd is ``workdir``. Returns the killed pids.

    Per-pid kill only — never killpg here: a matched pid may share its group
    with unrelated processes, and the discriminator must stay exactly cwd.
    """

    excluded = {os.getpid(), *exclude}
    killed: list[int] = []
    for pid in pids_with_cwd(workdir):
        if pid in excluded:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
    return killed


def spawn_reaper(
    workdir: Path,
    ttl_s: float,
    *,
    parent_pid: int | None = None,
) -> subprocess.Popen[bytes] | None:
    """Launch a detached reaper that sweeps ``workdir`` if the parent dies.

    Returns the reaper Popen, or None if it could not be started (probe still
    works; protection degrades to registry + next-probe sweep).
    """

    watched = os.getpid() if parent_pid is None else parent_pid
    # 설치본/소스 트리 양쪽에서 import 되도록 scopefuel 패키지 루트를 심는다.
    src_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src_root) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        return subprocess.Popen(  # noqa: S603 - sys.executable -m <our module>, fixed argv
            [sys.executable, "-m", "scopefuel.proctrack", str(watched), str(workdir), repr(ttl_s)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd="/",
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        return None


def _parent_alive(parent_pid: int) -> bool:
    if os.getppid() == 1:  # orphaned → reparented to launchd/init
        return False
    try:
        os.kill(parent_pid, 0)
    except OSError:  # dead, or pid reused by a process we cannot signal
        return False
    return True


def reaper_main(parent_pid: int, workdir: Path, ttl_s: float, interval_s: float = 1.0) -> int:
    """Watch ``parent_pid``; on its death SIGKILL every process in ``workdir``.

    Expires quietly after ``ttl_s`` while the parent is alive — the reaper's
    whole job is the parent-death window, nothing else.
    """

    deadline = time.monotonic() + ttl_s
    while time.monotonic() < deadline:
        if not _parent_alive(parent_pid):
            break
        time.sleep(interval_s)
    else:
        return 0
    for attempt in range(_REAPER_SWEEP_PASSES):
        if not kill_leftovers_at_cwd(workdir) and attempt:
            break
        time.sleep(_REAPER_SWEEP_GAP_S)
    return 0


if __name__ == "__main__":
    # python -m scopefuel.proctrack <parent_pid> <workdir> <ttl_s>
    raise SystemExit(reaper_main(int(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])))
