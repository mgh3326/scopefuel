"""hk 1731: free measurements stay independent of paid Devin quota; fixtures only."""

from __future__ import annotations

import datetime as dt
import signal
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopefuel import cache, cli, free_window, manual, recommend, refresh
from scopefuel.model import PROBE_IN_PROGRESS, Bucket, ProviderResult, Scope
from scopefuel.policy import set_policy

NOW = dt.datetime(2026, 10, 20, 2, 35, tzinfo=dt.UTC)
RUNGS = ("swe-2-high", "swe-2-medium", "swe-2-max")
SIX = tuple(recommend.SWE2_PROFILE_RUNG)
REFRESH_SWE2 = refresh.refresh_swe2_result
PAID = (
    "devin-ds41",
    "devin-ds41-max",
    "devin-glm52",
    "devin-swe17",
    "devin-fusion-opus55",
    "devin-fusion-sonnet55",
    "devin-argon",
)


def probe(*, daily=100.0, rungs=RUNGS, **changes):
    buckets = [
        Bucket("daily", "1d", daily, scope=Scope("account"), horizon="now"),
        Bucket("weekly", "7d", 59.0, scope=Scope("account"), horizon="week"),
        *[Bucket(rung, "", 0.0, scope=Scope("model", rung), horizon="now") for rung in rungs],
    ]
    return ProviderResult(id="devin", pool_class="spend", buckets=buckets, **changes)


def gate(result, name="builder-devin"):
    return recommend.gate_check([result], name, today=NOW.date(), now=NOW)


@pytest.fixture(autouse=True)
def isolated_probes(monkeypatch):
    # Every subprocess path must be opted into with a fixture stub.
    monkeypatch.setattr(refresh, "refresh_swe2_result", lambda: pytest.fail("unexpected refresh"))
    monkeypatch.setattr(recommend, "_observe_exhaustion", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_shadow_gate", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_emit_stale_build_warning", lambda: None)
    monkeypatch.setattr(cli.bench, "read_scores", lambda: [])
    monkeypatch.setattr(cli.bench, "read_prices", lambda: {})
    monkeypatch.setattr(cli.bench, "runtime_grade_table", lambda: recommend.GRADE_TABLE)


def cli_probe(monkeypatch, result):
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: result})
    monkeypatch.setattr(cli, "collect", lambda *a, **kw: [result])


@pytest.mark.parametrize("name", SIX)
@pytest.mark.parametrize(
    "warning",
    [
        None,
        "quota probe failed",
        "account daily exhausted",
        "banner weekly 100%, usage 59% mismatch",
        "PTY execution failed",
    ],
)
def test_current_free_bucket_ignores_paid_axes(name, warning):
    result = gate(probe(warning=warning), name)
    assert result.ok and result.used_pct == 0
    assert result.gate_basis == f"model-scope:{recommend.SWE2_PROFILE_RUNG[name]}"
    assert result.account_daily_pct == 100
    assert result.account_weekly_pct == 59
    assert result.free_tag == "observed"


@pytest.mark.parametrize(
    "warning",
    [
        "models list timeout",
        "models_list unavailable",
        "model row parse failed",
        "model-scope degraded",
        "swe-2 observation unavailable",
        "Free tag unknown",
        "모델 목록 실패",
        "Free 태그 판독 실패",
    ],
)
def test_model_warning_is_named_unavailable(warning):
    result = gate(probe(warning=warning))
    assert result.unmeasurable and not result.ok
    assert "swe2_measurement_unavailable" in result.reason
    assert result.used_pct is None and result.gate_basis is None


@pytest.mark.parametrize(
    "changes",
    [
        {"stale": True, "age_s": 120, "error_kind": "transport", "account_fp_match": True},
        {"age_s": 120},
        {"fetched_at": NOW.timestamp() - 120},
        {"error": "models list failed"},
    ],
)
def test_noncurrent_never_falls_back_to_paid_axis(changes):
    result = gate(probe(**changes))
    assert not result.ok and result.unmeasurable
    assert result.reason.startswith("swe2_measurement_unavailable")
    assert result.used_pct is None


