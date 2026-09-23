"""task #576 — usage API 429 구분 · 6h stale_accepted · backoff · --no-cache 부분 병합.

전부 fixture(ProviderResult/직접 fetcher)와 고정 시계(now= 파라미터)로 돌린다 —
실 usage API 호출 없음. 캐시·backoff·manual 파일은 conftest 의 tmp 격리를 쓴다.
"""

from __future__ import annotations

import datetime as dt
import json
import time

import pytest

from scopefuel import cache, cli, refresh
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import gate_check, recommend

TODAY = dt.date(2026, 7, 31)
NOW = dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=dt.UTC)
EPOCH = NOW.timestamp()
FP_A = "fp-account-a"
FP_B = "fp-account-b"


def _bucket(
    window: str,
    used: float,
    *,
    hours_ahead: float | None = 4.0,
    base: dt.datetime = NOW,
) -> Bucket:
    resets = (base + dt.timedelta(hours=hours_ahead)).isoformat() if hours_ahead is not None else None
    return Bucket(
        label=window,
        window=window,
        used_pct=used,
        resets_at=resets,
        scope=Scope("account"),
        horizon="now" if window == "5h" else "week",
    )


def _claude_ok(fp: str | None = FP_A, *, base: dt.datetime = NOW) -> ProviderResult:
    return ProviderResult(
        id="claude",
        plan="claude_pro",
        buckets=[
            _bucket("5h", 10.0, base=base),
            _bucket("7d", 30.0, hours_ahead=160.0, base=base),
        ],
        source="oauth-usage-api",
        http_status=200,
        account_fp=fp,
    )


def _claude_ok_real(fp: str | None = FP_A) -> ProviderResult:
    """cli.main 같은 실시간 경로용 — reset 을 실제 시계 기준으로 잡는다."""
    return _claude_ok(fp, base=dt.datetime.now(dt.UTC))


def _failing(
    error: str,
    *,
    kind: str,
    status: int | None = None,
    retry_after: float | None = None,
    fp: str | None = FP_A,
):
    """claude.fetch 와 같은 형태 — 분류된 실패 ProviderResult 를 돌려주는 fetcher."""

    def fetch() -> ProviderResult:
        return ProviderResult(
            id="claude",
            error=error,
            error_kind=kind,
            http_status=status,
            retry_after_s=retry_after,
            account_fp=fp,
        )

    fetch.current_account_fp = lambda: fp  # noqa: B023 — 로컬 전용 probe
    return fetch


def _seed(fp: str | None = FP_A, *, now: float = EPOCH - 600.0, result: ProviderResult | None = None) -> None:
    cache.collect({"claude": lambda: result or _claude_ok(fp)}, ["claude"], now=now)


def _rate_limited_collect(**kwargs):
    return cache.collect(
        {"claude": _failing("HTTP 429: rate limit", kind="rate_limited", status=429, **kwargs)},
        ["claude"],
        now=EPOCH,
        use_cache=False,
    )


def _read_entries() -> dict:
    return json.loads(cache.cache_path().read_text(encoding="utf-8"))


# ---------------------------------------------------------------- AC2: 부분 병합 + 보존


def test_no_cache_partial_merge_preserves_failed_and_other_provider():
    """뮤턴트 a: --no-cache 실패가 이전 정상 스냅샷·다른 provider 항목을 지우면 실패."""
    other = ProviderResult(id="grok", buckets=[_bucket("7d", 5.0, hours_ahead=160.0)])
    cache.collect(
        {"claude": lambda: _claude_ok(), "grok": lambda: other},
        ["claude", "grok"],
        now=EPOCH - 600,
    )
    before = _read_entries()

    results = _rate_limited_collect()
    stale = results[0]
    assert stale.stale is True
    assert stale.status == "stale"
    assert stale.account_fp_match is True
    assert stale.error_kind == "rate_limited"
    assert "속도 제한" in stale.note

    after = _read_entries()
    # 실패한 claude 의 정상 스냅샷과 이번 호출에 없던 grok 항목이 그대로 남는다.
    assert after["claude"]["result"] == before["claude"]["result"]
    assert after["claude"]["fetched_at"] == before["claude"]["fetched_at"]
    assert after["grok"]["result"] == before["grok"]["result"]
    # 감사 필드는 별도로 갱신된다.
    assert after["claude"]["last_error_kind"] == "rate_limited"
    assert after["claude"]["last_http_status"] == 429
    assert after["claude"]["last_error_at"] == EPOCH


