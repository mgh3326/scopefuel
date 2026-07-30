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


# ------------------------------------------------------------------ JSON numeric shape


def test_spend_high_usage_json_keeps_raw_used_pct():
    result = ProviderResult(id="kiro", pool_class="spend", buckets=[bucket(91.0)])
    payload = result.as_dict()
    assert payload["pool_class"] == "spend"
    assert payload["verdict"]["mark"] == "ok"
    assert payload["buckets"][0]["used_pct"] == 91.0
    assert payload["buckets"][0]["severity"] == "ok"
