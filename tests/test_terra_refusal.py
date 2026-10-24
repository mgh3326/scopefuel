"""Operator decision 2026-10-12 noterra: Terra is never launchable or ranked."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from scopefuel import bench, cli, launch, policy
from scopefuel import recommend as recommend_mod
from scopefuel.recommend import GRADE_TABLE, recommend, recommend_dict

TERRA_NAMES = {
    "codex-terra",
    "codex-terra-max",
    "codex-med",
    "builder-terra-high",
    "builder-terra-xhigh",
    "builder-terra-max",
}
NOW = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def isolated_user_dirs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        path = tmp_path / name.lower()
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))


def _server_view(rows):
    entries = bench._catalog_from_payload({"catalog": rows})
    return bench.CatalogView(tuple(entries), source="server", backend="handoffkeep")


def _all_recommendation_rows(payload):
    return [row for row in payload["rows"] if isinstance(row, dict)]


def test_bundled_table_never_emits_a_terra_candidate_exclusion_or_one_up_row():
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        payload = recommend_dict([], grade, today=NOW.date(), now=NOW)
        rows = _all_recommendation_rows(payload)
        assert not any(row["profile"] in TERRA_NAMES for row in rows), (grade, rows)
        # hk 1862: barred terra rows appear only on the folded excluded line
        terra_excluded = [e for e in payload["excluded"] if e["profile"] in TERRA_NAMES]
        assert all("barred" in e["reason"] for e in terra_excluded), (grade, payload["excluded"])
        rendered = recommend([], grade, today=NOW.date(), now=NOW)
        assert not any(
            name in line for name in TERRA_NAMES for line in rendered.splitlines() if line[:1].isdigit()
        )


def test_served_canon_promote_row_is_filtered_after_merge_for_exact_and_one_up_views():
    rows = [entry.as_dict() for entry in bench.catalog_snapshot()]
    terra = next(row for row in rows if row["profile"] == "codex-terra" and row["effort"] == "high")
    terra.update(
        grade="A+",
        deviation_ref="hk:task/1463; promote evidence srv:14631,srv:14632,srv:14633",
        decided_by="operator-desk",
        decided_at="2026-10-12T00:00:00Z",
    )
    table = bench._catalog_grade_table(_server_view(rows))
    assert table is not None
    promoted = next(row for row in table["A+"] if row.name == "codex-terra" and row.launcher_effort == "high")
    assert recommend_mod.is_rep_measured(promoted)

    exact = recommend_dict([], "A+", today=NOW.date(), now=NOW, grade_table=table)
    one_up = recommend_dict([], "S", today=NOW.date(), now=NOW, grade_table=table)
    assert not any(row["profile"] == "codex-terra" for row in _all_recommendation_rows(exact))
    assert not any(
        row["profile"] == "codex-terra" and row["one_up"] for row in _all_recommendation_rows(one_up)
    )


def test_codex_pool_config_boost_cannot_restore_terra():
    config = policy.config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        '[pools.codex]\nclass = "spend"\nboost = 99\nuntil = "2030-01-01"\n',
        encoding="utf-8",
    )
    boost, _ = policy.get_boost("codex", today=NOW.date())
    assert boost == 99

    payload = recommend_dict([], "A+", today=NOW.date(), now=NOW)
    assert not any(row["profile"] in TERRA_NAMES for row in _all_recommendation_rows(payload))
    assert "gpt-5.6-terra" not in json.dumps(payload)


@pytest.mark.parametrize("profile", sorted(TERRA_NAMES))
def test_gate_and_launch_refuse_every_terra_spelling(profile, monkeypatch, capsys):
    gate = recommend_mod.gate_check([], profile, today=NOW.date(), now=NOW)
    assert not gate.ok
    assert "model_refused" in gate.reason
    assert profile in gate.reason

    with pytest.raises(launch.LaunchError, match="refused") as exc:
        launch.resolve_launch(profile, view=bench.CatalogView((), source="server", backend="handoffkeep"))
    assert profile in str(exc.value)
    assert "gpt-5.6-terra" in str(exc.value)

    monkeypatch.setattr(
        cli,
        "collect",
        lambda *args, **kwargs: pytest.fail("barred gate must refuse before provider collection"),
    )
    # Older wrk versions call exactly `scopefuel gate -m PROFILE`.
    assert cli.main(["gate", "-m", profile]) == 3
    err = capsys.readouterr().err
    assert profile in err and "gpt-5.6-terra" in err and "noterra" in err


def test_nonterra_codex_recommendations_are_unchanged_by_the_filter(monkeypatch):
    table = {
        grade: [
            profile
            for profile in profiles
            if not recommend_mod.is_barred_model_id(profile.aa_agent_model_id or profile.benchmark_model_id)
        ]
        for grade, profiles in GRADE_TABLE.items()
    }
    expected = {
        grade: recommend_dict([], grade, today=NOW.date(), now=NOW, grade_table=table)
        for grade in ("S+", "A+", "A", "B")
    }
    with monkeypatch.context() as patcher:
        patcher.setattr(recommend_mod, "is_barred_model_id", lambda _model_id: False)
        actual = {
            grade: recommend_dict([], grade, today=NOW.date(), now=NOW, grade_table=table)
            for grade in ("S+", "A+", "A", "B")
        }
    assert actual == expected
    profiles_by_grade = {
        grade: {row["profile"] for row in _all_recommendation_rows(payload)}
        for grade, payload in expected.items()
    }
    assert "codex-sol" in profiles_by_grade["S+"]
    assert "codex-luna" in profiles_by_grade["A+"] | profiles_by_grade["A"]
