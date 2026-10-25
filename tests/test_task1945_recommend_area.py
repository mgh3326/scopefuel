"""hk 1945 (K6) — ``--recommend GRADE --area A``.

The fixed design is hk:doc reports/b953/hk1938-kind-grading-packet section 4c:
an area exception never changes the grade table. It is read from the bench
canon (``GET /v1/bench/area-exceptions?include_retired=1``), cached and
refreshed like the catalog, and applied only after today's ranking is already
computed:

* an ``exclude`` row removes matching candidates and folds them onto the
  existing excluded line with ``area:A exclude (decision_ref)``;
* a ``prefer`` row lifts matching exact-grade candidates to the front of their
  own grade, never above a row placed at a better grade;
* a row whose ``expires_with_model_id`` no longer matches the catalog model id
  of its ``(profile, effort)`` is expired: never applied, named in --explain;
* without ``--area`` nothing is fetched, applied or added to the output, so
  text and JSON stay byte-identical to main (the main regression guard).

Mutant invariants this file pins (each turns a named test RED):

* apply exceptions without --area -> test_area_exceptions_are_ignored_without_area
* let prefer cross a better grade -> test_prefer_never_moves_above_a_better_grade
* apply an expired row -> test_expired_row_is_not_applied_and_is_explained
* put excluded rows in a new section -> test_exclude_folds_onto_the_existing_line
"""

from __future__ import annotations

import copy
import datetime as dt
import functools
import json
import pathlib
import re
import urllib.parse
from collections import Counter

import pytest

from scopefuel import bench, cli, recommend
from scopefuel.http import HttpError
from scopefuel.model import Bucket, ProviderResult, Scope

TODAY = dt.date(2026, 10, 25)
NOW = dt.datetime(2026, 10, 25, 12, 0, 0, tzinfo=dt.UTC)

AREAS = [
    "blender",
    "unity",
    "web-qa",
    "ui-design",
    "image",
    "web-frontend",
    "go-backend",
    "python",
    "trading",
    "shell-wrk",
    "docs",
    "research",
    "ops",
]

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "hk1945"

# Model ids the bundled snapshot carries for the profiles these tests use.
MODEL_ID = {
    ("opus", "high"): "claude-opus-5-5",
    ("opus", "low"): "claude-opus-5-5",
    ("sonnet", "high"): "claude-sonnet-5-5",
    ("grok-hi", ""): "grok-4.7",
    ("grok-hi", "xhigh"): "grok-4.7",
    ("grok", "medium"): "grok-4.7",
    ("kiro-opus", ""): "claude-opus-5",
    ("kiro-sonnet", ""): "claude-sonnet-5",
    ("codex-luna", "xhigh"): "gpt-6-luna",
    ("devin-ds41", ""): "deepseek-v4-1-flash-high",
}


def _seed_rows():
    """The full bundled snapshot — a fetched catalog must cover every profile."""

    return [entry.as_dict() for entry in bench.catalog_snapshot()]


def _row(profile, effort, model_id, pool, grade, **overrides):
    row = {
        "profile": profile,
        "effort": effort,
        "model_id": model_id,
        "pool": pool,
        "grade": grade,
        "score": None,
        "gate": "default",
        "gate_reason": None,
        "benchmark_source": None,
        "benchmark_annotation": None,
        "boundary_version": "2026-10-25",
        "deviation_ref": "hk:doc/reports/b953/hk1938-kind-grading-packet",
        "decided_at": "2026-10-25T00:00:00Z",
        "decided_by": "operator-desk",
        "retired_at": None,
    }
    row.update(overrides)
    return row


def _seed_rows_with(profile, effort, **changes):
    rows = []
    for row in _seed_rows():
        if row["profile"] == profile and row["effort"] == effort:
            row = {**row, **changes}
        rows.append(row)
    return rows


def _exception(
    profile,
    effort,
    area,
    action,
    decision_ref="hk:doc/decision/2026-10-25/area-example",
    expires_with_model_id=None,
    evidence_refs=("hk:task/1945",),
    retired_at=None,
):
    return {
        "profile": profile,
        "effort": effort,
        "area": area,
        "action": action,
        "evidence_refs": list(evidence_refs),
        "decision_ref": decision_ref,
        "decided_by": "operator-desk",
        "decided_at": "2026-10-25T00:00:00Z",
        "expires_with_model_id": (
            MODEL_ID[(profile, effort)] if expires_with_model_id is None else expires_with_model_id
        ),
        "retired_at": retired_at,
    }


