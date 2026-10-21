"""hk 1787 (b953) — in-flight sum for shared quota pools.

The gate sums live quota_pool records (local arbiter + per-account_fp hk
docs) and judges reserved windows at used_pct + reserve*n. Every arbiter call
goes through ``inflight._run_arbiter`` (spy seam — never a real binary); every
hk call goes through ``quota_share.request_json`` (dict-backed fake — never a
real route). The git-archive differential pins byte-identity against 5be221b.

Mutant invariants:
- unset/all-zero: zero arbiter calls, zero hk calls, byte-identical output;
- remote filters (schema/pool/fp/skew/age/self/dedupe) each change n;
- headroom deny fires only when reserve*n crosses and plain does not;
- an unknown count never denies and never publishes without a local read.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import urllib.parse

from scopefuel import cli, inflight, policy, quota_share
from scopefuel.http import HttpError
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import gate_check

NOW = dt.datetime(2026, 10, 21, 12, 0, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
EPOCH = NOW.timestamp()
FP = "fp-account-aaaa1111"
HK_URL = "https://hk.invalid"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _reset(window: str) -> str:
    hours = {"5h": 4.9, "7d": 167.0, "30d": 719.0}.get(window, 167.0)
    return (NOW + dt.timedelta(hours=hours)).isoformat()


def _codex(*buckets: tuple[str, float], plan: str | None = "plus", fp: str | None = FP) -> ProviderResult:
    return ProviderResult(
        id="codex",
        plan=plan,
        pool_class="preserve",
        publish_fp=fp,
        publish_fp_kind="account" if fp else None,
        buckets=[
            Bucket(
                label=window,
                window=window,
                used_pct=used,
                resets_at=_reset(window),
                scope=Scope("account"),
                horizon="now" if window == "5h" else "week",
            )
            for window, used in buckets
        ],
    )


class FakeHk:
    """GET/PUT /v1/documents + GET /v1/documents?prefix= — dict-backed."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail = False

    def __call__(self, url, *, method="GET", headers=None, body=None, timeout=None, **_kw):
        if self.fail:
            raise HttpError(503, "unavailable")
        assert (headers or {}).get("Authorization") == "Bearer hk-test-token"
        url = str(url)
        if "/v1/documents?" in url:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            prefix = (query.get("prefix") or [""])[0]
            self.calls.append(("GET", f"?prefix={prefix}"))
            docs = [
                {"key": key, "kind": doc["kind"], "session": doc["session"], "body": doc["body"]}
                for key, doc in sorted(self.docs.items())
                if key.startswith(prefix)
            ]
            return {"documents": docs}
        key = urllib.parse.unquote(url.split("/v1/documents/", 1)[1])
        self.calls.append((method, key))
        if method == "PUT":
            self.docs[key] = dict(body)
            return {"document": {"key": key, **body}, "changed": True}
        doc = self.docs.get(key)
        if doc is None:
            raise HttpError(404, "not_found")
        return {"key": key, "kind": doc["kind"], "session": doc["session"], "body": doc["body"]}


def _enable_hk(monkeypatch) -> FakeHk:
    hk = FakeHk()
    monkeypatch.setenv("HANDOFFKEEP_URL", HK_URL)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", "hk-test-token")
    monkeypatch.setattr(quota_share, "request_json", hk)
    return hk


class ArbiterSpy:
    """Counts calls; serves canned quota_pool_records or a failure."""

    def __init__(self, rows=None, fail=False):
        self.rows = rows
        self.fail = fail
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"quota_pool_records": self.rows or []}), stderr=""
        )


def _spy_arbiter(monkeypatch, rows=None, fail=False) -> ArbiterSpy:
    spy = ArbiterSpy(rows, fail)
    # CI has no arbiter on PATH — stub resolution so the spy is always reached.
    monkeypatch.setattr(inflight, "_arbiter_bin", lambda: "/fake/arbiter")
    monkeypatch.setattr(inflight, "_run_arbiter", spy)
    return spy


