"""hk 2079: the anthropic-api credit pool and its pi E6 rungs.

The operator's Anthropic API credit (haiku/sonnet/opus 5.5) is spent IN
PARALLEL with the claude subscription CLI, through pi (no MCP).  It is a
separate pool, it has no automatic probe, and its balance is a manual
observation.  The pi rungs (one per profile and thinking level) are unmeasured
E6 catalog rows: marker-gated, worker/tester only, and they inherit no grade
from the claude rows.

Each test docstring names the mutant it kills where the brief lists one.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

import pytest

from scopefuel import bench, cli, launch, manual, providers, recommend
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    E6_ARM_ANNOTATION,
    E6_ARM_KEYS,
    E6_ARM_MARKER_ENV,
    GRADE_TABLE,
    e6_arm_rung_for,
    gate_check,
    profile_pool,
    provider_family,
)
from scopefuel.recommend import recommend as recommend_text

# The manual store validates timestamps against the real clock, so the fixed
# instant every test shares is taken from it once.
NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)
TODAY = NOW.date()

PI_PROFILES: dict[str, str] = {
    "pi-haiku55": "anthropic/claude-haiku-5-5",
    "pi-sonnet55": "anthropic/claude-sonnet-5-5",
    "pi-opus55": "anthropic/claude-opus-5-5",
}
PI_RUNGS = ("low", "medium", "high", "xhigh", "max")
# The rung a bare spelling names — it mirrors the claude profile of the same model.
PI_DEFAULT_RUNG = {"pi-haiku55": "low", "pi-sonnet55": "high", "pi-opus55": "high"}
PI_ROWS = [(profile, rung) for profile in sorted(PI_PROFILES) for rung in PI_RUNGS]
CREDIT_EXPIRES = (TODAY + dt.timedelta(days=30)).isoformat()


def _marker(profile: str, rung: str) -> str:
    return f"{profile}@{rung}"


def _record_credit(
    *,
    remaining: float = 150.0,
    total: float = 200.0,
    expires_on: str = CREDIT_EXPIRES,
    measured_at: dt.datetime = NOW,
    ttl_s: float = 900.0,
) -> dict:
    return manual.record_observation(
        pool="anthropic-api",
        window="credit",
        remaining_usd=remaining,
        total_usd=total,
        credit_expires_on=expires_on,
        measured_at=measured_at,
        reason="Anthropic console credit balance",
        ttl_s=ttl_s,
        now=measured_at,
    )


def _credit_result(*, now: dt.datetime = NOW, **kwargs) -> ProviderResult:
    _record_credit(**kwargs)
    resolution = manual.resolve_result(
        manual.manual_only_result("anthropic-api"), now=now, store=manual.load_store()
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


def _snapshot_view() -> launch.CatalogView:
    return launch.CatalogView(
        entries=bench.catalog_snapshot(), source=bench.CATALOG_SOURCE_SNAPSHOT, age_s=None
    )


def _set_manual_credit_cli(*, remaining: str = "150", total: str = "200", ttl: str = "15m") -> int:
    return cli.main(
        [
            "manual",
            "set",
            "--pool",
            "anthropic-api",
            "--window",
            "credit",
            "--remaining",
            remaining,
            "--total",
            total,
            "--expires-on",
            CREDIT_EXPIRES,
            "--measured-at",
            "now",
            "--reason",
            "Anthropic console credit balance",
            "--ttl",
            ttl,
        ]
    )


# ---------------------------------------------------------------------------
# 1. routing and family — the claude-pool mutant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", sorted(PI_PROFILES))
def test_pool_is_anthropic_api_not_claude(profile):
    """Mutant: map the pi profiles to the claude pool -> this goes RED."""

    assert profile_pool(profile) == ("anthropic-api", None)
    assert provider_family(profile) == "anthropic"
    # Control: the subscription rows stay on the claude pool.
    assert profile_pool("sonnet") == ("claude", None)
    assert provider_family("sonnet") == "claude"


def test_unregistered_pi_spelling_does_not_inherit_the_route():
    """The explicit-map rule: a pi- prefix is not a route."""

    assert profile_pool("pi-haiku56") == ("", None)
    assert provider_family("pi-haiku56") == ""
    assert profile_pool("pi-sonnet56") == ("", None)
    assert profile_pool("pi-") == ("", None)


def test_pi_rows_have_their_own_map_and_stay_out_of_the_oc_map():
    assert set(recommend._PI_PROFILE_POOLS) == set(PI_PROFILES)
    assert not set(PI_PROFILES) & set(recommend._OC_PROFILE_POOLS)
    # An oc- spelling of the same model is not the pi route either.
    assert profile_pool("oc-sonnet55") == ("", None)


def test_anthropic_api_has_no_automatic_probe():
    assert "anthropic-api" not in providers.BUILTIN
    assert "anthropic-api" not in providers.registry()
    synthetic = manual.manual_only_result("anthropic-api")
    assert synthetic.id == "anthropic-api"
    assert synthetic.error_kind == "manual_only"
    assert synthetic.pool_class == "spend"


# ---------------------------------------------------------------------------
# 2. catalog rows — the claude-grade mutant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("profile", "rung"), PI_ROWS)
def test_catalog_row_is_the_unmeasured_c_carrier(profile, rung):
    """Mutant: copy the claude grade into the pi rows -> this goes RED."""

    rows = [entry for entry in bench.catalog_snapshot() if entry.key == (profile, rung)]
    assert len(rows) == 1
    (row,) = rows
    assert row.grade == "C"
    assert row.score is None
    assert row.pool == "anthropic-api"
    assert row.model_id == PI_PROFILES[profile]
    assert row.gate == "default"
    assert row.benchmark_source is None
    assert row.benchmark_annotation.startswith(E6_ARM_ANNOTATION)
    assert "#594" in row.benchmark_annotation
    # The placement exclusion is recorded in the catalog notes.
    assert "worker/tester" in row.benchmark_annotation
    assert "MCP" in row.benchmark_annotation
    assert "pi" in row.benchmark_annotation


def test_no_grade_inheritance_from_the_claude_rows():
    """Mutant: copy the claude grade into the pi rows -> this goes RED."""

    # Control: the claude rows really do sit above C, so "C" is not a default.
    claude_grades = {
        grade
        for grade, profiles in GRADE_TABLE.items()
        for placed in profiles
        if placed.name in {"haiku", "sonnet", "opus"}
    }
    assert claude_grades - {"C"}
    # The pi profiles are not placements at any grade.
    assert all(placed.name not in PI_PROFILES for profiles in GRADE_TABLE.values() for placed in profiles)
    # And every catalog row for them is the C carrier, whatever the claude rows say.
    rows = [entry for entry in bench.catalog_snapshot() if entry.profile in PI_PROFILES]
    assert len(rows) == len(PI_ROWS)
    assert {entry.grade for entry in rows} == {"C"}
    assert {entry.score for entry in rows} == {None}


@pytest.mark.parametrize(("profile", "rung"), PI_ROWS)
def test_pi_rows_are_in_the_e6_arm_table(profile, rung):
    assert (profile, rung) in E6_ARM_KEYS
    row = e6_arm_rung_for(profile, rung)
    assert row is not None
    assert row.benchmark is None
    assert row.launcher_effort == rung


@pytest.mark.parametrize(("profile", "rung"), PI_ROWS)
def test_launch_spelling_takes_the_effort_flag(profile, rung):
    """pi's thinking level is the effort: wrk maps --effort to the :suffix."""

    assert launch.launcher_accepts_effort_flag(profile)
    assert launch.launch_spelling(profile, rung) == f"{profile} --effort {rung}"
    assert launch.DEFAULT_LAUNCH_EFFORTS[profile] == PI_DEFAULT_RUNG[profile]
    assert launch.LAUNCH_MODEL_IDS[profile] == PI_PROFILES[profile]


