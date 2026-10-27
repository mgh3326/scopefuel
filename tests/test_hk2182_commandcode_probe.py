"""hk 2182: commandcode probe binary path, deferred fetch, owner-pid lock.

(a) The cmd binary resolves from an explicit configured path
(``SCOPEFUEL_COMMANDCODE_BIN`` / ``[pools.commandcode] bin``), then PATH,
then known install dirs (``~/.hermes/node/bin`` …) — never a login shell.

(b) Short-budget callers never start or starve the PTY probe: the deferred
fetch serves the cached/manual value and starts at most one detached
``scopefuel refresh commandcode --_worker``, which writes the cache under
its own budget. The probe lock records a live owner pid and is reclaimed
when that owner is gone.

(c) An unparsable /usage screen fails closed naming the missing part.

Every probe test uses stub binaries — the real cmd is never run.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import signal
import subprocess
import sys

from scopefuel import cache, manual, proctrack, providers, refresh
from scopefuel.model import PROBE_IN_PROGRESS
from scopefuel.providers import commandcode

NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)

USAGE_TEXT = (
    "GOAT Plan · active / 0% used / 95 requests this month · 31 days to renew / "
    "5-hour 1% · resets in 3h 52m / Weekly 1% · resets in 6d 22h"
)
UNPARSED_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "cmd_usage_m1b_unparsed.synthetic.txt"
M1B_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "cmd_usage_m1b_screen.txt"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _stubborn_stub_cmd(tmp_path: pathlib.Path) -> pathlib.Path:
    """cmd stub reproducing the m1b fixture: an Enter read in the same chunk
    as the typed text is swallowed — the /usage panel never renders."""

    script = tmp_path / "cmd-stubborn"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time, tty\n"
        "tty.setraw(sys.stdin.fileno())\n"  # a TUI reads keystrokes, not lines
        "os.write(1, '❯ '.encode())\n"
        "typed = False\n"
        "buf = b''\n"
        "while True:\n"
        "    chunk = os.read(0, 4096)\n"
        "    if not chunk:\n"
        "        break\n"
        "    buf += chunk\n"
        "    if not typed and b'/usage' in buf:\n"
        "        typed = True\n"
        "        os.write(1, '❯ /usage   \\n'.encode())\n"  # the input-box echo
        "    elif typed and (b'\\r' in chunk or b'\\n' in chunk):\n"
        f"        os.write(1, {USAGE_TEXT.encode()!r} + b'\\n')\n"
        "        time.sleep(1)\n"
    )
    script.chmod(0o755)
    return script


def _stub_cmd(tmp_path: pathlib.Path, name: str = "cmd") -> pathlib.Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "print('❯ ', end='', flush=True)\n"
        "line = sys.stdin.readline()\n"
        "assert line.strip() == '/usage', line\n"
        f"print({USAGE_TEXT!r}, flush=True)\n"
        "time.sleep(1)\n"
    )
    script.chmod(0o755)
    return script


def _lock_helper(lock_dir: pathlib.Path, owner: int | str) -> subprocess.Popen:
    """A subprocess that flocks .probe.lock, records ``owner``, then sleeps."""

    lock_dir.mkdir(parents=True, exist_ok=True)
    code = (
        "import fcntl, os, sys, time\n"
        f"path = {str(lock_dir / '.probe.lock')!r}\n"
        "fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "os.ftruncate(fd, 0)\n"
        f"os.write(fd, (str({owner}) + '\\n').encode())\n"
        "sys.stdout.write('held\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(  # noqa: S603 - fixed argv, test helper
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "held"
    return process


def _dead_pid() -> int:
    process = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(process.stdout.strip())


# ---------------------------------------------------------------------------
# (a) binary resolution
# ---------------------------------------------------------------------------


def test_stub_cmd_under_hermes_dir_is_found_when_path_lacks_it(monkeypatch, tmp_path):
    home = tmp_path / "home"
    stub_dir = home / ".hermes" / "node" / "bin"
    _stub_cmd(stub_dir)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    (tmp_path / "empty-path").mkdir()
    monkeypatch.setattr(commandcode, "BINARY", "cmd")
    binary, tried = commandcode._resolve_binary()
    assert binary == str(stub_dir / "cmd")
    assert str(stub_dir / "cmd") in tried


def test_env_configured_binary_wins_over_path(monkeypatch, tmp_path):
    path_dir = tmp_path / "path-bin"
    _stub_cmd(path_dir)
    explicit_dir = tmp_path / "explicit"
    explicit = _stub_cmd(explicit_dir)
    monkeypatch.setenv("PATH", str(path_dir))
    monkeypatch.setattr(commandcode, "BINARY", str(explicit))
    binary, _tried = commandcode._resolve_binary()
    assert binary == str(explicit)


def test_config_bin_wins_over_path(monkeypatch, tmp_path, isolated_cache):
    path_dir = tmp_path / "path-bin"
    _stub_cmd(path_dir)
    configured_dir = tmp_path / "configured"
    configured = _stub_cmd(configured_dir)
    config = pathlib.Path(os.environ["XDG_CONFIG_HOME"]) / "scopefuel" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f'[pools.commandcode]\nbin = "{configured}"\n')
    monkeypatch.setenv("PATH", str(path_dir))
    monkeypatch.setattr(commandcode, "BINARY", "cmd")
    binary, _tried = commandcode._resolve_binary()
    assert binary == str(configured)


def test_missing_binary_error_names_the_paths_tried(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(commandcode, "BINARY", "cmd")
    result = commandcode.fetch()
    assert result.error_kind == "transport"
    assert "cmd 실행 파일 없음" in result.error
    assert ".hermes/node/bin" in result.error
    assert "PATH:cmd" in result.error


# ---------------------------------------------------------------------------
# (b) deferred fetch, single background refresh, lock ownership
# ---------------------------------------------------------------------------


def test_short_budget_fetch_serves_at_once_and_spawns_one_refresh(monkeypatch, tmp_path):
    """Mutant: the caller runs _probe_once inline -> this goes RED."""

    monkeypatch.setattr(
        commandcode, "_probe_once", lambda *_a: (_ for _ in ()).throw(AssertionError("inline probe"))
    )
    spawned = []
    monkeypatch.setattr(commandcode, "_spawn_background_refresh", lambda: spawned.append(1))
    monkeypatch.setattr(commandcode, "BINARY", str(_stub_cmd(tmp_path)))
    result = commandcode._deferred_fetch()
    assert result.error_kind == PROBE_IN_PROGRESS
    assert spawned == [1]


def test_held_probe_lock_means_in_flight_and_no_second_spawn(monkeypatch, tmp_path):
    """Mutant: spawn regardless of the lock -> the spawn count goes RED."""

    spawned = []
    monkeypatch.setattr(commandcode, "_spawn_background_refresh", lambda: spawned.append(1))
    monkeypatch.setattr(commandcode, "BINARY", str(_stub_cmd(tmp_path)))
    with proctrack.single_probe_lock(commandcode._probe_lock_dir()) as acquired:
        assert acquired is True
        result = commandcode._deferred_fetch()
    assert result.error_kind == PROBE_IN_PROGRESS
    assert spawned == []


def test_collect_serves_stale_value_for_short_budget_call(monkeypatch, tmp_path):
    """A stale-cache --json round is served by _in_progress_result, no probe."""

    spawned = []
    monkeypatch.setattr(commandcode, "_spawn_background_refresh", lambda: spawned.append(1))
    monkeypatch.setattr(commandcode, "BINARY", str(_stub_cmd(tmp_path)))
    monkeypatch.setattr(
        commandcode, "_probe_once", lambda *_a: (_ for _ in ()).throw(AssertionError("inline probe"))
    )
    good = commandcode.parse(USAGE_TEXT, now=NOW)
    good.fetched_at = 500.0
    cache.update_entry("commandcode", good, 500.0)
    now = 500.0 + cache.PROVIDER_TTL_S.get("commandcode", cache.DEFAULT_TTL_S) + 10.0
    results = cache.collect({"commandcode": providers.BUILTIN["commandcode"]}, ["commandcode"], now=now)
    assert results[0].stale is True
    assert results[0].error_kind == PROBE_IN_PROGRESS
    assert spawned == [1]


def test_collect_without_cache_still_offers_manual_fallback(monkeypatch, tmp_path):
    """No snapshot at all: the skipped round reaches the manual credit path."""

    spawned = []
    monkeypatch.setattr(commandcode, "_spawn_background_refresh", lambda: spawned.append(1))
    monkeypatch.setattr(commandcode, "BINARY", str(_stub_cmd(tmp_path)))
    manual.record_observation(
        pool="commandcode",
        window="credit",
        remaining_usd=0.75,
        total_usd=1.0,
        credit_expires_on=(NOW.date() + dt.timedelta(days=30)).isoformat(),
        measured_at=NOW,
        reason="cmd /usage credit balance",
        now=NOW,
    )
    results = cache.collect({"commandcode": providers.BUILTIN["commandcode"]}, ["commandcode"])
    assert results[0].error_kind == PROBE_IN_PROGRESS
    shown = manual.apply_for_display(results, now=NOW)
    assert shown[0].manual["selection"] == "manual_fallback"
    assert spawned == [1]


def test_killed_lock_owner_leaves_no_held_lock(tmp_path):
    """Mutant: the lock is kept after its owner dies -> acquire goes RED."""

    lock_dir = tmp_path / "locks"
    holder = _lock_helper(lock_dir, owner="os.getpid()")
    try:
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=5)
    finally:
        if holder.poll() is None:
            holder.kill()
    with proctrack.single_probe_lock(lock_dir) as acquired:
        assert acquired is True


def test_dead_owner_pid_is_reclaimed(tmp_path):
    """Mutant: no dead-owner reclaim -> the pinned inode keeps the lock (RED).

    An fd inherited by a detached process can pin the lock's inode long after
    the recorded owner is gone; the next caller must replace the file, not
    wait forever.
    """

    lock_dir = tmp_path / "locks"
    dead = _dead_pid()
    holder = _lock_helper(lock_dir, owner=str(dead))
    try:
        assert proctrack.probe_in_flight(lock_dir) is False
        with proctrack.single_probe_lock(lock_dir) as acquired:
            assert acquired is True
    finally:
        holder.kill()
        holder.wait(timeout=5)


def test_live_owner_lock_is_never_reclaimed(tmp_path):
    """Mutant: reclaim regardless of owner liveness -> this goes RED."""

    lock_dir = tmp_path / "locks"
    holder = _lock_helper(lock_dir, owner="os.getpid()")
    try:
        assert proctrack.probe_in_flight(lock_dir) is True
        with proctrack.single_probe_lock(lock_dir) as acquired:
            assert acquired is False
    finally:
        holder.kill()
        holder.wait(timeout=5)


def test_background_refresh_worker_writes_the_cache(monkeypatch, tmp_path):
    """Mutant: the detached refresh never writes the cache -> RED."""

    monkeypatch.setattr(commandcode, "BINARY", str(_stub_cmd(tmp_path)))
    monkeypatch.setattr(commandcode, "PROBE_WORKDIR", tmp_path / "work")
    monkeypatch.setattr(commandcode, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(commandcode, "READY_SETTLE_S", 0.05)
    rc = refresh.run_worker({"commandcode": providers.BUILTIN["commandcode"]}, "commandcode")
    assert rc == 0
    entry = cache._load().get("commandcode")
    assert isinstance(entry, dict)
    buckets = (entry.get("result") or {}).get("buckets") or []
    assert {bucket["window"] for bucket in buckets} == {"30d", "5h", "7d"}


def test_repeated_short_calls_never_stay_in_progress_forever(monkeypatch, tmp_path):
    """Polls at daemon cadence converge once the background refresh lands."""

    spawned = []
    monkeypatch.setattr(commandcode, "_spawn_background_refresh", lambda: spawned.append(1))
    monkeypatch.setattr(commandcode, "BINARY", str(_stub_cmd(tmp_path)))
    fetchers = {"commandcode": providers.BUILTIN["commandcode"]}

    first = cache.collect(fetchers, ["commandcode"], now=1000.0)
    assert first[0].error_kind == PROBE_IN_PROGRESS
    assert spawned == [1]

    with proctrack.single_probe_lock(commandcode._probe_lock_dir()) as acquired:
        assert acquired is True
        second = cache.collect(fetchers, ["commandcode"], now=1015.0)
        assert second[0].error_kind == PROBE_IN_PROGRESS
        assert spawned == [1]

    good = commandcode.parse(USAGE_TEXT, now=NOW)
    good.fetched_at = 1020.0
    cache.update_entry("commandcode", good, 1020.0)
    third = cache.collect(fetchers, ["commandcode"], now=1021.0)
    assert third[0].error_kind != PROBE_IN_PROGRESS
    assert third[0].stale is False


# ---------------------------------------------------------------------------
# (c) unparsed screens fail closed naming the missing part
# ---------------------------------------------------------------------------


def test_probe_writes_text_then_submits_enter_separately(monkeypatch, tmp_path):
    """Mutant: restore the single '/usage\\r' write -> the stubborn stub never

    renders the panel and this goes RED with stage=unparsed.
    """

    monkeypatch.setattr(commandcode, "BINARY", str(_stubborn_stub_cmd(tmp_path)))
    monkeypatch.setattr(commandcode, "PROBE_WORKDIR", tmp_path / "work")
    monkeypatch.setattr(commandcode, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(commandcode, "READY_SETTLE_S", 0.05)
    result = commandcode.fetch()
    assert result.status == "ok"
    assert len(result.buckets) == 3


def test_m1b_verbatim_screen_fails_closed_naming_the_missing_panel():
    """hk:doc fixtures/hk2182/m1b-usage-screen — '/usage' sat in the input box;

    no panel ever rendered, so the parse must name the missing windows.
    """

    result = commandcode.parse(M1B_FIXTURE.read_text(), now=NOW)
    assert result.error_kind == "parse_error"
    assert "usage windows are missing" in result.error
    detail = commandcode._missing_detail(M1B_FIXTURE.read_text())
    assert detail == "one or more usage windows are missing"


def test_m1b_synthetic_unparsed_screen_fails_closed():
    text = "\n".join(line for line in UNPARSED_FIXTURE.read_text().splitlines() if not line.startswith("#"))
    result = commandcode.parse(text, now=NOW)
    assert result.error_kind == "parse_error"
    assert "usage windows are missing" in result.error
    assert commandcode._missing_detail(text) == "one or more usage windows are missing"


def test_unparsed_probe_timeout_error_names_what_was_missing(monkeypatch, tmp_path):
    """stage=unparsed must say which /usage part was absent, not just bytes."""

    def boom(_binary):
        raise commandcode.ProbeTimeout(
            ["cmd", "--no-session"],
            30.0,
            stage="unparsed",
            nbytes=6383,
            detail=commandcode._missing_detail("GOAT Plan · 0% used · 31 days to renew"),
        )

    monkeypatch.setattr(commandcode, "_probe_once", boom)
    monkeypatch.setattr(commandcode, "BINARY", str(_stub_cmd(tmp_path)))
    result = commandcode.fetch()
    assert result.error_kind == "probe_timeout"
    assert "stage=unparsed" in result.error
    assert "missing:" in result.error
    assert "usage windows are missing" in result.error
    assert result.raw["missing"]
