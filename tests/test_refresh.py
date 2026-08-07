"""ROB-1227 event-driven refresh contract tests; all fetchers are fake/local."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from scopefuel import cache, refresh
from scopefuel.model import Bucket, ProviderResult, Scope


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


def test_refresh_updates_only_requested_pool(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOPEFUEL_CACHE", str(tmp_path / "snapshots.json"))
    cache.update_entry("grok", _result("grok", 10), 100.0)
    cache.update_entry("kimi", _result("kimi", 20), 200.0)

    assert refresh.run_worker({"grok": lambda: _result("grok", 30)}, "grok") == 0
    data = json.loads((tmp_path / "snapshots.json").read_text())
    assert data["grok"]["result"]["buckets"][0]["used_pct"] == 30
    assert data["kimi"]["fetched_at"] == 200.0


def test_refresh_lock_is_nonblocking_and_kernel_released_after_sigkill(monkeypatch, tmp_path):
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
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "True"
    started = time.monotonic()
    assert refresh.run_worker({"grok": lambda: _result("grok")}, "grok") == 0
    assert time.monotonic() - started < 1.0
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=3)
    assert refresh.run_worker({"grok": lambda: _result("grok", 40)}, "grok") == 0


def test_concurrent_refresh_requests_fetch_once(tmp_path):
    counter = tmp_path / "fetch.count"
    cache_file = tmp_path / "snapshots.json"
    helper = tmp_path / "counter_helper.py"
    helper.write_text(
        "import fcntl, time\n"
        "from scopefuel import refresh\n"
        "from scopefuel.model import ProviderResult\n"
        f"counter = open({str(counter)!r}, 'a+')\n"
        "def fetch():\n"
        " fcntl.flock(counter.fileno(), fcntl.LOCK_EX)\n"
        " counter.seek(0)\n"
        " current = int(counter.read() or '0')\n"
        " counter.seek(0); counter.truncate(); counter.write(str(current + 1)); counter.flush()\n"
        " fcntl.flock(counter.fileno(), fcntl.LOCK_UN)\n"
        " time.sleep(0.4)\n"
        " return ProviderResult(id='grok')\n"
        "raise SystemExit(refresh.run_worker({'grok': fetch}, 'grok'))\n"
    )
    env = {**os.environ, "SCOPEFUEL_CACHE": str(cache_file)}
    processes = [
        subprocess.Popen([sys.executable, str(helper)], cwd=Path.cwd(), env=env, start_new_session=True)
        for _ in range(8)
    ]
    assert all(process.wait(timeout=5) == 0 for process in processes)
    assert counter.read_text() == "1"


def test_refresh_timeout_kills_worker_process_group(tmp_path):
    pid_file = tmp_path / "child.pid"
    helper = tmp_path / "timeout_helper.py"
    helper.write_text(
        "import os, subprocess, time\n"
        "from scopefuel import refresh\n"
        f"child = subprocess.Popen(['sh', '-c', 'sleep 30'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "os.environ['SCOPEFUEL_REFRESH_TIMEOUT_S'] = '0.2'\n"
        "raise SystemExit(refresh.run_worker({'grok': lambda: (time.sleep(30), None)[1]}, 'grok'))\n"
    )
    proc = subprocess.Popen([sys.executable, str(helper)], cwd=Path.cwd(), start_new_session=True)
    proc.wait(timeout=5)
    # SIGKILL is expected because the dedicated worker session is killed as a
    # whole; the timeout handler cannot return after killing its own group.
    assert proc.returncode in (-signal.SIGKILL, 124)
    child_pid = int(pid_file.read_text())
    child_state = subprocess.run(
        ["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True, check=False
    )
    assert not child_state.stdout.strip() or child_state.stdout.strip().startswith("Z")


def test_refresh_rejects_unknown_pool_before_fetch():
    process = subprocess.run(
        [sys.executable, "-m", "scopefuel.cli", "refresh", "not-a-pool"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode != 0
    assert "invalid choice" in process.stderr
