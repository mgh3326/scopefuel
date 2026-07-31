"""ROB-1187 — bench DB, AA sync/import, source-aware display, and reps."""

from __future__ import annotations

import pytest

from scopefuel import bench, cli
from scopefuel.model import Bucket, ProviderResult


def _score(
    model_id: str = "gpt-5.6-terra",
    *,
    source: str = "AA-agent",
    metric: str = "agentic",
    score: float = 78.0,
    rank: int | None = 1,
    effort: str | None = "max",
    harness: str | None = "codex",
) -> bench.ModelScore:
    return bench.ModelScore(
        model_id=model_id,
        effort=effort,
        harness=harness,
        source=source,
        metric=metric,
        score=score,
        rank=rank,
        captured_at="2026-07-31T12:00:00+00:00",
    )


@pytest.fixture
def bench_home(tmp_path, monkeypatch):
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setattr(bench, "DOTENV_PATH", tmp_path / "missing.env")
    return data_home


def test_schema_and_xdg_path_are_exact(bench_home):
    path = bench.db_path()
    assert path == bench_home / "scopefuel" / "bench.db"
    conn = bench.connect(path)
    try:
        tables = {
            row[0]: [column[1] for column in conn.execute(f"PRAGMA table_info({row[0]})")]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert tables["model_scores"] == [
        "model_id",
        "effort",
        "harness",
        "source",
        "metric",
        "score",
        "rank",
        "captured_at",
    ]
    assert tables["reps"] == [
        "id",
        "profile",
        "model_id",
        "task_ref",
        "tier",
        "role",
        "rounds",
        "blockers_found",
        "completed",
        "notes",
        "recorded_at",
    ]
    assert ".cache" not in str(path)


def test_sync_with_fake_key_uses_official_endpoint_and_upserts(bench_home, monkeypatch):
    seen = {}

    def fake_request(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs["headers"]
        return {
            "status": 200,
            "data": [
                {
                    "id": "stable-id",
                    "slug": "gpt-5.6-terra",
                    "evaluations": {
                        "artificial_analysis_coding_index": 78.0,
                        "artificial_analysis_intelligence_index": 81.5,
                    },
                },
                {
                    "id": "other-id",
                    "slug": "other-model",
                    "evaluations": {"artificial_analysis_coding_index": 70.0},
                },
            ],
        }

    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "x")
    monkeypatch.setattr(bench, "request_json", fake_request)
    assert bench.run_sync(stderr=__import__("sys").stderr) == 0
    assert seen == {
        "url": bench.AA_API_URL,
        "headers": {"x-api-key": "x"},
    }
    rows = [row for row in bench.read_scores() if row.source == "AA-model"]
    assert {(row.model_id, row.source, row.metric) for row in rows} == {
        ("gpt-5.6-terra", "AA-model", "coding_index"),
        ("gpt-5.6-terra", "AA-model", "intelligence"),
        ("other-model", "AA-model", "coding_index"),
    }
    coding = [row for row in rows if row.metric == "coding_index"]
    assert {row.model_id: row.rank for row in coding} == {"gpt-5.6-terra": 1, "other-model": 2}


def test_sync_skips_null_aa_metrics_but_keeps_valid_metrics(bench_home, monkeypatch):
    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "x")

    def fake_request(url, **kwargs):
        return {
            "status": 200,
            "data": [
                {
                    "id": "null-coding",
                    "slug": "null-coding-model",
                    "evaluations": {
                        "artificial_analysis_coding_index": None,
                        "artificial_analysis_intelligence_index": 64.2,
                    },
                },
                {
                    "id": "valid-coding",
                    "slug": "valid-coding-model",
                    "evaluations": {"artificial_analysis_coding_index": 55.0},
                },
            ],
        }

    monkeypatch.setattr(bench, "request_json", fake_request)
    assert bench.sync_scores(captured_at="2026-07-31T12:00:00+00:00") == 2
    rows = bench.read_scores()
    assert ("null-coding-model", "AA-model", "coding_index") not in {
        (row.model_id, row.source, row.metric) for row in rows
    }
    assert ("null-coding-model", "AA-model", "intelligence") in {
        (row.model_id, row.source, row.metric) for row in rows
    }
    assert ("valid-coding-model", "AA-model", "coding_index") in {
        (row.model_id, row.source, row.metric) for row in rows
    }


