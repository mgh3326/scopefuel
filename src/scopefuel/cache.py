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

from .model import Bucket, PoolClass, ProviderResult, Scope, _is_valid_used_pct, _normalize_pool_class

DEFAULT_TTL_S = 60.0
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


def _from_entry(
    entry: dict, provider_id: str, now: float, pool_class: PoolClass | None = None
) -> ProviderResult:
    payload = entry.get("result") or {}
    fetched_at = float(entry.get("fetched_at") or 0)
    effective_class: PoolClass = (
        pool_class
        if pool_class is not None and pool_class in ("preserve", "spend")
        else (payload.get("pool_class") if payload.get("pool_class") in ("preserve", "spend") else "preserve")
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
    ttl_s: float = DEFAULT_TTL_S,
    use_cache: bool = True,
    now: float | None = None,
) -> list[ProviderResult]:
    """fetch → 실패하면 캐시 폴백. 반환 순서는 names 순서."""
    now = time.time() if now is None else now
    cache = _load() if use_cache else {}
    results: list[ProviderResult] = []
    dirty = False

    for name in names:
        entry = cache.get(name)
        fetcher = fetchers.get(name)
        explicit_class = _pool_class(fetcher)
        if use_cache and entry and now - float(entry.get("fetched_at") or 0) <= ttl_s:
            fresh = _from_entry(entry, name, now, explicit_class)
            fresh.stale = False  # TTL 안이면 신선한 값으로 취급
            results.append(fresh)
            continue

        result: ProviderResult
        if fetcher is None:
            result = ProviderResult(id=name, error="알 수 없는 provider")
        else:
            try:
                result = fetcher()  # type: ignore[operator]
            except Exception as exc:
                result = ProviderResult(id=name, error=str(exc))

        if explicit_class is not None:
            result.pool_class = _normalize_pool_class(explicit_class)
        elif not result.pool_class:
            result.pool_class = "preserve"

        if result.error and entry:
            age = now - float(entry.get("fetched_at") or 0)
            if age <= STALE_MAX_S:
                stale = _from_entry(entry, name, now, explicit_class)
                stale.note = f"조회 실패 → 캐시 사용 ({result.error})"
                results.append(stale)
                continue

        if not result.error and not result.warning:
            result.fetched_at = now
            result.age_s = 0.0
            cache[name] = _to_entry(result, now)
            dirty = True
        results.append(result)

    if dirty:
        _save(cache)
    return results


def format_age(age_s: float | None) -> str:
    if age_s is None:
        return ""
    if age_s < 90:
        return f"{int(age_s)}초 전"
    if age_s < 5400:
        return f"{int(age_s // 60)}분 전"
    return f"{age_s / 3600:.1f}시간 전"
