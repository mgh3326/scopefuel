"""Grade-based model recommendation using policy + quota headroom.

The static profile/model/benchmark table is the authoritative source from the
operator relay (OpenRouter rankings 2026-07-31). Profile-to-pool routing matches
``~/bin/herdr-spawn`` QUOTA GUARD, including the three CLIProxy exceptions:

- ``oc-gflash`` routes to ``agy/gemini``
- ``oc-sonnet46`` and ``oc-oss`` route to ``agy/3p``
- all other ``oc-*`` profiles route to ``clinepass``
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from .model import PoolClass, ProviderResult, _is_valid_used_pct
from .policy import get_policy

Grade = Literal["S", "A", "B", "C"]

_POOL_LABEL = {
    "claude": "Claude",
    "codex": "Codex",
    "kiro": "Kiro",
    "grok": "Grok",
    "agy": "AGY",
    "clinepass": "ClinePass",
}

_WINDOW_LABEL = {
    "5h": "시",
    "1d": "일",
    "7d": "주",
    "30d": "월",
}


@dataclass(frozen=True)
class Profile:
    name: str
    model: str
    benchmark: float | None


GRADE_TABLE: dict[Grade, list[Profile]] = {
    "S": [
        Profile("opus", "Opus 5", 60.7),
        Profile("kiro-opus", "Opus 5", 60.7),
        Profile("fable", "Fable 5", 59.9),
        Profile("codex-max", "GPT-5.6 Sol max", 58.9),
        Profile("codex-ultra", "GPT-5.6 Sol max", 58.9),
        Profile("kiro-sol", "GPT-5.6 Sol max", 58.9),
    ],
    "A": [
        Profile("oc-kimi-k3", "Kimi K3", 57.1),
        Profile("codex-med", "Terra", 55.0),
        Profile("grok-hi", "Grok 4.5", 53.8),
        Profile("sonnet", "Sonnet 5", 53.4),
        Profile("kiro-sonnet", "Sonnet 5", 53.4),
    ],
    "B": [
        Profile("codex-luna", "Luna", 51.2),
        Profile("oc-glm", "GLM-5.2", 51.1),
        Profile("oc-gflash", "Gemini 3.6 Flash", 50.1),
    ],
    "C": [
        Profile("oc-sonnet46", "Sonnet 4.6", 47.2),
        Profile("agy-pro", "Gemini 3.1 Pro", 46.5),
        Profile("oc-kimi-code", "Kimi K2.7 Code", None),
        Profile("oc-dsflash", "DeepSeek V4 Flash", None),
        Profile("oc-oss", "GPT-OSS 120B", None),
        Profile("kiro-cheap", "Qwen3 Coder", None),
    ],
}


def profile_pool(profile: str) -> tuple[str, str | None]:
    """Return (provider_id, group_name_if_group_scope)."""
    if profile in ("opus", "sonnet", "fable"):
        return "claude", None
    if profile.startswith("codex") or profile == "claudex":
        return "codex", None
    if profile in ("agy", "agy-flash", "agy-flash-med", "agy-pro"):
        return "agy", "gemini"
    if profile in ("agy-sonnet", "agy-opus", "agy-oss"):
        return "agy", "3p"
    if profile.startswith("kiro"):
        return "kiro", None
    if profile == "oc-gflash":
        return "agy", "gemini"
    if profile in ("oc-sonnet46", "oc-oss"):
        return "agy", "3p"
    if profile.startswith("oc-"):
        return "clinepass", None
    if profile in ("grok", "grok-hi", "grok-med"):
        return "grok", None
    return "", None


@dataclass
class _Candidate:
    profile: Profile
    provider_label: str
    window: str
    used_pct: float
    remaining_pct: float
    pool_class: PoolClass
    reset_at: str | None


@dataclass
class _Excluded:
    profile: Profile
    reason: str


def _matching_buckets(result: ProviderResult, group_name: str | None) -> list[tuple[float, str, str | None]]:
    out: list[tuple[float, str, str | None]] = []
    for bucket in result.buckets:
        if not _is_valid_used_pct(bucket.used_pct):
            continue
        scope = bucket.scope
        if group_name is None:
            if scope.kind != "account":
                continue
        else:
            if scope.kind != "group" or scope.label != group_name:
                continue
        assert isinstance(bucket.used_pct, (int, float))
        out.append((float(bucket.used_pct), bucket.window, bucket.resets_at))
    return out


def _reset_display(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        parsed = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%m-%d")
    except ValueError:
        return iso[:10]


def recommend(providers: list[ProviderResult], grade: Grade, today: dt.date | None = None) -> str:
    by_id = {r.id: r for r in providers}
    included: list[_Candidate] = []
    excluded: list[_Excluded] = []

    for profile in GRADE_TABLE[grade]:
        provider_id, group_name = profile_pool(profile.name)
        result = by_id.get(provider_id)
        if result is None or result.error or result.warning or result.status != "ok":
            excluded.append(_Excluded(profile, "측정 불가"))
            continue

        matches = _matching_buckets(result, group_name)
        if not matches:
            excluded.append(_Excluded(profile, "측정 불가"))
            continue

        used_pct, window, reset_at = max(matches, key=lambda m: m[0])
        if used_pct >= 90:
            excluded.append(_Excluded(profile, f"{used_pct:g}% 소진 (reset {_reset_display(reset_at)})"))
            continue

        builtin_class: PoolClass = result.pool_class
        effective_class = get_policy(provider_id, builtin_class, today=today)[0]
        provider_label = _POOL_LABEL.get(provider_id, provider_id)
        if provider_id == "agy" and group_name:
            provider_label = f"AGY {group_name}"

        included.append(
            _Candidate(
                profile=profile,
                provider_label=provider_label,
                window=_WINDOW_LABEL.get(window, window),
                used_pct=used_pct,
                remaining_pct=100.0 - used_pct,
                pool_class=effective_class,
                reset_at=reset_at,
            )
        )

    # Spend pools first, preserve later. Within the same class, keep the static
    # profile/table order; headroom is displayed as evidence and used only as a
    # deterministic tie-break when two profiles happen to share the exact same
    # class and priority (which the static table order already prevents).
    def sort_key(c: _Candidate) -> tuple[int, int, float]:
        class_order = 0 if c.pool_class == "spend" else 1
        profile_order = next((i for i, p in enumerate(GRADE_TABLE[grade]) if p.name == c.profile.name), 0)
        return (class_order, profile_order, -c.remaining_pct)

    included.sort(key=sort_key)

    lines: list[str] = []
    for rank, cand in enumerate(included, start=1):
        bench = f"  벤치 {cand.profile.benchmark}" if cand.profile.benchmark is not None else ""
        lines.append(
            f"{rank}. {cand.profile.name:<12} {cand.provider_label} {cand.window} {cand.used_pct:g}%  "
            f"{cand.pool_class:<7}{bench}"
        )
    for item in excluded:
        lines.append(f"✗ {item.profile.name:<12} {item.reason}")
    return "\n".join(lines)
