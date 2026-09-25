"""task #714 — one-time local bench.db reps -> handoffkeep migration tests.

``FakeRepStore`` models the deployed server contract faithfully: rep upserts
key on ``(created_by, origin_id)`` and the server stamps ``created_by`` itself
from the bearer-token identity (a client-supplied value is ignored), so the
source host can only be recorded in the row's client-owned fields (``notes``).
No test reaches the network — ``bench.request_json`` is replaced.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from collections import Counter

from scopefuel import bench, cli

HK_URL = "https://hk.invalid"
HK_HTTP_URL = "http://hk.invalid:8800"  # private-tunnel shape: non-local http
HK_TOKEN = "hk-test-token"
CLIENT = "ops"  # the token's client identity, stamped server-side on PUT
HOST = "test-host"


class FakeRepStore:
    """The reps scope only: GET/PUT /v1/bench/reps keyed on (created_by, origin_id)."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.reps: list[dict] = []
        self.hits: Counter[str] = Counter()
        # task_refs the fake silently loses on PUT while still ACKing the batch —
        # the only way ``reconcile`` can observe a missing row.
        self.drop_tasks: set[str] = set()

    def __call__(self, url, *, method="GET", headers=None, body=None, timeout=None, **_kw):
        assert (headers or {}).get("Authorization") == f"Bearer {HK_TOKEN}"
        url = str(url)
        assert url.startswith(self.url), f"request left the configured endpoint: {url}"
        split = urllib.parse.urlsplit(url)
        assert split.path == "/v1/bench/reps", f"unexpected path: {url}"
        params = urllib.parse.parse_qs(split.query)
        assert set(params) <= {"limit", "profile"}, f"unexpected query: {sorted(params)}"
        # The deployed server: ORDER BY id DESC LIMIT min(limit, 5000),
        # limit default 1000 — a bare GET sees only the newest 1000 rows.
        limit = min(int(params.get("limit", ["1000"])[0]), 5000)
        profile = params.get("profile", [""])[0]
        self.hits[method] += 1
        if method == "GET":
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
            # The server stamps created_by from the token identity and ignores
            # any client-supplied value.
            assert stored.get("created_by") is None
            stored["created_by"] = CLIENT
            stored.setdefault("created_at", "2026-09-25T00:00:00Z")
            if stored.get("task_ref") in self.drop_tasks:
                continue
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


def _seed_local(tmp_path, rows: list[dict]) -> None:
    """Write fixture reps into a local-backend bench.db."""
    _config(tmp_path, '[bench]\nbackend = "local"\n')
    for row in rows:
        bench.add_rep(**row)


def _remote_backend(tmp_path, monkeypatch, url: str = HK_URL) -> FakeRepStore:
    _config(tmp_path, '[bench]\nbackend = "handoffkeep"\n')
    monkeypatch.setenv("HANDOFFKEEP_URL", url)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", HK_TOKEN)
    fake = FakeRepStore(url)
    monkeypatch.setattr(bench, "request_json", fake)
    return fake


def _rep(task_ref: str, **overrides) -> dict:
    row = {
        "profile": "builder-devin",
        "model_id": "swe-2",
        "task_ref": task_ref,
        "tier": "T1",
        "role": "impl",
        "rounds": 1,
        "blockers_found": 0,
        "completed": 1,
        "recorded_at": "2026-09-20T10:00:00Z",
    }
    row.update(overrides)
    return row


def _remote_from_record(rep: bench.RepRecord, *, host: str | None, created_by: str = CLIENT) -> dict:
    """The remote wire shape of a local rep: ``host=None`` is an unstamped
    copy (e.g. written by ``bench push-local`` — raw origin rowid); a host gets
    the ``[src:<host>]`` marker and the derived origin_id."""
    if host is None:
        notes, origin_id = rep.notes, rep.id
    else:
        notes = f"{rep.notes} [src:{host}]" if rep.notes else f"[src:{host}]"
        origin_id = bench._migrate_origin_id(host, rep.id)
    return {
        "id": rep.id + 1000,
        "origin_id": origin_id,
        "created_by": created_by,
        "created_at": "2026-09-25T00:00:00Z",
        "profile": rep.profile,
        "model_id": rep.model_id,
        "task_ref": rep.task_ref,
        "tier": rep.tier,
        "role": rep.role,
        "rounds": rep.rounds,
        "blockers_found": rep.blockers_found,
        "completed": rep.completed,
        "input_tokens": rep.input_tokens,
        "output_tokens": rep.output_tokens,
        "notes": notes,
        "recorded_at": rep.recorded_at,
        "effort": rep.effort,
        "grade": rep.grade,
        "table_grade": rep.table_grade,
    }