class FakeAreaHK:
    """One fake handoffkeep: bench catalog, K1 vocabulary and K5 exceptions."""

    def __init__(self):
        self.catalog = _seed_rows()
        self.exceptions: list[dict] = []
        self.area_vocab: list[str] | None = list(AREAS)
        self.hits: Counter[tuple[str, str]] = Counter()
        self.offline = False
        self.fail_status: int | None = None
        self.urls: list[str] = []

    def request_json(self, url, *, method="GET", headers=None, body=None, timeout=20.0, **_kw):
        assert headers is not None
        assert headers["Authorization"] == "Bearer test-token"
        parts = urllib.parse.urlsplit(url)
        path = parts.path
        query = urllib.parse.parse_qs(parts.query)
        self.hits[(method, path)] += 1
        self.urls.append(url)
        if self.offline:
            raise OSError("offline")
        if self.fail_status is not None:
            raise HttpError(self.fail_status, "failed")
        if path == "/v1/task-areas":
            if self.area_vocab is None:
                raise HttpError(404, "not found")
            return {"areas": list(self.area_vocab)}
        if path == "/v1/bench/catalog":
            rows = self.catalog
            if query.get("include_retired") != ["1"]:
                rows = [row for row in rows if not row.get("retired_at")]
            return {"catalog": copy.deepcopy(rows)}
        if path == "/v1/bench/scores":
            return {"scores": []}
        if path == "/v1/bench/area-exceptions":
            rows = self.exceptions
            if query.get("include_retired") != ["1"]:
                rows = [row for row in rows if not row.get("retired_at")]
            return {"exceptions": copy.deepcopy(rows)}
        raise AssertionError(f"unexpected URL: {url}")


@pytest.fixture
def area_server(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data_home = tmp_path / "data"
    config_home = tmp_path / "config"
    cache_home = tmp_path / "cache"
    state_home = tmp_path / "state"
    for path in (home, data_home, config_home, cache_home, state_home):
        path.mkdir(parents=True, exist_ok=True)
    config_file = config_home / "scopefuel" / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        '[bench]\nbackend = "handoffkeep"\ncache_ttl_s = 21600\n'
        "catalog_ttl_s = 3600\ncatalog_stale_max_s = 86400\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("HANDOFFKEEP_URL", "https://hk.test")
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", "test-token")
    fake = FakeAreaHK()
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    bench.reset_catalog_memo()
    return data_home, fake


def _age_scope(seconds: float, scope: str) -> None:
    import sqlite3

    stamp = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds)).isoformat()
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute("UPDATE bench_cache_meta SET fetched_at = ? WHERE scope = ?", (stamp, scope))
        conn.commit()
    finally:
        conn.close()
    bench.reset_catalog_memo()


# ---------------------------------------------------------------------------
# Transport and cache: the same TTL, last-good and 429 rules as the catalog
# ---------------------------------------------------------------------------


def test_task_areas_are_cached_inside_the_catalog_ttl(area_server):
    _, fake = area_server
    first = bench.read_task_areas()
    assert first.areas == tuple(AREAS)
    assert first.source == "server"
    assert fake.hits[("GET", "/v1/task-areas")] == 1

    fake.area_vocab = ["blender"]
    bench.reset_catalog_memo()
    cached = bench.read_task_areas()
    assert cached.source == "cache"
    assert cached.areas == tuple(AREAS)
    assert fake.hits[("GET", "/v1/task-areas")] == 1


def test_task_areas_last_good_survives_a_429_and_is_not_retried(area_server):
    _, fake = area_server
    assert bench.read_task_areas().source == "server"
    _age_scope(3601, "task-areas")
    fake.fail_status = 429

    last_good = bench.read_task_areas()
    assert last_good.areas == tuple(AREAS)
    assert last_good.source in ("cache", "cache-stale")
    assert fake.hits[("GET", "/v1/task-areas")] == 2

    # The 429 did not refresh the stamp or replace the rows: once the server is
    # back the still-expired cache is refetched, never served as a 429 victim.
    fake.fail_status = None
    bench.reset_catalog_memo()
    served = bench.read_task_areas()
    assert served.areas == tuple(AREAS)
    assert served.source == "server"
    assert fake.hits[("GET", "/v1/task-areas")] == 3


