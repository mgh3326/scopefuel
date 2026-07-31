"""ROB-1181-B / ROB-1182 — grade recommendation routing, ranking, exclude, urgency."""

from __future__ import annotations

import datetime as dt

from scopefuel import policy
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    GRADE_TABLE,
    PRESERVE_EXCLUDE_PCT,
    SPEND_EXCLUDE_PCT,
    profile_pool,
    recommend,
)

TODAY = dt.date(2026, 7, 31)
NOW = dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=dt.UTC)
RESET = (dt.datetime(2026, 8, 1, 5, tzinfo=dt.UTC)).isoformat()  # ~17h later → not urgent


def _reset_in(hours: float) -> str:
    return (NOW + dt.timedelta(hours=hours)).isoformat()


def _bucket(
    used: float,
    window: str = "7d",
    horizon: str = "week",
    scope: Scope | None = None,
    resets_at: str | None = None,
) -> Bucket:
    return Bucket(
        label=window,
        window=window,
        used_pct=used,
        resets_at=resets_at if resets_at is not None else RESET,
        scope=scope or Scope("account"),
        horizon=horizon,  # type: ignore[arg-type]
    )


def _result(
    provider_id: str,
    used: float,
    pool_class: str = "spend",
    scope: Scope | None = None,
    window: str = "7d",
    resets_at: str | None = None,
) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class=pool_class,  # type: ignore[arg-defined]
        buckets=[_bucket(used, window=window, scope=scope, resets_at=resets_at)],
    )


def test_profile_pool_matches_quota_guard():
    assert profile_pool("oc-kimi-k3") == ("clinepass", None)
    assert profile_pool("oc-gflash") == ("agy", "gemini")
    assert profile_pool("oc-sonnet46") == ("agy", "3p")
    assert profile_pool("oc-oss") == ("agy", "3p")
    assert profile_pool("agy-pro") == ("agy", "gemini")
    assert profile_pool("agy-sonnet") == ("agy", "3p")
    assert profile_pool("codex-med") == ("codex", None)
    assert profile_pool("grok-hi") == ("grok", None)


def test_recommend_a_spend_sorts_by_remaining():
    """Within spend class, higher remaining% ranks first (ROB-1182 sort)."""
    providers = [
        _result("clinepass", 17.0, window="30d"),
        _result("grok", 8.0),
        _result("codex", 46.0, pool_class="preserve"),
        _result("agy", 95.0, scope=Scope("group", "gemini"), window="5h"),
    ]
    out = recommend(providers, "A", today=TODAY, now=NOW)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    assert lines[0].startswith("1. grok-hi")
    assert "Grok" in lines[0]
    assert any(line.startswith("2. oc-kimi-k3") for line in lines)


def test_recommend_preserves_spend_before_preserve():
    providers = [
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranks = [line.split()[1] for line in out.splitlines() if line[:1].isdigit()]
    # kiro-opus / kiro-sol are spend S profiles and precede preserve entries.
    assert ranks[0] in ("kiro-opus", "kiro-sol", "🔥")


def test_recommend_excludes_exhausted_and_unmeasurable():
    providers = [
        _result("clinepass", 17.0, window="30d"),
        _result("kiro", 99.0, pool_class="spend", window="30d"),
        _result("grok", 50.0),
    ]
    out = recommend(providers, "A", today=TODAY, now=NOW)
    assert any("oc-kimi-k3" in line for line in out.splitlines())
    assert any("kiro-sonnet" in line and "소진" in line for line in out.splitlines())


def test_recommend_excludes_unmeasurable_provider():
    providers = [ProviderResult(id="clinepass", error="API key 없음")]
    out = recommend(providers, "A", today=TODAY, now=NOW)
    assert any("oc-kimi-k3" in line and "측정 불가" in line for line in out.splitlines())


def test_grade_table_has_expected_a_profiles():
    names = [p.name for p in GRADE_TABLE["A"]]
    assert names == ["oc-kimi-k3", "codex-med", "grok-hi", "sonnet", "kiro-sonnet"]


# ------------------------------------------------------------------ ROB-1182


def test_exclude_policy_removes_claude_from_s_candidates():
    policy.set_policy("claude", "exclude", until=dt.date(2026, 8, 31), note="Pro 요금제")
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 20.0, pool_class="preserve"),
        # kiro 측정 불가 → codex-max becomes first normal candidate
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    lines = out.splitlines()
    ranked = [line for line in lines if line[:1].isdigit()]
    assert ranked[0].startswith("1. codex-max")
    assert any(line.startswith("✗ opus") and "정책 제외" in line for line in lines)
    assert any(line.startswith("✗ fable") and "정책 제외" in line for line in lines)
    assert "until 2026-08-31" in out
    assert "Pro 요금제" in out
    assert not any(line[:1].isdigit() and "opus" in line for line in lines)
    assert not any(line[:1].isdigit() and "fable" in line for line in lines)


