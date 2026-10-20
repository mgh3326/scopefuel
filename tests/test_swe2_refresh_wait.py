"""hk 1754: the swe2 gate waits, bounded, for a probe another worker holds in flight."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from scopefuel import cache, cli, recommend, refresh
from scopefuel.model import (
    PROBE_IN_PROGRESS,
    SWE2_BACKOFF,
    SWE2_IN_FLIGHT_TIMEOUT,
    Bucket,
    ProviderResult,
    Scope,
)

RUNGS = ("swe-2-high", "swe-2-medium", "swe-2-max")
REFRESH_SWE2 = refresh.refresh_swe2_result


def probe(**changes) -> ProviderResult:
    buckets = [
        Bucket("daily", "1d", 10.0, scope=Scope("account"), horizon="now"),
        Bucket("weekly", "7d", 59.0, scope=Scope("account"), horizon="week"),
        *[Bucket(rung, "", 0.0, scope=Scope("model", rung), horizon="now") for rung in RUNGS],
    ]
    return ProviderResult(id="devin", pool_class="spend", buckets=buckets, **changes)


def stale_probe() -> ProviderResult:
    return probe(stale=True, age_s=120.0, fetched_at=time.time() - 120)


@pytest.fixture(autouse=True)
def isolated_gate(monkeypatch):
    """The same off-band stubs test_swe2_gate_fallback uses: no subprocess,
    shadow, or bench work may leak out of a gate run."""
    monkeypatch.setattr(recommend, "_observe_exhaustion", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_shadow_gate", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_emit_stale_build_warning", lambda: None)
    monkeypatch.setattr(cli.bench, "read_scores", lambda: [])
    monkeypatch.setattr(cli.bench, "read_prices", lambda: {})
    monkeypatch.setattr(cli.bench, "runtime_grade_table", lambda: recommend.GRADE_TABLE)


def cli_probe(monkeypatch, result):
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: result})
    monkeypatch.setattr(cli, "collect", lambda *a, **kw: [result])


def stub_worker(monkeypatch, rc: int) -> None:
    """A fake --_swe2 worker that exits with the given code."""
    monkeypatch.setattr(
        refresh.subprocess,
        "Popen",
        lambda *a, **kw: SimpleNamespace(pid=424242, wait=lambda **kw: rc),
    )


def land_entry(delay_s: float, result: ProviderResult, *, poisoned: bool = False):
    """Write a fresh devin entry after delay_s — the in-flight probe landing.

    poisoned=True stamps a last_error_at newer than fetched_at inside the same
    locked write, so the poll can never observe the entry without it.
    """

    def write() -> None:
        time.sleep(delay_s)
        landed = time.time()
        if not poisoned:
            cache.update_entry("devin", result, landed)
            return
        with cache._exclusive_cache_lock():
            data = cache._load()
            entry = cache._to_entry(result, landed)
            entry["last_error_at"] = landed + 1.0
            data["devin"] = entry
            cache._save(data)

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    return thread


def small_wait(monkeypatch) -> None:
    """Keep the bounded wait bounded inside a test's budget."""
    monkeypatch.setattr(refresh, "_in_flight_wait_s", lambda: 0.6)
    monkeypatch.setattr(refresh, "SWE2_IN_FLIGHT_POLL_S", 0.05)


def stub_signals(monkeypatch) -> None:
    """run_worker in-process must not arm the real SIGALRM kill timer."""
    monkeypatch.setattr(refresh.signal, "signal", lambda *a: None)
    monkeypatch.setattr(refresh.signal, "setitimer", lambda *a: None)


def seed_backoff(remaining_s: float) -> None:
    cache._save_backoff(
        {
            "schema": cache.BACKOFF_SCHEMA,
            "pools": {
                "devin": {
                    "consecutive": 1,
                    "next_allowed_at": time.time() + remaining_s,
                    "updated_at": time.time(),
                }
            },
        }
    )


# --- worker side: the --_swe2 worker reports why it skipped (AC1) ---


