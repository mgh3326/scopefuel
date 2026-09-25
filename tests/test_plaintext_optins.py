"""task #697 — the plaintext-http opt-in is per use, not global.

The single ``[bench] allow_plaintext_url`` flag used to flip every handoffkeep
use at once: enabling it for quota sharing also moved the bench catalog reader
to the server, and a 1-row server catalog meant ``catalog=stale`` and wrk
briefs not sent (#667). These tests pin the split — catalog / quota_share /
reps each have their own opt-in, the deprecated alias still means all three,
and with every flag off no path sends the bearer token over plaintext http.

hk is a dict-backed fake — ``bench.request_json`` and
``quota_share.request_json`` are replaced so no test reaches the network.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.parse
from collections import Counter

import pytest

from scopefuel import bench, quota_share, recommend
from scopefuel.http import HttpError
from scopefuel.model import Bucket, ProviderResult, Scope

# A private-tunnel-style endpoint: http to a non-local host — the deployment
# shape that needs the opt-in at all. Deliberately not the real tailnet
# address: if a request path ever slips past the monkeypatch it must die on
# DNS, not reach the deployed hk with a test token.
HK_URL = "http://hk.invalid:8800"
HK_TOKEN = "hk-test-token"

NOW = dt.datetime(2026, 9, 25, 12, 0, 0, tzinfo=dt.UTC)
EPOCH = NOW.timestamp()
FP_A = "fp-account-aaaa1111"

CATALOG_ROW = {
    "profile": "opus",
    "effort": "high",
    "model_id": "claude-opus-5-5",
    "pool": "claude",
    "grade": "S+",
    "score": None,
    "gate": "default",
    "gate_reason": None,
    "benchmark_source": None,
    "benchmark_annotation": None,
    "boundary_version": "2026-09-25",
    "deviation_ref": "hk:doc/test",
    "decided_at": "2026-09-25T00:00:00Z",
    "decided_by": "operator-desk",
    "retired_at": None,
}


class FakeHk:
    """One fake endpoint covering /v1/bench/<scope> and /v1/documents/<key>."""

    def __init__(self) -> None:
        self.scores: list[dict] = []
        self.reps: list[dict] = []
        self.grades: list[dict] = []
        self.catalog: list[dict] = [dict(CATALOG_ROW)]
        self.docs: dict[str, dict] = {}
        self.hits: Counter[tuple[str, str]] = Counter()

    def __call__(self, url, *, method="GET", headers=None, body=None, timeout=None, **_kw):
        assert (headers or {}).get("Authorization") == f"Bearer {HK_TOKEN}"
        url = str(url)
        assert url.startswith(HK_URL), f"request left the configured endpoint: {url}"
        path = url[len(HK_URL) :]
        if path.startswith("/v1/bench/"):
            scope = path.rsplit("/", 1)[1]
            self.hits[(method, scope)] += 1
            if method == "GET":
                return {scope: getattr(self, scope)}
            assert method == "PUT" and body is not None
            rows = body[scope]
            if scope == "reps":
                for row in rows:
                    stored = dict(row)
                    stored["id"] = len(self.reps) + 1
                    stored.setdefault("created_by", "bench-client")
                    self.reps.append(stored)
            else:
                getattr(self, scope).extend(dict(row) for row in rows)
            return {"upserted": len(rows)}
        assert path.startswith("/v1/documents/"), f"unexpected path: {path}"
        key = urllib.parse.unquote(path[len("/v1/documents/") :])
        self.hits[(method, "documents")] += 1
        if method == "PUT":
            assert isinstance(body, dict)
            self.docs[key] = dict(body)
            return {"document": {"key": key, **body}, "changed": True}
        doc = self.docs.get(key)
        if doc is None:
            raise HttpError(404, "not_found")
        return {"key": key, "kind": doc["kind"], "session": doc["session"], "body": doc["body"]}


@pytest.fixture
def hk_plaintext(tmp_path, monkeypatch):
    """Credentials for a non-local http handoffkeep plus the fake wire."""

    monkeypatch.setenv("HANDOFFKEEP_URL", HK_URL)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", HK_TOKEN)
    fake = FakeHk()
    monkeypatch.setattr(bench, "request_json", fake)
    monkeypatch.setattr(quota_share, "request_json", fake)
    config = tmp_path / "config" / "scopefuel" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    return fake, config


def _quota_result() -> ProviderResult:
    return ProviderResult(
        id="claude",
        plan="claude_max",
        buckets=[Bucket(label="5h", window="5h", used_pct=10.0, scope=Scope("account"), horizon="now")],
        source="oauth-usage-api",
        account_fp=FP_A,
        account_fp_kind="account",
    )


def _failing_fetcher(fp: str = FP_A):
    def fetch() -> ProviderResult:
        return ProviderResult(
            id="claude",
            error="HTTP 429",
            error_kind="rate_limited",
            account_fp=fp,
            account_fp_kind="account",
        )

    fetch.current_account_fp = lambda: fp  # noqa: B023
    fetch.current_account_fp_kind = lambda: "account"  # noqa: B023
    return fetch


def _add_rep() -> bench.RepRecord:
    return bench.add_rep(
        profile="builder-devin",
        model_id="swe-2",
        task_ref="697",
        tier="T2",
        role="impl",
        rounds=1,
        blockers_found=0,
        completed=1,
    )


# --- AC3: reps + quota opt-ins must not move the catalog --------------------


def test_reps_and_quota_optins_do_not_move_the_catalog(hk_plaintext):
    """The operator's target state — and the exact #667 regression guard."""

    fake, config = hk_plaintext
    config.write_text(
        "[bench]\nallow_plaintext_reps = true\nallow_plaintext_quota_share = true\n",
        encoding="utf-8",
    )

    # The catalog source stays local: no request, snapshot (not stale), and the
    # canon-adjacent surfaces stay local with it.
    view = bench.read_catalog()
    assert view.source == "snapshot"
    assert view.stale is False
    assert bench.bench_backend(use="catalog").reason == "auto-local-insecure-url"
    assert bench.runtime_grade_table() is recommend.GRADE_TABLE
    assert bench.read_grades() == []
    assert bench.read_scores() == []
    assert fake.hits[("GET", "catalog")] == 0
    assert fake.hits[("GET", "scores")] == 0
    assert fake.hits[("GET", "grades")] == 0
    status = bench.catalog_status_report()
    assert "allow_plaintext_catalog" in status

    # reps add/list go to handoffkeep.
    rep = _add_rep()
    assert rep.task_ref == "697"
    assert fake.hits[("PUT", "reps")] == 1
    assert any(row.task_ref == "697" for row in bench.read_reps())

    # quota publish/read go to handoffkeep documents.
    assert quota_share.publish_result("claude", _quota_result(), now=EPOCH) is True
    assert fake.hits[("PUT", "documents")] == 1
    remote = quota_share.remote_result(
        "claude", _failing_fetcher(), _failing_fetcher()(), "spend", now=EPOCH + 60
    )
    assert remote is not None and remote.source == quota_share.REMOTE_SOURCE
    assert fake.hits[("GET", "documents")] == 1


