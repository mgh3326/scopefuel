"""task #578 stage 1 — account-scoped quota v2: evaluator, local store, shadow.

Everything runs on fixtures and fixed clocks. No usage API is called: fetchers
are stubs, and the cache/state directory is conftest's tmp path.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import socket

import pytest

from scopefuel import bench, cache, cli, quota_v2, recommend
from scopefuel.model import Bucket, ProviderResult, Scope

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
ACCOUNT_A = "acct_7k2m9q4x"
ACCOUNT_B = "acct_p3n8w5r1"
T0 = dt.datetime(2026, 9, 24, 1, 0, 0, tzinfo=dt.UTC)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _claude(base: dt.datetime, five: float = 22.0, week: float = 41.0, opus: float = 56.0) -> ProviderResult:
    return ProviderResult(
        id="claude",
        plan="max",
        buckets=[
            Bucket("5h", "5h", five, (base + dt.timedelta(hours=3)).isoformat(), Scope("account"), "now"),
            Bucket(
                "7d all", "7d", week, (base + dt.timedelta(hours=96)).isoformat(), Scope("account"), "week"
            ),
            Bucket(
                "7d Opus",
                "7d",
                opus,
                (base + dt.timedelta(hours=96)).isoformat(),
                Scope("model", "Opus"),
                "week",
            ),
        ],
        source="oauth-usage-api",
        http_status=200,
        account_fp="fp-a",
    )


@pytest.fixture
def slot_env(tmp_path, monkeypatch):
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return quota_v2.slot_locator("claude")


def _binding(
    slot: str,
    account: str,
    revision: int,
    *,
    machine: str = "node-a",
    verified: dt.datetime = T0,
    hours: float = 24,
    label: str | None = None,
    provider: str = "claude",
) -> dict:
    return {
        "account_ref": account,
        "provider": provider,
        "machine_id": machine,
        "local_slot_ref": slot,
        "entitlement_ref": "unknown",
        "identity_basis": "operator_attested",
        "binding_revision": revision,
        "verified_at": _iso(verified),
        "valid_until": _iso(verified + dt.timedelta(hours=hours)),
        "verifier_receipt": None,
        "label": label,
    }


def _enroll(*bindings: dict, machine: str = "node-a") -> None:
    quota_v2.import_bindings(
        {"schema": quota_v2.BINDINGS_SCHEMA, "machine_id": machine, "bindings": list(bindings)}
    )


def _identity(account: str = ACCOUNT_A, revision: int = 1, machine: str = "node-a", label=None) -> dict:
    return {
        "provider": "claude",
        "account_ref": account,
        "entitlement_ref": "unknown",
        "binding_revision": revision,
        "machine_id": machine,
        "local_slot_ref": "slot-0000000000000000",
        "verified_at": _iso(T0 - dt.timedelta(hours=1)),
        "valid_until": _iso(T0 + dt.timedelta(days=1)),
        "label": label,
    }


def _obs(
    oid: str,
    measured: dt.datetime,
    *,
    account: str = ACCOUNT_A,
    status: str = "success",
    machine: str = "node-a",
    revision: int = 1,
    result: ProviderResult | None = None,
) -> dict:
    identity = quota_v2.Identity("claude", account, "unknown", revision, machine, "slot-x", 0.0, 1e12)
    source = result if result is not None else _claude(measured)
    if status not in ("success", "partial"):
        source = ProviderResult(
            id="claude",
            error="x",
            error_kind={
                "rate_limited": "rate_limited",
                "auth_error": "auth",
                "transport_error": "network",
                "parse_error": "unknown",
            }[status],
        )
    obs = quota_v2.observation_from_result(
        source, identity, measured_at=measured.timestamp(), observation_id=oid
    )
    assert obs["status"] == status
    return obs


def _snapshot(identity: dict | None, observations: list[dict]) -> dict:
    return {
        "schema": quota_v2.SNAPSHOT_SCHEMA,
        "identities": {"claude": identity},
        "observations": observations,
    }


def _legacy(result: ProviderResult, profile: str, now: dt.datetime) -> recommend.GateResult:
    return recommend.gate_check([result], profile, today=now.date(), now=now)


def _run_gate(monkeypatch, capsys, fetch, *extra: str) -> tuple[int, str, str]:
    monkeypatch.setattr(cli, "registry", lambda: {"claude": fetch})
    monkeypatch.setattr(bench, "read_scores", lambda: None)
    rc = cli.main(["gate", "-m", "opus", *extra])
    out = capsys.readouterr()
    return rc, out.out, out.err


def _shadow_records() -> list[dict]:
    path = quota_v2.shadow_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


# ---------------------------------------------------------------- AC1: one account, two nodes


def test_hub_contract_fixture_two_nodes_evaluates_like_legacy_gate():
    """The hub's GET response (panewire testdata/quota-v2-two-nodes.json, copied
    verbatim) feeds evaluate(); the decision equals the legacy gate on the same
    newest measurement, both 7d limits survive, no other account leaks in."""
    document = json.loads((FIXTURES / "quota_v2_two_nodes.json").read_text())
    assert document["schema"] == quota_v2.OBSERVATIONS_SCHEMA
    rows = document["observations"]
    assert {row["source_machine"] for row in rows} == {"node-a", "node-b"}
    assert all(quota_v2.validate_observation(row) for row in rows)
    for row in rows:
        limits = {(b["limit_id"], b["scope"]["kind"], b["window"]) for b in row["buckets"]}
        assert ("account:-:7d", "account", "7d") in limits
        assert ("model:opus:7d", "model", "7d") in limits

    newest = max(rows, key=lambda row: row["measured_at"])
    measured = dt.datetime.fromisoformat(newest["measured_at"].replace("Z", "+00:00"))
    now = measured + dt.timedelta(seconds=60)
    foreign = _obs("obs-foreign", measured, account=ACCOUNT_B, result=_claude(measured, 1, 1, 1))
    for machine, revision in (
        ("node-a", rows[0]["source_binding_revision"]),
        ("node-b", rows[1]["source_binding_revision"]),
    ):
        identity = _identity(ACCOUNT_A, revision, machine)
        v2 = quota_v2.evaluate(_snapshot(identity, [*rows, foreign]), "opus", now=now)
        legacy_result = ProviderResult(
            id="claude",
            buckets=quota_v2._buckets_from_v2(newest["buckets"]),
            fetched_at=measured.timestamp(),
            age_s=60.0,
        )
        legacy = _legacy(legacy_result, "opus", now)
        diff, delta = quota_v2.compare(legacy, 0 if legacy.ok else 3, v2)
        assert diff == [] and delta == 0.0
        assert v2.code == "OK" and v2.observation_ids == (newest["observation_id"],)
        assert v2.account_ref == ACCOUNT_A
        assert v2.excluded["other_account"] == 1  # the foreign account never became a candidate


def test_two_nodes_gate_cli_shadow_matches_and_merges_one_account(tmp_path, monkeypatch, capsys):
    """End to end through `scopefuel gate` on two simulated nodes of one account:
    each node records only its own fetch, the hub response is imported on the
    other node, and the shadow decision equals the real gate on both."""
    nodes = {}
    for machine, five in (("node-a", 20.0), ("node-b", 22.0)):
        home = tmp_path / machine
        monkeypatch.setenv("SCOPEFUEL_CACHE", str(home / "cache" / "snapshots.json"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
        slot = quota_v2.slot_locator("claude")
        now = dt.datetime.now(dt.UTC)
        _enroll(
            _binding(
                slot,
                ACCOUNT_A,
                10 if machine == "node-a" else 11,
                machine=machine,
                verified=now - dt.timedelta(minutes=5),
                label="회사" if machine == "node-a" else "개인",
            ),
            machine=machine,
        )
        rc, out, _err = _run_gate(
            monkeypatch, capsys, lambda five=five: _claude(dt.datetime.now(dt.UTC), five=five)
        )
        assert rc == 0 and "pool=claude" in out
        record = _shadow_records()[-1]
        assert record["match"] is True and record["diff"] == []
        assert record["v2"]["account_ref"] == ACCOUNT_A
        own = quota_v2.account_observations("claude", ACCOUNT_A)
        assert [o["source_machine"] for o in own] == [machine]
        nodes[machine] = (home, own)

    # Hub hand-off: node-a imports node-b's observation (as the hub serves it).
    home_a, own_a = nodes["node-a"]
    _home_b, own_b = nodes["node-b"]
    monkeypatch.setenv("SCOPEFUEL_CACHE", str(home_a / "cache" / "snapshots.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home_a / ".claude"))
    served = [dict(o, received_at=_iso(dt.datetime.now(dt.UTC))) for o in own_b]
    assert (
        quota_v2.import_observations(
            {
                "schema": quota_v2.OBSERVATIONS_SCHEMA,
                "provider": "claude",
                "account_ref": ACCOUNT_A,
                "observations": served,
            }
        )
        == 1
    )
    merged = quota_v2.account_observations("claude", ACCOUNT_A)
    assert sorted(o["source_machine"] for o in merged) == ["node-a", "node-b"]
    for obs in merged:  # account 7d and model 7d kept on both nodes' rows
        assert {"account:-:7d", "model:opus:7d", "account:-:5h"} <= {b["limit_id"] for b in obs["buckets"]}

    rc, _out, _err = _run_gate(monkeypatch, capsys, lambda: pytest.fail("TTL hit must not fetch"))
    assert rc == 0
    record = _shadow_records()[-1]
    assert record["match"] is True
    assert record["v2"]["observation_ids"] == [own_b[0]["observation_id"]]  # newest, not lowest
    assert record["used_pct_delta"] is not None
    assert own_a[0]["observation_id"] not in record["v2"]["observation_ids"]


# ---------------------------------------------------------------- AC2 mutants


def test_same_slot_rebind_a_to_b_never_lets_a_snapshot_pass_b(slot_env, monkeypatch, capsys):
    """(c) same slot A→B: A's cached value may still open the *legacy* gate
    (shadow leaves it alone) but must never become B's v2 evidence."""
    now = dt.datetime.now(dt.UTC)
    _enroll(_binding(slot_env, ACCOUNT_A, 1, verified=now - dt.timedelta(minutes=10)))
    rc, _out, _err = _run_gate(monkeypatch, capsys, lambda: _claude(dt.datetime.now(dt.UTC), five=5.0))
    assert rc == 0
    assert len(quota_v2.account_observations("claude", ACCOUNT_A)) == 1

    # Operator rebinds the same slot to B; the node mirrors it.
    _enroll(_binding(slot_env, ACCOUNT_B, 2, verified=dt.datetime.now(dt.UTC)))
    rc, _out, _err = _run_gate(monkeypatch, capsys, lambda: pytest.fail("TTL hit must not fetch"))
    assert rc == 0  # legacy decision unchanged (shadow only)
    record = _shadow_records()[-1]
    assert record["v2"]["account_ref"] == ACCOUNT_B
    assert record["v2"]["ok"] is False and record["v2"]["code"] == "NO_SAMPLE"
    assert record["diff"] and record["match"] is False  # the mixing risk is made visible
    assert quota_v2.account_observations("claude", ACCOUNT_B) == []  # A's cache hit not recorded as B

    # Pure evaluate: B identity + only A observations → never admitted.
    a_rows = quota_v2.account_observations("claude", ACCOUNT_A)
    v2 = quota_v2.evaluate(_snapshot(_identity(ACCOUNT_B, 2), a_rows), "opus", now=dt.datetime.now(dt.UTC))
    assert not v2.gate.ok and v2.code == "NO_SAMPLE" and v2.excluded["other_account"] == len(a_rows)