def test_no_cache_success_updates_only_that_provider():
    """--no-cache 성공분만 덮어쓴다 — 다른 provider 엔트리는 유지."""
    other = ProviderResult(id="grok", buckets=[_bucket("7d", 5.0, hours_ahead=160.0)])
    cache.collect(
        {"claude": lambda: _claude_ok(), "grok": lambda: other},
        ["claude", "grok"],
        now=EPOCH - 600,
    )
    newer = _claude_ok()
    cache.collect({"claude": lambda: newer}, ["claude"], now=EPOCH, use_cache=False)
    after = _read_entries()
    assert after["claude"]["fetched_at"] == EPOCH
    assert after["grok"]["fetched_at"] == EPOCH - 600  # 미호출 provider 유지


# ---------------------------------------------------------------- AC1+AC2: 게이트 수용


def test_gate_stale_accepted_on_429_with_age_and_phrasing():
    _seed()
    results = _rate_limited_collect()
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is True
    assert res.stale_accepted is True
    assert "stale_accepted" in res.reason
    assert "속도 제한" in res.reason
    assert "10분 전" in res.reason  # 나이 표시


@pytest.mark.parametrize(
    ("kind", "status", "error"),
    [
        ("server", 503, "HTTP 503: upstream"),
        ("network", None, "urlopen error: connection refused"),
    ],
)
def test_gate_stale_accepted_on_5xx_and_network(kind, status, error):
    _seed()
    results = cache.collect(
        {"claude": _failing(error, kind=kind, status=status)},
        ["claude"],
        now=EPOCH,
        use_cache=False,
    )
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is True
    assert res.stale_accepted is True


def test_gate_stale_accepted_via_d3_path():
    """GRADE_TABLE 에 없지만 profile_pool 이 아는 프로필도 같은 계약을 받는다."""
    _seed()
    results = _rate_limited_collect()
    empty_table: dict = {"S+": [], "S": [], "A+": [], "A": [], "B": [], "C": []}
    res = gate_check(results, "opus", today=TODAY, now=NOW, grade_table=empty_table)
    assert res.ok is True
    assert res.stale_accepted is True
    assert "stale_accepted" in res.reason


# ---------------------------------------------------------------- fail-closed 음성 테스트


def test_gate_rejects_stale_older_than_6h():
    """뮤턴트 b: 6h 초과 스냅샷으로 rc 0 이 나가면 실패."""
    _seed(now=EPOCH - cache.STALE_MAX_S - 10.0)
    results = _rate_limited_collect()
    assert results[0].error is not None  # 6h 초과는 표시 폴백도 없다
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True
    assert res.stale_accepted is False
    assert "속도 제한" in res.reason  # AC1: 측정 불가가 아니라 속도 제한으로 보고


def test_gate_rejects_401_even_with_valid_stale():
    """뮤턴트 c: 401 인데 게이트가 옛 값을 받으면 실패."""
    _seed()
    results = cache.collect(
        {"claude": _failing("HTTP 401: unauthorized", kind="auth", status=401)},
        ["claude"],
        now=EPOCH,
        use_cache=False,
    )
    assert results[0].stale is True  # 표시는 마지막 값을 보여줄 수 있다
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True
    assert res.stale_accepted is False


def test_gate_rejects_stale_missing_required_bucket():
    """필수 bucket 결손(7d 없음)이면 수용하지 않는다 — 합성하지도 않는다."""
    _seed(
        result=ProviderResult(
            id="claude",
            buckets=[_bucket("5h", 10.0)],
            account_fp=FP_A,
        )
    )
    results = _rate_limited_collect()
    assert results[0].stale is True
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True


