"""hk 1753 (b953): Fusion GPT rows gate on the declared Devin-linked codex account.

The Devin-linked ChatGPT account is the billing meter for devin-fusion-sol61 /
-xhigh — not the host's codex login. A matching host (local codex account
fingerprint == declared) judges on its local measurement; any other host reads
``quota/codex/<declared fp>/latest`` through quota_share, never its own codex
pool. No declaration, or a missing/stale/unreadable document, is the named
unmeasurable refusal ``fusion_billing_unmeasured`` (rc 4).

뮤턴트 계약(각 테스트 주석에 'M#' 표기):
- M1  sol61 이 선언 계정이 아닌 codex 계정으로 게이트 → 비매칭 호스트 테스트가 RED
- M2  선언 계정 미측정이 sol61 을 통과 → rc 4 테스트가 RED
- M3  비-Fusion codex 프로필이 선언 문서를 읽기 → codex-sol 테스트가 RED
- M4  지문 일치 우선(로컬 측정 사용) 제거 → 매칭 호스트 테스트가 RED
- M5  free-limit hold 가 billing 판정 뒤에도 적용되지 않기 → hold 테스트가 RED
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import urllib.parse

import pytest

from scopefuel import bench, cache, cli, free_window, launch, policy, quota_share
from scopefuel.free_window import FREE_PROFILES
from scopefuel.http import HttpError
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.providers import codex
from scopefuel.recommend import (
    FUSION_BILLING_PROFILES,
    FUSION_BILLING_UNMEASURED,
    GRADE_TABLE,
    recommend_dict,
)

PROFILES = ("devin-fusion-sol61", "devin-fusion-sol61-xhigh")
NOW = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)
EPOCH = NOW.timestamp()
RESET_5H = "2026-10-20T17:00:00Z"
RESET_7D = "2026-10-27T12:00:00Z"
LOCAL_FP = "aa11bb22cc33dd44"  # this host's own codex account fingerprint
DECLARED_FP = "c0605596eeff0011"  # the declared Devin-linked billing account
HK_URL = "https://hk.invalid"
MEASURING_HOST = "mbp-server"


class FrozenDateTime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


class FakeHk:
    """PUT/GET /v1/documents/{key} 를 dict 로 흉내 낸다 (test_quota_share 와 같은 계약)."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.puts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    def __call__(self, url, *, method="GET", headers=None, body=None, timeout=None, **_kw):
        assert (headers or {}).get("Authorization") == "Bearer hk-test-token"
        assert str(url).startswith(f"{HK_URL}/v1/documents/")
        key = urllib.parse.unquote(str(url).split("/v1/documents/", 1)[1])
        if method == "PUT":
            assert isinstance(body, dict)
            self.docs[key] = dict(body)
            self.puts.append((key, dict(body)))
            return {"document": {"key": key, **body}, "changed": True}
        assert method == "GET"
        self.gets.append(key)
        doc = self.docs.get(key)
        if doc is None:
            raise HttpError(404, "not_found")
        return {"key": key, "kind": doc["kind"], "session": doc["session"], "body": doc["body"]}


def _enable(monkeypatch) -> FakeHk:
    hk = FakeHk()
    monkeypatch.setenv("HANDOFFKEEP_URL", HK_URL)
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", "hk-test-token")
    monkeypatch.setattr(quota_share, "request_json", hk)
    return hk


def _seed_codex_remote(
    hk: FakeHk,
    *,
    fp: str = DECLARED_FP,
    measured_at: float = EPOCH,
    host: str = MEASURING_HOST,
    used_5h: float = 10.0,
    used_7d: float = 30.0,
    account_label: str | None = None,
) -> str:
    """게시된 형태 그대로의 codex 스냅샷 문서를 fake 에 심는다."""
    key = quota_share.key_for("codex", fp)
    measured_by = {
        "host": host,
        "session_fp": "sess-mbp",
        "account_fp": fp,
        "account_label": account_label,
    }

    def _entry(label, window, horizon, used, resets_at):
        return {
            "label": label,
            "window": window,
            "horizon": horizon,
            "used_pct": used,
            "resets_at": resets_at,
            "scope": {"kind": "account", "name": None},
            "note": None,
            "measured_by": dict(measured_by),
        }

    body = json.dumps(
        {
            "schema": quota_share.SCHEMA,
            "pool": "codex",
            "account_fp": fp,
            "measured_at": dt.datetime.fromtimestamp(measured_at, dt.UTC).isoformat(),
            "measured_at_epoch": measured_at,
            "measured_by": measured_by,
            "source": "wham-usage-api",
            "plan": "chatgpt-plus",
            "buckets": [
                _entry("5h", "5h", "now", used_5h, RESET_5H),
                _entry("7d", "7d", "week", used_7d, RESET_7D),
            ],
        }
    )
    hk.docs[key] = {
        "key": key,
        "kind": quota_share.DOC_KIND,
        "session": quota_share.DOC_SESSION,
        "job": "",
        "body": body,
    }
    return key


