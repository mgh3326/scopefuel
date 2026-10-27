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
    """A complete canon: every bundled-snapshot profile, at its bundled placement.

    The #954 validity floor refuses a fetched catalog that never mentions a
    snapshot profile, so the fake server's base state has to cover all of them
    — a partial canon is a rejected canon, and the tests exercise that in their
    own rows below.
    """

    return [entry.as_dict() for entry in bench.catalog_snapshot()]


def _seed_rows_with(profile, effort, **changes):
    """The full seed with one row's fields replaced — a canon edit, not a gap."""

    rows = []
    for row in _seed_rows():
        if row["profile"] == profile and row["effort"] == effort:
            row = {**row, **changes}
        rows.append(row)
    return rows


def _seed_profiles() -> set[str]:
    """Every profile the seed carries.

    hk 2079: the seed is the full catalog snapshot, which also holds the
    profiles that exist only as E6 rows (the pi credit profiles). The
    placement snapshot — the validity floor — does not list those, so the
    seed's profiles are the floor plus the E6 profiles.
    """

    return bench.snapshot_profiles() | {row.name for row in recommend.E6_ARM_RUNGS}


def _cached_catalog_count() -> int:
    """Rows in bench_cache_catalog — 0 when the table/database does not exist."""

    target = bench.db_path()
    if not target.exists():
        return 0
    conn = sqlite3.connect(target)
    try:
        return conn.execute("SELECT COUNT(*) FROM bench_cache_catalog").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


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


def test_catalog_fetch_requests_retired_rows_and_ratchet_accepts_them(catalog_server, monkeypatch):
    """The server only returns retired rows when include_retired=1 is requested."""
    _, fake = catalog_server
    original_request = fake.request_json
    urls = []

    def query_sensitive_request(url, **kwargs):
        from urllib.parse import parse_qs, urlsplit

        urls.append(url)
        payload = original_request(url, **kwargs)
        if (
            urlsplit(url).path.endswith("/catalog")
            and kwargs.get("method", "GET") == "GET"
            and parse_qs(urlsplit(url).query).get("include_retired") != ["1"]
        ):
            payload["catalog"] = [row for row in payload["catalog"] if not row.get("retired_at")]
        return payload

    monkeypatch.setattr(bench, "request_json", query_sensitive_request)
    assert bench.read_catalog().source == "server"
    last_good_profiles = {entry.profile for entry in bench.read_catalog().entries}
    retired_profiles = set(sorted(last_good_profiles)[:7])
    retired_at = "2026-10-23T00:00:00Z"
    fake.catalog = [
        ({**row, "retired_at": retired_at} if row["profile"] in retired_profiles else row)
        for row in fake.catalog
    ]
    _age_catalog_cache(3601)
    refreshed = bench.read_catalog()
    assert refreshed.source == "server"
    assert retired_profiles <= {entry.profile for entry in refreshed.entries if entry.retired_at}
    assert any("include_retired=1" in url for url in urls)


def test_push_catalog_preflight_requests_retired_rows(catalog_server, monkeypatch, tmp_path):
    _, fake = catalog_server
    fake.catalog = _seed_rows_with("opus", "high", retired_at="2026-10-23T00:00:00Z")
    original_request = fake.request_json
    responses = []

    def capture_request(url, **kwargs):
        from urllib.parse import parse_qs, urlsplit

        payload = original_request(url, **kwargs)
        if kwargs.get("method", "GET") == "GET" and urlsplit(url).path.endswith("/catalog"):
            if parse_qs(urlsplit(url).query).get("include_retired") != ["1"]:
                payload["catalog"] = [row for row in payload["catalog"] if not row.get("retired_at")]
            responses.append((url, payload))
        return payload

    monkeypatch.setattr(bench, "request_json", capture_request)
    payload = _write_push(tmp_path, _push_rows(["devin-argon"]))
    bench.push_catalog(payload)
    from urllib.parse import parse_qs, urlsplit

    assert responses
    preflight_url, preflight_payload = responses[0]
    assert parse_qs(urlsplit(preflight_url).query).get("include_retired") == ["1"]
    assert any(
        row["profile"] == "opus" and row["effort"] == "high" and row["retired_at"]
        for row in preflight_payload["catalog"]
    )


def test_catalog_grade_change_reaches_recommend_after_the_ttl(catalog_server):
    _, fake = catalog_server
    assert bench.read_catalog().source == "server"

    fake.catalog = _seed_rows_with("opus", "high", grade="A")
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
    assert launch.resolve_launch("codex-sol").model_id == "gpt-6.1-sol"

    fake.catalog = _seed_rows_with("codex-sol", "max", model_id="gpt-6.2-sol")
    _age_catalog_cache(3601)
    assert launch.resolve_launch("codex-sol").model_id == "gpt-6.2-sol"


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
    """A half-covered catalog *view* must not silently empty the table.

    The #954 fetch-time floor now refuses such a catalog as a whole, so the
    merge fill-in is the second line of defence — it still has to hold for a
    view that bypassed the floor (a pre-floor cache, an in-process view).
    """

    view = bench.CatalogView(
        entries=(
            bench.CatalogEntry("opus", "high", "claude-opus-5-5", "claude", "S+"),
            bench.CatalogEntry("codex-sol", "max", "gpt-6.1-sol", "codex", "S+"),
        ),
        source="server",
        backend="handoffkeep",
    )
    table = bench._catalog_grade_table(view)
    names = {profile.name for profiles in table.values() for profile in profiles}
    assert "kimi-k3" in names, "an uncovered snapshot profile must survive the merge"


def test_a_pre_floor_partial_cache_still_discloses_its_gaps(catalog_server):
    """A cache written before the validity floor can hold a partial canon; the
    status report keeps naming the uncovered profiles it cannot vouch for."""

    backend = bench.bench_backend(use="catalog")
    bench._commit_catalog_cache(
        path=None,
        entries=[bench.CatalogEntry("opus", "high", "claude-opus-5-5", "claude", "S+")],
        backend=backend,
    )
    bench.reset_catalog_memo()

    assert bench.read_catalog().source == "cache"
    assert "uncovered" in bench.catalog_status_report()


# --- I6: the merge is all-or-nothing ---------------------------------------


def test_a_sol_profile_off_s_plus_rejects_the_whole_catalog(catalog_server, capsys):
    _, fake = catalog_server
    bench.read_catalog()  # prime the cache so the TTL below has something to age
    fake.catalog = _seed_rows_with("kiro-sol", "", grade="B")
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


def test_offline_past_stale_max_keeps_serving_the_last_good_cache(catalog_server, capsys):
    """#954: past ``catalog_stale_max_s`` the cache still beats the snapshot.

    Reverting to the bundled snapshot was the one path that resurrected a
    server-side demotion — the snapshot is older than the cache and never saw
    it. The last-good cache keeps serving, labelled ``cache-stale`` and gated
    stale. (This test was rewritten for the new contract: before #954 it
    asserted the snapshot fallback.)
    """

    _, fake = catalog_server
    # A grade that differs between cache and snapshot proves which one served.
    fake.catalog = _seed_rows_with("opus", "high", grade="A")
    bench.read_catalog()
    _age_catalog_cache(90000)  # > catalog_stale_max_s
    fake.offline = True
    bench.reset_catalog_memo()

    view = bench.read_catalog()
    assert view.source == "cache-stale"
    assert view.stale is True
    assert view.label == "catalog=cache-stale (age 25.0h; server unreachable)"
    assert any(e.profile == "opus" and e.effort == "high" and e.grade == "A" for e in view.entries)
    assert "unreachable" in capsys.readouterr().err


def test_offline_past_stale_max_without_a_cache_falls_back_to_the_snapshot(catalog_server, capsys):
    """The bundled snapshot is now only the no-cache floor — unchanged label."""

    _, fake = catalog_server
    fake.offline = True  # nothing was ever cached

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
    # A fresh profile: a single-rung merged ladder is always monotonic — the
    # #1298 preflight refuses a payload the destination canon would reject.
    payload.write_text(
        json.dumps({"catalog": [_row("push-fresh", "high", "claude-opus-5-5", "claude", "A")]}),
        encoding="utf-8",
    )
    assert bench.push_catalog(payload) == 1
    assert fake.put_bodies[-1][0] == "catalog"
    # The write invalidates the memo, so the next read sees the new placement
    # without waiting out the TTL.
    assert any(e.grade == "A" for e in bench.read_catalog().entries if e.profile == "push-fresh")


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