def test_task_areas_without_a_route_or_cache_are_unavailable(area_server):
    _, fake = area_server
    fake.area_vocab = None
    view = bench.read_task_areas()
    assert view.areas is None
    assert view.source == "unsupported"


def test_area_exceptions_request_retired_rows_and_cache_them(area_server):
    _, fake = area_server
    fake.exceptions = [
        _exception("opus", "high", "blender", "exclude"),
        _exception(
            "sonnet",
            "high",
            "blender",
            "prefer",
            retired_at="2026-10-25T01:00:00Z",
        ),
    ]
    first = bench.read_area_exceptions()
    assert first.source == "server"
    assert {(row.profile, row.effort) for row in first.entries} == {("opus", "high"), ("sonnet", "high")}
    assert any("include_retired=1" in url for url in fake.urls)

    fake.exceptions = []
    bench.reset_catalog_memo()
    cached = bench.read_area_exceptions()
    assert cached.source == "cache"
    assert len(cached.entries) == 2
    assert fake.hits[("GET", "/v1/bench/area-exceptions")] == 1


def test_area_exceptions_last_good_survives_a_429(area_server):
    _, fake = area_server
    fake.exceptions = [_exception("opus", "high", "blender", "exclude")]
    assert bench.read_area_exceptions().source == "server"
    _age_scope(3601, "area-exceptions")
    fake.fail_status = 429

    last_good = bench.read_area_exceptions()
    assert [(row.profile, row.effort) for row in last_good.entries] == [("opus", "high")]
    assert last_good.source in ("cache", "cache-stale")
    assert fake.hits[("GET", "/v1/bench/area-exceptions")] == 2


def test_area_exceptions_without_a_route_are_empty(area_server, monkeypatch):
    _, fake = area_server

    def request_json(url, **kwargs):
        if urllib.parse.urlsplit(url).path == "/v1/bench/area-exceptions":
            raise HttpError(404, "not found")
        return fake.request_json(url, **kwargs)

    monkeypatch.setattr(bench, "request_json", request_json)
    view = bench.read_area_exceptions()
    assert view.entries == ()
    assert view.source == "unsupported"


# --- round 2 (B2): an empty exception list is a valid cached value ----------


def test_empty_area_exception_list_is_cached_inside_the_ttl(area_server):
    """Mutant: truthiness check back -> this test RED (second read refetches)."""

    _, fake = area_server
    fake.exceptions = []
    first = bench.read_area_exceptions()
    assert first.source == "server"
    assert first.entries == ()
    assert fake.hits[("GET", "/v1/bench/area-exceptions")] == 1

    # A server-side change inside the TTL must not be picked up: the empty
    # cached list is a value, not the absence of one.
    fake.exceptions = [_exception("opus", "high", "blender", "exclude")]
    bench.reset_catalog_memo()
    cached = bench.read_area_exceptions()
    assert cached.source == "cache"
    assert cached.entries == ()
    assert fake.hits[("GET", "/v1/bench/area-exceptions")] == 1


def test_empty_area_exception_list_survives_a_429(area_server):
    _, fake = area_server
    fake.exceptions = []
    assert bench.read_area_exceptions().source == "server"
    _age_scope(3601, "area-exceptions")
    fake.fail_status = 429

    last_good = bench.read_area_exceptions()
    assert last_good.entries == ()
    assert last_good.source in ("cache", "cache-stale")
    # One initial request plus the failed refetch — no retry inside the read.
    assert fake.hits[("GET", "/v1/bench/area-exceptions")] == 2


def test_corrupt_area_exception_cache_is_refetched(area_server):
    _, fake = area_server
    fake.exceptions = [_exception("opus", "high", "blender", "exclude")]
    assert bench.read_area_exceptions().source == "server"

    import sqlite3

    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute("UPDATE bench_cache_area_exceptions SET evidence_refs = ?", ("not-json",))
        conn.commit()
    finally:
        conn.close()
    bench.reset_catalog_memo()
    fake.exceptions = [_exception("sonnet", "high", "blender", "prefer")]
    view = bench.read_area_exceptions()
    assert view.source == "server"
    assert [row.profile for row in view.entries] == ["sonnet"]
    assert fake.hits[("GET", "/v1/bench/area-exceptions")] == 2


