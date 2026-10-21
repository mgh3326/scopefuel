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
    if subprocess.run(["git", "cat-file", "-e", base_sha], cwd=REPO, capture_output=True).returncode != 0:
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


# ---------------------------------------------------------- hk 1787 r1 fixes


def _deep_json(n: int) -> str:
    return "[" * n + "]" * n


def test_local_jobs_deeply_nested_json_fails_open(monkeypatch):
    """D1 fix: pathological arbiter output folds to None, not RecursionError."""
    _spy_arbiter(monkeypatch)
    monkeypatch.setattr(
        inflight,
        "_run_arbiter",
        lambda argv: subprocess.CompletedProcess(argv, 0, stdout=_deep_json(200000), stderr=""),
    )
    assert inflight.local_jobs("codex") is None


def test_gate_deeply_nested_arbiter_json_unknown_not_crash(monkeypatch):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch)
    monkeypatch.setattr(
        inflight,
        "_run_arbiter",
        lambda argv: subprocess.CompletedProcess(argv, 0, stdout=_deep_json(200000), stderr=""),
    )
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 10.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok and result.inflight == "inflight=unknown(local)"


def test_doc_view_deeply_nested_body_drops(monkeypatch):
    """D1 fix: pathological remote doc body drops by schema, not RecursionError."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[])
    hk = _enable_hk(monkeypatch)
    key = inflight.doc_key("codex", FP, "oci")
    hk.docs[key] = {
        "key": key,
        "kind": "note",
        "session": inflight.DOC_SESSION,
        "job": "",
        "body": _deep_json(100000),
    }
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 10.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok and result.inflight == "inflight=0"


def test_policy_set_replaces_existing_table(monkeypatch):
    """D2 fix: set -> replace -> get, with an existing [pools.<p>] header."""
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 20.0})
    reserves, status = policy.get_inflight_reserve("codex")
    assert reserves == {"5h": 20.0} and status is None


def test_policy_set_replaces_without_pool_header():
    """D2 fix: only the implicit [pools.<p>.inflight_reserve_pct] exists."""
    path = policy.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[pools.codex.inflight_reserve_pct]\n5h = 12.0\n")
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 20.0})
    reserves, _ = policy.get_inflight_reserve("codex")
    assert reserves == {"5h": 20.0}


# ------------------------------------------------- round-2 survivor killers


def _claudex_gate(monkeypatch, local_rows, remote_docs=None, reserve=None, used=10.0):
    """Drive the D3 not-in-table site: 'claudex' judges a pool not in its table."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=local_rows)
    hk = _enable_hk(monkeypatch)
    for doc in remote_docs or []:
        hk.docs[doc["key"]] = doc
    policy.set_policy("codex", None, inflight_reserve_pct=reserve or {"5h": 12.0})
    return gate_check([_codex(("5h", used), ("7d", 10.0), plan="pro")], "claudex", today=TODAY, now=NOW), hk


def test_d3_headroom_deny_with_reserve(monkeypatch):
    result, _ = _claudex_gate(monkeypatch, [_row("a"), _row("b"), _row("c")], used=54.0)
    assert result.ok is False and "inflight_headroom" in result.reason


def test_d3_allow_tag_and_publish(monkeypatch):
    result, hk = _claudex_gate(monkeypatch, [_row("a")], used=10.0)
    assert result.ok and result.inflight == "inflight=1"
    assert result.reason.endswith(" inflight=1")
    assert [c for c in hk.calls if c[0] == "PUT" and "/inflight/" in c[1]]


def test_d3_unknown_tag_on_plain_deny(monkeypatch):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, fail=True)
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 95.0), ("7d", 10.0), plan="pro")], "claudex", today=TODAY, now=NOW)
    assert result.ok is False and "inflight=unknown(local)" in result.reason


def test_normal_site_unknown_tag_on_deny(monkeypatch):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, fail=True)
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 95.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is False and "inflight=unknown(local)" in result.reason


