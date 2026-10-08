"""task #1028 — the emitted seed against a mirror of the server contract.

handoffkeep (rev ``a69dfa8``, #1027) owns the canonical catalog. Its
``PUT /v1/bench/catalog`` decodes every row into Go ``time.Time`` fields, so
the bare ``2026-09-27`` the bundled snapshot stored for the #781 override
row was a whole-batch 400 (#955 step 2). This file mirrors that contract in
Python — the real ``--emit-seed`` output must pass it, so a bundled-snapshot
change the server would reject fails scopefuel CI before the next push.

Mirrored rules (handoffkeep a69dfa8, file:line):

- decode — ``internal/api/api.go``: the body is ``{"catalog": [...]}``
  (``benchCatalogInput`` :1383), decoded with ``DisallowUnknownFields``
  (:1420) — an unknown key at any level is ``invalid_bench_catalog_json``
  (:1422). Field types follow ``store.BenchCatalogEntry``'s json tags
  (``internal/store/store.go``:480-495): string fields take a JSON string or
  null (null decodes to the Go zero value), ``score`` is ``*float64`` (a
  JSON number or null — never bool, never out of float64 range), and
  ``decided_at``/``retired_at`` are ``time.Time``/``*time.Time`` — a JSON
  string must be strict RFC3339 (layout ``2006-01-02T15:04:05Z07:00``) or
  the decode fails. The batch is 1..1000 rows (``benchBatchValid``
  api.go:1261).
- per-row — ``Store.UpsertBenchCatalog`` (store.go:1203), in order:
  ``deviation_ref`` non-blank → ``deviation_ref_required`` (:1215);
  ``decided_by`` non-blank → ``decided_by_required`` (:1218); ``gate ""``
  becomes ``"default"`` (:1221); then ``validBenchCatalogEntry`` (:967) —
  profile/model_id/pool/decided_by non-empty ≤200 bytes NUL-free
  (``validBenchRequiredText`` :879), effort ≤200 bytes NUL-free
  (``validText`` :846), grade ∈ ``benchGradeValues`` (:877), gate ∈
  ``benchGateValues`` (:926), boundary_version/deviation_ref ≤64KiB NUL-free
  (``validBenchText`` :883, ``MaxBytes`` :26), score null or finite in
  [0,100], gate_reason/benchmark_source/benchmark_annotation null or
  ``validBenchText``; then the Sol rule (:1227) — ``codex-sol``/``kiro-sol``
  (``benchSolProfiles`` :940) must be grade S+ except a
  ``benchSolPlaceholder`` (:946): grade C with score null, the #594 E6
  "no grade claim" shape. A zero ``decided_at`` is stamped server-side
  (:1246), so absent/null is fine.
- batch — ``checkBenchCatalogMonotonicity`` (store.go:1275): within one
  profile, walking the ladder low<medium<high<xhigh<max
  (``benchEffortRanks`` :933), the grade rank (``benchGradeRank`` :950) may
  not worsen; retired rows, the ``""`` default row and unknown effort
  strings are exempt. The server evaluates the merged post-write state —
  for a full seed that state is the seed itself.

Not mirrored: operator auth (api.go:1404-1410 ``operator_required``) and
``guard.Reject``'s credential-pattern scan (store.go:1230-1241) — the fake
never authenticates and the seed carries no secret-shaped text. Go's field
matching is also case-insensitive; the mirror checks exact spellings, which
is stricter in the safe direction for a emitter that only writes lowercase.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import re

import pytest
from test_bench_backend import FakeHandoffkeep, _set_backend
from test_bench_catalog import _row, _seed_rows

from scopefuel import bench, cli

HK_REV = "a69dfa8"

# store.BenchCatalogEntry json tags (store.go:480-495).
_STRING_FIELDS = frozenset(
    {
        "profile",
        "effort",
        "model_id",
        "pool",
        "grade",
        "gate",
        "boundary_version",
        "deviation_ref",
        "decided_by",
    }
)
_OPT_STRING_FIELDS = frozenset({"gate_reason", "benchmark_source", "benchmark_annotation"})
_TIME_FIELDS = frozenset({"decided_at", "retired_at"})
_KNOWN_FIELDS = _STRING_FIELDS | _OPT_STRING_FIELDS | _TIME_FIELDS | {"score"}

_GRADE_VALUES = frozenset({"S+", "S", "A+", "A", "B", "C"})  # benchGradeValues :877
_GATE_VALUES = frozenset({"default", "escalation", "consult_only"})  # benchGateValues :926
_SOL_PROFILES = frozenset({"codex-sol", "kiro-sol"})  # benchSolProfiles :940
_EFFORT_RANKS = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}  # :933
_GRADE_RANK = {"S+": 0, "S": 1, "A+": 2, "A": 3, "B": 4}  # benchGradeRank :950 (else 5)
_MAX_BYTES = 64 << 10  # store.go:26
_BATCH_MAX = 1000  # benchBatchMax :875 / benchBatchValid api.go:1261

# Go's strict RFC3339 (2006-01-02T15:04:05Z07:00): uppercase T/Z, colonned
# numeric zone, seconds mandatory, fractional seconds optional.
_GO_RFC3339_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


def _go_rfc3339(value: object) -> bool:
    """Go ``time.Time.UnmarshalJSON``: a JSON string in strict RFC3339 only."""
    if not isinstance(value, str) or not _GO_RFC3339_RE.fullmatch(value):
        return False
    try:
        dt.datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _go_str(row: dict, field: str) -> str:
    """A Go string field after decode: null or absent is the zero value."""
    return row.get(field) or ""


def _valid_text(value: str, limit: int) -> bool:
    """validText (store.go:846): byte length, NUL-free."""
    return len(value.encode("utf-8")) <= limit and "\x00" not in value


def _valid_bench_required_text(value: str) -> bool:
    """validBenchRequiredText (store.go:879)."""
    return value != "" and _valid_text(value, 200)


def _valid_bench_text(value: str) -> bool:
    """validBenchText (store.go:883) — ≤MaxBytes, NUL-free."""
    return _valid_text(value, _MAX_BYTES)


def _decode_row(row: object) -> str | None:
    """The row-level half of ``invalid_bench_catalog_json`` (api.go:1420)."""
    if not isinstance(row, dict):
        return "row is not a JSON object"
    unknown = sorted(set(row) - _KNOWN_FIELDS)
    if unknown:
        return f"unknown field {unknown[0]!r}"
    for field in _STRING_FIELDS | _OPT_STRING_FIELDS:
        value = row.get(field)
        if value is not None and not isinstance(value, str):
            return f"{field}: not a string"
    score = row.get("score")
    if score is not None and (
        isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score)
    ):
        return "score: not a float64"
    for field in _TIME_FIELDS:
        value = row.get(field)
        if value is not None and not _go_rfc3339(value):
            return f"{field}: not strict RFC3339"
    return None


def _entry_error(row: dict) -> str | None:
    """validBenchCatalogEntry (store.go:967) on the decoded row."""
    if not (
        _valid_bench_required_text(_go_str(row, "profile"))
        and _valid_text(_go_str(row, "effort"), 200)
        and _valid_bench_required_text(_go_str(row, "model_id"))
        and _valid_bench_required_text(_go_str(row, "pool"))
        and row.get("grade") in _GRADE_VALUES
        and (_go_str(row, "gate") or "default") in _GATE_VALUES
        and _valid_bench_text(_go_str(row, "boundary_version"))
        and _valid_bench_text(_go_str(row, "deviation_ref"))
        and _valid_bench_required_text(_go_str(row, "decided_by"))
    ):
        return "invalid bench catalog entry"
    score = row.get("score")
    if score is not None and (score < 0 or score > 100):
        return "invalid bench catalog entry"
    for field in _OPT_STRING_FIELDS:
        value = row.get(field)
        if value is not None and not _valid_bench_text(value):
            return "invalid bench catalog entry"
    return None


def _sol_placeholder(row: dict) -> bool:
    """benchSolPlaceholder (store.go:946): C with score null claims no grade."""
    return row.get("grade") == "C" and row.get("score") is None


def _upsert_error(row: dict) -> str | None:
    """The per-row gates of UpsertBenchCatalog (store.go:1214-1229), in order."""
    if not _go_str(row, "deviation_ref").strip():
        return "deviation_ref_required"
    if not _go_str(row, "decided_by").strip():
        return "decided_by_required"
    if _entry_error(row) is not None:
        return "invalid bench catalog entry"
    if row.get("profile") in _SOL_PROFILES and row.get("grade") != "S+" and not _sol_placeholder(row):
        return "bench_catalog_sol_grade"
    return None


def _monotonicity_error(rows: list[dict]) -> str | None:
    """checkBenchCatalogMonotonicity (store.go:1275) on the post-write state."""
    by_profile: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("retired_at") or _go_str(row, "effort") not in _EFFORT_RANKS:
            continue
        by_profile.setdefault(_go_str(row, "profile"), []).append(row)
    for profile, entries in by_profile.items():
        entries.sort(key=lambda row: _EFFORT_RANKS[_go_str(row, "effort")])
        best = 5  # benchGradeRank("C")
        for row in entries:
            rank = _GRADE_RANK.get(row.get("grade"), 5)
            if rank > best:
                return (
                    f"bench_catalog_not_monotonic: {profile} effort "
                    f"{_go_str(row, 'effort')} grade {row.get('grade')} below lower effort"
                )
            best = rank
    return None


def _server_answer(payload: object) -> str | None:
    """The first rejection handoffkeep a69dfa8 would return, or None.

    Phases mirror benchCatalogPut → UpsertBenchCatalog: a full decode with
    unknown fields refused, then batch bounds, then per-row validation, then
    monotonicity on the merged state (the seed itself for a full seed).
    """
    if not isinstance(payload, dict) or set(payload) - {"catalog"}:
        return "invalid_bench_catalog_json"
    rows = payload.get("catalog")
    if not isinstance(rows, list):
        return "invalid_bench_catalog_json"
    for row in rows:
        error = _decode_row(row)
        if error is not None:
            return f"invalid_bench_catalog_json ({error})"
    if not 1 <= len(rows) <= _BATCH_MAX:
        return "invalid bench catalog"
    for row in rows:
        error = _upsert_error(row)
        if error is not None:
            return error
    return _monotonicity_error(rows)


def _emit_seed(capsys) -> dict:
    rc = cli.main(
        [
            "bench",
            "push-catalog",
            "--emit-seed",
            "--decided-by",
            "test-seed",
            "--deviation-ref",
            "hk:task/1028",
        ]
    )
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def _catalog_server(tmp_path, monkeypatch):
    _set_backend(tmp_path, monkeypatch)
    fake = FakeHandoffkeep()
    fake.catalog = _seed_rows()
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    bench.reset_catalog_memo()
    return fake


# --- AC1: the emitted seed's timestamps -------------------------------------


def test_every_emitted_timestamp_is_strict_rfc3339(capsys):
    """AC1 — invariant M1: "every seed timestamp is RFC3339".

    The mutant emits the date as stored (the #781 override's bare
    "2026-09-27"): the Go layout refuses it and this assertion is RED.
    """
    rows = _emit_seed(capsys)["catalog"]
    assert rows
    stamped = [
        (row["profile"], row["effort"], field, row[field])
        for row in rows
        for field in ("decided_at", "retired_at")
        if row[field] is not None
    ]
    assert stamped, "the seed should carry the #781 override's decided_at"
    for profile, effort, field, value in stamped:
        assert _go_rfc3339(value), (
            f"every seed timestamp is RFC3339: {profile}@{effort or '-'} {field}={value!r}"
        )
    override = next(row for row in rows if (row["profile"], row["effort"]) == ("devin-swe2-medium", ""))
    assert override["decided_at"] == "2026-09-27T00:00:00Z"
    # Rows without a timestamp keep the key and stay null — not "", not absent.
    assert any(row["decided_at"] is None for row in rows)
    for row in rows:
        assert "decided_at" in row and "retired_at" in row
        if row["decided_at"] is None:
            assert row["retired_at"] is None or _go_rfc3339(row["retired_at"])


# --- AC2: the mirror against the real seed -----------------------------------


def test_the_mirror_accepts_the_real_emitted_seed(capsys):
    """AC2 — invariant M2: "the mirror rejects what the server rejects".

    The mutant gives a codex-sol row score 60.0 at grade C — a real claim the
    placeholder exception does not cover — and the mirror answers
    bench_catalog_sol_grade: RED.
    """
    rejection = _server_answer(_emit_seed(capsys))
    assert rejection is None, f"the server at {HK_REV} would reject the seed: {rejection}"


# --- the mirror's own behaviour, per rule ------------------------------------


def _one_row_payload(**overrides):
    row = _row("opus", "high", "claude-opus-5-5", "claude", "A")
    row.update(overrides)
    return {"catalog": [row]}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"bogus": 1}, "invalid_bench_catalog_json (unknown field 'bogus')"),
        # The #955 step-2 failure: a bare date is not RFC3339.
        ({"decided_at": "2026-09-27"}, "invalid_bench_catalog_json (decided_at: not strict RFC3339)"),
        (
            {"decided_at": "2026-09-27T00:00:00"},
            "invalid_bench_catalog_json (decided_at: not strict RFC3339)",
        ),
        (
            {"retired_at": "2026-09-27t00:00:00z"},
            "invalid_bench_catalog_json (retired_at: not strict RFC3339)",
        ),
        ({"decided_at": 20260927}, "invalid_bench_catalog_json (decided_at: not strict RFC3339)"),
        ({"score": True}, "invalid_bench_catalog_json (score: not a float64)"),
        ({"score": 1e999}, "invalid_bench_catalog_json (score: not a float64)"),
        ({"deviation_ref": ""}, "deviation_ref_required"),
        ({"deviation_ref": None}, "deviation_ref_required"),
        ({"deviation_ref": "   "}, "deviation_ref_required"),
        ({"decided_by": ""}, "decided_by_required"),
        ({"decided_by": None}, "decided_by_required"),
        ({"grade": "D"}, "invalid bench catalog entry"),
        ({"gate": "sideload"}, "invalid bench catalog entry"),
        ({"model_id": ""}, "invalid bench catalog entry"),
        ({"pool": ""}, "invalid bench catalog entry"),
        ({"score": 100.5}, "invalid bench catalog entry"),
        ({"score": -0.5}, "invalid bench catalog entry"),
        ({"gate_reason": "x" * (_MAX_BYTES + 1)}, "invalid bench catalog entry"),
        ({"effort": "e" * 201}, "invalid bench catalog entry"),
        # The Sol rule: any non-S+ claim is refused, including a scored C.
        ({"profile": "codex-sol", "grade": "B"}, "bench_catalog_sol_grade"),
        ({"profile": "kiro-sol", "grade": "A+"}, "bench_catalog_sol_grade"),
        ({"profile": "codex-sol", "grade": "C", "score": 60.0}, "bench_catalog_sol_grade"),
    ],
)
def test_the_mirror_rejects_what_the_server_rejects(overrides, expected):
    """Each rule names itself — a row the server refuses gets its error."""
    assert _server_answer(_one_row_payload(**overrides)) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        # The #594 E6 placeholder shape: grade C with score null is no claim.
        {"profile": "codex-sol", "grade": "C", "score": None},
        {"profile": "kiro-sol", "grade": "C", "score": None},
        {"profile": "codex-sol", "grade": "S+", "score": 50.0},
        # Zero time stamps server-side (store.go:1246); null is a live row.
        {"decided_at": None, "retired_at": None},
        {"decided_at": "2026-09-27T00:00:00+09:00"},
        {"decided_at": "2026-09-27T00:00:00.123456Z"},
        {"gate": "escalation", "gate_reason": "quota floor"},
        {"effort": ""},
        {"effort": "non-reasoning"},  # an open column — exempt rungs are legal
        {"retired_at": "2026-09-29T00:00:00Z"},
    ],
    ids=[
        "sol-placeholder-codex",
        "sol-placeholder-kiro",
        "sol-placed",
        "null-times",
        "zoned-offset",
        "fractional",
        "escalation-gate",
        "default-row",
        "open-effort",
        "retired",
    ],
)
def test_the_mirror_accepts_what_the_server_accepts(overrides):
    assert _server_answer(_one_row_payload(**overrides)) is None


def test_the_mirror_checks_effort_monotonicity():
    """A higher-effort rung may not carry a worse grade (store.go:1275)."""
    worse = {
        "catalog": [
            _row("opus", "high", "claude-opus-5-5", "claude", "A"),
            _row("opus", "max", "claude-opus-5-5", "claude", "B"),
        ]
    }
    assert _server_answer(worse) == "bench_catalog_not_monotonic: opus effort max grade B below lower effort"

    # The "" default row and retired rows are exempt; unknown efforts skip.
    exempt = {
        "catalog": [
            _row("opus", "", "claude-opus-5-5", "claude", "C"),
            _row("opus", "high", "claude-opus-5-5", "claude", "A"),
            _row("opus", "max", "claude-opus-5-5", "claude", "B", retired_at="2026-09-29T00:00:00Z"),
            _row("opus", "turbo", "claude-opus-5-5", "claude", "C"),
        ]
    }
    assert _server_answer(exempt) is None


def test_the_mirror_checks_the_batch_envelope():
    assert _server_answer({"catalog": []}) == "invalid bench catalog"
    assert _server_answer({"catalog": "nope"}) == "invalid_bench_catalog_json"
    assert _server_answer({"catalog": [_row("opus", "high", "m", "claude", "A")], "x": 1}) == (
        "invalid_bench_catalog_json"
    )
    assert _server_answer({"catalog": ["nope"]}) == "invalid_bench_catalog_json (row is not a JSON object)"
    # The server only takes the {"catalog": ...} envelope — never a bare list.
    assert _server_answer([_row("opus", "high", "m", "claude", "A")]) == "invalid_bench_catalog_json"


# --- push-catalog applies the same normalization before the wire -------------


def test_push_catalog_normalizes_a_date_only_timestamp(tmp_path, monkeypatch):
    """A hand-reviewed seed file's bare date crosses the wire at day-start UTC."""
    fake = _catalog_server(tmp_path, monkeypatch)
    payload = tmp_path / "catalog.json"
    payload.write_text(
        json.dumps(
            {
                "catalog": [
                    # fresh profile — a one-rung merged ladder is always monotonic
                    _row("push-fresh", "high", "claude-opus-5-5", "claude", "A", decided_at="2026-09-27")
                ]
            }
        ),
        encoding="utf-8",
    )
    assert bench.push_catalog(payload) == 1
    sent = fake.put_bodies[-1][1]["catalog"][0]
    assert sent["decided_at"] == "2026-09-27T00:00:00Z"


