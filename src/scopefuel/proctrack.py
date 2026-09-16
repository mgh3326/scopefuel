"""Probe child process-group registry, cwd-based leftover sweep, and orphan reaper.

프로브는 CLI/PTY 자식을 전용 세션(start_new_session=True)에 띄우므로 refresh
워커 자기 그룹에 대한 killpg 가 그 자식에 닿지 않는다. 네 가지 장치로 고아를 막는다:

- 인스턴스 디렉터리: 프로브마다 workdir 아래 고유 디렉터리(``probe-*``)를
  만들고 ``.owner`` 파일에 flock 을 쥔다. 자식의 cwd 는 이 디렉터리다.
  락은 커널이 소유자 사망 시 자동 해제하므로 PID 재사용에 무관하게
  "살아있는 프로브"를 식별한다.
- 선제 스윕: 프로브 시작 전 workdir 루트의 cwd 잔존자와, owner 락이 풀린
  (죽은 프로브의) 인스턴스 디렉터리 안의 잔존자만 정리한다. 락이 잡힌
  디렉터리 — 살아있는 동시 프로브 — 에는 절대 닿지 않는다.
- 레지스트리: provider 가 띄운 자식의 pgid 와 기대 cwd 를 등록한다.
  refresh 타임아웃 핸들러가 os._exit 전에 등록된 그룹을 killpg 한다.
- 리퍼: 분리된 감시 프로세스가 부모 사망(ppid→1)을 폴링으로 감지해 자기
  인스턴스 디렉터리만 스윕한다 — 부모가 SIGKILL/SIGHUP 로 죽어 파이썬
  정리 경로가 전혀 못 도는 경우의 최종 방어선이다.

판별자는 오직 cwd 다(실제 경로 realpath 기준 — 심볼릭 링크 경유도 같은
디렉터리로 해소되면 안에 있는 것이다). 나이·CPU·이름으로 고르지 않는다 —
장수 정상 워커를 죽이지 않기 위해서다(2026-09-16 실측: 정상 작업 세션이
이틀 이상 도는 경우가 있다).

모든 신호 경로는 TOCTOU 를 막기 위해 **신호 직전에 대상을 재판별**한다.
열거된 pid/pgid 목록은 기록일 뿐이고, 실행 조건은 kill/killpg 바로 전에
대상의 cwd(그룹이면 그룹원의 cwd)를 다시 읽어 확인하는 것이다. 그 사이
죽거나 다른 곳으로 옮긴 대상 — 재사용된 PID/pgid 포함 — 에는 신호가
가지 않는다.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

# 프로브 자식(전용 세션 리더)의 pgid → 기대 cwd(자기 인스턴스 디렉터리).
# 같은 프로세스 안에서만 유효하다 — 부모 사망 시 이 테이블도 같이 죽으므로
# 그 경우는 리퍼/스윕이 담당한다.
_PGROUPS: dict[int, Path] = {}

_INSTANCE_PREFIX = "probe-"
_PENDING_PREFIX = ".probe-pending-"
_OWNER_LOCK = ".owner"

_LSOF_TIMEOUT_S = 10.0
_REAPER_SWEEP_PASSES = 4
_REAPER_SWEEP_GAP_S = 0.4


def register(pgid: int, cwd: Path) -> None:
    """Register a probe child's process group and the cwd it is expected to keep."""

    _PGROUPS[pgid] = Path(cwd)


def unregister(pgid: int) -> None:
    """Drop a pgid once the child has been reaped by the normal finally path."""

    _PGROUPS.pop(pgid, None)


def registered() -> frozenset[int]:
    return frozenset(_PGROUPS)


def _resolve(path: Path | str) -> Path:
    """realpath — symlink hops land on the real directory, so they count as inside."""

    return Path(os.path.realpath(path))


def _matches(cwd: Path, target: Path, nested: bool) -> bool:
    return cwd == target or (nested and cwd.is_relative_to(target))