def test_a_served_canon_without_the_pi_rows_still_passes_the_validity_floor():
    """The pi profiles exist only as E6 rows; an unseeded canon is not a rejected one."""

    entries = [entry for entry in bench.catalog_snapshot() if entry.profile not in PI_PROFILES]
    assert len(entries) < len(bench.catalog_snapshot())
    assert bench._server_catalog_rejection(entries) is None


# ---------------------------------------------------------------------------
# 3. the manual credit shape
# ---------------------------------------------------------------------------


def test_credit_observation_derives_used_pct_and_carries_the_expiry():
    entry = _record_credit(remaining=150.0, total=200.0)
    assert entry["window"] == "credit"
    assert entry["used_pct"] == pytest.approx(25.0)
    assert entry["credit"] == {
        "remaining_usd": 150.0,
        "total_usd": 200.0,
        "expires_on": CREDIT_EXPIRES,
    }
    assert entry["resets_at"] is None


def test_credit_manual_set_cli_accepts_remaining_total_and_expiry(capsys):
    assert _set_manual_credit_cli() == 0
    out = capsys.readouterr().out
    assert "pool=anthropic-api" in out
    assert "window=credit" in out
    assert "used_pct=25" in out

    assert cli.main(["--json", "manual", "list", "--pool", "anthropic-api"]) == 0
    payload = json.loads(capsys.readouterr().out)
    (entry,) = payload["latest_valid"]
    assert entry["credit"]["remaining_usd"] == 150.0
    assert entry["credit"]["expires_on"] == CREDIT_EXPIRES