def test_binding_change_during_fetch_records_nothing(slot_env):
    before = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    _enroll(_binding(slot_env, ACCOUNT_A, 1, verified=before))
    identities = quota_v2.capture_identities(["claude"], dt.datetime.now(dt.UTC).timestamp())
    _enroll(_binding(slot_env, ACCOUNT_B, 2, verified=dt.datetime.now(dt.UTC)))  # rebound mid-fetch
    measured = dt.datetime.now(dt.UTC).timestamp()
    assert (
        quota_v2.record_fetch({"claude": _claude(T0)}, {}, measured_at=measured, identities=identities) == 0
    )
    assert quota_v2.account_observations("claude", ACCOUNT_A) == []
    assert quota_v2.account_observations("claude", ACCOUNT_B) == []


def test_value_measured_before_binding_began_is_not_recorded(slot_env):
    verified = dt.datetime.now(dt.UTC)
    _enroll(_binding(slot_env, ACCOUNT_A, 1, verified=verified))
    identities = quota_v2.capture_identities(["claude"], verified.timestamp() + 1)
    early = verified.timestamp() - 30
    assert quota_v2.record_fetch({"claude": _claude(T0)}, {}, measured_at=early, identities=identities) == 0


def test_cache_hit_is_never_a_new_observation(slot_env):
    """A TTL cache hit carries an old fetched_at; recording it would re-stamp or
    re-attribute an old value. Only real attempts reach the v2 store."""
    start = T0  # fixed clock far from wall time: a render-time stamp would differ by hours
    _enroll(_binding(slot_env, ACCOUNT_A, 1, verified=start - dt.timedelta(minutes=5)))
    calls = []

    def fetch():
        calls.append(1)
        return _claude(start)

    cache.collect({"claude": fetch}, ["claude"], now=start.timestamp())
    cache.collect({"claude": fetch}, ["claude"], now=start.timestamp() + 60)
    rows = quota_v2.account_observations("claude", ACCOUNT_A)
    assert len(calls) == 1 and len(rows) == 1
    assert rows[0]["measured_at"] == quota_v2._iso(start.timestamp())


