"""ROB-1183 — 6-tier grade recommendation: routing, ranking, exclude, urgency, gate/escalation."""

from __future__ import annotations

import datetime as dt

import pytest

from scopefuel import bench, cli, policy
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    BRAKE_KNEE_PCT,
    CODEX_SOL_XHIGH_ESCALATION_REASON,
    GRADE_BOUNDARIES,
    GRADE_DISCRIM,
    GRADE_TABLE,
    HAIKU_ESTIMATE_ANNOTATION,
    MODEL_ONLY_ANNOTATION,
    MODEL_ONLY_EXTRAPOLATED_ANNOTATION,
    MODEL_ONLY_INTERPOLATED_ANNOTATION,
    OPUS_MAX_ESCALATION_REASON,
    PRESERVE_EXCLUDE_PCT,
    PROFILE_ALIASES,
    SPEND_EXCLUDE_PCT,
    Profile,
    _brake_factor,
    _score_components,
    _select_budget,
    _select_constraint,
    _select_throughput_window,
    _window_states,
    grade_help_text,
    profile_pool,
    recommend,
    validate_grade_table,
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
    assert profile_pool("kimi-k3") == ("kimi", None)
    assert profile_pool("kimi-k3-low") == ("kimi", None)
    assert profile_pool("kimi-k27") == ("kimi", None)
    assert profile_pool("oc-gflash") == ("agy", "gemini")  # 은퇴 중이나 pool 매핑 유지
    assert profile_pool("agy-flash") == ("agy", "gemini")  # ROB-1222: 신설 프로필
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
    # ROB-1252: cc-qwen38/cc-glm run the Claude Code CLI harness against ClinePass
    # (via CLIProxyAPI translation) — same quota pool as oc-*, not the claude
    # subscription-OAuth pool.
    assert profile_pool("cc-qwen38") == ("clinepass", None)
    assert profile_pool("cc-glm") == ("clinepass", None)


# ------------------------------------------------------------------ sort/ranking


