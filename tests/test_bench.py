"""ROB-1187 — bench DB, AA sync/import, source-aware display, and reps."""

from __future__ import annotations

import sqlite3

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


def _insert_approved_aa_rows(path):
    conn = bench.connect(path)
    try:
        conn.executemany(
            "INSERT INTO model_scores "
            "(model_id, effort, harness, source, metric, score, rank, captured_at, "
            "time_per_task_min, cost_per_task_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
            [(*row[:5], 50.0, None, "2026-08-01T00:00:00+00:00") for row in bench.AA_AGENT_MEASUREMENTS],
        )
        conn.commit()
    finally:
        conn.close()


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
        "time_per_task_min",
        "cost_per_task_usd",
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
        "input_tokens",
        "output_tokens",
        "notes",
        "recorded_at",
        "effort",
        "grade",
        "table_grade",
    ]
    assert ".cache" not in str(path)


def test_rob1194_approved_26_row_backfill_and_cost_coverage(bench_home):
    path = bench.db_path()
    _insert_approved_aa_rows(path)

    before = bench.read_scores(path=path)
    assert len(before) == 26
    assert all(row.time_per_task_min is None and row.cost_per_task_usd is None for row in before)

    assert bench.backfill_aa_agent_metrics(path=path) == 26
    assert bench.backfill_aa_agent_metrics(path=path) == 0

    rows = bench.read_scores(path=path)
    measurements = {
        (row.model_id, row.effort, row.harness, row.source, row.metric): (
            row.time_per_task_min,
            row.cost_per_task_usd,
        )
        for row in rows
    }
    expected = {row[:5]: (row[5], row[6]) for row in bench.AA_AGENT_MEASUREMENTS}
    assert measurements == expected
    assert len(measurements) == 26
    assert sum(cost is not None for _, cost in measurements.values()) == 12
    assert measurements[("gpt-5.6-sol", "xhigh", "codex", "AA-agent", "agentic")] == (7.4, 5.24)


def test_rob1194_backfill_fails_closed_on_partial_key_set(bench_home):
    path = bench.db_path()
    bench.upsert_scores([_score()])
    with pytest.raises(bench.BenchError, match="key mismatch"):
        bench.backfill_aa_agent_metrics(path=path)


def test_rob1194_time_cost_mutation_is_detected_and_repaired(bench_home):
    path = bench.db_path()
    _insert_approved_aa_rows(path)
    assert bench.backfill_aa_agent_metrics(path=path) == 26

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE model_scores SET time_per_task_min = ?, cost_per_task_usd = ? "
            "WHERE model_id = ? AND effort = ? AND harness = ? AND source = ? AND metric = ?",
            (7.5, 99.0, "gpt-5.6-sol", "xhigh", "codex", "AA-agent", "agentic"),
        )
        conn.commit()
    finally:
        conn.close()

    mutated = next(
        row for row in bench.read_scores(path=path) if row.model_id == "gpt-5.6-sol" and row.effort == "xhigh"
    )
    assert (mutated.time_per_task_min, mutated.cost_per_task_usd) != (7.4, 5.24)
    assert bench.backfill_aa_agent_metrics(path=path) == 1
    repaired = next(
        row for row in bench.read_scores(path=path) if row.model_id == "gpt-5.6-sol" and row.effort == "xhigh"
    )
    assert (repaired.time_per_task_min, repaired.cost_per_task_usd) == (7.4, 5.24)


