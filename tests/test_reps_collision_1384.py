"""task #1384 — collision-free rep origin ids, write-ahead, server-contract binds.

The old client derived ``origin_id`` from local SQLite maxima, so two hosts
sharing one bearer token (one ``created_by``) picked the same key and the
server's ``ON CONFLICT DO UPDATE`` silently overwrote the earlier rep. This
suite runs an in-process fake of ``PUT/GET /v1/bench/reps`` in both server
dialects — "new" (post-#1384: ``ids`` in the PUT response, 409
``bench_rep_conflict`` on a differing slot, identical resend returns the
stored id) and "old" (bare ``upserted`` count, silent overwrite) — plus two
client identities with separate local DBs under one token.

Each test doubles as a mutant guard named in its docstring: restoring
``_next_origin_id``, generating a fresh retry id, binding a non-unique or
content-different row, or showing cached content under a contradicted
``srv:`` id must all go RED.
"""

from __future__ import annotations

import itertools
import json
import re
import socket
import sqlite3
import urllib.parse
from collections import Counter

import pytest

from scopefuel import bench, cli

HK_URL = "https://hk.invalid"
HK_TOKEN = "hk-test-token"
CLIENT = "ops"  # the token's client identity, stamped server-side on PUT
BAND_BASE = 1 << 62
BAND_TOP = 1 << 63

# Wire fields the server's identical-resend rule compares — everything the
# client PUTs except the (created_by, origin_id) key itself.
_CONTENT_FIELDS = (
    "profile",
    "model_id",
    "task_ref",
    "tier",
    "role",
    "rounds",
    "blockers_found",
    "completed",
    "input_tokens",
    "output_tokens",
    "notes",
    "recorded_at",
    "effort",
    "grade",
    "table_grade",
)


class FakeRepStore:
    """In-process GET/PUT /v1/bench/reps keyed on (created_by, origin_id)."""

    def __init__(self, url: str, *, mode: str) -> None:
        assert mode in {"new", "old"}
        self.url = url
        self.mode = mode
        self.reps: list[dict] = []
        self.hits: Counter[str] = Counter()
        # Every PUT body as received, including ones the fake then refuses —
        # lets a test prove a retry resends the identical payload.
        self.put_payloads: list[list[dict]] = []
        self.offline = False
        # Raised on the next PUT after the write is applied ("landed") or
        # before it ("lost") — the two ways a response can go missing.
        self.fail_next_put: BaseException | None = None
        self.fail_next_put_after_write = False
        # Rewrites the store after a PUT applies — models a second host's
        # resend landing between our PUT and our post-write GET.
        self.after_put = None

    @staticmethod
    def _same_content(a: dict, b: dict) -> bool:
        return all(a.get(field) == b.get(field) for field in _CONTENT_FIELDS)

    def _put_new(self, rows: list[dict]) -> dict:
        ids: list[int] = []
        conflicts: list[dict] = []
        staged: list[dict] = []
        for index, row in enumerate(rows):
            stored = dict(row)
            stored["created_by"] = CLIENT
            stored.setdefault("created_at", "2026-10-10T00:00:00Z")
            match = next(
                (
                    old
                    for old in self.reps + staged
                    if old["origin_id"] == stored["origin_id"] and old["created_by"] == CLIENT
                ),
                None,
            )
            if match is None:
                stored["id"] = max((old["id"] for old in self.reps + staged), default=0) + 1
                staged.append(stored)
                ids.append(stored["id"])
            elif self._same_content(match, stored):
                ids.append(match["id"])  # identical resend: the stored id
            else:
                conflicts.append(
                    {
                        "index": index,
                        "origin_id": stored["origin_id"],
                        "conflict_server_id": match["id"],
                    }
                )
        if conflicts:
            raise bench.HttpError(409, json.dumps({"error": "bench_rep_conflict", "conflicts": conflicts}))
        self.reps.extend(staged)
        return {"upserted": len(rows), "ids": ids}

    def _put_old(self, rows: list[dict]) -> dict:
        for row in rows:
            stored = dict(row)
            stored["created_by"] = CLIENT
            stored.setdefault("created_at", "2026-10-10T00:00:00Z")
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
        self.put_payloads.append(rows)
        failure = self.fail_next_put
        self.fail_next_put = None
        if failure is not None and not self.fail_next_put_after_write:
            raise failure
        result = self._put_new(rows) if self.mode == "new" else self._put_old(rows)
        if self.after_put is not None:
            hook, self.after_put = self.after_put, None
            hook()
        if failure is not None:
            raise failure
        return result


