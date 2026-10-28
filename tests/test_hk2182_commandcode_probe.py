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

import contextlib
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


def _stubborn_stub_cmd(tmp_path: pathlib.Path, log: pathlib.Path | None = None) -> pathlib.Path:
    """cmd stub reproducing the m1b fixture: an Enter read in the same chunk
    as the typed text is swallowed — the /usage panel never renders.

    N8 (verdict finding): the stub sleeps before its first read, so a caller that
    writes "/usage" and "\\r" back-to-back has both bytes queued and the stub
    reads them as ONE chunk (m1b's merged write) instead of racing a reader
    that was already blocked. Every chunk it reads is logged, so a test can
    assert the Enter never shares the text's read.
    """

    script = tmp_path / "cmd-stubborn"
    chunk_log = log or tmp_path / "cmd-stubborn-chunks.log"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time, tty\n"
        "tty.setraw(sys.stdin.fileno())\n"  # a TUI reads keystrokes, not lines
        "os.write(1, '❯ '.encode())\n"
        # A TUI takes a moment to start reading keys; anything written in
        # that window is one read, exactly like m1b's merged "/usage\\r".
        "time.sleep(0.25)\n"
        f"log = open({str(chunk_log)!r}, 'ab', buffering=0)\n"
        "typed = False\n"
        "while True:\n"
        "    chunk = os.read(0, 4096)\n"
        "    if not chunk:\n"
        "        break\n"
        "    log.write(chunk.replace(b'\\r', b'<CR>').replace(b'\\n', b'<LF>') + b'\\n')\n"
        "    if not typed and b'/usage' in chunk:\n"
        "        typed = True\n"
        "        os.write(1, '❯ /usage   \\n'.encode())\n"  # the input-box echo
        "        if b'\\r' in chunk or b'\\n' in chunk:\n"
        "            break\n"  # merged text+Enter: the TUI swallows it
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


def test_path_cmd_wins_over_known_install_dirs(monkeypatch, tmp_path):
    """S1 (verdict r1): PATH before known dirs — a mutant reordering them

    must go RED. Both locations hold a cmd; the PATH one must win.
    """

    home = tmp_path / "home"
    _stub_cmd(home / ".hermes" / "node" / "bin")
    path_dir = tmp_path / "path-bin"
    on_path = _stub_cmd(path_dir)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(path_dir))
    monkeypatch.setattr(commandcode, "BINARY", "cmd")
    binary, _tried = commandcode._resolve_binary()
    assert binary == str(on_path)


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


def test_known_dir_cmd_must_be_executable(monkeypatch, tmp_path):
    """N4 (verdict finding): a non-executable cmd in a known dir is skipped.

    A mutant that only checks existence would pick the dead file here.
    """

    home = tmp_path / "home"
    hermes = home / ".hermes" / "node" / "bin"
    hermes.mkdir(parents=True)
    dead = hermes / "cmd"
    dead.write_text("#!/bin/sh\n")
    dead.chmod(0o644)
    executable = _stub_cmd(home / ".local" / "bin")
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(commandcode, "BINARY", "cmd")
    binary, tried = commandcode._resolve_binary()
    assert binary == str(executable)
    assert str(dead) in tried


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
    assert result.raw["background_refresh"] is True
    assert spawned == [1]


def test_background_refresh_spawn_failure_still_serves_the_round(monkeypatch, tmp_path):
    """N19 (verdict finding): a refresh spawn that raises is swallowed — the round

    still serves cached/manual instead of failing. A mutant that catches only
    one exception type lets this RuntimeError escape -> RED.
    """

    monkeypatch.setattr(commandcode, "BINARY", str(_stub_cmd(tmp_path)))

    def explode(*_args):
        raise RuntimeError("spawn exploded")

    monkeypatch.setattr(refresh, "spawn_detached", explode)
    result = commandcode._deferred_fetch()
    assert result.error_kind == PROBE_IN_PROGRESS
    assert result.raw["background_refresh"] is True


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


