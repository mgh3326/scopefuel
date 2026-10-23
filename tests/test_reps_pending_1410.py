"""hk 1410: install identity and durable, non-counting local rep evidence.

All transports are in-process fixtures. The base SQL fixture was dumped by
the actual a2ad282 client before implementing this change.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import shutil
import socket
import sqlite3

import pytest
from test_reps_collision_1384 import (
    BAND_BASE,
    _add_args,
    _client_env,
    _remote,
    _remote_row,
    _seed_cache_row,
)

from scopefuel import bench, cli, grades


def _local(home, monkeypatch, *, notes="local"):
    _client_env(home, monkeypatch, backend="local")
    monkeypatch.setattr(bench, "_utc_now", lambda: "2026-10-23T00:00:00Z")
    assert cli.main(_add_args(notes=notes)) == 0


def _pending():
    conn = bench._cache_connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM bench_pending_reps ORDER BY origin_id")]
    finally:
        conn.close()


def _expire():
    conn = bench._cache_connect()
    try:
        conn.execute("DELETE FROM bench_cache_meta WHERE scope = 'reps'")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["old", "new"])
def test_s1_same_hostname_installs_keep_both_reps(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(socket, "gethostname", lambda: "same-host")
    first, second = tmp_path / "a", tmp_path / "b"
    _local(first, monkeypatch, notes="identical independent evidence")
    fake = _remote(monkeypatch, first, mode)
    assert bench.push_local() == (0, 1)
    _local(second, monkeypatch, notes="identical independent evidence")
    _client_env(second, monkeypatch)
    assert bench.push_local() == (0, 1)
    assert len(fake.reps) == 2
    assert {row["notes"] for row in fake.reps} == {"identical independent evidence"}
    assert len({row["origin_id"] for row in fake.reps}) == 2


@pytest.mark.parametrize("mode", ["old", "new"])
@pytest.mark.parametrize("recreate", [False, True])
def test_s1_reused_rowid_keeps_both_contents(tmp_path, monkeypatch, mode, recreate):
    _local(tmp_path, monkeypatch, notes="original")
    fake = _remote(monkeypatch, tmp_path, mode)
    bench.push_local()
    if recreate:
        bench.db_path().unlink()
    else:
        conn = bench.connect()
        conn.execute("DELETE FROM reps")
        conn.commit()
        conn.close()
    _local(tmp_path, monkeypatch, notes="replacement")
    _client_env(tmp_path, monkeypatch)
    bench.push_local()
    assert {row["notes"] for row in fake.reps} == {"original", "replacement"}
    assert len({row["origin_id"] for row in fake.reps}) == 2


@pytest.mark.parametrize("mode", ["old", "new"])
def test_s1_hostname_change_does_not_duplicate(tmp_path, monkeypatch, mode):
    _local(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path, mode)
    monkeypatch.setattr(socket, "gethostname", lambda: "before")
    bench.push_local()
    before = [dict(row) for row in fake.reps]
    monkeypatch.setattr(socket, "gethostname", lambda: "after")
    bench.push_local()
    assert fake.reps == before


@pytest.mark.parametrize("mode", ["old", "new"])
def test_s1_precheck_refuses_held_origin_without_put(tmp_path, monkeypatch, mode):
    _local(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path, mode)
    bench.push_local()
    fake.reps[0]["notes"] = "held by different content"
    puts = fake.hits["PUT"]
    with pytest.raises(bench.BenchError, match="refus|overwrite"):
        bench.push_local()
    assert fake.hits["PUT"] == puts
    assert fake.reps[0]["notes"] == "held by different content"


def test_s2_compare_only_server_confirmed_and_grades_unchanged(tmp_path, monkeypatch, capsys):
    fake = _remote(monkeypatch, tmp_path)
    fake.reps = [
        _remote_row(id=1, origin_id=10, rounds=1, input_tokens=10, output_tokens=20),
        _remote_row(id=2, origin_id=11, rounds=3, input_tokens=30, output_tokens=40),
    ]
    _seed_cache_row(
        tmp_path,
        cache_key="shadow",
        server_id=1,
        origin_id=99,
        notes="lost evidence",
        rounds=99,
        completed=0,
        table_grade="S",
    )
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: 10)
    assert cli.main(_add_args(notes="refused evidence")) == 2
    capsys.readouterr()
    # Include a retryable pending row and an anonymous legacy echo too.
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: BAND_BASE + 12)
    fake.fail_next_put = OSError("offline")
    assert cli.main(_add_args(notes="pending evidence")) == 2
    capsys.readouterr()
    _seed_cache_row(tmp_path, cache_key="anonymous", origin_id=98, rounds=99, completed=0)
    result = bench.compare_reps(grade="A")
    assert len(result) == 1
    assert result[0].count == 2
    assert result[0].average_rounds == 2
    assert result[0].average_blockers_found == 0
    assert result[0].completion_rate == 100
    assert result[0].average_input_tokens == 20
    assert result[0].average_output_tokens == 30
    assert (result[0].upward_count, result[0].same_count, result[0].downward_count) == (0, 2, 0)
    listed = bench.read_reps()
    assert any(bench.rep_ref(row).startswith("lost:") for row in listed)
    view = bench.CatalogView(
        entries=(),
        source=bench.CATALOG_SOURCE_SERVER,
        backend=bench.BENCH_BACKEND_HANDOFFKEEP,
        reason="configured",
    )
    evidence = grades.gather_reps(view=view, host="fixture")
    assert [row.ref for row in evidence.rows] == ["srv:2", "srv:1"]


def test_s3_refusal_blocks_identical_readds_until_exact_local_drop(tmp_path, monkeypatch, capsys):
    fake = _remote(monkeypatch, tmp_path)
    fake.reps = [_remote_row(id=7, origin_id=100, notes="held")]
    origins = iter([100, 200, 300])
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: next(origins))
    args = _add_args(notes="my refused rep")
    assert cli.main(args) == 2
    capsys.readouterr()
    assert _pending()[0].get("state") == "refused"
    assert _pending()[0]["conflict_server_id"] == 7
    puts = fake.hits["PUT"]
    for _ in range(3):
        assert cli.main(args) == 2
        output = capsys.readouterr()
        assert "refused" in output.err and "srv:7" in output.err
        assert "scopefuel reps pending drop 100" in output.err
        assert fake.hits["PUT"] == puts
        assert [row["origin_id"] for row in _pending()] == [100]
    assert cli.main(["reps", "list"]) == 0
    assert "[refused]" in capsys.readouterr().out
    fake.fail_next_put = OSError("offline")
    assert cli.main(_add_args(notes="another pending rep")) == 2
    capsys.readouterr()
    assert cli.main(["reps", "pending", "list"]) == 0
    listed = capsys.readouterr().out
    assert "origin:100" in listed and "refused" in listed and "srv:7" in listed
    assert "builder-devin-max" in listed and "recorded_at=" in listed
    assert "origin:200" in listed and "pending" in listed
    # Even credentials/backend resolution and all transport calls are forbidden.
    with monkeypatch.context() as local_only:
        local_only.setattr(bench, "request_json", lambda *a, **kw: pytest.fail("drop sent a request"))
        local_only.setattr(bench, "bench_backend", lambda **kw: pytest.fail("drop resolved credentials"))
        assert cli.main(["reps", "pending", "list"]) == 0
        assert cli.main(["reps", "pending", "drop", "100"]) == 0
        assert [row["origin_id"] for row in _pending()] == [200]
        assert cli.main(["reps", "pending", "drop", "7"]) == 2
    conn = bench._cache_connect()
    assert conn.execute("SELECT server_id FROM bench_cache_reps WHERE server_id = 7").fetchone()
    conn.close()
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: 300)
    assert cli.main(args) == 0
    assert fake.reps[-1]["origin_id"] == 300


@pytest.mark.parametrize("outcome", ["conflict", "absent"])
def test_s4_unbound_write_survives_refresh_with_marker_and_reason(tmp_path, monkeypatch, capsys, outcome):
    fake = _remote(monkeypatch, tmp_path, "old")
    if outcome == "conflict":
        fake.after_put = lambda: fake.reps[0].update(notes="other host")
    else:
        fake.after_put = fake.reps.clear
    assert cli.main(_add_args(notes="keep my evidence")) == 0
    capsys.readouterr()
    pending = _pending()
    assert len(pending) == 1
    assert pending[0]["state"] == "pending" and pending[0]["reason"] == outcome
    for _ in range(2):
        _expire()
        rows = bench.read_reps()
        ours = [row for row in rows if row.notes == "keep my evidence"]
        assert len(ours) == 1
        assert bench.rep_ref(ours[0]).startswith("origin:")
        line = bench.format_rep(ours[0])
        assert "[pending]" in line and f"reason={outcome}" in line
        assert len(_pending()) == 1
        comparison = bench.compare_reps(grade="A")
        if fake.reps:
            assert comparison[0].count == 1
        else:
            assert comparison == []


def test_upgrade_from_base_client_db_preserves_pending_payload(tmp_path, monkeypatch):
    _remote(monkeypatch, tmp_path)
    path = bench.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript((pathlib.Path(__file__).parent / "fixtures/reps_1410_base.sql").read_text())
    before = dict(
        zip(
            [col[1] for col in conn.execute("PRAGMA table_info(bench_pending_reps)")],
            conn.execute("SELECT * FROM bench_pending_reps").fetchone(),
            strict=True,
        )
    )
    conn.close()
    after = _pending()[0]
    assert {key: after[key] for key in before} == before
    assert after["state"] == "pending" and after["conflict_server_id"] is None
    assert after["reason"] is None


def test_n1_deep_409_body_is_sanitized(tmp_path, monkeypatch, capsys):
    _remote(monkeypatch, tmp_path)

    def nested(*args, **kwargs):
        raise bench.HttpError(409, "[" * 100000 + "0" + "]" * 100000)

    monkeypatch.setattr(bench, "request_json", nested)
    assert cli.main(_add_args()) == 2
    assert "refused" in capsys.readouterr().err


def test_n5_put_bound_outside_get_window_clears_pending(tmp_path, monkeypatch):
    fake = _remote(monkeypatch, tmp_path)

    # New server supplies the id; GET has no creator to reconcile against.
    def fill_window():
        fake.reps.extend(
            _remote_row(id=i, origin_id=i + 100, profile="other-profile") for i in range(2, 5002)
        )

    fake.after_put = fill_window
    rep = bench.add_rep(
        profile="builder-devin-max",
        model_id="swe-2-max",
        task_ref="1410",
        tier="T1",
        role="impl",
        grade="A",
        rounds=1,
        blockers_found=0,
        completed=1,
    )
    assert bench.rep_ref(rep) == "srv:1"
    assert len(fake.reps) == 5001 and fake.reps[0]["id"] == 1
    assert _pending() == []


@pytest.mark.parametrize("mode", ["old", "new"])
@pytest.mark.parametrize("failure", ["timeout", "500", "502", "503", "reset-after-commit", "get"])
def test_pending_failure_transitions_retry_identical_payload(tmp_path, monkeypatch, capsys, mode, failure):
    fake = _remote(monkeypatch, tmp_path, mode)
    if failure == "get":
        fail_get = [False]
        fake.after_put = lambda: fail_get.__setitem__(0, True)

        def request(url, **kwargs):
            if kwargs.get("method", "GET") == "GET" and fail_get[0]:
                fail_get[0] = False
                raise OSError("post-write GET failed")
            return fake(url, **kwargs)

        monkeypatch.setattr(bench, "request_json", request)
    else:
        fake.fail_next_put = (
            bench.HttpError(int(failure), "untrusted body") if failure.isdecimal() else OSError(failure)
        )
        fake.fail_next_put_after_write = failure == "reset-after-commit"
    args = _add_args(notes="uncertain write")
    assert cli.main(args) == 2
    assert "[pending]" in capsys.readouterr().err
    before = _pending()[0]
    assert before["state"] == "pending"
    # Repeated refreshes do not erase an intent, even after the write landed.
    for _ in range(2):
        _expire()
        rows = bench.read_reps()
        assert any(row.pending_state == "pending" for row in rows)
        assert _pending() == [before]
    assert cli.main(args) == 0
    assert fake.put_payloads[0] == fake.put_payloads[1]
    assert len(fake.reps) == 1
    assert _pending() == []


@pytest.mark.parametrize("mode", ["old", "new"])
@pytest.mark.parametrize("body", ["unparseable", "out-of-range-id"])
def test_refused_without_conflicting_id_is_terminal(tmp_path, monkeypatch, capsys, mode, body):
    fake = _remote(monkeypatch, tmp_path, mode)
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: 100)
    if body == "out-of-range-id":
        body = json.dumps({"conflicts": [{"index": 0, "origin_id": 100, "conflict_server_id": 1 << 63}]})
    fake.fail_next_put = bench.HttpError(409, body)
    args = _add_args()
    assert cli.main(args) == 2
    capsys.readouterr()
    row = _pending()[0]
    assert row["state"] == "refused" and row["conflict_server_id"] is None
    assert cli.main(args) == 2
    assert "[refused]" in capsys.readouterr().err
    assert fake.hits["PUT"] == 1 and _pending() == [row]


def test_refusal_survives_refresh_and_unrelated_confirmed_add(tmp_path, monkeypatch, capsys):
    fake = _remote(monkeypatch, tmp_path)
    fake.reps = [_remote_row(id=7, origin_id=100, notes="held")]
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: 100)
    assert cli.main(_add_args(notes="refused")) == 2
    before = _pending()
    _expire()
    bench.read_reps()
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: 200)
    assert cli.main(_add_args(notes="confirmed")) == 0
    assert _pending() == before


def test_new_server_postwrite_contradiction_keeps_pending(tmp_path, monkeypatch, capsys):
    fake = _remote(monkeypatch, tmp_path)
    fake.after_put = lambda: fake.reps[0].update(notes="contradiction")
    assert cli.main(_add_args(notes="mine")) == 0
    assert _pending()[0]["reason"] == "conflict"
    _expire()
    mine = next(row for row in bench.read_reps() if row.notes == "mine")
    assert bench.rep_ref(mine).startswith("origin:") and mine.pending_state == "pending"


def test_install_id_lifecycle_and_copy(tmp_path, monkeypatch):
    first, copied = tmp_path / "first", tmp_path / "copied"
    _local(first, monkeypatch)
    fake = _remote(monkeypatch, first)
    bench.push_local()
    install_id = bench._rep_install_id(None)
    assert len(install_id) == 32 and len(bytes.fromhex(install_id)) == 16
    source = bench.db_path()
    _client_env(copied, monkeypatch)
    target = bench.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    assert bench._rep_install_id(None) == install_id
    bench.push_local()
    assert len(fake.reps) == 1  # copied evidence is the same evidence
    conn = bench.connect()
    conn.execute("UPDATE reps SET notes = 'changed after copy'")
    conn.commit()
    conn.close()
    bench.push_local()
    assert len(fake.reps) == 2
    target.unlink()
    assert bench._rep_install_id(None) != install_id


@pytest.mark.parametrize("damage", ["corrupt", "duplicate"])
def test_bad_install_meta_fails_closed(tmp_path, monkeypatch, damage):
    _local(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    install_id = bench._rep_install_id(None)
    conn = bench._cache_connect()
    if damage == "corrupt":
        conn.execute("UPDATE bench_install_meta SET install_id = 'not a random id'")
    else:
        conn.execute("DROP TABLE bench_install_meta")
        conn.execute("CREATE TABLE bench_install_meta (singleton INTEGER, install_id TEXT)")
        conn.executemany("INSERT INTO bench_install_meta VALUES (1, ?)", [(install_id,), (install_id,)])
    conn.commit()
    conn.close()
    with pytest.raises(bench.BenchBackendError, match="corrupt install identity"):
        bench.push_local()
    assert fake.hits["PUT"] == 0


def test_precheck_runs_before_every_push_put(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    record = bench._read_local_reps_for_push()[0]
    records = [dataclasses.replace(record, id=i) for i in range(1, 502)]
    monkeypatch.setattr(bench, "_read_local_reps_for_push", lambda **kw: records)
    origin = bench._push_local_origin_id(bench._rep_install_id(None), records[-1])
    fake.after_put = lambda: fake.reps.append(_remote_row(id=900, origin_id=origin, notes="held"))
    with pytest.raises(bench.BenchRepConflictError):
        bench.push_local()
    assert fake.hits["PUT"] == 1
    assert all(row["origin_id"] != origin for batch in fake.put_payloads for row in batch)


def test_origin_hash_uses_all_canonical_fields_and_normalizes_timestamp(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    record = bench._read_local_reps_for_push()[0]
    install_id = bench._rep_install_id(None)
    origin = bench._push_local_origin_id(install_id, record)
    for field in bench._REP_COLUMNS:
        if field == "id":
            continue
        old = getattr(record, field)
        changed = 123 if old is None else old + 1 if isinstance(old, int) else old + " changed"
        assert (
            bench._push_local_origin_id(install_id, dataclasses.replace(record, **{field: changed})) != origin
        )
    same_instant = dataclasses.replace(record, recorded_at="2026-10-23T09:00:00+09:00")
    assert bench._push_local_origin_id(install_id, same_instant) == origin


def test_drop_unbound_pending_removes_only_its_local_echo(tmp_path, monkeypatch, capsys):
    fake = _remote(monkeypatch, tmp_path, "old")
    fake.after_put = lambda: fake.reps[0].update(notes="holder")
    assert cli.main(_add_args(notes="unbound")) == 0
    origin = _pending()[0]["origin_id"]
    assert cli.main(["reps", "pending", "drop", f"origin:{origin}"]) == 0
    assert _pending() == []
    assert all(row.notes != "unbound" for row in bench.read_reps())
    assert len(fake.reps) == 1 and fake.reps[0]["notes"] == "holder"


def _old_server_conflict(tmp_path, monkeypatch, capsys):
    """Old dialect: our PUT lands, then another host's content holds origin 100."""
    fake = _remote(monkeypatch, tmp_path, "old")
    fake.after_put = lambda: fake.reps[0].update(notes="other host")
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: 100)
    assert cli.main(_add_args(notes="mine")) == 0
    capsys.readouterr()
    assert [(row["origin_id"], row["state"], row["reason"]) for row in _pending()] == [
        (100, "pending", "conflict")
    ]
    return fake


