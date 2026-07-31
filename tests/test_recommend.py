"""ROB-1183 — 6-tier grade recommendation: routing, ranking, exclude, urgency, gate/escalation."""

from __future__ import annotations

import datetime as dt

import pytest

from scopefuel import cli, policy
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    GRADE_DISCRIM,
    GRADE_TABLE,
    PRESERVE_EXCLUDE_PCT,
    SPEND_EXCLUDE_PCT,
    grade_help_text,
    profile_pool,
    recommend,
)

TODAY = dt.date(2026, 7, 31)
NOW = dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=dt.UTC)
RESET = (dt.datetime(2026, 8, 1, 5, tzinfo=dt.UTC)).isoformat()  # ~17h later → not urgent

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
    assert profile_pool("codex-luna-ultra") == ("codex", None)
    assert profile_pool("kiro-sol") == ("kiro", None)
    assert profile_pool("kiro-haiku") == ("kiro", None)
    assert profile_pool("oc-omni") == ("omniroute", None)


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
    assert any(line.startswith("2. oc-kimi-k3") for line in lines)


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
    assert names == ["oc-gflash", "oc-kimi-code", "oc-sonnet46"]


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
    # kiro-opus/kiro-sol spend, codex-max preserve → kiro first
    assert any("kiro-opus" in line for line in ranked)
    assert any("codex-max" in line for line in ranked)
    # opus is default gate but policy-excluded
    assert any(line.startswith("✗ opus") and "정책 제외" in line for line in lines)
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
    assert any("codex-max" in line and "소진" in line for line in out.splitlines())


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
    providers = [
        _result("kiro", 50.0, pool_class="spend", window="30d", resets_at=_reset_in(13)),
    ]
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert ranked
    assert "🔥" not in ranked[0]


def test_urgent_spend_outranks_non_urgent_higher_remaining():
    """Reset-imminent spend ranks above non-imminent spend even with lower remaining%."""
    providers = [
        _result("kiro", 80.0, pool_class="spend", window="30d", resets_at=_reset_in(4)),  # 20% rem, urgent
        _result("clinepass", 10.0, window="30d", resets_at=_reset_in(48)),  # 90% rem, not urgent
    ]
    out = recommend(providers, "A+", today=TODAY, now=NOW)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "🔥" in lines[0] and "kiro-sonnet" in lines[0]
    assert "oc-glm" in lines[1]


def test_multiple_urgent_sorted_by_remaining():
    providers = [
        _result("kiro", 80.0, pool_class="spend", window="30d", resets_at=_reset_in(3)),  # 20% rem
        _result("clinepass", 40.0, window="30d", resets_at=_reset_in(5)),  # 60% rem
    ]
    out = recommend(providers, "A+", today=TODAY, now=NOW)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    assert "🔥" in lines[0] and "oc-glm" in lines[0]
    assert "🔥" in lines[1] and "kiro-sonnet" in lines[1]


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
    out = recommend(providers, "S+", today=TODAY, now=NOW)
    lines = [line for line in out.splitlines() if line[:1].isdigit()]
    # spend before preserve
    assert "kiro" in lines[0]
    assert "🔥" not in out  # default RESET is ~17h away
    assert "정책 제외" not in out
    assert "비상 후보" not in out


# ------------------------------------------------------------------ AC1: S+


def test_ac1_sp_recommend_with_claude_exclude():
    """AC1: --recommend S+ shows kiro-opus/codex-max as normal, fable in escalation+reason+exclude."""
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
    # kiro-opus and codex-max are normal candidates
    assert any("kiro-opus" in line for line in ranked)
    assert any("codex-max" in line for line in ranked)

    # codex Sol recommendation row is exactly 1 (codex-max only, no codex-ultra)
    codex_sol_lines = [line for line in ranked if "codex-max" in line or "codex-ultra" in line]
    assert len(codex_sol_lines) == 1
    assert "codex-max" in codex_sol_lines[0]
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

    # opus is default-gate, policy-excluded → shows as ✗ not in escalation
    assert any(line.startswith("✗ opus") and "정책 제외" in line for line in lines)


# ------------------------------------------------------------------ AC2: S grade


def test_ac2_s_grade_profiles():
    """AC2: S grade has exactly codex-terra-max, oc-kimi-k3, grok-hi."""
    names = [p.name for p in GRADE_TABLE["S"]]
    assert names == ["codex-terra-max", "oc-kimi-k3", "grok-hi"]