def _cwds(pids: Iterable[int] | None = None) -> dict[int, str]:
    """pid → cwd 경로(lsof -Fn). 죽었거나 읽을 수 없는 pid 는 빠진다."""

    if pids is not None:
        pids = list(pids)
        if not pids:
            return {}
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    argv = [lsof, "-a", "-d", "cwd", "-Fn"]
    if pids is not None:
        argv += ["-p", ",".join(str(pid) for pid in pids)]
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv; numeric pid list only
            argv, capture_output=True, text=True, timeout=_LSOF_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    found: dict[int, str] = {}
    current: int | None = None
    for line in out.stdout.splitlines():
        if line.startswith("p"):
            current = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith("n") and current is not None:
            found[current] = line[1:]
    return found


def _cwd_of(pid: int) -> Path | None:
    """그 pid 의 cwd 를 지금 다시 읽는다 — 신호 직전 재판별에 쓰는 유일한 소스."""

    raw = _cwds([pid]).get(pid)
    return _resolve(raw) if raw else None


def pids_with_cwd(
    target: Path,
    *,
    nested: bool = False,
    pids: Iterable[int] | None = None,
) -> list[int]:
    """Pids whose cwd resolves to ``target`` (or inside it when ``nested``)."""

    want = _resolve(target)
    return [pid for pid, raw in _cwds(pids).items() if _matches(_resolve(raw), want, nested)]


def _pgid_members(pgid: int) -> list[int]:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            ["ps", "-eo", "pid=,pgid="],
            capture_output=True,
            text=True,
            timeout=_LSOF_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    members = []
    for line in out.stdout.splitlines():
        cols = line.split()
        if len(cols) == 2 and cols[0].isdigit() and cols[1].isdigit() and int(cols[1]) == pgid:
            members.append(int(cols[0]))
    return members


def _group_matches(pgid: int, expected: Path) -> bool:
    """그룹 안에 ``expected`` 안에 cwd 를 둔 프로세스가 아직 있는가.

    등록 시점과 신호 시점 사이의 pgid 재사용을 막는 killpg 직전 재판별이다.
    기대 디렉터리 안에 있는 그룹원이 하나도 없으면 그 그룹은 우리 것이 아니다.
    """

    want = _resolve(expected)
    for pid in {pgid, *_pgid_members(pgid)}:
        cwd = _cwd_of(pid)
        if cwd is not None and _matches(cwd, want, nested=True):
            return True
    return False


def kill_registered(grace_s: float = 0.2) -> None:
    """SIGTERM then SIGKILL every registered group — each re-verified before signaling."""

    groups = list(_PGROUPS.items())
    for pgid, expected in groups:
        if _group_matches(pgid, expected):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGTERM)
    if grace_s > 0:
        time.sleep(grace_s)
    for pgid, expected in groups:
        if _group_matches(pgid, expected):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)


def kill_leftovers_at_cwd(
    target: Path,
    *,
    nested: bool = False,
    exclude: Iterable[int] = (),
) -> list[int]:
    """SIGKILL every process whose cwd resolves inside ``target``.

    Per-pid kill only — never killpg here: a matched pid may share its group
    with unrelated processes, and the discriminator must stay exactly cwd.

    The enumeration is a record only: each pid's cwd is re-read immediately
    before its signal, and the signal fires only when it still resolves
    inside ``target``. A pid that exited or moved since enumeration is
    skipped, so a reused pid can never take the signal.
    """

    want = _resolve(target)
    excluded = {os.getpid(), *exclude}
    killed: list[int] = []
    for pid in pids_with_cwd(target, nested=nested):
        if pid in excluded:
            continue
        cwd = _cwd_of(pid)
        if cwd is None or not _matches(cwd, want, nested):
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
    return killed