def test_note_is_not_a_model_warning():
    assert gate(probe(note="banner weekly 100 vs usage 59 mismatch")).ok


@pytest.mark.parametrize("name", SIX)
@pytest.mark.parametrize("shape", ["missing", "sibling", "duplicate", "invalid"])
def test_current_missing_ambiguous_or_invalid_own_bucket_keeps_account_axis(name, shape):
    own = recommend.SWE2_PROFILE_RUNG[name]
    result = probe(rungs=() if shape == "missing" else RUNGS)
    if shape == "sibling":
        result.buckets = [b for b in result.buckets if b.scope.name != own]
    elif shape == "duplicate":
        result.buckets.append(Bucket(own, "", 0, scope=Scope("model", own)))
    elif shape == "invalid":
        result.buckets = [replace(b, used_pct=None) if b.scope.name == own else b for b in result.buckets]
    decision = gate(result, name)
    assert not decision.ok and not decision.unmeasurable
    assert decision.used_pct == 100 and decision.gate_basis is None


@pytest.mark.parametrize(
    "changes",
    [
        {"stale": True},
        {"age_s": 120},
        {"fetched_at": NOW.timestamp() - 120},
    ],
)
@pytest.mark.parametrize("succeeds", [True, False])
def test_cli_stale_refresh_once_and_no_manual_laundering(monkeypatch, capsys, changes, succeeds):
    cli_probe(monkeypatch, probe(**changes))
    calls = []
    monkeypatch.setattr(
        refresh,
        "refresh_swe2_result",
        lambda: calls.append("refresh") or (probe() if succeeds else None),
    )
    monkeypatch.setattr(manual, "resolve_result", lambda *a, **kw: pytest.fail("manual fallback"))
    rc = cli.main(["gate", "-m", "builder-devin", "--no-cache"])
    output = capsys.readouterr()
    assert calls == ["refresh"]
    assert rc == (0 if succeeds else 4)
    assert ("basis=model-scope:swe-2-high" if succeeds else "swe2_measurement_unavailable") in (
        output.out + output.err
    )


def test_cli_current_warning_admits_without_refresh(monkeypatch, capsys):
    cli_probe(monkeypatch, probe(warning="account quota exhausted"))
    assert cli.main(["gate", "-m", "builder-devin", "--no-cache"]) == 0
    assert "basis=model-scope:swe-2-high" in capsys.readouterr().out


@pytest.mark.parametrize("name", PAID)
def test_paid_never_uses_model_scope_or_refresh(monkeypatch, capsys, name):
    result = probe()
    cli_probe(monkeypatch, result)
    decision = gate(result, name)
    assert not decision.ok and decision.gate_basis is None
    assert "model-scope" not in decision.reason
    assert cli.main(["gate", "-m", name, "--no-cache"]) == 3
    capsys.readouterr()


def test_live_hold_still_only_overrides_admission(monkeypatch, capsys):
    cli_probe(monkeypatch, probe())
    record = SimpleNamespace()
    monkeypatch.setattr(free_window, "active_record", lambda **kw: record)
    monkeypatch.setattr(free_window, "reason", lambda value: "free_rate_limited fixture")
    assert cli.main(["gate", "-m", "builder-devin", "--no-cache"]) == 3
    assert "free_rate_limited" in capsys.readouterr().err


def rows(result, **kwargs):
    return recommend.recommend_dict([result], "A", today=NOW.date(), now=NOW, **kwargs)["rows"]


def test_recommendation_free_rows_not_demoted_by_account_axes():
    low = rows(probe(daily=0))
    high = rows(probe(warning="account exhausted"))
    low_free = [r for r in low if r["profile"] in SIX]
    high_free = [r for r in high if r["profile"] in SIX]
    assert low_free and low_free == high_free
    assert all(r["kind"] == "candidate" and "basis=model-scope:" in r["windows"] for r in high_free)
    text = recommend.recommend([probe()], "A", today=NOW.date(), now=NOW)
    assert "basis=model-scope:swe-2-medium" in text