def test_gate_rejects_stale_with_elapsed_reset():
    """reset 회차가 지난 스냅샷은 지난 회차의 값 — 수용 불가."""
    _seed(
        result=ProviderResult(
            id="claude",
            buckets=[
                _bucket("5h", 10.0, hours_ahead=-1.0),  # 이미 지난 reset
                _bucket("7d", 30.0, hours_ahead=160.0),
            ],
            account_fp=FP_A,
        )
    )
    results = _rate_limited_collect()
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True


def test_gate_rejects_stale_without_reset_marks():
    """reset 미기록(롤링 창 등)은 회차 증명 불가 — 수용하지 않는다(자문 2558)."""
    _seed(
        result=ProviderResult(
            id="claude",
            buckets=[_bucket("5h", 10.0, hours_ahead=None), _bucket("7d", 30.0, hours_ahead=None)],
            account_fp=FP_A,
        )
    )
    results = _rate_limited_collect()
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True


def test_gate_rejects_stale_on_account_fp_mismatch():
    """계정/구독 변경 의심 — 다른 계정의 스냅샷으로 게이트를 열지 않는다."""
    _seed(fp=FP_A)
    results = cache.collect(
        {"claude": _failing("HTTP 429", kind="rate_limited", status=429, fp=FP_B)},
        ["claude"],
        now=EPOCH,
        use_cache=False,
    )
    assert results[0].error is not None  # 다른 계정의 값은 표시용으로도 쓰지 않는다
    assert results[0].account_fp_match is False
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True


def test_gate_rejects_stale_on_parse_or_unknown_failure():
    """분류 불가 실패는 수용 사유가 아니다."""
    _seed()
    results = cache.collect(
        {"claude": _failing("unexpected payload shape", kind="unknown")},
        ["claude"],
        now=EPOCH,
        use_cache=False,
    )
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True
    assert "측정 불가" in res.reason  # 429 가 아니면 기존 문구 유지


def test_gate_rejects_stale_when_uncaught_exception_loses_fp():
    """fetcher 가 그냥 raise 하면 지문을 증명할 수 없어 수용하지 않는다."""

    def boom() -> ProviderResult:
        raise ConnectionError("network down")

    _seed()
    results = cache.collect({"claude": boom}, ["claude"], now=EPOCH, use_cache=False)
    assert results[0].stale is True  # 표시 폴백은 된다
    assert results[0].account_fp_match is False  # stored_fp 있음 + 시도 지문 없음 → 불일치 취급
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True


def test_old_format_entry_reads_but_gate_denies():
    """버전 차이: 옛 설치본 형식(account_fp 없음) 엔트리는 읽히지만 수용은 거부."""
    _seed(fp=None)
    results = _rate_limited_collect()
    assert results[0].stale is True  # 표시 폴백은 기존처럼 동작
    assert results[0].account_fp_match is False  # 한쪽 지문 부재 → 증명 불가
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True


# ---------------------------------------------------------------- AC3: backoff


def test_backoff_window_blocks_all_network_paths():
    """뮤턴트 d: backoff 창 안에서 fetcher 가 호출되면 실패."""
    _seed()
    calls: list[str] = []

    def counting() -> ProviderResult:
        calls.append("fetch")
        return ProviderResult(
            id="claude",
            error="HTTP 429",
            error_kind="rate_limited",
            http_status=429,
            account_fp=FP_A,
        )

    counting.current_account_fp = lambda: FP_A

    cache.collect({"claude": counting}, ["claude"], now=EPOCH, use_cache=False)
    assert calls == ["fetch"]
    assert cache.backoff_remaining("claude", EPOCH) == pytest.approx(60.0)

    # --no-cache 도 창 안에서는 네트워크 0
    second = cache.collect({"claude": counting}, ["claude"], now=EPOCH + 30, use_cache=False)
    assert calls == ["fetch"]
    assert second[0].stale is True
    assert second[0].backoff_until == pytest.approx(EPOCH + 60)
    assert "backoff 중" in second[0].note
    assert "마지막 값" in second[0].note

    # backoff 중에도 지문이 맞으면 게이트는 수용 가능하다
    res = gate_check(second, "opus", today=TODAY, now=NOW)
    assert res.ok is True
    assert res.stale_accepted is True

    # 창이 지나면 다시 친다
    cache.collect({"claude": counting}, ["claude"], now=EPOCH + 61, use_cache=False)
    assert calls == ["fetch", "fetch"]


