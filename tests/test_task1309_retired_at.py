"""task #1309 — the push-catalog preflight validates destination retired_at.

handoffkeep decodes ``BenchCatalogEntry.RetiredAt`` into a Go ``*time.Time``:
the server emits either null (a live row) or a strict RFC3339 timestamp with a
zone. The exact destination reader behind ``push_catalog``
(``bench._catalog_entry_exact``) is the one place that reads those rows, and it
must treat every other value as a row it cannot vouch for: a non-string, an
empty string, or a string that is not that timestamp refuses the whole push
with the named error ``bench_catalog_unreadable`` before any PUT, rather than
silently decoding it as live or retired. This is the S1-R3 follow-up from the
1298 verification, which recorded that the reader mapped ``123`` to None and
accepted any non-empty string as retired without a syntax check.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from scopefuel import bench

# Server wire forms the Go time.Time marshal emits — RFC3339 with a zone.
RET_Z = "2026-09-24T00:00:00Z"
RET_OFFSET = "2026-09-24T09:30:00+09:00"
RET_FRACTIONAL = "2026-09-24T00:00:00.123456Z"

# Forms the server never emits; the reader must refuse each, not guess. These
# cover the tester's counterexamples (a numeric retired_at, any non-empty
# string) plus the neighbouring malformed shapes a hostile wire could carry.
BAD_RETIRED_AT = [
    pytest.param(123, id="int"),
    pytest.param(1.5, id="float"),
    pytest.param(True, id="bool"),
    pytest.param([RET_Z], id="list"),
    pytest.param({"at": RET_Z}, id="object"),
    pytest.param("", id="empty-string"),
    pytest.param("not-a-timestamp", id="garbage"),
    pytest.param("2026-09-24", id="bare-date"),
    pytest.param("2026-09-24 00:00:00Z", id="space-separator"),
    pytest.param("2026-09-24T00:00:00", id="no-zone"),
    pytest.param("2026-13-40T00:00:00Z", id="impossible-date"),
]


def _fake_transport(monkeypatch, destination_rows: list[dict]) -> list:
    """The real push_catalog path with the fake transport at request_json.

    GET serves the destination catalog rows verbatim; a PUT is recorded and
    answered as landed. Nothing here reaches a real server.
    """
    backend = bench.BenchBackend(
        name=bench.BENCH_BACKEND_HANDOFFKEEP,
        cache_ttl_s=1,
        url="https://hk.invalid",
        token="fake-only",
        endpoint_id="fake",
    )
    monkeypatch.setattr(bench, "bench_backend", lambda **kw: backend)
    monkeypatch.setattr(bench, "_commit_catalog_cache", lambda **kw: None)
    calls: list = []

    def transport(url, **kw):
        method = kw.get("method", "GET")
        calls.append((method, kw.get("body")))
        if method == "PUT":
            return {"upserted": len(kw["body"]["catalog"])}
        return {"catalog": [dict(r) for r in destination_rows]}

    monkeypatch.setattr(bench, "request_json", transport)
    return calls


def _payload(tmp_path, name: str, rows: list[dict]) -> pathlib.Path:
    path = tmp_path / name
    path.write_text(json.dumps({"catalog": rows}), encoding="utf-8")
    return path


def _dest_row(profile: str, effort: str, grade: str, retired_at: object) -> dict:
    return {"profile": profile, "effort": effort, "grade": grade, "retired_at": retired_at}


def _write_row(profile: str, effort: str, grade: str) -> dict:
    return {
        "profile": profile,
        "effort": effort,
        "model_id": "kimi-k3",
        "pool": "test",
        "grade": grade,
        "decided_by": "operator:test",
    }


def _methods(calls: list) -> list:
    return [method for method, _ in calls]


# ---------------------------------------------------------------------------
# AC1 — each bad retired_at form refuses the push before any PUT.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", BAD_RETIRED_AT)
def test_bad_retired_at_refuses_the_push_before_any_put(tmp_path, monkeypatch, isolated_cache, bad):
    """A destination row whose retired_at is neither null nor a server
    timestamp is bench_catalog_unreadable, and no PUT leaves."""
    destination = [_dest_row("p", "high", "C", bad)]
    calls = _fake_transport(monkeypatch, destination)
    payload = _payload(tmp_path, "c.json", [_write_row("p", "low", "S")])
    with pytest.raises(bench.BenchError, match="bench_catalog_unreadable"):
        bench.push_catalog(payload)
    assert _methods(calls) == ["GET"], f"a request beyond the read left for retired_at={bad!r}"


# ---------------------------------------------------------------------------
# AC1 — null and valid timestamps keep behaving as today.
# ---------------------------------------------------------------------------


def test_null_retired_at_is_a_live_row_and_counts(tmp_path, monkeypatch, isolated_cache):
    """Destination p@high C is live: raising p@low to S leaves high C below
    it, so the server refuses and the preflight must refuse too."""
    destination = [_dest_row("p", "high", "C", None)]
    calls = _fake_transport(monkeypatch, destination)
    payload = _payload(tmp_path, "c.json", [_write_row("p", "low", "S")])
    with pytest.raises(bench.BenchError, match="bench_catalog_not_monotonic"):
        bench.push_catalog(payload)
    assert "PUT" not in _methods(calls)


def test_absent_retired_at_is_a_live_row_and_counts(tmp_path, monkeypatch, isolated_cache):
    """A row that omits retired_at entirely is live, same as an explicit null."""
    destination = [{"profile": "p", "effort": "high", "grade": "C"}]
    calls = _fake_transport(monkeypatch, destination)
    payload = _payload(tmp_path, "c.json", [_write_row("p", "low", "S")])
    with pytest.raises(bench.BenchError, match="bench_catalog_not_monotonic"):
        bench.push_catalog(payload)
    assert "PUT" not in _methods(calls)


@pytest.mark.parametrize("ret", [RET_Z, RET_OFFSET, RET_FRACTIONAL])
def test_valid_timestamp_is_retired_and_ignored(tmp_path, monkeypatch, isolated_cache, ret):
    """Destination p@high C retired: the higher rung never constrains the
    ladder, so raising p@low to S is accepted and the PUT lands."""
    destination = [_dest_row("p", "high", "C", ret)]
    calls = _fake_transport(monkeypatch, destination)
    payload = _payload(tmp_path, "c.json", [_write_row("p", "low", "S")])
    assert bench.push_catalog(payload) == 1
    assert _methods(calls).count("PUT") == 1


@pytest.mark.parametrize("ret", [RET_Z, RET_OFFSET, RET_FRACTIONAL])
def test_valid_timestamp_is_kept_verbatim(ret):
    """The reader is a projection: a valid server timestamp is preserved
    byte-for-byte, never rewritten or normalized."""
    entry = bench._catalog_entry_exact(_dest_row("p", "high", "C", ret))
    assert entry.retired_at == ret


# ---------------------------------------------------------------------------
# AC2 — the invariant, as a sentence.
# ---------------------------------------------------------------------------


def test_invariant_malformed_retired_at_never_reaches_the_ladder(tmp_path, monkeypatch, isolated_cache):
    """A destination row whose retired_at is not null and not a server
    timestamp never reaches the merged ladder: the preflight refuses the whole
    push as bench_catalog_unreadable instead of decoding the row live or
    retired. A live decode would let a malformed p@high C constrain the
    ladder; a retired decode would let it hide."""
    for index, bad in enumerate((123, True, [], {}, "", "not-a-timestamp", "2026-09-24")):
        destination = [_dest_row("p", "high", "C", bad)]
        calls = _fake_transport(monkeypatch, destination)
        payload = _payload(tmp_path, f"c-{index}.json", [_write_row("p", "low", "S")])
        with pytest.raises(bench.BenchError, match="bench_catalog_unreadable"):
            bench.push_catalog(payload)
        assert "PUT" not in _methods(calls), f"retired_at={bad!r} reached the wire"