def _client_env(home, monkeypatch, *, backend: str = "handoffkeep") -> None:
    config = home / "config" / "scopefuel" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f'[bench]\nbackend = "{backend}"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))


def _remote(monkeypatch, home, mode: str = "new") -> FakeRepStore:
    _client_env(home, monkeypatch)
    monkeypatch.setenv("HANDOFFKEEP_URL", HK_URL)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", HK_TOKEN)
    fake = FakeRepStore(HK_URL, mode=mode)
    monkeypatch.setattr(bench, "request_json", fake)
    return fake


def _add_args(task="1384", notes="rep", **overrides) -> list[str]:
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
        notes,
    ]
    if overrides.get("effort"):
        args += ["--effort", overrides["effort"]]
    return args


def _remote_row(*, id: int, origin_id: int, created_by: str = CLIENT, **fields) -> dict:
    row = {
        "id": id,
        "origin_id": origin_id,
        "created_by": created_by,
        "created_at": "2026-10-10T00:00:00Z",
        "profile": "builder-devin-max",
        "model_id": "swe-2-max",
        "task_ref": "1384",
        "tier": "T1",
        "role": "impl",
        "rounds": 1,
        "blockers_found": 0,
        "completed": 1,
        "input_tokens": None,
        "output_tokens": None,
        "notes": "rep",
        "recorded_at": "2026-10-10T00:00:00Z",
        "effort": None,
        "grade": "A",
        "table_grade": "A",
    }
    row.update(fields)
    return row


