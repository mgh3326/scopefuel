"""hk 2079: the anthropic-api credit pool and its pi rungs.

The operator's Anthropic API credit (haiku/sonnet/opus 5.5) is spent IN
PARALLEL with the claude subscription CLI, through pi (no MCP).  It is a
separate pool, it has no automatic probe, and its balance is a manual
observation.

hk 2185 (operator desk op-1028-grades FINAL) supersedes the original E6
contract: the pi rungs are ordinary per-effort PLACEMENTS now, each inheriting
the claude-code harness profile's grade for the same model and effort. They
keep the worker/tester-only seat rule and the closed rung vocabulary (the
thinking level is the effort), and the E6 marker is inert for them.

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
    ALTLANE_CANON_ROWS,
    E6_ARM_KEYS,
    E6_ARM_MARKER_ENV,
    GRADE_TABLE,
    altlane_annotation,
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
# hk 2185: only the rungs the canon places (pi-haiku55@low is retired upstream).
CANON = {(profile, rung): (grade, gate) for profile, rung, grade, gate, _reason in ALTLANE_CANON_ROWS}
PI_PLACED = [
    (profile, rung)
    for profile in sorted(PI_PROFILES)
    for rung in PI_RUNGS
    if (profile, rung) in CANON
]
PI_ROWS = PI_PLACED
PI_DEFAULT_RUNGS = [(profile, rung) for profile, rung in PI_PLACED if CANON[(profile, rung)][1] == "default"]
# hk 2185: the source claude-harness profile whose grade each pi rung inherits.
PI_SOURCE = {"pi-haiku55": "haiku", "pi-sonnet55": "sonnet", "pi-opus55": "opus"}
CREDIT_EXPIRES = (TODAY + dt.timedelta(days=30)).isoformat()


def _placed_grade(profile: str, rung: str) -> str | None:
    for grade, rows in GRADE_TABLE.items():
        for row in rows:
            if row.name == profile and row.launcher_effort == rung:
                return grade
    return None


def _placed_gate(profile: str, rung: str) -> str | None:
    for rows in GRADE_TABLE.values():
        for row in rows:
            if row.name == profile and row.launcher_effort == rung:
                return row.gate
    return None


def _marker(profile: str, rung: str) -> str:
    return f"{profile}@{rung}"


def _record_credit(
    *,
    remaining: float = 150.0,
    total: float = 200.0,
    expires_on: str = CREDIT_EXPIRES,
    measured_at: dt.datetime = NOW,
    ttl_s: float | None = None,
    now: dt.datetime = NOW,
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
        now=now,
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


def _set_manual_credit_cli(*, remaining: str = "150", total: str = "200", ttl: str | None = None) -> int:
    args = [
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
    ]
    if ttl is not None:
        args.extend(["--ttl", ttl])
    return cli.main(args)


# ---------------------------------------------------------------------------
# 1. routing and family — the claude-pool mutant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", sorted(PI_PROFILES))
def test_pool_is_anthropic_api_not_claude(profile):
    """Mutant: map the pi profiles to the claude pool -> this goes RED."""

    assert profile_pool(profile) == ("anthropic-api", None)
    # hk 2185 (op-1028-grades FINAL): pi-* are Anthropic FAMILY, the same family
    # value the claude subscription rows carry — a pi tester for a claude
    # builder is refused as same-family.
    assert provider_family(profile) == "claude"
    # Control: the subscription rows stay on the claude pool.
    assert profile_pool("sonnet") == ("claude", None)
    assert provider_family("sonnet") == "claude"
    assert provider_family(profile) == provider_family("sonnet")


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
# 2. catalog rows — the inherited-placement mutant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("profile", "rung"), PI_ROWS)
def test_catalog_row_is_the_inherited_placement(profile, rung):
    """Mutant: hand-copy a grade instead of inheriting it -> this goes RED."""

    rows = [entry for entry in bench.catalog_snapshot() if entry.key == (profile, rung)]
    assert len(rows) == 1
    (row,) = rows
    assert row.grade == _placed_grade(profile, rung) == CANON[(profile, rung)][0]
    assert row.score is None
    assert row.pool == "anthropic-api"
    assert row.model_id == PI_PROFILES[profile]
    assert row.gate == _placed_gate(profile, rung) == CANON[(profile, rung)][1]
    assert row.benchmark_source is None
    # hk 2185: the canon annotation, verbatim.
    assert row.benchmark_annotation == altlane_annotation(profile, rung)
    assert row.benchmark_annotation.startswith("inherited from claude harness")


def test_pi_canon_rows_inherit_from_the_claude_rows():
    """Each canon row inherits the claude row's grade per effort — read, not copied.

    The canon itself re-placed a few claude rungs before hk 2185
    (``ALTLANE_CANON_DIVERGENCES``); those are named, not silently accepted.
    """

    diverged = set()
    for profile, rung, grade, _gate, _reason in ALTLANE_CANON_ROWS:
        if profile not in PI_SOURCE:
            continue
        source = recommend.ALTLANE_CANON_SOURCES[(profile, rung)]
        source_profile, _, source_effort = source.partition("@")
        if (profile, rung) in recommend.ALTLANE_CANON_DIVERGENCES:
            diverged.add((profile, rung))
            assert grade != recommend.source_placement_grade(source_profile, source_effort)
            continue
        assert grade == recommend.source_placement_grade(source_profile, source_effort), (profile, rung)
    assert diverged == set(recommend.ALTLANE_CANON_DIVERGENCES)
    # Control: the source rows really do span several grades, so this is not a
    # constant comparison.
    opus_grades = {recommend.source_placement_grade("opus", rung) for rung in PI_RUNGS}
    sonnet_grades = {recommend.source_placement_grade("sonnet", rung) for rung in PI_RUNGS}
    assert len(opus_grades) > 1 and len(sonnet_grades) > 1


@pytest.mark.parametrize(("profile", "rung"), PI_ROWS)
def test_pi_rows_are_ordinary_placements_not_e6_arms(profile, rung):
    """hk 2185: a placed rung is an ordinary row — no marker, no carrier fold."""

    assert (profile, rung) not in E6_ARM_KEYS
    assert e6_arm_rung_for(profile, rung) is None
    assert recommend.is_e6_unmeasured_key(profile, rung) is False
    assert recommend.e6_only_profile(profile) is False


@pytest.mark.parametrize(("profile", "rung"), PI_ROWS)
def test_launch_spelling_takes_the_effort_flag(profile, rung):
    """pi's thinking level is the effort: wrk maps --effort to the :suffix."""

    assert launch.launcher_accepts_effort_flag(profile)
    assert launch.launch_spelling(profile, rung) == f"{profile} --effort {rung}"
    assert launch.DEFAULT_LAUNCH_EFFORTS[profile] == PI_DEFAULT_RUNG[profile]
    assert launch.LAUNCH_MODEL_IDS[profile] == PI_PROFILES[profile]