def test_sync_rejects_non_numeric_non_null_aa_metric(bench_home, monkeypatch):
    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "x")
    monkeypatch.setattr(
        bench,
        "request_json",
        lambda *args, **kwargs: {
            "data": [
                {
                    "slug": "bad-model",
                    "evaluations": {"artificial_analysis_coding_index": "not-a-score"},
                }
            ]
        },
    )
    with pytest.raises(bench.BenchError, match="score"):
        bench.sync_scores(captured_at="2026-07-31T12:00:00+00:00")
    assert not bench.db_path().exists()


def test_missing_key_is_warning_only_and_preserves_existing_rows(bench_home, monkeypatch, capsys):
    bench.upsert_scores([_score()])
    monkeypatch.delenv("ARTIFICIAL_ANALYSIS_API_KEY", raising=False)
    monkeypatch.setattr(bench, "request_json", lambda *args, **kwargs: pytest.fail("must skip"))

    assert bench.run_sync(stderr=__import__("sys").stderr) == 0
    rows = bench.read_scores()
    captured = capsys.readouterr()
    assert len(rows) == 1
    assert "API key" in captured.err
    assert captured.out == ""


def test_show_keeps_source_metric_harness_effort_rows_separate(bench_home, capsys):
    bench.upsert_scores(
        [
            _score(),
            _score(
                source="openrouter",
                metric="coding",
                score=57.1,
                rank=4,
                effort=None,
                harness=None,
            ),
        ]
    )
    assert cli.main(["bench", "show", "gpt-5.6-terra"]) == 0
    out = capsys.readouterr().out
    assert "AA-agent" in out and "openrouter" in out
    assert "agentic" in out and "coding" in out
    assert "codex" in out and "57.1" in out
    assert "2026-07-31T12:00:00+00:00" in out


def test_fresh_show_persists_grade_seed_idempotently_and_preserves_newer_value(bench_home):
    path = bench.db_path()
    assert not path.exists()

    first_insert = bench.seed_scores()
    second_insert = bench.seed_scores()
    assert first_insert > 0
    assert second_insert == 0
    seeded = next(row for row in bench.read_scores("gpt-5.6-terra") if row.source == "AA-agent")
    assert seeded.score == 78.0
    assert seeded.metric == "agentic"
    assert seeded.harness == "codex"
    assert seeded.effort == "max"

    bench.upsert_scores(
        [
            _score(score=99.0, rank=1),
        ]
    )
    assert bench.seed_scores() == 0
    current = next(row for row in bench.read_scores("gpt-5.6-terra") if row.source == "AA-agent")
    assert current.score == 99.0

    shown = bench.show_scores("gpt-5.6-terra")
    assert "78.0" not in shown
    assert "99.0" in shown


def test_fresh_show_uses_normalized_luna_model_id_and_preserves_display_name(bench_home):
    from scopefuel.recommend import GRADE_TABLE

    luna = next(profile for profile in GRADE_TABLE["A+"] if profile.name == "codex-luna-ultra")
    assert luna.model == "Luna (ultra)"
    assert luna.benchmark_model_id == "gpt-5.6-luna"
    shown = bench.show_scores("gpt-5.6-luna")
    assert "75.0" in shown and "ultra" in shown


def test_recommend_fallback_bench_cells_are_labeled(monkeypatch, bench_home, capsys):
    def healthy(provider_id):
        return lambda: ProviderResult(
            id=provider_id,
            buckets=[Bucket(label="7d", window="7d", used_pct=10.0)],
        )

    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {name: healthy(name) for name in ("codex", "clinepass", "grok")},
    )
    assert cli.main(["--recommend", "S", "--no-cache"]) == 0
    out = capsys.readouterr().out
    bench_cells = [line.split("벤치 ", 1)[1] for line in out.splitlines() if "벤치 " in line]
    assert bench_cells
    assert all("(" in cell for cell in bench_cells)
    assert "78.0(AA-agent; metric=agentic; harness=codex; effort=max)" in out
    assert "57.1(openrouter; metric=coding; harness=n/a; effort=n/a)" in out


