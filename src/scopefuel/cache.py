"""provider별 스냅샷 캐시 + stale 폴백.

폴백이 중요한 이유: agy 는 세션이 떠 있어야만 로컬 경로로 조회된다. 워커를 다 정리한 뒤에도
"12분 전 값"을 나이와 함께 보여주면 라우팅 판단에는 충분하다. 대신 **오래됐다는 사실을
반드시 표시**한다 — 조용히 옛 값을 신선한 값처럼 보여주는 것이 최악이다.
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import re
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager

from .http import classify_error
from .model import (
    PROBE_IN_PROGRESS,
    Bucket,
    PoolClass,
    ProviderResult,
    Scope,
    _is_valid_used_pct,
    _normalize_pool_class,
)
from .policy import get_policy

DEFAULT_TTL_S = 60.0
# 창 길이에서 허용 낡음을 약 1%로 유도한 정적 TTL 표다: 5h≈18,000s → 180s,
# 7d≈604,800s → 600s(반올림), 7d → 1,800s(약 0.3%, grok의 별도 운영 여유).
# fetch 전에 적용해야 하므로 캐시 버킷의 window를 읽어 동적으로 계산하지 않는다.
PROVIDER_TTL_S = {
    "claude": 180.0,
    "kimi": 180.0,
    "clinepass": 180.0,
    "agy": 180.0,
    "codex": 600.0,
    "grok": 1800.0,
}
MAX_FETCH_WORKERS = 8  # 현재 provider 수 이하: 독립 HTTP/PTY fetch를 병렬화하되 무제한 spawn은 피한다.
STALE_MAX_S = 6 * 3600.0  # 이보다 오래된 스냅샷은 폴백으로도 쓰지 않는다

# task #576 — 429 이후 host-local backoff. Retry-After 양수는 그대로 존중하고,
# 없거나 0이면 지수(60s → … → 15m 상한)로 늘린다. 상태는 호스트 로컬이다.
BACKOFF_SCHEMA = "scopefuel.backoff.v1"
BACKOFF_BASE_S = 60.0
BACKOFF_MAX_S = 15 * 60.0

# 구형식 결과(error_kind 없음)에서 429 텍스트를 알아채는 보조 장치.
_RATE_LIMITED_TEXT = re.compile(r"(?<!\d)429(?!\d)|rate[ _-]?limit|too many requests", re.I)


def cache_path() -> pathlib.Path:
    if override := os.environ.get("SCOPEFUEL_CACHE"):
        return pathlib.Path(os.path.expanduser(override))
    base = os.environ.get("XDG_CACHE_HOME") or (pathlib.Path.home() / ".cache")
    return pathlib.Path(base) / "scopefuel" / "snapshots.json"


def cache_dir() -> pathlib.Path:
    """Return the cache directory without touching it."""

    return cache_path().parent


@contextmanager
def _exclusive_cache_lock() -> Iterator[None]:
    """Serialize cache read/modify/write transactions with a kernel lock."""

    path = cache_dir() / "snapshots.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load() -> dict:
    try:
        return json.loads(cache_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.chmod(0o600)
        tmp.replace(path)
    except OSError:
        pass  # 캐시 실패가 조회를 막지는 않는다


def update_entry(name: str, result: ProviderResult, now: float) -> None:
    """Merge one freshly fetched pool into the cache atomically."""

    with _exclusive_cache_lock():
        data = _load()
        data[name] = _to_entry(result, now)
        _save(data)
        state = _load_backoff()
        if state["pools"].pop(name, None) is not None:
            _save_backoff(state)


def backoff_path() -> pathlib.Path:
    return cache_dir() / "backoff.json"


def _load_backoff() -> dict:
    try:
        data = json.loads(backoff_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {"schema": BACKOFF_SCHEMA, "pools": {}}
    if not isinstance(data, dict) or not isinstance(data.get("pools"), dict):
        return {"schema": BACKOFF_SCHEMA, "pools": {}}
    return data


def _save_backoff(data: dict) -> None:
    path = backoff_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.chmod(0o600)
        tmp.replace(path)
    except OSError:
        pass  # backoff 기록 실패가 조회를 막지는 않는다


def backoff_remaining(name: str, now: float) -> float:
    """name pool 의 남은 backoff 초. 기록이 없거나 창이 지났으면 0."""
    state = _load_backoff().get("pools", {}).get(name)
    if not isinstance(state, dict):
        return 0.0
    try:
        until = float(state.get("next_allowed_at") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, until - now)


def _is_rate_limited(result: ProviderResult) -> bool:
    if result.error_kind is not None:
        return result.error_kind == "rate_limited"
    return bool(result.error and _RATE_LIMITED_TEXT.search(result.error))


def _bump_backoff(pools: dict, name: str, result: ProviderResult, now: float) -> None:
    """429 실패의 다음 허용 시각을 기록한다.

    Retry-After 양수는 상한으로 깎지 않고 그대로 존중한다 — 서버 요구를 임의로
    줄이면 정확히 그 폭주를 다시 만든다. 없거나 0이면 60s → … → 15m 지수로.
    """
    prev = pools.get(name)
    consecutive = int(prev.get("consecutive") or 0) + 1 if isinstance(prev, dict) else 1
    retry_after = result.retry_after_s
    if retry_after is not None and retry_after > 0:
        delay = retry_after
    else:
        delay = min(BACKOFF_BASE_S * (2 ** (consecutive - 1)), BACKOFF_MAX_S)
    pools[name] = {
        "consecutive": consecutive,
        "next_allowed_at": now + delay,
        "last_error": result.error,
        "last_error_kind": result.error_kind,
        "last_http_status": result.http_status,
        "updated_at": now,
    }


def _audit_failure(entry: dict | None, result: ProviderResult, now: float) -> dict:
    """실패 감사 — 정상 스냅샷(result/fetched_at)은 건드리지 않고 last_error* 만 갱신한다."""
    if not isinstance(entry, dict):
        entry = {"fetched_at": 0, "result": {}}
    entry["last_error"] = result.error or result.warning
    entry["last_error_at"] = now
    # error_kind 가 없는 구형식 결과는 텍스트에서 429 를 복원한다.
    kind = result.error_kind or ("rate_limited" if _is_rate_limited(result) else None)
    if kind:
        entry["last_error_kind"] = kind
    if result.http_status is not None:
        entry["last_http_status"] = result.http_status
    return entry


def record_failure(name: str, result: ProviderResult, now: float) -> None:
    """단일-pool writer 경로(refresh worker)의 실패 감사 + 429 backoff 기록."""
    with _exclusive_cache_lock():
        data = _load()
        data[name] = _audit_failure(data.get(name), result, now)
        _save(data)
        if _is_rate_limited(result):
            state = _load_backoff()
            _bump_backoff(state["pools"], name, result, now)
            _save_backoff(state)


def _to_entry(result: ProviderResult, now: float) -> dict:
    payload = result.as_dict()
    payload.pop("verdict", None)  # 판정은 읽을 때 다시 계산한다
    # Manual observations and failed-probe details are separate audit state,
    # never part of the last successful automatic snapshot.
    payload.pop("manual", None)
    payload.pop("last_error", None)
    # 지문은 로컬 캐시 파일에만 둔다 — 계정 동일성 증명용이며 출력 계약이 아니다.
    if result.account_fp:
        payload["account_fp"] = result.account_fp
    return {"fetched_at": now, "result": payload}


def _pool_class(fetcher: object) -> PoolClass | None:
    return getattr(fetcher, "pool_class", None)


_VALID_POOL_CLASSES = frozenset({"preserve", "spend", "exclude"})


def _effective_class(name: str, fetcher: object, payload_class: PoolClass | None = None) -> PoolClass:
    explicit = _pool_class(fetcher)
    if explicit is not None:
        fallback: PoolClass = _normalize_pool_class(explicit)
    elif payload_class in _VALID_POOL_CLASSES:
        fallback = payload_class  # type: ignore[assignment]
    else:
        fallback = "preserve"
    return get_policy(name, fallback)[0]


def _from_entry(
    entry: dict, provider_id: str, now: float, pool_class: PoolClass | None = None
) -> ProviderResult:
    payload = entry.get("result") or {}
    fetched_at = float(entry.get("fetched_at") or 0)
    effective_class: PoolClass = (
        pool_class
        if pool_class is not None and pool_class in _VALID_POOL_CLASSES
        else (payload.get("pool_class") if payload.get("pool_class") in _VALID_POOL_CLASSES else "preserve")
    )
    buckets = [
        Bucket(
            label=b.get("label", "?"),
            window=b.get("window", "?"),
            used_pct=b.get("used_pct") if _is_valid_used_pct(b.get("used_pct")) else None,
            resets_at=b.get("resets_at"),
            scope=Scope((b.get("scope") or {}).get("kind", "account"), (b.get("scope") or {}).get("name")),
            horizon=b.get("horizon", "week"),
            note=b.get("note"),
        )
        for b in payload.get("buckets") or []
    ]
    return ProviderResult(
        id=provider_id,
        plan=payload.get("plan"),
        buckets=buckets,
        note=payload.get("note"),
        error=payload.get("error"),
        warning=payload.get("warning"),
        hint=payload.get("hint"),
        source=payload.get("source"),
        fetched_at=fetched_at,
        age_s=now - fetched_at,
        stale=True,
        pool_class=effective_class,
        account_fp=payload.get("account_fp"),
    )


def _failure_label(result: ProviderResult) -> str:
    return "속도 제한" if _is_rate_limited(result) else "조회 실패"


def _backoff_result(
    name: str,
    fetcher: object,
    entry: object,
    state: dict | None,
    now: float,
    until: float,
    policy_class: PoolClass,
) -> ProviderResult:
    """backoff 창 안의 결과 — 네트워크 0. 스냅샷이 있으면 나이와 함께 돌려준다."""
    remaining = until - now
    state = state if isinstance(state, dict) else {}
    last_error = state.get("last_error")
    if isinstance(entry, dict):
        age = now - float(entry.get("fetched_at") or 0)
        if age <= STALE_MAX_S:
            stale = _from_entry(entry, name, now, policy_class)
            stale.note = f"backoff 중, 마지막 값 {format_age(age)}"
            stale.last_error = last_error
            stale.last_error_at = state.get("updated_at") or now
            stale.error_kind = state.get("last_error_kind") or "rate_limited"
            status = state.get("last_http_status")
            stale.http_status = status if isinstance(status, int) else None
            stale.backoff_until = until
            # 네트워크 없이 읽을 수 있는 로컬 자격 지문으로 계정 동일성을 다시 확인한다.
            # 지문을 낼 수 없는 provider/구형식 엔트리는 "증명 불가"(None)로 둔다.
            probe = getattr(fetcher, "current_account_fp", None)
            current_fp = probe() if callable(probe) else None
            stored_fp = (entry.get("result") or {}).get("account_fp")
            stale.account_fp_match = (
                None
                if stored_fp is None and current_fp is None
                else stored_fp is not None and stored_fp == current_fp
            )
            return stale
    return ProviderResult(
        id=name,
        error=f"backoff 중, {remaining:.0f}s 뒤 재시도 가능" + (f" ({last_error})" if last_error else ""),
        error_kind="rate_limited",
        backoff_until=until,
    )


def _in_progress_result(
    name: str,
    fetcher: object,
    entry: dict | None,
    skipped: ProviderResult,
    now: float,
    ttl_s: float,
    policy_class: PoolClass,
) -> ProviderResult:
    """잠금으로 건너뛴 회차의 결과 — 측정 실패가 아니므로 스냅샷을 유지한다(#639).

    fresh TTL 안이면 그대로 통과, 밖이면 stale 폴백으로 표시해 게이트의
    stale_accepted 판정에 맡긴다. 스냅샷이 없거나 STALE_MAX_S 를 넘었으면
    건너뜀 결과를 그대로 돌려준다 — 마지막 정상 값 없음(측정 불가)은 유지.
    """
    if isinstance(entry, dict):
        age = now - float(entry.get("fetched_at") or 0)
        if age <= ttl_s:
            kept = _from_entry(entry, name, now, policy_class)
            kept.stale = False
            kept.note = f"탐침 진행 중 — 직전 값 {format_age(age)}"
            return kept
        if age <= STALE_MAX_S:
            stale = _from_entry(entry, name, now, policy_class)
            stale.note = f"탐침 진행 중 — 직전 값 {format_age(age)}"
            stale.last_error = skipped.error
            stale.last_error_at = now
            stale.error_kind = skipped.error_kind
            # 지문 대조는 로컬 프로브만으로 한다 — 건너뛴 회차는 새 자격 관측이 없다.
            probe = getattr(fetcher, "current_account_fp", None)
            current_fp = probe() if callable(probe) else None
            stored_fp = (entry.get("result") or {}).get("account_fp")
            stale.account_fp_match = (
                None
                if stored_fp is None and current_fp is None
                else stored_fp is not None and stored_fp == current_fp
            )
            return stale
    return skipped


def _merge_results(
    successes: dict[str, ProviderResult],
    failures: dict[str, ProviderResult],
    now: float,
) -> None:
    """파일을 다시 읽어 이번 호출의 결과만 병합한다 — 다른 writer/provider 의
    기존 엔트리는 남는다(#576 부분 병합). 실패는 스냅샷을 덮지 않고 감사만 남긴다."""
    with _exclusive_cache_lock():
        data = _load()
        backoff = _load_backoff()
        backoff_dirty = False
        for name, result in successes.items():
            data[name] = _to_entry(result, now)
            if backoff["pools"].pop(name, None) is not None:
                backoff_dirty = True
        for name, result in failures.items():
            data[name] = _audit_failure(data.get(name), result, now)
            if _is_rate_limited(result):
                _bump_backoff(backoff["pools"], name, result, now)
                backoff_dirty = True
        _save(data)
        if backoff_dirty:
            _save_backoff(backoff)


def collect(
    fetchers: dict[str, object],
    names: list[str],
    *,
    ttl_s: float | None = None,
    use_cache: bool = True,
    now: float | None = None,
) -> list[ProviderResult]:
    """fetch → 실패하면 캐시 폴백. 반환 순서는 names 순서.

    use_cache=False 도 파일을 읽는다 — 이번 호출에서 성공한 항목만 덮어쓰는 부분
    병합이므로 실패하거나 이번에 조회하지 않은 provider 의 기존 스냅샷은 남는다
    (#576: 예전에는 빈 dict 를 통째로 저장해 다른 provider 의 정상 값까지 사라졌다).
    """
    now = time.time() if now is None else now
    cache = _load()
    backoff = _load_backoff()
    results: list[ProviderResult | None] = [None] * len(names)
    misses: list[tuple[int, str, object, dict | None, PoolClass, float]] = []

    for index, name in enumerate(names):
        entry = cache.get(name)
        fetcher = fetchers.get(name)
        cached_class: PoolClass | None = None
        if isinstance(entry, dict):
            cached_class = (entry.get("result") or {}).get("pool_class")
        policy_class = _effective_class(name, fetcher, cached_class)
        effective_ttl_s = ttl_s if ttl_s is not None else PROVIDER_TTL_S.get(name, DEFAULT_TTL_S)
        if use_cache and entry and now - float(entry.get("fetched_at") or 0) <= effective_ttl_s:
            fresh = _from_entry(entry, name, now, policy_class)
            fresh.stale = False  # TTL 안이면 신선한 값으로 취급
            results[index] = fresh
            continue

        # backoff 창 안에서는 --no-cache 포함 어느 경로도 네트워크를 치지 않는다.
        state = backoff.get("pools", {}).get(name)
        try:
            until = float((state or {}).get("next_allowed_at") or 0)
        except (TypeError, ValueError):
            until = 0.0
        if until > now:
            results[index] = _backoff_result(name, fetcher, entry, state, now, until, policy_class)
            continue

        misses.append(
            (index, name, fetcher, entry if isinstance(entry, dict) else None, policy_class, effective_ttl_s)
        )

    def fetch_one(fetcher: object) -> ProviderResult:
        if fetcher is None:
            return ProviderResult(id="?", error="알 수 없는 provider", error_kind="unknown")
        try:
            return fetcher()  # type: ignore[operator]
        except Exception as exc:
            kind, status, retry_after = classify_error(exc)
            return ProviderResult(
                id="?",
                error=str(exc),
                error_kind=kind,
                http_status=status,
                retry_after_s=retry_after,
            )

    fetched: dict[int, Future[ProviderResult]] = {}
    if misses:
        with ThreadPoolExecutor(max_workers=min(MAX_FETCH_WORKERS, len(misses))) as pool:
            for index, _name, fetcher, _entry, _policy_class, _ttl in misses:
                fetched[index] = pool.submit(fetch_one, fetcher)

    successes: dict[str, ProviderResult] = {}
    failures: dict[str, ProviderResult] = {}
    for index, name, _fetcher, entry, policy_class, effective_ttl_s in misses:
        result = fetched[index].result()
        result.id = name

        result.pool_class = _effective_class(name, _fetcher, result.pool_class)

        if result.error:
            if result.error_kind == PROBE_IN_PROGRESS:
                # 잠금으로 건너뛴 회차 — 실패 감사도 쓰지 않고 스냅샷을 유지한다.
                results[index] = _in_progress_result(
                    name, _fetcher, entry, result, now, effective_ttl_s, policy_class
                )
                continue
            failures[name] = result
            if isinstance(entry, dict):
                age = now - float(entry.get("fetched_at") or 0)
                stored_fp = (entry.get("result") or {}).get("account_fp")
                if stored_fp is not None and result.account_fp is not None and stored_fp != result.account_fp:
                    # 계정/구독 변경 의심 — 다른 계정의 스냅샷은 쓰지 않는다(자문 2558)
                    result.account_fp_match = False
                elif age <= STALE_MAX_S:
                    stale = _from_entry(entry, name, now, policy_class)
                    stale.note = f"{_failure_label(result)} → 캐시 사용 ({result.error})"
                    stale.last_error = result.error
                    if result.hint:
                        stale.last_error += f" — {result.hint}"
                    stale.last_error_at = now
                    stale.error_kind = result.error_kind
                    stale.http_status = result.http_status
                    stale.retry_after_s = result.retry_after_s
                    stale.account_fp_match = (
                        None
                        if stored_fp is None and result.account_fp is None
                        else stored_fp is not None and stored_fp == result.account_fp
                    )
                    results[index] = stale
                    continue
            results[index] = result
            continue

        if result.warning:
            failures[name] = result  # warning 도 관측 감사는 남긴다
        else:
            result.fetched_at = now
            result.age_s = 0.0
            successes[name] = result
        results[index] = result

    if successes or failures:
        _merge_results(successes, failures, now)
    return [result for result in results if result is not None]


def format_age(age_s: float | None) -> str:
    if age_s is None:
        return ""
    if age_s < 90:
        return f"{int(age_s)}초 전"
    if age_s < 5400:
        return f"{int(age_s // 60)}분 전"
    return f"{age_s / 3600:.1f}시간 전"
