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

_MODEL_SCORE_COLUMNS = (
    "model_id",
    "effort",
    "harness",
    "source",
    "metric",
    "score",
    "rank",
    "captured_at",
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
    "notes",
    "recorded_at",
)


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
    notes: str | None
    recorded_at: str

    def as_dict(self) -> dict[str, object]:
        return {column: getattr(self, column) for column in _REP_COLUMNS}


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
          notes          TEXT,
          recorded_at    TEXT NOT NULL
        );
        """
    )


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
    )


def _score_from_row(row: sqlite3.Row) -> ModelScore:
    return ModelScore(**{column: row[column] for column in _MODEL_SCORE_COLUMNS})


def read_scores(model_id: str | None = None, *, path: pathlib.Path | str | None = None) -> list[ModelScore]:
    """Read scores without creating a DB when the target does not exist."""

    target = pathlib.Path(path) if path is not None else db_path()
    if str(target) != ":memory:" and not target.expanduser().exists():
        return []
    normalized = _model_id(model_id) if model_id is not None else None
    conn = connect(target)
    try:
        if normalized is None:
            rows = conn.execute(
                "SELECT model_id, effort, harness, source, metric, score, rank, captured_at "
                "FROM model_scores ORDER BY source, metric, model_id, effort, harness"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT model_id, effort, harness, source, metric, score, rank, captured_at "
                "FROM model_scores WHERE model_id = ? "
                "ORDER BY source, metric, effort, harness",
                (normalized,),
            ).fetchall()
        return [_score_from_row(row) for row in rows]
    finally:
        conn.close()


def _upsert(conn: sqlite3.Connection, score: ModelScore) -> None:
    where = (
        "SELECT 1 FROM model_scores WHERE model_id = ? AND source = ? AND metric = ? "
        "AND effort IS ? AND harness IS ?"
    )
    key = (score.model_id, score.source, score.metric, score.effort, score.harness)
    found = conn.execute(where, key).fetchone()
    values = (
        score.model_id,
        score.effort,
        score.harness,
        score.source,
        score.metric,
        score.score,
        score.rank,
        score.captured_at,
    )
    if found:
        conn.execute(
            "UPDATE model_scores SET score = ?, rank = ?, captured_at = ? "
            "WHERE model_id = ? AND source = ? AND metric = ? AND effort IS ? AND harness IS ?",
            (score.score, score.rank, score.captured_at, *key),
        )
    else:
        conn.execute(
            "INSERT INTO model_scores (model_id, effort, harness, source, metric, score, rank, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
    conn.execute(
        "INSERT INTO model_scores (model_id, effort, harness, source, metric, score, rank, captured_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            score.model_id,
            score.effort,
            score.harness,
            score.source,
            score.metric,
            score.score,
            score.rank,
            score.captured_at,
        ),
    )
    return True


def _seed_scores() -> list[ModelScore]:
    """Build the approved GRADE_TABLE seed without importing it at module load time."""

    from .recommend import GRADE_TABLE

    unique: dict[tuple[object, ...], ModelScore] = {}
    for profiles in GRADE_TABLE.values():
        for profile in profiles:
            if profile.benchmark is None:
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


def seed_scores(*, path: pathlib.Path | str | None = None) -> int:
    """Persist missing GRADE_TABLE seed rows without overwriting existing rows."""

    conn = connect(path)
    try:
        inserted = _seed_conn(conn)
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
            scores.append(
                _validate_score(
                    ModelScore(
                        model_id=model_id,
                        effort=None,
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


_IMPORT_DEFAULT_FIELDS = frozenset({"source", "metric", "effort", "harness", "captured_at"})
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
        for source, metric in {(row.source, row.metric) for row in rows}:
            _recompute_rank(conn, source, metric)
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def show_scores(model_id: str, *, path: pathlib.Path | str | None = None) -> str:
    """Render only source-separated rows for one normalized model id."""

    normalized = _model_id(model_id)
    seed_scores(path=path)
    scores = read_scores(normalized, path=path)
    lines = [f"model_id={normalized}"]
    if not scores:
        lines.append("없음/미측정")
        return "\n".join(lines)
    lines.append("source      metric          effort  harness  score  rank  captured_at")
    for score in scores:
        score_text = "없음/미측정" if score.score is None else f"{score.score:.1f}"
        lines.append(
            f"{score.source:<11} {score.metric:<15} {score.effort or '-':<7} "
            f"{score.harness or '-':<8} {score_text:<6} {score.rank or '-':<5} {score.captured_at}"
        )
    return "\n".join(lines)


def add_rep(
    *,
    profile: str,
    model_id: str,
    task_ref: str,
    tier: str,
    role: str,
    rounds: int,
    blockers_found: int,
    completed: int,
    notes: str | None = None,
    recorded_at: str | None = None,
    path: pathlib.Path | str | None = None,
) -> RepRecord:
    profile = _text(profile, "profile") or ""
    model_id = _text(model_id, "model") or ""
    task_ref = _text(task_ref, "task") or ""
    tier = _text(tier, "tier") or ""
    role = _text(role, "role") or ""
    if tier not in {"T0", "T1", "T2", "T3"}:
        raise BenchError("tier must be T0, T1, T2, or T3")
    if role not in {"impl", "verify", "fix", "orch"}:
        raise BenchError("role must be impl, verify, fix, or orch")
    for value, field in ((rounds, "rounds"), (blockers_found, "blockers_found")):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BenchError(f"{field} must be a non-negative integer")
    if isinstance(completed, bool) or completed not in (0, 1):
        raise BenchError("completed must be 0 or 1")
    notes = _optional_text(notes, "notes")
    recorded_at = _captured_at(recorded_at or _utc_now())

    conn = connect(path)
    try:
        cursor = conn.execute(
            "INSERT INTO reps "
            "(profile, model_id, task_ref, tier, role, rounds, blockers_found, completed, notes, "
            "recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (profile, model_id, task_ref, tier, role, rounds, blockers_found, completed, notes, recorded_at),
        )
        conn.commit()
        rep_id = int(cursor.lastrowid)
        row = conn.execute(
            "SELECT id, profile, model_id, task_ref, tier, role, rounds, blockers_found, completed, notes, "
            "recorded_at "
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


def read_reps(*, path: pathlib.Path | str | None = None, limit: int | None = None) -> list[RepRecord]:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise BenchError("limit must be a positive integer")
    target = pathlib.Path(path) if path is not None else db_path()
    if str(target) != ":memory:" and not target.expanduser().exists():
        return []
    conn = connect(target)
    try:
        query = (
            "SELECT id, profile, model_id, task_ref, tier, role, rounds, blockers_found, completed, notes, "
            "recorded_at "
            "FROM reps ORDER BY id DESC"
        )
        params: tuple[object, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        rows = conn.execute(query, params).fetchall()
        return [RepRecord(**{column: row[column] for column in _REP_COLUMNS}) for row in rows]
    finally:
        conn.close()


def format_rep(rep: RepRecord) -> str:
    fields = [
        f"id={rep.id}",
        f"profile={rep.profile}",
        f"model={rep.model_id or '-'}",
        f"task={rep.task_ref or '-'}",
        f"tier={rep.tier or '-'}",
        f"role={rep.role or '-'}",
        f"rounds={rep.rounds if rep.rounds is not None else '-'}",
        f"blockers-found={rep.blockers_found if rep.blockers_found is not None else '-'}",
        f"completed={rep.completed if rep.completed is not None else '-'}",
        f"recorded_at={rep.recorded_at}",
    ]
    if rep.notes:
        fields.append(f"notes={rep.notes}")
    return " ".join(fields)


def format_score_as_json(score: ModelScore) -> str:
    """Small test/CLI helper with no credential-bearing fields."""

    return json.dumps(score.as_dict(), ensure_ascii=False, sort_keys=True)
