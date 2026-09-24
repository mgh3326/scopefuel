"""quota v2 contract r3 — every corpus row (hk review/2026-09-24/578-contract-r3.1).

The corpus is the oracle: expected values were written by hand from the
contract text. This module only expands rows mechanically (tests/quota_v2_corpus.py)
and compares. No provider is called: the claude transport is stubbed.
"""

from __future__ import annotations

import copy
import datetime as dt
import itertools
import json
import sys
import types
from collections import Counter

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import quota_v2_corpus as corpus  # noqa: E402

from scopefuel import cache, quota_v2  # noqa: E402
from scopefuel.http import HttpError  # noqa: E402
from scopefuel.model import ProviderResult  # noqa: E402
from scopefuel.providers import claude as claude_mod  # noqa: E402
from scopefuel.quota_v2_contract import Attempt, valid_post, valid_stored  # noqa: E402
from scopefuel.quota_v2_eval import evaluate  # noqa: E402

EVALUATE = {row["id"]: row for row in corpus.CORPUS["evaluate"]}
ADAPTER = {row["id"]: row for row in corpus.CORPUS["adapter"]}
RECORDER = {row["id"]: row for row in corpus.CORPUS["recorder"]}


def run_evaluate(case: dict, observations: list[dict]):
    return evaluate(
        corpus.snapshot(case, observations),
        corpus.profile(case),
        now=corpus.now(case),
        clock=corpus.clock(case),
        support_list=corpus.support_list(case),
        definitions=corpus.CORPUS_DEFINITIONS,
    )


def summary(result) -> tuple:
    return (
        result.code,
        result.ok,
        result.reason,
        tuple(sorted(result.selected)),
        tuple(sorted(result.excluded.items())),
    )


@pytest.mark.parametrize("row_id", sorted(EVALUATE))
def test_evaluate_row(row_id):
    case = EVALUATE[row_id]
    observations = [corpus.observation(case, spec) for spec in case["obs"]]
    result = run_evaluate(case, observations)
    expect = case["expect"]
    assert (result.code, result.ok) == (expect["code"], expect["ok"])
    if "reason" in expect:
        assert result.reason == expect["reason"]
    if "selected" in expect:
        assert Counter(result.selected) == Counter(expect["selected"])
    for key, count in (expect.get("excluded") or {}).items():
        assert result.excluded.get(key) == count, key
    # PERM: every input order gives the same code, ok, reason, selection and exclusions.
    baseline = summary(result)
    for order in itertools.permutations(observations):
        assert summary(run_evaluate(case, list(order))) == baseline


# ------------------------------------------------------------------ adapter (claude, typed outcome)


def fake_transport(inp: dict, body: dict):
    def request_json(url, *, headers=None, status_out=None, **_kwargs):
        if "exception" in inp:
            raise (
                TimeoutError("timed out") if inp["exception"] == "timeout" else OSError("connection refused")
            )
        status = inp.get("http", 200)
        if status_out is not None:
            status_out.append(status)
        if status == 200:
            if "body_text" in inp:
                return json.loads(inp["body_text"])
            return body
        if 200 < status < 300:
            return {}
        raise HttpError(status, "error")

    return request_json


def patched_body(inp: dict, fixture_json) -> dict:
    body = fixture_json("claude_usage")
    patch = dict(inp.get("patch", {}))
    appended = patch.pop("append_limits_copy_of", None)
    for path, value in patch.items():
        cur, parts = body, path.replace("]", "").replace("[", ".").split(".")
        for part in parts[:-1]:
            cur = cur[int(part)] if part.isdigit() else cur[part]
        cur[int(parts[-1]) if parts[-1].isdigit() else parts[-1]] = value
    if appended is not None:
        body["limits"].append(copy.deepcopy(body["limits"][appended]))
    return body


IDENTITY = quota_v2.Identity("claude", "acct_7k2m9q4x", "unknown", 2, "node-a", "slot-a", 0.0, 4e9)


@pytest.mark.parametrize("row_id", sorted(k for k, v in ADAPTER.items() if "observations" not in v["expect"]))
def test_adapter_row(row_id, monkeypatch, fixture_json):
    case = ADAPTER[row_id]
    inp, expect = case["input"], case["expect"]
    if "typed_outcome" in inp and inp["typed_outcome"] is None:
        result = ProviderResult(id="claude", warning=inp.get("warning"), last_error=inp.get("last_error"))
    else:
        monkeypatch.setattr(
            claude_mod, "_load_oauth", lambda: ({"accessToken": "fixture", "subscriptionType": "max"}, "file")
        )
        monkeypatch.setattr(claude_mod, "request_json", fake_transport(inp, patched_body(inp, fixture_json)))
        result = claude_mod.fetch()
        result.warning = inp.get("warning", result.warning)
        result.last_error = inp.get("last_error", result.last_error)
    obs = quota_v2.observation_from_attempt(quota_v2.attempt_of(result), IDENTITY, measured_at=1790211600.0)
    assert valid_post(obs)
    assert obs["status"] == expect["status"]
    assert obs["error_ref"] == expect["error_ref"]
    assert [b["limit_id"] for b in obs["buckets"]] == expect["limit_ids"]
    assert obs["unshared_limit_count"] == expect["unshared_limit_count"]
    if "labels" in expect:
        assert [b["label"] for b in obs["buckets"]] == expect["labels"]


