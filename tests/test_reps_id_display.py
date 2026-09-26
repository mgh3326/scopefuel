"""task #755 — reps ids carry their store namespace; add learns the server pk.

``reps list``/``reps add`` must never print a bare ambiguous id: ``srv:<pk>``
is the server row, ``origin:<key>`` a server-bound rep whose pk is not yet
known locally, ``local:<rowid>`` a local-store row. ``reps refresh-ids``
backfills ``server_id`` in the cache, keyed by the server's own
``(created_by, origin_id)`` upsert identity plus identical content — a
key-only match could pin another client's pk onto this host's cache row.
No test reaches the network — ``bench.request_json`` is replaced.
"""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from collections import Counter

import pytest

from scopefuel import bench, cli, grades

HK_URL = "https://hk.invalid"
HK_TOKEN = "hk-test-token"
CLIENT = "ops"  # the token's client identity, stamped server-side on PUT


class FakeRepStore:
    """GET/PUT /v1/bench/reps keyed on (created_by, origin_id)."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.reps: list[dict] = []
        self.hits: Counter[str] = Counter()
        self.fail_next_get = False
        self.offline = False

    def __call__(self, url, *, method="GET", headers=None, body=None, timeout=None, **_kw):
        assert (headers or {}).get("Authorization") == f"Bearer {HK_TOKEN}"
        url = str(url)
        assert url.startswith(self.url), f"request left the configured endpoint: {url}"
        split = urllib.parse.urlsplit(url)
        assert split.path == "/v1/bench/reps", f"unexpected path: {url}"
        params = urllib.parse.parse_qs(split.query)
        assert set(params) <= {"limit", "profile"}, f"unexpected query: {sorted(params)}"
        self.hits[method] += 1
        if self.offline:
            raise OSError("offline")
        if method == "GET":
            if self.fail_next_get:
                self.fail_next_get = False
                raise OSError("post-write read failed")
            limit = min(int(params.get("limit", ["1000"])[0]), 5000)
            profile = params.get("profile", [""])[0]
            rows = self.reps
            if profile:
                rows = [row for row in rows if row["profile"] == profile]
            rows = sorted(rows, key=lambda row: row["id"], reverse=True)[:limit]
            return {"reps": [dict(row) for row in rows]}
        assert method == "PUT" and body is not None
        rows = body["reps"]
        assert 1 <= len(rows) <= 1000
        for row in rows:
            stored = dict(row)
            assert stored.get("created_by") is None
            stored["created_by"] = CLIENT
            stored.setdefault("created_at", "2026-09-26T00:00:00Z")
            match = next(
                (
                    old
                    for old in self.reps
                    if old["origin_id"] == stored["origin_id"] and old["created_by"] == CLIENT
                ),
                None,
            )
            if match is None:
                stored["id"] = max((old["id"] for old in self.reps), default=0) + 1
                self.reps.append(stored)
            else:
                stored["id"] = match["id"]
                self.reps[self.reps.index(match)] = stored
        return {"upserted": len(rows)}


def _config(tmp_path, text: str) -> None:
    config = tmp_path / "config" / "scopefuel" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(text, encoding="utf-8")


def _local_backend(tmp_path, monkeypatch):
    _config(tmp_path, '[bench]\nbackend = "local"\n')
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(bench, "request_json", lambda *a, **k: pytest.fail("network called"))


def _remote_backend(tmp_path, monkeypatch, url: str = HK_URL) -> FakeRepStore:
    _config(tmp_path, '[bench]\nbackend = "handoffkeep"\n')
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HANDOFFKEEP_URL", url)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", HK_TOKEN)
    fake = FakeRepStore(url)
    monkeypatch.setattr(bench, "request_json", fake)
    return fake


def _add_args(task="755", **overrides) -> list[str]:
    args = [
        "reps",
        "add",
        "--profile",
        "builder-devin-max",
        "--model",
        "swe-2-max",
        "--task",
        task,
        "--tier",
        "T1",
        "--role",
        "impl",
        "--grade",
        "A",
        "--rounds",
        "1",
        "--blockers-found",
        "0",
        "--completed",
        "1",
        "--notes",
        "tokens unknown",
    ]
    if overrides.get("effort"):
        args += ["--effort", overrides["effort"]]
    return args


def _cache_db(tmp_path) -> sqlite3.Connection:
    bench._cache_connect().close()  # ensure cache schema exists
    conn = sqlite3.connect(bench.db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _seed_cache_row(
    tmp_path,
    *,
    cache_key: str,
    origin_id: int,
    server_id: int | None = None,
    created_by: str | None = None,
    profile: str = "builder-devin-max",
    model_id: str = "swe-2-max",
    task_ref: str = "755",
    recorded_at: str = "2026-09-26T00:00:00Z",
    **fields,
) -> None:
    row = {
        "tier": "T1",
        "role": "impl",
        "rounds": 1,
        "blockers_found": 0,
        "completed": 1,
        "input_tokens": None,
        "output_tokens": None,
        "notes": None,
        "effort": None,
        "grade": "A",
        "table_grade": "A",
    }
    row.update(fields)
    conn = _cache_db(tmp_path)
    try:
        conn.execute(
            "INSERT INTO bench_cache_reps "
            "(cache_key, server_id, origin_id, created_by, profile, model_id, task_ref, tier, role, "
            "rounds, blockers_found, completed, input_tokens, output_tokens, notes, recorded_at, "
            "effort, grade, table_grade) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cache_key,
                server_id,
                origin_id,
                created_by,
                profile,
                model_id,
                task_ref,
                row["tier"],
                row["role"],
                row["rounds"],
                row["blockers_found"],
                row["completed"],
                row["input_tokens"],
                row["output_tokens"],
                row["notes"],
                recorded_at,
                row["effort"],
                row["grade"],
                row["table_grade"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _remote_row(
    *,
    id: int,
    origin_id: int,
    created_by: str | None = CLIENT,
    profile: str = "builder-devin-max",
    model_id: str = "swe-2-max",
    task_ref: str = "755",
    recorded_at: str = "2026-09-26T00:00:00Z",
    **fields,
) -> dict:
    row = {
        "id": id,
        "origin_id": origin_id,
        "created_by": created_by,
        "created_at": "2026-09-26T00:00:00Z",
        "profile": profile,
        "model_id": model_id,
        "task_ref": task_ref,
        "tier": "T1",
        "role": "impl",
        "rounds": 1,
        "blockers_found": 0,
        "completed": 1,
        "input_tokens": None,
        "output_tokens": None,
        "notes": None,
        "recorded_at": recorded_at,
        "effort": None,
        "grade": "A",
        "table_grade": "A",
    }
    row.update(fields)
    return row


_BARE_ID = re.compile(r"id=\d")


def _stamp_reps_cache_fresh() -> None:
    """Mark the reps cache fresh so ``reps list`` serves it without a GET."""

    backend = bench.bench_backend(use="reps")
    conn = bench._cache_connect()
    try:
        bench._stamp_cache(conn, "reps", backend, bench._cache_now())
        conn.commit()
    finally:
        conn.close()


def test_add_learns_server_pk_and_list_shows_srv_ref(tmp_path, monkeypatch, capsys):
    fake = _remote_backend(tmp_path, monkeypatch)

    assert cli.main(_add_args()) == 0
    out = capsys.readouterr().out
    assert "recorded rep id=srv:1" in out
    assert not _BARE_ID.search(out), out

    conn = _cache_db(tmp_path)
    try:
        cached = conn.execute("SELECT server_id, origin_id, created_by FROM bench_cache_reps").fetchall()
    finally:
        conn.close()
    assert len(cached) == 1
    assert (cached[0]["server_id"], cached[0]["origin_id"], cached[0]["created_by"]) == (
        1,
        1,
        CLIENT,
    )

    assert cli.main(_add_args(task="756")) == 0
    assert "recorded rep id=srv:2" in capsys.readouterr().out
    assert len(fake.reps) == 2

    assert cli.main(["reps", "list"]) == 0
    out = capsys.readouterr().out
    assert "id=srv:1" in out and "id=srv:2" in out
    assert not _BARE_ID.search(out), out


def test_add_returned_record_carries_srv_ref(tmp_path, monkeypatch):
    _remote_backend(tmp_path, monkeypatch)
    rep = bench.add_rep(
        profile="builder-devin-max",
        model_id="swe-2-max",
        task_ref="755",
        tier="T1",
        role="impl",
        grade="A",
        rounds=1,
        blockers_found=0,
        completed=1,
    )
    assert rep.id == 1
    assert rep.ref == "srv:1"
    assert bench.rep_ref(rep) == "srv:1"


def test_add_retry_after_post_write_read_failure_is_idempotent(tmp_path, monkeypatch, capsys):
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.fail_next_get = True  # the GET after PUT dies once

    assert cli.main(_add_args()) == 2
    assert len(fake.reps) == 1  # the write landed anyway
    conn = _cache_db(tmp_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM bench_cache_reps").fetchone()[0] == 0
    finally:
        conn.close()

    capsys.readouterr()
    assert cli.main(_add_args()) == 0
    out = capsys.readouterr().out
    assert "recorded rep id=srv:1" in out
    assert len(fake.reps) == 1  # same (created_by, origin_id) upserted, no dup


def test_list_shows_origin_ref_for_unbound_cache_rows(tmp_path, monkeypatch, capsys):
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps.append(_remote_row(id=1107, origin_id=568))
    bench.read_reps()  # stamp the cache fresh so list serves it within TTL
    capsys.readouterr()

    _seed_cache_row(
        tmp_path,
        cache_key="origin:282562456259734",
        origin_id=282562456259734,
        task_ref="migrated-row",
    )
    # A row whose origin_id numerically equals some server pk must still show
    # origin: — the label names the store, not the value.
    _seed_cache_row(
        tmp_path,
        cache_key="origin:1107",
        origin_id=1107,
        task_ref="collides-with-pk",
    )

    assert cli.main(["reps", "list"]) == 0
    out = capsys.readouterr().out
    assert "id=srv:1107" in out
    assert "id=origin:282562456259734" in out
    assert "id=origin:1107" in out
    assert not _BARE_ID.search(out), out


def test_local_backend_labels_local(tmp_path, monkeypatch, capsys):
    _local_backend(tmp_path, monkeypatch)
    assert cli.main(_add_args()) == 0
    assert "recorded rep id=local:1" in capsys.readouterr().out
    assert cli.main(["reps", "list"]) == 0
    out = capsys.readouterr().out
    assert "id=local:1" in out
    assert not _BARE_ID.search(out), out


def test_refresh_ids_dry_run_then_apply_is_idempotent(tmp_path, monkeypatch, capsys):
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps.extend(
        [
            _remote_row(id=101, origin_id=500),
            _remote_row(id=102, origin_id=501),
            _remote_row(id=103, origin_id=502, notes="different rep", task_ref="alien"),
            _remote_row(id=104, origin_id=504, created_by="other-client"),
            _remote_row(id=105, origin_id=504),
        ]
    )
    _seed_cache_row(
        tmp_path, cache_key="origin:500", origin_id=500
    )  # anonymous echo -> binds + learns created_by
    _seed_cache_row(
        tmp_path, cache_key="ops:501", origin_id=501, created_by=CLIENT
    )  # exact (created_by, origin_id) pair
    _seed_cache_row(
        tmp_path,
        cache_key="other:501",
        origin_id=501,
        created_by="other-client",
        task_ref="alien-copy",
    )  # pair exists for ops, not other-client -> must not take srv:102
    _seed_cache_row(
        tmp_path,
        cache_key="origin:502",
        origin_id=502,
        task_ref="502",
        notes="mine",
    )  # origin id slot held by a different rep -> conflict
    _seed_cache_row(tmp_path, cache_key="origin:503", origin_id=503)  # no server copy -> unmatched
    _seed_cache_row(
        tmp_path, cache_key="origin:504", origin_id=504
    )  # two same-content remote rows -> ambiguous
    _seed_cache_row(
        tmp_path, cache_key="ops:900", origin_id=900, server_id=900, created_by=CLIENT
    )  # already bound -> untouched, not a candidate

    assert cli.main(["reps", "refresh-ids"]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "candidates=6" in out
    assert "filled=2" in out
    assert "fill origin:500 -> srv:101" in out
    assert "fill ops:501 -> srv:102" in out
    assert "unmatched other:501" in out
    assert "conflict origin:502" in out
    assert "ambiguous origin:504" in out
    conn = _cache_db(tmp_path)
    try:
        bound = conn.execute("SELECT COUNT(*) FROM bench_cache_reps WHERE server_id IS NOT NULL").fetchone()[
            0
        ]
    finally:
        conn.close()
    assert bound == 1  # dry-run wrote nothing

    assert cli.main(["reps", "refresh-ids", "--apply"]) == 0
    conn = _cache_db(tmp_path)
    try:
        rows = {
            row["cache_key"]: row
            for row in conn.execute(
                "SELECT cache_key, server_id, created_by FROM bench_cache_reps"
            ).fetchall()
        }
    finally:
        conn.close()
    assert rows["origin:500"]["server_id"] == 101
    assert rows["origin:500"]["created_by"] == CLIENT  # learned from the server row
    assert rows["ops:501"]["server_id"] == 102
    assert rows["other:501"]["server_id"] is None  # created_by mismatch: not bound
    assert rows["origin:502"]["server_id"] is None  # different rep's content: not bound
    assert rows["origin:503"]["server_id"] is None
    assert rows["origin:504"]["server_id"] is None
    assert rows["ops:900"]["server_id"] == 900

    capsys.readouterr()
    assert cli.main(["reps", "refresh-ids", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "candidates=4" in out  # only the still-unbound rows remain
    assert "filled=0" in out

    _stamp_reps_cache_fresh()
    assert cli.main(["reps", "list"]) == 0
    out = capsys.readouterr().out
    assert "id=srv:101" in out and "id=srv:102" in out
    assert "id=origin:503" in out
    assert not _BARE_ID.search(out), out


def test_refresh_ids_requires_handoffkeep(tmp_path, monkeypatch, capsys):
    _local_backend(tmp_path, monkeypatch)
    assert cli.main(["reps", "refresh-ids"]) == 2
    assert "requires the handoffkeep backend" in capsys.readouterr().err


def test_display_refs_match_grades_ref_vocabulary(tmp_path, monkeypatch, capsys):
    """The labels reps list prints are the refs grades already speaks.

    ``srv:N``/``local:N`` in a reps line must parse as the same ref grades
    --exclude and rep_grade_annotations use; ``origin:N`` is cache-display
    only and stays out of the grades grammar.
    """

    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps.append(_remote_row(id=1107, origin_id=568))
    assert cli.main(_add_args(task="added")) == 0
    assert "recorded rep id=srv:1108" in capsys.readouterr().out
    bench.read_reps()
    _seed_cache_row(tmp_path, cache_key="origin:42", origin_id=42, task_ref="echo")
    capsys.readouterr()

    assert cli.main(["reps", "list"]) == 0
    out = capsys.readouterr().out
    labels: dict[str, list[str]] = {}
    for namespace, number in re.findall(r"id=(srv|origin|local):(\d+)", out):
        labels.setdefault(namespace, []).append(number)
    assert labels == {"srv": ["1108", "1107"], "origin": ["42"]}
    for number in labels["srv"]:
        assert grades.resolve_refs(f"srv:{number}", backend_name="handoffkeep", host="h") == (
            f"srv:{number}",
        )
    assert grades.resolve_refs("origin:42", backend_name="handoffkeep", host="h") == ()
    assert not _BARE_ID.search(out), out


def test_mutant_wrong_source_and_wrong_row_binding(tmp_path, monkeypatch, capsys):
    """Directed mutants: a row shown with the wrong source, or server_id bound
    to a row that is not the same rep (created_by or content mismatch)."""

    fake = _remote_backend(tmp_path, monkeypatch)
    # Remote holds rep #7 for client 'other-client'; this host's cache has an
    # anonymous echo with the same origin_id but different content, plus a
    # created_by='other-client' row with identical content.
    fake.reps.append(_remote_row(id=77, origin_id=7, created_by="other-client", task_ref="alien-7"))
    _seed_cache_row(tmp_path, cache_key="origin:7", origin_id=7, task_ref="mine-7", notes="own echo")
    _seed_cache_row(
        tmp_path,
        cache_key="other:7",
        origin_id=7,
        created_by="other-client",
        task_ref="alien-7",
    )

    result = bench.refresh_rep_server_ids(apply=True)
    # other:7 binds — (created_by, origin_id) pair + identical content proves
    # the same rep. origin:7 does not — the only key-holder's content differs.
    assert result.filled == [("other:7", 77)]
    assert result.conflicts == ["origin:7"]

    conn = _cache_db(tmp_path)
    try:
        rows = {
            row["cache_key"]: row["server_id"]
            for row in conn.execute("SELECT cache_key, server_id FROM bench_cache_reps").fetchall()
        }
    finally:
        conn.close()
    assert rows == {"origin:7": None, "other:7": 77}

    _stamp_reps_cache_fresh()
    assert cli.main(["reps", "list"]) == 0
    out = capsys.readouterr().out
    assert "id=srv:77" in out
    assert "id=origin:7" in out  # still honest about its source
    assert not _BARE_ID.search(out), out