def _local_reps() -> list[bench.RepRecord]:
    return bench._read_local_reps_for_push()


def test_dry_run_counts_sample_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """Dry-run is the default: counts + sample, no PUT, no local writes."""
    _seed_local(tmp_path, [_rep("714-a"), _rep("714-b", notes="E6 run"), _rep("714-c", role="verify")])
    fake = _remote_backend(tmp_path, monkeypatch)
    # One rep already migrated in an earlier run.
    fake.reps.append(_remote_from_record(_local_reps()[0], host=HOST))

    assert cli.main(["reps", "migrate", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "local=3 already-present=1 to-insert=2" in out
    assert "dry-run" in out and "--apply" in out
    assert "task=714-b" in out and "task=714-c" in out  # the sample shows pending rows
    assert "task=714-a" not in out.split("dry-run")[1]  # present row is not a sample row

    assert fake.hits["PUT"] == 0  # mutant target: a dry-run that PUTs turns RED
    assert [rep.task_ref for rep in _local_reps()] == ["714-a", "714-b", "714-c"]
    conn = sqlite3.connect(bench.db_path())
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "bench_cache_reps" not in tables  # dry-run must not even create cache tables


def test_apply_inserts_stamps_host_and_rerun_inserts_nothing(tmp_path, monkeypatch, capsys):
    _seed_local(
        tmp_path,
        [
            _rep("714-a"),
            _rep("714-b", notes="토큰 미상", input_tokens=120000, effort="xhigh", grade="A+"),
            _rep("714-c", role="verify", completed=0),
        ],
    )
    fake = _remote_backend(tmp_path, monkeypatch)

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "inserted=3" in out
    assert "local=3 remote-this-host=3 missing=0 extra-remote=0" in out
    assert len(fake.reps) == 3
    by_task = {row["task_ref"]: row for row in fake.reps}
    assert by_task["714-a"]["notes"] == f"[src:{HOST}]"
    assert by_task["714-b"]["notes"] == f"토큰 미상 [src:{HOST}]"
    assert by_task["714-c"]["notes"] == f"[src:{HOST}]"
    # Fields survive the trip: a dropped field here is data loss.
    for local in _local_reps():
        remote = by_task[local.task_ref]
        for field_name in bench._REP_COLUMNS:
            if field_name in ("id", "notes"):
                continue
            assert remote[field_name] == getattr(local, field_name), field_name
        # origin_id is the stable derived id, not the raw local rowid.
        assert remote["origin_id"] == bench._migrate_origin_id(HOST, local.id)
        assert remote["origin_id"] >= 1 << 40
        assert remote["created_by"] == CLIENT  # server-side stamp

    # Rerun: every row already present, nothing is PUT again.
    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "inserted=0 skipped=3" in out
    assert fake.hits["PUT"] == 1  # mutant target: re-PUTing on rerun turns RED
    assert len(fake.reps) == 3


def test_http_refuses_without_opt_in_or_one_shot_flag(tmp_path, monkeypatch, capsys):
    """CWE-319: plaintext http without any opt-in sends nothing — in auto mode
    (auto-local-insecure-url) and under an explicit backend = "handoffkeep"."""
    _seed_local(tmp_path, [_rep("714-a")])

    # auto mode: credentials exist but the URL is plaintext http.
    _config(tmp_path, "[bench]\n")
    monkeypatch.setenv("HANDOFFKEEP_URL", HK_HTTP_URL)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", HK_TOKEN)
    fake = FakeRepStore(HK_HTTP_URL)
    monkeypatch.setattr(bench, "request_json", fake)
    for argv in (
        ["reps", "migrate", "--host", HOST],
        ["reps", "migrate", "--apply", "--host", HOST],
    ):
        assert cli.main(argv) == 2
        assert "--allow-plaintext-http" in capsys.readouterr().err
    assert not fake.hits

    # explicit backend = "handoffkeep": same refusal before the wire.
    _config(tmp_path, '[bench]\nbackend = "handoffkeep"\n')
    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 2
    assert "allow_plaintext_reps" in capsys.readouterr().err
    assert not fake.hits


def test_one_shot_flag_and_config_opt_in_each_allow_http(tmp_path, monkeypatch, capsys):
    """The chosen trade-off: a per-invocation flag, not a config edit. The
    migration exists to move history *before* enabling allow_plaintext_reps —
    requiring the persistent flag would conflate the two decisions."""
    _seed_local(tmp_path, [_rep("714-a")])

    monkeypatch.setenv("HANDOFFKEEP_URL", HK_HTTP_URL)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", HK_TOKEN)
    fake = FakeRepStore(HK_HTTP_URL)
    monkeypatch.setattr(bench, "request_json", fake)
    _config(tmp_path, '[bench]\nbackend = "handoffkeep"\n')

    # One-shot flag: works without touching config.
    assert cli.main(["reps", "migrate", "--apply", "--host", HOST, "--allow-plaintext-http"]) == 0
    assert len(fake.reps) == 1
    assert fake.reps[0]["notes"] == f"[src:{HOST}]"

    # The flag is per-call: the catalog use is unaffected, and a second call
    # without it refuses again.
    assert bench.read_catalog().source == "snapshot"
    assert cli.main(["reps", "migrate", "--host", HOST]) == 2
    capsys.readouterr()

    # The persistent opt-in is also honored.
    _config(tmp_path, '[bench]\nbackend = "handoffkeep"\nallow_plaintext_reps = true\n')
    assert cli.main(["reps", "migrate", "--host", HOST]) == 0


def test_unstamped_identical_remote_row_counts_as_present(tmp_path, monkeypatch, capsys):
    """A rep already pushed by bench push-local (no marker, raw origin rowid)
    is the same rep — migrate must not write a second copy."""
    _seed_local(tmp_path, [_rep("714-a"), _rep("714-b")])
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps.append(_remote_from_record(_local_reps()[0], host=None))

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "inserted=1 skipped=1" in out
    assert len(fake.reps) == 2
    pushed = next(row for row in fake.reps if row["task_ref"] == "714-b")
    assert pushed["notes"].endswith(f"[src:{HOST}]")
    # The pre-existing unstamped copy is counted for this host in reconcile.
    assert "local=2 remote-this-host=2" in out


def test_other_host_marker_does_not_dedup(tmp_path, monkeypatch, capsys):
    """The dedup key includes the host: a *different* rep (same content key,
    different field values) migrated from another machine must not block this
    host's copy."""
    _seed_local(tmp_path, [_rep("714-a")])
    fake = _remote_backend(tmp_path, monkeypatch)
    remote = _remote_from_record(_local_reps()[0], host="other-host")
    remote["rounds"] = 99  # a genuinely different rep sharing the content key
    fake.reps.append(remote)

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "inserted=1" in out
    assert len(fake.reps) == 2
    assert "remote-this-host=1" in out  # only this host's copy counts


def test_identical_row_migrated_by_other_host_is_present(tmp_path, monkeypatch, capsys):
    """A rep already migrated under a different host string (a drifted
    gethostname(), or an identical copy on another machine) is the same rep —
    it must not be duplicated."""
    _seed_local(tmp_path, [_rep("714-a")])
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps.append(_remote_from_record(_local_reps()[0], host="old-hostname"))

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "inserted=0 skipped=1" in out
    assert len(fake.reps) == 1
    # the identical copy covers the local rep in reconcile
    assert "local=1 remote-this-host=1 missing=0" in out


def test_unstamped_remote_row_with_different_fields_reinserts(tmp_path, monkeypatch, capsys):
    """An unstamped remote row matching only the content key is NOT the same
    rep — e.g. the local row was edited after push-local (F3/M10 coverage)."""
    _seed_local(tmp_path, [_rep("714-a")])
    fake = _remote_backend(tmp_path, monkeypatch)
    remote = _remote_from_record(_local_reps()[0], host=None)
    remote["rounds"] = 99
    fake.reps.append(remote)

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "inserted=1 skipped=0" in out
    assert len(fake.reps) == 2


def _seed_local_bulk(tmp_path, n: int, *, task_prefix: str = "bulk") -> None:
    """Insert n fixture reps in one connection — add_rep per row is too slow
    at 1k+."""
    _seed_local(tmp_path, [_rep(f"{task_prefix}-0")])
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.executemany(
            "INSERT INTO reps (profile, model_id, task_ref, tier, role, rounds, "
            "blockers_found, completed, input_tokens, output_tokens, notes, "
            "recorded_at, effort, grade, table_grade) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "builder-devin",
                    "swe-2",
                    f"{task_prefix}-{i}",
                    "T1",
                    "impl",
                    1,
                    0,
                    1,
                    None,
                    None,
                    None,
                    "2026-09-20T10:00:00Z",
                    None,
                    None,
                    None,
                )
                for i in range(1, n)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _remote_row(
    row_id: int, task_ref: str, *, profile: str = "builder-devin", host: str | None = HOST
) -> dict:
    notes = f"[src:{host}]" if host else None
    return {
        "id": row_id,
        "origin_id": row_id + (1 << 40),
        "created_by": CLIENT,
        "created_at": "2026-09-25T00:00:00Z",
        "profile": profile,
        "model_id": "swe-2",
        "task_ref": task_ref,
        "tier": "T1",
        "role": "impl",
        "rounds": 1,
        "blockers_found": 0,
        "completed": 1,
        "input_tokens": None,
        "output_tokens": None,
        "notes": notes,
        "recorded_at": "2026-09-20T10:00:00Z",
        "effort": None,
        "grade": None,
        "table_grade": None,
    }


def test_apply_beyond_the_default_remote_window(tmp_path, monkeypatch, capsys):
    """The server's GET window defaults to the newest 1000 rows; at this
    tool's scale (~1k+ local reps) migrate must request the 5000 cap or
    dedup and reconcile see only a tail of the store."""
    _seed_local_bulk(tmp_path, 1005)
    fake = _remote_backend(tmp_path, monkeypatch)

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "inserted=1005" in out
    assert "local=1005 remote-this-host=1005 missing=0" in out
    assert len(fake.reps) == 1005

    # Rerun: every row still visible through the windowed fetch — no re-PUT.
    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "inserted=0 skipped=1005" in out
    assert "missing=0" in out


def test_full_unfiltered_window_still_migrates_local_profiles(tmp_path, monkeypatch, capsys):
    """A store bigger than the read window under *other* profiles does not
    block this host: the per-profile page is what must fit."""
    _seed_local(tmp_path, [_rep("714-a"), _rep("714-b")])
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps.extend(
        _remote_row(i + 1, f"other-{i}", profile="other-prof", host="other-host") for i in range(5000)
    )

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "inserted=2" in out
    assert "missing=0" in out
    assert len(fake.reps) == 5002


def test_full_profile_window_fails_closed(tmp_path, monkeypatch, capsys):
    """When a local profile's remote page comes back full, completeness is
    unprovable — refuse rather than dedup/reconcile against a tail."""
    _seed_local(tmp_path, [_rep("714-a")])
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps.extend(_remote_row(i + 1, f"other-{i}") for i in range(5000))

    for argv in (
        ["reps", "migrate", "--host", HOST],
        ["reps", "migrate", "--apply", "--host", HOST],
    ):
        assert cli.main(argv) == 2
        assert "read window" in capsys.readouterr().err
    assert fake.hits["PUT"] == 0


def test_content_key_collision_does_not_mask_a_missing_rep(tmp_path, monkeypatch, capsys):
    """Two local reps can share (profile, model, task, role, recorded_at) —
    only their fields differ. If the server holds just one stamped copy, the
    other must still be pending/missing, not masked by the key."""
    _seed_local(
        tmp_path,
        [_rep("714-a", rounds=1), _rep("714-a", rounds=2)],  # same key, different rows
    )
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps.append(_remote_from_record(_local_reps()[0], host=HOST))

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "inserted=1 skipped=1" in out
    assert len(fake.reps) == 2
    assert "missing=0" in out

    # and if the server loses one twin, reconcile must see it
    fake.drop_tasks.add("714-a")  # PUT-acked then dropped — but only one row exists per origin_id
    fake.reps.pop()  # the rounds=2 copy is gone from the store
    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 2
    assert "missing=1" in capsys.readouterr().out


def test_derived_origin_collision_refuses_without_force(tmp_path, monkeypatch, capsys):
    """N1: a pending rep whose derived origin_id is already held by a
    *different* remote rep would silently overwrite it (a second machine on
    the same --host string). Dry-run flags it; --apply refuses; --force is
    the explicit override."""
    _seed_local(tmp_path, [_rep("714-a")])
    fake = _remote_backend(tmp_path, monkeypatch)
    foreign = _remote_row(900, "m1-0")  # stamped for this host, different rep
    foreign["origin_id"] = bench._migrate_origin_id(HOST, 1)
    fake.reps.append(foreign)

    assert cli.main(["reps", "migrate", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "would-overwrite=1" in out and "--force" in out

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 2
    err = capsys.readouterr().err
    assert "would overwrite" in err and "--force" in err
    assert fake.hits["PUT"] == 0
    assert fake.reps[0]["task_ref"] == "m1-0"  # nothing was overwritten

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST, "--force"]) == 0
    assert fake.reps[0]["task_ref"] == "714-a"  # deliberate overwrite
    assert len(fake.reps) == 1


def test_renumbered_local_rowid_heals_via_identical_match(tmp_path, monkeypatch, capsys):
    """N4: a this-host stamped row whose origin_id no longer matches the
    local rowid (ids renumbered) still counts as present by full-row match —
    no duplicate copy."""
    _seed_local(tmp_path, [_rep("714-a")])
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps.append(_remote_from_record(_local_reps()[0], host=HOST))
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute("UPDATE reps SET id = id + 100")
        conn.commit()
    finally:
        conn.close()

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "inserted=0 skipped=1" in out
    assert len(fake.reps) == 1


def test_notes_marker_text_is_not_confused_with_stamp(tmp_path, monkeypatch, capsys):
    """A local notes field that literally contains '[src:X]' text: the remote
    stamp is the *last* marker — unstamp must restore 'a [src:X]' exactly."""
    _seed_local(tmp_path, [_rep("714-a", notes="a [src:X]")])
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.reps.append(_remote_from_record(_local_reps()[0], host="other-host"))

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    assert "inserted=0 skipped=1" in capsys.readouterr().out
    assert len(fake.reps) == 1


def test_stamped_plus_twins_do_not_double_count(tmp_path, monkeypatch, capsys):
    """R12/F6: a stamped-for-host row plus an unstamped push-local twin plus
    an other-host twin is one rep, counted once."""
    _seed_local(tmp_path, [_rep("714-a")])
    fake = _remote_backend(tmp_path, monkeypatch)
    rep = _local_reps()[0]
    fake.reps.extend(
        [
            _remote_from_record(rep, host=HOST),
            _remote_from_record(rep, host=None),
            _remote_from_record(rep, host="other-host"),
        ]
    )

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    out = capsys.readouterr().out
    assert "inserted=0 skipped=1" in out
    assert "local=1 remote-this-host=1" in out
    assert len(fake.reps) == 3


def test_empty_host_is_rejected(tmp_path, monkeypatch, capsys):
    """R14/F5: --host \"\" must not silently fall back to gethostname()."""
    _seed_local(tmp_path, [_rep("714-a")])
    _remote_backend(tmp_path, monkeypatch)
    assert cli.main(["reps", "migrate", "--host", ""]) == 2
    assert "--host" in capsys.readouterr().err


def test_non_rfc3339_offset_timestamp_refuses(tmp_path, monkeypatch, capsys):
    """N2: an offset-bearing but non-RFC3339 recorded_at (space separator,
    no seconds, colonless offset) parses in Python but fails the Go server."""
    _seed_local(tmp_path, [_rep("714-a")])
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute("UPDATE reps SET recorded_at = '2026-09-20 10:00:00+00:00' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    fake = _remote_backend(tmp_path, monkeypatch)

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 2
    assert "recorded_at" in capsys.readouterr().err
    assert fake.hits["PUT"] == 0


def test_naive_recorded_at_refuses_before_writing(tmp_path, monkeypatch, capsys):
    """handoffkeep decodes RFC3339 — a naive recorded_at would die inside a
    PUT batch. Refuse up front, naming the local row."""
    _seed_local(tmp_path, [_rep("714-a")])
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute("UPDATE reps SET recorded_at = '2026-09-20T10:00:00' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    fake = _remote_backend(tmp_path, monkeypatch)

    for argv in (
        ["reps", "migrate", "--host", HOST],
        ["reps", "migrate", "--apply", "--host", HOST],
    ):
        assert cli.main(argv) == 2
        err = capsys.readouterr().err
        assert "recorded_at" in err and "ids: 1" in err
    assert fake.hits["PUT"] == 0


def test_reconcile_lists_missing_rows_and_fails(tmp_path, monkeypatch, capsys):
    """A row the server ACK'd but lost shows up in the reconcile output."""
    _seed_local(tmp_path, [_rep("714-a"), _rep("lost-1"), _rep("714-c")])
    fake = _remote_backend(tmp_path, monkeypatch)
    fake.drop_tasks.add("lost-1")

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 2
    out = capsys.readouterr().out
    assert "missing=1" in out
    assert "task=lost-1" in out


def test_recorded_at_timezone_spelling_does_not_duplicate(tmp_path, monkeypatch, capsys):
    """The server returns TIMESTAMPTZ in its own spelling — the same instant
    written as +00:00 instead of Z must still dedup."""
    _seed_local(tmp_path, [_rep("714-a", recorded_at="2026-09-20T10:00:00Z")])
    fake = _remote_backend(tmp_path, monkeypatch)
    remote = _remote_from_record(_local_reps()[0], host=HOST)
    remote["recorded_at"] = "2026-09-20T10:00:00+00:00"
    fake.reps.append(remote)

    assert cli.main(["reps", "migrate", "--apply", "--host", HOST]) == 0
    assert "inserted=0" in capsys.readouterr().out
    assert len(fake.reps) == 1


def test_migrate_without_handoffkeep_backend_names_the_blocker(tmp_path, monkeypatch, capsys):
    _seed_local(tmp_path, [_rep("714-a")])
    monkeypatch.delenv("HANDOFFKEEP_URL", raising=False)
    monkeypatch.delenv("HANDOFFKEEP_TOKEN", raising=False)

    assert cli.main(["reps", "migrate", "--host", HOST]) == 2
    assert "handoffkeep backend" in capsys.readouterr().err


def test_migrate_rejects_bad_host_marker(tmp_path, monkeypatch, capsys):
    """A host containing ']' would corrupt the [src:host] marker round-trip."""
    _seed_local(tmp_path, [_rep("714-a")])
    _remote_backend(tmp_path, monkeypatch)
    assert cli.main(["reps", "migrate", "--apply", "--host", "bad]host"]) == 2
    assert "--host" in capsys.readouterr().err


def test_dry_run_default_leaves_fixture_db_byte_identical(tmp_path, monkeypatch):
    """fixture bench.db: the local file is opened read-only for the whole run."""
    _seed_local(tmp_path, [_rep("714-a", notes="fixture"), _rep("714-b")])
    db = bench.db_path()
    before = db.read_bytes()
    _remote_backend(tmp_path, monkeypatch)

    result = bench.migrate_reps(host=HOST)
    assert result.applied is False
    assert result.local_count == 2 and len(result.pending) == 2
    assert db.read_bytes() == before