def test_old_server_conflict_is_terminal_for_identical_readds(tmp_path, monkeypatch, capsys):
    """Mutant guard: without the conflict check in _open_pending_rep, a re-add
    resends origin 100 and the old server overwrites the other host's row, so
    the PUT count and the holder row go RED. A conflict re-add must mint no origin."""
    fake = _old_server_conflict(tmp_path, monkeypatch, capsys)
    row, holder, puts = _pending(), dict(fake.reps[0]), fake.hits["PUT"]
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: pytest.fail("minted a new origin"))
    for _ in range(3):
        assert cli.main(_add_args(notes="mine")) == 2
        err = capsys.readouterr().err
        assert "origin:100" in err and "[pending] reason=conflict" in err
        assert "scopefuel reps pending drop 100" in err
        assert "refused" not in err
        assert fake.hits["PUT"] == puts
        assert fake.reps == [holder]
        assert _pending() == row
    assert cli.main(["reps", "pending", "list"]) == 0
    listed = capsys.readouterr().out
    assert "origin:100" in listed and "reason=conflict" in listed


@pytest.mark.parametrize("reason", ["awaiting-confirmation", None])
def test_safe_pending_reasons_still_resend_identical_payload(tmp_path, monkeypatch, capsys, reason):
    fake = _remote(monkeypatch, tmp_path, "old")
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: 100)
    fake.fail_next_put = OSError("timeout")
    args = _add_args(notes="mine")
    assert cli.main(args) == 2
    capsys.readouterr()
    conn = bench._cache_connect()
    conn.execute("UPDATE bench_pending_reps SET reason = ?", (reason,))
    conn.commit()
    conn.close()
    assert _pending()[0]["state"] == "pending" and _pending()[0]["reason"] == reason
    assert cli.main(args) == 0
    assert [batch[0]["origin_id"] for batch in fake.put_payloads] == [100, 100]
    assert fake.put_payloads[0] == fake.put_payloads[1]
    assert len(fake.reps) == 1 and _pending() == []


def test_drop_conflict_then_identical_add_mints_fresh_origin(tmp_path, monkeypatch, capsys):
    fake = _old_server_conflict(tmp_path, monkeypatch, capsys)
    holder = dict(fake.reps[0])
    assert cli.main(["reps", "pending", "drop", "100"]) == 0
    capsys.readouterr()
    assert _pending() == []
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: 200)
    assert cli.main(_add_args(notes="mine")) == 0
    assert fake.put_payloads[-1][0]["origin_id"] == 200
    assert fake.reps[0] == holder
    assert sorted(row["origin_id"] for row in fake.reps) == [100, 200]
    assert _pending() == []