def test_rob1194_read_scores_keeps_legacy_db_read_only(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE model_scores ("
            "model_id TEXT NOT NULL, effort TEXT, harness TEXT, source TEXT NOT NULL, "
            "metric TEXT NOT NULL, score REAL, rank INTEGER, captured_at TEXT NOT NULL, "
            "PRIMARY KEY (model_id, effort, harness, source, metric))"
        )
        conn.execute(
            "INSERT INTO model_scores VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("gpt-5.6-luna", "medium", "codex", "AA-agent", "agentic", 42.0, 25, "2026-08-01"),
        )
        conn.commit()
    finally:
        conn.close()
    before_mtime = path.stat().st_mtime_ns

    rows = bench.read_scores(path=path)

    assert rows[0].time_per_task_min is None
    assert rows[0].cost_per_task_usd is None
    assert path.stat().st_mtime_ns == before_mtime


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
    assert "unspecified" in out
    assert "2026-07-31T12:00:00+00:00" in out


def test_rob1194_bench_show_exposes_time_and_cost(bench_home):
    path = bench.db_path()
    _insert_approved_aa_rows(path)
    assert bench.backfill_aa_agent_metrics(path=path) == 26

    shown = bench.show_scores("gpt-5.6-luna", path=path)

    assert "time_per_task_min" in shown
    assert "cost_per_task_usd" in shown
    assert "3.4" in shown and "8.0" in shown
    assert "$5.24" not in shown


def test_fresh_show_persists_grade_seed_idempotently_and_preserves_newer_value(bench_home):
    path = bench.db_path()
    assert not path.exists()

    first_insert = bench.seed_scores()
    second_insert = bench.seed_scores()
    assert first_insert > 0
    assert second_insert == 0
    seeded = next(
        row for row in bench.read_scores("gpt-5.6-terra") if row.source == "AA-agent" and row.effort == "max"
    )
    assert seeded.score == 62.0
    assert seeded.metric == "agentic"
    assert seeded.harness == "codex"
    assert seeded.effort == "max"

    bench.upsert_scores(
        [
            _score(score=99.0, rank=1),
        ]
    )
    assert bench.seed_scores() == 0
    current = next(
        row for row in bench.read_scores("gpt-5.6-terra") if row.source == "AA-agent" and row.effort == "max"
    )
    assert current.score == 99.0

    shown = bench.show_scores("gpt-5.6-terra")
    assert "78.0" not in shown
    assert "99.0" in shown


def test_fresh_show_uses_normalized_luna_model_id_and_preserves_display_name(bench_home):
    from scopefuel.recommend import GRADE_TABLE

    luna = next(profile for profile in GRADE_TABLE["A+"] if profile.name == "codex-luna-max")
    assert luna.model == "Luna (max)"
    assert luna.benchmark_model_id == "gpt-5.6-luna"
    bench.seed_scores()
    shown = bench.show_scores("gpt-5.6-luna")
    assert "59.0" in shown and "max" in shown


def test_read_scores_missing_db_has_no_filesystem_side_effect(bench_home):
    path = bench.db_path()
    assert not path.exists()
    assert bench.read_scores() == []
    assert not path.exists()
    assert not path.parent.exists()


def test_show_missing_db_has_no_filesystem_side_effect(bench_home):
    path = bench.db_path()
    assert bench.show_scores("gpt-5.6-luna") == "model_id=gpt-5.6-luna\n없음/미측정"
    assert not path.exists()
    assert not path.parent.exists()


def test_read_scores_and_show_keep_existing_db_mtime(bench_home):
    path = bench.db_path()
    bench.seed_scores()
    before = path.stat().st_mtime_ns

    assert bench.read_scores()
    assert "59.0" in bench.show_scores("gpt-5.6-luna")

    assert path.stat().st_mtime_ns == before


def test_recommend_fallback_bench_cells_are_labeled(monkeypatch, bench_home, capsys):
    def healthy(provider_id):
        return lambda: ProviderResult(
            id=provider_id,
            buckets=[Bucket(label="7d", window="7d", used_pct=10.0)],
        )

    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {name: healthy(name) for name in ("codex", "clinepass", "grok", "kimi")},
    )
    assert cli.main(["--recommend", "S", "--no-cache"]) == 0
    out = capsys.readouterr().out
    bench_cells = [line.split("벤치 ", 1)[1] for line in out.splitlines() if "벤치 " in line]
    assert bench_cells
    assert all(
        "(" in cell or "모델지수만 있음(에이전트 미측정)" in cell or cell in {"미측정", "미지정"}
        for cell in bench_cells
    )
    # ROB-1191 ④ compact single-effort cells
    assert "78.0(AA-agent; metric=agentic; harness=codex; effort=max)" not in out
    assert "62.0(AA-agent/codex/max)" in out
    assert "61.0(AA-agent/kimi-code-cli)" in out


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
        'harness = "codex"\neffort = "max"\nscore = 75.0\n'
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
    assert reps[0].input_tokens is None
    assert reps[0].output_tokens is None
    assert reps[0].notes == "fixture"
    assert reps[0].effort is None
    assert reps[0].grade is None

    assert cli.main(["reps", "list"]) == 0
    assert "profile=codex-terra-max" in capsys.readouterr().out


def test_reps_help_and_filters_preserve_nullable_legacy_rows(bench_home, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["reps", "add", "--help"])
    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--effort {low,medium,high,xhigh,max}" in help_text
    assert "--grade {S+,S,A+,A,B,C}" in help_text

    for profile, effort, grade in (
        ("legacy-one", None, None),
        ("legacy-two", None, None),
        ("codex-luna", "medium", "B"),
        ("haiku", "high", "B"),
        ("codex-luna", "high", "A+"),
    ):
        args = [
            "reps",
            "add",
            "--profile",
            profile,
            "--model",
            "model",
            "--task",
            "ROB-1203",
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
        ]
        if effort is not None:
            args.extend(["--effort", effort])
        if grade is not None:
            args.extend(["--grade", grade])
        assert cli.main(args) == 0
        capsys.readouterr()

    rows = bench.read_reps()
    assert len(rows) == 5
    by_id = {row.id: row for row in rows}
    assert by_id[1].effort is None and by_id[1].grade is None
    assert by_id[2].effort is None and by_id[2].grade is None
    assert by_id[3].effort == "medium" and by_id[3].grade == "B"

    assert cli.main(["reps", "list", "--grade", "B"]) == 0
    grade_b = capsys.readouterr().out
    assert "profile=codex-luna" in grade_b and "profile=haiku" in grade_b
    assert "profile=codex-luna" in grade_b

    assert cli.main(["reps", "list", "--profile", "codex-luna"]) == 0
    profile_rows = capsys.readouterr().out
    assert profile_rows.count("profile=codex-luna") == 2

    assert cli.main(["reps", "list", "--effort", "high"]) == 0
    high_rows = capsys.readouterr().out
    assert high_rows.count("effort=high") == 2

    assert cli.main(["reps", "list", "--grade", "B", "--profile", "codex-luna", "--effort", "medium"]) == 0
    combined = capsys.readouterr().out
    assert "id=3" in combined and "id=4" not in combined and "id=5" not in combined

    assert cli.main(["reps", "list", "--grade", "C"]) == 0
    empty = capsys.readouterr().out
    assert "기록 없음" in empty


