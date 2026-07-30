"""ROB-1178 — provider pool class (preserve/spend) + WASTE informational signal."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from scopefuel import cache, cli, render
from scopefuel.model import (
    CRIT_PCT,
    WARN_PCT,
    WASTE_WINDOW_S,
    Bucket,
    ProviderResult,
    Scope,
    overall_mark,
    verdict_for,
)
from scopefuel.providers import BUILTIN

NOW = dt.datetime(2026, 7, 31, 0, 0, 0, tzinfo=dt.UTC)


def bucket(
    used: float | None,
    *,
    label: str = "credits",
    window: str = "30d",
    horizon: str = "week",
    reset_delta_s: float | None = None,
    scope: Scope | None = None,
) -> Bucket:
    resets_at = None
    if reset_delta_s is not None:
        resets_at = (NOW + dt.timedelta(seconds=reset_delta_s)).isoformat()
    return Bucket(
        label=label,
        window=window,
        used_pct=used,
        resets_at=resets_at,
        scope=scope or Scope("account"),
        horizon=horizon,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ metadata


def test_builtin_providers_have_pool_class_metadata():
    assert BUILTIN["claude"].pool_class == "preserve"
    assert BUILTIN["codex"].pool_class == "preserve"
    assert BUILTIN["kiro"].pool_class == "spend"
    assert BUILTIN["clinepass"].pool_class == "spend"
    assert BUILTIN["agy"].pool_class == "spend"


def test_default_provider_result_is_preserve():
    result = ProviderResult(id="x")
    assert result.pool_class == "preserve"


# ------------------------------------------------------------------ spend suppression


def test_spend_high_usage_is_ok_not_crit():
    result = ProviderResult(id="kiro", pool_class="spend", buckets=[bucket(91.0)])
    assert result.verdict.mark == "ok"
    assert result.verdict.blocking_pct == 91.0


def test_spend_high_usage_bucket_severity_is_ok():
    result = ProviderResult(id="kiro", pool_class="spend", buckets=[bucket(91.0)])
    payload = result.as_dict()
    assert payload["verdict"]["mark"] == "ok"
    assert payload["buckets"][0]["severity"] == "ok"
    assert payload["buckets"][0]["used_pct"] == 91.0


def test_spend_does_not_populate_exhausted():
    result = ProviderResult(
        id="agy",
        pool_class="spend",
        buckets=[bucket(100.0, scope=Scope("group", "gemini"), window="5h", horizon="now")],
    )
    assert result.verdict.exhausted == []
    assert result.verdict.mark == "ok"


def test_preserve_exhausted_and_crit_unchanged():
    result = ProviderResult(
        id="agy",
        pool_class="preserve",
        buckets=[bucket(100.0, scope=Scope("group", "gemini"), window="5h", horizon="now")],
    )
    assert len(result.verdict.exhausted) == 1
    assert result.verdict.mark == "crit"


# ------------------------------------------------------------------ WASTE boundaries


def test_spend_69_9_with_23_59_59_reset_is_waste():
    result = ProviderResult(
        id="kiro",
        pool_class="spend",
        buckets=[bucket(69.9, reset_delta_s=WASTE_WINDOW_S - 1)],
    )
    assert result.verdict_at(NOW).waste is True
    assert "리셋 전 소진 권장" in (result.verdict_at(NOW).waste_advice or "")


def test_spend_exactly_70_pct_is_not_waste():
    result = ProviderResult(
        id="kiro",
        pool_class="spend",
        buckets=[bucket(70.0, reset_delta_s=WASTE_WINDOW_S - 1)],
    )
    assert result.verdict_at(NOW).waste is False


def test_spend_exactly_24h_reset_is_not_waste():
    result = ProviderResult(
        id="kiro",
        pool_class="spend",
        buckets=[bucket(69.9, reset_delta_s=WASTE_WINDOW_S)],
    )
    assert result.verdict_at(NOW).waste is False


@pytest.mark.parametrize(
    "used,reset_delta_s",
    [
        (69.9, None),
        (69.9, -1),
        (69.9, 0),
        (None, WASTE_WINDOW_S - 1),
        (float("nan"), WASTE_WINDOW_S - 1),
        (float("inf"), WASTE_WINDOW_S - 1),
        (-1, WASTE_WINDOW_S - 1),
        (101, WASTE_WINDOW_S - 1),
        (True, WASTE_WINDOW_S - 1),
    ],
)
def test_invalid_or_past_inputs_are_not_waste(used, reset_delta_s):
    result = ProviderResult(
        id="kiro",
        pool_class="spend",
        buckets=[bucket(used, reset_delta_s=reset_delta_s)],
    )
    assert result.verdict_at(NOW).waste is False


def test_preserve_is_never_waste():
    result = ProviderResult(
        id="claude",
        pool_class="preserve",
        buckets=[bucket(10.0, reset_delta_s=3600)],
    )
    assert result.verdict_at(NOW).waste is False


# ------------------------------------------------------------------ failure precedence


def test_spend_error_is_degraded_not_waste():
    result = ProviderResult(
        id="kiro",
        pool_class="spend",
        buckets=[bucket(20.0, reset_delta_s=3600)],
        error="kiro-cli 실패",
    )
    assert result.verdict.mark == "degraded"
    assert result.verdict.waste is False


def test_spend_warning_suppresses_waste():
    result = ProviderResult(
        id="clinepass",
        pool_class="spend",
        buckets=[bucket(20.0, reset_delta_s=3600)],
        warning="인증 실패",
    )
    assert result.verdict.mark == "warn"
    assert result.verdict.waste is False


def test_spend_stale_suppresses_waste():
    result = ProviderResult(
        id="kiro",
        pool_class="spend",
        buckets=[bucket(20.0, reset_delta_s=3600)],
        stale=True,
    )
    assert result.verdict.mark == "degraded"
    assert result.verdict.waste is False


# ------------------------------------------------------------------ preserve thresholds


def test_preserve_thresholds_unchanged():
    assert verdict_for([bucket(WARN_PCT - 0.001)]).mark == "ok"
    assert verdict_for([bucket(WARN_PCT)]).mark == "warn"
    assert verdict_for([bucket(CRIT_PCT - 0.001)]).mark == "warn"
    assert verdict_for([bucket(CRIT_PCT)]).mark == "crit"


# ------------------------------------------------------------------ overall / exit


def test_waste_only_does_not_escalate_overall_mark():
    waste = ProviderResult(
        id="kiro",
        pool_class="spend",
        buckets=[bucket(20.0, reset_delta_s=3600)],
    )
    assert waste.verdict_at(NOW).waste is True
    assert overall_mark([waste]) == "ok"


def test_waste_only_exit_code_is_success(capsys, monkeypatch):
    def fetcher():
        return ProviderResult(
            id="kiro",
            pool_class="spend",
            buckets=[bucket(20.0, reset_delta_s=3600)],
        )

    fetcher.pool_class = "spend"  # type: ignore[attr-defined]
    monkeypatch.setattr(cli, "registry", lambda: {"kiro": fetcher})

    class MockDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(cli.dt, "datetime", MockDatetime)
    assert cli.main(["--brief", "--no-cache", "--exit-code-on", "warn"]) == 0
    assert "WASTE" in capsys.readouterr().out


def test_preserve_warn_still_triggers_exit_code_on_warn(capsys, monkeypatch):
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {
            "claude": lambda: ProviderResult(
                id="claude",
                pool_class="preserve",
                buckets=[bucket(WARN_PCT)],
            )
        },
    )
    assert cli.main(["--brief", "--no-cache", "--exit-code-on", "warn"]) == 2


# ------------------------------------------------------------------ render


def test_table_shows_spend_progress_without_crit():
    result = ProviderResult(id="kiro", pool_class="spend", buckets=[bucket(91.0)])
    out = render.table([result], color=False, now=NOW)
    assert "[ok]" in out
    assert "소진 진행" in out
    assert "CRIT" not in out
    assert "소진(" not in out


def test_table_shows_waste():
    result = ProviderResult(
        id="kiro", pool_class="spend", buckets=[bucket(69.9, reset_delta_s=WASTE_WINDOW_S - 1)]
    )
    out = render.table([result], color=False, now=NOW)
    assert "WASTE" in out
    assert "리셋 전 소진 권장" in out


def test_brief_shows_waste():
    result = ProviderResult(
        id="kiro", pool_class="spend", buckets=[bucket(69.9, reset_delta_s=WASTE_WINDOW_S - 1)]
    )
    out = render.brief([result], color=False, now=NOW)
    assert "WASTE" in out


def test_table_failure_not_relabeled_waste():
    result = ProviderResult(
        id="kiro",
        pool_class="spend",
        buckets=[bucket(20.0, reset_delta_s=3600)],
        error="boom",
    )
    out = render.table([result], color=False, now=NOW)
    assert "WASTE" not in out
    assert "소진 진행" not in out


# ------------------------------------------------------------------ multi-axis / group


def test_clinepass_multi_axis_waste_per_bucket():
    result = ProviderResult(
        id="clinepass",
        pool_class="spend",
        buckets=[
            bucket(69.9, label="five_hour", window="5h", horizon="now", reset_delta_s=3600),
            bucket(95.0, label="weekly", window="7d", horizon="week", reset_delta_s=3600),
            bucket(20.0, label="monthly", window="30d", horizon="month", reset_delta_s=WASTE_WINDOW_S + 1),
        ],
    )
    v = result.verdict_at(NOW)
    assert v.waste is True
    advice = v.waste_advice or ""
    assert "five_hour" in advice
    assert "weekly" not in advice
    assert "monthly" not in advice


def test_agy_group_independent_waste():
    result = ProviderResult(
        id="agy",
        pool_class="spend",
        buckets=[
            bucket(
                20.0,
                label="gemini 5h",
                window="5h",
                horizon="now",
                reset_delta_s=3600,
                scope=Scope("group", "gemini"),
            ),
            bucket(
                95.0,
                label="3p 5h",
                window="5h",
                horizon="now",
                reset_delta_s=3600,
                scope=Scope("group", "3p"),
            ),
        ],
    )
    v = result.verdict_at(NOW)
    assert v.waste is True
    assert "gemini" in (v.waste_advice or "")
    assert "3p" not in (v.waste_advice or "")


# ------------------------------------------------------------------ cache / spec


def test_cache_round_trips_pool_class(monkeypatch):
    spend = ProviderResult(id="kiro", pool_class="spend", buckets=[bucket(50.0)])
    fetchers = {"kiro": lambda: spend}
    fetchers["kiro"].pool_class = "spend"  # type: ignore[attr-defined]
    first = cache.collect(fetchers, ["kiro"], now=1000.0)
    assert first[0].pool_class == "spend"

    def error_fetcher():
        raise RuntimeError("x")

    error_fetcher.pool_class = "spend"  # type: ignore[attr-defined]
    second = cache.collect({"kiro": error_fetcher}, ["kiro"], now=1000.0 + 120, ttl_s=60)
    assert second[0].pool_class == "spend"
    assert second[0].stale is True


def test_legacy_cache_without_pool_class_defaults_to_registry_class(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOPEFUEL_CACHE", str(tmp_path / "snapshots.json"))
    cache_path = tmp_path / "snapshots.json"
    cache_path.write_text(
        json.dumps(
            {
                "kiro": {
                    "fetched_at": 1.0,
                    "result": {"id": "kiro", "buckets": [], "pool_class": "preserve"},
                }
            }
        )
    )

    def fetcher():
        return ProviderResult(id="kiro")

    fetcher.pool_class = "spend"  # type: ignore[attr-defined]
    result = cache.collect({"kiro": fetcher}, ["kiro"], now=100.0, ttl_s=0.0)[0]
    assert result.pool_class == "spend"


def test_spec_supports_class_spend(tmp_path, monkeypatch):
    from scopefuel import spec

    creds = tmp_path / "auth.json"
    creds.write_text(json.dumps({"tokens": {"access_token": "tok"}}))
    monkeypatch.setattr(spec, "request_json", lambda *a, **k: {"usage": 12, "reset": "2099-01-01T00:00:00Z"})
    provider = spec.make_provider(
        {
            "id": "myplan",
            "class": "spend",
            "credentials": {"file": str(creds), "token_path": ["tokens", "access_token"]},
            "request": {"url": "https://example.test/u"},
            "buckets": [
                {
                    "label": "5h",
                    "window": "5h",
                    "used_pct_path": ["usage"],
                    "resets_at_path": ["reset"],
                }
            ],
        }
    )
    assert provider.pool_class == "spend"
    result = provider()
    assert result.pool_class == "spend"


def test_spec_rejects_invalid_class(tmp_path):
    from scopefuel import spec

    with pytest.raises(spec.SpecError, match="class"):
        spec.make_provider(
            {
                "id": "bad",
                "class": "gold",
                "request": {"url": "https://example.test/u"},
            }
        )


def test_spec_omitted_class_defaults_preserve(tmp_path, monkeypatch):
    from scopefuel import spec

    creds = tmp_path / "auth.json"
    creds.write_text(json.dumps({"tokens": {"access_token": "tok"}}))
    monkeypatch.setattr(spec, "request_json", lambda *a, **k: {"usage": 12})
    provider = spec.make_provider(
        {
            "id": "myplan",
            "credentials": {"file": str(creds), "token_path": ["tokens", "access_token"]},
            "request": {"url": "https://example.test/u"},
            "buckets": [{"label": "5h", "window": "5h", "used_pct_path": ["usage"]}],
        }
    )
    assert provider.pool_class == "preserve"


# ------------------------------------------------------------------ regression tests for audit findings


def test_stale_brief_suppresses_progress_and_waste():
    result = ProviderResult(
        id="kiro",
        pool_class="spend",
        buckets=[bucket(20.0, reset_delta_s=3600)],
        age_s=600.0,
        stale=True,
    )
    out = render.brief([result], color=False, now=NOW)
    assert "(소진 진행)" not in out
    assert "WASTE" not in out
    assert out.count("10분 전") == 1


@pytest.mark.parametrize(
    "invalid_val",
    [float("nan"), float("inf"), float("-inf"), -1.0, 101.0, True],
)
def test_invalid_numeric_matrix_does_not_emit_progress_or_waste(invalid_val):
    b = Bucket("credits", "30d", invalid_val, scope=Scope("account"), horizon="week")
    result = ProviderResult(id="kiro", pool_class="spend", buckets=[b])
    v = result.verdict_at(NOW)
    assert v.basis == "none"
    assert v.waste is False

    tbl = render.table([result], color=False, now=NOW)
    brf = render.brief([result], color=False, now=NOW)
    assert "소진 진행" not in tbl
    assert "소진 진행" not in brf
    assert "nanx" not in tbl and "infx" not in tbl
    assert "nanx" not in brf and "infx" not in brf


def test_toml_and_agy_bool_ingestion_end_to_end(tmp_path, monkeypatch):
    from scopefuel import spec
    from scopefuel.providers import agy

    creds = tmp_path / "auth.json"
    creds.write_text(json.dumps({"tokens": {"access_token": "tok"}}))
    monkeypatch.setattr(spec, "request_json", lambda *a, **k: {"usage": True, "remaining": True})
    provider_used = spec.make_provider(
        {
            "id": "myplan",
            "class": "spend",
            "credentials": {"file": str(creds), "token_path": ["tokens", "access_token"]},
            "request": {"url": "https://example.test/u"},
            "buckets": [{"label": "5h", "window": "5h", "used_pct_path": ["usage"]}],
        }
    )
    res1 = provider_used()
    assert res1.buckets[0].used_pct is None

    provider_rem = spec.make_provider(
        {
            "id": "myplan2",
            "class": "spend",
            "credentials": {"file": str(creds), "token_path": ["tokens", "access_token"]},
            "request": {"url": "https://example.test/u"},
            "buckets": [{"label": "5h", "window": "5h", "remaining_fraction_path": ["remaining"]}],
        }
    )
    res2 = provider_rem()
    assert res2.buckets[0].used_pct is None

    # test agy local with bool
    raw_local = {
        "response": {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [{"window": "5h", "remainingFraction": True}],
                }
            ]
        }
    }
    agy_res = agy._from_local(raw_local)
    assert agy_res.buckets[0].used_pct is None


def test_plugin_callable_non_mutation_slots_and_precedence(monkeypatch):
    # 1. Plain function without pool_class metadata
    def plain_plugin():
        return ProviderResult(id="custom", pool_class="spend", buckets=[bucket(50.0)])

    assert not hasattr(plain_plugin, "pool_class")

    # 2. Slotted / non-assignable callable class
    class SlottedPlugin:
        __slots__ = ()

        def __call__(self):
            return ProviderResult(id="slotted", pool_class="spend", buckets=[bucket(40.0)])

    slotted_instance = SlottedPlugin()
    with pytest.raises(AttributeError):
        slotted_instance.pool_class = "spend"

    fetchers = {
        "plain": plain_plugin,
        "slotted": slotted_instance,
    }

    results = cache.collect(fetchers, ["plain", "slotted"], use_cache=False)
    # plain_plugin was NOT mutated
    assert not hasattr(plain_plugin, "pool_class")
    # returned ProviderResult.pool_class "spend" was preserved
    assert results[0].pool_class == "spend"
    assert results[1].pool_class == "spend"

    # 3. Precedence: explicit metadata > returned class
    def metadata_plugin():
        return ProviderResult(id="meta", pool_class="preserve")

    wrapped_meta = cache._pool_class(metadata_plugin)
    assert wrapped_meta is None

    from scopefuel.providers import _with_class

    meta_fetcher = _with_class(metadata_plugin, "spend")
    res_meta = cache.collect({"meta": meta_fetcher}, ["meta"], use_cache=False)[0]
    assert res_meta.pool_class == "spend"


def test_injected_clock_pace_consistency():
    t0 = NOW
    t_captured = t0 + dt.timedelta(hours=2.5)  # 5h window half elapsed -> pace ratio 1.0 for used_pct=50%
    b = Bucket(
        "5h",
        "5h",
        50.0,
        resets_at=(t0 + dt.timedelta(hours=5)).isoformat(),
        scope=Scope("account"),
        horizon="now",
    )
    res = ProviderResult(id="test", pool_class="preserve", buckets=[b])

    dict_out = res.as_dict(now=t_captured)
    assert dict_out["buckets"][0]["pace"] == 1.0

    tbl = render.table([res], color=False, now=t_captured)
    assert "1.00x" in tbl

    brf = render.brief([res], color=False, now=t_captured)
    assert "1.0x" in brf


# ------------------------------------------------------------------ JSON numeric shape


def test_spend_high_usage_json_keeps_raw_used_pct():
    result = ProviderResult(id="kiro", pool_class="spend", buckets=[bucket(91.0)])
    payload = result.as_dict()
    assert payload["pool_class"] == "spend"
    assert payload["verdict"]["mark"] == "ok"
    assert payload["buckets"][0]["used_pct"] == 91.0
    assert payload["buckets"][0]["severity"] == "ok"


# ------------------------------------------------------------------ captured-time consistency (Fix 1)


def test_verdict_as_dict_passes_now_to_exhausted_pace():
    t0 = NOW
    t_captured = t0 + dt.timedelta(hours=2.5)
    ex = Bucket(
        "5h",
        "5h",
        95.0,
        resets_at=(t0 + dt.timedelta(hours=5)).isoformat(),
        scope=Scope("model", "Fable"),
        horizon="now",
    )
    from scopefuel.model import Verdict

    v = Verdict(
        now_pct=95.0,
        week_pct=None,
        month_pct=None,
        blocking_pct=95.0,
        basis="account",
        mark="crit",
        exhausted=[ex],
    )
    dict_normal = v.as_dict(now=t_captured)
    dict_no_now = v.as_dict()
    pace_captured = dict_normal["exhausted"][0]["pace"]
    pace_no_now = dict_no_now["exhausted"][0]["pace"]
    assert pace_captured is not None
    assert pace_captured != pace_no_now


def test_provider_result_as_dict_passes_now_to_verdict_exhausted():
    t0 = NOW
    t_captured = t0 + dt.timedelta(hours=2.5)
    ex = Bucket(
        "5h",
        "5h",
        95.0,
        resets_at=(t0 + dt.timedelta(hours=5)).isoformat(),
        scope=Scope("model", "Fable"),
        horizon="now",
    )
    res = ProviderResult(id="test", pool_class="preserve", buckets=[ex])
    payload = res.as_dict(now=t_captured)
    if payload["verdict"]["exhausted"]:
        exhausted_pace = payload["verdict"]["exhausted"][0]["pace"]
        self_pace = ex.pace_at(t_captured).ratio
        assert exhausted_pace == self_pace


# --------------------------------------------------------- wrapper false-green regression tests (Fix 2)


def test_with_class_does_not_mutate_original_callable():
    from scopefuel.providers import _with_class

    def my_fn():
        return ProviderResult(id="test", pool_class="spend")

    assert not hasattr(my_fn, "pool_class")
    wrapped = _with_class(my_fn, "spend")
    assert hasattr(wrapped, "pool_class")
    assert wrapped.pool_class == "spend"
    assert not hasattr(my_fn, "pool_class")
    from scopefuel.providers import FetcherWrapper

    assert isinstance(wrapped, FetcherWrapper)


def test_with_class_slotted_callable_not_mutated_and_not_dropped():
    from scopefuel.providers import _with_class

    class SlottedFn:
        __slots__ = ()

        def __call__(self):
            return ProviderResult(id="slotted", pool_class="spend", buckets=[bucket(40.0)])

    slotted = SlottedFn()
    with pytest.raises(AttributeError):
        slotted.pool_class = "spend"
    wrapped = _with_class(slotted, "spend")
    assert not hasattr(slotted, "pool_class")
    result = wrapped()
    assert result.pool_class == "spend"


def test_with_class_readonly_setattr_not_dropped():
    from scopefuel.providers import _with_class

    class GuardFn:
        def __call__(self):
            return ProviderResult(id="guarded", pool_class="spend", buckets=[bucket(50.0)])

        def __setattr__(self, name, value):
            raise AttributeError("read-only")

    guarded = GuardFn()
    with pytest.raises(AttributeError):
        guarded.pool_class = "spend"
    wrapped = _with_class(guarded, "spend")
    assert not hasattr(guarded, "pool_class")
    result = wrapped()
    assert result.pool_class == "spend"


def test_with_class_precedence_metadata_overrides_returned():
    from scopefuel.providers import _with_class

    def returns_preserve():
        return ProviderResult(id="meta", pool_class="preserve")

    wrapped = _with_class(returns_preserve, "spend")
    results = cache.collect({"meta": wrapped}, ["meta"], use_cache=False)
    assert results[0].pool_class == "spend"
    assert not hasattr(returns_preserve, "pool_class")


# --------------------------------------------------------- invalid usage output normalization (Fix 3)


@pytest.mark.parametrize(
    "invalid_val",
    [float("nan"), float("inf"), float("-inf"), -1.0, 101.0, True, "nope", [], {}],
)
def test_bucket_as_dict_normalizes_invalid_used_pct(invalid_val):
    b = Bucket("credits", "30d", invalid_val, scope=Scope("account"), horizon="week")
    d = b.as_dict()
    assert d["used_pct"] is None
    assert d["remaining_pct"] is None
    assert d["severity"] == "ok"


def test_render_pct_shows_question_for_invalid():
    from scopefuel.render import _pct

    assert _pct(float("nan")) == "?"
    assert _pct(float("inf")) == "?"
    assert _pct(float("-inf")) == "?"
    assert _pct(-1.0) == "?"
    assert _pct(101.0) == "?"
    assert _pct(None) == "?"
    assert _pct(0.0) == "사용 0%"
    assert _pct(50.0) == "사용 50%"
    assert _pct(100.0) == "사용 100%"


def test_cli_json_does_not_emit_nan_or_infinity(capsys, monkeypatch):
    from scopefuel import cli

    b = Bucket("credits", "30d", float("nan"), scope=Scope("account"), horizon="week")
    result = ProviderResult(id="test", pool_class="preserve", buckets=[b])
    monkeypatch.setattr(cli, "registry", lambda: {"test": lambda: result})
    ec = cli.main(["--json", "--no-cache"])
    out = capsys.readouterr().out
    assert "NaN" not in out
    assert "Infinity" not in out
    assert "-Infinity" not in out
    assert ec == 0


def test_cache_restore_normalizes_invalid_used_pct(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOPEFUEL_CACHE", str(tmp_path / "snapshots.json"))
    cache_path = tmp_path / "snapshots.json"
    cache_path.write_text(
        json.dumps(
            {
                "test": {
                    "fetched_at": 1.0,
                    "result": {
                        "id": "test",
                        "buckets": [
                            {
                                "label": "x",
                                "window": "5h",
                                "used_pct": float("nan"),
                                "scope": {"kind": "account"},
                                "horizon": "now",
                            }
                        ],
                        "pool_class": "preserve",
                    },
                }
            }
        )
    )
    from scopefuel import cache as cache_mod

    def fetcher():
        raise RuntimeError("trigger stale")

    fetcher.pool_class = "preserve"
    results = cache_mod.collect({"test": fetcher}, ["test"], now=100.0, ttl_s=0.0)
    assert results[0].buckets[0].used_pct is None


# ------------------------------------------------------------------ OverflowError numeric converters (Fix 4)


class OverflowNum:
    def __float__(self):
        raise OverflowError("too large")


def test_invalid_used_pct_rejects_overflow():
    from scopefuel.model import _is_valid_used_pct

    assert _is_valid_used_pct(OverflowNum()) is False
    assert _is_valid_used_pct(10**1000) is False
    assert _is_valid_used_pct(2**1024) is False


def test_spec_used_pct_catches_overflow(monkeypatch, tmp_path):
    from scopefuel import spec

    creds = tmp_path / "auth.json"
    creds.write_text(json.dumps({"tokens": {"access_token": "tok"}}))
    monkeypatch.setattr(spec, "request_json", lambda *a, **k: {"usage": 10**1000})
    provider = spec.make_provider(
        {
            "id": "myplan",
            "class": "spend",
            "credentials": {"file": str(creds), "token_path": ["tokens", "access_token"]},
            "request": {"url": "https://example.test/u"},
            "buckets": [{"label": "5h", "window": "5h", "used_pct_path": ["usage"]}],
        }
    )
    result = provider()
    assert result.buckets[0].used_pct is None


# ------------------------------------------------------------------ warning+stale brief precedence (Fix 5)


def test_warning_stale_brief_preserves_age_and_degraded():
    result = ProviderResult(
        id="kiro",
        pool_class="spend",
        buckets=[bucket(20.0, reset_delta_s=3600)],
        warning="auth fail",
        age_s=600.0,
        stale=True,
    )
    out = render.brief([result], color=False, now=NOW)
    assert "[DEGRADED]" in out
    assert "auth fail" in out
    assert "10분 전" in out
    assert out.count("10분 전") == 1


def test_warning_only_brief_unchanged():
    result = ProviderResult(
        id="kiro",
        pool_class="spend",
        buckets=[bucket(20.0, reset_delta_s=3600)],
        warning="auth fail",
    )
    out = render.brief([result], color=False, now=NOW)
    assert "[WARN]" in out
    assert "auth fail" in out
    assert "10분 전" not in out


def test_stale_only_brief_unchanged():
    result = ProviderResult(
        id="kiro",
        pool_class="spend",
        buckets=[bucket(20.0, reset_delta_s=3600)],
        age_s=600.0,
        stale=True,
    )
    out = render.brief([result], color=False, now=NOW)
    assert "[DEGRADED]" in out
    assert "10분 전" in out
    assert out.count("10분 전") == 1


# ------------------------------------------------------------------ invalid pool_class validation (Fix 6)


@pytest.mark.parametrize("invalid", ["gold", "SPEND", "", 123, True, None])
def test_provider_result_rejects_invalid_pool_class(invalid):
    r = ProviderResult(id="x", pool_class=invalid)
    assert r.pool_class == "preserve"


def test_cache_from_entry_normalizes_invalid_pool_class(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOPEFUEL_CACHE", str(tmp_path / "snapshots.json"))
    cache_path = tmp_path / "snapshots.json"
    cache_path.write_text(
        json.dumps(
            {
                "test": {
                    "fetched_at": 1.0,
                    "result": {
                        "id": "test",
                        "buckets": [],
                        "pool_class": "gold",
                    },
                }
            }
        )
    )
    from scopefuel import cache as cache_mod

    results = cache_mod.collect({"test": lambda: ProviderResult(id="test")}, ["test"], now=100.0, ttl_s=0.0)
    assert results[0].pool_class == "preserve"


def test_to_entry_pool_class_round_trip_valid():
    r = ProviderResult(id="x", pool_class="spend", buckets=[bucket(50.0)])
    entry = cache._to_entry(r, 100.0)
    restored = cache._from_entry(entry, "x", 200.0, None)
    assert restored.pool_class == "spend"


# ------------------------------------------------------------------ strict JSON round-trip


def test_full_json_serialization_allow_nan_false():
    result = ProviderResult(id="kiro", pool_class="spend", buckets=[bucket(91.0)])
    payload = result.as_dict()
    dumped = json.dumps(payload, allow_nan=False)
    loaded = json.loads(dumped)
    assert loaded["buckets"][0]["used_pct"] == 91.0
    assert loaded["pool_class"] == "spend"


def test_invalid_values_in_json_do_not_break_serialization():
    b = Bucket("credits", "30d", float("nan"), scope=Scope("account"), horizon="week")
    result = ProviderResult(id="test", pool_class="preserve", buckets=[b])
    payload = result.as_dict()
    dumped = json.dumps(payload, allow_nan=False)
    assert "NaN" not in dumped
