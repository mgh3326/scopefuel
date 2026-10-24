"""hk 1918 — collect() 의 pool 당 단일 비행 잠금 + Retry-After HTTP-date + claude 네트워크 backoff.

전부 stub fetcher·tmp 캐시·파일 핸드셰이크로 돌린다 — 실 provider/API 호출 없음.
다중 프로세스 케이스는 같은 SCOPEFUEL_CACHE 파일을 공유하는 자식 프로세스로 진짜
크로스-프로세스 잠금을 검증한다. 조율은 파일 신호(호출 로그/GATE)로 하고 sleep 으로
순서를 맞추지 않는다.
"""

from __future__ import annotations

import email.message
import email.utils
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import urllib.error

import pytest

import scopefuel
from scopefuel import cache, http, proctrack
from scopefuel.model import Bucket, ProviderResult, Scope

NOW_EPOCH = 1_800_000_000.0  # 고정 시계 — 실제 시각과 무관
SRC_ROOT = pathlib.Path(scopefuel.__file__).resolve().parent.parent


def _ok(pool: str = "claude") -> ProviderResult:
    return ProviderResult(
        id=pool,
        buckets=[Bucket(label="5h", window="5h", used_pct=10.0, scope=Scope("account"), horizon="now")],
        source="stub",
    )


def _seed_stale(pool: str = "claude", *, at: float | None = None) -> None:
    """TTL 을 지난 정상 스냅샷을 심는다 — 다음 collect 는 miss 가 된다."""
    stamped = time.time() - 300.0 if at is None else at
    cache.update_entry(pool, _ok(pool), stamped)


def _wait_for(path: pathlib.Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"handshake file never appeared: {path}")


