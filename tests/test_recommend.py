"""ROB-1181-B — grade recommendation routing and ranking."""

from __future__ import annotations

import datetime as dt

from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import GRADE_TABLE, profile_pool, recommend

TODAY = dt.date(2026, 7, 31)
RESET = (dt.datetime(2026, 8, 1, 5, tzinfo=dt.UTC)).isoformat()


def _bucket(used: float, window: str = "7d", horizon: str = "week", scope: Scope | None = None) -> Bucket:
    return Bucket(
        label=window,
        window=window,
        used_pct=used,
        resets_at=RESET,
        scope=scope or Scope("account"),
        horizon=horizon,  # type: ignore[arg-type]
    )


def _result(
    provider_id: str,
    used: float,
    pool_class: str = "spend",
    scope: Scope | None = None,
    window: str = "7d",
) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class=pool_class,  # type: ignore[arg-defined]
        buckets=[_bucket(used, window=window, scope=scope)],
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


def test_recommend_a_puts_oc_kimi_k3_first():
    providers = [
        _result("clinepass", 17.0, window="30d"),
        _result("grok", 8.0),
        _result("codex", 46.0),
        _result("agy", 95.0, scope=Scope("group", "gemini"), window="5h"),
    ]
    out = recommend(providers, "A", today=TODAY)
    lines = out.splitlines()
    assert lines[0].startswith("1. oc-kimi-k3")
    assert "ClinePass" in lines[0]
    assert "벤치 57.1" in lines[0]


def test_recommend_preserves_spend_before_preserve():
    providers = [
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "S", today=TODAY)
    ranks = [line.split()[1] for line in out.splitlines() if line.startswith("1.")]
    # kiro-opus / kiro-sol are spend S profiles and precede preserve entries.
    assert "kiro-opus" in ranks[0]


def test_recommend_excludes_exhausted_and_unmeasurable():
    providers = [
        _result("clinepass", 17.0, window="30d"),
        _result("kiro", 95.0, pool_class="spend", window="30d"),
        _result("grok", 50.0),
    ]
    out = recommend(providers, "A", today=TODAY)
    assert any("oc-kimi-k3" in line for line in out.splitlines())
    assert any("kiro-sonnet" in line and "소진" in line for line in out.splitlines())


def test_recommend_excludes_unmeasurable_provider():
    providers = [ProviderResult(id="clinepass", error="API key 없음")]
    out = recommend(providers, "A", today=TODAY)
    assert any("oc-kimi-k3" in line and "측정 불가" in line for line in out.splitlines())


def test_grade_table_has_expected_a_profiles():
    names = [p.name for p in GRADE_TABLE["A"]]
    assert names == ["oc-kimi-k3", "codex-med", "grok-hi", "sonnet", "kiro-sonnet"]