def test_measured_at_is_fetch_time_not_evaluation_time():
    """measured_at, not render/receive time, ages the value: a 200s-old Claude
    value is past its 180s TTL no matter when it was received or rendered."""
    obs = _obs("obs-m", T0)
    obs["received_at"] = _iso(T0 + dt.timedelta(seconds=199))
    now = T0 + dt.timedelta(seconds=200)
    v2 = quota_v2.evaluate(_snapshot(_identity(), [obs]), "opus", now=now)
    assert v2.code == "STALE_EXPIRED" and not v2.gate.ok and v2.age_s == pytest.approx(200.0)
    fresh = quota_v2.evaluate(_snapshot(_identity(), [obs]), "opus", now=T0 + dt.timedelta(seconds=100))
    assert fresh.code == "OK" and fresh.measured_at == _iso(T0)


def test_shadow_never_changes_the_real_gate(slot_env, monkeypatch, capsys, tmp_path):
    """Same rc, stdout, stderr and --gate-output with and without enrollment,
    and when the v2 evaluator blows up."""
    scenarios = {
        "ok": lambda: _claude(dt.datetime.now(dt.UTC), five=10.0),
        "blocked": lambda: _claude(dt.datetime.now(dt.UTC), five=99.0, week=99.0),
        "unmeasurable": lambda: ProviderResult(id="claude", error="HTTP 500", error_kind="server"),
    }

    def run(fetch) -> tuple[int, str, str, dict]:
        cache.cache_path().unlink(missing_ok=True)
        output = tmp_path / "gate.json"
        rc, out, err = _run_gate(monkeypatch, capsys, fetch, "--no-cache", "--gate-output", str(output))
        record = json.loads(output.read_text())
        record.pop("generated_at")
        return rc, out, err, record

    baseline = {name: run(fetch) for name, fetch in scenarios.items()}
    assert baseline["ok"][0] == 0 and baseline["blocked"][0] == 3 and baseline["unmeasurable"][0] == 4

    _enroll(_binding(slot_env, ACCOUNT_A, 1, verified=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)))
    for name, fetch in scenarios.items():
        assert run(fetch) == baseline[name], name
    assert len(_shadow_records()) == len(scenarios)

    def boom(*_args, **_kwargs):
        raise RuntimeError("v2 evaluator failure")

    monkeypatch.setattr(quota_v2, "evaluate", boom)
    for name, fetch in scenarios.items():
        assert run(fetch) == baseline[name], name


