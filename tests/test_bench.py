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
    assert "unspecified" in out
    assert "2026-07-31T12:00:00+00:00" in out


def test_fresh_show_persists_grade_seed_idempotently_and_preserves_newer_value(bench_home):
    path = bench.db_path()
    assert not path.exists()

    first_insert = bench.seed_scores()
    second_insert = bench.seed_scores()
    assert first_insert > 0
    assert second_insert == 0
    seeded = next(row for row in bench.read_scores("gpt-5.6-terra") if row.source == "AA-agent")
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
    current = next(row for row in bench.read_scores("gpt-5.6-terra") if row.source == "AA-agent")
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
        lambda: {name: healthy(name) for name in ("codex", "clinepass", "grok")},
    )
    assert cli.main(["--recommend", "S", "--no-cache"]) == 0
    out = capsys.readouterr().out
    bench_cells = [line.split("벤치 ", 1)[1] for line in out.splitlines() if "벤치 " in line]
    assert bench_cells
    assert all("(" in cell for cell in bench_cells)
    # ROB-1191 ④ compact single-effort cells
    assert "78.0(AA-agent; metric=agentic; harness=codex; effort=max)" not in out
    assert "62.0(AA-agent/codex/max)" in out
    assert "57.1(openrouter/unspecified)" in out


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
    assert "벤치 62.0(AA-agent/codex/max)" in codex_line
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
    assert "1.0(AA-agent/codex/max)" in ranked[0]
    assert "99.0(openrouter/unspecified)" in ranked[1]


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
