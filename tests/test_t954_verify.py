"""t954-verify: independent counterexamples for PR #115 (catalog validity floor,
cache-stale, ``bench catalog status --check``). Tester-owned, outside the
worker's shapes. Fakes only — no network.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
import test_bench_catalog
from test_bench_catalog import _age_catalog_cache, _row, _seed_rows, _seed_rows_with

from scopefuel import bench, cli, grades, launch, recommend

# The worker's fixture, reused as-is: full-seed fake handoffkeep, 1h TTL, 24h stale max.
catalog_server = test_bench_catalog.catalog_server


def _dump_cache() -> tuple[list[tuple], list[tuple]]:
    """Full content of bench_cache_catalog and the catalog meta row."""

    conn = sqlite3.connect(bench.db_path())
    try:
        rows = conn.execute("SELECT * FROM bench_cache_catalog ORDER BY profile, effort").fetchall()
        meta = conn.execute("SELECT * FROM bench_cache_meta WHERE scope = 'catalog'").fetchall()
    finally:
        conn.close()
    return rows, meta


def _set_meta(**fields) -> None:
    conn = sqlite3.connect(bench.db_path())
    try:
        for key, value in fields.items():
            conn.execute(f"UPDATE bench_cache_meta SET {key} = ? WHERE scope = 'catalog'", (value,))
        conn.commit()
    finally:
        conn.close()
    bench.reset_catalog_memo()


def _emitted_seed(capsys) -> list[dict]:
    capsys.readouterr()
    rc = cli.main(
        ["bench", "push-catalog", "--emit-seed", "--decided-by", "test", "--deviation-ref", "hk:task/954"]
    )
    assert rc == 0
    return json.loads(capsys.readouterr().out)["catalog"]


def _placements() -> dict[tuple[str, str], str]:
    return {
        (p.name, p.launcher_effort): grade
        for grade, profiles in bench.runtime_grade_table().items()
        for p in profiles
    }


# --- X1: the real emitted seed passes the floor (day-one activation) --------


def test_x1_the_real_emitted_seed_is_accepted_as_canon(catalog_server, capsys, monkeypatch):
    _, fake = catalog_server
    seed = _emitted_seed(capsys)
    assert len(seed) == len(bench.catalog_snapshot())
    assert bench._server_catalog_rejection(bench._catalog_from_payload({"catalog": seed})) is None

    fake.catalog = seed
    bench.reset_catalog_memo()
    monkeypatch.setattr(cli, "registry", lambda: {})
    assert cli.main(["--recommend", "A+"]) == 0
    assert "catalog=server (age 0.0h)" in capsys.readouterr().out
    assert cli.main(["bench", "catalog", "status", "--check"]) == 0


# --- X2: AC1 with a non-empty (foreign-endpoint) cache table -----------------


def test_x2_rejected_catalog_leaves_a_foreign_endpoint_cache_byte_identical(
    catalog_server, capsys, monkeypatch
):
    """The table is non-empty (another endpoint's rows) so "unchanged" is not
    trivially true; the foreign cache must not be served either."""

    _, fake = catalog_server
    fake.catalog = _seed_rows_with("opus", "high", grade="A")
    bench.read_catalog()
    _set_meta(endpoint_id="some-other-endpoint")
    before = _dump_cache()
    assert len(before[0]) == len(bench.catalog_snapshot())

    fake.catalog = [_row("opus", "high", "claude-opus-5-5", "claude", "B")]
    monkeypatch.setattr(cli, "registry", lambda: {})
    assert cli.main(["--recommend", "A+"]) == 0
    missing = len(bench.snapshot_profiles()) - 1
    out = capsys.readouterr().out
    assert f"catalog=stale (server catalog rejected: 1 row, missing {missing} snapshot profiles)" in out
    served = _placements()
    expected = {
        (p.name, p.launcher_effort): grade
        for grade, profiles in recommend.GRADE_TABLE.items()
        for p in profiles
    }
    assert served == expected
    assert served[("opus", "high")] == "S+"  # neither the foreign cache's A nor the rejected B
    assert _dump_cache() == before


# --- X3 (mutant MA invariant): a rejected catalog never updates the cache ----


@pytest.mark.parametrize("age", [3601, 90000])
def test_x3_a_rejected_catalog_never_updates_fetched_at_or_rows(catalog_server, age):
    _, fake = catalog_server
    fake.catalog = _seed_rows_with("opus", "high", grade="A")
    bench.read_catalog()
    _age_catalog_cache(age)
    before = _dump_cache()

    fake.catalog = [_row("opus", "high", "claude-opus-5-5", "claude", "B")]
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert view.source == ("cache" if age < 86400 else "cache-stale")
    assert view.detail.startswith("server catalog rejected: 1 row")
    assert "server catalog rejected: 1 row" in view.label
    # rows served are the cache's, never the rejected rows
    assert _placements()[("opus", "high")] == "A"
    assert _dump_cache() == before, "a rejected catalog must not touch rows or fetched_at"


# --- X4 (mutant MB invariant): cache-stale never widens a non-default gate ---


def test_x4_cache_stale_never_widens_any_non_default_gate(catalog_server):
    _, fake = catalog_server
    bench.read_catalog()
    _age_catalog_cache(90000)
    fake.offline = True
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert view.source == "cache-stale"

    gated = [
        e
        for e in view.entries
        if e.gate not in (launch.GATE_DEFAULT, launch.GATE_CONSULT_ONLY)
        and not e.retired_at
        and e.grade != "C"
    ]
    assert gated, "the seed must carry at least one escalation-style gate"
    refused = 0
    for entry in gated:
        with pytest.raises(launch.LaunchError, match="stale") as info:
            launch.resolve_launch(entry.profile, effort=entry.effort or None)
        assert "cannot widen" in str(info.value)
        refused += 1
        decision = launch.resolve_launch(entry.profile, effort=entry.effort or None, operator_request=True)
        assert decision.catalog_source == "cache-stale" and decision.catalog_stale is True
    assert refused == len(gated)
    assert view.stale is True


# --- X5: request count with a persistently rejecting server (RISK probe) ----


def test_x5_rejecting_server_is_refetched_on_every_call_and_check_fetches_once(
    catalog_server, monkeypatch, capsys
):
    _, fake = catalog_server
    fake.catalog = [_row("opus", "high", "claude-opus-5-5", "claude", "S+")]
    monkeypatch.setattr(cli, "registry", lambda: {})
    for _ in range(5):
        bench.reset_catalog_memo()  # one process per call
        assert cli.main(["--recommend", "A+"]) == 0
    assert fake.hits[("GET", "catalog")] == 5  # no TTL suppression for a rejecting server

    bench.reset_catalog_memo()
    fake.hits.clear()
    assert cli.main(["bench", "catalog", "status", "--check"]) == 2
    assert fake.hits[("GET", "catalog")] == 1, "--check must reuse the memoised view"


# --- X6: endpoint mismatch / future stamp past stale_max -> snapshot ---------


def test_x6_a_foreign_or_future_cache_is_never_served_as_cache_stale(catalog_server):
    import datetime as dt

    _, fake = catalog_server
    fake.catalog = _seed_rows_with("opus", "high", grade="A")
    bench.read_catalog()
    _age_catalog_cache(90000)
    fake.offline = True

    _set_meta(endpoint_id="other")
    view = bench.read_catalog()
    assert view.source == "snapshot" and view.label == "catalog=stale (snapshot)"
    assert _placements()[("opus", "high")] == "S+"

    bench.reset_catalog_memo()
    future = (dt.datetime.now(dt.UTC) + dt.timedelta(days=3)).isoformat()
    _set_meta(endpoint_id=bench.bench_backend(use="catalog").endpoint_id, fetched_at=future)
    view = bench.read_catalog()
    assert view.source == "snapshot" and view.age_s is None


# --- X7: the AC5 rc matrix, every source ------------------------------------


def _check(capsys) -> tuple[int, str, str]:
    bench.reset_catalog_memo()
    capsys.readouterr()
    rc = cli.main(["bench", "catalog", "status", "--check"])
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_x7_check_rc_matrix(catalog_server, capsys):
    _, fake = catalog_server
    prefixes = ("backend=", "credentials ", "catalog_ttl_s=", "catalog=", "rows=")

    # server
    rc, out, err = _check(capsys)
    assert (rc, err) == (0, "")
    # cache, fresh, clean
    rc, out, err = _check(capsys)
    assert rc == 0 and "catalog=cache (age 0.0h)" in out
    # cache, ttl < age < stale_max, unreachable
    _age_catalog_cache(7200)
    fake.offline = True
    rc, out, err = _check(capsys)
    assert rc == 2 and "check failed: catalog=cache (age 2.0h)" in err
    # cache, rejected
    fake.offline = False
    fake.catalog = []
    rc, out, err = _check(capsys)
    assert rc == 2 and "server catalog rejected: empty catalog" in err
    # cache-stale
    _age_catalog_cache(90000)
    fake.offline = True
    rc, out, err = _check(capsys)
    assert rc == 2 and "check failed: catalog=cache-stale" in err
    # snapshot (no cache)
    conn = sqlite3.connect(bench.db_path())
    conn.execute("DELETE FROM bench_cache_catalog")
    conn.execute("DELETE FROM bench_cache_meta WHERE scope = 'catalog'")
    conn.commit()
    conn.close()
    rc, out, err = _check(capsys)
    assert rc == 2 and "check failed: catalog=stale (snapshot)" in err
    # unsupported (404)
    fake.offline = False
    fake.catalog = None
    rc, out, err = _check(capsys)
    assert rc == 2 and "check failed: catalog=unsupported" in err
    for line_prefix in prefixes:
        assert any(line.startswith(line_prefix) for line in out.splitlines()), line_prefix


# --- X8: all rows retired passes the floor (RISK probe) ---------------------


def test_x8_an_all_retired_catalog_passes_the_floor_and_refuses_every_profile(catalog_server, capsys):
    _, fake = catalog_server
    fake.catalog = [{**row, "retired_at": "2026-09-29T00:00:00Z"} for row in _seed_rows()]
    view = bench.read_catalog()
    assert view.source == "server"
    refused = []
    for profile in sorted(bench.snapshot_profiles()):
        capsys.readouterr()
        rc = cli.main(["policy", "launch", profile, "--json"])
        if rc != 0:
            refused.append(profile)
    assert refused == sorted(bench.snapshot_profiles())


# --- X9: grade proposals on a cache-stale view are degraded -----------------


def test_x9_cache_stale_view_is_a_degraded_input_for_grades_apply():
    view = bench.CatalogView(
        entries=bench.catalog_snapshot(),
        source=bench.CATALOG_SOURCE_CACHE_STALE,
        age_s=90000.0,
        backend=bench.BENCH_BACKEND_HANDOFFKEEP,
        reason="configured",
        detail="server unreachable",
    )
    evidence = grades.RepsEvidence(
        backend=bench.BENCH_BACKEND_HANDOFFKEEP,
        backend_reason="configured",
        host="h",
        remote_count=0,
        local_count=0,
        window_incomplete=False,
        rows=[],
    )
    reasons = grades.degraded_reasons(view, evidence)
    assert reasons and "stale cache past catalog_stale_max_s" in reasons[0]
    assert "catalog=cache-stale (age 25.0h; server unreachable)" in reasons[0]


# --- X10: AC2 end-to-end — one profile retired server-side, via --recommend --


def test_x10_a_full_seed_with_one_profile_retired_is_canon_and_the_rung_leaves_recommend(
    catalog_server, capsys, monkeypatch
):
    _, fake = catalog_server
    monkeypatch.setattr(cli, "registry", lambda: {})
    target = "agy-flash"
    grade = next(
        g for g, profiles in recommend.GRADE_TABLE.items() if any(p.name == target for p in profiles)
    )

    assert cli.main(["--recommend", grade]) == 0
    assert target in capsys.readouterr().out  # control: present before the retirement

    fake.catalog = [
        {**row, "retired_at": "2026-09-29T00:00:00Z"} if row["profile"] == target else row
        for row in _seed_rows()
    ]
    bench.reset_catalog_memo()
    _age_catalog_cache(3601)
    assert cli.main(["--recommend", grade]) == 0
    out = capsys.readouterr().out
    assert "catalog=server (age 0.0h)" in out
    assert target not in out
    assert not any(p.name == target for profiles in bench.runtime_grade_table().values() for p in profiles)


# --- X11: AC8 Known-limit claim — a pre-floor partial cache past stale_max ---


def test_x11_status_lists_a_pre_floor_partial_caches_gaps_even_past_stale_max(catalog_server, capsys):
    """docs/catalog-server-mode.md Known limit: a pre-floor partial cache "keeps
    serving under the same staleness rules, and `bench catalog status` still
    lists its uncovered profiles"."""

    _, fake = catalog_server
    bench.read_catalog()  # a full cache, then trim it to a pre-floor partial canon
    conn = sqlite3.connect(bench.db_path())
    conn.execute("DELETE FROM bench_cache_catalog WHERE profile != 'opus'")
    conn.commit()
    conn.close()
    fake.offline = True

    _age_catalog_cache(7200)  # inside stale_max: source cache
    capsys.readouterr()
    assert cli.main(["bench", "catalog", "status"]) == 0
    inside = capsys.readouterr().out
    assert "catalog=cache (age 2.0h)" in inside
    assert "uncovered (snapshot-only, catalog has no row):" in inside

    _age_catalog_cache(90000)  # past stale_max: source cache-stale
    capsys.readouterr()
    assert cli.main(["bench", "catalog", "status"]) == 0
    past = capsys.readouterr().out
    assert "catalog=cache-stale (age 25.0h; server unreachable)" in past
    assert "uncovered (snapshot-only, catalog has no row):" in past