def test_reps_compare_reports_profile_averages_and_tokens(bench_home, capsys):
    def add(profile, rounds, blockers, completed, input_tokens=None, output_tokens=None):
        args = [
            "reps",
            "add",
            "--profile",
            profile,
            "--model",
            "model",
            "--task",
            "ROB-1203",
            "--tier",
            "T1",
            "--role",
            "impl",
            "--effort",
            "high",
            "--grade",
            "B",
            "--rounds",
            str(rounds),
            "--blockers-found",
            str(blockers),
            "--completed",
            str(completed),
        ]
        if input_tokens is not None:
            args.extend(["--input-tokens", str(input_tokens)])
        if output_tokens is not None:
            args.extend(["--output-tokens", str(output_tokens)])
        assert cli.main(args) == 0
        capsys.readouterr()

    add("alpha", 2, 1, 1, 10, 20)
    add("alpha", 4, 3, 0, 30, 40)
    add("beta", 1, 0, 1)

    assert cli.main(["reps", "compare", "--grade", "B"]) == 0
    out = capsys.readouterr().out
    alpha = next(line for line in out.splitlines() if "profile=alpha" in line)
    beta = next(line for line in out.splitlines() if "profile=beta" in line)
    assert "count=2" in alpha
    assert "avg-rounds=3.00" in alpha
    assert "avg-blockers-found=2.00" in alpha
    assert "completion-rate=50.0%" in alpha
    assert "avg-input-tokens=20.00" in alpha
    assert "avg-output-tokens=30.00" in alpha
    assert "count=1" in beta and "avg-rounds=1.00" in beta
    assert "avg-blockers-found=0.00" in beta and "completion-rate=100.0%" in beta


def test_reps_tokens_round_trip_and_legacy_read_compat(bench_home, capsys, tmp_path):
    assert (
        cli.main(
            [
                "reps",
                "add",
                "--profile",
                "codex-luna",
                "--model",
                "gpt-5.6-luna",
                "--task",
                "ROB-1193",
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
                "--input-tokens",
                "3975",
                "--output-tokens",
                "10292",
            ]
        )
        == 0
    )
    capsys.readouterr()
    recorded = bench.read_reps()[0]
    assert recorded.input_tokens == 3975
    assert recorded.output_tokens == 10292
    assert cli.main(["reps", "list"]) == 0
    listed = capsys.readouterr().out
    assert "input-tokens=3975" in listed and "output-tokens=10292" in listed

    legacy_path = tmp_path / "legacy-reps.db"
    legacy = sqlite3.connect(legacy_path)
    try:
        legacy.execute(
            "CREATE TABLE reps ("
            "id INTEGER PRIMARY KEY, profile TEXT NOT NULL, model_id TEXT, task_ref TEXT, "
            "tier TEXT, role TEXT, rounds INTEGER, blockers_found INTEGER, completed INTEGER, "
            "notes TEXT, recorded_at TEXT NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO reps (profile, model_id, task_ref, tier, role, rounds, blockers_found, "
            "completed, notes, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("opus", "claude-opus-5", "ROB-1187", "T1", "impl", 1, 0, 1, None, "2026-08-01T00:00:00Z"),
        )
        legacy.commit()
    finally:
        legacy.close()
    old_rows = bench.read_reps(path=legacy_path)
    assert old_rows[0].input_tokens is None and old_rows[0].output_tokens is None
    migrated_mtime = legacy_path.stat().st_mtime_ns
    bench.read_reps(path=legacy_path)
    assert legacy_path.stat().st_mtime_ns == migrated_mtime

    bench.add_rep(
        profile="haiku",
        model_id="claude-haiku-4.5",
        task_ref="ROB-1193",
        tier="T1",
        role="verify",
        rounds=1,
        blockers_found=0,
        completed=1,
        input_tokens=10,
        output_tokens=20,
        path=legacy_path,
    )
    migrated_rows = bench.read_reps(path=legacy_path)
    assert migrated_rows[0].input_tokens == 10
    assert migrated_rows[0].output_tokens == 20
    assert migrated_rows[1].input_tokens is None


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
    assert "벤치 62.0(AA-agent/codex/max)" in codex_line
    assert not (tmp_path / "empty-data" / "scopefuel" / "bench.db").exists()


def test_recommend_does_not_rank_by_raw_scores_across_sources_or_metrics(bench_home):
    from scopefuel.recommend import recommend

    providers = [
        ProviderResult(id=name, buckets=[Bucket(label="7d", window="7d", used_pct=10.0)])
        for name in ("codex", "clinepass", "grok", "kimi")
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
    assert ranked[1].startswith("2. kimi-k3")
    assert "1.0(AA-agent/codex/max)" in ranked[0]
    assert "61.0(AA-agent/kimi-code-cli)" in ranked[1]


def test_no_secret_like_response_body_is_printed(bench_home, monkeypatch, capsys):
    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "x")

    def fail_request(*args, **kwargs):
        raise bench.HttpError(401, "do-not-print")

    monkeypatch.setattr(bench, "request_json", fail_request)
    assert bench.run_sync(stderr=__import__("sys").stderr) == 1
    captured = capsys.readouterr()
    assert "do-not-print" not in captured.out + captured.err