def _row(job_id: str, pool: str = "codex", hours_ago: float = 1.0, profile: str = "codex-sol") -> dict:
    return {
        "job_id": job_id,
        "pool": pool,
        "profile": profile,
        "started_at": (NOW - dt.timedelta(hours=hours_ago)).isoformat(),
    }


def _doc(host: str, jobs: list[dict], *, pool: str = "codex", fp: str = FP, skew_s: float = 0.0) -> dict:
    payload = {
        "schema": inflight.SCHEMA,
        "pool": pool,
        "account_fp": fp,
        "host": host,
        "published_at": (NOW + dt.timedelta(seconds=skew_s)).isoformat(),
        "jobs": jobs,
    }
    key = inflight.doc_key(pool, fp, host)
    return {
        "key": key,
        "kind": "note",
        "session": inflight.DOC_SESSION,
        "job": "",
        "body": json.dumps(payload, sort_keys=True),
    }


def _remote_job(job_id: str, hours_ago: float = 1.0) -> dict:
    return {"job_id": job_id, "started_at": (NOW - dt.timedelta(hours=hours_ago)).isoformat()}


def _host(monkeypatch, name: str = "gatehost"):
    monkeypatch.setattr(inflight, "_local_host", lambda: name)
    monkeypatch.setattr(inflight.socket, "gethostname", lambda: name)
    return name


# ---------------------------------------------------------------- AC1: unset


def _matrix() -> list[tuple[list[ProviderResult], str]]:
    return [
        ([_codex(("5h", 10.0), ("7d", 10.0))], "codex-max"),
        ([_codex(("5h", 100.0), ("7d", 10.0))], "codex-max"),
        ([_codex(("7d", 10.0), plan=None)], "codex-max"),
        ([_codex(("7d", 10.0), plan="prolite")], "codex-max"),
        ([ProviderResult(id="codex", plan="plus", error="HTTP 503")], "codex-max"),
        ([_codex(("7d", 64.0), plan="prolite")], "codex-sol"),
        ([_codex(("30d", 74.0), plan="free")], "codex-max"),
        (
            [
                ProviderResult(
                    id="claude",
                    pool_class="preserve",
                    buckets=[
                        Bucket(
                            label="5h",
                            window="5h",
                            used_pct=10.0,
                            resets_at=_reset("5h"),
                            scope=Scope("account"),
                        ),
                        Bucket(
                            label="7d",
                            window="7d",
                            used_pct=10.0,
                            resets_at=_reset("7d"),
                            scope=Scope("account"),
                        ),
                    ],
                )
            ],
            "opus",
        ),
        ([_codex(("7d", 10.0), plan="pro")], "claudex"),  # D3 path
        ([], "nonexistent-profile"),
    ]