def test_recommend_s_spend_sorts_by_remaining():
    """Within spend class, higher remaining% ranks first (ROB-1182 sort)."""
    providers = [
        _result("clinepass", 17.0, window="30d"),
        _result("kimi", 10.0, pool_class="spend", window="30d"),
        _result("grok", 8.0),
        _result("codex", 46.0, pool_class="preserve"),
        _result("agy", 95.0, scope=Scope("group", "gemini"), window="5h"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    assert lines[0].startswith("1. grok-hi")
    assert "Grok" in lines[0]
    assert any("kimi-k3" in line for line in lines)


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
    assert any("kimi-k3" in line and "측정 불가" in line for line in out.splitlines())
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
    assert any("kimi-k3" in line and "측정 불가" in line for line in out.splitlines())


def test_grade_table_has_expected_a_profiles():
    names = [p.name for p in GRADE_TABLE["A"]]
    assert "kimi-k27" not in names
    assert "kimi-k27" not in [p.name for p in GRADE_TABLE["C"]]
    assert "kimi-k3-max" not in names
    assert "kimi-k3-low" in names
    assert "oc-gflash" not in names
    assert {"codex-sol", "codex-luna", "codex-terra"}.issubset(names)
    assert "oc-sonnet46" not in names


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
    # ROB-1219: an excluded pool is suppressed from the recommendation entirely.
    # The invariant that matters is that it is not offered as a candidate; the
    # decision itself is recorded in `policy list`, not repeated per grade.
    assert "claude 풀 제외" not in out
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
        _result("codex", 10.0, pool_class="spend", window="?", resets_at=_reset_in(48)),  # not urgent
    ]
    out = recommend(providers, "A+", today=TODAY, now=NOW, urgency_hours=12.0)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "🔥" in lines[0] and "kiro-sonnet" in lines[0]
    assert len(lines) >= 2
    assert "🔥" not in lines[1]


def test_multiple_urgent_sorted_by_remaining():
    """폴백 경로: 여러 후보가 모두 시간-임박 urgent 면 잔여율(큰 순)로 정렬."""
    providers = [
        _result("kiro", 80.0, pool_class="spend", window="?", resets_at=_reset_in(3)),  # 20% rem
        _result("codex", 40.0, pool_class="spend", window="?", resets_at=_reset_in(5)),  # 60% rem
    ]
    out = recommend(providers, "A+", today=TODAY, now=NOW, urgency_hours=12.0)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "🔥" in lines[0]
    assert "kiro-sonnet" not in lines[0]  # kiro is lower remaining, not first
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
    # ROB-1219: excluded pools are suppressed, not folded into a per-grade line
    assert "claude 풀 제외" not in out


# ------------------------------------------------------------------ AC2: S grade


def test_ac2_s_grade_profiles():
    """AC2: S keeps the original profiles; measured Grok high only (medium/low moved out)."""
    names = [p.name for p in GRADE_TABLE["S"]]
    assert {"codex-terra-max", "kimi-k3", "grok-hi"}.issubset(names)
    assert "grok" not in names


# ------------------------------------------------------------------ AC3: A+/A/C


def test_ac3_aplus_has_four_and_b_relocates_sonnet46_and_luna_medium():
    """AC3: A+ retains legacy profiles and adds the measured effort variants."""
    aplus_names = [p.name for p in GRADE_TABLE["A+"]]
    assert {"kiro-sonnet", "codex-luna-max", "sonnet"}.issubset(aplus_names)
    assert {"codex-terra", "codex-luna", "opus"}.issubset(aplus_names)
    assert {"agy-flash", "oc-dsflash"}.issubset(aplus_names)  # ROB-1251: AA v1.3 실측 승급
    assert "oc-minimax-m3" not in aplus_names

    a_names = [p.name for p in GRADE_TABLE["A"]]
    assert "oc-sonnet46" not in a_names

    b_profiles = GRADE_TABLE["B"]
    assert any(p.name == "codex-luna" and p.launcher_effort == "medium" for p in b_profiles)
    assert not any(p.name == "haiku" and p.launcher_effort == "low" for p in b_profiles)

    c_names = [p.name for p in GRADE_TABLE["C"]]
    assert c_names[0] == "codex-luna"
    assert not any(p.name == "codex-luna" and p.launcher_effort == "medium" for p in GRADE_TABLE["C"])
    assert any(p.name == "haiku" and p.launcher_effort == "low" for p in GRADE_TABLE["C"])
    assert "oc-sonnet46" in c_names
    assert c_names.index("oc-sonnet46") > 1
    # ROB-1251: agy-flash·oc-dsflash는 A+ 승급 — C급에 없어야 함
    assert "agy-flash" not in c_names
    assert "oc-dsflash" not in c_names


def test_rob1194_c_tier_order_and_display_metadata_are_not_rank_inputs():
    providers = [_result("codex", 10.0, pool_class="preserve")]
    measured = [
        bench.ModelScore(
            model_id="gpt-5.6-luna",
            effort="max",
            harness="codex",
            source="AA-agent",
            metric="agentic",
            score=59.0,
            rank=14,
            captured_at="2026-08-01T00:00:00+00:00",
            time_per_task_min=8.0,
        )
    ]
    unmeasured = [bench.ModelScore(**{**score.as_dict(), "time_per_task_min": None}) for score in measured]

    with_measurement = recommend(providers, "A+", today=TODAY, now=NOW, bench_scores=measured)
    without_measurement = recommend(providers, "A+", today=TODAY, now=NOW, bench_scores=unmeasured)

    def prefix(output: str) -> list[str]:
        return [line.split("벤치", 1)[0] for line in output.splitlines() if line[:1].isdigit()]

    assert prefix(with_measurement) == prefix(without_measurement)
    assert "8.0분" in with_measurement
    assert "8.0분" not in without_measurement

    c_names = [profile.model for profile in GRADE_TABLE["C"][:4]]
    assert c_names == ["Luna (low)", "Qwen3 Coder", "Sonnet 4.6", "Claude Haiku 4.5"]


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

    # ROB-1202: Grok medium/low relocated out of S (measured Grok high only stays there).
    # Both are explicit extrapolations from the high anchor; low keeps its conservative placement note.
    aplus_grok = next(p for p in GRADE_TABLE["A+"] if p.name == "grok" and p.launcher_effort == "medium")
    b_grok = next(p for p in GRADE_TABLE["B"] if p.name == "grok" and p.launcher_effort == "low")
    assert aplus_grok.benchmark == 59.4
    assert b_grok.benchmark == 52.0
    assert aplus_grok.benchmark_annotation == "추정(외삽)"
    assert b_grok.benchmark_annotation == "추정(외삽)"
    assert b_grok.placement_note and "보수 배치" in b_grok.placement_note
    assert aplus_grok.estimate_reason and "미측정" in aplus_grok.estimate_reason
    assert b_grok.estimate_reason and "미측정" in b_grok.estimate_reason

    c_profiles = GRADE_TABLE["C"]
    qwen = next(p for p in c_profiles if p.name == "oc-qwen37-max")
    minimax = next(p for p in c_profiles if p.name == "oc-minimax-m3")
    assert all(p.model_only and p.benchmark_source is None for p in (qwen, minimax))
    assert qwen.benchmark == 40.6
    assert minimax.benchmark == 34.2
    assert qwen.benchmark_annotation == MODEL_ONLY_INTERPOLATED_ANNOTATION
    assert minimax.benchmark_annotation == MODEL_ONLY_EXTRAPOLATED_ANNOTATION
    assert all(MODEL_ONLY_ANNOTATION not in (p.benchmark_annotation or "") for p in (qwen, minimax))
    assert not any(p.name == "oc-qwen37-max" for p in GRADE_TABLE["S+"])
    assert not any(p.name == "oc-qwen37-max" for p in GRADE_TABLE["S"])
    # ROB-1222: oc-gflash는 은퇴 — C급에 없어야 함
    assert not any(p.name == "oc-gflash" for p in c_profiles)
    # ROB-1251: agy-flash·oc-dsflash는 AA v1.3 실측으로 A+ 승급 — C급에 없어야 함
    assert not any(p.name == "agy-flash" for p in c_profiles)
    assert not any(p.name == "oc-dsflash" for p in c_profiles)
    # ROB-1251: agy-flash·oc-dsflash A+ 실측 배치
    aplus_gflash = next(p for p in GRADE_TABLE["A+"] if p.name == "agy-flash")
    aplus_dsflash = next(p for p in GRADE_TABLE["A+"] if p.name == "oc-dsflash")
    assert aplus_gflash.benchmark == 57.0
    assert aplus_gflash.model == "Gemini 3.7 Flash"
    assert aplus_gflash.benchmark_source == "AA-agent"
    assert not aplus_gflash.model_only
    assert aplus_dsflash.benchmark == 55.0
    assert aplus_dsflash.model == "DeepSeek V4 Flash"
    assert aplus_dsflash.benchmark_source == "AA-agent"
    assert not aplus_dsflash.model_only

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


def test_rob1201_measured_grade_boundaries_and_explicit_exceptions():
    validate_grade_table()
    assert any(
        p.name == "codex-luna" and p.launcher_effort == "medium" and p.benchmark == 42.0
        for p in GRADE_TABLE["B"]
    )
    assert not any(p.name == "codex-luna" and p.launcher_effort == "medium" for p in GRADE_TABLE["C"])
    assert any(p.name == "haiku" and p.launcher_effort == "low" for p in GRADE_TABLE["C"])
    assert not any(p.name == "haiku" and p.launcher_effort == "low" for p in GRADE_TABLE["B"])

    invalid = {grade: list(profiles) for grade, profiles in GRADE_TABLE.items()}
    invalid["B"].append(
        Profile(
            "bad-measured",
            "bad",
            39.0,
            benchmark_source="AA-agent",
            benchmark_metric="agentic",
            benchmark_harness="codex",
            benchmark_effort="medium",
        )
    )
    with pytest.raises(ValueError, match="grade boundary violation"):
        validate_grade_table(invalid)


def test_rob1212_raw_aa_model_value_cannot_pass_as_a_grade_score():
    """A model-benchmark number in an otherwise unannotated grade row is a hard failure."""
    invalid = {grade: list(profiles) for grade, profiles in GRADE_TABLE.items()}
    invalid["S+"].append(
        Profile(
            "raw-model-fixture",
            "Qwen fixture",
            66.0,
            benchmark_source="AA-model",
            benchmark_metric="coding_index",
            benchmark_model_id="qwen3-7-max",
        )
    )
    with pytest.raises(ValueError, match="raw AA-model"):
        validate_grade_table(invalid)


def test_rob1193_supplement_claude_cost_efficiency_and_estimates():
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 10.0, pool_class="preserve"),
        _result("clinepass", 10.0, window="30d"),
        _result("grok", 10.0),
    ]

    s_output = recommend(providers, "S", today=TODAY, now=NOW)
    assert "opus --effort high" in s_output
    assert "opus --effort medium" in s_output

    aplus_output = recommend(providers, "A+", today=TODAY, now=NOW)
    assert any(line[:1].isdigit() and "sonnet --effort high" in line for line in aplus_output.splitlines())
    assert "sonnet --effort xhigh" in aplus_output
    assert "opus --effort low" in aplus_output
    assert "벤치 55.0(추정(내삽))" in aplus_output
    assert not any(line[:1].isdigit() and "opus --effort low" in line for line in aplus_output.splitlines())
    # ROB-1202: Grok medium relocated here — estimated + unmeasured, not the raw high score.
    assert any(line[:1].isdigit() and "grok --effort medium" in line for line in aplus_output.splitlines())
    assert "벤치 59.4(추정(외삽))" in aplus_output

    a_output = recommend(providers, "A", today=TODAY, now=NOW)
    assert any(line[:1].isdigit() and "codex-luna --effort high" in line for line in a_output.splitlines())
    assert "codex-sol --effort low" in a_output
    assert "비용효율" in a_output
    assert not any(line[:1].isdigit() and "codex-sol --effort low" in line for line in a_output.splitlines())

    b_output = recommend(providers, "B", today=TODAY, now=NOW)
    assert "codex-luna --effort medium" in b_output
    assert "haiku --effort low" not in b_output
    # ROB-1202: Haiku high (extrapolated/unmeasured estimate) belongs to B; Grok low relocated here too.
    assert any(line[:1].isdigit() and "haiku --effort high" in line for line in b_output.splitlines())
    assert "haiku --effort medium" not in b_output
    assert any(line[:1].isdigit() and "grok --effort low" in line for line in b_output.splitlines())
    assert f"벤치 44.0({HAIKU_ESTIMATE_ANNOTATION})" in b_output

    c_output = recommend(providers, "C", today=TODAY, now=NOW)
    assert f"벤치 35.0({HAIKU_ESTIMATE_ANNOTATION})" in c_output
    assert "codex-luna --effort low" in c_output
    assert "미측정" in c_output
    assert "codex-luna --effort medium" not in c_output

    all_profiles = {profile.name for profiles in GRADE_TABLE.values() for profile in profiles}
    assert "gpt-5.4-mini" not in all_profiles