# ------------------------------------------------------------------ ROB-1190 ②-1: effort suffix parsing


@pytest.mark.parametrize(
    "model_id,expected_base,expected_effort",
    [
        ("gpt-5-6-terra-xhigh", "gpt-5-6-terra", "xhigh"),
        ("gpt-5-6-terra-high", "gpt-5-6-terra", "high"),
        ("gpt-5-6-terra-medium", "gpt-5-6-terra", "medium"),
        ("gpt-5-6-terra-low", "gpt-5-6-terra", "low"),
        ("gpt-5-6-terra-non-reasoning", "gpt-5-6-terra", "non-reasoning"),
        ("gpt-5-6-terra", "gpt-5-6-terra", None),  # 무접미사 -> unspecified(None), 추측 금지
        ("claude-opus-5-xhigh", "claude-opus-5", "xhigh"),
        ("claude-opus-5", "claude-opus-5", None),
    ],
)
def test_parse_effort_suffix(model_id, expected_base, expected_effort):
    base, effort = bench.parse_effort_suffix(model_id)
    assert base == expected_base
    assert effort == expected_effort


def test_sync_parses_effort_suffix_into_column(bench_home, monkeypatch):
    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "x")
    monkeypatch.setattr(
        bench,
        "request_json",
        lambda *a, **k: {
            "data": [
                {
                    "slug": "gpt-5-6-terra-xhigh",
                    "evaluations": {"artificial_analysis_coding_index": 70.6},
                },
                {
                    "slug": "gpt-5-6-terra",
                    "evaluations": {"artificial_analysis_coding_index": 76.7},
                },
            ]
        },
    )
    assert bench.sync_scores(captured_at="2026-07-31T12:00:00+00:00") == 2
    rows = {row.model_id: row for row in bench.read_scores() if row.source == "AA-model"}
    assert "gpt-5-6-terra-xhigh" not in rows  # 접미사가 model_id 에 남아있으면 안 됨
    xhigh_rows = [
        row
        for row in bench.read_scores("gpt-5-6-terra")
        if row.source == "AA-model" and row.effort == "xhigh"
    ]
    assert len(xhigh_rows) == 1 and xhigh_rows[0].score == 70.6
    unspecified_rows = [
        row for row in bench.read_scores("gpt-5-6-terra") if row.source == "AA-model" and row.effort is None
    ]
    assert len(unspecified_rows) == 1 and unspecified_rows[0].score == 76.7


def test_migrate_aa_model_effort_suffixes_backfills_existing_rows(bench_home):
    # 마이그레이션 전 상태 시뮬레이션: 접미사가 model_id 에 남고 effort=NULL.
    bench.upsert_scores(
        [
            bench.ModelScore(
                model_id="gpt-5-6-terra-xhigh",
                effort=None,
                harness=None,
                source="AA-model",
                metric="coding_index",
                score=70.6,
                rank=None,
                captured_at="2026-07-31T00:00:00+00:00",
            )
        ]
    )
    migrated = bench.migrate_aa_model_effort_suffixes()
    assert migrated == 1
    rows = bench.read_scores("gpt-5-6-terra")
    aa_model_rows = [row for row in rows if row.source == "AA-model"]
    assert len(aa_model_rows) == 1
    assert aa_model_rows[0].effort == "xhigh"
    assert aa_model_rows[0].model_id == "gpt-5-6-terra"

    # idempotent
    assert bench.migrate_aa_model_effort_suffixes() == 0


def test_migrate_leaves_no_suffix_and_already_parsed_rows_untouched(bench_home):
    bench.upsert_scores(
        [
            bench.ModelScore(
                model_id="gpt-5-6-terra",
                effort=None,
                harness=None,
                source="AA-model",
                metric="coding_index",
                score=76.7,
                rank=None,
                captured_at="2026-07-31T00:00:00+00:00",
            ),
            bench.ModelScore(
                model_id="claude-opus-5",
                effort="xhigh",
                harness=None,
                source="AA-model",
                metric="coding_index",
                score=77.0,
                rank=None,
                captured_at="2026-07-31T00:00:00+00:00",
            ),
        ]
    )
    assert bench.migrate_aa_model_effort_suffixes() == 0
    rows = bench.read_scores("gpt-5-6-terra")
    assert next(r for r in rows if r.source == "AA-model").effort is None


def test_migrate_preserves_newer_normalized_target_on_collision(bench_home):
    bench.upsert_scores(
        [
            bench.ModelScore(
                model_id="gpt-5-6-terra-xhigh",
                effort=None,
                harness=None,
                source="AA-model",
                metric="coding_index",
                score=70.6,
                rank=None,
                captured_at="2026-07-31T00:00:00+00:00",
            ),
            bench.ModelScore(
                model_id="gpt-5-6-terra",
                effort="xhigh",
                harness=None,
                source="AA-model",
                metric="coding_index",
                score=72.4,
                rank=None,
                captured_at="2026-08-01T00:00:00+00:00",
            ),
        ]
    )

    assert bench.migrate_aa_model_effort_suffixes() == 1
    rows = bench.read_scores("gpt-5-6-terra")
    assert len(rows) == 1
    assert rows[0].score == 72.4
    assert rows[0].captured_at == "2026-08-01T00:00:00+00:00"
    assert bench.read_scores("gpt-5-6-terra-xhigh") == []