def test_shadow_record_is_a_log_not_a_decision(slot_env, monkeypatch, capsys):
    """If shadow_gate's result were fed back (mutant), a v2 NO_SAMPLE would turn
    the legacy allow into a refusal. The gate must ignore what shadow returns."""
    _enroll(_binding(slot_env, ACCOUNT_B, 2, verified=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)))
    rc, out, _err = _run_gate(monkeypatch, capsys, lambda: _claude(dt.datetime.now(dt.UTC)), "--no-cache")
    record = _shadow_records()[-1]
    assert record["v2"]["ok"] is False  # binding began in the future → nothing recorded
    assert rc == 0 and "pool=claude" in out


# ---------------------------------------------------------------- AC3: evaluate is pure


def test_evaluate_touches_no_network_cache_or_fetcher(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network/cache access from evaluate()")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(cache, "collect", forbidden)
    monkeypatch.setattr(cache, "_load", forbidden)
    monkeypatch.setattr(bench, "read_scores", forbidden)
    monkeypatch.setattr(bench, "read_prices", forbidden)
    now = T0 + dt.timedelta(seconds=30)
    v2 = quota_v2.evaluate(_snapshot(_identity(), [_obs("obs-p", T0)]), "opus", now=now)
    assert v2.gate.ok and v2.code == "OK"


# ---------------------------------------------------------------- evaluator semantics


def test_stale_acceptance_matches_legacy_after_rate_limit():
    success = _obs("obs-s", T0)
    limited = _obs("obs-r", T0 + dt.timedelta(minutes=10), status="rate_limited")
    now = T0 + dt.timedelta(minutes=11)
    v2 = quota_v2.evaluate(_snapshot(_identity(), [success, limited]), "opus", now=now)
    stale = _claude(T0)
    stale.stale, stale.error_kind, stale.account_fp_match = True, "rate_limited", True
    stale.fetched_at, stale.age_s = T0.timestamp(), 660.0
    legacy = _legacy(stale, "opus", now)
    assert legacy.ok and legacy.stale_accepted
    assert v2.code == "STALE_ACCEPTED" and quota_v2.compare(legacy, 0, v2)[0] == []
    past_six_hours = T0 + dt.timedelta(hours=6, minutes=1)
    assert not quota_v2.evaluate(
        _snapshot(_identity(), [success, limited]), "opus", now=past_six_hours
    ).gate.ok


def test_auth_failure_blocks_only_its_own_slot():
    success = _obs("obs-s", T0, machine="node-b", revision=2)
    auth_a = _obs(
        "obs-auth", T0 + dt.timedelta(seconds=20), status="auth_error", machine="node-a", revision=1
    )
    now = T0 + dt.timedelta(seconds=30)
    mine = quota_v2.evaluate(
        _snapshot(_identity(machine="node-a", revision=1), [success, auth_a]), "opus", now=now
    )
    other = quota_v2.evaluate(
        _snapshot(_identity(machine="node-b", revision=2), [success, auth_a]), "opus", now=now
    )
    assert mine.code == "AUTH_BLOCKED" and not mine.gate.ok
    assert other.code == "OK" and other.gate.ok


def test_same_instant_conflicting_values_are_not_resolved_optimistically():
    low = _obs("obs-low", T0, result=_claude(T0, five=5.0), machine="node-a")
    high = _obs("obs-high", T0, result=_claude(T0, five=95.0), machine="node-b", revision=2)
    v2 = quota_v2.evaluate(_snapshot(_identity(), [low, high]), "opus", now=T0 + dt.timedelta(seconds=10))
    assert v2.code == "CONFLICT" and not v2.gate.ok


def test_identity_unknown_and_expired_binding():
    rows = [_obs("obs-1", T0)]
    now = T0 + dt.timedelta(seconds=10)
    assert quota_v2.evaluate(_snapshot(None, rows), "opus", now=now).code == "IDENTITY_UNKNOWN"
    expired = _identity()
    expired["valid_until"] = _iso(T0)
    assert quota_v2.evaluate(_snapshot(expired, rows), "opus", now=now).code == "IDENTITY_UNKNOWN"


def test_label_is_an_alias_not_a_key(slot_env):
    rows = [_obs("obs-1", T0)]
    now = T0 + dt.timedelta(seconds=10)
    work = quota_v2.evaluate(_snapshot(_identity(label="회사"), rows), "opus", now=now)
    personal = quota_v2.evaluate(_snapshot(_identity(label="개인"), rows), "opus", now=now)
    assert work.as_dict() == personal.as_dict()
    # A label never selects a binding: two bindings on one slot is ambiguous.
    _enroll(_binding(slot_env, ACCOUNT_A, 1, label="회사"), _binding(slot_env, ACCOUNT_B, 2, label="회사"))
    identity, reason = quota_v2.resolve_identity("claude", dt.datetime.now(dt.UTC).timestamp())
    assert identity is None and reason == "ambiguous"


def test_two_slots_of_one_provider_on_one_machine_stay_apart(tmp_path):
    slot_1 = quota_v2.slot_locator("claude", {"CLAUDE_CONFIG_DIR": str(tmp_path / "one"), "HOME": "/h"})
    slot_2 = quota_v2.slot_locator("claude", {"CLAUDE_CONFIG_DIR": str(tmp_path / "two"), "HOME": "/h"})
    assert slot_1 != slot_2
    _enroll(_binding(slot_1, ACCOUNT_A, 1), _binding(slot_2, ACCOUNT_B, 2))
    now = dt.datetime.now(dt.UTC).timestamp()
    first, _ = quota_v2.resolve_identity(
        "claude", now, env={"CLAUDE_CONFIG_DIR": str(tmp_path / "one"), "HOME": "/h"}
    )
    second, _ = quota_v2.resolve_identity(
        "claude", now, env={"CLAUDE_CONFIG_DIR": str(tmp_path / "two"), "HOME": "/h"}
    )
    assert first is not None and second is not None
    assert (first.account_ref, second.account_ref) == (ACCOUNT_A, ACCOUNT_B)


def test_envelope_keeps_every_limit_and_rejects_email_account():
    obs = _obs("obs-e", T0)
    assert [b["limit_id"] for b in obs["buckets"]] == ["account:-:5h", "account:-:7d", "model:opus:7d"]
    assert all(b["window_instance"] != "unknown" for b in obs["buckets"])
    bad = dict(obs, account_ref="someone@example.com")
    assert not quota_v2.validate_observation(bad)
    failure = _obs("obs-f", T0, status="rate_limited")
    assert failure["buckets"] == [] and failure["error_ref"] == "rate_limited"


# ---------------------------------------------------------------- imports


def test_import_rejects_foreign_bindings_and_foreign_accounts(slot_env):
    with pytest.raises(quota_v2.QuotaV2Error):
        quota_v2.import_bindings({"schema": quota_v2.BINDINGS_SCHEMA, "machine_id": None, "bindings": []})
    with pytest.raises(quota_v2.QuotaV2Error):
        _enroll(_binding(slot_env, ACCOUNT_A, 1, machine="node-b"), machine="node-a")
    _enroll(_binding(slot_env, ACCOUNT_A, 1))
    with pytest.raises(quota_v2.QuotaV2Error):
        quota_v2.import_observations(
            {
                "schema": quota_v2.OBSERVATIONS_SCHEMA,
                "provider": "claude",
                "account_ref": ACCOUNT_B,
                "observations": [],
            }
        )
    planted = dict(_obs("obs-planted", T0, account=ACCOUNT_B), received_at=_iso(T0))
    added = quota_v2.import_observations(
        {
            "schema": quota_v2.OBSERVATIONS_SCHEMA,
            "provider": "claude",
            "account_ref": ACCOUNT_A,
            "observations": [planted],
        }
    )
    assert added == 0 and quota_v2.account_observations("claude", ACCOUNT_B) == []
    with pytest.raises(quota_v2.QuotaV2Error):
        quota_v2.import_observations({"schema": "quota-observations/v1"})


# ---------------------------------------------------------------- AC4: version skew (read-only)


def test_old_provider_keyed_cache_is_never_promoted_to_an_account(slot_env, monkeypatch, capsys):
    """An old-format snapshots.json (1871bbc shape, no v2 files) keeps serving
    the legacy gate but is not migrated into any account's v2 view."""
    now = dt.datetime.now(dt.UTC)
    old = {
        "claude": {
            "fetched_at": now.timestamp() - 30,
            "result": {
                "id": "claude",
                "status": "ok",
                "plan": "max",
                "source": "oauth-usage-api",
                "pool_class": "preserve",
                "buckets": [b.as_dict() for b in _claude(now).buckets],
                "account_fp": "fp-a",
            },
        }
    }
    cache.cache_path().parent.mkdir(parents=True, exist_ok=True)
    cache.cache_path().write_text(json.dumps(old))
    _enroll(_binding(slot_env, ACCOUNT_A, 1, verified=now - dt.timedelta(hours=1)))
    rc, out, _err = _run_gate(monkeypatch, capsys, lambda: pytest.fail("TTL hit must not fetch"))
    assert rc == 0 and "pool=claude" in out
    record = _shadow_records()[-1]
    assert record["v2"]["code"] == "NO_SAMPLE" and record["match"] is False
    assert quota_v2.account_observations("claude", ACCOUNT_A) == []


def test_old_hub_without_v2_routes_is_rejected_cleanly(slot_env):
    """A hub at a317aed has no /v2 routes: what an operator could feed back is a
    404 body or the legacy /v1/quota list. Neither is accepted as v2 data."""
    for document in (
        {"nodes": [{"machine_id": "node-a", "state": "ok"}]},
        {"error": "404 page not found"},
        [],
    ):
        with pytest.raises(quota_v2.QuotaV2Error):
            quota_v2.import_bindings(document)
        with pytest.raises(quota_v2.QuotaV2Error):
            quota_v2.import_observations(document)
    assert quota_v2.load_bindings() is None


def test_unenrolled_node_writes_no_v2_state(slot_env, monkeypatch, capsys):
    rc, _out, _err = _run_gate(monkeypatch, capsys, lambda: _claude(dt.datetime.now(dt.UTC)), "--no-cache")
    assert rc == 0
    assert not quota_v2.v2_dir().exists()


def test_json_output_schema_unchanged_when_enrolled(slot_env, monkeypatch, capsys):
    """The hub's legacy R29 collector parses `scopefuel --json`; enrollment must
    not add or rename a field there."""
    monkeypatch.setattr(cli, "registry", lambda: {"claude": lambda: _claude(dt.datetime.now(dt.UTC))})
    cli.main(["--json", "--no-cache"])
    before = json.loads(capsys.readouterr().out)
    _enroll(_binding(slot_env, ACCOUNT_A, 1, verified=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)))
    cli.main(["--json", "--no-cache"])
    after = json.loads(capsys.readouterr().out)
    assert set(before) == set(after)
    assert [set(p) for p in before["providers"]] == [set(p) for p in after["providers"]]


