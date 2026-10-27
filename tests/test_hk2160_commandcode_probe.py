"""hk 2160: the commandcode /usage three-window probe.

The GOAT usage screen provides monthly, 5-hour, and weekly percentages with
their own reset times. A PTY probe reads only cmd's /usage UI (no model call);
failures use hk 2114's negative cache and a valid manual credit observation
remains a fallback. Excluded or unsubscribed hosts are never probed, and the
hk 2141 observed-limit hold still stalls the pool until its named reset.

Every probe test uses a stub ``cmd`` binary on a monkeypatched path — no real
cmd run, no model call.

Each test docstring names the mutant it kills where the brief lists one.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest

from scopefuel import cache, manual, providers, recommend
from scopefuel.model import ProviderResult
from scopefuel.providers import commandcode
from scopefuel.recommend import gate_check

NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)
TODAY = NOW.date()

USAGE_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "cmd_usage_goat.txt"
LIMIT_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "cmd_usage_limit.placeholder.txt"
USAGE_TEXT = (
    "GOAT Plan · active / 0% used / 95 requests this month · 31 days to renew / "
    "5-hour 1% · resets in 3h 52m / Weekly 1% · resets in 6d 22h"
)
CREDIT_EXPIRES = (TODAY + dt.timedelta(days=30)).isoformat()


def _marker(profile: str, rung: str) -> str:
    return f"{profile}@{rung}"


def _record_credit(
    *,
    remaining: float = 0.75,
    total: float = 1.0,
    expires_on: str = CREDIT_EXPIRES,
    measured_at: dt.datetime = NOW,
    ttl_s: float | None = None,
    now: dt.datetime = NOW,
) -> dict:
    return manual.record_observation(
        pool="commandcode",
        window="credit",
        remaining_usd=remaining,
        total_usd=total,
        credit_expires_on=expires_on,
        measured_at=measured_at,
        reason="cmd /usage credit balance",
        ttl_s=ttl_s,
        now=now,
    )


def _failed_probe() -> ProviderResult:
    return ProviderResult(
        id="commandcode", error="cmd unavailable", error_kind="transport", pool_class="spend"
    )


# ---------------------------------------------------------------------------
# 1. the desk fixture — three windows with reset times
# ---------------------------------------------------------------------------


def test_desk_usage_fixture_parses_all_three_percentages_and_resets():
    assert USAGE_FIXTURE.read_text().strip() == USAGE_TEXT
    result = commandcode.parse(USAGE_FIXTURE.read_text(), now=NOW)
    assert result.status == "ok"
    buckets = {bucket.window: bucket for bucket in result.buckets}
    assert set(buckets) == {"30d", "5h", "7d"}
    assert buckets["30d"].used_pct == 0
    assert buckets["30d"].resets_at == (NOW + dt.timedelta(days=31)).isoformat().replace("+00:00", "Z")
    assert buckets["5h"].used_pct == 1
    assert buckets["5h"].resets_at == (NOW + dt.timedelta(hours=3, minutes=52)).isoformat().replace(
        "+00:00", "Z"
    )
    assert buckets["7d"].used_pct == 1
    assert buckets["7d"].resets_at == (NOW + dt.timedelta(days=6, hours=22)).isoformat().replace(
        "+00:00", "Z"
    )


def test_commandcode_has_automatic_three_window_coverage():
    """Mutant: drop a window from REQUIRED_WINDOWS -> coverage check goes RED."""

    assert "commandcode" in providers.BUILTIN
    assert manual.required_windows_for(commandcode.parse(USAGE_TEXT, now=NOW)) == frozenset(
        {"5h", "7d", "30d"}
    )


@pytest.mark.parametrize("window", ["30d", "5h", "7d"])
def test_any_window_at_100_is_exhausted_and_uses_that_windows_reset(window):
    """A window at 100% exhausts the pool until that window's own reset."""

    prior = {"30d": "0% used", "5h": "5-hour 1%", "7d": "Weekly 1%"}[window]
    current = {"30d": "100% used", "5h": "5-hour 100%", "7d": "Weekly 100%"}[window]
    result = commandcode.parse(USAGE_TEXT.replace(prior, current), now=NOW)
    bucket = next(bucket for bucket in result.buckets if bucket.window == window)
    assert bucket.used_pct == 100
    assert bucket.resets_at
    gate = gate_check(
        [result],
        "cmd-dsflash41",
        role="worker",
        effort="high",
        e6_arm=_marker("cmd-dsflash41", "high"),
        today=TODAY,
        now=NOW,
    )
    assert gate.ok is False


def test_credit_recommendation_requires_every_commandcode_window():
    healthy = commandcode.parse(USAGE_TEXT, now=NOW)
    assert recommend.credit_row_available([healthy], "commandcode")
    exhausted = commandcode.parse(USAGE_TEXT.replace("Weekly 1%", "Weekly 100%"), now=NOW)
    assert not recommend.credit_row_available([exhausted], "commandcode")


def test_limit_wording_maps_to_its_named_window_and_unmapped_wording_fails_closed():
    fixture = LIMIT_FIXTURE.read_text()
    assert "PLACEHOLDER" in fixture
    text = "\n".join(line for line in fixture.splitlines() if not line.startswith("#"))
    result = commandcode.parse(text, now=NOW)
    buckets = {bucket.window: bucket for bucket in result.buckets}
    assert buckets["7d"].used_pct == 100
    assert buckets["7d"].resets_at == (NOW + dt.timedelta(days=6, hours=22)).isoformat().replace(
        "+00:00", "Z"
    )
    bad = commandcode.parse(USAGE_TEXT.replace("Weekly 1%", "usage limit reached"), now=NOW)
    assert bad.error_kind == "parse_error"


