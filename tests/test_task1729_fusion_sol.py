"""hk 1729 Fusion-Sol catalog, Codex-pool, and free-limit contracts."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json

import pytest

from scopefuel import bench, cli, free_window, launch
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.free_window import FREE_PROFILES, OBSERVED_RUNG_IDS
from scopefuel.recommend import (
    DEVIN_FUSION_SOL61_ANNOTATION,
    GRADE_TABLE,
    PROVIDER_FAMILY_OVERRIDES,
    gate_check,
    is_rep_measured,
    profile_pool,
    provider_family,
    recommend_dict,
)

PROFILES = ("devin-fusion-sol61", "devin-fusion-sol61-xhigh")
MODEL_IDS = {
    "devin-fusion-sol61": "fusion-gpt-6-1-sol-high-sidekick-swe-2-medium",
    "devin-fusion-sol61-xhigh": "fusion-gpt-6-1-sol-xhigh-sidekick-swe-2-medium",
}
NOW = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)
RESET_1D = "2026-10-21T12:00:00Z"
RESET_7D = "2026-10-27T12:00:00Z"


class FrozenDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


def _providers() -> list[ProviderResult]:
    return [
        ProviderResult(
            id="codex",
            pool_class="spend",
            buckets=[Bucket("week", "7d", 30.0, RESET_7D, Scope("account"), "week")],
        ),
        ProviderResult(
            id="devin",
            pool_class="spend",
            buckets=[
                Bucket("daily", "1d", 10.0, RESET_1D, Scope("account"), "now"),
                Bucket("weekly", "7d", 10.0, RESET_7D, Scope("account"), "week"),
                *(
                    Bucket(
                        rung,
                        "",
                        0.0,
                        None,
                        Scope("model", rung),
                        "now",
                        "Free tag observed",
                    )
                    for rung in ("swe-2-high", "swe-2-medium", "swe-2-max")
                ),
            ],
        ),
    ]


def _profile_rows():
    return {
        profile.name: (grade, profile)
        for grade, profiles in GRADE_TABLE.items()
        for profile in profiles
        if profile.name in PROFILES
    }


def _install_cli_stubs(monkeypatch):
    providers = _providers()
    monkeypatch.setattr(cli, "registry", lambda: {"codex": object(), "devin": object()})
    monkeypatch.setattr(cli, "collect", lambda *args, **kwargs: providers)
    monkeypatch.setattr(cli.bench, "read_scores", lambda: [])
    monkeypatch.setattr(cli.bench, "read_prices", lambda: {})
    monkeypatch.setattr(cli.bench, "runtime_grade_table", lambda: GRADE_TABLE)
    monkeypatch.setattr(
        cli.bench,
        "read_catalog",
        lambda: bench.CatalogView(entries=launch.snapshot_entries(), source="snapshot"),
    )
    monkeypatch.setattr(cli, "_shadow_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_emit_stale_build_warning", lambda: None)


def _capture_cli(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = cli.main(argv)
    return rc, stdout.getvalue(), stderr.getvalue()


def test_rows_resolve_to_unmeasured_codex_pool_rungs_with_no_invented_price(tmp_path):
    rows = _profile_rows()
    assert set(rows) == set(PROFILES)
    snapshot = {entry.profile: entry for entry in launch.snapshot_entries() if entry.profile in PROFILES}
    assert set(snapshot) == set(PROFILES)

    for name, uid in MODEL_IDS.items():
        grade, profile = rows[name]
        entry = snapshot[name]
        assert grade == "B"
        assert profile.benchmark is None
        assert profile.benchmark_source is None
        assert profile.benchmark_annotation == DEVIN_FUSION_SOL61_ANNOTATION
        assert profile.benchmark_annotation.startswith("미측정")
        assert "급 실측(" not in profile.benchmark_annotation
        assert "promote" not in profile.benchmark_annotation.casefold()
        assert "hk 1729" in profile.benchmark_annotation
        assert "pool codex" in profile.benchmark_annotation
        assert not is_rep_measured(profile)
        assert entry.model_id == uid
        assert entry.pool == "codex"
        assert entry.grade == "B"
        assert entry.gate == "default"

        decision = launch.resolve_launch(
            name,
            view=bench.CatalogView(entries=launch.snapshot_entries(), source="snapshot"),
        )
        assert decision.pool == "codex"
        assert decision.family == "openai"
        assert decision.billing == launch.resolve_launch(
            "codex-sol",
            view=bench.CatalogView(entries=launch.snapshot_entries(), source="snapshot"),
        ).billing

    prices = bench.read_prices(path=tmp_path / "missing.sqlite")
    assert not (set(MODEL_IDS.values()) & set(prices))


def test_codex_tester_family_and_exact_model_id_mapping():
    from scopefuel.grades import MODEL_EQUIVALENCE

    for name, uid in MODEL_IDS.items():
        assert MODEL_EQUIVALENCE[uid] == frozenset({name})
        assert profile_pool(name) == ("codex", None)
        assert provider_family(name) == "openai"
        assert PROVIDER_FAMILY_OVERRIDES[name] == "openai"
        assert profile_pool("codex-sol") == ("codex", None)

    assert provider_family("devin-fusion-opus55") == "claude"
    assert provider_family("devin-fusion-sonnet55") == "claude"
    assert provider_family("devin-argon") == "google"
    assert all(profile_pool(name) == ("devin", None) for name in (
        name
        for profiles in GRADE_TABLE.values()
        for profile in profiles
        for name in (profile.name,)
        if name.startswith("devin-") and name not in PROFILES
    ))


@pytest.mark.parametrize(
    "fast_id",
    (
        "fusion-gpt-6-1-sol-high-fast-sidekick-swe-2-medium",
        "fusion-gpt-6-1-sol-xhigh-fast-sidekick-swe-2-medium",
        "fusion-gpt-6-1-sol-high-sidekick-swe-2-medium-fast",
        "fusion-gpt-6-1-sol-xhigh-sidekick-swe-2-medium-fast",
    ),
)
def test_fast_model_ids_do_not_resolve_to_supported_profiles(fast_id):
    from scopefuel.grades import MODEL_EQUIVALENCE

    assert fast_id not in MODEL_EQUIVALENCE
    with pytest.raises(launch.LaunchError, match="not in the catalog"):
        launch.resolve_launch(
            fast_id,
            view=bench.CatalogView(entries=launch.snapshot_entries(), source="snapshot"),
        )


def test_fusion_sol_reads_codex_quota_and_live_hold_only_blocks_six_free_rungs(monkeypatch):
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch)

    # Codex has usable headroom while Devin is intentionally exhausted. This
    # makes a wrong pool route fail at the gate instead of looking plausible.
    providers = _providers()
    providers[1].buckets[0] = Bucket("daily", "1d", 99.0, RESET_1D, Scope("account"), "now")
    providers[1].buckets[1] = Bucket("weekly", "7d", 99.0, RESET_7D, Scope("account"), "week")
    monkeypatch.setattr(cli, "collect", lambda *args, **kwargs: providers)

    table = {grade: [] for grade in GRADE_TABLE}
    table["B"] = [profile for _, profile in _profile_rows().values()]
    baseline_rows = recommend_dict(
        providers, "B", today=NOW.date(), now=NOW, grade_table=table
    )["rows"]
    baseline_target_rows = {row["profile"]: row for row in baseline_rows if row["profile"] in PROFILES}
    assert set(baseline_target_rows) == set(PROFILES)
    for row in baseline_target_rows.values():
        assert row["kind"] == "candidate"
        assert row["provider"] == "codex"
        assert row["billing"] == launch.resolve_launch(
            "codex-sol",
            view=bench.CatalogView(entries=launch.snapshot_entries(), source="snapshot"),
        ).billing

    baseline_gate = {name: _capture_cli(["gate", "-m", name]) for name in PROFILES}
    assert all(result[0] == 0 and "pool=codex" in result[1] for result in baseline_gate.values())

    rc, out, err = _capture_cli(
        [
            "observe",
            "devin-free-limit",
            "--reset",
            (NOW + dt.timedelta(minutes=45)).isoformat(),
            "--observed",
            (NOW - dt.timedelta(minutes=5)).isoformat(),
            "--rung",
            "devin-fusion-sol61",
            "--source",
            "job:hk1729-sol61",
        ]
    )
    assert rc == 0, (out, err)
    record = free_window.active_record(now=NOW)
    assert record is not None
    assert record.rung == "devin-fusion-sol61"
    assert record.source == "job:hk1729-sol61"
    assert "devin-fusion-sol61" in OBSERVED_RUNG_IDS
    assert "devin-fusion-sol61-xhigh" in OBSERVED_RUNG_IDS
    assert not (set(PROFILES) & FREE_PROFILES)

    for name in PROFILES:
        assert _capture_cli(["gate", "-m", name]) == baseline_gate[name]

    held_rows = recommend_dict(
        providers,
        "B",
        today=NOW.date(),
        now=NOW,
        grade_table=table,
        free_rate_limit=free_window.reason(record),
    )["rows"]
    held_target_rows = {row["profile"]: row for row in held_rows if row["profile"] in PROFILES}
    assert held_target_rows == baseline_target_rows

    for free_profile in sorted(FREE_PROFILES):
        rc, _out, err = _capture_cli(["gate", "-m", free_profile])
        assert rc == 3, (free_profile, err)
        assert "free_rate_limited" in err, (free_profile, err)
        assert "rung devin-fusion-sol61" in err, (free_profile, err)

