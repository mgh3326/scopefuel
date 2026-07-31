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

from .model import PoolClass, ProviderResult, _is_valid_used_pct, _parse_reset
from .policy import get_active_override, get_policy

Grade = Literal["S", "A", "B", "C"]

# spend 풀: 이 사용률 미만이면 후보. preserve 는 90.
PRESERVE_EXCLUDE_PCT = 90.0
SPEND_EXCLUDE_PCT = 99.0
# spend 풀이 리셋까지 이 시간 이내이면 정렬 최상위 + 🔥.
RESET_URGENCY_HOURS = 12.0

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
    provider_id: str
    window: str
    used_pct: float
    remaining_pct: float
    pool_class: PoolClass
    reset_at: str | None
    hours_to_reset: float | None
    urgent: bool


@dataclass
class _Excluded:
    profile: Profile
    reason: str


@dataclass
class _PolicyExcluded:
    profile: Profile
    provider_id: str
    provider_label: str
    until: dt.date | None
    note: str | None


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


def _hours_to_reset(iso: str | None, now: dt.datetime) -> float | None:
    reset_at = _parse_reset(iso)
    if reset_at is None:
        return None
    now_utc = now.replace(tzinfo=dt.UTC) if now.tzinfo is None else now.astimezone(dt.UTC)
    hours = (reset_at - now_utc).total_seconds() / 3600.0
    return hours if hours > 0 else None


def _format_hours(hours: float) -> str:
    if hours >= 1:
        # trim trailing zeros: 3.0 -> 3, 3.25 -> 3.25
        text = f"{hours:.1f}".rstrip("0").rstrip(".")
        return f"{text}h"
    minutes = hours * 60.0
    text = f"{minutes:.0f}"
    return f"{text}m"


def _usage_cutoff(pool_class: PoolClass) -> float:
    if pool_class == "spend":
        return SPEND_EXCLUDE_PCT
    return PRESERVE_EXCLUDE_PCT


def _provider_label(provider_id: str, group_name: str | None) -> str:
    label = _POOL_LABEL.get(provider_id, provider_id)
    if provider_id == "agy" and group_name:
        return f"AGY {group_name}"
    return label


def _policy_reason(item: _PolicyExcluded) -> str:
    parts = [f"정책 제외 ({item.provider_id}"]
    if item.until is not None:
        parts.append(f"until {item.until.isoformat()}")
    if item.note:
        parts.append(item.note)
    return ", ".join(parts) + ")"


def recommend(
    providers: list[ProviderResult],
    grade: Grade,
    today: dt.date | None = None,
    now: dt.datetime | None = None,
    *,
    urgency_hours: float = RESET_URGENCY_HOURS,
) -> str:
    today = today or dt.datetime.now(dt.UTC).date()
    now = now or dt.datetime.now(dt.UTC)
    by_id = {r.id: r for r in providers}
    included: list[_Candidate] = []
    excluded: list[_Excluded] = []
    policy_excluded: list[_PolicyExcluded] = []

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
        # result.pool_class may already include a policy stamp from cache; only preserve/spend
        # are valid builtins. get_policy re-reads XDG config for the effective class.
        fallback_class: PoolClass = (
            result.pool_class if result.pool_class in ("preserve", "spend") else "preserve"
        )
        effective_class = get_policy(provider_id, fallback_class, today=today)[0]
        override = get_active_override(provider_id, today=today)

        provider_label = _provider_label(provider_id, group_name)

        if effective_class == "exclude":
            policy_excluded.append(
                _PolicyExcluded(
                    profile=profile,
                    provider_id=provider_id,
                    provider_label=provider_label,
                    until=override.until if override is not None else None,
                    note=override.note if override is not None else None,
                )
            )
            continue

        cutoff = _usage_cutoff(effective_class)
        if used_pct >= cutoff:
            excluded.append(_Excluded(profile, f"{used_pct:g}% 소진 (reset {_reset_display(reset_at)})"))
            continue

        remaining_pct = 100.0 - used_pct
        hours = _hours_to_reset(reset_at, now)
        urgent = (
            effective_class == "spend" and remaining_pct > 0 and hours is not None and hours <= urgency_hours
        )
        included.append(
            _Candidate(
                profile=profile,
                provider_label=provider_label,
                provider_id=provider_id,
                window=_WINDOW_LABEL.get(window, window),
                used_pct=used_pct,
                remaining_pct=remaining_pct,
                pool_class=effective_class,
                reset_at=reset_at,
                hours_to_reset=hours,
                urgent=urgent,
            )
        )

    # reset 임박 → class(spend > preserve) → 잔여율(큰 순) → 표 순서(결정성)
    def sort_key(c: _Candidate) -> tuple[int, int, float, int]:
        imminent = 0 if c.urgent else 1
        class_order = 0 if c.pool_class == "spend" else 1
        profile_order = next((i for i, p in enumerate(GRADE_TABLE[grade]) if p.name == c.profile.name), 0)
        return (imminent, class_order, -c.remaining_pct, profile_order)

    included.sort(key=sort_key)

    lines: list[str] = []
    if not included and policy_excluded:
        lines.append("✗ 정책 가용 후보 없음")
        lines.append("⚠ 비상 후보 (정책상 제외 — 사용 시 근거를 이슈에 기록할 것)")
        for item in policy_excluded:
            until_s = item.until.isoformat() if item.until is not None else "-"
            note_s = f"  {item.note}" if item.note else ""
            lines.append(
                f"  {item.profile.name:<12} {item.provider_label}  pool={item.provider_id}"
                f"  until={until_s}{note_s}"
            )
    else:
        for rank, cand in enumerate(included, start=1):
            bench = f"  벤치 {cand.profile.benchmark}" if cand.profile.benchmark is not None else ""
            if cand.urgent and cand.hours_to_reset is not None:
                lines.append(
                    f"{rank}. 🔥 {cand.profile.name:<10} {cand.provider_label} {cand.window} "
                    f"{cand.used_pct:g}%  {cand.pool_class:<7}"
                    f"잔여 {cand.remaining_pct:g}% · 리셋 {_format_hours(cand.hours_to_reset)}{bench}"
                )
            else:
                lines.append(
                    f"{rank}. {cand.profile.name:<12} {cand.provider_label} "
                    f"{cand.window} {cand.used_pct:g}%  {cand.pool_class:<7}{bench}"
                )
        for item in policy_excluded:
            lines.append(f"✗ {item.profile.name:<12} {_policy_reason(item)}")

    for item in excluded:
        lines.append(f"✗ {item.profile.name:<12} {item.reason}")
    return "\n".join(lines)
