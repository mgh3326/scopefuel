from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tarfile
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from scopefuel import cli, recommend
from scopefuel.model import Bucket, ProviderResult, Scope

NOW = dt.datetime(2026, 10, 26, 12, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
PACE_BASE_COMMIT = "49a25ed181f1342f78b0c12ff5a997ffed88a529"


def _bucket(window: str, used: float, reset_in_hours: float | None, *, scope: Scope | None = None) -> Bucket:
    reset = None if reset_in_hours is None else (NOW + dt.timedelta(hours=reset_in_hours)).isoformat()
    return Bucket(
        label=window,
        window=window,
        used_pct=used,
        resets_at=reset,
        scope=scope or Scope("account"),
        horizon="now" if window == "5h" else "week",
    )


def _provider(pool: str, *buckets: Bucket, pool_class: str = "spend") -> ProviderResult:
    return ProviderResult(id=pool, pool_class=pool_class, buckets=list(buckets))  # type: ignore[arg-type]


def _load_pace_base(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = pathlib.Path(__file__).resolve().parents[1]
    if subprocess.run(
        ["git", "cat-file", "-e", PACE_BASE_COMMIT], cwd=repo, capture_output=True
    ).returncode:
        subprocess.run(
            ["git", "fetch", "--depth=1", "origin", PACE_BASE_COMMIT],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    archive = subprocess.run(
        ["git", "archive", PACE_BASE_COMMIT, "src"], cwd=repo, check=True, capture_output=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(tmp_path, filter="data")
    package = tmp_path / "src/scopefuel"
    spec = importlib.util.spec_from_file_location(
        "scopefuel_pace_base", package / "__init__.py", submodule_search_locations=[str(package)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return importlib.import_module("scopefuel_pace_base.recommend")


@pytest.fixture(autouse=True)
def isolate_gate_side_effects(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(recommend, "_observe_exhaustion", lambda *args, **kwargs: None)
    monkeypatch.setattr(recommend, "_inflight_gate", lambda *args, **kwargs: None)


def test_weekly_over_pace_is_admitted_and_reports_all_required_fields(monkeypatch):
    monkeypatch.setattr(
        recommend,
        "_inflight_gate",
        lambda *args, **kwargs: SimpleNamespace(
            n=2, tag="inflight=2", warning=None, status=None, reserves={}, unknown=False
        ),
    )
    result = recommend.gate_check(
        [_provider("claude", _bucket("7d", 50, 144))], "sonnet", today=TODAY, now=NOW
    )

    assert result.ok is True
    assert "pace-warning pool=claude window=7d" in result.reason
    for field in (
        "pool=claude",
        "window=7d",
        "remaining=50%",
        "hours_to_reset=144h",
        "time_to_exhaust=24h",
        "allowed_daily_budget=8.33%/day",
        "inflight=2",
    ):
        assert field in result.reason


def test_weekly_pace_with_headroom_is_admitted():
    result = recommend.gate_check(
        [_provider("claude", _bucket("7d", 20, 120))], "sonnet", today=TODAY, now=NOW
    )
    assert result.ok is True
    assert "pace=within-limit" in result.reason


def test_5h_only_over_pace_is_admitted_with_warning():
    result = recommend.gate_check(
        [
            _provider(
                "grok",
                _bucket("5h", 50, 4),
                _bucket("7d", 20, 120),
            )
        ],
        "grok-hi",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True
    assert "pace-warning pool=grok window=5h" in result.reason
    assert "window=5h" in result.reason


def test_consult_only_over_pace_is_admitted_with_operator_request():
    result = recommend.gate_check(
        [_provider("claude", _bucket("7d", 50, 144))],
        "fable",
        today=TODAY,
        now=NOW,
        operator_request="hk:task/1",
    )
    assert result.ok is True
    assert result.operator_request_ref == "hk:task/1"
    assert "pace-warning pool=claude window=7d" in result.reason


@pytest.mark.parametrize(("window", "used"), [("7d", 99.0), ("7d", 100.0), ("5h", 100.0), ("1d", 100.0)])
def test_real_exhaustion_cutoff_still_refuses(window, used):
    result = recommend.gate_check(
        [_provider("claude", _bucket(window, used, 4 if window == "5h" else 144))],
        "sonnet",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert "pace-warning" not in result.reason


def test_decision_equivalence_to_real_exhaustion_rule():
    snapshots = [
        ("in pace", "7d", 20.0, 120.0, False),
        ("weekly over pace", "7d", 50.0, 144.0, False),
        ("5h over pace", "5h", 50.0, 4.0, False),
        ("99 percent cutoff", "7d", 99.0, 144.0, True),
        ("100 percent used", "7d", 100.0, 144.0, True),
    ]
    for _name, window, used, reset, refuses in snapshots:
        result = recommend.gate_check(
            [_provider("claude", _bucket(window, used, reset))],
            "sonnet",
            today=TODAY,
            now=NOW,
        )
        assert result.ok is not refuses


def test_underpace_and_nonspend_outputs_match_49a25ed(tmp_path, monkeypatch):
    base = _load_pace_base(tmp_path / "base")
    monkeypatch.setattr(base, "_observe_exhaustion", lambda *args, **kwargs: None)
    monkeypatch.setattr(base, "_inflight_gate", lambda *args, **kwargs: None)

    def snapshot(module):
        model = importlib.import_module(f"{module.__package__}.model")

        def item(pool, used, reset, pool_class):
            bucket = model.Bucket(
                label="7d",
                window="7d",
                used_pct=used,
                resets_at=(NOW + dt.timedelta(hours=reset)).isoformat(),
                scope=model.Scope("account"),
                horizon="week",
            )
            return model.ProviderResult(id=pool, pool_class=pool_class, buckets=[bucket])

        return [item("claude", 20, 120, "spend"), item("codex", 80, 144, "preserve")]

    for profile in ("sonnet", "codex-luna"):
        current_result = recommend.gate_check(snapshot(recommend), profile, today=TODAY, now=NOW)
        base_result = base.gate_check(snapshot(base), profile, today=TODAY, now=NOW)
        assert asdict(current_result) == asdict(base_result)

    outputs = []
    for module in (recommend, base):
        profiles = {profile.name: profile for rows in module.GRADE_TABLE.values() for profile in rows}
        table = {grade: [] for grade in module.GRADE_TABLE}
        table["A"] = [profiles["sonnet"], profiles["codex-luna"]]
        providers = snapshot(module)
        outputs.append(
            (
                module.recommend(providers, "A", today=TODAY, now=NOW, grade_table=table),
                json.dumps(
                    module.recommend_dict(providers, "A", today=TODAY, now=NOW, grade_table=table),
                    ensure_ascii=False,
                ),
            )
        )
    assert outputs[0] == outputs[1]


def test_non_spend_pool_and_swe2_free_bucket_are_exempt():
    preserve = recommend.gate_check(
        [_provider("codex", _bucket("7d", 80, 144), pool_class="preserve")],
        "codex-luna",
        today=TODAY,
        now=NOW,
    )
    free_bucket = _bucket("7d", 80, 144, scope=Scope("model", "swe-2-high"))
    free = recommend.gate_check([_provider("devin", free_bucket)], "devin-swe2", today=TODAY, now=NOW)
    assert preserve.ok is True
    assert free.ok is True
    assert "pace_exhaustion" not in free.reason


def test_pace_override_requires_valid_ref_and_records_valid_ref():
    provider = [_provider("claude", _bucket("7d", 50, 144))]
    admitted = recommend.gate_check(
        provider,
        "sonnet",
        today=TODAY,
        now=NOW,
        pace_override_ref="hk:task/625",
    )
    malformed = recommend.gate_check(
        provider,
        "sonnet",
        today=TODAY,
        now=NOW,
        pace_override_ref="please-override",
    )
    assert admitted.ok is True
    assert admitted.pace_override_ref == "hk:task/625"
    assert "pace-warning pool=claude window=7d" in admitted.reason
    assert malformed.ok is False
    assert "pace_override_ref_invalid" in malformed.reason


def test_pace_override_does_not_satisfy_existing_consult_only_rung_rule():
    result = recommend.gate_check(
        [],
        "fable",
        today=TODAY,
        now=NOW,
        pace_override_ref="hk:task/625",
    )
    assert result.ok is False
    assert "--operator-request" in result.reason


def test_consult_only_spend_pool_records_both_refs_without_pace_override():
    provider = [_provider("claude", _bucket("7d", 50, 144))]
    admitted = recommend.gate_check(
        provider,
        "fable",
        today=TODAY,
        now=NOW,
        operator_request="hk:task/1",
    )
    overridden = recommend.gate_check(
        provider,
        "fable",
        today=TODAY,
        now=NOW,
        operator_request="hk:task/1",
        pace_override_ref="hk:task/2",
    )

    assert admitted.ok is True
    assert "pace-warning pool=claude window=7d" in admitted.reason
    assert admitted.operator_request_ref == "hk:task/1"
    assert overridden.ok is True
    assert "pace-warning pool=claude window=7d" in overridden.reason
    assert overridden.operator_request_ref == "hk:task/1"
    assert overridden.pace_override_ref == "hk:task/2"


def test_cli_option_and_gate_record_carry_the_pace_override_ref(monkeypatch, tmp_path, capsys):
    provider = _provider("claude", _bucket("7d", 50, 144))

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(cli, "dt", SimpleNamespace(datetime=FrozenDateTime, UTC=dt.UTC))
    monkeypatch.setattr(cli, "registry", lambda: {"claude": object()})
    monkeypatch.setattr(cli, "collect", lambda *args, **kwargs: [provider])
    monkeypatch.setattr(cli.bench, "read_scores", lambda: [])
    monkeypatch.setattr(cli.bench, "read_prices", lambda: {})
    monkeypatch.setattr(cli.bench, "runtime_grade_table", lambda: recommend.GRADE_TABLE)
    monkeypatch.setattr(cli, "_retired_rungs", lambda: frozenset())
    monkeypatch.setattr(cli, "_consult_only_rungs", lambda: frozenset())
    monkeypatch.setattr(cli, "_gate_policy_context", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_shadow_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_emit_stale_build_warning", lambda: None)
    record_path = tmp_path / "gate.json"

    admitted_rc = cli.main(["gate", "-m", "sonnet", "--no-cache"])
    assert admitted_rc == 0
    assert "pace-warning pool=claude window=7d" in capsys.readouterr().out

    rc = cli.main(
        [
            "gate",
            "-m",
            "sonnet",
            "--pace-override",
            "hk:task/625",
            "--gate-output",
            str(record_path),
            "--no-cache",
        ]
    )
    record = json.loads(record_path.read_text())
    assert rc == 0
    assert "pace_override_ref=hk:task/625" in capsys.readouterr().out
    assert record["pace_override_ref"] == "hk:task/625"
    assert record["operator_request_ref"] is None


def test_gate_help_states_pace_rule_and_exemptions(capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(["gate", "--help"])
    assert result.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--pace-override REF" in help_text
    assert "첫 5%" in help_text
    assert "non-spend" in help_text
    assert "SWE-2 free" in help_text
    assert "감사" in help_text
    assert "real exhaustion refuses" in help_text


def test_recommend_help_states_pace_ranks_and_warns(capsys):
    parser = cli.build_parser([])
    with pytest.raises(SystemExit) as result:
        parser.parse_args(["--help"])
    assert result.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Pace ranks and warns; only real exhaustion refuses" in help_text


@pytest.mark.parametrize(
    ("used", "reset_in_hours", "elapsed_fraction"),
    [(50, None, None), (50, 167.16, 0.01)],
)
def test_uncomputable_or_early_window_is_admitted_and_named_pace_unknown(
    used, reset_in_hours, elapsed_fraction
):
    if elapsed_fraction is not None:
        reset_in_hours = 168 * (1 - elapsed_fraction)
    result = recommend.gate_check(
        [_provider("claude", _bucket("7d", used, reset_in_hours))],
        "sonnet",
        today=TODAY,
        now=NOW,
    )
    assert recommend.PACE_EARLY_WINDOW_FRACTION == 0.05
    assert result.ok is True
    assert "pace-unknown window=7d" in result.reason


def test_operator_snapshot_claude_over_pace_is_admitted_and_codex_is_admitted():
    claude = recommend.gate_check(
        [_provider("claude", _bucket("7d", 69, 127.2))], "sonnet", today=TODAY, now=NOW
    )
    codex = recommend.gate_check(
        [_provider("codex", _bucket("7d", 4, 127.2), pool_class="preserve")],
        "codex-luna",
        today=TODAY,
        now=NOW,
    )
    assert claude.ok is True
    assert "pace-warning pool=claude window=7d" in claude.reason
    assert codex.ok is True


def test_manual_window_observation_uses_the_same_pace_admission():
    manual = _provider("claude", _bucket("7d", 50, 144))
    manual.source = "manual"
    result = recommend.gate_check([manual], "sonnet", today=TODAY, now=NOW)
    assert result.ok is True
    assert "pace-warning pool=claude window=7d" in result.reason


def test_recommend_lists_over_pace_rows_with_warnings_and_headroom_order():
    profiles = {profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows}
    table = {grade: [] for grade in recommend.GRADE_TABLE}
    table["A"] = [profiles["sonnet"], profiles["grok-hi"], profiles["kimi-k3-low"]]
    providers = [
        _provider("claude", _bucket("7d", 20, 120)),
        _provider("grok", _bucket("7d", 50, 144)),
        _provider("kimi", _bucket("7d", 80, 144)),
    ]
    data = recommend.recommend_dict(providers, "A", today=TODAY, now=NOW, grade_table=table)
    assert [row["profile"] for row in data["rows"]] == ["sonnet", "grok-hi", "kimi-k3-low"]
    assert all(row["kind"] == "candidate" for row in data["rows"])
    grok = next(row for row in data["rows"] if row["profile"] == "grok-hi")
    kimi = next(row for row in data["rows"] if row["profile"] == "kimi-k3-low")
    assert "pace-warning pool=grok window=7d" in grok["windows"]
    assert "pace-warning pool=kimi window=7d" in kimi["windows"]


def test_swe2_free_bucket_stays_listed_and_exempt_from_pace_and_waste():
    profiles = {profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows}
    table = {grade: [] for grade in recommend.GRADE_TABLE}
    table["A+"] = [profiles["devin-swe2"]]
    evaluation = recommend._evaluate(
        [_provider("devin", _bucket("7d", 80, 144, scope=Scope("model", "swe-2-high")))],
        "A+",
        TODAY,
        NOW,
        urgency_hours=24,
        bench_scores=None,
        normalized_prices={},
        table=table,
    )
    assert len(evaluation.included) == 1
    candidate = evaluation.included[0]
    assert "pace=exempt (SWE-2 free bucket)" in candidate.windows_display
    assert candidate.waste_term == 0


def test_recommend_marks_pace_unknown_in_candidate_windows():
    profiles = {profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows}
    table = {grade: [] for grade in recommend.GRADE_TABLE}
    table["A+"] = [profiles["grok-hi"]]
    result = recommend.recommend_dict(
        [_provider("grok", _bucket("7d", 20, None))],
        "A+",
        today=TODAY,
        now=NOW,
        grade_table=table,
    )
    row = next(item for item in result["rows"] if item["profile"] == "grok-hi")
    assert row["kind"] == "candidate"
    assert "pace-unknown window=7d" in row["windows"]


def test_recommend_unknown_pace_does_not_get_waste_boost():
    profiles = {profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows}
    table = {grade: [] for grade in recommend.GRADE_TABLE}
    table["A+"] = [profiles["grok-hi"]]
    evaluation = recommend._evaluate(
        [_provider("grok", _bucket("7d", 1, 160))],
        "A+",
        TODAY,
        NOW,
        urgency_hours=24,
        bench_scores=None,
        normalized_prices={},
        table=table,
    )

    assert len(evaluation.included) == 1
    candidate = evaluation.included[0]
    assert "pace-unknown window=7d" in candidate.windows_display
    assert candidate.waste_term == 0