def test_rob1204_grok_estimates_are_numeric_and_low_placement_is_explicit():
    providers = [_result("grok", 10.0)]

    aplus = recommend(providers, "A+", today=TODAY, now=NOW)
    medium_line = next(line for line in aplus.splitlines() if "grok --effort medium" in line)
    assert "벤치 59.4(추정(외삽))" in medium_line
    # ROB-1244: grok-hi 가 4.6 추정으로 바뀌며 "상위 급 실측 대안" 자격을 잃었다 —
    # 추정 프로필은 승급 후보로도 A+ 에 나타나지 않아야 한다(실측만 대안 자격).
    assert "grok-hi" not in aplus

    b = recommend(providers, "B", today=TODAY, now=NOW)
    low_line = next(line for line in b.splitlines() if "grok --effort low" in line)
    assert "벤치 52.0(추정(외삽))" in low_line
    assert "보수 배치(B; 점수상 A 범위지만 추정 위의 추정)" in low_line


def test_rob1204_unscored_profile_is_last_even_with_boost(monkeypatch):
    measured = Profile(
        "codex-luna",
        "Luna (medium)",
        42.0,
        launcher_effort="medium",
        benchmark_source="AA-agent",
        benchmark_metric="agentic",
        benchmark_harness="codex",
        benchmark_effort="medium",
        benchmark_model_id="gpt-5.6-luna",
    )
    unscored = Profile("kiro-cheap", "Qwen3 Coder", None)
    monkeypatch.setitem(GRADE_TABLE, "C", [measured, unscored])
    policy.set_policy("kiro", "spend", until=dt.date(2026, 8, 31), boost=1, note="test boost")
    out = recommend(
        [
            _result("codex", 10.0, pool_class="preserve"),
            _result("kiro", 10.0, pool_class="spend", window="30d"),
        ],
        "C",
        today=TODAY,
        now=NOW,
    )
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert ranked[0].startswith("1. codex-luna")
    assert ranked[1].startswith("2. kiro-cheap")


