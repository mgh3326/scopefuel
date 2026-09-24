"""Probe child process-group registry, cwd-based leftover sweep, and orphan reaper.

프로브는 CLI/PTY 자식을 전용 세션(start_new_session=True)에 띄우므로 refresh
워커 자기 그룹에 대한 killpg 가 그 자식에 닿지 않는다. 네 가지 장치로 고아를 막는다:

- 인스턴스 디렉터리: 프로브마다 workdir 아래 고유 디렉터리(``probe-*``)를
  만들고 디렉터리 자체에 flock 을 쥔다(락은 inode 에 붙어 rename 을 견딘다).
  자식의 cwd 는 이 디렉터리다. 커널이 소유자 사망 시 락을 자동 해제하므로
  PID 재사용에 무관하게 "살아있는 프로브"를 식별한다.
- 선제 스윕: 프로브 시작 전 workdir 루트의 cwd 잔존자와, 디렉터리 락이 풀린
  (죽은 프로브의) ``probe-*``/``.probe-pending-*`` 디렉터리 안의 잔존자를
  정리한다. 락이 잡힌 디렉터리 — 살아있는 동시 프로브 — 는 건너뛴다.
  생성자는 workdir 의 ``.sweep.lock`` 을 쥔 채 mkdtemp→flock→rename 을
  지나고 스윕은 같은 락 아래서만 인스턴스 디렉터리를 열거하므로, 락 없는
  pending 이 스윕에 보이는 순간은 없다 — 락 없는 pending 은 곧 죽은 잔해다
  (나이 추정 없음).
- 레지스트리: provider 가 띄운 자식의 pgid 와 기대 cwd 를 등록한다.
  refresh 타임아웃 핸들러가 os._exit 전에, 등록된 그룹의 구성원 중 cwd 가
  기대 디렉터리 안에 있는 것으로 확인된 프로세스에만 신호한다 — killpg 는
  쓰지 않는다(같은 pgid 에 무관한 프로세스가 섞일 수 있으므로).
- 리퍼: 분리된 감시 프로세스가 부모 사망(ppid→1)을 폴링으로 감지해 자기
  인스턴스 디렉터리만 스윕한다 — 부모가 SIGKILL/SIGHUP 로 죽어 파이썬
  정리 경로가 전혀 못 도는 경우의 최종 방어선이다.

판별자는 오직 cwd 다(실제 경로 realpath 기준 — 심볼릭 링크 경유도 같은
디렉터리로 해소되면 안에 있는 것이다). 나이·CPU·이름으로 고르지 않는다 —
장수 정상 워커를 죽이지 않기 위해서다(2026-09-16 실측: 정상 작업 세션이
이틀 이상 도는 경우가 있다).

모든 신호 경로는 신호 직전에 대상 cwd 를 다시 읽어 재판별한다 — 열거된
pid 목록은 기록일 뿐이다. 다만 재판별과 os.kill 사이에는 스케줄링 창이
남아 있다: 그 사이 대상이 종료되고 PID 가 재사용되면 신호가 다른
프로세스에 닿을 수 있다. 이 창은 커널 지원 없이는 제거할 수 없으므로,
재판별은 "창을 한 번의 lsof 왕복 안쪽으로 줄이는" 장치이지 원자성
보장이 아니다.
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
_OWNER_LOCK = ".owner"  # 구판본이 남긴 잔해 — 있으면 그것도 락 대상으로 본다
_SWEEP_LOCK = ".sweep.lock"  # 생성 임계구간 ↔ 스윕 열거를 직렬화하는 workdir 락

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
    """그 pid 의 cwd 를 지금 다시 읽는다 — 신호 직전 재판별에 쓰는 소스."""

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


def _signal_verified_members(pgid: int, expected: Path, sig: signal.Signals) -> list[int]:
    """그룹원 각각의 cwd 를 신호 직전에 확인해, ``expected`` 안에 있는 것만 신호한다.

    killpg 를 쓰지 않는 이유: 같은 pgid 에 기대 밖 프로세스가 섞이는 입력이
    있다 — 세션 리더가 자기 cwd 만 옮긴 경우, 등록 후 그룹에 합류한 경우,
    pgid 재사용. 구성원 단위로 재판별하면 이 입력들에서 무고한 프로세스는
    전부 걸러진다. 기대 안에 있는 구성원만 우리 것이다(cwd 가 판별자).
    """

    want = _resolve(expected)
    sent: list[int] = []
    for pid in {pgid, *_pgid_members(pgid)}:
        cwd = _cwd_of(pid)
        if cwd is None or not _matches(cwd, want, nested=True):
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)
            sent.append(pid)
    return sent


def kill_registered(grace_s: float = 0.2) -> None:
    """SIGTERM then SIGKILL the verified members of every registered group."""

    groups = list(_PGROUPS.items())
    for pgid, expected in groups:
        _signal_verified_members(pgid, expected, signal.SIGTERM)
    if grace_s > 0:
        time.sleep(grace_s)
    for pgid, expected in groups:
        _signal_verified_members(pgid, expected, signal.SIGKILL)


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
    skipped. The check and the kill are not atomic — a pid that exits and
    is reused inside the remaining scheduling window can still take the
    signal; re-verification shrinks that window to one lsof round-trip.
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
    """Create a unique instance dir under ``workdir`` and lock the dir itself.

    Returns ``(instance_dir, lock_fd)`` where ``lock_fd`` is an open fd on the
    directory inode holding an exclusive flock — the lock survives the rename
    to the final name. The caller keeps ``lock_fd`` for the whole probe and
    closes it only after the probe child is reaped; a released lock is how
    sweepers learn the owner died. The fd is non-inheritable and children
    spawn ``close_fds=True``, so only the probe parent holds it. A lock that
    cannot be taken fails the probe closed: an unlocked instance dir looks
    like dead residue to sweepers.

    Ordering: take the workdir sweep lock → ``mkdtemp`` under a pending
    name → flock the directory → atomic rename to the final ``probe-*``
    name → release the sweep lock. Sweepers enumerate instance dirs only
    while holding the same lock, so an unlocked pending dir is never
    visible to a sweep: the whole create window is serialized against
    enumeration, and any lock-free pending a sweeper can see is provably
    creator-dead residue — there is no age or stat-result guessing. A
    creator stalled arbitrarily long inside the window still cannot lose
    its dir; if it dies there, the kernel releases both locks and the
    next sweep reclaims the residue.
    """

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    sweep_fd = _acquire_sweep_lock(workdir, blocking=True)
    try:
        pending = Path(tempfile.mkdtemp(prefix=_PENDING_PREFIX, dir=workdir))
        fd = os.open(pending, os.O_RDONLY)
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
    finally:
        os.close(sweep_fd)
    return instance, fd


def _probe_dirs(workdir: Path) -> list[Path]:
    """Instance dirs this module can own: published ``probe-*`` and pending.

    Anything else in ``workdir`` — foreign names, symlinks, plain files —
    is not ours and is never removed.
    """

    with contextlib.suppress(OSError):
        return [
            d
            for d in workdir.iterdir()
            if d.is_dir() and not d.is_symlink() and d.name.startswith((_INSTANCE_PREFIX, _PENDING_PREFIX))
        ]
    return []


def _acquire_dir_locks(instance: Path) -> list[int] | None:
    """flock the dir (and a legacy ``.owner`` if present); None when any is held.

    Acquiring every lock means the owner is dead — kernel-released locks
    need no PID. The caller keeps the returned fds open through kill+rmtree
    so the liveness check and the removal are one held-lock interval, then
    closes them. A lock held by someone else, or a dir that cannot be
    opened, is indeterminate → skipped.
    """

    fds: list[int] = []
    try:
        try:
            fd = os.open(instance, os.O_RDONLY)
        except OSError:
            return None
        fds.append(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner = instance / _OWNER_LOCK
        if owner.exists():
            try:
                ofd = os.open(owner, os.O_RDWR)
            except OSError:
                return None
            fds.append(ofd)
            fcntl.flock(ofd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        for fd in fds:
            with contextlib.suppress(OSError):
                os.close(fd)
        return None
    return fds


def _acquire_sweep_lock(workdir: Path, *, blocking: bool) -> int | None:
    """flock ``workdir/.sweep.lock`` — serializes create windows against sweeps.

    Returns the open fd while the lock is held (close to release), or None
    when a non-blocking caller finds it contended — a creator or another
    sweeper is mid-critical-section, so this cycle is skipped. A dead
    holder's lock is released by the kernel, so a blocking caller can
    never wait forever on a corpse.
    """

    try:
        fd = os.open(workdir / _SWEEP_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        if blocking:
            raise
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def kill_stale_probe_leftovers(workdir: Path, *, exclude: Iterable[int] = ()) -> list[int]:
    """Sweep leftovers under ``workdir`` before a probe starts.

    Kills (a) processes whose cwd is exactly ``workdir`` — no live probe
    uses the root itself, only legacy orphans — and (b) everything inside
    ``probe-*``/``.probe-pending-*`` dirs whose locks are all released,
    then removes those dirs. The instance-dir pass runs only while holding
    the workdir sweep lock, and only non-blockingly: if a creator or
    another sweeper holds it, this cycle skips the pass rather than wait.
    Inside the lock, an unlocked dir is provably dead — a live creator
    holds the sweep lock through its whole unlocked-pending window. Names
    outside our two prefixes and anything outside ``workdir`` are not
    ours and are not touched.
    """

    workdir = Path(workdir)
    killed = kill_leftovers_at_cwd(workdir, nested=False, exclude=exclude)
    sweep_fd = _acquire_sweep_lock(workdir, blocking=False)
    if sweep_fd is None:
        return killed
    try:
        for instance in _probe_dirs(workdir):
            fds = _acquire_dir_locks(instance)
            if fds is None:
                continue
            try:
                killed += kill_leftovers_at_cwd(instance, nested=True, exclude=exclude)
                shutil.rmtree(instance, ignore_errors=True)
            finally:
                for fd in fds:
                    with contextlib.suppress(OSError):
                        os.close(fd)
    finally:
        os.close(sweep_fd)
    return killed


_CALL_LOG_NAME = "probe-calls.log"


def log_probe_call(workdir: Path, provider: str) -> None:
    """Append one caller-identification line per probe attempt.

    The 2026-09-23 incident's open question was *who* kept polling — the
    leaked grok children were countable but the ~15s caller was never
    identified. Every fetch() that reaches a real probe attempt (including
    ones the single-probe lock then skips) writes one line here:
    wall-clock time, this process's pid/ppid/argv, the parent process's
    cmdline, and the Python call path that reached the probe. Logging is
    best-effort and never raises — a probe must not fail because its
    audit line could not be written.
    """

    try:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        ppid = os.getppid()
        parent = _cmdline(ppid) or "?"
        frames = []
        frame = sys._getframe(1)
        while frame is not None and len(frames) < 6:
            frames.append(f"{Path(frame.f_code.co_filename).name}:{frame.f_code.co_name}")
            frame = frame.f_back
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
        argv = " ".join(sys.argv) or "?"
        line = (
            f"{stamp} probe={provider} pid={os.getpid()} ppid={ppid} "
            f"argv={argv} parent={parent} via={' < '.join(frames)}\n"
        )
        with (workdir / _CALL_LOG_NAME).open("a", encoding="utf-8") as log_file:
            log_file.write(line)
    except Exception:  # noqa: BLE001 - audit logging must never break a probe
        pass


def _cmdline(pid: int) -> str | None:
    """Best-effort cmdline of ``pid`` — /proc first, ``ps`` elsewhere."""

    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        if raw:
            return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        pass
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv; numeric pid only
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_LSOF_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


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
