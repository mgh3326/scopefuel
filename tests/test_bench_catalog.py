"""#593: the handoffkeep catalog as the canonical (profile, effort) table.

The subject is not "does a catalog row parse" but the four ways a canonical
store quietly stops being canonical: a merge that only moves rows it already
knew about, a cache that never expires, a cache that expires into free dispatch,
and a flag meant for quota snapshots that reaches the catalog's cache.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3

import pytest
from test_bench_backend import FakeHandoffkeep, _set_backend

from scopefuel import bench, cli, launch, recommend
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


def test_a_ttl_above_the_stale_ceiling_cannot_keep_serving_the_cache(catalog_server):
    """`catalog_stale_max_s` is the safety bound; a larger TTL must not outrank it."""

    _, fake = catalog_server
    config = bench.pathlib.Path(os.environ["XDG_CONFIG_HOME"]) / "scopefuel" / "config.toml"
    config.write_text(
        '[bench]\nbackend = "handoffkeep"\ncatalog_ttl_s = 1000000000000\ncatalog_stale_max_s = 60\n',
        encoding="utf-8",
    )
    bench.read_catalog()
    _age_catalog_cache(172800)
    fake.offline = True

    view = bench.read_catalog()
    assert view.source == "snapshot"
    assert view.stale is True


def test_a_cache_stamped_in_the_future_is_refetched_not_trusted_forever(catalog_server):
    """A stamp ahead of the clock is a broken cache, not a very fresh one.

    Clamping its age to 0 pinned the host to that cache permanently — no server
    change ever arrived again.
    """

    _, fake = catalog_server
    bench.read_catalog()
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute(
            "UPDATE bench_cache_meta SET fetched_at = ? WHERE scope = 'catalog'",
            ("9999-01-01T00:00:00+00:00",),
        )
        conn.commit()
    finally:
        conn.close()
    bench.reset_catalog_memo()

    fake.catalog = [_row("opus", "high", "claude-opus-99", "claude", "S+")]
    view = bench.read_catalog()
    assert view.source == "server"
    assert any(entry.model_id == "claude-opus-99" for entry in view.entries)


def test_every_snapshot_row_satisfies_the_server_row_contract():
    """handoffkeep rejects the whole batch if any row is invalid, so the seed has
    to be acceptable row by row.

    `profile`, `model_id`, `pool` and `decided_by` are required non-blank on the
    catalog route, `grade`/`gate` come from closed sets, and `score` must be a
    finite 0-100 (handoffkeep 6de6d6d internal/store/store.go
    validBenchCatalogEntry). Verified against a real local server at that commit:
    before this check, 15 of 49 seed rows carried an empty model_id and the seed
    PUT failed with 400 invalid_context — the documented one-time seed step would
    not have worked.
    """

    for entry in launch.snapshot_entries():
        where = f"{entry.profile}/{entry.effort or '-'}"
        assert entry.profile.strip(), f"{where}: profile must not be blank"
        assert entry.model_id.strip(), f"{where}: model_id is required by the catalog route"
        assert entry.pool.strip(), f"{where}: pool is required by the catalog route"
        assert entry.grade in bench.REP_GRADES, f"{where}: grade {entry.grade!r} is off the ladder"
        assert entry.gate in bench.CATALOG_GATES, f"{where}: gate {entry.gate!r} is not a catalog gate"
        if entry.score is not None:
            assert 0.0 <= entry.score <= 100.0, f"{where}: score {entry.score} out of range"


def test_the_emitted_seed_carries_provenance_on_every_row():
    """`decided_by` is caller-supplied on this route; the server rejects a blank."""

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert cli.main(["bench", "push-catalog", "--emit-seed", "--decided-by", "operator-desk"]) == 0
    rows = json.loads(buffer.getvalue())["catalog"]
    assert rows
    for row in rows:
        assert str(row["decided_by"]).strip()
        assert str(row["deviation_ref"]).strip()
        assert str(row["model_id"]).strip()


def test_a_fully_retired_profile_leaves_the_recommendations(catalog_server):
    """Coverage counts retired rows, or the canon's retirement is half-applied.

    Before this, retiring every rung of a profile dropped it from the *live* set,
    so the merge treated it as "never mentioned" and restored its snapshot rows —
    `--recommend` kept proposing a profile `policy launch` refused to start.
    """

    _, fake = catalog_server
    bench.read_catalog()
    retired = "2026-09-24T00:00:00Z"
    fake.catalog = [
        _row("opus", effort, "claude-opus-5-5", "claude", "S+", retired_at=retired)
        for effort in ("high", "xhigh")
    ] + [_row("codex-sol", "max", "gpt-6-sol", "codex", "S+")]
    _age_catalog_cache(3601)

    table = bench.runtime_grade_table()
    assert not any(p.name == "opus" for profiles in table.values() for p in profiles)
    with pytest.raises(launch.LaunchError):
        launch.resolve_launch("opus")
    # A profile the catalog never mentioned is still untouched.
    assert any(p.name == "kimi-k3" for profiles in table.values() for p in profiles)


def test_an_empty_catalog_still_falls_back_to_the_snapshot(catalog_server):
    """ "Nothing seeded yet" and "everything retired" are different statements."""

    _, fake = catalog_server
    bench.read_catalog()
    fake.catalog = []
    _age_catalog_cache(3601)

    table = bench.runtime_grade_table()
    assert any(p.name == "opus" for profiles in table.values() for p in profiles)


def test_a_half_set_environment_override_is_not_completed_from_config_env(tmp_path, monkeypatch):
    """Setting one variable must not redirect the stored bearer token.

    Completing the pair from config.env meant `HANDOFFKEEP_URL=https://elsewhere`
    alone was enough to make the client send the config.env token to that host.
    """

    env = tmp_path / "hk.env"
    env.write_text("HANDOFFKEEP_URL=https://real.example\nHANDOFFKEEP_TOKEN=secret\n", encoding="utf-8")
    monkeypatch.setenv("HANDOFFKEEP_CONFIG", str(env))
    monkeypatch.delenv("HANDOFFKEEP_TOKEN", raising=False)
    monkeypatch.setenv("HANDOFFKEEP_URL", "https://attacker.example")

    url, token = bench._handoffkeep_credentials()
    assert url == "https://attacker.example"
    assert token is None, "the config.env token must not follow an environment-chosen URL"
    backend = bench.bench_backend()
    assert backend.name == bench.BENCH_BACKEND_LOCAL
    assert "all-or-nothing" in bench.catalog_status_report()


# --- I2, stated properly: an addition that cannot be dispatched is not an
#     addition, and a recommendation that cannot be launched is a trap. --------


def _codex_provider():
    from scopefuel.model import Bucket, ProviderResult, Scope

    return ProviderResult(
        id="codex",
        pool_class="preserve",
        buckets=[Bucket(label="5h", window="5h", used_pct=10.0, scope=Scope("account"), horizon="now")],
    )


def test_a_catalog_only_profile_is_recommendable_on_the_servers_pool_and_launchable():
    """Table membership was never the point.

    A catalog row whose profile name this build has never seen reached the grade
    table and then rendered "측정 불가", because routing went through
    `profile_pool(name)` and the catalog's own `pool` was dropped. The server
    could add a profile that could never actually be recommended.
    """

    view = bench.CatalogView(
        entries=(
            bench.CatalogEntry("opus", "high", "claude-opus-5-5", "claude", "S+"),
            bench.CatalogEntry("brand-new", "high", "brand-new-1", "codex", "S", score=63.0),
        ),
        source="server",
        backend="handoffkeep",
    )
    table = bench._catalog_grade_table(view)

    rendered = recommend.recommend([_codex_provider()], "S", grade_table=table)
    assert "brand-new" in rendered, "the server's addition never reached the output"
    brand_new_lines = [line for line in rendered.splitlines() if "brand-new" in line]
    assert any("측정 불가" not in line for line in brand_new_lines), (
        f"the addition is listed but undispatchable: {brand_new_lines}"
    )
    assert any("Codex" in line for line in brand_new_lines), (
        f"the server's pool did not route the addition: {brand_new_lines}"
    )

    decision = launch.resolve_launch("brand-new", view=view)
    assert decision.model_id == "brand-new-1"
    assert decision.pool == "codex"


def test_every_recommended_profile_in_a_partly_seeded_catalog_can_be_launched():
    """The invariant, checked as one statement rather than two halves.

    A catalog that covers some profiles and is silent about others is the normal
    state during the rollout. The grade table keeps the uncovered ones so a
    half-seeded catalog cannot empty it — so launching them has to work, or
    `--recommend` hands a dispatcher a profile nothing can start.
    """

    view = bench.CatalogView(
        entries=(
            bench.CatalogEntry("opus", "high", "claude-opus-5-5", "claude", "S+"),
            bench.CatalogEntry("codex-sol", "max", "gpt-6-sol", "codex", "S+"),
        ),
        source="server",
        backend="handoffkeep",
    )
    table = bench._catalog_grade_table(view)

    unlaunchable = []
    for profiles in table.values():
        for profile in profiles:
            try:
                launch.resolve_launch(profile.name, operator_request=True, view=view)
            except launch.LaunchError as exc:
                unlaunchable.append(f"{profile.name}: {exc}")
    assert not unlaunchable, "recommended but unlaunchable: " + "; ".join(sorted(set(unlaunchable)))


def test_an_uncovered_profile_resolves_from_the_snapshot_and_says_so():
    """It launches, but it is not the canon speaking — so it is labelled, and it
    may not widen a gate."""

    view = bench.CatalogView(
        entries=(bench.CatalogEntry("opus", "high", "claude-opus-5-5", "claude", "S+"),),
        source="server",
        backend="handoffkeep",
    )
    decision = launch.resolve_launch("kimi-k3", view=view)
    assert decision.catalog_source == "snapshot"
    assert decision.catalog_stale is True

    # ...and the stale rule still applies to a non-default gate: oc-omni is an
    # escalation row in the bundled snapshot, and nothing here may widen it.
    with pytest.raises(launch.LaunchError, match="stale"):
        launch.resolve_launch("oc-omni", view=view)
    assert launch.resolve_launch("oc-omni", operator_request=True, view=view).gate == "escalation"


def test_a_profile_the_catalog_retired_is_still_refused_not_snapshot_resolved():
    """The snapshot fallback must not resurrect what the canon retired."""

    view = bench.CatalogView(
        entries=(
            bench.CatalogEntry("opus", "high", "claude-opus-5-5", "claude", "S+"),
            bench.CatalogEntry("grok-hi", "", "grok-4.7", "grok", "S", retired_at="2026-09-24T00:00:00Z"),
        ),
        source="server",
        backend="handoffkeep",
    )
    with pytest.raises(launch.LaunchError, match="retired"):
        launch.resolve_launch("grok-hi", view=view)