def test_credit_display_names_the_balance_and_days_left():
    result = _credit_result()
    text = manual.credit_display(result.manual, now=NOW)
    assert text is not None
    assert "$150.00/$200.00" in text
    assert "30일 남음" in text
    assert CREDIT_EXPIRES in text


def test_credit_window_rejects_inconsistent_shapes():
    with pytest.raises(manual.ManualError):
        manual.record_observation(
            pool="anthropic-api",
            window="credit",
            used_pct=10.0,
            measured_at=NOW,
            reason="no credit numbers",
            now=NOW,
        )
    with pytest.raises(manual.ManualError):
        _record_credit(remaining=250.0, total=200.0)
    with pytest.raises(manual.ManualError):
        _record_credit(total=0.0)
    # The credit numbers belong to the credit window only.
    with pytest.raises(manual.ManualError):
        manual.record_observation(
            pool="claude",
            window="7d",
            used_pct=10.0,
            remaining_usd=10.0,
            measured_at=NOW,
            reason="credit numbers on a rolling window",
            now=NOW,
        )


def test_credit_expiry_in_the_past_fails_closed():
    past = (TODAY - dt.timedelta(days=1)).isoformat()
    _record_credit(expires_on=past)
    resolution = manual.resolve_result(
        manual.manual_only_result("anthropic-api"), now=NOW, store=manual.load_store()
    )
    assert resolution.applied is False


def test_zero_remaining_balance_is_not_admitted():
    credit = _credit_result(remaining=0.0)
    result = gate_check(
        [credit],
        "pi-sonnet55",
        role="worker",
        effort="high",
        e6_arm="pi-sonnet55@high",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert result.provider_id == "anthropic-api"


# ---------------------------------------------------------------------------
# 4. gate admission
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("profile", "rung"), PI_ROWS)
def test_gate_refuses_a_rung_without_the_marker_as_unmeasured(profile, rung):
    credit = _credit_result()
    result = gate_check([credit], profile, role="worker", effort=rung, today=TODAY, now=NOW)
    assert result.ok is False
    assert "e6_arm_required" in result.reason
    assert f"SCOPEFUEL_E6_ARM={_marker(profile, rung)}" in result.reason