def test_adapter_does_not_change_legacy_output(monkeypatch, fixture_json):
    """The typed attempt rides along; the legacy result (--json view) is the same as before."""
    monkeypatch.setattr(
        claude_mod, "_load_oauth", lambda: ({"accessToken": "fixture", "subscriptionType": "max"}, "file")
    )
    monkeypatch.setattr(
        claude_mod, "request_json", fake_transport({"http": 200}, fixture_json("claude_usage"))
    )
    result = claude_mod.fetch()
    view = result.as_dict()
    assert "v2_attempt" not in json.dumps(view)
    assert [b["label"] for b in view["buckets"]] == ["5h", "7d all", "7d Fable"]


# ------------------------------------------------------------------ wire (POST / stored forms)


WIRE = json.loads((corpus.FIXTURES / "quota_v2_wire_r3.json").read_text())


def test_wire_fixture_is_the_mechanical_expansion():
    """Go reads the same file; it must equal the corpus expansion."""
    assert corpus.expanded_wire() == WIRE


@pytest.mark.parametrize("row", WIRE, ids=[r["id"] for r in WIRE])
def test_wire_row(row):
    check = valid_post if row["form"] == "post" else valid_stored
    assert ("accept" if check(row["envelope"]) else "reject") == row["expect"]


PARITY = json.loads((corpus.FIXTURES / "quota_v2_parity_r3.json").read_text())


@pytest.mark.parametrize("row", PARITY, ids=[r["id"] for r in PARITY])
def test_parity_row(row):
    """§7.4: edge envelopes (time text, JSON types, absent keys) the Go hub also checks."""
    check = valid_post if row["form"] == "post" else valid_stored
    assert ("accept" if check(row["envelope"]) else "reject") == row["expect"], row["note"]


# panewire testdata carries byte-identical copies; the Go test pins the same digests.
SHARED_FIXTURE_SHA256 = {
    "quota_v2_contract_r3.json": "904f80ffebd941b87021c65a25e3bdfc99b45990f031b7c93decd4d4a7c35490",
    "quota_v2_wire_r3.json": "efdbf028a29fa3dc64a637fbe925aac5c3c389a79a08fe9b60fe52875f1ea02c",
    "quota_v2_parity_r3.json": "5c21150ce5d755456c83c93b17feaac8278bc2d59dbc04b969754a1b5d010e74",
}


def test_shared_fixtures_are_pinned():
    import hashlib

    for name, digest in SHARED_FIXTURE_SHA256.items():
        assert hashlib.sha256((corpus.FIXTURES / name).read_bytes()).hexdigest() == digest, name


# ------------------------------------------------------------------ recorder (§5.7)


@pytest.fixture
def slot_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return quota_v2.slot_locator("claude")


def mirror(
    slot: str, valid_from: float, valid_until: float, account: str = "acct_7k2m9q4x", revision: int = 2
) -> None:
    quota_v2.import_bindings(
        {
            "schema": quota_v2.BINDINGS_SCHEMA,
            "machine_id": "node-a",
            "bindings": [
                {
                    "account_ref": account,
                    "provider": "claude",
                    "machine_id": "node-a",
                    "local_slot_ref": slot,
                    "entitlement_ref": "unknown",
                    "identity_basis": "operator_attested",
                    "binding_revision": revision,
                    "verified_at": corpus.at(valid_from),
                    "valid_until": corpus.at(valid_until),
                    "verifier_receipt": None,
                    "label": None,
                }
            ],
        }
    )


def claude_result(outcome: str | None = None) -> ProviderResult:
    if outcome == "rate_limited":
        return ProviderResult(
            id="claude",
            error="HTTP 429",
            error_kind="rate_limited",
            v2_attempt=Attempt("rate_limited", "rate_limited:http_429"),
        )
    buckets = [
        {
            "limit_id": "claude.five_hour",
            "label": "5h",
            "scope": {"kind": "account", "ref": None},
            "horizon": "now",
            "window": "5h",
            "window_instance": "unknown",
            "used_pct": 10.0,
            "reset_at": corpus.at(10800),
            "observed_at": None,
        },
        {
            "limit_id": "claude.seven_day",
            "label": "7d all",
            "scope": {"kind": "account", "ref": None},
            "horizon": "week",
            "window": "7d",
            "window_instance": "unknown",
            "used_pct": 20.0,
            "reset_at": corpus.at(345600),
            "observed_at": None,
        },
    ]
    return ProviderResult(id="claude", v2_attempt=Attempt("success", None, buckets, 1))