def test_swe2_worker_lock_held_reports_distinct_rc(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOPEFUEL_CACHE", str(tmp_path / "snapshots.json"))
    with refresh.pool_lock("devin") as acquired:
        assert acquired
        assert refresh.run_worker({"devin": lambda: probe()}, "devin", swe2=True) == refresh.SWE2_RC_LOCK_HELD
        # Every other caller keeps rc 0 for the same skip.
        assert refresh.run_worker({"devin": lambda: probe()}, "devin") == 0


def test_swe2_worker_backoff_reports_distinct_rc(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOPEFUEL_CACHE", str(tmp_path / "snapshots.json"))
    stub_signals(monkeypatch)
    seed_backoff(300.0)
    assert refresh.run_worker({"devin": lambda: probe()}, "devin", swe2=True) == refresh.SWE2_RC_BACKOFF
    assert refresh.run_worker({"devin": lambda: probe()}, "devin") == 0


def test_swe2_worker_probe_in_progress_reports_distinct_rc(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOPEFUEL_CACHE", str(tmp_path / "snapshots.json"))
    stub_signals(monkeypatch)
    monkeypatch.setattr(refresh, "_v2_capture", lambda *a: {})
    skipped = ProviderResult(id="devin", error="probe already running", error_kind=PROBE_IN_PROGRESS)
    assert (
        refresh.run_worker({"devin": lambda: skipped}, "devin", swe2=True)
        == refresh.SWE2_RC_PROBE_IN_PROGRESS
    )
    # A probe-in-progress skip is not a measurement: no cache entry is written.
    assert cache._load().get("devin") is None
    assert refresh.run_worker({"devin": lambda: skipped}, "devin") == 0


# --- parent side: bounded wait for the in-flight landing (AC2) ---


def test_lock_held_skip_then_landing_admits_model_scope(monkeypatch, capsys):
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)
    cli_probe(monkeypatch, stale)
    monkeypatch.setattr(refresh, "refresh_swe2_result", REFRESH_SWE2)
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)
    thread = land_entry(2.0, probe())
    try:
        assert cli.main(["gate", "-m", "builder-devin", "--no-cache"]) == 0
    finally:
        thread.join(timeout=10)
    assert "basis=model-scope:swe-2-high" in capsys.readouterr().out


def test_probe_in_progress_skip_also_waits_for_landing(monkeypatch):
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)
    stub_worker(monkeypatch, refresh.SWE2_RC_PROBE_IN_PROGRESS)
    thread = land_entry(0.4, probe())
    try:
        result = REFRESH_SWE2()
    finally:
        thread.join(timeout=10)
    assert result is not None and not result.error
    assert result.fetched_at > stale.fetched_at


def test_in_flight_entry_that_never_lands_times_out_named(monkeypatch):
    small_wait(monkeypatch)
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)
    result = REFRESH_SWE2()
    assert result is not None
    assert result.error_kind == SWE2_IN_FLIGHT_TIMEOUT
    assert "in-flight probe did not land in time" in result.error


def test_in_flight_timeout_is_named_rc4_at_the_gate(monkeypatch, capsys):
    small_wait(monkeypatch)
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)
    cli_probe(monkeypatch, stale)
    monkeypatch.setattr(refresh, "refresh_swe2_result", REFRESH_SWE2)
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)
    assert cli.main(["gate", "-m", "builder-devin", "--no-cache"]) == 4
    err = capsys.readouterr().err
    assert "swe2_measurement_unavailable" in err
    assert "state=in-flight-timeout" in err
    assert "in-flight probe did not land in time" in err


def test_landing_with_newer_last_error_never_admits(monkeypatch):
    """A landed entry whose last_error_at is at/after fetched_at fails the
    no-newer-last_error_at check — the wait runs out instead of admitting it."""
    small_wait(monkeypatch)
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)
    thread = land_entry(0.1, probe(), poisoned=True)
    try:
        result = REFRESH_SWE2()
    finally:
        thread.join(timeout=10)
    assert result is not None
    assert result.error_kind == SWE2_IN_FLIGHT_TIMEOUT


def test_wait_never_admits_the_entry_seen_before_waiting(monkeypatch):
    """Mutant invariant: the pre-wait entry itself is never the answer — only
    a strictly newer fetched_at admits, even after the bound expires."""
    small_wait(monkeypatch)
    before = time.time() - 5
    cache.update_entry("devin", probe(), before)
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)
    result = REFRESH_SWE2()
    assert result is not None
    assert result.error_kind == SWE2_IN_FLIGHT_TIMEOUT
    assert result.fetched_at != before


