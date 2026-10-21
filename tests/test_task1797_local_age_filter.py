"""hk 1797 (b953 follow-up) — the local in-flight set counts only recent rows.

hk 1787 counted every live ``quota_pool`` row, but the arbiter table keeps
rows that were never released (2026-10-21 read-only measurement: codex 95
live rows with 2 started in 8h on mbp, claude 72/2, devin 103/3). With
inflight_reserve_pct set, every gate on such a host would deny on
``inflight_headroom`` forever. The local set now applies the same rule as
remote docs: ``started_at`` must parse as an aware ISO-8601 time
(``_parse_ts``) and sit within ``INFLIGHT_MAX_AGE_S`` of now — rows that
fail are not counted and not published, and ``inflight show`` reports them
with reason ``aged`` or ``invalid_started_at``.

Mutant invariant (AC4): removing the local age check goes RED on the AC1
tests — every dropped-row case would be counted and published.
"""

from __future__ import annotations

import datetime as dt
import json

from test_task1787_inflight_sum import (
    EPOCH,
    NOW,
    TODAY,
    _codex,
    _doc,
    _enable_hk,
    _gate_with,
    _host,
    _live_row,
    _remote_job,
    _row,
    _spy_arbiter,
)

from scopefuel import cli, inflight, policy
from scopefuel.recommend import gate_check


def _inflight_puts(hk) -> list[tuple[str, str]]:
    return [c for c in hk.calls if c[0] == "PUT" and "/inflight/" in c[1]]


def _published_jobs(hk) -> list[dict]:
    puts = _inflight_puts(hk)
    assert len(puts) == 1
    return json.loads(hk.docs[puts[0][1]]["body"])["jobs"]


def _dropped(pool: str = "codex") -> list[tuple[str, str]]:
    read = inflight.local_jobs(pool, now=EPOCH)
    assert read is not None
    return [(view.job_id, view.reason) for view in read.dropped]


# ------------------------------------------------------------------ AC1


def test_aged_local_rows_not_counted_or_published(monkeypatch):
    """Rows past INFLIGHT_MAX_AGE_S — in either direction — drop as ``aged``."""
    future = _row("future")
    future["started_at"] = (NOW + dt.timedelta(hours=9)).isoformat()
    result, _, hk = _gate_with(monkeypatch, [_row("old", hours_ago=9.0), future, _row("new")], [])
    assert result.ok and result.inflight == "inflight=1"
    assert [j["job_id"] for j in _published_jobs(hk)] == ["new"]
    assert _dropped() == [("old", "aged"), ("future", "aged")]


def test_naive_started_at_not_counted_or_published(monkeypatch):
    naive = _row("naive")
    naive["started_at"] = (NOW - dt.timedelta(hours=1)).replace(tzinfo=None).isoformat()
    result, _, hk = _gate_with(monkeypatch, [naive, _row("new")], [])
    assert result.ok and result.inflight == "inflight=1"
    assert [j["job_id"] for j in _published_jobs(hk)] == ["new"]
    assert _dropped() == [("naive", "invalid_started_at")]


def test_unparseable_started_at_not_counted_or_published(monkeypatch):
    bad = _row("bad")
    bad["started_at"] = "not-a-date"
    nonstr = _row("nonstr")
    nonstr["started_at"] = 12345
    result, _, hk = _gate_with(monkeypatch, [bad, nonstr, _row("new")], [])
    assert result.ok and result.inflight == "inflight=1"
    assert [j["job_id"] for j in _published_jobs(hk)] == ["new"]
    assert _dropped() == [("bad", "invalid_started_at"), ("nonstr", "invalid_started_at")]


def test_missing_started_at_not_counted_or_published(monkeypatch):
    gone = _row("gone")
    del gone["started_at"]
    result, _, hk = _gate_with(monkeypatch, [gone, _row("new")], [])
    assert result.ok and result.inflight == "inflight=1"
    assert [j["job_id"] for j in _published_jobs(hk)] == ["new"]
    assert _dropped() == [("gone", "invalid_started_at")]


def test_boundary_age_counts_like_remote(monkeypatch):
    """Exactly INFLIGHT_MAX_AGE_S old counts — same ``>`` comparison as remote."""
    result, _, hk = _gate_with(monkeypatch, [_row("edge", hours_ago=8.0)], [])
    assert result.ok and result.inflight == "inflight=1"
    assert [j["job_id"] for j in _published_jobs(hk)] == ["edge"]
    assert _dropped() == []


