"""Provider 레지스트리.

각 fetcher는 callable이며 `.pool_class` 메타데이터를 가질 수 있다.
registry는 성공/예외/캐시 폴백 모두에서 이 메타데이터를 authoritative class로 사용한다.
"""

from __future__ import annotations

from typing import Protocol

from ..model import PoolClass, ProviderResult, _normalize_pool_class
from . import agy, claude, clinepass, codex, commandcode, devin, grok, kimi, kiro, upstage


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

    def __getattr__(self, name: str) -> object:
        # 감싼 callable의 부가 probe(예: current_account_fp)를 그대로 노출한다.
        return getattr(self.fn, name)


def _with_class(fn: object, pool_class: PoolClass) -> Fetcher:
    """callable을 감싸 pool_class 메타데이터를 부여한다."""
    return FetcherWrapper(fn, pool_class)  # type: ignore[return-value]


BUILTIN: dict[str, Fetcher] = {
    "claude": _with_class(claude.fetch, "spend"),
    "codex": _with_class(codex.fetch, "spend"),
    "agy": _with_class(agy.fetch, "spend"),
    "kiro": _with_class(kiro.fetch, "spend"),
    "clinepass": _with_class(clinepass.fetch, "spend"),
    "grok": _with_class(grok.fetch, "spend"),
    "kimi": _with_class(kimi.fetch, "spend"),
    "devin": _with_class(devin.fetch, "spend"),
    "upstage": _with_class(upstage.fetch, "spend"),
    "commandcode": _with_class(commandcode.fetch, "spend"),
}


_warned_unknown_pools: set[tuple[str, str]] = set()


def registry() -> dict[str, Fetcher]:
    import sys

    from .. import policy

    pools = policy.load_config().get("pools")
    if isinstance(pools, dict):
        for name in pools.keys() - BUILTIN.keys():
            key = (str(policy.config_path()), name)
            if key not in _warned_unknown_pools:
                _warned_unknown_pools.add(key)
                print("scopefuel: warning: unknown pool section ignored", file=sys.stderr)
    return dict(BUILTIN)


def default_order(names: list[str]) -> list[str]:
    """내장 provider 를 먼저, 나머지는 알파벳 순."""
    builtin = [n for n in BUILTIN if n in names]
    rest = sorted(n for n in names if n not in BUILTIN)
    return builtin + rest