def test_backoff_stale_rejected_when_local_credentials_change():
    """backoff 중에도 로컬 자격 지문이 바뀌면 수용하지 않는다."""
    _seed()

    def fetch() -> ProviderResult:
        return ProviderResult(
            id="claude", error="HTTP 429", error_kind="rate_limited", http_status=429, account_fp=FP_A
        )

    fetch.current_account_fp = lambda: FP_B  # 그 사이 계정이 바뀌었다
    cache.collect({"claude": fetch}, ["claude"], now=EPOCH, use_cache=False)
    results = cache.collect({"claude": fetch}, ["claude"], now=EPOCH + 30, use_cache=False)
    assert results[0].stale is True
    assert results[0].account_fp_match is False
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is True


def test_backoff_retry_after_is_respected_not_capped():
    """Retry-After 양수는 서버 요구이므로 상한으로 깎지 않는다."""
    _seed()
    _rate_limited_collect(retry_after=900.0)
    assert cache.backoff_remaining("claude", EPOCH) == pytest.approx(900.0)


def test_backoff_grows_exponentially_and_caps_at_15m():
    _seed()
    counting_calls: list[str] = []

    def counting() -> ProviderResult:
        counting_calls.append("fetch")
        return ProviderResult(
            id="claude", error="HTTP 429", error_kind="rate_limited", http_status=429, account_fp=FP_A
        )

    counting.current_account_fp = lambda: FP_A

    t = EPOCH
    expected = [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    for delay in expected:
        cache.collect({"claude": counting}, ["claude"], now=t, use_cache=False)
        assert cache.backoff_remaining("claude", t) == pytest.approx(delay)
        t += delay + 1.0
    assert len(counting_calls) == len(expected)


def test_no_backoff_for_auth_failure():
    _seed()
    cache.collect(
        {"claude": _failing("HTTP 401", kind="auth", status=401)},
        ["claude"],
        now=EPOCH,
        use_cache=False,
    )
    assert cache.backoff_remaining("claude", EPOCH) == 0.0


def test_success_clears_backoff_state():
    _seed()
    _rate_limited_collect()
    assert cache.backoff_remaining("claude", EPOCH) > 0
    cache.collect({"claude": lambda: _claude_ok()}, ["claude"], now=EPOCH + 61, use_cache=False)
    assert cache.backoff_remaining("claude", EPOCH + 61) == 0.0


def test_refresh_worker_respects_backoff_and_audits_failure(capsys):
    """refresh 도 같은 host-local backoff 를 공유하고 실패 감사를 남긴다."""
    _seed()
    calls: list[str] = []

    def fetch() -> ProviderResult:
        calls.append("fetch")
        return ProviderResult(id="claude", error="HTTP 429", error_kind="rate_limited", http_status=429)

    fetchers = {"claude": fetch}
    assert refresh.run_worker(fetchers, "claude") == 1
    assert calls == ["fetch"]
    captured = capsys.readouterr()
    assert "status=429" in captured.err
    assert "kind=rate_limited" in captured.err

    # backoff 창 안 — 두 번째 호출은 네트워크 0
    assert refresh.run_worker(fetchers, "claude") == 0
    assert calls == ["fetch"]
    assert "backoff 중" in capsys.readouterr().out

    entry = _read_entries()["claude"]
    assert entry["result"]["buckets"]  # 스냅샷 유지
    assert entry["last_error_kind"] == "rate_limited"


# ---------------------------------------------------------------- 수동 관측 우선순위


def _set_manual_pair(capsys) -> None:
    for window in ("5h", "7d"):
        rc = cli.main(
            [
                "manual",
                "set",
                "--pool",
                "claude",
                "--window",
                window,
                "--used",
                "20",
                "--measured-at",
                "now",
                "--reason",
                "automatic measurement outage",
                "--ttl",
                "15m",
            ]
        )
        assert rc == 0
    capsys.readouterr()


def test_stale_accepted_wins_over_manual_observation(monkeypatch, capsys):
    """우선순위: fresh 자동 > stale_accepted > 유효 manual > 거부.

    stale 자동이 수용 가능하면 manual 관측은 호출되지 않는다 — 실측값이
    자기신고보다 위다.
    """
    _seed(now=None, result=_claude_ok_real())  # 실시간 시드 — cli.main 은 실제 시계를 쓴다
    _set_manual_pair(capsys)
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"claude": _failing("HTTP 429", kind="rate_limited", status=429)},
    )
    rc = cli.main(["gate", "-m", "opus", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    assert "stale_accepted=true" in out.out
    assert "source=operator" not in out.out  # manual 경로가 아니다
    assert "속도 제한" in out.out


def test_manual_rescues_when_stale_is_not_acceptable(monkeypatch, capsys):
    """stale 수용 불가(지문 부재)일 때만 manual 관측이 게이트를 구한다."""
    _seed(fp=None, now=None, result=_claude_ok_real(fp=None))  # 구형식 엔트리 — 지문 증명 불가
    _set_manual_pair(capsys)
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"claude": _failing("HTTP 429", kind="rate_limited", status=429)},
    )
    rc = cli.main(["gate", "-m", "opus", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    assert "source=operator" in out.out


# ---------------------------------------------------------------- AC4: 관측성


def test_json_reports_error_kind_status_and_backoff(monkeypatch, capsys):
    # TTL(180s)보다 오래된 시드여야 collect 가 fetcher 를 다시 부른다.
    _seed(now=None, result=_claude_ok_real())
    cache_path = cache.cache_path()
    data = json.loads(cache_path.read_text())
    data["claude"]["fetched_at"] = time.time() - 200.0
    cache_path.write_text(json.dumps(data))
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"claude": _failing("HTTP 429", kind="rate_limited", status=429, retry_after=120)},
    )
    rc = cli.main(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    claude = next(p for p in payload["providers"] if p["id"] == "claude")
    assert claude["status"] == "stale"
    assert claude["error_kind"] == "rate_limited"
    assert claude["http_status"] == 429
    assert claude["last_error_at"] is not None


def test_gate_stdout_contract_first_line_pool_and_stale_token(monkeypatch, capsys):
    """옛 wrk 가 파싱하는 첫 줄 계약(profile=… pool=…)을 깨지 않는다."""
    _seed(now=None, result=_claude_ok_real())
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"claude": _failing("HTTP 429", kind="rate_limited", status=429)},
    )
    rc = cli.main(["gate", "-m", "opus", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    first = out.out.splitlines()[0]
    assert "profile=opus" in first
    assert "pool=claude" in first
    assert "stale_accepted=true" in first
    assert "stale_accepted" in out.out.splitlines()[1]


def test_recommend_includes_stale_accepted_candidate_with_age():
    _seed()
    results = _rate_limited_collect()
    out = recommend(results, "S+", today=TODAY, now=NOW)
    assert "stale_accepted" in out
    assert "속도 제한" in out
    # 수용된 claude 는 제외(✗) 목록이 아니라 후보 목록에 있다.
    excluded_lines = [line for line in out.splitlines() if line.startswith("✗")]
    assert not any("claude" in line for line in excluded_lines)


def test_recommend_marks_rate_limited_pool_distinctly_when_unacceptable():
    results = [ProviderResult(id="claude", error="HTTP 429", error_kind="rate_limited", http_status=429)]
    out = recommend(results, "S+", today=TODAY, now=NOW)
    assert "속도 제한" in out
    assert any("속도 제한" in line for line in out.splitlines() if line.startswith("✗"))


def test_gate_stale_accepted_over_cutoff_still_denied():
    """stale_accepted 는 '측정 불가'를 정상 평가로 바꿀 뿐 cutoff 를 우회하지 않는다."""
    _seed(
        result=ProviderResult(
            id="claude",
            buckets=[_bucket("5h", 99.5), _bucket("7d", 30.0, hours_ahead=160.0)],
            account_fp=FP_A,
        )
    )
    results = _rate_limited_collect()
    res = gate_check(results, "opus", today=TODAY, now=NOW)
    assert res.ok is False
    assert res.unmeasurable is False  # 측정값은 있다 — 소진 거부
    assert "소진" in res.reason
