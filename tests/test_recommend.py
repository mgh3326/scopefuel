"""ROB-1183 — 6-tier grade recommendation: routing, ranking, exclude, urgency, gate/escalation."""

from __future__ import annotations

import datetime as dt

import pytest

from scopefuel import cli, policy
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    CODEX_SOL_XHIGH_ESCALATION_REASON,
    ESTIMATED_ANNOTATION,
    GRADE_BOUNDARIES,
    GRADE_DISCRIM,
    GRADE_TABLE,
    HAIKU_ESTIMATE_ANNOTATION,
    OPUS_MAX_ESCALATION_REASON,
    PRESERVE_EXCLUDE_PCT,
    PROFILE_ALIASES,
    SPEND_EXCLUDE_PCT,
    grade_help_text,
    profile_pool,
    recommend,
)

TODAY = dt.date(2026, 7, 31)
NOW = dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=dt.UTC)


def _reset_almost_full(window: str) -> str:
    """window 가 방금 시작된 것처럼(거의 전체 남음) reset 시각을 계산.

    pace(잔여%/소모속도 vs reset까지 남은시간)가 non-urgent 로 나오도록 하는 "평상시" 기본값.
    순수 시간-임계값 폴백을 테스트하려는 케이스는 pace 를 의도적으로 죽이는(used_pct=0 등)
    별도 resets_at 을 명시한다.
    """
    hours = {"5h": 4.9, "1d": 23.5, "7d": 167.0, "30d": 719.0}.get(window, 167.0)
    return (NOW + dt.timedelta(hours=hours)).isoformat()


RESET = _reset_almost_full("7d")  # 기존 테스트 다수의 기본 window(7d)에 대한 non-urgent 기준값

REMOVED = ["agy-pro", "kiro-glm", "kiro-minimax", "kiro-minimax21", "kiro-deepseek"]


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
        resets_at=resets_at if resets_at is not None else _reset_almost_full(window),
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


# ------------------------------------------------------------------ profile_pool


def test_profile_pool_matches_quota_guard():
    assert profile_pool("oc-kimi-k3") == ("clinepass", None)
    assert profile_pool("oc-gflash") == ("agy", "gemini")
    assert profile_pool("oc-sonnet46") == ("agy", "3p")
    assert profile_pool("oc-oss") == ("agy", "3p")
    assert profile_pool("agy-pro") == ("agy", "gemini")
    assert profile_pool("agy-sonnet") == ("agy", "3p")
    assert profile_pool("codex-med") == ("codex", None)
    assert profile_pool("grok-hi") == ("grok", None)
    assert profile_pool("codex-terra-max") == ("codex", None)
    assert profile_pool("codex-luna-max") == ("codex", None)
    assert profile_pool("kiro-sol") == ("kiro", None)
    assert profile_pool("kiro-haiku") == ("kiro", None)
    assert profile_pool("oc-omni") == ("omniroute", None)
    assert profile_pool("haiku") == ("claude", None)


# ------------------------------------------------------------------ sort/ranking