def _child_env(tmp_path: pathlib.Path, **extra: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["SCOPEFUEL_CACHE"] = str(tmp_path / "snapshots.json")
    env["PROBE_CALL_LOG"] = str(tmp_path / "calls.log")
    env.update(extra)
    return env


def _result_line(proc: subprocess.Popen) -> dict:
    out = proc.stdout.read() if proc.stdout else ""
    for line in reversed(out.splitlines()):
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT ") :])
    raise AssertionError(f"child printed no RESULT line: {out!r}")


def _call_log(tmp_path: pathlib.Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text().split() if log.exists() else []


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


# 자식 프로세스 스크립트 — fetcher 가 호출 로그를 남기고 GATE 파일이 생길 때까지
# 버티는(= 잠금 보유 상태를 유지하는) collect 1회 실행.
_CHILD_COLLECT = """
import json, os, time
from scopefuel import cache
from scopefuel.model import Bucket, ProviderResult, Scope

call_log = os.environ["PROBE_CALL_LOG"]
gate = os.environ.get("PROBE_GATE") or None

def fetch():
    with open(call_log, "a") as handle:
        handle.write(f"{os.getpid()}\\n")
    if gate:
        deadline = time.monotonic() + 120.0
        while not os.path.exists(gate) and time.monotonic() < deadline:
            time.sleep(0.05)
    return ProviderResult(
        id="claude",
        buckets=[Bucket(label="5h", window="5h", used_pct=10.0, scope=Scope("account"), horizon="now")],
        source="stub",
    )

result = cache.collect({"claude": fetch}, ["claude"], use_cache=False)[0]
print("RESULT " + json.dumps({
    "error": result.error,
    "error_kind": result.error_kind,
    "stale": result.stale,
    "note": result.note,
    "hint": result.hint,
}))
"""


def _spawn_collect(tmp_path: pathlib.Path, *, gate: pathlib.Path | None) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_COLLECT],
        env=_child_env(tmp_path, PROBE_GATE=str(gate) if gate else ""),
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


# ---------------------------------------------------------------- 단일 비행 잠금


def test_two_concurrent_collects_fetch_once(tmp_path):
    """두 collect() 가 같은 pool 을 동시에 노려도 실제 fetch 는 1회뿐이다.

    뮤턴트: 단일 비행 잠금을 제거하면 두 번째 호출도 fetch 해 호출 로그가
    2줄이 되어 이 테스트가 RED 다.
    """
    _seed_stale()
    gate = tmp_path / "gate"
    holder = _spawn_collect(tmp_path, gate=gate)
    contender: subprocess.Popen | None = None
    try:
        # A 의 fetcher 가 호출 로그를 쓴 시점 = A 가 잠금을 쥐고 fetch 중.
        _wait_for(tmp_path / "calls.log")

        contender = _spawn_collect(tmp_path, gate=None)
        contender.wait(timeout=60)
        second = _result_line(contender)

        # 두 번째 호출자는 fetch 하지 않는다 — stale 스냅샷이 있으므로
        # '탐침 진행 중' 표식의 stale 서빙을 받는다.
        assert len(_call_log(tmp_path)) == 1
        assert second["error_kind"] == "probe_in_progress"
        assert second["stale"] is True
        assert "탐침 진행 중" in (second["note"] or "")
    finally:
        gate.touch()
        if contender is not None:
            _reap(contender)
        holder.wait(timeout=60)

    # 첫 프로브가 끝난 뒤 TTL 안의 호출은 캐시 히트 — fetch 없음.
    calls: list[str] = []
    results = cache.collect(
        {"claude": lambda: calls.append("fetch") or _ok()},
        ["claude"],
        now=time.time(),
    )
    assert calls == []
    assert results[0].stale is False


def test_probe_in_flight_result_when_no_usable_entry(tmp_path):
    """캐시가 아예 없으면 두 번째 호출자는 probe_in_progress 결과를 받는다 —
    하드 오류가 아니라 '진행 중' 표식 + 한 줄 힌트."""
    gate = tmp_path / "gate"
    holder = _spawn_collect(tmp_path, gate=gate)
    contender: subprocess.Popen | None = None
    try:
        _wait_for(tmp_path / "calls.log")
        contender = _spawn_collect(tmp_path, gate=None)
        contender.wait(timeout=60)
        result = _result_line(contender)
        assert result["error_kind"] == "probe_in_progress"
        assert result["error"]  # 사유 문자열이 채워져 있다
        assert result["hint"]  # 한 줄 힌트
        assert len(_call_log(tmp_path)) == 1
    finally:
        gate.touch()
        if contender is not None:
            _reap(contender)
        holder.wait(timeout=60)


def test_double_check_serves_entry_landed_before_lock(monkeypatch):
    """잠금 획득 직전에 다른 프로세스가 신선한 스냅샷을 떨궜으면 fetch 하지 않는다.

    첫 스캔(stale)과 잠금 획득 사이에 착지한 엔트리를 잠금 래퍼 안에서 기록해
    결정적으로 재현한다. 뮤턴트: 획득 후 재확인을 빼면 counting 이 호출되어 RED.
    """
    _seed_stale("claude", at=NOW_EPOCH - 300.0)
    calls: list[str] = []

    def counting() -> ProviderResult:
        calls.append("fetch")
        return _ok()

    real_lock = proctrack.single_probe_lock

    def landing_then_lock(workdir: pathlib.Path):
        # 잠금 대기 중(첫 스캔 이후) 다른 프로세스의 정상 병합이 착지했다.
        cache.update_entry("claude", _ok(), NOW_EPOCH)
        return real_lock(workdir)

    monkeypatch.setattr(proctrack, "single_probe_lock", landing_then_lock)
    results = cache.collect({"claude": counting}, ["claude"], now=NOW_EPOCH)

    assert calls == []
    assert results[0].error is None
    assert results[0].stale is False


def test_double_check_honours_backoff_opened_before_lock(monkeypatch):
    """잠금 획득 전에 열린 backoff 창도 재확인한다 — 창 안에서 네트워크 0."""
    _seed_stale("claude", at=NOW_EPOCH - 300.0)
    calls: list[str] = []

    def counting() -> ProviderResult:
        calls.append("fetch")
        return _ok()

    real_lock = proctrack.single_probe_lock

    def backoff_lands_then_lock(workdir: pathlib.Path):
        cache.record_failure(
            "claude",
            ProviderResult(id="claude", error="HTTP 429", error_kind="rate_limited", http_status=429),
            NOW_EPOCH,
        )
        return real_lock(workdir)

    monkeypatch.setattr(proctrack, "single_probe_lock", backoff_lands_then_lock)
    results = cache.collect({"claude": counting}, ["claude"], now=NOW_EPOCH)

    assert calls == []
    assert "backoff 중" in (results[0].note or results[0].error or "")


def test_dead_lock_holder_frees_the_next_caller(tmp_path):
    """잠금 보유자가 SIGKILL 로 죽으면 커널이 flock 을 해제해 다음 호출이 fetch 한다."""
    _seed_stale()
    # GATE 는 영원히 생기지 않는다 — 자식은 fetch 안에서 잠금을 쥔 채 멈춘다.
    holder = _spawn_collect(tmp_path, gate=tmp_path / "never")
    _wait_for(tmp_path / "calls.log")
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=30)
    assert holder.returncode == -signal.SIGKILL

    calls: list[str] = []

    def counting() -> ProviderResult:
        calls.append("fetch")
        return _ok()

    results = cache.collect({"claude": counting}, ["claude"], now=time.time())
    assert calls == ["fetch"]
    assert results[0].error is None