def test_exclude_clear_restores_claude_candidates():
    policy.set_policy("claude", "exclude", until=dt.date(2026, 8, 31), note="Pro 요금제")
    policy.clear_policy("claude")
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 20.0, pool_class="preserve"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranked_names = [line.split()[1] for line in out.splitlines() if line[:1].isdigit()]
    assert "opus" in ranked_names
    assert "fable" in ranked_names
    assert not any("정책 제외" in line for line in out.splitlines())


def test_preserve_cutoff_90():
    providers = [
        _result("claude", PRESERVE_EXCLUDE_PCT - 0.001, pool_class="preserve"),
        _result("codex", PRESERVE_EXCLUDE_PCT, pool_class="preserve"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    assert any(line[:1].isdigit() and "opus" in line for line in out.splitlines())
    assert any("codex-max" in line and "소진" in line for line in out.splitlines())


def test_spend_cutoff_95_is_candidate_99_is_excluded():
    providers = [
        _result("kiro", 95.0, pool_class="spend", window="30d"),
        _result("grok", SPEND_EXCLUDE_PCT, pool_class="spend"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    assert any(line[:1].isdigit() and "kiro-opus" in line for line in out.splitlines())
    # grade S has no grok profile; use grade A for grok 99%
    out_a = recommend(
        [
            _result("kiro", 95.0, pool_class="spend", window="30d"),
            _result("grok", SPEND_EXCLUDE_PCT, pool_class="spend"),
            _result("clinepass", 10.0, window="30d"),
        ],
        "A",
        today=TODAY,
        now=NOW,
    )
    assert any(line[:1].isdigit() and "kiro-sonnet" in line for line in out_a.splitlines())
    assert any("grok-hi" in line and "소진" in line for line in out_a.splitlines())


def test_spend_95_reset_imminent_gets_fire_and_top_rank():
    providers = [
        _result("kiro", 95.0, pool_class="spend", window="30d", resets_at=_reset_in(6)),
        _result("codex", 10.0, pool_class="preserve"),
        _result("claude", 10.0, pool_class="preserve"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "🔥" in lines[0]
    assert "kiro-opus" in lines[0] or "kiro-sol" in lines[0]
    assert "잔여 5%" in lines[0]
    assert "리셋" in lines[0]


def test_spend_reset_not_imminent_no_fire():
    providers = [
        _result("kiro", 50.0, pool_class="spend", window="30d", resets_at=_reset_in(13)),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert ranked
    assert "🔥" not in ranked[0]


def test_urgent_spend_outranks_non_urgent_higher_remaining():
    """Reset-imminent spend ranks above non-imminent spend even with lower remaining%."""
    providers = [
        _result("kiro", 80.0, pool_class="spend", window="30d", resets_at=_reset_in(4)),  # 20% rem, urgent
        _result("clinepass", 10.0, window="30d", resets_at=_reset_in(48)),  # 90% rem, not urgent
    ]
    out = recommend(providers, "A", today=TODAY, now=NOW)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "🔥" in lines[0] and "kiro-sonnet" in lines[0]
    assert "oc-kimi-k3" in lines[1]


def test_multiple_urgent_sorted_by_remaining():
    providers = [
        _result("kiro", 80.0, pool_class="spend", window="30d", resets_at=_reset_in(3)),  # 20% rem
        _result("clinepass", 40.0, window="30d", resets_at=_reset_in(5)),  # 60% rem
    ]
    out = recommend(providers, "A", today=TODAY, now=NOW)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "🔥" in lines[0] and "oc-kimi-k3" in lines[0]
    assert "🔥" in lines[1] and "kiro-sonnet" in lines[1]


def test_all_policy_excluded_shows_emergency_block():
    for pool in ("claude", "codex", "kiro"):
        policy.set_policy(pool, "exclude", until=dt.date(2026, 8, 31), note=f"{pool} off")
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    assert "✗ 정책 가용 후보 없음" in out
    assert "⚠ 비상 후보 (정책상 제외 — 사용 시 근거를 이슈에 기록할 것)" in out
    assert "pool=claude" in out and "until=2026-08-31" in out
    assert "opus" in out and "fable" in out and "codex-max" in out
    # no ranked normal candidates
    assert not any(line[:1].isdigit() for line in out.splitlines())


def test_no_config_backcompat_sort_and_output():
    """Missing config → previous cutoff (preserve-style 90 for all without policy) via builtins."""
    providers = [
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
        _result("claude", 10.0, pool_class="preserve"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    # spend before preserve
    assert "kiro" in lines[0]
    assert "🔥" not in out  # default RESET is ~17h away
    assert "정책 제외" not in out
    assert "비상 후보" not in out
