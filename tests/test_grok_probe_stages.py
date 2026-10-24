"""hk 1868 — the grok /usage probe must say where it stalled, and stop stacking.

mbp evidence (hk 1868): panewire and herdr-event each start a fresh grok probe
about every 30 s while grok's own startup takes longer than that, so every
probe timed out with one opaque message — nobody could tell where it stalled.

The probe now reports the furthest stage it reached (no-output / no-prompt /
trust-prompt / no-panel / unparsed) with a byte count and never raw screen
text, holds a
per-pool flock in the cache dir for the whole probe, persists an exponential
backoff after each timeout, and reaps its whole process group on timeout.

No test here executes the real grok CLI: BINARY is redirected to a stub for
every case that spawns anything.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from scopefuel import cache, proctrack
from scopefuel.providers import grok

# A marker string the stubs paint on the PTY — it may stand for account
# details and must never appear in the error, hint or raw payload.
SENTINEL = "SENTINEL-ACCOUNT-DETAIL-9f3b"

# U+276F ❯ — the prompt marker the probe waits for before sending /usage.
PROMPT = "\\342\\235\\257"


def _write_stub(tmp_path: Path, name: str, body: str) -> Path:
    stub = tmp_path / name
    stub.write_text("#!/bin/sh\n" + body)
    stub.chmod(stub.stat().st_mode | 0o111)
    return stub


def _install(
    monkeypatch, tmp_path: Path, stub: Path, *, timeout: float = 3.0, workdir: Path | None = None
) -> Path:
    workdir = workdir if workdir is not None else tmp_path / "grok-probe-workdir"
    monkeypatch.setattr(grok, "BINARY", str(stub))
    monkeypatch.setattr(grok, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(grok, "TIMEOUT_S", timeout)
    monkeypatch.setattr(grok, "STARTUP_DELAY_S", 0.05)
    monkeypatch.setattr(grok, "USAGE_SETTLE_S", 0.05)
    monkeypatch.setattr(grok, "_credential_meta", lambda: {"credential_present": False})
    return workdir


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


def _assert_no_probe_left(workdir: Path, extra_pids=()) -> None:
    left = _wait_gone([*proctrack.pids_with_cwd(workdir, nested=True), *extra_pids])
    assert left == [], f"a timed-out probe left processes alive: {left}"


def _assert_stage(result, stage: str) -> None:
    assert result.error, f"expected a DEGRADED result, got {result.as_dict()}"
    assert f"stage={stage}" in result.error
    assert f"stage={stage}" in (result.hint or "")
    assert result.verdict.mark == "degraded"


def _assert_no_raw_text(result) -> None:
    assert SENTINEL not in result.error
    assert SENTINEL not in (result.hint or "")
    assert SENTINEL not in json.dumps(result.raw or {})


def _backoff_entry() -> dict:
    return cache._load_backoff()["pools"].get("grok") or {}


# ------------------------------------------------------------- stage names


def test_timeout_stage_no_output(tmp_path, monkeypatch):
    """The stub prints nothing forever — nobody can tell that today."""
    stub = _write_stub(tmp_path, "fake-grok-silent", "trap '' HUP\nexec sleep 120\n")
    workdir = _install(monkeypatch, tmp_path, stub)

    result = grok.fetch()

    _assert_stage(result, "no-output")
    assert "bytes=0" in result.error
    _assert_no_raw_text(result)
    _assert_no_probe_left(workdir)


def test_timeout_stage_output_but_no_prompt(tmp_path, monkeypatch):
    """Bytes arrive but the prompt marker never does — /usage is never sent."""
    stub = _write_stub(
        tmp_path,
        "fake-grok-noprompt",
        f"trap '' HUP\nprintf '{SENTINEL} boot banner\\r\\n'\nexec sleep 120\n",
    )
    workdir = _install(monkeypatch, tmp_path, stub)

    result = grok.fetch()

    _assert_stage(result, "no-prompt")
    assert "bytes=0" not in result.error  # bytes arrived, count must be > 0
    assert "bytes=" in result.error
    _assert_no_raw_text(result)
    _assert_no_probe_left(workdir)


def test_timeout_stage_prompt_sent_no_panel(tmp_path, monkeypatch):
    """Prompt seen, /usage sent, no Weekly panel ever painted."""
    stub = _write_stub(
        tmp_path,
        "fake-grok-nopanel",
        f"trap '' HUP\nprintf '{PROMPT} '\nIFS= read -r command\nprintf '{SENTINEL}\\r\\n'\nexec sleep 120\n",
    )
    workdir = _install(monkeypatch, tmp_path, stub)

    result = grok.fetch()

    _assert_stage(result, "no-panel")
    _assert_no_raw_text(result)
    _assert_no_probe_left(workdir)


def test_timeout_stage_panel_seen_but_unparsed(tmp_path, monkeypatch):
    """A Weekly fragment matches but the panel never parses (new format)."""
    stub = _write_stub(
        tmp_path,
        "fake-grok-unparsed",
        "trap '' HUP\n"
        f"printf '{PROMPT} '\n"
        "IFS= read -r command\n"
        f"printf 'Weekly limit: 58%%\\r\\nResets: not-a-date\\r\\n{SENTINEL}\\r\\n'\n"
        "exec sleep 120\n",
    )
    workdir = _install(monkeypatch, tmp_path, stub)

    result = grok.fetch()

    _assert_stage(result, "unparsed")
    _assert_no_raw_text(result)
    _assert_no_probe_left(workdir)


def test_timeout_reaps_forked_child_in_probe_group(tmp_path, monkeypatch):
    """Reap on timeout (defensive): the stub forks a child; the group dies.

    SIGTERM is trapped everywhere, so only the SIGKILL escalation of the
    probe's own process-group kill can end them — and the probe must wait
    for it rather than leaving it to the reaper's ttl. The forked child
    chdirs out of the instance dir, so the cwd sweep cannot see it either:
    only the group signal reaches it.
    """
    self_pidfile = tmp_path / "probe-self.pid"
    child_pidfile = tmp_path / "probe-child.pid"
    stub = _write_stub(
        tmp_path,
        "fake-grok-forker",
        "trap '' TERM HUP\n"
        "( trap '' TERM HUP; cd /; exec sleep 120 ) &\n"
        f"echo $! > {shlex.quote(str(child_pidfile))}\n"
        f"echo $$ > {shlex.quote(str(self_pidfile))}\n"
        f"printf '{PROMPT} '\n"
        "IFS= read -r command\n"
        f"printf '{SENTINEL}\\r\\n'\n"
        "exec sleep 120\n",
    )
    workdir = _install(monkeypatch, tmp_path, stub, timeout=5.0)

    probe = threading.Thread(target=lambda: grok.fetch())
    probe.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self_pidfile.exists() and child_pidfile.exists():
                break
            time.sleep(0.05)
        assert self_pidfile.exists() and child_pidfile.exists(), (
            "the stub never forked — fixture did not exercise the scenario"
        )
        probe.join(timeout=30)
        assert not probe.is_alive(), "probe did not finish at its timeout"
        pids = {int(self_pidfile.read_text()), int(child_pidfile.read_text())}
        _assert_no_probe_left(workdir, pids)
    finally:
        probe.join(timeout=30)
        for pidfile in (self_pidfile, child_pidfile):
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)


def test_timeout_reaps_group_when_only_the_leader_obeys_term(tmp_path, monkeypatch):
    """The leader dying on SIGTERM proves nothing about its stragglers.

    The hole this guards: wait() returns as soon as the leader exits, so a
    SIGKILL that only fires when the leader ignores TERM never runs — and a
    descendant that traps TERM and chdirs out of the instance dir survives
    both the escalation and the cwd sweep. The group kill is unconditional
    for exactly this shape.
    """
    self_pidfile = tmp_path / "probe-self.pid"
    child_pidfile = tmp_path / "probe-child.pid"
    stub = _write_stub(
        tmp_path,
        "fake-grok-term-ok",
        "( trap '' TERM HUP; cd /; exec sleep 120 ) &\n"
        f"echo $! > {shlex.quote(str(child_pidfile))}\n"
        f"echo $$ > {shlex.quote(str(self_pidfile))}\n"
        f"printf '{PROMPT} '\n"
        "IFS= read -r command\n"
        f"printf '{SENTINEL}\\r\\n'\n"
        "exec sleep 120\n",  # leader becomes sleep — default disposition dies on TERM
    )
    workdir = _install(monkeypatch, tmp_path, stub, timeout=5.0)

    probe = threading.Thread(target=lambda: grok.fetch())
    probe.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self_pidfile.exists() and child_pidfile.exists():
                break
            time.sleep(0.05)
        assert self_pidfile.exists() and child_pidfile.exists(), (
            "the stub never forked — fixture did not exercise the scenario"
        )
        probe.join(timeout=30)
        assert not probe.is_alive(), "probe did not finish at its timeout"
        pids = {int(self_pidfile.read_text()), int(child_pidfile.read_text())}
        _assert_no_probe_left(workdir, pids)
    finally:
        probe.join(timeout=30)
        for pidfile in (self_pidfile, child_pidfile):
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)


# ------------------------------------------------------- timeout env bounds


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 30.0),
        ("10", 10.0),
        ("120", 120.0),
        ("9.9", 30.0),
        ("120.1", 30.0),
        ("0", 30.0),
        ("-5", 30.0),
        ("abc", 30.0),
        ("", 30.0),
    ],
)
def test_probe_timeout_env_is_bounded(monkeypatch, raw, expected):
    """SCOPEFUEL_GROK_TIMEOUT_S is refused outside [10, 120] — default 30."""
    if raw is None:
        monkeypatch.delenv("SCOPEFUEL_GROK_TIMEOUT_S", raising=False)
    else:
        monkeypatch.setenv("SCOPEFUEL_GROK_TIMEOUT_S", raw)
    assert grok._probe_timeout_s() == expected


def test_probe_timeout_clamps_below_the_callers_armed_timer(monkeypatch):
    """A raised probe timeout must not outlive the refresh worker's SIGALRM.

    refresh.run_worker arms ITIMER_REAL at _timeout_seconds() (default 60 s):
    a probe timeout above that envelope is cut off before the stage report
    lands. The effective timeout is clamped below the *remaining* armed
    budget by the teardown margin; callers with no armed timer keep the
    full configured value.
    """
    monkeypatch.setenv("SCOPEFUEL_GROK_TIMEOUT_S", "120")
    assert grok._probe_timeout_s() == 120.0  # no timer armed — full budget

    signal.setitimer(signal.ITIMER_REAL, 60.0)  # refresh worker's default
    try:
        clamped = grok._probe_timeout_s()
        assert clamped <= 60.0 - grok.OUTER_TIMER_MARGIN_S
        assert clamped > 40.0  # the margin eats ~10 s, not the whole budget
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)

    # An envelope smaller than the margin leaves no usable budget at all.
    signal.setitimer(signal.ITIMER_REAL, 5.0)
    try:
        assert grok._probe_timeout_s() == 0.0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def test_fetch_reports_stage_inside_an_armed_outer_envelope(tmp_path, monkeypatch):
    """Under a refresh-like ITIMER_REAL the probe still lands its stage."""
    stub = _write_stub(tmp_path, "fake-grok-silent-env", "trap '' HUP\nexec sleep 120\n")
    workdir = _install(monkeypatch, tmp_path, stub)
    monkeypatch.setenv("SCOPEFUEL_GROK_TIMEOUT_S", "120")
    # 12 s armed envelope: without the clamp the probe would run to 120 s and
    # the SIGALRM would kill this process before any stage is reported; with
    # it the probe times out at ~2 s and reports — the test relies on that.
    signal.setitimer(signal.ITIMER_REAL, 12.0)
    try:
        started = time.monotonic()
        result = grok.fetch()
        elapsed = time.monotonic() - started
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    _assert_stage(result, "no-output")
    assert elapsed < 11.0, "the probe outlived its clamped share of the envelope"
    entry = _backoff_entry()
    assert entry.get("consecutive") == 1  # the staged timeout still records backoff
    _assert_no_probe_left(workdir)


def test_fetch_with_no_outer_budget_reports_no_output_without_spawning(tmp_path, monkeypatch):
    """An envelope smaller than the margin must not even spawn the stub."""
    pidfile = tmp_path / "stub-pids"
    stub = _write_stub(
        tmp_path,
        "fake-grok-no-budget",
        f"echo $$ >> {shlex.quote(str(pidfile))}\nexec sleep 120\n",
    )
    workdir = _install(monkeypatch, tmp_path, stub)
    monkeypatch.setenv("SCOPEFUEL_GROK_TIMEOUT_S", "120")
    signal.setitimer(signal.ITIMER_REAL, 5.0)  # < margin → zero probe budget
    try:
        result = grok.fetch()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    _assert_stage(result, "no-output")
    assert not pidfile.exists(), "a zero-budget probe still spawned the stub"
    _assert_no_probe_left(workdir)


# ------------------------------------------------------- per-pool lock


def test_two_concurrent_probes_spawn_exactly_one_stub(tmp_path, monkeypatch):
    """One probe in flight per pool, across callers — the second is skipped."""
    pidfile = tmp_path / "stub-pids"
    stub = _write_stub(
        tmp_path,
        "fake-grok-slow",
        f"echo $$ >> {shlex.quote(str(pidfile))}\n"
        "trap '' HUP\n"
        f"printf '{PROMPT} '\n"
        "IFS= read -r command\n"
        "exec sleep 120\n",
    )
    workdir = _install(monkeypatch, tmp_path, stub, timeout=5.0)

    results = []
    errors = []

    def run() -> None:
        try:
            results.append(grok.fetch())
        except Exception as exc:  # noqa: BLE001 - reported by the assert below
            errors.append(exc)

    first = threading.Thread(target=run)
    first.start()
    time.sleep(1.0)  # first caller takes the lock and spawns the stub
    second = threading.Thread(target=run)
    second.start()
    first.join(timeout=30)
    second.join(timeout=30)

    assert errors == []
    spawned = pidfile.read_text().split() if pidfile.exists() else []
    assert len(spawned) == 1, f"both callers spawned a grok: {spawned}"
    stages = [
        "stage=probe-in-flight" in (r.error or "") or "stage=probe-in-flight" in (r.hint or "")
        for r in results
    ]
    assert stages.count(True) == 1, (
        f"exactly one caller must report probe-in-flight: {[r.error for r in results]}"
    )
    _assert_no_probe_left(workdir)


def test_probe_lock_is_freed_when_holder_dies(tmp_path, monkeypatch):
    """The lock is an flock in the cache dir — a dead holder frees it."""
    stub = _write_stub(
        tmp_path,
        "fake-grok-ok",
        f"printf '{PROMPT} '\n"
        "IFS= read -r command\n"
        "printf 'Weekly limit: 42%%\\r\\nNext reset: August 14, 04:58\\r\\n'\n"
        "exit 0\n",
    )
    _install(monkeypatch, tmp_path, stub)
    lock_dir = grok._probe_lock_dir()
    ready = tmp_path / "lock-held"
    helper = tmp_path / "lock_holder.py"
    helper.write_text(
        textwrap.dedent(
            f"""
            import sys
            import time
            from pathlib import Path

            sys.path.insert(0, {str(Path("src").resolve())!r})
            from scopefuel import proctrack

            with proctrack.single_probe_lock(Path({str(lock_dir)!r})) as acquired:
                assert acquired
                Path({str(ready)!r}).write_text("held")
                time.sleep(120)
            """
        )
    )

    holder = subprocess.Popen(
        [sys.executable, str(helper)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists(), "helper never took the probe lock"

        result = grok.fetch()
        assert "stage=probe-in-flight" in (result.error or "")

        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=10)

        result = grok.fetch()
        assert result.error is None, f"lock not freed after holder death: {result.error}"
        assert result.buckets[0].used_pct == 42.0
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_held_lock_returns_cached_result_via_collect(tmp_path, monkeypatch):
    """A second caller that finds the lock held gets the last cached result."""
    now = time.time()
    seeded = grok.ProviderResult(
        id="grok",
        buckets=[
            grok.Bucket(
                label="weekly",
                window="7d",
                used_pct=11.0,
                resets_at=dt.datetime.fromtimestamp(now + 3600, dt.UTC).isoformat(),
                scope=grok.Scope("account"),
                horizon="week",
            )
        ],
        source="test",
        pool_class="spend",
    )
    cache.update_entry("grok", seeded, now - 400.0)  # inside grok's 1800s TTL

    stub = _write_stub(tmp_path, "fake-grok-should-not-run", "exit 9\n")
    _install(monkeypatch, tmp_path, stub)

    with proctrack.single_probe_lock(grok._probe_lock_dir()) as acquired:
        assert acquired is True
        result = cache.collect({"grok": grok.fetch}, ["grok"], now=now, use_cache=False)[0]

    assert result.error is None
    assert result.buckets and result.buckets[0].used_pct == 11.0
    assert "탐침 진행 중" in (result.note or "")


# --------------------------------------------------------------- backoff


def test_timeout_persists_backoff_and_blocks_next_probe(tmp_path, monkeypatch):
    """After a timeout a caller inside the window spawns nothing."""
    pidfile = tmp_path / "stub-pids"
    stub = _write_stub(
        tmp_path,
        "fake-grok-hang",
        f"echo $$ >> {shlex.quote(str(pidfile))}\ntrap '' HUP\nexec sleep 120\n",
    )
    _install(monkeypatch, tmp_path, stub, timeout=3.0)

    first = grok.fetch()
    _assert_stage(first, "no-output")

    entry = _backoff_entry()
    assert entry.get("consecutive") == 1
    assert entry.get("next_allowed_at", 0) > time.time()
    assert entry.get("last_timeout_at")

    probed = []

    def spy() -> str:
        probed.append(True)
        return "Weekly limit: 1%\nNext reset: August 14, 04:58\n"

    monkeypatch.setattr(grok, "_probe_once", spy)
    second = grok.fetch()

    assert probed == [], "backoff window must not spawn a probe"
    assert "stage=backoff" in (second.error or "")
    assert "stage=backoff" in (second.hint or "")
    assert second.backoff_until == pytest.approx(entry["next_allowed_at"])
    assert pidfile.read_text().split().__len__() == 1  # still only the first stub


def test_backoff_consecutive_timeouts_double_the_window_up_to_cap(monkeypatch):
    monkeypatch.setattr(grok, "BACKOFF_BASE_S", 60.0)
    monkeypatch.setattr(grok, "BACKOFF_CAP_S", 1800.0)
    now = 1_000_000.0
    windows = ((1, 60.0), (2, 120.0), (3, 240.0), (4, 480.0), (5, 960.0), (6, 1800.0), (7, 1800.0))
    for consecutive, window in windows:
        until = grok._note_probe_timeout(now)
        entry = _backoff_entry()
        assert entry["consecutive"] == consecutive
        assert entry["last_timeout_at"] == now
        assert until == pytest.approx(now + window)
        now += window + 1.0  # next timeout lands just past the window


def test_backoff_base_and_cap_env_bounds(monkeypatch):
    monkeypatch.setenv("SCOPEFUEL_GROK_BACKOFF_BASE_S", "120")
    monkeypatch.setenv("SCOPEFUEL_GROK_BACKOFF_CAP_S", "240")
    until = grok._note_probe_timeout(1_000.0)
    assert until == pytest.approx(1_000.0 + 120.0)

    monkeypatch.setenv("SCOPEFUEL_GROK_BACKOFF_BASE_S", "5")
    monkeypatch.setenv("SCOPEFUEL_GROK_BACKOFF_CAP_S", "999999")
    state = cache._load_backoff()
    state["pools"].pop("grok", None)
    cache._save_backoff(state)
    until = grok._note_probe_timeout(1_000.0)
    assert until == pytest.approx(1_000.0 + 60.0)  # refused → defaults


def test_probe_runs_again_after_backoff_window(monkeypatch, tmp_path):
    stub = _write_stub(tmp_path, "fake-grok-unused", "exit 9\n")
    _install(monkeypatch, tmp_path, stub)
    state = cache._load_backoff()
    state["pools"]["grok"] = {
        "consecutive": 2,
        "next_allowed_at": time.time() - 1.0,  # window already closed
        "last_timeout_at": time.time() - 61.0,
        "updated_at": time.time() - 61.0,
    }
    cache._save_backoff(state)

    probed = []
    monkeypatch.setattr(
        grok,
        "_probe_once",
        lambda: probed.append(True) or "Weekly limit: 42%\nNext reset: August 14, 04:58\n",
    )
    result = grok.fetch()

    assert probed == [True], "a caller past the backoff window must probe again"
    assert result.error is None
    # the first successful parse resets the consecutive count to 0
    assert _backoff_entry() == {}


def test_successful_parse_resets_backoff(monkeypatch, tmp_path):
    stub = _write_stub(tmp_path, "fake-grok-unused", "exit 9\n")
    _install(monkeypatch, tmp_path, stub)
    state = cache._load_backoff()
    state["pools"]["grok"] = {
        "consecutive": 3,
        "next_allowed_at": time.time() - 1.0,
        "updated_at": time.time() - 61.0,
    }
    cache._save_backoff(state)

    monkeypatch.setattr(grok, "_probe_once", lambda: "Weekly limit: 42%\nNext reset: August 14, 04:58\n")
    result = grok.fetch()

    assert result.error is None
    assert _backoff_entry() == {}


def test_collect_does_not_spawn_during_backoff(monkeypatch, tmp_path):
    """Every collect caller (status/herdr-event/panewire path) stays off the binary."""
    stub = _write_stub(tmp_path, "fake-grok-unused", "exit 9\n")
    _install(monkeypatch, tmp_path, stub)
    now = time.time()
    grok._note_probe_timeout(now)

    def boom():
        raise AssertionError("collect must not call the fetcher inside backoff")

    results = cache.collect({"grok": boom}, ["grok"], now=now + 1.0, use_cache=False)
    assert "backoff" in (results[0].error or "")
    assert results[0].backoff_until and results[0].backoff_until > now


# ------------------------------------------- hk 1964: grok 1.0.70 folder trust


# The exact folder-trust question desk captured from grok 1.0.70 (hk 1964
# comment 7229 — the screen also shows the probed path and Yes/No rows, but
# the probe matches this sentence only, after ANSI stripping).
TRUST_QUESTION = "Do you trust the contents of this directory?"


def _write_trust_stub(tmp_path: Path, name: str = "fake-grok-1.0.70") -> Path:
    """A grok 1.0.70 that gates on folder trust before printing its prompt.

    Like the real CLI: it asks TRUST_QUESTION and waits — unless
    ``git rev-parse --show-toplevel`` in its cwd resolves to
    FAKE_GROK_TRUSTED_ROOT (the probe keeps PROBE_WORKDIR a git root so one
    desk trust entry for it covers every ``probe-*`` instance dir).
    FAKE_GROK_ASK_ALWAYS=1 asks even inside the trusted root — m1's
    ~/.grok/hooks mode (desk g70b). FAKE_GROK_THEN_PROMPT=1 still prints the
    prompt after asking, in the same write, for the prompt-wins case.
    """

    return _write_stub(
        tmp_path,
        name,
        "top=$(git rev-parse --show-toplevel 2>/dev/null)\n"
        'if [ -n "$top" ]; then top=$(cd "$top" 2>/dev/null && pwd -P); fi\n'
        "trusted=\n"
        'if [ -n "${FAKE_GROK_TRUSTED_ROOT:-}" ]; then\n'
        '    trusted=$(cd "$FAKE_GROK_TRUSTED_ROOT" 2>/dev/null && pwd -P)\n'
        "fi\n"
        "ask=1\n"
        'if [ -n "$top" ] && [ "$top" = "$trusted" ]; then ask=; fi\n'
        'if [ -n "${FAKE_GROK_ASK_ALWAYS:-}" ]; then ask=1; fi\n'
        'if [ -n "$ask" ]; then\n'
        '    if [ -n "${FAKE_GROK_THEN_PROMPT:-}" ]; then\n'
        f"        printf '{TRUST_QUESTION}\\r\\n{SENTINEL}\\r\\n{PROMPT} '\n"
        "    else\n"
        f"        printf '{TRUST_QUESTION}\\r\\n'\n"
        f"        printf '{SENTINEL}\\r\\n'\n"
        "        exec sleep 120\n"
        "    fi\n"
        "else\n"
        f"    printf '{PROMPT} '\n"
        "fi\n"
        "IFS= read -r command\n"
        "printf 'Weekly limit: 42%%\\r\\nNext reset: August 14, 04:58\\r\\n'\n"
        "exit 0\n",
    )


def test_fresh_workdir_probe_is_git_rooted_and_parses(tmp_path, monkeypatch):
    """A first probe in a workdir that does not exist yet works.

    The probe keeps PROBE_WORKDIR the root of a git repository so one
    desk-side trust entry for it covers every ``probe-*`` instance dir —
    grok 1.0.70 inherits folder trust only within the same git repository.
    On main the stub's trust question stalls the probe as stage=no-prompt:
    the RED.
    """

    workdir = _install(monkeypatch, tmp_path, _write_trust_stub(tmp_path))
    monkeypatch.setenv("FAKE_GROK_TRUSTED_ROOT", str(workdir))
    assert not workdir.exists(), "this case starts from a missing workdir"

    result = grok.fetch()

    assert result.error is None, f"trusted workdir probe failed: {result.error}"
    assert result.buckets[0].used_pct == 42.0
    assert (workdir / ".git").exists(), "the probe did not leave the workdir a git root"
    # kill_stale_probe_leftovers and the instance-dir lifecycle leave the
    # .git root in place — it is not a ``probe-*`` dir.
    proctrack.kill_stale_probe_leftovers(workdir)
    assert (workdir / ".git").exists()
    _assert_no_probe_left(workdir)


def test_git_init_runs_once_not_once_per_probe(tmp_path, monkeypatch):
    """The second probe finds workdir/.git and does not run git init again."""

    real_git = shutil.which("git")
    assert real_git, "this test needs git on PATH"
    calls = tmp_path / "git-calls.log"
    bindir = tmp_path / "git-spy-bin"
    bindir.mkdir()
    _write_stub(
        bindir,
        "git",
        f'printf \'%s\\n\' "$*" >> {shlex.quote(str(calls))}\nexec {shlex.quote(real_git)} "$@"\n',
    )
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    workdir = _install(monkeypatch, tmp_path, _write_trust_stub(tmp_path))
    monkeypatch.setenv("FAKE_GROK_TRUSTED_ROOT", str(workdir))

    first = grok.fetch()
    second = grok.fetch()

    assert first.error is None and second.error is None, (first.error, second.error)
    inits = [line for line in calls.read_text().splitlines() if line.strip() == "init -q"]
    assert inits == ["init -q"], f"git init calls: {calls.read_text()}"
    _assert_no_probe_left(workdir)


def test_untrusted_workdir_reports_trust_prompt_without_the_full_timeout(tmp_path, monkeypatch):
    """Trust question seen and prompt never seen → stage=trust-prompt, at once.

    It counts as a probe timeout for backoff exactly like no-prompt does,
    and the error/hint carry no screen text — only the workdir path.
    """

    workdir = _install(monkeypatch, tmp_path, _write_trust_stub(tmp_path), timeout=5.0)
    monkeypatch.setenv("FAKE_GROK_TRUSTED_ROOT", str(tmp_path / "trusted-elsewhere"))

    started = time.monotonic()
    result = grok.fetch()
    elapsed = time.monotonic() - started

    _assert_stage(result, "trust-prompt")
    assert elapsed < 5.0, "the probe waited out its whole timeout on the trust question"
    assert str(workdir) in (result.hint or ""), "the hint must name the workdir"
    _assert_no_raw_text(result)
    assert _backoff_entry().get("consecutive") == 1
    _assert_no_probe_left(workdir)


def test_trust_question_inside_the_git_root_is_still_trust_prompt(tmp_path, monkeypatch):
    """m1 hooks mode (desk g70b): ~/.grok/hooks makes grok ask in every
    folder, so a git-rooted workdir does not help there — the stage must
    still be trust-prompt with the workdir-only hint, not no-prompt or
    unparsed."""

    workdir = _install(monkeypatch, tmp_path, _write_trust_stub(tmp_path))
    monkeypatch.setenv("FAKE_GROK_TRUSTED_ROOT", str(workdir))
    monkeypatch.setenv("FAKE_GROK_ASK_ALWAYS", "1")

    result = grok.fetch()

    _assert_stage(result, "trust-prompt")
    assert str(workdir) in (result.hint or "")
    _assert_no_raw_text(result)
    _assert_no_probe_left(workdir)


def test_a_seen_prompt_wins_over_the_trust_question(tmp_path, monkeypatch):
    """Precedence: question and prompt both visible → /usage is still sent.

    trust-prompt applies only when the question is seen and the prompt is
    not; the stub prints both in one write, so the prompt check must be
    consulted before the trust-question check.
    """

    _install(monkeypatch, tmp_path, _write_trust_stub(tmp_path))
    monkeypatch.setenv("FAKE_GROK_ASK_ALWAYS", "1")
    monkeypatch.setenv("FAKE_GROK_THEN_PROMPT", "1")

    result = grok.fetch()

    assert result.error is None, f"prompt was visible but /usage never went out: {result.error}"
    assert result.buckets[0].used_pct == 42.0


def test_missing_git_leaves_the_probe_to_report_trust_prompt(tmp_path, monkeypatch):
    """No git on PATH: the init fails open (never raises) and the question
    the CLI then asks is still stage=trust-prompt."""

    bindir = tmp_path / "no-git-bin"
    bindir.mkdir()
    (bindir / "sleep").symlink_to("/bin/sleep")  # the stub needs sleep; no git here
    monkeypatch.setenv("PATH", str(bindir))

    workdir = _install(monkeypatch, tmp_path, _write_trust_stub(tmp_path))
    monkeypatch.setenv("FAKE_GROK_TRUSTED_ROOT", str(workdir))

    result = grok.fetch()

    _assert_stage(result, "trust-prompt")
    assert str(workdir) in (result.hint or "")
    _assert_no_raw_text(result)
    _assert_no_probe_left(workdir)


def test_workdir_nested_in_another_repo_becomes_its_own_root(tmp_path, monkeypatch):
    """A PROBE_WORKDIR inside another repository still gets its own .git.

    git rev-parse from an instance dir then answers the workdir, not the
    outer root — and the probe leaves the enclosing repository untouched.
    """

    outer = tmp_path / "outer-repo"
    outer.mkdir()
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    workdir = outer / "grok-probe-workdir"
    workdir.mkdir()
    (workdir / "seed").write_text("x\n")  # the dir shows in outer status either way

    def outer_status() -> str:
        return subprocess.run(
            ["git", "-C", str(outer), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    before = outer_status()

    _install(monkeypatch, tmp_path, _write_trust_stub(tmp_path), workdir=workdir)
    monkeypatch.setenv("FAKE_GROK_TRUSTED_ROOT", str(workdir))

    result = grok.fetch()

    assert result.error is None, f"nested workdir probe failed: {result.error}"
    assert (workdir / ".git").exists()
    assert outer_status() == before, "the probe changed the enclosing repository"
    _assert_no_probe_left(workdir)
