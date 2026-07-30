"""Provider 레지스트리.

우선순위(뒤가 이김): 내장 Python provider < 선언형 TOML 스펙 < entry-point 플러그인.
스펙이 내장을 덮어쓸 수 있게 한 것은 의도적이다 — 엔드포인트가 깨졌을 때
릴리스를 기다리지 않고 사용자가 TOML 한 장으로 고칠 수 있어야 한다.
"""

from __future__ import annotations

from collections.abc import Callable

from ..model import ProviderResult
from . import agy, claude, clinepass, codex, kiro

Fetcher = Callable[[], ProviderResult]

BUILTIN: dict[str, Fetcher] = {
    "claude": claude.fetch,
    "codex": codex.fetch,
    "agy": agy.fetch,
    "kiro": kiro.fetch,
    "clinepass": clinepass.fetch,
}


def _entry_point_providers() -> dict[str, Fetcher]:
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return {}
    found: dict[str, Fetcher] = {}
    for ep in entry_points(group="scopefuel.providers"):
        try:
            found[ep.name] = ep.load()
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