def test_a_served_canon_without_the_pi_rows_is_rejected_until_reseeded():
    """The pi rows are placements now, so an unseeded canon is incomplete (fail closed)."""

    entries = [entry for entry in bench.catalog_snapshot() if entry.profile not in PI_PROFILES]
    assert len(entries) < len(bench.catalog_snapshot())
    rejection = bench._server_catalog_rejection(entries)
    assert rejection is not None
    assert "missing 3 snapshot profiles" in rejection


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
    validity = manual.parse_timestamp(entry["expires_at"]) - manual.parse_timestamp(entry["entered_at"])
    assert validity > dt.timedelta(hours=24)
    assert entry["credit_ttl_policy"] == "credit-until-expiry-v2"
    assert manual.parse_timestamp(entry["expires_at"]) == dt.datetime.combine(
        dt.date.fromisoformat(CREDIT_EXPIRES), dt.time.min, tzinfo=dt.UTC
    )


def test_credit_display_names_the_balance_and_days_left():
    entry = _record_credit()
    text = manual.credit_display({"latest_valid": [entry]}, now=NOW)
    assert text is not None
    assert "$150.00/$200.00" in text
    assert "30일 남음" in text
    assert CREDIT_EXPIRES in text
    assert f"valid_until={entry['expires_at']}" in text
    assert "(in " in text

    payload = manual.list_payload(pool="anthropic-api", now=NOW)
    status_line = manual.format_list(payload)
    assert f"valid_until={entry['expires_at']}" in status_line


def test_credit_ttl_accepts_shorter_values_without_a_24_hour_cap(capsys):
    assert _set_manual_credit_cli(ttl="12h") == 0
    (entry,) = manual.load_store()["history"]
    assert manual.parse_timestamp(entry["expires_at"]) == manual.parse_timestamp(
        entry["measured_at"]
    ) + dt.timedelta(hours=12)
    assert entry["credit_ttl_policy"] == "credit-until-expiry-v2"

    capsys.readouterr()
    assert _set_manual_credit_cli(ttl="30h") == 0
    (entry,) = manual.load_store()["history"][-1:]
    assert manual.parse_timestamp(entry["expires_at"]) == manual.parse_timestamp(
        entry["measured_at"]
    ) + dt.timedelta(hours=30)


