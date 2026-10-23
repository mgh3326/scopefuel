"""hk 1410 — push-local identity, compare inputs, refused and unbound pending reps.

RED on main for S1-S4. Each test names the mutant that must bring it back to
RED: hostname restored in the origin, lost rows counted by compare, or the
write-ahead row deleted when the post-write GET does not bind.
"""

from __future__ import annotations

import socket
import sqlite3

import pytest
import test_reps_collision_1384 as collision

from scopefuel import bench, cli, grades

HK_URL = collision.HK_URL
BAND_BASE = collision.BAND_BASE


def _no_network(*_args, **_kwargs):
    raise AssertionError("network")


def _seed_local(monkeypatch, home, **overrides) -> None:
    collision._client_env(home, monkeypatch, backend="local")
    monkeypatch.setattr(bench, "request_json", _no_network)
    assert cli.main(collision._add_args(**overrides)) == 0


def _attach(monkeypatch, home, fake: collision.FakeRepStore) -> None:
    collision._client_env(home, monkeypatch)
    monkeypatch.setenv("HANDOFFKEEP_URL", HK_URL)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", collision.HK_TOKEN)
    monkeypatch.setattr(bench, "request_json", fake)


def _pin_host(monkeypatch, name: str = "raspberrypi") -> dict[str, str]:
    current = {"name": name}
    monkeypatch.setattr(socket, "gethostname", lambda: current["name"])
    return current