def test_sol61_switch_snapshot_and_seed_carry_gpt61(capsys):
    """#1026 AC1: every codex-sol row in the snapshot and the emitted seed
    carries gpt-6.1-sol — grades, scores, gates and efforts unchanged.
    kiro-sol stays on gpt-5.6-sol."""
    snapshot = {entry.effort: entry for entry in bench.catalog_snapshot() if entry.profile == "codex-sol"}
    # hk 1788: codex-sol@low joins as an E6 arm row (carrier C, no score).
    assert set(snapshot) == {"max", "xhigh", "high", "medium", "low"}
    for entry in snapshot.values():
        assert entry.model_id == "gpt-6.1-sol"
    placements = {effort: (row.grade, row.score, row.gate) for effort, row in snapshot.items()}
    assert placements == {
        "max": ("S+", 67.0, "default"),
        "xhigh": ("S+", 65.0, "escalation"),
        "high": ("C", None, "default"),
        "medium": ("C", None, "default"),
        "low": ("C", None, "default"),
    }, "the switch changes no grade"

    assert cli.main(["bench", "push-catalog", "--emit-seed", "--decided-by", "operator-desk"]) == 0
    seed = {(row["profile"], row["effort"]): row for row in json.loads(capsys.readouterr().out)["catalog"]}
    for effort in ("max", "xhigh", "high", "medium", "low"):
        assert seed[("codex-sol", effort)]["model_id"] == "gpt-6.1-sol"
    assert seed[("kiro-sol", "")]["model_id"] == "gpt-5.6-sol"


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

    backend = bench.bench_backend(use="catalog")
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

    backend = bench.bench_backend(use="catalog")
    assert backend.name == bench.BENCH_BACKEND_LOCAL
    assert backend.reason == "auto-local-insecure-url"
    assert "allow_plaintext_catalog" in bench.catalog_status_report()


def test_explicit_opt_in_allows_a_private_tunnel_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("HANDOFFKEEP_URL", raising=False)
    monkeypatch.delenv("HANDOFFKEEP_TOKEN", raising=False)
    env = tmp_path / "hk.env"
    env.write_text("HANDOFFKEEP_URL=http://100.122.100.56:8800\nHANDOFFKEEP_TOKEN=secret\n", encoding="utf-8")
    monkeypatch.setenv("HANDOFFKEEP_CONFIG", str(env))
    config_file = tmp_path / "config" / "scopefuel" / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("[bench]\nallow_plaintext_catalog = true\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    backend = bench.bench_backend(use="catalog")
    assert backend.name == bench.BENCH_BACKEND_HANDOFFKEEP
    assert backend.allow_plaintext_url is True


def test_deprecated_allow_plaintext_url_alias_opts_in_every_use(tmp_path, monkeypatch, capsys):
    """task #697: the old flag keeps its exact old meaning — all three uses —
    so existing configs behave identically; it prints one deprecation warning."""

    monkeypatch.delenv("HANDOFFKEEP_URL", raising=False)
    monkeypatch.delenv("HANDOFFKEEP_TOKEN", raising=False)
    env = tmp_path / "hk.env"
    env.write_text("HANDOFFKEEP_URL=http://100.122.100.56:8800\nHANDOFFKEEP_TOKEN=secret\n", encoding="utf-8")
    monkeypatch.setenv("HANDOFFKEEP_CONFIG", str(env))
    config_file = tmp_path / "config" / "scopefuel" / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("[bench]\nallow_plaintext_url = true\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    bench._WARNED_DEPRECATED_KEYS.clear()

    for use in bench.PLAINTEXT_USES:
        backend = bench.bench_backend(use=use)
        assert backend.name == bench.BENCH_BACKEND_HANDOFFKEEP, use
        assert backend.allow_plaintext_url is True, use
    warnings = [line for line in capsys.readouterr().err.splitlines() if "allow_plaintext_url" in line]
    assert warnings == [
        "warning: [bench] allow_plaintext_url is deprecated and enables plaintext http for "
        "all uses; prefer allow_plaintext_catalog / allow_plaintext_quota_share / allow_plaintext_reps"
    ]


def test_a_per_use_opt_in_overrides_the_deprecated_alias(tmp_path, monkeypatch):
    """An explicit allow_plaintext_<use> = false wins over the alias for that
    use — the operator can narrow the alias without deleting it."""

    monkeypatch.delenv("HANDOFFKEEP_URL", raising=False)
    monkeypatch.delenv("HANDOFFKEEP_TOKEN", raising=False)
    env = tmp_path / "hk.env"
    env.write_text("HANDOFFKEEP_URL=http://100.122.100.56:8800\nHANDOFFKEEP_TOKEN=secret\n", encoding="utf-8")
    monkeypatch.setenv("HANDOFFKEEP_CONFIG", str(env))
    config_file = tmp_path / "config" / "scopefuel" / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        "[bench]\nallow_plaintext_url = true\nallow_plaintext_catalog = false\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert bench.bench_backend(use="catalog").name == bench.BENCH_BACKEND_LOCAL
    assert bench.bench_backend(use="reps").name == bench.BENCH_BACKEND_HANDOFFKEEP


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
    assert sorted(p.launcher_effort for p in opus_rows) == ["high", "low", "max", "medium", "xhigh"]


def test_a_profile_keyed_only_on_its_default_row_still_lands(catalog_server):
    """The skip above must not drop a profile whose single row *is* the default."""

    _, fake = catalog_server
    bench.read_catalog()
    fake.catalog = _seed_rows() + [_row("brand-new", "", "brand-new-1", "codex", "S", score=67.9)]
    _age_catalog_cache(3601)

    table = bench.runtime_grade_table()
    assert any(p.name == "brand-new" for p in table["S"])


def test_a_ttl_above_the_stale_ceiling_cannot_keep_the_cache_looking_fresh(catalog_server):
    """`catalog_stale_max_s` bounds the label: a TTL that outranks it cannot
    keep a condemned cache serving as plain ``cache`` — it comes back
    ``cache-stale`` and gated stale, never as a clean canon copy."""

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
    assert view.source == "cache-stale"
    assert view.stale is True
    assert "cache-stale" in view.label


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

    fake.catalog = _seed_rows_with("opus", "high", model_id="claude-opus-99")
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
        ({**row, "retired_at": retired} if row["profile"] == "opus" else row) for row in _seed_rows()
    ]
    _age_catalog_cache(3601)

    table = bench.runtime_grade_table()
    assert not any(p.name == "opus" for profiles in table.values() for p in profiles)
    with pytest.raises(launch.LaunchError):
        launch.resolve_launch("opus")
    # Every other covered profile keeps its canon placement.
    assert any(p.name == "haiku" for profiles in table.values() for p in profiles)


def test_retired_catalog_rows_are_never_ranked_in_recommend_output(catalog_server):
    _, fake = catalog_server
    retired = "2026-10-23T00:00:00Z"
    fake.catalog = [
        ({**row, "retired_at": retired} if row["profile"] == "opus" else row) for row in _seed_rows()
    ]

    output = recommend.recommend([_codex_provider()], "S+", grade_table=bench.runtime_grade_table())

    ranked_lines = [line for line in output.splitlines() if line[:1].isdigit()]
    assert not any("opus" in line for line in ranked_lines)


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
    backend = bench.bench_backend(use="catalog")
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
            bench.CatalogEntry("codex-sol", "max", "gpt-6.1-sol", "codex", "S+"),
        ),
        source="server",
        backend="handoffkeep",
    )
    table = bench._catalog_grade_table(view)

    # Barred Terra rows (operator 2026-10-12 noterra) stay in the table as data
    # but are filtered out of every recommendation, so they are not "recommended":
    # the invariant for them is that launch refuses each one.
    unlaunchable = []
    barred_refused = []
    barred_launched = []
    for profiles in table.values():
        for profile in profiles:
            barred = recommend.barred_model_id_for_profile(profile.name, grade_table=table)
            try:
                launch.resolve_launch(profile.name, operator_request=True, view=view)
            except launch.LaunchError as exc:
                if barred is None:
                    unlaunchable.append(f"{profile.name}: {exc}")
                else:
                    barred_refused.append(profile.name)
                continue
            if barred is not None:
                barred_launched.append(profile.name)
    assert not unlaunchable, "recommended but unlaunchable: " + "; ".join(sorted(set(unlaunchable)))
    assert barred_refused, "positive control: the table must still carry a barred Terra row"
    assert not barred_launched, "barred but launchable: " + "; ".join(sorted(set(barred_launched)))


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


# --- #954: the validity floor (#667 class) ---------------------------------
# A fetched catalog is refused as a whole when it is empty or never mentions a
# profile the bundled snapshot places. The refuse is *carried*, not thrown: the
# host degrades exactly like an unreachable server — cache if one exists, else
# the snapshot — with the reason on the label, and nothing is ever committed.


