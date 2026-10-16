"""SWE-2 allowlist gates on the current Free model-scope bucket.

Fixtures only. No devin binary, no credentials, no live quota.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal

from scopefuel import cli
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.providers import devin
from scopefuel.recommend import (
    GRADE_TABLE,
    SWE2_FREE_TAG_OBSERVED,
    SWE2_MODEL_SCOPE_BASIS,
    SWE2_MODEL_SCOPE_PROFILES,
    gate_check,
    profile_pool,
)

NOW = dt.datetime(2026, 10, 16, 7, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
_RESET = "2026-10-20T00:00:00+00:00"
_POOL_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_SIX = frozenset(
    {
        "devin-swe2",
        "devin-swe2-medium",
        "devin-swe2-max",
        "builder-devin",
        "builder-devin-medium",
        "builder-devin-max",
    }
)


def _parsed_pool(line: str) -> str:
    """Same token loop as agent-skills bin/wrk quota_hub_policy_gate."""
    pool = ""
    for field in line.split():
        if field.startswith("pool="):
            pool = field.removeprefix("pool=")
            break
    assert _POOL_RE.fullmatch(pool), line
    return pool


def _account(label: str, window: str, used: float | None, horizon: Literal["now", "week", "month"]) -> Bucket:
    return Bucket(
        label=label,
        window=window,
        used_pct=used,
        resets_at=_RESET,
        scope=Scope("account"),
        horizon=horizon,
    )


def _swe2(used: float | None = 0.0) -> Bucket:
    return Bucket(
        label="swe-2",
        window="",
        used_pct=used,
        resets_at=None,
        scope=Scope("model", "swe-2"),
        horizon="now",
        note="Free tag observed in devin models list at 2026-10-16T07:00:00Z",
    )


def _probe(
    *,
    daily: float | None = 90.0,
    weekly: float | None = 10.0,
    swe2: float | None = 0.0,
    include_account: bool = True,
    include_swe2: bool = True,
    **overrides: object,
) -> ProviderResult:
    buckets: list[Bucket] = []
    if include_account:
        buckets.append(_account("daily", "1d", daily, "now"))
        buckets.append(_account("weekly", "7d", weekly, "week"))
    if include_swe2:
        buckets.append(_swe2(swe2))
    result = ProviderResult(id="devin", pool_class="spend", buckets=buckets)
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


def _gate(result: ProviderResult, profile: str):
    return gate_check([result], profile, today=TODAY, now=NOW)


def test_allowlist_is_exactly_the_six_profiles():
    assert SWE2_MODEL_SCOPE_PROFILES == _SIX
    assert "builder-ds41" not in SWE2_MODEL_SCOPE_PROFILES
    assert "builder-ds41-max" not in SWE2_MODEL_SCOPE_PROFILES
    assert "devin-ds41" not in SWE2_MODEL_SCOPE_PROFILES
    assert "devin-fusion-opus55" not in SWE2_MODEL_SCOPE_PROFILES


def test_free_tag_present_daily_90_allows_devin_swe2_on_model_scope():
    result = _gate(_probe(daily=90.0, weekly=12.0), "devin-swe2")
    assert result.ok is True
    assert result.profile == "devin-swe2"
    assert result.grade == "A+"
    assert result.used_pct == 0.0
    assert result.gate_basis == SWE2_MODEL_SCOPE_BASIS
    assert result.free_tag == SWE2_FREE_TAG_OBSERVED
    assert result.account_daily_pct == 90.0
    assert result.account_weekly_pct == 12.0
    assert "account_daily=90" in result.reason
    assert "account_weekly=12" in result.reason
    assert "basis=model-scope:swe-2" in result.reason.split()
    assert "free_tag=observed" in result.reason.split()


def test_free_tag_present_daily_90_allows_builder_devin_on_model_scope():
    result = _gate(_probe(daily=90.0), "builder-devin")
    assert result.ok is True
    assert result.profile == "builder-devin"
    assert result.provider_id == "devin"
    assert result.grade == "A+"
    assert result.used_pct == 0.0
    assert result.gate_basis == SWE2_MODEL_SCOPE_BASIS
    assert "account_daily=90" in result.reason


def test_free_tag_present_daily_90_allows_every_allowlisted_profile():
    expected_grade = {
        "devin-swe2": "A+",
        "devin-swe2-medium": "A",
        "devin-swe2-max": "A+",
        "builder-devin": "A+",
        "builder-devin-medium": "A",
        "builder-devin-max": "A+",
    }
    for name in sorted(_SIX):
        result = _gate(_probe(daily=90.0), name)
        assert result.ok is True, name
        assert result.profile == name
        assert result.used_pct == 0.0
        assert result.gate_basis == SWE2_MODEL_SCOPE_BASIS
        assert result.grade == expected_grade[name]


def test_free_tag_present_daily_99_still_allows_on_model_scope():
    """Daily 99 is already over the spend cutoff. The Free bucket must still allow."""
    result = _gate(_probe(daily=99.0, weekly=99.0), "devin-swe2")
    assert result.ok is True
    assert result.used_pct == 0.0
    assert result.gate_basis == SWE2_MODEL_SCOPE_BASIS
    assert result.account_daily_pct == 99.0
    assert result.account_weekly_pct == 99.0
    assert "account_daily=99" in result.reason
    assert "account_weekly=99" in result.reason
    assert "소진" not in result.reason


def test_free_tag_absent_daily_99_denies_on_account():
    result = _gate(_probe(daily=99.0, include_swe2=False), "devin-swe2")
    assert result.ok is False
    assert result.unmeasurable is False
    assert result.gate_basis is None
    assert result.used_pct == 99.0
    assert "model-scope" not in result.reason
    assert "소진" in result.reason


def test_models_list_failed_uses_account_gate():
    denied = _gate(_probe(daily=99.0, include_swe2=False), "devin-swe2")
    assert denied.ok is False
    assert denied.gate_basis is None
    assert denied.used_pct == 99.0
    allowed = _gate(_probe(daily=50.0, include_swe2=False), "devin-swe2")
    assert allowed.ok is True
    assert allowed.gate_basis is None
    assert allowed.used_pct == 50.0
    assert "basis=" not in allowed.reason


def test_models_list_timeout_uses_account_gate():
    """A failed probe stays unmeasurable even when a swe-2 bucket object is attached."""
    failed = _probe(daily=50.0, error="devin models list timed out")
    result = _gate(failed, "devin-swe2")
    assert result.ok is False
    assert result.unmeasurable is True
    assert result.gate_basis is None
    assert "basis=model-scope" not in result.reason


def test_stale_probe_with_free_bucket_uses_account_gate():
    stale = _probe(
        daily=99.0,
        weekly=10.0,
        swe2=0.0,
        stale=True,
        age_s=120.0,
        error_kind="transport",
        account_fp_match=True,
    )
    result = _gate(stale, "devin-swe2")
    assert result.ok is False
    assert result.gate_basis is None
    assert result.used_pct == 99.0
    assert "model-scope" not in result.reason
    assert "소진" in result.reason


def test_aged_past_freshness_with_free_bucket_uses_account_gate():
    aged = _probe(daily=99.0, age_s=120.0)
    denied = _gate(aged, "devin-swe2")
    assert denied.ok is False
    assert denied.gate_basis is None
    assert denied.used_pct == 99.0

    from_stamp = _probe(daily=99.0, fetched_at=NOW.timestamp() - 1000.0)
    denied_stamp = _gate(from_stamp, "devin-swe2")
    assert denied_stamp.ok is False
    assert denied_stamp.gate_basis is None
    assert denied_stamp.used_pct == 99.0

    fresh = _gate(_probe(daily=99.0, age_s=10.0), "devin-swe2")
    assert fresh.ok is True
    assert fresh.gate_basis == SWE2_MODEL_SCOPE_BASIS
    assert fresh.used_pct == 0.0

    live = _gate(_probe(daily=99.0), "devin-swe2")
    assert live.ok is True
    assert live.gate_basis == SWE2_MODEL_SCOPE_BASIS
    assert live.account_daily_pct == 99.0


def test_warning_probe_with_free_bucket_stays_unmeasurable():
    warned = _probe(daily=None, weekly=None, warning="quota probe failed")
    result = _gate(warned, "devin-swe2")
    assert result.ok is False
    assert result.unmeasurable is True
    assert result.gate_basis is None


def test_invalid_or_ambiguous_swe2_bucket_uses_account_gate():
    invalid = _gate(_probe(daily=99.0, swe2=None), "devin-swe2")
    assert invalid.ok is False
    assert invalid.gate_basis is None
    assert invalid.used_pct == 99.0
    doubled = _probe(daily=99.0)
    doubled.buckets.append(_swe2(0.0))
    ambiguous = _gate(doubled, "devin-swe2")
    assert ambiguous.ok is False
    assert ambiguous.gate_basis is None
    assert ambiguous.used_pct == 99.0


def test_fusion_opus55_with_free_bucket_daily_99_denied_on_account():
    result = _gate(_probe(daily=99.0), "devin-fusion-opus55")
    assert result.ok is False
    assert result.profile == "devin-fusion-opus55"
    assert result.gate_basis is None
    assert result.used_pct == 99.0
    assert "model-scope" not in result.reason
    assert "소진" in result.reason


def test_non_swe2_devin_profiles_never_use_model_scope_bucket():
    """Invariant: a profile outside the allowlist is never judged on the swe-2 bucket.

    billing=free is not membership. glm52 and swe17 carry that static field and
    must still deny on the account when Daily is 99.
    """
    devin_names = {
        profile.name
        for profiles in GRADE_TABLE.values()
        for profile in profiles
        if profile_pool(profile.name)[0] == "devin"
    }
    outside = devin_names - SWE2_MODEL_SCOPE_PROFILES
    assert {
        "devin-glm52",
        "devin-swe17",
        "devin-ds41",
        "devin-ds41-max",
        "devin-fusion-opus55",
        "devin-fusion-sonnet55",
    } <= outside
    billing = {
        profile.name: profile.billing
        for profiles in GRADE_TABLE.values()
        for profile in profiles
        if profile.name in outside
    }
    assert billing["devin-glm52"] == "free"
    assert billing["devin-swe17"] == "free"
    assert billing["devin-ds41"] == "paid"
    assert billing["devin-fusion-opus55"] == "paid"
    probe = _probe(daily=99.0, swe2=0.0)
    for name in sorted(outside):
        result = _gate(probe, name)
        assert result.ok is False, name
        assert result.gate_basis is None, name
        assert "basis=model-scope:swe-2" not in result.reason, name
        assert result.used_pct == 99.0, name
        assert result.used_pct != 0.0


def test_paid_builder_seats_are_not_judged_on_model_scope():
    probe = _probe(daily=99.0, swe2=0.0)
    for name in ("builder-ds41", "builder-ds41-max"):
        assert name not in SWE2_MODEL_SCOPE_PROFILES
        result = _gate(probe, name)
        assert result.ok is False
        assert result.gate_basis is None
        assert "model-scope" not in result.reason


def test_free_note_text_contains_no_fixed_date(fixture_text):
    """The prefix has no end date. The stamp in free_note is the probe time."""
    prefix = devin.FREE_NOTE_PREFIX
    assert re.search(r"\d{4}-\d{2}-\d{2}", prefix) is None
    assert "until" not in prefix
    assert "2026-10-10" not in prefix
    observed = dt.datetime(2026, 10, 16, 7, 16, 42, tzinfo=dt.UTC)
    note = devin.free_note(observed)
    assert note == "Free tag observed in devin models list at 2026-10-16T07:16:42Z"
    result = devin.parse(fixture_text("devin_models_list"), now=observed)
    assert result.note == note
    assert result.buckets[0].note == note
    assert "until" not in note


def test_allow_line_names_basis_and_keeps_pool_token(monkeypatch, capsys):
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: _probe(daily=90.0, weekly=10.0)})
    rc = cli.main(["gate", "-m", "devin-swe2", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    first = out.out.splitlines()[0]
    assert first.startswith("profile=devin-swe2 pool=devin used_pct=0.0 class=spend ")
    assert "basis=model-scope:swe-2" in first.split()
    assert "free_tag=observed" in first.split()
    assert "account_daily=90" in first.split()
    assert "account_weekly=10" in first.split()
    assert _parsed_pool(first) == "devin"


def test_model_scope_deny_line_names_basis_and_pool(monkeypatch, capsys):
    """Account Daily is under the cutoff. Only the swe-2 bucket is over it."""
    denied = _probe(daily=10.0, weekly=10.0, swe2=100.0)
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: denied})
    rc = cli.main(["gate", "-m", "builder-devin", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 3
    deny = out.err.splitlines()[0]
    assert "profile=builder-devin" in deny.split()
    assert "used_pct=100" in deny.split()
    assert "class=spend" in deny.split()
    assert "basis=model-scope:swe-2" in deny.split()
    assert "free_tag=observed" in deny.split()
    assert "account_daily=10" in deny.split()
    assert _parsed_pool(deny) == "devin"


def test_builder_seats_are_gate_choices_not_recommend_list(capsys):
    parser = cli.build_parser(["devin"])
    gate = parser._subparsers._group_actions[0].choices["gate"]
    profile_action = next(action for action in gate._actions if action.dest == "profile")
    for name in _SIX:
        assert name in profile_action.choices
    assert cli.main(["--list-recommend-profiles"]) == 0
    listed = set(capsys.readouterr().out.splitlines())
    assert "builder-devin" not in listed
    assert "devin-swe2" in listed