def _seed_cache_row(
    home,
    *,
    cache_key: str,
    origin_id: int,
    server_id: int | None = None,
    created_by: str | None = None,
    **fields,
) -> None:
    row = {
        "profile": "builder-devin-max",
        "model_id": "swe-2-max",
        "task_ref": "1384",
        "tier": "T1",
        "role": "impl",
        "rounds": 1,
        "blockers_found": 0,
        "completed": 1,
        "input_tokens": None,
        "output_tokens": None,
        "notes": "rep",
        "recorded_at": "2026-10-10T00:00:00Z",
        "effort": None,
        "grade": "A",
        "table_grade": "A",
    }
    row.update(fields)
    conn = bench._cache_connect()
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
                row["profile"],
                row["model_id"],
                row["task_ref"],
                row["tier"],
                row["role"],
                row["rounds"],
                row["blockers_found"],
                row["completed"],
                row["input_tokens"],
                row["output_tokens"],
                row["notes"],
                row["recorded_at"],
                row["effort"],
                row["grade"],
                row["table_grade"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _pending_rows(home) -> list[sqlite3.Row]:
    conn = bench._cache_connect()
    try:
        return conn.execute(
            "SELECT origin_id, task_ref, notes FROM bench_pending_reps ORDER BY origin_id"
        ).fetchall()
    finally:
        conn.close()


def test_origin_ids_stay_inside_the_band_over_10k_draws():
    """Band guard (mutant: _next_origin_id restored). Every draw must sit in
    [2^62, 2^63) — disjoint from legacy counters and the migrate band — and
    10k draws must not collide."""

    assert not hasattr(bench, "_next_origin_id")
    draws = [bench._new_rep_origin_id() for _ in range(10_000)]
    assert all(BAND_BASE <= value < BAND_TOP for value in draws)
    assert len(set(draws)) == len(draws)


def test_two_clients_same_token_distinct_origins_and_bound_ids(tmp_path, monkeypatch, capsys):
    """Two hosts, one token, separate local DBs: every add gets its own key
    and the printed srv id is the row that actually holds the rep."""

    fake = _remote(monkeypatch, tmp_path / "hosta")
    printed: dict[int, str] = {}
    origins: set[int] = set()
    for client, task, notes in (
        ("hosta", "a1", "rep from host A"),
        ("hostb", "b1", "rep from host B"),
        ("hosta", "a2", "second rep from host A"),
        ("hostb", "b2", "second rep from host B"),
    ):
        _client_env(tmp_path / client, monkeypatch)
        assert cli.main(_add_args(task=task, notes=notes)) == 0
        match = re.search(r"recorded rep id=srv:(\d+)", capsys.readouterr().out)
        assert match, "the add did not print a server id"
        printed[int(match.group(1))] = task

    assert len(printed) == 4
    assert len(fake.reps) == 4
    for row in fake.reps:
        assert BAND_BASE <= row["origin_id"] < BAND_TOP
        origins.add(row["origin_id"])
        # The printed id names exactly the row holding that rep's content.
        assert printed[row["id"]] == row["task_ref"]
    assert len(origins) == 4

    # Each client's cache holds at least its own reps, server-bound. (The
    # cache mirrors the fetched page — hosta's predates hostb's later adds.)
    for client, tasks in (("hosta", {"a1", "a2"}), ("hostb", {"b1", "b2"})):
        _client_env(tmp_path / client, monkeypatch)
        conn = bench._cache_connect()
        try:
            cached = conn.execute("SELECT server_id, task_ref FROM bench_cache_reps").fetchall()
        finally:
            conn.close()
        mine = [row for row in cached if row["task_ref"] in tasks]
        assert {row["task_ref"] for row in mine} == tasks
        assert all(row["server_id"] is not None for row in mine)


def test_new_server_409_prints_both_ids_and_differing_fields(tmp_path, monkeypatch, capsys):
    """Forced same-origin/different-content collision on the new server:
    409, both ids named, differing fields named, exit 2, nothing written."""

    fake = _remote(monkeypatch, tmp_path / "hosta")
    _client_env(tmp_path / "hosta", monkeypatch)
    assert cli.main(_add_args(task="victim", notes="host A's rep")) == 0
    out = capsys.readouterr().out
    assert "recorded rep id=srv:1" in out
    origin = fake.reps[0]["origin_id"]

    _client_env(tmp_path / "hostb", monkeypatch)
    # A buggy/mutant client reusing a taken key: force the collision the band
    # makes unreachable in practice.
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: origin)
    assert cli.main(_add_args(task="attacker", notes="host B's rep")) == 2
    captured = capsys.readouterr()
    assert f"origin:{origin}" in captured.err
    assert "srv:1" in captured.err
    assert "task_ref" in captured.err or "notes" in captured.err  # differing fields
    # The refused batch wrote nothing — the victim's row is intact and the
    # client did not retry under a fresh id.
    assert len(fake.reps) == 1
    assert fake.reps[0]["task_ref"] == "victim"
    assert fake.hits["PUT"] == 2

    # The refused rep stays queued locally as origin:<O> — never srv:1.
    pending = _pending_rows(tmp_path / "hostb")
    assert [row["origin_id"] for row in pending] == [origin]
    assert cli.main(["reps", "list"]) == 0
    out = capsys.readouterr().out
    attacker_line = next(line for line in out.splitlines() if "task=attacker" in line)
    assert f"id=origin:{origin}" in attacker_line
    assert "srv:" not in attacker_line


def test_old_server_contradicted_slot_binds_fail_closed(tmp_path, monkeypatch, capsys):
    """Old server (no ids, silent overwrite): host B's PUT lands, but the
    victim's resend reclaims the slot before B's post-write GET — the real
    ping-pong from the root-cause transcript. The bind then sees a different
    rep under the key and must stay origin:<O> (mutant: bind accepts a
    content-different row)."""

    fake = _remote(monkeypatch, tmp_path / "hosta", mode="old")
    _client_env(tmp_path / "hosta", monkeypatch)
    assert cli.main(_add_args(task="victim", notes="host A's rep")) == 0
    capsys.readouterr()
    origin = fake.reps[0]["origin_id"]
    victim = dict(fake.reps[0])

    _client_env(tmp_path / "hostb", monkeypatch)
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: origin)
    fake.after_put = lambda: fake.reps.__setitem__(
        next(i for i, row in enumerate(fake.reps) if row["origin_id"] == origin), victim
    )
    assert cli.main(_add_args(task="attacker", notes="host B's rep")) == 0
    captured = capsys.readouterr()
    assert f"recorded rep id=origin:{origin}" in captured.out
    assert "srv:" not in captured.out
    assert "warning" in captured.err
    assert f"origin:{origin}" in captured.err

    # B's own rep must not claim srv:1 — the cache row under srv:1 is the
    # server's row (A's content), and B's rep stays an unbound origin marker.
    conn = bench._cache_connect()
    try:
        rows = conn.execute(
            "SELECT cache_key, server_id, task_ref FROM bench_cache_reps WHERE origin_id = ?",
            (origin,),
        ).fetchall()
    finally:
        conn.close()
    by_task = {row["task_ref"]: row["server_id"] for row in rows}
    assert by_task["victim"] == 1  # the server's row, cached faithfully
    assert by_task["attacker"] is None  # never bound to the foreign pk