def test_rob1204_existing_top_rank_intent_remains_for_claude_only_inputs():
    aplus = recommend([_result("claude", 10.0, pool_class="preserve")], "A+", today=TODAY, now=NOW)
    b = recommend([_result("claude", 10.0, pool_class="preserve")], "B", today=TODAY, now=NOW)
    assert next(line for line in aplus.splitlines() if line[:1].isdigit()).startswith(
        "1. sonnet --effort high"
    )
    assert next(line for line in b.splitlines() if line[:1].isdigit()).startswith("1. haiku --effort high")


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
        ModelScore(
            model_id="gemini-3-6-flash",
            effort=None,
            harness=None,
            source="AA-model",
            metric="coding_index",
            score=69.2,
            rank=1,
            captured_at="2026-08-01T00:00:00+00:00",
        ),
    ]
    providers = [
        _result("clinepass", 10.0, window="30d"),
        _result("agy", 10.0, scope=Scope("group", "gemini")),
    ]
    c_output = recommend(providers, "C", today=TODAY, now=NOW, bench_scores=scores)
    qwen_line = next(line for line in c_output.splitlines() if "oc-qwen37-max" in line and line[:1].isdigit())
    minimax_line = next(
        line for line in c_output.splitlines() if "oc-minimax-m3" in line and line[:1].isdigit()
    )
    assert "40.6(추정(내삽·harness-이식)" in qwen_line
    assert "34.2(추정(외삽·harness-이식)" in minimax_line
    assert "모델지수만 있음(에이전트 미측정)" in qwen_line
    assert "모델지수만 있음(에이전트 미측정)" in minimax_line
    assert "66.0" not in qwen_line and "58.6" not in minimax_line
    assert "46.0" not in qwen_line and "44.4" not in minimax_line
    # ROB-1251: agy-flash는 A+ 승급 — C급 출력에 없어야 함
    assert not any("agy-flash" in line and line[:1].isdigit() for line in c_output.splitlines())


# ------------------------------------------------------------------ AC4: C grade


def test_ac4_c_omni_escalation_only():
    """AC4: C 정상 순위엔 oc-omni 없음 — escalation 섹션에만 나타남. oc-oss는 ROB-1221 이후 은퇴."""
    providers = [
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "C", today=TODAY, now=NOW)
    lines = out.splitlines()
    ranked = [line for line in lines if line[:1].isdigit()]

    # kiro-cheap (유일한 정상 C 후보) 가 1위
    assert ranked[0].startswith("1. kiro-cheap")

    # oc-omni는 정상 후보에 없음 (escalation)
    assert not any(line[:1].isdigit() and "oc-omni" in line for line in lines)

    # oc-omni는 escalation 섹션에, 정상후보/제외 뒤에 위치
    escalation_idx = next(i for i, line in enumerate(lines) if "⚠ 승급 후보" in line)
    last_ranked = max((i for i, line in enumerate(lines) if line[:1].isdigit()), default=-1)
    last_excluded = max((i for i, line in enumerate(lines) if line.startswith("✗")), default=-1)
    assert escalation_idx > last_ranked
    assert escalation_idx > last_excluded

    assert "oc-omni" in out
    assert "oc-oss" not in out  # oc-oss 은퇴됨
    assert "big-pickle 162콜" in out
    assert "deepseek-v4-flash 21콜" in out


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