def test_catalog_opt_in_alone_does_not_enable_reps_or_quota(hk_plaintext):
    """The split is real in both directions — catalog is not 'all of bench'."""

    fake, config = hk_plaintext
    config.write_text("[bench]\nallow_plaintext_catalog = true\n", encoding="utf-8")

    assert bench.read_catalog().source == "server"
    assert fake.hits[("GET", "catalog")] == 1

    # reps resolve local: the write lands in sqlite, nothing reaches the wire.
    rep = _add_rep()
    assert rep.id >= 1
    assert fake.hits[("PUT", "reps")] == 0
    assert fake.hits[("GET", "reps")] == 0

    # quota share never resolves an endpoint.
    assert quota_share.publish_result("claude", _quota_result(), now=EPOCH) is False
    assert fake.hits[("PUT", "documents")] == 0


def test_reps_opt_in_alone_leaves_catalog_and_quota_local(hk_plaintext):
    """Mutant target: a catalog read honoring the reps flag must turn RED."""

    fake, config = hk_plaintext
    config.write_text("[bench]\nallow_plaintext_reps = true\n", encoding="utf-8")

    rep = _add_rep()
    assert rep.task_ref == "697"
    assert fake.hits[("PUT", "reps")] == 1

    assert bench.read_catalog().source == "snapshot"
    assert fake.hits[("GET", "catalog")] == 0
    assert quota_share.publish_result("claude", _quota_result(), now=EPOCH) is False
    assert fake.hits[("PUT", "documents")] == 0


# --- AC4: with no opt-in no use sends the token over plaintext --------------


def test_no_opt_in_means_no_plaintext_request_anywhere(hk_plaintext):
    """Every call site with every flag off: nothing reaches the wire."""

    fake, config = hk_plaintext
    config.write_text("[bench]\ncache_ttl_s = 21600\n", encoding="utf-8")

    assert bench.read_catalog().source == "snapshot"
    assert bench.read_reps() == []
    _add_rep()  # lands in local sqlite
    assert bench.read_scores() == []
    assert bench.read_grades() == []
    assert quota_share.publish_result("claude", _quota_result(), now=EPOCH) is False
    remote = quota_share.remote_result(
        "claude", _failing_fetcher(), _failing_fetcher()(), "spend", now=EPOCH + 60
    )
    assert remote is None
    assert not fake.hits


