"""provider별 스냅샷 캐시 + stale 폴백.

폴백이 중요한 이유: agy 는 세션이 떠 있어야만 로컬 경로로 조회된다. 워커를 다 정리한 뒤에도
"12분 전 값"을 나이와 함께 보여주면 라우팅 판단에는 충분하다. 대신 **오래됐다는 사실을
반드시 표시**한다 — 조용히 옛 값을 신선한 값처럼 보여주는 것이 최악이다.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from concurrent.futures import Future, ThreadPoolExecutor

from .model import Bucket, PoolClass, ProviderResult, Scope, _is_valid_used_pct, _normalize_pool_class
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
MAX_FETCH_WORKERS = 7  # 현재 provider 수 이하: 독립 HTTP/PTY fetch를 병렬화하되 무제한 spawn은 피한다.
STALE_MAX_S = 6 * 3600.0  # 이보다 오래된 스냅샷은 폴백으로도 쓰지 않는다


def cache_path() -> pathlib.Path:
    if override := os.environ.get("SCOPEFUEL_CACHE"):
        return pathlib.Path(os.path.expanduser(override))
    base = os.environ.get("XDG_CACHE_HOME") or (pathlib.Path.home() / ".cache")
    return pathlib.Path(base) / "scopefuel" / "snapshots.json"


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


def _to_entry(result: ProviderResult, now: float) -> dict:
    payload = result.as_dict()
    payload.pop("verdict", None)  # 판정은 읽을 때 다시 계산한다
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
    )


def collect(
    fetchers: dict[str, object],
    names: list[str],
    *,
    ttl_s: float | None = None,
    use_cache: bool = True,
    now: float | None = None,
) -> list[ProviderResult]:
    """fetch → 실패하면 캐시 폴백. 반환 순서는 names 순서."""
    now = time.time() if now is None else now
    cache = _load() if use_cache else {}
    results: list[ProviderResult | None] = [None] * len(names)
    dirty = False
    misses: list[tuple[int, str, object, dict | None, PoolClass]] = []

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

        misses.append((index, name, fetcher, entry if isinstance(entry, dict) else None, policy_class))

    def fetch_one(fetcher: object) -> ProviderResult:
        if fetcher is None:
            return ProviderResult(id="?", error="알 수 없는 provider")
        try:
            return fetcher()  # type: ignore[operator]
        except Exception as exc:
            return ProviderResult(id="?", error=str(exc))

    fetched: dict[int, Future[ProviderResult]] = {}
    if misses:
        with ThreadPoolExecutor(max_workers=min(MAX_FETCH_WORKERS, len(misses))) as pool:
            for index, _name, fetcher, _entry, _policy_class in misses:
                fetched[index] = pool.submit(fetch_one, fetcher)

    for index, name, _fetcher, entry, policy_class in misses:
        result = fetched[index].result()
        result.id = name

        result.pool_class = _effective_class(name, _fetcher, result.pool_class)

        if result.error and entry:
            age = now - float(entry.get("fetched_at") or 0)
            if age <= STALE_MAX_S:
                stale = _from_entry(entry, name, now, policy_class)
                stale.note = f"조회 실패 → 캐시 사용 ({result.error})"
                results[index] = stale
                continue

        if not result.error and not result.warning:
            result.fetched_at = now
            result.age_s = 0.0
            cache[name] = _to_entry(result, now)
            dirty = True
        results[index] = result

    if dirty:
        _save(cache)
    return [result for result in results if result is not None]


def format_age(age_s: float | None) -> str:
    if age_s is None:
        return ""
    if age_s < 90:
        return f"{int(age_s)}초 전"
    if age_s < 5400:
        return f"{int(age_s // 60)}분 전"
    return f"{age_s / 3600:.1f}시간 전"