# ---------------------------------------------------------------------------
# Ranking inputs
# ---------------------------------------------------------------------------


def _bucket(used: float, window: str = "7d", scope: Scope | None = None) -> Bucket:
    return Bucket(
        label=window,
        window=window,
        used_pct=used,
        resets_at=None,
        scope=scope or Scope("account"),
        horizon="week",
    )


def _result(pool: str, used: float) -> ProviderResult:
    buckets = [_bucket(used)]
    if pool == "agy":
        buckets.append(_bucket(used, scope=Scope("group", "gemini")))
    return ProviderResult(id=pool, pool_class="spend", buckets=buckets)


POOL_USED = {
    "claude": 10.0,
    "codex": 20.0,
    "kiro": 30.0,
    "kimi": 40.0,
    "grok": 15.0,
    "agy": 25.0,
    "clinepass": 35.0,
    "devin": 5.0,
    "upstage": 45.0,
}


def _providers(**overrides: float) -> list[ProviderResult]:
    used = {**POOL_USED, **overrides}
    return [_result(pool, used[pool]) for pool in POOL_USED]


def _table():
    return bench.runtime_grade_table()


def _model_ids() -> dict[tuple[str, str], str]:
    return {(entry.profile, entry.effort): entry.model_id for entry in bench.read_catalog().entries}


def _ranked(out: str) -> list[str]:
    return [line for line in out.splitlines() if line[:1].isdigit()]


def _ranked_bodies(out: str) -> list[str]:
    return [re.sub(r"^\d+\. ", "", line) for line in _ranked(out)]


def _rank_of(out: str, label: str) -> int | None:
    for index, line in enumerate(_ranked(out)):
        if label in line:
            return index
    return None


def _rec(grade, **kwargs):
    return recommend.recommend(
        _providers(**kwargs.pop("provider_used", {})),
        grade,
        today=TODAY,
        now=NOW,
        grade_table=_table(),
        bench_scores=None,
        model_prices={},
        **kwargs,
    )


def _rec_dict(grade, **kwargs):
    return recommend.recommend_dict(
        _providers(**kwargs.pop("provider_used", {})),
        grade,
        today=TODAY,
        now=NOW,
        grade_table=_table(),
        bench_scores=None,
        model_prices={},
        **kwargs,
    )


def _exc(*args, **kwargs) -> bench.AreaException:
    raw = _exception(*args, **kwargs)
    return bench._area_exception_from_wire(raw)


# ---------------------------------------------------------------------------
# Without --area: the main regression guard
# ---------------------------------------------------------------------------


def test_area_exceptions_are_ignored_without_area(area_server):
    """Mutant: apply exceptions without --area -> this test RED."""

    _, fake = area_server
    fake.exceptions = [
        _exception("opus", "high", "blender", "exclude"),
        _exception("sonnet", "high", "blender", "prefer"),
    ]
    baseline = _rec("S+")
    same = _rec(
        "S+",
        area_exceptions=[
            _exc("opus", "high", "blender", "exclude"),
            _exc("sonnet", "high", "blender", "prefer"),
        ],
        catalog_model_ids=_model_ids(),
    )
    assert same == baseline

    # --explain and --hide-excluded are part of the same byte-identical guard.
    quiet_baseline = _rec("S+", explain=True, hide_excluded=True)
    quiet_same = _rec(
        "S+",
        explain=True,
        hide_excluded=True,
        area_exceptions=[_exc("opus", "high", "blender", "exclude")],
        catalog_model_ids=_model_ids(),
    )
    assert quiet_same == quiet_baseline


@pytest.mark.parametrize("grade", ["S+", "A+", "A"])
def test_without_area_text_matches_the_main_golden(area_server, grade):
    _, fake = area_server
    fake.exceptions = [
        _exception("opus", "high", "blender", "exclude"),
        _exception("sonnet", "high", "blender", "prefer"),
        _exception("grok", "medium", "blender", "exclude", expires_with_model_id="grok-4.6"),
    ]
    golden = (FIXTURES / f"{grade.replace('+', 'p')}-text.txt").read_text(encoding="utf-8")
    assert _rec(grade) == golden