def test_explicit_handoffkeep_backend_still_gates_plaintext_per_use(hk_plaintext):
    """backend = "handoffkeep" does not bypass the scheme check: reads fail
    open, writes fail closed, and no request leaves with the matching opt-in
    off."""

    fake, config = hk_plaintext
    config.write_text('[bench]\nbackend = "handoffkeep"\nallow_plaintext_reps = true\n', encoding="utf-8")

    # reps write goes through on its own opt-in.
    _add_rep()
    assert fake.hits[("PUT", "reps")] == 1

    # The catalog read is refused at request-build time, then fails open.
    view = bench.read_catalog()
    assert view.source == "snapshot"
    assert fake.hits[("GET", "catalog")] == 0

    # push-catalog fails closed and names the catalog opt-in.
    seed = config.parent / "seed.json"
    seed.write_text(json.dumps({"catalog": [CATALOG_ROW]}), encoding="utf-8")
    with pytest.raises(bench.BenchBackendError, match="allow_plaintext_catalog"):
        bench.push_catalog(seed)


def test_reps_write_without_the_opt_in_fails_closed_and_sends_nothing(hk_plaintext):
    """Mutant target: a reps write that ignores its flag turns this RED — the
    PUT would land in fake.hits."""

    fake, config = hk_plaintext
    config.write_text('[bench]\nbackend = "handoffkeep"\n', encoding="utf-8")

    with pytest.raises(bench.BenchBackendError, match="allow_plaintext_reps"):
        _add_rep()
    assert fake.hits[("GET", "reps")] == 0
    assert fake.hits[("PUT", "reps")] == 0


def test_hk_reps_write_then_local_source_reads_on_a_cache_only_db(hk_plaintext):
    """B1 regression (#697 round 2): the mixed target state creates bench.db
    with only bench_cache_* tables — the reps write goes to handoffkeep while
    scores stay local. Every local source-table reader must still work on a
    file that has never had the source schema."""

    fake, config = hk_plaintext
    config.write_text(
        "[bench]\nallow_plaintext_reps = true\nallow_plaintext_quota_share = true\n",
        encoding="utf-8",
    )

    rep = _add_rep()  # remote write + cache stamp — creates a cache-only bench.db
    assert rep.task_ref == "697"
    assert fake.hits[("PUT", "reps")] == 1

    # Local source reads on the cache-only file must not raise.
    assert bench.read_scores() == []
    assert isinstance(bench.read_prices(), dict)
    assert isinstance(bench.read_reps(), list)  # remote read through the cache
    assert bench.read_catalog().source == "snapshot"


def _score_row() -> bench.ModelScore:
    return bench.ModelScore(
        model_id="t697-model",
        effort=None,
        harness=None,
        source="AA-model",
        metric="coding_index",
        score=61.0,
        rank=1,
        captured_at="2026-09-25T00:00:00Z",
    )


_IMPORT_TOML = (
    'source = "AA-agent"\n'
    'metric = "agentic"\n'
    'effort = "max"\n'
    'harness = "codex"\n'
    'captured_at = "2026-09-25T00:00:00Z"\n'
    "[[scores]]\n"
    'model_id = "t697-model"\n'
    "score = 61.0\n"
)

_AA_PAYLOAD = {
    "data": [
        {
            "slug": "t697-model",
            "evaluations": {"artificial_analysis_coding_index": 61.0},
        }
    ]
}


def test_catalog_writers_fail_closed_under_the_reps_only_opt_in(hk_plaintext, tmp_path):
    """Pin every catalog-use write path: with only the reps opt-in on, each
    catalog-family writer must refuse before the wire and name its own key —
    a writer resolving the wrong use turns this RED."""

    fake, config = hk_plaintext
    config.write_text('[bench]\nbackend = "handoffkeep"\nallow_plaintext_reps = true\n', encoding="utf-8")

    with pytest.raises(bench.BenchBackendError, match="allow_plaintext_catalog"):
        bench.upsert_scores([_score_row()])

    imported = tmp_path / "scores.toml"
    imported.write_text(_IMPORT_TOML, encoding="utf-8")
    with pytest.raises(bench.BenchBackendError, match="allow_plaintext_catalog"):
        bench.import_scores(imported)

    with pytest.raises(bench.BenchBackendError, match="allow_plaintext_catalog"):
        bench.sync_scores(api_key="k", request_fn=lambda *a, **k: _AA_PAYLOAD)

    with pytest.raises(bench.BenchBackendError, match="allow_plaintext_catalog"):
        bench.set_grade(profile="opus", grade="S+", deviation_ref="hk:doc/test")

    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"catalog": [CATALOG_ROW]}), encoding="utf-8")
    with pytest.raises(bench.BenchBackendError, match="allow_plaintext_catalog"):
        bench.push_catalog(seed)

    assert not fake.hits