def _age_reps_cache() -> None:
    conn = bench._cache_connect()
    try:
        conn.execute(
            "UPDATE bench_cache_meta SET fetched_at = ? WHERE scope = 'reps'",
            ("2000-01-01T00:00:00+00:00",),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_mark(
    table: str, *, origin_id: int, notes: str, rounds: int, completed: int, task_ref: str
) -> None:
    """A lost or pending row using only the columns main already has."""

    conn = bench._cache_connect()
    try:
        if table == "bench_lost_reps":
            conn.execute(
                "INSERT INTO bench_lost_reps "
                "(cache_key, shadowed_by, lost_at, origin_id, created_by, profile, model_id, task_ref, "
                "tier, role, rounds, blockers_found, completed, input_tokens, output_tokens, notes, "
                "recorded_at, effort, grade, table_grade) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"lost:{origin_id}",
                    9,
                    "2026-10-10T00:00:00+00:00",
                    origin_id,
                    collision.CLIENT,
                    "builder-devin-max",
                    "swe-2-max",
                    task_ref,
                    "T1",
                    "impl",
                    rounds,
                    0,
                    completed,
                    None,
                    None,
                    notes,
                    "2026-10-10T00:00:00+00:00",
                    None,
                    "A",
                    "A",
                ),
            )
        else:
            conn.execute(
                "INSERT INTO bench_pending_reps "
                "(origin_id, profile, model_id, task_ref, tier, role, rounds, blockers_found, completed, "
                "input_tokens, output_tokens, notes, recorded_at, effort, grade, table_grade, queued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    origin_id,
                    "builder-devin-max",
                    "swe-2-max",
                    task_ref,
                    "T1",
                    "impl",
                    rounds,
                    0,
                    completed,
                    None,
                    None,
                    notes,
                    "2026-10-10T00:00:00+00:00",
                    None,
                    "A",
                    "A",
                    "2026-10-10T00:00:00+00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["old", "new"])
def test_s1_same_hostname_two_installs_keep_both_rows(tmp_path, monkeypatch, capsys, mode):
    """Two installs, one hostname, identical rep content.

    The notes and recorded_at match, so the content key does not separate
    them. Mutant: put the hostname back in place of the install id. Both
    installs then share an origin, each dialect ends at 1 server row, and
    the length assertion goes RED.
    """

    _pin_host(monkeypatch)
    fake = collision.FakeRepStore(HK_URL, mode=mode)
    stamp = "2026-10-10T00:00:00+00:00"
    for name in ("host-a", "host-b"):
        home = tmp_path / name
        _seed_local(monkeypatch, home, notes="shared rep")
        capsys.readouterr()
        conn = sqlite3.connect(bench.db_path())
        try:
            conn.execute("UPDATE reps SET recorded_at = ?", (stamp,))
            conn.commit()
        finally:
            conn.close()
        _attach(monkeypatch, home, fake)
        try:
            bench.push_local()
        except bench.BenchRepConflictError as exc:
            pytest.fail(f"push-local raised 409 on the {mode} server: {exc}")
    assert len(fake.reps) == 2
    assert len({row["origin_id"] for row in fake.reps}) == 2
    assert {row["notes"] for row in fake.reps} == {"shared rep"}


@pytest.mark.parametrize("mode", ["old", "new"])
def test_s1_recreated_db_reuses_rowid_without_overwrite_or_409(tmp_path, monkeypatch, capsys, mode):
    """A new bench.db mints a new install id, so rowid 1 can be a different rep."""

    _pin_host(monkeypatch)
    home = tmp_path / "host"
    fake = collision.FakeRepStore(HK_URL, mode=mode)
    _seed_local(monkeypatch, home, notes="first rep")
    capsys.readouterr()
    _attach(monkeypatch, home, fake)
    bench.push_local()
    assert len(fake.reps) == 1
    bench.db_path().unlink()

    _seed_local(monkeypatch, home, notes="different rep")
    capsys.readouterr()
    _attach(monkeypatch, home, fake)
    try:
        bench.push_local()
    except bench.BenchRepConflictError as exc:
        pytest.fail(f"recreated db is stuck on a permanent 409: {exc}")
    assert sorted(row["notes"] for row in fake.reps) == ["different rep", "first rep"]


@pytest.mark.parametrize("mode", ["old", "new"])
def test_s1_hostname_change_does_not_duplicate(tmp_path, monkeypatch, capsys, mode):
    """The origin does not contain the hostname, so a rename is one server row."""

    host = _pin_host(monkeypatch, "Robins-MBP.local")
    home = tmp_path / "host"
    fake = collision.FakeRepStore(HK_URL, mode=mode)
    _seed_local(monkeypatch, home, notes="same rep")
    capsys.readouterr()
    _attach(monkeypatch, home, fake)
    bench.push_local()
    first = fake.reps[0]["origin_id"]
    host["name"] = "Robins-MBP"
    bench.push_local()
    assert len(fake.reps) == 1
    assert fake.reps[0]["origin_id"] == first
    assert fake.reps[0]["notes"] == "same rep"


def test_s1_same_install_rowid_reuse_does_not_overwrite(tmp_path, monkeypatch, capsys):
    """Same bench.db, same rowid, different canonical fields: a second origin.

    The install id alone still collides here. The content key is what splits them.
    """

    _pin_host(monkeypatch)
    home = tmp_path / "host"
    fake = collision.FakeRepStore(HK_URL, mode="old")
    _seed_local(monkeypatch, home, notes="original")
    capsys.readouterr()
    _attach(monkeypatch, home, fake)
    bench.push_local()
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute("UPDATE reps SET notes = ? WHERE id = 1", ("rewritten",))
        conn.commit()
    finally:
        conn.close()
    try:
        bench.push_local()
    except bench.BenchRepConflictError as exc:
        pytest.fail(f"rowid reuse raised a permanent 409: {exc}")
    assert sorted(row["notes"] for row in fake.reps) == ["original", "rewritten"]


def test_s1_precheck_refuses_held_origin_with_zero_puts(tmp_path, monkeypatch, capsys):
    """A derived origin already held with different content is not PUT."""

    _pin_host(monkeypatch)
    home = tmp_path / "host"
    fake = collision.FakeRepStore(HK_URL, mode="new")
    _seed_local(monkeypatch, home, notes="local content")
    capsys.readouterr()
    _attach(monkeypatch, home, fake)
    record = bench._read_local_reps_for_push()[0]
    origin = bench._push_local_origin_id(socket.gethostname() or "local", record)
    fake.reps.append(
        collision._remote_row(id=7, origin_id=origin, notes="server content", task_ref="held-by-other")
    )
    with pytest.raises(bench.BenchRepConflictError) as excinfo:
        bench.push_local()
    message = str(excinfo.value)
    assert f"origin:{origin}" in message
    assert "srv:7" in message
    assert "nothing was written" in message
    assert fake.hits["PUT"] == 0
    assert [row["notes"] for row in fake.reps] == ["server content"]


def test_s1_corrupt_or_duplicated_install_id_refuses_before_put(tmp_path, monkeypatch, capsys):
    """A bad install id is not replaced. Replacing it would move every origin."""

    _pin_host(monkeypatch)
    home = tmp_path / "host"
    fake = collision.FakeRepStore(HK_URL, mode="old")
    _seed_local(monkeypatch, home, notes="kept")
    capsys.readouterr()
    _attach(monkeypatch, home, fake)
    bench._bench_install_id()
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute("UPDATE bench_install SET install_id = 'deadbeef'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(bench.BenchError, match="corrupt"):
        bench.push_local()
    assert fake.hits["PUT"] == 0

    db = bench.db_path()
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            DROP TABLE bench_install;
            CREATE TABLE bench_install (singleton INTEGER, install_id TEXT);
            """
        )
        conn.execute("INSERT INTO bench_install VALUES (1, ?)", ("a" * 32,))
        conn.execute("INSERT INTO bench_install VALUES (2, ?)", ("b" * 32,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(bench.BenchError, match="duplicated"):
        bench.push_local()
    assert fake.hits["PUT"] == 0


def test_s1_copied_bench_db_shares_the_install_id(tmp_path, monkeypatch, capsys):
    """A copied bench.db keeps one install id. Identical content resends.
    Different content at the same rowid does not overwrite the first row.
    """

    _pin_host(monkeypatch)
    source = tmp_path / "source"
    fake = collision.FakeRepStore(HK_URL, mode="old")
    _seed_local(monkeypatch, source, notes="shared")
    capsys.readouterr()
    _attach(monkeypatch, source, fake)
    bench.push_local()
    origin = fake.reps[0]["origin_id"]
    payload = bench.db_path().read_bytes()

    copied = tmp_path / "copied"
    collision._client_env(copied, monkeypatch)
    target = bench.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    _attach(monkeypatch, copied, fake)
    bench.push_local()
    assert len(fake.reps) == 1
    assert fake.reps[0]["origin_id"] == origin

    conn = sqlite3.connect(target)
    try:
        conn.execute("UPDATE reps SET notes = ? WHERE id = 1", ("diverged",))
        conn.commit()
    finally:
        conn.close()
    bench.push_local()
    assert sorted(row["notes"] for row in fake.reps) == ["diverged", "shared"]


def test_s2_compare_counts_only_server_confirmed_reps(tmp_path, monkeypatch, capsys):
    """Server holds 2 rows. One lost row and one refused pending row do not count.

    Mutant: count lost rows (or pending rows) in compare. count stops being 2.
    reps list still shows both, labelled.
    """

    fake = collision._remote(monkeypatch, tmp_path / "host")
    fake.reps.extend(
        [
            collision._remote_row(id=1, origin_id=11, notes="server-a", rounds=1, completed=1),
            collision._remote_row(id=2, origin_id=12, notes="server-b", rounds=1, completed=1),
        ]
    )
    _insert_mark(
        "bench_lost_reps",
        origin_id=BAND_BASE + 1,
        notes="lost-row",
        rounds=9,
        completed=0,
        task_ref="lost-task",
    )
    _insert_mark(
        "bench_pending_reps",
        origin_id=BAND_BASE + 2,
        notes="refused-row",
        rounds=5,
        completed=0,
        task_ref="refused-task",
    )
    conn = bench._cache_connect()
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(bench_pending_reps)").fetchall()}
        if "state" in columns:
            conn.execute(
                "UPDATE bench_pending_reps SET state = 'refused', conflict_server_id = 9, "
                "reason = 'bench_rep_conflict' WHERE notes = 'refused-row'"
            )
            conn.commit()
    finally:
        conn.close()
    comparisons = bench.compare_reps(grade="A")
    assert len(comparisons) == 1
    assert comparisons[0].count == 2
    assert comparisons[0].average_rounds == 1.0
    assert comparisons[0].completion_rate == 100.0

    assert cli.main(["reps", "list"]) == 0
    listed = capsys.readouterr().out
    assert "id=lost:" in listed and "lost-row" in listed
    assert "id=origin:" in listed and "refused-row" in listed
    assert "state=pending" in listed or "state=refused" in listed


def test_s2_grades_gather_reps_ignores_lost_and_pending(tmp_path, monkeypatch):
    """grades.gather_reps reads the server fetch, not the local lost/pending tables."""

    fake = collision._remote(monkeypatch, tmp_path / "host")
    fake.reps.extend(
        [
            collision._remote_row(id=1, origin_id=11, notes="server-a"),
            collision._remote_row(id=2, origin_id=12, notes="server-b"),
        ]
    )
    _insert_mark(
        "bench_lost_reps",
        origin_id=BAND_BASE + 3,
        notes="lost-row",
        rounds=9,
        completed=0,
        task_ref="lost-task",
    )
    _insert_mark(
        "bench_pending_reps",
        origin_id=BAND_BASE + 4,
        notes="refused-row",
        rounds=5,
        completed=0,
        task_ref="refused-task",
    )
    evidence = grades.gather_reps(view=bench.CatalogView(entries=(), source="server"))
    refs = [row.ref for row in evidence.rows]
    assert evidence.remote_count == 2
    assert sorted(ref for ref in refs if ref.startswith("srv:")) == ["srv:1", "srv:2"]
    assert not any(ref.startswith("lost:") or ref.startswith("origin:") for ref in refs)
    assert all(row.rep.notes != "lost-row" and row.rep.notes != "refused-row" for row in evidence.rows)


def test_s3_refused_readd_prints_state_and_drop_sends_nothing(tmp_path, monkeypatch, capsys):
    """409, then three identical re-adds fail locally: rc 2, no new origin.

    Each prints state=refused, the conflict id, and the pending-drop hint.
    Zero further PUTs. A fresh origin is not minted. pending list shows the
    row. pending drop removes only it and sends nothing. Only after that drop
    does an identical re-add start a new write.
    """

    fake = collision._remote(monkeypatch, tmp_path / "host")
    origin = BAND_BASE + 77
    fake.reps.append(collision._remote_row(id=4, origin_id=origin, task_ref="victim", notes="held"))
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: origin)
    assert cli.main(collision._add_args(task="attacker", notes="refused rep")) == 2
    capsys.readouterr()
    assert fake.hits["PUT"] == 1

    def _refuse_fresh_origin() -> int:
        raise AssertionError("identical re-add of a refused row minted a fresh origin")

    monkeypatch.setattr(bench, "_new_rep_origin_id", _refuse_fresh_origin)
    for _ in range(3):
        assert cli.main(collision._add_args(task="attacker", notes="refused rep")) == 2
        captured = capsys.readouterr()
        text = captured.out + captured.err
        assert "state=refused" in text
        assert "conflict-id=4" in text
        assert f"scopefuel reps pending drop {origin}" in text
        assert f"origin:{origin}" in text
        assert "srv:" not in text
    assert fake.hits["PUT"] == 1
    assert [(row["origin_id"], row["notes"]) for row in collision._pending_rows(tmp_path / "host")] == [
        (origin, "refused rep")
    ]

    assert cli.main(["reps", "pending", "list"]) == 0
    listed = capsys.readouterr().out
    assert f"origin={origin}" in listed
    assert "state=refused" in listed
    assert "profile=builder-devin-max" in listed
    assert "recorded_at=" in listed
    assert "conflict-id=4" in listed

    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: origin + 1)
    fake.fail_next_put = bench.HttpError(503, "")
    assert cli.main(collision._add_args(task="other", notes="still pending")) == 2
    capsys.readouterr()
    puts = fake.hits["PUT"]
    calls = {"n": 0}

    def _forbid_network(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("pending drop sent a request")

    monkeypatch.setattr(bench, "request_json", _forbid_network)
    assert cli.main(["reps", "pending", "drop", str(origin)]) == 0
    assert calls["n"] == 0
    assert fake.hits["PUT"] == puts
    conn = bench._cache_connect()
    try:
        left = [row["notes"] for row in conn.execute("SELECT notes FROM bench_pending_reps").fetchall()]
        cached = conn.execute("SELECT COUNT(*) FROM bench_cache_reps").fetchone()[0]
    finally:
        conn.close()
    assert left == ["still pending"]
    assert cached == 0

    monkeypatch.setattr(bench, "request_json", fake)
    assert cli.main(["reps", "pending", "drop", "4"]) == 2
    conn = bench._cache_connect()
    try:
        left = [row["notes"] for row in conn.execute("SELECT notes FROM bench_pending_reps").fetchall()]
    finally:
        conn.close()
    assert left == ["still pending"]

    fresh = origin + 2
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: fresh)
    puts = fake.hits["PUT"]
    rows_before = len(fake.reps)
    assert cli.main(collision._add_args(task="attacker", notes="refused rep")) == 0
    recorded = capsys.readouterr().out
    assert "recorded rep id=srv:" in recorded
    assert fake.hits["PUT"] == puts + 1
    assert len(fake.reps) == rows_before + 1
    assert any(row["origin_id"] == fresh and row["notes"] == "refused rep" for row in fake.reps)
    assert any(row["origin_id"] == origin and row["notes"] == "held" for row in fake.reps)
    pending = collision._pending_rows(tmp_path / "host")
    assert [row["notes"] for row in pending] == ["still pending"]
    assert all(row["origin_id"] != origin for row in pending)


@pytest.mark.parametrize(
    ("hook_name", "reason"),
    [("conflict", "conflict"), ("absent", "absent")],
)
def test_s4_unbound_write_survives_refresh(tmp_path, monkeypatch, capsys, hook_name, reason):
    """Old server, bind conflict or absent: the write-ahead row stays pending.

    Mutant: delete the write-ahead row when the post-write GET does not bind.
    The row is then gone after the next refresh, and this test goes RED.
    """

    fake = collision._remote(monkeypatch, tmp_path / "host", mode="old")
    victim_origin = BAND_BASE + 5
    if hook_name == "conflict":
        assert cli.main(collision._add_args(task="victim", notes="server content")) == 0
        capsys.readouterr()
        victim = dict(fake.reps[0])
        victim_origin = victim["origin_id"]
        monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: victim_origin)

        def _restore() -> None:
            fake.reps[:] = [victim]

        fake.after_put = _restore
    else:

        def _drop() -> None:
            fake.reps.clear()

        fake.after_put = _drop

    assert cli.main(collision._add_args(task="ours", notes="our unbound rep")) == 0
    captured = capsys.readouterr()
    assert "recorded rep id=origin:" in captured.out
    assert "srv:" not in captured.out
    pending = collision._pending_rows(tmp_path / "host")
    assert len(pending) == 1

    _age_reps_cache()
    assert cli.main(["reps", "list"]) == 0
    listed = capsys.readouterr().out
    assert collision._pending_rows(tmp_path / "host")
    ours = next(line for line in listed.splitlines() if "our unbound rep" in line)
    assert "state=pending" in ours
    assert f"reason={reason}" in ours
    assert "id=origin:" in ours
    for line in listed.splitlines():
        if "id=srv:" in line:
            assert "our unbound rep" not in line


def test_n1_deeply_nested_409_body_does_not_escape(tmp_path, monkeypatch, capsys):
    """RecursionError from a nested 409 body is not an uncaught traceback."""

    fake = collision._remote(monkeypatch, tmp_path / "host")
    marker = "BODYMARKER1410"
    depth = 1200
    body = '{"m":"' + marker + '","d":' + ("[" * depth) + "1" + ("]" * depth) + "}"
    fake.fail_next_put = bench.HttpError(409, body)
    try:
        code = cli.main(collision._add_args())
    except RecursionError as exc:
        pytest.fail(f"nested 409 body escaped as RecursionError: {exc}")
    captured = capsys.readouterr()
    assert code == 2
    assert marker not in captured.out
    assert marker not in captured.err


def test_n5_pending_done_clears_a_bound_row_outside_the_get_window(tmp_path, monkeypatch, capsys):
    """The PUT ids bind the row, but the GET page does not contain it.

    Reconcile cannot see our creator, so only the pending_done DELETE clears
    the write-ahead row. Dropping that DELETE leaves the row and turns RED.
    """

    fake = collision._remote(monkeypatch, tmp_path / "host")
    fake.reps.append(collision._remote_row(id=50, origin_id=BAND_BASE + 9, task_ref="decoy", notes="decoy"))
    real_fetch = bench._fetch_reps

    def _hide(backend, *, query=None):
        return [row for row in real_fetch(backend, query=query) if row.record.task_ref == "decoy"]

    monkeypatch.setattr(bench, "_fetch_reps", _hide)
    assert cli.main(collision._add_args(task="outside", notes="bound outside the window")) == 0
    assert "id=srv:" in capsys.readouterr().out
    assert collision._pending_rows(tmp_path / "host") == []
    assert len(fake.reps) == 2


def test_upgrade_from_base_client_db_adds_state_and_install_table(tmp_path, monkeypatch):
    """A bench.db from the base client gains the new columns without losing rows."""

    home = tmp_path / "base"
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    db = home / "data" / "scopefuel" / "bench.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE bench_cache_meta (
          scope TEXT PRIMARY KEY CHECK (scope IN ('scores', 'reps', 'grades', 'catalog')),
          fetched_at TEXT NOT NULL,
          endpoint_id TEXT NOT NULL DEFAULT '',
          snapshot_fill TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE bench_cache_reps (
          cache_key TEXT PRIMARY KEY,
          server_id INTEGER,
          origin_id INTEGER NOT NULL,
          created_by TEXT,
          profile TEXT NOT NULL,
          model_id TEXT,
          task_ref TEXT,
          tier TEXT,
          role TEXT,
          rounds INTEGER,
          blockers_found INTEGER,
          completed INTEGER,
          input_tokens INTEGER,
          output_tokens INTEGER,
          notes TEXT,
          recorded_at TEXT NOT NULL,
          effort TEXT,
          grade TEXT,
          table_grade TEXT
        );
        CREATE TABLE bench_pending_reps (
          origin_id INTEGER PRIMARY KEY,
          profile TEXT NOT NULL,
          model_id TEXT,
          task_ref TEXT,
          tier TEXT,
          role TEXT,
          rounds INTEGER,
          blockers_found INTEGER,
          completed INTEGER,
          input_tokens INTEGER,
          output_tokens INTEGER,
          notes TEXT,
          recorded_at TEXT NOT NULL,
          effort TEXT,
          grade TEXT,
          table_grade TEXT,
          queued_at TEXT NOT NULL
        );
        INSERT INTO bench_cache_meta (scope, fetched_at, endpoint_id)
        VALUES ('reps', '2026-10-01T00:00:00+00:00', 'endpoint');
        INSERT INTO bench_cache_reps (
          cache_key, server_id, origin_id, created_by, profile, model_id, task_ref, tier, role,
          rounds, blockers_found, completed, notes, recorded_at, grade, table_grade
        ) VALUES (
          'server:1', 1, 11, 'ops', 'builder-devin-max', 'swe-2-max', 'kept', 'T1', 'impl',
          1, 0, 1, 'cached', '2026-10-01T00:00:00+00:00', 'A', 'A'
        );
        INSERT INTO bench_pending_reps (
          origin_id, profile, model_id, task_ref, tier, role, rounds, blockers_found, completed,
          notes, recorded_at, grade, table_grade, queued_at
        ) VALUES (
          42, 'builder-devin-max', 'swe-2-max', 'queued', 'T1', 'impl', 1, 0, 1,
          'waiting', '2026-10-01T00:00:00+00:00', 'A', 'A', '2026-10-01T00:00:00+00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    conn = bench._cache_connect()
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(bench_pending_reps)").fetchall()}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        pending = conn.execute("SELECT origin_id, notes, state FROM bench_pending_reps").fetchone()
        cached = conn.execute("SELECT notes FROM bench_cache_reps").fetchone()
        meta = conn.execute("SELECT endpoint_id FROM bench_cache_meta WHERE scope = 'reps'").fetchone()
    finally:
        conn.close()
    assert {"state", "conflict_server_id", "reason"} <= columns
    assert "bench_install" in tables
    assert pending["origin_id"] == 42 and pending["notes"] == "waiting" and pending["state"] == "pending"
    assert cached["notes"] == "cached"
    assert meta["endpoint_id"] == "endpoint"
    first = bench._bench_install_id()
    assert len(first) == 32 and bench._bench_install_id() == first