def test_old_server_clean_bind_still_learns_srv_id(tmp_path, monkeypatch, capsys):
    """On an old server with no collision the GET bind still proves and
    prints the real server pk."""

    _remote(monkeypatch, tmp_path / "hosta", mode="old")
    assert cli.main(_add_args()) == 0
    assert "recorded rep id=srv:1" in capsys.readouterr().out


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("request timed out"), bench.HttpError(503, "")],
    ids=["timeout", "5xx"],
)
def test_write_ahead_retry_resends_same_origin(tmp_path, monkeypatch, capsys, failure):
    """Write-ahead (mutant: fresh id on retry). A PUT whose response never
    arrives leaves a pending row; the retry resends the same origin_id and
    payload, and the server's identical-resend rule answers the same id."""

    fake = _remote(monkeypatch, tmp_path / "hosta")
    fake.fail_next_put = failure
    monkeypatch.setattr(bench, "_utc_now", lambda: "2026-10-10T12:00:00+00:00")

    assert cli.main(_add_args()) == 2
    captured = capsys.readouterr()
    assert "stays queued locally as origin:" in captured.err
    pending = _pending_rows(tmp_path / "hosta")
    assert len(pending) == 1
    origin = pending[0]["origin_id"]
    assert BAND_BASE <= origin < BAND_TOP
    assert fake.reps == []  # the PUT was refused before it landed

    # While the server is unreachable the rep lists as its origin key only.
    fake.offline = True
    assert cli.main(["reps", "list"]) == 0
    out = capsys.readouterr().out
    assert f"id=origin:{origin}" in out
    assert "id=srv:" not in out
    fake.offline = False

    assert cli.main(_add_args()) == 0
    out = capsys.readouterr().out
    assert "recorded rep id=srv:1" in out
    assert len(fake.reps) == 1
    assert fake.reps[0]["origin_id"] == origin  # resent under the same key
    assert _pending_rows(tmp_path / "hosta") == []  # confirmed: cleared


def test_write_ahead_retry_after_lost_response_keeps_one_row(tmp_path, monkeypatch, capsys):
    """The dangerous case: the PUT landed but the answer was lost. The retry
    must resend the same key — the server returns the same id, no dup."""

    fake = _remote(monkeypatch, tmp_path / "hosta")
    fake.fail_next_put = OSError("connection dropped after write")
    fake.fail_next_put_after_write = True
    monkeypatch.setattr(bench, "_utc_now", lambda: "2026-10-10T12:00:00+00:00")

    assert cli.main(_add_args()) == 2
    capsys.readouterr()
    assert len(fake.reps) == 1  # the write landed anyway
    origin = fake.reps[0]["origin_id"]

    assert cli.main(_add_args()) == 0
    out = capsys.readouterr().out
    assert "recorded rep id=srv:1" in out
    assert len(fake.reps) == 1
    assert fake.reps[0]["origin_id"] == origin
    assert _pending_rows(tmp_path / "hosta") == []