def test_a_one_row_server_catalog_is_rejected_whole_and_never_cached(catalog_server, monkeypatch, capsys):
    """AC1: a 1-row catalog is not a canon — refused, never cached, snapshot.

    #667 shipped exactly this shape: the server's single row became the fleet's
    placements and ``catalog=stale`` ended up on spawn briefs. The floor makes
    the small answer the one thing it cannot be — canonical.
    """

    _, fake = catalog_server
    fake.catalog = [_row("opus", "high", "claude-opus-5-5", "claude", "S+")]
    before = _cached_catalog_count()

    monkeypatch.setattr(cli, "registry", lambda: {})
    assert cli.main(["--recommend", "A+"]) == 0
    out = capsys.readouterr().out
    missing = len(bench.snapshot_profiles()) - 1
    assert f"catalog=stale (server catalog rejected: 1 row, missing {missing} snapshot profiles)" in out

    # The candidate list is the snapshot list — the 1-row "canon" placed nothing.
    served = {
        (p.name, p.launcher_effort): grade
        for grade, profiles in bench.runtime_grade_table().items()
        for p in profiles
    }
    expected = {
        (p.name, p.launcher_effort): grade
        for grade, profiles in recommend.GRADE_TABLE.items()
        for p in profiles
    }
    assert served == expected
    assert _cached_catalog_count() == before, "a refused catalog must never reach the cache"


def test_a_rejected_catalog_falls_back_to_the_cache_not_the_snapshot(catalog_server, capsys):
    """Degrades exactly like an unreachable server: a good cache still serves,
    labelled with the rejection — and the refused rows stay out of the cache."""

    _, fake = catalog_server
    bench.read_catalog()  # prime the last-good cache (the full seed)
    _age_catalog_cache(3601)  # past the 1h TTL, inside the 24h stale ceiling
    fake.catalog = [_row("opus", "high", "claude-opus-5-5", "claude", "A")]
    bench.reset_catalog_memo()

    view = bench.read_catalog()
    assert view.source == "cache"
    assert view.stale is False
    assert "server catalog rejected: 1 row" in view.label
    assert "server catalog rejected" in capsys.readouterr().err
    assert _cached_catalog_count() == len(bench.catalog_snapshot())


def test_an_empty_server_catalog_is_rejected_and_the_snapshot_stands_in(catalog_server):
    """ "Nothing seeded yet" is not a canon — an empty catalog is refused whole
    and never reaches the cache (#667 class)."""

    _, fake = catalog_server
    fake.catalog = []

    view = bench.read_catalog()
    assert view.source == "snapshot"
    assert view.stale is True
    assert "server catalog rejected: empty catalog" in view.label

    table = bench.runtime_grade_table()
    assert any(p.name == "opus" for profiles in table.values() for p in profiles)
    assert _cached_catalog_count() == 0


def test_a_retired_row_counts_as_mentioned_so_the_catalog_still_passes_the_floor(catalog_server):
    """A server-side retirement is a statement, not a gap: a retired row is
    *mentioned*, so the validity floor lets the catalog stand — and the
    retirement takes effect instead of falling back to the snapshot."""

    _, fake = catalog_server
    fake.catalog = _seed_rows_with("codex-luna-max", "", retired_at="2026-09-29T00:00:00Z")

    assert bench.read_catalog().source == "server"
    table = bench.runtime_grade_table()
    assert not any(p.name == "codex-luna-max" for profiles in table.values() for p in profiles)
    with pytest.raises(launch.LaunchError, match="retired"):
        launch.resolve_launch("codex-luna-max")


# --- #954: cache-stale — the last-good cache past the stale ceiling ---------


