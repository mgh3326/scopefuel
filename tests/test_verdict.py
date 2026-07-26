"""판정 로직 — 이 도구의 존재 이유이므로 가장 촘촘하게 테스트한다."""

from __future__ import annotations

from scopefuel.model import Bucket, ProviderResult, Scope, overall_mark, verdict_for


def account(used: float, *, window: str = "7d", horizon: str = "week") -> Bucket:
    return Bucket(label=window, window=window, used_pct=used, scope=Scope("account"), horizon=horizon)


def model(used: float, name: str) -> Bucket:
    return Bucket(label=name, window="7d", used_pct=used, scope=Scope("model", name), horizon="week")


def group(used: float, name: str, *, window: str = "5h", horizon: str = "now") -> Bucket:
    return Bucket(label=name, window=window, used_pct=used, scope=Scope("group", name), horizon=horizon)


def test_model_scoped_exhaustion_does_not_block_account():
    """Fable 100% 소진이 계정 차단으로 오독되면 안 된다 (이 도구가 만들어진 이유)."""
    verdict = verdict_for([account(6, window="5h", horizon="now"), account(97), model(100, "Fable")])
    assert verdict.basis == "account"
    assert verdict.blocking_pct == 97  # 100(Fable)이 아니다
    assert verdict.mark == "crit"
    assert [b.scope.name for b in verdict.exhausted] == ["Fable"]


def test_now_and_week_axes_are_separate():
    verdict = verdict_for([account(6, window="5h", horizon="now"), account(97)])
    assert verdict.now_pct == 6  # 지금은 일할 수 있다
    assert verdict.week_pct == 97  # 이번 주 예산은 거의 없다


def test_model_scope_excluded_from_axes():
    """모델 한정 한도는 now/week 축을 오염시키지 않는다."""
    verdict = verdict_for([account(10), model(100, "Fable")])
    assert verdict.week_pct == 10


def test_group_basis_uses_most_available_group():
    """그룹은 서로 독립 — 한 그룹이 막혀도 다른 그룹으로 작업할 수 있다."""
    verdict = verdict_for([group(7.4, "gemini"), group(57.3, "3p")])
    assert verdict.basis == "group"
    assert verdict.blocking_pct == 7.4
    assert verdict.mark == "ok"
    assert verdict.groups == {"gemini": 7.4, "3p": 57.3}


def test_group_basis_blocks_only_when_all_groups_full():
    verdict = verdict_for([group(100, "gemini"), group(95, "3p")])
    assert verdict.blocking_pct == 95
    assert verdict.mark == "crit"


def test_unknown_values_are_not_treated_as_free():
    verdict = verdict_for([Bucket(label="5h", window="5h", used_pct=None, horizon="now")])
    assert verdict.basis == "none"
    assert verdict.now_pct is None


def test_provider_result_serialization_includes_scope_and_horizon():
    result = ProviderResult(id="x", buckets=[account(50), model(100, "Fable")])
    payload = result.as_dict()
    assert payload["buckets"][0]["scope"] == {"kind": "account", "name": None}
    assert payload["buckets"][1]["scope"] == {"kind": "model", "name": "Fable"}
    assert payload["buckets"][0]["horizon"] == "week"
    assert payload["verdict"]["blocking_pct"] == 50
    assert payload["status"] == "ok"


def test_errored_provider_has_degraded_verdict_mark():
    res = ProviderResult(id="codex", error="HTTP 503 circuit open")
    assert res.verdict.mark == "degraded"
    assert res.as_dict()["verdict"]["mark"] == "degraded"


def test_errored_provider_summary_mark_is_not_ok():
    ok_res = ProviderResult(id="claude", buckets=[account(16)])
    err_res = ProviderResult(id="codex", error="HTTP 503 circuit open")
    assert overall_mark([ok_res, err_res]) == "degraded"