def test_fresh_seed_uses_claude_code_for_opus_agent(bench_home):
    bench.seed_scores()
    opus = next(row for row in bench.read_scores("claude-opus-5") if row.source == "AA-agent")
    assert opus.harness == "claude-code"


@pytest.mark.parametrize("effort", ["ultra", "arbitrary"])
def test_score_input_rejects_retired_and_arbitrary_efforts(bench_home, effort):
    with pytest.raises(bench.BenchError, match="unsupported effort"):
        bench.upsert_scores([_score(effort=effort)])


def test_cli_bench_migrate_effort_command(bench_home, capsys):
    from scopefuel import cli

    bench.upsert_scores(
        [
            bench.ModelScore(
                model_id="gpt-5-6-terra-high",
                effort=None,
                harness=None,
                source="AA-model",
                metric="coding_index",
                score=67.1,
                rank=None,
                captured_at="2026-07-31T00:00:00+00:00",
            )
        ]
    )
    assert cli.main(["bench", "migrate-effort"]) == 0
    out = capsys.readouterr().out
    assert "migrated 1 row(s)" in out


# ------------------------------------------------------------------ ROB-1190 ②-4/②-5: AA-agent priority


def test_recommend_prefers_aa_agent_over_aa_model_fallback(monkeypatch, bench_home, capsys):
    """AA-agent 실측이 있으면 AA-model 값이 있어도 AA-agent 를 표시(우선순위)."""
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
    assert "AA-agent" in codex_line
    assert "AA-model" not in codex_line


def test_coverage_report_marks_unlisted_profiles_without_implying_low(bench_home):
    path = bench.db_path()
    report = bench.coverage_report()
    assert "kiro-cheap" in report
    assert "⚠" in report
    assert "'낮음'으로 취급 금지" in report
    assert "kiro-cheap" in report.rsplit("⚠", 1)[1]
    assert not path.exists()
    assert not path.parent.exists()


def test_coverage_report_shows_hit_for_codex_terra_max(bench_home):
    bench.seed_scores()
    report = bench.coverage_report()
    terra_line = next(line for line in report.splitlines() if line.startswith("codex-terra-max"))
    assert "있음" in terra_line  # AA-agent 열에 있음(시드 데이터)


def test_cli_bench_coverage_command(bench_home, capsys):
    from scopefuel import cli

    assert cli.main(["bench", "coverage"]) == 0
    out = capsys.readouterr().out
    assert "AA-agent" in out and "AA-model" in out and "openrouter" in out


def test_source_specific_aa_agent_mapping_wins_over_openrouter(bench_home):
    from scopefuel.recommend import recommend

    scores = [
        _score(
            model_id="claude-opus-5",
            source="AA-agent",
            metric="agentic",
            score=66.0,
            effort="xhigh",  # matches Profile.benchmark_effort for opus (ROB-1191)
            harness="claude-code",
        ),
        _score(
            model_id="claude-opus-5",
            source="AA-model",
            metric="coding_index",
            score=78.0,
            effort=None,
            harness=None,
        ),
    ]
    providers = [
        ProviderResult(id=name, buckets=[Bucket(label="7d", window="7d", used_pct=10.0)])
        for name in ("claude", "codex", "kiro")
    ]
    out = recommend(providers, "S+", bench_scores=scores)
    opus_line = next(
        line
        for line in out.splitlines()
        if line[:1].isdigit() and len(line.split()) > 1 and line.split()[1] == "opus"
    )
    assert "AA-agent" in opus_line
    assert "AA-model" not in opus_line
    assert "xhigh" in opus_line


def test_codex_aa_model_matching_ignores_unspecified_effort(bench_home):
    from scopefuel.recommend import recommend

    scores = [
        _score(
            model_id="gpt-5-6-luna",
            source="AA-model",
            metric="coding_index",
            score=99.0,
            effort=None,
            harness=None,
        ),
        _score(
            model_id="gpt-5-6-luna",
            source="AA-model",
            metric="coding_index",
            score=62.0,
            effort="max",
            harness=None,
        ),
    ]
    out = recommend(
        [ProviderResult(id="codex", buckets=[Bucket(label="7d", window="7d", used_pct=10.0)])],
        "A+",
        bench_scores=scores,
    )
    luna_line = next(line for line in out.splitlines() if "codex-luna-max" in line and line[:1].isdigit())
    assert "62.0(AA-model/max)" in luna_line
    assert "99.0" not in luna_line


def test_coverage_uses_source_specific_aa_agent_id(bench_home):
    bench.upsert_scores(
        [
            _score(
                model_id="claude-sonnet-4.6",
                source="AA-agent",
                metric="agentic",
                score=38.0,
                effort="medium",
                harness="claude-code",
            )
        ]
    )
    report = bench.coverage_report()
    sonnet_line = next(line for line in report.splitlines() if line.startswith("oc-sonnet46"))
    assert sonnet_line.split()[1] == "있음"