# ------------------------------------------------------------------ AC3: A+/A/C


def test_ac3_aplus_has_four_and_a_has_sonnet46_and_c_no_sonnet46():
    """AC3: A+ has exactly 4 profiles, A includes oc-sonnet46, C does not."""
    aplus_names = [p.name for p in GRADE_TABLE["A+"]]
    assert aplus_names == ["kiro-sonnet", "codex-luna-ultra", "oc-glm", "sonnet"]
    assert len(aplus_names) == 4

    a_names = [p.name for p in GRADE_TABLE["A"]]
    assert "oc-sonnet46" in a_names

    c_names = [p.name for p in GRADE_TABLE["C"]]
    assert "oc-sonnet46" not in c_names


# ------------------------------------------------------------------ AC4: C grade


def test_ac4_c_omni_first_and_oss_escalation():
    """AC4: C has oc-omni as quota-0 1st priority, oc-oss in escalation section at end."""
    providers = [
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    out = recommend(providers, "C", today=TODAY, now=NOW)
    lines = out.splitlines()
    ranked = [line for line in lines if line[:1].isdigit()]

    # oc-omni is always #1 (quota-0, 100% remaining, spend)
    assert ranked[0].startswith("1. oc-omni")
    assert "OmniRoute" in ranked[0]

    # oc-oss is NOT in normal candidates (it's escalation)
    assert not any(line[:1].isdigit() and "oc-oss" in line for line in lines)

    # oc-oss is in escalation section with reason, at the end
    escalation_idx = None
    oss_idx = None
    for i, line in enumerate(lines):
        if "⚠ 승급 후보" in line:
            escalation_idx = i
        if "oc-oss" in line and escalation_idx is not None and i > escalation_idx:
            oss_idx = i
            break
    assert oss_idx is not None
    assert "agy 3p 풀 소모" in out

    # escalation section is after ranked candidates and excluded
    assert escalation_idx is not None
    last_ranked = max((i for i, line in enumerate(lines) if line[:1].isdigit()), default=-1)
    last_excluded = max((i for i, line in enumerate(lines) if line.startswith("✗")), default=-1)
    assert escalation_idx > last_ranked
    assert escalation_idx > last_excluded


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
    assert any(line[:1].isdigit() and "codex-max" in line for line in out.splitlines())
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
    assert any("codex-max" in line and "소진" in line for line in out.splitlines())
    assert not any(line[:1].isdigit() and "codex-max" in line for line in out.splitlines())


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
    # fable pool is available → status shows usage
    assert any("사용 10%" in line for line in lines)


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

    # oc-omni is #1 ranked
    ranked = [line for line in lines if line[:1].isdigit()]
    assert ranked[0].startswith("1. oc-omni")

    # oc-oss is in escalation section, after all ranked and excluded
    escalation_idx = next(i for i, line in enumerate(lines) if "⚠ 승급 후보" in line)
    last_ranked = max((i for i, line in enumerate(lines) if line[:1].isdigit()), default=-1)
    last_excluded = max((i for i, line in enumerate(lines) if line.startswith("✗")), default=-1)
    last_content = max(last_ranked, last_excluded)
    assert escalation_idx > last_content
    assert any("oc-oss" in line for line in lines[escalation_idx:])
    assert "agy 3p 풀 소모" in out


def test_oc_omni_always_available_without_provider():
    """oc-omni is quota-0 free — available even when no OmniRoute provider is registered."""
    providers: list[ProviderResult] = []
    out = recommend(providers, "C", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert ranked[0].startswith("1. oc-omni")
    assert "OmniRoute" in ranked[0]


def test_oc_omni_ranks_above_urgent_spend():
    """oc-omni (quota-0) must rank #1 even when a spend pool is about to reset."""
    providers = [
        _result("kiro", 95.0, pool_class="spend", window="30d", resets_at=_reset_in(3)),
    ]
    out = recommend(providers, "C", today=TODAY, now=NOW)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert ranked[0].startswith("1. oc-omni")
    # kiro-cheap is urgent but still #2
    assert "kiro-cheap" in ranked[1]
    assert "🔥" in ranked[1]


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
    assert len(ranked) >= 3  # kiro-opus, kiro-sol, codex-max, opus
    # Escalation section is separate
    assert "⚠ 승급 후보" in out
