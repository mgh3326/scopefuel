"""SQLite-backed benchmark scores and manual representative-run records.

Benchmark values are kept separate by ``source`` and ``metric``.  The module
does not rank or compare values across those dimensions; it only stores the
rank supplied by a source or computes a rank within one source/metric pair.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import socket
import sqlite3
import sys
import tomllib
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import TextIO

from .http import HttpError, request_json
from .policy import load_config

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

BENCH_BACKEND_LOCAL = "local"
BENCH_BACKEND_HANDOFFKEEP = "handoffkeep"
BENCH_BACKEND_AUTO = "auto"
DEFAULT_CACHE_TTL_S = 6 * 60 * 60
# Catalog freshness. Below ``DEFAULT_CATALOG_TTL_S`` the cache is served without
# a request; past it the server is re-read.  Only past ``DEFAULT_CATALOG_STALE_MAX_S``
# does an unreachable server demote the client to the bundled snapshot, and that
# demotion is always labelled ``stale`` (2558: a down server is not free rein).
DEFAULT_CATALOG_TTL_S = 60 * 60
DEFAULT_CATALOG_STALE_MAX_S = 24 * 60 * 60
# Tolerance for a cache stamp ahead of the local clock before it is treated as
# corrupt rather than fresh. NTP steps and container clock drift are seconds.
_CACHE_CLOCK_SKEW_S = 60.0
# CWE-319: the bearer token must never leave the process over plaintext HTTP,
# except to a local test/dev server where "plaintext" never leaves the host.
_HANDOFFKEEP_PLAINTEXT_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_BENCH_SCOPES = frozenset({"scores", "reps", "grades", "catalog"})
CATALOG_GATES = ("default", "escalation", "consult_only")
# "" is the profile-default row and sorts first; the named rungs are ordered.
CATALOG_EFFORT_RANKS: dict[str, int] = {"": 0, "low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}
_WARNED_UNKNOWN_BACKENDS: set[str] = set()
_WARNED_DEPRECATED_KEYS: set[str] = set()

# task #697 — the plaintext-http opt-in is per use, not global: a host may
# share quota snapshots and rep records over a private-tunnel http endpoint
# without letting the catalog leave local mode (the #667 failure mode — a
# 1-row server catalog made wrk briefs go unsent — cannot recur from the reps
# or quota flags). ``catalog`` covers the canonical bench tables — the catalog
# route, its legacy ``grades`` projection, and the ``scores`` canon — so every
# surface that decides placements and rankings moves together.
PLAINTEXT_USES = ("catalog", "quota_share", "reps")
# The deprecated single flag means "all uses", exactly its old behavior.
_PLAINTEXT_ALIAS_KEY = "allow_plaintext_url"
# Each bench wire scope belongs to exactly one use (task #697): the canonical
# tables are the catalog use; only rep records are the reps use.
_SCOPE_USE = {"catalog": "catalog", "grades": "catalog", "scores": "catalog", "reps": "reps"}


def plaintext_opt_in_key(use: str) -> str:
    """The ``[bench]`` config key that opts one use into plaintext http."""

    if use not in PLAINTEXT_USES:
        raise BenchBackendError(f"unknown plaintext use: {use}")
    return f"allow_plaintext_{use}"


def plaintext_opt_in(bench_config: dict, use: str, *, stderr: TextIO | None = None) -> bool:
    """Whether one use may carry the bearer token over plaintext http.

    A per-use ``allow_plaintext_<use>`` key wins when present;
    ``allow_plaintext_url`` is a deprecated alias that opts in every use at
    once (its old behavior) and prints one warning per process so the operator
    knows the flag's reach.
    """

    key = plaintext_opt_in_key(use)
    if bench_config.get(_PLAINTEXT_ALIAS_KEY) is True and _PLAINTEXT_ALIAS_KEY not in _WARNED_DEPRECATED_KEYS:
        print(
            f"warning: [bench] {_PLAINTEXT_ALIAS_KEY} is deprecated and enables plaintext http for "
            "all uses; prefer " + " / ".join(f"allow_plaintext_{name}" for name in PLAINTEXT_USES),
            file=stderr or sys.stderr,
        )
        _WARNED_DEPRECATED_KEYS.add(_PLAINTEXT_ALIAS_KEY)
    if key in bench_config:
        # A present key always wins over the alias; a non-bool value fails
        # closed rather than widening the opt-in it was meant to narrow.
        return bench_config.get(key) is True
    return bench_config.get(_PLAINTEXT_ALIAS_KEY) is True


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
    "table_grade",
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


class BenchBackendError(BenchError):
    """A handoffkeep operation failed without changing the local cache."""


class BenchRouteMissing(BenchBackendError):
    """The deployment does not serve this route (404), as opposed to being down.

    During the catalog rollout these are genuinely different states: production
    handoffkeep predates ``/v1/bench/catalog``, so a 404 means "no canon exists
    here yet, keep using the grades projection" — labelling that ``stale`` would
    stamp every spawn brief with a warning about a server that is working fine.
    """


@dataclass(frozen=True)
class BenchBackend:
    """Resolved benchmark storage settings.

    ``endpoint_id`` intentionally fingerprints, rather than retains, the URL so
    a copied cache cannot leak values from one endpoint into another one.
    """

    name: str
    cache_ttl_s: float
    url: str | None
    token: str | None
    endpoint_id: str
    # The catalog has its own lifetimes: it is the launch/placement canon and is
    # re-read hourly, while scores/reps/grades keep the slower ``cache_ttl_s``.
    catalog_ttl_s: float = DEFAULT_CATALOG_TTL_S
    catalog_stale_max_s: float = DEFAULT_CATALOG_STALE_MAX_S
    # Why this mode was selected ("configured" | "auto-credentials" | "auto-local"
    # | "auto-local-insecure-url"), so ``bench catalog status`` can explain a
    # host's mode without the reader having to guess.
    reason: str = "configured"
    # Whether this resolved backend may send the bearer token over plaintext
    # http — the resolved per-use opt-in (task #697), not the raw config key.
    allow_plaintext_url: bool = False
    # The use this backend was resolved for (task #697). ``_backend_url``
    # refuses a scope that maps to a different use, so a backend resolved under
    # one opt-in can never serve another use even if a caller mixes them.
    plaintext_use: str = "catalog"


@dataclass(frozen=True)
class GradeAssignment:
    """A server-owned placement of a profile in the grade table."""

    profile: str
    grade: str
    boundary_version: str | None
    deviation_ref: str
    decided_at: str | None = None
    decided_by: str | None = None


@dataclass(frozen=True)
class _RemoteRep:
    """A cached server rep plus the client-local id used for idempotency."""

    record: RepRecord
    origin_id: int
    created_by: str | None = None
    server_id: int | None = None


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
    provenance: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {column: getattr(self, column) for column in _MODEL_SCORE_COLUMNS}


@dataclass(frozen=True)
class ModelPrice:
    model_id: str
    price_1m_blended_3_to_1: float
    price_1m_input_tokens: float | None
    price_1m_output_tokens: float | None
    captured_at: str


# operator decision 2026-09-09 doc1144. These are the only approved static
# AA-model price seeds; a synchronized DB row takes precedence at read time.
AA_MODEL_PRICE_SEEDS: tuple[ModelPrice, ...] = (
    ModelPrice("kimi-k2-7-code", 1.7125, 0.95, 4.0, "2026-09-09T00:00:00+00:00"),
    ModelPrice("grok-4-6", 3.0, 2.0, 6.0, "2026-09-09T00:00:00+00:00"),
)


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
    table_grade: str | None = None
    # Store-namespaced id for display (``srv:<pk>`` | ``origin:<key>`` |
    # ``local:<rowid>``), set by the reader that knows which store the id
    # belongs to. Display-only — never persisted and never part of the rep key.
    ref: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {column: getattr(self, column) for column in _REP_COLUMNS}


def rep_ref(rep: RepRecord) -> str:
    """The rep's id with its store namespace — never a bare ambiguous number.

    ``srv:<id>`` is the server primary key; ``origin:<id>`` is the client-side
    idempotency key of a rep whose server copy exists but whose server pk was
    never learned locally (a write-through echo, or a cache row predating the
    refresh pass); ``local:<id>`` is a rowid of the local ``reps`` table, the
    same namespace ``grades`` evidence and ``reps backfill`` refs already use.
    """

    if rep.ref:
        return rep.ref
    return f"local:{rep.id}"


@dataclass(frozen=True)
class RepComparison:
    profile: str
    count: int
    average_rounds: float | None
    average_blockers_found: float | None
    completion_rate: float
    average_input_tokens: float | None
    average_output_tokens: float | None
    upward_count: int = 0
    same_count: int = 0
    downward_count: int = 0


def bench_backend(
    *, use: str, stderr: TextIO | None = None, allow_plaintext_http: bool = False
) -> BenchBackend:
    """Resolve the configured backend for one use, without touching the database.

    ``use`` is one of ``PLAINTEXT_USES`` and selects which per-use plaintext
    opt-in applies to the ``auto`` resolution and the resolved backend
    (task #697). A typo must never make normal local commands unavailable, so
    unknown values deliberately degrade to ``local`` with one actionable
    warning.

    ``allow_plaintext_http`` is a per-call, non-persistent opt-in on top of the
    configured one (task #714): ``reps migrate`` passes it so a one-time move
    can run before the operator decides whether to set the persistent
    ``allow_plaintext_<use>`` config key.
    """

    config = load_config()
    raw_bench = config.get("bench") if isinstance(config, dict) else None
    bench_config = raw_bench if isinstance(raw_bench, dict) else {}
    raw_name = bench_config.get("backend", BENCH_BACKEND_AUTO)
    name = raw_name.strip().lower() if isinstance(raw_name, str) else ""
    if name not in {BENCH_BACKEND_LOCAL, BENCH_BACKEND_HANDOFFKEEP, BENCH_BACKEND_AUTO}:
        warning_key = repr(raw_name)
        if warning_key not in _WARNED_UNKNOWN_BACKENDS:
            print("warning: unknown bench backend; using local", file=stderr or sys.stderr)
            _WARNED_UNKNOWN_BACKENDS.add(warning_key)
        name = BENCH_BACKEND_LOCAL

    ttl = _config_seconds(bench_config.get("cache_ttl_s"), DEFAULT_CACHE_TTL_S)
    catalog_ttl = _config_seconds(bench_config.get("catalog_ttl_s"), DEFAULT_CATALOG_TTL_S)
    catalog_stale_max = _config_seconds(bench_config.get("catalog_stale_max_s"), DEFAULT_CATALOG_STALE_MAX_S)

    # Do not normalize before hashing: the configured URL itself identifies the
    # endpoint, while only its non-reversible digest is kept in SQLite.
    url, token = _handoffkeep_credentials()

    # #593: ``auto`` is the default so a host that already holds handoffkeep
    # credentials reads the canonical catalog without a per-host config edit.
    # The failure mode this avoids is the quiet one — one machine left in local
    # mode keeps dispatching from its bundled table and nothing says so. A host
    # with no credentials is exactly as local as before, and an explicit
    # ``backend = "local"`` still pins local.
    allow_plaintext = plaintext_opt_in(bench_config, use, stderr=stderr) or allow_plaintext_http
    if name == BENCH_BACKEND_AUTO:
        if not (url and token):
            name, reason = BENCH_BACKEND_LOCAL, "auto-local"
        elif not _plaintext_allowed(url, allow_plaintext=allow_plaintext):
            # Credentials exist but the endpoint would carry the bearer token in
            # the clear.  Auto-enabling would turn every request into an error;
            # staying local keeps the host working and ``bench catalog status``
            # names the blocker instead of hiding it behind a request failure.
            name, reason = BENCH_BACKEND_LOCAL, "auto-local-insecure-url"
        else:
            name, reason = BENCH_BACKEND_HANDOFFKEEP, "auto-credentials"
    else:
        reason = "configured"

    if name == BENCH_BACKEND_LOCAL:
        return BenchBackend(
            name=name,
            cache_ttl_s=ttl,
            url=None,
            token=None,
            endpoint_id="",
            catalog_ttl_s=catalog_ttl,
            catalog_stale_max_s=catalog_stale_max,
            reason=reason,
            allow_plaintext_url=allow_plaintext,
            plaintext_use=use,
        )

    endpoint_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16] if url else ""
    return BenchBackend(
        name=name,
        cache_ttl_s=ttl,
        url=url,
        token=token,
        endpoint_id=endpoint_id,
        catalog_ttl_s=catalog_ttl,
        catalog_stale_max_s=catalog_stale_max,
        reason=reason,
        allow_plaintext_url=allow_plaintext,
        plaintext_use=use,
    )


def _config_seconds(raw: object, default: float) -> float:
    """Read a non-negative finite second count, falling back to ``default``."""

    if raw is None:
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value) or value < 0:
        return float(default)
    return value


def db_path() -> pathlib.Path:
    """Return the persistent data path, never the cache path."""

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    base = (
        pathlib.Path(os.path.expanduser(xdg_data_home))
        if xdg_data_home
        else pathlib.Path.home() / ".local" / "share"
    )
    return base / "scopefuel" / "bench.db"


def derive_table_grade(profile: str, effort: str | None = None) -> str | None:
    """Derive the profile's table grade from recommend.GRADE_TABLE without operator input.

    Returns the grade ("S+", "S", "A+", "A", "B", "C") if found in the grade table,
    or None if the profile is not in the grade table.
    """
    if not profile:
        return None
    from .recommend import GRADE_TABLE, PROFILE_ALIASES

    canonical = PROFILE_ALIASES.get(profile, profile)
    if effort:
        for grade, profiles in GRADE_TABLE.items():
            for p in profiles:
                if p.name == canonical and (p.launcher_effort == effort or p.benchmark_effort == effort):
                    return grade
    for grade, profiles in GRADE_TABLE.items():
        for p in profiles:
            if p.name == canonical:
                return grade
    return None


def _backfill_table_grade(conn: sqlite3.Connection) -> None:
    """Backfill table_grade for existing reps rows where grade is present but table_grade is NULL."""
    rows = conn.execute(
        "SELECT id, profile, effort FROM reps WHERE grade IS NOT NULL AND table_grade IS NULL"
    ).fetchall()
    for row in rows:
        derived = derive_table_grade(row["profile"], row["effort"])
        if derived is not None:
            conn.execute("UPDATE reps SET table_grade = ? WHERE id = ?", (derived, row["id"]))


_CACHE_SCHEMA = """
        CREATE TABLE IF NOT EXISTS bench_cache_meta (
          scope       TEXT PRIMARY KEY CHECK (scope IN ('scores', 'reps', 'grades', 'catalog')),
          fetched_at  TEXT NOT NULL,
          endpoint_id TEXT NOT NULL DEFAULT ''
        );

        -- Remote score rows are separate from the historical local source
        -- table.  This prevents a canonical response from rewriting a user's
        -- pre-migration rows while still using the same SQLite file as cache.
        CREATE TABLE IF NOT EXISTS bench_cache_scores (
          model_id          TEXT NOT NULL,
          effort            TEXT NOT NULL DEFAULT '',
          harness           TEXT NOT NULL DEFAULT '',
          source            TEXT NOT NULL,
          metric            TEXT NOT NULL,
          score             REAL,
          rank              INTEGER,
          captured_at       TEXT NOT NULL,
          time_per_task_min REAL,
          cost_per_task_usd REAL,
          provenance        TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (model_id, effort, harness, source, metric)
        );

        -- A remote rep id is not a local SQLite rowid.  Keeping cache entries
        -- apart makes that distinction durable and prevents accidental id
        -- replacement during a GET.
        CREATE TABLE IF NOT EXISTS bench_cache_reps (
          cache_key      TEXT PRIMARY KEY,
          server_id      INTEGER,
          origin_id      INTEGER NOT NULL,
          created_by     TEXT,
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
          grade          TEXT,
          table_grade    TEXT
        );

        CREATE TABLE IF NOT EXISTS bench_cache_grades (
          profile          TEXT PRIMARY KEY,
          grade            TEXT NOT NULL,
          boundary_version TEXT,
          deviation_ref    TEXT NOT NULL,
          decided_at       TEXT,
          decided_by       TEXT
        );

        -- #593: the canonical (profile, effort) launch/placement catalog.
        -- bench_cache_grades above stays as the pre-catalog projection so a
        -- client talking to a pre-#592 server keeps working unchanged.
        CREATE TABLE IF NOT EXISTS bench_cache_catalog (
          profile              TEXT NOT NULL,
          effort               TEXT NOT NULL DEFAULT '',
          model_id             TEXT NOT NULL DEFAULT '',
          pool                 TEXT NOT NULL DEFAULT '',
          grade                TEXT NOT NULL,
          score                REAL,
          gate                 TEXT NOT NULL DEFAULT 'default',
          gate_reason          TEXT,
          benchmark_source     TEXT,
          benchmark_annotation TEXT,
          boundary_version     TEXT,
          deviation_ref        TEXT NOT NULL DEFAULT '',
          decided_at           TEXT,
          decided_by           TEXT,
          retired_at           TEXT,
          PRIMARY KEY (profile, effort)
        );
"""


def _migrate_cache_meta_scopes(conn: sqlite3.Connection) -> None:
    """Widen ``bench_cache_meta``'s scope CHECK to admit ``catalog``.

    ``CREATE TABLE IF NOT EXISTS`` never revises an existing table's
    constraints, so a database created before #593 still carries
    ``CHECK (scope IN ('scores','reps','grades'))`` and rejects every catalog
    stamp. Only an already-created, already-narrow table is rebuilt; the rows
    are carried over, so the rebuild is idempotent and loses no cache age.
    """

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'bench_cache_meta'"
    ).fetchone()
    if row is None:
        return
    existing_sql = row[0] or ""
    if "catalog" in existing_sql:
        return
    conn.executescript(
        """
        CREATE TABLE bench_cache_meta_new (
          scope       TEXT PRIMARY KEY CHECK (scope IN ('scores', 'reps', 'grades', 'catalog')),
          fetched_at  TEXT NOT NULL,
          endpoint_id TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO bench_cache_meta_new(scope, fetched_at, endpoint_id)
          SELECT scope, fetched_at, endpoint_id FROM bench_cache_meta;
        DROP TABLE bench_cache_meta;
        ALTER TABLE bench_cache_meta_new RENAME TO bench_cache_meta;
        """
    )


def _cache_schema(conn: sqlite3.Connection) -> None:
    _migrate_cache_meta_scopes(conn)
    conn.executescript(_CACHE_SCHEMA)
    existing_score_cache_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(bench_cache_scores)").fetchall()
    }
    if "provenance" not in existing_score_cache_columns:
        conn.execute("ALTER TABLE bench_cache_scores ADD COLUMN provenance TEXT NOT NULL DEFAULT ''")


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

        CREATE TABLE IF NOT EXISTS model_prices (
          model_id TEXT PRIMARY KEY,
          price_1m_blended_3_to_1 REAL,
          price_1m_input_tokens REAL,
          price_1m_output_tokens REAL,
          captured_at TEXT NOT NULL
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
          recorded_at    TEXT NOT NULL,
          effort         TEXT,
          grade          TEXT,
          table_grade    TEXT
        );

        CREATE TABLE IF NOT EXISTS rep_grade_annotations (
          rep_ref      TEXT PRIMARY KEY,
          grade        TEXT NOT NULL,
          task_ref     TEXT,
          source       TEXT,
          recorded_at  TEXT NOT NULL
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
    for column in ("effort", "grade", "table_grade"):
        if column not in existing_rep_columns:
            conn.execute(f"ALTER TABLE reps ADD COLUMN {column} TEXT")
    _backfill_table_grade(conn)


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


def _cache_connect(path: pathlib.Path | str | None = None) -> sqlite3.Connection:
    """Open only remote-cache tables, leaving local source rows untouched."""

    target = pathlib.Path(path) if path is not None else db_path()
    if str(target) != ":memory:":
        target = pathlib.Path(os.path.expanduser(str(target)))
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    _cache_schema(conn)
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
        provenance=_optional_text(value.provenance, "provenance"),
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


def _read_local_scores(
    model_id: str | None = None, *, path: pathlib.Path | str | None = None
) -> list[ModelScore]:
    """Read the historical local source table without creating a database."""

    target = pathlib.Path(path) if path is not None else db_path()
    if str(target) == ":memory:" or not target.expanduser().exists():
        return []
    normalized = _model_id(model_id) if model_id is not None else None
    conn = _readonly_connect(target)
    try:
        # The file may exist with only bench_cache_* tables — a per-use opt-in
        # mix (task #697) lets a reps/quota write create the cache while the
        # source tables were never created.
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'model_scores'"
            ).fetchone()
            is None
        ):
            return []
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


def _cache_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _cache_timestamp(now: dt.datetime) -> str:
    return now.astimezone(dt.UTC).isoformat()


def _cached_at(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _cache_state(
    conn: sqlite3.Connection, scope: str, backend: BenchBackend, now: dt.datetime
) -> tuple[bool, float | None]:
    """Return whether a scope may be served from this endpoint's cache."""

    if scope not in _BENCH_SCOPES or not backend.url:
        return False, None
    row = conn.execute(
        "SELECT fetched_at, endpoint_id FROM bench_cache_meta WHERE scope = ?", (scope,)
    ).fetchone()
    if row is None or row["endpoint_id"] != backend.endpoint_id:
        return False, None
    fetched_at = _cached_at(row["fetched_at"])
    if fetched_at is None:
        return False, None
    age_s = max(0.0, (now - fetched_at).total_seconds())
    return age_s < backend.cache_ttl_s, age_s / 3600.0


def _stamp_cache(conn: sqlite3.Connection, scope: str, backend: BenchBackend, now: dt.datetime) -> None:
    conn.execute(
        "INSERT INTO bench_cache_meta(scope, fetched_at, endpoint_id) VALUES (?, ?, ?) "
        "ON CONFLICT(scope) DO UPDATE SET fetched_at = excluded.fetched_at, "
        "endpoint_id = excluded.endpoint_id",
        (scope, _cache_timestamp(now), backend.endpoint_id),
    )


def _plaintext_allowed(url: str, *, allow_plaintext: bool) -> bool:
    """Whether a request URL may carry the bearer token (CWE-319).

    ``https://`` is always allowed. ``http://`` is allowed only to
    localhost/127.0.0.1/::1 (test and local-dev servers, where the request never
    reaches a network), or — when the operator has explicitly opted in for this
    use with ``[bench] allow_plaintext_<use> = true`` — to any host. The opt-in
    exists for a handoffkeep endpoint reached over a WireGuard tunnel (a
    Tailscale 100.64.0.0/10 address), where the transport is already encrypted
    end to end; it is off by default and never enabled by auto-detection,
    because "the operator says this link is private" is a claim only the
    operator can make.
    """

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http":
        return False
    if parsed.hostname in _HANDOFFKEEP_PLAINTEXT_HOSTS:
        return True
    return allow_plaintext


def _check_handoffkeep_scheme(url: str, *, allow_plaintext: bool = False, use: str = "catalog") -> None:
    """Refuse to build a request URL that would send the bearer token in the clear."""

    if _plaintext_allowed(url, allow_plaintext=allow_plaintext):
        return
    raise BenchBackendError(
        "HANDOFFKEEP_URL must use https (http allowed only to localhost, or to a "
        f"private tunnel with [bench] {plaintext_opt_in_key(use)} = true)"
    )


def handoffkeep_dotenv_path() -> pathlib.Path:
    """Where the handoffkeep CLI keeps its endpoint credentials."""

    override = os.environ.get("HANDOFFKEEP_CONFIG")
    if override:
        return pathlib.Path(os.path.expanduser(override))
    base = os.environ.get("XDG_CONFIG_HOME") or (pathlib.Path.home() / ".config")
    return pathlib.Path(base) / "handoffkeep" / "config.env"


def _handoffkeep_credentials() -> tuple[str | None, str | None]:
    """Resolve the handoffkeep endpoint, environment first, then the CLI's config.

    #593: the credentials that make a host server-canonical already exist on
    every host that runs ``handoffkeep`` — but in ``config.env``, not in the
    process environment. Reading the same file is what lets a host switch to the
    canonical catalog with no per-host scopefuel edit. The environment still
    wins, so a shell can point one command at a different endpoint.
    """

    env_url = os.environ.get("HANDOFFKEEP_URL")
    env_token = os.environ.get("HANDOFFKEEP_TOKEN")
    if env_url or env_token:
        # An environment override is all-or-nothing. Completing a half-set pair
        # from config.env would send the stored bearer token to whatever host the
        # environment named — setting one variable would be enough to redirect
        # the credential (CWE-522). An incomplete pair resolves to local instead,
        # and ``bench catalog status`` says which half is missing.
        return env_url, env_token
    url: str | None = None
    token: str | None = None
    try:
        raw = handoffkeep_dotenv_path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return url, token
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if not value:
            continue
        if key.strip() == "HANDOFFKEEP_URL" and not url:
            url = value
        elif key.strip() == "HANDOFFKEEP_TOKEN" and not token:
            token = value
    return url, token


def _backend_url(backend: BenchBackend, scope: str) -> str:
    if scope not in _BENCH_SCOPES:
        raise BenchBackendError("invalid bench cache scope")
    if not backend.url or not backend.token:
        raise BenchBackendError("handoffkeep URL and token are required")
    if _SCOPE_USE[scope] != backend.plaintext_use:
        raise BenchBackendError(
            f"bench backend resolved for use '{backend.plaintext_use}' cannot serve scope '{scope}'"
        )
    _check_handoffkeep_scheme(backend.url, allow_plaintext=backend.allow_plaintext_url, use=_SCOPE_USE[scope])
    return f"{backend.url.rstrip('/')}/v1/bench/{scope}"


def _handoffkeep_request(
    backend: BenchBackend,
    scope: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    query: dict[str, object] | None = None,
) -> dict:
    """Make one authenticated bench request without exposing response bodies."""

    url = _backend_url(backend, scope)
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    headers = {"Authorization": f"Bearer {backend.token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        payload = request_json(
            url,
            method=method,
            headers=headers,
            body=body,
            timeout=20.0,
        )
    except HttpError as exc:
        if exc.status == 404:
            raise BenchRouteMissing(f"handoffkeep has no /v1/bench/{scope} route") from exc
        raise BenchBackendError("handoffkeep request failed") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise BenchBackendError("handoffkeep request failed") from exc
    if not isinstance(payload, dict):
        raise BenchBackendError("handoffkeep returned an invalid response")
    return payload


def _warn_cached(scope: str, *, age_hours: float | None, has_data: bool) -> None:
    label = f"bench {scope}"
    if not has_data:
        suffix = "no cached data"
    elif age_hours is None:
        suffix = "age unknown"
    else:
        suffix = f"age {age_hours:.1f}h"
    print(f"warning: handoffkeep unreachable; using cached {label} ({suffix})", file=sys.stderr)


def _score_from_wire(value: object) -> ModelScore:
    if not isinstance(value, dict):
        raise BenchError("invalid handoffkeep score row")
    source = _text(value.get("source"), "source")
    metric = _text(value.get("metric"), "metric")
    assert source is not None and metric is not None
    return _validate_score(
        ModelScore(
            model_id=_model_id(value.get("model_id")),
            effort=_optional_text(value.get("effort"), "effort"),
            harness=_optional_text(value.get("harness"), "harness"),
            source=source,
            metric=metric,
            score=_score(value.get("score"), allow_none=True),
            rank=_rank(value.get("rank")),
            captured_at=_captured_at(value.get("captured_at")),
            time_per_task_min=_measurement(value.get("time_per_task_min"), "time_per_task_min"),
            cost_per_task_usd=_measurement(value.get("cost_per_task_usd"), "cost_per_task_usd"),
            provenance=_optional_text(value.get("provenance"), "provenance"),
        ),
        allow_none_score=True,
    )


def _scores_from_payload(payload: dict) -> list[ModelScore]:
    values = payload.get("scores")
    if not isinstance(values, list):
        raise BenchBackendError("handoffkeep returned invalid score data")
    try:
        return [_score_from_wire(value) for value in values]
    except BenchError as exc:
        raise BenchBackendError("handoffkeep returned invalid score data") from exc


def _fetch_scores(backend: BenchBackend) -> list[ModelScore]:
    return _scores_from_payload(_handoffkeep_request(backend, "scores"))


def _score_to_wire(score: ModelScore) -> dict[str, object]:
    """Map client ``None`` identity fields to the contract's empty strings."""

    return {
        "model_id": score.model_id,
        "effort": score.effort or "",
        "harness": score.harness or "",
        "source": score.source,
        "metric": score.metric,
        "score": score.score,
        "rank": score.rank,
        "captured_at": score.captured_at,
        "time_per_task_min": score.time_per_task_min,
        "cost_per_task_usd": score.cost_per_task_usd,
        "provenance": score.provenance or "",
    }


def _cached_scores(conn: sqlite3.Connection) -> list[ModelScore]:
    rows = conn.execute(
        "SELECT model_id, effort, harness, source, metric, score, rank, captured_at, "
        "time_per_task_min, cost_per_task_usd, provenance FROM bench_cache_scores "
        "ORDER BY source, metric, model_id, effort, harness"
    ).fetchall()
    return [
        ModelScore(
            model_id=row["model_id"],
            effort=row["effort"] or None,
            harness=row["harness"] or None,
            source=row["source"],
            metric=row["metric"],
            score=row["score"],
            rank=row["rank"],
            captured_at=row["captured_at"],
            time_per_task_min=row["time_per_task_min"],
            cost_per_task_usd=row["cost_per_task_usd"],
            provenance=row["provenance"] or None,
        )
        for row in rows
    ]


def _put_cached_score(conn: sqlite3.Connection, score: ModelScore) -> None:
    conn.execute(
        "INSERT INTO bench_cache_scores "
        "(model_id, effort, harness, source, metric, score, rank, captured_at, "
        "time_per_task_min, cost_per_task_usd, provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(model_id, effort, harness, source, metric) DO UPDATE SET "
        "score = excluded.score, rank = excluded.rank, captured_at = excluded.captured_at, "
        "time_per_task_min = excluded.time_per_task_min, "
        "cost_per_task_usd = excluded.cost_per_task_usd, "
        "provenance = excluded.provenance",
        (
            score.model_id,
            score.effort or "",
            score.harness or "",
            score.source,
            score.metric,
            score.score,
            score.rank,
            score.captured_at,
            score.time_per_task_min,
            score.cost_per_task_usd,
            score.provenance or "",
        ),
    )


def _replace_cached_scores(
    conn: sqlite3.Connection, scores: list[ModelScore], backend: BenchBackend, now: dt.datetime
) -> None:
    conn.execute("DELETE FROM bench_cache_scores")
    for score in scores:
        _put_cached_score(conn, score)
    _stamp_cache(conn, "scores", backend, now)


def _commit_score_cache(
    *,
    path: pathlib.Path | str | None,
    fetched: list[ModelScore],
    written: list[ModelScore],
    backend: BenchBackend,
) -> None:
    conn = _cache_connect(path)
    try:
        now = _cache_now()
        conn.execute("BEGIN")
        _replace_cached_scores(conn, fetched, backend, now)
        for score in written:
            _put_cached_score(conn, score)
        _stamp_cache(conn, "scores", backend, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _put_score_batches(backend: BenchBackend, scores: list[ModelScore], *, batch_size: int = 500) -> None:
    for start in range(0, len(scores), batch_size):
        batch = scores[start : start + batch_size]
        try:
            payload = _handoffkeep_request(
                backend,
                "scores",
                method="PUT",
                body={"scores": [_score_to_wire(score) for score in batch]},
            )
        except BenchBackendError:
            raise
        accepted = payload.get("upserted")
        if isinstance(accepted, bool) or not isinstance(accepted, int) or accepted != len(batch):
            raise BenchBackendError("handoffkeep rejected a score write")


def _score_key(score: ModelScore) -> tuple[str, str | None, str | None, str, str]:
    return (score.model_id, score.effort, score.harness, score.source, score.metric)


def _ranked_score_updates(existing: list[ModelScore], incoming: list[ModelScore]) -> list[ModelScore]:
    """Recalculate every affected source/metric group before a remote PUT."""

    by_key = {_score_key(score): score for score in existing}
    for score in incoming:
        by_key[_score_key(score)] = score
    affected = {(score.source, score.metric) for score in incoming}
    updated: list[ModelScore] = []
    for source, metric in sorted(affected):
        group = [score for score in by_key.values() if (score.source, score.metric) == (source, metric)]
        ranked = sorted(
            (score for score in group if score.score is not None),
            key=lambda score: (-float(score.score), score.model_id, score.effort or "", score.harness or ""),
        )
        previous_score: float | None = None
        previous_rank = 0
        ranked_by_key: dict[tuple[str, str | None, str | None, str, str], ModelScore] = {}
        for index, score in enumerate(ranked, start=1):
            if previous_score is None or score.score != previous_score:
                previous_rank = index
                previous_score = score.score
            ranked_by_key[_score_key(score)] = replace(score, rank=previous_rank)
        for score in group:
            if score.score is None:
                ranked_by_key[_score_key(score)] = replace(score, rank=None)
        updated.extend(ranked_by_key.values())
    return sorted(updated, key=lambda score: (score.source, score.metric, score.model_id, score.effort or ""))


def _write_scores_handoffkeep(
    scores: list[ModelScore],
    *,
    path: pathlib.Path | str | None,
    backend: BenchBackend,
    recompute_ranks: bool = False,
) -> int:
    """Refresh remotely, PUT, then atomically make the local cache visible."""

    if not scores:
        return 0
    fetched = _fetch_scores(backend)
    written = _ranked_score_updates(fetched, scores) if recompute_ranks else scores
    _put_score_batches(backend, written)
    try:
        _commit_score_cache(path=path, fetched=fetched, written=written, backend=backend)
    except (sqlite3.Error, OSError) as exc:
        raise BenchBackendError("local bench cache update failed") from exc
    return len(scores)


def _read_scores_handoffkeep(
    model_id: str | None, *, path: pathlib.Path | str | None, backend: BenchBackend
) -> list[ModelScore]:
    normalized = _model_id(model_id) if model_id is not None else None
    conn = _cache_connect(path)
    try:
        now = _cache_now()
        cached = _cached_scores(conn)
        fresh, age_hours = _cache_state(conn, "scores", backend, now)
    finally:
        conn.close()
    cached_for_endpoint = cached if age_hours is not None else []
    if fresh:
        rows = cached_for_endpoint
    else:
        try:
            rows = _fetch_scores(backend)
            _commit_score_cache(path=path, fetched=rows, written=[], backend=backend)
        except (BenchBackendError, sqlite3.Error, OSError, ValueError):
            _warn_cached("scores", age_hours=age_hours, has_data=bool(cached_for_endpoint))
            rows = cached_for_endpoint
    return [row for row in rows if normalized is None or row.model_id == normalized]


def read_scores(model_id: str | None = None, *, path: pathlib.Path | str | None = None) -> list[ModelScore]:
    """Read canonical scores when configured, otherwise preserve local behavior."""

    backend = bench_backend(use="catalog")
    if backend.name == BENCH_BACKEND_LOCAL:
        return _read_local_scores(model_id, path=path)
    return _read_scores_handoffkeep(model_id, path=path, backend=backend)


def _positive_price(value: object) -> float | None:
    """Return a usable USD/1M value without coercing strings or booleans."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _price_from_row(row: sqlite3.Row) -> ModelPrice | None:
    blended = _positive_price(row["price_1m_blended_3_to_1"])
    if blended is None:
        return None
    return ModelPrice(
        model_id=row["model_id"],
        price_1m_blended_3_to_1=blended,
        price_1m_input_tokens=_positive_price(row["price_1m_input_tokens"]),
        price_1m_output_tokens=_positive_price(row["price_1m_output_tokens"]),
        captured_at=row["captured_at"],
    )


def read_prices(*, path: pathlib.Path | str | None = None) -> dict[str, ModelPrice]:
    """Read prices keyed by normalized base AA model id without migrating the DB.

    The two operator-approved seeds are always available. Any synchronized row
    with the same normalized base model id replaces its seed, including when the
    DB value is higher or lower.
    """

    prices = {normalize_aa_model_id(item.model_id): item for item in AA_MODEL_PRICE_SEEDS}
    target = pathlib.Path(path) if path is not None else db_path()
    if str(target) == ":memory:" or not target.expanduser().exists():
        return prices
    conn = _readonly_connect(target)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'model_prices'"
        ).fetchone()
        if exists is None:
            return prices
        rows = conn.execute(
            "SELECT model_id, price_1m_blended_3_to_1, price_1m_input_tokens, "
            "price_1m_output_tokens, captured_at FROM model_prices ORDER BY model_id"
        ).fetchall()
        for row in rows:
            price = _price_from_row(row)
            if price is not None:
                prices[normalize_aa_model_id(price.model_id)] = price
        return prices
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
    backend = bench_backend(use="catalog")
    if backend.name == BENCH_BACKEND_HANDOFFKEEP:
        return _write_scores_handoffkeep(checked, path=path, backend=backend, recompute_ranks=True)
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


def _price_cost_key(price: ModelPrice) -> tuple[float, float, float]:
    """Order duplicate observations deterministically, preferring dearer data."""

    return (
        price.price_1m_blended_3_to_1,
        price.price_1m_input_tokens or 0.0,
        price.price_1m_output_tokens or 0.0,
    )


def _aa_prices(payload: object, *, captured_at: str) -> list[ModelPrice]:
    """Extract valid AA pricing rows; malformed prices never abort score sync."""

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise BenchError("invalid Artificial Analysis response: data must be an array")
    prices: dict[str, ModelPrice] = {}
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        model_id = item.get("slug") or item.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        pricing = item.get("pricing")
        if not isinstance(pricing, dict):
            continue
        blended = _positive_price(pricing.get("price_1m_blended_3_to_1"))
        if blended is None:
            continue
        base_model_id, _effort = parse_effort_suffix(model_id)
        normalized_base = _model_id(base_model_id)
        candidate = ModelPrice(
            model_id=normalized_base,
            price_1m_blended_3_to_1=blended,
            price_1m_input_tokens=_positive_price(pricing.get("price_1m_input_tokens")),
            price_1m_output_tokens=_positive_price(pricing.get("price_1m_output_tokens")),
            captured_at=captured_at,
        )
        current = prices.get(normalized_base)
        if current is None or _price_cost_key(candidate) > _price_cost_key(current):
            prices[normalized_base] = candidate
    return list(prices.values())


def _upsert_price(conn: sqlite3.Connection, price: ModelPrice) -> None:
    row = conn.execute(
        "SELECT model_id, price_1m_blended_3_to_1, price_1m_input_tokens, "
        "price_1m_output_tokens, captured_at FROM model_prices WHERE model_id = ?",
        (price.model_id,),
    ).fetchone()
    existing = _price_from_row(row) if row is not None else None
    if existing is not None and _price_cost_key(existing) >= _price_cost_key(price):
        return
    conn.execute(
        "INSERT INTO model_prices "
        "(model_id, price_1m_blended_3_to_1, price_1m_input_tokens, "
        "price_1m_output_tokens, captured_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(model_id) DO UPDATE SET "
        "price_1m_blended_3_to_1 = excluded.price_1m_blended_3_to_1, "
        "price_1m_input_tokens = excluded.price_1m_input_tokens, "
        "price_1m_output_tokens = excluded.price_1m_output_tokens, "
        "captured_at = excluded.captured_at",
        (
            price.model_id,
            price.price_1m_blended_3_to_1,
            price.price_1m_input_tokens,
            price.price_1m_output_tokens,
            price.captured_at,
        ),
    )


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
    prices = _aa_prices(payload, captured_at=timestamp)
    backend = bench_backend(use="catalog")
    if backend.name == BENCH_BACKEND_HANDOFFKEEP:
        return _write_scores_handoffkeep(scores, path=path, backend=backend, recompute_ranks=True)
    conn = connect(path)
    try:
        _seed_conn(conn)
        _apply_known_aa_agent_measurements(conn)
        for score in scores:
            _upsert(conn, score)
        for price in prices:
            _upsert_price(conn, price)
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
    except BenchBackendError:
        print("error: handoffkeep bench sync failed", file=stderr)
        return 2
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
    backend = bench_backend(use="catalog")
    if backend.name == BENCH_BACKEND_HANDOFFKEEP:
        return _write_scores_handoffkeep(rows, path=db, backend=backend, recompute_ranks=True)
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


def _grade_from_wire(value: object) -> GradeAssignment:
    if not isinstance(value, dict):
        raise BenchError("invalid handoffkeep grade row")
    profile = _text(value.get("profile"), "profile")
    grade = _text(value.get("grade"), "grade")
    deviation_ref = _text(value.get("deviation_ref"), "deviation_ref")
    assert profile is not None and grade is not None and deviation_ref is not None
    if grade not in REP_GRADES:
        raise BenchError("invalid handoffkeep grade")
    return GradeAssignment(
        profile=profile,
        grade=grade,
        boundary_version=_optional_text(value.get("boundary_version"), "boundary_version"),
        deviation_ref=deviation_ref,
        decided_at=_optional_text(value.get("decided_at"), "decided_at"),
        decided_by=_optional_text(value.get("decided_by"), "decided_by"),
    )


def _grades_from_payload(payload: dict) -> list[GradeAssignment]:
    values = payload.get("grades")
    if not isinstance(values, list):
        raise BenchBackendError("handoffkeep returned invalid grade data")
    try:
        return [_grade_from_wire(value) for value in values]
    except BenchError as exc:
        raise BenchBackendError("handoffkeep returned invalid grade data") from exc


def _fetch_grades(backend: BenchBackend) -> list[GradeAssignment]:
    return _grades_from_payload(_handoffkeep_request(backend, "grades"))


def _cached_grades(conn: sqlite3.Connection) -> list[GradeAssignment]:
    rows = conn.execute(
        "SELECT profile, grade, boundary_version, deviation_ref, decided_at, decided_by "
        "FROM bench_cache_grades ORDER BY profile"
    ).fetchall()
    return [
        GradeAssignment(
            profile=row["profile"],
            grade=row["grade"],
            boundary_version=row["boundary_version"],
            deviation_ref=row["deviation_ref"],
            decided_at=row["decided_at"],
            decided_by=row["decided_by"],
        )
        for row in rows
    ]


def _put_cached_grade(conn: sqlite3.Connection, grade: GradeAssignment) -> None:
    conn.execute(
        "INSERT INTO bench_cache_grades "
        "(profile, grade, boundary_version, deviation_ref, decided_at, decided_by) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(profile) DO UPDATE SET "
        "grade = excluded.grade, boundary_version = excluded.boundary_version, "
        "deviation_ref = excluded.deviation_ref, decided_at = excluded.decided_at, "
        "decided_by = excluded.decided_by",
        (
            grade.profile,
            grade.grade,
            grade.boundary_version,
            grade.deviation_ref,
            grade.decided_at,
            grade.decided_by,
        ),
    )


def _replace_cached_grades(
    conn: sqlite3.Connection, grades: list[GradeAssignment], backend: BenchBackend, now: dt.datetime
) -> None:
    conn.execute("DELETE FROM bench_cache_grades")
    for grade in grades:
        _put_cached_grade(conn, grade)
    _stamp_cache(conn, "grades", backend, now)


def _commit_grade_cache(
    *,
    path: pathlib.Path | str | None,
    fetched: list[GradeAssignment],
    written: list[GradeAssignment],
    backend: BenchBackend,
) -> None:
    conn = _cache_connect(path)
    try:
        now = _cache_now()
        conn.execute("BEGIN")
        _replace_cached_grades(conn, fetched, backend, now)
        for grade in written:
            _put_cached_grade(conn, grade)
        _stamp_cache(conn, "grades", backend, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_grades(*, path: pathlib.Path | str | None = None) -> list[GradeAssignment]:
    """Read canonical grade placements, failing open to a matching cache."""

    backend = bench_backend(use="catalog")
    if backend.name == BENCH_BACKEND_LOCAL:
        return []
    conn = _cache_connect(path)
    try:
        now = _cache_now()
        cached = _cached_grades(conn)
        fresh, age_hours = _cache_state(conn, "grades", backend, now)
    finally:
        conn.close()
    cached_for_endpoint = cached if age_hours is not None else []
    if fresh:
        return cached_for_endpoint
    try:
        rows = _fetch_grades(backend)
        _commit_grade_cache(path=path, fetched=rows, written=[], backend=backend)
        return rows
    except (BenchBackendError, sqlite3.Error, OSError, ValueError):
        _warn_cached("grades", age_hours=age_hours, has_data=bool(cached_for_endpoint))
        return cached_for_endpoint


def _grade_to_wire(grade: GradeAssignment) -> dict[str, object]:
    value: dict[str, object] = {
        "profile": grade.profile,
        "grade": grade.grade,
        "deviation_ref": grade.deviation_ref,
    }
    if grade.boundary_version is not None:
        value["boundary_version"] = grade.boundary_version
    return value


def set_grade(
    *,
    profile: str,
    grade: str,
    deviation_ref: str,
    boundary_version: str | None = None,
    path: pathlib.Path | str | None = None,
) -> int:
    """Set a canonical placement after explicitly recording its deviation."""

    normalized_profile = _text(profile, "profile")
    normalized_grade = _text(grade, "grade")
    if not isinstance(deviation_ref, str) or not deviation_ref.strip():
        raise BenchError("deviation_ref is required")
    normalized_ref = deviation_ref.strip()
    assert normalized_profile is not None and normalized_grade is not None
    if normalized_grade not in REP_GRADES:
        raise BenchError(f"grade must be one of: {', '.join(REP_GRADES)}")
    normalized_boundary = _optional_text(boundary_version, "boundary_version")
    backend = bench_backend(use="catalog")
    if backend.name != BENCH_BACKEND_HANDOFFKEEP:
        raise BenchBackendError("bench grades require backend = handoffkeep")
    written = [
        GradeAssignment(
            profile=normalized_profile,
            grade=normalized_grade,
            boundary_version=normalized_boundary,
            deviation_ref=normalized_ref,
        )
    ]
    fetched = _fetch_grades(backend)
    payload = _handoffkeep_request(
        backend,
        "grades",
        method="PUT",
        body={"grades": [_grade_to_wire(item) for item in written]},
    )
    accepted = payload.get("upserted")
    if isinstance(accepted, bool) or not isinstance(accepted, int) or accepted != len(written):
        raise BenchBackendError("handoffkeep rejected a grade write")
    try:
        _commit_grade_cache(path=path, fetched=fetched, written=written, backend=backend)
    except (sqlite3.Error, OSError) as exc:
        raise BenchBackendError("local bench cache update failed") from exc
    return len(written)


# ---------------------------------------------------------------------------
# #593: the canonical (profile, effort) catalog.
#
# handoffkeep ``/v1/bench/catalog`` (schema v12) owns model ids, grade
# placement, pool and gate. ``recommend.GRADE_TABLE`` is demoted to a bundled
# offline snapshot. Three states, never two: a single "fresh?" boolean cannot
# express both "the cache is a copy of the canon" and "we are down to the
# bundled table", and conflating them is how a dead server turns into free
# dispatch (2558).
#   fresh    age < catalog_ttl_s            — cache served, no request
#   cached   ttl <= age < stale_max         — refetch attempted, cache on failure
#   snapshot age >= stale_max, or no cache  — bundled table, labelled ``stale``
# ---------------------------------------------------------------------------

CATALOG_SOURCE_SERVER = "server"
CATALOG_SOURCE_CACHE = "cache"
CATALOG_SOURCE_SNAPSHOT = "snapshot"
# The endpoint answered, and answered that it has no catalog route.
CATALOG_SOURCE_UNSUPPORTED = "unsupported"

_CATALOG_COLUMNS = (
    "profile",
    "effort",
    "model_id",
    "pool",
    "grade",
    "score",
    "gate",
    "gate_reason",
    "benchmark_source",
    "benchmark_annotation",
    "boundary_version",
    "deviation_ref",
    "decided_at",
    "decided_by",
    "retired_at",
)


@dataclass(frozen=True)
class CatalogEntry:
    """One canonical (profile, effort) row, mirroring the server wire shape."""

    profile: str
    effort: str
    model_id: str
    pool: str
    grade: str
    score: float | None = None
    gate: str = "default"
    gate_reason: str | None = None
    benchmark_source: str | None = None
    benchmark_annotation: str | None = None
    boundary_version: str | None = None
    deviation_ref: str = ""
    decided_at: str | None = None
    decided_by: str | None = None
    retired_at: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.profile, self.effort)

    def as_dict(self) -> dict[str, object]:
        return {column: getattr(self, column) for column in _CATALOG_COLUMNS}


@dataclass(frozen=True)
class CatalogView:
    """A catalog read plus the provenance every consumer has to disclose."""

    entries: tuple[CatalogEntry, ...]
    source: str
    age_s: float | None = None
    backend: str = BENCH_BACKEND_LOCAL
    reason: str = ""

    @property
    def local_only(self) -> bool:
        """True when this host is not configured to read the canon at all.

        A local-backend host is not *lagging* the canon — it has none, by
        configuration.  Keeping the two apart matters: losing a canon you were
        reading is a reason to stop widening gates, whereas never having had one
        is the status quo and must not newly break a local host's launches.
        """

        return self.backend == BENCH_BACKEND_LOCAL

    @property
    def stale(self) -> bool:
        """True when the bundled snapshot stands in for a canon we should have.

        Not every snapshot read is stale. A local-only host has no canon by
        configuration, and a server that has no catalog route has none to be
        behind — only losing a canon this host was supposed to read is stale, and
        only that justifies refusing to widen a gate.
        """

        return self.source == CATALOG_SOURCE_SNAPSHOT and not self.local_only

    @property
    def label(self) -> str:
        if self.source == CATALOG_SOURCE_UNSUPPORTED:
            return "catalog=unsupported (server has no catalog route; using bench grades)"
        if self.source == CATALOG_SOURCE_SNAPSHOT:
            if self.local_only:
                return "catalog=snapshot (local backend — server catalog not in use)"
            return "catalog=stale (snapshot)"
        if self.age_s is None:
            return f"catalog={self.source}"
        return f"catalog={self.source} (age {self.age_s / 3600.0:.1f}h)"

    def by_key(self) -> dict[tuple[str, str], CatalogEntry]:
        return {entry.key: entry for entry in self.entries}

    def profiles(self) -> set[str]:
        return {entry.profile for entry in self.entries}


def _catalog_score(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchError("catalog score must be a finite number")
    score = float(value)
    if not math.isfinite(score) or score < 0.0 or score > 100.0:
        raise BenchError("catalog score must be between 0 and 100")
    return score


def _catalog_from_wire(value: object) -> CatalogEntry:
    if not isinstance(value, dict):
        raise BenchError("invalid handoffkeep catalog row")
    profile = _text(value.get("profile"), "profile")
    grade = _text(value.get("grade"), "grade")
    assert profile is not None and grade is not None
    if grade not in REP_GRADES:
        raise BenchError("invalid handoffkeep catalog grade")
    gate = _optional_text(value.get("gate"), "gate") or "default"
    if gate not in CATALOG_GATES:
        raise BenchError("invalid handoffkeep catalog gate")
    effort = _optional_text(value.get("effort"), "effort") or ""
    return CatalogEntry(
        profile=profile,
        effort=effort,
        model_id=_optional_text(value.get("model_id"), "model_id") or "",
        pool=_optional_text(value.get("pool"), "pool") or "",
        grade=grade,
        score=_catalog_score(value.get("score")),
        gate=gate,
        gate_reason=_optional_text(value.get("gate_reason"), "gate_reason"),
        benchmark_source=_optional_text(value.get("benchmark_source"), "benchmark_source"),
        benchmark_annotation=_optional_text(value.get("benchmark_annotation"), "benchmark_annotation"),
        boundary_version=_optional_text(value.get("boundary_version"), "boundary_version"),
        deviation_ref=_optional_text(value.get("deviation_ref"), "deviation_ref") or "",
        decided_at=_optional_text(value.get("decided_at"), "decided_at"),
        decided_by=_optional_text(value.get("decided_by"), "decided_by"),
        retired_at=_optional_text(value.get("retired_at"), "retired_at"),
    )


def _catalog_from_payload(payload: dict) -> list[CatalogEntry]:
    values = payload.get("catalog")
    if not isinstance(values, list):
        raise BenchBackendError("handoffkeep returned invalid catalog data")
    try:
        return [_catalog_from_wire(value) for value in values]
    except BenchError as exc:
        raise BenchBackendError("handoffkeep returned invalid catalog data") from exc


def _fetch_catalog(backend: BenchBackend) -> list[CatalogEntry]:
    return _catalog_from_payload(_handoffkeep_request(backend, "catalog"))


def _catalog_to_wire(entry: CatalogEntry) -> dict[str, object]:
    value = entry.as_dict()
    # decided_by is caller-supplied provenance on this route and the server
    # rejects a blank one; everything else may legitimately be null.
    return value


def _cached_catalog(conn: sqlite3.Connection) -> list[CatalogEntry]:
    columns = ", ".join(_CATALOG_COLUMNS)
    rows = conn.execute(f"SELECT {columns} FROM bench_cache_catalog ORDER BY profile, effort").fetchall()
    return [CatalogEntry(**{column: row[column] for column in _CATALOG_COLUMNS}) for row in rows]


def _put_cached_catalog(conn: sqlite3.Connection, entry: CatalogEntry) -> None:
    columns = ", ".join(_CATALOG_COLUMNS)
    placeholders = ", ".join("?" for _ in _CATALOG_COLUMNS)
    updates = ", ".join(
        f"{column} = excluded.{column}" for column in _CATALOG_COLUMNS if column not in ("profile", "effort")
    )
    conn.execute(
        f"INSERT INTO bench_cache_catalog ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(profile, effort) DO UPDATE SET {updates}",
        tuple(getattr(entry, column) for column in _CATALOG_COLUMNS),
    )


def _commit_catalog_cache(
    *, path: pathlib.Path | str | None, entries: list[CatalogEntry], backend: BenchBackend
) -> None:
    conn = _cache_connect(path)
    try:
        now = _cache_now()
        conn.execute("BEGIN")
        conn.execute("DELETE FROM bench_cache_catalog")
        for entry in entries:
            _put_cached_catalog(conn, entry)
        _stamp_cache(conn, "catalog", backend, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# One resolved catalog per process. Without this, ``--recommend`` and a gate
# check inside the same command can straddle the TTL boundary and disagree —
# one admitting on the old grade while the other launches the new model.
_CATALOG_MEMO: dict[tuple[str, str, str], CatalogView] = {}


def reset_catalog_memo() -> None:
    """Drop the per-process catalog memo (tests, and long-lived --watch loops)."""

    _CATALOG_MEMO.clear()


def read_catalog(
    *,
    path: pathlib.Path | str | None = None,
    commit_cache: bool = True,
    allow_plaintext_http: bool = False,
) -> CatalogView:
    """Read the canonical catalog, disclosing which of the three states served it.

    Never raises for an unreachable server: the bundled snapshot is a reviewed
    copy of the last canon, so a handoffkeep outage degrades the fleet rather
    than stopping it. What the outage must not do is widen anything — that rule
    lives with the consumers (``scopefuel.launch``), which refuse to relax a
    non-default gate while this view is stale.

    ``commit_cache=False`` serves the same view without persisting the server
    response into the local cache — the strictly read-only path ``grades
    propose``/``apply`` take.
    """

    backend = bench_backend(use="catalog", allow_plaintext_http=allow_plaintext_http)
    memo_key = (str(path or ""), backend.name, backend.endpoint_id)
    memoized = _CATALOG_MEMO.get(memo_key)
    if memoized is not None:
        return memoized

    view = _read_catalog_uncached(backend, path=path, commit_cache=commit_cache)
    _CATALOG_MEMO[memo_key] = view
    return view


def _read_catalog_uncached(
    backend: BenchBackend, *, path: pathlib.Path | str | None, commit_cache: bool = True
) -> CatalogView:
    if backend.name == BENCH_BACKEND_LOCAL:
        return CatalogView(
            entries=catalog_snapshot(),
            source=CATALOG_SOURCE_SNAPSHOT,
            age_s=None,
            backend=backend.name,
            reason=backend.reason,
        )

    now = _cache_now()
    cached: list[CatalogEntry] = []
    row = None
    if commit_cache:
        conn = _cache_connect(path)
        try:
            cached = _cached_catalog(conn)
            row = conn.execute(
                "SELECT fetched_at, endpoint_id FROM bench_cache_meta WHERE scope = 'catalog'"
            ).fetchone()
        except sqlite3.Error:
            cached, row = [], None
        finally:
            conn.close()
    else:
        # The read-only path (grades propose/apply): probe the cache without
        # creating the DB or its schema. A missing/unreadable cache is simply
        # "no cache" — the server fetch below still happens.
        target = pathlib.Path(path) if path is not None else db_path()
        if str(target) != ":memory:" and target.expanduser().exists():
            try:
                conn = _readonly_connect(target)
                try:
                    cached = _cached_catalog(conn)
                    row = conn.execute(
                        "SELECT fetched_at, endpoint_id FROM bench_cache_meta WHERE scope = 'catalog'"
                    ).fetchone()
                finally:
                    conn.close()
            except sqlite3.Error:
                cached, row = [], None

    age_s: float | None = None
    if row is not None and row["endpoint_id"] == backend.endpoint_id:
        fetched_at = _cached_at(row["fetched_at"])
        if fetched_at is not None:
            age = (now - fetched_at).total_seconds()
            # A stamp from the future is not a very fresh cache, it is a broken
            # one — a clock skew or a corrupted row. Clamping it to age 0 pinned
            # the host to that cache forever and no server change ever arrived.
            age_s = age if age >= -_CACHE_CLOCK_SKEW_S else None
            if age_s is not None:
                age_s = max(0.0, age_s)
    if age_s is None:
        cached = []

    # ``catalog_stale_max_s`` is a ceiling on trusting the cache at all, so a TTL
    # configured above it must not be able to keep serving a cache the ceiling
    # has already condemned.
    fresh_before = min(backend.catalog_ttl_s, backend.catalog_stale_max_s)
    if cached and age_s is not None and age_s < fresh_before:
        return CatalogView(
            entries=tuple(cached),
            source=CATALOG_SOURCE_CACHE,
            age_s=age_s,
            backend=backend.name,
            reason=backend.reason,
        )

    try:
        entries = _fetch_catalog(backend)
        if commit_cache:
            _commit_catalog_cache(path=path, entries=entries, backend=backend)
        return CatalogView(
            entries=tuple(entries),
            source=CATALOG_SOURCE_SERVER,
            age_s=0.0,
            backend=backend.name,
            reason=backend.reason,
        )
    except BenchRouteMissing:
        return CatalogView(
            entries=catalog_snapshot(),
            source=CATALOG_SOURCE_UNSUPPORTED,
            age_s=None,
            backend=backend.name,
            reason=backend.reason,
        )
    except (BenchBackendError, sqlite3.Error, OSError, ValueError):
        pass

    if cached and age_s is not None and age_s < backend.catalog_stale_max_s:
        _warn_cached("catalog", age_hours=age_s / 3600.0, has_data=True)
        return CatalogView(
            entries=tuple(cached),
            source=CATALOG_SOURCE_CACHE,
            age_s=age_s,
            backend=backend.name,
            reason=backend.reason,
        )

    print(
        "warning: handoffkeep catalog unavailable; using the bundled snapshot (catalog=stale)",
        file=sys.stderr,
    )
    return CatalogView(
        entries=catalog_snapshot(),
        source=CATALOG_SOURCE_SNAPSHOT,
        age_s=age_s,
        backend=backend.name,
        reason=backend.reason,
    )


def catalog_snapshot() -> tuple[CatalogEntry, ...]:
    """The bundled offline catalog, derived from the reviewed code tables.

    Placements plus the #692 E6 measurement rungs: the rungs are catalog rows
    (grade C, unmeasured, marker-gated) but not placements, so they are kept out
    of ``snapshot_entries()`` — that snapshot mirrors the placement canon and
    keeps its invariants (Sol S+-only) — while the catalog view, ``bench catalog
    list`` and the canon seed all carry them.
    """

    from .launch import e6_arm_entries, snapshot_entries

    return snapshot_entries() + e6_arm_entries()


def _catalog_sort_key(entry: CatalogEntry) -> tuple[int, str, int, str]:
    grade_rank = REP_GRADES.index(entry.grade) if entry.grade in REP_GRADES else len(REP_GRADES)
    return (grade_rank, entry.profile, CATALOG_EFFORT_RANKS.get(entry.effort, 99), entry.effort)


def _catalog_rows_from_json(payload: object) -> list[CatalogEntry]:
    rows = payload.get("catalog") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise BenchError('catalog JSON must be a non-empty list (or {"catalog": [...]})')
    return [_catalog_from_wire(row) for row in rows]


def push_catalog(source: pathlib.Path | str, *, path: pathlib.Path | str | None = None) -> int:
    """Write catalog rows to handoffkeep (operator token) and refresh the cache."""

    backend = bench_backend(use="catalog")
    if backend.name != BENCH_BACKEND_HANDOFFKEEP:
        raise BenchError(
            "bench push-catalog requires the handoffkeep backend "
            "(set HANDOFFKEEP_URL/HANDOFFKEEP_TOKEN, or [bench] backend)"
        )
    try:
        raw = pathlib.Path(os.path.expanduser(str(source))).read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchError(f"cannot read catalog JSON: {source}") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise BenchError("catalog JSON is not valid JSON") from exc
    entries = _catalog_rows_from_json(payload)
    for entry in entries:
        if not entry.decided_by:
            raise BenchError(
                f"{entry.profile}/{entry.effort or '-'}: decided_by is required caller-supplied "
                "provenance on the catalog route"
            )
    response = _handoffkeep_request(
        backend,
        "catalog",
        method="PUT",
        body={"catalog": [_catalog_to_wire(entry) for entry in entries]},
    )
    accepted = response.get("upserted")
    if isinstance(accepted, bool) or not isinstance(accepted, int) or accepted != len(entries):
        raise BenchBackendError("handoffkeep rejected the catalog write")
    reset_catalog_memo()
    try:
        _commit_catalog_cache(path=path, entries=_fetch_catalog(backend), backend=backend)
    except (BenchBackendError, sqlite3.Error, OSError, ValueError):
        # The write landed; a cache refresh failure only costs one extra read.
        print("warning: catalog written but the local cache was not refreshed", file=sys.stderr)
    return len(entries)


def _entry_subscribed(entry: CatalogEntry) -> bool:
    """#742 — config-level subscription state of this catalog row.

    The flag lives in config, not in the canon: a row is never deleted for
    being unsubscribed, only marked. Pool resolution mirrors the gate — the
    name table wins where it has a mapping, the row's own pool otherwise.
    """
    from .recommend import profile_pool, profile_subscription

    pool = profile_pool(entry.profile)[0] or entry.pool or None
    return profile_subscription(entry.profile, pool).subscribed


def catalog_report(*, path: pathlib.Path | str | None = None) -> str:
    """Render the catalog as one row per (profile, effort)."""

    view = read_catalog(path=path)
    lines = [view.label]
    for entry in sorted(view.entries, key=_catalog_sort_key):
        retired = " retired" if entry.retired_at else ""
        unsubscribed = " unsubscribed" if not _entry_subscribed(entry) else ""
        score = "-" if entry.score is None else f"{entry.score:g}"
        lines.append(
            f"{entry.grade:<3} {entry.profile}"
            f"{'@' + entry.effort if entry.effort else ''} "
            f"model={entry.model_id or '-'} pool={entry.pool or '-'} "
            f"gate={entry.gate} score={score}{retired}{unsubscribed}"
        )
    return "\n".join(lines)


def catalog_status_report(*, path: pathlib.Path | str | None = None) -> str:
    """One screen answering "is this host reading the canon, and if not, why?"."""

    backend = bench_backend(use="catalog")
    view = read_catalog(path=path)
    # Report the credentials that exist on the host, not the ones the resolved
    # backend kept — "token missing" on a host that has one sends the reader to
    # the wrong problem.
    found_url, found_token = _handoffkeep_credentials()
    lines = [
        f"backend={backend.name} reason={backend.reason}",
        f"credentials url={'found' if found_url else 'none'} "
        f"token={'found' if found_token else 'none'} "
        f"(env or {handoffkeep_dotenv_path()})",
        f"catalog_ttl_s={backend.catalog_ttl_s:g} catalog_stale_max_s={backend.catalog_stale_max_s:g}",
        view.label,
        f"rows={len(view.entries)} profiles={len(view.profiles())}",
    ]
    env_url = os.environ.get("HANDOFFKEEP_URL")
    env_token = os.environ.get("HANDOFFKEEP_TOKEN")
    if bool(env_url) != bool(env_token):
        missing = "HANDOFFKEEP_TOKEN" if env_url else "HANDOFFKEEP_URL"
        lines.append(
            f"note: only one of HANDOFFKEEP_URL/HANDOFFKEEP_TOKEN is set ({missing} is missing); "
            "an environment override is all-or-nothing and config.env is not used to complete it"
        )
    override = os.environ.get("HANDOFFKEEP_CONFIG")
    if override and not pathlib.Path(os.path.expanduser(override)).is_file():
        # An explicit override is honoured as written — it deliberately does not
        # fall back to the default config.env. Say so, because a typo in that
        # variable otherwise leaves the host in local mode with no sign of why,
        # which is the exact silence this whole change exists to remove.
        lines.append(
            f"note: HANDOFFKEEP_CONFIG points at {override}, which does not exist; "
            "the default ~/.config/handoffkeep/config.env is NOT consulted while it is set"
        )
    if backend.reason == "auto-local-insecure-url":
        lines.append(
            "blocked: handoffkeep credentials exist but the URL is plaintext http to a "
            "non-local host; serve it over https, or set [bench] allow_plaintext_catalog = true "
            "for a private WireGuard/Tailscale tunnel"
        )
    if view.stale:
        lines.append(
            "stale: running on the bundled snapshot — server placements are NOT in effect; "
            "non-default gates require --operator-request until the canon is readable"
        )
    uncovered = sorted(snapshot_profiles() - view.profiles()) if not view.stale else []
    if uncovered:
        lines.append("uncovered (snapshot-only, catalog has no row): " + ", ".join(uncovered))
    return "\n".join(lines)


def snapshot_profiles() -> set[str]:
    from .launch import snapshot_entries

    return {entry.profile for entry in snapshot_entries()}


def _profile_from_catalog(entry: CatalogEntry, template: object | None):
    """Build the runtime Profile for one catalog row.

    A row that also exists in the bundled snapshot keeps that row's display and
    provenance metadata (estimate reasons, annotations, AA lookup keys) and takes
    only placement, model id and gate from the canon — losing the metadata would
    turn every ``--recommend`` line into a bare model id the moment a host went
    server-canonical.
    """

    from .recommend import Profile

    effort = entry.effort or None
    if template is not None:
        return replace(
            template,
            gate=entry.gate,
            gate_reason=entry.gate_reason or template.gate_reason,
            benchmark=entry.score if entry.score is not None else template.benchmark,
            aa_agent_model_id=entry.model_id or template.aa_agent_model_id,
            launcher_effort=effort or template.launcher_effort,
        )
    label = entry.model_id or entry.profile
    return Profile(
        entry.profile,
        f"{label} ({entry.effort})" if entry.effort else label,
        entry.score,
        gate=entry.gate,
        gate_reason=entry.gate_reason,
        launcher_effort=effort,
        benchmark_annotation=entry.benchmark_annotation,
        benchmark_source=entry.benchmark_source,
        benchmark_effort=effort,
        aa_agent_model_id=entry.model_id or None,
        # Carry the server's pool: this row's name is one the local routing table
        # may never have seen, and without it the profile reaches the grade table
        # only to render "측정 불가" — a server addition that can never be
        # recommended is not an addition.
        catalog_pool=entry.pool or None,
    )


def _catalog_grade_table(view: CatalogView) -> dict | None:
    """Rebuild the grade table with the catalog as the canon, or None if unusable.

    The rules that make the canon actually canonical:

    * a ``(profile, effort)`` the catalog carries is placed where the catalog
      says — additions and moves both land;
    * a snapshot row whose *profile* the catalog knows but whose *rung* it does
      not is dropped, so retiring or deleting a rung server-side takes effect;
    * a snapshot row for a profile the catalog never mentions is kept, so a
      half-seeded catalog cannot silently empty the table (``bench catalog
      status`` lists these as uncovered);
    * ``consult_only`` rows never enter the table at all — they are launchable
      on an explicit operator request, never recommendation candidates.

    The merge is all-or-nothing: if the result fails the boundary validator the
    whole catalog is rejected.  A partially applied canon is worse than a stale
    one, because nothing downstream could tell which half it got.
    """

    from .recommend import E6_ARM_GRADE, E6_ARM_KEYS, GRADE_TABLE, validate_grade_table

    if not view.entries:
        # Nothing seeded yet. A catalog that has said nothing cannot be the
        # reason the table empties, so the snapshot stands.
        return None
    live = [entry for entry in view.entries if not entry.retired_at]
    # Coverage counts retired rows too. A profile whose every rung the operator
    # retired is a profile the canon has spoken about — leaving it out here put
    # its snapshot rows back into the recommendations while ``resolve_launch``
    # refused to start it, so a dispatcher could be handed a profile it cannot
    # launch. A catalog where everything is retired therefore empties the table,
    # which is what it was asked to say.
    covered_profiles = {entry.profile for entry in view.entries}
    templates: dict[tuple[str, str], object] = {}
    for profiles in GRADE_TABLE.values():
        for profile in profiles:
            templates.setdefault((profile.name, profile.launcher_effort or ""), profile)

    # A profile-default row (effort "") is the legacy /v1/bench/grades projection
    # of the profile's default placement, not an extra launchable rung. Once the
    # same profile carries enumerated rungs, admitting both would list it twice —
    # once with an effort and once without — and a `bench grades set` during the
    # rollout would silently add that phantom candidate.
    enumerated = {entry.profile for entry in live if entry.effort}

    proposed: dict[str, list] = {grade: [] for grade in GRADE_TABLE}
    for entry in sorted(live, key=_catalog_sort_key):
        if entry.gate == "consult_only":
            continue
        # #692: an unmeasured E6 measurement rung is a catalog row, not a
        # candidate. A canon that carries the row (the seed emits them) must not
        # turn it into a recommendation at C — `--recommend` never lists one, and
        # only the E6 arm marker opens the rung at the gate. Once the canon
        # measures it (grade above C) it is an ordinary row again.
        if entry.key in E6_ARM_KEYS and entry.grade == E6_ARM_GRADE:
            continue
        if not entry.effort and entry.profile in enumerated:
            continue
        if entry.grade not in proposed:
            return None
        proposed[entry.grade].append(_profile_from_catalog(entry, templates.get(entry.key)))

    for grade, profiles in GRADE_TABLE.items():
        for profile in profiles:
            if profile.name in covered_profiles:
                continue
            proposed[grade].append(profile)

    try:
        validate_grade_table(proposed)
    except ValueError:
        print(
            "warning: handoffkeep bench catalog failed boundary validation; using the code table",
            file=sys.stderr,
        )
        return None
    return proposed


def runtime_grade_table(*, path: pathlib.Path | str | None = None) -> dict:
    """Resolve the grade table: canonical catalog first, then grades, then code.

    The layering matters during the rollout — production handoffkeep predates the
    catalog route, so a host that switches to server mode today gets a 404 on
    ``/v1/bench/catalog`` and must keep working off the pre-existing
    ``/v1/bench/grades`` projection rather than losing server placements.
    """

    from .recommend import GRADE_TABLE, validate_grade_table

    if bench_backend(use="catalog").name != BENCH_BACKEND_HANDOFFKEEP:
        return GRADE_TABLE

    # Only a view that actually came from the canon may rebuild the table. A
    # snapshot view (local, unsupported route, or stale) must fall through to the
    # grades projection and then to the code table — rebuilding from the snapshot
    # would look like a canonical answer while being a copy of the code table.
    view = read_catalog(path=path)
    if view.source in (CATALOG_SOURCE_SERVER, CATALOG_SOURCE_CACHE):
        table = _catalog_grade_table(view)
        if table is not None:
            return table

    assignments = read_grades(path=path)
    if not assignments:
        return GRADE_TABLE
    proposed = {grade: list(profiles) for grade, profiles in GRADE_TABLE.items()}
    for assignment in assignments:
        moving = [
            profile
            for profiles in proposed.values()
            for profile in profiles
            if profile.name == assignment.profile
        ]
        if not moving:
            continue
        for grade in proposed:
            proposed[grade] = [profile for profile in proposed[grade] if profile.name != assignment.profile]
        proposed[assignment.grade].extend(moving)
    try:
        validate_grade_table(proposed)
    except ValueError:
        print(
            "warning: handoffkeep bench grades failed boundary validation; using code grade table",
            file=sys.stderr,
        )
        return GRADE_TABLE
    return proposed


def grades_report(*, path: pathlib.Path | str | None = None) -> str:
    """Render server placements beside the static table and flag disagreement."""

    from .recommend import GRADE_TABLE

    code_grades: dict[str, list[str]] = {}
    for grade, profiles in GRADE_TABLE.items():
        for profile in profiles:
            placements = code_grades.setdefault(profile.name, [])
            if grade not in placements:
                placements.append(grade)
    server_grades = {item.profile: item.grade for item in read_grades(path=path)}
    profiles = sorted(set(code_grades) | set(server_grades))
    lines = ["profile server table"]
    for profile in profiles:
        server = server_grades.get(profile, "-")
        table = ",".join(code_grades.get(profile, [])) or "-"
        lines.append(f"{profile} server={server} table={table}")
        if server != "-" and server != table:
            lines.append(f"⚠ drift: {profile} server={server} table={table}")
    return "\n".join(lines)


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


def _wire_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BenchError(f"{field} must be a positive integer")
    return value


def _wire_optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BenchError(f"{field} must be an integer or null")
    return value


def _remote_rep_from_wire(value: object) -> _RemoteRep:
    if not isinstance(value, dict):
        raise BenchError("invalid handoffkeep rep row")
    server_id = _wire_positive_int(value.get("id"), "id")
    origin_id = _wire_positive_int(value.get("origin_id"), "origin_id")
    profile = _text(value.get("profile"), "profile")
    assert profile is not None
    record = RepRecord(
        id=server_id,
        profile=profile,
        model_id=_optional_text(value.get("model_id"), "model_id"),
        task_ref=_optional_text(value.get("task_ref"), "task_ref"),
        tier=_optional_text(value.get("tier"), "tier"),
        role=_optional_text(value.get("role"), "role"),
        rounds=_wire_optional_int(value.get("rounds"), "rounds"),
        blockers_found=_wire_optional_int(value.get("blockers_found"), "blockers_found"),
        completed=_wire_optional_int(value.get("completed"), "completed"),
        input_tokens=_wire_optional_int(value.get("input_tokens"), "input_tokens"),
        output_tokens=_wire_optional_int(value.get("output_tokens"), "output_tokens"),
        notes=_optional_text(value.get("notes"), "notes"),
        recorded_at=_captured_at(value.get("recorded_at")),
        effort=_optional_text(value.get("effort"), "effort"),
        grade=_optional_text(value.get("grade"), "grade"),
        table_grade=_optional_text(value.get("table_grade"), "table_grade"),
        ref=f"srv:{server_id}",
    )
    return _RemoteRep(
        record=record,
        origin_id=origin_id,
        created_by=_optional_text(value.get("created_by"), "created_by"),
        server_id=server_id,
    )


def _reps_from_payload(payload: dict) -> list[_RemoteRep]:
    values = payload.get("reps")
    if not isinstance(values, list):
        raise BenchBackendError("handoffkeep returned invalid rep data")
    try:
        return [_remote_rep_from_wire(value) for value in values]
    except BenchError as exc:
        raise BenchBackendError("handoffkeep returned invalid rep data") from exc


def _fetch_reps(backend: BenchBackend, *, query: dict[str, object] | None = None) -> list[_RemoteRep]:
    return _reps_from_payload(_handoffkeep_request(backend, "reps", query=query))


def _rep_to_wire(item: _RemoteRep) -> dict[str, object]:
    record = item.record
    return {
        "origin_id": item.origin_id,
        "profile": record.profile,
        "model_id": record.model_id,
        "task_ref": record.task_ref,
        "tier": record.tier,
        "role": record.role,
        "rounds": record.rounds,
        "blockers_found": record.blockers_found,
        "completed": record.completed,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "notes": record.notes,
        "recorded_at": record.recorded_at,
        "effort": record.effort,
        "grade": record.grade,
        "table_grade": record.table_grade,
    }


def _cached_reps(conn: sqlite3.Connection) -> list[_RemoteRep]:
    rows = conn.execute(
        "SELECT cache_key, server_id, origin_id, created_by, profile, model_id, task_ref, tier, role, "
        "rounds, blockers_found, completed, input_tokens, output_tokens, notes, recorded_at, effort, "
        "grade, table_grade FROM bench_cache_reps "
        "ORDER BY COALESCE(server_id, origin_id) DESC, cache_key DESC"
    ).fetchall()
    return [
        _RemoteRep(
            record=RepRecord(
                id=row["server_id"] if row["server_id"] is not None else row["origin_id"],
                profile=row["profile"],
                model_id=row["model_id"],
                task_ref=row["task_ref"],
                tier=row["tier"],
                role=row["role"],
                rounds=row["rounds"],
                blockers_found=row["blockers_found"],
                completed=row["completed"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                notes=row["notes"],
                recorded_at=row["recorded_at"],
                effort=row["effort"],
                grade=row["grade"],
                table_grade=row["table_grade"],
                ref=(
                    f"srv:{row['server_id']}"
                    if row["server_id"] is not None
                    else f"origin:{row['origin_id']}"
                ),
            ),
            origin_id=row["origin_id"],
            created_by=row["created_by"],
            server_id=row["server_id"],
        )
        for row in rows
    ]


def _put_cached_rep(conn: sqlite3.Connection, item: _RemoteRep) -> None:
    """Upsert one rep cache row.

    Keyed by ``(created_by, origin_id)`` per contract §3.2 — ``origin_id`` alone
    is a per-machine local rowid and collides by default across machines, so
    matching on it alone would collapse two different machines' rep #N into one
    cache row (see the client id column doc comment on ``_RemoteRep``).

    ``item.created_by`` is only ever ``None`` for an *anonymous* write-through
    echo — a just-written rep cached before its server identity was proven.
    The echo is this process's own write, but a *named* cache row sharing its
    ``origin_id`` may be another client's same-content twin: folding the echo
    into that row would pin a foreign pk onto our rep's display identity and
    erase the local write marker. An echo therefore merges only into another
    fully-unbound anonymous row for that ``origin_id`` (dedup of repeated
    unbound writes) — never into a row carrying a ``server_id``, which it
    cannot prove is its own server copy.
    The server row for our own write is folded by the *bound* echo —
    ``created_by`` + ``server_id`` copied from the post-write GET by
    ``_bind_server_ids`` — through the exact-pair branch, never anonymously.
    """
    if item.created_by is not None:
        existing = conn.execute(
            "SELECT cache_key, server_id, created_by FROM bench_cache_reps "
            "WHERE origin_id = ? AND created_by IS ? "
            "ORDER BY server_id IS NULL, cache_key "
            "LIMIT 1",
            (item.origin_id, item.created_by),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT cache_key, server_id, created_by FROM bench_cache_reps "
            "WHERE origin_id = ? AND created_by IS NULL AND server_id IS NULL "
            "ORDER BY cache_key "
            "LIMIT 1",
            (item.origin_id,),
        ).fetchone()
    if existing is not None:
        cache_key = existing["cache_key"]
    elif item.created_by is not None:
        cache_key = f"{item.created_by}:{item.origin_id}"
    elif item.server_id is not None:
        cache_key = f"server:{item.server_id}"
    else:
        cache_key = f"origin:{item.origin_id}"
    existing_server_id = existing["server_id"] if existing else None
    server_id = item.server_id if item.server_id is not None else existing_server_id
    created_by = (
        item.created_by if item.created_by is not None else (existing["created_by"] if existing else None)
    )
    record = item.record
    conn.execute(
        "INSERT INTO bench_cache_reps "
        "(cache_key, server_id, origin_id, created_by, profile, model_id, task_ref, tier, role, rounds, "
        "blockers_found, completed, input_tokens, output_tokens, notes, recorded_at, effort, grade, "
        "table_grade) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(cache_key) DO UPDATE SET server_id = excluded.server_id, "
        "origin_id = excluded.origin_id, created_by = excluded.created_by, profile = excluded.profile, "
        "model_id = excluded.model_id, task_ref = excluded.task_ref, tier = excluded.tier, "
        "role = excluded.role, "
        "rounds = excluded.rounds, blockers_found = excluded.blockers_found, completed = excluded.completed, "
        "input_tokens = excluded.input_tokens, output_tokens = excluded.output_tokens, "
        "notes = excluded.notes, "
        "recorded_at = excluded.recorded_at, effort = excluded.effort, grade = excluded.grade, "
        "table_grade = excluded.table_grade",
        (
            cache_key,
            server_id,
            item.origin_id,
            created_by,
            record.profile,
            record.model_id,
            record.task_ref,
            record.tier,
            record.role,
            record.rounds,
            record.blockers_found,
            record.completed,
            record.input_tokens,
            record.output_tokens,
            record.notes,
            record.recorded_at,
            record.effort,
            record.grade,
            record.table_grade,
        ),
    )


def _replace_cached_reps(
    conn: sqlite3.Connection, reps: list[_RemoteRep], backend: BenchBackend, now: dt.datetime
) -> None:
    conn.execute("DELETE FROM bench_cache_reps")
    for item in reps:
        _put_cached_rep(conn, item)
    _stamp_cache(conn, "reps", backend, now)


def _commit_rep_cache(
    *,
    path: pathlib.Path | str | None,
    fetched: list[_RemoteRep],
    written: list[_RemoteRep],
    backend: BenchBackend,
) -> None:
    conn = _cache_connect(path)
    try:
        now = _cache_now()
        conn.execute("BEGIN")
        _replace_cached_reps(conn, fetched, backend, now)
        for item in written:
            _put_cached_rep(conn, item)
        _stamp_cache(conn, "reps", backend, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _put_rep_batches(backend: BenchBackend, reps: list[_RemoteRep], *, batch_size: int = 500) -> None:
    for start in range(0, len(reps), batch_size):
        batch = reps[start : start + batch_size]
        payload = _handoffkeep_request(
            backend,
            "reps",
            method="PUT",
            body={"reps": [_rep_to_wire(item) for item in batch]},
        )
        accepted = payload.get("upserted")
        if isinstance(accepted, bool) or not isinstance(accepted, int) or accepted != len(batch):
            raise BenchBackendError("handoffkeep rejected a rep write")


def _bind_server_ids(
    written: list[_RemoteRep],
    fetched: list[_RemoteRep],
    *,
    backend: BenchBackend,
    window_complete: bool | None = None,
) -> list[_RemoteRep]:
    """Attach the server pk each written rep got, proven by the post-write GET.

    A PUT response carries only an upsert count — the assigned ``id`` is only
    observable through a read. The server's upsert key is ``(created_by,
    origin_id)`` and it stamps ``created_by`` itself, so the client-side echo
    cannot name its own row by key. The remote row that proves a write is the
    *unique* holder of the rep's ``origin_id`` + content — but uniqueness is
    only decidable inside a provably complete window: two clients may write
    same-content reps under the same per-machine ``origin_id``, so when the
    fetched page is full the just-written row may sit outside it while
    another client's twin sits inside. ``fetched`` must therefore come from a
    GET at ``_MIGRATE_REP_WINDOW``: a short page proves completeness. A full
    page falls back to the rep's own profile page, which still contains every
    possible twin (identical content implies identical profile); if that page
    is full too the write stays unbound — ``origin:`` is honest, a foreign pk
    is not.
    """

    by_origin: dict[int, list[_RemoteRep]] = {}
    for item in fetched:
        by_origin.setdefault(item.origin_id, []).append(item)
    if window_complete is None:
        window_complete = len(fetched) < _MIGRATE_REP_WINDOW
    profile_pages: dict[str, tuple[list[_RemoteRep], bool]] = {}

    def _profile_page(profile: str) -> tuple[list[_RemoteRep], bool]:
        if profile not in profile_pages:
            page = _fetch_reps(backend, query={"limit": _MIGRATE_REP_WINDOW, "profile": profile})
            profile_pages[profile] = (page, len(page) < _MIGRATE_REP_WINDOW)
        return profile_pages[profile]

    def _proven_match(item: _RemoteRep) -> _RemoteRep | None:
        candidates = [
            remote
            for remote in by_origin.get(item.origin_id, ())
            if _same_rep_row(item.record, remote.record)
        ]
        if len(candidates) > 1:
            return None
        if window_complete:
            return candidates[0] if candidates else None
        page, complete = _profile_page(item.record.profile)
        if not complete:
            return None
        candidates = [
            remote
            for remote in page
            if remote.origin_id == item.origin_id and _same_rep_row(item.record, remote.record)
        ]
        return candidates[0] if len(candidates) == 1 else None

    bound: list[_RemoteRep] = []
    for item in written:
        remote = _proven_match(item)
        if remote is None:
            bound.append(item)
            continue
        bound.append(
            _RemoteRep(
                record=item.record,
                origin_id=item.origin_id,
                created_by=remote.created_by,
                server_id=remote.server_id,
            )
        )
    return bound


def _write_reps_handoffkeep(
    reps: list[_RemoteRep],
    *,
    path: pathlib.Path | str | None,
    backend: BenchBackend,
) -> list[_RemoteRep]:
    """PUT the reps, then re-read so each written row's server pk is learned.

    The GET follows the PUT: the fetched set is the freshest state (it already
    carries the new rows' ``id``/``created_by``), so the commit lands them
    with ``server_id`` filled instead of an anonymous echo. If the read fails
    the error propagates without touching the cache — a retried add then
    derives the same ``origin_id`` and the server upsert lands on the same
    row, so the failure cannot leave a duplicate.
    """

    if not reps:
        return reps
    _put_rep_batches(backend, reps)
    fetched = _fetch_reps(backend, query={"limit": _MIGRATE_REP_WINDOW})
    written = _bind_server_ids(reps, fetched, backend=backend)
    try:
        _commit_rep_cache(path=path, fetched=fetched, written=written, backend=backend)
    except (sqlite3.Error, OSError) as exc:
        raise BenchBackendError("local bench cache update failed") from exc
    return written


def _filter_reps(
    rows: list[RepRecord],
    *,
    limit: int | None,
    grade: str | None,
    profile: str | None,
    effort: str | None,
) -> list[RepRecord]:
    filtered = [
        row
        for row in rows
        if (grade is None or row.grade == grade)
        and (profile is None or row.profile == profile)
        and (effort is None or row.effort == effort)
    ]
    filtered.sort(key=lambda row: row.id, reverse=True)
    return filtered[:limit] if limit is not None else filtered


def _read_reps_handoffkeep(
    *,
    path: pathlib.Path | str | None,
    limit: int | None,
    grade: str | None,
    profile: str | None,
    effort: str | None,
    backend: BenchBackend,
) -> list[RepRecord]:
    conn = _cache_connect(path)
    try:
        now = _cache_now()
        cached = _cached_reps(conn)
        fresh, age_hours = _cache_state(conn, "reps", backend, now)
    finally:
        conn.close()
    cached_for_endpoint = cached if age_hours is not None else []
    if fresh:
        rows = cached_for_endpoint
    else:
        try:
            rows = _fetch_reps(backend)
            _commit_rep_cache(path=path, fetched=rows, written=[], backend=backend)
        except (BenchBackendError, sqlite3.Error, OSError, ValueError):
            _warn_cached("reps", age_hours=age_hours, has_data=bool(cached_for_endpoint))
            rows = cached_for_endpoint
    return _filter_reps(
        [item.record for item in rows], limit=limit, grade=grade, profile=profile, effort=effort
    )


def _next_origin_id(path: pathlib.Path | str | None) -> int:
    target = pathlib.Path(path) if path is not None else db_path()
    if str(target) == ":memory:" or not target.expanduser().exists():
        return 1
    conn = _readonly_connect(target)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        values: list[int] = []
        if "reps" in tables:
            value = conn.execute("SELECT MAX(id) FROM reps").fetchone()[0]
            if isinstance(value, int):
                values.append(value)
        if "bench_cache_reps" in tables:
            value = conn.execute("SELECT MAX(origin_id) FROM bench_cache_reps").fetchone()[0]
            if isinstance(value, int):
                values.append(value)
        return max(values, default=0) + 1
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
    table_grade = derive_table_grade(profile, effort)
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

    backend = bench_backend(use="reps")
    if backend.name == BENCH_BACKEND_HANDOFFKEEP:
        origin_id = _next_origin_id(path)
        record = RepRecord(
            id=origin_id,
            profile=profile,
            model_id=model_id,
            task_ref=task_ref,
            tier=tier,
            role=role,
            rounds=rounds,
            blockers_found=blockers_found,
            completed=completed,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            notes=notes,
            recorded_at=recorded_at,
            effort=effort,
            grade=grade,
            table_grade=table_grade,
        )
        written = _write_reps_handoffkeep(
            [_RemoteRep(record=record, origin_id=origin_id)], path=path, backend=backend
        )
        remote = written[0]
        if remote.server_id is not None:
            return replace(record, id=remote.server_id, ref=f"srv:{remote.server_id}")
        return replace(record, ref=f"origin:{origin_id}")

    conn = connect(path)
    try:
        cursor = conn.execute(
            "INSERT INTO reps "
            "(profile, model_id, task_ref, tier, role, rounds, blockers_found, completed, "
            "input_tokens, output_tokens, notes, recorded_at, effort, grade, table_grade) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                table_grade,
            ),
        )
        conn.commit()
        rep_id = int(cursor.lastrowid)
        row = conn.execute(
            "SELECT id, profile, model_id, task_ref, tier, role, rounds, blockers_found, completed, "
            "input_tokens, output_tokens, notes, recorded_at, effort, grade, table_grade "
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
    backend = bench_backend(use="reps")
    if backend.name == BENCH_BACKEND_HANDOFFKEEP:
        return _read_reps_handoffkeep(
            path=path,
            limit=limit,
            grade=grade,
            profile=profile,
            effort=effort,
            backend=backend,
        )
    target = pathlib.Path(path) if path is not None else db_path()
    if str(target) != ":memory:" and not target.expanduser().exists():
        return []
    conn = connect(target)
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


def _read_local_reps_for_push(*, path: pathlib.Path | str | None = None) -> list[RepRecord]:
    """Read source rows directly, even while the configured reader is remote."""

    target = pathlib.Path(path) if path is not None else db_path()
    if str(target) != ":memory:" and not target.expanduser().exists():
        return []
    conn = _readonly_connect(target)
    try:
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reps'").fetchone()
        if table is None:
            return []
        available = {row[1] for row in conn.execute("PRAGMA table_info(reps)").fetchall()}
        columns = ", ".join(column if column in available else f"NULL AS {column}" for column in _REP_COLUMNS)
        rows = conn.execute(f"SELECT {columns} FROM reps ORDER BY id").fetchall()
        return [RepRecord(**{column: row[column] for column in _REP_COLUMNS}) for row in rows]
    finally:
        conn.close()


@dataclass(frozen=True)
class RepGradeAnnotation:
    """A backfilled task grade for one rep, linked by its canonical ref.

    Written by ``reps backfill``; read by ``grades.gather_reps``. The rep row
    itself is never modified — this table is the whole change.
    """

    rep_ref: str  # local:<id> | srv:<id>
    grade: str
    task_ref: str
    source: str
    recorded_at: str


def read_rep_grade_annotations(*, path: pathlib.Path | str | None = None) -> dict[str, RepGradeAnnotation]:
    """Every backfilled grade, keyed by canonical rep ref."""
    target = pathlib.Path(path) if path is not None else db_path()
    if str(target) != ":memory:" and not target.expanduser().exists():
        return {}
    conn = _readonly_connect(target)
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rep_grade_annotations'"
        ).fetchone()
        if table is None:
            return {}
        rows = conn.execute(
            "SELECT rep_ref, grade, task_ref, source, recorded_at FROM rep_grade_annotations"
        ).fetchall()
        return {
            row["rep_ref"]: RepGradeAnnotation(
                rep_ref=row["rep_ref"],
                grade=row["grade"],
                task_ref=row["task_ref"],
                source=row["source"],
                recorded_at=row["recorded_at"],
            )
            for row in rows
        }
    finally:
        conn.close()


def write_rep_grade_annotations(
    annotations: list[RepGradeAnnotation], *, path: pathlib.Path | str | None = None
) -> int:
    """INSERT annotations; an existing ref is a conflict, never overwritten."""
    conn = connect(path)
    try:
        for annotation in annotations:
            conn.execute(
                "INSERT INTO rep_grade_annotations (rep_ref, grade, task_ref, source, recorded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    annotation.rep_ref,
                    annotation.grade,
                    annotation.task_ref,
                    annotation.source,
                    annotation.recorded_at,
                ),
            )
        conn.commit()
        return len(annotations)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _commit_push_cache(
    *,
    path: pathlib.Path | str | None,
    fetched_scores: list[ModelScore],
    written_scores: list[ModelScore],
    fetched_reps: list[_RemoteRep],
    written_reps: list[_RemoteRep],
    score_backend: BenchBackend,
    rep_backend: BenchBackend,
) -> None:
    """Publish both migrated scopes together only after every PUT succeeded."""

    conn = _cache_connect(path)
    try:
        now = _cache_now()
        conn.execute("BEGIN")
        if written_scores:
            _replace_cached_scores(conn, fetched_scores, score_backend, now)
            for score in written_scores:
                _put_cached_score(conn, score)
            _stamp_cache(conn, "scores", score_backend, now)
        if written_reps:
            _replace_cached_reps(conn, fetched_reps, rep_backend, now)
            for item in written_reps:
                _put_cached_rep(conn, item)
            _stamp_cache(conn, "reps", rep_backend, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def push_local(*, path: pathlib.Path | str | None = None) -> tuple[int, int]:
    """Upload existing local source rows without deleting or rewriting them.

    Scores are canonical-table writes (the ``catalog`` plaintext opt-in) while
    reps follow the ``reps`` opt-in (task #697); a scope with rows requires its
    own resolved handoffkeep backend.
    """

    catalog_backend = bench_backend(use="catalog")
    reps_backend = bench_backend(use="reps")
    scores = _read_local_scores(path=path)
    reps = _read_local_reps_for_push(path=path)
    remote_reps = [_RemoteRep(record=record, origin_id=record.id) for record in reps]

    if scores and catalog_backend.name != BENCH_BACKEND_HANDOFFKEEP:
        raise BenchBackendError(
            "bench push-local: scores require the handoffkeep backend "
            f"(resolved {catalog_backend.name}/{catalog_backend.reason}; needs https, or "
            "[bench] allow_plaintext_catalog = true on a private tunnel)"
        )
    if remote_reps and reps_backend.name != BENCH_BACKEND_HANDOFFKEEP:
        raise BenchBackendError(
            "bench push-local: reps require the handoffkeep backend "
            f"(resolved {reps_backend.name}/{reps_backend.reason}; needs https, or "
            "[bench] allow_plaintext_reps = true on a private tunnel)"
        )
    # The plaintext opt-in is otherwise enforced lazily inside the request
    # layer — but the reps write now precedes its fetch, so a disallowed reps
    # endpoint must be refused up front or the scores PUT would leak through
    # before the check fires.
    if scores:
        _check_handoffkeep_scheme(
            catalog_backend.url, allow_plaintext=catalog_backend.allow_plaintext_url, use="catalog"
        )
    if remote_reps:
        _check_handoffkeep_scheme(
            reps_backend.url, allow_plaintext=reps_backend.allow_plaintext_url, use="reps"
        )

    # Fetch scores before the first PUT so a failed refresh or write leaves
    # all local cache tables and their timestamps untouched. Reps are read
    # after their PUT instead: the post-write fetch both refreshes the cache
    # and proves which server row each pushed rep became, so the committed
    # echoes fold into their own srv rows instead of sitting anonymous beside
    # them.
    fetched_scores = _fetch_scores(catalog_backend) if scores else []
    if scores:
        _put_score_batches(catalog_backend, scores)
    if remote_reps:
        _put_rep_batches(reps_backend, remote_reps)
        fetched_reps = _fetch_reps(reps_backend, query={"limit": _MIGRATE_REP_WINDOW})
        remote_reps = _bind_server_ids(remote_reps, fetched_reps, backend=reps_backend)
    else:
        fetched_reps = []
    try:
        _commit_push_cache(
            path=path,
            fetched_scores=fetched_scores,
            written_scores=scores,
            fetched_reps=fetched_reps,
            written_reps=remote_reps,
            score_backend=catalog_backend,
            rep_backend=reps_backend,
        )
    except (sqlite3.Error, OSError) as exc:
        raise BenchBackendError("local bench cache update failed") from exc
    return len(scores), len(remote_reps)


# --- task #714: one-time local bench.db reps -> handoffkeep migration --------
#
# The server keys rep dedup on ``(created_by, origin_id)`` and stamps
# ``created_by`` itself from the bearer-token identity, so two things the wire
# cannot carry are handled client-side here: the source host is recorded as a
# ``[src:<host>]`` marker appended to the migrated row's ``notes`` (the only
# free-text field the server stores verbatim), and ``origin_id`` is a stable
# hash of ``(host, local rowid)`` in a band above any real local rowid — local
# rowids collide across hosts that share one token identity, and a raw rowid
# would silently overwrite the other host's migrated rep.
_MIGRATE_SRC_RE = re.compile(r"\[src:([^\[\]]+)\]")
_MIGRATE_ORIGIN_BASE = 1 << 40
_MIGRATE_ORIGIN_SPAN = 1 << 48


@dataclass(frozen=True)
class RepMigration:
    """Outcome of ``reps migrate`` — a dry-run plan or an applied result."""

    host: str
    applied: bool
    local_count: int
    present_count: int
    pending: list[RepRecord]
    inserted_count: int
    remote_for_host: int | None
    missing: list[RepRecord]
    extra_remote_count: int
    # Pending reps whose derived origin_id is already taken on the server by a
    # different rep — a PUT would silently overwrite that row (a second machine
    # running under the same --host string is the realistic cause).
    would_overwrite: list[RepRecord]


def _migrate_src_host(notes: str | None) -> str | None:
    if not notes:
        return None
    marks = _MIGRATE_SRC_RE.findall(notes)
    return marks[-1] if marks else None


def _migrate_origin_id(host: str, profile: str, local_id: int) -> int:
    # Profile participates so the collision domain stays inside the per-profile
    # read window: a remote row with the same derived id necessarily sits under
    # a profile this run fetches completely. It also lets two machines sharing
    # a --host string coexist as long as their profiles differ.
    digest = hashlib.sha256(f"reps-migrate\x00{host}\x00{profile}\x00{local_id}".encode()).digest()
    return _MIGRATE_ORIGIN_BASE + int.from_bytes(digest[:6], "big") % _MIGRATE_ORIGIN_SPAN


def _stamp_rep_notes(notes: str | None, host: str) -> str:
    marker = f"[src:{host}]"
    return f"{notes} {marker}" if notes else marker


def _unstamp_rep_notes(notes: str | None) -> str | None:
    """Strip the last [src:<host>] marker, restoring the pre-migration notes."""

    if not notes:
        return notes
    marks = list(_MIGRATE_SRC_RE.finditer(notes))
    if not marks:
        return notes
    last = marks[-1]
    stripped = (notes[: last.start()] + notes[last.end() :]).strip()
    return stripped or None


# What the Go server accepts for time.Time JSON decoding: RFC3339 is stricter
# than fromisoformat (requires dashes, T, seconds, and a colon'd offset or Z).
_MIGRATE_RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})")


def _rep_wire_timestamp(rep: RepRecord) -> bool:
    """Whether rep.recorded_at is a timestamp handoffkeep can store."""

    if not isinstance(rep.recorded_at, str) or not _MIGRATE_RFC3339_RE.fullmatch(rep.recorded_at):
        return False
    try:
        dt.datetime.fromisoformat(rep.recorded_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


_MIGRATE_REP_WINDOW = 5000  # handoffkeep GET /v1/bench/reps limit cap (queryLimit)


def _fetch_reps_for_migrate(backend: BenchBackend, profiles: set[str]) -> list[_RemoteRep]:
    """Fetch remote reps for dedup and reconcile inside the server's GET window.

    The server returns only the newest ``limit`` rows (``ORDER BY id DESC``,
    default 1000, cap 5000). A bare GET therefore sees a tail of the store and
    would silently mis-dedup and mis-reconcile at this tool's operating scale
    (~1k local reps and growing): reconcile would list window-invisible rows
    as missing forever, and reruns would re-PUT them.

    Fetch the unfiltered window (a complete view whenever the whole store
    fits) plus one windowed page per local profile — only remote rows whose
    profile matches a local rep can dedup or reconcile against one anyway.
    A *full* per-profile page means completeness cannot be proven for that
    profile: refuse instead of writing on an unreadable remote.
    """

    seen: dict[int, _RemoteRep] = {}
    for item in _fetch_reps(backend, query={"limit": _MIGRATE_REP_WINDOW}):
        assert item.server_id is not None
        seen[item.server_id] = item
    for profile in sorted(profiles):
        page = _fetch_reps(
            backend,
            query={"limit": _MIGRATE_REP_WINDOW, "profile": profile},
        )
        if len(page) >= _MIGRATE_REP_WINDOW:
            raise BenchBackendError(
                f"reps migrate: remote reps for profile {profile!r} fill the server's "
                f"{_MIGRATE_REP_WINDOW}-row read window — remote completeness cannot be "
                "proven, so dedup and reconcile are unreliable"
            )
        for item in page:
            assert item.server_id is not None
            seen[item.server_id] = item
    return list(seen.values())


def _recorded_at_key(value: str) -> object:
    """Normalize an ISO timestamp for comparison — the server may serialize the
    same instant differently (``Z`` vs ``+00:00``) than the local row."""

    parsed = _cached_at(value)
    return parsed if parsed is not None else value


def _rep_content_key(record: RepRecord) -> tuple:
    """The dedup key minus the host: what the rep records, not where it was taken."""

    return (
        record.profile,
        record.model_id,
        record.task_ref,
        record.role,
        _recorded_at_key(record.recorded_at),
    )


def _rep_row_key(record: RepRecord) -> tuple:
    """Every rep field except the id, with recorded_at normalized to an instant."""

    return tuple(
        _recorded_at_key(record.recorded_at) if column == "recorded_at" else getattr(record, column)
        for column in _REP_COLUMNS
        if column != "id"
    )


def _same_rep_row(local: RepRecord, remote: RepRecord) -> bool:
    """Every rep field equal except the id (the remote one is a server id)."""

    return _rep_row_key(local) == _rep_row_key(remote)


def _remote_rep_host(item: _RemoteRep) -> str | None:
    """The recorded source host of a remote rep, if it was migrated."""

    return _migrate_src_host(item.record.notes)


def _rep_present_remote(
    rep: RepRecord,
    index: dict[tuple, list[_RemoteRep]],
    host: str,
) -> bool:
    """Whether ``rep`` is already on the server for this host.

    A row stamped ``[src:<host>]`` counts for that host only when it carries
    this rep's derived ``origin_id`` — key-level likeness is not enough. A row
    stamped for a *different* host still counts when it is field-for-field
    identical once its marker is stripped — the same rep migrated under a
    drifted hostname (or on a host holding an identical copy) must not be
    duplicated.
    An unstamped remote row (e.g. an earlier ``bench push-local`` copy) counts
    only when every rep field is identical, which is what makes it the same
    rep rather than a coincidence.
    """

    for item in index.get(_rep_content_key(rep), ()):
        remote_host = _remote_rep_host(item)
        if remote_host == host and item.origin_id == _migrate_origin_id(host, rep.profile, rep.id):
            # Identity, not just likeness: the stamped row must carry this
            # rep's derived origin_id. Two local reps can share a content key
            # (same profile/model/task/role/instant, different rounds) — a
            # key-only match would mask one of them missing on the server.
            return True
        candidate = (
            item.record
            if remote_host is None
            else replace(item.record, notes=_unstamp_rep_notes(item.record.notes))
        )
        if _same_rep_row(rep, candidate):
            return True
    return False


def _rep_content_index(reps: list[_RemoteRep]) -> dict[tuple, list[_RemoteRep]]:
    index: dict[tuple, list[_RemoteRep]] = {}
    for item in reps:
        index.setdefault(_rep_content_key(item.record), []).append(item)
    return index


def migrate_reps(
    *,
    path: pathlib.Path | str | None = None,
    apply: bool = False,
    host: str | None = None,
    allow_plaintext_http: bool = False,
    force: bool = False,
) -> RepMigration:
    """Upload local bench.db rep rows to the handoffkeep reps store, once.

    Dry-run unless ``apply``: both modes read the local ``reps`` table and GET
    the remote store, then report local / already-present / to-insert counts.
    ``apply`` PUTs only the missing rows (each stamped ``[src:<host>]`` in
    ``notes`` and given a stable ``origin_id`` derived from ``(host, local
    id)``), commits the read-through cache, and re-fetches for reconciliation.

    The per-use plaintext opt-in is respected: over plaintext http the command
    refuses unless ``allow_plaintext_http`` is passed for this invocation (or
    ``[bench] allow_plaintext_reps`` is already set). The flag is deliberately
    one-shot — migrating history is a separate decision from enabling
    persistent plaintext reps writes.
    """

    resolved_host = host if host is not None else socket.gethostname()
    resolved_host = _optional_text(resolved_host, "host")
    if not resolved_host or "[" in resolved_host or "]" in resolved_host:
        raise BenchError("reps migrate: could not determine source host (pass --host)")
    host = resolved_host

    backend = bench_backend(use="reps", allow_plaintext_http=allow_plaintext_http)
    if backend.name != BENCH_BACKEND_HANDOFFKEEP:
        if backend.reason == "auto-local-insecure-url":
            raise BenchBackendError(
                "reps migrate: the endpoint is plaintext http — pass --allow-plaintext-http for a "
                "one-time run, or set [bench] allow_plaintext_reps = true"
            )
        raise BenchBackendError(
            f"reps migrate requires the handoffkeep backend (resolved {backend.name}/{backend.reason}; "
            "needs HANDOFFKEEP_URL+HANDOFFKEEP_TOKEN, and https or the reps plaintext opt-in)"
        )
    if backend.url and not _plaintext_allowed(backend.url, allow_plaintext=backend.allow_plaintext_url):
        # An explicit backend = "handoffkeep" with a plaintext URL gets here —
        # refuse before any request is built rather than inside _backend_url.
        raise BenchBackendError(
            "reps migrate: the endpoint is plaintext http — pass --allow-plaintext-http for a "
            "one-time run, or set [bench] allow_plaintext_reps = true"
        )

    local_reps = _read_local_reps_for_push(path=path)
    remote = _fetch_reps_for_migrate(backend, {rep.profile for rep in local_reps})
    index = _rep_content_index(remote)
    pending = [rep for rep in local_reps if not _rep_present_remote(rep, index, host)]
    # The derived origin_id is the server's upsert key — if a pending rep's
    # target slot is already held by a different rep (another machine migrated
    # under the same host string, or local rowids were renumbered), the PUT
    # would silently overwrite it. Flagged in dry-run; refused under --apply
    # unless the operator passes --force.
    remote_by_origin = {item.origin_id: item for item in remote}
    would_overwrite = [
        rep for rep in pending if _migrate_origin_id(host, rep.profile, rep.id) in remote_by_origin
    ]
    unwritable = [rep for rep in pending if not _rep_wire_timestamp(rep)]
    if unwritable:
        shown = ", ".join(str(rep.id) for rep in unwritable[:5])
        raise BenchBackendError(
            f"reps migrate: {len(unwritable)} local rep(s) have a recorded_at handoffkeep "
            f"cannot store (RFC3339 needs a timezone offset; local ids: {shown}) — "
            "fix or delete them, then re-run"
        )
    if not apply:
        return RepMigration(
            host=host,
            applied=False,
            local_count=len(local_reps),
            present_count=len(local_reps) - len(pending),
            pending=pending,
            inserted_count=0,
            remote_for_host=None,
            missing=[],
            extra_remote_count=0,
            would_overwrite=would_overwrite,
        )
    if would_overwrite and not force:
        shown = ", ".join(str(rep.id) for rep in would_overwrite[:5])
        raise BenchBackendError(
            f"reps migrate: {len(would_overwrite)} pending rep(s) would overwrite remote rows "
            f"already held under this host's derived ids (local ids: {shown}) — likely a "
            "second machine migrated under the same --host string, or local rowids changed. "
            "Inspect, then re-run with --force only if the overwrite is intended"
        )

    written = [
        _RemoteRep(
            record=replace(rep, notes=_stamp_rep_notes(rep.notes, host)),
            origin_id=_migrate_origin_id(host, rep.profile, rep.id),
        )
        for rep in pending
    ]
    origin_ids = [item.origin_id for item in written]
    if len(set(origin_ids)) != len(origin_ids):
        raise BenchBackendError("reps migrate: derived origin_id collision — do not proceed")
    if written:
        _put_rep_batches(backend, written)

    remote_after = _fetch_reps_for_migrate(backend, {rep.profile for rep in local_reps})
    if written:
        # remote_after is provably complete for these profiles — a full
        # per-profile page raises inside _fetch_reps_for_migrate — so a
        # unique same-content match here is the migrated row itself.
        bound = _bind_server_ids(written, remote_after, backend=backend, window_complete=True)
        try:
            # Commit the post-write read: the freshly migrated rows already
            # carry their server id, so the cache shows srv: refs at once
            # instead of waiting on the refresh pass.
            _commit_rep_cache(path=path, fetched=remote_after, written=bound, backend=backend)
        except (sqlite3.Error, OSError) as exc:
            raise BenchBackendError("local bench cache update failed") from exc
    index_after = _rep_content_index(remote_after)
    missing = [rep for rep in local_reps if not _rep_present_remote(rep, index_after, host)]
    local_keys = {_rep_content_key(rep) for rep in local_reps}
    local_row_index: dict[tuple, list[int]] = {}
    for rep in local_reps:
        local_row_index.setdefault(_rep_row_key(rep), []).append(rep.id)
    # remote-this-host = rows stamped for this host + local reps only visible
    # through an identical non-host row (an earlier push-local copy, or the
    # same rep migrated under a drifted hostname). Coverage is per local rep
    # so a stamped row plus its push-local twin does not double count.
    remote_for_host = 0
    extra_remote_count = 0
    stamped_keys: set[tuple] = set()
    covered: set[int] = set()
    for item in remote_after:
        remote_host = _remote_rep_host(item)
        if remote_host == host:
            remote_for_host += 1
            stamped_keys.add(_rep_content_key(item.record))
            # A migrated row no longer matching any local rep on the content
            # key is an orphan (the local row was edited or deleted after an
            # earlier migrate) — surfaced, not silently counted as coverage.
            if _rep_content_key(item.record) not in local_keys:
                extra_remote_count += 1
            continue
        candidate = (
            item.record
            if remote_host is None
            else replace(item.record, notes=_unstamp_rep_notes(item.record.notes))
        )
        for rep_id in local_row_index.get(_rep_row_key(candidate), ()):
            covered.add(rep_id)
    remote_for_host += sum(
        1 for rep in local_reps if rep.id in covered and _rep_content_key(rep) not in stamped_keys
    )
    return RepMigration(
        host=host,
        applied=True,
        local_count=len(local_reps),
        present_count=len(local_reps) - len(pending),
        pending=pending,
        inserted_count=len(written),
        remote_for_host=remote_for_host,
        missing=missing,
        extra_remote_count=extra_remote_count,
        would_overwrite=would_overwrite,
    )


# --- task #755: one-time server_id repair for cached rep rows ----------------
#
# Cache rows written before the server pk was captured (write-through echoes
# of ``reps add``/``push-local``, and rows cached by pre-#755 migrate runs)
# show ``origin:<origin_id>`` — a host-derived key in a band far above any
# server pk — where the server row is e.g. srv:1107. This pass binds the real
# pk. A cached row may only take the id of a remote row that provably is the
# same rep: ``origin_id`` alone collides across machines by default, so
# matching on it could pin another client's pk onto this cache row.


@dataclass(frozen=True)
class RepIdRefresh:
    """Outcome of ``reps refresh-ids`` — a dry-run plan or an applied result."""

    applied: bool
    candidates: int  # cache rows missing server_id
    filled: list[tuple[str, int]]  # (cache_key, bound server_id)
    unmatched: list[str]  # cache keys whose origin_id no remote row carries
    conflicts: list[str]  # keys the server holds under a different rep's content
    ambiguous: list[str]  # keys with more than one same-content server copy
    window_blocked: list[str]  # NULL-created_by keys no fetched window can prove
    window_incomplete: bool  # unfiltered remote window full — some proofs degraded


def refresh_rep_server_ids(
    *,
    path: pathlib.Path | str | None = None,
    apply: bool = False,
    allow_plaintext_http: bool = False,
) -> RepIdRefresh:
    """Fill ``server_id`` for cached rep rows whose server copy exists.

    Dry-run unless ``apply``. The match follows the server's own upsert key:
    a cache row carrying ``created_by`` binds only the remote row under the
    same ``(created_by, origin_id)`` pair — the pair is unique server-side, so
    a visible pair row *is* that row, whatever else the window hides. An
    anonymous echo row (``created_by`` NULL — this host's own write, cached
    before its identity was known) binds only a *unique* same-content holder
    of its ``origin_id``, and uniqueness is only decidable inside a provably
    complete window: the unfiltered page, or the cache row's own profile page
    (identical content implies identical profile, so every rival is in it).
    Echoes no window can prove land in ``window_blocked`` rather than taking a
    possibly-foreign pk. Either way the rep fields must be equal, so a pk is
    never bound to a different rep — and a second run finds the rows it
    filled already excluded from the candidate set, making the pass
    idempotent.
    """

    backend = bench_backend(use="reps", allow_plaintext_http=allow_plaintext_http)
    if backend.name != BENCH_BACKEND_HANDOFFKEEP:
        raise BenchBackendError(
            f"reps refresh-ids requires the handoffkeep backend (resolved {backend.name}/{backend.reason}; "
            "needs HANDOFFKEEP_URL+HANDOFFKEEP_TOKEN, and https or the reps plaintext opt-in)"
        )

    conn = _cache_connect(path)
    try:
        candidates = conn.execute(
            "SELECT cache_key, origin_id, created_by, profile, model_id, task_ref, tier, role, rounds, "
            "blockers_found, completed, input_tokens, output_tokens, notes, recorded_at, effort, grade, "
            "table_grade FROM bench_cache_reps WHERE server_id IS NULL"
        ).fetchall()
    finally:
        conn.close()

    remote = _fetch_reps(backend, query={"limit": _MIGRATE_REP_WINDOW})
    window_incomplete = len(remote) >= _MIGRATE_REP_WINDOW
    by_origin: dict[int, list[_RemoteRep]] = {}
    for item in remote:
        by_origin.setdefault(item.origin_id, []).append(item)

    profile_pages: dict[str, tuple[list[_RemoteRep], bool]] = {}

    def _profile_page(profile: str) -> tuple[list[_RemoteRep], bool]:
        """(rows, complete) for one profile — fetched lazily, only when the
        unfiltered window cannot prove a candidate."""

        if profile not in profile_pages:
            page = _fetch_reps(backend, query={"limit": _MIGRATE_REP_WINDOW, "profile": profile})
            profile_pages[profile] = (page, len(page) < _MIGRATE_REP_WINDOW)
        return profile_pages[profile]

    def _echo_holders(row: sqlite3.Row) -> list[_RemoteRep] | None:
        """Every remote row carrying this origin_id — or None when no fetched
        window provably contains all of them."""

        holders = by_origin.get(row["origin_id"], [])
        if not window_incomplete:
            return holders
        page, complete = _profile_page(row["profile"])
        if not complete:
            return None
        return [item for item in page if item.origin_id == row["origin_id"]]

    filled: list[tuple[str, int, str | None]] = []
    unmatched: list[str] = []
    conflicts: list[str] = []
    ambiguous: list[str] = []
    window_blocked: list[str] = []
    for row in candidates:
        record = RepRecord(
            id=row["origin_id"],
            profile=row["profile"],
            model_id=row["model_id"],
            task_ref=row["task_ref"],
            tier=row["tier"],
            role=row["role"],
            rounds=row["rounds"],
            blockers_found=row["blockers_found"],
            completed=row["completed"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            notes=row["notes"],
            recorded_at=row["recorded_at"],
            effort=row["effort"],
            grade=row["grade"],
            table_grade=row["table_grade"],
        )
        if row["created_by"] is not None:
            # The server's upsert key is unique, so this can only ever be the
            # one pair row — window-completeness cannot change that. When the
            # unfiltered page misses it, the profile page may still hold it.
            pair = [
                item for item in by_origin.get(row["origin_id"], ()) if item.created_by == row["created_by"]
            ]
            if not pair and window_incomplete:
                page, complete = _profile_page(row["profile"])
                if complete:
                    pair = [
                        item
                        for item in page
                        if item.origin_id == row["origin_id"] and item.created_by == row["created_by"]
                    ]
            if not pair:
                unmatched.append(row["cache_key"])
            elif _same_rep_row(record, pair[0].record):
                remote_row = pair[0]
                assert remote_row.server_id is not None
                filled.append((row["cache_key"], remote_row.server_id, remote_row.created_by))
            else:
                conflicts.append(row["cache_key"])
            continue

        holders = _echo_holders(row)
        if holders is None:
            window_blocked.append(row["cache_key"])
            continue
        content_matches = [item for item in holders if _same_rep_row(record, item.record)]
        if len(content_matches) == 1:
            remote_row = content_matches[0]
            assert remote_row.server_id is not None  # wire rows always carry id
            filled.append((row["cache_key"], remote_row.server_id, remote_row.created_by))
        elif len(content_matches) > 1:
            ambiguous.append(row["cache_key"])
        elif holders:
            conflicts.append(row["cache_key"])
        else:
            unmatched.append(row["cache_key"])

    if apply and filled:
        conn = _cache_connect(path)
        try:
            conn.execute("BEGIN")
            for cache_key, server_id, created_by in filled:
                conn.execute(
                    "UPDATE bench_cache_reps SET server_id = ?, created_by = ? WHERE cache_key = ?",
                    (server_id, created_by, cache_key),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return RepIdRefresh(
        applied=apply,
        candidates=len(candidates),
        filled=[(cache_key, server_id) for cache_key, server_id, _ in filled],
        unmatched=unmatched,
        conflicts=conflicts,
        ambiguous=ambiguous,
        window_blocked=window_blocked,
        window_incomplete=window_incomplete,
    )


_REP_GRADE_ORDER: tuple[str, ...] = ("S+", "S", "A+", "A", "B", "C")


def _format_rep_grade(grade: str | None, table_grade: str | None) -> str:
    if not grade:
        return "-"
    if (
        not table_grade
        or grade == table_grade
        or grade not in _REP_GRADE_ORDER
        or table_grade not in _REP_GRADE_ORDER
    ):
        return grade
    idx_grade = _REP_GRADE_ORDER.index(grade)
    idx_table = _REP_GRADE_ORDER.index(table_grade)
    if idx_grade < idx_table:
        direction = "↑"
    elif idx_grade > idx_table:
        direction = "↓"
    else:
        return grade
    return f"{grade}(표{table_grade}{direction})"


def format_rep(rep: RepRecord) -> str:
    fields = [
        f"id={rep_ref(rep)}",
        f"profile={rep.profile}",
        f"effort={rep.effort or '-'}",
        f"grade={_format_rep_grade(rep.grade, rep.table_grade)}",
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
        upward_count = 0
        same_count = 0
        downward_count = 0
        for row in group:
            if (
                row.grade is not None
                and row.table_grade is not None
                and row.grade in _REP_GRADE_ORDER
                and row.table_grade in _REP_GRADE_ORDER
            ):
                idx_grade = _REP_GRADE_ORDER.index(row.grade)
                idx_table = _REP_GRADE_ORDER.index(row.table_grade)
                if idx_grade < idx_table:
                    upward_count += 1
                elif idx_grade > idx_table:
                    downward_count += 1
                else:
                    same_count += 1
            else:
                same_count += 1

        comparisons.append(
            RepComparison(
                profile=profile_name,
                count=len(group),
                average_rounds=_average(row.rounds for row in group),
                average_blockers_found=_average(row.blockers_found for row in group),
                completion_rate=(sum(row.completed == 1 for row in group) / len(group)) * 100.0,
                average_input_tokens=_average(row.input_tokens for row in group),
                average_output_tokens=_average(row.output_tokens for row in group),
                upward_count=upward_count,
                same_count=same_count,
                downward_count=downward_count,
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
    fields.append(
        f"상방 {comparison.upward_count}건 / "
        f"동급 {comparison.same_count}건 / "
        f"하방 {comparison.downward_count}건"
    )
    return " ".join(fields)


def format_score_as_json(score: ModelScore) -> str:
    """Small test/CLI helper with no credential-bearing fields."""

    return json.dumps(score.as_dict(), ensure_ascii=False, sort_keys=True)