def test_cache_shadow_moves_contradicted_row_to_lost(tmp_path, monkeypatch, capsys):
    """Shadow rule (mutant: cached content shown under a contradicted srv id).

    The cache remembers our rep at srv:7; the server now holds a different
    rep there (an overwrite happened while the cache was stale). Refetch must
    show the server row as srv:7 and our copy as lost:<origin> — warned once,
    never written back."""

    fake = _remote(monkeypatch, tmp_path / "hosta")
    origin = BAND_BASE + 4242
    fake.reps.append(_remote_row(id=7, origin_id=origin, task_ref="other-rep", notes="whoever wrote last"))
    _seed_cache_row(
        tmp_path / "hosta",
        cache_key=f"shared:{origin}",
        origin_id=origin,
        server_id=7,
        created_by=CLIENT,
        task_ref="my-rep",
        notes="the rep this host recorded",
    )

    assert cli.main(["reps", "list"]) == 0
    captured = capsys.readouterr()
    assert "shadowed" in captured.err
    server_line = next(line for line in captured.out.splitlines() if "id=srv:7" in line)
    assert "task=other-rep" in server_line  # server content is authoritative
    lost_line = next(line for line in captured.out.splitlines() if f"id=lost:{origin}" in line)
    assert "task=my-rep" in lost_line  # the local copy keeps its own content
    assert "shadowed-by=srv:7" in lost_line
    assert "reps add" in captured.out  # the re-add hint
    assert fake.hits["PUT"] == 0  # nothing is ever written back

    conn = bench._cache_connect()
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM bench_cache_reps WHERE task_ref = 'my-rep'").fetchone()[0] == 0
        )
        lost = conn.execute("SELECT shadowed_by FROM bench_lost_reps").fetchall()
    finally:
        conn.close()
    assert [row["shadowed_by"] for row in lost] == [7]

    # The warning fires once — the copy lives in the lost table afterwards.
    assert cli.main(["reps", "list"]) == 0
    captured = capsys.readouterr()
    assert "shadowed" not in captured.err
    assert f"id=lost:{origin}" in captured.out


def test_push_local_uses_stable_banded_ids_and_is_idempotent(tmp_path, monkeypatch, capsys):
    """push_local derives a stable band key per local row, so a re-run
    resends the same origin_id and the server stays at one row."""

    home = tmp_path / "hosta"
    _client_env(home, monkeypatch, backend="local")
    monkeypatch.setattr(bench, "request_json", lambda *a, **k: pytest.fail("network called"))
    assert cli.main(_add_args(task="local-rep", notes="kept locally")) == 0
    capsys.readouterr()

    fake = _remote(monkeypatch, home)
    scores, reps = bench.push_local()
    assert (scores, reps) == (0, 1)
    assert len(fake.reps) == 1
    first = dict(fake.reps[0])
    assert BAND_BASE <= first["origin_id"] < BAND_TOP
    assert first["task_ref"] == "local-rep"

    scores, reps = bench.push_local()
    assert (scores, reps) == (0, 1)
    assert len(fake.reps) == 1  # resend, not a duplicate
    assert fake.reps[0] == first  # same server id, same content, same key


def test_migrate_still_uses_its_own_band(tmp_path, monkeypatch):
    """reps migrate keeps the [2^40, 2^40+2^48) band — untouched by #1384."""

    origin = bench._migrate_origin_id("host", "profile", 7)
    assert (1 << 40) <= origin < (1 << 40) + (1 << 48)


def _assert_acyclic_chain(exc: BaseException) -> None:
    """Every __cause__/__context__ edge reachable from ``exc``, walked with a
    visited set — hk 1403: a self-caused BenchRepConflictError made a set-less
    walk spin until the box OOM-killed it. No exception may be its own cause
    or context, and the walk must terminate."""

    nodes: list[BaseException] = []
    seen: set[int] = set()
    queue: list[BaseException] = [exc]
    while queue:
        node = queue.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        nodes.append(node)
        assert node.__cause__ is not node, f"{node!r} is its own __cause__"
        assert node.__context__ is not node, f"{node!r} is its own __context__"
        queue.extend(link for link in (node.__cause__, node.__context__) if link is not None)
    assert len(nodes) <= 8, "the exception chain did not terminate"


def _local_rep(task_ref: str) -> dict:
    return {
        "profile": "builder-devin-max",
        "model_id": "swe-2-max",
        "task_ref": task_ref,
        "tier": "T1",
        "role": "impl",
        "rounds": 1,
        "blockers_found": 0,
        "completed": 1,
        "recorded_at": "2026-09-20T10:00:00Z",
    }