def _codex_result(used_5h: float = 10.0, used_7d: float = 30.0, fp: str = LOCAL_FP) -> ProviderResult:
    """로컬 codex 측정 — 지문은 이 호스트의 계정(=LOCAL_FP), 매칭 테스트만 DECLARED_FP."""
    return ProviderResult(
        id="codex",
        pool_class="spend",
        buckets=[
            Bucket("5h", "5h", used_5h, RESET_5H, Scope("account"), "now"),
            Bucket("7d", "7d", used_7d, RESET_7D, Scope("account"), "week"),
        ],
        source="wham-usage-api",
        publish_fp=fp,
        publish_fp_kind="account",
    )


def _declare(fp: str = DECLARED_FP) -> None:
    """기존 policy set 경로로 선언 청구 계정을 심는다 — config 직접 편집 아님."""
    policy.set_policy("codex", None, fusion_billing_fp=fp)


def _install_cli_stubs(monkeypatch, providers):
    monkeypatch.setattr(cli, "registry", lambda: {"codex": object(), "devin": object()})
    monkeypatch.setattr(cli, "collect", lambda *args, **kwargs: providers)
    monkeypatch.setattr(cli.bench, "read_scores", lambda: [])
    monkeypatch.setattr(cli.bench, "read_prices", lambda: {})
    monkeypatch.setattr(cli.bench, "runtime_grade_table", lambda: GRADE_TABLE)
    monkeypatch.setattr(
        cli.bench,
        "read_catalog",
        lambda: bench.CatalogView(entries=launch.snapshot_entries(), source="snapshot"),
    )
    monkeypatch.setattr(cli, "_shadow_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_emit_stale_build_warning", lambda: None)


def _isolate_free_window(tmp_path, monkeypatch):
    monkeypatch.setattr(free_window, "state_path", lambda: tmp_path / "free-limit.json")
    monkeypatch.setattr(free_window, "lock_path", lambda: tmp_path / "free-limit.lock")
    monkeypatch.setattr(free_window.cache, "_load", lambda: {})