@pytest.mark.parametrize("grade", ["S+", "A+", "A"])
def test_without_area_json_matches_the_main_golden(area_server, grade):
    """Mutant: always emit area:null -> this byte comparison RED."""

    _, fake = area_server
    fake.exceptions = [
        _exception("opus", "high", "blender", "exclude"),
        _exception("sonnet", "high", "blender", "prefer"),
    ]
    golden = json.loads((FIXTURES / f"{grade.replace('+', 'p')}-json.json").read_text(encoding="utf-8"))
    assert _rec_dict(grade) == golden
    assert "area" not in _rec_dict(grade)


def test_cli_without_area_makes_no_area_requests(area_server, monkeypatch, capsys):
    _, fake = area_server
    fake.exceptions = [_exception("opus", "high", "blender", "exclude")]
    monkeypatch.setattr(cli, "registry", lambda: {})
    assert cli.main(["--recommend", "A+"]) == 0
    capsys.readouterr()
    assert fake.hits[("GET", "/v1/bench/area-exceptions")] == 0
    assert fake.hits[("GET", "/v1/task-areas")] == 0


# ---------------------------------------------------------------------------
# Exclude: folds onto the existing excluded line
# ---------------------------------------------------------------------------


def test_exclude_folds_onto_the_existing_line(area_server):
    """Mutant: a new output section instead of the fold -> this test RED."""

    _ = area_server
    baseline = _rec("S+")
    assert _rank_of(baseline, "opus --effort high") is not None

    out = _rec(
        "S+",
        area="blender",
        area_exceptions=[_exc("opus", "high", "blender", "exclude", decision_ref="hk:doc/d1")],
        catalog_model_ids=_model_ids(),
    )
    assert _rank_of(out, "opus --effort high") is None
    fold = [line for line in out.splitlines() if line.startswith("✗ 제외")]
    assert len(fold) == 1
    assert "opus@high — area:blender exclude (hk:doc/d1)" in fold[0]
    assert not any("area:blender exclude" in line for line in _ranked(out))


def test_exclude_with_empty_effort_matches_every_effort(area_server):
    _ = area_server
    at_s = _rec(
        "S",
        area="blender",
        area_exceptions=[_exc("grok-hi", "", "blender", "exclude", decision_ref="hk:doc/d2")],
        catalog_model_ids=_model_ids(),
    )
    assert _rank_of(at_s, "grok-hi") is None
    assert "grok-hi@default — area:blender exclude (hk:doc/d2)" in at_s

    at_a = _rec(
        "A",
        area="blender",
        area_exceptions=[_exc("grok-hi", "", "blender", "exclude", decision_ref="hk:doc/d2")],
        catalog_model_ids=_model_ids(),
    )
    assert _rank_of(at_a, "grok-hi --effort xhigh") is None
    assert "grok-hi@xhigh — area:blender exclude (hk:doc/d2)" in at_a


def test_retired_exception_rows_are_not_applied(area_server):
    _ = area_server
    out = _rec(
        "S+",
        area="blender",
        area_exceptions=[
            _exc(
                "opus",
                "high",
                "blender",
                "exclude",
                retired_at="2026-10-25T01:00:00Z",
            )
        ],
        catalog_model_ids=_model_ids(),
    )
    assert _rank_of(out, "opus --effort high") is not None


# --- round 2 (B1): an effort-empty row expires per candidate effort ---------


def test_effort_empty_exclude_on_profile_without_default_row_removes_every_effort(area_server):
    """Mutant: lookup back to (profile, empty effort) -> this test RED."""

    _ = area_server
    assert _model_ids().get(("opus", "")) is None  # opus has no default-effort row
    out = _rec(
        "S+",
        area="blender",
        area_exceptions=[
            _exc(
                "opus",
                "",
                "blender",
                "exclude",
                decision_ref="hk:doc/b1a",
                expires_with_model_id="claude-opus-5-5",
            )
        ],
        catalog_model_ids=_model_ids(),
    )
    for effort in ("medium", "high", "xhigh"):
        assert _rank_of(out, f"opus --effort {effort}") is None
    fold = next(line for line in out.splitlines() if line.startswith("✗ 제외"))
    for effort in ("medium", "high", "xhigh"):
        assert f"opus@{effort} — area:blender exclude (hk:doc/b1a)" in fold