def test_wait_bound_is_worker_timeout_plus_margin(monkeypatch):
    """R2-1: the bound derives from the worker hard cap, not one probe phase."""
    monkeypatch.setenv("SCOPEFUEL_REFRESH_TIMEOUT_S", "60")
    assert refresh.SWE2_IN_FLIGHT_WAIT_MARGIN_S >= 5.0
    assert refresh._in_flight_wait_s() == refresh._timeout_seconds() + refresh.SWE2_IN_FLIGHT_WAIT_MARGIN_S
    monkeypatch.setenv("SCOPEFUEL_REFRESH_TIMEOUT_S", "12")
    assert refresh._in_flight_wait_s() == 12 + refresh.SWE2_IN_FLIGHT_WAIT_MARGIN_S
    assert refresh.SWE2_IN_FLIGHT_POLL_S < 1.0


def test_landing_at_1_7x_probe_timeout_is_admitted(monkeypatch):
    """R2-1: a landing at 1.7 x the devin probe timeout — inside the worker's
    two sequential probe phases — still admits; the old provider-timeout
    bound of TIMEOUT_S + margin would have refused it."""
    from scopefuel.providers import devin

    monkeypatch.setenv("SCOPEFUEL_REFRESH_TIMEOUT_S", "60")
    late = 1.7 * devin.TIMEOUT_S
    assert refresh._in_flight_wait_s() > late
    clock = {"mono": 1000.0, "wall": 1_700_000_000.0}
    monkeypatch.setattr(refresh.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(refresh.time, "time", lambda: clock["wall"])

    def fake_sleep(seconds: float) -> None:
        clock["mono"] += seconds
        clock["wall"] += seconds

    monkeypatch.setattr(refresh.time, "sleep", fake_sleep)
    cache.update_entry("devin", probe(), clock["wall"] - 120)
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)
    real_load = cache._load
    landed = cache._to_entry(probe(), clock["wall"] + late)

    def fake_load() -> dict:
        data = real_load()
        if clock["mono"] - 1000.0 >= late:
            data["devin"] = landed
        return data

    monkeypatch.setattr(cache, "_load", fake_load)
    result = REFRESH_SWE2()
    assert result is not None and not result.error


# --- backoff: refuse at once, never wait (AC3) ---


def test_backoff_skip_refuses_at_once_named(monkeypatch):
    seed_backoff(42.0)
    stub_worker(monkeypatch, refresh.SWE2_RC_BACKOFF)
    sleeps = []
    monkeypatch.setattr(refresh.time, "sleep", lambda s: sleeps.append(s))
    started = time.monotonic()
    result = REFRESH_SWE2()
    assert time.monotonic() - started < 5.0
    assert sleeps == []  # no cache poll ran — backoff never waits
    assert result is not None
    assert result.error_kind == SWE2_BACKOFF
    assert "backoff" in result.error and "42" in result.error


def test_backoff_skip_is_named_rc4_at_the_gate(monkeypatch, capsys):
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)
    # update_entry clears the pool's backoff record — seed after the write.
    seed_backoff(42.0)
    cli_probe(monkeypatch, stale)
    monkeypatch.setattr(refresh, "refresh_swe2_result", REFRESH_SWE2)
    stub_worker(monkeypatch, refresh.SWE2_RC_BACKOFF)
    assert cli.main(["gate", "-m", "builder-devin", "--no-cache"]) == 4
    err = capsys.readouterr().err
    assert "swe2_measurement_unavailable" in err
    assert "state=backoff" in err
    assert "42" in err


# --- a normal run is unchanged ---


def test_normal_run_still_admits_own_fresh_entry(monkeypatch):
    def wait(**kw):
        cache.update_entry("devin", probe(), time.time())
        return 0

    monkeypatch.setattr(
        refresh.subprocess,
        "Popen",
        lambda *a, **kw: SimpleNamespace(pid=424242, wait=wait),
    )
    result = REFRESH_SWE2()
    assert result is not None and not result.error and not result.stale
    assert result.buckets


def test_worker_failure_rc_still_returns_none(monkeypatch):
    stub_worker(monkeypatch, 1)
    assert REFRESH_SWE2() is None