def test_lock_records_the_holder_pid_and_survives_fd_inheritance(tmp_path):
    """N10 (verdict finding): the production pid write inside single_probe_lock —

    an owner that is SIGKILLed while a forked child pins the lock's inode
    must be reclaimed via its recorded pid. Mutant: drop the os.write of
    os.getpid() -> the owner record is empty, the reclaim never fires -> RED.
    """

    lock_dir = tmp_path / "locks"
    grandchild_pid = tmp_path / "grandchild.pid"
    code = (
        "import os, subprocess, sys, time\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})\n"
        "from scopefuel import proctrack\n"
        f"with proctrack.single_probe_lock({str(lock_dir)!r}) as acquired:\n"
        "    assert acquired\n"
        # A child that inherits the lock fd keeps the inode pinned after the
        # owner dies — the exact daemon-corpse shape from the incident.
        "    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], "
        "close_fds=False)\n"
        f"    open({str(grandchild_pid)!r}, 'w').write(str(child.pid))\n"
        "    sys.stdout.write(f'{os.getpid()}\\n'); sys.stdout.flush()\n"
        "    time.sleep(60)\n"
    )
    owner = subprocess.Popen(  # noqa: S603 - fixed argv, test helper
        [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    try:
        assert owner.stdout is not None
        owner_pid = int(owner.stdout.readline().strip())
        assert proctrack._read_lock_owner(lock_dir / ".probe.lock") == owner_pid
        os.kill(owner_pid, signal.SIGKILL)
        owner.wait(timeout=5)
        assert proctrack.probe_in_flight(lock_dir) is False
        with proctrack.single_probe_lock(lock_dir) as acquired:
            assert acquired is True
            assert proctrack._read_lock_owner(lock_dir / ".probe.lock") == os.getpid()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        # The fd-inheriting grandchild would otherwise outlive the test.
        if grandchild_pid.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(grandchild_pid.read_text()), signal.SIGKILL)


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


def test_unlabelled_lock_holder_counts_as_in_flight(tmp_path):
    """S4 (verdict r1): a held lock with no readable owner stays in-flight —

    an unknown holder is assumed live, never reclaimed.
    """

    lock_dir = tmp_path / "locks"
    holder = _lock_helper(lock_dir, owner="''")  # writes a blank owner line
    try:
        assert proctrack.probe_in_flight(lock_dir) is True
        with proctrack.single_probe_lock(lock_dir) as acquired:
            assert acquired is False
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


def test_reclaim_loses_to_a_competing_replacer(monkeypatch, tmp_path):
    """S5 (verdict r1): two reclaimers each lock their own replacement file —

    the inode check must make the loser report a miss, not hold the lock.
    A mutant deleting the check acquires on a stale inode -> RED.
    """

    lock_dir = tmp_path / "locks"
    holder = _lock_helper(lock_dir, owner=str(_dead_pid()))
    try:
        real_try = proctrack._try_lock_fd
        raced = {"done": False}

        def competing(path):
            fd = real_try(path)
            if fd is not None and not raced["done"]:
                raced["done"] = True
                rival = pathlib.Path(path).with_name(".probe.lock.rival")
                rival.write_text("")
                os.replace(rival, path)  # a second reclaimer wins the publish
            return fd

        monkeypatch.setattr(proctrack, "_try_lock_fd", competing)
        with proctrack.single_probe_lock(lock_dir) as acquired:
            assert acquired is False
    finally:
        holder.kill()
        holder.wait(timeout=5)


def test_spawn_detached_starts_the_worker_in_its_own_session(monkeypatch, tmp_path):
    """N3 (verdict finding): spawn_detached mechanics — a worker that shares the

    caller's session dies with it. Mutant: start_new_session=False -> RED.
    """

    calls = []

    class _Popen:
        def __init__(self, argv, **kwargs):
            calls.append({"argv": argv, **kwargs})

    monkeypatch.setattr(refresh.subprocess, "Popen", _Popen)
    assert refresh.spawn_detached("commandcode") is True
    assert len(calls) == 1
    call = calls[0]
    assert call["argv"] == [
        sys.executable,
        "-m",
        "scopefuel.cli",
        "refresh",
        "commandcode",
        "--_worker",
    ]
    assert call["start_new_session"] is True
    assert call["close_fds"] is True
    assert call["stdin"] is refresh.subprocess.DEVNULL


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

    renders the panel and this goes RED with stage=unparsed. N8 (verdict finding):
    the stub reads only after its start-up pause, so the two writes land as
    one chunk whenever the Enter does not wait for the echo; the chunk log
    then shows "/usage<CR>" in the text's read (or no panel at all).
    """

    chunk_log = tmp_path / "chunks.log"
    monkeypatch.setattr(commandcode, "BINARY", str(_stubborn_stub_cmd(tmp_path, log=chunk_log)))
    monkeypatch.setattr(commandcode, "PROBE_WORKDIR", tmp_path / "work")
    monkeypatch.setattr(commandcode, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(commandcode, "READY_SETTLE_S", 0.05)
    monkeypatch.setenv("SCOPEFUEL_COMMANDCODE_TIMEOUT_S", "5")
    result = commandcode.fetch()
    assert result.status == "ok"
    assert len(result.buckets) == 3
    chunks = chunk_log.read_bytes().splitlines()
    # The Enter must be a later read than the text: the probe waited for the
    # echo. A caller that submits immediately puts both in the first chunk.
    assert chunks[0] == b"/usage"
    assert b"<CR>" in chunks[1]


def test_silent_tui_gets_the_enter_from_the_echo_wait_fallback(monkeypatch, tmp_path):
    """N22 (verdict r2): a TUI that never echoes the typed text goes quiet —

    the idle-branch fallback is then the only path that ever writes the
    Enter. Mutant: drop that fallback -> the probe times out -> RED.
    """

    script = tmp_path / "cmd-silent-tui"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time, tty\n"
        "tty.setraw(sys.stdin.fileno())\n"
        "os.write(1, '❯ '.encode())\n"  # the prompt only — no input echo
        "while True:\n"
        "    chunk = os.read(0, 4096)\n"
        "    if not chunk:\n"
        "        break\n"
        "    if b'\\r' in chunk or b'\\n' in chunk:\n"
        f"        os.write(1, {USAGE_TEXT.encode()!r} + b'\\n')\n"
        "        time.sleep(1)\n"
    )
    script.chmod(0o755)
    monkeypatch.setattr(commandcode, "BINARY", str(script))
    monkeypatch.setattr(commandcode, "PROBE_WORKDIR", tmp_path / "work")
    monkeypatch.setattr(commandcode, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(commandcode, "READY_SETTLE_S", 0.05)
    monkeypatch.setenv("SCOPEFUEL_COMMANDCODE_TIMEOUT_S", "5")
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


def test_probe_input_carries_no_enter_and_submit_is_the_carriage_return():
    """S3 (verdict r1): '/usage\\r' back in PROBE_INPUT reintroduces the merged

    write on TUIs that coalesce — pin the two-write contract itself.
    """

    assert "\r" not in commandcode.PROBE_INPUT
    assert "\n" not in commandcode.PROBE_INPUT
    assert commandcode.PROBE_SUBMIT == "\r"


def test_unparsed_screen_end_to_end_names_the_missing_part(monkeypatch, tmp_path):
    """S2 (verdict r1): the real _probe_once must wire _missing_detail into

    the timeout — a silent stub that never renders the panel yields
    stage=unparsed with the missing part named.
    """

    script = tmp_path / "cmd-silent"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time, tty\n"
        "tty.setraw(sys.stdin.fileno())\n"
        "os.write(1, '❯ '.encode())\n"
        "time.sleep(60)\n"
    )
    script.chmod(0o755)
    monkeypatch.setattr(commandcode, "BINARY", str(script))
    monkeypatch.setattr(commandcode, "PROBE_WORKDIR", tmp_path / "work")
    monkeypatch.setattr(commandcode, "STARTUP_DELAY_S", 0)
    monkeypatch.setenv("SCOPEFUEL_COMMANDCODE_TIMEOUT_S", "5")
    result = commandcode.fetch()
    assert result.error_kind == "probe_timeout"
    assert "stage=unparsed" in result.error
    assert "missing:" in result.error
    assert result.raw["missing"] == "one or more usage windows are missing"


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
