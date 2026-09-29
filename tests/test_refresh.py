"""ROB-1227 event-driven refresh contract tests; all fetchers are fake/local."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from scopefuel import cache, refresh
from scopefuel.cli import build_parser
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.providers import BUILTIN

# Single upper bound for every subprocess wait in this file. It only bounds
# how long a test may block on a helper — no assertion depends on it. Bump
# SCOPEFUEL_TEST_DEADLINE_S on slow hosts instead of editing literals.
SUBPROCESS_DEADLINE_S = float(os.environ.get("SCOPEFUEL_TEST_DEADLINE_S", "60"))


def _result(pool: str, used: float = 10.0) -> ProviderResult:
    return ProviderResult(
        id=pool,
        buckets=[
            Bucket(
                label="test",
                window="5h",
                used_pct=used,
                scope=Scope("account"),
                horizon="now",
            )
        ],
    )


def _kill_session_group(proc: subprocess.Popen) -> None:
    """SIGKILL a session-leader helper's whole group if it is still running.

    Helpers run with start_new_session=True, so pid == pgid and the group
    reaches every non-detached child the helper spawned. wait() reaps the
    leader so a failed assertion cannot leave it behind either.
    """
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def _pid_is_sleep_child(pid: int, sleep_s: int) -> bool:
    """True only while pid still is the sleep child the helper started.

    The pid file outlives the child when the timeout path works, and a dead
    pid may already be recycled by an unrelated process — never signal a pid
    whose command line does not match the exact sleep the helper launched.
    """
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False
    ).stdout.strip()
    return out in {f"sleep {sleep_s}", f"sh -c sleep {sleep_s}"}


def _kill_recorded_child_group(pid_file: Path, sleep_s: int) -> None:
    """Kill the recorded child's process group only while it is still ours."""
    if not pid_file.exists():
        return
    raw = pid_file.read_text().strip()
    if not raw:
        return
    try:
        pid = int(raw)
    except ValueError:
        # A torn write can leave non-integer bytes in the pid file — this
        # helper runs inside test finally blocks, so nothing may escape it.
        return
    if not _pid_is_sleep_child(pid, sleep_s):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)


