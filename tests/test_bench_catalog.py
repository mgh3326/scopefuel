"""#593: the handoffkeep catalog as the canonical (profile, effort) table.

The subject is not "does a catalog row parse" but the four ways a canonical
store quietly stops being canonical: a merge that only moves rows it already
knew about, a cache that never expires, a cache that expires into free dispatch,
and a flag meant for quota snapshots that reaches the catalog's cache.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from test_bench_backend import FakeHandoffkeep, _set_backend

from scopefuel import bench, cli, launch
from scopefuel.http import HttpError

SEED_REF = "hk:doc/task/2026-09-23/scopefuel-catalog-server-canonical"


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
        "boundary_version": "2026-09-23",
        "deviation_ref": SEED_REF,
        "decided_at": "2026-09-23T00:00:00Z",
        "decided_by": "operator-desk",
        "retired_at": None,
    }
    row.update(overrides)
    return row


def _seed_rows():
    """A small but complete canon: two Opus rungs, Sol, and one launcher-only row."""

    return [
        _row("opus", "high", "claude-opus-5-5", "claude", "S+"),
        _row("opus", "xhigh", "claude-opus-5-5", "claude", "S+"),
        _row("codex-sol", "max", "gpt-6-sol", "codex", "S+"),
        _row("codex-terra-max", "", "gpt-5.6-terra", "codex", "S"),
    ]


@pytest.fixture
def catalog_server(tmp_path, monkeypatch):
    """A handoffkeep that serves the catalog route, with a 1h catalog TTL."""

    data_home = _set_backend(tmp_path, monkeypatch)
    config_file = tmp_path / "config" / "scopefuel" / "config.toml"
    config_file.write_text(
        '[bench]\nbackend = "handoffkeep"\ncache_ttl_s = 21600\n'
        "catalog_ttl_s = 3600\ncatalog_stale_max_s = 86400\n",
        encoding="utf-8",
    )
    fake = FakeHandoffkeep()
    fake.catalog = _seed_rows()
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    bench.reset_catalog_memo()
    return data_home, fake


def _age_catalog_cache(seconds: float) -> None:
    """Backdate the catalog cache stamp — the TTL injection AC1 asks for."""

    import datetime as dt

    stamp = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds)).isoformat()
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute("UPDATE bench_cache_meta SET fetched_at = ? WHERE scope = 'catalog'", (stamp,))
        conn.commit()
    finally:
        conn.close()
    bench.reset_catalog_memo()


# --- T1: the TTL is real in both directions --------------------------------


def test_catalog_cache_is_served_without_a_request_inside_the_ttl(catalog_server):
    _, fake = catalog_server
    first = bench.read_catalog()
    assert first.source == "server"
    assert fake.hits[("GET", "catalog")] == 1

    # A server-side change inside the TTL must NOT be picked up: asserting only
    # that the new value eventually arrives cannot tell a working cache from a
    # cache that is never consulted.
    fake.catalog = [_row("opus", "high", "claude-opus-5-5", "claude", "A")]
    bench.reset_catalog_memo()
    cached = bench.read_catalog()
    assert cached.source == "cache"
    assert fake.hits[("GET", "catalog")] == 1
    assert {(e.profile, e.effort, e.grade) for e in cached.entries} >= {("opus", "high", "S+")}


def test_catalog_grade_change_reaches_recommend_after_the_ttl(catalog_server):
    _, fake = catalog_server
    assert bench.read_catalog().source == "server"

    fake.catalog = [
        _row("opus", "high", "claude-opus-5-5", "claude", "A"),
        _row("codex-sol", "max", "gpt-6-sol", "codex", "S+"),
    ]
    _age_catalog_cache(3601)

    table = bench.runtime_grade_table()
    placements = {
        (profile.name, profile.launcher_effort): grade
        for grade, profiles in table.items()
        for profile in profiles
    }
    assert placements[("opus", "high")] == "A"


def test_catalog_model_id_change_reaches_the_launcher_without_a_grade_change(catalog_server):
    """④(a): a canon whose model ids are ignored is not a canon.

    AC1 changes a *grade*; an overlay-only merge passes that while silently
    dropping every model-id change, which is the half wrk actually consumes.
    """

    _, fake = catalog_server
    assert launch.resolve_launch("codex-sol").model_id == "gpt-6-sol"

    fake.catalog = [_row("codex-sol", "max", "gpt-6-1-sol", "codex", "S+")]
    _age_catalog_cache(3601)
    assert launch.resolve_launch("codex-sol").model_id == "gpt-6-1-sol"


# --- T2/T3: additions and removals both land -------------------------------


def test_catalog_only_profile_becomes_a_recommendation_candidate(catalog_server):
    _, fake = catalog_server
    bench.read_catalog()  # prime the cache so the TTL below has something to age
    fake.catalog = _seed_rows() + [_row("brand-new", "high", "brand-new-1", "codex", "S", score=63.0)]
    _age_catalog_cache(3601)

    table = bench.runtime_grade_table()
    assert any(profile.name == "brand-new" for profile in table["S"])


def test_retiring_a_rung_server_side_removes_it_from_the_table(catalog_server):
    _, fake = catalog_server
    table = bench.runtime_grade_table()
    assert any(p.name == "opus" and p.launcher_effort == "xhigh" for p in table["S+"])

    fake.catalog = [
        row for row in _seed_rows() if not (row["profile"] == "opus" and row["effort"] == "xhigh")
    ]
    _age_catalog_cache(3601)

    table = bench.runtime_grade_table()
    assert not any(p.name == "opus" and p.launcher_effort == "xhigh" for p in table["S+"])
    # The profile itself is still covered, so its other rung survives.
    assert any(p.name == "opus" and p.launcher_effort == "high" for p in table["S+"])


def test_a_profile_the_catalog_never_mentions_is_kept_not_deleted(catalog_server):
    """A half-seeded catalog must not silently empty the table."""

    table = bench.runtime_grade_table()
    names = {profile.name for profiles in table.values() for profile in profiles}
    assert "kimi-k3" in names, "an uncovered snapshot profile must survive the merge"
    assert "uncovered" in bench.catalog_status_report()


# --- I6: the merge is all-or-nothing ---------------------------------------


def test_a_sol_profile_off_s_plus_rejects_the_whole_catalog(catalog_server, capsys):
    _, fake = catalog_server
    bench.read_catalog()  # prime the cache so the TTL below has something to age
    fake.catalog = _seed_rows() + [_row("kiro-sol", "", "gpt-5.6-sol", "kiro", "B")]
    _age_catalog_cache(3601)

    table = bench.runtime_grade_table()
    # The whole catalog is discarded, not just the offending row: a half-applied
    # canon is worse than none, because nothing downstream can tell which half
    # it got. kiro-sol therefore keeps the code table's S+ placement.
    placements = {profile.name: grade for grade, profiles in table.items() for profile in profiles}
    assert placements["kiro-sol"] == "S+"
    assert not any(p.name == "brand-new" for ps in table.values() for p in ps)
    assert "failed boundary validation" in capsys.readouterr().err


def test_consult_only_rows_never_enter_the_grade_table(catalog_server):
    _, fake = catalog_server
    bench.read_catalog()  # prime the cache so the TTL below has something to age
    fake.catalog = _seed_rows() + [_row("fable", "", "claude-fable-5-1", "claude", "S+", gate="consult_only")]
    _age_catalog_cache(3601)

    table = bench.runtime_grade_table()
    assert not any(p.name == "fable" for profiles in table.values() for p in profiles)
    # …but it stays launchable on an explicit operator request.
    assert launch.resolve_launch("fable", operator_request=True).model_id == "claude-fable-5-1"


# --- AC2 / T5 / T6: the three states ---------------------------------------


def test_offline_within_stale_max_keeps_using_the_cached_canon(catalog_server):
    _, fake = catalog_server
    bench.read_catalog()
    _age_catalog_cache(7200)  # past the 1h TTL, far short of the 24h stale ceiling
    fake.offline = True

    view = bench.read_catalog()
    assert view.source == "cache"
    assert view.stale is False


def test_offline_past_stale_max_falls_back_to_the_snapshot_and_says_so(catalog_server, capsys):
    _, fake = catalog_server
    bench.read_catalog()
    _age_catalog_cache(90000)  # > catalog_stale_max_s
    fake.offline = True

    view = bench.read_catalog()
    assert view.source == "snapshot"
    assert view.stale is True
    assert view.label == "catalog=stale (snapshot)"
    assert "catalog=stale" in capsys.readouterr().err


def test_a_server_without_the_catalog_route_is_not_stale(tmp_path, monkeypatch):
    """Production handoffkeep predates #592 — a 404 there must not brand every
    spawn brief stale, and must leave the grades projection in charge."""

    _set_backend(tmp_path, monkeypatch)
    fake = FakeHandoffkeep()
    fake.catalog = None  # the default: 404
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    bench.reset_catalog_memo()

    view = bench.read_catalog()
    assert view.source == "unsupported"
    assert view.stale is False
    assert "no catalog route" in view.label


# --- AC5 / ④(d): --no-cache is a quota flag, not a catalog flag -------------


def test_no_cache_neither_refetches_nor_invalidates_the_catalog_cache(catalog_server, monkeypatch):
    _, fake = catalog_server
    bench.read_catalog()
    baseline_hits = fake.hits[("GET", "catalog")]

    conn = sqlite3.connect(bench.db_path())
    try:
        before_stamp = conn.execute(
            "SELECT fetched_at FROM bench_cache_meta WHERE scope = 'catalog'"
        ).fetchone()[0]
        before_rows = conn.execute(
            "SELECT profile, effort, model_id, grade FROM bench_cache_catalog ORDER BY profile, effort"
        ).fetchall()
    finally:
        conn.close()

    monkeypatch.setattr(cli, "registry", lambda: {})
    bench.reset_catalog_memo()
    assert cli.main(["--recommend", "S+", "--no-cache"]) == 0

    conn = sqlite3.connect(bench.db_path())
    try:
        after_stamp = conn.execute(
            "SELECT fetched_at FROM bench_cache_meta WHERE scope = 'catalog'"
        ).fetchone()[0]
        after_rows = conn.execute(
            "SELECT profile, effort, model_id, grade FROM bench_cache_catalog ORDER BY profile, effort"
        ).fetchall()
    finally:
        conn.close()

    # Both directions: --no-cache must not force a catalog request, and must not
    # invalidate the cache either — during a 429 storm the second failure mode
    # turns "give me fresh data" into "lose the canon".
    assert fake.hits[("GET", "catalog")] == baseline_hits
    assert after_stamp == before_stamp
    assert after_rows == before_rows


def test_recommend_always_prints_the_catalog_provenance(catalog_server, monkeypatch, capsys):
    monkeypatch.setattr(cli, "registry", lambda: {})
    assert cli.main(["--recommend", "S+"]) == 0
    assert "catalog=" in capsys.readouterr().out


# --- push-catalog ----------------------------------------------------------


def test_push_catalog_writes_rows_and_refreshes_the_cache(catalog_server, tmp_path):
    _, fake = catalog_server
    payload = tmp_path / "catalog.json"
    payload.write_text(
        json.dumps({"catalog": [_row("opus", "high", "claude-opus-5-5", "claude", "A")]}),
        encoding="utf-8",
    )
    assert bench.push_catalog(payload) == 1
    assert fake.put_bodies[-1][0] == "catalog"
    # The write invalidates the memo, so the next read sees the new placement
    # without waiting out the TTL.
    assert any(e.grade == "A" for e in bench.read_catalog().entries if e.profile == "opus")


def test_push_catalog_refuses_a_row_without_decided_by(catalog_server, tmp_path):
    payload = tmp_path / "catalog.json"
    payload.write_text(
        json.dumps({"catalog": [_row("opus", "high", "claude-opus-5-5", "claude", "A", decided_by=None)]}),
        encoding="utf-8",
    )
    with pytest.raises(bench.BenchError, match="decided_by"):
        bench.push_catalog(payload)


def test_emit_seed_produces_rows_the_server_route_accepts(catalog_server, capsys, tmp_path):
    assert cli.main(["bench", "push-catalog", "--emit-seed", "--decided-by", "operator-desk"]) == 0
    seed = json.loads(capsys.readouterr().out)
    assert seed["catalog"], "the seed must not be empty"
    assert all(row["decided_by"] for row in seed["catalog"])
    payload = tmp_path / "seed.json"
    payload.write_text(json.dumps(seed), encoding="utf-8")
    assert bench.push_catalog(payload) == len(seed["catalog"])


# --- the pre-#593 database still works -------------------------------------


def test_a_pre_593_cache_database_is_migrated_not_broken(catalog_server):
    """``CREATE TABLE IF NOT EXISTS`` never revises a CHECK constraint, so a
    database made before #593 would reject every catalog stamp. Only an existing
    user's machine hits this; a fresh test database would not."""

    bench.db_path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS bench_cache_meta;
            CREATE TABLE bench_cache_meta (
              scope       TEXT PRIMARY KEY CHECK (scope IN ('scores', 'reps', 'grades')),
              fetched_at  TEXT NOT NULL,
              endpoint_id TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO bench_cache_meta(scope, fetched_at, endpoint_id)
              VALUES ('grades', '2026-09-23T00:00:00+00:00', 'legacy');
            """
        )
        conn.commit()
    finally:
        conn.close()

    bench.reset_catalog_memo()
    assert bench.read_catalog().source == "server"

    conn = sqlite3.connect(bench.db_path())
    try:
        scopes = {row[0] for row in conn.execute("SELECT scope FROM bench_cache_meta")}
    finally:
        conn.close()
    assert "catalog" in scopes
    assert "grades" in scopes, "the rebuild must carry existing rows over"


# --- ④(e): one process, one catalog ----------------------------------------


def test_read_catalog_is_memoised_within_one_process(catalog_server):
    _, fake = catalog_server
    bench.read_catalog()
    bench.read_catalog()
    bench.read_catalog()
    assert fake.hits[("GET", "catalog")] == 1


# --- backend auto-detection (AC6) ------------------------------------------


def test_auto_backend_uses_handoffkeep_when_the_cli_credentials_exist(tmp_path, monkeypatch):
    """The fleet switch must not require editing five hosts' config.toml."""

    monkeypatch.delenv("HANDOFFKEEP_URL", raising=False)
    monkeypatch.delenv("HANDOFFKEEP_TOKEN", raising=False)
    env = tmp_path / "hk.env"
    env.write_text("HANDOFFKEEP_URL=https://hk.example\nHANDOFFKEEP_TOKEN=secret\n", encoding="utf-8")
    monkeypatch.setenv("HANDOFFKEEP_CONFIG", str(env))

    backend = bench.bench_backend()
    assert backend.name == bench.BENCH_BACKEND_HANDOFFKEEP
    assert backend.reason == "auto-credentials"


def test_auto_backend_stays_local_when_the_url_would_leak_the_token(tmp_path, monkeypatch):
    """Auto-enabling a plaintext endpoint would make every request an error and
    hide the real blocker; staying local names it instead."""

    monkeypatch.delenv("HANDOFFKEEP_URL", raising=False)
    monkeypatch.delenv("HANDOFFKEEP_TOKEN", raising=False)
    env = tmp_path / "hk.env"
    env.write_text("HANDOFFKEEP_URL=http://100.122.100.56:8800\nHANDOFFKEEP_TOKEN=secret\n", encoding="utf-8")
    monkeypatch.setenv("HANDOFFKEEP_CONFIG", str(env))

    backend = bench.bench_backend()
    assert backend.name == bench.BENCH_BACKEND_LOCAL
    assert backend.reason == "auto-local-insecure-url"
    assert "allow_plaintext_url" in bench.catalog_status_report()


def test_explicit_opt_in_allows_a_private_tunnel_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("HANDOFFKEEP_URL", raising=False)
    monkeypatch.delenv("HANDOFFKEEP_TOKEN", raising=False)
    env = tmp_path / "hk.env"
    env.write_text("HANDOFFKEEP_URL=http://100.122.100.56:8800\nHANDOFFKEEP_TOKEN=secret\n", encoding="utf-8")
    monkeypatch.setenv("HANDOFFKEEP_CONFIG", str(env))
    config_file = tmp_path / "config" / "scopefuel" / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("[bench]\nallow_plaintext_url = true\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    backend = bench.bench_backend()
    assert backend.name == bench.BENCH_BACKEND_HANDOFFKEEP
    assert backend.allow_plaintext_url is True


def test_a_public_plaintext_endpoint_is_refused_even_with_the_opt_in_absent(tmp_path, monkeypatch):
    backend = bench.BenchBackend(
        name=bench.BENCH_BACKEND_HANDOFFKEEP,
        cache_ttl_s=1.0,
        url="http://handoffkeep.example.com",
        token="secret",
        endpoint_id="x",
    )
    with pytest.raises(bench.BenchBackendError, match="https"):
        bench._backend_url(backend, "catalog")


def test_a_non_404_http_error_is_an_outage_not_a_missing_route(catalog_server, monkeypatch):
    _, fake = catalog_server

    def boom(*args, **kwargs):
        raise HttpError(503, "down")

    monkeypatch.setattr(bench, "request_json", boom)
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert view.source == "snapshot"
    assert view.stale is True


def test_a_legacy_grades_write_does_not_add_a_phantom_candidate(catalog_server):
    """`bench grades set` mirrors into the catalog's profile-default row. With
    enumerated rungs already present, admitting that row too would list the
    profile twice — once with an effort and once without."""

    _, fake = catalog_server
    bench.read_catalog()
    fake.catalog = _seed_rows() + [_row("opus", "", "claude-opus-5-5", "claude", "S+")]
    _age_catalog_cache(3601)

    table = bench.runtime_grade_table()
    opus_rows = [p for profiles in table.values() for p in profiles if p.name == "opus"]
    assert sorted(p.launcher_effort for p in opus_rows) == ["high", "xhigh"]


def test_a_profile_keyed_only_on_its_default_row_still_lands(catalog_server):
    """The skip above must not drop a profile whose single row *is* the default."""

    _, fake = catalog_server
    bench.read_catalog()
    fake.catalog = _seed_rows() + [_row("grok-hi", "", "grok-4.7", "grok", "S", score=67.9)]
    _age_catalog_cache(3601)

    table = bench.runtime_grade_table()
    assert any(p.name == "grok-hi" for p in table["S"])