def _unreadable_conflict_row(monkeypatch, fake: FakeRepStore, real_fetch_reps) -> None:
    """The post-409 annotate read fails while pre-PUT reads still work — the
    "server row not readable" branch of ``_annotate_rep_conflict``, where the
    self-cause bug lived."""

    def flaky(backend, *, query=None):
        if fake.hits["PUT"]:
            raise bench.BenchBackendError("reps read failed")
        return real_fetch_reps(backend, query=query)

    monkeypatch.setattr(bench, "_fetch_reps", flaky)


def test_409_error_chain_has_no_self_reference(tmp_path, monkeypatch):
    """Mutant guard: ``raise _annotate_rep_conflict(...) from exc`` with the
    annotate result being ``exc`` itself made BenchRepConflictError its own
    __cause__ (the hk 1403 OOM). For each call site — add_rep, migrate and
    push_local — with the conflicting server row unreadable, the raised chain
    must be acyclic and the walked error must still name both ids."""

    real_fetch_reps = bench._fetch_reps

    # add_rep -> _write_reps_handoffkeep -> PUT 409 -> annotate.
    fake = _remote(monkeypatch, tmp_path / "add")
    origin = BAND_BASE + 777
    fake.reps.append(_remote_row(id=1, origin_id=origin, task_ref="victim", notes="held by A"))
    monkeypatch.setattr(bench, "_new_rep_origin_id", lambda: origin)
    _unreadable_conflict_row(monkeypatch, fake, real_fetch_reps)
    with pytest.raises(bench.BenchRepConflictError) as excinfo:
        bench.add_rep(
            profile="builder-devin-max",
            model_id="swe-2-max",
            task_ref="attacker",
            tier="T1",
            role="impl",
            rounds=1,
            blockers_found=0,
            completed=1,
            notes="B's rep",
        )
    _assert_acyclic_chain(excinfo.value)
    assert isinstance(excinfo.value.__cause__, bench.BenchRepConflictError)
    assert f"origin:{origin}" in str(excinfo.value) and "srv:1" in str(excinfo.value)

    # migrate_reps -> PUT 409 -> annotate (pre-PUT reads must still succeed).
    mig = tmp_path / "migrate"
    _client_env(mig, monkeypatch, backend="local")
    bench.add_rep(**_local_rep("local-1"))
    fake = _remote(monkeypatch, mig)
    origin = bench._migrate_origin_id("mig-host", "builder-devin-max", 1)
    fake.reps.append(_remote_row(id=5, origin_id=origin, task_ref="occupied", notes="[src:mig-host] x"))
    _unreadable_conflict_row(monkeypatch, fake, real_fetch_reps)
    with pytest.raises(bench.BenchRepConflictError) as excinfo:
        bench.migrate_reps(host="mig-host", apply=True, force=True)
    _assert_acyclic_chain(excinfo.value)
    assert isinstance(excinfo.value.__cause__, bench.BenchRepConflictError)
    assert f"origin:{origin}" in str(excinfo.value) and "srv:5" in str(excinfo.value)

    # push_local -> PUT 409 -> annotate.
    pl = tmp_path / "pushlocal"
    _client_env(pl, monkeypatch, backend="local")
    bench.add_rep(**_local_rep("push-1"))
    fake = _remote(monkeypatch, pl)
    record = bench._read_local_reps_for_push()[0]
    origin = bench._push_local_origin_id(socket.gethostname() or "local", record)
    fake.reps.append(_remote_row(id=7, origin_id=origin, task_ref="occupied", notes="different rep"))
    _unreadable_conflict_row(monkeypatch, fake, real_fetch_reps)
    with pytest.raises(bench.BenchRepConflictError) as excinfo:
        bench.push_local()
    _assert_acyclic_chain(excinfo.value)
    assert isinstance(excinfo.value.__cause__, bench.BenchRepConflictError)
    assert f"origin:{origin}" in str(excinfo.value) and "srv:7" in str(excinfo.value)


