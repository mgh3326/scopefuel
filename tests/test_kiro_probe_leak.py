"""#608: the kiro quota probe must not leave orphans behind.

kiro's ``subprocess.run`` probe timed out by killing only the direct child —
grandchildren the CLI backgrounded survived, and a SIGKILLed scopefuel left
the kiro-cli child itself running (no reaper, no registration, no lock). The
probe now runs inside the same proctrack device chain devin (be0c0a9) and
grok (#593) carry.

No test here executes the real kiro-cli: BINARY is redirected to a fake for
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
from scopefuel.providers import kiro


def _hanging_fake(tmp_path: Path) -> Path:
    """A kiro-cli that consumes stdin and never exits."""

    fake = tmp_path / "fake-kiro-hang"
    fake.write_text("#!/bin/sh\ncat >/dev/null &\nexec sleep 120\n")
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
    """A probe that times out leaves nothing running — children included."""

    workdir = tmp_path / "kiro-probe-workdir"
    monkeypatch.setattr(kiro, "BINARY", str(_hanging_fake(tmp_path)))
    monkeypatch.setattr(kiro, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(kiro, "TIMEOUT_S", 1.0)

    result = kiro._probe_once()

    assert result.error and "끝나지 않음" in result.error
    assert _wait_gone(proctrack.pids_with_cwd(workdir, nested=True)) == [], (
        "a timed-out probe left a kiro-cli child running"
    )


def test_fetch_reports_timeout_without_leaking(tmp_path, monkeypatch):
    workdir = tmp_path / "kiro-probe-workdir"
    monkeypatch.setattr(kiro, "BINARY", str(_hanging_fake(tmp_path)))
    monkeypatch.setattr(kiro, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(kiro, "TIMEOUT_S", 1.0)
    monkeypatch.setattr(kiro.shutil, "which", lambda _binary: kiro.BINARY)

    result = kiro.fetch()
    assert result.error and "끝나지 않음" in result.error
    assert _wait_gone(proctrack.pids_with_cwd(workdir, nested=True)) == []


def test_an_exception_mid_probe_still_reaps_the_child(tmp_path, monkeypatch):
    """The exception path must clean up exactly like the timeout path."""

    workdir = tmp_path / "kiro-probe-workdir"
    monkeypatch.setattr(kiro, "BINARY", str(_hanging_fake(tmp_path)))
    monkeypatch.setattr(kiro, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(kiro, "TIMEOUT_S", 30.0)

    def _exploding_reaper(*_args, **_kwargs):
        raise RuntimeError("simulated reaper failure")

    monkeypatch.setattr(kiro.proctrack, "spawn_reaper", _exploding_reaper)

    with pytest.raises(RuntimeError, match="simulated reaper failure"):
        kiro._probe_once()

    assert _wait_gone(proctrack.pids_with_cwd(workdir, nested=True)) == [], (
        "an exception mid-probe left a kiro-cli child running"
    )


def test_a_second_probe_is_skipped_while_one_is_running(tmp_path, monkeypatch):
    """One probe per pool at a time — the stacking itself, not just cleanup."""

    workdir = tmp_path / "kiro-probe-workdir"
    monkeypatch.setattr(kiro, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(kiro, "BINARY", str(_hanging_fake(tmp_path)))
    monkeypatch.setattr(kiro.shutil, "which", lambda _binary: kiro.BINARY)

    entered = []

    def _never_called():
        entered.append(True)
        raise AssertionError("the second probe must not start a kiro-cli")

    with kiro._single_probe_lock(workdir) as acquired:
        assert acquired is True
        monkeypatch.setattr(kiro, "_probe_once", _never_called)
        result = kiro.fetch()

    assert entered == []
    assert result.error and "이미 실행 중" in result.error


def test_a_sigkilled_probe_leaves_no_orphan(tmp_path):
    """The incident's exact shape: the probe's own process is SIGKILLed.

    No Python cleanup runs at all, so only proctrack's detached reaper can end
    the child.
    """

    workdir = tmp_path / "kiro-probe-workdir"
    workdir.mkdir(parents=True)
    fake = _hanging_fake(tmp_path)

    helper = tmp_path / "probe_helper.py"
    helper.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(Path("src").resolve())!r})
            from scopefuel.providers import kiro

            kiro.BINARY = {str(fake)!r}
            kiro.PROBE_WORKDIR = {str(workdir)!r}
            kiro.TIMEOUT_S = 120.0
            kiro._probe_once()
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
        assert children, "the fake kiro-cli child never started"

        os.kill(probe.pid, signal.SIGKILL)
        probe.wait(timeout=10)

        assert _wait_gone(children, timeout=30) == [], (
            "a SIGKILLed probe orphaned its kiro-cli child"
        )
    finally:
        if probe.poll() is None:
            probe.kill()
            probe.wait(timeout=5)
        for pid in proctrack.pids_with_cwd(workdir, nested=True):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def test_a_backgrounded_grandchild_does_not_survive_a_successful_probe(tmp_path, monkeypatch):
    """A CLI that backgrounds a helper and returns 0 leaks it without the sweep."""

    workdir = tmp_path / "kiro-probe-workdir"
    pidfile = tmp_path / "grandchild.pid"
    fake = tmp_path / "fake-kiro-backgrounder"
    fake.write_text(f"#!/bin/sh\nsleep 120 &\necho $! > {pidfile}\nexit 0\n")
    fake.chmod(fake.stat().st_mode | 0o111)

    monkeypatch.setattr(kiro, "BINARY", str(fake))
    monkeypatch.setattr(kiro, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(kiro, "TIMEOUT_S", 2.0)

    kiro._probe_once()

    pid = int(pidfile.read_text())
    try:
        assert _wait_gone([pid], timeout=10) == [], "a backgrounded grandchild outlived a successful probe"
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def test_fetch_writes_one_caller_log_line_per_probe(tmp_path, monkeypatch):
    """The post-incident question — *who* called the probe — must be answerable."""

    workdir = tmp_path / "kiro-probe-workdir"
    binary = tmp_path / "fake-kiro"
    binary.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        "printf 'Estimated Usage | resets on 2099-01-01 | KIRO PRO MAX\\n'\n"
        "printf 'Credits (0.50 of 5000 covered in plan)\\n'\n"
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(kiro, "BINARY", str(binary))
    monkeypatch.setattr(kiro, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(kiro.shutil, "which", lambda _binary: kiro.BINARY)

    result = kiro.fetch()
    assert result.error is None

    log = workdir / "probe-calls.log"
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert "probe=kiro" in line
    assert f"pid={os.getpid()}" in line
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", line), line
    assert "via=" in line and "fetch" in line