def test_effort_empty_exclude_is_per_effort_when_one_model_id_moved(area_server):
    _, fake = area_server
    _table()  # prime the catalog cache before ageing it
    fake.catalog = _seed_rows_with("opus", "high", model_id="claude-opus-6")
    _age_scope(3601, "catalog")

    out = _rec(
        "S+",
        explain=True,
        area="blender",
        area_exceptions=[
            _exc(
                "opus",
                "",
                "blender",
                "exclude",
                decision_ref="hk:doc/b1b",
                expires_with_model_id="claude-opus-5-5",
            )
        ],
        catalog_model_ids=_model_ids(),
    )
    # The moved effort is inactive and stays ranked; the matching efforts are
    # excluded and folded.
    assert _rank_of(out, "opus --effort high") is not None
    assert _rank_of(out, "opus --effort medium") is None
    assert _rank_of(out, "opus --effort xhigh") is None
    fold = next(line for line in out.splitlines() if line.startswith("✗ 제외"))
    assert "opus@medium — area:blender exclude (hk:doc/b1b)" in fold
    assert "opus@xhigh — area:blender exclude (hk:doc/b1b)" in fold
    assert "opus@high" not in fold
    expired = [line for line in out.splitlines() if "area exception" in line and "expired" in line]
    assert any(
        "key=opus@any" in line and "effort=high" in line and "catalog_model_id=claude-opus-6" in line
        for line in expired
    )


def test_effort_empty_prefer_lifts_only_efforts_whose_model_id_matches(area_server):
    _, fake = area_server
    _table()
    fake.catalog = _seed_rows_with("opus", "high", model_id="claude-opus-6")
    _age_scope(3601, "catalog")

    out = _rec(
        "S+",
        explain=True,
        area="blender",
        area_exceptions=[
            _exc(
                "opus",
                "",
                "blender",
                "prefer",
                decision_ref="hk:doc/b1c",
                expires_with_model_id="claude-opus-5-5",
            )
        ],
        catalog_model_ids=_model_ids(),
    )
    ranked = _ranked(out)
    assert "opus --effort medium" in ranked[0]
    assert "opus --effort xhigh" in ranked[1]
    assert "opus --effort high" not in ranked[0] and "opus --effort high" not in ranked[1]
    expired = [line for line in out.splitlines() if "area exception" in line and "expired" in line]
    assert any("key=opus@any" in line and "effort=high" in line for line in expired)


# ---------------------------------------------------------------------------
# Prefer: front of its own grade only
# ---------------------------------------------------------------------------


def test_prefer_moves_to_the_front_of_its_grade(area_server):
    _ = area_server
    baseline = _rec("S+", provider_used={"claude": 80.0})
    assert _rank_of(baseline, "opus --effort high") != 0

    out = _rec(
        "S+",
        provider_used={"claude": 80.0},
        area="blender",
        area_exceptions=[_exc("opus", "high", "blender", "prefer", decision_ref="hk:doc/d3")],
        catalog_model_ids=_model_ids(),
    )
    assert _rank_of(out, "opus --effort high") == 0
    # Everything else keeps its relative order (rank numbers renumber).
    others_before = [line for line in _ranked_bodies(baseline) if "opus --effort high" not in line]
    others_after = [line for line in _ranked_bodies(out) if "opus --effort high" not in line]
    assert others_after == others_before


def test_prefer_never_moves_above_a_better_grade(area_server):
    """Mutant: lift to index 0 instead of the own-grade block -> RED."""

    _ = area_server
    # claude is nearly idle, so the S-placed borrowed opus@low row ranks first
    # at A+; the exact A+ grok@medium row must stop below it.
    used = {pool: 50.0 for pool in POOL_USED}
    used["claude"] = 1.0
    providers = _providers(**used)
    baseline = recommend.recommend(
        providers,
        "A+",
        today=TODAY,
        now=NOW,
        grade_table=_table(),
        bench_scores=None,
        model_prices={},
    )
    assert "opus --effort low" in _ranked(baseline)[0]

    out = recommend.recommend(
        providers,
        "A+",
        today=TODAY,
        now=NOW,
        grade_table=_table(),
        bench_scores=None,
        model_prices={},
        area="blender",
        area_exceptions=[_exc("grok", "medium", "blender", "prefer", decision_ref="hk:doc/d4")],
        catalog_model_ids=_model_ids(),
    )
    ranked = _ranked(out)
    better = next(index for index, line in enumerate(ranked) if "opus --effort low" in line)
    preferred = next(index for index, line in enumerate(ranked) if "grok --effort medium" in line)
    assert preferred > better
    assert preferred == better + 1