def test_retryable_503_resends_the_same_pending_payload(tmp_path, monkeypatch, capsys):
    """503 bench_reps_retryable (the merged server contract, on deadlock or
    serialization failure) is a transient refusal: the rep stays pending and
    the retry resends the SAME payload — same origin_id, same recorded_at,
    every wire field identical (mutant: re-stamp recorded_at on resend, so
    the clock is advanced between the two adds to make that visible)."""

    fake = _remote(monkeypatch, tmp_path / "hosta")
    clock = itertools.count()
    monkeypatch.setattr(bench, "_utc_now", lambda: f"2026-10-10T12:00:{next(clock):02d}+00:00")
    fake.fail_next_put = bench.HttpError(503, json.dumps({"error": "bench_reps_retryable"}))

    assert cli.main(_add_args()) == 2
    assert "stays queued locally as origin:" in capsys.readouterr().err
    assert fake.reps == []  # refused before it landed
    assert len(_pending_rows(tmp_path / "hosta")) == 1

    assert cli.main(_add_args()) == 0
    assert "recorded rep id=srv:1" in capsys.readouterr().out
    assert len(fake.reps) == 1  # the identical resend landed once
    assert _pending_rows(tmp_path / "hosta") == []

    # Byte-for-byte identical wire payloads: the resend kept the pending
    # row's origin_id and its ORIGINAL recorded_at even though the clock moved.
    assert len(fake.put_payloads) == 2
    first, second = fake.put_payloads
    assert first == second
    assert second[0]["recorded_at"] == "2026-10-10T12:00:00+00:00"
    assert second[0]["origin_id"] == fake.reps[0]["origin_id"]


def test_anonymous_cache_row_is_never_retired_by_origin_alone(tmp_path, monkeypatch, capsys):
    """Shadow rule, anonymous echo (mutant: retire by origin_id alone). An
    unbound cache row carries no server id and no created_by — its server key
    was never learned — so a fetched row at the same origin with different
    content may be another creator's rep and must NOT park it in lost."""

    fake = _remote(monkeypatch, tmp_path / "hosta")
    origin = BAND_BASE + 31337
    fake.reps.append(
        _remote_row(id=3, origin_id=origin, created_by="other-client", task_ref="other", notes="not ours")
    )
    _seed_cache_row(
        tmp_path / "hosta",
        cache_key=f"origin:{origin}",
        origin_id=origin,
        server_id=None,
        created_by=None,
        task_ref="our-echo",
        notes="our unbound write",
    )

    assert cli.main(["reps", "list"]) == 0
    captured = capsys.readouterr()
    assert "shadowed" not in captured.err
    assert "id=lost:" not in captured.out
    assert "id=srv:3" in captured.out  # the foreign row displays under its own pk
    conn = bench._cache_connect()
    try:
        lost = conn.execute("SELECT task_ref FROM bench_lost_reps").fetchall()
    finally:
        conn.close()
    assert lost == []


