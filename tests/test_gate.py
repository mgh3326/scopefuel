"""ROB-1184 — `scopefuel gate -m <profile>`: spawn go/no-go 판정 (exit 0/3/4)."""

from __future__ import annotations

import datetime as dt
import json

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
    policy.set_policy("claude", "exclude", until=dt.date(2099, 8, 31), note="Pro 요금제")
    providers = [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    result = gate_check(providers, "opus", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is False
    assert "정책 제외" in result.reason
    assert "until 2099-08-31" in result.reason
    # 동일 grade(S+)의 가용 정상 후보를 대안으로 제시 (등급 낮추지 않음)
    assert result.alternatives
    assert "opus" not in result.alternatives
    assert any(name in ("kiro-opus", "kiro-sol", "codex-max") for name in result.alternatives)


def test_gate_cli_blocked_exit_3_with_stderr_alternatives(monkeypatch, capsys):
    policy.set_policy("claude", "exclude", until=dt.date(2099, 8, 31), note="Pro 요금제")
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


def test_gate_fable_consult_only_blocked_by_pool_policy_exclude():
    """ROB-591 BLOCKER fix: fable left GRADE_TABLE (explicit-consult-only), but an
    active policy exclude on its own pool must still fail-closed — scoped narrowly
    to CONSULT_ONLY_PROFILES, not every D3 ("not in GRADE_TABLE") profile (a retired
    oc-* spelling, for instance, keeps its ordinary D3 behavior unchanged — see
    test_gate_oc_oss_not_in_grade_table_* below). #625: reaching the exclude check
    now needs the operator request that consult_only requires."""
    policy.set_policy("claude", "exclude", until=dt.date(2026, 8, 31), note="Pro 요금제")
    result = gate_check(
        _healthy_s_plus_providers(), "fable", today=TODAY, now=NOW, operator_request="hk:task/625"
    )
    assert result.ok is False
    assert result.grade is None
    assert result.unmeasurable is False
    assert "정책 제외" in result.reason
    assert "until 2026-08-31" in result.reason
    assert "Pro 요금제" in result.reason
    assert result.pool_class == "exclude"
    # 요청은 consult_only 충족일 뿐 quota 검사 면제가 아니다 — 감사 필드는 남는다.
    assert result.operator_request_ref == "hk:task/625"
    assert result.ref_resolution == "unverified"
    assert result.escalation_override is False


def test_gate_fable_consult_only_still_blocked_by_own_pool_unmeasurable():
    """ROB-591: the D3 path still checks the profile's own pool measurability.
    #625: the request satisfies consult_only; measurability still fails."""
    providers = [
        ProviderResult(id="claude", error="HTTP 503"),
        _result("codex", 95.0, pool_class="preserve"),  # codex-max 소진
        _result("kiro", 99.5, pool_class="spend", window="30d"),  # kiro-opus/kiro-sol 도 소진
    ]
    result = gate_check(providers, "fable", today=TODAY, now=NOW, operator_request="hk:task/625")
    assert result.ok is False
    assert result.unmeasurable is True
    assert "측정 불가" in result.reason


def test_gate_fable_consult_only_denied_without_operator_request():
    """#625/#527 AC⑤: fable's consult gate is an explicit operator request — a bare
    `scopefuel gate -m fable` must be refused even under a healthy pool. The #591-era
    quota-only pass let the two surfaces disagree: policy launch refused while the
    gate said spawnable."""
    result = gate_check(_healthy_s_plus_providers(), "fable", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is False
    assert result.role_denied is False
    assert "consult_only" in result.reason
    assert "--operator-request" in result.reason


def test_gate_fable_consult_only_purpose_alone_never_satisfies():
    """purpose 는 fable 의 consult_only 를 열지 않는다 — launch 와 같은 규칙."""
    for purpose in ("architect", "director", "operator-request", "builder", "worker", "tester"):
        result = gate_check(_healthy_s_plus_providers(), "fable", today=TODAY, now=NOW, purpose=purpose)
        assert result.ok is False, (purpose, result.reason)
        assert "consult_only" in result.reason


def test_gate_fable_consult_only_accepted_with_operator_request():
    """#625 AC2: a valid operator request satisfies consult_only, then the D3 quota
    checks run normally — healthy pool → ok, with the audit fields recorded."""
    result = gate_check(
        _healthy_s_plus_providers(),
        "fable",
        today=TODAY,
        now=NOW,
        operator_request="hk:doc/note/2026-09-23/model-refresh-opus55-gpt6-grok47",
        requested_by="operator",
    )
    assert result.ok is True
    assert result.grade is None
    assert result.provider_id == "claude"
    assert result.escalation_override is False  # 대안 거부를 건너뛴 것이 아니다
    assert result.operator_request_ref == "hk:doc/note/2026-09-23/model-refresh-opus55-gpt6-grok47"
    assert result.requested_by == "operator"
    assert result.ref_resolution == "unverified"
    assert "operator_request=hk:doc/note/2026-09-23/model-refresh-opus55-gpt6-grok47" in result.reason
    assert "escalation_override=false" in result.reason


def test_gate_fable_consult_only_blocked_by_own_pool_cutoff():
    providers = [
        _result("claude", 95.0, pool_class="preserve"),  # fable pool 소진 (preserve cutoff 90%)
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    result = gate_check(providers, "fable", today=TODAY, now=NOW, operator_request="hk:task/625")
    assert result.ok is False
    assert result.unmeasurable is False
    assert "소진" in result.reason


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
    assert "차단선 99%" in result.reason  # task #638 문구: 사용·남음·차단선


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


# ------------------------------------------------------------------ task #461: operator-request


def _healthy_s_plus_providers() -> list[ProviderResult]:
    """S+ 정상 후보(codex/kiro)가 가용하고 fable 자체 pool(claude)도 정상인 픽스처."""
    return [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]


# ROB-591: fable left GRADE_TABLE and is no longer escalation-gated (see the
# fable_consult_only_* tests above/below), so it can no longer exercise the task
# #461 operator-request override mechanism end to end. Every remaining
# GRADE_TABLE escalation profile shares its launcher name with a default-gate
# sibling (_find_profile matches by name and returns the first list entry, which
# is always the default-gate row — see recommend.py), so gate_check(providers,
# name, ...) can no longer resolve any of them unambiguously by name either. A
# minimal synthetic grade_table with one uniquely-named escalation profile keeps
# this operator-request coverage generic (not fable-specific) and unambiguous.
def _escalation_grade_table() -> dict[str, list]:
    from scopefuel.recommend import Profile

    return {
        "S+": [
            Profile("codex-sol", "Test Normal", 60.0),
            Profile(
                "opus",
                "Test Escalation",
                55.0,
                gate="escalation",
                gate_reason="test escalation reason",
            ),
        ],
        "S": [],
        "A+": [],
        "A": [],
        "B": [],
        "C": [],
    }


def test_gate_escalation_denied_without_operator_request():
    """플래그 없음 — 대안 가용 시 escalation 프로필은 exit 3 (이유·대안 포함)."""
    table = _escalation_grade_table()
    result = gate_check(_healthy_s_plus_providers(), "opus", today=TODAY, now=NOW, grade_table=table)
    assert result.ok is False
    assert result.unmeasurable is False
    assert "escalation 후보" in result.reason
    assert "다른 S+ 후보가 아직 가용하므로 사용 불가" in result.reason
    assert result.alternatives
    # additive 기본값 — 플래그 없는 결과에는 override 감사 필드가 비어 있다
    assert result.escalation_override is False
    assert result.operator_request_ref is None
    assert result.requested_by is None
    assert result.ref_resolution is None


def test_gate_escalation_operator_request_overrides_alternative_denial():
    """유효한 durable REF + escalation 프로필 → '대안 가용' 갈래만 건너뛰고 통과."""
    table = _escalation_grade_table()
    ref = "hk:doc/decision-req/2026-09-20/escalation-operator-request"
    result = gate_check(
        _healthy_s_plus_providers(),
        "opus",
        today=TODAY,
        now=NOW,
        operator_request=ref,
        requested_by="operator",
        grade_table=table,
    )
    assert result.ok is True
    assert result.escalation_override is True
    assert result.operator_request_ref == ref
    assert result.requested_by == "operator"
    assert result.ref_resolution == "unverified"
    assert "escalation_override" in result.reason
    assert ref in result.reason


def test_gate_escalation_operator_request_task_ref_form():
    """hk:task/<정수> 형태도 유효한 durable REF 다."""
    table = _escalation_grade_table()
    result = gate_check(
        _healthy_s_plus_providers(),
        "opus",
        today=TODAY,
        now=NOW,
        operator_request="hk:task/461",
        grade_table=table,
    )
    assert result.ok is True
    assert result.operator_request_ref == "hk:task/461"
    assert result.requested_by == "unknown"  # 미지정 시 기본값
    assert result.ref_resolution == "unverified"


def test_gate_escalation_operator_request_still_blocked_by_exclude():
    """override 는 '대안 가용' 갈래만 연다 — escalation 프로필 자체 pool exclude 는 그대로 거부."""
    table = _escalation_grade_table()
    policy.set_policy("claude", "exclude", until=dt.date(2099, 8, 31), note="Pro 요금제")
    result = gate_check(
        _healthy_s_plus_providers(),
        "opus",
        today=TODAY,
        now=NOW,
        operator_request="hk:task/461",
        grade_table=table,
    )
    assert result.ok is False
    assert "정책 제외" in result.reason
    assert result.escalation_override is True  # 대안 거부는 건너뛰었지만 quota 검사에서 차단
    assert result.ref_resolution == "unverified"


def test_gate_escalation_operator_request_still_blocked_by_cutoff():
    """quota cutoff 초과는 유효한 operator-request 가 있어도 거부."""
    table = _escalation_grade_table()
    providers = [
        _result("claude", 95.0, pool_class="preserve"),  # 자체 pool 소진 (preserve cutoff 90%)
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    result = gate_check(
        providers, "opus", today=TODAY, now=NOW, operator_request="hk:task/461", grade_table=table
    )
    assert result.ok is False
    assert result.unmeasurable is False
    assert "소진" in result.reason


def test_gate_escalation_operator_request_still_unmeasurable_on_provider_error():
    """pool 측정불가는 유효한 operator-request 가 있어도 exit 4."""
    table = _escalation_grade_table()
    providers = [
        ProviderResult(id="claude", error="HTTP 503"),
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]
    result = gate_check(
        providers, "opus", today=TODAY, now=NOW, operator_request="hk:task/461", grade_table=table
    )
    assert result.ok is False
    assert result.unmeasurable is True
    assert "측정 불가" in result.reason


@pytest.mark.parametrize(
    "bad_ref",
    [
        "",  # 빈 값
        "please let fable run this once",  # 자유 문장
        "hk:doc/../etc/passwd",  # 경로 탐색
        "hk:doc/brief/2026-09-20/BAD KEY",  # 허용 문자 밖 (대문자·공백)
        "hk:doc/" + "a" * 200,  # 길이 초과
        "hk:task/abc",  # 정수가 아닌 task 번호
    ],
)
def test_gate_operator_request_invalid_ref_rejected(bad_ref):
    result = gate_check(
        _healthy_s_plus_providers(),
        "fable",
        today=TODAY,
        now=NOW,
        operator_request=bad_ref,
    )
    assert result.ok is False
    assert result.unmeasurable is False
    assert "operator_request_ref_invalid" in result.reason


def test_gate_operator_request_on_non_escalation_profile_rejected():
    """opus 는 gate=default — 유효한 REF 도 operator_request_not_applicable 로 거부."""
    result = gate_check(
        _healthy_s_plus_providers(),
        "opus",
        today=TODAY,
        now=NOW,
        operator_request="hk:task/461",
    )
    assert result.ok is False
    assert "operator_request_not_applicable" in result.reason


def test_gate_operator_request_on_plain_d3_profile_rejected():
    """#625: consult_only 가 아닌 D3(GRADE_TABLE 밖) 철자에는 여전히 not_applicable —
    요청 경로가 열린 것은 consult_only 정체성뿐이다."""
    providers = [
        _result("agy", 10.0, pool_class="spend", scope=Scope("group", "3p"), window="30d"),
    ]
    result = gate_check(providers, "oc-oss", today=TODAY, now=NOW, operator_request="hk:task/625")
    assert result.ok is False
    assert "operator_request_not_applicable" in result.reason


def test_gate_fable_cli_operator_request_exit_0_with_audit_fields(monkeypatch, capsys, tmp_path):
    """#625 AC2: `scopefuel gate -m fable --operator-request <ref>` 가 rc 0 이고
    감사 필드가 stdout 첫 줄과 --gate-output 레코드에 남는다."""
    providers = {
        "claude": lambda: _result("claude", 10.0, pool_class="preserve"),
    }
    monkeypatch.setattr(cli, "registry", lambda: providers)
    gate_file = tmp_path / "gate.json"
    rc = cli.main(
        [
            "gate",
            "-m",
            "fable",
            "--operator-request",
            "hk:task/625",
            "--requested-by",
            "operator",
            "--gate-output",
            str(gate_file),
            "--no-cache",
        ]
    )
    out = capsys.readouterr()
    assert rc == 0
    assert "operator_request_ref=hk:task/625" in out.out
    assert "requested_by=operator" in out.out
    assert "ref_resolution=unverified" in out.out
    assert "escalation_override=false" in out.out

    record = json.loads(gate_file.read_text())
    assert record["operator_request_ref"] == "hk:task/625"
    assert record["requested_by"] == "operator"
    assert record["ref_resolution"] == "unverified"
    assert record["escalation_override"] is False
    assert record["exit_code"] == 0


def test_gate_fable_cli_no_request_exit_3(monkeypatch, capsys):
    """#625 AC3: `scopefuel gate -m fable` (요청 없음) 은 consult_only 거부."""
    monkeypatch.setattr(
        cli, "registry", lambda: {"claude": lambda: _result("claude", 10.0, pool_class="preserve")}
    )
    rc = cli.main(["gate", "-m", "fable", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 3
    assert "consult_only" in out.err


def test_gate_operator_request_cli_oc_omni_exit_0_with_audit_fields(monkeypatch, capsys, tmp_path):
    """ROB-591: fable is no longer GRADE_TABLE-escalation-gated, so this end-to-end
    CLI test (real argparse choices, real GRADE_TABLE) uses oc-omni — the one
    remaining real escalation profile whose launcher name is unique (no default-gate
    sibling shares it) — to exercise the operator-request override mechanism."""
    providers = {
        "kiro": lambda: _result("kiro", 10.0, pool_class="spend", window="30d"),  # kiro-cheap alt 가용
    }
    monkeypatch.setattr(cli, "registry", lambda: providers)
    gate_file = tmp_path / "gate.json"
    rc = cli.main(
        [
            "gate",
            "-m",
            "oc-omni",
            "--operator-request",
            "hk:task/461",
            "--requested-by",
            "operator",
            "--gate-output",
            str(gate_file),
            "--no-cache",
        ]
    )
    out = capsys.readouterr()
    assert rc == 0
    assert "escalation_override=true" in out.out
    assert "operator_request_ref=hk:task/461" in out.out
    assert "requested_by=operator" in out.out
    assert "ref_resolution=unverified" in out.out

    record = json.loads(gate_file.read_text())
    assert record["escalation_override"] is True
    assert record["operator_request_ref"] == "hk:task/461"
    assert record["requested_by"] == "operator"
    assert record["ref_resolution"] == "unverified"
    assert record["exit_code"] == 0


def test_gate_operator_request_cli_invalid_ref_exit_3(monkeypatch, capsys):
    monkeypatch.setattr(cli, "registry", lambda: {"claude": lambda: _result("claude", 10.0)})
    rc = cli.main(["gate", "-m", "fable", "--operator-request", "hk:task/abc", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 3
    assert "operator_request_ref_invalid" in out.err


def test_gate_operator_request_cli_opus_exit_3_not_applicable(monkeypatch, capsys):
    providers = {
        "claude": lambda: _result("claude", 10.0, pool_class="preserve"),
        "codex": lambda: _result("codex", 10.0, pool_class="preserve"),
    }
    monkeypatch.setattr(cli, "registry", lambda: providers)
    rc = cli.main(["gate", "-m", "opus", "--operator-request", "hk:task/461", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 3
    assert "operator_request_not_applicable" in out.err


# ------------------------------------------------------------------ task #527: astra purpose gate
#
# astra 는 모델 식별(ASTRA_ROLE_PROFILES 멤버십)로 판별되고 --purpose 가
# ASTRA_ALLOWED_PURPOSES 일 때만 쿼타 검사로 진행한다. 역할 거부(exit 5)는
# 쿼타 거부(exit 3)와 rc·reason 토큰 양쪽으로 구별된다.


def test_astra_role_profiles_membership_pinned():
    """뮤턴트 핀: ASTRA_ROLE_PROFILES 를 비우거나 줄이면 이 단언이 RED 다.

    아래 테스트들이 세트를 순회하는 이유로, 빈 세트는 거부 단언을 vacuous 하게
    만든다 — 멤버십 자체를 정확히 고정한다."""
    from scopefuel.recommend import ASTRA_ROLE_PROFILES

    assert frozenset({"codex-astra", "builder-astra", "captain-astra", "gpt-6-astra"}) == ASTRA_ROLE_PROFILES


def test_astra_allowed_purposes_pinned():
    """decision 2376 의 세 범주와 1:1 — 새 용도 추가·제거는 이 단언을 건드려야 한다."""
    from scopefuel.recommend import ASTRA_ALLOWED_PURPOSES

    assert frozenset({"director", "architect", "operator-request"}) == ASTRA_ALLOWED_PURPOSES


@pytest.mark.parametrize("profile_name", ["codex-astra", "builder-astra", "captain-astra", "gpt-6-astra"])
def test_gate_astra_allowed_purpose_passes_quota_check(profile_name):
    """AC: 허용 용도 + 쿼타 여유 → 통과. builder-*/captain-* 는 런처 철자가 아니라
    역할 세트 멤버십으로 판별된다(wrk 측 tombstone 과 별개)."""
    providers = [_result("codex", 10.0, pool_class="preserve")]
    for purpose in ("director", "architect", "operator-request"):
        result = gate_check(providers, profile_name, today=TODAY, now=NOW, purpose=purpose)
        assert result.ok is True, (profile_name, purpose, result.reason)
        assert result.role_denied is False
        assert result.provider_id == "codex"


def test_gate_astra_allowed_purpose_quota_exhausted_is_quota_denial():
    """AC: 허용 용도 + 쿼타 소진 → 쿼타 거부 — 역할 거부와 정확히 구별."""
    providers = [_result("codex", 95.0, pool_class="preserve")]
    result = gate_check(providers, "codex-astra", today=TODAY, now=NOW, purpose="architect")
    assert result.ok is False
    assert result.role_denied is False  # 역할 거부가 아니다
    assert result.unmeasurable is False
    assert "소진" in result.reason


@pytest.mark.parametrize("purpose", [None, "", "  ", "builder", "worker", "tester", "architct"])
def test_gate_astra_disallowed_or_missing_purpose_is_role_denial(purpose):
    """AC: builder/worker/tester·오타·미지정 용도 → 역할 거부 (쿼타 여유와 무관)."""
    providers = [_result("codex", 0.0, pool_class="preserve")]
    result = gate_check(providers, "codex-astra", today=TODAY, now=NOW, purpose=purpose)
    assert result.ok is False
    assert result.role_denied is True
    assert result.unmeasurable is False
    assert result.reason.startswith("role_restricted:")
    # 허용 용도 목록이 reason 에 실려 호출자가 자기수정할 수 있다
    assert "architect" in result.reason


def test_gate_astra_purpose_comparison_is_casefold_normalized():
    """용도 비교는 strip+casefold — 'Architect' 도 허용 용도다."""
    providers = [_result("codex", 10.0, pool_class="preserve")]
    result = gate_check(providers, "codex-astra", today=TODAY, now=NOW, purpose="  Architect ")
    assert result.ok is True
    assert result.role_denied is False


def test_gate_astra_role_denial_and_quota_denial_have_exact_distinct_rc(monkeypatch, capsys):
    """AC 핵심: 같은 프로필·같은 게이트에서 역할 거부(rc 5)와 쿼타 거부(rc 3)가
    다른 종료코드로 나간다 — 부분 문자열이 아니라 rc 의 정확 비교."""
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"codex": lambda: _result("codex", 10.0, pool_class="preserve")},
    )
    rc_role = cli.main(["gate", "-m", "codex-astra", "--purpose", "builder", "--no-cache"])
    err_role = capsys.readouterr().err
    assert rc_role == 5
    assert "role_restricted:" in err_role
    assert "역할 거부" in err_role

    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"codex": lambda: _result("codex", 95.0, pool_class="preserve")},
    )
    rc_quota = cli.main(["gate", "-m", "codex-astra", "--purpose", "architect", "--no-cache"])
    err_quota = capsys.readouterr().err
    assert rc_quota == 3
    assert "소진" in err_quota

    assert rc_role != rc_quota


def test_gate_astra_role_denial_cli_exit_5_and_gate_output(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"codex": lambda: _result("codex", 10.0, pool_class="preserve")},
    )
    gate_file = tmp_path / "gate.json"
    rc = cli.main(
        [
            "gate",
            "-m",
            "codex-astra",
            "--purpose",
            "worker",
            "--gate-output",
            str(gate_file),
            "--no-cache",
        ]
    )
    assert rc == 5
    record = json.loads(gate_file.read_text())
    assert record["role_denied"] is True
    assert record["purpose"] == "worker"
    assert record["exit_code"] == 5


def test_gate_astra_cli_allowed_purpose_exit_0(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"codex": lambda: _result("codex", 10.0, pool_class="preserve")},
    )
    rc = cli.main(["gate", "-m", "codex-astra", "--purpose", "architect", "--no-cache"])
    assert rc == 0
    assert "pool=codex" in capsys.readouterr().out


def test_gate_astra_name_substring_alone_is_not_role_denied():
    """AC: 이름에 astra 가 든 무관 프로필(세트 비멤버)은 역할 거부되지 않는다.

    뮤턴트 방향 ②: `"astra" in profile_name.casefold()` 매칭을 복원하면 이
    테스트가 RED 다 — codex-astra-foo 는 codex pool 로 정상 쿼타 검사를 받는다."""
    providers = [_result("codex", 10.0, pool_class="preserve")]
    result = gate_check(providers, "codex-astra-foo", today=TODAY, now=NOW)
    assert result.role_denied is False
    assert result.ok is True


def test_gate_astra_model_spelling_is_role_gated_and_quota_checked():
    """gpt-6-astra(모델 철자)도 같은 게이트: 용도 없으면 역할 거부, 허용 용도면
    codex pool 쿼타 검사로 진행한다."""
    providers = [_result("codex", 10.0, pool_class="preserve")]
    denied = gate_check(providers, "gpt-6-astra", today=TODAY, now=NOW)
    assert denied.role_denied is True
    allowed = gate_check(providers, "gpt-6-astra", today=TODAY, now=NOW, purpose="director")
    assert allowed.ok is True
    assert allowed.provider_id == "codex"


def test_gate_astra_operator_request_still_not_applicable():
    """⑤ 회귀: 허용 용도를 줘도 --operator-request 는 escalation 전용이라
    astra 에서는 여전히 operator_request_not_applicable 로 거부된다."""
    providers = [_result("codex", 10.0, pool_class="preserve")]
    result = gate_check(
        providers,
        "codex-astra",
        today=TODAY,
        now=NOW,
        purpose="architect",
        operator_request="hk:task/461",
    )
    assert result.ok is False
    assert result.role_denied is False
    assert "operator_request_not_applicable" in result.reason


def test_gate_purpose_ignored_for_non_astra_profile():
    """비-astra 프로필에서 --purpose 는 판정을 바꾸지 않는다."""
    providers = [_result("codex", 10.0, pool_class="preserve")]
    plain = gate_check(providers, "codex-max", today=TODAY, now=NOW)
    with_purpose = gate_check(providers, "codex-max", today=TODAY, now=NOW, purpose="builder")
    assert with_purpose.ok == plain.ok is True
    assert with_purpose.role_denied is False


# ------------------------------------------------------------------ #573 kimi
# 소진된 kimi 풀이 "5h 0% · 주 0%" 로 측정돼 게이트를 통과한 사고.
# /usage 패널의 monthly 행(멤버십 쿼타)도 측정 대상이며, 한도/조회 오류
# 텍스트가 찍힌 출력은 측정 불가다.


def _kimi_result(
    h5: float,
    weekly: float,
    monthly: float | None = None,
) -> ProviderResult:
    buckets = [
        Bucket(
            label="5h",
            window="5h",
            used_pct=h5,
            scope=Scope("account"),
            horizon="now",  # type: ignore[arg-type]
        ),
        Bucket(
            label="weekly",
            window="7d",
            used_pct=weekly,
            scope=Scope("account"),
            horizon="week",  # type: ignore[arg-type]
        ),
    ]
    if monthly is not None:
        buckets.append(
            Bucket(
                label="monthly",
                window="30d",
                used_pct=monthly,
                scope=Scope("account"),
                horizon="month",  # type: ignore[arg-type]
            )
        )
    return ProviderResult(id="kimi", pool_class="spend", buckets=buckets)


def test_gate_kimi_monthly_exhausted_blocks_despite_zero_windows():
    # 사고 형태: 5h/주간은 0% 인데 monthly 캡이 소진 → 소진이 아니라 통과하면 안 됨.
    result = gate_check([_kimi_result(0.0, 0.0, monthly=100.0)], "kimi-k3", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is False
    assert "100%" in result.reason


def test_gate_kimi_quota_error_is_unmeasurable():
    providers = [
        ProviderResult(
            id="kimi",
            pool_class="spend",
            error="Kimi CLI /usage 출력에 사용 한도 도달·조회 오류 표시가 있음: "
            "403 You've reached your 5-hour usage limit.",
        )
    ]
    result = gate_check(providers, "kimi-k3", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is True


def test_gate_kimi_healthy_values_still_pass():
    result = gate_check([_kimi_result(30.0, 20.0, monthly=45.0)], "kimi-k3", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.provider_id == "kimi"


def test_gate_kimi_zero_windows_without_monthly_is_the_known_shape():
    # 정상 회귀: 패널이 5h/주간만 렌더하고 0% 면 계측된 건강 상태로 통과한다.
    result = gate_check([_kimi_result(0.0, 0.0)], "kimi-k3", today=TODAY, now=NOW)
    assert result.ok is True


# ------------------------------------------------- task #690: grok weekly-only + 미측정 창 가시성


def _grok_weekly(used: float) -> ProviderResult:
    """실 grok provider 출력 형태 — 주간(7d) 계정 bucket 하나뿐, 5h 는 원래 없다."""
    return ProviderResult(
        id="grok",
        pool_class="spend",
        buckets=[
            Bucket(
                label="7d",
                window="7d",
                used_pct=used,
                resets_at=_reset_almost_full("7d"),
                scope=Scope("account"),
                horizon="week",
            )
        ],
    )


def test_grok_required_windows_are_weekly_only():
    """#690: grok 은 5시간 한도 자체가 없다 — 필수 창 집합은 주간(7d) 하나뿐."""
    from scopefuel import manual

    assert manual.REQUIRED_WINDOWS["grok"] == frozenset({"7d"})


def test_gate_grok_weekly_only_selectable():
    """#690 AC: 5h 값이 없어도 주간 한도가 가용하면 요청 프로필 grok-hi 가 선택된다."""
    result = gate_check([_grok_weekly(0.0)], "grok-hi", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.provider_id == "grok"
    assert result.missing_windows == ()
    assert "pool=grok" in result.reason


def test_gate_grok_unreadable_5h_still_selectable():
    """#690: 읽히지 않는 5h bucket(used_pct=None)이 섞여 있어도 grok 은 주간 값으로 통과.

    5h 는 grok 의 필수 창이 아니므로 미측정 태그도 붙지 않는다 — grok 5h 칸의
    '?' 는 결함이 아니라 provider 특성이다.
    """
    providers = [
        ProviderResult(
            id="grok",
            pool_class="spend",
            buckets=[
                Bucket(
                    label="5h",
                    window="5h",
                    used_pct=None,
                    resets_at=None,
                    scope=Scope("account"),
                    horizon="now",
                ),
                Bucket(
                    label="7d",
                    window="7d",
                    used_pct=12.0,
                    resets_at=_reset_almost_full("7d"),
                    scope=Scope("account"),
                    horizon="week",
                ),
            ],
        )
    ]
    result = gate_check(providers, "grok-hi", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.used_pct == 12.0
    assert result.missing_windows == ()


def test_gate_grok_weekly_exhausted_denied_with_reason():
    """#690 AC: 주간 한도가 차단선을 넘으면 grok 는 거부 — 사유가 항상 출력된다."""
    result = gate_check([_grok_weekly(99.9)], "grok-hi", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is False
    assert "소진" in result.reason
    assert "차단선 99%" in result.reason


def test_gate_missing_required_window_named_on_admit():
    """#690: 필수 창이 빠진 fresh 스냅샷은 측정된 창으로 판정하되 창 이름을 남긴다.

    claude 는 5h+7d 가 필수 — 7d 만 있는 결과는 '5h 미측정' 을 표기한 채 통과.
    """
    providers = [_result("claude", 30.0, pool_class="preserve")]  # 7d 만 — 5h 없음
    result = gate_check(providers, "opus", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.missing_windows == ("5h",)
    assert "필수 창 미측정: 5h" in result.reason


def test_gate_unreadable_required_window_named_on_admit():
    """#690: bucket 은 있으나 값이 읽히지 않는(used_pct=None) 창도 미측정으로 표기."""
    providers = [
        ProviderResult(
            id="claude",
            pool_class="preserve",
            buckets=[
                Bucket(
                    label="5h",
                    window="5h",
                    used_pct=None,
                    resets_at=None,
                    scope=Scope("account"),
                    horizon="now",
                ),
                Bucket(
                    label="7d",
                    window="7d",
                    used_pct=30.0,
                    resets_at=_reset_almost_full("7d"),
                    scope=Scope("account"),
                    horizon="week",
                ),
            ],
        )
    ]
    result = gate_check(providers, "opus", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.missing_windows == ("5h",)
    assert "필수 창 미측정: 5h" in result.reason


def test_gate_stale_denial_names_missing_required_window():
    """#690: 필수 창 부족으로 stale 수용이 거부될 때 어떤 창이 비었는지 사유에 남는다.

    5h 만 있는 claude stale 스냅샷은 필수 7d 가 없어 수용 불가 — 사유가
    '7d' 를 이름으로 지목한다.
    """
    stale = ProviderResult(
        id="claude",
        stale=True,
        age_s=300.0,
        error_kind="rate_limited",
        account_fp_match=True,
        buckets=[
            Bucket(
                label="5h",
                window="5h",
                used_pct=10.0,
                resets_at=_reset_almost_full("5h"),
                scope=Scope("account"),
                horizon="now",
            ),
        ],
    )
    result = gate_check([stale], "opus", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is True
    assert result.missing_windows == ("7d",)
    assert "필수 창 미측정: 7d" in result.reason