def test_reps_write_under_the_catalog_only_opt_in_fails_closed(hk_plaintext):
    """The mirror: catalog on, reps off — a rep write must refuse naming the
    reps key and send nothing."""

    fake, config = hk_plaintext
    config.write_text('[bench]\nbackend = "handoffkeep"\nallow_plaintext_catalog = true\n', encoding="utf-8")

    with pytest.raises(bench.BenchBackendError, match="allow_plaintext_reps"):
        _add_rep()
    assert fake.hits[("GET", "reps")] == 0
    assert fake.hits[("PUT", "reps")] == 0
    # And the catalog read does go through on its own opt-in.
    assert bench.read_catalog().source == "server"
    assert fake.hits[("GET", "catalog")] == 1


def test_push_local_routes_each_scope_to_its_own_opt_in(hk_plaintext):
    """push_local resolves catalog and reps backends separately: each scope's
    rows go out only when that scope's opt-in is on."""

    fake, config = hk_plaintext
    config.write_text('[bench]\nbackend = "local"\n', encoding="utf-8")
    bench.upsert_scores([_score_row()])
    _add_rep()

    config.write_text('[bench]\nbackend = "handoffkeep"\nallow_plaintext_reps = true\n', encoding="utf-8")
    with pytest.raises(bench.BenchBackendError, match="allow_plaintext_catalog"):
        bench.push_local()
    assert fake.hits[("PUT", "scores")] == 0

    config.write_text('[bench]\nbackend = "handoffkeep"\nallow_plaintext_catalog = true\n', encoding="utf-8")
    with pytest.raises(bench.BenchBackendError, match="allow_plaintext_reps"):
        bench.push_local()
    assert fake.hits[("PUT", "reps")] == 0

    config.write_text(
        '[bench]\nbackend = "handoffkeep"\nallow_plaintext_catalog = true\nallow_plaintext_reps = true\n',
        encoding="utf-8",
    )
    assert bench.push_local() == (1, 1)
    assert fake.hits[("PUT", "scores")] == 1
    assert fake.hits[("PUT", "reps")] == 1


# --- AC1/AC4: the alias and https -------------------------------------------


def test_deprecated_alias_enables_all_uses_with_one_warning(hk_plaintext, capsys):
    """``allow_plaintext_url`` keeps its exact old behavior: every use opted
    in, one deprecation line."""

    fake, config = hk_plaintext
    config.write_text("[bench]\nallow_plaintext_url = true\n", encoding="utf-8")
    bench._WARNED_DEPRECATED_KEYS.clear()

    assert bench.read_catalog().source == "server"
    _add_rep()
    assert quota_share.publish_result("claude", _quota_result(), now=EPOCH) is True
    assert fake.hits[("GET", "catalog")] == 1
    assert fake.hits[("PUT", "reps")] == 1
    assert fake.hits[("PUT", "documents")] == 1

    err_lines = [line for line in capsys.readouterr().err.splitlines() if "allow_plaintext_url" in line]
    assert err_lines == [
        "warning: [bench] allow_plaintext_url is deprecated and enables plaintext http for "
        "all uses; prefer allow_plaintext_catalog / allow_plaintext_quota_share / allow_plaintext_reps"
    ]


def test_https_needs_no_opt_in_for_any_use(tmp_path, monkeypatch):
    """https behaves exactly as before — the flags only govern plaintext."""

    monkeypatch.setenv("HANDOFFKEEP_URL", "https://hk.invalid")
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", HK_TOKEN)

    class HttpsFake(FakeHk):
        def __call__(self, url, **kw):
            assert str(url).startswith("https://hk.invalid")
            return FakeHk.__call__(self, HK_URL + str(url)[len("https://hk.invalid") :], **kw)

    fake = HttpsFake()
    monkeypatch.setattr(bench, "request_json", fake)
    monkeypatch.setattr(quota_share, "request_json", fake)

    assert bench.read_catalog().source == "server"
    _add_rep()
    assert quota_share.publish_result("claude", _quota_result(), now=EPOCH) is True
    assert fake.hits[("GET", "catalog")] == 1
    assert fake.hits[("PUT", "reps")] == 1
    assert fake.hits[("PUT", "documents")] == 1