def test_recommendation_missing_bucket_and_stale_are_not_admitted():
    for result in (probe(rungs=()), probe(stale=True)):
        free = [r for r in rows(result) if r["profile"] in SIX]
        assert free and all(r["kind"] != "candidate" for r in free)
        if result.stale:
            assert all("swe2_measurement_unavailable" in r["reason"] for r in free)


def test_manual_account_observation_cannot_stand_in_for_model_measurement():
    result = probe(source=manual.SOURCE)
    assert gate(result).unmeasurable
    assert all(r["kind"] == "unmeasurable" for r in rows(result) if r["profile"] in SIX)


def test_recommend_uses_automatic_models_but_keeps_paid_manual_rows():
    automatic = probe(warning="account probe failed")
    selected = probe(daily=10, rungs=(), source=manual.SOURCE)
    baseline = rows(selected)
    actual = rows(selected, swe2_result=automatic)
    assert [r for r in baseline if r["profile"] not in SIX] == [r for r in actual if r["profile"] not in SIX]
    assert all(r["kind"] == "candidate" for r in actual if r["profile"] in SIX)


def test_unknown_status_is_named_unavailable():
    class NonOK(ProviderResult):
        @property
        def status(self):
            return "degraded"

    result = NonOK(id="devin", buckets=probe().buckets, pool_class="spend")
    assert gate(result).unmeasurable
    assert "status:degraded" in gate(result).reason


def test_probe_lock_skip_cannot_turn_stale_free_rows_into_account_admission():
    result = probe(
        daily=10,
        stale=True,
        age_s=80,
        error_kind=PROBE_IN_PROGRESS,
        account_fp_match=True,
    )
    result.buckets = [
        replace(b, resets_at=(NOW + dt.timedelta(days=7)).isoformat()) if b.scope.kind == "account" else b
        for b in result.buckets
    ]
    free = gate(result)
    paid = gate(result, "devin-ds41")
    assert free.unmeasurable and not free.ok
    assert free.reason.startswith("swe2_measurement_unavailable")
    assert paid.ok and paid.stale_accepted and paid.gate_basis is None


def test_policy_exclude_unchanged_without_refresh(monkeypatch, capsys):
    set_policy("devin", "exclude", until=dt.date(2099, 1, 1))
    cli_probe(monkeypatch, probe())
    assert cli.main(["gate", "-m", "builder-devin", "--no-cache"]) == 3
    assert "until 2099-01-01" in capsys.readouterr().err
    free = [r for r in rows(probe()) if r["profile"] in SIX]
    assert free and all(r["kind"] == "policy_excluded" for r in free)


@pytest.mark.parametrize("json_output", [True, False])
def test_recommend_cli_refreshes_once_for_all_swe2_candidates(monkeypatch, capsys, json_output):
    cli_probe(monkeypatch, probe(stale=True))
    calls = []
    monkeypatch.setattr(refresh, "refresh_swe2_result", lambda: calls.append("refresh") or probe())
    args = ["--recommend", "A", "--no-cache"]
    assert cli.main([*args, *(["--json"] if json_output else [])]) == 0
    assert calls == ["refresh"]
    assert "basis=model-scope:swe-2-medium" in capsys.readouterr().out


def test_refresh_helper_is_bounded_and_session_isolated(monkeypatch):
    calls = []
    process = SimpleNamespace(pid=12345, wait=lambda **kw: calls.append(("wait", kw)) or 0)
    monkeypatch.setattr(
        refresh.subprocess,
        "Popen",
        lambda cmd, **kw: calls.append((cmd, kw)) or process,
    )
    monkeypatch.setattr(refresh.time, "time", lambda: 1000)
    cache.update_entry("devin", probe(), 1000)
    result = REFRESH_SWE2(now=1001)
    assert result is not None and not result.stale and result.age_s == 1
    assert calls[0][0][-4:] == ["refresh", "devin", "--_worker", "--_swe2"]
    assert calls[0][1]["start_new_session"] is True
    assert calls[1] == ("wait", {"timeout": refresh._timeout_seconds() + 5})


