from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from scopefuel import cli, recommend
from scopefuel.model import Bucket, ProviderResult, Scope

NOW = dt.datetime(2026, 10, 26, 12, 0, tzinfo=dt.UTC)
TODAY = NOW.date()


def _bucket(window: str, used: float, reset_in_hours: float | None, *, scope: Scope | None = None) -> Bucket:
    reset = None if reset_in_hours is None else (NOW + dt.timedelta(hours=reset_in_hours)).isoformat()
    return Bucket(
        label=window,
        window=window,
        used_pct=used,
        resets_at=reset,
        scope=scope or Scope("account"),
        horizon="now" if window == "5h" else "week",
    )


def _provider(pool: str, *buckets: Bucket, pool_class: str = "spend") -> ProviderResult:
    return ProviderResult(id=pool, pool_class=pool_class, buckets=list(buckets))  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def isolate_gate_side_effects(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(recommend, "_observe_exhaustion", lambda *args, **kwargs: None)
    monkeypatch.setattr(recommend, "_inflight_gate", lambda *args, **kwargs: None)


def test_weekly_pace_exhaustion_refuses_and_reports_all_required_fields(monkeypatch):
    monkeypatch.setattr(
        recommend,
        "_inflight_gate",
        lambda *args, **kwargs: SimpleNamespace(
            n=2, tag="inflight=2", warning=None, status=None, reserves={}, unknown=False
        ),
    )
    result = recommend.gate_check(
        [_provider("claude", _bucket("7d", 50, 144))], "sonnet", today=TODAY, now=NOW
    )

    assert result.ok is False
    assert result.reason.startswith("pace_exhaustion")
    for field in (
        "pool=claude",
        "window=7d",
        "remaining=50%",
        "hours_to_reset=144h",
        "time_to_exhaust=24h",
        "allowed_daily_budget=8.33%/day",
        "inflight=2",
    ):
        assert field in result.reason


def test_weekly_pace_with_headroom_is_admitted():
    result = recommend.gate_check(
        [_provider("claude", _bucket("7d", 20, 120))], "sonnet", today=TODAY, now=NOW
    )
    assert result.ok is True
    assert "pace=within-limit" in result.reason


def test_any_window_over_pace_refuses_even_when_weekly_is_on_pace():
    result = recommend.gate_check(
        [
            _provider(
                "grok",
                _bucket("5h", 50, 4),
                _bucket("7d", 20, 120),
            )
        ],
        "grok-hi",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert "pace_exhaustion" in result.reason
    assert "window=5h" in result.reason


def test_non_spend_pool_and_swe2_free_bucket_are_exempt():
    preserve = recommend.gate_check(
        [_provider("codex", _bucket("7d", 80, 144), pool_class="preserve")],
        "codex-luna",
        today=TODAY,
        now=NOW,
    )
    free_bucket = _bucket("7d", 80, 144, scope=Scope("model", "swe-2-high"))
    free = recommend.gate_check([_provider("devin", free_bucket)], "devin-swe2", today=TODAY, now=NOW)
    assert preserve.ok is True
    assert free.ok is True
    assert "pace_exhaustion" not in free.reason


def test_pace_override_requires_valid_ref_and_records_valid_ref():
    provider = [_provider("claude", _bucket("7d", 50, 144))]
    admitted = recommend.gate_check(
        provider,
        "sonnet",
        today=TODAY,
        now=NOW,
        pace_override_ref="hk:task/625",
    )
    malformed = recommend.gate_check(
        provider,
        "sonnet",
        today=TODAY,
        now=NOW,
        pace_override_ref="please-override",
    )
    assert admitted.ok is True
    assert admitted.pace_override_ref == "hk:task/625"
    assert "pace_exhaustion overridden" in admitted.reason
    assert malformed.ok is False
    assert "pace_override_ref_invalid" in malformed.reason


def test_pace_override_does_not_satisfy_existing_consult_only_rung_rule():
    result = recommend.gate_check(
        [],
        "fable",
        today=TODAY,
        now=NOW,
        pace_override_ref="hk:task/625",
    )
    assert result.ok is False
    assert "--operator-request" in result.reason


def test_consult_only_spend_pool_uses_pace_gate_and_keeps_both_refs():
    provider = [_provider("claude", _bucket("7d", 50, 144))]
    refused = recommend.gate_check(
        provider,
        "fable",
        today=TODAY,
        now=NOW,
        operator_request="hk:task/1",
    )
    overridden = recommend.gate_check(
        provider,
        "fable",
        today=TODAY,
        now=NOW,
        operator_request="hk:task/1",
        pace_override_ref="hk:task/2",
    )

    assert refused.ok is False
    assert refused.reason.startswith("pace_exhaustion pool=claude")
    assert overridden.ok is True
    assert "pace_exhaustion overridden" in overridden.reason
    assert overridden.operator_request_ref == "hk:task/1"
    assert overridden.pace_override_ref == "hk:task/2"


def test_cli_option_and_gate_record_carry_the_pace_override_ref(monkeypatch, tmp_path, capsys):
    provider = _provider("claude", _bucket("7d", 50, 144))

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(cli, "dt", SimpleNamespace(datetime=FrozenDateTime, UTC=dt.UTC))
    monkeypatch.setattr(cli, "registry", lambda: {"claude": object()})
    monkeypatch.setattr(cli, "collect", lambda *args, **kwargs: [provider])
    monkeypatch.setattr(cli.bench, "read_scores", lambda: [])
    monkeypatch.setattr(cli.bench, "read_prices", lambda: {})
    monkeypatch.setattr(cli.bench, "runtime_grade_table", lambda: recommend.GRADE_TABLE)
    monkeypatch.setattr(cli, "_retired_rungs", lambda: frozenset())
    monkeypatch.setattr(cli, "_consult_only_rungs", lambda: frozenset())
    monkeypatch.setattr(cli, "_gate_policy_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_shadow_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_emit_stale_build_warning", lambda: None)
    record_path = tmp_path / "gate.json"

    rc = cli.main(
        [
            "gate",
            "-m",
            "sonnet",
            "--pace-override",
            "hk:task/625",
            "--gate-output",
            str(record_path),
            "--no-cache",
        ]
    )
    record = json.loads(record_path.read_text())
    assert rc == 0
    assert "pace_override_ref=hk:task/625" in capsys.readouterr().out
    assert record["pace_override_ref"] == "hk:task/625"
    assert record["operator_request_ref"] is None


def test_gate_help_states_pace_rule_and_exemptions(capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(["gate", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "--pace-override REF" in help_text
    assert "첫 5%" in help_text
    assert "non-spend" in help_text
    assert "SWE-2 free" in help_text


@pytest.mark.parametrize(
    ("used", "reset_in_hours", "elapsed_fraction"),
    [(50, None, None), (50, 167.16, 0.01)],
)
def test_uncomputable_or_early_window_is_admitted_and_named_pace_unknown(
    used, reset_in_hours, elapsed_fraction
):
    if elapsed_fraction is not None:
        reset_in_hours = 168 * (1 - elapsed_fraction)
    result = recommend.gate_check(
        [_provider("claude", _bucket("7d", used, reset_in_hours))],
        "sonnet",
        today=TODAY,
        now=NOW,
    )
    assert recommend.PACE_EARLY_WINDOW_FRACTION == 0.05
    assert result.ok is True
    assert "pace-unknown window=7d" in result.reason


def test_operator_snapshot_claude_is_refused_and_codex_is_admitted():
    claude = recommend.gate_check(
        [_provider("claude", _bucket("7d", 69, 127.2))], "sonnet", today=TODAY, now=NOW
    )
    codex = recommend.gate_check(
        [_provider("codex", _bucket("7d", 4, 127.2), pool_class="preserve")],
        "codex-luna",
        today=TODAY,
        now=NOW,
    )
    assert claude.ok is False
    assert "pace_exhaustion" in claude.reason
    assert codex.ok is True


def test_manual_window_observation_uses_the_same_pace_admission():
    manual = _provider("claude", _bucket("7d", 50, 144))
    manual.source = "manual"
    result = recommend.gate_check([manual], "sonnet", today=TODAY, now=NOW)
    assert result.ok is False
    assert "pace_exhaustion" in result.reason


def test_recommend_marks_any_window_refusal_and_never_scores_it_as_waste():
    profiles = {profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows}
    table = {grade: [] for grade in recommend.GRADE_TABLE}
    table["A+"] = [profiles["grok-hi"], profiles["sonnet"]]
    providers = [
        _provider("grok", _bucket("5h", 50, 4), _bucket("7d", 20, 120)),
        _provider("claude", _bucket("7d", 20, 120), pool_class="preserve"),
    ]
    data = recommend.recommend_dict(providers, "A+", today=TODAY, now=NOW, grade_table=table)
    grok = next(row for row in data["rows"] if row["profile"] == "grok-hi")
    sonnet = next(row for row in data["rows"] if row["profile"] == "sonnet")
    assert grok["kind"] == "pace_exhaustion"
    assert "window=5h" in grok["reason"]
    assert "score" not in grok
    assert sonnet["kind"] == "candidate"


def test_recommend_marks_pace_unknown_in_candidate_windows():
    profiles = {profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows}
    table = {grade: [] for grade in recommend.GRADE_TABLE}
    table["A+"] = [profiles["grok-hi"]]
    result = recommend.recommend_dict(
        [_provider("grok", _bucket("7d", 20, None))],
        "A+",
        today=TODAY,
        now=NOW,
        grade_table=table,
    )
    row = next(item for item in result["rows"] if item["profile"] == "grok-hi")
    assert row["kind"] == "candidate"
    assert "pace-unknown window=7d" in row["windows"]


def test_recommend_unknown_pace_does_not_get_waste_boost():
    profiles = {profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows}
    table = {grade: [] for grade in recommend.GRADE_TABLE}
    table["A+"] = [profiles["grok-hi"]]
    evaluation = recommend._evaluate(
        [_provider("grok", _bucket("7d", 1, 160))],
        "A+",
        TODAY,
        NOW,
        urgency_hours=24,
        bench_scores=None,
        normalized_prices={},
        table=table,
    )

    assert len(evaluation.included) == 1
    candidate = evaluation.included[0]
    assert "pace-unknown window=7d" in candidate.windows_display
    assert candidate.waste_term == 0


def test_recommend_swe2_free_bucket_is_pace_exempt_not_excluded():
    """The gate's SWE-2 free-bucket exemption (hk 2087 rule 5) holds at recommend level too."""
    profiles = {profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows}
    table = {grade: [] for grade in recommend.GRADE_TABLE}
    table["A+"] = [profiles["devin-swe2"]]
    # This rung's bucket would exhaust long before reset if it were judged as
    # a spend-pool window: elapsed 24h, burn 3.33%/h, time_to_exhaust 6h < 144h.
    free_bucket = _bucket("7d", 80, 144, scope=Scope("model", "swe-2-high"))
    evaluation = recommend._evaluate(
        [_provider("devin", free_bucket)],
        "A+",
        TODAY,
        NOW,
        urgency_hours=24,
        bench_scores=None,
        normalized_prices={},
        table=table,
    )

    assert [candidate.profile.name for candidate in evaluation.included] == ["devin-swe2"]
    candidate = evaluation.included[0]
    assert "pace=exempt (SWE-2 free bucket)" in candidate.windows_display
    assert candidate.waste_term == 0
    assert not any(item.kind == "pace_exhaustion" for item in evaluation.excluded)


def test_recommend_waste_removal_subtracts_the_braked_share():
    """Removing WASTE subtracts SCORE_WASTE_WEIGHT*waste*brake, not the unbraked share."""
    profiles = {profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows}
    table = {grade: [] for grade in recommend.GRADE_TABLE}
    table["A+"] = [profiles["grok-hi"]]
    # The 5h window sits under the brake knee and reports no reset (pace
    # unknown) → the row is not pace_waste_eligible, so its WASTE share is
    # removed. The 7d budget window is on pace and carries a nonzero waste
    # term (90 remaining, burn 10/24%/h → waste 30).
    evaluation = recommend._evaluate(
        [_provider("grok", _bucket("5h", 80, None), _bucket("7d", 10, 144))],
        "A+",
        TODAY,
        NOW,
        urgency_hours=24,
        bench_scores=None,
        normalized_prices={},
        table=table,
    )

    assert len(evaluation.included) == 1
    candidate = evaluation.included[0]
    assert candidate.brake == pytest.approx(0.4)
    assert candidate.waste_term == 0
    expected = (
        candidate.capacity_term + recommend.SCORE_THRU_WEIGHT * candidate.throughput_term
    ) * candidate.brake
    assert candidate.score == pytest.approx(expected)


def test_recommend_waste_removal_is_unchanged_when_brake_is_one():
    """A spend row with no short window keeps today's exact score (brake 1)."""
    profiles = {profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows}
    table = {grade: [] for grade in recommend.GRADE_TABLE}
    table["A+"] = [profiles["grok-hi"]]
    # 7d at elapsed 8h is inside the first 5% of its window → pace unknown →
    # WASTE removal path; waste term is 79 and the brake is 1 (no ≤6h window).
    evaluation = recommend._evaluate(
        [_provider("grok", _bucket("7d", 1, 160))],
        "A+",
        TODAY,
        NOW,
        urgency_hours=24,
        bench_scores=None,
        normalized_prices={},
        table=table,
    )

    assert len(evaluation.included) == 1
    candidate = evaluation.included[0]
    assert candidate.brake == pytest.approx(1.0)
    assert candidate.waste_term == 0
    expected = (
        candidate.capacity_term + recommend.SCORE_THRU_WEIGHT * candidate.throughput_term
    ) * candidate.brake
    assert candidate.score == pytest.approx(expected)