def test_refresh_updates_only_requested_pool(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOPEFUEL_CACHE", str(tmp_path / "snapshots.json"))
    cache.update_entry("grok", _result("grok", 10), 100.0)
    cache.update_entry("kimi", _result("kimi", 20), 200.0)

    assert refresh.run_worker({"grok": lambda: _result("grok", 30)}, "grok") == 0
    data = json.loads((tmp_path / "snapshots.json").read_text())
    assert data["grok"]["result"]["buckets"][0]["used_pct"] == 30
    assert data["kimi"]["fetched_at"] == 200.0


def test_refresh_lock_is_nonblocking_and_kernel_released_after_sigkill(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SCOPEFUEL_CACHE", str(tmp_path / "snapshots.json"))
    env = {**os.environ, "SCOPEFUEL_CACHE": str(tmp_path / "snapshots.json")}
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from scopefuel.refresh import pool_lock\n"
                "import time\n"
                "with pool_lock('grok') as acquired:\n"
                " print(acquired, flush=True)\n"
                " time.sleep(30)\n"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "True"
        started = time.monotonic()
        assert refresh.run_worker({"grok": lambda: _result("grok")}, "grok") == 0
        assert time.monotonic() - started < 1.0
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=SUBPROCESS_DEADLINE_S)
        capsys.readouterr()  # drop the contended run's "skipped" line
        assert refresh.run_worker({"grok": lambda: _result("grok", 40)}, "grok") == 0
        # A lock skip also returns 0 — only the update line proves this run
        # acquired the kernel-released lock and fetched.
        assert "refresh: pool=grok updated" in capsys.readouterr().out
        data = json.loads((tmp_path / "snapshots.json").read_text())
        assert data["grok"]["result"]["buckets"][0]["used_pct"] == 40
    finally:
        # A failed early assertion must not leave the sleep-30 holder
        # holding the lock — kill its session group if it is still alive.
        _kill_session_group(holder)


def test_concurrent_refresh_requests_fetch_once(tmp_path):
    """8 contenders overlap deterministically: the holder's fetch blocks on a
    release file until the other 7 have all contended and skipped — exactly
    one fetch no matter how slow interpreter start-up is."""
    counter = tmp_path / "fetch.count"
    release = tmp_path / "fetch.release"
    cache_file = tmp_path / "snapshots.json"
    helper = tmp_path / "counter_helper.py"
    helper.write_text(
        "import fcntl, os, time\n"
        "from scopefuel import refresh\n"
        "from scopefuel.model import ProviderResult\n"
        f"counter_path = {str(counter)!r}\n"
        f"release_path = {str(release)!r}\n"
        f"deadline_s = {SUBPROCESS_DEADLINE_S * 2!r}\n"
        "def fetch():\n"
        " deadline = time.monotonic() + deadline_s\n"
        " while not os.path.exists(release_path):\n"
        "  if time.monotonic() > deadline:\n"
        "   raise TimeoutError('release file never appeared')\n"
        "  time.sleep(0.05)\n"
        " counter = open(counter_path, 'a+')\n"
        " fcntl.flock(counter.fileno(), fcntl.LOCK_EX)\n"
        " counter.seek(0)\n"
        " current = int(counter.read() or '0')\n"
        " counter.seek(0); counter.truncate(); counter.write(str(current + 1)); counter.flush()\n"
        " fcntl.flock(counter.fileno(), fcntl.LOCK_UN)\n"
        " return ProviderResult(id='grok')\n"
        "raise SystemExit(refresh.run_worker({'grok': fetch}, 'grok'))\n"
    )
    env = {**os.environ, "SCOPEFUEL_CACHE": str(cache_file)}
    # A deliberate stagger proves the overlap does not depend on start-up
    # speed: the fetch only completes once every contender has run.
    processes = []
    for _ in range(8):
        processes.append(
            subprocess.Popen(
                [sys.executable, str(helper)],
                cwd=Path.cwd(),
                env=env,
                stdout=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        )
        time.sleep(0.3)
    try:
        deadline = time.monotonic() + SUBPROCESS_DEADLINE_S
        while sum(p.poll() is not None for p in processes) < 7 and time.monotonic() < deadline:
            time.sleep(0.05)
        exited = [p for p in processes if p.poll() is not None]
        running = [p for p in processes if p.poll() is None]
        for p in exited:
            out = p.stdout.read() if p.stdout is not None else ""
            assert p.returncode == 0
            assert "already in progress; skipped" in out
        assert len(running) == 1, f"expected one lock holder still blocked in fetch, got {len(running)}"
        release.touch()
        holder = running[0]
        assert holder.wait(timeout=SUBPROCESS_DEADLINE_S) == 0
        # The drained holder is the one run that fetched — its stdout must
        # show the update line, never the contended-skip line the other 7
        # printed. Deterministic: the lock holder cannot take the skip path.
        holder_out = holder.stdout.read() if holder.stdout is not None else ""
        assert "refresh: pool=grok updated" in holder_out
        assert "already in progress" not in holder_out
        assert counter.read_text() == "1"
    finally:
        # Helpers run in dedicated sessions — kill each surviving group so a
        # failed assertion cannot leave 8 helpers waiting on the release file.
        for p in processes:
            if p.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(p.pid, signal.SIGKILL)
                p.wait()


def test_refresh_timeout_kills_worker_process_group(tmp_path):
    pid_file = tmp_path / "child.pid"
    helper = tmp_path / "timeout_helper.py"
    # Longer than any wait in this file — a natural exit can never fit inside
    # the deadline, so only the timeout handler can end the helper early.
    sleep_s = int(SUBPROCESS_DEADLINE_S * 3)
    helper.write_text(
        "import os, subprocess, time\n"
        "from scopefuel import refresh\n"
        f"child = subprocess.Popen(['sh', '-c', 'sleep {sleep_s}'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "os.environ['SCOPEFUEL_REFRESH_TIMEOUT_S'] = '0.2'\n"
        "def fetch():\n"
        f" return (time.sleep({sleep_s}), None)[1]\n"
        "raise SystemExit(refresh.run_worker({'grok': fetch}, 'grok'))\n"
    )
    proc = subprocess.Popen([sys.executable, str(helper)], cwd=Path.cwd(), start_new_session=True)
    try:
        try:
            returncode = proc.wait(timeout=SUBPROCESS_DEADLINE_S)
        except subprocess.TimeoutExpired:
            # The fetch outlives the wait — only a missing timer lets the
            # helper still be running; the returncode assertion below reports it.
            returncode = None
        # SIGKILL is expected because the dedicated worker session is killed as a
        # whole; the timeout handler cannot return after killing its own group.
        assert returncode in (-signal.SIGKILL, 124)
        child_pid = int(pid_file.read_text())
        child_state = subprocess.run(
            ["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True, check=False
        )
        assert not child_state.stdout.strip() or child_state.stdout.strip().startswith("Z")
    finally:
        # A live helper or child means the timeout path did not run to
        # completion — kill both session groups so a failing run leaves
        # nothing behind. The pid-file child is signalled only while its
        # command line still proves it is our sleep, never a reused pid.
        _kill_session_group(proc)
        _kill_recorded_child_group(pid_file, sleep_s)


def test_refresh_timeout_kills_registered_probe_child_in_other_session(tmp_path):
    """타임아웃 핸들러는 레지스트리의 pgid 도 정리한다 — 다른 세션의 자식도 닿는다."""
    pid_file = tmp_path / "probe.pid"
    helper = tmp_path / "registered_helper.py"
    # Longer than any wait in this file — a natural exit can never fit inside
    # the deadline, so only the timeout handler can end the helper early.
    sleep_s = int(SUBPROCESS_DEADLINE_S * 3)
    helper.write_text(
        "import os, subprocess, time\n"
        "from scopefuel import proctrack, refresh\n"
        f"child = subprocess.Popen(['sleep', '{sleep_s}'], start_new_session=True)\n"
        "proctrack.register(child.pid, os.getcwd())\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "os.environ['SCOPEFUEL_REFRESH_TIMEOUT_S'] = '0.2'\n"
        "def fetch():\n"
        f" return (time.sleep({sleep_s}), None)[1]\n"
        "raise SystemExit(refresh.run_worker({'grok': fetch}, 'grok'))\n"
    )
    proc = subprocess.Popen([sys.executable, str(helper)], cwd=Path.cwd(), start_new_session=True)
    try:
        try:
            returncode = proc.wait(timeout=SUBPROCESS_DEADLINE_S)
        except subprocess.TimeoutExpired:
            # The fetch outlives the wait — only a missing timer lets the
            # helper still be running; the returncode assertion below reports it.
            returncode = None
        # SIGKILL is expected because the dedicated worker session is killed
        # as a whole; the timeout handler cannot return after killing it.
        assert returncode in (-signal.SIGKILL, 124)
        child_pid = int(pid_file.read_text())
        child_state = subprocess.run(
            ["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True, check=False
        )
        assert not child_state.stdout.strip() or child_state.stdout.strip().startswith("Z")
    finally:
        # A live helper or child means the timeout path did not run to
        # completion — kill both session groups so a failing run leaves
        # nothing behind. The pid-file child is signalled only while its
        # command line still proves it is our sleep, never a reused pid.
        _kill_session_group(proc)
        _kill_recorded_child_group(pid_file, sleep_s)


def test_kill_recorded_child_group_skips_pid_that_is_not_our_sleep(monkeypatch, tmp_path):
    """A live pid whose command line is not our sleep must never trigger
    killpg — a stale pid file may already point at a recycled, unrelated
    process. A dead pid would pass even if the guard always said "ours",
    so the decoy has to be alive at check time."""
    pid_file = tmp_path / "child.pid"
    other = subprocess.Popen(["sleep", "31"], start_new_session=True)
    try:
        pid_file.write_text(str(other.pid))
        calls: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
        _kill_recorded_child_group(pid_file, sleep_s=30)
        assert calls == []
    finally:
        # killpg is stubbed for the whole test — reap the real decoy directly.
        other.kill()
        other.wait()


def test_kill_recorded_child_group_skips_non_integer_pid_file(monkeypatch, tmp_path):
    """A torn write can leave non-integer bytes in the pid file — the helper
    must skip the kill, not raise ValueError out of a test finally block."""
    pid_file = tmp_path / "child.pid"
    pid_file.write_text("not-a-pid")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    caught: ValueError | None = None
    try:
        _kill_recorded_child_group(pid_file, sleep_s=30)
    except ValueError as exc:
        caught = exc
    assert caught is None, "non-integer pid file must skip the kill, not raise"
    assert calls == []


def test_kill_recorded_child_group_kills_live_sleep_child(monkeypatch, tmp_path):
    """The guard passes for a live sleep child — its group still gets killed."""
    pid_file = tmp_path / "child.pid"
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    pid_file.write_text(str(child.pid))
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    try:
        _kill_recorded_child_group(pid_file, sleep_s=30)
    finally:
        # killpg was stubbed — reap the real child directly.
        child.kill()
        child.wait()
    assert calls == [(child.pid, signal.SIGKILL)]


def test_refresh_pools_match_provider_registry():
    """refresh 가 아는 pool 은 providers.BUILTIN 에서만 파생한다 (하드코딩 목록 금지)."""

    expected = tuple(sorted(BUILTIN))
    assert expected == refresh.REFRESH_POOLS

    parser = build_parser(list(BUILTIN))
    refresh_parser = parser._subparsers._group_actions[0].choices["refresh"]
    pool_action = next(action for action in refresh_parser._actions if action.dest == "pool")
    assert expected == tuple(pool_action.choices)


def test_refresh_rejects_unknown_pool_before_fetch():
    process = subprocess.run(
        [sys.executable, "-m", "scopefuel.cli", "refresh", "not-a-pool"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode != 0
    assert "invalid choice" in process.stderr