def test_prefer_cannot_lift_a_quota_exhausted_profile(area_server):
    _ = area_server
    out = _rec(
        "A",
        provider_used={"claude": 99.0},
        area="blender",
        area_exceptions=[_exc("sonnet", "high", "blender", "prefer", decision_ref="hk:doc/d5")],
        catalog_model_ids=_model_ids(),
    )
    assert _rank_of(out, "sonnet --effort high") is None
    assert "sonnet@high" in next(line for line in out.splitlines() if line.startswith("✗ 제외"))


def test_prefer_cannot_lift_an_unsubscribed_profile(area_server):
    _ = area_server
    from scopefuel import policy

    path = policy.config_path()
    path.write_text(
        path.read_text(encoding="utf-8") + "\n[profiles.opus]\nsubscribed = false\n",
        encoding="utf-8",
    )
    out = _rec(
        "S+",
        area="blender",
        area_exceptions=[_exc("opus", "high", "blender", "prefer", decision_ref="hk:doc/d5b")],
        catalog_model_ids=_model_ids(),
    )
    assert _rank_of(out, "opus --effort high") is None
    fold = next(line for line in out.splitlines() if line.startswith("✗ 제외"))
    assert "opus@high" in fold and "unsubscribed" in fold


def test_exception_for_a_retired_catalog_row_has_no_effect(area_server):
    _, fake = area_server
    _table()  # prime the catalog cache before ageing it
    fake.catalog = _seed_rows_with("opus", "high", retired_at="2026-10-25T01:00:00Z")
    _age_scope(3601, "catalog")

    baseline = _rec("S+")
    assert _rank_of(baseline, "opus --effort high") is None
    out = _rec(
        "S+",
        area="blender",
        area_exceptions=[_exc("opus", "high", "blender", "exclude", decision_ref="hk:doc/d5c")],
        catalog_model_ids=_model_ids(),
    )
    assert out == baseline
    explained = _rec(
        "S+",
        explain=True,
        area="blender",
        area_exceptions=[_exc("opus", "high", "blender", "exclude", decision_ref="hk:doc/d5c")],
        catalog_model_ids=_model_ids(),
    )
    assert not any("area exception" in line for line in explained.splitlines())


def test_exclude_beats_prefer(area_server):
    _ = area_server
    out = _rec(
        "A",
        area="blender",
        area_exceptions=[
            _exc("sonnet", "high", "blender", "exclude", decision_ref="hk:doc/d6"),
            _exc("sonnet", "high", "blender", "prefer", decision_ref="hk:doc/d6"),
        ],
        catalog_model_ids=_model_ids(),
    )
    assert _rank_of(out, "sonnet --effort high") is None
    assert "area:blender exclude (hk:doc/d6)" in out


# ---------------------------------------------------------------------------
# Expired rows
# ---------------------------------------------------------------------------


def test_expired_row_is_not_applied_and_is_explained(area_server):
    """Mutant: apply an expired row -> this test RED."""

    _ = area_server
    out = _rec(
        "A",
        explain=True,
        area="blender",
        area_exceptions=[
            _exc(
                "sonnet",
                "high",
                "blender",
                "exclude",
                decision_ref="hk:doc/d7",
                expires_with_model_id="claude-sonnet-5-4",
                evidence_refs=("hk:task/1945", "srv:7"),
            )
        ],
        catalog_model_ids=_model_ids(),
    )
    assert _rank_of(out, "sonnet --effort high") is not None
    line = next(line for line in out.splitlines() if "area exception" in line)
    assert "expired" in line
    assert "sonnet@high" in line
    assert "exclude" in line
    assert "hk:doc/d7" in line
    assert "hk:task/1945" in line and "srv:7" in line