@pytest.mark.parametrize(
    "fetched,failed,code",
    [
        (999, None, 0),
        (1000, 1001, 0),
        (1000, None, 1),
        (1000, None, 0),
    ],
)
def test_refresh_does_not_admit_old_failed_or_expired_cache(monkeypatch, fetched, failed, code):
    monkeypatch.setattr(refresh.time, "time", lambda: 1000)
    monkeypatch.setattr(
        refresh.subprocess,
        "Popen",
        lambda *a, **kw: SimpleNamespace(wait=lambda **kw: code),
    )
    cache.update_entry("devin", probe(), fetched)
    if failed:
        data = cache._load()
        data["devin"]["last_error_at"] = failed
        monkeypatch.setattr(cache, "_load", lambda: data)
    assert REFRESH_SWE2(now=1120 if fetched == 1000 and failed is None and code == 0 else 1002) is None


def test_refresh_timeout_cleans_only_tracked_session(monkeypatch):
    waits = []
    kills = []

    def wait(**kw):
        waits.append(kw)
        if len(waits) == 1:
            raise subprocess.TimeoutExpired("fixture", kw["timeout"])
        return -9

    monkeypatch.setattr(
        refresh.subprocess,
        "Popen",
        lambda *a, **kw: SimpleNamespace(pid=12345, wait=wait),
    )
    monkeypatch.setattr(refresh.os, "killpg", lambda pid, sig: kills.append((pid, sig)))
    assert REFRESH_SWE2() is None
    assert kills == [(12345, signal.SIGKILL)]
    assert waits == [{"timeout": refresh._timeout_seconds() + 5}, {"timeout": 5}]


@pytest.mark.parametrize("cleanup_error", [subprocess.TimeoutExpired, OSError])
def test_cli_unreapable_refresh_still_returns_named_rc4(monkeypatch, capsys, cleanup_error):
    waits = []
    kills = []

    def wait(**kw):
        waits.append(kw)
        if len(waits) == 1 or cleanup_error is subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired("fixture", kw["timeout"])
        raise OSError("fixture reap failed")

    cli_probe(monkeypatch, probe(stale=True))
    monkeypatch.setattr(refresh, "refresh_swe2_result", REFRESH_SWE2)
    monkeypatch.setattr(
        refresh.subprocess,
        "Popen",
        lambda *a, **kw: SimpleNamespace(pid=12345, wait=wait),
    )
    monkeypatch.setattr(refresh.os, "killpg", lambda pid, sig: kills.append((pid, sig)))
    assert cli.main(["gate", "-m", "builder-devin", "--no-cache"]) == 4
    assert "swe2_measurement_unavailable" in capsys.readouterr().err
    assert kills == [(12345, signal.SIGKILL)]
    assert waits == [{"timeout": refresh._timeout_seconds() + 5}, {"timeout": 5}]


@pytest.mark.parametrize(
    "swe2,warning,expected",
    [
        (True, "account quota failed", 0),
        (False, "account quota failed", 1),
        (True, "models list failed", 1),
    ],
)
def test_worker_warning_exception_is_only_for_usable_swe2_models(monkeypatch, swe2, warning, expected):
    monkeypatch.setattr(refresh.signal, "signal", lambda *a: None)
    monkeypatch.setattr(refresh.signal, "setitimer", lambda *a: None)
    monkeypatch.setattr(refresh, "_v2_capture", lambda *a: {})
    monkeypatch.setattr(refresh.quota_share, "publish_result", lambda *a, **kw: None)
    assert refresh.run_worker({"devin": lambda: probe(warning=warning)}, "devin", swe2=swe2) == expected
    data = cache._load().get("devin")
    if expected == 0:
        assert data["result"]["warning"] == warning
        assert recommend._swe2_probe_is_current(
            replace(cache._from_entry(data, "devin", data["fetched_at"]), stale=False), NOW
        )
