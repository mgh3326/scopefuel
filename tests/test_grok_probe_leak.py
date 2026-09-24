"""#593 §5 / #608: the grok quota probe must not leave orphans behind.

2026-09-23 incident: a caller polling every ~15s outran the 30s grok probe, so
each round started another grok CLI while the previous was still running. When
the caller's own timeout SIGKILLed scopefuel, the probe's Python cleanup never
ran, and because the child lives in its own session it survived, was reparented
to init and kept burning ~20% CPU — 22 of them on desktop, load 28 on 4 cores.

The devin probe already had proctrack's protections (be0c0a9); grok never got
them. These tests hold that line with a fake grok that never exits on its own,
so a regression shows up as a leaked process rather than a slow test.

No test here executes the real grok CLI: BINARY is redirected to the fake for
every case that spawns anything.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from scopefuel import proctrack
from scopefuel.providers import grok


def _hanging_fake(tmp_path: Path) -> Path:
    """A grok that prints a prompt, answers nothing, and ignores SIGHUP."""

    fake = tmp_path / "fake-grok-hang"
    fake.write_text("#!/bin/sh\ntrap '' HUP\nprintf '\\342\\235\\257 \\r\\n'\nexec sleep 120\n")
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

    workdir = tmp_path / "grok-probe-workdir"
    monkeypatch.setattr(grok, "BINARY", str(_hanging_fake(tmp_path)))
    monkeypatch.setattr(grok, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(grok, "TIMEOUT_S", 1.0)
    monkeypatch.setattr(grok, "STARTUP_DELAY_S", 0.05)

    with pytest.raises(subprocess.TimeoutExpired):
        grok._probe_once()

    assert _wait_gone(proctrack.pids_with_cwd(workdir, nested=True)) == [], (
        "a timed-out probe left a grok child running"
    )


def test_fetch_reports_timeout_without_leaking(tmp_path, monkeypatch):
    workdir = tmp_path / "grok-probe-workdir"
    monkeypatch.setattr(grok, "BINARY", str(_hanging_fake(tmp_path)))
    monkeypatch.setattr(grok, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(grok, "TIMEOUT_S", 1.0)
    monkeypatch.setattr(grok, "STARTUP_DELAY_S", 0.05)

    result = grok.fetch()
    assert result.error and "끝나지 않음" in result.error
    assert _wait_gone(proctrack.pids_with_cwd(workdir, nested=True)) == []


def test_a_second_probe_is_skipped_while_one_is_running(tmp_path, monkeypatch):
    """The stacking itself, not just the cleanup: one probe per pool at a time.

    Without this, a 15s poll against a 30s probe adds a process every round no
    matter how well each individual probe cleans up after itself.
    """

    workdir = tmp_path / "grok-probe-workdir"
    monkeypatch.setattr(grok, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(grok, "BINARY", str(_hanging_fake(tmp_path)))

    entered = []

    def _never_called() -> str:
        entered.append(True)
        raise AssertionError("the second probe must not start a grok")

    with grok._single_probe_lock(workdir) as acquired:
        assert acquired is True
        monkeypatch.setattr(grok, "_probe_once", _never_called)
        result = grok.fetch()

    assert entered == []
    assert result.error and "이미 실행 중" in result.error


def test_a_sigkilled_probe_leaves_no_orphan(tmp_path):
    """The incident's exact shape: the probe's own process is SIGKILLed.

    No Python cleanup runs at all, so only proctrack's detached reaper can end
    the child. This is the case that produced the 22 desktop orphans.
    """

    workdir = tmp_path / "grok-probe-workdir"
    workdir.mkdir(parents=True)
    fake = _hanging_fake(tmp_path)

    helper = tmp_path / "probe_helper.py"
    helper.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(Path("src").resolve())!r})
            from scopefuel.providers import grok

            grok.BINARY = {str(fake)!r}
            grok.PROBE_WORKDIR = {str(workdir)!r}
            grok.TIMEOUT_S = 120.0
            grok.STARTUP_DELAY_S = 0.05
            grok._probe_once()
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
        assert children, "the fake grok child never started"

        os.kill(probe.pid, signal.SIGKILL)
        probe.wait(timeout=10)

        assert _wait_gone(children, timeout=30) == [], (
            "a SIGKILLed probe orphaned its grok child — this is the 2026-09-23 incident"
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

    workdir = tmp_path / "grok-probe-workdir"
    pidfile = tmp_path / "grandchild.pid"
    fake = tmp_path / "fake-grok-backgrounder"
    fake.write_text(f"#!/bin/sh\nsleep 120 &\necho $! > {pidfile}\nexit 0\n")
    fake.chmod(fake.stat().st_mode | 0o111)

    monkeypatch.setattr(grok, "BINARY", str(fake))
    monkeypatch.setattr(grok, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(grok, "TIMEOUT_S", 2.0)
    monkeypatch.setattr(grok, "STARTUP_DELAY_S", 0.05)

    with contextlib.suppress(subprocess.TimeoutExpired):
        grok._probe_once()

    pid = int(pidfile.read_text())
    try:
        assert _wait_gone([pid], timeout=10) == [], "a backgrounded grandchild outlived a successful probe"
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
