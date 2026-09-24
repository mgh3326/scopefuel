"""#608: the kimi quota probe must not leave orphans behind.

kimi's PTY probe had the same shape as grok's pre-#593 code — a child in its
own session, no proctrack registration, no reaper, no single-probe lock — so
it carried the same 2026-09-23 incident (a ~15s caller outrunning a 30s probe
leaks one orphan per round; 22 grok children, load 28 on desktop). These tests
hold the same line test_grok_probe_leak.py holds for grok, with a fake kimi
that never exits on its own.

No test here executes the real kimi CLI: BINARY is redirected to the fake for
every case that spawns anything.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from scopefuel import proctrack
from scopefuel.providers import kimi


def _hanging_fake(tmp_path: Path) -> Path:
    """A kimi that prints a ready prompt, answers nothing, and ignores SIGHUP."""

    fake = tmp_path / "fake-kimi-hang"
    fake.write_text("#!/bin/sh\ntrap '' HUP\nprintf '\\342\\224\\202 >\\r\\n'\nexec sleep 120\n")
    fake.chmod(fake.stat().st_mode | 0o111)
    return fake


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pids, timeout: float = 10.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = [pid for pid in pids if _alive(pid)]
        if not remaining:
            return []
        time.sleep(0.05)
    return [pid for pid in pids if _alive(pid)]


def test_probe_timeout_kills_the_child_and_its_group(tmp_path, monkeypatch):
    """The ordinary path: a probe that times out leaves nothing running."""

    workdir = tmp_path / "kimi-probe-workdir"
    monkeypatch.setattr(kimi, "BINARY", str(_hanging_fake(tmp_path)))
    monkeypatch.setattr(kimi, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(kimi, "TIMEOUT_S", 1.0)
    monkeypatch.setattr(kimi, "STARTUP_DELAY_S", 0.05)

    with pytest.raises(subprocess.TimeoutExpired):
        kimi._probe_once()

    assert _wait_gone(proctrack.pids_with_cwd(workdir, nested=True)) == [], (
        "a timed-out probe left a kimi child running"
    )


def test_fetch_reports_timeout_without_leaking(tmp_path, monkeypatch):
    workdir = tmp_path / "kimi-probe-workdir"
    monkeypatch.setattr(kimi, "BINARY", str(_hanging_fake(tmp_path)))
    monkeypatch.setattr(kimi, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(kimi, "TIMEOUT_S", 1.0)
    monkeypatch.setattr(kimi, "STARTUP_DELAY_S", 0.05)

    result = kimi.fetch()
    assert result.error and "끝나지 않음" in result.error
    assert _wait_gone(proctrack.pids_with_cwd(workdir, nested=True)) == []


def test_an_exception_mid_probe_still_reaps_the_child(tmp_path, monkeypatch):
    """The exception path must clean up exactly like the timeout path."""

    workdir = tmp_path / "kimi-probe-workdir"
    monkeypatch.setattr(kimi, "BINARY", str(_hanging_fake(tmp_path)))
    monkeypatch.setattr(kimi, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(kimi, "TIMEOUT_S", 30.0)
    monkeypatch.setattr(kimi, "STARTUP_DELAY_S", 0.05)

    # EIO would break out of the read loop instead of raising — pick an errno
    # the probe does not swallow.
    def _raising_select(*_args):
        raise OSError(22, "simulated select failure")

    monkeypatch.setattr(kimi.select, "select", _raising_select)

    with pytest.raises(OSError, match="simulated select failure"):
        kimi._probe_once()

    assert _wait_gone(proctrack.pids_with_cwd(workdir, nested=True)) == [], (
        "an exception mid-probe left a kimi child running"
    )


def test_a_second_probe_is_skipped_while_one_is_running(tmp_path, monkeypatch):
    """One probe per pool at a time — the stacking itself, not just cleanup."""

    workdir = tmp_path / "kimi-probe-workdir"
    monkeypatch.setattr(kimi, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(kimi, "BINARY", str(_hanging_fake(tmp_path)))

    entered = []

    def _never_called() -> str:
        entered.append(True)
        raise AssertionError("the second probe must not start a kimi")

    with proctrack.single_probe_lock(workdir) as acquired:
        assert acquired is True
        monkeypatch.setattr(kimi, "_probe_once", _never_called)
        result = kimi.fetch()

    assert entered == []
    assert result.error and "이미 실행 중" in result.error


def test_a_sigkilled_probe_leaves_no_orphan(tmp_path):
    """The incident's exact shape: the probe's own process is SIGKILLed.

    No Python cleanup runs at all, so only proctrack's detached reaper can end
    the child.
    """

    workdir = tmp_path / "kimi-probe-workdir"
    workdir.mkdir(parents=True)
    fake = _hanging_fake(tmp_path)

    helper = tmp_path / "probe_helper.py"
    helper.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(Path("src").resolve())!r})
            from scopefuel.providers import kimi

            kimi.BINARY = {str(fake)!r}
            kimi.PROBE_WORKDIR = {str(workdir)!r}
            kimi.TIMEOUT_S = 120.0
            kimi.STARTUP_DELAY_S = 0.05
            kimi._probe_once()
            """
        )
    )

    probe = subprocess.Popen([sys.executable, str(helper)])
    try:
        deadline = time.monotonic() + 20
        children: list[int] = []
        while time.monotonic() < deadline:
            children = proctrack.pids_with_cwd(workdir, nested=True)
            if children:
                break
            time.sleep(0.1)
        assert children, "the fake kimi child never started"

        os.kill(probe.pid, signal.SIGKILL)
        probe.wait(timeout=10)

        assert _wait_gone(children, timeout=30) == [], (
            "a SIGKILLed probe orphaned its kimi child — this is the 2026-09-23 incident"
        )
    finally:
        if probe.poll() is None:
            probe.kill()
            probe.wait(timeout=5)
        for pid in proctrack.pids_with_cwd(workdir, nested=True):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def test_a_backgrounded_grandchild_does_not_survive_a_successful_probe(tmp_path, monkeypatch):
    """The success path leaked where the timeout path did not.

    A CLI that backgrounds a helper and returns 0 leaves the direct child dead,
    so the kill block is skipped entirely — and the instance directory is then
    removed, taking with it the only thing proctrack identifies descendants by.
    """

    workdir = tmp_path / "kimi-probe-workdir"
    pidfile = tmp_path / "grandchild.pid"
    fake = tmp_path / "fake-kimi-backgrounder"
    fake.write_text(f"#!/bin/sh\nsleep 120 &\necho $! > {pidfile}\nexit 0\n")
    fake.chmod(fake.stat().st_mode | 0o111)

    monkeypatch.setattr(kimi, "BINARY", str(fake))
    monkeypatch.setattr(kimi, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(kimi, "TIMEOUT_S", 2.0)
    monkeypatch.setattr(kimi, "STARTUP_DELAY_S", 0.05)

    with contextlib.suppress(subprocess.TimeoutExpired):
        kimi._probe_once()

    pid = int(pidfile.read_text())
    try:
        assert _wait_gone([pid], timeout=10) == [], "a backgrounded grandchild outlived a successful probe"
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def test_fetch_writes_one_caller_log_line_per_probe(tmp_path, monkeypatch):
    """The post-incident question — *who* called the probe — must be answerable."""

    workdir = tmp_path / "kimi-probe-workdir"
    binary = tmp_path / "fake-kimi"
    binary.write_text(
        "#!/bin/sh\n"
        "printf '│ >\\r\\n'\n"
        "IFS= read -r command\n"
        "printf 'Weekly: 80%% left (resets in 1d)\\r\\n5h: 50%% left (resets in 1h)\\r\\n'\n"
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(kimi, "BINARY", str(binary))
    monkeypatch.setattr(kimi, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(kimi, "STARTUP_DELAY_S", 0.05)

    result = kimi.fetch()
    assert result.error is None

    log = workdir / "probe-calls.log"
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert "probe=kimi" in line
    assert f"pid={os.getpid()}" in line
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", line), line
    assert "via=" in line and "fetch" in line
