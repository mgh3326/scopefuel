"""hk 2141: the commandcode /usage pool and its cmd-dsflash41 E6 rungs.

The GOAT usage screen provides monthly, 5-hour, and weekly percentages with
their own reset times. A PTY probe reads only cmd's /usage UI; failures use
hk 2114's negative cache and a valid manual credit observation remains a
fallback. The cmd rungs are unmeasured E6 rows, marker-gated and worker/tester
only, with no grade inherited from devin-ds41.

Each test docstring names the mutant it kills where the brief lists one.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from scopefuel import bench, cache, cli, launch, manual, providers, recommend
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.providers import commandcode
from scopefuel.recommend import (
    E6_ARM_ANNOTATION,
    E6_ARM_KEYS,
    GRADE_TABLE,
    e6_arm_rung_for,
    gate_check,
    profile_pool,
    provider_family,
)

NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)
TODAY = NOW.date()

CMD_PROFILES: dict[str, str] = {
    "cmd-dsflash41": "deepseek/deepseek-v4.1-flash",
}
# A real ``cmd -m deepseek/deepseek-v4.1-flash --effort xhigh`` run on
# 2026-10-27 answered ``Supported: off, low, high, max`` — the catalog
# vocabulary intersection, off not exposed.
CMD_RUNGS = ("low", "high", "max")
CMD_DEFAULT_RUNG = {"cmd-dsflash41": "high"}
CMD_ROWS = [(profile, rung) for profile in sorted(CMD_PROFILES) for rung in CMD_RUNGS]
CREDIT_EXPIRES = (TODAY + dt.timedelta(days=30)).isoformat()
USAGE_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "cmd_usage_goat.txt"
LIMIT_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "cmd_usage_limit.placeholder.txt"
USAGE_TEXT = (
    "GOAT Plan · active / 0% used / 95 requests this month · 31 days to renew / "
    "5-hour 1% · resets in 3h 52m / Weekly 1% · resets in 6d 22h"
)


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


def _credit_result(*, now: dt.datetime = NOW, **kwargs) -> ProviderResult:
    # Gate/catalog cases use a valid automatic observation; manual fallback
    # behavior is covered separately below.
    return commandcode.parse(USAGE_TEXT, now=now)


def _claude_result(used: float = 10.0) -> ProviderResult:
    return ProviderResult(
        id="claude",
        pool_class="preserve",
        buckets=[
            Bucket(
                label="7d",
                window="7d",
                used_pct=used,
                scope=Scope("account"),
                horizon="week",
            )
        ],
    )


# ---------------------------------------------------------------------------
# 1. routing and family — the cmd-pool mutant
# ---------------------------------------------------------------------------


def test_pool_is_commandcode_not_devin():
    """Mutant: route cmd-dsflash41 to the devin pool (devin-ds41's pool) -> RED."""

    assert profile_pool("cmd-dsflash41") == ("commandcode", None)
    assert provider_family("cmd-dsflash41") == "commandcode"
    # Control: the same model on the devin harness stays on the devin pool.
    assert profile_pool("devin-ds41") == ("devin", None)
    assert provider_family("devin-ds41") == "devin"


def test_unregistered_cmd_spelling_does_not_inherit_the_route():
    """The explicit-map rule: a cmd- prefix is not a route."""

    assert profile_pool("cmd-dsflash42") == ("", None)
    assert provider_family("cmd-dsflash42") == ""
    assert profile_pool("cmd-") == ("", None)


def test_cmd_rows_have_their_own_map_and_stay_out_of_the_pi_map():
    assert set(recommend._CMD_PROFILE_POOLS) == set(CMD_PROFILES)
    assert not set(CMD_PROFILES) & set(recommend._PI_PROFILE_POOLS)


def test_commandcode_has_an_automatic_probe_registration():
    assert "commandcode" in providers.BUILTIN
    assert "commandcode" in providers.registry()
    assert providers.BUILTIN["commandcode"].pool_class == "spend"
    synthetic = manual.manual_only_result("commandcode")
    assert synthetic.id == "commandcode"
    assert synthetic.error_kind == "manual_only"
    assert synthetic.pool_class == "spend"


# ---------------------------------------------------------------------------
# 2. catalog rows — the devin-ds41-grade mutant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("profile", "rung"), CMD_ROWS)
def test_catalog_row_is_the_unmeasured_c_carrier(profile, rung):
    """Mutant: copy the devin-ds41 grade into the cmd rows -> this goes RED."""

    rows = [entry for entry in bench.catalog_snapshot() if entry.key == (profile, rung)]
    assert len(rows) == 1
    (row,) = rows
    assert row.grade == "C"
    assert row.score is None
    assert row.pool == "commandcode"
    assert row.model_id == CMD_PROFILES[profile]
    assert row.gate == "default"
    assert row.benchmark_source is None
    assert row.benchmark_annotation.startswith(E6_ARM_ANNOTATION)
    # The placement exclusion is recorded in the catalog notes.
    assert "worker/tester" in row.benchmark_annotation
    assert "cmd" in row.benchmark_annotation


def test_no_grade_inheritance_from_the_devin_ds41_rows():
    """Mutant: copy the devin-ds41 grade into the cmd rows -> this goes RED."""

    # Control: the devin-ds41 rows really do sit above C, so "C" is not a default.
    devin_grades = {
        grade
        for grade, profiles in GRADE_TABLE.items()
        for placed in profiles
        if placed.name in {"devin-ds41", "devin-ds41-max"}
    }
    assert devin_grades - {"C"}
    # The cmd profile is not a placement at any grade.
    assert all(placed.name not in CMD_PROFILES for profiles in GRADE_TABLE.values() for placed in profiles)
    # And every catalog row for it is the C carrier, whatever devin-ds41 says.
    rows = [entry for entry in bench.catalog_snapshot() if entry.profile in CMD_PROFILES]
    assert len(rows) == len(CMD_ROWS)
    assert {entry.grade for entry in rows} == {"C"}
    assert {entry.score for entry in rows} == {None}


@pytest.mark.parametrize(("profile", "rung"), CMD_ROWS)
def test_cmd_rows_are_in_the_e6_arm_table(profile, rung):
    assert (profile, rung) in E6_ARM_KEYS
    row = e6_arm_rung_for(profile, rung)
    assert row is not None
    assert row.benchmark is None
    assert row.launcher_effort == rung


def test_unsupported_efforts_are_not_rungs():
    """The model answered ``Supported: off, low, high, max`` — medium/xhigh are not rows."""

    for rung in ("medium", "xhigh", "ultra"):
        for profile in CMD_PROFILES:
            assert (profile, rung) not in E6_ARM_KEYS
            assert e6_arm_rung_for(profile, rung) is None


@pytest.mark.parametrize(("profile", "rung"), CMD_ROWS)
def test_launch_spelling_takes_the_effort_flag(profile, rung):
    """cmd's effort is the ``--effort`` flag: wrk maps it onto cmd's argv."""

    assert launch.launcher_accepts_effort_flag(profile)
    assert launch.launch_spelling(profile, rung) == f"{profile} --effort {rung}"
    assert launch.DEFAULT_LAUNCH_EFFORTS[profile] == CMD_DEFAULT_RUNG[profile]
    assert launch.LAUNCH_MODEL_IDS[profile] == CMD_PROFILES[profile]


# ---------------------------------------------------------------------------
# 3. /usage windows, passive limit mapping, and manual fallback
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


@pytest.mark.parametrize("window", ["30d", "5h", "7d"])
def test_any_window_at_100_is_exhausted_and_uses_that_windows_reset(window):
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


def test_manual_credit_fallback_is_used_only_after_probe_failure():
    _record_credit(remaining=0.75, total=1.0)
    failed = ProviderResult(
        id="commandcode", error="cmd unavailable", error_kind="transport", pool_class="spend"
    )
    resolution = manual.resolve_result(failed, now=NOW, store=manual.load_store())
    assert resolution.applied
    assert resolution.result.manual["selection"] == "manual_fallback"
    assert resolution.result.buckets[0].used_pct == pytest.approx(25.0)
    assert manual.required_windows_for(resolution.result) == frozenset({"credit"})


def test_stale_100_percent_observation_holds_until_its_named_reset_only():
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
    failed = ProviderResult(
        id="commandcode", error="cmd unavailable", error_kind="transport", pool_class="spend"
    )
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
        resolution = manual.resolve_result(failed, now=resolve_now, store=manual.load_store())
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


def test_commandcode_has_automatic_three_window_coverage():
    assert "commandcode" in providers.BUILTIN
    assert "commandcode" in providers.registry()
    assert providers.BUILTIN["commandcode"].pool_class == "spend"
    assert manual.required_windows_for(commandcode.parse(USAGE_TEXT, now=NOW)) == frozenset(
        {"5h", "7d", "30d"}
    )


def test_missing_observation_refuses_admission():
    """Mutant: read a missing observation as available -> this goes RED."""

    resolution = manual.resolve_result(
        manual.manual_only_result("commandcode"),
        now=NOW,
        store=manual._empty_store(),
    )
    assert resolution.applied is False
    result = gate_check(
        [resolution.result],
        "cmd-dsflash41",
        role="worker",
        effort="high",
        e6_arm=_marker("cmd-dsflash41", "high"),
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert result.unmeasurable is True


def test_stale_manual_value_refuses_admission():
    """Mutant: read a stale observation as available -> this goes RED."""

    _record_credit(measured_at=NOW - dt.timedelta(hours=2), ttl_s=900.0)
    resolution = manual.resolve_result(
        manual.manual_only_result("commandcode"), now=NOW, store=manual.load_store()
    )
    assert resolution.applied is False
    result = gate_check(
        [resolution.result],
        "cmd-dsflash41",
        role="worker",
        effort="high",
        e6_arm=_marker("cmd-dsflash41", "high"),
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert result.unmeasurable is True


# ---------------------------------------------------------------------------
# 4. the marker and seat gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("profile", "rung"), CMD_ROWS)
def test_gate_refuses_a_rung_without_the_marker_as_unmeasured(profile, rung):
    result = gate_check(
        [_credit_result()],
        profile,
        role="worker",
        effort=rung,
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert result.unmeasurable is False
    assert "e6_arm_required" in result.reason
    assert "SCOPEFUEL_E6_ARM" in result.reason


@pytest.mark.parametrize(("profile", "with_marker"), [(p, m) for p in CMD_PROFILES for m in (True, False)])
def test_gate_refuses_the_bare_spelling_even_with_a_marker(profile, with_marker):
    """A bare cmd spelling names no rung — marker or not, --effort is required."""

    result = gate_check(
        [_credit_result()],
        profile,
        role="worker",
        e6_arm=_marker(profile, "high") if with_marker else None,
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert result.reason == recommend.credit_effort_required_reason(profile)


@pytest.mark.parametrize(
    ("profile", "rung", "role"),
    [(p, r, s) for p in CMD_PROFILES for r in CMD_RUNGS for s in ("worker", "tester")],
)
def test_gate_admits_with_the_marker_and_a_worker_or_tester_seat(profile, rung, role):
    result = gate_check(
        [_credit_result()],
        profile,
        role=role,
        effort=rung,
        e6_arm=_marker(profile, rung),
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True, result.reason
    assert result.provider_id == "commandcode"


@pytest.mark.parametrize(("profile", "rung"), CMD_ROWS)
def test_gate_refuses_the_builder_role_for_the_marker(profile, rung):
    """Mutant: drop the worker/tester seat rule -> this goes RED."""

    result = gate_check(
        [_credit_result()],
        profile,
        role="builder",
        effort=rung,
        e6_arm=_marker(profile, rung),
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert result.role_denied is True
    assert "role_restricted" in result.reason


@pytest.mark.parametrize("role", [None, "captain", "reviewer", "orch"])
@pytest.mark.parametrize(("profile", "rung"), CMD_ROWS)
def test_gate_refuses_every_seat_other_than_worker_and_tester(role, profile, rung):
    """A missing --role is refused the same way a wrong one is."""

    result = gate_check(
        [_credit_result()],
        profile,
        role=role,
        effort=rung,
        e6_arm=_marker(profile, rung),
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert result.role_denied is True


@pytest.mark.parametrize("profile", sorted(CMD_PROFILES))
def test_gate_reason_names_commandcode_not_devin(profile):
    """The gate line and the allow line both say pool=commandcode."""

    result = gate_check(
        [_credit_result()],
        profile,
        role="worker",
        effort="high",
        e6_arm=_marker(profile, "high"),
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True
    assert result.provider_id == "commandcode"


# ---------------------------------------------------------------------------
# 5. probe failure/cache behavior and --json listing
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


# ---------------------------------------------------------------------------
# 6. --json lists commandcode fail-closed (the hk 2136 mechanism)
# ---------------------------------------------------------------------------


def _claude_healthy() -> ProviderResult:
    return _claude_result()


def _stub_registry(monkeypatch, fetcher=_claude_healthy) -> None:
    monkeypatch.setattr(cli, "registry", lambda: {"claude": fetcher})


def _json_payload(capsys, argv: list[str]) -> tuple[int, dict]:
    rc = cli.main(["--json", "--no-cache", *argv])
    return rc, json.loads(capsys.readouterr().out)


def _entry(payload: dict, pool: str) -> dict:
    (entry,) = [item for item in payload["providers"] if item["id"] == pool]
    return entry


def _no_usage_shown(entry: dict) -> None:
    """Fail-closed: the entry carries no used_pct, so nothing reads as availability."""

    assert all(bucket["used_pct"] is None for bucket in entry["buckets"])
    assert entry["verdict"]["now_pct"] is None
    assert entry["verdict"]["week_pct"] is None


def test_json_lists_commandcode_with_no_observation_fail_closed(capsys, monkeypatch):
    """Mutant: drop commandcode from the manual-only pools -> this goes RED."""

    _stub_registry(monkeypatch)
    rc, payload = _json_payload(capsys, [])

    assert rc == 0
    assert "commandcode" in [item["id"] for item in payload["providers"]]
    entry = _entry(payload, "commandcode")
    assert entry["status"] == "error"
    assert entry["error_kind"] == "manual_only"
    assert entry["pool_class"] == "spend"
    _no_usage_shown(entry)


def test_json_commandcode_shows_a_valid_credit_observation(capsys, monkeypatch):
    _record_credit(remaining=0.75, total=1.0)
    _stub_registry(monkeypatch)
    rc, payload = _json_payload(capsys, [])

    assert rc == 0
    entry = _entry(payload, "commandcode")
    assert entry["error"] is None
    assert entry["status"] == "ok"
    (bucket,) = entry["buckets"]
    assert bucket["used_pct"] == pytest.approx(25.0)


def test_json_commandcode_stale_observation_fails_closed(capsys, monkeypatch):
    _record_credit(measured_at=NOW - dt.timedelta(hours=2), ttl_s=900.0)
    _stub_registry(monkeypatch)
    rc, payload = _json_payload(capsys, [])

    assert rc == 0
    entry = _entry(payload, "commandcode")
    assert entry["status"] == "error"
    _no_usage_shown(entry)


def _gate_printable_pools() -> set[str]:
    """Every ``pool=`` a placed profile resolves to (grade table plus the E6 arm rungs)."""

    placed = {profile.name for rows in GRADE_TABLE.values() for profile in rows}
    placed |= {profile.name for profile in recommend.E6_ARM_RUNGS}
    return {profile_pool(name)[0] for name in placed} - {""}


def test_json_provider_ids_equal_the_pools_gate_can_print(capsys, monkeypatch):
    """The hk 2136 drift test: json provider ids == gate pool= values (minus the pinned exception).

    Mutant: drop the manual-only append from the json path -> commandcode (and
    anthropic-api) leave the set -> RED.
    """

    # Pools that gate can name in ``pool=`` but that --json does not list.
    # oc-omni bills ``omniroute``, which has no registered fetcher and no
    # manual route, so gate can never answer it ok and --json has no entry.
    unlisted = frozenset({"omniroute"})

    real_names = set(providers.registry())
    stubs = {name: (lambda name=name: ProviderResult(id=name)) for name in real_names}
    monkeypatch.setattr(cli, "registry", lambda: stubs)
    rc, payload = _json_payload(capsys, [])

    assert rc == 0
    json_ids = {item["id"] for item in payload["providers"]}
    gate_pools = _gate_printable_pools()
    assert "commandcode" in json_ids
    assert "anthropic-api" in json_ids
    assert json_ids - gate_pools == set()
    assert gate_pools - json_ids == unlisted
    assert "omniroute" not in real_names


# ---------------------------------------------------------------------------
# 7. gate CLI end to end
# ---------------------------------------------------------------------------


def test_gate_cli_refuses_when_the_manual_value_is_missing(monkeypatch, capsys):
    monkeypatch.setenv("SCOPEFUEL_E6_ARM", "cmd-dsflash41@high")
    monkeypatch.setattr(cli, "collect", lambda *a, **k: [])
    rc = cli.main(["gate", "-m", "cmd-dsflash41", "--role", "worker", "--effort", "high"])
    assert rc == 4


def test_gate_cli_refuses_without_the_marker(monkeypatch, capsys):
    monkeypatch.delenv("SCOPEFUEL_E6_ARM", raising=False)
    monkeypatch.setattr(cli, "collect", lambda *a, **k: [])
    _record_credit()
    rc = cli.main(["gate", "-m", "cmd-dsflash41", "--role", "worker", "--effort", "high"])
    captured = capsys.readouterr()
    assert rc == 3
    assert "SCOPEFUEL_E6_ARM" in captured.out + captured.err


def test_gate_cli_allow_line_shows_pool_commandcode(monkeypatch, capsys, tmp_path):
    """Requirement 4: the gate prints pool=commandcode for cmd-dsflash41."""

    _record_credit()
    monkeypatch.setenv("SCOPEFUEL_E6_ARM", "cmd-dsflash41@high")
    monkeypatch.setattr(cli, "collect", lambda *a, **k: [])
    record_path = tmp_path / "gate.json"
    rc = cli.main(
        [
            "gate",
            "-m",
            "cmd-dsflash41",
            "--role",
            "worker",
            "--effort",
            "high",
            "--gate-output",
            str(record_path),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "pool=commandcode" in captured.out
