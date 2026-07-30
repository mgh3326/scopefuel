"""Provider 레지스트리.

우선순위(뒤가 이김): 내장 Python provider < 선언형 TOML 스펙 < entry-point 플러그인.
스펙이 내장을 덮어쓸 수 있게 한 것은 의도적이다 — 엔드포인트가 깨졌을 때
릴리스를 기다리지 않고 사용자가 TOML 한 장으로 고칠 수 있어야 한다.

각 fetcher는 callable이며 `.pool_class` 메타데이터를 가질 수 있다.
registry는 성공/예외/캐시 폴백 모두에서 이 메타데이터를 authoritative class로 사용한다.
"""

from __future__ import annotations

from typing import Protocol

from ..model import PoolClass, ProviderResult, _normalize_pool_class
from . import agy, claude, clinepass, codex, kiro


class Fetcher(Protocol):
    """provider callable + class metadata."""

    pool_class: PoolClass

    def __call__(self) -> ProviderResult: ...


class FetcherWrapper:
    """Callable wrapper that exposes pool_class metadata without mutating the original callable."""

    def __init__(self, fn: object, pool_class: PoolClass):
        self.fn = fn
        self.pool_class = _normalize_pool_class(pool_class)

    def __call__(self) -> ProviderResult:
        return self.fn()  # type: ignore[no-any-return]


def _with_class(fn: object, pool_class: PoolClass) -> Fetcher:
    """callable을 감싸 pool_class 메타데이터를 부여한다."""
    return FetcherWrapper(fn, pool_class)  # type: ignore[return-value]


BUILTIN: dict[str, Fetcher] = {
    "claude": _with_class(claude.fetch, "preserve"),
    "codex": _with_class(codex.fetch, "preserve"),
    "agy": _with_class(agy.fetch, "spend"),
    "kiro": _with_class(kiro.fetch, "spend"),
    "clinepass": _with_class(clinepass.fetch, "spend"),
}


def _entry_point_providers() -> dict[str, Fetcher]:
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return {}
    found: dict[str, Fetcher] = {}
    for ep in entry_points(group="scopefuel.providers"):
        try:
            loaded = ep.load()
            explicit_class = getattr(loaded, "pool_class", None)
            if explicit_class is not None:
                found[ep.name] = _with_class(loaded, _normalize_pool_class(explicit_class))
            else:
                found[ep.name] = loaded
        except Exception:  # 플러그인 하나가 도구 전체를 막지 않는다
            continue
    return found


def registry() -> dict[str, Fetcher]:
    from ..spec import discover_specs

    merged: dict[str, Fetcher] = dict(BUILTIN)
    merged.update(discover_specs())
    merged.update(_entry_point_providers())
    return merged


def default_order(names: list[str]) -> list[str]:
    """내장 provider 를 먼저, 나머지는 알파벳 순."""
    builtin = [n for n in BUILTIN if n in names]
    rest = sorted(n for n in names if n not in BUILTIN)
    return builtin + rest