def _capture_cli(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = cli.main(argv)
    return rc, stdout.getvalue(), stderr.getvalue()


def _observe_sol61_hold():
    rc, out, err = _capture_cli(
        [
            "observe",
            "devin-free-limit",
            "--reset",
            (NOW + dt.timedelta(minutes=45)).isoformat(),
            "--observed",
            (NOW - dt.timedelta(minutes=5)).isoformat(),
            "--rung",
            "devin-fusion-sol61",
            "--source",
            "job:hk1753-sol61",
        ]
    )
    assert rc == 0, (out, err)


# ------------------------------------------------------------ membership


def test_billing_profile_membership_is_exactly_the_two_sol61_rows():
    """b953 경계 — 이 집합 밖의 codex 프로필은 선언 계정을 절대 읽지 않는다."""
    assert frozenset(PROFILES) == FUSION_BILLING_PROFILES
    assert set(PROFILES) <= FREE_PROFILES


# ------------------------------------------------------------ non-matching host


def test_nonmatching_host_gates_sol61_on_declared_doc_not_local_pool(monkeypatch):
    """M1 — 로컬 codex 가 74%(무료 플랜 미터)여도 선언 계정 문서(30%)로 판정."""
    hk = _enable(monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch, [_codex_result(used_5h=74.0, used_7d=74.0)])
    _declare()
    _seed_codex_remote(hk, used_5h=30.0, used_7d=30.0)

    rc, out, err = _capture_cli(["gate", "-m", "devin-fusion-sol61"])
    assert rc == 0, (out, err)
    # 사용률은 선언 문서의 30 — 로컬 74 가 아니다.
    assert "used_pct=30.0" in out
    assert f"quota/codex/{DECLARED_FP}/latest" in hk.gets
    # provenance: remote 소스와 선언 계정 태그(fp8)가 나간다.
    assert "source=remote" in out
    assert DECLARED_FP[:8] in out
    assert LOCAL_FP[:8] not in out


def test_nonmatching_host_refuses_on_declared_doc_even_with_empty_local_pool(monkeypatch):
    """로컬 codex 가 0% 여도 선언 계정 문서 소진으로 거부 — 로컬 풀로 절대 폴백 없음."""
    hk = _enable(monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch, [_codex_result(used_5h=0.0, used_7d=0.0)])
    _declare()
    _seed_codex_remote(hk, used_5h=100.0, used_7d=100.0)

    for name in PROFILES:
        rc, out, err = _capture_cli(["gate", "-m", name])
        assert rc == 3, (name, out, err)
        assert FUSION_BILLING_UNMEASURED not in err
        assert DECLARED_FP[:8] in err


# ------------------------------------------------------------ matching host


def test_matching_host_uses_local_measurement_and_never_reads_the_doc(monkeypatch):
    """M4 — 로컬 지문 == 선언 지문이면 로컬 측정 그대로, hk GET 도 하지 않는다."""
    hk = _enable(monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch, [_codex_result(used_5h=42.0, used_7d=42.0, fp=DECLARED_FP)])
    _declare()
    # A contradictory doc exists (full) — the matching host must not read it.
    _seed_codex_remote(hk, used_5h=100.0, used_7d=100.0)

    rc, out, err = _capture_cli(["gate", "-m", "devin-fusion-sol61"])
    assert rc == 0, (out, err)
    assert "used_pct=42.0" in out
    assert f"quota/codex/{DECLARED_FP}/latest" not in hk.gets
    assert "source=remote" not in out
    assert DECLARED_FP[:8] in out


# ------------------------------------------------------------ unmeasured (rc 4)


def test_no_declaration_is_named_unmeasurable(monkeypatch):
    """M2 — 선언 없음 → rc 4, fusion_billing_unmeasured. 로컬 풀로 폴백하지 않는다."""
    _enable(monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch, [_codex_result()])

    for name in PROFILES:
        rc, out, err = _capture_cli(["gate", "-m", name])
        assert rc == 4, (name, out, err)
        assert FUSION_BILLING_UNMEASURED in err
        assert "fusion_billing_fp" in err


def test_missing_declared_doc_is_named_unmeasurable(monkeypatch):
    """M2 — 문서 부재 → rc 4, 사유가 계정 태그(fp8)를 이름 붙인다."""
    _enable(monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch, [_codex_result()])
    _declare()

    rc, out, err = _capture_cli(["gate", "-m", "devin-fusion-sol61"])
    assert rc == 4, (out, err)
    assert FUSION_BILLING_UNMEASURED in err
    assert DECLARED_FP[:8] in err


def test_stale_declared_doc_is_named_unmeasurable_with_host(monkeypatch):
    """M2 — 15분 초과 문서 → rc 4, 사유가 계정 태그와 측정 호스트를 이름 붙인다."""
    hk = _enable(monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch, [_codex_result()])
    _declare()
    _seed_codex_remote(hk, measured_at=EPOCH - quota_share.REMOTE_MAX_AGE_S - 1, used_5h=5.0, used_7d=5.0)

    rc, out, err = _capture_cli(["gate", "-m", "devin-fusion-sol61"])
    assert rc == 4, (out, err)
    assert FUSION_BILLING_UNMEASURED in err
    assert DECLARED_FP[:8] in err
    assert MEASURING_HOST in err


def test_unreadable_declared_doc_is_named_unmeasurable(monkeypatch):
    """M2 — 검증 불가 문서(깨진 body)도 같은 거부다."""
    hk = _enable(monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch, [_codex_result()])
    _declare()
    hk.docs[quota_share.key_for("codex", DECLARED_FP)] = {
        "key": quota_share.key_for("codex", DECLARED_FP),
        "kind": quota_share.DOC_KIND,
        "session": quota_share.DOC_SESSION,
        "job": "",
        "body": "{not json",
    }

    rc, out, err = _capture_cli(["gate", "-m", "devin-fusion-sol61"])
    assert rc == 4, (out, err)
    assert FUSION_BILLING_UNMEASURED in err
    assert DECLARED_FP[:8] in err


# ------------------------------------------------------------ non-Fusion rows


def test_codex_sol_on_nonmatching_host_still_reads_local_pool(monkeypatch):
    """M3 — codex-sol 은 로컬 codex 풀 그대로. 선언 문서는 절대 읽지 않는다."""
    hk = _enable(monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch, [_codex_result(used_5h=30.0, used_7d=30.0)])
    _declare()
    _seed_codex_remote(hk, used_5h=100.0, used_7d=100.0)  # declared account is full

    rc, out, err = _capture_cli(["gate", "-m", "codex-sol"])
    assert rc == 0, (out, err)
    assert "used_pct=30.0" in out
    assert f"quota/codex/{DECLARED_FP}/latest" not in hk.gets


def test_codex_sol_local_failure_never_falls_to_declared_doc(monkeypatch):
    """M3 — 로컬 측정 불가여도 codex-sol 은 선언 문서로 대체되지 않는다."""
    hk = _enable(monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    failing = ProviderResult(id="codex", error="HTTP 500", error_kind="server")
    _install_cli_stubs(monkeypatch, [failing])
    _declare()
    _seed_codex_remote(hk, used_5h=5.0, used_7d=5.0)

    rc, out, err = _capture_cli(["gate", "-m", "codex-sol"])
    assert rc == 4, (out, err)
    assert FUSION_BILLING_UNMEASURED not in err
    assert f"quota/codex/{DECLARED_FP}/latest" not in hk.gets


# ------------------------------------------------------------ recommend rows


def _table_for_sol61():
    profiles = {
        profile.name: profile for rows in GRADE_TABLE.values() for profile in rows if profile.name in PROFILES
    }
    assert set(profiles) == set(PROFILES)
    return {grade: (list(profiles.values()) if grade == "B" else []) for grade in GRADE_TABLE}


def test_recommend_rows_read_declared_account_on_nonmatching_host(monkeypatch):
    """recommend 도 같은 계정을 본다 — 선언 문서 30% 로 후보, 로컬 74% 는 무관."""
    hk = _enable(monkeypatch)
    _declare()
    _seed_codex_remote(hk, used_5h=10.0, used_7d=30.0)
    payload = recommend_dict(
        [_codex_result(used_7d=74.0)],
        "B",
        today=NOW.date(),
        now=NOW,
        grade_table=_table_for_sol61(),
    )
    rows = {row["profile"]: row for row in payload["rows"]}
    assert set(rows) == set(PROFILES)
    assert all(row["kind"] == "candidate" for row in rows.values())
    assert all(row["provider"] == "codex" for row in rows.values())
    # 두 형제 행이 한 문서 읽기를 공유한다 (memo 제거 뮤턴트는 이 assert 를 죽인다).
    assert hk.gets.count(f"quota/codex/{DECLARED_FP}/latest") == 1


def test_recommend_rows_are_named_unmeasurable_without_declared_account(monkeypatch):
    """M2 — 선언/문서가 없으면 추천 행도 같은 이름의 측정불가로 접힌다."""
    _enable(monkeypatch)
    _declare()
    payload = recommend_dict(
        [_codex_result()],
        "B",
        today=NOW.date(),
        now=NOW,
        grade_table=_table_for_sol61(),
    )
    rows = {row["profile"]: row for row in payload["rows"]}
    assert set(rows) == set(PROFILES)
    assert all(row["kind"] == "unmeasurable" for row in rows.values())
    assert all(FUSION_BILLING_UNMEASURED in row["reason"] for row in rows.values())


# ------------------------------------------------------------ free-limit hold


def test_free_limit_hold_still_refuses_sol61_after_billing_admission(monkeypatch, tmp_path):
    """M5 — 선언 계정 문서가 통과시킨 뒤에도 free-limit hold 가 그대로 거부한다."""
    _isolate_free_window(tmp_path, monkeypatch)
    hk = _enable(monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch, [_codex_result(used_7d=74.0)])
    _declare()
    _seed_codex_remote(hk, used_5h=10.0, used_7d=30.0)
    _observe_sol61_hold()

    for name in PROFILES:
        rc, _out, err = _capture_cli(["gate", "-m", name])
        assert rc == 3, (name, err)
        assert "free_rate_limited" in err, (name, err)


# ------------------------------------------------------------ policy set path


def test_policy_set_writes_and_clears_fusion_billing_fp(monkeypatch, tmp_path):
    """기존 policy set 경로 — CLI 가 키를 쓰고 'none' 이 지운다."""
    monkeypatch.setattr(cli, "registry", lambda: {"codex": object()})

    rc, out, err = _capture_cli(["policy", "set", "codex", "--fusion-billing-fp", DECLARED_FP])
    assert rc == 0, (out, err)
    assert policy.get_fusion_billing_fp("codex") == DECLARED_FP

    rc, out, err = _capture_cli(["policy", "set", "codex", "--fusion-billing-fp", "none"])
    assert rc == 0, (out, err)
    assert policy.get_fusion_billing_fp("codex") is None


def test_policy_set_rejects_non_fingerprint_values():
    """지문 형태가 아닌 값(공백·토큰·이메일 등)은 쓰지 않는다 — 지문만 허용."""
    with pytest.raises(ValueError):
        policy.set_policy("codex", None, fusion_billing_fp="has space/token@like")
    with pytest.raises(ValueError):
        policy.set_policy("codex", None, fusion_billing_fp="../escape")


def test_policy_set_rejects_jwt_shaped_and_uppercase_values():
    """D5 — 지문은 provider 가 내는 형태(16자 소문자 hex)뿐이다."""
    with pytest.raises(ValueError):
        policy.set_policy("codex", None, fusion_billing_fp="aaaaaaaa.bbbbbbbb.cccccccc")
    with pytest.raises(ValueError):
        policy.set_policy("codex", None, fusion_billing_fp=DECLARED_FP.upper())
    # 정상 hex 지문은 통과.
    policy.set_policy("codex", None, fusion_billing_fp=DECLARED_FP)
    assert policy.get_fusion_billing_fp("codex") == DECLARED_FP


def test_policy_set_none_removal_is_case_and_space_tolerant(monkeypatch):
    """D5 — 'NONE'·' none ' 도 키 제거다(지문으로 저장되지 않는다)."""
    monkeypatch.setattr(cli, "registry", lambda: {"codex": object()})
    for spelling in ("NONE", " none ", "None"):
        policy.set_policy("codex", None, fusion_billing_fp=DECLARED_FP)
        rc, out, err = _capture_cli(["policy", "set", "codex", "--fusion-billing-fp", spelling])
        assert rc == 0, (spelling, out, err)
        assert policy.get_fusion_billing_fp("codex") is None


# ------------------------------------------------------------ tester round-2 regressions


def test_d3_path_unmeasured_declared_account_does_not_crash(monkeypatch):
    """D1 회귀 — runtime table 이 sol61 을 빼도(D3 경로) crash 없이 rc 4."""
    _enable(monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch, [_codex_result()])
    monkeypatch.setattr(
        cli.bench,
        "runtime_grade_table",
        lambda: {grade: [p for p in rows if p.name not in PROFILES] for grade, rows in GRADE_TABLE.items()},
    )
    _declare()  # doc is missing → unmeasured D3 path (fields + audit splat)

    for name in PROFILES:
        rc, out, err = _capture_cli(["gate", "-m", name])
        assert rc == 4, (name, out, err)
        assert FUSION_BILLING_UNMEASURED in err
        assert "Traceback" not in err


def test_fusion_verdict_provenance_not_overwritten_by_host_remote_result(monkeypatch):
    """D3 회귀 — 호스트 자체 remote 결과가 Fusion 판정 provenance 를 덮지 않는다."""
    hk = _enable(monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    host_remote = ProviderResult(
        id="codex",
        pool_class="spend",
        source=quota_share.REMOTE_SOURCE,
        note="remote measured (other-host) · account aa11bb22",
        fetched_at=EPOCH - 600,
        age_s=600.0,
        buckets=[Bucket("7d", "7d", 74.0, RESET_7D, Scope("account"), "week")],
        publish_fp=LOCAL_FP,
        publish_fp_kind="account",
    )
    _install_cli_stubs(monkeypatch, [host_remote])
    _declare()
    _seed_codex_remote(hk, used_5h=10.0, used_7d=30.0)

    rc, out, err = _capture_cli(["gate", "-m", "devin-fusion-sol61"])
    assert rc == 0, (out, err)
    assert MEASURING_HOST in out
    assert "other-host" not in out


def test_recommend_memoized_refusal_names_each_sibling_row(monkeypatch):
    """D4 회귀 — 같은 문서를 공유해도 각 행 사유는 자기 프로필을 이름 붙인다."""
    _enable(monkeypatch)
    _declare()  # no doc → both rows fold to the same memoized resolution
    payload = recommend_dict(
        [_codex_result()],
        "B",
        today=NOW.date(),
        now=NOW,
        grade_table=_table_for_sol61(),
    )
    rows = {row["profile"]: row for row in payload["rows"]}
    assert set(rows) == set(PROFILES)
    for name in PROFILES:
        assert f"{FUSION_BILLING_UNMEASURED}: {name} pool=codex" in rows[name]["reason"]


def test_codex_fetch_survives_corrupt_auth_json(tmp_path, monkeypatch):
    """D6 회귀 — 깨진 auth.json(비-dict 최상위·비문자열 account_id)도 crash 없음."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text("[]")
    result = codex.fetch()
    assert result.error and result.publish_fp is None
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"account_id": 123, "access_token": "tok"}}))
    monkeypatch.setattr(codex, "request_json", lambda *a, **k: {"rate_limit": {}})
    result = codex.fetch()
    assert result.publish_fp_kind == "token"  # non-str account_id fell to token hash


def test_codex_result_does_not_open_generic_fp_gates(monkeypatch):
    """D2 — codex 결과는 게시만 하고 로컬 지문 게이트는 그대로 닫혀 있다.

    account_fp 를 싣지 않으므로 stale 수용 일치(account_fp_match)와 같은-계정
    원격 폴백(remote_eligible → _current_identity)이 codex 에 대해 활성화되지
    않는다 — 비-Fusion codex 프로필의 호스트-로컬 판정이 byte-identical 한 근거.
    """
    local = _codex_result()
    assert local.account_fp is None
    assert local.account_fp_match is None
    assert local.publish_fp == LOCAL_FP
    # 게시 지문 해석은 publish_fp 우선 — codex 결과도 게시된다.
    hk = _enable(monkeypatch)
    assert quota_share.publish_result("codex", local, now=EPOCH)
    assert quota_share.key_for("codex", LOCAL_FP) in hk.docs


def test_cache_roundtrip_preserves_publish_fp_for_matching_host():
    """D7 회귀 — TTL 캐시 히트도 매칭 호스트 판정(publish_fp)을 잃지 않는다."""
    restored = cache._from_entry(cache._to_entry(_codex_result(fp=DECLARED_FP), EPOCH), "codex", EPOCH)
    assert restored.publish_fp == DECLARED_FP
    assert restored.publish_fp_kind == "account"


def test_matching_host_still_local_on_cache_hit_with_share_off(monkeypatch):
    """D7 회귀(통합) — 캐시된 publish_fp 결과로 SCOPEFUEL_QUOTA_SHARE=off 도 로컬 판정."""
    monkeypatch.setenv("SCOPEFUEL_QUOTA_SHARE", "off")
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    cached = cache._from_entry(
        cache._to_entry(_codex_result(used_5h=42.0, used_7d=42.0, fp=DECLARED_FP), EPOCH),
        "codex",
        EPOCH,
    )
    cached.stale = False  # TTL-hit path (cache.py collect) marks fresh hits this way
    _install_cli_stubs(monkeypatch, [cached])
    _declare()

    rc, out, err = _capture_cli(["gate", "-m", "devin-fusion-sol61"])
    assert rc == 0, (out, err)
    assert "used_pct=42.0" in out


def test_declared_doc_with_wrong_body_fp_is_refused(monkeypatch):
    """N3 뮤턴트 — 키는 선언 지문이어도 본문 지문이 다르면 읽지 않는다."""
    hk = _enable(monkeypatch)
    monkeypatch.setattr(cli.dt, "datetime", FrozenDateTime)
    _install_cli_stubs(monkeypatch, [_codex_result()])
    _declare()
    _seed_codex_remote(hk, fp=LOCAL_FP)  # body fp = LOCAL; move under declared key
    hk.docs[quota_share.key_for("codex", DECLARED_FP)] = hk.docs.pop(quota_share.key_for("codex", LOCAL_FP))

    rc, out, err = _capture_cli(["gate", "-m", "devin-fusion-sol61"])
    assert rc == 4, (out, err)
    assert FUSION_BILLING_UNMEASURED in err
