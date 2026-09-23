"""task173 — AA prices, pool-local value ordering, and role gates."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3

import pytest

from scopefuel import bench, cli
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    ASTRA_ROLE_PROFILES,
    GRADE_TABLE,
    MODEL_ONLY_ANNOTATION,
    Profile,
    gate_check,
    profile_pool,
    recommend,
    validate_grade_table,
)

NOW = dt.datetime(2026, 9, 9, 6, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
GRADES = ("S+", "S", "A+", "A", "B", "C")


@pytest.fixture(autouse=True)
def local_task_backend(tmp_path, monkeypatch):
    """Every task173 test starts local and cannot reach AA or handoffkeep."""

    monkeypatch.delenv("HANDOFFKEEP_URL", raising=False)
    monkeypatch.delenv("HANDOFFKEEP_TOKEN", raising=False)
    monkeypatch.setattr(bench, "DOTENV_PATH", tmp_path / "missing.env")
    monkeypatch.setattr(
        bench,
        "request_json",
        lambda *args, **kwargs: pytest.fail("task173 fixture attempted network access"),
    )
    assert bench.bench_backend().name == bench.BENCH_BACKEND_LOCAL


def _provider(provider_id: str, used: float = 10.0) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class="preserve",
        buckets=[
            Bucket(
                label="7d",
                window="7d",
                used_pct=used,
                resets_at=(NOW + dt.timedelta(days=6)).isoformat(),
                scope=Scope("account"),
                horizon="week",
            )
        ],
    )


def _price(model_id: str, blended: float, input_price: float = 1.0, output_price: float = 4.0):
    return bench.ModelPrice(
        model_id=model_id,
        price_1m_blended_3_to_1=blended,
        price_1m_input_tokens=input_price,
        price_1m_output_tokens=output_price,
        captured_at="2026-09-09T06:00:00+00:00",
    )


def _table(grade: str, profiles: list[Profile]):
    table = {name: [] for name in GRADES}
    table[grade] = profiles
    return table


def _ranked_labels(output: str) -> list[str]:
    labels: list[str] = []
    for line in output.splitlines():
        if not line[:1].isdigit():
            continue
        tokens = line.split(".", 1)[1].strip().split()
        while tokens and tokens[0].startswith("🔥"):
            tokens.pop(0)
        label = tokens[0]
        if len(tokens) >= 3 and tokens[1] == "--effort":
            label += f" --effort {tokens[2]}"
        labels.append(label)
    return labels


def test_ac1_sync_stores_real_shape_prices_by_base_model_and_keeps_dearer_duplicate(tmp_path):
    path = tmp_path / "bench.db"
    payload = {
        "data": [
            {
                "slug": "value-model-high",
                "evaluations": {"artificial_analysis_coding_index": 60.0},
                "pricing": {
                    "price_1m_blended_3_to_1": 2.5,
                    "price_1m_input_tokens": 1.5,
                    "price_1m_output_tokens": 5.5,
                },
            },
            {
                "slug": "value-model-low",
                "evaluations": {"artificial_analysis_coding_index": 59.0},
                "pricing": {
                    "price_1m_blended_3_to_1": 1.925,
                    "price_1m_input_tokens": 1.1,
                    "price_1m_output_tokens": 4.4,
                },
            },
        ]
    }

    assert (
        bench.sync_scores(
            api_key="fixture-key",
            path=path,
            request_fn=lambda *args, **kwargs: payload,
            captured_at="2026-09-09T06:00:00+00:00",
        )
        == 2
    )
    prices = bench.read_prices(path=path)
    assert prices["value-model"] == _price("value-model", 2.5, 1.5, 5.5)

    conn = sqlite3.connect(path)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(model_prices)")]
        rows = conn.execute("SELECT model_id FROM model_prices").fetchall()
    finally:
        conn.close()
    assert columns == [
        "model_id",
        "price_1m_blended_3_to_1",
        "price_1m_input_tokens",
        "price_1m_output_tokens",
        "captured_at",
    ]
    assert rows == [("value-model",)]


def test_price_failure_rolls_back_scores_in_the_same_local_transaction(tmp_path, monkeypatch):
    path = tmp_path / "bench.db"
    payload = {
        "data": [
            {
                "slug": "atomic-model",
                "evaluations": {"artificial_analysis_coding_index": 60.0},
                "pricing": {
                    "price_1m_blended_3_to_1": 2.0,
                    "price_1m_input_tokens": 1.0,
                    "price_1m_output_tokens": 5.0,
                },
            }
        ]
    }

    def fail_price(*args, **kwargs):
        raise RuntimeError("fixture price write failed")

    monkeypatch.setattr(bench, "_upsert_price", fail_price)
    with pytest.raises(RuntimeError, match="fixture price write failed"):
        bench.sync_scores(api_key="fixture", path=path, request_fn=lambda *a, **k: payload)
    assert not any(row.model_id == "atomic-model" for row in bench.read_scores(path=path))


def test_ac2_invalid_prices_do_not_abort_scores_or_promote_partial_group(tmp_path):
    path = tmp_path / "bench.db"
    invalid = [
        ("bad-null-case", None),
        ("bad-string-case", "1.0"),
        ("bad-nan-case", float("nan")),
        ("bad-inf-case", float("inf")),
        ("bad-zero-case", 0.0),
        ("bad-negative-case", -1.0),
    ]
    invalid_ids = {model_id for model_id, _value in invalid} | {"bad-missing-case"}
    payload = {
        "data": [
            *[
                {
                    "slug": model_id,
                    "evaluations": {"artificial_analysis_coding_index": 50.0},
                    "pricing": {
                        "price_1m_blended_3_to_1": value,
                        "price_1m_input_tokens": 1.0,
                        "price_1m_output_tokens": 4.0,
                    },
                }
                for model_id, value in invalid
            ],
            {
                "slug": "bad-missing-case",
                "evaluations": {"artificial_analysis_coding_index": 50.0},
                "pricing": {
                    "price_1m_input_tokens": 1.0,
                    "price_1m_output_tokens": 4.0,
                },
            },
            {
                "slug": "known-price-case",
                "evaluations": {"artificial_analysis_coding_index": 49.0},
                "pricing": {
                    "price_1m_blended_3_to_1": 2.0,
                    "price_1m_input_tokens": 1.0,
                    "price_1m_output_tokens": 5.0,
                },
            },
        ]
    }

    assert bench.sync_scores(api_key="fixture", path=path, request_fn=lambda *a, **k: payload) == 8
    assert invalid_ids <= {row.model_id for row in bench.read_scores(path=path) if row.source == "AA-model"}
    prices = bench.read_prices(path=path)
    assert invalid_ids.isdisjoint(prices)

    profiles = [
        Profile("codex-luna", "unknown price", 50.0, aa_model_id="bad-null-case"),
        Profile("codex-terra", "known price", 49.0, aa_model_id="known-price-case"),
    ]
    output = recommend(
        [_provider("codex")],
        "A",
        today=TODAY,
        now=NOW,
        grade_table=_table("A", profiles),
        model_prices=prices,
    )
    assert _ranked_labels(output) == ["codex-luna", "codex-terra"]


def test_ac3_aplus_codex_pool_orders_luna_max_before_terra_xhigh_by_value():
    prices = {
        "gpt-6-luna": _price("gpt-6-luna", 0.1),  # ROB-591: codex-luna-max aa_model_id
        "gpt-5-6-terra": _price("gpt-5-6-terra", 1.0),
    }
    output = recommend([_provider("codex")], "A+", today=TODAY, now=NOW, model_prices=prices)
    assert _ranked_labels(output) == [
        "codex-luna-max",
        "codex-luna --effort xhigh",
        "codex-terra --effort xhigh",
        "codex-terra --effort high",
    ]


def test_ac4_s_has_terra_max_first_and_no_sol_in_candidates_or_alternatives():
    output = recommend(
        [_provider("codex")],
        "S",
        today=TODAY,
        now=NOW,
        model_prices={"gpt-5-6-terra": _price("gpt-5-6-terra", 1.0)},
    )
    assert _ranked_labels(output) == ["codex-terra-max"]
    assert "codex-sol" not in output
    assert "Sol" not in output


def test_ac5_value_sort_preserves_pool_slot_sequence_exactly():
    profiles = [
        Profile("codex-terra", "Terra", 40.0, aa_model_id="terra"),
        Profile("opus", "Opus", 50.0, aa_model_id="opus"),
        Profile("codex-luna", "Luna", 60.0, aa_model_id="luna"),
        Profile("kimi-k3", "Kimi", 50.0, aa_model_id="kimi"),
    ]
    providers = [_provider(name) for name in ("codex", "claude", "kimi")]
    table = _table("A", profiles)
    without_prices = recommend(providers, "A", today=TODAY, now=NOW, grade_table=table)
    with_prices = recommend(
        providers,
        "A",
        today=TODAY,
        now=NOW,
        grade_table=table,
        model_prices={
            "terra": _price("terra", 10.0),
            "opus": _price("opus", 5.0),
            "luna": _price("luna", 1.0),
            "kimi": _price("kimi", 2.0),
        },
    )
    before = _ranked_labels(without_prices)
    after = _ranked_labels(with_prices)
    assert (
        [profile_pool(name)[0] for name in before]
        == [profile_pool(name)[0] for name in after]
        == ["codex", "claude", "codex", "kimi"]
    )
    assert before == ["codex-terra", "opus", "codex-luna", "kimi-k3"]
    assert after == ["codex-luna", "opus", "codex-terra", "kimi-k3"]


def test_b1_base_order_oracle_and_priced_aplus_pool_sequence(monkeypatch):
    monkeypatch.setattr("scopefuel.recommend.get_boost", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr("scopefuel.recommend.get_capacity_weight", lambda *args, **kwargs: (1.0, None))
    pool_scopes: dict[str, set[str | None]] = {}
    for profiles in GRADE_TABLE.values():
        for profile in profiles:
            provider_id, group_name = profile_pool(profile.name)
            assert provider_id
            pool_scopes.setdefault(provider_id, set()).add(group_name)

    providers = []
    for provider_id, group_names in pool_scopes.items():
        buckets = [
            Bucket(
                label="7d",
                window="7d",
                used_pct=10.0,
                resets_at=(NOW + dt.timedelta(days=6)).isoformat(),
                scope=Scope("account") if group_name is None else Scope("group", group_name),
                horizon="week",
            )
            for group_name in group_names
        ]
        providers.append(ProviderResult(id=provider_id, pool_class="preserve", buckets=buckets))

    def label(profile: Profile) -> str:
        effort = f" --effort {profile.launcher_effort}" if profile.launcher_effort else ""
        return f"{profile.name}{effort}"

    expected: dict[str, list[str]] = {}
    actual: dict[str, list[str]] = {}
    for grade in GRADES:
        profiles = [profile for profile in GRADE_TABLE[grade] if profile.gate != "escalation"]
        expected[grade] = [
            label(profile)
            for profile in sorted(
                profiles,
                key=lambda profile: (
                    profile.benchmark is None,
                    next(
                        index
                        for index, candidate in enumerate(GRADE_TABLE[grade])
                        if candidate.name == profile.name
                    ),
                ),
            )
        ]
        actual[grade] = _ranked_labels(
            recommend(providers, grade, today=TODAY, now=NOW, grade_table=GRADE_TABLE)
        )

    assert [
        (index, profile.launcher_effort)
        for index, profile in enumerate(GRADE_TABLE["A+"])
        if profile.name == "codex-terra"
    ] == [(2, "xhigh"), (6, "high")]  # ROB-591: opus --effort low left A+ (moved to S)
    assert actual == expected

    priced_aplus = _ranked_labels(
        recommend(
            providers,
            "A+",
            today=TODAY,
            now=NOW,
            grade_table=GRADE_TABLE,
            model_prices={
                "gpt-6-luna": _price("gpt-6-luna", 0.1),  # ROB-591: codex-luna-max aa_model_id
                "gpt-5-6-terra": _price("gpt-5-6-terra", 1.0),
            },
        )
    )

    def pool_sequence(labels: list[str]) -> list[str]:
        return [profile_pool(item.split()[0])[0] for item in labels]

    assert pool_sequence(priced_aplus) == pool_sequence(actual["A+"])
    assert [item for item in priced_aplus if profile_pool(item.split()[0])[0] == "codex"] == [
        "codex-luna-max",
        "codex-luna --effort xhigh",
        "codex-terra --effort xhigh",
        "codex-terra --effort high",
    ]


def test_value_tie_keeps_effective_table_order_and_unscored_candidate_stays_last():
    profiles = [
        Profile("codex-terra", "Terra", 50.0, aa_model_id="terra"),
        Profile("codex-luna", "Luna", 100.0, aa_model_id="luna"),
        Profile("codex-luna", "Luna low", None, launcher_effort="low"),
    ]
    output = recommend(
        [_provider("codex")],
        "A",
        today=TODAY,
        now=NOW,
        grade_table=_table("A", profiles),
        model_prices={
            "terra": _price("terra", 2.0),
            "luna": _price("luna", 4.0),
        },
    )
    assert _ranked_labels(output) == [
        "codex-terra",
        "codex-luna",
        "codex-luna --effort low",
    ]


def test_ac7_sol_is_splus_only_and_absent_from_a_output():
    invalid = {grade: list(profiles) for grade, profiles in GRADE_TABLE.items()}
    invalid["A"].append(next(profile for profile in GRADE_TABLE["S+"] if profile.name == "codex-sol"))
    with pytest.raises(ValueError, match=r"Sol 계열 프로필은 S\+ 이외의 급"):
        validate_grade_table(invalid)

    output = recommend([_provider("codex")], "A", today=TODAY, now=NOW)
    assert "codex-sol" not in output
    assert all(
        profile.name not in {"codex-sol", "kiro-sol"}
        for grade, profiles in GRADE_TABLE.items()
        if grade != "S+"
        for profile in profiles
    )


def test_ac8_astra_is_absent_and_role_gate_denies_even_with_quota(monkeypatch, capsys):
    assert all(
        "astra" not in profile.name.casefold() and "astra" not in profile.model.casefold()
        for profiles in GRADE_TABLE.values()
        for profile in profiles
    )
    provider = _provider("codex", used=0.0)
    for profile_name in ASTRA_ROLE_PROFILES:
        result = gate_check([provider], profile_name, today=TODAY, now=NOW)
        assert result.ok is False
        assert result.unmeasurable is False
        assert "director 판정 전용" in result.reason

    monkeypatch.setattr(cli, "registry", lambda: {"codex": lambda: provider})
    assert cli.main(["gate", "-m", "codex-astra", "--no-cache"]) == 3
    assert "director 판정 전용" in capsys.readouterr().err


def test_ac9_operator_price_seeds_and_kimi_k27_code_a_candidate(tmp_path):
    prices = bench.read_prices(path=tmp_path / "missing.db")
    assert prices["kimi-k2-7-code"].price_1m_blended_3_to_1 == 1.7125
    assert prices["kimi-k2-7-code"].price_1m_input_tokens == 0.95
    assert prices["kimi-k2-7-code"].price_1m_output_tokens == 4.0
    assert prices["grok-4-6"].price_1m_blended_3_to_1 == 3.0

    db_path = tmp_path / "bench.db"
    payload = {
        "data": [
            {
                "slug": "kimi-k2-7-code",
                "evaluations": {"artificial_analysis_coding_index": 60.8},
                "pricing": {
                    "price_1m_blended_3_to_1": 2.0,
                    "price_1m_input_tokens": 1.0,
                    "price_1m_output_tokens": 5.0,
                },
            }
        ]
    }
    assert bench.sync_scores(api_key="fixture", path=db_path, request_fn=lambda *a, **k: payload) == 1
    prices = bench.read_prices(path=db_path)
    assert prices["kimi-k2-7-code"].price_1m_blended_3_to_1 == 2.0

    prices["kimi-k3"] = _price("kimi-k3", 2.0)
    output = recommend([_provider("kimi")], "A", today=TODAY, now=NOW, model_prices=prices)
    assert _ranked_labels(output) == ["kimi-k27-code", "kimi-k3-low"]
    profile = next(profile for profile in GRADE_TABLE["A"] if profile.name == "kimi-k27-code")
    assert profile.benchmark == 60.8
    assert profile.aa_model_id == "kimi-k2-7-code"
    assert profile.model_only is True
    assert profile.benchmark_annotation == MODEL_ONLY_ANNOTATION
    assert profile.placement_note and "운영자 승인 배치(A" in profile.placement_note


def test_ac10_grok_47_remains_and_46_is_not_registered():
    """ROB-591: Grok 4.6 → 4.7 refresh — same "old generation purged" invariant this
    test previously checked for the 4.5 → 4.6 refresh (4.3 was two generations stale)."""
    grok_profiles = [
        profile
        for profiles in GRADE_TABLE.values()
        for profile in profiles
        if profile.name.startswith("grok")
    ]
    assert grok_profiles
    assert all(
        "4.3" not in profile.model and "4-3" not in (profile.aa_model_id or "") for profile in grok_profiles
    )
    assert all(
        "4.6" not in profile.model and "4-6" not in (profile.aa_model_id or "") for profile in grok_profiles
    )
    assert any(profile.model == "Grok 4.7" for profile in grok_profiles)
    assert all(profile.aa_agent_model_id in (None, "grok-4.7") for profile in grok_profiles)


def test_ac14_explain_shows_value_math_and_partial_unknown_reason_only_in_explain():
    profiles = [
        Profile("codex-terra", "Terra", 50.0, aa_model_id="terra"),
        Profile("codex-luna", "Luna", 49.0, aa_model_id="luna"),
    ]
    kwargs = {
        "providers": [_provider("codex")],
        "grade": "A",
        "today": TODAY,
        "now": NOW,
        "grade_table": _table("A", profiles),
        "model_prices": {"terra": _price("terra", 2.0)},
    }
    plain = recommend(**kwargs)
    explained = recommend(**kwargs, explain=True)

    assert "가성비:" not in plain and "혼합단가" not in plain
    assert "가성비: 벤치 50 ÷ 혼합단가 $2/1M = 25.00" in explained
    assert "단가 미상 포함 — 표 순서 유지" in explained
    assert _ranked_labels(plain) == _ranked_labels(explained) == ["codex-terra", "codex-luna"]


def test_ac16_runtime_rejects_remote_sol_move_and_falls_back_without_network(monkeypatch, capsys):
    monkeypatch.setattr(
        bench,
        "bench_backend",
        lambda **kwargs: bench.BenchBackend(
            name=bench.BENCH_BACKEND_HANDOFFKEEP,
            cache_ttl_s=21600,
            url="https://fixture.invalid",
            token="fixture-token",
            endpoint_id="fixture",
        ),
    )
    monkeypatch.setattr(
        bench,
        "read_grades",
        lambda **kwargs: [
            bench.GradeAssignment(
                profile="codex-sol",
                grade="A",
                boundary_version="fixture",
                deviation_ref="fixture-role-bypass",
            )
        ],
    )

    table = bench.runtime_grade_table(path=":memory:")
    assert table is GRADE_TABLE
    assert capsys.readouterr().err.splitlines() == [
        "warning: handoffkeep bench grades failed boundary validation; using code grade table"
    ]
    output = recommend([_provider("codex")], "A", today=TODAY, now=NOW, grade_table=table)
    assert "codex-sol" not in output


def test_ac17_task_fixture_is_local_and_environment_isolated():
    assert os.environ.get("HANDOFFKEEP_URL") is None
    assert os.environ.get("HANDOFFKEEP_TOKEN") is None
    assert bench.bench_backend().name == bench.BENCH_BACKEND_LOCAL
    assert "pytest-" in str(bench.db_path()) or "/tmp" in str(bench.db_path())