def test_push_catalog_refuses_a_bad_timestamp_before_http(tmp_path, monkeypatch, capsys):
    """What Go's time.Time would 400 is a local error naming its row."""
    fake = _catalog_server(tmp_path, monkeypatch)
    payload = tmp_path / "catalog.json"
    payload.write_text(
        json.dumps(
            {"catalog": [_row("opus", "high", "claude-opus-5-5", "claude", "A", decided_at="last tuesday")]}
        ),
        encoding="utf-8",
    )
    assert cli.main(["bench", "push-catalog", str(payload)]) == 2
    err = capsys.readouterr().err
    assert "opus/high" in err
    assert "RFC3339" in err
    assert fake.hits[("PUT", "catalog")] == 0


def test_push_catalog_refuses_an_impossible_date(tmp_path, monkeypatch, capsys):
    fake = _catalog_server(tmp_path, monkeypatch)
    payload = tmp_path / "catalog.json"
    payload.write_text(
        json.dumps(
            {"catalog": [_row("opus", "high", "claude-opus-5-5", "claude", "A", retired_at="2026-13-40")]}
        ),
        encoding="utf-8",
    )
    assert cli.main(["bench", "push-catalog", str(payload)]) == 2
    err = capsys.readouterr().err
    assert "opus/high" in err and "not a real date" in err
    assert fake.hits[("PUT", "catalog")] == 0


def test_the_emitted_seed_round_trips_through_push_catalog(tmp_path, monkeypatch, capsys):
    """The whole pipeline: emit, review as a file, push — nothing to fix."""
    fake = _catalog_server(tmp_path, monkeypatch)
    seed = _emit_seed(capsys)
    payload = tmp_path / "seed.json"
    payload.write_text(json.dumps(seed), encoding="utf-8")
    assert cli.main(["bench", "push-catalog", str(payload)]) == 0
    assert fake.put_bodies[-1][1]["catalog"] == seed["catalog"]


# --- AC4: the docs pin --------------------------------------------------------


def test_docs_say_the_token_crosses_the_plaintext_hop():
    """AC4: the --allow-plaintext-http paragraph discloses the token exposure."""
    doc = pathlib.Path(__file__).parent.parent / "docs" / "catalog-server-mode.md"
    text = doc.read_text(encoding="utf-8")
    paragraphs = [p for p in text.split("\n\n") if "--allow-plaintext-http" in p]
    assert any("bearer token" in p and "plaintext" in p and "WireGuard/Tailscale" in p for p in paragraphs), (
        "the --allow-plaintext-http paragraph must say the bearer token crosses the plaintext hop"
    )
