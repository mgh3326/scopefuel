"""proctrack: pgid registry, cwd-discriminated sweep, instance dirs, orphan reaper."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from scopefuel import proctrack


def _sleep_in(directory: Path, seconds: str = "60") -> subprocess.Popen:
    directory.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(["sleep", seconds], cwd=directory, start_new_session=True)


def _kill_and_reap(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def test_registry_kill_registered_kills_the_group():
    child = _sleep_in(Path.cwd())
    try:
        proctrack.register(child.pid, Path.cwd())  # session leader → pgid == pid
        assert child.pid in proctrack.registered()
        proctrack.kill_registered(grace_s=0.05)
        assert child.wait(timeout=5) is not None
    finally:
        proctrack.unregister(child.pid)
        _kill_and_reap(child)
    assert child.pid not in proctrack.registered()


def test_registry_skips_group_outside_expected_cwd(tmp_path):
    """신호 직전 재판별: 등록된 기대 cwd 밖에 있는 그룹은 신호를 받지 않는다."""
    child = _sleep_in(tmp_path / "elsewhere")
    try:
        proctrack.register(child.pid, tmp_path / "expected-elsewhere")
        proctrack.kill_registered(grace_s=0.05)
        assert child.poll() is None
    finally:
        proctrack.unregister(child.pid)
        _kill_and_reap(child)


def test_kill_registered_signals_only_members_inside_expected(tmp_path):
    """기대 cwd 밖의 같은-그룹 구성원은 신호를 받지 않는다 — killpg 가 아니다.

    그룹원 각각의 cwd 를 신호 직전에 확인해, expected 안에 있는 구성원에만
    보낸다. 이 확인을 무력화하면 기대 밖 구성원이 죽어 assertion 이 실패한다.
    """
    inside = tmp_path / "expected"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    # bash 리더(cwd=outside) + 배경 잡 하나(cwd=inside): 같은 pgid, 다른 cwd.
    leader = subprocess.Popen(
        ["bash", "-c", f'cd "{inside}" && exec sleep 60 & sleep 60 & wait'],
        cwd=outside,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        member_in = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            cands = [p for p in proctrack.pids_with_cwd(inside) if p != leader.pid]
            if cands:
                member_in = cands[0]
                break
            time.sleep(0.05)
        assert member_in is not None, "inside-cwd group member never appeared"

        proctrack.register(leader.pid, inside)  # session leader → pgid == leader pid
        proctrack.kill_registered(grace_s=0.05)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and member_in in proctrack.pids_with_cwd(inside):
            time.sleep(0.1)
        assert member_in not in proctrack.pids_with_cwd(inside)
        assert leader.poll() is None, "outside-cwd group leader was signaled"
    finally:
        proctrack.unregister(leader.pid)
        _kill_and_reap(leader)


def test_pids_with_cwd_matches_exact_directory_only(tmp_path):
    inside = _sleep_in(tmp_path / "probe-workdir")
    outside = _sleep_in(tmp_path / "elsewhere")
    nested = _sleep_in(tmp_path / "probe-workdir" / "deeper")
    try:
        pids = proctrack.pids_with_cwd(tmp_path / "probe-workdir")
        assert inside.pid in pids
        assert outside.pid not in pids
        assert nested.pid not in pids
        assert os.getpid() not in pids
    finally:
        _kill_and_reap(inside)
        _kill_and_reap(outside)
        _kill_and_reap(nested)


def test_pids_with_cwd_nested_matches_descendants_not_prefix_siblings(tmp_path):
    base = tmp_path / "probe-workdir"
    sibling = tmp_path / "probe-workdir-other"  # 접두사만 같은 경로 — 안쪽이 아니다
    inside = _sleep_in(base / "probe-x" / "deeper")
    sibling_proc = _sleep_in(sibling)
    try:
        nested = proctrack.pids_with_cwd(base, nested=True)
        assert inside.pid in nested
        assert sibling_proc.pid not in nested
        exact = proctrack.pids_with_cwd(base)
        assert inside.pid not in exact
    finally:
        _kill_and_reap(inside)
        _kill_and_reap(sibling_proc)


def test_dotdot_normalized_cwd_is_not_inside_target(tmp_path):
    target = tmp_path / "probe-workdir"
    escaped = target / ".." / "x"  # realpath → tmp_path/x — 안쪽이 아니다
    proc = _sleep_in(escaped)
    try:
        assert proc.pid not in proctrack.pids_with_cwd(target, nested=True)
        assert proc.pid in proctrack.pids_with_cwd(tmp_path / "x")
    finally:
        _kill_and_reap(proc)


def test_symlinked_cwd_resolves_into_target(tmp_path):
    target = tmp_path / "workdir" / "probe-x"
    target.mkdir(parents=True)
    alias = tmp_path / "alias-to-instance"
    alias.symlink_to(target)
    proc = subprocess.Popen(["sleep", "60"], cwd=alias, start_new_session=True)
    try:
        # lsof 는 vnode 의 실제 경로를 보고한다 — 링크 경유도 안에 있는 것이다.
        assert proc.pid in proctrack.pids_with_cwd(target, nested=True)
    finally:
        _kill_and_reap(proc)


def test_kill_leftovers_at_cwd_kills_only_matching_processes(tmp_path):
    workdir = tmp_path / "probe-workdir"
    inside = _sleep_in(workdir / "probe-x" / "deep")
    outside = _sleep_in(tmp_path / "elsewhere")
    try:
        killed = proctrack.kill_leftovers_at_cwd(workdir / "probe-x", nested=True)
        assert inside.pid in killed
        assert inside.wait(timeout=5) == -signal.SIGKILL
        assert outside.poll() is None
    finally:
        _kill_and_reap(inside)
        _kill_and_reap(outside)


def test_enumerated_pid_is_reverified_before_kill(tmp_path, monkeypatch):
    """TOCTOU: 목록은 기록용 — 신호 직전 재판별에서 벗어난 대상은 죽지 않는다."""
    target = tmp_path / "target"
    victim = _sleep_in(tmp_path / "elsewhere")  # 실제 cwd 는 target 밖
    monkeypatch.setattr(
        proctrack, "pids_with_cwd", lambda *a, **k: [victim.pid] if k.get("pids") is None else []
    )
    try:
        # 열거가 오래된(또는 거짓) 목록을 줘도 신호 직전 재판별에서 걸러진다.
        assert proctrack.kill_leftovers_at_cwd(target) == []
        assert victim.poll() is None
    finally:
        _kill_and_reap(victim)


def test_enumerated_pid_that_exited_is_not_killed(tmp_path, monkeypatch):
    """열거 후 종료된 pid — 재사용돼도 신호가 가지 않는다."""
    victim = _sleep_in(tmp_path / "target")
    _kill_and_reap(victim)
    monkeypatch.setattr(proctrack, "pids_with_cwd", lambda *a, **k: [victim.pid])
    assert proctrack.kill_leftovers_at_cwd(tmp_path / "target") == []


def test_stale_sweep_kills_unlocked_instance_dir(tmp_path):
    workdir = tmp_path / "workdir"
    instance, fd = proctrack.new_probe_dir(workdir)
    leftover = _sleep_in(instance / "deeper")
    try:
        os.close(fd)  # 주인 사망과 동일 — 커널이 락을 푼다
        killed = proctrack.kill_stale_probe_leftovers(workdir)
        assert leftover.pid in killed
        assert leftover.wait(timeout=5) == -signal.SIGKILL
        assert not instance.exists()
    finally:
        _kill_and_reap(leftover)


def test_stale_sweep_skips_locked_instance_dir(tmp_path):
    """락이 잡힌 디렉터리 — 살아있는 동시 프로브 — 에는 절대 닿지 않는다."""
    workdir = tmp_path / "workdir"
    instance, fd = proctrack.new_probe_dir(workdir)
    child = _sleep_in(instance / "deeper")
    try:
        assert proctrack.kill_stale_probe_leftovers(workdir) == []
        assert child.poll() is None
        assert instance.exists()
    finally:
        os.close(fd)
        _kill_and_reap(child)


def test_stale_sweep_reclaims_foreign_probe_dir_with_no_locks(tmp_path):
    """우리 네임스페이스(probe-*) 안에서 잡힌 락이 없는 디렉터리는 죽은 잔해다."""
    workdir = tmp_path / "workdir"
    unlocked = workdir / "probe-noLock"
    proc = _sleep_in(unlocked)
    try:
        killed = proctrack.kill_stale_probe_leftovers(workdir)
        assert proc.pid in killed
        assert not unlocked.exists()
    finally:
        _kill_and_reap(proc)


def test_stale_sweep_skips_legacy_owner_locked_dir(tmp_path):
    """구판본이 남긴 .owner 락이 잡힌 디렉터리도 살아있는 것으로 본다."""
    workdir = tmp_path / "workdir"
    legacy = workdir / "probe-legacy"
    legacy.mkdir(parents=True)
    lock = legacy / ".owner"
    lock.touch()
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert proctrack.kill_stale_probe_leftovers(workdir) == []
        assert legacy.exists()
    finally:
        os.close(fd)


def test_stale_sweep_reclaims_dead_pending_dir(tmp_path):
    """mkdtemp~flock 사이에 죽은 pending 잔해 — 회수 대상이다(나이 무관).

    살아있는 생성자는 .sweep.lock 을 쥔 채 락 없는 pending 을 만들므로,
    스윕이 락을 잡고 열거한 락 없는 pending 은 곧 죽은 잔해다.
    """
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    pending = workdir / ".probe-pending-dead"
    pending.mkdir()
    proc = _sleep_in(pending)
    try:
        first = proctrack.kill_stale_probe_leftovers(workdir)
        second = proctrack.kill_stale_probe_leftovers(workdir)
        assert proc.pid in first
        assert not pending.exists(), (first, second, list(workdir.iterdir()))
    finally:
        _kill_and_reap(proc)


def test_sweep_preserves_pending_during_create_window(tmp_path, monkeypatch):
    """생성 창(mkdtemp~flock)이 벌어져도, 창 안쪽 스윕은 pending 을 지우지 않는다.

    생성자는 .sweep.lock 을 쥔 채 그 창을 지나므로 스윕은 락을 못 잡아
    디렉터리 열거 자체를 건너뛴다 — 나이 추정 없이 창이 직렬화로 닫힌다.
    생성자의 락 획득을 무력화하면 이 테스트가 assertion 실패로 RED 가 된다.
    """
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    entered = threading.Event()
    release = threading.Event()
    state: dict = {}
    orig_mkdtemp = proctrack.tempfile.mkdtemp

    def slow_mkdtemp(*args, **kwargs):
        d = orig_mkdtemp(*args, **kwargs)
        state["pending"] = Path(d)
        entered.set()
        release.wait(timeout=30)  # 창을 인위적으로 벌린다
        return d

    monkeypatch.setattr(proctrack.tempfile, "mkdtemp", slow_mkdtemp)
    creator = threading.Thread(target=lambda: state.setdefault("result", proctrack.new_probe_dir(workdir)))
    creator.start()
    try:
        assert entered.wait(timeout=10), "creator never entered the window"
        assert proctrack.kill_stale_probe_leftovers(workdir) == []
        assert state["pending"].exists()
    finally:
        release.set()
        creator.join(timeout=10)
    instance, fd = state["result"]
    os.close(fd)


def test_sweep_is_nonblocking_while_sweep_lock_held(tmp_path):
    """스윕은 best-effort — .sweep.lock 을 못 잡으면 기다리지 않고 건너뛴다.

    생성자가 창 안에서 오래 stall 해도 스윕이 멈추면 안 된다. LOCK_NB 를
    블로킹으로 바꾸는 뮤턴트에서 스윕이 끝나지 않아 assertion RED 가 된다.
    """
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    pending = workdir / ".probe-pending-x"
    pending.mkdir()
    fd = os.open(workdir / proctrack._SWEEP_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    done = threading.Event()
    results: list = []
    sweeper = threading.Thread(
        target=lambda: (results.append(proctrack.kill_stale_probe_leftovers(workdir)), done.set())
    )
    try:
        sweeper.start()
        sweeper.join(timeout=10)
        assert done.is_set(), "sweep blocked on a held .sweep.lock"
        assert results == [[]]
        assert pending.exists()
    finally:
        os.close(fd)
        sweeper.join(timeout=10)


def test_stale_sweep_skips_locked_pending_dir(tmp_path):
    """기동 중(mkdtemp~rename 사이) pending — 디렉터리 락이 잡혀 있으면 불가침."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    pending = workdir / ".probe-pending-starting"
    pending.mkdir()
    fd = os.open(pending, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert proctrack.kill_stale_probe_leftovers(workdir) == []
        assert pending.exists()
    finally:
        os.close(fd)


def test_stale_sweep_spares_prefix_sibling_and_dotdot_escape(tmp_path):
    workdir = tmp_path / "devin-probe-workdir"
    sibling = _sleep_in(tmp_path / "devin-probe-workdir-other")
    escapee = _sleep_in(workdir / ".." / "x")
    try:
        proctrack.kill_stale_probe_leftovers(workdir)
        assert sibling.poll() is None
        assert escapee.poll() is None
    finally:
        _kill_and_reap(sibling)
        _kill_and_reap(escapee)


def test_stale_sweep_kills_symlinked_cwd_resolving_into_stale_instance(tmp_path):
    """링크 경유도 realpath 가 대상 안이면 안에 있는 것이다(정정된 정책)."""
    workdir = tmp_path / "workdir"
    instance, fd = proctrack.new_probe_dir(workdir)
    alias = tmp_path / "alias-to-instance"
    alias.symlink_to(instance)
    proc = subprocess.Popen(["sleep", "60"], cwd=alias, start_new_session=True)
    try:
        os.close(fd)  # 주인 사망 → stale
        killed = proctrack.kill_stale_probe_leftovers(workdir)
        assert proc.pid in killed
    finally:
        _kill_and_reap(proc)


def test_stale_sweep_spares_symlink_resolving_outside(tmp_path):
    workdir = tmp_path / "workdir"
    outside_real = tmp_path / "elsewhere"
    outside_real.mkdir(parents=True)
    alias = tmp_path / "alias-out"
    alias.symlink_to(outside_real)
    proc = subprocess.Popen(["sleep", "60"], cwd=alias, start_new_session=True)
    try:
        proctrack.kill_stale_probe_leftovers(workdir)
        assert proc.poll() is None
    finally:
        _kill_and_reap(proc)


def test_log_probe_call_appends_one_identifying_line(tmp_path):
    """#608: every probe attempt leaves one line — caller path, pid, time."""
    workdir = tmp_path / "probe-workdir"

    proctrack.log_probe_call(workdir, "kimi")

    lines = (workdir / "probe-calls.log").read_text().splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert "probe=kimi" in line
    assert f"pid={os.getpid()}" in line
    assert f"ppid={os.getppid()}" in line
    assert "argv=" in line and "parent=" in line and "via=" in line


def test_log_probe_call_never_raises_on_unwritable_workdir(tmp_path, monkeypatch):
    """A probe must not fail because its audit line could not be written."""
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    proctrack.log_probe_call(tmp_path / "nope", "kimi")


def test_reaper_sweeps_target_dir_when_watched_parent_dies(tmp_path):
    target = tmp_path / "probe-workdir"
    leftover = _sleep_in(target / "deeper")
    survivor = _sleep_in(tmp_path / "elsewhere")
    parent = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    reaper = proctrack.spawn_reaper(target, ttl_s=20, parent_pid=parent.pid)
    assert reaper is not None
    try:
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and leftover.poll() is None:
            time.sleep(0.2)
        assert leftover.wait(timeout=5) is not None
        assert reaper.wait(timeout=10) == 0
        assert survivor.poll() is None
    finally:
        _kill_and_reap(leftover)
        _kill_and_reap(survivor)
        _kill_and_reap(parent)
        _kill_and_reap(reaper)


def test_reaper_expires_quietly_while_parent_alive(tmp_path):
    target = tmp_path / "probe-workdir"
    leftover = _sleep_in(target)
    reaper = proctrack.spawn_reaper(target, ttl_s=1.0, parent_pid=os.getpid())
    assert reaper is not None
    try:
        assert reaper.wait(timeout=10) == 0
        assert leftover.poll() is None  # no sweep while the parent lives
    finally:
        _kill_and_reap(leftover)
        _kill_and_reap(reaper)
