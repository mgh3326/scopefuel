"""hk 2141: the commandcode credit pool and its cmd-dsflash41 rungs.

The operator's Command Code credit (cmd 1.88.0, plan GOAT per r1027cmdcode /
r1027cmdgoat — no amount or period is hardcoded anywhere) is spent through
cmd. It is a separate pool with no usage API and no probe: its balance is a
manual observation the operator or desk enters (the ``/usage`` credit balance
plus an expiry), and nothing is ever fetched or scraped from the cmd
account.

hk 2185 (operator desk op-1028-grades FINAL) supersedes the original E6
contract: the cmd rungs are ordinary per-effort PLACEMENTS now, each
inheriting the devin DeepSeek V4.1 Flash rung's grade per effort (high =
devin-ds41, max = devin-ds41-max, each lower effort one step down). They keep
the worker/tester-only seat rule and the closed rung vocabulary, and the E6
marker is inert for them.

Each test docstring names the mutant it kills where the brief lists one.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from scopefuel import bench, cli, commandcode_limit, launch, manual, providers, recommend
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    ALTLANE_CANON_ROWS,
    E6_ARM_KEYS,
    GRADE_TABLE,
    altlane_annotation,
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
# hk 2185: the canon places cmd-dsflash41@high only. @max as a copy of
# devin-ds41-max (C) would sit below @high (A+) and break the canon's effort
# monotonicity, and @low has no source rung — both are deliberately unplaced.
CMD_CANON = {(profile, rung): (grade, gate) for profile, rung, grade, gate, _r in ALTLANE_CANON_ROWS}
CMD_ROWS = [
    (profile, rung) for profile in sorted(CMD_PROFILES) for rung in CMD_RUNGS if (profile, rung) in CMD_CANON
]
CMD_UNPLACED = [("cmd-dsflash41", rung) for rung in CMD_RUNGS if ("cmd-dsflash41", rung) not in CMD_CANON]
CREDIT_EXPIRES = (TODAY + dt.timedelta(days=30)).isoformat()


def _snapshot_view() -> launch.CatalogView:
    return launch.CatalogView(
        entries=bench.catalog_snapshot(), source=bench.CATALOG_SOURCE_SNAPSHOT, age_s=None
    )


def _placed_grade(profile: str, rung: str) -> str | None:
    for grade, rows in GRADE_TABLE.items():
        for row in rows:
            if row.name == profile and row.launcher_effort == rung:
                return grade
    return None


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
    _record_credit(**kwargs)
    resolution = manual.resolve_result(
        manual.manual_only_result("commandcode"), now=now, store=manual.load_store()
    )
    assert resolution.applied, resolution.failure_reason
    return resolution.result


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
    # hk 2185 (op-1028-grades FINAL): cmd-dsflash41 is DeepSeek FAMILY, the same
    # family value devin-ds41 carries — a cmd tester for a devin builder is
    # refused as same-family.
    assert provider_family("cmd-dsflash41") == "devin"
    # Control: the same model on the devin harness stays on the devin pool.
    assert profile_pool("devin-ds41") == ("devin", None)
    assert provider_family("devin-ds41") == "devin"
    assert provider_family("cmd-dsflash41") == provider_family("devin-ds41")


def test_unregistered_cmd_spelling_does_not_inherit_the_route():
    """The explicit-map rule: a cmd- prefix is not a route."""

    assert profile_pool("cmd-dsflash42") == ("", None)
    assert provider_family("cmd-dsflash42") == ""
    assert profile_pool("cmd-") == ("", None)


def test_cmd_rows_have_their_own_map_and_stay_out_of_the_pi_map():
    assert set(recommend._CMD_PROFILE_POOLS) == set(CMD_PROFILES)
    assert not set(CMD_PROFILES) & set(recommend._PI_PROFILE_POOLS)


def test_commandcode_has_an_automatic_probe_registration():
    # hk 2160: cmd's /usage PTY probe is the automatic path; the manual credit
    # stays only as the fallback where the screen cannot be read.
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
def test_catalog_row_is_the_inherited_placement(profile, rung):
    """Mutant: hand-copy a grade instead of inheriting it -> this goes RED."""

    rows = [entry for entry in bench.catalog_snapshot() if entry.key == (profile, rung)]
    assert len(rows) == 1
    (row,) = rows
    assert row.grade == _placed_grade(profile, rung) == CMD_CANON[(profile, rung)][0]
    assert row.score is None
    assert row.pool == "commandcode"
    assert row.model_id == CMD_PROFILES[profile]
    assert row.gate == CMD_CANON[(profile, rung)][1]
    assert row.benchmark_source is None
    # hk 2185: the canon annotation, verbatim.
    assert row.benchmark_annotation == altlane_annotation(profile, rung)
    assert row.benchmark_annotation.startswith("inherited from devin ds41")


def test_cmd_high_inherits_devin_ds41_and_the_other_rungs_are_unplaced():
    """hk 2185: high = devin-ds41 (A+); @low/@max stay unplaced (canon decision)."""

    assert _placed_grade("cmd-dsflash41", "high") == recommend.source_placement_grade("devin-ds41", "")
    assert _placed_grade("cmd-dsflash41", "high") == "A+"
    assert _placed_grade("cmd-dsflash41", "low") is None
    assert _placed_grade("cmd-dsflash41", "max") is None
    # No catalog row for the unplaced rungs either.
    keys = {(entry.profile, entry.effort) for entry in bench.catalog_snapshot()}
    assert ("cmd-dsflash41", "high") in keys
    assert ("cmd-dsflash41", "low") not in keys
    assert ("cmd-dsflash41", "max") not in keys


def test_cmd_ladder_is_monotone_so_a_seed_can_never_400():
    """A canon push refuses a non-monotonic ladder — the bundle must never carry one."""

    order = recommend.REP_GRADES_ORDER
    ladder = [
        (rung, _placed_grade("cmd-dsflash41", rung))
        for rung in CMD_RUNGS
        if _placed_grade("cmd-dsflash41", rung) is not None
    ]
    ranks = [order.index(grade) for _rung, grade in ladder]
    assert ranks == sorted(ranks), ladder


@pytest.mark.parametrize(("profile", "rung"), CMD_ROWS)
def test_cmd_rows_are_ordinary_placements_not_e6_arms(profile, rung):
    """hk 2185: a placed rung is an ordinary row — no marker, no carrier fold."""

    assert (profile, rung) not in E6_ARM_KEYS
    assert e6_arm_rung_for(profile, rung) is None
    assert recommend.is_e6_unmeasured_key(profile, rung) is False
    assert recommend.e6_only_profile(profile) is False


def test_unsupported_efforts_are_not_rungs():
    """The model answered ``Supported: off, low, high, max`` — medium/xhigh are not rungs."""

    for rung in ("medium", "xhigh", "ultra"):
        for profile in CMD_PROFILES:
            assert (profile, rung) not in E6_ARM_KEYS
            assert e6_arm_rung_for(profile, rung) is None
    # low/max are supported efforts but deliberately unplaced (hk 2185).
    assert recommend.worker_tester_rungs("cmd-dsflash41") == ("high",)


@pytest.mark.parametrize(("profile", "rung"), CMD_ROWS)
def test_launch_spelling_takes_the_effort_flag(profile, rung):
    """cmd's effort is the ``--effort`` flag: wrk maps it onto cmd's argv."""

    assert launch.launcher_accepts_effort_flag(profile)
    assert launch.launch_spelling(profile, rung) == f"{profile} --effort {rung}"
    assert launch.DEFAULT_LAUNCH_EFFORTS[profile] == CMD_DEFAULT_RUNG[profile]
    assert launch.LAUNCH_MODEL_IDS[profile] == CMD_PROFILES[profile]


@pytest.mark.parametrize(("profile", "rung"), CMD_UNPLACED)
def test_launch_and_gate_refuse_an_unplaced_rung(profile, rung):
    """@low/@max name no placement anywhere — the gate says not_placed."""

    result = gate_check([_credit_result()], profile, role="worker", effort=rung, today=TODAY, now=NOW)
    assert result.ok is False
    assert "not_placed" in result.reason
    with pytest.raises(launch.LaunchError, match="not placed at effort"):
        launch.resolve_launch(profile, effort=rung, role="worker", view=_snapshot_view())


# ---------------------------------------------------------------------------
# 3. the manual credit shape — fail closed without a valid observation
# ---------------------------------------------------------------------------


def test_credit_observation_derives_used_pct_and_carries_the_expiry():
    entry = _record_credit(remaining=0.75, total=1.0)
    assert entry["window"] == "credit"
    assert entry["used_pct"] == pytest.approx(25.0)
    assert entry["credit"] == {
        "remaining_usd": 0.75,
        "total_usd": 1.0,
        "expires_on": CREDIT_EXPIRES,
    }
    assert entry["resets_at"] is None


def test_a_noncredit_window_on_commandcode_is_refused():
    """The credit window is the manual-only pool's window and nothing else's."""

    with pytest.raises(manual.ManualError, match="credit"):
        manual.record_observation(
            pool="commandcode",
            window="7d",
            used_pct=25.0,
            measured_at=NOW,
            reason="wrong window",
            now=NOW,
        )


def test_missing_manual_value_refuses_admission():
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


def test_zero_remaining_balance_is_not_admitted():
    """Mutant: read a 100%-used observation as available -> this goes RED."""

    result = gate_check(
        [_credit_result(remaining=0.0)],
        "cmd-dsflash41",
        role="worker",
        effort="high",
        e6_arm=_marker("cmd-dsflash41", "high"),
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False


def test_credit_expired_at_midnight_utc_fails_closed():
    """Mutant: read an expired observation as available -> this goes RED."""

    expires_on = TODAY + dt.timedelta(days=1)
    _record_credit(expires_on=expires_on.isoformat())
    expiry_start = dt.datetime.combine(expires_on, dt.time.min, tzinfo=dt.UTC)
    resolution = manual.resolve_result(
        manual.manual_only_result("commandcode"),
        now=expiry_start + dt.timedelta(seconds=1),
        store=manual.load_store(),
    )
    assert resolution.applied is False
    result = gate_check(
        [resolution.result],
        "cmd-dsflash41",
        role="worker",
        effort="high",
        e6_arm=_marker("cmd-dsflash41", "high"),
        today=expires_on,
        now=expiry_start + dt.timedelta(seconds=1),
    )
    assert result.ok is False
    assert result.unmeasurable is True


# ---------------------------------------------------------------------------
# 4. the marker and seat gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("profile", "rung"), CMD_ROWS)
def test_gate_admits_a_placed_rung_without_the_marker(profile, rung):
    """hk 2185: a placed rung needs no E6 marker — the marker is inert for cmd."""

    result = gate_check(
        [_credit_result()],
        profile,
        role="worker",
        effort=rung,
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True, result.reason
    assert result.e6_arm is None
    assert result.grade == CMD_CANON[(profile, rung)][0]


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
    [(p, r, s) for p in CMD_PROFILES for r in ("high",) for s in ("worker", "tester")],
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
# 5. the observed-limit hook — pool exhausted until the named reset
# ---------------------------------------------------------------------------


def _observe_limit(*, reset: dt.datetime | None = None, observed: dt.datetime = NOW) -> None:
    reset = reset or NOW + dt.timedelta(hours=6)
    saved = commandcode_limit.record_observation(
        reset=reset.isoformat(),
        observed=observed.isoformat(),
        rung="cmd-dsflash41",
        source="job:2141-test",
    )
    assert saved is True


def test_observe_commandcode_limit_records_and_blocks_the_pool():
    """A live observed cmd limit maps the pool to exhausted until its reset."""

    _observe_limit()
    # Even a fresh, positive credit observation cannot override the refusal
    # the pane already recorded.
    resolution = manual.resolve_result(
        manual.manual_only_result("commandcode"), now=NOW, store=manual.load_store()
    )
    assert resolution.applied is False
    assert "commandcode" in (resolution.failure_reason or "")
    assert "rate_limited" in (resolution.failure_reason or "")

    _record_credit()
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


def test_the_pool_reads_free_again_after_the_named_reset():
    _observe_limit(reset=NOW + dt.timedelta(hours=6))
    _record_credit()
    after_reset = NOW + dt.timedelta(hours=6, seconds=1)
    resolution = manual.resolve_result(
        manual.manual_only_result("commandcode"), now=after_reset, store=manual.load_store()
    )
    assert resolution.applied is True, resolution.failure_reason


def test_limit_observation_uses_the_named_reset_without_a_fixed_horizon():
    """A reset beyond an assumed plan period is preserved exactly as entered."""

    named_reset = NOW + dt.timedelta(days=45)
    _observe_limit(reset=named_reset)
    record = commandcode_limit.active_record(now=NOW)
    assert record is not None
    assert record.reset_at == named_reset
    assert commandcode_limit.active_record(now=named_reset - dt.timedelta(seconds=1)) is not None
    assert commandcode_limit.active_record(now=named_reset) is None


def test_clear_hold_reopens_the_pool():
    _observe_limit()
    removed = commandcode_limit.clear_hold(now=NOW)
    assert removed is not None
    _record_credit()
    resolution = manual.resolve_result(
        manual.manual_only_result("commandcode"), now=NOW, store=manual.load_store()
    )
    assert resolution.applied is True, resolution.failure_reason


def test_observe_cli_records_and_clears(capsys):
    rc = cli.main(
        [
            "observe",
            "commandcode-limit",
            "--reset",
            (NOW + dt.timedelta(hours=6)).isoformat(),
            "--observed",
            NOW.isoformat(),
            "--rung",
            "cmd-dsflash41",
            "--source",
            "job:2141-test",
        ]
    )
    assert rc == 0
    assert "recorded" in capsys.readouterr().out
    assert commandcode_limit.active_record(now=NOW) is not None

    rc = cli.main(["observe", "commandcode-limit", "--clear"])
    assert rc == 0
    assert "cleared" in capsys.readouterr().out
    assert commandcode_limit.active_record(now=NOW) is None


def test_observe_cli_rejects_an_unknown_rung(capsys):
    """Fail closed: an observation for a profile we do not launch is refused."""

    rc = cli.main(
        [
            "observe",
            "commandcode-limit",
            "--reset",
            (NOW + dt.timedelta(hours=6)).isoformat(),
            "--observed",
            NOW.isoformat(),
            "--rung",
            "cmd-dsflash42",
            "--source",
            "job:2141-test",
        ]
    )
    assert rc == 2
    assert commandcode_limit.active_record(now=NOW) is None


def test_observe_limit_wording_fixture_is_a_placeholder():
    """Pins the fixture state: real cmd limit wording is still unknown (PR body)."""

    text = (
        pathlib.Path(__file__).parent / "fixtures" / "commandcode_limit_screen.placeholder.txt"
    ).read_text()
    assert "PLACEHOLDER" in text


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


def test_json_commandcode_observed_limit_fails_closed(capsys, monkeypatch):
    _record_credit()
    _observe_limit()
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


def test_gate_cli_admits_without_the_marker(monkeypatch, capsys):
    """hk 2185: the E6 marker is inert for a placed cmd rung."""

    monkeypatch.delenv("SCOPEFUEL_E6_ARM", raising=False)
    monkeypatch.setattr(cli, "collect", lambda *a, **k: [])
    _record_credit()
    rc = cli.main(["gate", "-m", "cmd-dsflash41", "--role", "worker", "--effort", "high"])
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert "pool=commandcode" in captured.out


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
