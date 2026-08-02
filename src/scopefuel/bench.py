"""SQLite-backed benchmark scores and manual representative-run records.

Benchmark values are kept separate by ``source`` and ``metric``.  The module
does not rank or compare values across those dimensions; it only stores the
rank supplied by a source or computes a rank within one source/metric pair.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import sqlite3
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TextIO

from .http import HttpError, request_json

AA_API_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
API_KEY_ENV = "ARTIFICIAL_ANALYSIS_API_KEY"
DOTENV_PATH = pathlib.Path("~/work/scopefuel/.env")
SEED_CAPTURED_AT = "2026-07-31T00:00:00+00:00"

MANUAL_SOURCES = frozenset({"AA-agent", "benchlm", "openrouter"})
SOURCES = MANUAL_SOURCES | {"AA-model"}
METRICS = frozenset({"coding_index", "intelligence", "agentic", "coding"})
APPROVED_EFFORTS = frozenset({"default", "low", "medium", "high", "xhigh", "max", "non-reasoning"})
REP_EFFORTS = ("low", "medium", "high", "xhigh", "max")
REP_GRADES = ("S+", "S", "A+", "A", "B", "C")

# ROB-1190 ②-1: AA-model slug 의 effort 접미사. 순서가 중요하다 — "non-reasoning" 이
# "-high"/"-low" 등 다른 접미사의 부분열이 아니므로 순서 무관하지만, 길이가 긴 접미사부터
# 검사해 예를 들어 "-xhigh" 를 "-high" 로 오매칭하지 않게 한다.
_EFFORT_SUFFIXES: tuple[str, ...] = (
    "-xhigh",
    "-non-reasoning",
    "-high",
    "-medium",
    "-low",
)


def parse_effort_suffix(model_id: str) -> tuple[str, str | None]:
    """AA-model slug 에서 effort 접미사를 분리한다.

    반환: (정규화된 base model_id, effort or None). 접미사가 없으면 effort=None —
    "무접미사가 무슨 effort 인지"는 AA 공식 문서/API 필드로 확정할 수 없으므로(ROB-1190 ②-2,
    확인: /api/v2/language/models 응답 스키마에 reasoning effort 레벨 필드가 없고,
    "GPT-5.4" 무접미사와 "GPT-5.4 (xhigh)" 가 사이트에서 별개 페이지로 존재하며 실측
    스코어 방향이 모델마다 다르다 — Sol 은 무접미사(77.4) < xhigh(78.3) 인데 Terra/Luna 는
    반대), 이 함수는 effort=None 을 반환하고 호출자가 그 의미를 ``"unspecified"`` 로
    명시하며 추측하지 않는다.
    """
    lowered = model_id.lower()
    for suffix in _EFFORT_SUFFIXES:
        if lowered.endswith(suffix):
            base = model_id[: -len(suffix)]
            effort = suffix[1:]  # "-xhigh" -> "xhigh"
            return base, effort
    return model_id, None


def normalize_aa_model_id(model_id: str) -> str:
    """Normalize AA-model source slugs for profile-to-row comparisons."""

    return model_id.strip().lower().replace(".", "-")


def display_effort(effort: str | None) -> str:
    """Render a missing effort explicitly instead of silently hiding it."""

    return effort or "unspecified"


_MODEL_SCORE_COLUMNS = (
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
)
_REP_COLUMNS = (
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
)

# ROB-1194: operator-approved AA-agent measurements.  The complete key is
# intentional: model, effort, harness, source, and metric identify one row.
# These values are display metadata only; recommendation ranking never reads
# them.
AA_AGENT_MEASUREMENTS: tuple[tuple[str, str, str, str, str, float, float | None], ...] = (
    ("gpt-5.6-luna", "medium", "codex", "AA-agent", "agentic", 3.4, None),
    ("gpt-5.6-sol", "low", "codex", "AA-agent", "agentic", 3.7, None),
    ("gpt-5.6-terra", "medium", "codex", "AA-agent", "agentic", 4.3, None),
    ("gpt-5.6-sol", "medium", "codex", "AA-agent", "agentic", 5.2, 2.99),
    ("gpt-5.6-luna", "high", "codex", "AA-agent", "agentic", 5.7, None),
    ("gpt-5.6-terra", "high", "codex", "AA-agent", "agentic", 6.2, None),
    ("gpt-5.6-sol", "high", "codex", "AA-agent", "agentic", 6.3, 4.14),
    ("gpt-5.6-luna", "xhigh", "codex", "AA-agent", "agentic", 6.6, None),
    ("gpt-5.6-terra", "xhigh", "codex", "AA-agent", "agentic", 6.9, None),
    ("gpt-5.6-sol", "xhigh", "codex", "AA-agent", "agentic", 7.4, 5.24),
    ("gpt-5.6-luna", "max", "codex", "AA-agent", "agentic", 8.0, None),
    ("gpt-5.6-terra", "max", "codex", "AA-agent", "agentic", 8.4, None),
    ("claude-opus-5", "low", "claude-code", "AA-agent", "agentic", 9.5, None),
    ("gpt-5.6-sol", "max", "codex", "AA-agent", "agentic", 10.2, 7.08),
    ("claude-opus-4.7", "medium", "opencode", "AA-agent", "agentic", 12.2, 2.93),
    ("claude-opus-5", "medium", "claude-code", "AA-agent", "agentic", 12.2, 3.14),
    ("muse-spark-1.1", "xhigh", "opencode", "AA-agent", "agentic", 12.6, None),
    ("claude-opus-5", "high", "claude-code", "AA-agent", "agentic", 13.4, 3.80),
    ("claude-sonnet-4.6", "medium", "claude-code", "AA-agent", "agentic", 13.5, None),
    ("grok-4.5", "high", "grok-build", "AA-agent", "agentic", 16.5, None),
    ("claude-fable-5", "max", "claude-code", "AA-agent", "agentic", 23.4, 11.7),
    ("claude-opus-5", "xhigh", "claude-code", "AA-agent", "agentic", 23.6, 8.23),
    ("claude-opus-5", "max", "claude-code", "AA-agent", "agentic", 23.7, 8.95),
    ("kimi-k3", "default", "kimi-code-cli", "AA-agent", "agentic", 23.8, 3.18),
    ("glm-5.2", "default", "claude-code", "AA-agent", "agentic", 25.1, 6.51),
    ("kimi-k2.6", "default", "claude-code", "AA-agent", "agentic", 41.0, None),
)

_AA_AGENT_MEASUREMENT_BY_KEY: dict[tuple[str, str, str, str, str], tuple[float, float | None]] = {
    row[:5]: (row[5], row[6]) for row in AA_AGENT_MEASUREMENTS
}
if len(_AA_AGENT_MEASUREMENT_BY_KEY) != len(AA_AGENT_MEASUREMENTS):  # pragma: no cover - static guard
    raise RuntimeError("duplicate ROB-1194 AA-agent measurement key")


class BenchError(ValueError):
    """A user-facing validation or upstream-data error."""


@dataclass(frozen=True)
class ModelScore:
    model_id: str
    effort: str | None
    harness: str | None
    source: str
    metric: str
    score: float | None
    rank: int | None
    captured_at: str
    time_per_task_min: float | None = None
    cost_per_task_usd: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {column: getattr(self, column) for column in _MODEL_SCORE_COLUMNS}


@dataclass(frozen=True)
class RepRecord:
    id: int
    profile: str
    model_id: str | None
    task_ref: str | None
    tier: str | None
    role: str | None
    rounds: int | None
    blockers_found: int | None
    completed: int | None
    input_tokens: int | None
    output_tokens: int | None
    notes: str | None
    recorded_at: str
    effort: str | None
    grade: str | None

    def as_dict(self) -> dict[str, object]:
        return {column: getattr(self, column) for column in _REP_COLUMNS}


@dataclass(frozen=True)
class RepComparison:
    profile: str
    count: int
    average_rounds: float | None
    average_blockers_found: float | None
    completion_rate: float
    average_input_tokens: float | None
    average_output_tokens: float | None


def db_path() -> pathlib.Path:
    """Return the persistent data path, never the cache path."""

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    base = (
        pathlib.Path(os.path.expanduser(xdg_data_home))
        if xdg_data_home
        else pathlib.Path.home() / ".local" / "share"
    )
    return base / "scopefuel" / "bench.db"


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS model_scores (
          model_id    TEXT NOT NULL,
          effort      TEXT,
          harness     TEXT,
          source      TEXT NOT NULL,
          metric      TEXT NOT NULL,
          score       REAL,
          rank        INTEGER,
          captured_at TEXT NOT NULL,
          time_per_task_min REAL,
          cost_per_task_usd REAL,
          PRIMARY KEY (model_id, effort, harness, source, metric)
        );

        CREATE TABLE IF NOT EXISTS reps (
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
          recorded_at    TEXT NOT NULL
        );
        """
    )
    existing_score_columns = {row[1] for row in conn.execute("PRAGMA table_info(model_scores)").fetchall()}
    for column in ("time_per_task_min", "cost_per_task_usd"):
        if column not in existing_score_columns:
            conn.execute(f"ALTER TABLE model_scores ADD COLUMN {column} REAL")
    existing_rep_columns = {row[1] for row in conn.execute("PRAGMA table_info(reps)").fetchall()}
    for column in ("input_tokens", "output_tokens"):
        if column not in existing_rep_columns:
            conn.execute(f"ALTER TABLE reps ADD COLUMN {column} INTEGER")
    for column in ("effort", "grade"):
        if column not in existing_rep_columns:
            conn.execute(f"ALTER TABLE reps ADD COLUMN {column} TEXT")