def test_import_validation_is_atomic_and_rejects_mixed_sources(bench_home, tmp_path):
    valid = tmp_path / "valid.toml"
    valid.write_text(
        "[[scores]]\n"
        'model_id = "gpt-5.6-terra"\nsource = "openrouter"\nmetric = "coding"\n'
        'score = 57.1\ncaptured_at = "2026-07-31T12:00:00Z"\n'
    )
    assert bench.import_scores(valid) == 1
    before = bench.read_scores()

    mixed = tmp_path / "mixed.toml"
    mixed.write_text(
        "[[scores]]\n"
        'model_id = "gpt-5.6-terra"\nsource = "openrouter"\nmetric = "coding"\n'
        'score = 57.1\ncaptured_at = "2026-07-31T12:00:00Z"\n'
        "[[scores]]\n"
        'model_id = "gpt-5.6-luna"\nsource = "AA-agent"\nmetric = "agentic"\n'
        'harness = "codex"\neffort = "ultra"\nscore = 75.0\n'
        'captured_at = "2026-07-31T12:00:00Z"\n'
    )
    with pytest.raises(bench.BenchError, match="one source"):
        bench.import_scores(mixed)
    assert bench.read_scores() == before


def test_reps_add_and_list_round_trip(bench_home, capsys):
    assert (
        cli.main(
            [
                "reps",
                "add",
                "--profile",
                "codex-terra-max",
                "--model",
                "gpt-5.6-terra",
                "--task",
                "ROB-1187",
                "--tier",
                "T1",
                "--role",
                "impl",
                "--rounds",
                "1",
                "--blockers-found",
                "0",
                "--completed",
                "1",
                "--notes",
                "fixture",
            ]
        )
        == 0
    )
    assert "recorded rep id=1" in capsys.readouterr().out
    reps = bench.read_reps()
    assert len(reps) == 1
    assert reps[0].task_ref == "ROB-1187"
    assert reps[0].completed == 1
    assert reps[0].notes == "fixture"

    assert cli.main(["reps", "list"]) == 0
    assert "profile=codex-terra-max" in capsys.readouterr().out


def test_missing_db_recommend_does_not_treat_missing_scores_as_low(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty-data"))
    monkeypatch.setattr(bench, "DOTENV_PATH", tmp_path / "missing.env")
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {
            name: (
                lambda provider_id=name: ProviderResult(
                    id=provider_id, buckets=[Bucket(label="7d", window="7d", used_pct=10.0)]
                )
            )
            for name in ("codex", "clinepass", "grok")
        },
    )
    assert cli.main(["--recommend", "S", "--no-cache"]) == 0
    out = capsys.readouterr().out
    codex_line = next(line for line in out.splitlines() if "codex-terra-max" in line)
    assert "벤치 78.0(AA-agent; metric=agentic; harness=codex; effort=max)" in codex_line
    assert not (tmp_path / "empty-data" / "scopefuel" / "bench.db").exists()


def test_recommend_does_not_rank_by_raw_scores_across_sources_or_metrics(bench_home):
    from scopefuel.recommend import recommend

    providers = [
        ProviderResult(id=name, buckets=[Bucket(label="7d", window="7d", used_pct=10.0)])
        for name in ("codex", "clinepass", "grok")
    ]
    scores = [
        _score(score=1.0),
        _score(
            model_id="kimi-k3",
            source="openrouter",
            metric="coding",
            score=99.0,
            rank=1,
            effort=None,
            harness=None,
        ),
    ]
    out = recommend(providers, "S", bench_scores=scores)
    ranked = [line for line in out.splitlines() if line[:1].isdigit()]
    assert ranked[0].startswith("1. codex-terra-max")
    assert ranked[1].startswith("2. oc-kimi-k3")
    assert "1.0(AA-agent; metric=agentic; harness=codex; effort=max)" in ranked[0]
    assert "99.0(openrouter; metric=coding; harness=n/a; effort=n/a)" in ranked[1]


def test_no_secret_like_response_body_is_printed(bench_home, monkeypatch, capsys):
    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "x")

    def fail_request(*args, **kwargs):
        raise bench.HttpError(401, "do-not-print")

    monkeypatch.setattr(bench, "request_json", fail_request)
    assert bench.run_sync(stderr=__import__("sys").stderr) == 1
    captured = capsys.readouterr()
    assert "do-not-print" not in captured.out + captured.err