def epoch(offset: float) -> float:
    return (corpus.T0 + dt.timedelta(seconds=offset)).timestamp()


@pytest.mark.parametrize("row_id", sorted(k for k, v in RECORDER.items() if "start" in v["fetch"]))
def test_recorder_row(row_id, slot_env):
    case = RECORDER[row_id]
    binding, fetch, expect = case["binding"], case["fetch"], case["expect"]
    mirror(slot_env, binding["valid_from"], binding["valid_until"])
    start = epoch(fetch["start"])
    identities = quota_v2.capture_identities(["claude"], start)
    if "rebind_at" in binding:
        mirror(slot_env, binding["valid_from"], binding["valid_until"], account="acct_p3n8w5r1", revision=3)
    quota_v2.record_attempts(
        {"claude": (claude_result(fetch.get("outcome")), epoch(fetch["complete"]))},
        started_at=start,
        identities=identities,
    )
    rows = quota_v2.account_observations("claude", "acct_7k2m9q4x")
    assert len(rows) == expect["recorded"]
    if "measured_at" in expect:
        assert rows[0]["measured_at"] == corpus.at(expect["measured_at"])
    if "status" in expect:
        assert rows[0]["status"] == expect["status"]


@pytest.mark.parametrize("which", ["AD20", "AD21", "RC05", "RC06"])
def test_cache_hit_and_backoff_are_not_attempts(which, slot_env):
    mirror(slot_env, -3600, 86400)
    start = epoch(500)
    cache.collect({"claude": claude_result}, ["claude"], now=start)
    before = len(quota_v2.account_observations("claude", "acct_7k2m9q4x"))
    assert before == 1
    if which in ("AD21", "RC06"):
        state = {
            "schema": cache.BACKOFF_SCHEMA,
            "pools": {"claude": {"consecutive": 1, "next_allowed_at": start + 900}},
        }
        cache.backoff_path().write_text(json.dumps(state))
        cache.collect({"claude": claude_result}, ["claude"], now=start + 500, use_cache=False)
    else:
        cache.collect({"claude": claude_result}, ["claude"], now=start + 60)
    assert len(quota_v2.account_observations("claude", "acct_7k2m9q4x")) == before


def test_collect_stamps_completion_not_start(slot_env, monkeypatch):
    """§5.1: measured_at is when the response completed (start + elapsed)."""
    mirror(slot_env, -3600, 86400)
    ticks = iter([100.0, 103.5])
    monkeypatch.setattr(
        cache, "time", types.SimpleNamespace(time=__import__("time").time, monotonic=lambda: next(ticks))
    )
    cache.collect({"claude": claude_result}, ["claude"], now=epoch(500))
    rows = quota_v2.account_observations("claude", "acct_7k2m9q4x")
    assert [r["measured_at"] for r in rows] == [corpus.at(503.5)]


def test_collect_stamps_completion_after_a_worker_queue_wait(slot_env, monkeypatch):
    """§5.1: a fetch that waited for a free worker is stamped when it completed, not start + own duration."""
    mirror(slot_env, -3600, 86400)
    ticks = iter([1.0, 2.0, 3.0])  # collect begins, the first fetch ends, the claude fetch ends
    monkeypatch.setattr(
        cache, "time", types.SimpleNamespace(time=__import__("time").time, monotonic=lambda: next(ticks))
    )
    monkeypatch.setattr(cache, "MAX_FETCH_WORKERS", 1)
    cache.collect(
        {"other": lambda: ProviderResult(id="other"), "claude": claude_result},
        ["other", "claude"],
        now=epoch(500),
    )
    rows = quota_v2.account_observations("claude", "acct_7k2m9q4x")
    assert [r["measured_at"] for r in rows] == [corpus.at(502)]


def test_collect_does_not_record_when_the_binding_expires_during_the_fetch(slot_env, monkeypatch):
    """RC03 through the real collect path: start inside the window, completion after it."""
    mirror(slot_env, -60, 502)
    ticks = iter([100.0, 103.0])
    monkeypatch.setattr(
        cache, "time", types.SimpleNamespace(time=__import__("time").time, monotonic=lambda: next(ticks))
    )
    cache.collect({"claude": claude_result}, ["claude"], now=epoch(500))
    assert quota_v2.account_observations("claude", "acct_7k2m9q4x") == []