def test_credit_ttl_help_explains_pool_limit_and_expiry_default(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["manual", "set", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "2h" in help_text
    assert "expires_on" in help_text
    assert "midnight UTC" in help_text


def test_credit_expiring_today_is_refused_at_record_time():
    """Mutant: drop the credit expires_on cap -> today's balance is accepted."""

    with pytest.raises(manual.ManualError, match="만료"):
        _record_credit(expires_on=TODAY.isoformat())


def test_credit_expiry_is_midnight_utc_and_stale_at_that_boundary():
    """Mutant: drop the midnight bound -> one second after expiry stays fresh."""

    expires_on = TODAY + dt.timedelta(days=1)
    entry = _record_credit(expires_on=expires_on.isoformat())
    expiry_start = dt.datetime.combine(expires_on, dt.time.min, tzinfo=dt.UTC)
    assert manual.parse_timestamp(entry["expires_at"]) == expiry_start

    fresh_resolution = manual.resolve_result(
        manual.manual_only_result("anthropic-api"),
        now=expiry_start - dt.timedelta(seconds=1),
        store=manual.load_store(),
    )
    assert fresh_resolution.applied is True
    fresh_gate = gate_check(
        [fresh_resolution.result],
        "pi-sonnet55",
        role="worker",
        effort="high",
        e6_arm="pi-sonnet55@high",
        today=expires_on - dt.timedelta(days=1),
        now=expiry_start - dt.timedelta(seconds=1),
    )
    assert fresh_gate.ok is True

    resolution = manual.resolve_result(
        manual.manual_only_result("anthropic-api"),
        now=expiry_start + dt.timedelta(seconds=1),
        store=manual.load_store(),
    )
    assert resolution.applied is False
    assert "credit" in (resolution.failure_reason or "")

    result = gate_check(
        [resolution.result],
        "pi-sonnet55",
        role="worker",
        effort="high",
        e6_arm="pi-sonnet55@high",
        today=expires_on,
        now=expiry_start + dt.timedelta(seconds=1),
    )
    assert result.ok is False
    assert result.unmeasurable is True


def test_credit_observation_three_days_old_remains_admissible_before_expiry():
    """Mutant: restore the 24h cap for v2 -> this three-days-old case goes RED."""

    measured_at = NOW - dt.timedelta(days=3)
    expires_on = TODAY + dt.timedelta(days=10)
    entry = _record_credit(
        measured_at=measured_at,
        expires_on=expires_on.isoformat(),
        now=NOW,
    )
    assert entry["credit_ttl_policy"] == "credit-until-expiry-v2"
    assert manual.parse_timestamp(entry["expires_at"]) == dt.datetime.combine(
        expires_on, dt.time.min, tzinfo=dt.UTC
    )

    resolution = manual.resolve_result(
        manual.manual_only_result("anthropic-api"), now=NOW, store=manual.load_store()
    )
    assert resolution.applied is True
    gate = gate_check(
        [resolution.result],
        "pi-sonnet55",
        role="worker",
        effort="high",
        e6_arm="pi-sonnet55@high",
        today=TODAY,
        now=NOW,
    )
    assert gate.ok is True


@pytest.mark.parametrize("offset", [dt.timedelta(0), dt.timedelta(seconds=1)])
def test_credit_observation_on_or_after_expiry_midnight_is_refused(offset):
    expires_on = TODAY + dt.timedelta(days=1)
    expiry_start = dt.datetime.combine(expires_on, dt.time.min, tzinfo=dt.UTC)
    with pytest.raises(manual.ManualError, match="만료"):
        _record_credit(
            expires_on=expires_on.isoformat(),
            measured_at=NOW,
            now=expiry_start + offset,
        )


def test_legacy_credit_overrun_is_clamped_on_read_without_rewriting_store():
    expiry_start = dt.datetime.combine(TODAY, dt.time.min, tzinfo=dt.UTC)
    measured_at = expiry_start - dt.timedelta(hours=1)
    manual.record_observation(
        pool="anthropic-api",
        window="credit",
        remaining_usd=150,
        total_usd=200,
        credit_expires_on=TODAY.isoformat(),
        measured_at=measured_at,
        reason="legacy credit fixture",
        ttl_s=24 * 3600.0,
        now=measured_at,
    )

    path = manual.manual_path()
    store = json.loads(path.read_text())
    legacy = store["history"][-1]
    legacy.pop("credit_ttl_policy")
    legacy["expires_at"] = manual._iso(measured_at + dt.timedelta(hours=2))
    path.write_text(json.dumps(store))
    original = path.read_bytes()

    loaded = manual.load_store()
    assert loaded["history"][-1]["expires_at"] == legacy["expires_at"]
    before_expiry = measured_at + dt.timedelta(minutes=30)
    payload = manual.list_payload(pool="anthropic-api", now=before_expiry)
    (view,) = payload["entries"]
    assert view["status"] == "active"
    assert view["expires_at"] == manual._iso(expiry_start)
    assert view["legacy_clamped"] is True
    list_line = manual.format_list(payload)
    assert f"valid_until={manual._iso(expiry_start)} (in 30m)" in list_line
    assert "legacy-clamped=true" in list_line
    display = manual.credit_display(payload, now=before_expiry)
    assert display is not None
    assert "legacy-clamped" in display
    assert path.read_bytes() == original

    resolution = manual.resolve_result(
        manual.manual_only_result("anthropic-api"),
        now=expiry_start,
        store=manual.load_store(),
    )
    assert resolution.applied is False
    result = gate_check(
        [resolution.result],
        "pi-sonnet55",
        role="worker",
        effort="high",
        e6_arm="pi-sonnet55@high",
        today=TODAY,
        now=expiry_start,
    )
    assert result.ok is False
    assert result.unmeasurable is True
    assert path.read_bytes() == original


def test_hand_edited_v1_credit_expiry_beyond_24_hour_bound_is_rejected():
    """Mutant: remove the v1 24h bound -> this hand edit passes."""

    _record_credit(ttl_s=24 * 3600.0)
    path = manual.manual_path()
    store = json.loads(path.read_text())
    entry = store["history"][-1]
    entry["credit_ttl_policy"] = "credit-24h-v1"
    too_late = manual.parse_timestamp(entry["measured_at"]) + dt.timedelta(hours=24, seconds=1)
    entry["expires_at"] = manual._iso(too_late)
    path.write_text(json.dumps(store))
    with pytest.raises(manual.ManualError, match="expiry 불일치"):
        manual.load_store()


def test_hand_edited_v2_credit_expiry_beyond_expires_on_is_rejected():
    """Mutant: remove the v2 midnight bound -> this hand edit passes."""

    expires_on = TODAY + dt.timedelta(days=10)
    _record_credit(expires_on=expires_on.isoformat())
    path = manual.manual_path()
    store = json.loads(path.read_text())
    entry = store["history"][-1]
    beyond_midnight = dt.datetime.combine(expires_on, dt.time.min, tzinfo=dt.UTC) + dt.timedelta(seconds=1)
    entry["expires_at"] = manual._iso(beyond_midnight)
    path.write_text(json.dumps(store))
    with pytest.raises(manual.ManualError, match="expiry 불일치"):
        manual.load_store()


def test_legacy_credit_expiry_beyond_old_two_hour_bound_is_rejected():
    _record_credit()
    path = manual.manual_path()
    store = json.loads(path.read_text())
    entry = store["history"][-1]
    entry.pop("credit_ttl_policy")
    too_late = manual.parse_timestamp(entry["measured_at"]) + dt.timedelta(hours=2, seconds=1)
    entry["expires_at"] = manual._iso(too_late)
    path.write_text(json.dumps(store))
    with pytest.raises(manual.ManualError, match="expiry 불일치"):
        manual.load_store()


def test_v1_credit_observation_keeps_its_24_hour_expiry_bound():
    _record_credit(ttl_s=24 * 3600.0)
    path = manual.manual_path()
    store = json.loads(path.read_text())
    entry = store["history"][-1]
    entry["credit_ttl_policy"] = "credit-24h-v1"
    path.write_text(json.dumps(store))

    loaded = manual.load_store()
    assert loaded["history"][-1]["credit_ttl_policy"] == "credit-24h-v1"
    assert manual.parse_timestamp(entry["expires_at"]) == manual.parse_timestamp(
        entry["measured_at"]
    ) + dt.timedelta(hours=24)


def test_hand_edited_v1_expiry_beyond_expires_on_is_rejected():
    """Mutant: remove the v1 midnight bound -> this hand edit passes."""

    expires_on = TODAY + dt.timedelta(days=1)
    measured_at = dt.datetime.combine(TODAY, dt.time(0, 2), tzinfo=dt.UTC)
    _record_credit(expires_on=expires_on.isoformat(), measured_at=measured_at)
    path = manual.manual_path()
    store = json.loads(path.read_text())
    entry = store["history"][-1]
    entry["credit_ttl_policy"] = "credit-24h-v1"
    beyond_midnight = dt.datetime.combine(expires_on, dt.time.min, tzinfo=dt.UTC) + dt.timedelta(seconds=30)
    entry["expires_at"] = manual._iso(beyond_midnight)
    path.write_text(json.dumps(store))
    with pytest.raises(manual.ManualError, match="expiry 불일치"):
        manual.load_store()


def test_hand_edited_noncredit_expiry_beyond_two_hours_is_rejected():
    """Mutant: let _validate_store accept any expiry -> this hand edit passes."""

    manual.record_observation(
        pool="devin",
        used_pct=8,
        window="daily",
        measured_at=NOW,
        reason="daily console reading",
        ttl_s=manual.MAX_TTL_S,
        now=NOW,
    )
    path = manual.manual_path()
    store = json.loads(path.read_text())
    entry = store["history"][-1]
    too_late = manual.parse_timestamp(entry["measured_at"]) + dt.timedelta(seconds=manual.MAX_TTL_S + 1)
    entry["expires_at"] = too_late.isoformat()
    path.write_text(json.dumps(store))
    with pytest.raises(manual.ManualError, match="expiry 불일치"):
        manual.load_store()


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
    expires_on = TODAY + dt.timedelta(days=1)
    entry = _record_credit(expires_on=expires_on.isoformat())
    expiry_start = dt.datetime.combine(expires_on, dt.time.min, tzinfo=dt.UTC)
    assert manual.parse_timestamp(entry["expires_at"]) == expiry_start
    resolution = manual.resolve_result(
        manual.manual_only_result("anthropic-api"), now=expiry_start, store=manual.load_store()
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


@pytest.mark.parametrize(("profile", "rung"), PI_DEFAULT_RUNGS)
def test_gate_admits_a_placed_rung_without_the_marker(profile, rung):
    """hk 2185: a placed rung needs no E6 marker — the marker is inert for pi."""

    credit = _credit_result()
    result = gate_check([credit], profile, role="worker", effort=rung, today=TODAY, now=NOW)
    assert result.ok is True, result.reason
    assert result.provider_id == "anthropic-api"
    assert result.grade == CANON[(profile, rung)][0]
    assert result.e6_arm is None


@pytest.mark.parametrize("with_marker", [False, True])
@pytest.mark.parametrize("profile", sorted(PI_PROFILES))
def test_gate_refuses_the_bare_spelling_even_with_a_marker(profile, with_marker):
    """pi's thinking level is the effort: nothing but --effort names a rung."""

    credit = _credit_result()
    arm = _marker(profile, PI_DEFAULT_RUNG[profile]) if with_marker else None
    result = gate_check([credit], profile, role="worker", e6_arm=arm, today=TODAY, now=NOW)
    assert result.ok is False
    assert result.reason == recommend.credit_effort_required_reason(profile)
    assert result.reason.startswith("effort_required:")


def test_gate_refuses_an_unregistered_rung_whatever_the_marker_names():
    """The rung vocabulary is closed: an unregistered effort names no placement."""

    credit = _credit_result()
    for rung in ("bogus", "off", "default", "ultra"):
        result = gate_check(
            [credit],
            "pi-sonnet55",
            role="worker",
            effort=rung,
            e6_arm=f"pi-sonnet55@{rung}",
            today=TODAY,
            now=NOW,
        )
        assert result.ok is False, rung
        assert "not_placed" in result.reason


@pytest.mark.parametrize("role", ["worker", "tester"])
@pytest.mark.parametrize(("profile", "rung"), PI_DEFAULT_RUNGS)
def test_gate_admits_when_credit_is_fresh_and_positive(profile, rung, role):
    credit = _credit_result()
    result = gate_check([credit], profile, role=role, effort=rung, today=TODAY, now=NOW)
    assert result.ok is True, result.reason
    assert result.provider_id == "anthropic-api"
    assert result.grade == CANON[(profile, rung)][0]
    assert result.used_pct == pytest.approx(25.0)


def test_three_hour_old_credit_observation_still_admits_pi_sonnet55():
    """Mutant: reapply the 2h TTL to credit -> this admission goes RED."""

    credit = _credit_result(measured_at=NOW - dt.timedelta(hours=3))
    result = gate_check([credit], "pi-sonnet55", role="worker", effort="high", today=TODAY, now=NOW)
    assert result.ok is True, result.reason
    assert result.provider_id == "anthropic-api"


def test_twenty_five_hour_old_v1_credit_observation_fails_closed_as_unmeasurable():
    _record_credit(measured_at=NOW - dt.timedelta(hours=25), ttl_s=24 * 3600.0)
    path = manual.manual_path()
    store = json.loads(path.read_text())
    store["history"][-1]["credit_ttl_policy"] = "credit-24h-v1"
    path.write_text(json.dumps(store))
    resolution = manual.resolve_result(
        manual.manual_only_result("anthropic-api"), now=NOW, store=manual.load_store()
    )
    assert resolution.applied is False
    result = gate_check(
        [resolution.result], "pi-sonnet55", role="worker", effort="high", today=TODAY, now=NOW
    )
    assert result.ok is False
    assert result.unmeasurable is True


@pytest.mark.parametrize(("profile", "rung"), PI_ROWS)
def test_gate_refuses_the_builder_role(profile, rung):
    """Mutant: drop the builder refusal -> this goes RED."""

    credit = _credit_result()
    result = gate_check([credit], profile, role="builder", effort=rung, today=TODAY, now=NOW)
    assert result.ok is False
    assert result.role_denied is True
    assert "worker/tester" in result.reason


@pytest.mark.parametrize("role", [None, "", "captain", "reviewer", "orch"])
def test_gate_refuses_every_seat_other_than_worker_and_tester(role):
    """The brief says worker or tester only — an unnamed seat is not a worker."""

    credit = _credit_result()
    result = gate_check([credit], "pi-sonnet55", role=role, effort="high", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.role_denied is True


@pytest.mark.parametrize("profile", sorted(PI_PROFILES))
def test_gate_reason_names_anthropic_api_not_claude(profile):
    credit = _credit_result()
    result = gate_check([credit], profile, role="worker", effort="high", today=TODAY, now=NOW)
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
    rc = cli.main(["gate", "-m", "pi-sonnet55", "--effort", "high", "--role", "worker", "--no-cache"])
    captured = capsys.readouterr()
    assert rc == 4
    assert "manual observation 없음" in captured.err


def test_gate_cli_refuses_when_the_manual_value_is_stale(monkeypatch, capsys):
    stale_measured = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat()
    _record_credit(measured_at=manual.parse_timestamp(stale_measured), ttl_s=900.0)
    monkeypatch.setattr(cli, "registry", lambda: {"claude": _claude_result})
    rc = cli.main(["gate", "-m", "pi-sonnet55", "--effort", "high", "--role", "worker", "--no-cache"])
    captured = capsys.readouterr()
    assert rc == 4
    assert "credit" in captured.err


def test_gate_cli_admits_without_the_marker(monkeypatch, capsys):
    """hk 2185: the E6 marker is inert for a placed credit rung."""

    assert _set_manual_credit_cli() == 0
    capsys.readouterr()
    monkeypatch.setattr(cli, "registry", lambda: {"claude": _claude_result})
    monkeypatch.delenv(E6_ARM_MARKER_ENV, raising=False)
    rc = cli.main(["gate", "-m", "pi-sonnet55", "--effort", "high", "--role", "worker", "--no-cache"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "pool=anthropic-api" in captured.out
    # A bare spelling still names no rung.
    rc = cli.main(["gate", "-m", "pi-sonnet55", "--role", "worker", "--no-cache"])
    captured = capsys.readouterr()
    assert rc == 3
    assert "effort_required" in captured.err + captured.out


def test_gate_cli_allow_line_shows_pool_credit_and_days_left(monkeypatch, capsys, tmp_path):
    assert _set_manual_credit_cli() == 0
    capsys.readouterr()
    stored_entry = manual.load_store()["history"][-1]
    monkeypatch.setattr(cli, "registry", lambda: {"claude": _claude_result})
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
    assert f"valid_until={stored_entry['expires_at']}" in captured.out
    assert "(in " in captured.out
    record = json.loads(record_path.read_text())
    assert record["provider_id"] == "anthropic-api"
    assert record.get("e6_arm") is None
    (observation,) = record["manual_observations"]
    assert observation["credit"]["remaining_usd"] == 150.0


# ---------------------------------------------------------------------------
# 5. launch resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("profile", "rung"), PI_DEFAULT_RUNGS)
def test_launch_resolves_the_rung_with_the_anthropic_model_id(profile, rung):
    decision = launch.resolve_launch(profile, effort=rung, role="worker", view=_snapshot_view())
    assert decision.model_id == PI_PROFILES[profile]
    assert decision.pool == "anthropic-api"
    assert decision.family == "claude"
    assert decision.effort == rung
    assert decision.grade == CANON[(profile, rung)][0]
    assert decision.e6_arm is None


@pytest.mark.parametrize(("profile", "rung"), PI_DEFAULT_RUNGS)
def test_launch_resolves_from_the_bundled_row_when_the_served_canon_is_silent(profile, rung):
    """A served canon not yet seeded with the pi rows must not hide the placement."""

    view = launch.CatalogView(
        entries=tuple(entry for entry in bench.catalog_snapshot() if entry.profile == "sonnet"),
        source=bench.CATALOG_SOURCE_CACHE,
        age_s=0.0,
    )
    decision = launch.resolve_launch(profile, effort=rung, role="worker", view=view)
    assert (decision.model_id, decision.pool, decision.effort) == (
        PI_PROFILES[profile],
        "anthropic-api",
        rung,
    )
    assert decision.grade == CANON[(profile, rung)][0]


def test_launch_refuses_the_bare_spelling_and_the_builder_seat():
    view = _snapshot_view()
    with pytest.raises(launch.LaunchError, match="effort_required"):
        launch.resolve_launch("pi-sonnet55", role="worker", view=view)
    with pytest.raises(launch.LaunchError, match="role_restricted"):
        launch.resolve_launch("pi-sonnet55", effort="high", role="builder", view=view)


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


def test_credit_rows_are_listed_only_at_their_placed_grade():
    """hk 2185: each rung is listed at the grade it inherits, never another."""

    credit = _credit_result()
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        out = recommend_text([_claude_result(), credit], grade, today=TODAY, now=NOW)
        listed = set(_ranked_names(out)) & set(PI_PROFILES)
        expected = {p for p, r in PI_ROWS if CANON[(p, r)] == (grade, "default")}
        assert listed == expected, (grade, listed, expected)


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
def test_gate_cli_ignores_a_marker_that_names_an_unregistered_rung(monkeypatch, capsys, marker, role):
    """hk 2185: the marker is inert for pi; only --effort names a rung."""

    assert _set_manual_credit_cli() == 0
    capsys.readouterr()
    monkeypatch.setattr(cli, "registry", lambda: {})
    monkeypatch.setenv(E6_ARM_MARKER_ENV, marker)
    # A bare spelling still names no rung.
    argv = ["gate", "-m", "pi-sonnet55", "--no-cache"]
    if role is not None:
        argv += ["--role", role]
    rc = cli.main(argv)
    captured = capsys.readouterr()
    assert rc == 3, captured.out + captured.err
    assert "effort_required" in captured.out + captured.err
    # --effort names a registered rung, so the stale marker changes nothing.
    argv = ["gate", "-m", "pi-sonnet55", "--effort", "high", "--no-cache"]
    if role is not None:
        argv += ["--role", role]
    rc = cli.main(argv)
    captured = capsys.readouterr()
    assert rc == (0 if role == "worker" else 5), captured.out + captured.err


@pytest.mark.parametrize("rung", ["bogus", "ultra", "off", "default"])
@pytest.mark.parametrize("role", [None, "builder", "worker", "tester"])
def test_gate_refuses_every_unregistered_rung_for_every_seat(rung, role):
    credit = _credit_result()
    result = gate_check(
        [credit],
        "pi-sonnet55",
        role=role,
        effort=rung,
        e6_arm=f"pi-sonnet55@{rung}",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert "not_placed" in result.reason


@pytest.mark.parametrize("marker", ["pi-sonnet55@bogus", "pi-sonnet55@off", "sonnet@high"])
def test_a_marker_naming_another_rung_or_profile_is_inert(marker):
    """hk 2185: --effort names the rung; a stale marker opens and closes nothing."""

    credit = _credit_result()
    result = gate_check(
        [credit],
        "pi-sonnet55",
        role="worker",
        effort="high",
        e6_arm=marker,
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True, result.reason
    assert result.e6_arm is None


@pytest.mark.parametrize("role", [None, "builder", "worker"])
def test_launch_admits_a_consult_only_credit_row_only_for_worker_and_tester(role):
    """F2: a consult_only gate on a credit row keeps the request and seat rules."""

    if role == "worker":
        decision = launch.resolve_launch(
            "pi-sonnet55",
            effort="high",
            role=role,
            operator_request="hk:task/2079",
            view=_consult_only_view(),
        )
        assert decision.gate == "consult_only"
        return
    with pytest.raises(launch.LaunchError, match="role_restricted"):
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
def test_gate_cli_keeps_the_request_and_seat_rules_on_a_consult_only_credit_row(
    monkeypatch, capsys, role
):
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
    # worker: admitted on the request; builder/none: the seat rule refuses.
    assert rc == (0 if role == "worker" else 5), captured.out + captured.err


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
    """F3: the canon's consult_only exclusion stays — a placement is not an offer.

    hk 2185: pi-haiku55@high is a B default placement; a canon consult_only gate
    on it removes it from the B candidates while the other B rows stay.
    """

    view = _consult_only_view(profile="pi-haiku55", rung="high")
    table = bench._catalog_grade_table(view)
    assert table is not None
    credit = _credit_result()
    payload = recommend.recommend_dict([credit], "B", today=TODAY, now=NOW, grade_table=table)
    assert not [
        r
        for r in payload["rows"]
        if r["profile"] == "pi-haiku55" and r["kind"] == "candidate" and r["launch"].endswith("high")
    ]
    assert any(
        r["profile"] == "pi-haiku55" and r["effort"] == "high" and "consult_only" in r["reason"]
        for r in payload["excluded"]
    )
    # The canon's own consult_only rungs (sonnet@medium, haiku@xhigh/max) never
    # become candidates either.
    assert not {
        r["profile"]
        for r in payload["rows"]
        if r["kind"] == "candidate" and r["profile"] in PI_PROFILES
    } - {"pi-haiku55"}


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
    window = "credit" if pool == "commandcode" else "7d"
    rc = cli.main(
        [
            "manual",
            "set",
            "--pool",
            pool,
            "--window",
            window,
            "--measured-at",
            "now",
            "--reason",
            "missing used",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    if pool == "commandcode":
        assert "--remaining" in captured.err
        assert "--expires-on" in captured.err
    else:
        assert "--used" in captured.err


# ---------------------------------------------------------------------------
# 8. tester round 2 findings, kept as regressions
# ---------------------------------------------------------------------------


def _canon_with_pi_row_at(effort: str, *, keep_registered: bool):
    """A served canon whose pi-sonnet55@high carrier row also appears under another key."""

    entries = list(bench.catalog_snapshot())
    high = next(entry for entry in entries if entry.key == ("pi-sonnet55", "high"))
    if not keep_registered:
        entries.remove(high)
    entries.append(replace(high, effort=effort))
    entries = bench._catalog_from_payload({"catalog": [entry.as_dict() for entry in entries]})
    assert bench._server_catalog_rejection(entries) is None
    view = launch.CatalogView(tuple(entries), source=bench.CATALOG_SOURCE_CACHE, age_s=0)
    table = bench._catalog_grade_table(view)
    assert table is not None
    return view, table


def _serve_canon(monkeypatch, view, table):
    monkeypatch.setattr(cli, "registry", lambda: {})
    monkeypatch.setattr(bench, "read_catalog", lambda **kwargs: view)
    monkeypatch.setattr(launch, "read_catalog", lambda **kwargs: view)
    monkeypatch.setattr(bench, "runtime_grade_table", lambda **kwargs: table)


@pytest.mark.parametrize("keep_registered", [True, False])
@pytest.mark.parametrize("effort", ["", "bogus", "off", "default"])
@pytest.mark.parametrize("command", ["gate", "launch"])
@pytest.mark.parametrize("role", [None, "builder", "worker"])
def test_a_carrier_row_under_another_key_is_not_a_placement(
    monkeypatch, capsys, command, role, effort, keep_registered
):
    """F5: an effort-less or unregistered canon key for a credit profile is still unmeasured."""

    _record_credit()
    view, table = _canon_with_pi_row_at(effort, keep_registered=keep_registered)
    _serve_canon(monkeypatch, view, table)
    monkeypatch.delenv(E6_ARM_MARKER_ENV, raising=False)
    argv = (
        ["gate", "-m", "pi-sonnet55", "--no-cache"]
        if command == "gate"
        else ["policy", "launch", "pi-sonnet55"]
    )
    if role is not None:
        argv += ["--role", role]
    rc = cli.main(argv)
    captured = capsys.readouterr()
    assert rc != 0, captured.out + captured.err
    if command == "gate":
        assert rc in {3, 5}


@pytest.mark.parametrize("effort", ["", "bogus"])
def test_a_carrier_row_under_another_key_does_not_hide_the_registered_rung(monkeypatch, capsys, effort):
    """The registered rung still opens through its marker for the right seat."""

    _record_credit()
    view, table = _canon_with_pi_row_at(effort, keep_registered=True)
    _serve_canon(monkeypatch, view, table)
    monkeypatch.setenv(E6_ARM_MARKER_ENV, "pi-sonnet55@high")
    for role, expected in (("worker", 0), ("tester", 0), ("builder", 5), (None, 5)):
        argv = ["gate", "-m", "pi-sonnet55", "--effort", "high", "--no-cache"]
        if role is not None:
            argv += ["--role", role]
        assert cli.main(argv) == expected, role
        capsys.readouterr()


@pytest.mark.parametrize("keep_registered", [True, False])
@pytest.mark.parametrize("effort", ["", "bogus", "off", "default"])
def test_recommend_lists_the_placed_rungs_whatever_other_keys_the_canon_carries(effort, keep_registered):
    """hk 2185: only placed rungs list, once each; an extra canon key adds nothing.

    At C the pi-sonnet55 rungs are low (consult_only) and medium (B) — neither is
    a C candidate, so the C surface carries no pi-sonnet55 row whatever the canon
    says about the extra key.
    """

    _, table = _canon_with_pi_row_at(effort, keep_registered=keep_registered)
    payload = recommend.recommend_dict([_credit_result()], "C", today=TODAY, now=NOW, grade_table=table)
    launches = [
        r["launch"] for r in payload["rows"] if r["kind"] == "candidate" and r["profile"] == "pi-sonnet55"
    ]
    assert launches == []


@pytest.mark.parametrize("gate", ["escalation", "consult_only"])
def test_recommend_keeps_a_non_default_canon_gate_on_the_credit_row(gate):
    """F6: a canon gate on a placed credit rung is honoured, never overridden."""

    entries = tuple(
        replace(entry, gate=gate) if entry.key == ("pi-haiku55", "medium") else entry
        for entry in bench.catalog_snapshot()
    )
    view = launch.CatalogView(entries, source=bench.CATALOG_SOURCE_CACHE, age_s=0)
    table = bench._catalog_grade_table(view)
    assert table is not None
    credit = _credit_result()
    payload = recommend.recommend_dict([credit], "C", today=TODAY, now=NOW, grade_table=table)
    # The gated rung never becomes a candidate.
    assert not [
        r
        for r in payload["rows"]
        if r["profile"] == "pi-haiku55" and r["kind"] == "candidate" and r["launch"].endswith("medium")
    ]
    if gate == "escalation":
        assert any(
            r["kind"] == "escalation" and r["profile"] == "pi-haiku55" for r in payload["rows"]
        )
    else:
        assert any(
            r["profile"] == "pi-haiku55" and r["effort"] == "medium" and "consult_only" in r["reason"]
            for r in payload["excluded"]
        )
    text = recommend_text([credit], "C", today=TODAY, now=NOW, grade_table=table)
    assert "pi-haiku55 --effort medium " not in "\n".join(
        line for line in text.splitlines() if line[:1].isdigit()
    )


# ---------------------------------------------------------------------------
# 9. tester round 3 findings, kept as regressions
# ---------------------------------------------------------------------------


def _canon_with_measured_siblings(extra_effort: str):
    """high/xhigh/max placed at B by the canon, low/medium still carriers, plus one unmeasured extra key."""

    base = list(bench.catalog_snapshot())
    carrier_high = next(entry for entry in base if entry.key == ("pi-sonnet55", "high"))
    entries = [
        replace(entry, grade="B")
        if entry.profile == "pi-sonnet55" and entry.effort in {"high", "xhigh", "max"}
        else entry
        for entry in base
    ]
    entries.append(replace(carrier_high, effort=extra_effort))
    entries = bench._catalog_from_payload({"catalog": [entry.as_dict() for entry in entries]})
    assert bench._server_catalog_rejection(entries) is None
    view = launch.CatalogView(tuple(entries), source=bench.CATALOG_SOURCE_CACHE, age_s=0)
    table = bench._catalog_grade_table(view)
    assert table is not None
    return view, table


@pytest.mark.parametrize("extra", ["off", "bogus", "default"])
@pytest.mark.parametrize("role", [None, "builder", "worker"])
def test_an_unknown_rung_never_borrows_a_sibling_placement_in_the_gate(extra, role):
    """F8: an unknown rung names no row — a placed sibling cannot stand in for it."""

    _, table = _canon_with_measured_siblings(extra)
    credit = _credit_result()
    for marker in (f"pi-sonnet55@{extra}", None):
        result = gate_check(
            [credit],
            "pi-sonnet55",
            role=role,
            effort=extra,
            e6_arm=marker,
            grade_table=table,
            today=TODAY,
            now=NOW,
        )
        assert result.ok is False, result.reason
        assert "not_placed" in result.reason


@pytest.mark.parametrize("extra", ["off", "bogus", "default"])
@pytest.mark.parametrize("role", [None, "builder", "worker"])
def test_an_unknown_rung_never_borrows_a_sibling_placement_in_launch(extra, role):
    view, _ = _canon_with_measured_siblings(extra)
    with pytest.raises(launch.LaunchError, match="not placed at effort"):
        launch.resolve_launch("pi-sonnet55", effort=extra, role=role, view=view)


def test_a_canon_placed_credit_rung_is_an_ordinary_placement():
    """A canon move is an ordinary re-placement; the worker/tester seat rule stays."""

    view, table = _canon_with_measured_siblings("off")
    decision = launch.resolve_launch("pi-sonnet55", effort="high", role="worker", view=view)
    assert (decision.effort, decision.grade) == ("high", "B")
    credit = _credit_result()
    result = gate_check(
        [credit], "pi-sonnet55", role="worker", effort="high", grade_table=table, today=TODAY, now=NOW
    )
    assert result.ok is True, result.reason
    assert result.grade == "B"
    # The placement is still worker/tester only — a placement is not a measurement.
    refused = gate_check(
        [credit], "pi-sonnet55", role="builder", effort="high", grade_table=table, today=TODAY, now=NOW
    )
    assert refused.ok is False and refused.role_denied is True


def _area_prefer(profile: str, effort: str, area: str, model_id: str):
    return bench._area_exception_from_wire(
        {
            "profile": profile,
            "effort": effort,
            "area": area,
            "action": "prefer",
            "evidence_refs": ["hk:task/1945"],
            "decision_ref": "hk:doc/decision/2026-10-25/area-example",
            "decided_by": "operator-desk",
            "decided_at": "2026-10-25T00:00:00Z",
            "expires_with_model_id": model_id,
            "retired_at": None,
        }
    )


def test_an_area_preference_never_lifts_a_credit_row_above_a_subscription_row():
    """F9: the credit is a parallel option on the final ranking too."""

    credit = _credit_result()
    exception = _area_prefer("pi-haiku55", "medium", "python", "anthropic/claude-haiku-5-5")
    kwargs = dict(
        today=TODAY,
        now=NOW,
        bench_scores=[],
        model_prices={},
        area="python",
        area_exceptions=[exception],
        catalog_model_ids={("pi-haiku55", "medium"): "anthropic/claude-haiku-5-5"},
    )
    payload = recommend.recommend_dict([_claude_result(), credit], "C", **kwargs)
    names = [r["profile"] for r in payload["rows"] if r["kind"] == "candidate"]
    credit_idx = [i for i, name in enumerate(names) if name in PI_PROFILES]
    other_idx = [i for i, name in enumerate(names) if name not in PI_PROFILES]
    assert credit_idx and other_idx, names
    assert max(other_idx) < min(credit_idx), names
    # The preference still orders the credit rows among themselves.
    assert names[credit_idx[0]] == "pi-haiku55", names
    text = recommend_text([_claude_result(), credit], "C", **kwargs)
    ranked = _ranked_names(text)
    assert [i for i, n in enumerate(ranked) if n in PI_PROFILES][0] > max(
        i for i, n in enumerate(ranked) if n not in PI_PROFILES
    ), ranked


def test_launch_reports_the_explicit_pool_whatever_the_canon_row_says():
    """F10: the canon pool field cannot move a credit profile onto the claude pool."""

    entries = tuple(
        replace(entry, pool="claude") if entry.profile == "pi-sonnet55" else entry
        for entry in bench.catalog_snapshot()
    )
    view = launch.CatalogView(entries, source=bench.CATALOG_SOURCE_CACHE, age_s=0)
    decision = launch.resolve_launch(
        "pi-sonnet55", effort="high", role="worker", e6_arm="pi-sonnet55@high", view=view
    )
    assert decision.pool == "anthropic-api"


def test_a_hand_edited_credit_window_on_another_pool_is_rejected_on_load():
    """F11: the reader enforces what normalize_window enforces for the writer."""

    _record_credit()
    path = manual.manual_path()
    store = json.loads(path.read_text())
    entry = store["history"][-1]
    entry["pool"] = "claude"
    entry["account_ref"]["pool"] = "claude"
    store["latest"] = {
        key.replace("anthropic-api", "claude"): value for key, value in store["latest"].items()
    }
    path.write_text(json.dumps(store))
    with pytest.raises(manual.ManualError):
        manual.load_store()


# ---------------------------------------------------------------------------
# 10. tester round 4 finding, kept as a regression
# ---------------------------------------------------------------------------


def _mixed_canon_view(*, measured=("low", "medium", "xhigh", "max")) -> launch.CatalogView:
    """high stays the unmeasured carrier, the other registered rungs sit at B, plus an unmeasured off key."""

    rows = [
        replace(row, grade="B") if row.profile == "pi-sonnet55" and row.effort in measured else row
        for row in bench.catalog_snapshot()
    ]
    sonnet = next(row for row in rows if row.profile == "pi-sonnet55")
    rows.append(
        bench.CatalogEntry(
            profile="pi-sonnet55",
            effort="off",
            model_id=sonnet.model_id,
            pool="anthropic-api",
            grade="C",
        )
    )
    return launch.CatalogView(entries=tuple(rows), source="server")


@pytest.mark.parametrize("role", [None, "builder", "worker"])
def test_a_bare_launch_is_refused_in_the_round_4_mixed_canon(monkeypatch, capsys, role):
    """Round 4: no sibling row stands in for a bare pi spelling, in launch or in the gate."""

    view = _mixed_canon_view()
    monkeypatch.setattr(launch, "read_catalog", lambda path=None: view)
    monkeypatch.delenv(E6_ARM_MARKER_ENV, raising=False)
    argv = ["policy", "launch", "pi-sonnet55"]
    if role is not None:
        argv += ["--role", role]
    rc = cli.main(argv)
    captured = capsys.readouterr()
    assert rc == 3, captured.out + captured.err
    assert recommend.credit_effort_required_reason("pi-sonnet55") in captured.err + captured.out
    result = gate_check(
        [_credit_result()],
        "pi-sonnet55",
        role=role,
        grade_table=bench._catalog_grade_table(view),
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert result.reason == recommend.credit_effort_required_reason("pi-sonnet55")


@pytest.mark.parametrize(("profile", "rung"), PI_DEFAULT_RUNGS)
def test_an_explicit_effort_selects_exactly_that_rung(profile, rung):
    """--effort names the rung in launch and in the gate; a marker adds nothing."""

    view = _snapshot_view()
    decision = launch.resolve_launch(profile, effort=rung, role="worker", view=view)
    assert (decision.effort, decision.e6_arm) == (rung, None)
    credit = _credit_result()
    admitted = gate_check([credit], profile, role="worker", effort=rung, today=TODAY, now=NOW)
    assert admitted.ok is True, admitted.reason
    # A marker naming another rung is inert — --effort still selects the rung.
    other = next(candidate for candidate in PI_RUNGS if candidate != rung)
    still = gate_check(
        [credit], profile, role="worker", effort=rung, e6_arm=_marker(profile, other), today=TODAY, now=NOW
    )
    assert still.ok is True, still.reason
    # A marker alone names no rung.
    with pytest.raises(launch.LaunchError, match="effort_required"):
        launch.resolve_launch(profile, role="worker", e6_arm=_marker(profile, rung), view=view)


@pytest.mark.parametrize("profile", sorted(PI_PROFILES))
def test_gate_and_launch_print_the_same_line_for_a_bare_spelling(profile):
    gate_line = gate_check([_credit_result()], profile, role="worker", today=TODAY, now=NOW).reason
    with pytest.raises(launch.LaunchError) as error:
        launch.resolve_launch(profile, role="worker", view=_snapshot_view())
    assert str(error.value) == gate_line == recommend.credit_effort_required_reason(profile)


def test_the_credit_rows_are_accounted_once_each_on_the_c_surface():
    """Every C-placed pi rung is accounted for exactly once on the C surface."""

    c_rows = sorted((p, r) for p, r in PI_ROWS if _placed_grade(p, r) == "C")
    c_candidates = {(p, r) for p, r in c_rows if _placed_gate(p, r) == "default"}
    for credit_state in ("missing", "fresh"):
        providers_ = [_claude_result()]
        if credit_state == "fresh":
            providers_.append(_credit_result())
        payload = recommend.recommend_dict(
            providers_, "C", today=TODAY, now=NOW, bench_scores=[], model_prices={}
        )
        text = recommend_text(providers_, "C", today=TODAY, now=NOW, bench_scores=[], model_prices={})
        keys = [
            (r["profile"], r["launch"].rsplit(" ", 1)[-1] if " --effort " in r["launch"] else r["effort"])
            for r in payload["rows"]
            if r["kind"] == "candidate" and r["profile"] in PI_PROFILES
        ] + [(r["profile"], r["effort"]) for r in payload["excluded"] if r["profile"] in PI_PROFILES]
        assert sorted(keys) == c_rows, (credit_state, keys)
        fold = next(line for line in text.splitlines() if line.startswith("✗ 제외"))
        assert int(fold.split("(", 1)[1].split(")", 1)[0]) == len(payload["excluded"])
        offered = {
            r["profile"] for r in payload["rows"] if r["kind"] == "candidate" and r["profile"] in PI_PROFILES
        }
        assert offered == ({p for p, _r in c_candidates} if credit_state == "fresh" else set())


# ---------------------------------------------------------------------------
# 11. the spend-pool pace gate (main #196) and the credit pool
# ---------------------------------------------------------------------------


def test_the_credit_pool_is_pace_unknown_and_needs_no_pace_override():
    """The credit window has no length or reset time, so the pace gate cannot judge it.

    It is a parallel, expiry-driven balance (r1026prio): even a heavily used one is
    admitted on its own cutoff, never refused as pace_exhaustion.
    """

    credit = _credit_result(remaining=20.0)
    result = gate_check(
        [credit],
        "pi-sonnet55",
        role="worker",
        effort="high",
        e6_arm="pi-sonnet55@high",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True, result.reason
    assert result.pace_override_ref is None
    assert "pace-unknown window=credit" in result.reason
    assert "pace_exhaustion" not in result.reason