# ---------------------------------------------------------------- round 2 (tester t578-verify findings)


def test_own_slot_auth_failure_is_not_cleared_by_other_node_or_later_429():
    """SF-1: the execution slot's own auth failure stands until that same slot
    measures again."""
    now = T0 + dt.timedelta(seconds=90)
    own = {"machine": "node-a", "revision": 1}
    low = _obs("obs-low", T0, result=_claude(T0, five=10.0), **own)
    auth = _obs("obs-auth", T0 + dt.timedelta(seconds=10), status="auth_error", **own)
    other_success = _obs("obs-other", T0 + dt.timedelta(seconds=20), machine="node-b", revision=2)
    own_429 = _obs("obs-429", T0 + dt.timedelta(seconds=30), status="rate_limited", **own)
    for rows in ([low, auth, other_success], [low, auth, own_429]):
        v2 = quota_v2.evaluate(_snapshot(_identity(), rows), "opus", now=now)
        assert v2.code == "AUTH_BLOCKED" and not v2.gate.ok
    recovered = _obs("obs-own-again", T0 + dt.timedelta(seconds=40), **own)
    v2 = quota_v2.evaluate(_snapshot(_identity(), [low, auth, own_429, recovered]), "opus", now=now)
    assert v2.code == "OK" and v2.observation_ids == ("obs-own-again",)


