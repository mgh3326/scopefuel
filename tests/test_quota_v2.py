"""task #578 — quota v2 system behaviour around the contract r3 evaluator.

Shadow invariance (contract §1.2/§9), the local binding mirror and store,
the two-node hand-off through the hub's response shapes, and the CLI. The
decision table itself is pinned row by row in test_quota_v2_contract.py.
No provider is called; fetchers are stubs.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from scopefuel import bench, cache, cli, quota_v2
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.quota_v2_contract import Attempt, valid_post, valid_stored
from scopefuel.quota_v2_eval import Clock

ACCOUNT_A = "acct_7k2m9q4x"
ACCOUNT_B = "acct_p3n8w5r1"


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def claude_fetch(five: float = 10.0, week: float = 20.0):
    def fetch() -> ProviderResult:
        now = dt.datetime.now(dt.UTC)
        reset_5h, reset_7d = iso(now + dt.timedelta(hours=3)), iso(now + dt.timedelta(days=4))
        buckets = [
            {
                "limit_id": "claude.five_hour",
                "label": "5h",
                "scope": {"kind": "account", "ref": None},
                "horizon": "now",
                "window": "5h",
                "window_instance": "unknown",
                "used_pct": five,
                "reset_at": reset_5h,
                "observed_at": None,
            },
            {
                "limit_id": "claude.seven_day",
                "label": "7d all",
                "scope": {"kind": "account", "ref": None},
                "horizon": "week",
                "window": "7d",
                "window_instance": "unknown",
                "used_pct": week,
                "reset_at": reset_7d,
                "observed_at": None,
            },
        ]
        legacy = [
            Bucket("5h", "5h", five, reset_5h, Scope("account"), "now"),
            Bucket("7d all", "7d", week, reset_7d, Scope("account"), "week"),
        ]
        return ProviderResult(
            id="claude",
            plan="max",
            buckets=legacy,
            source="stub",
            http_status=200,
            v2_attempt=Attempt("success", None, buckets, 0),
        )

    return fetch


@pytest.fixture
def slot_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return quota_v2.slot_locator("claude")


def enroll(
    slot: str,
    account: str = ACCOUNT_A,
    revision: int = 2,
    *,
    machine: str = "node-a",
    start: dt.datetime | None = None,
    hours: float = 24,
) -> None:
    start = start or dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
    quota_v2.import_bindings(
        {
            "schema": quota_v2.BINDINGS_SCHEMA,
            "machine_id": machine,
            "bindings": [
                {
                    "account_ref": account,
                    "provider": "claude",
                    "machine_id": machine,
                    "local_slot_ref": slot,
                    "entitlement_ref": "unknown",
                    "identity_basis": "operator_attested",
                    "binding_revision": revision,
                    "verified_at": iso(start),
                    "valid_until": iso(start + dt.timedelta(hours=hours)),
                    "verifier_receipt": None,
                    "label": None,
                }
            ],
        }
    )


def run_gate(monkeypatch, capsys, fetch, *extra: str) -> tuple[int, str, str]:
    monkeypatch.setattr(cli, "registry", lambda: {"claude": fetch})
    monkeypatch.setattr(bench, "read_scores", lambda: None)
    rc = cli.main(["gate", "-m", "opus", *extra])
    out = capsys.readouterr()
    return rc, out.out, out.err


def shadow_records() -> list[dict]:
    path = quota_v2.shadow_path()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# ------------------------------------------------------------------ §1.2 / §9 shadow invariance


def gate_snapshot(monkeypatch, capsys, tmp_path, fetch) -> tuple:
    cache.cache_path().unlink(missing_ok=True)
    output = tmp_path / "gate.json"
    rc, out, err = run_gate(monkeypatch, capsys, fetch, "--no-cache", "--gate-output", str(output))
    record = json.loads(output.read_text())
    record.pop("generated_at")
    monkeypatch.setattr(cli, "registry", lambda: {"claude": fetch})
    json_rc = cli.main(["--json", "--no-cache"])
    view = json.loads(capsys.readouterr().out)
    view.pop("generated_at", None)
    for provider in view["providers"]:
        for volatile in ("age_s", "fetched_at"):
            provider.pop(volatile, None)
        for bucket in provider["buckets"]:
            for volatile in ("pace", "full_use_rate", "resets_at"):
                bucket.pop(volatile, None)
    return rc, out, err, record, json_rc, view


SCENARIOS = {
    "ok": claude_fetch(10.0),
    "blocked": claude_fetch(95.0, 96.0),
    "unmeasurable": lambda: ProviderResult(
        id="claude",
        error="HTTP 500",
        error_kind="server",
        v2_attempt=Attempt("transport_error", "transport_error:http_5xx"),
    ),
}
CONDITIONS = [
    "enrolled",
    "store_corrupt",
    "mirror_corrupt",
    "capture_identities",
    "record_attempts",
    "evaluate",
    "shadow_gate",
]


@pytest.mark.parametrize("condition", CONDITIONS)
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_shadow_never_changes_the_real_gate(condition, scenario, slot_env, monkeypatch, capsys, tmp_path):
    fetch = SCENARIOS[scenario]
    baseline = gate_snapshot(monkeypatch, capsys, tmp_path, fetch)
    enroll(slot_env)
    if condition == "store_corrupt":
        quota_v2.observations_path().write_text("{not json")
    elif condition == "mirror_corrupt":
        quota_v2.bindings_path().write_text("{not json")
    elif condition != "enrolled":

        def boom(*_a, **_k):
            raise ZeroDivisionError("injected")

        monkeypatch.setattr(quota_v2, condition, boom)
    assert gate_snapshot(monkeypatch, capsys, tmp_path, fetch) == baseline


def test_unenrolled_node_writes_no_v2_state(slot_env, monkeypatch, capsys):
    rc, _out, _err = run_gate(monkeypatch, capsys, claude_fetch(), "--no-cache")
    assert rc == 0
    assert not quota_v2.v2_dir().exists()


def test_enrolled_gate_records_and_shadows(slot_env, monkeypatch, capsys):
    enroll(slot_env)
    rc, _out, _err = run_gate(monkeypatch, capsys, claude_fetch(), "--no-cache")
    assert rc == 0
    rows = quota_v2.account_observations("claude", ACCOUNT_A)
    assert len(rows) == 1 and valid_stored(rows[0]) and rows[0]["status"] == "success"
    assert rows[0]["source_slot_ref"] == slot_env and rows[0]["contract_rev"] == "quota-v2.r3"
    record = shadow_records()[-1]
    assert record["v2"]["code"] == "OK" and record["match"] is True
    assert record["clock"] == {"skew_bound_s": None, "source": "unknown"}


# ------------------------------------------------------------------ identity and binding


def test_same_slot_rebound_a_to_b_never_admits_b_with_a_values(slot_env, monkeypatch, capsys):
    enroll(slot_env, ACCOUNT_A, 2)
    assert run_gate(monkeypatch, capsys, claude_fetch(5.0), "--no-cache")[0] == 0
    enroll(slot_env, ACCOUNT_B, 3, start=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
    rc, _out, _err = run_gate(monkeypatch, capsys, lambda: pytest.fail("TTL hit must not fetch"))
    assert rc == 0  # legacy decision unchanged (shadow only)
    record = shadow_records()[-1]
    assert record["v2"]["account_ref"] == ACCOUNT_B and record["v2"]["code"] == "NO_SAMPLE"
    assert quota_v2.account_observations("claude", ACCOUNT_B) == []


def test_binding_not_started_or_degenerate_is_not_an_identity(slot_env):
    now = dt.datetime.now(dt.UTC)
    enroll(slot_env, start=now + dt.timedelta(minutes=5))
    assert quota_v2.resolve_identity("claude", now.timestamp()) == (None, "not_started")
    with pytest.raises(quota_v2.QuotaV2Error):
        enroll(slot_env, start=now, hours=0)


def test_two_slots_of_one_provider_stay_apart(tmp_path):
    one = quota_v2.slot_locator("claude", {"CLAUDE_CONFIG_DIR": str(tmp_path / "one"), "HOME": "/h"})
    two = quota_v2.slot_locator("claude", {"CLAUDE_CONFIG_DIR": str(tmp_path / "two"), "HOME": "/h"})
    assert one != two


def test_imports_reject_foreign_shapes(slot_env):
    for document in ({"nodes": []}, {"error": "404 page not found"}, []):
        with pytest.raises(quota_v2.QuotaV2Error):
            quota_v2.import_bindings(document)
        with pytest.raises(quota_v2.QuotaV2Error):
            quota_v2.import_observations(document)
    with pytest.raises(quota_v2.QuotaV2Error):
        quota_v2.import_bindings({"schema": quota_v2.BINDINGS_SCHEMA, "machine_id": None, "bindings": []})
    enroll(slot_env)
    with pytest.raises(quota_v2.QuotaV2Error):
        quota_v2.import_observations(
            {
                "schema": quota_v2.OBSERVATIONS_SCHEMA,
                "provider": "claude",
                "account_ref": ACCOUNT_B,
                "observations": [],
            }
        )


# ------------------------------------------------------------------ two nodes through hub shapes


def test_two_nodes_hand_off_through_hub_response(tmp_path, monkeypatch, capsys):
    """Node B's observation, exported in POST form and served back by the hub
    (received_at stamped), is used by node A only with a declared clock bound."""
    exported = {}
    for machine, five in (("node-b", 22.0), ("node-a", 20.0)):
        home = tmp_path / machine
        monkeypatch.setenv("SCOPEFUEL_CACHE", str(home / "cache" / "snapshots.json"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
        enroll(quota_v2.slot_locator("claude"), machine=machine, revision=5 if machine == "node-b" else 2)
        assert run_gate(monkeypatch, capsys, claude_fetch(five), "--no-cache")[0] == 0
        identity, _ = quota_v2.resolve_identity("claude", dt.datetime.now(dt.UTC).timestamp())
        rows = quota_v2.export_observations(identity)
        assert len(rows) == 1 and valid_post(rows[0])
        exported[machine] = rows[0]
    served = dict(exported["node-b"], received_at=iso(dt.datetime.now(dt.UTC)))
    assert (
        quota_v2.import_observations(
            {
                "schema": quota_v2.OBSERVATIONS_SCHEMA,
                "provider": "claude",
                "account_ref": ACCOUNT_A,
                "observations": [served],
            }
        )
        == 1
    )
    now = dt.datetime.now(dt.UTC)
    identity, _ = quota_v2.resolve_identity("claude", now.timestamp())
    snap = quota_v2.snapshot_for("claude", identity)
    assert sorted(o["source_machine"] for o in snap["observations"]) == ["node-a", "node-b"]
    assert quota_v2.evaluate(snap, "opus", now=now).code == "TIME_UNCERTAIN"  # production: bound unknown
    fixture_clock = quota_v2.evaluate(snap, "opus", now=now, clock=Clock(0.0, "fixture"))
    assert fixture_clock.code == "OK" and fixture_clock.selected == (exported["node-a"]["observation_id"],)


# ------------------------------------------------------------------ CLI


def test_cli_quota_v2_commands(slot_env, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "registry", lambda: {"claude": claude_fetch()})
    assert cli.main(["quota-v2", "slot", "--provider", "claude"]) == 0
    assert capsys.readouterr().out.strip() == slot_env
    start = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    doc = tmp_path / "bindings.json"
    doc.write_text(
        json.dumps(
            {
                "schema": quota_v2.BINDINGS_SCHEMA,
                "machine_id": "node-a",
                "bindings": [
                    {
                        "account_ref": ACCOUNT_A,
                        "provider": "claude",
                        "machine_id": "node-a",
                        "local_slot_ref": slot_env,
                        "entitlement_ref": "unknown",
                        "identity_basis": "operator_attested",
                        "binding_revision": 2,
                        "verified_at": iso(start),
                        "valid_until": iso(start + dt.timedelta(hours=1)),
                        "verifier_receipt": None,
                        "label": "회사",
                    }
                ],
            }
        )
    )
    assert cli.main(["quota-v2", "bindings", "import", str(doc)]) == 0
    capsys.readouterr()
    cache.collect({"claude": claude_fetch()}, ["claude"])
    assert cli.main(["quota-v2", "observations", "export", "--provider", "claude"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and valid_post(json.loads(lines[0]))
    assert cli.main(["quota-v2", "evaluate", "-m", "opus"]) == 0
    assert json.loads(capsys.readouterr().out)["code"] == "OK"