def test_shadow_retire_compares_only_the_proven_holder(tmp_path, monkeypatch, capsys):
    """The retire match must be key-precise (mutant: any origin_id holder
    retires). A creator-known row moves to lost only when the UNIQUE fetched
    holder of its own (created_by, origin_id) pair contradicts it; a holder
    under another creator, or several holders of the pair, proves nothing."""

    fake = _remote(monkeypatch, tmp_path / "hosta")
    contradicted = BAND_BASE + 100
    foreign_only = BAND_BASE + 200
    ambiguous = BAND_BASE + 300
    # Our own pair slot holds a different rep -> real overwrite evidence.
    fake.reps.append(
        _remote_row(id=5, origin_id=contradicted, created_by=CLIENT, task_ref="overwriter", notes="diff")
    )
    # Only a FOREIGN creator holds this origin -> our pair slot is unproven.
    fake.reps.append(
        _remote_row(id=6, origin_id=foreign_only, created_by="other-client", task_ref="other", notes="diff")
    )
    # Two fetched rows claim our pair (a server invariant violation) -> keep.
    fake.reps.append(
        _remote_row(id=7, origin_id=ambiguous, created_by=CLIENT, task_ref="twin-a", notes="one")
    )
    fake.reps.append(
        _remote_row(id=8, origin_id=ambiguous, created_by=CLIENT, task_ref="twin-b", notes="two")
    )
    home = tmp_path / "hosta"
    _seed_cache_row(
        home,
        cache_key=f"{CLIENT}:{contradicted}",
        origin_id=contradicted,
        created_by=CLIENT,
        task_ref="ours",
    )
    _seed_cache_row(
        home,
        cache_key=f"{CLIENT}:{foreign_only}",
        origin_id=foreign_only,
        created_by=CLIENT,
        task_ref="ours-2",
    )
    _seed_cache_row(
        home,
        cache_key=f"{CLIENT}:{ambiguous}",
        origin_id=ambiguous,
        created_by=CLIENT,
        task_ref="ours-3",
    )

    assert cli.main(["reps", "list"]) == 0
    captured = capsys.readouterr()
    assert "shadowed" in captured.err  # exactly one row retires
    out = captured.out
    assert f"id=lost:{contradicted}" in out  # own pair contradicted -> lost
    assert "shadowed-by=srv:5" in out
    assert "task=ours-2" not in out and "task=ours-3" not in out
    assert f"id=lost:{foreign_only}" not in out
    assert f"id=lost:{ambiguous}" not in out
    conn = bench._cache_connect()
    try:
        lost_keys = [
            row["cache_key"] for row in conn.execute("SELECT cache_key FROM bench_lost_reps").fetchall()
        ]
    finally:
        conn.close()
    assert lost_keys == [f"{CLIENT}:{contradicted}"]


def test_pending_row_survives_identical_content_under_another_creator(tmp_path, monkeypatch, capsys):
    """Pending reconciliation (mutant: equal content under ANY creator
    clears). The server keys reps on (created_by, origin_id); a fetched row
    at our pending origin with identical content but another creator is not
    our write — the pending row must survive a generic refresh."""

    fake = _remote(monkeypatch, tmp_path / "hosta")
    monkeypatch.setattr(bench, "_utc_now", lambda: "2026-10-10T12:00:00+00:00")
    fake.fail_next_put = OSError("request dropped")

    assert cli.main(_add_args()) == 2
    capsys.readouterr()
    pending = _pending_rows(tmp_path / "hosta")
    assert len(pending) == 1
    origin = pending[0]["origin_id"]

    # Another creator's identical rep occupies that origin on the server.
    fake.reps.append(
        _remote_row(
            id=9,
            origin_id=origin,
            created_by="other-client",
            recorded_at="2026-10-10T12:00:00+00:00",
            table_grade=None,
        )
    )
    assert cli.main(["reps", "list"]) == 0
    captured = capsys.readouterr()
    assert "id=srv:9" in captured.out
    # The pending write is still queued — never confirmed under our key.
    assert [row["origin_id"] for row in _pending_rows(tmp_path / "hosta")] == [origin]


def test_pending_row_clears_on_own_confirmed_row(tmp_path, monkeypatch, capsys):
    """The positive half (mutant: pending rows never reconcile). A pending
    row whose own-creator server row is visible inside a write commit is
    confirmed and clears — here the first add's PUT landed but its answer
    was lost; the second add's commit proves the row under our created_by."""

    fake = _remote(monkeypatch, tmp_path / "hosta")
    fake.fail_next_put = OSError("connection dropped after write")
    fake.fail_next_put_after_write = True

    assert cli.main(_add_args(task="landed", notes="first rep")) == 2
    capsys.readouterr()
    assert len(fake.reps) == 1  # the write landed anyway
    origin = fake.reps[0]["origin_id"]
    assert len(_pending_rows(tmp_path / "hosta")) == 1

    # An unrelated successful add: its commit's bound write carries our
    # created_by, and the fetched page holds the pending rep's own row.
    assert cli.main(_add_args(task="second", notes="second rep")) == 0
    capsys.readouterr()
    assert len(fake.reps) == 2
    assert _pending_rows(tmp_path / "hosta") == []  # reconciled, not just hidden
    conn = bench._cache_connect()
    try:
        row = conn.execute(
            "SELECT server_id, created_by FROM bench_cache_reps WHERE origin_id = ?", (origin,)
        ).fetchone()
    finally:
        conn.close()
    assert row["server_id"] == 1 and row["created_by"] == CLIENT