def test_aa_model_fallback_normalizes_dot_dash_slug(bench_home, monkeypatch):
    from dataclasses import replace

    from scopefuel import recommend as recommend_module
    from scopefuel.recommend import recommend

    original = recommend_module.GRADE_TABLE["S+"]
    monkeypatch.setitem(
        recommend_module.GRADE_TABLE,
        "S+",
        [
            replace(profile, aa_model_id="gpt-5.6-sol") if profile.name == "kiro-sol" else profile
            for profile in original
        ],
    )
    providers = [ProviderResult(id="kiro", buckets=[Bucket(label="7d", window="7d", used_pct=10.0)])]
    scores = [
        _score(
            model_id="gpt-5-6-sol",
            source="AA-model",
            metric="coding_index",
            score=63.0,
            effort=None,
            harness=None,
        )
    ]
    out = recommend(providers, "S+", bench_scores=scores)
    kiro_line = next(
        line
        for line in out.splitlines()
        if line[:1].isdigit() and len(line.split()) > 1 and line.split()[1] == "kiro-sol"
    )
    assert "AA-model" in kiro_line
    assert "unspecified" in kiro_line


# ------------------------------------------------------------------ ROB-1220: reps --grade 축 확정


def test_d1_help_texts_show_task_required_grade_intent(capsys):
    with pytest.raises(SystemExit):
        cli.main(["reps", "add", "--help"])
    add_help = capsys.readouterr().out
    assert "과제가 요구한 급" in add_help
    assert "프로필의 급표 배치가 아니라 과제 난이도" in add_help

    with pytest.raises(SystemExit):
        cli.main(["reps", "list", "--help"])
    list_help = capsys.readouterr().out
    assert "과제가 요구한 급 필터" in list_help

    with pytest.raises(SystemExit):
        cli.main(["reps", "compare", "--help"])
    compare_help = capsys.readouterr().out
    assert "비교할 과제가 요구한 급" in compare_help


def _find_profile_for_grade(
    target_grade: str, exclude: set[str] | None = None
) -> tuple[str, str | None, str]:
    """Dynamically find an active (profile_name, effort, grade) from recommend.GRADE_TABLE."""
    from scopefuel import bench
    from scopefuel.recommend import GRADE_TABLE

    exclude_set = exclude or set()
    for p in GRADE_TABLE.get(target_grade, []):
        if p.name in exclude_set:
            continue
        effort = p.launcher_effort or p.benchmark_effort
        derived = bench.derive_table_grade(p.name, effort)
        if derived == target_grade:
            return p.name, effort, target_grade
    raise RuntimeError(f"No profile found for table grade {target_grade} in GRADE_TABLE")


def test_d2_table_grade_auto_derived_and_no_flag(bench_home, capsys):
    c_profile, c_effort, c_grade = _find_profile_for_grade("C")
    task_grade = "B"
    add_args = [
        "reps",
        "add",
        "--profile",
        c_profile,
        "--model",
        "test-model",
        "--task",
        "ROB-1220",
        "--tier",
        "T1",
        "--role",
        "impl",
        "--grade",
        task_grade,
        "--rounds",
        "1",
        "--blockers-found",
        "0",
        "--completed",
        "1",
    ]
    if c_effort:
        add_args.extend(["--effort", c_effort])

    assert cli.main(add_args) == 0
    capsys.readouterr()

    # D2: profile not in grade table -> table_grade=None (NULL)
    assert (
        cli.main(
            [
                "reps",
                "add",
                "--profile",
                "unknown-custom-profile",
                "--model",
                "custom-model",
                "--task",
                "ROB-1220",
                "--tier",
                "T1",
                "--role",
                "impl",
                "--grade",
                "A",
                "--rounds",
                "1",
                "--blockers-found",
                "0",
                "--completed",
                "1",
            ]
        )
        == 0
    )
    capsys.readouterr()

    reps = bench.read_reps()
    by_profile = {rep.profile: rep for rep in reps}
    assert by_profile[c_profile].table_grade == c_grade
    assert by_profile[c_profile].grade == task_grade
    assert by_profile["unknown-custom-profile"].table_grade is None

    # D2 & AC 5: verify --table-grade flag does NOT exist
    with pytest.raises(SystemExit):
        cli.main(["reps", "add", "--help"])
    add_help = capsys.readouterr().out
    assert "--table-grade" not in add_help


