"""SWE-2 allowlist gates on its own rung's current Free model-scope bucket.

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
    SWE2_MODEL_SCOPE_PROFILES,
    SWE2_PROFILE_RUNG,
    gate_check,
    profile_pool,
    swe2_model_scope_basis,
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
_RUNGS = ("swe-2-high", "swe-2-medium", "swe-2-max")
_HIGH_BASIS = "model-scope:swe-2-high"


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


def _swe2(used: float | None = 0.0, rung: str = "swe-2-high") -> Bucket:
    return Bucket(
        label=rung,
        window="",
        used_pct=used,
        resets_at=None,
        scope=Scope("model", rung),
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
    rungs: tuple[str, ...] = _RUNGS,
    **overrides: object,
) -> ProviderResult:
    buckets: list[Bucket] = []
    if include_account:
        buckets.append(_account("daily", "1d", daily, "now"))
        buckets.append(_account("weekly", "7d", weekly, "week"))
    if include_swe2:
        buckets.extend(_swe2(swe2, rung) for rung in rungs)
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
    assert result.gate_basis == _HIGH_BASIS
    assert result.free_tag == SWE2_FREE_TAG_OBSERVED
    assert result.account_daily_pct == 90.0
    assert result.account_weekly_pct == 12.0
    assert "account_daily=90" in result.reason
    assert "account_weekly=12" in result.reason
    assert "basis=model-scope:swe-2-high" in result.reason.split()
    assert "free_tag=observed" in result.reason.split()


def test_free_tag_present_daily_90_allows_builder_devin_on_model_scope():
    result = _gate(_probe(daily=90.0), "builder-devin")
    assert result.ok is True
    assert result.profile == "builder-devin"
    assert result.provider_id == "devin"
    assert result.grade == "A+"
    assert result.used_pct == 0.0
    assert result.gate_basis == _HIGH_BASIS
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
        assert result.gate_basis == swe2_model_scope_basis(SWE2_PROFILE_RUNG[name])
        assert f"basis={result.gate_basis}" in result.reason.split()
        assert result.grade == expected_grade[name]


def test_free_tag_present_daily_99_still_allows_on_model_scope():
    """Daily 99 is already over the spend cutoff. The Free bucket must still allow."""
    result = _gate(_probe(daily=99.0, weekly=99.0), "devin-swe2")
    assert result.ok is True
    assert result.used_pct == 0.0
    assert result.gate_basis == _HIGH_BASIS
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


def test_stale_probe_with_free_bucket_is_measurement_unavailable():
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
    assert result.unmeasurable is True
    assert result.used_pct is None
    assert "model-scope" not in result.reason
    assert "swe2_measurement_unavailable" in result.reason


def test_aged_past_freshness_with_free_bucket_is_measurement_unavailable():
    aged = _probe(daily=99.0, age_s=120.0)
    denied = _gate(aged, "devin-swe2")
    assert denied.ok is False
    assert denied.gate_basis is None
    assert denied.unmeasurable is True
    assert denied.used_pct is None

    from_stamp = _probe(daily=99.0, fetched_at=NOW.timestamp() - 1000.0)
    denied_stamp = _gate(from_stamp, "devin-swe2")
    assert denied_stamp.ok is False
    assert denied_stamp.gate_basis is None
    assert denied_stamp.unmeasurable is True
    assert denied_stamp.used_pct is None

    fresh = _gate(_probe(daily=99.0, age_s=10.0), "devin-swe2")
    assert fresh.ok is True
    assert fresh.gate_basis == _HIGH_BASIS
    assert fresh.used_pct == 0.0

    live = _gate(_probe(daily=99.0), "devin-swe2")
    assert live.ok is True
    assert live.gate_basis == _HIGH_BASIS
    assert live.account_daily_pct == 99.0


def test_account_warning_does_not_disqualify_current_free_bucket():
    warned = _probe(daily=None, weekly=None, warning="quota probe failed")
    result = _gate(warned, "devin-swe2")
    assert result.ok is True
    assert result.unmeasurable is False
    assert result.gate_basis == _HIGH_BASIS


def test_invalid_or_ambiguous_swe2_bucket_uses_account_gate():
    invalid = _gate(_probe(daily=99.0, swe2=None), "devin-swe2")
    assert invalid.ok is False
    assert invalid.gate_basis is None
    assert invalid.used_pct == 99.0
    doubled = _probe(daily=99.0)
    doubled.buckets.append(_swe2(0.0, "swe-2-high"))
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
        assert "basis=model-scope:" not in result.reason, name
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
    assert [bucket.note for bucket in result.buckets] == [note, note, note]
    assert "until" not in note


def test_allow_line_names_basis_and_keeps_pool_token(monkeypatch, capsys):
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: _probe(daily=90.0, weekly=10.0)})
    rc = cli.main(["gate", "-m", "devin-swe2", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    first = out.out.splitlines()[0]
    assert first.startswith("profile=devin-swe2 pool=devin used_pct=0.0 class=spend ")
    assert "basis=model-scope:swe-2-high" in first.split()
    assert "free_tag=observed" in first.split()
    assert "account_daily=90" in first.split()
    assert "account_weekly=10" in first.split()
    assert _parsed_pool(first) == "devin"


def test_model_scope_deny_line_names_basis_and_pool(monkeypatch, capsys):
    """Account Daily is under the cutoff. Only the swe-2-high bucket is over it."""
    denied = _probe(daily=10.0, weekly=10.0, swe2=100.0)
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: denied})
    rc = cli.main(["gate", "-m", "builder-devin", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 3
    deny = out.err.splitlines()[0]
    assert "profile=builder-devin" in deny.split()
    assert "used_pct=100" in deny.split()
    assert "class=spend" in deny.split()
    assert "basis=model-scope:swe-2-high" in deny.split()
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


# hk 1614 — one Free model-scope bucket per SWE-2 rung. Each allowlisted name
# is judged only on its own rung's bucket; a sibling rung's Free never admits it.


def _free_only(text: str, keep: frozenset[str]) -> str:
    """Drop the Free tag from every SWE-2 family row whose id is not in keep."""
    in_swe2 = False
    out: list[str] = []
    for line in text.splitlines(True):
        stripped = line.strip()
        if stripped.startswith("SWE-2 (swe-2)"):
            in_swe2 = True
            out.append(line)
            continue
        if in_swe2 and stripped and not line[:1].isspace():
            in_swe2 = False
        row_id = stripped.split()[0] if stripped else ""
        if in_swe2 and line[:1].isspace() and row_id not in keep:
            line = line.replace("[262K context, Free]", "[262K context]")
        out.append(line)
    return "".join(out)


def _parsed_probe(text: str, *, daily: float = 99.0, weekly: float = 10.0) -> ProviderResult:
    """Account axes plus the models-list buckets, merged the way devin.fetch does."""
    models = devin.parse(text, now=NOW)
    buckets = [_account("daily", "1d", daily, "now"), _account("weekly", "7d", weekly, "week")]
    if models.error is None:
        buckets += models.buckets
    return ProviderResult(id="devin", pool_class="spend", buckets=buckets)


def _single_free_fixtures(fixture_text) -> dict[str, str]:
    text = fixture_text("devin_models_list")
    return {rung: _free_only(text, frozenset({rung})) for rung in _RUNGS}


def test_rung_map_names_exactly_one_rung_per_allowlisted_name():
    assert set(SWE2_PROFILE_RUNG) == _SIX
    assert SWE2_PROFILE_RUNG == {
        "devin-swe2": "swe-2-high",
        "builder-devin": "swe-2-high",
        "devin-swe2-medium": "swe-2-medium",
        "builder-devin-medium": "swe-2-medium",
        "devin-swe2-max": "swe-2-max",
        "builder-devin-max": "swe-2-max",
    }


def test_parse_emits_one_model_bucket_per_free_swe2_row(fixture_text):
    result = devin.parse(fixture_text("devin_models_list"), now=NOW)
    assert result.error is None
    assert [(b.label, b.scope.kind, b.scope.name) for b in result.buckets] == [
        ("swe-2-high", "model", "swe-2-high"),
        ("swe-2-medium", "model", "swe-2-medium"),
        ("swe-2-max", "model", "swe-2-max"),
    ]
    for bucket in result.buckets:
        assert bucket.used_pct == 0.0
        assert bucket.window == ""
        assert bucket.horizon == "now"
        assert bucket.note == devin.free_note(NOW)


def test_parse_emits_no_bucket_for_a_row_without_free(fixture_text):
    for rung, text in _single_free_fixtures(fixture_text).items():
        result = devin.parse(text, now=NOW)
        assert result.error is None, rung
        assert [b.scope.name for b in result.buckets] == [rung]


def test_all_rows_free_each_name_passes_on_its_own_rung(fixture_text):
    probe = _parsed_probe(fixture_text("devin_models_list"), daily=99.0)
    for name in sorted(_SIX):
        rung = SWE2_PROFILE_RUNG[name]
        result = _gate(probe, name)
        assert result.ok is True, name
        assert result.used_pct == 0.0, name
        assert result.gate_basis == f"model-scope:{rung}", name
        assert f"basis=model-scope:{rung}" in result.reason.split(), name
        assert result.account_daily_pct == 99.0


def test_only_high_free_admits_high_names_and_denies_siblings_on_account(fixture_text):
    probe = _parsed_probe(_single_free_fixtures(fixture_text)["swe-2-high"], daily=99.0)
    for name in ("devin-swe2", "builder-devin"):
        result = _gate(probe, name)
        assert result.ok is True, name
        assert result.gate_basis == "model-scope:swe-2-high", name
        assert result.used_pct == 0.0
    for name in ("devin-swe2-max", "builder-devin-max", "devin-swe2-medium", "builder-devin-medium"):
        result = _gate(probe, name)
        assert result.ok is False, name
        assert result.unmeasurable is False, name
        assert result.gate_basis is None, name
        assert result.used_pct == 99.0, name
        assert "model-scope" not in result.reason, name
        assert "소진" in result.reason, name


def test_only_max_free_devin_swe2_falls_back_to_account_not_max(fixture_text):
    text = _single_free_fixtures(fixture_text)["swe-2-max"]
    denied = _gate(_parsed_probe(text, daily=99.0), "devin-swe2")
    assert denied.ok is False
    assert denied.gate_basis is None
    assert denied.used_pct == 99.0
    assert "model-scope" not in denied.reason
    allowed = _gate(_parsed_probe(text, daily=50.0), "devin-swe2")
    assert allowed.ok is True
    assert allowed.gate_basis is None
    assert allowed.used_pct == 50.0
    assert "basis=" not in allowed.reason
    max_seat = _gate(_parsed_probe(text, daily=99.0), "builder-devin-max")
    assert max_seat.ok is True
    assert max_seat.gate_basis == "model-scope:swe-2-max"


def test_invariant_no_name_passes_on_a_sibling_rung_bucket(fixture_text):
    """Mutant guard: a name never passes on a bucket whose scope name is not its own rung."""
    cases = dict(_single_free_fixtures(fixture_text))
    cases["all"] = fixture_text("devin_models_list")
    for label, text in cases.items():
        probe = _parsed_probe(text, daily=99.0)
        present = {b.scope.name for b in probe.buckets if b.scope.kind == "model"}
        for name in sorted(_SIX):
            rung = SWE2_PROFILE_RUNG[name]
            result = _gate(probe, name)
            if rung in present:
                assert result.ok is True, (label, name)
                assert result.gate_basis == f"model-scope:{rung}", (label, name)
            else:
                assert result.ok is False, (label, name)
                assert result.gate_basis is None, (label, name)
                assert result.used_pct == 99.0, (label, name)
            for other in set(_RUNGS) - {rung}:
                assert f"model-scope:{other}" not in result.reason, (label, name)
                assert result.gate_basis != f"model-scope:{other}", (label, name)


def test_sibling_rung_buckets_never_stand_in(fixture_text):
    """Hand-built probes: every bucket except the name's own rung is present."""
    for name in sorted(_SIX):
        rung = SWE2_PROFILE_RUNG[name]
        others = tuple(r for r in _RUNGS if r != rung)
        result = _gate(_probe(daily=99.0, rungs=others), name)
        assert result.ok is False, name
        assert result.gate_basis is None, name
        assert result.used_pct == 99.0, name