# --- backoff observed during the wait (R2-2, adopted tester repros) ---


def test_backoff_on_disk_short_circuits_lock_held_wait(monkeypatch):
    """R2-2: backoff already on disk when a lock-held skip arrives wins at once."""
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)
    # update_entry clears the pool's backoff record — seed after the write.
    seed_backoff(300.0)
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)
    monkeypatch.setattr(refresh, "_in_flight_wait_s", lambda: 2.0)
    monkeypatch.setattr(refresh, "SWE2_IN_FLIGHT_POLL_S", 0.05)
    started = time.monotonic()
    result = REFRESH_SWE2()
    assert time.monotonic() - started < 1.5
    assert result is not None
    assert result.error_kind == SWE2_BACKOFF
    assert "backoff" in result.error


def test_backoff_recorded_by_in_flight_probe_refuses_quickly(monkeypatch):
    """R2-2: a 429 the in-flight probe records while we wait ends the wait."""
    monkeypatch.setattr(refresh, "_in_flight_wait_s", lambda: 3.0)
    monkeypatch.setattr(refresh, "SWE2_IN_FLIGHT_POLL_S", 0.05)
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)

    def record_429() -> None:
        time.sleep(0.2)
        cache.record_failure(
            "devin",
            ProviderResult(
                id="devin",
                error="429",
                error_kind="rate_limited",
                http_status=429,
                retry_after_s=120,
            ),
            time.time(),
        )

    thread = threading.Thread(target=record_429, daemon=True)
    thread.start()
    started = time.monotonic()
    result = REFRESH_SWE2()
    took = time.monotonic() - started
    thread.join(timeout=10)
    assert result is not None
    assert result.error_kind == SWE2_BACKOFF and took < 1.5
    assert cache.backoff_remaining("devin", time.time()) > 0


# --- landing invariants that pin the surviving mutants (R2-3) ---


def _write_raw(entry_fetched: float, *, last_error_at: float | None = None, error: str | None = None):
    """One locked cache write — a landing carries last_error_at atomically."""
    result = probe(error=error) if error else probe()
    entry = cache._to_entry(result, entry_fetched)
    if last_error_at is not None:
        entry["last_error_at"] = last_error_at
    with cache._exclusive_cache_lock():
        data = cache._load()
        data["devin"] = entry
        cache._save(data)


def _late_write(delay_s: float, **kw):
    def go() -> None:
        time.sleep(delay_s)
        _write_raw(**kw)

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread


def test_landing_past_ttl_never_admits(monkeypatch):
    """Kills M2f: a landing newer than the pre-wait entry but past TTL."""
    small_wait(monkeypatch)
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)
    thread = _late_write(0.1, entry_fetched=time.time() - 100)
    result = REFRESH_SWE2()
    thread.join(timeout=10)
    assert result.error_kind == SWE2_IN_FLIGHT_TIMEOUT


def test_no_prewait_entry_stale_landing_never_admits(monkeypatch):
    """Kills M2h: with no pre-wait entry a past-TTL landing still never admits."""
    small_wait(monkeypatch)
    assert cache._load().get("devin") is None
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)
    thread = _late_write(0.1, entry_fetched=time.time() - 100)
    result = REFRESH_SWE2()
    thread.join(timeout=10)
    assert result.error_kind == SWE2_IN_FLIGHT_TIMEOUT


def test_no_prewait_failure_landing_never_admits(monkeypatch):
    """A failing in-flight probe on an empty cache leaves fetched_at=0 + last_error_at."""
    small_wait(monkeypatch)
    assert cache._load().get("devin") is None
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)

    def fail() -> None:
        time.sleep(0.1)
        cache.record_failure(
            "devin", ProviderResult(id="devin", error="boom", error_kind="unknown"), time.time()
        )

    thread = threading.Thread(target=fail, daemon=True)
    thread.start()
    result = REFRESH_SWE2()
    thread.join(timeout=10)
    assert result.error_kind == SWE2_IN_FLIGHT_TIMEOUT