def test_grok_collect_lock_does_not_self_skip():
    """collect 잠금(probe-locks/collect-grok)은 grok 의 안쪽 잠금(probe-locks/grok)과
    다른 경로다 — 같은 호출 안에서 둘 다 잡혀 자가 건너뜀이 일어나지 않는다.

    뮤턴트: collect 잠금을 probe-locks/<pool> 로 옮기면 grok.fetch 의 안쪽 잠금이
    같은 파일을 두 번 비차단 시도해 항상 실패 → 이 테스트가 RED 다.
    """
    ran: list[bool] = []

    def grok_like_fetch() -> ProviderResult:
        # providers/grok.py 의 안쪽 잠금과 동일 경로를 fetch 안에서 잡는다.
        with proctrack.single_probe_lock(cache.cache_dir() / "probe-locks" / "grok") as got:
            assert got, "inner probe lock self-skipped under the collect lock"
            ran.append(got)
        return _ok("grok")

    results = cache.collect({"grok": grok_like_fetch}, ["grok"], now=NOW_EPOCH, use_cache=False)
    assert ran == [True]
    assert results[0].error is None


def test_pools_fetch_in_parallel_not_serialized():
    """per-pool 잠금이어도 pool 간 fetch 는 병렬이다(MAX_FETCH_WORKERS 유지)."""
    kimi_started = threading.Event()

    def claude_fetch() -> ProviderResult:
        assert kimi_started.wait(timeout=5.0), "claude fetch ran before kimi started — serialized"
        return _ok("claude")

    def kimi_fetch() -> ProviderResult:
        kimi_started.set()
        return _ok("kimi")

    results = cache.collect(
        {"claude": claude_fetch, "kimi": kimi_fetch},
        ["claude", "kimi"],
        now=NOW_EPOCH,
        use_cache=False,
    )
    assert all(r.error is None for r in results)


# ---------------------------------------------------------------- Retry-After HTTP-date


def _http_429(retry_after_header: str) -> urllib.error.HTTPError:
    headers = email.message.Message()
    headers["Retry-After"] = retry_after_header
    return urllib.error.HTTPError("https://api.test/usage", 429, "Too Many Requests", headers, None)


def test_retry_after_numeric_seconds_unchanged():
    assert http._retry_after_seconds(_http_429("30")) == 30.0
    assert http._retry_after_seconds(_http_429("0")) == 0.0
    assert http._retry_after_seconds(_http_429("-5")) is None
    assert http._retry_after_seconds(_http_429("  12.5 ")) == 12.5


def test_retry_after_http_date_future():
    future = NOW_EPOCH + 120.0
    header = email.utils.formatdate(future, usegmt=True)
    assert http._retry_after_seconds(_http_429(header), now=NOW_EPOCH) == pytest.approx(120.0)


def test_retry_after_http_date_past_is_zero():
    header = email.utils.formatdate(NOW_EPOCH - 120.0, usegmt=True)
    assert http._retry_after_seconds(_http_429(header), now=NOW_EPOCH) == 0.0


def test_retry_after_garbage_is_none():
    assert http._retry_after_seconds(_http_429("tomorrow")) is None
    assert http._retry_after_seconds(_http_429("")) is None
    headers = email.message.Message()  # 헤더 부재
    exc = urllib.error.HTTPError("https://api.test/usage", 429, "x", headers, None)
    assert http._retry_after_seconds(exc) is None