def test_d3_reps_list_direction_indicators(bench_home, capsys):
    c_profile, c_effort, c_table_grade = _find_profile_for_grade("C")
    b_profile, b_effort, b_table_grade = _find_profile_for_grade("B")
    s_profile, s_effort, s_table_grade = _find_profile_for_grade("S")

    task_grade = "B"

    # Upward fixture: table C profile performing B task
    up_args = [
        "reps",
        "add",
        "--profile",
        c_profile,
        "--model",
        "test-model-1",
        "--task",
        "ROB-1220-UP",
        "--tier",
        "T1",
        "--role",
        "impl",
        "--grade",
        task_grade,
        "--rounds",
        "1",
        "--blockers-found",
        "0",
        "--completed",
        "1",
    ]
    if c_effort:
        up_args.extend(["--effort", c_effort])
    assert cli.main(up_args) == 0
    capsys.readouterr()

    # Same tier fixture: table B profile performing B task
    same_args = [
        "reps",
        "add",
        "--profile",
        b_profile,
        "--model",
        "test-model-2",
        "--task",
        "ROB-1220-SAME",
        "--tier",
        "T1",
        "--role",
        "impl",
        "--grade",
        task_grade,
        "--rounds",
        "1",
        "--blockers-found",
        "0",
        "--completed",
        "1",
    ]
    if b_effort:
        same_args.extend(["--effort", b_effort])
    assert cli.main(same_args) == 0
    capsys.readouterr()

    # Downward fixture: table S profile performing B task
    down_args = [
        "reps",
        "add",
        "--profile",
        s_profile,
        "--model",
        "test-model-3",
        "--task",
        "ROB-1220-DOWN",
        "--tier",
        "T1",
        "--role",
        "impl",
        "--grade",
        task_grade,
        "--rounds",
        "1",
        "--blockers-found",
        "0",
        "--completed",
        "1",
    ]
    if s_effort:
        down_args.extend(["--effort", s_effort])
    assert cli.main(down_args) == 0
    capsys.readouterr()

    assert cli.main(["reps", "list"]) == 0
    list_out = capsys.readouterr().out
    assert f"grade={task_grade}(표{c_table_grade}↑)" in list_out
    assert f"grade={task_grade}(표{s_table_grade}↓)" in list_out
    # Same tier displays grade without parentheses
    same_line = next(line for line in list_out.splitlines() if f"profile={b_profile}" in line)
    assert f"grade={task_grade} " in same_line or same_line.endswith(f"grade={task_grade}")


def test_d3_reps_compare_counts_summary(bench_home, capsys):
    c_profile, c_effort, _ = _find_profile_for_grade("C")
    b_profile, b_effort, _ = _find_profile_for_grade("B", exclude={c_profile})

    # Upward run: table C profile on grade B task
    up_args = [
        "reps",
        "add",
        "--profile",
        c_profile,
        "--model",
        "test-model-1",
        "--task",
        "ROB-1220-1",
        "--tier",
        "T1",
        "--role",
        "impl",
        "--grade",
        "B",
        "--rounds",
        "1",
        "--blockers-found",
        "0",
        "--completed",
        "1",
    ]
    if c_effort:
        up_args.extend(["--effort", c_effort])
    cli.main(up_args)

    # Same-tier run: table B profile on grade B task
    same_args = [
        "reps",
        "add",
        "--profile",
        b_profile,
        "--model",
        "test-model-2",
        "--task",
        "ROB-1220-2",
        "--tier",
        "T1",
        "--role",
        "impl",
        "--grade",
        "B",
        "--rounds",
        "2",
        "--blockers-found",
        "1",
        "--completed",
        "1",
    ]
    if b_effort:
        same_args.extend(["--effort", b_effort])
    cli.main(same_args)
    capsys.readouterr()

    assert cli.main(["reps", "compare", "--grade", "B"]) == 0
    compare_out = capsys.readouterr().out
    assert "reps 비교 grade=B" in compare_out
    assert f"profile={c_profile}" in compare_out
    assert "상방 1건 / 동급 0건 / 하방 0건" in compare_out
    assert f"profile={b_profile}" in compare_out
    assert "상방 0건 / 동급 1건 / 하방 0건" in compare_out