# ------------------------------------------------------------------ contract text without a corpus row
# Mutation testing found these rules had no corpus row. The expected values
# come by hand from the contract text; they are candidates for the next corpus revision.


def test_k1_overlapping_later_window_is_an_instance_conflict():
    """§6 K1: E_new > E_old but S_new < E_old (the windows overlap) → CONFLICT/INSTANCE."""
    case = {"provider": "ctest"}
    observations = [
        corpus.observation(case, {"id": "s1", "t": 500, "vals": "ctest.std"}),
        corpus.observation(
            case,
            {"id": "s2", "t": 560, "vals": {"ctest.fixed_5h": [10, 14400], "ctest.fixed_7d": [20, 345600]}},
        ),
    ]
    result = run_evaluate(case, observations)
    assert (result.code, result.reason, result.ok) == ("CONFLICT", "INSTANCE", False)


@pytest.mark.parametrize("form", ["post", "stored"])
def test_every_envelope_field_is_required(form):
    """§2: all fields are required, the nullable ones included (absent ≠ null)."""
    check = valid_post if form == "post" else valid_stored
    base = corpus.wire_envelope({"patch": {}})
    assert check(base)
    for key in base:
        env = copy.deepcopy(base)
        del env[key]
        assert not check(env), key
    for key in base["buckets"][0]:
        env = copy.deepcopy(base)
        del env["buckets"][0][key]
        assert not check(env), f"buckets[0].{key}"


def test_recorder_needs_the_binding_valid_at_fetch_start(slot_env):
    """§5.7: the binding must be valid when the fetch started, not only at completion."""
    mirror(slot_env, 500, 86400)
    identities = quota_v2.capture_identities(["claude"], epoch(600))
    quota_v2.record_attempts(
        {"claude": (claude_result(), epoch(610))}, started_at=epoch(400), identities=identities
    )
    assert quota_v2.account_observations("claude", "acct_7k2m9q4x") == []


def test_recorder_skips_a_backoff_result(slot_env):
    """A backoff placeholder is not an attempt (RC06), whoever calls the recorder."""
    mirror(slot_env, -3600, 86400)
    identities = quota_v2.capture_identities(["claude"], epoch(500))
    result = claude_result("rate_limited")
    result.backoff_until = epoch(1400)
    quota_v2.record_attempts({"claude": (result, epoch(501))}, started_at=epoch(500), identities=identities)
    assert quota_v2.account_observations("claude", "acct_7k2m9q4x") == []


def test_a_lone_surrogate_label_is_excluded_not_raised():
    """§2 + X2: an unencodable label makes that one observation invalid; evaluate still answers."""
    case = {"obs": []}
    good = corpus.observation(case, {"id": "s1", "t": 500, "vals": "claude.std"})
    bad = corpus.observation(case, {"id": "s2", "t": 560, "vals": "claude.std"})
    bad["buckets"][0]["label"] = "5h\ud800"
    result = run_evaluate(case, [good, bad])
    assert result.excluded.get("invalid") == 1
    assert result.selected == ("s1",)


@pytest.mark.parametrize(
    "identity",
    [
        {"account": "not-an-acct"},
        {"revision": 0},
        {"slot": "Slot A"},
        {"machine": "node_a!"},
        {"entitlement": ""},
    ],
)
def test_a_malformed_identity_is_identity_unknown(identity):
    """§6 I: a malformed execution identity is IDENTITY_UNKNOWN, not a usable identity."""
    case = {"identity": identity}
    observations = [corpus.observation(case, {"id": "s1", "t": 500, "vals": "claude.std"})]
    assert run_evaluate(case, observations).code == "IDENTITY_UNKNOWN"


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-24T24:00:00Z",
        "2026-09-24T24:00:00.000000+09:00",
        "2026-01-00T24:00:00Z",  # day 00: Python 3.14's fromisoformat rolls it into 2026-01-01
        "2026-09-24T23:59:60Z",
        "2026-02-29T00:00:00Z",
        "9999-12-31T23:59:59-01:00",
    ],
)
def test_parse_time_rejects_what_some_interpreters_accept(text):
    """§2 times: the accepted set is fixed by the contract text, not by the Python version."""
    from scopefuel.quota_v2_contract import parse_time

    assert parse_time(text) is None


def test_parse_time_keeps_offsets_and_fractions():
    from scopefuel.quota_v2_contract import parse_time

    assert parse_time("2026-09-24T10:08:20.1234567+09:00") == dt.datetime(
        2026, 9, 24, 1, 8, 20, 123456, tzinfo=dt.UTC
    )
    assert parse_time("0001-01-01T00:00:00Z") == dt.datetime(1, 1, 1, tzinfo=dt.UTC)