def test_429_with_future_http_date_sets_next_allowed_at_to_that_date():
    """날짜 형 Retry-After 도 양수 초와 같이 상한 없이 존중된다.

    뮤턴트: 날짜 분기를 무시하면 retry_after_s=None → 지수 backoff(60s) 가 되어
    next_allowed_at 이 날짜가 아닌 NOW_EPOCH+60 으로 RED.
    """
    _seed_stale("claude", at=NOW_EPOCH - 300.0)
    date_epoch = NOW_EPOCH + 400.0
    header = email.utils.formatdate(date_epoch, usegmt=True)

    def fetch() -> ProviderResult:
        exc = _http_429(header)
        # request_json 이 하는 것과 같은 변환 — 파싱된 초를 실어 올린다.
        raise http.HttpError(429, "rate limit", http._retry_after_seconds(exc, now=NOW_EPOCH))

    cache.collect({"claude": fetch}, ["claude"], now=NOW_EPOCH, use_cache=False)
    state = json.loads(cache.backoff_path().read_text())["pools"]["claude"]
    assert state["next_allowed_at"] == pytest.approx(date_epoch)


# ---------------------------------------------------------------- 네트워크 backoff


def _network_failure(error: str = "timed out"):
    def fetch() -> ProviderResult:
        raise TimeoutError(error)

    return fetch


def test_claude_network_failure_opens_backoff_window():
    """claude 네트워크 실패는 429 없는 지수 backoff(60s)를 연다.

    뮤턴트: NETWORK_BACKOFF_POOLS 게이트를 빼면 backoff_remaining 이 0이라 RED.
    """
    _seed_stale("claude", at=NOW_EPOCH - 300.0)
    results = cache.collect({"claude": _network_failure()}, ["claude"], now=NOW_EPOCH, use_cache=False)
    assert results[0].error_kind == "network"  # 라벨은 network 유지 (rate_limited 가 아님)
    assert cache.backoff_remaining("claude", NOW_EPOCH) == pytest.approx(60.0)

    # 창 안의 다음 collect 는 fetch 0 — stale 엔트리를 network 라벨로 서빙한다.
    calls: list[str] = []
    second = cache.collect(
        {"claude": lambda: calls.append("fetch") or _ok()},
        ["claude"],
        now=NOW_EPOCH + 30.0,
        use_cache=False,
    )
    assert calls == []
    assert second[0].stale is True
    assert second[0].error_kind == "network"
    assert "backoff 중" in (second[0].note or "")


def test_claude_network_backoff_result_keeps_network_label_without_entry():
    """스냅샷이 아예 없는 창 안의 결과 라벨도 network 로 남는다 (rate_limited 가 아님)."""
    cache.collect({"claude": _network_failure()}, ["claude"], now=NOW_EPOCH, use_cache=False)
    inside = cache.collect({"claude": _network_failure()}, ["claude"], now=NOW_EPOCH + 30.0, use_cache=False)
    assert inside[0].error_kind == "network"
    assert "backoff 중" in (inside[0].error or "")


def test_kimi_network_failure_sets_no_backoff():
    """다른 pool 의 네트워크 실패는 지금처럼 backoff 를 열지 않는다.

    뮤턴트: 네트워크 backoff 를 전 pool 에 적용하면 여기서 RED.
    """
    _seed_stale("kimi", at=NOW_EPOCH - 300.0)
    cache.collect({"kimi": _network_failure()}, ["kimi"], now=NOW_EPOCH, use_cache=False)
    assert cache.backoff_remaining("kimi", NOW_EPOCH) == 0.0


def test_claude_success_clears_network_backoff():
    _seed_stale("claude", at=NOW_EPOCH - 300.0)
    cache.collect({"claude": _network_failure()}, ["claude"], now=NOW_EPOCH, use_cache=False)
    assert cache.backoff_remaining("claude", NOW_EPOCH) > 0.0
    cache.collect({"claude": lambda: _ok()}, ["claude"], now=NOW_EPOCH + 61.0, use_cache=False)
    assert cache.backoff_remaining("claude", NOW_EPOCH + 61.0) == 0.0