# ---------------------------------------------------------------------------
# 2. manual credit fallback — only after the probe cannot be read
# ---------------------------------------------------------------------------


def test_manual_credit_fallback_is_used_only_after_probe_failure():
    _record_credit(remaining=0.75, total=1.0)
    resolution = manual.resolve_result(_failed_probe(), now=NOW, store=manual.load_store())
    assert resolution.applied
    assert resolution.result.manual["selection"] == "manual_fallback"
    assert resolution.result.buckets[0].used_pct == pytest.approx(25.0)
    assert manual.required_windows_for(resolution.result) == frozenset({"credit"})


def test_stale_100_percent_observation_holds_until_its_named_reset_only():
    """A 100% window blocks fallback; once its reset passes the hold expires."""

    _record_credit(remaining=0.75, total=1.0)
    limited_text = USAGE_TEXT.replace("Weekly 1%", "Weekly 100%")
    active_limit = commandcode.parse(limited_text, now=NOW)
    active_limit.stale = True
    active_limit.error = "cmd probe failed"
    active_limit.error_kind = "transport"
    blocked = manual.resolve_result(active_limit, now=NOW, store=manual.load_store())
    assert blocked.applied is False

    expired_limit = commandcode.parse(limited_text, now=NOW)
    expired_limit.stale = True
    expired_limit.error = "cmd probe failed"
    expired_limit.error_kind = "transport"
    for bucket in expired_limit.buckets:
        bucket.resets_at = (NOW - dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    reopened = manual.resolve_result(expired_limit, now=NOW, store=manual.load_store())
    assert reopened.applied is True


def test_manual_credit_fallback_rejects_missing_stale_expired_and_exhausted():
    cases = (
        (None, NOW),
        ({"measured_at": NOW - dt.timedelta(hours=2), "ttl_s": 900.0}, NOW),
        ({"remaining": 0.0}, NOW),
        (
            {"expires_on": (TODAY + dt.timedelta(days=1)).isoformat()},
            dt.datetime.combine(TODAY + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC)
            + dt.timedelta(seconds=1),
        ),
    )
    for kwargs, resolve_now in cases:
        manual.clear_pool("commandcode", now=NOW)
        if kwargs is not None:
            _record_credit(**kwargs)
        resolution = manual.resolve_result(_failed_probe(), now=resolve_now, store=manual.load_store())
        if kwargs == {"remaining": 0.0}:
            assert resolution.applied is True
            denied = gate_check(
                [resolution.result],
                "cmd-dsflash41",
                role="worker",
                effort="high",
                e6_arm=_marker("cmd-dsflash41", "high"),
                today=TODAY,
                now=resolve_now,
            )
            assert denied.ok is False
        else:
            assert resolution.applied is False, kwargs
            assert resolution.result.status == "error"


# ---------------------------------------------------------------------------
# 3. PTY probe through a stub cmd — no model call
# ---------------------------------------------------------------------------


def test_probe_uses_stub_cmd_and_reads_usage_without_a_model_prompt(monkeypatch, tmp_path):
    script = tmp_path / "cmd-stub"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "print('❯ ', end='', flush=True)\n"
        "line = sys.stdin.readline()\n"
        "assert line.strip() == '/usage', line\n"
        f"print({USAGE_TEXT!r}, flush=True)\n"
        "time.sleep(1)\n"
    )
    script.chmod(0o755)
    monkeypatch.setattr(commandcode, "BINARY", str(script))
    monkeypatch.setattr(commandcode, "PROBE_WORKDIR", tmp_path / "work")
    monkeypatch.setattr(commandcode, "STARTUP_DELAY_S", 0)
    monkeypatch.setattr(commandcode, "READY_SETTLE_S", 0.05)
    result = commandcode.fetch()
    assert result.status == "ok"
    assert len(result.buckets) == 3


def test_probe_failure_uses_hk2114_negative_cache(monkeypatch, tmp_path):
    calls = []

    def fail():
        calls.append(1)
        return ProviderResult(
            id="commandcode", error="stub failed", error_kind="transport", pool_class="spend"
        )

    fetchers = {"commandcode": fail}
    first = cache.collect(fetchers, ["commandcode"], use_cache=False, now=1000.0)
    second = cache.collect(fetchers, ["commandcode"], use_cache=False, now=1001.0)
    assert len(calls) == 1
    assert first[0].status == second[0].status == "error"


@pytest.mark.parametrize("state", ["exclude", "unsubscribed"])
def test_excluded_or_unsubscribed_commandcode_is_never_probed(monkeypatch, state):
    calls = []
    if state == "exclude":
        monkeypatch.setattr(cache, "get_policy", lambda *args, **kwargs: ("exclude", None))
    else:
        monkeypatch.setattr(cache, "get_subscribed", lambda _name: (False, "test unsubscribed"))
    results = cache.collect({"commandcode": lambda: calls.append(1)}, ["commandcode"], use_cache=False)
    assert calls == []
    assert results[0].error_kind in {"excluded", "unsubscribed"}
