"""hk 1890: legacy identity, independently seeded, against both server dialects."""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3

import pytest
from test_reps_collision_1384 import _add_args, _client_env, _remote

from scopefuel import bench, cli


@pytest.fixture(params=["old", "new"])
def legacy(tmp_path, monkeypatch, request):
    monkeypatch.setattr(socket, "gethostname", lambda: "current-host")
    monkeypatch.setattr(bench, "_utc_now", lambda: "2026-10-24T01:02:03.123456Z")
    _client_env(tmp_path, monkeypatch, backend="local")
    for number in range(3):
        assert cli.main(_add_args(task=f"legacy-{number}", notes=f"evidence {number}")) == 0
    fake = _remote(monkeypatch, tmp_path, request.param)
    return fake


def _origin(host, rep):
    # Frozen pre-35bf3b3 formula, not the implementation under test.
    digest = hashlib.sha256(f"reps-push-local\x00{host}\x00{rep.profile}\x00{rep.id}".encode()).digest()
    return (1 << 62) + int.from_bytes(digest[:8], "big") % (1 << 62)


def _seed(fake, *, host="current-host", owner="legacy-shared", reps=None):
    for rep in reps if reps is not None else bench._read_local_reps_for_push():
        row = {column: getattr(rep, column) for column in bench._REP_COLUMNS}
        row.update(
            id=len(fake.reps) + 1,
            origin_id=_origin(host, rep),
            created_by=owner,
            created_at="2026-10-24T09:00:00Z",
        )
        fake.reps.append(row)


def _migrate(*, apply=False, hosts=(), accept=()):
    return bench.migrate_push_local(
        apply=apply, legacy_created_by=["legacy-shared"], legacy_hosts=list(hosts), accept_new=list(accept)
    )


def _dump():
    conn = sqlite3.connect(bench.db_path())
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def _table(name):
    conn = sqlite3.connect(bench.db_path())
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {name}")]
    finally:
        conn.close()


def _edit(sql, params=()):
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_duplicate_fixed_legacy_rows_are_never_reput(legacy):
    """Invariant: a proven legacy identity is skipped, even under a new bearer identity."""
    _seed(legacy)
    before = json.dumps(legacy.reps, sort_keys=True)
    result = _migrate(apply=True)
    assert len(result.confirmed) == 3
    assert bench.push_local() == (0, 0)
    assert legacy.hits["PUT"] == 0
    assert json.dumps(legacy.reps, sort_keys=True) == before
    mapped = _table("bench_legacy_identity")
    assert {row["server_id"] for row in mapped} == {1, 2, 3}
    assert {row["legacy_created_by"] for row in mapped} == {"legacy-shared"}
    assert {row["decision"] for row in mapped} == {"confirmed"}


def test_fresh_install_requires_receipt_then_uses_current_origins(legacy, capsys):
    assert cli.main(["bench", "push-local"]) == 2
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and "bench push-local-migrate" in err
    assert legacy.hits["PUT"] == 0
    result = _migrate(apply=True)
    assert len(result.never_pushed) == 3 and not result.confirmed
    assert _table("bench_legacy_identity") == []
    assert bench.push_local() == (0, 3)
    install = _table("bench_install_meta")[0]["install_id"]
    local = bench._read_local_reps_for_push()
    assert {row["origin_id"] for row in legacy.reps} == {
        bench._push_local_origin_id(install, rep) for rep in local
    }
    assert len(legacy.reps) == 3


def test_new_rows_after_migration_and_second_push(legacy, tmp_path, monkeypatch):
    _seed(legacy)
    _migrate(apply=True)
    legacy_before = json.dumps(legacy.reps, sort_keys=True)
    _client_env(tmp_path, monkeypatch, backend="local")
    assert cli.main(_add_args(task="new", notes="new evidence")) == 0
    _client_env(tmp_path, monkeypatch)
    assert bench.push_local() == (0, 1)
    assert bench.push_local() == (0, 1)
    assert len(legacy.reps) == 4
    assert json.dumps(legacy.reps[:3], sort_keys=True) == legacy_before
    assert all(len(payload) == 1 and payload[0]["task_ref"] == "new" for payload in legacy.put_payloads)