def new_probe_dir(workdir: Path) -> tuple[Path, int]:
    """Create a unique instance dir under ``workdir`` and take its owner lock.

    Returns ``(instance_dir, lock_fd)``. The caller holds ``lock_fd`` for the
    whole probe and closes it only after the probe child is reaped — a
    released lock is how sweepers learn the owner died. The fd is
    non-inheritable and children spawn ``close_fds=True``, so only the probe
    parent ever holds it. A lock that cannot be taken fails the probe
    closed: an unprotected instance dir would look stale to sweepers.

    Ordering is the safety invariant: the dir is created under a pending
    name sweepers never match, ``.owner`` is flocked, and only then is it
    atomically renamed to its final ``probe-*`` name. A dir visible to a
    sweep therefore always has its lock held — there is no window where a
    still-starting probe's dir looks stale and gets removed.
    """

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    pending = Path(tempfile.mkdtemp(prefix=_PENDING_PREFIX, dir=workdir))
    fd = os.open(pending / _OWNER_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        shutil.rmtree(pending, ignore_errors=True)
        raise
    instance = workdir / f"{_INSTANCE_PREFIX}{pending.name.removeprefix(_PENDING_PREFIX)}"
    try:
        os.rename(pending, instance)
    except OSError:
        os.close(fd)
        shutil.rmtree(pending, ignore_errors=True)
        raise
    return instance, fd


def _instance_dirs(workdir: Path) -> list[Path]:
    with contextlib.suppress(OSError):
        return [
            d
            for d in workdir.iterdir()
            if d.is_dir() and not d.is_symlink() and d.name.startswith(_INSTANCE_PREFIX)
        ]
    return []


def _instance_live(instance: Path) -> bool | None:
    """True = owner lock held (live probe); False = released (stale);
    None = indeterminate (no lock file — never swept, never removed)."""

    lock = instance / _OWNER_LOCK
    if not lock.exists():
        return None
    try:
        fd = os.open(lock, os.O_RDWR)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def kill_stale_probe_leftovers(workdir: Path, *, exclude: Iterable[int] = ()) -> list[int]:
    """Sweep leftovers under ``workdir`` before a probe starts.

    Kills (a) processes whose cwd is exactly ``workdir`` — no live probe
    uses the root itself, only legacy orphans — and (b) everything inside
    instance dirs whose owner lock is released, then removes those dirs.
    Instance dirs still locked by a live probe, dirs without an owner lock,
    and anything outside ``workdir`` are never touched — one probe's
    cleanup cannot reach another probe's living child.
    """

    workdir = Path(workdir)
    killed = kill_leftovers_at_cwd(workdir, nested=False, exclude=exclude)
    for instance in _instance_dirs(workdir):
        if _instance_live(instance) is not False:
            continue
        killed += kill_leftovers_at_cwd(instance, nested=True, exclude=exclude)
        shutil.rmtree(instance, ignore_errors=True)
    return killed


def spawn_reaper(
    target_dir: Path,
    ttl_s: float,
    *,
    parent_pid: int | None = None,
) -> subprocess.Popen[bytes] | None:
    """Launch a detached reaper that sweeps ``target_dir`` if the parent dies.

    ``target_dir`` is this probe's own instance dir — the reaper never looks
    at the shared workdir or a sibling probe's dir. Returns the reaper
    Popen, or None if it could not be started (probe still works;
    protection degrades to registry + next-probe sweep).
    """

    watched = os.getpid() if parent_pid is None else parent_pid
    # 설치본/소스 트리 양쪽에서 import 되도록 scopefuel 패키지 루트를 심는다.
    src_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src_root) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        return subprocess.Popen(  # noqa: S603 - sys.executable -m <our module>, fixed argv
            [sys.executable, "-m", "scopefuel.proctrack", str(watched), str(target_dir), repr(ttl_s)],
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


def reaper_main(parent_pid: int, target_dir: Path, ttl_s: float, interval_s: float = 1.0) -> int:
    """Watch ``parent_pid``; on its death SIGKILL every process in ``target_dir``.

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
        if not kill_leftovers_at_cwd(target_dir, nested=True) and attempt:
            break
        time.sleep(_REAPER_SWEEP_GAP_S)
    return 0


if __name__ == "__main__":
    # python -m scopefuel.proctrack <parent_pid> <target_dir> <ttl_s>
    raise SystemExit(reaper_main(int(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])))