def test_oc_omni_escalation_at_end_of_c():
    """C 급 escalation 프로필(oc-omni)은 정상 후보/제외 뒤에 나타남. oc-oss는 ROB-1221 이후 은퇴."""
    providers = [
        _result("kiro", 10.0, pool_class="spend", window="30d"),
        _result("agy", 10.0, scope=Scope("group", "3p")),
    ]
    out = recommend(providers, "C", today=TODAY, now=NOW)
    lines = out.splitlines()

    # oc-sonnet46 이 1위 (정상 후보)
    ranked = [line for line in lines if line[:1].isdigit()]
    assert ranked[0].startswith("1. oc-sonnet46")

    # oc-omni 는 escalation 섹션에, 정상후보/제외 뒤에 위치
    escalation_idx = next(i for i, line in enumerate(lines) if "⚠ 승급 후보" in line)
    last_ranked = max((i for i, line in enumerate(lines) if line[:1].isdigit()), default=-1)
    last_excluded = max((i for i, line in enumerate(lines) if line.startswith("✗")), default=-1)
    last_content = max(last_ranked, last_excluded)
    assert escalation_idx > last_content
    assert any("oc-omni" in line for line in lines[escalation_idx:])
    assert "oc-oss" not in out  # oc-oss 은퇴됨


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
        _result("kimi", 10.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    # boost 만료 → 일반 정렬(잔여율 큰 순): clinepass(90%) 가 codex(50%) 보다 우선.
    assert "kimi-k3" in ranked[0]


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
        _result("kimi", 10.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    # boost 무시 → 잔여율 정렬: clinepass 가 codex 보다 우선.
    assert "kimi-k3" in ranked[0]


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
            "kimi",
            98.5,  # 잔여 1.5% -> 조정된 임계 1% 이상이면 유의미
            pool_class="spend",
            window="7d",
            resets_at=_reset_in(2.5),  # 조정된 임계 3h 이내
        ),
    ]
    out = recommend(providers, "S", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "kimi-k3" in ranked[0]
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


def _result_windows_with_resets(
    provider_id: str,
    windows: list[tuple[float, str, float]],
    pool_class: str = "spend",
) -> ProviderResult:
    """Build account windows as (used_pct, window, hours_until_reset)."""
    buckets = []
    for used, window, hours in windows:
        horizon = "now" if window == "5h" else "week"
        buckets.append(
            _bucket(
                used,
                window=window,
                horizon=horizon,
                resets_at=_reset_in(hours),
            )
        )
    return ProviderResult(
        id=provider_id,
        pool_class=pool_class,  # type: ignore[arg-type]
        buckets=buckets,
    )


def test_rob1210_budget_is_longest_and_unknown_duration_wins():
    """Budget selection uses window duration, with an unparseable duration as longest."""
    states = _window_states(
        [
            (10.0, "5h", _reset_in(0.5)),
            (40.0, "7d", _reset_in(68.0)),
        ],
        NOW,
    )
    assert _select_constraint(states).window == "5h"
    assert _select_budget(states).window == "7d"

    unknown = _window_states(
        [
            (10.0, "7d", _reset_in(68.0)),
            (20.0, "week", _reset_in(68.0)),
        ],
        NOW,
    )
    assert _select_budget(unknown).window == "week"


def test_rob1210_budget_window_drives_capacity_waste_and_explain():
    """A 5h constraint cannot make capacity/waste use the short window."""
    out = recommend(
        [
            _result_windows_with_resets(
                "claude",
                [(10.0, "5h", 0.5), (40.0, "7d", 68.0)],
            )
        ],
        "S+",
        today=TODAY,
        now=NOW,
        explain=True,
    )
    opus_line = next(line for line in out.splitlines() if line[:1].isdigit() and "opus" in line)
    explain = next(line for line in out.splitlines() if line.strip().startswith("score="))

    assert "5h 10%" in opus_line and "주 40%" in opus_line
    assert "제약=5h" in opus_line
    assert "잔여 60%" in opus_line  # budget=7d, not constraint=5h's 90%
    assert "🔥" in opus_line  # budget-window pace is urgent
    assert "capacity=60.00=w1×rem60 (budget=주)" in explain
    assert "waste×50=32.80 (budget=주)" in explain
    assert "thru×0.25=90.00 (short=5h)" in explain
    assert "제약=5h" in explain


def test_rob1210_multi_window_ranks_by_weekly_waste_against_weekly_only_pool():
    """The short-window waste illusion no longer outranks a higher weekly budget waste."""
    out = recommend(
        [
            # The old constraint-based score would count almost all 5h remainder as waste.
            _result_windows_with_resets(
                "claude",
                [(10.0, "5h", 0.5), (40.0, "7d", 68.0)],
            ),
            # Weekly-only control: 80% remains and the slow pace leaves 66.4% at risk.
            _result_windows_with_resets("grok", [(20.0, "7d", 68.0)]),
        ],
        "S",
        today=TODAY,
        now=NOW,
        explain=True,
    )
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert ranked[0].find("grok-hi") >= 0
    assert ranked[1].find("opus") >= 0
    grok_explain = next(
        line for line in out.splitlines() if line.strip().startswith("score=") and "66.40" in line
    )
    claude_explain = next(
        line for line in out.splitlines() if line.strip().startswith("score=") and "32.80" in line
    )
    assert "waste×50=66.40 (budget=주)" in grok_explain
    assert "waste×50=32.80 (budget=주)" in claude_explain


def test_rob1210_imminent_exhaustion_uses_budget_window():
    """A 5h reset within an hour is not imminent if the weekly budget is safe."""
    safe_week = recommend(
        [
            _result_windows_with_resets(
                "claude",
                [(10.0, "5h", 0.5), (40.0, "7d", 68.0)],
            )
        ],
        "S+",
        today=TODAY,
        now=NOW,
    )
    safe_line = next(line for line in safe_week.splitlines() if line[:1].isdigit() and "opus" in line)
    assert "🔥🔥" not in safe_line
    assert "소멸 임박 우선" not in safe_line

    urgent_week = recommend(
        [
            _result_windows_with_resets(
                "claude",
                [(10.0, "5h", 4.9), (40.0, "7d", 0.5)],
            )
        ],
        "S+",
        today=TODAY,
        now=NOW,
    )
    urgent_line = next(line for line in urgent_week.splitlines() if line[:1].isdigit() and "opus" in line)
    assert "🔥🔥" in urgent_line
    assert "소멸 임박 우선" in urgent_line
    assert "잔여 60%" in urgent_line


def test_rob1210_urgency_uses_budget_window_pace():
    """A pace-urgent 5h window does not add 🔥 when the weekly budget pace is safe."""
    out = recommend(
        [
            _result_windows_with_resets(
                "claude",
                [(50.0, "5h", 0.5), (60.0, "7d", 68.0)],
            )
        ],
        "S+",
        today=TODAY,
        now=NOW,
    )
    opus_line = next(line for line in out.splitlines() if line[:1].isdigit() and "opus" in line)
    assert "🔥" not in opus_line


def test_rob1210_short_window_cutoff_remains_a_brake():
    """A 5h spend cutoff still excludes the pool even when its weekly budget is low-use."""
    out = recommend(
        [
            _result_windows_with_resets(
                "claude",
                [(99.0, "5h", 0.5), (10.0, "7d", 68.0)],
            )
        ],
        "S+",
        today=TODAY,
        now=NOW,
    )
    assert not any(line[:1].isdigit() and "opus" in line for line in out.splitlines())
    assert any("claude 풀 소진" in line and "99%" in line for line in out.splitlines())


def test_rob1210_throughput_stays_on_shortest_window():
    states = _window_states(
        [
            (80.0, "5h", _reset_in(0.5)),
            (20.0, "7d", _reset_in(68.0)),
        ],
        NOW,
    )
    constraint = _select_constraint(states)
    budget = _select_budget(states)
    assert budget.window == "7d"
    assert _select_throughput_window(states, constraint).window == "5h"
    capacity, waste, throughput, _score = _score_components(
        states=states,
        constraint=constraint,
        budget=budget,
        weight=1.0,
        pool_class="spend",
        urgency_hours=12.0,
    )
    assert capacity == pytest.approx(80.0)
    assert throughput == pytest.approx(20.0)
    assert waste == pytest.approx(66.4)


@pytest.mark.parametrize("provider_id", ["grok", "codex"])
def test_rob1210_single_window_score_is_numerically_unchanged(provider_id):
    """For a single window, budget and constraint are the same score source."""
    states = _window_states([(10.0, "7d", _reset_almost_full("7d"))], NOW)
    constraint = _select_constraint(states)
    budget = _select_budget(states)
    new_score = _score_components(
        states=states,
        constraint=constraint,
        budget=budget,
        weight=1.0,
        pool_class="spend",
        urgency_hours=12.0,
    )

    # ROB-1210's before-value: the legacy formula used the sole constraint window.
    legacy_capacity = constraint.remaining_pct
    legacy_waste = max(0.0, constraint.remaining_pct - constraint.burn_rate * constraint.hours_to_reset)
    legacy_throughput = constraint.remaining_pct
    legacy_score = (
        legacy_capacity,
        legacy_waste,
        legacy_throughput,
        legacy_capacity + 50.0 * legacy_waste + 0.25 * legacy_throughput,
    )
    assert budget is constraint
    assert new_score == pytest.approx(legacy_score)


def test_rob1210_brake_is_exactly_short_remaining_over_knee():
    """5h 70% used leaves a 0.60 brake on the full budget score."""
    states = _window_states(
        [
            (70.0, "5h", _reset_in(0.5)),
            (10.0, "7d", _reset_in(68.0)),
        ],
        NOW,
    )
    constraint = _select_constraint(states)
    budget = _select_budget(states)
    capacity, waste, throughput, score = _score_components(
        states=states,
        constraint=constraint,
        budget=budget,
        weight=1.0,
        pool_class="spend",
        urgency_hours=12.0,
    )
    brake, brake_window = _brake_factor(states)
    base_score = capacity + 50.0 * waste + 0.25 * throughput

    assert BRAKE_KNEE_PCT == 50.0
    assert brake_window is not None and brake_window.window == "5h"
    assert brake == pytest.approx(0.6)
    assert base_score == pytest.approx(4257.5)
    assert score == pytest.approx(2554.5)
    assert score == pytest.approx(base_score * 0.6)


def test_rob1210_knee_or_more_short_remaining_has_no_score_penalty():
    """At the knee (50% remaining), the r1 score is unchanged."""
    states = _window_states(
        [
            (50.0, "5h", _reset_in(0.5)),
            (10.0, "7d", _reset_in(68.0)),
        ],
        NOW,
    )
    constraint = _select_constraint(states)
    budget = _select_budget(states)
    capacity, waste, throughput, score = _score_components(
        states=states,
        constraint=constraint,
        budget=budget,
        weight=1.0,
        pool_class="spend",
        urgency_hours=12.0,
    )
    brake, _brake_window = _brake_factor(states)
    base_score = capacity + 50.0 * waste + 0.25 * throughput
    assert brake == pytest.approx(1.0)
    assert score == pytest.approx(base_score)


def test_rob1210_brake_reverses_rank_against_weekly_only_pool():
    """A progressing 5h depletion moves the braked multi-window pool below weekly-only."""
    out = recommend(
        [
            _result_windows_with_resets(
                "claude",
                [(70.0, "5h", 0.5), (10.0, "7d", 68.0)],
            ),
            _result_windows_with_resets("grok", [(20.0, "7d", 68.0)]),
        ],
        "S",
        today=TODAY,
        now=NOW,
        explain=True,
    )
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert ranked[0].find("grok-hi") >= 0
    assert ranked[1].find("opus") >= 0
    assert "score=3420.00" in out
    assert "score=2554.50" in out
    assert "× brake=0.60 (5h 잔여 30% < knee 50%)" in out


@pytest.mark.parametrize("provider_id", ["grok", "codex"])
def test_rob1210_no_short_window_brake_is_one(provider_id):
    """Grok/Codex-shaped long-only pools retain the r1 numeric score."""
    states = _window_states([(20.0, "7d", _reset_in(68.0))], NOW)
    constraint = _select_constraint(states)
    budget = _select_budget(states)
    score = _score_components(
        states=states,
        constraint=constraint,
        budget=budget,
        weight=1.0,
        pool_class="spend",
        urgency_hours=12.0,
    )[-1]
    brake, brake_window = _brake_factor(states)
    assert brake == pytest.approx(1.0)
    assert brake_window is None
    assert score == pytest.approx(3420.0)


@pytest.mark.parametrize(
    "pool_class, cutoff", [("spend", SPEND_EXCLUDE_PCT), ("preserve", PRESERVE_EXCLUDE_PCT)]
)
def test_rob1210_cutoff_still_excludes_at_existing_threshold(pool_class, cutoff):
    """The multiplicative brake never changes the final 99%/90% cutoff."""
    out = recommend(
        [
            _result_windows_with_resets(
                "claude",
                [(cutoff, "5h", 0.5), (10.0, "7d", 68.0)],
                pool_class=pool_class,
            )
        ],
        "S+",
        today=TODAY,
        now=NOW,
    )
    assert not any(line[:1].isdigit() and "opus" in line for line in out.splitlines())
    assert any("claude 풀 소진" in line and f"{cutoff:g}%" in line for line in out.splitlines())


def test_rob1210_explain_only_shows_brake_when_below_knee():
    """The explain line names the brake source below knee and stays quiet at brake=1."""
    braked = recommend(
        [
            _result_windows_with_resets(
                "claude",
                [(70.0, "5h", 0.5), (10.0, "7d", 68.0)],
            )
        ],
        "S",
        today=TODAY,
        now=NOW,
        explain=True,
    )
    flat = recommend(
        [_result_windows_with_resets("grok", [(20.0, "7d", 68.0)])],
        "S",
        today=TODAY,
        now=NOW,
        explain=True,
    )
    braked_explain = next(line for line in braked.splitlines() if line.strip().startswith("score="))
    flat_explain = next(line for line in flat.splitlines() if line.strip().startswith("score="))
    assert "× brake=0.60 (5h 잔여 30% < knee 50%)" in braked_explain
    assert "brake" not in flat_explain


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

    # Kimi at 10% rem90 ranks above grok at 40% rem60 (capacity term).
    before = recommend(
        [
            _result("grok", 40.0),
            _result("clinepass", 10.0, window="30d"),
            _result("kimi", 10.0, pool_class="spend", window="30d"),
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
            _result("kimi", 85.0, pool_class="spend", window="30d"),  # deplete former #1 pool
            _result("codex", 70.0, pool_class="preserve"),
        ],
        "S",
        today=TODAY,
        now=NOW,
    )
    before_names = [_name(line) for line in before.splitlines() if line[:1].isdigit()]
    after_names = [_name(line) for line in after.splitlines() if line[:1].isdigit()]
    assert before_names[0] == "kimi-k3"
    assert before_names.index("kimi-k3") < before_names.index("grok-hi")
    # After depleting clinepass/Kimi remaining, grok-hi becomes #1
    assert after_names[0] == "grok-hi"
    assert after_names.index("grok-hi") < after_names.index("kimi-k3")


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
    assert not ranked[0].startswith("1. kimi-k3")


def test_rob1219_excluded_pool_is_absent_from_recommendation():
    """ROB-1219 (supersedes ROB-1191 AC6): an excluded pool is suppressed entirely.

    ROB-1191 folded same-grade kiro excludes into one "✗ kiro 풀 제외" line per
    grade. Operator judgment 2026-08-06: that line is noise — `class = exclude`
    is a decision already made, repeated in every grade. The record lives in
    `policy list` (class/until/note) and GRADE_TABLE, so restoring the pool stays
    a one-command `policy clear`. The emergency-candidate path (no usable
    candidate in the grade) is unaffected and still surfaces the pool.
    """
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

    # no exclusion line, and no per-profile leakage either
    assert "kiro 풀 제외" not in shown
    assert "kiro" not in shown
    assert "kiro" not in hidden
    # the usable candidates are still there — suppression must not empty the grade
    assert "codex" in shown or "opus" in shown


def test_rob1219_excluded_pool_still_surfaces_when_grade_has_no_candidate():
    """Suppression is display-only: with nothing usable, the pool must reappear."""
    policy.set_policy(
        "kiro",
        "exclude",
        until=dt.date(2026, 9, 1),
        note="무료 전환(구독 종료 2026-08-01)",
    )
    # kiro is the only provider that reports at all -> grade has no usable candidate
    providers = [_result("kiro", 10.0, pool_class="spend", window="30d")]
    shown = recommend(providers, "S+", today=TODAY, now=NOW, hide_excluded=False)

    assert "정책 가용 후보 없음" in shown
    assert "비상 후보" in shown
    assert "kiro" in shown


def test_rob1191_one_effort_bench_cells_and_kimi_default_is_exact():
    """AC7: declared effort cells select one row; Kimi's CLI default is explicit."""
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
        ModelScore(
            model_id="kimi-k3",
            effort="default",
            harness="kimi-code-cli",
            source="AA-agent",
            metric="agentic",
            score=61.0,
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
            _result("kimi", 10.0, pool_class="spend"),
        ],
        "S",
        today=TODAY,
        now=NOW,
        bench_scores=scores,
    )

    codex_line = next(line for line in sp.splitlines() if "codex-sol" in line and line[:1].isdigit())
    opus_line = next(line for line in sp.splitlines() if line[:1].isdigit() and "opus" in line.split())
    kimi_line = next(line for line in s.splitlines() if "kimi-k3" in line and line[:1].isdigit())

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

    # Kimi's measured CLI default is a single declared cell, with no effort suffix.
    assert kimi_bench == "61.0(AA-agent/kimi-code-cli)"
    assert "57.0" not in kimi_line
    assert "50.0" not in kimi_line
    assert "대표" not in kimi_line
    assert "추정" not in kimi_line