def test_recommend_s_spend_sorts_by_remaining():
    """Within spend class, higher remaining% ranks first (ROB-1182 sort)."""
    providers = [
        _result("clinepass", 17.0, window="30d"),
        _result("grok", 8.0),
        _result("codex", 46.0, pool_class="preserve"),
        _result("agy", 95.0, scope=Scope("group", "gemini"), window="5h"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    assert lines[0].startswith("1. grok-hi")
    assert "Grok" in lines[0]
    assert any("oc-kimi-k3" in line for line in lines)


def test_recommend_preserves_spend_before_preserve():
    providers = [
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    ranks = [line.split()[1] for line in out.splitlines() if line[:1].isdigit()]
    # kiro-opus / kiro-sol are spend S+ profiles and precede preserve entries.
    assert ranks[0] in ("kiro-opus", "kiro-sol", "🔥")


def test_recommend_excludes_exhausted_and_unmeasurable():
    providers = [
        _result("clinepass", 17.0, window="30d"),
        _result("grok", 50.0),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    assert any("oc-kimi-k3" in line for line in out.splitlines())
    assert any("codex-terra-max" in line and "측정 불가" in line for line in out.splitlines())


def test_recommend_excludes_exhausted_spend():
    providers = [
        _result("kiro", 99.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "A+", today=TODAY, now=NOW)
    assert any("kiro-sonnet" in line and "소진" in line for line in out.splitlines())


def test_recommend_excludes_unmeasurable_provider():
    providers = [ProviderResult(id="clinepass", error="API key 없음")]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    assert any("oc-kimi-k3" in line and "측정 불가" in line for line in out.splitlines())


def test_grade_table_has_expected_a_profiles():
    names = [p.name for p in GRADE_TABLE["A"]]
    assert {"oc-gflash", "oc-kimi-code", "oc-sonnet46"}.issubset(names)
    assert {"codex-sol", "codex-luna", "codex-terra"}.issubset(names)


# ------------------------------------------------------------------ ROB-1182


def test_exclude_policy_removes_claude_from_sp_candidates():
    policy.set_policy("claude", "exclude", until=dt.date(2026, 8, 31), note="Pro 요금제")
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 20.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    lines = out.splitlines()
    ranked = [line for line in lines if line[:1].isdigit()]
    # kiro-opus/kiro-sol spend, codex-sol preserve → kiro first
    assert any("kiro-opus" in line for line in ranked)
    assert any("codex-sol" in line for line in ranked)
    # opus is default gate but policy-excluded (ROB-1191: folded per pool)
    assert any(line.startswith("✗ claude 풀 제외") and "opus" in line for line in lines)
    assert "until 2026-08-31" in out
    assert "Pro 요금제" in out
    assert not any(line[:1].isdigit() and "opus" in line.split() for line in lines)
    # fable is escalation — not in normal candidates
    assert not any(line[:1].isdigit() and "fable" in line for line in lines)


def test_exclude_clear_restores_claude_candidates():
    policy.set_policy("claude", "exclude", until=dt.date(2026, 8, 31), note="Pro 요금제")
    policy.clear_policy("claude")
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 20.0, pool_class="preserve"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    ranked_names = [line.split()[1] for line in out.splitlines() if line[:1].isdigit()]
    assert "opus" in ranked_names
    # fable is escalation, not a normal candidate — but present in escalation section
    assert not any(line[:1].isdigit() and "fable" in line for line in out.splitlines())
    assert "⚠ 승급 후보" in out
    assert "fable" in out
    assert not any("정책 제외" in line for line in out.splitlines())


def test_preserve_cutoff_90():
    providers = [
        _result("claude", PRESERVE_EXCLUDE_PCT - 0.001, pool_class="preserve"),
        _result("codex", PRESERVE_EXCLUDE_PCT, pool_class="preserve"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    assert any(line[:1].isdigit() and "opus" in line for line in out.splitlines())
    assert any("codex-sol" in line and "소진" in line for line in out.splitlines())


def test_spend_cutoff_95_is_candidate_99_is_excluded():
    providers = [
        _result("kiro", 95.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    assert any(line[:1].isdigit() and "kiro-opus" in line for line in out.splitlines())
    # grade S has grok-hi; use it for grok 99% exclusion
    out_s = recommend(
        [
            _result("grok", SPEND_EXCLUDE_PCT, pool_class="spend"),
            _result("clinepass", 10.0, window="30d"),
        ],
        "S",
        today=TODAY,
        now=NOW,
    )
    assert any("grok-hi" in line and "소진" in line for line in out_s.splitlines())


def test_spend_95_reset_imminent_gets_fire_and_top_rank():
    providers = [
        _result("kiro", 95.0, pool_class="spend", window="30d", resets_at=_reset_in(6)),
        _result("codex", 10.0, pool_class="preserve"),
        _result("claude", 10.0, pool_class="preserve"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "🔥" in lines[0]
    assert "kiro-opus" in lines[0] or "kiro-sol" in lines[0]
    assert "잔여 5%" in lines[0]
    assert "리셋" in lines[0]


def test_spend_reset_not_imminent_no_fire():
    """폴백(시간 임계값) 경로: window 를 파싱할 수 없어 pace 계산이 불가 → reset_urgency_hours 로 판정."""
    providers = [
        _result("kiro", 50.0, pool_class="spend", window="?", resets_at=_reset_in(13)),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW, urgency_hours=12.0)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert ranked
    assert "🔥" not in ranked[0]


def test_urgent_spend_outranks_non_urgent_higher_remaining():
    """폴백 경로: reset-imminent spend 가 non-imminent spend 보다 remaining% 낮아도 앞선다."""
    providers = [
        _result("kiro", 80.0, pool_class="spend", window="?", resets_at=_reset_in(4)),  # 20% rem, urgent
        _result("clinepass", 10.0, window="?", resets_at=_reset_in(48)),  # 90% rem, not urgent
    ]
    out = recommend(providers, "A+", today=TODAY, now=NOW, urgency_hours=12.0)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "🔥" in lines[0] and "kiro-sonnet" in lines[0]
    assert "oc-glm" in lines[1]


def test_multiple_urgent_sorted_by_remaining():
    """폴백 경로: 여러 후보가 모두 시간-임박 urgent 면 잔여율(큰 순)로 정렬."""
    providers = [
        _result("kiro", 80.0, pool_class="spend", window="?", resets_at=_reset_in(3)),  # 20% rem
        _result("clinepass", 40.0, window="?", resets_at=_reset_in(5)),  # 60% rem
    ]
    out = recommend(providers, "A+", today=TODAY, now=NOW, urgency_hours=12.0)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "🔥" in lines[0] and "oc-glm" in lines[0]
    assert "🔥" in lines[1] and "oc-minimax-m3" in lines[1]
    kiro_line = next(line for line in lines if "kiro-sonnet" in line)
    assert "🔥" in kiro_line


def test_all_policy_excluded_shows_emergency_block():
    for pool in ("claude", "codex", "kiro"):
        policy.set_policy(pool, "exclude", until=dt.date(2026, 8, 31), note=f"{pool} off")
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    assert "✗ 정책 가용 후보 없음" in out
    assert "⚠ 비상 후보 (정책상 제외 — 사용 시 근거를 이슈에 기록할 것)" in out
    assert "pool=claude" in out and "until=2026-08-31" in out
    assert "opus" in out and "fable" in out and "codex-sol" in out
    # no ranked normal candidates
    assert not any(line[:1].isdigit() for line in out.splitlines())


def test_no_config_backcompat_sort_and_output():
    """Missing config → previous cutoff (preserve-style 90 for all without policy) via builtins."""
    providers = [
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
        _result("claude", 10.0, pool_class="preserve"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    # spend before preserve
    assert "kiro" in lines[0]
    assert "🔥" not in out  # default RESET is ~17h away
    assert "정책 제외" not in out
    assert "비상 후보" not in out


# ------------------------------------------------------------------ AC1: S+


def test_ac1_sp_recommend_with_claude_exclude():
    """AC1: --recommend S+ shows kiro-opus/codex-sol as normal, fable in escalation+reason+exclude."""
    policy.set_policy("claude", "exclude", until=dt.date(2026, 8, 31), note="Pro 요금제")
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 20.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    lines = out.splitlines()

    # Grade discrimination header
    assert lines[0].startswith("S+ |")

    ranked = [line for line in lines if line[:1].isdigit()]
    # kiro-opus and codex-sol are normal candidates
    assert any("kiro-opus" in line for line in ranked)
    assert any("codex-sol" in line for line in ranked)

    # codex Sol recommendation row is exactly 1 (canonical codex-sol only, no ultra)
    codex_sol_lines = [line for line in ranked if "codex-sol" in line or "codex-ultra" in line]
    assert len(codex_sol_lines) == 1
    assert "codex-sol" in codex_sol_lines[0]
    assert "codex-ultra" not in out

    # kiro-sol is also a normal candidate (different pool fallback)
    assert any("kiro-sol" in line for line in ranked)

    # fable is in escalation section with reason
    assert "⚠ 승급 후보 (조건 충족 시에만 · 근거를 이슈에 기록)" in out
    assert "fable" in out
    assert "Opus 5 대비 2배 가격" in out

    # fable NOT in normal candidates
    assert not any(line[:1].isdigit() and "fable" in line for line in lines)

    # Claude exclude status shown alongside fable escalation
    fable_section = False
    for i, line in enumerate(lines):
        if "fable" in line and "pool=claude" in line:
            fable_section = True
            # next lines should have gate_reason and policy-excluded status
            remaining = "\n".join(lines[i:])
            assert "Opus 5 대비 2배 가격" in remaining
            assert "정책 제외" in remaining
            assert "until 2026-08-31" in remaining
            break
    assert fable_section

    # opus is default-gate, policy-excluded → folded pool line, not in escalation
    assert any(line.startswith("✗ claude 풀 제외") and "opus" in line for line in lines)


# ------------------------------------------------------------------ AC2: S grade


def test_ac2_s_grade_profiles():
    """AC2: S keeps the original profiles and adds unmeasured Grok effort variants."""
    names = [p.name for p in GRADE_TABLE["S"]]
    assert {"codex-terra-max", "oc-kimi-k3", "grok-hi", "grok"}.issubset(names)


# ------------------------------------------------------------------ AC3: A+/A/C


def test_ac3_aplus_has_four_and_a_has_sonnet46_and_c_no_sonnet46():
    """AC3: A+ retains legacy profiles and adds the measured effort variants."""
    aplus_names = [p.name for p in GRADE_TABLE["A+"]]
    assert {"kiro-sonnet", "codex-luna-max", "oc-glm", "sonnet"}.issubset(aplus_names)
    assert {"codex-terra", "codex-luna", "opus", "oc-minimax-m3"}.issubset(aplus_names)

    a_names = [p.name for p in GRADE_TABLE["A"]]
    assert "oc-sonnet46" in a_names

    c_names = [p.name for p in GRADE_TABLE["C"]]
    assert "oc-sonnet46" not in c_names


def test_rob1193_splus_default_and_escalation_efforts_have_exact_reasons():
    defaults = [p for p in GRADE_TABLE["S+"] if p.gate == "default"]
    escalations = [p for p in GRADE_TABLE["S+"] if p.gate == "escalation"]
    opus_default = next(p for p in defaults if p.name == "opus")
    sol_default = next(p for p in defaults if p.name == "codex-sol")
    opus_escalation = next(p for p in escalations if p.name == "opus")
    sol_escalation = next(p for p in escalations if p.name == "codex-sol")
    assert opus_default.launcher_effort == "xhigh"
    assert sol_default.launcher_effort == "max"
    assert opus_escalation.launcher_effort == "max"
    assert opus_escalation.gate_reason == OPUS_MAX_ESCALATION_REASON
    assert sol_escalation.launcher_effort == "xhigh"
    assert sol_escalation.gate_reason == CODEX_SOL_XHIGH_ESCALATION_REASON


def test_rob1193_boundaries_effort_variants_and_lower_tier_candidates():
    assert GRADE_BOUNDARIES == {"S+": 65, "S": 61, "A+": 55, "A": 48, "B": 40}
    assert "S+≥65" in grade_help_text()
    assert "S≥61" in grade_help_text()
    assert "A+≥55" in grade_help_text()
    assert "A≥48" in grade_help_text()
    assert "B≥40" in grade_help_text()
    assert "C<40" in grade_help_text()
    assert PROFILE_ALIASES == {"codex-max": "codex-sol"}

    s_profiles = GRADE_TABLE["S"]
    for effort in ("medium", "low"):
        profile = next(p for p in s_profiles if p.name == "grok" and p.launcher_effort == effort)
        assert profile.benchmark_annotation == "미측정"

    aplus_profiles = GRADE_TABLE["A+"]
    qwen = next(p for p in GRADE_TABLE["S+"] if p.name == "oc-qwen37-max")
    minimax = next(p for p in aplus_profiles if p.name == "oc-minimax-m3")
    assert qwen.benchmark_source == minimax.benchmark_source == "AA-model"
    assert qwen.benchmark == 66.0
    assert minimax.benchmark == 58.6
    assert qwen.benchmark_annotation == minimax.benchmark_annotation == "모델지수만 있음(에이전트 미측정)"

    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 10.0, pool_class="preserve"),
        _result("clinepass", 10.0, window="30d"),
    ]
    for grade in ("A", "B", "C"):
        ranked = [
            line
            for line in recommend(providers, grade, today=TODAY, now=NOW).splitlines()
            if line[:1].isdigit()
        ]
        assert ranked, grade
    c_output = recommend(providers, "C", today=TODAY, now=NOW)
    assert "haiku --effort low" in c_output
    assert "codex-luna --effort low" in c_output


def test_rob1193_supplement_claude_cost_efficiency_and_estimates():
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 10.0, pool_class="preserve"),
        _result("clinepass", 10.0, window="30d"),
    ]

    s_output = recommend(providers, "S", today=TODAY, now=NOW)
    assert "opus --effort high" in s_output
    assert "opus --effort medium" in s_output

    aplus_output = recommend(providers, "A+", today=TODAY, now=NOW)
    assert any(line[:1].isdigit() and "sonnet --effort high" in line for line in aplus_output.splitlines())
    assert "sonnet --effort xhigh" in aplus_output
    assert "opus --effort low" in aplus_output
    assert "벤치 55.0(추정)" in aplus_output
    assert not any(line[:1].isdigit() and "opus --effort low" in line for line in aplus_output.splitlines())

    a_output = recommend(providers, "A", today=TODAY, now=NOW)
    assert any(line[:1].isdigit() and "codex-luna --effort high" in line for line in a_output.splitlines())
    assert "codex-sol --effort low" in a_output
    assert "비용효율" in a_output
    assert not any(line[:1].isdigit() and "codex-sol --effort low" in line for line in a_output.splitlines())

    b_output = recommend(providers, "B", today=TODAY, now=NOW)
    assert "haiku --effort low" in b_output
    assert f"벤치 35.0({ESTIMATED_ANNOTATION})" in b_output

    c_output = recommend(providers, "C", today=TODAY, now=NOW)
    assert f"벤치 35.0({HAIKU_ESTIMATE_ANNOTATION})" in c_output
    assert "codex-luna --effort low" in c_output
    assert "미측정" in c_output

    all_profiles = {profile.name for profiles in GRADE_TABLE.values() for profile in profiles}
    assert "gpt-5.4-mini" not in all_profiles


def test_rob1193_model_only_profiles_use_registered_coding_index_metric():
    from scopefuel.bench import ModelScore

    scores = [
        ModelScore(
            model_id="qwen3-7-max",
            effort=None,
            harness=None,
            source="AA-model",
            metric="intelligence",
            score=46.0,
            rank=2,
            captured_at="2026-08-01T00:00:00+00:00",
        ),
        ModelScore(
            model_id="qwen3-7-max",
            effort=None,
            harness=None,
            source="AA-model",
            metric="coding_index",
            score=66.0,
            rank=1,
            captured_at="2026-08-01T00:00:00+00:00",
        ),
        ModelScore(
            model_id="minimax-m3",
            effort=None,
            harness=None,
            source="AA-model",
            metric="intelligence",
            score=44.4,
            rank=2,
            captured_at="2026-08-01T00:00:00+00:00",
        ),
        ModelScore(
            model_id="minimax-m3",
            effort=None,
            harness=None,
            source="AA-model",
            metric="coding_index",
            score=58.6,
            rank=1,
            captured_at="2026-08-01T00:00:00+00:00",
        ),
    ]
    providers = [_result("clinepass", 10.0, window="30d")]
    splus = recommend(providers, "S+", today=TODAY, now=NOW, bench_scores=scores)
    aplus = recommend(providers, "A+", today=TODAY, now=NOW, bench_scores=scores)
    qwen_line = next(line for line in splus.splitlines() if "oc-qwen37-max" in line and line[:1].isdigit())
    minimax_line = next(line for line in aplus.splitlines() if "oc-minimax-m3" in line and line[:1].isdigit())
    assert "66.0(AA-model/unspecified)" in qwen_line
    assert "58.6(AA-model/unspecified)" in minimax_line
    assert "모델지수만 있음(에이전트 미측정)" in qwen_line
    assert "모델지수만 있음(에이전트 미측정)" in minimax_line
    assert "46.0" not in qwen_line and "44.4" not in minimax_line


# ------------------------------------------------------------------ AC4: C grade


def test_ac4_c_omni_escalation_and_oss_escalation():
    """AC4: C 정상 순위엔 oc-omni/oc-oss 둘 다 없음 — 둘 다 escalation 섹션(지정 사유 포함)."""
    providers = [
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "C", today=TODAY, now=NOW)
    lines = out.splitlines()
    ranked = [line for line in lines if line[:1].isdigit()]

    # kiro-cheap (유일한 정상 C 후보) 가 1위
    assert ranked[0].startswith("1. kiro-cheap")

    # oc-omni, oc-oss 모두 정상 후보에 없음 (escalation)
    assert not any(line[:1].isdigit() and "oc-omni" in line for line in lines)
    assert not any(line[:1].isdigit() and "oc-oss" in line for line in lines)

    # 둘 다 escalation 섹션에, 정상후보/제외 뒤에 위치
    escalation_idx = next(i for i, line in enumerate(lines) if "⚠ 승급 후보" in line)
    last_ranked = max((i for i, line in enumerate(lines) if line[:1].isdigit()), default=-1)
    last_excluded = max((i for i, line in enumerate(lines) if line.startswith("✗")), default=-1)
    assert escalation_idx > last_ranked
    assert escalation_idx > last_excluded

    assert "oc-omni" in out and "oc-oss" in out
    assert "big-pickle 162콜" in out
    assert "deepseek-v4-flash 21콜" in out
    assert "agy 3p 풀 소모" in out


# ------------------------------------------------------------------ AC5: removed


def test_ac5_removed_profiles_absent():
    """AC5: removed 5 profiles absent from all grade tables and recommend output."""
    for grade in GRADE_TABLE:
        names = [p.name for p in GRADE_TABLE[grade]]
        for removed in REMOVED:
            assert removed not in names, f"{removed} found in grade {grade}"

    # Also verify they don't appear in recommend output for any grade
    for grade in GRADE_TABLE:
        providers = [
            _result("clinepass", 10.0, window="30d"),
            _result("kiro", 10.0, pool_class="spend", window="30d"),
            _result("codex", 10.0, pool_class="preserve"),
            _result("claude", 10.0, pool_class="preserve"),
            _result("grok", 10.0),
            _result("agy", 10.0, scope=Scope("group", "gemini")),
            _result("agy", 50.0, scope=Scope("group", "3p")),
        ]
        out = recommend(providers, grade, today=TODAY, now=NOW)
        for removed in REMOVED:
            assert removed not in out, f"{removed} found in grade {grade} output"


def test_ac5_kiro_sol_present_in_splus():
    """Operator correction: kiro-sol is a different-pool Sol fallback and stays in S+."""
    names = [p.name for p in GRADE_TABLE["S+"]]
    assert "kiro-sol" in names


# ------------------------------------------------------------------ AC6: help


def test_ac6_help_shows_discrimination_table():
    """AC6: argparse help exposes the 6-tier discrimination table."""
    help_text = grade_help_text()
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        assert grade in help_text
    assert "틀리면 되돌리는 비용이 큰가?" in help_text
    assert "무엇이 잘못될 수 있는지 스스로 열거해야 하나?" in help_text
    assert "방향은 정해졌고 설계 판단이 좀 남았나?" in help_text
    assert "무엇을 만들지 문서에 다 적혀 있나?" in help_text
    assert "정답이 유일하고 검색·치환에 가까운가?" in help_text
    assert "실패해도 버리고 다시 하면 되나?" in help_text


def test_ac6_cli_recommend_choices_include_all_grades():
    """AC6: CLI --recommend choices include all 6 grades."""
    parser = cli.build_parser(["claude", "codex", "kiro", "grok", "agy", "clinepass"])
    action = next(a for a in parser._actions if "--recommend" in a.option_strings)
    assert set(action.choices) == {"S+", "S", "A+", "A", "B", "C"}


def test_ac6_cli_help_contains_discrimination_text(capsys):
    """AC6: `scopefuel --help` output contains the discrimination table."""
    parser = cli.build_parser(["claude", "codex", "kiro", "grok", "agy", "clinepass"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])
    out = capsys.readouterr().out
    assert "S+" in out and "A+" in out
    assert "틀리면 되돌리는 비용이 큰가?" in out
    assert "실패해도 버리고 다시 하면 되나?" in out


def test_ac6_recommend_output_shows_grade_header():
    """AC6: each --recommend <grade> output shows the grade discrimination header."""
    for grade in GRADE_TABLE:
        info = GRADE_DISCRIM[grade]
        out = recommend([], grade, today=TODAY, now=NOW)
        first_line = out.splitlines()[0]
        assert grade in first_line
        assert info.task_class in first_line
        assert info.decision_question in first_line


# ------------------------------------------------------------------ AC7: no-regression


def test_ac7_no_config_no_regression():
    """AC7: config-less state → builtin cutoff/policy/urgency unchanged."""
    providers = [
        _result("codex", 89.0, pool_class="preserve"),
        _result("kiro", 50.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    # 89% < 90% preserve cutoff → included
    assert any(line[:1].isdigit() and "codex-sol" in line for line in out.splitlines())
    # no policy exclude, no emergency
    assert "정책 제외" not in out
    assert "비상 후보" not in out
    # not urgent (RESET ~17h)
    assert "🔥" not in out


def test_ac7_preserve_cutoff_at_90_excludes():
    providers = [
        _result("codex", 90.0, pool_class="preserve"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    assert any("codex-sol" in line and "소진" in line for line in out.splitlines())
    assert not any(line[:1].isdigit() and "codex-sol" in line for line in out.splitlines())


# ------------------------------------------------------------------ gate/escalation


def test_escalation_profiles_not_in_normal_candidates():
    """Escalation profiles (fable, oc-oss) never appear in ranked normal candidates."""
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
        _result("clinepass", 10.0, window="30d"),
        _result("agy", 10.0, scope=Scope("group", "3p")),
    ]
    for grade in GRADE_TABLE:
        out = recommend(providers, grade, today=TODAY, now=NOW)
        ranked = [line for line in out.splitlines() if line[:1].isdigit()]
        for line in ranked:
            assert "fable" not in line
            assert "oc-oss" not in line


def test_fable_escalation_shows_reason_and_pool_status():
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    lines = out.splitlines()
    assert "⚠ 승급 후보 (조건 충족 시에만 · 근거를 이슈에 기록)" in out
    assert any("fable" in line for line in lines)
    assert "Opus 5 대비 2배 가격" in out
    # fable pool is available → status shows usage (multi-window display after ROB-1191)
    assert any("사용 " in line and "10%" in line for line in lines)


def test_fable_escalation_with_policy_exclude_shows_both():
    """When fable's pool is policy-excluded, both escalation reason and exclude status show."""
    policy.set_policy("claude", "exclude", until=dt.date(2026, 8, 31), note="Pro 요금제")
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    assert "⚠ 승급 후보" in out
    assert "fable" in out
    assert "Opus 5 대비 2배 가격" in out
    assert "정책 제외" in out
    assert "until 2026-08-31" in out


def test_oc_oss_escalation_at_end_of_c():
    providers = [
        _result("kiro", 10.0, pool_class="spend", window="30d"),
        _result("agy", 10.0, scope=Scope("group", "3p")),
    ]
    out = recommend(providers, "C", today=TODAY, now=NOW)
    lines = out.splitlines()

    # kiro-cheap 이 1위 (oc-omni/oc-oss 는 escalation)
    ranked = [line for line in lines if line[:1].isdigit()]
    assert ranked[0].startswith("1. kiro-cheap")

    # oc-oss 는 escalation 섹션에, 정상후보/제외 뒤에 위치
    escalation_idx = next(i for i, line in enumerate(lines) if "⚠ 승급 후보" in line)
    last_ranked = max((i for i, line in enumerate(lines) if line[:1].isdigit()), default=-1)
    last_excluded = max((i for i, line in enumerate(lines) if line.startswith("✗")), default=-1)
    last_content = max(last_ranked, last_excluded)
    assert escalation_idx > last_content
    assert any("oc-oss" in line for line in lines[escalation_idx:])
    assert "agy 3p 풀 소모" in out


def test_oc_omni_escalation_available_without_provider():
    """oc-omni 는 escalation 후보 — provider 미등록이어도 다른 C 후보가 없으면 통계상 측정불가로 표시."""
    providers: list[ProviderResult] = []
    out = recommend(providers, "C", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    # kiro-cheap 은 provider 없어 측정 불가 → 정상 후보 없음
    assert not ranked
    assert "⚠ 승급 후보" in out
    assert "oc-omni" in out
    assert "OmniRoute" in out
    assert "big-pickle 162콜" in out


def test_oc_omni_escalation_not_ranked_above_urgent_spend():
    """oc-omni 는 escalation 이므로 urgent spend 정상후보보다 위에 랭크되지 않는다(정상후보 아님)."""
    providers = [
        _result("kiro", 95.0, pool_class="spend", window="30d", resets_at=_reset_in(3)),
    ]
    out = recommend(providers, "C", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    # kiro-cheap 이 urgent 1위 (유일한 정상 후보)
    assert ranked[0].startswith("1. 🔥 kiro-cheap") or ranked[0].startswith("1. kiro-cheap")
    assert not any("oc-omni" in line for line in ranked)


def test_gate_does_not_promote_normal_candidates():
    """Gate escalation should not auto-promote or lower grade for normal candidates."""
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    # Normal candidates are present (gate didn't remove them)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert len(ranked) >= 3  # kiro-opus, kiro-sol, codex-sol, opus
    # Escalation section is separate
    assert "⚠ 승급 후보" in out


# ------------------------------------------------------------------ ROB-1184: numeric boost


def test_boost_promotes_candidate_to_first_rank():
    """AC1: boost=1 (until 유효) 인 codex 가 S grade 에서 1순위가 된다."""
    policy.set_policy("codex", "spend", until=dt.date(2026, 8, 5), boost=1, note="리셋권")
    providers = [
        _result("codex", 10.0, pool_class="spend", window="30d"),
        _result("clinepass", 10.0, window="30d"),
        _result("grok", 10.0),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert ranked[0].startswith("1. codex-terra-max")


def test_boost_none_restores_default_sort():
    """AC2: `policy set codex --boost none` 후 boost 효과만 원복(다른 정책은 유지)."""
    policy.set_policy("codex", "spend", until=dt.date(2026, 8, 5), boost=1, note="리셋권")
    policy.set_policy("codex", None, boost=None)
    providers = [
        _result("codex", 10.0, pool_class="spend", window="30d"),
        _result("clinepass", 17.0, window="30d"),
        _result("grok", 8.0),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    # boost 해제 → class(둘 다 spend) 동률 → capacity_weight(둘 다 기본 1.0) 반영 실효잔여 정렬로 복귀.
    # codex 는 여전히 spend 클래스 정책이 남아있으므로 class 자체는 유지된다.
    effective_class, _ = policy.get_policy("codex", "preserve", today=TODAY)
    assert effective_class == "spend"
    boost, _ = policy.get_boost("codex", today=TODAY)
    assert boost is None
    # boost 가 없으니 정렬은 잔여율 기준 — grok(92% 잔여) 이 1위.
    assert "grok-hi" in ranked[0]


def test_boost_expired_does_not_affect_sort():
    policy.set_policy("codex", "spend", until=dt.date(2026, 7, 1), boost=1)  # 이미 만료
    providers = [
        _result("codex", 50.0, pool_class="spend", window="30d"),
        _result("clinepass", 10.0, window="30d"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    # boost 만료 → 일반 정렬(잔여율 큰 순): clinepass(90%) 가 codex(50%) 보다 우선.
    assert "oc-kimi-k3" in ranked[0]


def test_boost_bool_true_in_config_is_rejected_not_treated_as_1():
    """config 에 boost=true 가 있어도 숫자로 오인하지 않고 무시(기본 정렬로 복귀)."""
    import tomllib

    text = '[pools.codex]\nboost = true\nclass = "spend"\nuntil = "2026-08-05"\n'
    config_path = policy.config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text)
    assert tomllib.loads(text)["pools"]["codex"]["boost"] is True  # bool 로 파싱됨을 확인

    providers = [
        _result("codex", 50.0, pool_class="spend", window="30d"),
        _result("clinepass", 10.0, window="30d"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    # boost 무시 → 잔여율 정렬: clinepass 가 codex 보다 우선.
    assert "oc-kimi-k3" in ranked[0]


# ------------------------------------------------------------------ ROB-1188: imminent-reset boost 역전


def test_imminent_reset_outranks_boost():
    """리셋 1h 이내 + 잔여 5% 초과인 spend 풀이 boost 걸린 풀보다 앞선다."""
    policy.set_policy("codex", "spend", until=dt.date(2026, 8, 5), boost=1, note="리셋권")
    providers = [
        _result("codex", 10.0, pool_class="spend", window="30d"),  # boost=1, 리셋 08-05(안 임박)
        _result(
            "clinepass",
            97.64,  # 잔여 2.36% -> 유의미 임계(기본 5%) 미달이면 역전 안 함 확인용 대조가 아니라
            pool_class="spend",
            window="7d",
            resets_at=_reset_in(0.5),  # 30분 뒤 소멸 -> 임박
        ),
        _result("grok", 88.0, window="7d", resets_at=_reset_in(0.5)),  # 잔여 12% -> 유의미 + 임박
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    # grok(잔여 12%, 30분 뒤 소멸)이 boost=1 인 codex 보다 앞서야 한다.
    assert "grok-hi" in ranked[0]
    assert "소멸 임박 우선" in ranked[0]
    assert "🔥🔥" in ranked[0]


def test_imminent_reset_does_not_reverse_when_remaining_is_trivial():
    """잔여가 사소하면(기본 임계 5% 미만) 역전하지 않는다 — 실측 사례(잔여 2.36%)."""
    policy.set_policy("codex", "spend", until=dt.date(2026, 8, 5), boost=1, note="리셋권")
    providers = [
        _result("codex", 10.0, pool_class="spend", window="30d"),  # boost=1
        _result(
            "clinepass",
            97.64,  # 잔여 2.36% < 기본 임계 5% -> 역전 대상 아님
            pool_class="spend",
            window="7d",
            resets_at=_reset_in(0.47),  # 28분 뒤
        ),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    # boost 가 여전히 최우선 — codex 가 1위.
    assert ranked[0].startswith("1. codex-terra-max")
    assert "소멸 임박 우선" not in ranked[0]


def test_imminent_reset_does_not_apply_when_reset_not_imminent():
    """리셋까지 시간이 아직 넉넉하면(기본 1h 초과) 역전하지 않는다."""
    policy.set_policy("codex", "spend", until=dt.date(2026, 8, 5), boost=1, note="리셋권")
    providers = [
        _result("codex", 10.0, pool_class="spend", window="30d"),  # boost=1
        _result(
            "clinepass",
            50.0,  # 잔여 50% (유의미) 이지만 리셋까지 2시간 -> 기본 임계(1h) 초과
            pool_class="spend",
            window="7d",
            resets_at=_reset_in(2.0),
        ),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert ranked[0].startswith("1. codex-terra-max")
    assert "소멸 임박 우선" not in ranked[0]


def test_imminent_reset_thresholds_configurable_via_settings():
    """[settings] imminent_reset_hours/imminent_remaining_pct 로 조정 가능."""
    config_path = policy.config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[settings]\nimminent_reset_hours = 3\nimminent_remaining_pct = 1\n\n"
        '[pools.codex]\nclass = "spend"\nuntil = "2026-08-05"\nboost = 1\n'
    )
    providers = [
        _result("codex", 10.0, pool_class="spend", window="30d"),
        _result(
            "clinepass",
            98.5,  # 잔여 1.5% -> 조정된 임계 1% 이상이면 유의미
            pool_class="spend",
            window="7d",
            resets_at=_reset_in(2.5),  # 조정된 임계 3h 이내
        ),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "oc-kimi-k3" in ranked[0]
    assert "소멸 임박 우선" in ranked[0]


# ------------------------------------------------------------------ ROB-1184: capacity_weight


def test_capacity_weight_price_usd_shifts_rank_but_not_cutoff():
    """AC3: claude price_usd=200 → weight=10 → 실효잔여 반영으로 순위 상승, raw 90%/99% 컷은 불변."""
    policy.set_policy("claude", "preserve", until=dt.date(2026, 8, 31))
    config = policy.load_config()
    config["pools"]["claude"]["price_usd"] = 200
    policy._write_config(config)

    weight, _ = policy.get_capacity_weight("claude")
    assert weight == 10.0

    # claude(89% used, preserve, weight=10 → 실효잔여=1.1*10=11) vs
    # codex(50% used, preserve, weight=1 → 실효잔여=50) — weight 없으면 claude 가 밀리지만
    # weight=10 이 실효잔여를 끌어올려 순위에 반영된다(정확한 순위는 값에 따라 달라짐 —
    # 여기서는 raw cutoff 는 그대로 89%<90% 로 포함됨을 확인).
    providers = [
        _result("claude", 89.0, pool_class="preserve"),
        _result("codex", 89.0, pool_class="preserve"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    # 둘 다 89% < 90% cutoff 이므로 포함됨 (raw cutoff 불변 확인)
    assert any("opus" in line for line in ranked)
    assert any("codex-sol" in line for line in ranked)
    # weight=10 인 claude(opus) 의 실효잔여(11*10=110)가 codex(11*1=11) 보다 커서 opus 가 먼저.
    assert ranked[0].split()[1] == "opus"


def test_capacity_weight_does_not_bypass_raw_cutoff():
    """capacity_weight 가 커도 raw used_pct 컷(90%/99%)은 그대로 적용되어 후보에서 제외된다."""
    config = policy.load_config()
    pools = config.setdefault("pools", {})
    pools["claude"] = {"price_usd": 200}
    policy._write_config(config)

    providers = [_result("claude", 90.0, pool_class="preserve")]  # cutoff 90% 이상 → 소진
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    assert any("opus" in line and "소진" in line for line in out.splitlines())
    assert not any(line[:1].isdigit() and "opus" in line for line in out.splitlines())


# ------------------------------------------------------------------ ROB-1184: reset_urgency_hours settings


def test_reset_urgency_hours_setting_changes_fallback_threshold():
    """AC6: [settings] reset_urgency_hours 변경이 폴백(pace 불가) 🔥 판정에 반영된다."""
    config = policy.load_config()
    config["settings"] = {"reset_urgency_hours": 20}
    policy._write_config(config)

    # window="?" → pace 계산 불가 → 폴백. reset 15h 남음: 기본 12h 라면 not urgent, 20h 로 늘리면 urgent.
    providers = [_result("kiro", 50.0, pool_class="spend", window="?", resets_at=_reset_in(15))]
    out = recommend(providers, "S+", today=TODAY, now=NOW)  # urgency_hours 인자 생략 → config 값 사용
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "🔥" in ranked[0]


def test_reset_urgency_hours_default_backcompat_without_settings():
    """설정 없으면 기본 12.0 유지 (백컴팻)."""
    providers = [_result("kiro", 50.0, pool_class="spend", window="?", resets_at=_reset_in(15))]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "🔥" not in ranked[0]


def test_pace_urgent_path_is_independent_of_reset_urgency_hours_setting():
    """pace 경로(소모속도 산출 가능)는 reset_urgency_hours 설정과 무관하게 수식으로만 결정된다."""
    config = policy.load_config()
    config["settings"] = {"reset_urgency_hours": 0.01}  # 매우 작게 설정해도 pace 판정에는 영향 없음
    policy._write_config(config)

    # 30d 창, 707h 경과, 50% 사용 (느린 소모) → pace 로 urgent=True (reset_urgency_hours 무관)
    providers = [_result("kiro", 50.0, pool_class="spend", window="30d", resets_at=_reset_in(13))]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "🔥" in ranked[0]


# ------------------------------------------------------------------ ROB-1190 ③-1: ultra 폐기 — 벤치 셀 필터


def test_benchmark_cell_filters_out_ultra_effort_scores():
    """DB 에 과거 ultra 실측이 남아있어도 추천 벤치 셀에는 표시하지 않는다."""
    from scopefuel.bench import ModelScore
    from scopefuel.recommend import recommend as recommend_fn

    scores = [
        ModelScore(
            model_id="gpt-5.6-luna",
            effort="ultra",
            harness="codex",
            source="AA-agent",
            metric="agentic",
            score=75.0,
            rank=1,
            captured_at="2026-07-31T12:00:00+00:00",
        ),
        ModelScore(
            model_id="gpt-5.6-luna",
            effort="max",
            harness="codex",
            source="AA-agent",
            metric="agentic",
            score=59.0,
            rank=2,
            captured_at="2026-07-31T12:00:00+00:00",
        ),
    ]
    providers = [_result("codex", 10.0, pool_class="spend", window="30d")]
    out = recommend_fn(providers, "A+", today=TODAY, now=NOW, bench_scores=scores)
    luna_line = next(line for line in out.splitlines() if "codex-luna-max" in line)
    assert "ultra" not in luna_line
    assert "max" in luna_line
    assert "59.0" in luna_line


def test_benchmark_cell_filters_ultra_from_model_and_other_sources():
    """폐기된 ultra 는 AA-model/other dynamic source에서도 추천에 다시 나오지 않는다."""
    from scopefuel.bench import ModelScore
    from scopefuel.recommend import recommend as recommend_fn

    scores = [
        ModelScore(
            model_id="claude-sonnet-5",
            effort="ultra",
            harness=None,
            source="AA-model",
            metric="coding_index",
            score=75.0,
            rank=1,
            captured_at="2026-07-31T12:00:00+00:00",
        ),
        ModelScore(
            model_id="sonnet-5",
            effort="ultra",
            harness=None,
            source="openrouter",
            metric="coding",
            score=75.0,
            rank=1,
            captured_at="2026-07-31T12:00:00+00:00",
        ),
    ]
    out = recommend_fn(
        [_result("kiro", 10.0, pool_class="spend", window="30d")],
        "A+",
        today=TODAY,
        now=NOW,
        bench_scores=scores,
    )
    kiro_line = next(line for line in out.splitlines() if "kiro-sonnet" in line)
    # ultra retired: never re-surface as best/representative score
    assert "ultra" not in kiro_line
    assert "75.0" not in kiro_line
    assert "대표" not in kiro_line


# ---------------------------------------------------------- ROB-1191 AC: multi-window / score / fold / effort


def _result_windows(
    provider_id: str,
    windows: list[tuple[float, str]],
    pool_class: str = "preserve",
) -> ProviderResult:
    """Build a provider with multiple account-scope windows: [(used_pct, window), ...]."""
    buckets = []
    for used, window in windows:
        horizon = "now" if window in ("5h",) else "week"
        buckets.append(_bucket(used, window=window, horizon=horizon))
    return ProviderResult(
        id=provider_id,
        pool_class=pool_class,  # type: ignore[arg-type]
        buckets=buckets,
    )


def test_rob1191_any_window_over_cutoff_excludes_candidate():
    """AC1: 5h 95% + 7d 46% preserve → 5h exceeds 90% cutoff → excluded (not max-only)."""
    providers = [
        _result_windows("claude", [(95.0, "5h"), (46.0, "7d")], pool_class="preserve"),
        _result("codex", 20.0, pool_class="preserve"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert not any("opus" in line for line in ranked)
    # positive: folded exhausted line names the over-cutoff window usage
    assert any("claude 풀 소진" in line and "opus" in line and "95%" in line for line in out.splitlines())
    # negative: must not keep opus as a ranked candidate via the lower 7d window
    assert not any(line[:1].isdigit() and "opus" in line and "46%" in line for line in out.splitlines())
    assert any(line[:1].isdigit() and "codex-sol" in line for line in ranked)


def test_rob1191_recommend_shows_all_windows_and_constraint():
    """AC2: --recommend lists every measurable window plus 제약=."""
    providers = [
        _result_windows("claude", [(31.0, "5h"), (46.0, "7d")], pool_class="preserve"),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    opus_line = next(line for line in out.splitlines() if line[:1].isdigit() and "opus" in line)
    assert "5h 31%" in opus_line
    assert "주 46%" in opus_line or "7d 46%" in opus_line
    assert "제약=5h" in opus_line
    # constraint must be the short window (lower TTE), not the higher used_pct week window alone
    assert "제약=주" not in opus_line


def test_rob1191_explain_shows_score_components():
    """AC3: --explain exposes numeric score parts + constraint window."""
    providers = [
        _result("grok", 10.0),
        _result("clinepass", 20.0, window="30d"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW, explain=True)
    explain_lines = [line for line in out.splitlines() if line.strip().startswith("score=")]
    assert explain_lines, "expected at least one score= explain line"
    sample = explain_lines[0]
    assert "capacity=" in sample
    assert "waste" in sample
    assert "thru" in sample or "throughput" in sample
    assert "제약=" in sample
    # components must be numeric, not opaque magic-only
    assert any(ch.isdigit() for ch in sample)


def test_rob1191_rotation_when_top_pool_remaining_drops():
    """AC4: same grade — depleting rank-1 pool remaining promotes former rank-2."""

    def _name(line: str) -> str:
        rest = line.split(".", 1)[1].strip()
        tok = rest.split()[1] if rest.startswith("🔥") else rest.split()[0]
        return tok

    # oc-kimi at 10% rem90 ranks above grok at 40% rem60 (capacity term).
    before = recommend(
        [
            _result("grok", 40.0),
            _result("clinepass", 10.0, window="30d"),
            _result("codex", 70.0, pool_class="preserve"),
        ],
        "S",
        today=TODAY,
        now=NOW,
    )
    after = recommend(
        [
            _result("grok", 40.0),
            _result("clinepass", 85.0, window="30d"),  # deplete former #1 pool
            _result("codex", 70.0, pool_class="preserve"),
        ],
        "S",
        today=TODAY,
        now=NOW,
    )
    before_names = [_name(line) for line in before.splitlines() if line[:1].isdigit()]
    after_names = [_name(line) for line in after.splitlines() if line[:1].isdigit()]
    assert before_names[0] == "oc-kimi-k3"
    assert before_names.index("oc-kimi-k3") < before_names.index("grok-hi")
    # After depleting clinepass/oc-kimi remaining, grok-hi becomes #1
    assert after_names[0] == "grok-hi"
    assert after_names.index("grok-hi") < after_names.index("oc-kimi-k3")


def test_rob1191_boost_hard_override_stays_first():
    """AC5: numeric boost pool is always rank 1 under continuous score."""
    policy.set_policy("codex", "spend", until=dt.date(2026, 8, 5), boost=1, note="리셋권")
    providers = [
        _result("codex", 80.0, pool_class="spend", window="30d"),  # low remaining
        _result("clinepass", 5.0, window="30d"),  # high remaining
        _result("grok", 5.0),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW, explain=True)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert ranked[0].startswith("1. codex-terra-max")
    # negative: higher-remaining pools must not overtake boost
    assert not ranked[0].startswith("1. grok-hi")
    assert not ranked[0].startswith("1. oc-kimi-k3")


def test_rob1191_excluded_kiro_pool_folds_and_hide_excluded():
    """AC6: same-grade kiro policy excludes fold to one pool line; --hide-excluded removes it."""
    policy.set_policy(
        "kiro",
        "exclude",
        until=dt.date(2026, 9, 1),
        note="무료 전환(구독 종료 2026-08-01)",
    )
    providers = [
        _result("kiro", 10.0, pool_class="spend", window="30d"),
        _result("codex", 20.0, pool_class="preserve"),
        _result("claude", 10.0, pool_class="preserve"),
    ]
    shown = recommend(providers, "S+", today=TODAY, now=NOW, hide_excluded=False)
    hidden = recommend(providers, "S+", today=TODAY, now=NOW, hide_excluded=True)

    fold_lines = [line for line in shown.splitlines() if "kiro 풀 제외" in line]
    assert len(fold_lines) == 1
    fold = fold_lines[0]
    assert "2개" in fold
    assert "kiro-opus" in fold and "kiro-sol" in fold
    assert "until 2026-09-01" in fold
    assert "무료 전환" in fold
    # negative: must not emit one long line per profile for the same pool
    assert not any(line.startswith("✗ kiro-opus") for line in shown.splitlines())
    assert not any(line.startswith("✗ kiro-sol") for line in shown.splitlines())

    assert "kiro 풀 제외" not in hidden
    assert "kiro-opus" not in hidden or "승급" in hidden  # names only if elsewhere; fold gone
    assert not any("풀 제외" in line and "kiro" in line for line in hidden.splitlines())


def test_rob1191_one_effort_bench_cells_and_unknown_is_mijeong():
    """AC7: codex-sol → max 1점; opus → xhigh 1점; multi-effort unknown → 미지정 (no best-pick)."""
    from scopefuel.bench import ModelScore

    scores = [
        ModelScore(
            model_id="gpt-5.6-sol",
            effort="max",
            harness="codex",
            source="AA-agent",
            metric="agentic",
            score=67.0,
            rank=1,
            captured_at="2026-08-01T00:00:00+00:00",
        ),
        ModelScore(
            model_id="gpt-5.6-sol",
            effort="high",
            harness="codex",
            source="AA-agent",
            metric="agentic",
            score=60.0,
            rank=2,
            captured_at="2026-08-01T00:00:00+00:00",
        ),
        ModelScore(
            model_id="claude-opus-5",
            effort="xhigh",
            harness="claude-code",
            source="AA-agent",
            metric="agentic",
            score=67.0,
            rank=1,
            captured_at="2026-08-01T00:00:00+00:00",
        ),
        ModelScore(
            model_id="claude-opus-5",
            effort="high",
            harness="claude-code",
            source="AA-agent",
            metric="agentic",
            score=63.0,
            rank=2,
            captured_at="2026-08-01T00:00:00+00:00",
        ),
        # oc-kimi-k3 has no Profile.benchmark_effort → multi-effort must not pick best
        ModelScore(
            model_id="kimi-k3",
            effort="high",
            harness="codex",
            source="AA-agent",
            metric="agentic",
            score=50.0,
            rank=2,
            captured_at="2026-08-01T00:00:00+00:00",
        ),
        ModelScore(
            model_id="kimi-k3",
            effort="max",
            harness="codex",
            source="AA-agent",
            metric="agentic",
            score=57.0,
            rank=1,
            captured_at="2026-08-01T00:00:00+00:00",
        ),
    ]
    sp = recommend(
        [
            _result("claude", 10.0, pool_class="preserve"),
            _result("codex", 10.0, pool_class="preserve"),
            _result("kiro", 10.0, pool_class="spend", window="30d"),
        ],
        "S+",
        today=TODAY,
        now=NOW,
        bench_scores=scores,
    )
    s = recommend(
        [
            _result("clinepass", 10.0, window="30d"),
            _result("grok", 10.0),
            _result("codex", 10.0, pool_class="preserve"),
        ],
        "S",
        today=TODAY,
        now=NOW,
        bench_scores=scores,
    )

    codex_line = next(line for line in sp.splitlines() if "codex-sol" in line and line[:1].isdigit())
    opus_line = next(line for line in sp.splitlines() if line[:1].isdigit() and "opus" in line.split())
    kimi_line = next(line for line in s.splitlines() if "oc-kimi-k3" in line and line[:1].isdigit())

    # positive: single declared-effort cell
    codex_bench = codex_line.split("벤치", 1)[1].strip()
    opus_bench = opus_line.split("벤치", 1)[1].strip()
    kimi_bench = kimi_line.split("벤치", 1)[1].strip()
    assert codex_bench == "67.0(AA-agent/codex/max)"
    assert "60.0" not in codex_line  # non-declared high row must not appear
    assert "; " not in codex_bench  # not multi-effort list
    assert opus_bench == "67.0(AA-agent/claude-code/xhigh)"
    assert "63.0" not in opus_line
    assert "; " not in opus_bench
    assert "/high)" not in opus_bench  # high≠xhigh

    # positive + negative for unknown multi-effort (no best-pick / 대표)
    assert kimi_bench == "미지정"
    assert "57.0" not in kimi_line
    assert "50.0" not in kimi_line
    assert "대표" not in kimi_line
    assert "AA-agent" not in kimi_bench
    assert "(" not in kimi_bench  # no score-bearing parenthetical


@pytest.mark.parametrize(
    ("profile_name", "grade", "model_id", "score_harness", "score_effort", "expected_label"),
    [
        ("oc-kimi-k3", "S", "kimi-k3", "kimi-code-cli", "max", "Kimi K3"),
        ("oc-glm", "A+", "glm-5.2", "claude-code", "max", "GLM-5.2"),
    ],
)
def test_opencode_harness_mismatch_is_display_only_with_native_hint(
    profile_name, grade, model_id, score_harness, score_effort, expected_label
):
    from scopefuel.bench import ModelScore

    scores = [
        ModelScore(
            model_id=model_id,
            effort=score_effort,
            harness=score_harness,
            source="AA-agent",
            metric="agentic",
            score=61.0,
            rank=1,
            captured_at="2026-08-01T00:00:00+00:00",
        )
    ]
    provider_id = "clinepass"
    out = recommend(
        [_result(provider_id, 10.0, pool_class="spend")],
        grade,
        today=TODAY,
        now=NOW,
        bench_scores=scores,
    )
    line = next(line for line in out.splitlines() if profile_name in line and line[:1].isdigit())
    assert f"61.0(AA-agent/{score_harness}/{score_effort}) ⚠️ 다른 하네스 참고치" in line
    assert f"💡 {expected_label} 는 " in line
    assert "네이티브 하네스 검토 권장" in line


def test_matching_opencode_harness_has_no_warning_or_native_hint():
    from scopefuel.bench import ModelScore

    out = recommend(
        [_result("clinepass", 10.0, pool_class="spend")],
        "S",
        today=TODAY,
        now=NOW,
        bench_scores=[
            ModelScore(
                model_id="kimi-k3",
                effort="max",
                harness="opencode",
                source="AA-agent",
                metric="agentic",
                score=61.0,
                rank=1,
                captured_at="2026-08-01T00:00:00+00:00",
            )
        ],
    )
    kimi_line = next(line for line in out.splitlines() if "oc-kimi-k3" in line and line[:1].isdigit())
    assert "61.0(AA-agent/opencode/max)" in kimi_line
    assert "⚠️" not in kimi_line
    assert "💡" not in kimi_line


def test_rob1191_stale_ultra_exact_delete_on_temp_db(tmp_path, monkeypatch):
    """AC8: temp DB exact-predicate delete of luna|ultra returns changes=1; other rows intact.

    ultra is outside APPROVED_EFFORTS (cannot upsert via public API) — insert raw like the
    legacy real-DB residue, then prove delete_score_exact predicate.
    """
    import sqlite3

    from scopefuel import bench

    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setattr(bench, "DOTENV_PATH", tmp_path / "missing.env")
    path = bench.db_path()

    keep = bench.ModelScore(
        model_id="gpt-5.6-luna",
        effort="max",
        harness="codex",
        source="AA-agent",
        metric="agentic",
        score=59.0,
        rank=1,
        captured_at="2026-08-01T00:00:00+00:00",
    )
    other = bench.ModelScore(
        model_id="gpt-5.6-terra",
        effort="max",
        harness="codex",
        source="AA-agent",
        metric="agentic",
        score=62.0,
        rank=1,
        captured_at="2026-08-01T00:00:00+00:00",
    )
    assert bench.upsert_scores([keep, other], path=path) == 2
    # Legacy residue row (mirrors real bench.db content) — bypass effort allow-list.
    conn = bench.connect(path)
    try:
        conn.execute(
            "INSERT INTO model_scores "
            "(model_id, effort, harness, source, metric, score, rank, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "gpt-5.6-luna",
                "ultra",
                "codex",
                "AA-agent",
                "agentic",
                75.0,
                1,
                "2026-07-31T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(path)
    try:
        before_rows = conn.execute(
            "SELECT model_id, effort, score FROM model_scores ORDER BY model_id, effort"
        ).fetchall()
    finally:
        conn.close()
    assert ("gpt-5.6-luna", "ultra", 75.0) in before_rows
    assert len(before_rows) == 3

    deleted = bench.delete_score_exact(
        model_id="gpt-5.6-luna",
        effort="ultra",
        harness="codex",
        source="AA-agent",
        metric="agentic",
        path=path,
    )
    assert deleted == 1

    conn = sqlite3.connect(path)
    try:
        after_rows = conn.execute(
            "SELECT model_id, effort, score FROM model_scores ORDER BY model_id, effort"
        ).fetchall()
    finally:
        conn.close()
    assert ("gpt-5.6-luna", "ultra", 75.0) not in after_rows
    assert ("gpt-5.6-luna", "max", 59.0) in after_rows
    assert ("gpt-5.6-terra", "max", 62.0) in after_rows
    assert len(after_rows) == 2
    shown = bench.show_scores("gpt-5.6-luna", path=path)
    assert "ultra" not in shown
    assert "59.0" in shown
    # second delete is no-op (exact changes=0)
    assert (
        bench.delete_score_exact(
            model_id="gpt-5.6-luna",
            effort="ultra",
            harness="codex",
            source="AA-agent",
            metric="agentic",
            path=path,
        )
        == 0
    )


def test_rob1191_cli_explain_and_hide_excluded_flags(monkeypatch, capsys):
    """CLI wires --explain / --hide-excluded into recommend()."""
    from scopefuel import cli

    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {
            name: (
                lambda provider_id=name: ProviderResult(
                    id=provider_id,
                    buckets=[_bucket(10.0)],
                )
            )
            for name in ("codex", "clinepass", "grok", "claude", "kiro")
        },
    )
    policy.set_policy("kiro", "exclude", until=dt.date(2026, 9, 1), note="fold-me")
    assert cli.main(["--recommend", "S+", "--explain", "--no-cache"]) == 0
    out = capsys.readouterr().out
    assert "score=" in out and "capacity=" in out

    assert cli.main(["--recommend", "S+", "--hide-excluded", "--no-cache"]) == 0
    hidden = capsys.readouterr().out
    assert "kiro 풀 제외" not in hidden
