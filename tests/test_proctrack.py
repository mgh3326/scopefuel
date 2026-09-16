"""proctrack: pgid registry, cwd-discriminated sweep, orphan reaper."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
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
        proctrack.register(child.pid)  # session leader → pgid == pid
        assert child.pid in proctrack.registered()
        proctrack.kill_registered(grace_s=0.05)
        assert child.wait(timeout=5) is not None
    finally:
        proctrack.unregister(child.pid)
        _kill_and_reap(child)
    assert child.pid not in proctrack.registered()


def test_pids_with_cwd_matches_exact_directory_only(tmp_path):
    inside = _sleep_in(tmp_path / "probe-workdir")
    outside = _sleep_in(tmp_path / "elsewhere")
    try:
        pids = proctrack.pids_with_cwd(tmp_path / "probe-workdir")
        assert inside.pid in pids
        assert outside.pid not in pids
        assert os.getpid() not in pids
    finally:
        _kill_and_reap(inside)
        _kill_and_reap(outside)


def test_kill_leftovers_at_cwd_kills_only_matching_processes(tmp_path):
    workdir = tmp_path / "probe-workdir"
    inside = _sleep_in(workdir)
    outside = _sleep_in(tmp_path / "elsewhere")
    try:
        killed = proctrack.kill_leftovers_at_cwd(workdir)
        assert inside.pid in killed
        assert inside.wait(timeout=5) == -signal.SIGKILL
        assert outside.poll() is None
    finally:
        _kill_and_reap(inside)
        _kill_and_reap(outside)


def test_reaper_sweeps_workdir_when_watched_parent_dies(tmp_path):
    workdir = tmp_path / "probe-workdir"
    leftover = _sleep_in(workdir)
    survivor = _sleep_in(tmp_path / "elsewhere")
    parent = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    reaper = proctrack.spawn_reaper(workdir, ttl_s=20, parent_pid=parent.pid)
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
    workdir = tmp_path / "probe-workdir"
    leftover = _sleep_in(workdir)
    reaper = proctrack.spawn_reaper(workdir, ttl_s=1.0, parent_pid=os.getpid())
    assert reaper is not None
    try:
        assert reaper.wait(timeout=10) == 0
        assert leftover.poll() is None  # no sweep while the parent lives
    finally:
        _kill_and_reap(leftover)
        _kill_and_reap(reaper)