def test_two_distinct_window_reserves(monkeypatch):
    """Each reserve applies to its own window only (kills max/first mutants)."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a"), _row("b")])
    _enable_hk(monkeypatch)
    # 7d reserve 5 * n=2 -> 80+10=90 == cutoff on 7d; 5h reserve 12 * 2 -> 10+24=34 < 90
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0, "7d": 5.0})
    result = gate_check([_codex(("5h", 10.0), ("7d", 80.0))], "codex-max", today=TODAY, now=NOW)
    assert result.reason == "codex inflight_headroom window=7d used=80 n=2 reserve=5 cutoff=90"


def test_reserve_on_unrequired_window_ignored(monkeypatch):
    """A reserve on a window the profile does not require never fires."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a"), _row("b")])
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"30d": 50.0})
    result = gate_check(
        [_codex(("5h", 10.0), ("7d", 10.0), ("30d", 80.0))], "codex-max", today=TODAY, now=NOW
    )
    # 30d is not a required window for codex: mutant judging it would deny (80+100)
    assert result.ok and result.inflight == "inflight=2"


def test_drop_wrong_schema_value(monkeypatch):
    doc = _doc("oci", [_remote_job("j1")])
    doc["body"] = json.dumps({**json.loads(doc["body"]), "schema": "scopefuel.other.v1"})
    result, _, _ = _gate_with(monkeypatch, [], [doc])
    assert result.inflight == "inflight=0"


def test_drop_wrong_pool_under_matching_key(monkeypatch):
    """Doc sits under the codex prefix but its body says another pool."""
    key = inflight.doc_key("codex", FP, "oci")
    doc = _doc("oci", [_remote_job("j1")])
    doc["key"] = key
    doc["body"] = json.dumps({**json.loads(doc["body"]), "pool": "claude"})
    result, _, _ = _gate_with(monkeypatch, [], [doc])
    assert result.inflight == "inflight=0"


def test_drop_future_started_at(monkeypatch):
    """started_at beyond the 8h window in the future drops; +1h still counts."""
    job = {"job_id": "fut", "started_at": (NOW + dt.timedelta(hours=9)).isoformat()}
    near = {"job_id": "near", "started_at": (NOW + dt.timedelta(hours=1)).isoformat()}
    result, _, _ = _gate_with(monkeypatch, [], [_doc("oci", [job, near])])
    assert result.inflight == "inflight=1"


def test_7h_old_job_counts(monkeypatch):
    result, _, _ = _gate_with(monkeypatch, [], [_doc("oci", [_remote_job("j1", hours_ago=7.0)])])
    assert result.inflight == "inflight=1"


def test_drop_unparseable_started_at_and_published_at(monkeypatch):
    job = {"job_id": "j1", "started_at": "not-a-date"}
    bad_pub = _doc("pi", [_remote_job("j1")])
    bad_pub["body"] = json.dumps({**json.loads(bad_pub["body"]), "published_at": "nope"})
    # two docs under distinct keys: bad started_at drops its job, bad
    # published_at drops its doc — either mutant surviving would count j1
    result, _, _ = _gate_with(monkeypatch, [], [_doc("oci", [job]), bad_pub])
    assert result.inflight == "inflight=0"


def test_self_exclusion_key_suffix_leg(monkeypatch):
    """Key-suffix self leg: seed a self doc and make the publish PUT fail."""
    _host(monkeypatch, "gatehost")
    _spy_arbiter(monkeypatch, rows=[])
    hk = _enable_hk(monkeypatch)
    # doc key ends in this host but its body names a different host — only the
    # key-suffix leg can drop it (kills the body-leg-only mutant too)
    key = inflight.doc_key("codex", FP, "gatehost")
    doc = _doc("othername", [_remote_job("mine")])
    doc["key"] = key
    hk.docs[key] = doc

    original = hk.__call__

    def flaky(url, *, method="GET", **kw):
        if method == "PUT":
            raise HttpError(503, "unavailable")
        return original(url, method=method, **kw)

    monkeypatch.setattr(quota_share, "request_json", flaky)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 10.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.inflight == "inflight=0"