def test_never_content_alone(legacy):
    """Invariant: equal content without a candidate origin never confirms identity."""
    _seed(legacy)
    for row in legacy.reps:
        row["origin_id"] = row["id"] + 100
    result = _migrate(apply=True)
    assert len(result.never_pushed) == 3 and not result.confirmed
    assert bench.push_local() == (0, 3)
    assert len(legacy.reps) == 6
    assert bench.push_local() == (0, 3)
    assert len(legacy.reps) == 6


def test_candidate_origin_under_other_creator_does_not_confirm(legacy):
    """Invariant: only explicit legacy creators can confirm, regardless of content/origin."""
    _seed(legacy, owner="another-token")
    result = _migrate(apply=True)
    assert len(result.never_pushed) == 3 and not result.confirmed
    assert bench.push_local() == (0, 3)
    assert len(legacy.reps) == 6


@pytest.mark.parametrize("supply_former_host", [False, True])
def test_hostname_change_requires_explicit_former_host(legacy, supply_former_host):
    _seed(legacy, host="former-host")
    result = _migrate(apply=True, hosts=["former-host"] if supply_former_host else [])
    assert len(result.confirmed) == (3 if supply_former_host else 0)
    assert len(result.never_pushed) == (0 if supply_former_host else 3)
    bench.push_local()
    assert len(legacy.reps) == (3 if supply_former_host else 6)


def test_conflict_blocks_until_accept_new_and_preserves_legacy(legacy, capsys):
    _seed(legacy)
    legacy.reps[0]["notes"] = "other host or edited evidence"
    before = json.dumps(legacy.reps, sort_keys=True)
    result = _migrate(apply=True)
    assert len(result.conflict) == 1 and len(result.confirmed) == 2
    assert cli.main(["bench", "push-local"]) == 2
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and "conflict=1" in err and "bench push-local-migrate" in err
    assert legacy.hits["PUT"] == 0
    _migrate(apply=True, accept=[1])
    decision = next(row for row in _table("bench_legacy_identity") if row["rowid"] == 1)
    assert decision["decision"] == "accept-new" and decision["server_id"] is None
    assert bench.push_local() == (0, 1)
    assert bench.push_local() == (0, 1)
    assert len(legacy.reps) == 4
    assert json.dumps(legacy.reps[:3], sort_keys=True) == before


def test_two_confirming_hosts_are_ambiguous(legacy):
    _seed(legacy)
    _seed(legacy, host="other-host")
    result = _migrate(apply=True, hosts=["other-host"])
    assert len(result.ambiguous) == 3 and not result.confirmed
    with pytest.raises(bench.BenchError, match="ambiguous=3.*bench push-local-migrate"):
        bench.push_local()
    assert legacy.hits["PUT"] == 0
    _migrate(apply=True, hosts=["other-host"], accept=[1, 2, 3])
    assert bench.push_local() == (0, 3)
    assert len(legacy.reps) == 9


def test_other_host_differing_slot_is_conflict_even_with_one_confirmation(legacy):
    _seed(legacy)
    _seed(legacy, host="other-host")
    for row in legacy.reps[3:]:
        row["notes"] = "different host evidence"
    result = _migrate(apply=True, hosts=["other-host"])
    assert len(result.conflict) == 3 and not result.confirmed
    with pytest.raises(bench.BenchError, match="conflict=3"):
        bench.push_local()
    assert legacy.hits["PUT"] == 0