def test_landing_last_error_equal_fetched_never_admits(monkeypatch):
    """Kills M2c: last_error_at == fetched_at is a failure stamp, not a landing."""
    small_wait(monkeypatch)
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)
    stamp = time.time() + 0.05
    thread = _late_write(0.15, entry_fetched=stamp, last_error_at=stamp)
    result = REFRESH_SWE2()
    thread.join(timeout=10)
    assert result.error_kind == SWE2_IN_FLIGHT_TIMEOUT


def test_landing_with_error_payload_never_admits(monkeypatch):
    """Kills M2e: a landed entry whose payload is an error result."""
    small_wait(monkeypatch)
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)
    thread = _late_write(0.1, entry_fetched=time.time(), error="models list failed")
    result = REFRESH_SWE2()
    thread.join(timeout=10)
    assert result.error_kind == SWE2_IN_FLIGHT_TIMEOUT


def test_future_dated_landing_never_admits(monkeypatch):
    """Kills M2g: a fetched_at ahead of the poll clock never admits."""
    small_wait(monkeypatch)
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)
    thread = _late_write(0.1, entry_fetched=time.time() + 3600)
    result = REFRESH_SWE2()
    thread.join(timeout=10)
    assert result.error_kind == SWE2_IN_FLIGHT_TIMEOUT


def test_landing_during_worker_lifetime_is_admitted(monkeypatch):
    """Kills M2l: the pre-wait snapshot is taken before the worker spawn, so a
    landing that arrives while the worker is still alive still admits."""
    small_wait(monkeypatch)
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)

    def wait(**kw):
        cache.update_entry("devin", probe(), time.time())  # lands mid-wait
        return refresh.SWE2_RC_LOCK_HELD

    monkeypatch.setattr(
        refresh.subprocess,
        "Popen",
        lambda *a, **kw: SimpleNamespace(pid=424242, wait=wait),
    )
    result = REFRESH_SWE2()
    assert result is not None and result.error_kind is None and not result.error
    assert result.buckets


def test_poll_cadence_and_no_network(monkeypatch):
    """Kills M2m/M3a/M3b: fixed 0.25 s cadence and no network during the wait."""
    import socket
    import urllib.request

    monkeypatch.setattr(refresh, "_in_flight_wait_s", lambda: 2.0)  # real poll interval stays
    stale = stale_probe()
    cache.update_entry("devin", stale, stale.fetched_at)
    stub_worker(monkeypatch, refresh.SWE2_RC_LOCK_HELD)
    loads = []
    real_load = cache._load
    monkeypatch.setattr(cache, "_load", lambda: loads.append(time.monotonic()) or real_load())

    def no_net(*a, **kw):
        raise AssertionError("network call during wait")

    monkeypatch.setattr(socket.socket, "connect", no_net)
    monkeypatch.setattr(socket, "create_connection", no_net)
    monkeypatch.setattr(urllib.request, "urlopen", no_net)
    result = REFRESH_SWE2()
    # loads[0] is the pre-Popen snapshot read; the gaps are poll-to-poll.
    gaps = [round(b - a, 2) for a, b in zip(loads[1:], loads[2:], strict=False)]
    assert result.error_kind == SWE2_IN_FLIGHT_TIMEOUT
    assert gaps and all(0.2 < g < 0.4 for g in gaps)


# --- worker-code and dispatch seams (R2-3) ---


def test_skip_codes_are_pairwise_distinct_and_not_generic():
    """Kills M1a: the three skip codes must not collapse onto each other or rc 0/1."""
    codes = [refresh.SWE2_RC_LOCK_HELD, refresh.SWE2_RC_PROBE_IN_PROGRESS, refresh.SWE2_RC_BACKOFF]
    assert len(set(codes)) == 3
    assert not set(codes) & {0, 1, 2, 124}
    assert codes == [10, 11, 12]


@pytest.mark.parametrize("flag,expected", [(["--_swe2"], True), ([], False)])
def test_cli_dispatches_swe2_flag_to_run_worker(monkeypatch, flag, expected):
    """Kills M1j: --_swe2 must reach run_worker — it is what makes AC1 real."""
    seen = []
    monkeypatch.setattr(cli, "run_worker", lambda fetchers, pool, **kw: seen.append((pool, kw)) or 77)
    assert cli.main(["refresh", "devin", "--_worker", *flag]) == 77
    assert seen == [("devin", {"swe2": True} if expected else {})]