def test_offline_past_stale_max_serves_the_last_good_cache_labelled(catalog_server, monkeypatch, capsys):
    """AC3: a cache older than the stale ceiling still beats the snapshot.

    Everything a consumer can ask reports the same answer: --recommend prints
    the cache-stale label, the table carries the cached grade, policy launch
    discloses source=cache-stale with stale=true, and a non-default gate still
    needs --operator-request.
    """

    _, fake = catalog_server
    # A grade that differs between cache and snapshot proves which one served:
    # the cache holds opus@high at A, the snapshot says S+.
    fake.catalog = _seed_rows_with("opus", "high", grade="A")
    bench.read_catalog()
    _age_catalog_cache(90000)  # > catalog_stale_max_s
    fake.offline = True
    bench.reset_catalog_memo()

    monkeypatch.setattr(cli, "registry", lambda: {})
    assert cli.main(["--recommend", "A"]) == 0
    assert "catalog=cache-stale (age 25.0h; server unreachable)" in capsys.readouterr().out

    placements = {
        (p.name, p.launcher_effort): grade
        for grade, profiles in bench.runtime_grade_table().items()
        for p in profiles
    }
    assert placements[("opus", "high")] == "A", "the cached row must win over the snapshot's S+"

    capsys.readouterr()
    assert cli.main(["policy", "launch", "opus", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["catalog"]["source"] == "cache-stale"
    assert payload["catalog"]["stale"] is True

    # A non-default gate still needs --operator-request under a stale source.
    capsys.readouterr()
    assert cli.main(["policy", "launch", "opus", "--effort", "max", "--json"]) != 0
    assert cli.main(["policy", "launch", "opus", "--effort", "max", "--operator-request", "--json"]) == 0


# --- #954: bench catalog status --check ------------------------------------


def test_catalog_status_check_exits_zero_on_a_server_view(catalog_server, capsys):
    assert cli.main(["bench", "catalog", "status", "--check"]) == 0
    assert "catalog=server" in capsys.readouterr().out


def test_catalog_status_check_exits_two_on_a_local_backend(capsys):
    """AC5: the fleet check must never raise on a local-backend host — and must
    answer 2, naming the state, so an un-migrated machine is a red line."""

    rc = cli.main(["bench", "catalog", "status", "--check"])
    captured = capsys.readouterr()
    assert rc == 2
    # stdout keeps the five report lines.
    for prefix in ("backend=", "credentials ", "catalog_ttl_s=", "catalog=", "rows="):
        assert any(line.startswith(prefix) for line in captured.out.splitlines()), prefix
    assert "local backend" in captured.err


def test_catalog_status_check_exits_two_on_a_stale_cache(catalog_server, capsys):
    _, fake = catalog_server
    bench.read_catalog()
    _age_catalog_cache(90000)
    fake.offline = True
    bench.reset_catalog_memo()

    rc = cli.main(["bench", "catalog", "status", "--check"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "catalog=cache-stale" in captured.out
    assert "check failed: catalog=cache-stale" in captured.err


def test_catalog_status_check_exits_two_on_a_rejected_catalog(catalog_server, capsys):
    _, fake = catalog_server
    fake.catalog = [_row("opus", "high", "claude-opus-5-5", "claude", "S+")]

    rc = cli.main(["bench", "catalog", "status", "--check"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "server catalog rejected" in captured.out
    assert "check failed: catalog=stale (server catalog rejected:" in captured.err


def test_catalog_status_check_exits_zero_on_a_fresh_cache(catalog_server, capsys):
    """AC5 (corrected): a healthy host answers from the cache between
    refetches — source ``cache`` inside ``catalog_ttl_s`` with nothing
    degraded about it is the canon as far as the fleet check cares."""

    bench.read_catalog()  # prime the cache, then drop the memo
    bench.reset_catalog_memo()

    rc = cli.main(["bench", "catalog", "status", "--check"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "catalog=cache" in captured.out


def test_catalog_status_check_exits_two_on_a_rejection_served_cache(catalog_server, capsys):
    """AC5 (corrected): a cache serving only because the server refused its
    catalog carries the rejection on ``detail`` — degraded, not canon."""

    _, fake = catalog_server
    bench.read_catalog()  # prime with the full seed
    _age_catalog_cache(3601)  # past the 1h TTL, inside the 24h stale ceiling
    fake.catalog = [_row("opus", "high", "claude-opus-5-5", "claude", "S+")]
    bench.reset_catalog_memo()

    rc = cli.main(["bench", "catalog", "status", "--check"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "server catalog rejected" in captured.out
    assert "check failed: catalog=cache" in captured.err
    assert "server catalog rejected" in captured.err


def test_catalog_status_check_exits_two_on_an_unreachable_served_cache(catalog_server, capsys):
    """AC5 (corrected): a cache older than the TTL is served only because the
    server is unreachable — empty ``detail`` still fails the check on age."""

    _, fake = catalog_server
    bench.read_catalog()
    _age_catalog_cache(3601)
    fake.offline = True
    bench.reset_catalog_memo()

    rc = cli.main(["bench", "catalog", "status", "--check"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "catalog=cache (age 1.0h)" in captured.out
    assert "check failed: catalog=cache (age 1.0h)" in captured.err


# --- #1768: the last-good ratchet — bundle-new profiles do not reject ------
# The floor is no longer "every snapshot profile mentioned" but "every profile
# the last-good committed cache mentions". A snapshot profile neither side
# mentions is new to the bundle: it is served from the snapshot, warned about
# on every call, and attested in the cache meta so the warning persists until
# bench push-catalog lands the rows.

NEW_TO_BUNDLE = (
    "devin-argon",
    "devin-argon-low",
    "devin-argon-medium",
    "devin-fusion-sol61",
    "devin-fusion-sol61-xhigh",
)


def _canon_without(profiles):
    """The 66-row / 39-profile canon the fleet ran before the bundle grew.

    Measured on mbp 2026-10-20: the server catalog covered every profile the
    last-good cache knew and none of the five bundle additions.
    """

    return [row for row in _seed_rows() if row["profile"] not in set(profiles)]


def _commit_canon(rows, *, snapshot_fill=()):
    """Write a last-good cache directly, the way a prior run's accept did."""

    bench._commit_catalog_cache(
        path=None,
        entries=bench._catalog_from_payload({"catalog": rows}),
        backend=bench.bench_backend(use="catalog"),
        snapshot_fill=snapshot_fill,
    )
    bench.reset_catalog_memo()


def _stored_fill() -> str:
    conn = sqlite3.connect(bench.db_path())
    try:
        row = conn.execute("SELECT snapshot_fill FROM bench_cache_meta WHERE scope = 'catalog'").fetchone()
    finally:
        conn.close()
    return row[0] if row else ""


def test_a_bundle_new_profile_does_not_reject_the_canon(catalog_server, capsys):
    """The measured case: canon covers the cache, not the 5 bundle additions.

    Old floor rejected this catalog outright; the ratchet accepts it, warns
    once naming all five, and admits builder-sonnet's pinned rung
    (sonnet@xhigh, gate=escalation) by its normal gate.
    """

    _, fake = catalog_server
    canon = _canon_without(NEW_TO_BUNDLE)
    assert {row["profile"] for row in canon} == _seed_profiles() - set(NEW_TO_BUNDLE)
    _commit_canon(canon)  # the pre-bundle last-good cache
    _age_catalog_cache(3601)
    fake.catalog = canon
    bench.reset_catalog_memo()

    view = bench.read_catalog()
    assert view.source == "server"
    assert view.stale is False
    assert view.snapshot_fill == tuple(sorted(NEW_TO_BUNDLE))

    err = capsys.readouterr().err
    for profile in NEW_TO_BUNDLE:
        assert profile in err
    assert "bundled snapshot" in err
    assert "bench push-catalog" in err

    decision = launch.resolve_launch("sonnet", effort="xhigh")
    assert decision.gate == "escalation"
    assert decision.catalog_source == "server"
    assert decision.catalog_stale is False

    # A bundle-new profile is served from the snapshot — exactly the snapshot
    # placement, gate included, never a wider one.
    decision = launch.resolve_launch("devin-argon")
    assert decision.model_id == "gemini-4-argon-high"
    assert decision.gate == "default"
    assert decision.catalog_source == "snapshot"
    assert decision.catalog_stale is True


def test_retired_new_bundle_rows_are_covered_and_not_snapshot_filled(catalog_server, monkeypatch):
    """A retired canon row covers a bundle profile and must not restore it as a candidate."""
    from urllib.parse import parse_qs, urlsplit

    _, fake = catalog_server
    bench.read_catalog()  # establish the full last-good profile set
    canon = _canon_without(NEW_TO_BUNDLE)
    retired_at = "2026-10-23T00:00:00Z"
    retired_rows = [
        {**row, "retired_at": retired_at} for row in _seed_rows() if row["profile"] in NEW_TO_BUNDLE
    ]
    fake.catalog = canon + retired_rows
    original_request = fake.request_json

    def query_sensitive_request(url, **kwargs):
        payload = original_request(url, **kwargs)
        if (
            urlsplit(url).path.endswith("/catalog")
            and kwargs.get("method", "GET") == "GET"
            and parse_qs(urlsplit(url).query).get("include_retired") != ["1"]
        ):
            payload["catalog"] = [row for row in payload["catalog"] if not row.get("retired_at")]
        return payload

    monkeypatch.setattr(bench, "request_json", query_sensitive_request)
    _age_catalog_cache(3601)

    view = bench.read_catalog()
    assert view.source == "server"
    assert view.snapshot_fill == ()
    table = bench.runtime_grade_table()
    candidates = {profile.name for profiles in table.values() for profile in profiles}
    assert not set(NEW_TO_BUNDLE) & candidates


def test_the_cache_after_commit_holds_only_server_rows_and_keeps_warning(catalog_server, capsys):
    """The committed last-good cache carries no snapshot-filled rows: the
    attestation lives in the meta stamp, so the same profiles stay new to the
    bundle on the next run and the warning persists until the push."""

    _, fake = catalog_server
    canon = _canon_without(NEW_TO_BUNDLE)
    _commit_canon(canon)
    _age_catalog_cache(3601)
    fake.catalog = canon
    bench.reset_catalog_memo()
    bench.read_catalog()
    capsys.readouterr()

    conn = sqlite3.connect(bench.db_path())
    try:
        cached_profiles = {row[0] for row in conn.execute("SELECT profile FROM bench_cache_catalog")}
    finally:
        conn.close()
    assert cached_profiles == {row["profile"] for row in canon}
    assert not set(NEW_TO_BUNDLE) & cached_profiles
    assert set(_stored_fill().split(",")) == set(NEW_TO_BUNDLE)

    # Inside the TTL the cache serves and the warning still fires — the fill
    # was never absorbed into the cache rows, so it stays new to the bundle.
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert view.source == "cache"
    assert view.snapshot_fill == tuple(sorted(NEW_TO_BUNDLE))
    err = capsys.readouterr().err
    assert "devin-argon" in err and "devin-fusion-sol61-xhigh" in err


def test_a_catalog_dropping_a_cached_profile_is_rejected(catalog_server, capsys):
    """The ratchet: a profile the last-good cache mentions can never be dropped.

    Rejected exactly as today's floor rejected — refused whole, never
    committed, the cache keeps serving with the reason on the label.
    """

    _, fake = catalog_server
    bench.read_catalog()  # prime the last-good cache (the full seed)
    _age_catalog_cache(3601)
    fake.catalog = [row for row in _seed_rows() if row["profile"] != "kimi-k3"]
    bench.reset_catalog_memo()

    view = bench.read_catalog()
    assert view.source == "cache"
    assert view.stale is False
    assert "server catalog rejected" in view.detail
    assert "missing 1 snapshot profile" in view.detail
    assert "server catalog rejected" in capsys.readouterr().err
    assert _cached_catalog_count() == len(bench.catalog_snapshot())


def test_a_one_row_catalog_against_a_full_cache_is_still_rejected(catalog_server, capsys):
    """#667: the 1-row canon is still the thing the floor exists to refuse."""

    _, fake = catalog_server
    _commit_canon(_canon_without(NEW_TO_BUNDLE))
    _age_catalog_cache(3601)
    fake.catalog = [_row("opus", "high", "claude-opus-5-5", "claude", "S+")]
    bench.reset_catalog_memo()

    view = bench.read_catalog()
    assert view.source == "cache"
    # The cache holds the seed, which carries the E6-only credit profiles the
    # bundled placement snapshot does not — so the dropped set is "cached".
    assert view.detail == "server catalog rejected: 1 row, missing 42 cached profiles"
    # hk 1885: 68 = the 67 pre-1885 rows + the sonnet@low C row (the profile
    # count of "missing 38" is unchanged — sonnet was already a profile).
    # hk 2079: 83 = those 68 + the 15 pi credit E6 rows (3 profiles x 5 rungs).
    # hk 2141: 86 = those 83 + the 3 cmd-dsflash41 credit E6 rows.
    assert _cached_catalog_count() == 86


def test_no_cache_keeps_the_bundle_floor(catalog_server):
    """No last-good cache (first run): nothing to ratchet against, so the
    bundle floor fails closed exactly as before."""

    _, fake = catalog_server
    fake.catalog = _canon_without({"devin-argon"})

    view = bench.read_catalog()
    assert view.source == "snapshot"
    assert view.stale is True
    assert "server catalog rejected" in view.detail
    assert "missing 1 snapshot profile" in view.detail
    assert _cached_catalog_count() == 0


def test_a_foreign_endpoint_cache_is_not_the_ratchet(catalog_server):
    """An endpoint change must not ratchet: the other endpoint's cache does
    not count as last-good, so the bundle floor applies and fails closed."""

    _, fake = catalog_server
    _commit_canon(_canon_without(NEW_TO_BUNDLE))
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute("UPDATE bench_cache_meta SET endpoint_id = 'other-endpoint' WHERE scope = 'catalog'")
        conn.commit()
    finally:
        conn.close()
    fake.catalog = _canon_without(NEW_TO_BUNDLE)
    bench.reset_catalog_memo()

    view = bench.read_catalog()
    assert view.source == "snapshot"
    assert "missing 5 snapshot profiles" in view.detail


def test_a_stale_cache_still_ratchets(catalog_server):
    """Membership is not freshness: a cache past catalog_stale_max_s still
    supplies the profiles the canon may not drop."""

    _, fake = catalog_server
    _commit_canon(_canon_without(NEW_TO_BUNDLE))
    _age_catalog_cache(90000)  # past catalog_stale_max_s
    fake.catalog = [row for row in _canon_without(NEW_TO_BUNDLE) if row["profile"] != "kimi-k3"]
    bench.reset_catalog_memo()

    view = bench.read_catalog()
    assert view.source == "cache-stale"
    assert "server catalog rejected" in view.detail
    assert "missing 1 snapshot profile" in view.detail


def test_a_server_retired_profile_stays_retired_under_the_ratchet(catalog_server):
    """A profile the server marks retired is *mentioned*, so the catalog
    stands — and the retirement still wins over the bundled placement."""

    _, fake = catalog_server
    canon = _canon_without(NEW_TO_BUNDLE)
    _commit_canon(canon)
    _age_catalog_cache(3601)
    retired = "2026-10-20T00:00:00Z"
    fake.catalog = [
        ({**row, "retired_at": retired} if row["profile"] == "devin-ds41" else row) for row in canon
    ]
    bench.reset_catalog_memo()

    view = bench.read_catalog()
    assert view.source == "server"
    with pytest.raises(launch.LaunchError, match="retired"):
        launch.resolve_launch("devin-ds41")
    table = bench.runtime_grade_table()
    assert not any(p.name == "devin-ds41" for profiles in table.values() for p in profiles)


def test_status_lists_new_to_bundle_under_their_own_heading(catalog_server, capsys):
    """bench catalog status names the attested set apart from uncovered —
    and the call itself prints the fill warning on stderr."""

    _, fake = catalog_server
    canon = _canon_without(NEW_TO_BUNDLE)
    _commit_canon(canon)
    _age_catalog_cache(3601)
    fake.catalog = canon
    bench.reset_catalog_memo()

    assert cli.main(["bench", "catalog", "status"]) == 0
    captured = capsys.readouterr()
    heading = next(line for line in captured.out.splitlines() if line.startswith("new to the bundle"))
    for profile in NEW_TO_BUNDLE:
        assert profile in heading
    assert "uncovered" not in captured.out
    for profile in NEW_TO_BUNDLE:
        assert profile in captured.err

    # A cache-served status call still splits attested from unattested gaps:
    # trim one cached row and the profile it covered shows as uncovered while
    # the attested five keep their own heading.
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute("DELETE FROM bench_cache_catalog WHERE profile = 'oc-solar4'")
        conn.commit()
    finally:
        conn.close()
    bench.reset_catalog_memo()
    assert cli.main(["bench", "catalog", "status"]) == 0
    out = capsys.readouterr().out
    heading = next(line for line in out.splitlines() if line.startswith("new to the bundle"))
    assert "devin-argon" in heading and "oc-solar4" not in heading
    uncovered = next(line for line in out.splitlines() if line.startswith("uncovered"))
    assert "oc-solar4" in uncovered and "devin-argon" not in uncovered


def test_two_committers_leave_a_consistent_cache(catalog_server):
    """A concurrent commit from a second process replaces rows+stamp+fill as
    one triple — the last committer wins whole, never row-vs-fill torn."""

    backend = bench.bench_backend(use="catalog")
    first = _canon_without(NEW_TO_BUNDLE)
    second = _canon_without(NEW_TO_BUNDLE + ("oc-solar4",))
    bench._commit_catalog_cache(
        path=None,
        entries=bench._catalog_from_payload({"catalog": first}),
        backend=backend,
        snapshot_fill=tuple(sorted(NEW_TO_BUNDLE)),
    )
    bench._commit_catalog_cache(
        path=None,
        entries=bench._catalog_from_payload({"catalog": second}),
        backend=backend,
        snapshot_fill=tuple(sorted(NEW_TO_BUNDLE + ("oc-solar4",))),
    )

    conn = sqlite3.connect(bench.db_path())
    try:
        profiles = {row[0] for row in conn.execute("SELECT profile FROM bench_cache_catalog")}
        fill = conn.execute("SELECT snapshot_fill FROM bench_cache_meta WHERE scope = 'catalog'").fetchone()[
            0
        ]
    finally:
        conn.close()
    assert profiles == {row["profile"] for row in second}
    assert set(fill.split(",")) == set(NEW_TO_BUNDLE + ("oc-solar4",))


def test_a_snapshot_filled_profile_keeps_the_snapshot_gate_not_a_wider_one(catalog_server):
    """Mutant invariant: the fill is never placed wider than the snapshot.

    oc-omni is escalation-gated in the bundle; while it is a bundle-new
    profile the snapshot fill applies its own gate, so it still needs
    --operator-request — the fill must not launder a wider placement.
    """

    _, fake = catalog_server
    canon = _canon_without(NEW_TO_BUNDLE + ("oc-omni",))
    _commit_canon(canon)
    _age_catalog_cache(3601)
    fake.catalog = canon
    bench.reset_catalog_memo()

    view = bench.read_catalog()
    assert view.source == "server"
    assert "oc-omni" in view.snapshot_fill
    with pytest.raises(launch.LaunchError, match="stale"):
        launch.resolve_launch("oc-omni")
    decision = launch.resolve_launch("oc-omni", operator_request=True)
    assert decision.gate == "escalation"
    assert decision.catalog_source == "snapshot"


def test_push_catalog_refetch_clears_the_same_floor(catalog_server, tmp_path, capsys):
    """B1 (tester round 1): the post-write refetch may not launder a
    regressed canon into the last-good cache.

    The server drops a cached profile *before* the push; the write succeeds
    but the refetched catalog fails the ratchet, so the refresh is skipped —
    the cache still holds the dropped profile and the next read keeps
    rejecting the regressed canon.
    """

    _, fake = catalog_server
    canon = _canon_without(NEW_TO_BUNDLE)
    _commit_canon(canon)
    _age_catalog_cache(3601)
    regressed = [row for row in canon if row["profile"] != "kimi-k3"]
    fake.catalog = regressed
    bench.reset_catalog_memo()

    payload = tmp_path / "push.json"
    payload.write_text(
        json.dumps({"catalog": [_row("push-fresh", "high", "m-1", "codex", "A")]}),
        encoding="utf-8",
    )
    assert bench.push_catalog(payload) == 1

    assert "refetched catalog was rejected" in capsys.readouterr().err
    conn = sqlite3.connect(bench.db_path())
    try:
        cached_profiles = {row[0] for row in conn.execute("SELECT profile FROM bench_cache_catalog")}
    finally:
        conn.close()
    assert "kimi-k3" in cached_profiles, "the regressed refetch must not overwrite the cache"

    # And the next read still rejects the regressed canon — the ratchet's
    # baseline was never laundered.
    _age_catalog_cache(3601)
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert "server catalog rejected" in view.detail
    assert "kimi-k3" not in {entry.profile for entry in view.entries} or view.source != "server"


# --- #1774: ratchet invariants the PR-164 suite left to the tester ----------
# Each test names the mutant it kills: a single-edit change that survived the
# shipped suite and only the hk 1768 tester's probes caught (hk:doc
# reports/b953/hk1768-verdict-r2; probes adapted from
# reports/b953/hk1768-tester-probes).

FILL_WARNING = "warning: catalog canon does not cover bundled"


def _fill_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith(FILL_WARNING)]


def _setup_pre_bundle(fake, *, age=3601, fill=()):
    """A pre-bundle last-good cache: the 66-row canon, aged past the TTL."""

    canon = _canon_without(NEW_TO_BUNDLE)
    _commit_canon(canon, snapshot_fill=fill)
    if age is not None:
        _age_catalog_cache(age)
    fake.catalog = canon
    bench.reset_catalog_memo()
    return canon


def _dump_cache():
    """Rows and meta, verbatim — a byte-level before/after for commit tests."""

    conn = sqlite3.connect(bench.db_path())
    try:
        rows = conn.execute("SELECT * FROM bench_cache_catalog ORDER BY profile, effort").fetchall()
        meta = conn.execute("SELECT * FROM bench_cache_meta WHERE scope = 'catalog'").fetchall()
    finally:
        conn.close()
    return rows, meta


def _set_meta(**fields):
    conn = sqlite3.connect(bench.db_path())
    try:
        for key, value in fields.items():
            conn.execute(f"UPDATE bench_cache_meta SET {key} = ? WHERE scope = 'catalog'", (value,))
        conn.commit()
    finally:
        conn.close()
    bench.reset_catalog_memo()


def _push_rows(profiles):
    return [
        {**row, "decided_by": "test", "decided_at": "2026-10-21T00:00:00Z"}
        for row in _seed_rows()
        if row["profile"] in set(profiles)
    ]


def _write_push(tmp_path, rows):
    src = tmp_path / "push.json"
    src.write_text(json.dumps({"catalog": rows}), encoding="utf-8")
    return src


def _cache_profiles() -> set:
    if not bench.db_path().exists():
        return set()
    conn = sqlite3.connect(bench.db_path())
    try:
        return {row[0] for row in conn.execute("SELECT profile FROM bench_cache_catalog")}
    finally:
        conn.close()


def _db_sha() -> str:
    import hashlib

    return hashlib.sha256(bench.db_path().read_bytes()).hexdigest()


def test_the_rejection_floor_keeps_its_empty_and_floor_semantics(catalog_server):
    """Unit matrix: empty always rejects; no last-good means the bundle floor."""

    snap = list(bench.catalog_snapshot())
    assert bench._server_catalog_rejection([]) == "empty catalog"
    assert bench._server_catalog_rejection([], snap) == "empty catalog"
    assert bench._server_catalog_rejection([], None) == "empty catalog"
    part = [entry for entry in snap if entry.profile != "devin-argon"]
    assert "missing 1 snapshot profile" in bench._server_catalog_rejection(part, None)
    assert "missing 1 snapshot profile" in bench._server_catalog_rejection(part, [])
    assert bench._server_catalog_rejection(part, part) is None
    assert "missing 1 snapshot profile" in bench._server_catalog_rejection(part, snap)


def test_a_retired_only_cached_profile_still_counts_for_the_ratchet(catalog_server):
    """M01: cached rows that are all retired still *mention* their profile.

    Dropping a profile whose only cached rows are retired is the same #667
    regression as dropping a live one — retirement is coverage, so the
    ratchet still rejects. Keeping it (retired) is a mention and stands.
    """

    _, fake = catalog_server
    retired = "2026-10-20T00:00:00Z"
    canon = _canon_without(NEW_TO_BUNDLE)
    canon = [({**row, "retired_at": retired} if row["profile"] == "devin-ds41" else row) for row in canon]
    _commit_canon(canon)
    _age_catalog_cache(3601)

    fake.catalog = [row for row in canon if row["profile"] != "devin-ds41"]
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert view.source == "cache"
    assert "missing 1 snapshot profile" in view.detail

    fake.catalog = canon
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert view.source == "server"
    with pytest.raises(launch.LaunchError, match="retired"):
        launch.resolve_launch("devin-ds41")


def test_a_dropped_profile_the_bundle_never_carried_is_named_cached(catalog_server):
    """M08: the rejection noun is "cached" for a non-bundled dropped profile.

    A server-only profile the bundle never carried is not a "snapshot"
    profile — and a mixed drop (bundled + server-only) keeps the noun too.
    """

    _, fake = catalog_server
    canon = _seed_rows() + [_row("brand-new", "high", "brand-new-1", "codex", "S", score=63.0)]
    _commit_canon(canon)
    _age_catalog_cache(3601)

    fake.catalog = _seed_rows()
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert view.source == "cache"
    assert "missing 1 cached profile" in view.detail

    fake.catalog = [row for row in _seed_rows() if row["profile"] != "kimi-k3"]
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert "missing 2 cached profiles" in view.detail


def test_the_fill_warning_fires_on_every_degraded_serve(catalog_server, capsys):
    """M10: rejected-to-cache, offline-to-cache and cache-stale serves all
    print exactly one fill warning, like the TTL serve does."""

    _, fake = catalog_server
    canon = _setup_pre_bundle(fake)
    bench.read_catalog()  # accept: attests the fill on the commit
    _age_catalog_cache(3601)

    # The server rejects its own catalog -> the cache serves.
    fake.catalog = [row for row in canon if row["profile"] != "kimi-k3"]
    bench.reset_catalog_memo()
    capsys.readouterr()
    view = bench.read_catalog()
    assert view.source == "cache" and view.detail
    assert len(_fill_lines(capsys.readouterr().err)) == 1

    # The server is unreachable -> the cache serves.
    fake.offline = True
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert view.source == "cache"
    assert len(_fill_lines(capsys.readouterr().err)) == 1

    # ... and past catalog_stale_max_s -> the cache-stale serve warns too.
    _age_catalog_cache(90000)
    view = bench.read_catalog()
    assert view.source == "cache-stale"
    assert len(_fill_lines(capsys.readouterr().err)) == 1


class _FailOnFillUpdate:
    """Connection proxy that fails the snapshot_fill UPDATE (fault injection)."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        if str(sql).lstrip().startswith("UPDATE bench_cache_meta SET snapshot_fill"):
            raise sqlite3.OperationalError("injected")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_the_fill_update_commits_with_the_rows_and_the_stamp(catalog_server, monkeypatch):
    """M11: rows, stamp and fill are one transaction.

    A fault in the fill UPDATE must roll the rows and the stamp back with it —
    committing them first would leave a cache whose fill attests nothing.
    """

    _commit_canon(_canon_without(NEW_TO_BUNDLE), snapshot_fill=NEW_TO_BUNDLE)
    before = _dump_cache()
    real = bench._cache_connect
    monkeypatch.setattr(bench, "_cache_connect", lambda path=None: _FailOnFillUpdate(real(path)))
    backend = bench.bench_backend(use="catalog")
    with pytest.raises(sqlite3.OperationalError):
        bench._commit_catalog_cache(
            path=None,
            entries=bench._catalog_from_payload({"catalog": _canon_without(NEW_TO_BUNDLE + ("oc-solar4",))}),
            backend=backend,
            snapshot_fill=tuple(sorted(NEW_TO_BUNDLE + ("oc-solar4",))),
        )
    assert _dump_cache() == before


def test_commit_cache_false_without_a_db_creates_nothing(catalog_server):
    """M14: the read-only path never creates the database."""

    _, fake = catalog_server
    fake.catalog = _seed_rows()
    path = bench.db_path()
    assert not path.exists()
    view = bench.read_catalog(commit_cache=False)
    assert view.source == "server"
    assert not path.exists()


def test_commit_cache_false_ratchets_and_never_writes(catalog_server, capsys):
    """M14: the read-only path honours the ratchet without touching the DB."""

    _, fake = catalog_server
    canon = _setup_pre_bundle(fake)
    before = _db_sha()
    bench.reset_catalog_memo()
    capsys.readouterr()
    view = bench.read_catalog(commit_cache=False)
    assert view.source == "server"
    assert view.snapshot_fill == tuple(sorted(NEW_TO_BUNDLE))
    assert len(_fill_lines(capsys.readouterr().err)) == 1
    assert _db_sha() == before
    # Dropping a cached profile is still rejected on the read-only path.
    fake.catalog = [row for row in canon if row["profile"] != "kimi-k3"]
    bench.reset_catalog_memo()
    view = bench.read_catalog(commit_cache=False)
    assert view.source == "cache" and "server catalog rejected" in view.detail
    assert _db_sha() == before


def test_commit_cache_false_survives_a_db_lacking_snapshot_fill(catalog_server):
    """M14+M15: a pre-fill DB is a usable cache, and the read-only path
    must not migrate it."""

    _, fake = catalog_server
    _setup_pre_bundle(fake)
    conn = sqlite3.connect(bench.db_path())
    conn.execute("ALTER TABLE bench_cache_meta DROP COLUMN snapshot_fill")
    conn.commit()
    conn.close()
    before = _db_sha()

    bench.reset_catalog_memo()
    view = bench.read_catalog(commit_cache=False)
    assert view.source == "server"  # fetched; the ratchet read the rows anyway
    assert _db_sha() == before
    conn = sqlite3.connect(bench.db_path())
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bench_cache_meta)")]
    finally:
        conn.close()
    assert "snapshot_fill" not in cols

    # The TTL serve on the same legacy DB attests no fill.
    _age_catalog_cache(60)
    before = _db_sha()
    bench.reset_catalog_memo()
    view = bench.read_catalog(commit_cache=False)
    assert view.source == "cache" and view.snapshot_fill == ()
    assert _db_sha() == before


def test_commit_cache_false_survives_a_db_lacking_the_meta_table(catalog_server):
    """M14: no meta table at all means no stamp, so the bundle floor decides —
    and the read-only path still writes nothing."""

    _, fake = catalog_server
    canon = _setup_pre_bundle(fake)
    conn = sqlite3.connect(bench.db_path())
    conn.execute("DROP TABLE bench_cache_meta")
    conn.commit()
    conn.close()
    before = _db_sha()
    fake.catalog = canon
    bench.reset_catalog_memo()
    view = bench.read_catalog(commit_cache=False)
    assert view.source == "snapshot"
    assert _db_sha() == before


def test_a_pre_fill_cache_reads_as_usable_with_no_attested_fill(catalog_server, capsys):
    """M15: a cache committed before snapshot_fill existed still serves; the
    gaps it cannot attest are listed under uncovered, never warned about."""

    path = bench.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE bench_cache_meta (scope TEXT PRIMARY KEY CHECK (scope IN "
        "('scores','reps','grades','catalog')), fetched_at TEXT NOT NULL, "
        "endpoint_id TEXT NOT NULL DEFAULT '')"
    )
    conn.commit()
    conn.close()
    canon = _canon_without(NEW_TO_BUNDLE)
    backend = bench.bench_backend(use="catalog")
    # The head build opens the DB (which adds the column); stamping without a
    # fill value emulates a pre-floor commit.
    conn = bench._cache_connect(None)
    try:
        for entry in bench._catalog_from_payload({"catalog": canon}):
            bench._put_cached_catalog(conn, entry)
        bench._stamp_cache(conn, "catalog", backend, bench._cache_now())
        conn.commit()
    finally:
        conn.close()
    assert _stored_fill() == ""
    bench.reset_catalog_memo()
    capsys.readouterr()

    assert cli.main(["bench", "catalog", "status"]) == 0
    cap = capsys.readouterr()
    assert not any(line.startswith("new to the bundle") for line in cap.out.splitlines())
    uncovered = next(line for line in cap.out.splitlines() if line.startswith("uncovered"))
    for profile in NEW_TO_BUNDLE:
        assert profile in uncovered
    assert not _fill_lines(cap.err)


def test_the_old_meta_table_migrates_with_the_fill_column(catalog_server):
    """A pre-#593-shape meta table gains snapshot_fill on first open, and a
    commit then records the attested fill end to end."""

    path = bench.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE bench_cache_meta (scope TEXT PRIMARY KEY CHECK (scope IN "
        "('scores','reps','grades')), fetched_at TEXT NOT NULL, "
        "endpoint_id TEXT NOT NULL DEFAULT '')"
    )
    conn.execute("INSERT INTO bench_cache_meta VALUES ('scores','2026-01-01T00:00:00+00:00','e')")
    conn.commit()
    conn.close()

    conn = bench._cache_connect(None)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bench_cache_meta)")]
    finally:
        conn.close()
    assert "snapshot_fill" in cols

    _commit_canon(_canon_without(NEW_TO_BUNDLE), snapshot_fill=NEW_TO_BUNDLE)
    assert set(_stored_fill().split(",")) == set(NEW_TO_BUNDLE)


def test_the_stored_fill_is_intersected_with_this_builds_bundle(catalog_server, capsys, monkeypatch):
    """M07: a profile the bundle dropped since the commit is not attested.

    The stored fill names profiles *new to this build's bundle*; a later build
    that drops one must not echo it on the warning or the view.
    """

    _setup_pre_bundle(catalog_server[1])
    bench.read_catalog()  # commits fill = NEW_TO_BUNDLE
    real = bench.snapshot_profiles
    monkeypatch.setattr(bench, "snapshot_profiles", lambda: real() - {"devin-argon"})
    bench.reset_catalog_memo()
    capsys.readouterr()

    view = bench.read_catalog()
    assert view.source == "cache"
    assert "devin-argon" not in view.snapshot_fill
    line = "".join(_fill_lines(capsys.readouterr().err))
    named = line.split("bundled profiles ")[1].split(";")[0].split(", ")
    assert "devin-argon" not in named
    assert "devin-argon-low" in named


def test_push_refetch_ratchet_is_the_cache_not_the_bundle(catalog_server, tmp_path, capsys):
    """N3/N7: the push-path floor is the last-good cache, not the bundle.

    The dropped profile is server-only — the bundle never carried it — so the
    bundle floor alone cannot reject the refetch (nor can an always-empty
    baseline). Only the cache ratchet does.
    """

    _, fake = catalog_server
    canon = _canon_without(NEW_TO_BUNDLE) + [_row("srv-only", "high", "srv-1", "codex", "S", score=63.0)]
    _commit_canon(canon)
    _age_catalog_cache(3601)
    fake.catalog = [row for row in canon if row["profile"] != "srv-only"]
    before = _dump_cache()
    capsys.readouterr()

    bench.push_catalog(_write_push(tmp_path, _push_rows(["devin-argon"])))
    err = capsys.readouterr().err
    assert "refetched catalog was rejected" in err
    assert "missing 1 cached profile" in err
    assert _dump_cache() == before
    assert "srv-only" in _cache_profiles()


def test_push_refetch_foreign_endpoint_cache_is_no_baseline(catalog_server, tmp_path, capsys):
    """N1: another endpoint's cache never ratchets the push path — the
    refetch is judged by the bundle floor and refreshes the cache."""

    _, fake = catalog_server
    canon = _canon_without(NEW_TO_BUNDLE) + [_row("srv-only", "high", "srv-1", "codex", "S", score=63.0)]
    _commit_canon(canon)
    _set_meta(endpoint_id="some-other-endpoint")
    fake.catalog = _seed_rows()  # the full bundle; lacks srv-only
    capsys.readouterr()

    bench.push_catalog(_write_push(tmp_path, _push_rows(["devin-argon"])))
    assert "was rejected" not in capsys.readouterr().err
    assert "srv-only" not in _cache_profiles()  # refreshed to the new endpoint's canon
    assert _stored_fill() == ""


def test_push_refetch_stale_cache_still_ratchets(catalog_server, tmp_path, capsys):
    """N4: membership is not freshness — a cache far past catalog_stale_max_s
    is still the push-path baseline. The server serves the complete bundle, so
    only the cache ratchet can reject the refetch."""

    _, fake = catalog_server
    canon = _canon_without(NEW_TO_BUNDLE) + [_row("srv-only", "high", "srv-1", "codex", "S", score=63.0)]
    _commit_canon(canon)
    _age_catalog_cache(100 * 86400)
    fake.catalog = _seed_rows()  # complete bundle coverage; drops srv-only
    capsys.readouterr()

    bench.push_catalog(_write_push(tmp_path, _push_rows(["devin-argon"])))
    err = capsys.readouterr().err
    assert "refetched catalog was rejected" in err
    assert "missing 1 cached profile" in err
    assert "srv-only" in _cache_profiles()


def test_push_refetch_bundle_only_gap_refreshes_and_records_the_fill(catalog_server, tmp_path, capsys):
    """M16: a gap that is bundle-new only does not reject — the refresh lands
    and the commit records the still-missing attested fill."""

    _, fake = catalog_server
    _setup_pre_bundle(fake)  # cache and server both lack the five additions
    capsys.readouterr()

    bench.push_catalog(_write_push(tmp_path, _push_rows(["devin-argon"])))
    assert "was rejected" not in capsys.readouterr().err
    assert "devin-argon" in _cache_profiles()
    assert set(_stored_fill().split(",")) == set(NEW_TO_BUNDLE) - {"devin-argon"}


def test_push_refetch_repair_push_restores_the_dropped_profile(catalog_server, tmp_path, capsys):
    """A push that restores the dropped profile refetches clean and the cache
    is complete again — the ratchet is a floor, not a wedge."""

    _, fake = catalog_server
    canon = _setup_pre_bundle(fake)
    fake.catalog = [row for row in canon if row["profile"] != "kimi-k3"]
    capsys.readouterr()

    repair = [
        {**row, "decided_by": "test", "decided_at": "2026-10-21T00:00:00Z"}
        for row in canon
        if row["profile"] == "kimi-k3"
    ]
    assert repair
    bench.push_catalog(_write_push(tmp_path, repair))
    assert "was rejected" not in capsys.readouterr().err
    assert {row["profile"] for row in canon} <= _cache_profiles()


def test_push_refetch_without_a_cache_uses_the_bundle_floor(catalog_server, tmp_path, capsys):
    """First run, no DB: a partial seed push is refused by the bundle floor
    and creates no cache; a complete seed is accepted and fills it."""

    _, fake = catalog_server
    fake.catalog = []
    capsys.readouterr()

    bench.push_catalog(_write_push(tmp_path, _push_rows(["devin-argon"])))
    err = capsys.readouterr().err
    assert "refetched catalog was rejected" in err and "snapshot" in err
    assert _cache_profiles() == set()

    full = [{**row, "decided_by": "test", "decided_at": "2026-10-21T00:00:00Z"} for row in _seed_rows()]
    capsys.readouterr()
    bench.push_catalog(_write_push(tmp_path, full))
    assert "was rejected" not in capsys.readouterr().err
    assert _cache_profiles() == _seed_profiles()


def test_push_refetch_cli_output_unchanged_on_a_rejected_refresh(catalog_server, tmp_path, capsys):
    """The write still reports what it wrote; a rejected refresh is a
    warning, not a failure."""

    _, fake = catalog_server
    canon = _canon_without(NEW_TO_BUNDLE) + [_row("srv-only", "high", "srv-1", "codex", "S", score=63.0)]
    _commit_canon(canon)
    _age_catalog_cache(3601)
    fake.catalog = [row for row in canon if row["profile"] != "srv-only"]
    src = _write_push(tmp_path, _push_rows(["devin-argon"]))
    capsys.readouterr()

    rc = cli.main(["bench", "push-catalog", str(src)])
    cap = capsys.readouterr()
    assert rc == 0
    assert f"catalog rows written: {len(_push_rows(['devin-argon']))}" in cap.out
    assert "refetched catalog was rejected" in cap.err


def test_the_last_good_gate_unit_matrix(catalog_server):
    """N1/N4/N5/N8: the stamp gate on the push-path baseline — no DB, no
    stamp, foreign endpoint, and garbage/empty/future stamps are no baseline;
    age is irrelevant."""

    import datetime as dt

    backend = bench.bench_backend(use="catalog")
    assert bench._last_good_catalog(path=None, backend=backend) == []  # no DB
    canon = _canon_without(NEW_TO_BUNDLE)
    _commit_canon(canon)
    got = bench._last_good_catalog(path=None, backend=backend)
    assert {e.profile for e in got} == {row["profile"] for row in canon}
    _age_catalog_cache(100 * 86400)
    assert bench._last_good_catalog(path=None, backend=backend)  # age is irrelevant
    for bad in (
        (dt.datetime.now(dt.UTC) + dt.timedelta(days=30)).isoformat(),
        "garbage",
        "",
    ):
        _set_meta(fetched_at=bad)
        assert bench._last_good_catalog(path=None, backend=backend) == [], bad
    _set_meta(fetched_at=dt.datetime.now(dt.UTC).isoformat(), endpoint_id="other")
    assert bench._last_good_catalog(path=None, backend=backend) == []
    conn = sqlite3.connect(bench.db_path())
    conn.execute("DELETE FROM bench_cache_meta WHERE scope = 'catalog'")
    conn.commit()
    conn.close()
    assert bench._last_good_catalog(path=None, backend=backend) == []


@pytest.mark.parametrize("serve", ["ttl", "rejected", "offline", "stale"])
def test_a_tampered_fill_never_names_a_covered_profile(catalog_server, capsys, serve):
    """R2: hand-edited meta naming a covered profile is not echoed — on any
    of the serve paths that use the stored fill (TTL, rejected-to-cache,
    offline-to-cache, cache-stale)."""

    _, fake = catalog_server
    canon = _setup_pre_bundle(fake)  # aged 3601 by default
    _set_meta(snapshot_fill="opus,devin-argon")
    if serve == "ttl":
        _age_catalog_cache(60)
    elif serve == "rejected":
        fake.catalog = [row for row in canon if row["profile"] != "kimi-k3"]
    elif serve == "offline":
        fake.offline = True
    else:  # stale
        _age_catalog_cache(90000)
        fake.offline = True
    bench.reset_catalog_memo()
    capsys.readouterr()

    view = bench.read_catalog()
    assert view.source == ("cache-stale" if serve == "stale" else "cache")
    assert view.snapshot_fill == ("devin-argon",)
    warned = "".join(_fill_lines(capsys.readouterr().err))
    assert "devin-argon" in warned
    assert "opus" not in warned

    out = bench.catalog_status_report()
    heading = next(line for line in out.splitlines() if line.startswith("new to the bundle"))
    assert "devin-argon" in heading and "opus" not in heading
    uncovered = next(line for line in out.splitlines() if line.startswith("uncovered"))
    assert "devin-argon-low" in uncovered


@pytest.mark.parametrize("serve", ["ttl", "rejected", "offline", "stale"])
def test_a_commit_landing_mid_read_never_names_a_covered_profile(catalog_server, capsys, monkeypatch, serve):
    """R1: the torn read cannot echo a covered profile — on any serve path.

    The rows SELECT and the meta SELECT are separate statements; a commit
    landing between them yields rows from one commit and the stamp+fill from
    another. The served profiles are subtracted from the stored fill, so the
    warning and the view still cannot name a profile the served entries cover.
    The racing commit's stamp is backdated so each case really takes its own
    serve path rather than always the TTL serve.
    """

    _, fake = catalog_server
    backend = bench.bench_backend(use="catalog")
    canon_a = _canon_without(NEW_TO_BUNDLE)  # covers oc-solar4
    canon_b = _canon_without(NEW_TO_BUNDLE + ("oc-solar4",))
    _commit_canon(canon_a, snapshot_fill=NEW_TO_BUNDLE)
    real = bench._catalog_meta_row
    state = {"done": False}
    age_s = {"ttl": 60, "rejected": 3601, "offline": 3601, "stale": 90000}[serve]

    def racing(conn):
        if not state["done"]:
            state["done"] = True
            bench._commit_catalog_cache(
                path=None,
                entries=bench._catalog_from_payload({"catalog": canon_b}),
                backend=backend,
                snapshot_fill=tuple(sorted(NEW_TO_BUNDLE + ("oc-solar4",))),
            )
            import datetime as dt

            stamp = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=age_s)).isoformat()
            conn.execute("UPDATE bench_cache_meta SET fetched_at = ? WHERE scope = 'catalog'", (stamp,))
        return real(conn)

    monkeypatch.setattr(bench, "_catalog_meta_row", racing)
    if serve == "rejected":
        fake.catalog = canon_b  # drops canon_a's oc-solar4 -> the fetch is refused
    if serve in ("offline", "stale"):
        fake.offline = True
    bench.reset_catalog_memo()
    capsys.readouterr()

    view = bench.read_catalog()
    assert state["done"] is True
    assert view.source == ("cache-stale" if serve == "stale" else "cache")
    served = {entry.profile for entry in view.entries}
    assert "oc-solar4" in served
    assert "oc-solar4" not in view.snapshot_fill
    assert set(view.snapshot_fill) == set(NEW_TO_BUNDLE)
    warned = "".join(_fill_lines(capsys.readouterr().err))
    assert "oc-solar4" not in warned
    assert "devin-argon" in warned


@pytest.mark.parametrize("bad_stamp", ["not-a-timestamp", "", "2026-13-45T00:00:00Z"])
def test_the_read_path_distrusts_an_unparseable_stamp(catalog_server, bad_stamp):
    """N8, read path: an unparseable stamp is no baseline and no cache.

    The cache holds a server-only profile; the fetched catalog covers the
    whole bundle but lacks it — only a trusted baseline could reject that
    catalog. With the stamp unparseable the bundle floor accepts it, and an
    unreachable server means the snapshot, not the unreadable cache.
    """

    _, fake = catalog_server
    canon = _canon_without(NEW_TO_BUNDLE) + [_row("srv-only", "high", "srv-1", "codex", "S", score=63.0)]
    _commit_canon(canon)
    _age_catalog_cache(3601)
    _set_meta(fetched_at=bad_stamp)

    fake.offline = True
    view = bench.read_catalog()
    assert view.source == "snapshot"
    fake.offline = False
    fake.catalog = _seed_rows()
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert view.source == "server"


def test_the_fill_warning_is_grammatical_in_the_singular(catalog_server, capsys):
    """The singular reads "filling it ... to add it"; the plural keeps
    "filling them ... to add them"."""

    _, fake = catalog_server
    canon = _canon_without(("devin-argon",))
    _commit_canon(canon)
    _age_catalog_cache(3601)
    fake.catalog = canon
    bench.reset_catalog_memo()
    capsys.readouterr()

    view = bench.read_catalog()
    assert view.snapshot_fill == ("devin-argon",)
    (line,) = _fill_lines(capsys.readouterr().err)
    assert "bundled profile devin-argon;" in line
    assert "filling it from the bundled snapshot" in line
    assert "to add it to the canon" in line

    _setup_pre_bundle(fake)
    capsys.readouterr()
    view = bench.read_catalog()
    assert len(view.snapshot_fill) == len(NEW_TO_BUNDLE)
    (line,) = _fill_lines(capsys.readouterr().err)
    assert "bundled profiles" in line
    assert "filling them from the bundled snapshot" in line
    assert "to add them to the canon" in line


def test_push_refetch_ratchet_counts_a_retired_only_cached_profile(catalog_server, tmp_path, capsys):
    """M01 on the push path: a cached profile whose rows are all retired
    still counts toward the baseline — the server serves the complete
    bundle, so only the cache ratchet can reject the refetch."""

    _, fake = catalog_server
    canon = _canon_without(NEW_TO_BUNDLE) + [
        _row("srv-only", "high", "srv-1", "codex", "S", score=63.0, retired_at="2026-10-20T00:00:00Z")
    ]
    _commit_canon(canon)
    _age_catalog_cache(3601)
    fake.catalog = _seed_rows()  # full bundle; srv-only hard-deleted
    capsys.readouterr()

    bench.push_catalog(_write_push(tmp_path, _push_rows(["devin-argon"])))
    err = capsys.readouterr().err
    assert "refetched catalog was rejected" in err
    assert "missing 1 cached profile" in err
    assert "srv-only" in _cache_profiles()