# ------------------------------------------------------------------ AC2


def test_publish_payload_holds_only_kept_rows(monkeypatch):
    """The PUT body keeps the fixed key set and lists kept rows only."""
    result, _, hk = _gate_with(
        monkeypatch,
        [_row("a", hours_ago=0.5), _row("old", hours_ago=9.0)],
        [],
        host="mbp",
    )
    assert result.ok
    puts = _inflight_puts(hk)
    assert puts == [("PUT", "quota/codex/fp-account-aaaa1111/inflight/mbp")]
    body = json.loads(hk.docs[puts[0][1]]["body"])
    assert set(body) == {"schema", "pool", "account_fp", "host", "published_at", "jobs"}
    assert body["schema"] == inflight.SCHEMA and body["host"] == "mbp"
    assert [set(j) for j in body["jobs"]] == [{"job_id", "started_at"}]
    assert [j["job_id"] for j in body["jobs"]] == ["a"]
    assert body["jobs"][0]["started_at"] == _row("a", hours_ago=0.5)["started_at"]


def test_gate_n_sums_kept_local_plus_remote(monkeypatch):
    """n = kept local rows + counted remote jobs — aged locals add nothing."""
    docs = [_doc("oci", [_remote_job("j1"), _remote_job("j2")])]
    result, _, _ = _gate_with(monkeypatch, [_row("a"), _row("old", hours_ago=9.0)], docs)
    assert result.ok and result.inflight == "inflight=3"


def test_leaked_rows_do_not_deny(monkeypatch):
    """The measured failure mode: 95 unreleased rows must not trip headroom."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row(f"leak{i}", hours_ago=24.0) for i in range(95)])
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 10.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok and result.inflight == "inflight=0"


def test_local_filter_failures_still_fold_to_none(monkeypatch):
    """The read failure contract is unchanged — None, not an empty LocalRead."""
    _spy_arbiter(monkeypatch, fail=True)
    assert inflight.local_jobs("codex", now=EPOCH) is None


# ------------------------------------------------------------------ AC3: show


def test_show_prints_dropped_local_rows(monkeypatch, capsys):
    """``inflight show`` lists dropped local rows with reasons and the count."""
    _host(monkeypatch, "mbp")
    live = dt.datetime.now(dt.UTC)
    _spy_arbiter(
        monkeypatch,
        rows=[
            _live_row("a"),
            {**_live_row("old"), "started_at": (live - dt.timedelta(hours=9)).isoformat()},
            {**_live_row("bad"), "started_at": "not-a-date"},
            {**_live_row("gone"), "started_at": None},
        ],
    )
    hk = _enable_hk(monkeypatch)
    rc = cli.main(["inflight", "show", "--pool", "codex"])
    out = capsys.readouterr()
    assert rc == 0
    assert "local: 1 live job(s) (3 dropped)" in out.out
    assert "old dropped: aged" in out.out
    assert "bad dropped: invalid_started_at" in out.out
    assert "gone dropped: invalid_started_at" in out.out
    assert [c for c in hk.calls if c[0] == "PUT"] == []


def test_show_json_reports_dropped_local_rows(monkeypatch, capsys):
    _host(monkeypatch, "mbp")
    live = dt.datetime.now(dt.UTC)
    _spy_arbiter(
        monkeypatch,
        rows=[
            _live_row("a"),
            {**_live_row("old"), "started_at": (live - dt.timedelta(hours=9)).isoformat()},
        ],
    )
    _enable_hk(monkeypatch)
    rc = cli.main(["inflight", "show", "--pool", "codex", "--json"])
    out = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out.out)
    assert payload["schema"] == "scopefuel.inflight-show.v1"
    assert payload["local_count"] == 1
    assert payload["local_dropped"] == [{"job_id": "old", "counted": False, "reason": "aged"}]


def test_show_local_unavailable_has_no_dropped_list(monkeypatch, capsys):
    _host(monkeypatch, "mbp")
    _spy_arbiter(monkeypatch, fail=True)
    _enable_hk(monkeypatch)
    rc = cli.main(["inflight", "show", "--pool", "codex", "--json"])
    out = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out.out)
    assert payload["local"] is None and payload["local_dropped"] is None