def test_duplicated_own_rung_bucket_falls_back_to_account():
    for name in sorted(_SIX):
        rung = SWE2_PROFILE_RUNG[name]
        doubled = _probe(daily=99.0)
        doubled.buckets.append(_swe2(0.0, rung))
        result = _gate(doubled, name)
        assert result.ok is False, name
        assert result.gate_basis is None, name
        assert result.used_pct == 99.0, name


def test_legacy_family_bucket_admits_no_name():
    """A cached pre-1614 snapshot carries Scope("model", "swe-2"). No rung is named swe-2."""
    legacy = _probe(daily=99.0, rungs=("swe-2",))
    for name in sorted(_SIX):
        result = _gate(legacy, name)
        assert result.ok is False, name
        assert result.gate_basis is None, name


def test_non_current_probe_with_own_rung_bucket_is_unavailable(fixture_text):
    for name in sorted(_SIX):
        stale = _probe(daily=99.0, age_s=120.0)
        result = _gate(stale, name)
        assert result.ok is False, name
        assert result.gate_basis is None, name
        assert result.unmeasurable is True, name
        assert "swe2_measurement_unavailable" in result.reason, name


def test_paid_devin_rungs_stay_on_account_with_every_fixture(fixture_text):
    cases = dict(_single_free_fixtures(fixture_text))
    cases["all"] = fixture_text("devin_models_list")
    paid = (
        "devin-ds41",
        "devin-ds41-max",
        "devin-glm52",
        "devin-fusion-opus55",
        "devin-fusion-sonnet55",
    )
    for label, text in cases.items():
        for daily, ok in ((99.0, False), (50.0, True)):
            probe = _parsed_probe(text, daily=daily)
            for name in paid:
                result = _gate(probe, name)
                assert result.ok is ok, (label, name, daily)
                assert result.gate_basis is None, (label, name)
                assert "model-scope" not in result.reason, (label, name)
                assert result.used_pct == daily, (label, name)
            # The paid builder seats are not catalog spellings here; they must
            # still never reach the model-scope path.
            for name in ("builder-ds41", "builder-ds41-max"):
                result = _gate(probe, name)
                assert result.ok is False, (label, name)
                assert result.gate_basis is None, (label, name)
                assert "model-scope" not in result.reason, (label, name)


def test_max_allow_line_names_the_max_rung_and_keeps_pool_token(monkeypatch, capsys):
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: _probe(daily=99.0, rungs=("swe-2-max",))})
    rc = cli.main(["gate", "-m", "builder-devin-max", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    first = out.out.splitlines()[0]
    assert first.startswith("profile=builder-devin-max pool=devin used_pct=0.0 class=spend ")
    assert "basis=model-scope:swe-2-max" in first.split()
    assert _parsed_pool(first) == "devin"