def test_self_exclusion_body_host_leg(monkeypatch):
    """Body-host self leg: doc under another key claims host == this host."""
    key = inflight.doc_key("codex", FP, "otherkey")
    doc = _doc("gatehost", [_remote_job("mine")])
    doc["key"] = key
    result, _, _ = _gate_with(monkeypatch, [], [doc])
    assert result.inflight == "inflight=0"


def test_local_failed_remote_present_not_applied(monkeypatch):
    """Local read failed: remote n must NOT apply, tag unknown(local), no PUT."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, fail=True)
    hk = _enable_hk(monkeypatch)
    hk.docs[inflight.doc_key("codex", FP, "oci")] = _doc(
        "oci", [_remote_job("j1"), _remote_job("j2"), _remote_job("j3")]
    )
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = gate_check([_codex(("5h", 54.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok and "inflight=unknown(local)" in result.reason
    assert [c for c in hk.calls if c[0] == "PUT"] == []


def test_deny_path_warning_order(monkeypatch, capsys):
    """Deny path: reason line first, then unknown-plan, then inflight warning."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, fail=True)
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    providers_result = _codex(("5h", 95.0), ("7d", 10.0), plan="pro")
    monkeypatch.setattr(cli, "registry", lambda: {"codex": lambda: providers_result})
    rc = cli.main(["gate", "-m", "codex-max", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 3
    warnings = [
        w
        for w in out.err.splitlines()
        if w.startswith("warning:") and ("plan_type" in w or "in-flight" in w or "build stale" in w)
    ]
    kinds = ["plan" if "plan_type" in w else ("inflight" if "in-flight" in w else "stale") for w in warnings]
    # unknown-plan -> inflight -> stale-build order on the deny path too
    assert kinds == ["plan", "inflight", "stale"] or kinds == ["plan", "inflight"]
    assert "inflight=unknown(local)" in out.err


def test_publish_key_uses_sanitised_host(monkeypatch):
    # patch gethostname only — _local_host must run _safe_host on it
    monkeypatch.setattr(inflight.socket, "gethostname", lambda: "we ird/host!")
    _spy_arbiter(monkeypatch, rows=[])
    hk = _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    gate_check([_codex(("5h", 10.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    puts = [c[1] for c in hk.calls if c[0] == "PUT" and "/inflight/" in c[1]]
    assert puts == ["quota/codex/fp-account-aaaa1111/inflight/unknown"]


def test_show_two_account_fps_and_failed_local(monkeypatch, capsys):
    _host(monkeypatch, "mbp")
    _spy_arbiter(monkeypatch, fail=True)
    hk = _enable_hk(monkeypatch)
    live = dt.datetime.now(dt.UTC)
    for fp, host, jobs in (
        (FP, "oci", [{"job_id": "j1", "started_at": live.isoformat()}]),
        ("fp-other-2222", "pi", [{"job_id": "j2", "started_at": live.isoformat()}]),
    ):
        body = {
            "schema": inflight.SCHEMA,
            "pool": "codex",
            "account_fp": fp,
            "host": host,
            "published_at": live.isoformat(),
            "jobs": jobs,
        }
        key = inflight.doc_key("codex", fp, host)
        hk.docs[key] = {
            "key": key,
            "kind": "note",
            "session": inflight.DOC_SESSION,
            "job": "",
            "body": json.dumps(body, sort_keys=True),
        }
    rc = cli.main(["inflight", "show", "--pool", "codex"])
    out = capsys.readouterr()
    assert rc == 0
    assert "local: unavailable" in out.out
    assert f"account_fp={FP} n=unknown" in out.out
    assert "account_fp=fp-other-2222 n=unknown" in out.out
    assert [c for c in hk.calls if c[0] == "PUT"] == []


def test_token_kind_fp_never_publishes_or_queries(monkeypatch):
    """A token-kind publish fp must not be treated as an account fingerprint."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a")])
    hk = _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    result = ProviderResult(
        id="codex",
        plan="plus",
        pool_class="preserve",
        publish_fp="fp-token-bbbb",
        publish_fp_kind="token",
        buckets=[
            Bucket(
                label="5h",
                window="5h",
                used_pct=10.0,
                resets_at=_reset("5h"),
                scope=Scope("account"),
                horizon="now",
            ),
            Bucket(
                label="7d",
                window="7d",
                used_pct=10.0,
                resets_at=_reset("7d"),
                scope=Scope("account"),
                horizon="week",
            ),
        ],
    )
    gate = gate_check([result], "codex-max", today=TODAY, now=NOW)
    assert gate.inflight == "inflight=unknown(remote)"
    assert [c for c in hk.calls if c[0] == "PUT"] == []
    assert [c for c in hk.calls if "inflight" in c[1] and c[0] == "GET"] == []


# ------------------------------------------------- round-3 survivor killers


def _sentinel_stale(monkeypatch):
    """Replace the stale-build emitter with a fixed sentinel line."""
    monkeypatch.setattr(
        cli, "_emit_stale_build_warning", lambda: print("warning: stale-sentinel", file=sys.stderr)
    )


def test_allow_path_warning_order_with_stale(monkeypatch, capsys):
    """inflight warning sits between unknown-plan and stale-build on allow."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, fail=True)
    _enable_hk(monkeypatch)
    _sentinel_stale(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    providers_result = _codex(("5h", 10.0), ("7d", 10.0), plan="pro")
    monkeypatch.setattr(cli, "registry", lambda: {"codex": lambda: providers_result})
    rc = cli.main(["gate", "-m", "codex-max", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    warnings = [
        w
        for w in out.err.splitlines()
        if w.startswith("warning:") and ("plan_type" in w or "in-flight" in w or "stale-sentinel" in w)
    ]
    kinds = ["plan" if "plan_type" in w else ("inflight" if "in-flight" in w else "stale") for w in warnings]
    assert kinds == ["plan", "inflight", "stale"]


def test_deny_path_warning_order_with_stale(monkeypatch, capsys):
    """Deny: reason is stderr line 1, then plan, inflight, stale sentinel."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, fail=True)
    _enable_hk(monkeypatch)
    _sentinel_stale(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    providers_result = _codex(("5h", 95.0), ("7d", 10.0), plan="pro")
    monkeypatch.setattr(cli, "registry", lambda: {"codex": lambda: providers_result})
    rc = cli.main(["gate", "-m", "codex-max", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 3
    # deny reason line precedes the trailing warning block (bench warnings
    # from the hk fake may precede the reason itself — they are unrelated)
    assert "소진" in out.err
    warnings = [
        w
        for w in out.err.splitlines()
        if w.startswith("warning:") and ("plan_type" in w or "in-flight" in w or "stale-sentinel" in w)
    ]
    kinds = ["plan" if "plan_type" in w else ("inflight" if "in-flight" in w else "stale") for w in warnings]
    assert kinds == ["plan", "inflight", "stale"]
    deny_idx = out.err.index("소진")
    first_warn_idx = min(out.err.index(w) for w in warnings)
    assert deny_idx < first_warn_idx


def test_show_per_account_n_with_working_local(monkeypatch, capsys):
    """Two account_fps with a live arbiter: n is per-fp, not accumulated."""
    _host(monkeypatch, "mbp")
    _spy_arbiter(monkeypatch, rows=[_row("a")])
    hk = _enable_hk(monkeypatch)
    live = dt.datetime.now(dt.UTC)
    for fp, host, jobs in (
        (FP, "oci", [{"job_id": "j1", "started_at": live.isoformat()}]),
        ("fp-other-2222", "pi", [{"job_id": "j2", "started_at": live.isoformat()}]),
    ):
        body = {
            "schema": inflight.SCHEMA,
            "pool": "codex",
            "account_fp": fp,
            "host": host,
            "published_at": live.isoformat(),
            "jobs": jobs,
        }
        key = inflight.doc_key("codex", fp, host)
        hk.docs[key] = {
            "key": key,
            "kind": "note",
            "session": inflight.DOC_SESSION,
            "job": "",
            "body": json.dumps(body, sort_keys=True),
        }
    rc = cli.main(["inflight", "show", "--pool", "codex"])
    out = capsys.readouterr()
    assert rc == 0
    # local 1 + remote 1 each — a mutant summing across fps would print 3
    assert f"account_fp={FP} n=2" in out.out
    assert "account_fp=fp-other-2222 n=2" in out.out
    assert [c for c in hk.calls if c[0] == "PUT"] == []


def test_invalid_job_fields_not_counted(monkeypatch):
    """Jobs with missing or non-str job_id are dropped, not counted."""
    doc = _doc(
        "oci",
        [{"started_at": NOW.isoformat()}, {"job_id": 42, "started_at": NOW.isoformat()}],
    )
    result, _, _ = _gate_with(monkeypatch, [], [doc])
    assert result.inflight == "inflight=0"


def test_policy_replace_keeps_sibling_keys(monkeypatch):
    """Replacing the reserve table must not drop sibling keys under pools.<p>."""
    policy.set_policy("codex", None, subscribed=True, inflight_reserve_pct={"5h": 12.0})
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 20.0})
    reserves, _ = policy.get_inflight_reserve("codex")
    assert reserves == {"5h": 20.0}
    assert "subscribed = true" in policy.config_path().read_text()


def test_gate_output_record_carries_inflight(monkeypatch, tmp_path):
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a")])
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 12.0})
    out_file = tmp_path / "gate.json"
    providers_result = _codex(("5h", 10.0), ("7d", 10.0))
    monkeypatch.setattr(cli, "registry", lambda: {"codex": lambda: providers_result})
    rc = cli.main(["gate", "-m", "codex-max", "--no-cache", "--gate-output", str(out_file)])
    assert rc == 0
    record = json.loads(out_file.read_text())
    assert record.get("inflight") == "inflight=1"


def test_reserve_bool_and_over_100_fall_back(monkeypatch):
    path = policy.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[pools.codex.inflight_reserve_pct]\n5h = true\n7d = 150.0\n30d = 4.0\n")
    reserves, status = policy.get_inflight_reserve("codex")
    assert reserves == {"30d": 4.0} and status is not None
    # a table whose every value is invalid behaves as unset
    path.write_text("[pools.codex.inflight_reserve_pct]\n5h = true\n7d = 150.0\n")
    assert policy.get_inflight_reserve("codex") == ({}, policy.get_inflight_reserve("codex")[1])


def test_wrong_window_reserve_would_overcharge(monkeypatch):
    """Wrong-window reserve mutants (max/first/swap for all windows) must be
    visible: judge codex-sol, whose required window is only 7d. Its own
    reserve (2*3=6) leaves 64+6=70 < 90; any mutant pulling the 5h reserve
    (50) for 7d judges 64+150 >= 90 and wrongly denies."""
    _host(monkeypatch)
    _spy_arbiter(monkeypatch, rows=[_row("a"), _row("b"), _row("c")])
    _enable_hk(monkeypatch)
    policy.set_policy("codex", None, inflight_reserve_pct={"5h": 50.0, "7d": 2.0})
    result = gate_check(
        [_codex(("5h", 10.0), ("7d", 64.0), plan="prolite")],
        "codex-sol",
        today=TODAY,
        now=NOW,
    )
    assert result.ok and result.inflight == "inflight=3"
