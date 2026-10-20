"""hk 1729 Fusion-Sol catalog, Codex-pool, and free-limit contracts."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json

import pytest

from scopefuel import bench, cli, free_window, launch
from scopefuel.free_window import FREE_PROFILES, OBSERVED_RUNG_IDS
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    DEVIN_FUSION_SOL61_ANNOTATION,
    GRADE_TABLE,
    PROVIDER_FAMILY_OVERRIDES,
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


def _isolate_free_window(tmp_path, monkeypatch):
    monkeypatch.setattr(free_window, "state_path", lambda: tmp_path / "free-limit.json")
    monkeypatch.setattr(free_window, "lock_path", lambda: tmp_path / "free-limit.lock")
    monkeypatch.setattr(free_window.cache, "_load", lambda: {})


def _table_for_profiles(names):
    profiles = {
        profile.name: profile for rows in GRADE_TABLE.values() for profile in rows if profile.name in names
    }
    assert set(profiles) == set(names)
    return {grade: (list(profiles.values()) if grade == "B" else []) for grade in GRADE_TABLE}


def _recommend_output(providers, names, free_limit=None):
    payload = recommend_dict(
        providers,
        "B",
        today=NOW.date(),
        now=NOW,
        grade_table=_table_for_profiles(names),
        free_rate_limit=free_window.reason(free_limit) if free_limit is not None else None,
    )
    return payload, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _observe_sol61_hold():
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
    return record


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
        assert (
            decision.billing
            == launch.resolve_launch(
                "codex-sol",
                view=bench.CatalogView(entries=launch.snapshot_entries(), source="snapshot"),
            ).billing
        )

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
    assert all(
        profile_pool(name) == ("devin", None)
        for name in (
            name
            for profiles in GRADE_TABLE.values()
            for profile in profiles
            for name in (profile.name,)
            if name.startswith("devin-") and name not in PROFILES
        )
    )


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


def test_sol61_observation_holds_all_free_rungs_without_entering_swe2_scope(monkeypatch, tmp_path):
    _isolate_free_window(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch)

    record = _observe_sol61_hold()
    assert record.rung == "devin-fusion-sol61"
    assert record.source == "job:hk1729-sol61"
    assert "devin-fusion-sol61" in OBSERVED_RUNG_IDS
    assert "devin-fusion-sol61-xhigh" in OBSERVED_RUNG_IDS
    assert set(PROFILES) <= FREE_PROFILES

    from scopefuel.recommend import SWE2_MODEL_SCOPE_PROFILES, SWE2_PROFILE_RUNG

    assert not (set(PROFILES) & SWE2_PROFILE_RUNG.keys())
    assert not (set(PROFILES) & SWE2_MODEL_SCOPE_PROFILES)

    for name in PROFILES:
        rc, _out, err = _capture_cli(["gate", "-m", name])
        assert rc == 3, (name, err)
        assert "free_rate_limited" in err, (name, err)

    for free_profile in sorted(FREE_PROFILES):
        rc, _out, err = _capture_cli(["gate", "-m", free_profile])
        assert rc == 3, (free_profile, err)
        assert "free_rate_limited" in err, (free_profile, err)
        assert "rung devin-fusion-sol61" in err, (free_profile, err)


def test_live_hold_refuses_sol61_in_gate_and_excludes_recommend_rows_with_codex_pool(monkeypatch, tmp_path):
    _isolate_free_window(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch)
    providers = _providers()
    _observe_sol61_hold()

    for name in PROFILES:
        rc, _out, err = _capture_cli(["gate", "-m", name])
        assert rc == 3, (name, err)
        assert "free_rate_limited" in err, (name, err)

    record = free_window.active_record(now=NOW)
    assert record is not None
    payload, _encoded = _recommend_output(providers, PROFILES, record)
    rows = {row["profile"]: row for row in payload["rows"]}
    assert set(rows) == set(PROFILES)
    for row in rows.values():
        assert row["kind"] == "free_rate_limited"
        assert row["provider"] == "codex"
        assert row["reason"] == free_window.reason(record)


def test_no_or_expired_hold_keeps_sol61_gate_and_recommend_output_unchanged(monkeypatch, tmp_path):
    _isolate_free_window(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch)
    providers = _providers()
    providers[1].buckets[0] = Bucket("daily", "1d", 99.0, RESET_1D, Scope("account"), "now")
    providers[1].buckets[1] = Bucket("weekly", "7d", 99.0, RESET_7D, Scope("account"), "week")
    monkeypatch.setattr(cli, "collect", lambda *args, **kwargs: providers)

    baseline_gates = {name: _capture_cli(["gate", "-m", name]) for name in PROFILES}
    assert all(rc == 0 and "pool=codex" in out for rc, out, _err in baseline_gates.values())
    base_payload, baseline_recommend = _recommend_output(providers, PROFILES)
    base_rows = {row["profile"]: row for row in base_payload["rows"]}
    assert set(base_rows) == set(PROFILES)
    assert all(row["kind"] == "candidate" and row["provider"] == "codex" for row in base_rows.values())
    assert free_window.active_record(now=NOW) is None

    expired = free_window.FreeLimitRecord(
        NOW - dt.timedelta(minutes=1),
        NOW - dt.timedelta(minutes=10),
        "devin-fusion-sol61",
        "job:hk1729-sol61-expired",
    )
    free_window._save_records({free_window._identity_key(): expired})
    assert free_window.active_record(now=NOW) is None
    assert {name: _capture_cli(["gate", "-m", name]) for name in PROFILES} == baseline_gates
    _, expired_recommend = _recommend_output(providers, PROFILES, free_window.active_record(now=NOW))
    assert expired_recommend == baseline_recommend


def test_base_codex_refusal_keeps_its_reason_and_rc_during_live_hold(monkeypatch, tmp_path):
    _isolate_free_window(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch)
    providers = _providers()
    providers[0].buckets[0] = Bucket("week", "7d", 100.0, RESET_7D, Scope("account"), "week")
    monkeypatch.setattr(cli, "collect", lambda *args, **kwargs: providers)

    baseline_gate = _capture_cli(["gate", "-m", "devin-fusion-sol61"])
    _baseline_payload, baseline_recommend = _recommend_output(providers, PROFILES)
    _observe_sol61_hold()

    held_gate = _capture_cli(["gate", "-m", "devin-fusion-sol61"])
    assert held_gate == baseline_gate
    assert held_gate[0] == 3
    assert "free_rate_limited" not in held_gate[2]

    record = free_window.active_record(now=NOW)
    assert record is not None
    held_payload, held_recommend = _recommend_output(providers, PROFILES, record)
    assert held_recommend == baseline_recommend
    rows = {row["profile"]: row for row in held_payload["rows"]}
    assert all(row["kind"] != "free_rate_limited" for row in rows.values())
    assert all(row["provider"] == "codex" for row in rows.values())


def test_live_hold_leaves_paid_devin_and_codex_only_profiles_byte_identical(monkeypatch, tmp_path):
    _isolate_free_window(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch)
    providers = _providers()
    protected = (
        "devin-ds41",
        "devin-glm52",
        "devin-swe17",
        "devin-fusion-opus55",
        "devin-fusion-sonnet55",
        "codex-sol",
        "codex-luna",
    )
    # Argon is an ungraded trial row, so it has a gate path but no recommend row.
    protected_gate_profiles = (*protected, "devin-argon", "gpt-6-astra")

    baseline_gates = {name: _capture_cli(["gate", "-m", name]) for name in protected_gate_profiles}
    _baseline_payload, baseline_recommend = _recommend_output(providers, protected)
    _observe_sol61_hold()

    assert {name: _capture_cli(["gate", "-m", name]) for name in protected_gate_profiles} == baseline_gates
    record = free_window.active_record(now=NOW)
    assert record is not None
    held_payload, held_recommend = _recommend_output(providers, protected, record)
    assert held_recommend == baseline_recommend
    rows = {row["profile"]: row for row in held_payload["rows"]}
    assert {name: rows[name]["provider"] for name in ("codex-sol", "codex-luna")} == {
        "codex-sol": "codex",
        "codex-luna": "codex",
    }
    assert profile_pool("gpt-6-astra") == ("codex", None)
