"""hk 1916: safely map legacy push-local identities before the PR 175 key."""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import sqlite3

import pytest
from test_reps_collision_1384 import BAND_BASE, _add_args, _client_env, _remote, _remote_row

from scopefuel import bench, cli

LEGACY_CREATED_BY = "legacy-shared"


def _local_rows(tmp_path, monkeypatch, count=1):
    _client_env(tmp_path, monkeypatch, backend="local")
    monkeypatch.setattr(bench, "_utc_now", lambda: "2026-10-24T12:00:00.123456+09:00")
    for number in range(count):
        assert cli.main(_add_args(task=f"legacy-{number}", notes=f"legacy row {number}")) == 0
    return bench._read_local_reps_for_push()


def _legacy_origin(host: str, record: bench.RepRecord) -> int:
    digest = hashlib.sha256(f"reps-push-local\x00{host}\x00{record.profile}\x00{record.id}".encode()).digest()
    return BAND_BASE + int.from_bytes(digest[:8], "big") % (1 << 62)


def _seed_legacy(fake, records, *, host="former-host", created_by=LEGACY_CREATED_BY, start_id=100):
    for offset, record in enumerate(records):
        row = bench._rep_to_wire(bench._RemoteRep(record=record, origin_id=_legacy_origin(host, record)))
        row.update(
            id=start_id + offset,
            created_by=created_by,
            created_at="2026-10-24T03:00:00Z",
        )
        fake.reps.append(row)


def _migrate_args(*, apply=False, hosts=(), created_by=LEGACY_CREATED_BY, accept_new=None):
    args = ["bench", "push-local-migrate"]
    if apply:
        args.append("--apply")
    for name in created_by if isinstance(created_by, (list, tuple)) else (created_by,):
        args += ["--legacy-created-by", name]
    for host in hosts:
        args += ["--legacy-host", host]
    if accept_new is not None:
        args += ["--accept-new", str(accept_new)]
    return args


def _migration_rows():
    conn = bench._cache_connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM bench_legacy_identity ORDER BY rowid")]
    finally:
        conn.close()