def connect(path: pathlib.Path | str | None = None) -> sqlite3.Connection:
    """Open the bench DB and ensure both approved tables exist."""

    target = pathlib.Path(path) if path is not None else db_path()
    if str(target) != ":memory:":
        target = pathlib.Path(os.path.expanduser(str(target)))
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    _schema(conn)
    conn.commit()
    return conn


def _text(value: object, field: str, *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise BenchError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise BenchError(f"{field} must be a string")
    value = value.strip()
    if not value and required:
        raise BenchError(f"{field} must not be empty")
    return value or None


def _model_id(value: object) -> str:
    text = _text(value, "model_id")
    assert text is not None
    return text.lower()


def _optional_text(value: object, field: str) -> str | None:
    return _text(value, field, required=False)


def _rep_choice(value: object, field: str, choices: tuple[str, ...]) -> str | None:
    value = _optional_text(value, field)
    if value is not None and value not in choices:
        allowed = ", ".join(choices)
        raise BenchError(f"{field} must be one of: {allowed}")
    return value


def _score(value: object, *, allow_none: bool) -> float | None:
    if value is None:
        if allow_none:
            return None
        raise BenchError("score is required")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchError("score must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise BenchError("score must be between 0 and 100")
    return result


def _measurement(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchError(f"{field} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise BenchError(f"{field} must be a finite non-negative number")
    return result


def _rank(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BenchError("rank must be a positive integer")
    return value


def _captured_at(value: object) -> str:
    result = _text(value, "captured_at")
    assert result is not None
    try:
        dt.datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BenchError("captured_at must be ISO-8601") from exc
    return result


def _validate_score(
    value: ModelScore,
    *,
    allow_aa_model: bool = True,
    allow_none_score: bool = True,
) -> ModelScore:
    model_id = _model_id(value.model_id)
    source = _text(value.source, "source")
    assert source is not None
    if source not in SOURCES or (not allow_aa_model and source == "AA-model"):
        raise BenchError(f"unsupported source: {source}")
    metric = _text(value.metric, "metric")
    assert metric is not None
    if metric not in METRICS:
        raise BenchError(f"unsupported metric: {metric}")
    effort = _optional_text(value.effort, "effort")
    if effort is not None and effort not in APPROVED_EFFORTS:
        raise BenchError(f"unsupported effort: {effort}")
    harness = _optional_text(value.harness, "harness")
    if source == "AA-agent" and (effort is None or harness is None):
        raise BenchError("AA-agent rows require effort and harness")
    return ModelScore(
        model_id=model_id,
        effort=effort,
        harness=harness,
        source=source,
        metric=metric,
        score=_score(value.score, allow_none=allow_none_score),
        rank=_rank(value.rank),
        captured_at=_captured_at(value.captured_at),
        time_per_task_min=_measurement(value.time_per_task_min, "time_per_task_min"),
        cost_per_task_usd=_measurement(value.cost_per_task_usd, "cost_per_task_usd"),
    )


def _score_from_row(row: sqlite3.Row) -> ModelScore:
    return ModelScore(**{column: row[column] for column in _MODEL_SCORE_COLUMNS})


def _readonly_connect(target: pathlib.Path | str) -> sqlite3.Connection:
    """Open an existing bench DB without allowing schema or data writes."""

    path = pathlib.Path(os.path.expanduser(str(target))).absolute()
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _model_score_select_columns(conn: sqlite3.Connection) -> str:
    """Select the current score shape without migrating a legacy read target."""

    available = {row[1] for row in conn.execute("PRAGMA table_info(model_scores)").fetchall()}
    return ", ".join(
        column if column in available else f"NULL AS {column}" for column in _MODEL_SCORE_COLUMNS
    )


def read_scores(model_id: str | None = None, *, path: pathlib.Path | str | None = None) -> list[ModelScore]:
    """Read scores without creating a DB when the target does not exist."""

    target = pathlib.Path(path) if path is not None else db_path()
    if str(target) == ":memory:" or not target.expanduser().exists():
        return []
    normalized = _model_id(model_id) if model_id is not None else None
    conn = _readonly_connect(target)
    try:
        select_columns = _model_score_select_columns(conn)
        if normalized is None:
            rows = conn.execute(
                f"SELECT {select_columns} FROM model_scores "
                "ORDER BY source, metric, model_id, effort, harness"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {select_columns} FROM model_scores WHERE model_id = ? "
                "ORDER BY source, metric, effort, harness",
                (normalized,),
            ).fetchall()
        return [_score_from_row(row) for row in rows]
    finally:
        conn.close()


def _measurement_values(score: ModelScore) -> tuple[float | None, float | None]:
    approved = _AA_AGENT_MEASUREMENT_BY_KEY.get(
        (score.model_id, score.effort, score.harness, score.source, score.metric)
    )
    if approved is not None:
        return approved
    return score.time_per_task_min, score.cost_per_task_usd


def _upsert(conn: sqlite3.Connection, score: ModelScore) -> None:
    where = (
        "SELECT 1 FROM model_scores WHERE model_id = ? AND source = ? AND metric = ? "
        "AND effort IS ? AND harness IS ?"
    )
    key = (score.model_id, score.source, score.metric, score.effort, score.harness)
    found = conn.execute(where, key).fetchone()
    time_per_task_min, cost_per_task_usd = _measurement_values(score)
    values = (
        score.model_id,
        score.effort,
        score.harness,
        score.source,
        score.metric,
        score.score,
        score.rank,
        score.captured_at,
        time_per_task_min,
        cost_per_task_usd,
    )
    if found:
        conn.execute(
            "UPDATE model_scores SET score = ?, rank = ?, captured_at = ?, "
            "time_per_task_min = ?, cost_per_task_usd = ? "
            "WHERE model_id = ? AND source = ? AND metric = ? AND effort IS ? AND harness IS ?",
            (score.score, score.rank, score.captured_at, time_per_task_min, cost_per_task_usd, *key),
        )
    else:
        conn.execute(
            "INSERT INTO model_scores "
            "(model_id, effort, harness, source, metric, score, rank, captured_at, "
            "time_per_task_min, cost_per_task_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )


def _insert_if_missing(conn: sqlite3.Connection, score: ModelScore) -> bool:
    key = (score.model_id, score.source, score.metric, score.effort, score.harness)
    found = conn.execute(
        "SELECT 1 FROM model_scores WHERE model_id = ? AND source = ? AND metric = ? "
        "AND effort IS ? AND harness IS ?",
        key,
    ).fetchone()
    if found:
        return False
    time_per_task_min, cost_per_task_usd = _measurement_values(score)
    conn.execute(
        "INSERT INTO model_scores "
        "(model_id, effort, harness, source, metric, score, rank, captured_at, "
        "time_per_task_min, cost_per_task_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            score.model_id,
            score.effort,
            score.harness,
            score.source,
            score.metric,
            score.score,
            score.rank,
            score.captured_at,
            time_per_task_min,
            cost_per_task_usd,
        ),
    )
    return True


def _seed_scores() -> list[ModelScore]:
    """Build the approved GRADE_TABLE seed without importing it at module load time."""

    from .recommend import GRADE_TABLE

    unique: dict[tuple[object, ...], ModelScore] = {}
    for profiles in GRADE_TABLE.values():
        for profile in profiles:
            # AA-model values and estimates are display references. They are
            # shown from the local bench DB when present, but are not seeded
            # into a new DB as agent measurements.
            if (
                profile.benchmark is None
                or profile.benchmark_source == "AA-model"
                or (profile.benchmark_source is None and profile.benchmark_annotation)
            ):
                continue
            if (
                profile.benchmark_source is None
                or profile.benchmark_metric is None
                or profile.benchmark_model_id is None
            ):
                raise BenchError(f"missing benchmark metadata for {profile.name}")
            score = _validate_score(
                ModelScore(
                    model_id=profile.benchmark_model_id,
                    effort=profile.benchmark_effort,
                    harness=profile.benchmark_harness,
                    source=profile.benchmark_source,
                    metric=profile.benchmark_metric,
                    score=profile.benchmark,
                    rank=None,
                    captured_at=SEED_CAPTURED_AT,
                ),
                allow_aa_model=False,
                allow_none_score=False,
            )
            key = (score.model_id, score.effort, score.harness, score.source, score.metric)
            unique[key] = score
    return list(unique.values())


def _seed_conn(conn: sqlite3.Connection) -> int:
    scores = _seed_scores()
    inserted = sum(_insert_if_missing(conn, score) for score in scores)
    if inserted:
        for source, metric in {(score.source, score.metric) for score in scores}:
            _recompute_rank(conn, source, metric)
    return inserted


def _apply_known_aa_agent_measurements(conn: sqlite3.Connection) -> int:
    """Fill approved metadata for matching rows without creating new rows."""

    rows = conn.execute(
        "SELECT model_id, effort, harness, source, metric, time_per_task_min, cost_per_task_usd "
        "FROM model_scores WHERE source = 'AA-agent' AND metric = 'agentic'"
    ).fetchall()
    changed = 0
    for row in rows:
        key = (row["model_id"], row["effort"], row["harness"], row["source"], row["metric"])
        approved = _AA_AGENT_MEASUREMENT_BY_KEY.get(key)
        if approved is None or (row["time_per_task_min"], row["cost_per_task_usd"]) == approved:
            continue
        conn.execute(
            "UPDATE model_scores SET time_per_task_min = ?, cost_per_task_usd = ? "
            "WHERE model_id = ? AND source = ? AND metric = ? AND effort IS ? AND harness IS ?",
            (*approved, row["model_id"], row["source"], row["metric"], row["effort"], row["harness"]),
        )
        changed += 1
    return changed


def _strict_aa_agent_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT model_id, effort, harness, source, metric, time_per_task_min, cost_per_task_usd "
        "FROM model_scores WHERE source = 'AA-agent' AND metric = 'agentic' "
        "ORDER BY model_id, effort, harness"
    ).fetchall()
    keys = [(row["model_id"], row["effort"], row["harness"], row["source"], row["metric"]) for row in rows]
    expected = set(_AA_AGENT_MEASUREMENT_BY_KEY)
    actual = set(keys)

    def sort_key(key: tuple[object, ...]) -> tuple[str, ...]:
        return tuple("" if value is None else str(value) for value in key)

    duplicates = sorted({key for key in keys if keys.count(key) > 1}, key=sort_key)
    missing = sorted(expected - actual, key=sort_key)
    extra = sorted(actual - expected, key=sort_key)
    if missing or extra or duplicates:
        raise BenchError(
            "AA-agent/agentic key mismatch: "
            f"expected={len(expected)} actual={len(keys)} "
            f"missing={missing} extra={extra} duplicates={duplicates}"
        )
    return rows


def backfill_aa_agent_metrics(*, path: pathlib.Path | str | None = None) -> int:
    """Backfill the exact approved 26-row AA-agent metadata set.

    This is an explicit write operation.  It fails closed unless the target
    contains exactly the approved full keys, so a partial or ambiguous source
    cannot receive guessed updates.
    """

    conn = connect(path)
    try:
        _strict_aa_agent_rows(conn)
        changed = _apply_known_aa_agent_measurements(conn)
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def seed_scores(*, path: pathlib.Path | str | None = None) -> int:
    """Persist missing GRADE_TABLE seed rows without overwriting existing rows."""

    conn = connect(path)
    try:
        inserted = _seed_conn(conn)
        _apply_known_aa_agent_measurements(conn)
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _recompute_rank(conn: sqlite3.Connection, source: str, metric: str) -> None:
    rows = conn.execute(
        "SELECT model_id, effort, harness, score FROM model_scores "
        "WHERE source = ? AND metric = ? AND score IS NOT NULL "
        "ORDER BY score DESC, model_id, effort, harness",
        (source, metric),
    ).fetchall()
    previous: float | None = None
    previous_rank = 0
    for index, row in enumerate(rows, start=1):
        score = float(row["score"])
        if previous is None or score != previous:
            previous_rank = index
            previous = score
        conn.execute(
            "UPDATE model_scores SET rank = ? WHERE model_id = ? AND source = ? AND metric = ? "
            "AND effort IS ? AND harness IS ?",
            (
                previous_rank,
                row["model_id"],
                source,
                metric,
                row["effort"],
                row["harness"],
            ),
        )
    conn.execute(
        "UPDATE model_scores SET rank = NULL WHERE source = ? AND metric = ? AND score IS NULL",
        (source, metric),
    )


def _captured_at_key(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def upsert_scores(scores: Iterable[ModelScore], *, path: pathlib.Path | str | None = None) -> int:
    """Validate and atomically upsert scores, preserving source/metric keys."""

    checked = [_validate_score(score) for score in scores]
    if not checked:
        return 0
    conn = connect(path)
    try:
        for score in checked:
            _upsert(conn, score)
        conn.commit()
        return len(checked)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _dotenv_api_key(path: pathlib.Path | None = None) -> str | None:
    dotenv = path or DOTENV_PATH.expanduser()
    try:
        lines = dotenv.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("export "):
            entry = entry[7:].lstrip()
        name, separator, value = entry.partition("=")
        if separator and name.strip() == API_KEY_ENV:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                return value
    return None


def get_api_key() -> str | None:
    """Read the key without ever returning it to a log/output function."""

    value = os.environ.get(API_KEY_ENV)
    if value and value.strip():
        return value.strip()
    return _dotenv_api_key()


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _aa_scores(payload: object, *, captured_at: str) -> list[ModelScore]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise BenchError("invalid Artificial Analysis response: data must be an array")
    scores: list[ModelScore] = []
    fields = {
        "artificial_analysis_coding_index": "coding_index",
        "artificial_analysis_intelligence_index": "intelligence",
    }
    for item in payload["data"]:
        if not isinstance(item, dict):
            raise BenchError("invalid Artificial Analysis response: model row must be an object")
        model_id = item.get("slug") or item.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise BenchError("invalid Artificial Analysis response: model slug/id is missing")
        evaluations = item.get("evaluations")
        if not isinstance(evaluations, dict):
            raise BenchError("invalid Artificial Analysis response: evaluations is missing")
        for field, metric in fields.items():
            if field not in evaluations:
                continue
            if evaluations[field] is None:
                continue
            base_model_id, effort = parse_effort_suffix(model_id)
            scores.append(
                _validate_score(
                    ModelScore(
                        model_id=base_model_id,
                        effort=effort,
                        harness=None,
                        source="AA-model",
                        metric=metric,
                        score=_score(evaluations[field], allow_none=False),
                        rank=None,
                        captured_at=captured_at,
                    )
                )
            )
    return scores


def migrate_aa_model_effort_suffixes(*, path: pathlib.Path | str | None = None) -> int:
    """ROB-1190 ②-1 백필 — 기존 AA-model 행의 model_id 접미사를 effort 컬럼으로 분리한다.

    새 스키마(파싱된 base model_id + effort)로 삽입하고, 접미사가 붙은 옛 model_id 행은
    제거한다. 이미 파싱된 행(effort NOT NULL)이나 접미사 없는 model_id 는 그대로 둔다.
    idempotent: 이미 마이그레이션된 DB에서 재실행해도 0을 반환한다.
    """
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT model_id, effort, harness, source, metric, score, rank, captured_at "
            "FROM model_scores WHERE source = 'AA-model' AND effort IS NULL"
        ).fetchall()
        migrated = 0
        for row in rows:
            base_model_id, effort = parse_effort_suffix(row["model_id"])
            if effort is None:
                continue
            new_score = _validate_score(
                ModelScore(
                    model_id=base_model_id,
                    effort=effort,
                    harness=row["harness"],
                    source=row["source"],
                    metric=row["metric"],
                    score=row["score"],
                    rank=row["rank"],
                    captured_at=row["captured_at"],
                )
            )
            target = conn.execute(
                "SELECT captured_at FROM model_scores "
                "WHERE model_id = ? AND source = ? AND metric = ? AND effort IS ? AND harness IS ?",
                (base_model_id, row["source"], row["metric"], effort, row["harness"]),
            ).fetchone()
            if target is None or _captured_at_key(target["captured_at"]) < _captured_at_key(
                row["captured_at"]
            ):
                _upsert(conn, new_score)
            conn.execute(
                "DELETE FROM model_scores WHERE model_id = ? AND effort IS NULL AND harness IS ? "
                "AND source = ? AND metric = ?",
                (row["model_id"], row["harness"], row["source"], row["metric"]),
            )
            migrated += 1
        if migrated:
            for metric in {row["metric"] for row in rows}:
                _recompute_rank(conn, "AA-model", metric)
        conn.commit()
        return migrated
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sync_scores(
    *,
    api_key: str | None = None,
    path: pathlib.Path | str | None = None,
    request_fn: Callable[..., object] | None = None,
    captured_at: str | None = None,
) -> int:
    """Fetch official AA model data and upsert it as ``AA-model`` rows."""

    key = api_key or get_api_key()
    if not key:
        raise BenchError("API key is missing")
    fetch = request_fn or request_json
    timestamp = captured_at or _utc_now()
    payload = fetch(AA_API_URL, headers={"x-api-key": key})
    scores = _aa_scores(payload, captured_at=timestamp)
    conn = connect(path)
    try:
        _seed_conn(conn)
        _apply_known_aa_agent_measurements(conn)
        for score in scores:
            _upsert(conn, score)
        for metric in {score.metric for score in scores}:
            _recompute_rank(conn, "AA-model", metric)
        conn.commit()
        return len(scores)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_sync(*, stderr: TextIO) -> int:
    """CLI wrapper: missing credentials are a warning-only no-op."""

    if not get_api_key():
        print("warning: Artificial Analysis API key not found; bench sync skipped", file=stderr)
        return 0
    try:
        count = sync_scores()
    except HttpError as exc:
        print(f"warning: Artificial Analysis sync failed (HTTP {exc.status})", file=stderr)
        return 1
    except (BenchError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        # Do not print exception text: upstream bodies and parser errors are not part of
        # the CLI contract and must never provide a path for credential/header leakage.
        _ = exc
        print("warning: Artificial Analysis sync failed (invalid response or local DB error)", file=stderr)
        return 1
    print(f"bench sync: stored {count} score(s)")
    return 0


_IMPORT_DEFAULT_FIELDS = frozenset(
    {
        "source",
        "metric",
        "effort",
        "harness",
        "captured_at",
        "time_per_task_min",
        "cost_per_task_usd",
    }
)
_IMPORT_ROW_FIELDS = _IMPORT_DEFAULT_FIELDS | frozenset({"model_id", "score", "rank"})


def _import_rows(payload: object) -> list[ModelScore]:
    if not isinstance(payload, dict):
        raise BenchError("import TOML must contain a table")
    table_keys = [key for key in ("scores", "model_scores") if key in payload]
    if len(table_keys) != 1 or not isinstance(payload[table_keys[0]], list):
        raise BenchError("import TOML must contain exactly one [[scores]] or [[model_scores]] list")
    allowed_top = _IMPORT_DEFAULT_FIELDS | frozenset({"scores", "model_scores", "meta", "metadata"})
    unknown_top = set(payload) - allowed_top
    if unknown_top:
        raise BenchError(f"unsupported import field: {sorted(unknown_top)[0]}")

    defaults: dict[str, object] = {key: payload[key] for key in _IMPORT_DEFAULT_FIELDS if key in payload}
    for metadata_key in ("meta", "metadata"):
        metadata = payload.get(metadata_key)
        if metadata is None:
            continue
        if not isinstance(metadata, dict):
            raise BenchError(f"{metadata_key} must be a table")
        unknown_meta = set(metadata) - _IMPORT_DEFAULT_FIELDS
        if unknown_meta:
            raise BenchError(f"unsupported import field: {sorted(unknown_meta)[0]}")
        defaults.update(metadata)

    rows: list[ModelScore] = []
    for raw in payload[table_keys[0]]:
        if not isinstance(raw, dict):
            raise BenchError("each imported score must be a table")
        unknown_row = set(raw) - _IMPORT_ROW_FIELDS
        if unknown_row:
            raise BenchError(f"unsupported score field: {sorted(unknown_row)[0]}")
        merged = {**defaults, **raw}
        if "score" not in merged:
            raise BenchError("score is required for imported rows")
        score = _validate_score(
            ModelScore(
                model_id=_model_id(merged.get("model_id")),
                effort=_optional_text(merged.get("effort"), "effort"),
                harness=_optional_text(merged.get("harness"), "harness"),
                source=_text(merged.get("source"), "source") or "",
                metric=_text(merged.get("metric"), "metric") or "",
                score=_score(merged.get("score"), allow_none=False),
                rank=_rank(merged.get("rank")),
                captured_at=_captured_at(merged.get("captured_at")),
                time_per_task_min=_measurement(merged.get("time_per_task_min"), "time_per_task_min"),
                cost_per_task_usd=_measurement(merged.get("cost_per_task_usd"), "cost_per_task_usd"),
            ),
            allow_aa_model=False,
            allow_none_score=False,
        )
        rows.append(score)
    if not rows:
        raise BenchError("import TOML contains no scores")
    sources = {score.source for score in rows}
    if len(sources) != 1:
        raise BenchError("one import file must contain one source only")
    return rows


def import_scores(path: pathlib.Path | str, *, db: pathlib.Path | str | None = None) -> int:
    """Validate an import completely before changing the SQLite database."""

    source_path = pathlib.Path(path).expanduser()
    try:
        payload = tomllib.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BenchError("cannot read valid TOML import file") from exc
    rows = _import_rows(payload)
    conn = connect(db)
    try:
        _seed_conn(conn)
        for row in rows:
            _upsert(conn, row)
        _apply_known_aa_agent_measurements(conn)
        for source, metric in {(row.source, row.metric) for row in rows}:
            _recompute_rank(conn, source, metric)
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def coverage_report(*, path: pathlib.Path | str | None = None) -> str:
    """ROB-1190 ②-5 — 프로필별 출처 커버리지. "점수 없음" != "낮음"(경고를 함께 낸다).

    GRADE_TABLE 의 모든 프로필을 순회하며 AA-agent/AA-model/openrouter 각 출처에 대해
    (있음|없음) 을 표시한다. 표시 전용 경로이므로 DB를 seed하거나 변경하지 않는다.
    """
    from .recommend import GRADE_TABLE

    all_scores = read_scores(path=path)
    by_source_model: set[tuple[str, str]] = set()
    for score in all_scores:
        if score.score is None:
            continue
        lookup_id = (
            normalize_aa_model_id(score.model_id) if score.source == "AA-model" else score.model_id.lower()
        )
        by_source_model.add((score.source, lookup_id))

    lines = ["profile       AA-agent  AA-model  openrouter"]
    no_score_profiles: list[str] = []
    for profiles in GRADE_TABLE.values():
        for profile in profiles:
            agent_id = profile.aa_agent_model_id or (
                profile.benchmark_model_id if profile.benchmark_source == "AA-agent" else None
            )
            model_id = profile.aa_model_id or (
                profile.benchmark_model_id if profile.benchmark_source == "AA-model" else None
            )
            openrouter_id = profile.benchmark_model_id if profile.benchmark_source == "openrouter" else None

            has_agent = agent_id is not None and ("AA-agent", agent_id.lower()) in by_source_model
            has_model = (
                model_id is not None
                and (
                    "AA-model",
                    normalize_aa_model_id(model_id),
                )
                in by_source_model
            )
            has_openrouter = (
                openrouter_id is not None
                and (
                    "openrouter",
                    openrouter_id.lower(),
                )
                in by_source_model
            )

            def mark(has: bool, checked: bool) -> str:
                if not checked:
                    return "-"
                return "있음" if has else "없음"

            lines.append(
                f"{profile.name:<13} {mark(has_agent, agent_id is not None):<9} "
                f"{mark(has_model, model_id is not None):<9} "
                f"{mark(has_openrouter, openrouter_id is not None)}"
            )
            if not has_agent and not has_model and not has_openrouter:
                no_score_profiles.append(profile.name)

    if no_score_profiles:
        lines.append("")
        lines.append(
            "⚠ AA 미수록(점수 없음) — '낮음'으로 취급 금지, reps 로만 판정: " + ", ".join(no_score_profiles)
        )
    return "\n".join(lines)


def show_scores(model_id: str, *, path: pathlib.Path | str | None = None) -> str:
    """Render only source-separated rows for one normalized model id."""

    normalized = _model_id(model_id)
    scores = read_scores(normalized, path=path)
    lines = [f"model_id={normalized}"]
    if not scores:
        lines.append("없음/미측정")
        return "\n".join(lines)
    lines.append(
        "source      metric          effort  harness  score  rank  "
        "time_per_task_min  cost_per_task_usd  captured_at"
    )
    for score in scores:
        score_text = "없음/미측정" if score.score is None else f"{score.score:.1f}"
        time_text = "-" if score.time_per_task_min is None else f"{score.time_per_task_min:.1f}"
        cost_text = "-" if score.cost_per_task_usd is None else f"${score.cost_per_task_usd:g}"
        lines.append(
            f"{score.source:<11} {score.metric:<15} {display_effort(score.effort):<7} "
            f"{score.harness or '-':<8} {score_text:<6} {score.rank or '-':<5} "
            f"{time_text:<17} {cost_text:<18} {score.captured_at}"
        )
    return "\n".join(lines)


def delete_score_exact(
    *,
    model_id: str,
    effort: str,
    harness: str | None,
    source: str,
    metric: str,
    path: pathlib.Path | str | None = None,
) -> int:
    """Delete exactly one model_scores row by full key. Returns changes count.

    ROB-1191 ⑤: intended for temp/fixture DBs and post-merge orch handoff only.
    Never call against the real user DB from implementation tests.
    """
    normalized = _model_id(model_id)
    target = pathlib.Path(path) if path is not None else db_path()
    if str(target) != ":memory:" and not target.expanduser().exists():
        return 0
    conn = connect(target)
    try:
        cur = conn.execute(
            "DELETE FROM model_scores WHERE model_id = ? AND effort IS ? AND harness IS ? "
            "AND source = ? AND metric = ?",
            (normalized, effort, harness, source, metric),
        )
        conn.commit()
        return int(cur.rowcount)
    finally:
        conn.close()


def add_rep(
    *,
    profile: str,
    model_id: str,
    task_ref: str,
    tier: str,
    role: str,
    effort: str | None = None,
    grade: str | None = None,
    rounds: int,
    blockers_found: int,
    completed: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    notes: str | None = None,
    recorded_at: str | None = None,
    path: pathlib.Path | str | None = None,
) -> RepRecord:
    profile = _text(profile, "profile") or ""
    model_id = _text(model_id, "model") or ""
    task_ref = _text(task_ref, "task") or ""
    tier = _text(tier, "tier") or ""
    role = _text(role, "role") or ""
    effort = _rep_choice(effort, "effort", REP_EFFORTS)
    grade = _rep_choice(grade, "grade", REP_GRADES)
    if tier not in {"T0", "T1", "T2", "T3"}:
        raise BenchError("tier must be T0, T1, T2, or T3")
    if role not in {"impl", "verify", "fix", "orch"}:
        raise BenchError("role must be impl, verify, fix, or orch")
    for value, field in ((rounds, "rounds"), (blockers_found, "blockers_found")):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BenchError(f"{field} must be a non-negative integer")
    if isinstance(completed, bool) or completed not in (0, 1):
        raise BenchError("completed must be 0 or 1")
    for value, field in ((input_tokens, "input_tokens"), (output_tokens, "output_tokens")):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise BenchError(f"{field} must be a non-negative integer")
    notes = _optional_text(notes, "notes")
    recorded_at = _captured_at(recorded_at or _utc_now())

    conn = connect(path)
    try:
        cursor = conn.execute(
            "INSERT INTO reps "
            "(profile, model_id, task_ref, tier, role, rounds, blockers_found, completed, "
            "input_tokens, output_tokens, notes, recorded_at, effort, grade) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                profile,
                model_id,
                task_ref,
                tier,
                role,
                rounds,
                blockers_found,
                completed,
                input_tokens,
                output_tokens,
                notes,
                recorded_at,
                effort,
                grade,
            ),
        )
        conn.commit()
        rep_id = int(cursor.lastrowid)
        row = conn.execute(
            "SELECT id, profile, model_id, task_ref, tier, role, rounds, blockers_found, completed, "
            "input_tokens, output_tokens, notes, recorded_at, effort, grade "
            "FROM reps WHERE id = ?",
            (rep_id,),
        ).fetchone()
        assert row is not None
        return RepRecord(**{column: row[column] for column in _REP_COLUMNS})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_reps(
    *,
    path: pathlib.Path | str | None = None,
    limit: int | None = None,
    grade: str | None = None,
    profile: str | None = None,
    effort: str | None = None,
) -> list[RepRecord]:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise BenchError("limit must be a positive integer")
    grade = _rep_choice(grade, "grade", REP_GRADES)
    effort = _rep_choice(effort, "effort", REP_EFFORTS)
    profile = _optional_text(profile, "profile")
    target = pathlib.Path(path) if path is not None else db_path()
    if str(target) != ":memory:" and not target.expanduser().exists():
        return []
    conn = connect(target) if str(target) == ":memory:" else _readonly_connect(target)
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reps'"
        ).fetchone()
        if table_exists is None:
            return []
        available_columns = {row[1] for row in conn.execute("PRAGMA table_info(reps)").fetchall()}
        if (grade is not None and "grade" not in available_columns) or (
            effort is not None and "effort" not in available_columns
        ):
            return []
        select_columns = ", ".join(
            column if column in available_columns else f"NULL AS {column}" for column in _REP_COLUMNS
        )
        where: list[str] = []
        params_list: list[object] = []
        for column, value in (("grade", grade), ("profile", profile), ("effort", effort)):
            if value is not None:
                where.append(f"{column} = ?")
                params_list.append(value)
        query = f"SELECT {select_columns} FROM reps"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY id DESC"
        params: tuple[object, ...] = tuple(params_list)
        if limit is not None:
            query += " LIMIT ?"
            params = (*params_list, limit)
        rows = conn.execute(query, params).fetchall()
        return [RepRecord(**{column: row[column] for column in _REP_COLUMNS}) for row in rows]
    finally:
        conn.close()


def format_rep(rep: RepRecord) -> str:
    fields = [
        f"id={rep.id}",
        f"profile={rep.profile}",
        f"effort={rep.effort or '-'}",
        f"grade={rep.grade or '-'}",
        f"model={rep.model_id or '-'}",
        f"task={rep.task_ref or '-'}",
        f"tier={rep.tier or '-'}",
        f"role={rep.role or '-'}",
        f"rounds={rep.rounds if rep.rounds is not None else '-'}",
        f"blockers-found={rep.blockers_found if rep.blockers_found is not None else '-'}",
        f"completed={rep.completed if rep.completed is not None else '-'}",
        f"recorded_at={rep.recorded_at}",
    ]
    if rep.input_tokens is not None:
        fields.append(f"input-tokens={rep.input_tokens}")
    if rep.output_tokens is not None:
        fields.append(f"output-tokens={rep.output_tokens}")
    if rep.notes:
        fields.append(f"notes={rep.notes}")
    return " ".join(fields)


def _average(values: Iterable[int | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return sum(present) / len(present) if present else None


def compare_reps(
    *,
    grade: str,
    profile: str | None = None,
    effort: str | None = None,
    path: pathlib.Path | str | None = None,
) -> list[RepComparison]:
    """Compare representative runs by profile within one recorded grade."""

    grade = _rep_choice(grade, "grade", REP_GRADES)
    assert grade is not None
    rows = read_reps(path=path, grade=grade, profile=profile, effort=effort)
    by_profile: dict[str, list[RepRecord]] = {}
    for row in rows:
        by_profile.setdefault(row.profile, []).append(row)

    comparisons: list[RepComparison] = []
    for profile_name in sorted(by_profile):
        group = by_profile[profile_name]
        comparisons.append(
            RepComparison(
                profile=profile_name,
                count=len(group),
                average_rounds=_average(row.rounds for row in group),
                average_blockers_found=_average(row.blockers_found for row in group),
                completion_rate=(sum(row.completed == 1 for row in group) / len(group)) * 100.0,
                average_input_tokens=_average(row.input_tokens for row in group),
                average_output_tokens=_average(row.output_tokens for row in group),
            )
        )
    return comparisons


def format_rep_comparison(comparison: RepComparison) -> str:
    def render_average(value: float | None) -> str:
        return f"{value:.2f}" if value is not None else "-"

    fields = [
        f"profile={comparison.profile}",
        f"count={comparison.count}",
        f"avg-rounds={render_average(comparison.average_rounds)}",
        f"avg-blockers-found={render_average(comparison.average_blockers_found)}",
        f"completion-rate={comparison.completion_rate:.1f}%",
    ]
    if comparison.average_input_tokens is not None:
        fields.append(f"avg-input-tokens={render_average(comparison.average_input_tokens)}")
    if comparison.average_output_tokens is not None:
        fields.append(f"avg-output-tokens={render_average(comparison.average_output_tokens)}")
    return " ".join(fields)


def format_score_as_json(score: ModelScore) -> str:
    """Small test/CLI helper with no credential-bearing fields."""

    return json.dumps(score.as_dict(), ensure_ascii=False, sort_keys=True)
