"""ROB-1184 — `scopefuel gate -m <profile>`: spawn go/no-go 판정 (exit 0/3/4)."""

from __future__ import annotations

import datetime as dt

import pytest

from scopefuel import cli, policy
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import gate_check

TODAY = dt.date(2026, 7, 31)
NOW = dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=dt.UTC)


def _reset_almost_full(window: str) -> str:
    hours = {"5h": 4.9, "1d": 23.5, "7d": 167.0, "30d": 719.0}.get(window, 167.0)
    return (NOW + dt.timedelta(hours=hours)).isoformat()


def _result(
    provider_id: str,
    used: float,
    pool_class: str = "spend",
    scope: Scope | None = None,
    window: str = "7d",
) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class=pool_class,  # type: ignore[arg-type]
        buckets=[
            Bucket(
                label=window,
                window=window,
                used_pct=used,
                resets_at=_reset_almost_full(window),
                scope=scope or Scope("account"),
                horizon="week",  # type: ignore[arg-type]
            )
        ],
    )


# ------------------------------------------------------------------ exit 0 (ok)


def test_gate_ok_returns_exit_0_with_pool_and_usage():
    providers = [_result("codex", 10.0, pool_class="preserve")]
    result = gate_check(providers, "codex-max", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.provider_id == "codex"
    assert result.grade == "S+"
    assert result.used_pct == 10.0
    assert result.pool_class == "preserve"


def test_gate_cli_ok_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"codex": lambda: ProviderResult(id="codex", pool_class="preserve", buckets=[])},
    )
    # codex-max needs a usable bucket; make one directly via fetch stub.
    providers_result = _result("codex", 10.0, pool_class="preserve")
    monkeypatch.setattr(cli, "registry", lambda: {"codex": lambda: providers_result})
    rc = cli.main(["gate", "-m", "codex-max", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    assert "codex-max" in out.out
    assert "class=preserve" in out.out


# ------------------------------------------------------------------ exit 3 (blocked)


def test_gate_blocked_by_policy_exclude():
    policy.set_policy("claude", "exclude", until=dt.date(2026, 8, 31), note="Pro 요금제")
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    result = gate_check(providers, "opus", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is False
    assert "정책 제외" in result.reason
    assert "until 2026-08-31" in result.reason
    # 동일 grade(S+)의 가용 정상 후보를 대안으로 제시 (등급 낮추지 않음)
    assert result.alternatives
    assert "opus" not in result.alternatives
    assert any(name in ("kiro-opus", "kiro-sol", "codex-max") for name in result.alternatives)


def test_gate_cli_blocked_exit_3_with_stderr_alternatives(monkeypatch, capsys):
    policy.set_policy("claude", "exclude", until=dt.date(2026, 8, 31), note="Pro 요금제")
    providers = {
        "claude": lambda: _result("claude", 10.0, pool_class="preserve"),
        "codex": lambda: _result("codex", 10.0, pool_class="preserve"),
        "kiro": lambda: _result("kiro", 10.0, pool_class="spend", window="30d"),
    }
    monkeypatch.setattr(cli, "registry", lambda: providers)
    rc = cli.main(["gate", "-m", "opus", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 3
    assert "정책 제외" in out.err
    assert "대안" in out.err


def test_gate_blocked_by_raw_used_pct_cutoff():
    providers = [_result("codex", 90.0, pool_class="preserve")]
    result = gate_check(providers, "codex-max", today=TODAY, now=NOW)
    assert result.ok is False
    assert "소진" in result.reason
    assert result.used_pct == 90.0


def test_gate_blocked_alternatives_stay_same_grade_not_lower_tier():
    """대안은 등급을 낮추지 않고 같은 grade 안에서만 제시한다."""
    providers = [
        _result("codex", 95.0, pool_class="preserve"),  # codex-max 소진
        _result("kiro", 5.0, pool_class="spend", window="30d"),  # kiro-opus/kiro-sol 은 가용
        _result("claude", 95.0, pool_class="preserve"),  # opus 도 소진
    ]
    result = gate_check(providers, "codex-max", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.grade == "S+"
    assert result.alternatives
    for name in result.alternatives:
        # S+ 소속 정상 프로필만 등장 (하위 grade 로 내려가지 않음)
        assert name in ("kiro-opus", "kiro-sol")


def test_gate_escalation_profile_blocked_when_normal_candidates_available():
    """oc-omni 는 escalation — 같은 grade(C) 정상 후보가 가용하면 차단."""
    providers = [_result("kiro", 10.0, pool_class="spend", window="30d")]
    result = gate_check(providers, "oc-omni", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.grade == "C"
    assert "escalation" in result.reason
    assert "kiro-cheap" in result.alternatives


def test_gate_escalation_profile_ok_when_no_normal_candidates():
    """다른 C 후보가 전부 소진/측정불가면 oc-omni escalation 자격 충족 → exit 0 (무료 레인 예외)."""
    providers = [_result("kiro", 99.5, pool_class="spend", window="30d")]  # kiro-cheap 소진
    result = gate_check(providers, "oc-omni", today=TODAY, now=NOW)
    assert result.ok is True
    assert "escalation 자격 충족" in result.reason


def test_gate_escalation_fable_still_blocked_by_own_pool_exclude():
    """BLOCKER 수정: escalation 자격 충족해도 fable 자체 pool(claude) exclude 는 그대로 검사한다."""
    policy.set_policy("claude", "exclude", until=dt.date(2026, 8, 31), note="Pro 요금제")
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 95.0, pool_class="preserve"),  # codex-max 소진 (preserve cutoff 90%)
        _result(
            "kiro", 99.5, pool_class="spend", window="30d"
        ),  # kiro-opus/kiro-sol 도 소진 (spend cutoff 99%)
    ]
    result = gate_check(providers, "fable", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is False
    assert "정책 제외" in result.reason
    assert "until 2026-08-31" in result.reason


def test_gate_escalation_fable_still_blocked_by_own_pool_unmeasurable():
    """BLOCKER 수정: escalation 자격 충족해도 fable 자체 pool(claude) 측정불가면 exit 4."""
    providers = [
        ProviderResult(id="claude", error="HTTP 503"),
        _result("codex", 95.0, pool_class="preserve"),  # codex-max 소진
        _result("kiro", 99.5, pool_class="spend", window="30d"),  # kiro-opus/kiro-sol 도 소진
    ]
    result = gate_check(providers, "fable", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is True
    assert "측정 불가" in result.reason


def test_gate_escalation_fable_ok_when_own_pool_healthy_and_alts_exhausted():
    """S+ grade 는 opus/fable 이 같은 pool(claude) 을 공유하므로, claude 가 살아있으면 opus 가
    항상 먼저 정상후보로 남아 fable 은 escalation 자격을 얻지 못한다(같은 pool 공유의 자연스러운
    결과) — 이 케이스는 oc-oss(자기 pool 을 공유하는 default-gate 형제가 없는 프로필)로 검증한다.
    """
    providers = [
        _result("kiro", 99.5, pool_class="spend", window="30d"),  # kiro-cheap 소진 → escalation 자격 OK
        _result("agy", 10.0, pool_class="spend", scope=Scope("group", "3p"), window="30d"),  # 정상
    ]
    result = gate_check(providers, "oc-oss", today=TODAY, now=NOW)
    assert result.ok is True
    assert "escalation 자격 충족" in result.reason
    assert result.used_pct == 10.0


def test_gate_escalation_oc_oss_still_blocked_by_raw_cutoff():
    """BLOCKER 수정: oc-oss escalation 자격 충족해도 agy/3p raw cutoff 는 그대로 검사한다."""
    providers = [
        _result("kiro", 99.5, pool_class="spend", window="30d"),  # kiro-cheap 소진 → escalation 자격 OK
        _result("agy", 99.0, pool_class="spend", scope=Scope("group", "3p"), window="30d"),  # 소진
    ]
    result = gate_check(providers, "oc-oss", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is False
    assert "소진" in result.reason


# ------------------------------------------------------------------ exit 4 (unmeasurable)


def test_gate_unmeasurable_provider_error():
    providers = [ProviderResult(id="codex", error="HTTP 503")]
    result = gate_check(providers, "codex-max", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is True
    assert "측정 불가" in result.reason


def test_gate_unmeasurable_missing_provider():
    result = gate_check([], "codex-max", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is True


def test_gate_cli_unmeasurable_exit_4(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "registry", lambda: {"codex": lambda: ProviderResult(id="codex", error="HTTP 503")}
    )
    rc = cli.main(["gate", "-m", "codex-max", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 4
    assert "측정 불가" in out.err


def test_gate_unmeasurable_bucket_scope_mismatch():
    providers = [_result("agy", 10.0, scope=Scope("group", "gemini"))]  # oc-sonnet46 needs 3p scope
    result = gate_check(providers, "oc-sonnet46", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is True


# ------------------------------------------------------------------ unknown profile


def test_gate_check_unknown_profile_is_unmeasurable():
    result = gate_check([], "not-a-real-profile", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.grade is None
    assert "unknown profile" in result.reason


def test_gate_cli_rejects_unknown_profile_via_argparse(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["gate", "-m", "not-a-real-profile"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_gate_cli_requires_profile_argument(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["gate"])
    assert exc.value.code == 2
    assert "-m" in capsys.readouterr().err or "--profile" in capsys.readouterr().err


# ------------------------------------------------------------------ single-source profile->pool mapping


def test_gate_reuses_profile_pool_single_source_of_truth():
    """gate 는 profile_pool() (herdr-spawn QUOTA GUARD 와 동일) 을 재사용 — 별도 매핑 없음."""
    from scopefuel.recommend import profile_pool

    for profile in ("codex-max", "opus", "oc-sonnet46", "oc-gflash", "oc-omni", "kiro-opus"):
        result = gate_check([], profile, today=TODAY, now=NOW)
        expected_provider, _ = profile_pool(profile)
        assert result.provider_id == expected_provider