def test_newer_partial_is_not_bypassed_by_an_older_complete_success():
    """SF-2: a newer partial showing 99% must not fall back to an older 10%."""
    low = _obs("obs-low", T0, result=_claude(T0, five=10.0))
    high = _claude(T0, five=99.0, week=99.0)
    high.warning = "incomplete response"
    partial = _obs("obs-partial", T0 + dt.timedelta(seconds=20), status="partial", result=high)
    v2 = quota_v2.evaluate(_snapshot(_identity(), [low, partial]), "opus", now=T0 + dt.timedelta(seconds=30))
    assert v2.code == "PARTIAL" and not v2.gate.ok and v2.gate.unmeasurable


@pytest.mark.parametrize("hook", ["capture_identities", "record_fetch", "shadow_gate", "load_bindings"])
def test_v2_hook_failure_never_reaches_the_real_gate(hook, slot_env, monkeypatch, capsys, tmp_path):
    """SF-3: an exception raised by any v2 entry point is cut at the legacy
    boundary — rc, stdout, stderr and --gate-output stay the same."""

    def run() -> tuple[int, str, str, dict]:
        cache.cache_path().unlink(missing_ok=True)
        output = tmp_path / "gate.json"
        rc, out, err = _run_gate(
            monkeypatch,
            capsys,
            lambda: _claude(dt.datetime.now(dt.UTC)),
            "--no-cache",
            "--gate-output",
            str(output),
        )
        record = json.loads(output.read_text())
        record.pop("generated_at")
        return rc, out, err, record

    baseline = run()
    _enroll(_binding(slot_env, ACCOUNT_A, 1, verified=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)))

    def boom(*_args, **_kwargs):
        raise ZeroDivisionError("injected")

    monkeypatch.setattr(quota_v2, hook, boom)
    assert run() == baseline
    assert cli.main(["--json", "--no-cache"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "scopefuel.v1"


def test_limit_ids_do_not_depend_on_bucket_order():
    """SF-4: two limits sharing scope/window keep their ids when reordered.
    Buckets identical in every fixed field cannot be told apart; they only
    need distinct ids."""
    first = Bucket("7d weekly", "7d", 30.0, _iso(T0 + dt.timedelta(days=3)), Scope("account"), "week")
    second = Bucket("7d rolling", "7d", 60.0, _iso(T0 + dt.timedelta(days=5)), Scope("account"), "week")
    forward = {b["label"]: b["limit_id"] for b in quota_v2.buckets_to_v2([first, second])}
    backward = {b["label"]: b["limit_id"] for b in quota_v2.buckets_to_v2([second, first])}
    assert forward == backward and len(set(forward.values())) == 2
    twin_a = Bucket("7d", "7d", 30.0, _iso(T0 + dt.timedelta(days=3)), Scope("account"), "week")
    twin_b = Bucket("7d", "7d", 60.0, _iso(T0 + dt.timedelta(days=5)), Scope("account"), "week")
    assert len(set(quota_v2._limit_ids([twin_a, twin_b]))) == 2


# ---------------------------------------------------------------- round 3 (t578-verify r2 findings)


def test_limit_ids_stay_unique_and_bounded_so_no_observation_is_dropped(slot_env):
    """R2-SF-1: colliding slugs, suffix look-alikes and long labels must still
    give unique ids — a duplicate would drop the whole envelope."""
    reset = _iso(T0 + dt.timedelta(days=3))
    long_a, long_b = "x" * 121 + "a", "x" * 121 + "b"
    for labels in (("requests", "Requests", "requests-1"), (long_a, long_b), ("same", "same")):
        buckets = [
            Bucket(label, "7d", 10.0 * (i + 1), reset, Scope("model", label), "week")
            for i, label in enumerate(labels)
        ] + [Bucket(label, "7d", 50.0, reset, Scope("account"), "week") for label in labels]
        ids = quota_v2._limit_ids(buckets)
        assert len(set(ids)) == len(ids) and all(len(i) <= 128 for i in ids)
        identity = quota_v2.Identity("claude", ACCOUNT_A, "unknown", 1, "node-a", "slot-x", 0.0, 1e12)
        obs = quota_v2.observation_from_result(
            ProviderResult(id="claude", buckets=buckets), identity, measured_at=T0.timestamp()
        )
        assert quota_v2.validate_observation(obs)
        assert quota_v2.append_observations([obs]) == 1


def test_limit_ids_do_not_move_when_usage_changes():
    """R2-SF-3: ids depend on fixed identity, not on the current value."""
    reset = _iso(T0 + dt.timedelta(days=3))

    def ids(first: float, second: float) -> dict[str, str]:
        pair = [
            Bucket("A B", "7d", first, reset, Scope("account"), "week"),
            Bucket("A-B", "7d", second, reset, Scope("account"), "week"),
        ]
        return {b.label: i for b, i in zip(pair, quota_v2._limit_ids(pair), strict=True)}

    low_high, high_low = ids(10.0, 90.0), ids(90.0, 10.0)
    assert low_high == high_low and len(set(low_high.values())) == 2


@pytest.mark.parametrize("order", ["auth_first", "success_first"])
def test_same_instant_own_auth_failure_is_not_cleared_by_input_order(order):
    """R2-SF-2: an own-slot auth failure and a success at the same instant have
    no provable order; the auth failure wins either way."""
    auth = _obs("obs-auth", T0, status="auth_error")
    success = _obs("obs-ok", T0, result=_claude(T0, five=10.0))
    rows = [auth, success] if order == "auth_first" else [success, auth]
    v2 = quota_v2.evaluate(_snapshot(_identity(), rows), "opus", now=T0 + dt.timedelta(seconds=10))
    assert v2.code == "AUTH_BLOCKED" and not v2.gate.ok