def test_applied_exception_is_explained(area_server):
    _ = area_server
    out = _rec(
        "S+",
        explain=True,
        area="blender",
        area_exceptions=[
            _exc("opus", "high", "blender", "prefer", decision_ref="hk:doc/d8", evidence_refs=("srv:9",))
        ],
        catalog_model_ids=_model_ids(),
    )
    line = next(line for line in out.splitlines() if "area exception" in line)
    assert "expired" not in line
    assert "opus@high" in line
    assert "prefer" in line
    assert "hk:doc/d8" in line and "srv:9" in line


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_json_area_field_and_adjustments(area_server):
    _ = area_server
    payload = _rec_dict(
        "A",
        area="blender",
        area_exceptions=[
            _exc("sonnet", "high", "blender", "prefer", decision_ref="hk:doc/d9"),
            _exc("grok-hi", "xhigh", "blender", "exclude", decision_ref="hk:doc/d10"),
        ],
        catalog_model_ids=_model_ids(),
    )
    assert payload["area"] == "blender"
    sonnet = next(row for row in payload["rows"] if row["profile"] == "sonnet" and row["kind"] == "candidate")
    assert sonnet["area_adjustment"] == {"action": "prefer", "decision_ref": "hk:doc/d9"}
    grok = next(
        row for row in payload["rows"] if row["profile"] == "grok-hi" and row["kind"] == "area_exclude"
    )
    assert grok["area_adjustment"] == {"action": "exclude", "decision_ref": "hk:doc/d10"}
    folded = next(item for item in payload["excluded"] if item["reason"].startswith("area:blender exclude"))
    assert folded["reason"] == "area:blender exclude (hk:doc/d10)"
    assert folded["area_adjustment"] == {"action": "exclude", "decision_ref": "hk:doc/d10"}
    for row in payload["rows"]:
        if row["profile"] not in ("sonnet", "grok-hi"):
            assert "area_adjustment" not in row


def test_json_without_area_has_no_area_key(area_server):
    _ = area_server
    payload = _rec_dict("A+")
    assert "area" not in payload


# ---------------------------------------------------------------------------
# CLI: vocabulary validation and refusals
# ---------------------------------------------------------------------------


def test_area_without_recommend_is_exit_2(area_server, monkeypatch, capsys):
    _ = area_server
    monkeypatch.setattr(cli, "registry", lambda: {})
    assert cli.main(["--area", "blender", "--list-providers"]) == 2
    captured = capsys.readouterr()
    assert "--area requires --recommend" in captured.err
    assert captured.out == ""


def test_unknown_area_is_exit_2_and_lists_the_allowed_set(area_server, monkeypatch, capsys):
    _ = area_server
    monkeypatch.setattr(cli, "registry", lambda: {})
    assert cli.main(["--recommend", "A+", "--area", "nope"]) == 2
    captured = capsys.readouterr()
    assert "nope" in captured.err
    for area in AREAS:
        assert area in captured.err
    assert captured.out == ""


def test_area_without_a_vocabulary_or_cache_is_refused(area_server, monkeypatch, capsys):
    _, fake = area_server
    fake.offline = True
    monkeypatch.setattr(cli, "registry", lambda: {})
    assert cli.main(["--recommend", "A+", "--area", "blender"]) == 2
    captured = capsys.readouterr()
    assert len([line for line in captured.err.splitlines() if line.strip()]) == 1
    assert "area" in captured.err
    assert captured.out == ""


def test_cached_vocabulary_still_serves_after_the_server_goes_away(area_server, monkeypatch, capsys):
    _, fake = area_server
    monkeypatch.setattr(cli, "registry", lambda: {})
    assert cli.main(["--recommend", "A+", "--area", "blender"]) == 0
    capsys.readouterr()

    fake.offline = True
    assert cli.main(["--recommend", "A+", "--area", "blender"]) == 0
    captured = capsys.readouterr()
    assert "A+ |" in captured.out


def test_cli_area_json_carries_area_and_adjustment(area_server, monkeypatch, capsys):
    _, fake = area_server
    fake.exceptions = [_exception("sonnet", "high", "blender", "prefer", decision_ref="hk:doc/cli1")]
    monkeypatch.setattr(cli, "registry", lambda: _fake_registry())
    assert cli.main(["--recommend", "A", "--area", "blender", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["area"] == "blender"
    sonnet = next(row for row in payload["rows"] if row["profile"] == "sonnet" and row["kind"] == "candidate")
    assert sonnet["area_adjustment"] == {"action": "prefer", "decision_ref": "hk:doc/cli1"}


def _fake_registry():
    return {pool: functools.partial(_result, pool, POOL_USED[pool]) for pool in POOL_USED}
