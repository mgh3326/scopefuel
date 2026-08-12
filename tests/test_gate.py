"""ROB-1184 — `scopefuel gate -m <profile>`: spawn go/no-go 판정 (exit 0/3/4)."""

from __future__ import annotations

import datetime as dt

import pytest

from scopefuel import cli
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


def test_gate_blocked_by_raw_used_pct_cutoff():
    providers = [_result("codex", 90.0, pool_class="preserve")]
    result = gate_check(providers, "codex-max", today=TODAY, now=NOW)
    assert result.ok is False
    assert "소진" in result.reason
    assert result.used_pct == 90.0


def test_gate_oc_oss_not_in_grade_table_passes_quota_check():
    """D3: oc-oss (ROB-1221 이후 은퇴)는 GRADE_TABLE에 없지만 profile_pool에는 있으므로,
    escalation 로직 없이 quota cutoff만 검사한다. pool 사용이 cutoff 이하면 통과.
    """
    providers = [
        _result("agy", 10.0, pool_class="spend", scope=Scope("group", "3p"), window="30d"),
    ]
    result = gate_check(providers, "oc-oss", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.grade is None  # GRADE_TABLE에 없으므로 grade=None
    assert result.unmeasurable is False
    assert "pool=agy" in result.reason
    assert result.alternatives == ()  # No alternatives for non-grade profiles


def test_gate_oc_oss_not_in_grade_table_blocked_by_quota_cutoff():
    """D3: GRADE_TABLE에 없는 프로필도 quota cutoff로 차단된다.
    oc-oss는 escalation 로직 없이 agy/3p pool의 raw cutoff만 검사한다.
    """
    providers = [
        _result("agy", 99.0, pool_class="spend", scope=Scope("group", "3p"), window="30d"),  # 소진
    ]
    result = gate_check(providers, "oc-oss", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.grade is None  # GRADE_TABLE에 없으므로 grade=None
    assert result.unmeasurable is False
    assert "소진" in result.reason
    assert "cutoff" in result.reason


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