def test_d4_backfill_idempotent_and_preserves_null(tmp_path):
    import sqlite3

    b_profile, b_effort, b_grade = _find_profile_for_grade("B")
    a_profile, a_effort, a_grade = _find_profile_for_grade("A")
    s_profile, s_effort, s_grade = _find_profile_for_grade("S")

    db = tmp_path / "bench.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        f"""
        CREATE TABLE reps (
          id             INTEGER PRIMARY KEY,
          profile        TEXT NOT NULL,
          model_id       TEXT,
          task_ref       TEXT,
          tier           TEXT,
          role           TEXT,
          rounds         INTEGER,
          blockers_found INTEGER,
          completed      INTEGER,
          input_tokens   INTEGER,
          output_tokens  INTEGER,
          notes          TEXT,
          recorded_at    TEXT NOT NULL,
          effort         TEXT,
          grade          TEXT
        );
        INSERT INTO reps (
          id, profile, model_id, task_ref, tier, role,
          rounds, blockers_found, completed, recorded_at, effort, grade
        ) VALUES
          (1, 'codex-luna-ultra', 'm1', 'R1', 'T1', 'impl', 1, 0, 1, '2026-08-06Z', NULL, NULL),
          (2, 'codex-luna-ultra', 'm2', 'R2', 'T1', 'impl', 1, 0, 1, '2026-08-06Z', NULL, NULL),
          (3, '{b_profile}', 'm3', 'R3', 'T1', 'i', 1, 0, 1, '2026-08-06Z', '{b_effort or ""}', '{b_grade}'),
          (4, '{b_profile}', 'm4', 'R4', 'T1', 'i', 1, 0, 1, '2026-08-06Z', '{b_effort or ""}', '{b_grade}'),
          (5, '{a_profile}', 'm5', 'R5', 'T1', 'i', 1, 0, 1, '2026-08-06Z', '{a_effort or ""}', '{a_grade}'),
          (6, '{s_profile}', 'm6', 'R6', 'T1', 'i', 1, 0, 1, '2026-08-06Z', '{s_effort or ""}', '{s_grade}');
        """
    )
    conn.commit()
    conn.close()

    # First migration run
    conn1 = bench.connect(db)
    reps1 = bench.read_reps(path=db)
    conn1.close()
    by_id1 = {r.id: r for r in reps1}

    # Verify D4: grade IS NULL rows preserve table_grade IS NULL
    assert by_id1[1].grade is None and by_id1[1].table_grade is None
    assert by_id1[2].grade is None and by_id1[2].table_grade is None

    # Verify D4: grade IS NOT NULL rows get table_grade populated matching derived table grade
    assert by_id1[3].grade == b_grade and by_id1[3].table_grade == b_grade
    assert by_id1[4].grade == b_grade and by_id1[4].table_grade == b_grade
    assert by_id1[5].grade == a_grade and by_id1[5].table_grade == a_grade
    assert by_id1[6].grade == s_grade and by_id1[6].table_grade == s_grade

    # Second migration run (test rerun safety / idempotency)
    conn2 = bench.connect(db)
    reps2 = bench.read_reps(path=db)
    conn2.close()
    by_id2 = {r.id: r for r in reps2}

    assert by_id1 == by_id2


def test_read_path_triggers_schema_migration_and_backfill(tmp_path):
    import sqlite3

    c_profile, c_effort, c_grade = _find_profile_for_grade("C")
    b_profile, b_effort, b_grade = _find_profile_for_grade("B")
    a_profile, a_effort, a_grade = _find_profile_for_grade("A")
    s_profile, s_effort, s_grade = _find_profile_for_grade("S")

    db = tmp_path / "legacy_bench.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        f"""
        CREATE TABLE reps (
          id             INTEGER PRIMARY KEY,
          profile        TEXT NOT NULL,
          model_id       TEXT,
          task_ref       TEXT,
          tier           TEXT,
          role           TEXT,
          rounds         INTEGER,
          blockers_found INTEGER,
          completed      INTEGER,
          input_tokens   INTEGER,
          output_tokens  INTEGER,
          notes          TEXT,
          recorded_at    TEXT NOT NULL,
          effort         TEXT,
          grade          TEXT
        );
        INSERT INTO reps (
          id, profile, model_id, task_ref, tier, role,
          rounds, blockers_found, completed, recorded_at, effort, grade
        ) VALUES
          (1, 'codex-luna-ultra', 'm1', 'ROB-1', 'T1', 'impl', 1, 0, 1, '2026-08-06Z', NULL, NULL),
          (2, 'codex-luna-ultra', 'm2', 'ROB-2', 'T1', 'impl', 1, 0, 1, '2026-08-06Z', NULL, NULL),
          (3, '{b_profile}', 'm3', 'ROB-3', 'T1', 'impl', 1, 0, 1, '2026-08-06Z', '{b_effort or ""}', 'A+'),
          (4, '{b_profile}', 'm4', 'ROB-4', 'T1', 'impl', 1, 0, 1, '2026-08-06Z', '{b_effort or ""}', 'A'),
          (5, '{a_profile}', 'm5', 'ROB-5', 'T1', 'impl', 1, 0, 1, '2026-08-06Z', '{a_effort or ""}', 'B'),
          (6, '{s_profile}', 'm6', 'ROB-6', 'T1', 'impl', 1, 0, 1, '2026-08-06Z', '{s_effort or ""}', 'C'),
          (7, '{c_profile}', 'm7', 'ROB-7', 'T1', 'impl', 1, 0, 1, '2026-08-06Z', '{c_effort or ""}', 'B');
        """
    )
    conn.commit()
    conn.close()

    # R1: Run ONLY read_reps (reps list) on legacy schema DB copy
    reps = bench.read_reps(path=db)
    assert len(reps) == 7

    # Verify R1: table_grade column exists in database schema
    conn_verify = sqlite3.connect(str(db))
    conn_verify.row_factory = sqlite3.Row
    columns = [row[1] for row in conn_verify.execute("PRAGMA table_info(reps)").fetchall()]
    assert "table_grade" in columns

    # Verify R2: id 3~7 table_grade populated, id 1~2 NULL preserved
    by_id = {
        row["id"]: dict(row)
        for row in conn_verify.execute("SELECT id, grade, table_grade FROM reps").fetchall()
    }
    conn_verify.close()

    assert by_id[1]["grade"] is None and by_id[1]["table_grade"] is None
    assert by_id[2]["grade"] is None and by_id[2]["table_grade"] is None
    assert by_id[3]["table_grade"] == b_grade
    assert by_id[4]["table_grade"] == b_grade
    assert by_id[5]["table_grade"] == a_grade
    assert by_id[6]["table_grade"] == s_grade
    assert by_id[7]["table_grade"] == c_grade

    # R3: Run compare_reps --grade B on legacy schema DB copy -> 1 upward item (c_profile table C, task B)
    comps = bench.compare_reps(grade="B", path=db)
    c_comp = next(c for c in comps if c.profile == c_profile)
    assert c_comp.upward_count == 1
    assert c_comp.same_count == 0

    # R4: Re-run safety (consecutive execution yields identical result)
    reps2 = bench.read_reps(path=db)
    assert len(reps2) == 7
