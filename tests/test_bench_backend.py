"""Canonical handoffkeep bench backend tests at the HTTP boundary."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter

import pytest

from scopefuel import bench, cli, recommend
from scopefuel.http import HttpError
from scopefuel.model import Bucket, ProviderResult, Scope

# Contract §2.1 fixtures are intentionally retained verbatim, including fields
# owned by the server, so parsing cannot silently depend on a reduced fake.
SCORE_FIXTURES = json.loads(
    r"""[
  {
    "model_id": "claude-opus-5",
    "effort": "high",
    "harness": "claude-code",
    "source": "AA-agent",
    "metric": "agentic",
    "score": 13.4,
    "rank": 18,
    "captured_at": "2026-07-31T00:00:00Z",
    "time_per_task_min": 13.4,
    "cost_per_task_usd": 3.8,
    "provenance": "operator-approved manual import 2026-09-07",
    "updated_by": "bench-client",
    "updated_at": "2026-09-07T04:30:00Z"
  },
  {
    "model_id": "kimi-k3",
    "effort": "",
    "harness": "",
    "source": "AA-model",
    "metric": "coding_index",
    "score": 72.0,
    "rank": null,
    "captured_at": "2026-09-07T00:00:00Z",
    "time_per_task_min": null,
    "cost_per_task_usd": null,
    "provenance": "",
    "updated_by": "bench-client",
    "updated_at": "2026-09-07T04:30:00Z"
  }
]"""
)

REP_FIXTURE = json.loads(
    r"""{
  "id": 1,
  "origin_id": 568,
  "profile": "codex-terra",
  "model_id": "gpt-5.6-terra",
  "task_ref": "PR#42",
  "tier": "T2",
  "role": "impl",
  "rounds": 1,
  "blockers_found": 0,
  "completed": 1,
  "input_tokens": 120000,
  "output_tokens": 8000,
  "notes": "토큰 미상",
  "recorded_at": "2026-09-01T10:00:00Z",
  "effort": "max",
  "grade": "A+",
  "table_grade": "A+",
  "created_by": "bench-client",
  "created_at": "2026-09-07T04:30:00Z"
}"""
)

GRADE_FIXTURE = json.loads(
    r"""{
  "profile": "codex-terra-max",
  "grade": "S",
  "boundary_version": "2026-09-07",
  "deviation_ref": "deviation-2026-09-07-bench-canonical",
  "decided_at": "2026-09-07T04:30:00Z",
  "decided_by": "bench-client"
}"""
)


class FakeHandoffkeep:
    """A stateful protocol fake: only the HTTP boundary is replaced."""

    def __init__(self):
        self.scores = self._copy(SCORE_FIXTURES)
        self.reps = [self._copy(REP_FIXTURE)]
        self.grades = [self._copy(GRADE_FIXTURE)]
        self.hits: Counter[tuple[str, str]] = Counter()
        self.put_bodies: list[tuple[str, dict]] = []
        self.offline = False
        self.fail_put = False

    @staticmethod
    def _copy(value):
        return json.loads(json.dumps(value))

    @staticmethod
    def _scope(url: str) -> str:
        for scope in ("scores", "reps", "grades"):
            if url.rstrip("/").endswith(f"/v1/bench/{scope}"):
                return scope
        raise AssertionError(f"unexpected URL: {url}")

    def request_json(self, url, *, method="GET", headers=None, body=None, timeout=20.0):
        assert headers is not None
        assert headers["Authorization"] == "Bearer test-token"
        assert timeout == 20.0
        scope = self._scope(url)
        self.hits[(method, scope)] += 1
        if self.offline:
            raise OSError("offline")
        if method == "GET":
            return {scope: self._copy(getattr(self, scope))}
        assert method == "PUT"
        assert body is not None
        self.put_bodies.append((scope, self._copy(body)))
        if self.fail_put:
            raise HttpError(500, "failed")
        rows = body[scope]
        if scope == "scores":
            for row in rows:
                fields = ("model_id", "effort", "harness", "source", "metric")
                key = tuple(row.get(field, "") for field in fields)
                stored = self._copy(row)
                stored.setdefault("updated_by", "bench-client")
                stored.setdefault("updated_at", "2026-09-07T04:30:00Z")
                for index, old in enumerate(self.scores):
                    old_key = tuple(old.get(field, "") for field in fields)
                    if old_key == key:
                        self.scores[index] = stored
                        break
                else:
                    self.scores.append(stored)
        elif scope == "reps":
            for row in rows:
                stored = self._copy(row)
                match = next((old for old in self.reps if old["origin_id"] == stored["origin_id"]), None)
                next_id = max((old["id"] for old in self.reps), default=0) + 1
                stored["id"] = match["id"] if match is not None else next_id
                stored.setdefault("created_by", "bench-client")
                stored.setdefault("created_at", "2026-09-07T04:30:00Z")
                if match is None:
                    self.reps.append(stored)
                else:
                    self.reps[self.reps.index(match)] = stored
        else:
            for row in rows:
                stored = self._copy(row)
                stored.setdefault("decided_at", "2026-09-07T04:30:00Z")
                stored.setdefault("decided_by", "bench-client")
                match = next((old for old in self.grades if old["profile"] == stored["profile"]), None)
                if match is None:
                    self.grades.append(stored)
                else:
                    self.grades[self.grades.index(match)] = stored
        return {"upserted": len(rows)}


def _set_backend(tmp_path, monkeypatch, name="handoffkeep", ttl=21600):
    data_home = tmp_path / "data"
    config_home = tmp_path / "config"
    config_file = config_home / "scopefuel" / "config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(f'[bench]\nbackend = "{name}"\ncache_ttl_s = {ttl}\n', encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("HANDOFFKEEP_URL", "https://example.invalid")
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", "test-token")
    return data_home


@pytest.fixture
def handoffkeep(tmp_path, monkeypatch):
    data_home = _set_backend(tmp_path, monkeypatch)
    fake = FakeHandoffkeep()
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    return data_home, fake


def _score(model_id="gpt-5.6-terra", *, effort="max", score=61.0):
    return bench.ModelScore(
        model_id=model_id,
        effort=effort,
        harness="codex" if effort is not None else None,
        source="AA-agent" if effort is not None else "AA-model",
        metric="agentic" if effort is not None else "coding_index",
        score=score,
        rank=1,
        captured_at="2026-09-07T00:00:00Z",
    )


def _provider(provider_id="kiro"):
    now = dt.datetime.now(dt.UTC)
    return ProviderResult(
        id=provider_id,
        pool_class="spend",
        buckets=[
            Bucket(
                label="30d",
                window="30d",
                used_pct=10.0,
                resets_at=(now + dt.timedelta(days=20)).isoformat(),
                scope=Scope("account"),
                horizon="week",
            )
        ],
    )


def test_default_backend_is_local_without_creating_a_cache(tmp_path, monkeypatch):
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(bench, "request_json", lambda *args, **kwargs: pytest.fail("network called"))

    resolved = bench.bench_backend()
    assert resolved.name == bench.BENCH_BACKEND_LOCAL
    assert resolved.cache_ttl_s == 21600
    assert bench.read_scores() == []
    assert not (data_home / "scopefuel" / "bench.db").exists()


def test_local_backend_never_calls_network_for_bench_paths(tmp_path, monkeypatch):
    data_home = _set_backend(tmp_path, monkeypatch, "local")
    monkeypatch.setattr(bench, "request_json", lambda *args, **kwargs: pytest.fail("network called"))
    provider = _provider()
    monkeypatch.setattr(cli, "registry", lambda: {"kiro": lambda: provider})

    assert cli.main(["--recommend", "S", "--no-cache"]) == 0
    assert cli.main(["gate", "--profile", "kiro-sonnet", "--no-cache"]) == 0
    assert cli.main(["bench", "show", "gpt-5.6-terra"]) == 0
    assert not (data_home / "scopefuel" / "bench.db").exists()


def test_unknown_backend_degrades_to_local_once(tmp_path, monkeypatch, capsys):
    _set_backend(tmp_path, monkeypatch, "postgres")
    monkeypatch.setattr(bench, "request_json", lambda *args, **kwargs: pytest.fail("network called"))

    assert bench.read_scores() == []
    assert capsys.readouterr().err.splitlines() == ["warning: unknown bench backend; using local"]


def test_score_wire_mapping_ttl_endpoint_and_fail_open(handoffkeep, monkeypatch, capsys):
    data_home, fake = handoffkeep
    first = bench.read_scores()
    assert len(first) == 2
    assert first[1].effort is None and first[1].harness is None
    assert fake.hits[("GET", "scores")] == 1

    assert bench.read_scores() == first
    assert fake.hits[("GET", "scores")] == 1

    conn = sqlite3.connect(data_home / "scopefuel" / "bench.db")
    try:
        old = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=21601)).isoformat()
        conn.execute("UPDATE bench_cache_meta SET fetched_at = ? WHERE scope = 'scores'", (old,))
        conn.commit()
    finally:
        conn.close()
    bench.read_scores()
    assert fake.hits[("GET", "scores")] == 2

    monkeypatch.setenv("HANDOFFKEEP_URL", "https://example.invalid/second")
    bench.read_scores()
    assert fake.hits[("GET", "scores")] == 3

    conn = sqlite3.connect(data_home / "scopefuel" / "bench.db")
    try:
        old = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=21601)).isoformat()
        conn.execute("UPDATE bench_cache_meta SET fetched_at = ? WHERE scope = 'scores'", (old,))
        conn.commit()
    finally:
        conn.close()
    fake.offline = True
    try:
        cached = bench.read_scores()
    except bench.BenchBackendError:
        cached = None
    assert cached == first
    assert capsys.readouterr().err.splitlines() == [
        "warning: handoffkeep unreachable; using cached bench scores (age 6.0h)"
    ]


def test_empty_cache_offline_is_a_single_warning(handoffkeep, capsys):
    _, fake = handoffkeep
    fake.offline = True

    assert bench.read_scores() == []
    assert capsys.readouterr().err.splitlines() == [
        "warning: handoffkeep unreachable; using cached bench scores (no cached data)"
    ]


def test_write_is_fail_closed_and_writes_through_cache(handoffkeep, tmp_path, capsys):
    data_home, fake = handoffkeep
    bench.read_scores()
    before_conn = sqlite3.connect(data_home / "scopefuel" / "bench.db")
    try:
        before_rows = before_conn.execute("SELECT * FROM bench_cache_scores ORDER BY model_id").fetchall()
        before_meta = before_conn.execute("SELECT * FROM bench_cache_meta WHERE scope = 'scores'").fetchall()
    finally:
        before_conn.close()

    fake.fail_put = True
    with pytest.raises(bench.BenchBackendError):
        bench.upsert_scores([_score("new-model")])
    after_conn = sqlite3.connect(data_home / "scopefuel" / "bench.db")
    try:
        after_rows = after_conn.execute("SELECT * FROM bench_cache_scores ORDER BY model_id").fetchall()
        after_meta = after_conn.execute("SELECT * FROM bench_cache_meta WHERE scope = 'scores'").fetchall()
        assert after_rows == before_rows
        assert after_meta == before_meta
    finally:
        after_conn.close()

    imported = tmp_path / "fail-closed.toml"
    imported.write_text(
        'source = "AA-agent"\n'
        'metric = "agentic"\n'
        'effort = "max"\n'
        'harness = "codex"\n'
        'captured_at = "2026-09-07T00:00:00Z"\n'
        "[[scores]]\n"
        'model_id = "fail-closed"\n'
        "score = 60.0\n",
        encoding="utf-8",
    )
    assert cli.main(["bench", "import", str(imported)]) == 2
    assert capsys.readouterr().err.splitlines() == ["error: handoffkeep request failed"]
    after_cli_conn = sqlite3.connect(data_home / "scopefuel" / "bench.db")
    try:
        assert (
            after_cli_conn.execute("SELECT * FROM bench_cache_scores ORDER BY model_id").fetchall()
            == before_rows
        )
        assert (
            after_cli_conn.execute("SELECT * FROM bench_cache_meta WHERE scope = 'scores'").fetchall()
            == before_meta
        )
    finally:
        after_cli_conn.close()

    fake.fail_put = False
    written = _score("wire-none", effort=None, score=72.0)
    assert bench.upsert_scores([written]) == 1
    sent = fake.put_bodies[-1][1]["scores"]
    assert sent[-1]["effort"] == "" and sent[-1]["harness"] == ""
    before_hits = fake.hits[("GET", "scores")]
    assert next(row for row in bench.read_scores() if row.model_id == "wire-none").effort is None
    assert fake.hits[("GET", "scores")] == before_hits
    assert capsys.readouterr().err == ""


def test_import_is_idempotent_and_cache_survives_a_new_process(handoffkeep, tmp_path):
    data_home, fake = handoffkeep
    imported = tmp_path / "scores.toml"
    imported.write_text(
        'source = "AA-agent"\n'
        'metric = "agentic"\n'
        'effort = "max"\n'
        'harness = "codex"\n'
        'captured_at = "2026-09-07T00:00:00Z"\n'
        "[[scores]]\n"
        'model_id = "idempotent"\n'
        "score = 60.0\n",
        encoding="utf-8",
    )
    assert cli.main(["bench", "import", str(imported)]) == 0
    score_count = len(fake.scores)
    cache_db = bench.db_path()
    conn = sqlite3.connect(cache_db)
    try:
        cached_count = conn.execute("SELECT COUNT(*) FROM bench_cache_scores").fetchone()[0]
    finally:
        conn.close()
    assert cli.main(["bench", "import", str(imported)]) == 0
    assert len(fake.scores) == score_count
    conn = sqlite3.connect(cache_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM bench_cache_scores").fetchone()[0] == cached_count
    finally:
        conn.close()

    hits_before = fake.hits[("GET", "scores")]
    child_env = os.environ.copy()
    child_env["XDG_DATA_HOME"] = str(data_home)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from scopefuel import bench\n"
            "def no_network(*args, **kwargs):\n"
            "    raise AssertionError('network called')\n"
            "bench.request_json = no_network\n"
            "assert any(row.model_id == 'idempotent' for row in bench.read_scores())\n",
        ],
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 0, child.stderr
    assert fake.hits[("GET", "scores")] == hits_before


def test_push_local_preserves_source_rows_and_rejects_local_backend(tmp_path, monkeypatch, capsys):
    data_home = _set_backend(tmp_path, monkeypatch, "local")
    scores = [_score("push-one"), _score("push-two", score=60.0), _score("push-three", score=59.0)]
    assert bench.upsert_scores(scores) == 3
    for number in (1, 2):
        bench.add_rep(
            profile="codex-terra",
            model_id="gpt-5.6-terra",
            task_ref=f"PR#{number}",
            tier="T2",
            role="impl",
            effort="max",
            grade="S",
            rounds=0,
            blockers_found=0,
            completed=1,
        )
    before_scores = bench._read_local_scores()
    before_reps = bench._read_local_reps_for_push()
    assert cli.main(["bench", "push-local"]) == 2
    assert capsys.readouterr().err.splitlines() == ["error: bench push-local requires backend = handoffkeep"]

    _set_backend(tmp_path, monkeypatch, "handoffkeep")
    fake = FakeHandoffkeep()
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    assert cli.main(["bench", "push-local"]) == 0
    out = capsys.readouterr().out
    assert "3 score(s), 2 rep(s)" in out
    assert sum(len(body[scope]) for scope, body in fake.put_bodies) == 5
    remote_counts = (len(fake.scores), len(fake.reps))
    assert cli.main(["bench", "push-local"]) == 0
    assert (len(fake.scores), len(fake.reps)) == remote_counts
    assert bench._read_local_scores() == before_scores
    assert bench._read_local_reps_for_push() == before_reps
    assert data_home / "scopefuel" / "bench.db"


def test_grade_set_requires_deviation_and_reports_drift(handoffkeep, capsys):
    _, fake = handoffkeep
    with pytest.raises(SystemExit) as exc:
        cli.main(["bench", "grades", "set", "--profile", "codex-terra-max", "--grade", "S"])
    assert exc.value.code != 0
    assert "--deviation-ref" in capsys.readouterr().err
    puts_before = fake.hits[("PUT", "grades")]
    for invalid in ("", "  "):
        with pytest.raises(bench.BenchError, match="deviation_ref"):
            bench.set_grade(profile="codex-terra-max", grade="S", deviation_ref=invalid)
    assert fake.hits[("PUT", "grades")] == puts_before

    fake.grades = [dict(GRADE_FIXTURE, grade="A+")]
    assert cli.main(["bench", "grades", "list"]) == 0
    assert "⚠ drift: codex-terra-max server=A+ table=S" in capsys.readouterr().out


def test_runtime_grades_drive_recommend_and_gate(handoffkeep, monkeypatch, capsys):
    _, fake = handoffkeep
    fake.grades = [dict(GRADE_FIXTURE, profile="kiro-sonnet", grade="S")]
    table = bench.runtime_grade_table()
    provider = _provider()
    s_output = recommend.recommend([provider], "S", bench_scores=bench.read_scores(), grade_table=table)
    a_output = recommend.recommend([provider], "A+", bench_scores=bench.read_scores(), grade_table=table)
    assert "kiro-sonnet" in s_output
    assert "kiro-sonnet" not in a_output

    static = recommend.gate_check([provider], "kiro-sonnet")
    remote = recommend.gate_check(
        [provider], "kiro-sonnet", bench_scores=bench.read_scores(), grade_table=table
    )
    assert static.grade == "A+" and remote.grade == "S"
    assert fake.hits[("GET", "scores")] >= 1 and fake.hits[("GET", "grades")] >= 1

    cache_db = bench.db_path()
    conn = sqlite3.connect(cache_db)
    try:
        conn.execute(
            "UPDATE bench_cache_meta SET fetched_at = ? WHERE scope IN ('scores', 'grades')",
            ("2000-01-01T00:00:00+00:00",),
        )
        conn.commit()
    finally:
        conn.close()
    fake.hits.clear()
    monkeypatch.setattr(cli, "registry", lambda: {"kiro": lambda: provider})
    assert cli.main(["gate", "--profile", "kiro-sonnet", "--no-cache"]) == 0
    assert fake.hits[("GET", "scores")] == 1
    assert fake.hits[("GET", "grades")] == 1

    fake.grades = [dict(GRADE_FIXTURE, profile="codex-terra", grade="S")]
    conn = sqlite3.connect(cache_db)
    try:
        conn.execute(
            "UPDATE bench_cache_meta SET fetched_at = ? WHERE scope = 'grades'",
            ("2000-01-01T00:00:00+00:00",),
        )
        conn.commit()
    finally:
        conn.close()
    fallback = bench.runtime_grade_table()
    assert fallback is recommend.GRADE_TABLE
    assert capsys.readouterr().err.splitlines() == [
        "warning: handoffkeep bench grades failed boundary validation; using code grade table"
    ]