def _migration_run():
    conn = bench._cache_connect()
    try:
        row = conn.execute("SELECT * FROM bench_legacy_migration").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _db_dump():
    conn = sqlite3.connect(bench.db_path())
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def test_push_local_migrate_help_explains_contract(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["bench", "push-local-migrate", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    for text in (
        "--legacy-created-by",
        "--legacy-host",
        "--apply",
        "--accept-new ROWID",
        "confirmed",
        "never-pushed",
        "conflict",
        "ambiguous",
        "Nothing is sent to the server",
        "push-local refuses",
    ):
        assert text in help_text


def test_fresh_install_requires_one_migration_then_pushes_never_pushed_rows(tmp_path, monkeypatch, capsys):
    _local_rows(tmp_path, monkeypatch, count=2)
    fake = _remote(monkeypatch, tmp_path)
    puts = fake.hits["PUT"]

    assert cli.main(["bench", "push-local"]) == 2
    refused = capsys.readouterr().err.strip()
    assert "migration" in refused and "push-local-migrate" in refused
    assert fake.hits["PUT"] == puts

    assert cli.main(_migrate_args(apply=True)) == 0
    assert "never-pushed: 2" in capsys.readouterr().out
    run = _migration_run()
    assert run is not None
    assert json.loads(run["legacy_created_by_json"]) == [LEGACY_CREATED_BY]
    assert json.loads(run["legacy_host_json"]) == []

    assert cli.main(["bench", "push-local"]) == 0
    assert fake.hits["PUT"] == puts + 1
    assert len(fake.reps) == 2


@pytest.mark.parametrize("mode", ["old", "new"])
def test_pr175_origin_resend_baseline_duplicates_three_legacy_rows(tmp_path, monkeypatch, mode):
    """The pre-fix PR 175 origin path adds three rows beside the legacy rows."""
    monkeypatch.setattr(socket, "gethostname", lambda: "former-host")
    records = _local_rows(tmp_path, monkeypatch, count=3)
    fake = _remote(monkeypatch, tmp_path, mode)
    _seed_legacy(fake, records)
    original_server_rows = copy.deepcopy(fake.reps)
    install_id = bench._rep_install_id(None)
    resend = [
        bench._RemoteRep(record=record, origin_id=bench._push_local_origin_id(install_id, record))
        for record in records
    ]

    bench._put_rep_batches(bench.bench_backend(use="reps"), resend, check_origins=True)

    assert len(original_server_rows) == 3
    assert len(fake.reps) == 6
    assert fake.reps[:3] == original_server_rows
    assert {row["origin_id"] for row in fake.reps[3:]} == {item.origin_id for item in resend}


@pytest.mark.parametrize("mode", ["old", "new"])
def test_duplicate_fixed_legacy_rows_are_mapped_and_never_put_again(tmp_path, monkeypatch, capsys, mode):
    """Mutant guard: if push_local ignores bench_legacy_identity, the PUT count goes above zero."""
    monkeypatch.setattr(socket, "gethostname", lambda: "former-host")
    records = _local_rows(tmp_path, monkeypatch, count=3)
    fake = _remote(monkeypatch, tmp_path, mode)
    _seed_legacy(fake, records)
    original_server_rows = copy.deepcopy(fake.reps)
    puts = fake.hits["PUT"]

    assert cli.main(_migrate_args(apply=True)) == 0
    assert "confirmed: 3" in capsys.readouterr().out
    mappings = _migration_rows()
    install_id = bench._rep_install_id(None)
    assert len(mappings) == 3
    for record, mapping in zip(records, mappings, strict=True):
        assert mapping["install_id"] == install_id
        assert mapping["content_key"] == bench._legacy_rep_content_json(record)
        assert mapping["decision"] == "confirmed"
        assert mapping["server_id"] == 100 + record.id - 1
        assert mapping["legacy_origin"] == _legacy_origin("former-host", record)
        assert mapping["legacy_created_by"] == LEGACY_CREATED_BY
    assert cli.main(["bench", "push-local"]) == 0
    assert fake.hits["PUT"] == puts
    assert fake.reps == original_server_rows


@pytest.mark.parametrize("mode", ["old", "new"])
def test_new_local_row_after_migration_pushes_once_then_resends_idempotently(
    tmp_path, monkeypatch, capsys, mode
):
    monkeypatch.setattr(socket, "gethostname", lambda: "former-host")
    original = _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path, mode)
    _seed_legacy(fake, original)
    assert cli.main(_migrate_args(apply=True)) == 0
    capsys.readouterr()

    _client_env(tmp_path, monkeypatch, backend="local")
    assert cli.main(_add_args(task="after-migration", notes="new local evidence")) == 0
    new_record = bench._read_local_reps_for_push()[-1]
    _client_env(tmp_path, monkeypatch)
    install_id = bench._rep_install_id(None)
    original_server_rows = copy.deepcopy(fake.reps)
    puts = fake.hits["PUT"]

    assert cli.main(["bench", "push-local"]) == 0
    assert fake.hits["PUT"] == puts + 1
    assert len(fake.reps) == 2
    pushed = next(row for row in fake.reps if row["created_by"] == "ops")
    assert pushed["origin_id"] == bench._push_local_origin_id(install_id, new_record)
    assert pushed["origin_id"] != _legacy_origin("former-host", new_record)
    assert len(fake.put_payloads[-1]) == 1
    assert fake.put_payloads[-1][0]["origin_id"] == pushed["origin_id"]
    after_first_push = copy.deepcopy(fake.reps)
    assert len(original_server_rows) == 1

    assert cli.main(["bench", "push-local"]) == 0
    assert fake.hits["PUT"] == puts + 2
    assert fake.reps == after_first_push


@pytest.mark.parametrize(("local_host", "expected_server_id"), [("host-A", 100), ("host-B", 200)])
def test_each_host_maps_only_its_own_legacy_slot(
    tmp_path, monkeypatch, capsys, local_host, expected_server_id
):
    monkeypatch.setattr(socket, "gethostname", lambda: local_host)
    records = _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    _seed_legacy(fake, records, host="host-A", start_id=100)
    _seed_legacy(fake, records, host="host-B", start_id=200)

    assert cli.main(_migrate_args(apply=True)) == 0
    assert "confirmed: 1" in capsys.readouterr().out
    assert _migration_rows()[0]["server_id"] == expected_server_id
    puts = fake.hits["PUT"]
    assert cli.main(["bench", "push-local"]) == 0
    assert fake.hits["PUT"] == puts


def test_equal_content_without_candidate_origin_is_never_pushed_identity(tmp_path, monkeypatch, capsys):
    """Mutant guard: dropping the origin check would confirm this equal-content server row."""
    monkeypatch.setattr(socket, "gethostname", lambda: "current-host")
    records = _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    equal = bench._rep_to_wire(bench._RemoteRep(record=records[0], origin_id=BAND_BASE + 77))
    equal.update(id=77, created_by=LEGACY_CREATED_BY, created_at="2026-10-24T03:00:00Z")
    fake.reps.append(equal)

    assert cli.main(_migrate_args(apply=True)) == 0
    assert "never-pushed: 1" in capsys.readouterr().out
    assert _migration_rows() == []
    puts = fake.hits["PUT"]
    assert cli.main(["bench", "push-local"]) == 0
    assert fake.hits["PUT"] == puts + 1
    assert len(fake.reps) == 2
    assert fake.reps[-1]["created_by"] == "ops"


def test_equal_candidate_origin_under_unapproved_creator_does_not_confirm(tmp_path, monkeypatch, capsys):
    """Mutant guard: dropping created_by from confirmation would skip this row's required PUT."""
    monkeypatch.setattr(socket, "gethostname", lambda: "former-host")
    records = _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    _seed_legacy(fake, records, created_by="another-host-token")

    assert cli.main(_migrate_args(apply=True)) == 0
    assert "never-pushed: 1" in capsys.readouterr().out
    puts = fake.hits["PUT"]
    assert cli.main(["bench", "push-local"]) == 0
    assert fake.hits["PUT"] == puts + 1
    assert len(fake.reps) == 2


def test_empty_hostname_uses_the_legacy_local_fallback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    records = _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    _seed_legacy(fake, records, host="local")

    assert cli.main(_migrate_args(apply=True)) == 0
    assert "confirmed: 1" in capsys.readouterr().out
    assert _migration_rows()[0]["server_id"] == 100


@pytest.mark.parametrize("supply_former_host", [False, True])
def test_hostname_change_requires_operator_to_supply_former_host(
    tmp_path, monkeypatch, capsys, supply_former_host
):
    monkeypatch.setattr(socket, "gethostname", lambda: "current-host-B")
    records = _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    _seed_legacy(fake, records, host="former-host-A")

    hosts = ("former-host-A",) if supply_former_host else ()
    assert cli.main(_migrate_args(apply=True, hosts=hosts)) == 0
    output = capsys.readouterr().out
    assert ("confirmed: 1" in output) is supply_former_host
    assert ("never-pushed: 1" in output) is not supply_former_host
    puts = fake.hits["PUT"]
    assert cli.main(["bench", "push-local"]) == 0
    assert fake.hits["PUT"] == puts + (0 if supply_former_host else 1)


def test_conflict_blocks_push_until_explicit_accept_new(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(socket, "gethostname", lambda: "former-host")
    records = _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path, "old")
    _seed_legacy(fake, records)
    fake.reps[0]["notes"] = "edited after the legacy push"
    original_server_rows = copy.deepcopy(fake.reps)

    assert cli.main(_migrate_args()) == 0
    output = capsys.readouterr().out
    assert "conflict: 1" in output
    assert "conflict rowid=1 profile=builder-devin-max server_ids=100" in output
    assert _migration_rows() == []

    assert cli.main(_migrate_args(apply=True)) == 0
    capsys.readouterr()

    puts = fake.hits["PUT"]
    assert cli.main(["bench", "push-local"]) == 2
    refusal = capsys.readouterr().err.strip()
    assert "conflict=1" in refusal and "push-local-migrate" in refusal
    assert fake.hits["PUT"] == puts

    assert cli.main(_migrate_args(apply=True, accept_new=records[0].id)) == 0
    capsys.readouterr()
    mapping = _migration_rows()[0]
    assert mapping["decision"] == "accept-new" and mapping["server_id"] is None
    assert cli.main(["bench", "push-local"]) == 0
    assert fake.hits["PUT"] == puts + 1
    assert fake.reps[0] == original_server_rows[0]
    assert len(fake.reps) == 2 and fake.reps[1]["created_by"] == "ops"


def test_two_candidate_hosts_confirming_same_row_are_ambiguous(tmp_path, monkeypatch, capsys):
    """Supplying the other host name adds both own and foreign identity slots."""
    monkeypatch.setattr(socket, "gethostname", lambda: "former-B")
    records = _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    _seed_legacy(fake, records, host="former-A", start_id=100)
    _seed_legacy(fake, records, host="former-B", start_id=200)

    assert cli.main(_migrate_args(hosts=("former-A",))) == 0
    output = capsys.readouterr().out
    assert "ambiguous: 1" in output
    assert "ambiguous rowid=1 profile=builder-devin-max server_ids=100,200" in output
    assert cli.main(_migrate_args(apply=True, hosts=("former-A",))) == 0
    capsys.readouterr()
    puts = fake.hits["PUT"]
    assert cli.main(["bench", "push-local"]) == 2
    assert "ambiguous=1" in capsys.readouterr().err
    assert fake.hits["PUT"] == puts


def test_dry_run_writes_no_rows_and_sends_no_put(tmp_path, monkeypatch, capsys):
    """Mutant guard: creating either migration table during a dry run changes this database dump."""
    monkeypatch.setattr(socket, "gethostname", lambda: "former-host")
    records = _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    _seed_legacy(fake, records)
    before_db = _db_dump()
    before_hits = fake.hits.copy()
    before_rows = copy.deepcopy(fake.reps)

    assert cli.main(_migrate_args()) == 0
    output = capsys.readouterr().out
    assert "confirmed: 1" in output
    assert _db_dump() == before_db
    assert fake.hits - before_hits == {"GET": 2}
    assert fake.reps == before_rows


def test_apply_is_idempotent_and_refuses_a_changed_mapping(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(socket, "gethostname", lambda: "former-host")
    records = _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    _seed_legacy(fake, records)
    args = _migrate_args(apply=True)

    assert cli.main(args) == 0
    capsys.readouterr()
    before_mappings = _migration_rows()
    before_run = _migration_run()
    assert cli.main(args) == 0
    capsys.readouterr()
    assert _migration_rows() == before_mappings
    assert _migration_run() == before_run

    fake.reps[0]["id"] = 101
    assert cli.main(args) == 2
    error = capsys.readouterr().err
    assert "rowid=1" in error and "mapping" in error
    assert _migration_rows() == before_mappings


def test_mapped_row_changed_locally_refuses_before_any_put(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(socket, "gethostname", lambda: "former-host")
    records = _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    _seed_legacy(fake, records)
    assert cli.main(_migrate_args(apply=True)) == 0
    capsys.readouterr()
    conn = bench.connect()
    try:
        conn.execute("UPDATE reps SET notes = 'edited locally' WHERE id = ?", (records[0].id,))
        conn.commit()
    finally:
        conn.close()
    puts = fake.hits["PUT"]

    assert cli.main(["bench", "push-local"]) == 2
    error = capsys.readouterr().err.strip()
    assert f"rowid={records[0].id}" in error
    assert fake.hits["PUT"] == puts


def test_apply_transaction_rolls_back_all_mappings_and_run_record(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(socket, "gethostname", lambda: "former-host")
    records = _local_rows(tmp_path, monkeypatch, count=2)
    fake = _remote(monkeypatch, tmp_path)
    _seed_legacy(fake, records)
    conn = bench._cache_connect()
    try:
        conn.execute(
            "CREATE TRIGGER fail_legacy_mapping BEFORE INSERT ON bench_legacy_identity "
            "WHEN NEW.rowid = 2 BEGIN SELECT RAISE(ABORT, 'forced transaction failure'); END"
        )
        conn.commit()
    finally:
        conn.close()

    assert cli.main(_migrate_args(apply=True)) == 2
    assert "forced transaction failure" in capsys.readouterr().err
    assert _migration_rows() == []
    assert _migration_run() is None
    assert fake.hits["PUT"] == 0


def test_full_profile_window_refuses_migration_without_claiming_completeness(tmp_path, monkeypatch, capsys):
    _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    fake.reps = [
        _remote_row(id=number, origin_id=BAND_BASE + number)
        for number in range(1, bench._MIGRATE_REP_WINDOW + 1)
    ]

    assert cli.main(_migrate_args(apply=True)) == 2
    error = capsys.readouterr().err
    assert "fill the server's" in error and "completeness cannot be proven" in error
    assert _migration_rows() == []
    assert _migration_run() is None
    assert fake.hits["PUT"] == 0


@pytest.mark.parametrize(
    ("server_recorded_at", "expected_class"),
    [
        ("2026-10-24T03:00:00.123456Z", "confirmed"),
        ("2026-10-24T03:00:00.123457Z", "conflict"),
    ],
)
def test_timestamp_precision_and_timezone_forms_use_canonical_content(
    tmp_path, monkeypatch, capsys, server_recorded_at, expected_class
):
    monkeypatch.setattr(socket, "gethostname", lambda: "former-host")
    records = _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    _seed_legacy(fake, records)
    fake.reps[0]["recorded_at"] = server_recorded_at

    assert cli.main(_migrate_args()) == 0
    output = capsys.readouterr().out
    assert f"{expected_class}: 1" in output


def test_second_install_id_does_not_reuse_first_install_mapping(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(socket, "gethostname", lambda: "former-host")
    records = _local_rows(tmp_path, monkeypatch)
    fake = _remote(monkeypatch, tmp_path)
    _seed_legacy(fake, records)
    assert cli.main(_migrate_args(apply=True)) == 0
    capsys.readouterr()
    previous = _migration_rows()[0]

    conn = bench._cache_connect()
    try:
        conn.execute("UPDATE bench_install_meta SET install_id = ?", ("f" * 32,))
        conn.execute("DELETE FROM bench_legacy_migration")
        conn.commit()
    finally:
        conn.close()
    assert cli.main(_migrate_args(apply=True)) == 2
    assert "rowid=1" in capsys.readouterr().err
    assert _migration_rows() == [previous]


def test_reps_add_and_reps_migrate_keep_their_existing_paths(tmp_path, monkeypatch, capsys):
    """push-local identity migration must not change independent reps add/migrate identity paths."""
    fake = _remote(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bench,
        "_push_local_origin_id",
        lambda *_args: pytest.fail("reps add or migrate used a push-local origin"),
    )
    assert cli.main(_add_args(task="add-is-independent", notes="normal reps add")) == 0
    assert len(fake.reps) == 1
    assert fake.reps[0]["created_by"] == "ops"
    capsys.readouterr()

    _client_env(tmp_path, monkeypatch, backend="local")
    assert cli.main(_add_args(task="migrate-is-independent", notes="local migrate evidence")) == 0
    capsys.readouterr()
    fake = _remote(monkeypatch, tmp_path)
    assert cli.main(["reps", "migrate", "--apply", "--host", "migrate-host"]) == 0
    migrated = capsys.readouterr().out
    assert "inserted=1" in migrated
    assert len(fake.reps) == 1
    local = bench._read_local_reps_for_push()[0]
    assert fake.reps[0]["origin_id"] == bench._migrate_origin_id("migrate-host", local.profile, local.id)
    assert fake.reps[0]["notes"].endswith("[src:migrate-host]")