@pytest.mark.parametrize(
    ("profile_name", "grade", "model_id", "score_harness", "score_effort", "score_value", "expected_label"),
    [
        ("oc-glm", "B", "glm-5.2", "claude-code", "default", 43.0, "GLM-5.2"),
    ],
)
def test_opencode_harness_mismatch_is_display_only_with_native_hint(
    profile_name, grade, model_id, score_harness, score_effort, score_value, expected_label
):
    from scopefuel.bench import ModelScore

    scores = [
        ModelScore(
            model_id=model_id,
            effort=score_effort,
            harness=score_harness,
            source="AA-agent",
            metric="agentic",
            score=score_value,
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
    effort_suffix = f"/{score_effort}" if score_effort != "default" else ""
    assert f"{score_value:.1f}(AA-agent/{score_harness}{effort_suffix}) ⚠️ harness-이식 추정" in line
    assert f"💡 {expected_label} 는 " in line
    assert "네이티브 하네스 검토 권장" in line


def test_oc_sonnet46_shows_harness_transfer_estimate_annotation():
    """ROB-1202 item 4 acceptance example: oc-sonnet46 cross-harness AA-agent score is marked
    harness-이식 추정 without altering rank/scoring."""
    from scopefuel.bench import ModelScore

    scores = [
        ModelScore(
            model_id="claude-sonnet-4.6",
            effort="medium",
            harness="claude-code",
            source="AA-agent",
            metric="agentic",
            score=38.0,
            rank=1,
            captured_at="2026-08-01T00:00:00+00:00",
        )
    ]
    out = recommend(
        [_result("agy", 10.0, pool_class="spend", scope=Scope("group", "3p"))],
        "C",
        today=TODAY,
        now=NOW,
        bench_scores=scores,
    )
    line = next(line for line in out.splitlines() if "oc-sonnet46" in line and line[:1].isdigit())
    assert "38.0(AA-agent/claude-code/medium) ⚠️ harness-이식 추정" in line
    assert "💡 Sonnet 4.6 는 Claude Code 에서 측정됨" in line
    assert "네이티브 하네스 검토 권장" in line


@pytest.mark.parametrize(
    ("grade", "effort", "score", "expected_label"),
    [
        ("A+", "medium", 55.5, "grok --effort medium"),
        ("B", "low", 41.0, "grok --effort low"),
    ],
)
def test_grok_live_aa_agent_match_suppresses_stale_estimate_annotation_and_reason(
    grade, effort, score, expected_label
):
    """ROB-1202 rework r1 (BLOCKER fix): once a real AA-agent measurement exists for Grok
    medium/low, the static 추정(외삽·미측정) annotation and its --explain reason must not
    render alongside the real score — otherwise the output contradicts itself."""
    from scopefuel.bench import ModelScore

    live_scores = [
        ModelScore(
            model_id="grok-4.6",
            effort=effort,
            harness="grok-build",
            source="AA-agent",
            metric="agentic",
            score=score,
            rank=1,
            captured_at="2026-08-01T00:00:00+00:00",
        )
    ]
    providers = [_result("grok", 10.0)]

    without_measurement = recommend(providers, grade, today=TODAY, now=NOW, explain=True)
    with_measurement = recommend(
        providers, grade, today=TODAY, now=NOW, bench_scores=live_scores, explain=True
    )

    # Before a live measurement: static estimate annotation + explain reason are shown.
    stale_line = next(line for line in without_measurement.splitlines() if expected_label in line)
    assert "추정(외삽)" in stale_line
    stale_block = "\n".join(without_measurement.splitlines())
    from scopefuel.recommend import GROK_LOW_ESTIMATE_REASON, GROK_MEDIUM_ESTIMATE_REASON

    expected_reason = GROK_MEDIUM_ESTIMATE_REASON if effort == "medium" else GROK_LOW_ESTIMATE_REASON
    assert f"추정근거: {expected_reason}" in stale_block

    # After a live measurement: real score shown, stale annotation/reason both gone.
    live_line = next(
        line for line in with_measurement.splitlines() if expected_label in line and line[:1].isdigit()
    )
    assert f"{score:.1f}(AA-agent/grok-build/{effort})" in live_line
    assert "추정" not in live_line
    assert "미측정" not in live_line
    # Grok's own stale reason must be gone; other unrelated candidates' reasons (if any) are untouched.
    assert f"추정근거: {expected_reason}" not in with_measurement


def test_matching_kimi_harness_has_no_warning_or_native_hint():
    from scopefuel.bench import ModelScore

    out = recommend(
        [_result("kimi", 10.0, pool_class="spend")],
        "S",
        today=TODAY,
        now=NOW,
        bench_scores=[
            ModelScore(
                model_id="kimi-k3",
                effort="default",
                harness="kimi-code-cli",
                source="AA-agent",
                metric="agentic",
                score=61.0,
                rank=1,
                captured_at="2026-08-01T00:00:00+00:00",
            )
        ],
    )
    kimi_line = next(line for line in out.splitlines() if "kimi-k3" in line and line[:1].isdigit())
    assert "61.0(AA-agent/kimi-code-cli)" in kimi_line
    assert "⚠️" not in kimi_line
    assert "💡" not in kimi_line


@pytest.mark.parametrize(
    ("profile_name", "grade", "model_id", "effort", "score", "expected_render"),
    [
        ("cc-qwen38", "A+", "qwen3.8-max", "default", 57.0, "57.0(AA-agent/claude-code)"),
        ("cc-glm", "B", "glm-5.2", "default", 43.0, "43.0(AA-agent/claude-code)"),
    ],
)
def test_cc_profiles_harness_matched_have_no_warning_or_native_hint(
    profile_name, grade, model_id, effort, score, expected_render
):
    """ROB-1252: cc-qwen38/cc-glm run the AA-agent-measured harness (claude-code)
    natively — unlike oc-glm/oc-sonnet46 (opencode-hosted harness-이식), no
    cross-harness warning or native-hint should render for these profiles."""
    from scopefuel.bench import ModelScore

    out = recommend(
        [_result("clinepass", 10.0, pool_class="spend")],
        grade,
        today=TODAY,
        now=NOW,
        bench_scores=[
            ModelScore(
                model_id=model_id,
                effort=effort,
                harness="claude-code",
                source="AA-agent",
                metric="agentic",
                score=score,
                rank=1,
                captured_at="2026-08-01T00:00:00+00:00",
            )
        ],
    )
    line = next(line for line in out.splitlines() if profile_name in line and line[:1].isdigit())
    assert expected_render in line
    assert "⚠️" not in line
    assert "💡" not in line
    assert "harness-이식" not in line


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


def test_cross_grade_alternatives_are_adjacent_grade_only():
    """ROB-1218: the measured-alternative rule spans exactly one grade, never further.

    Unbounded it degenerated: C-grade work (haiku low, est. 35) was offered opus
    xhigh (S+, measured 67) and kimi-k3 (S, 61) as "alternatives" — a 26-32 point
    jump no C task wants. The near-miss case it exists for (A+ grok medium
    estimated -> S grok-hi measured) is one grade up by construction.
    """
    from scopefuel.recommend import (
        _GRADE_ORDER,
        GRADE_TABLE,
        _cross_grade_measured_alternatives,
        profile_pool,
    )

    for grade in _GRADE_ORDER:
        grade_index = _GRADE_ORDER.index(grade)
        if grade_index == 0:
            assert _cross_grade_measured_alternatives(grade) == []
            continue
        adjacent = {p.name for p in GRADE_TABLE[_GRADE_ORDER[grade_index - 1]]}
        for profile, reason in _cross_grade_measured_alternatives(grade):
            assert profile.name in adjacent, (
                f"{grade}: {profile.name} is not from the adjacent grade {_GRADE_ORDER[grade_index - 1]}"
            )
            # the alternative must share a pool with an estimated same-grade entry
            assert profile_pool(profile.name)[0] in {profile_pool(p.name)[0] for p in GRADE_TABLE[grade]}, (
                f"{grade}: {profile.name} shares no pool with this grade"
            )
            assert "실측" in reason


def test_c_grade_has_no_top_tier_escalation():
    """ROB-1218 regression: no S/S+ profile may surface as a C-grade alternative."""
    from scopefuel.recommend import _GRADE_ORDER, GRADE_TABLE, _cross_grade_measured_alternatives

    top_tier = {p.name for g in _GRADE_ORDER[:3] for p in GRADE_TABLE[g]}
    offered = {p.name for p, _ in _cross_grade_measured_alternatives("C")}
    assert not (offered & top_tier), f"C grade offered top-tier profiles: {offered & top_tier}"