def test_dry_run_purity_and_class_output(legacy, capsys):
    """Invariant: planning changes neither the DB dump nor any remote row; GET only."""
    _seed(legacy)
    legacy.reps[0]["notes"] = "edited"
    _seed(legacy, host="old-host", reps=bench._read_local_reps_for_push()[1:2])
    legacy.reps[2]["origin_id"] = 123
    db_before, server_before, puts = _dump(), json.dumps(legacy.reps, sort_keys=True), legacy.hits["PUT"]
    assert (
        cli.main(
            [
                "bench",
                "push-local-migrate",
                "--legacy-created-by",
                "legacy-shared",
                "--legacy-host",
                "old-host",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    for expected in ("confirmed=0", "never-pushed=1", "conflict=1", "ambiguous=1"):
        assert expected in out
    assert "rowid=1" in out and "rowid=2" in out and "srv:4" in out
    assert _dump() == db_before
    assert json.dumps(legacy.reps, sort_keys=True) == server_before and legacy.hits["PUT"] == puts


def test_idempotent_apply_and_changed_mapping_refused(legacy):
    _seed(legacy)
    _migrate(apply=True)
    before = _dump()
    _migrate(apply=True)
    assert _dump() == before
    legacy.reps[0]["id"] = 99
    with pytest.raises(bench.BenchError, match="mapping|identity"):
        _migrate(apply=True)
    assert _dump() == before and legacy.hits["PUT"] == 0


def test_mapped_local_edit_refuses_before_any_put(legacy, capsys):
    _seed(legacy)
    _migrate(apply=True)
    _edit("UPDATE reps SET notes = 'changed after mapping' WHERE id = 2")
    assert cli.main(["bench", "push-local"]) == 2
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and "rowid=2" in err
    assert legacy.hits["PUT"] == 0


def test_new_confirmation_after_apply_requires_mapping(legacy):
    _migrate(apply=True)
    _seed(legacy)
    with pytest.raises(bench.BenchError, match="confirmed=3.*bench push-local-migrate"):
        bench.push_local()
    assert legacy.hits["PUT"] == 0
    _migrate(apply=True)
    assert bench.push_local() == (0, 0)


def test_full_profile_page_refuses_without_local_write(legacy, monkeypatch):
    _seed(legacy)
    monkeypatch.setattr(bench, "_MIGRATE_REP_WINDOW", 3)
    before = _dump()
    with pytest.raises(bench.BenchError, match="completeness cannot be proven"):
        _migrate(apply=True)
    assert _dump() == before and legacy.hits["PUT"] == 0


@pytest.mark.parametrize(
    ("timestamp", "confirmed"),
    [("2026-10-24T10:02:03.123456+09:00", True), ("2026-10-24T01:02:03.123457Z", False)],
)
def test_canonical_time_equality_keeps_microseconds(legacy, timestamp, confirmed):
    _seed(legacy)
    for row in legacy.reps:
        row["recorded_at"] = timestamp
    result = _migrate(apply=True)
    assert len(result.confirmed) == (3 if confirmed else 0)
    assert len(result.conflict) == (0 if confirmed else 3)


def test_second_install_does_not_reuse_mapping(legacy):
    _seed(legacy)
    _migrate(apply=True)
    _edit("UPDATE bench_install_meta SET install_id = ?", ("f" * 32,))
    with pytest.raises(bench.BenchError, match="install|migration"):
        bench.push_local()
    with pytest.raises(bench.BenchError, match="mapping|identity|install"):
        _migrate(apply=True)
    assert legacy.hits["PUT"] == 0


def test_apply_is_atomic_when_a_later_insert_fails(legacy):
    _seed(legacy)
    conn = bench._cache_connect()
    conn.executescript(
        "CREATE TRIGGER interrupt_migration BEFORE INSERT ON bench_legacy_identity "
        "WHEN NEW.rowid = 2 BEGIN SELECT RAISE(ABORT, 'injected interruption'); END;"
    )
    conn.close()
    with pytest.raises(bench.BenchError, match="migration|identity|local"):
        _migrate(apply=True)
    assert _table("bench_legacy_identity") == []
    assert _table("bench_legacy_migration") == []
    assert legacy.hits["PUT"] == 0


def test_accept_new_requires_apply_and_unresolved_existing_row(legacy):
    _seed(legacy)
    for apply, accept in [(False, [1]), (True, [99]), (True, [1])]:
        with pytest.raises(bench.BenchError, match="accept-new"):
            _migrate(apply=apply, accept=accept)
    assert legacy.hits["PUT"] == 0


def test_reps_add_unchanged_without_migration(legacy):
    assert cli.main(_add_args(task="direct-remote", notes="direct add")) == 0
    assert len(legacy.reps) == 1 and legacy.hits["PUT"] == 1


def test_reps_migrate_keeps_its_distinct_band(legacy):
    result = bench.migrate_reps(apply=True, host="old-714-host")
    assert result.inserted_count == 3
    assert all((1 << 40) <= row["origin_id"] < (1 << 40) + (1 << 48) for row in legacy.reps)


def test_no_reps_push_does_not_fetch_server(tmp_path, monkeypatch):
    fake = _remote(monkeypatch, tmp_path)
    assert bench.push_local() == (0, 0)
    assert not fake.hits


def test_migrate_help_and_required_inputs(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["bench", "push-local-migrate", "--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    for term in (
        "--legacy-created-by",
        "--legacy-host",
        "--accept-new",
        "--apply",
        "confirmed",
        "never-pushed",
        "conflict",
        "ambiguous",
        "nothing",
        "server",
        "refuses",
    ):
        assert term in help_text
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["bench", "push-local-migrate", "--apply"])
    assert exit_info.value.code == 2


def test_duplicate_confirmation_ignores_other_creators_at_same_origins(legacy):
    """Invariant: token rotation cannot make another creator's slot our legacy identity."""
    _seed(legacy)
    _seed(legacy, owner="unrelated-creator")
    result = _migrate(apply=True)
    assert len(result.confirmed) == 3 and not result.ambiguous
    assert bench.push_local() == (0, 0)
    assert legacy.hits["PUT"] == 0
    assert {row["server_id"] for row in _table("bench_legacy_identity")} == {1, 2, 3}


def test_two_hosts_only_confirm_their_own_identity(legacy):
    _seed(legacy)
    _seed(legacy, host="other-host")
    for row in legacy.reps[3:]:
        row["notes"] = "other host evidence at same local rowid/profile"
    result = _migrate(apply=True)
    assert len(result.confirmed) == 3 and not result.conflict
    assert {row["server_id"] for row in _table("bench_legacy_identity")} == {1, 2, 3}
    assert bench.push_local() == (0, 0)
    assert legacy.hits["PUT"] == 0


def test_legacy_empty_hostname_uses_local_fallback(legacy, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    _seed(legacy, host="local")
    assert len(_migrate(apply=True).confirmed) == 3
    assert bench.push_local() == (0, 0)
    assert legacy.hits["PUT"] == 0


def test_migration_receipt_preserves_repeatable_inputs(legacy, capsys):
    _seed(legacy, host="previous-host", owner="old-shared-2")
    assert (
        cli.main(
            [
                "bench",
                "push-local-migrate",
                "--apply",
                "--legacy-created-by",
                "legacy-shared",
                "--legacy-created-by",
                "old-shared-2",
                "--legacy-host",
                "previous-host",
                "--legacy-host",
                "even-older-host",
            ]
        )
        == 0
    )
    capsys.readouterr()
    run = _table("bench_legacy_migration")[0]
    assert set(json.loads(run["legacy_created_by"])) == {"legacy-shared", "old-shared-2"}
    assert set(json.loads(run["legacy_hosts"])) == {"previous-host", "even-older-host"}
    # The push has no migration flags: it must read the durable receipt.
    assert bench.push_local() == (0, 0)
    assert legacy.hits["PUT"] == 0


def test_legacy_gate_precedes_score_puts(legacy, tmp_path, monkeypatch):
    from test_bench_backend import _score

    _client_env(tmp_path, monkeypatch, backend="local")
    bench.upsert_scores([_score("local-score")])
    _client_env(tmp_path, monkeypatch)
    _seed(legacy)
    legacy.reps[0]["notes"] = "edited"
    _migrate(apply=True)
    with pytest.raises(bench.BenchError, match="conflict=1"):
        bench.push_local()
    assert legacy.hits["PUT"] == 0


def test_apply_refuses_concurrent_source_edit(legacy, monkeypatch):
    _seed(legacy)
    real_fetch = bench._fetch_reps_for_migrate

    def fetch_then_edit(*args, **kwargs):
        result = real_fetch(*args, **kwargs)
        _edit("UPDATE reps SET notes = 'changed while fetching' WHERE id = 1")
        return result

    monkeypatch.setattr(bench, "_fetch_reps_for_migrate", fetch_then_edit)
    with pytest.raises(bench.BenchError, match="local reps changed"):
        _migrate(apply=True)
    assert _table("bench_legacy_identity") == [] and _table("bench_legacy_migration") == []
    assert legacy.hits["PUT"] == 0


def test_push_classification_uses_one_migrate_fetch(legacy, monkeypatch):
    _seed(legacy)
    _migrate(apply=True)
    real_fetch = bench._fetch_reps_for_migrate
    calls = []

    def counted_fetch(*args, **kwargs):
        calls.append((args, kwargs))
        return real_fetch(*args, **kwargs)

    monkeypatch.setattr(bench, "_fetch_reps_for_migrate", counted_fetch)
    assert bench.push_local() == (0, 0)
    assert len(calls) == 1
    assert legacy.hits["PUT"] == 0