# --- task #713: pin the #697 r2 fixes the mutant sweep left surviving -------


def test_nonbool_per_use_key_fails_closed_and_never_falls_to_the_alias(hk_plaintext):
    """Mutant R_N2 (bench.py plaintext_opt_in): a present
    ``allow_plaintext_<use>`` wins even when its value is not a bool. Falling
    through to the alias — or testing truthiness — re-opens plaintext http for
    a key the operator set to narrow it (fail-open, catalog bearer over http)."""

    fake, config = hk_plaintext
    config.write_text(
        '[bench]\nallow_plaintext_url = true\nallow_plaintext_catalog = "false"\n',
        encoding="utf-8",
    )

    assert bench.read_catalog().source == "snapshot"
    assert fake.hits[("GET", "catalog")] == 0


def test_plaintext_opt_in_values_must_be_the_bool_true():
    """Same pin at unit level: only the literal TOML ``true`` opts a use in —
    truthy strings fail closed, on the per-use key and on the alias alike."""

    assert bench.plaintext_opt_in({"allow_plaintext_catalog": "yes"}, "catalog") is False
    assert (
        bench.plaintext_opt_in({"allow_plaintext_url": True, "allow_plaintext_catalog": 1}, "catalog")
        is False
    )
    assert bench.plaintext_opt_in({"allow_plaintext_url": "yes"}, "quota_share") is False
    assert bench.plaintext_opt_in({"allow_plaintext_url": 1}, "reps") is False
    assert bench.plaintext_opt_in({"allow_plaintext_reps": True}, "reps") is True


def test_alias_warning_fires_only_when_the_alias_is_true(hk_plaintext, capsys):
    """Mutant N3: a warning keyed on key *presence* (or unconditional) nags the
    operator who explicitly set ``allow_plaintext_url = false``."""

    fake, config = hk_plaintext
    config.write_text(
        "[bench]\nallow_plaintext_url = false\nallow_plaintext_catalog = true\n",
        encoding="utf-8",
    )
    bench._WARNED_DEPRECATED_KEYS.clear()

    assert bench.read_catalog().source == "server"
    assert "allow_plaintext_url" not in capsys.readouterr().err


def test_backend_resolved_for_one_use_cannot_serve_another_scope():
    """Mutant N1 (bench.py _backend_url): drop the scope/use guard and a
    backend resolved under the reps opt-in silently serves the catalog scopes —
    one opt-in covering a use it was never granted."""

    backend = bench.BenchBackend(
        name=bench.BENCH_BACKEND_HANDOFFKEEP,
        cache_ttl_s=60.0,
        url=HK_URL,
        token=HK_TOKEN,
        endpoint_id="e",
        allow_plaintext_url=True,
        plaintext_use="reps",
    )
    for scope in ("catalog", "scores", "grades"):
        with pytest.raises(bench.BenchBackendError, match="cannot serve scope"):
            bench._backend_url(backend, scope)
    assert bench._backend_url(backend, "reps").endswith("/v1/bench/reps")


def test_runtime_grade_table_uses_the_catalog_opt_in(hk_plaintext):
    """Mutant M15 (bench.py runtime_grade_table): resolving ``use="reps"``
    instead of ``use="catalog"`` means a catalog-only host silently keeps the
    code table and never reads the server canon it opted into."""

    fake, config = hk_plaintext
    config.write_text("[bench]\nallow_plaintext_catalog = true\n", encoding="utf-8")

    table = bench.runtime_grade_table()
    assert fake.hits[("GET", "catalog")] == 1
    assert table is not recommend.GRADE_TABLE
    assert any(profile.name == "opus" for profile in table["S+"])


def test_per_use_false_wins_over_the_alias_for_reps(hk_plaintext):
    """Mirror of the catalog pin in test_bench_catalog: alias on + reps
    explicitly off → a rep write fails closed while the catalog still reads
    from the server on the alias."""

    fake, config = hk_plaintext
    config.write_text(
        '[bench]\nbackend = "handoffkeep"\nallow_plaintext_url = true\nallow_plaintext_reps = false\n',
        encoding="utf-8",
    )

    with pytest.raises(bench.BenchBackendError, match="allow_plaintext_reps"):
        _add_rep()
    assert fake.hits[("PUT", "reps")] == 0
    assert fake.hits[("GET", "reps")] == 0
    assert bench.read_catalog().source == "server"