@pytest.mark.parametrize("profile", sorted(PI_PROFILES))
def test_gate_refuses_the_bare_spelling_without_the_marker(profile):
    """A bare ``gate -m pi-x`` names the profile's default rung — never the quota-only path."""

    credit = _credit_result()
    result = gate_check([credit], profile, role="worker", today=TODAY, now=NOW)
    assert result.ok is False
    assert "e6_arm_required" in result.reason
    assert _marker(profile, PI_DEFAULT_RUNG[profile]) in result.reason


def test_gate_refuses_a_marker_that_names_another_rung_or_profile():
    credit = _credit_result()
    for marker in ("pi-sonnet55@low", "pi-haiku55@high", "sonnet@high"):
        result = gate_check(
            [credit],
            "pi-sonnet55",
            role="worker",
            effort="high",
            e6_arm=marker,
            today=TODAY,
            now=NOW,
        )
        assert result.ok is False, marker
        assert "e6_arm_required" in result.reason


@pytest.mark.parametrize("role", ["worker", "tester"])
@pytest.mark.parametrize(("profile", "rung"), PI_ROWS)
def test_gate_admits_with_the_marker_when_credit_is_fresh_and_positive(profile, rung, role):
    credit = _credit_result()
    result = gate_check(
        [credit],
        profile,
        role=role,
        effort=rung,
        e6_arm=_marker(profile, rung),
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True, result.reason
    assert result.provider_id == "anthropic-api"
    assert result.e6_arm == _marker(profile, rung)
    assert result.used_pct == pytest.approx(25.0)


@pytest.mark.parametrize(("profile", "rung"), PI_ROWS)
def test_gate_refuses_the_builder_role_for_the_marker(profile, rung):
    """Mutant: drop the builder refusal -> this goes RED."""

    credit = _credit_result()
    result = gate_check(
        [credit],
        profile,
        role="builder",
        effort=rung,
        e6_arm=_marker(profile, rung),
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert result.role_denied is True
    assert "worker/tester" in result.reason
    assert result.e6_arm == _marker(profile, rung)


@pytest.mark.parametrize("role", [None, "", "captain", "reviewer", "orch"])
def test_gate_refuses_every_seat_other_than_worker_and_tester(role):
    """The brief says worker or tester only — an unnamed seat is not a worker."""

    credit = _credit_result()
    result = gate_check(
        [credit],
        "pi-sonnet55",
        role=role,
        effort="high",
        e6_arm="pi-sonnet55@high",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert result.role_denied is True


@pytest.mark.parametrize("profile", sorted(PI_PROFILES))
def test_gate_reason_names_anthropic_api_not_claude(profile):
    credit = _credit_result()
    result = gate_check(
        [credit],
        profile,
        role="worker",
        effort="high",
        e6_arm=_marker(profile, "high"),
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True
    assert result.provider_id == "anthropic-api"
    assert "pool=anthropic-api" in result.reason
    assert "pool=claude" not in result.reason


def test_missing_manual_value_refuses_admission():
    resolution = manual.resolve_result(
        manual.manual_only_result("anthropic-api"), now=NOW, store=manual._empty_store()
    )
    assert resolution.applied is False
    assert resolution.failure_reason == "manual observation 없음"
    # What the gate sees when the resolution did not apply is the probe-less
    # synthetic result: unmeasurable, not admitted.
    result = gate_check(
        [manual.manual_only_result("anthropic-api")],
        "pi-sonnet55",
        role="worker",
        effort="high",
        e6_arm="pi-sonnet55@high",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert result.unmeasurable is True


def test_stale_manual_value_refuses_admission():
    _record_credit(measured_at=NOW, ttl_s=900.0)
    resolution = manual.resolve_result(
        manual.manual_only_result("anthropic-api"),
        now=NOW + dt.timedelta(hours=1),
        store=manual.load_store(),
    )
    assert resolution.applied is False
    assert resolution.failure_reason is not None
    assert "credit" in resolution.failure_reason


def test_gate_cli_refuses_when_the_manual_value_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(cli, "registry", lambda: {"claude": _claude_result})
    monkeypatch.setenv(E6_ARM_MARKER_ENV, "pi-sonnet55@high")
    rc = cli.main(["gate", "-m", "pi-sonnet55", "--effort", "high", "--role", "worker", "--no-cache"])
    captured = capsys.readouterr()
    assert rc == 4
    assert "manual observation 없음" in captured.err


def test_gate_cli_refuses_when_the_manual_value_is_stale(monkeypatch, capsys):
    stale_measured = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat()
    _record_credit(measured_at=manual.parse_timestamp(stale_measured), ttl_s=900.0)
    monkeypatch.setattr(cli, "registry", lambda: {"claude": _claude_result})
    monkeypatch.setenv(E6_ARM_MARKER_ENV, "pi-sonnet55@high")
    rc = cli.main(["gate", "-m", "pi-sonnet55", "--effort", "high", "--role", "worker", "--no-cache"])
    captured = capsys.readouterr()
    assert rc == 4
    assert "credit" in captured.err


def test_gate_cli_refuses_without_the_marker(monkeypatch, capsys):
    assert _set_manual_credit_cli() == 0
    capsys.readouterr()
    monkeypatch.setattr(cli, "registry", lambda: {"claude": _claude_result})
    monkeypatch.delenv(E6_ARM_MARKER_ENV, raising=False)
    for argv in (
        ["gate", "-m", "pi-sonnet55", "--effort", "high", "--role", "worker", "--no-cache"],
        ["gate", "-m", "pi-sonnet55", "--role", "worker", "--no-cache"],
    ):
        rc = cli.main(argv)
        captured = capsys.readouterr()
        assert rc == 3, argv
        assert "e6_arm_required" in captured.err + captured.out


def test_gate_cli_allow_line_shows_pool_credit_and_days_left(monkeypatch, capsys, tmp_path):
    assert _set_manual_credit_cli() == 0
    capsys.readouterr()
    monkeypatch.setattr(cli, "registry", lambda: {"claude": _claude_result})
    monkeypatch.setenv(E6_ARM_MARKER_ENV, "pi-sonnet55@high")
    record_path = tmp_path / "gate.json"
    rc = cli.main(
        [
            "gate",
            "-m",
            "pi-sonnet55",
            "--effort",
            "high",
            "--role",
            "worker",
            "--no-cache",
            "--gate-output",
            str(record_path),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "pool=anthropic-api" in captured.out
    assert "$150.00/$200.00" in captured.out
    assert "30일 남음" in captured.out
    record = json.loads(record_path.read_text())
    assert record["provider_id"] == "anthropic-api"
    assert record["e6_arm"] == "pi-sonnet55@high"
    (observation,) = record["manual_observations"]
    assert observation["credit"]["remaining_usd"] == 150.0


# ---------------------------------------------------------------------------
# 5. launch resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("profile", "rung"), PI_ROWS)
def test_launch_resolves_the_marked_rung_with_the_anthropic_model_id(profile, rung):
    decision = launch.resolve_launch(
        profile, effort=rung, role="worker", e6_arm=_marker(profile, rung), view=_snapshot_view()
    )
    assert decision.model_id == PI_PROFILES[profile]
    assert decision.pool == "anthropic-api"
    assert decision.family == "anthropic"
    assert decision.effort == rung
    assert decision.grade == "C"


@pytest.mark.parametrize(("profile", "rung"), PI_ROWS)
def test_launch_resolves_from_the_bundled_row_when_the_served_canon_is_silent(profile, rung):
    """A served canon not yet seeded with the pi rows must not hide the marked rung."""

    view = launch.CatalogView(
        entries=tuple(entry for entry in bench.catalog_snapshot() if entry.profile == "sonnet"),
        source=bench.CATALOG_SOURCE_CACHE,
        age_s=0.0,
    )
    decision = launch.resolve_launch(
        profile, effort=rung, role="worker", e6_arm=_marker(profile, rung), view=view
    )
    assert (decision.model_id, decision.pool, decision.effort) == (
        PI_PROFILES[profile],
        "anthropic-api",
        rung,
    )
    # Without the marker the same silent canon is still a refusal, never a fallback.
    with pytest.raises(launch.LaunchError):
        launch.resolve_launch(profile, effort=rung, role="worker", view=view)


def test_launch_refuses_without_the_marker_and_for_the_builder_seat():
    view = _snapshot_view()
    for kwargs in ({"effort": "high"}, {}):
        with pytest.raises(launch.LaunchError, match="e6_arm_required"):
            launch.resolve_launch("pi-sonnet55", role="worker", view=view, **kwargs)
    with pytest.raises(launch.LaunchError, match="role_restricted"):
        launch.resolve_launch(
            "pi-sonnet55", effort="high", role="builder", e6_arm="pi-sonnet55@high", view=view
        )


# ---------------------------------------------------------------------------
# 6. separate pools, parallel use, ranking
# ---------------------------------------------------------------------------


def test_anthropic_api_spend_record_does_not_move_the_claude_pool_numbers():
    claude = _claude_result(used=10.0)
    before = manual.resolve_result(claude, now=NOW, store=manual._empty_store())
    credit = _credit_result()
    after = manual.resolve_result(claude, now=NOW, store=manual.load_store())
    assert before.result.buckets == after.result.buckets
    assert credit.id == "anthropic-api"
    assert credit.buckets[0].window == "credit"
    assert credit.buckets[0].used_pct == pytest.approx(25.0)
    # And the claude gate answer is the same with and without the credit record.
    without = gate_check([claude], "sonnet", role="worker", today=TODAY, now=NOW)
    with_credit = gate_check([claude, credit], "sonnet", role="worker", today=TODAY, now=NOW)
    assert (without.ok, without.used_pct, without.provider_id) == (
        with_credit.ok,
        with_credit.used_pct,
        with_credit.provider_id,
    )


def test_pi_row_is_admitted_while_the_claude_pool_still_has_headroom():
    claude = _claude_result(used=10.0)
    credit = _credit_result()
    result = gate_check(
        [claude, credit],
        "pi-sonnet55",
        role="worker",
        effort="high",
        e6_arm="pi-sonnet55@high",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True, result.reason
    # The claude pool is still far from its cutoff; admission did not wait for it.
    assert claude.buckets[0].used_pct == 10.0
    assert result.provider_id == "anthropic-api"


def test_recommend_shows_the_credit_balance_and_days_left():
    credit = _credit_result()
    out = recommend_text([_claude_result(), credit], "C", today=TODAY, now=NOW)
    assert "anthropic-api credit $150.00/$200.00" in out
    assert "30일 남음" in out


def _ranked_names(out: str) -> list[str]:
    names = []
    for line in out.splitlines():
        head, _, rest = line.partition(". ")
        if head.isdigit():
            names.append(rest.split()[0])
    return names


def test_credit_row_ranks_below_an_eligible_subscription_row():
    """r1026prio: the credit is a parallel option, never ahead of subscription quota."""

    credit = _credit_result()
    out = recommend_text([_claude_result(used=10.0), credit], "C", today=TODAY, now=NOW)
    names = _ranked_names(out)
    credit_idx = [i for i, name in enumerate(names) if name in PI_PROFILES]
    subscription_idx = [i for i, name in enumerate(names) if name in {"haiku", "sonnet", "opus"}]
    assert credit_idx, out
    assert subscription_idx, out
    assert max(subscription_idx) < min(credit_idx), out


def test_credit_rows_are_not_listed_without_a_usable_balance():
    out = recommend_text([_claude_result()], "C", today=TODAY, now=NOW)
    assert not set(_ranked_names(out)) & set(PI_PROFILES)
    out = recommend_text(
        [_claude_result(), manual.manual_only_result("anthropic-api")], "C", today=TODAY, now=NOW
    )
    assert not set(_ranked_names(out)) & set(PI_PROFILES)


def test_credit_rows_are_not_listed_above_their_carrier_grade():
    credit = _credit_result()
    out = recommend_text([_claude_result(), credit], "A", today=TODAY, now=NOW)
    assert not set(_ranked_names(out)) & set(PI_PROFILES)


# ---------------------------------------------------------------------------
# 7. tester round 1 findings, kept as regressions
# ---------------------------------------------------------------------------


def _consult_only_view(profile: str = "pi-sonnet55", rung: str = "high") -> launch.CatalogView:
    """A served canon whose only change is the gate of one unmeasured credit row."""

    entries = tuple(
        replace(entry, gate="consult_only") if entry.key == (profile, rung) else entry
        for entry in bench.catalog_snapshot()
    )
    return launch.CatalogView(entries=entries, source=bench.CATALOG_SOURCE_CACHE, age_s=0)


@pytest.mark.parametrize(
    "marker", ["pi-sonnet55@bogus", "pi-sonnet55@ultra", "pi-sonnet55@off", "pi-sonnet55@default"]
)
@pytest.mark.parametrize("role", [None, "builder", "worker"])
def test_gate_cli_refuses_a_marker_that_names_an_unregistered_rung(monkeypatch, capsys, marker, role):
    """F1: a marker rung that is not a registered row must not fall into the quota-only path."""

    assert _set_manual_credit_cli() == 0
    capsys.readouterr()
    monkeypatch.setattr(cli, "registry", lambda: {})
    monkeypatch.setenv(E6_ARM_MARKER_ENV, marker)
    argv = ["gate", "-m", "pi-sonnet55", "--no-cache"]
    if role is not None:
        argv += ["--role", role]
    rc = cli.main(argv)
    captured = capsys.readouterr()
    assert rc in {3, 5}, captured.out + captured.err
    assert "e6_arm_required" in captured.out + captured.err


@pytest.mark.parametrize("rung", ["bogus", "ultra", "off", "default"])
@pytest.mark.parametrize("role", [None, "builder", "worker", "tester"])
def test_gate_refuses_every_unregistered_marker_rung_for_every_seat(rung, role):
    credit = _credit_result()
    result = gate_check(
        [credit], "pi-sonnet55", role=role, e6_arm=f"pi-sonnet55@{rung}", today=TODAY, now=NOW
    )
    assert result.ok is False
    assert "e6_arm_required" in result.reason


@pytest.mark.parametrize("role", [None, "builder", "worker"])
def test_launch_refuses_a_consult_only_credit_row_without_the_marker(role):
    """F2: a consult_only gate on an unmeasured credit row keeps the marker rule."""

    with pytest.raises(launch.LaunchError):
        launch.resolve_launch(
            "pi-sonnet55",
            effort="high",
            role=role,
            operator_request="hk:task/2079",
            view=_consult_only_view(),
        )


@pytest.mark.parametrize("role", [None, "builder", "reviewer"])
def test_launch_refuses_a_consult_only_credit_row_for_any_other_seat_with_the_marker(role):
    with pytest.raises(launch.LaunchError, match="role_restricted"):
        launch.resolve_launch(
            "pi-sonnet55",
            effort="high",
            role=role,
            operator_request="hk:task/2079",
            e6_arm="pi-sonnet55@high",
            view=_consult_only_view(),
        )


def test_launch_admits_a_consult_only_credit_row_for_a_worker_with_marker_and_request():
    decision = launch.resolve_launch(
        "pi-sonnet55",
        effort="high",
        role="worker",
        operator_request="hk:task/2079",
        e6_arm="pi-sonnet55@high",
        view=_consult_only_view(),
    )
    assert (decision.model_id, decision.gate, decision.effort) == (
        "anthropic/claude-sonnet-5-5",
        "consult_only",
        "high",
    )


@pytest.mark.parametrize("role", [None, "builder", "worker"])
def test_gate_cli_keeps_the_marker_rule_on_a_consult_only_credit_row(monkeypatch, capsys, role):
    assert _set_manual_credit_cli() == 0
    capsys.readouterr()
    view = _consult_only_view()
    monkeypatch.setattr(cli, "registry", lambda: {})
    monkeypatch.setattr(bench, "read_catalog", lambda **kwargs: view)
    monkeypatch.setattr(bench, "runtime_grade_table", lambda **kwargs: bench._catalog_grade_table(view))
    monkeypatch.delenv(E6_ARM_MARKER_ENV, raising=False)
    argv = [
        "gate",
        "-m",
        "pi-sonnet55",
        "--effort",
        "high",
        "--operator-request",
        "hk:task/2079",
        "--no-cache",
    ]
    if role is not None:
        argv += ["--role", role]
    rc = cli.main(argv)
    captured = capsys.readouterr()
    assert rc in {3, 5}, captured.out + captured.err


def test_gate_keeps_the_seat_rule_on_a_consult_only_credit_row_with_the_marker():
    credit = _credit_result()
    for role, ok in (("worker", True), ("tester", True), ("builder", False), (None, False)):
        result = gate_check(
            [credit],
            "pi-sonnet55",
            role=role,
            effort="high",
            e6_arm="pi-sonnet55@high",
            operator_request="hk:task/2079",
            consult_only_rungs=frozenset({("pi-sonnet55", "high")}),
            today=TODAY,
            now=NOW,
        )
        assert result.ok is ok, (role, result.reason)
        if not ok:
            assert result.role_denied is True


def test_recommend_does_not_restore_a_consult_only_credit_row():
    """F3: the canon's consult_only exclusion stays; the offering only replaces the E6 fold."""

    view = _consult_only_view()
    table = bench._catalog_grade_table(view)
    assert table is not None
    payload = recommend.recommend_dict([_credit_result()], "C", today=TODAY, now=NOW, grade_table=table)
    rows = [r for r in payload["rows"] if r["profile"] == "pi-sonnet55" and r["kind"] == "candidate"]
    assert not rows, rows
    assert any(
        r["profile"] == "pi-sonnet55" and r["effort"] == "high" and "consult_only" in r["reason"]
        for r in payload["excluded"]
    )
    # The other default rungs are still offered, once each.
    offered = {
        r["profile"] for r in payload["rows"] if r["kind"] == "candidate" and r["profile"] in PI_PROFILES
    }
    assert offered == {"pi-haiku55", "pi-opus55"}


@pytest.mark.parametrize(
    "edit", ["used", "zero-remaining", "negative", "nan", "zero-total", "missing-credit", "invalid-date"]
)
def test_hand_edited_credit_store_is_rejected(edit):
    """F4: the stored used_pct must equal the value the USD numbers derive."""

    _record_credit()
    path = manual.manual_path()
    store = json.loads(path.read_text())
    entry = store["history"][-1]
    if edit == "used":
        entry["used_pct"] = 0.0
        entry["entitlement_and_limits"]["used_pct"] = 0.0
    elif edit == "zero-remaining":
        entry["credit"]["remaining_usd"] = 0.0
    elif edit == "negative":
        entry["credit"]["remaining_usd"] = -1
    elif edit == "nan":
        entry["credit"]["remaining_usd"] = float("nan")
    elif edit == "zero-total":
        entry["credit"]["total_usd"] = 0
    elif edit == "missing-credit":
        entry.pop("credit")
    elif edit == "invalid-date":
        entry["credit"]["expires_on"] = "nonsense"
    path.write_text(json.dumps(store))
    with pytest.raises(manual.ManualError):
        manual.load_store()


@pytest.mark.parametrize("pool", sorted(providers.BUILTIN))
def test_used_is_still_required_for_every_probed_pool(pool, capsys):
    rc = cli.main(
        [
            "manual",
            "set",
            "--pool",
            pool,
            "--window",
            "7d",
            "--measured-at",
            "now",
            "--reason",
            "missing used",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "--used" in captured.err