def _load_base(tmp_path):
    """scopefuel @ 5be221b from a git archive, as package ``scopefuel_base``."""
    base_sha = "5be221b4375681aca084fabe162aaabd77cbb996"
    if subprocess.run(
        ["git", "cat-file", "-e", base_sha], cwd=REPO, capture_output=True
    ).returncode != 0:
        # Shallow CI checkouts lack the base commit — fetch just that object.
        subprocess.run(
            ["git", "fetch", "--depth=1", "origin", base_sha],
            cwd=REPO,
            capture_output=True,
            check=True,
        )
    archive = subprocess.run(
        ["git", "archive", base_sha, "src"],
        capture_output=True,
        cwd=REPO,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp_path)
    pkg_dir = os.path.join(tmp_path, "src", "scopefuel")
    spec = importlib.util.spec_from_file_location(
        "scopefuel_base", os.path.join(pkg_dir, "__init__.py"), submodule_search_locations=[pkg_dir]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["scopefuel_base"] = module
    spec.loader.exec_module(module)
    return module


def test_unset_byte_identical_gate_check(tmp_path, monkeypatch):
    """AC1: unset inflight_reserve_pct → identical GateResult + zero calls."""
    _load_base(tmp_path / "base")
    base_recommend = importlib.import_module("scopefuel_base.recommend")
    spy = _spy_arbiter(monkeypatch, rows=[_row("x")])
    hk = _enable_hk(monkeypatch)
    for providers, profile in _matrix():
        new = gate_check(providers, profile, today=TODAY, now=NOW)
        old = base_recommend.gate_check(providers, profile, today=TODAY, now=NOW)
        for field in (
            "ok",
            "reason",
            "provider_id",
            "grade",
            "used_pct",
            "pool_class",
            "unmeasurable",
            "alternatives",
            "stale_accepted",
            "missing_windows",
            "unknown_plan",
            "role_denied",
            "unsubscribed",
        ):
            assert getattr(new, field) == getattr(old, field), (profile, field, providers)
        assert new.inflight is None and new.inflight_warning is None
    assert spy.calls == []
    # zero hk requests from the new code — quota_share's own publish is base
    assert [c for c in hk.calls if "inflight" in c[1]] == []


def test_all_zero_is_unset(tmp_path, monkeypatch):
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 0.0, "7d": 0.0})
    spy = _spy_arbiter(monkeypatch)
    hk = _enable_hk(monkeypatch)
    result = gate_check([_codex(("5h", 10.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok and result.inflight is None
    assert spy.calls == [] and hk.calls == []


def test_unset_cli_byte_identical(tmp_path, monkeypatch, capsys):
    """AC1 CLI: stdout/stderr/rc identical to base across the matrix."""
    _load_base(tmp_path / "base")
    base_cli = importlib.import_module("scopefuel_base.cli")
    importlib.import_module("scopefuel_base.refresh")
    spy = _spy_arbiter(monkeypatch)
    hk = _enable_hk(monkeypatch)
    providers_result = _codex(("5h", 10.0), ("7d", 10.0))
    for module in (cli, base_cli):
        monkeypatch.setattr(module, "registry", lambda: {"codex": lambda: providers_result})
        capsys.readouterr()
        rc = module.main(["gate", "-m", "codex-max", "--no-cache"])
        out = capsys.readouterr()
        if module is cli:
            new = (rc, out.out, out.err)
        else:
            old = (rc, out.out, out.err)
    assert new == old
    assert spy.calls == []
    assert [c for c in hk.calls if "inflight" in c[1]] == []


def test_invalid_table_entries_fall_back_per_window(monkeypatch):
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    # negative entry written directly past the CLI validator
    path = policy.config_path()
    text = path.read_text()
    path.write_text(text.replace("5h = 12.0", "5h = 12.0\n7d = -3.0"))
    reserves, status = policy.get_inflight_reserve("codex")
    assert reserves == {"5h": 12.0}
    assert status and "7d" in status


def test_non_dict_table_is_unset(monkeypatch):
    path = policy.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[pools.codex]\ninflight_reserve_pct = "oops"\n')
    assert policy.get_inflight_reserve("codex") == ({}, policy.get_inflight_reserve("codex")[1])
    reserves, status = policy.get_inflight_reserve("codex")
    assert reserves == {} and status is not None


def test_policy_set_writes_sub_table():
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0, "7d": 2.0})
    reserves, status = policy.get_inflight_reserve("codex")
    assert reserves == {"5h": 12.0, "7d": 2.0} and status is None
    policy.set_policy("codex", None, inflight_reserve_pct=None)
    assert policy.get_inflight_reserve("codex") == ({}, None)


# ---------------------------------------------------------------- AC2: the n


def _gate_with(monkeypatch, local_rows, remote_docs, host="gatehost", reserve=None):
    _host(monkeypatch, host)
    spy = _spy_arbiter(monkeypatch, rows=local_rows)
    hk = _enable_hk(monkeypatch)
    for doc in remote_docs:
        hk.docs[doc["key"]] = doc
    policy.set_policy("codex", None, inflight_reserve_pct=reserve or {"5h": 12.0})
    result = gate_check([_codex(("5h", 10.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    return result, spy, hk


def test_n_counts_local_pool_rows_only(monkeypatch):
    result, spy, hk = _gate_with(monkeypatch, [_row("a"), _row("b"), _row("c", pool="claude")], [])
    assert result.ok and result.inflight == "inflight=2"
    assert len(spy.calls) == 1 and spy.calls[0][-2:] == ["status", "--json"]


def test_n_adds_remote_docs(monkeypatch):
    docs = [_doc("oci", [_remote_job("j1"), _remote_job("j2")]), _doc("pi", [_remote_job("j3")])]
    result, spy, hk = _gate_with(monkeypatch, [_row("a")], docs)
    assert result.inflight == "inflight=4"


def test_own_doc_is_excluded(monkeypatch):
    docs = [_doc("gatehost", [_remote_job("mine")]), _doc("oci", [_remote_job("j1")])]
    result, spy, hk = _gate_with(monkeypatch, [], docs)
    assert result.inflight == "inflight=1"


def test_job_ids_dedupe_across_docs(monkeypatch):
    docs = [_doc("oci", [_remote_job("j1"), _remote_job("j2")]), _doc("pi", [_remote_job("j2")])]
    result, spy, hk = _gate_with(monkeypatch, [], docs)
    assert result.inflight == "inflight=2"


def test_drop_bad_schema(monkeypatch):
    doc = _doc("oci", [_remote_job("j1")])
    doc["body"] = json.dumps({**json.loads(doc["body"]), "extra": 1})
    result, _, _ = _gate_with(monkeypatch, [], [doc])
    assert result.inflight == "inflight=0"


def test_drop_wrong_pool(monkeypatch):
    doc = _doc("oci", [_remote_job("j1")], pool="claude")
    result, _, _ = _gate_with(monkeypatch, [], [doc])
    assert result.inflight == "inflight=0"


def test_drop_wrong_account_fp(monkeypatch):
    hk_key = inflight.doc_key("codex", FP, "oci")
    doc = _doc("oci", [_remote_job("j1")])
    doc["key"] = hk_key
    doc["body"] = json.dumps({**json.loads(doc["body"]), "account_fp": "fp-other"})
    result, _, _ = _gate_with(monkeypatch, [], [doc])
    assert result.inflight == "inflight=0"


def test_drop_future_skewed_doc(monkeypatch):
    result, _, _ = _gate_with(monkeypatch, [], [_doc("oci", [_remote_job("j1")], skew_s=120.0)])
    assert result.inflight == "inflight=0"


def test_doc_age_never_drops(monkeypatch):
    """published_at far in the past still counts — only job age matters."""
    result, _, _ = _gate_with(monkeypatch, [], [_doc("oci", [_remote_job("j1")], skew_s=-86400.0)])
    assert result.inflight == "inflight=1"


def test_drop_aged_job(monkeypatch):
    docs = [_doc("oci", [_remote_job("old", hours_ago=9.0), _remote_job("new")])]
    result, _, _ = _gate_with(monkeypatch, [], docs)
    assert result.inflight == "inflight=1"


# ---------------------------------------------------------------- AC3: rule


def test_headroom_deny_exact_reason(monkeypatch):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a"), _row("b"), _row("c")])
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 54.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is False and result.unmeasurable is False
    assert result.reason == "codex inflight_headroom window=5h used=54 n=3 reserve=12 cutoff=90"


def test_headroom_boundary_equal_denies(monkeypatch):
    """used + reserve*n == cutoff behaves like the base >= comparison."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a")])
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 40.0})
    result = gate_check([_codex(("5h", 50.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is False and "inflight_headroom" in result.reason


def test_headroom_boundary_below_allows(monkeypatch):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a")])
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 39.0})
    result = gate_check([_codex(("5h", 50.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok and result.inflight == "inflight=1"


def test_window_without_reserve_keeps_plain(monkeypatch):
    """Only 5h reserved: a 7d window under cutoff stays plain (no deny)."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a"), _row("b")])
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 10.0), ("7d", 80.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok


def test_plain_over_deny_keeps_base_reason(monkeypatch):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a")])
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 95.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is False and "inflight_headroom" not in result.reason
    assert "소진" in result.reason


def test_allow_appends_inflight_only(monkeypatch):
    result, _, _ = _gate_with(monkeypatch, [_row("a")], [])
    assert result.ok and result.reason.endswith(" inflight=1")


# ---------------------------------------------------------------- AC4: publish


def test_publish_fires_with_fixed_key_set(monkeypatch):
    result, spy, hk = _gate_with(monkeypatch, [_row("a", hours_ago=0.5)], [], host="mbp")
    assert result.ok
    puts = [call for call in hk.calls if call[0] == "PUT"]
    assert puts == [("PUT", "quota/codex/fp-account-aaaa1111/inflight/mbp")]
    body = json.loads(hk.docs[puts[0][1]]["body"])
    assert set(body) == {"schema", "pool", "account_fp", "host", "published_at", "jobs"}
    assert body["schema"] == inflight.SCHEMA and body["host"] == "mbp"
    assert body["pool"] == "codex" and body["account_fp"] == FP
    assert [set(j) for j in body["jobs"]] == [{"job_id", "started_at"}]
    assert body["jobs"][0]["job_id"] == "a"


def test_publish_empty_list(monkeypatch):
    _, _, hk = _gate_with(monkeypatch, [], [], host="mbp")
    puts = [call for call in hk.calls if call[0] == "PUT"]
    assert len(puts) == 1
    assert json.loads(hk.docs[puts[0][1]]["body"])["jobs"] == []


def test_no_publish_when_setting_inactive(monkeypatch):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a")])
    hk = _enable_hk(monkeypatch)
    gate_check([_codex(("5h", 10.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert [c for c in hk.calls if c[0] == "PUT"] == []


def test_no_publish_when_local_read_failed(monkeypatch):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, fail=True)
    hk = _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    gate_check([_codex(("5h", 10.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert [c for c in hk.calls if c[0] == "PUT"] == []


def test_no_publish_without_account_fp(monkeypatch):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a")])
    hk = _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 10.0), ("7d", 10.0), fp=None)], "codex-max", today=TODAY, now=NOW)
    assert result.inflight == "inflight=unknown(remote)"
    assert [c for c in hk.calls if c[0] == "PUT"] == []


# ---------------------------------------------------------------- AC5: fail-open


def test_local_failure_unknown_tag_and_warning(monkeypatch, capsys):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, fail=True)
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    providers_result = _codex(("5h", 10.0), ("7d", 10.0))
    monkeypatch.setattr(cli, "registry", lambda: {"codex": lambda: providers_result})
    rc = cli.main(["gate", "-m", "codex-max", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    assert "inflight=unknown(local)" in out.out
    warnings = [w for w in out.err.splitlines() if w.startswith("warning:") and "in-flight" in w]
    assert len(warnings) == 1 and "in-flight local count unavailable" in warnings[0]


def test_remote_failure_keeps_local_n(monkeypatch):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a"), _row("b")])
    hk = _enable_hk(monkeypatch)
    hk.fail = True
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 10.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok and result.inflight == "inflight=unknown(remote)"


def test_remote_failure_still_denies_on_local_n(monkeypatch):
    """Partial result still applies local n — unknown never widens."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a"), _row("b"), _row("c")])
    hk = _enable_hk(monkeypatch)
    hk.fail = True
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 54.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is False and "inflight_headroom" in result.reason


def test_unknown_local_never_denies(monkeypatch):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, fail=True)
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 89.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok and "inflight=unknown(local)" in result.reason


def test_warning_order_after_unknown_plan(monkeypatch, capsys):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, fail=True)
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    providers_result = _codex(("5h", 10.0), ("7d", 10.0), plan="pro")
    monkeypatch.setattr(cli, "registry", lambda: {"codex": lambda: providers_result})
    cli.main(["gate", "-m", "codex-max", "--no-cache"])
    out = capsys.readouterr()
    warnings = [
        w for w in out.err.splitlines() if w.startswith("warning:") and ("plan_type" in w or "in-flight" in w)
    ]
    assert len(warnings) == 2
    assert "plan_type" in warnings[0] and "in-flight" in warnings[1]


# ---------------------------------------------------------------- AC6: show


def test_inflight_show_is_read_only(monkeypatch, capsys):
    _host(monkeypatch, "mbp")
    _spy_arbiter(monkeypatch, rows=[_row("a")])
    hk = _enable_hk(monkeypatch)
    live = dt.datetime.now(dt.UTC)
    fresh = {"job_id": "j1", "started_at": (live - dt.timedelta(hours=1)).isoformat()}
    aged = {"job_id": "old", "started_at": (live - dt.timedelta(hours=9)).isoformat()}
    for host, jobs in (("oci", [fresh, aged]), ("mbp", [fresh])):
        body = {
            "schema": inflight.SCHEMA,
            "pool": "codex",
            "account_fp": FP,
            "host": host,
            "published_at": live.isoformat(),
            "jobs": jobs,
        }
        hk.docs[inflight.doc_key("codex", FP, host)] = {
            "key": inflight.doc_key("codex", FP, host),
            "kind": "note",
            "session": inflight.DOC_SESSION,
            "job": "",
            "body": json.dumps(body, sort_keys=True),
        }
    rc = cli.main(["inflight", "show", "--pool", "codex"])
    out = capsys.readouterr()
    assert rc == 0
    assert "local: 1 live job(s)" in out.out
    assert "account_fp=fp-account-aaaa1111 n=2" in out.out
    assert "dropped: aged" in out.out and "dropped: self" in out.out
    assert [c for c in hk.calls if c[0] == "PUT"] == []


def test_inflight_show_json(monkeypatch, capsys):
    _host(monkeypatch, "mbp")
    _spy_arbiter(monkeypatch, rows=[])
    hk = _enable_hk(monkeypatch)
    live = dt.datetime.now(dt.UTC)
    body = {
        "schema": inflight.SCHEMA,
        "pool": "codex",
        "account_fp": FP,
        "host": "oci",
        "published_at": live.isoformat(),
        "jobs": [{"job_id": "j1", "started_at": (live - dt.timedelta(hours=1)).isoformat()}],
    }
    key = inflight.doc_key("codex", FP, "oci")
    hk.docs[key] = {
        "key": key,
        "kind": "note",
        "session": inflight.DOC_SESSION,
        "job": "",
        "body": json.dumps(body, sort_keys=True),
    }
    rc = cli.main(["inflight", "show", "--pool", "codex", "--json"])
    out = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out.out)
    assert payload["schema"] == "scopefuel.inflight-show.v1"
    assert payload["accounts"][0]["n"] == 1
    assert [c for c in hk.calls if c[0] == "PUT"] == []


# ------------------------------------------------------------ unit seams


def test_local_jobs_parses_rows(monkeypatch):
    _spy_arbiter(monkeypatch, rows=[_row("a"), _row("b", pool="devin")])
    assert inflight.local_jobs("codex") == [{"job_id": "a", "started_at": _row("a")["started_at"]}]


def test_local_jobs_failures(monkeypatch):
    _spy_arbiter(monkeypatch, fail=True)
    assert inflight.local_jobs("codex") is None
    monkeypatch.setattr(inflight, "_arbiter_bin", lambda: None)
    assert inflight.local_jobs("codex") is None
    monkeypatch.setattr(inflight, "_arbiter_bin", lambda: "/x/arbiter")
    monkeypatch.setattr(
        inflight, "_run_arbiter", lambda argv: subprocess.CompletedProcess(argv, 0, stdout="bad", stderr="")
    )
    assert inflight.local_jobs("codex") is None


def test_arbiter_bin_env_resolution(monkeypatch, tmp_path):
    stub = tmp_path / "arbiter"
    stub.write_text("#!/bin/sh\n")
    stub.chmod(0o755)
    monkeypatch.setenv("ARBITER_BIN", str(stub))
    assert inflight._arbiter_bin() == str(stub)
    monkeypatch.setenv("ARBITER_BIN", str(tmp_path / "missing"))
    assert inflight._arbiter_bin() is None
    monkeypatch.delenv("ARBITER_BIN")
    monkeypatch.setattr(
        inflight.shutil, "which", lambda name: "/usr/bin/arbiter" if name == "arbiter" else None
    )
    assert inflight._arbiter_bin() == "/usr/bin/arbiter"
