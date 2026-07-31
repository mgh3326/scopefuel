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

Grade = Literal["S+", "S", "A+", "A", "B", "C"]
Gate = Literal["default", "escalation"]

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
    "omniroute": "OmniRoute",
}

_WINDOW_LABEL = {
    "5h": "시",
    "1d": "일",
    "7d": "주",
    "30d": "월",
}

# quota-0 free candidate: 측정 불필, 항상 가용.
_FREE_PROFILES = frozenset({"oc-omni"})


@dataclass(frozen=True)
class GradeInfo:
    task_class: str
    decision_question: str


GRADE_DISCRIM: dict[Grade, GradeInfo] = {
    "S+": GradeInfo(
        "되돌리기 어려운 판단, 설계 분기, 안전장치 설계, 다중 시스템 영향",
        "틀리면 되돌리는 비용이 큰가?",
    ),
    "S": GradeInfo(
        "복잡한 구현, 새 추상화, 적대검증/급소 찾기",
        "무엇이 잘못될 수 있는지 스스로 열거해야 하나?",
    ),
    "A+": GradeInfo(
        "일반 기능 구현, 기존 패턴 확장, 다단계 리팩터",
        "방향은 정해졌고 설계 판단이 좀 남았나?",
    ),
    "A": GradeInfo(
        "명세 확정 구현, 테스트 작성, 국소 수정",
        "무엇을 만들지 문서에 다 적혀 있나?",
    ),
    "B": GradeInfo(
        "기계적 변경, 리네이밍, 포맷, 문서 반영",
        "정답이 유일하고 검색·치환에 가까운가?",
    ),
    "C": GradeInfo(
        "단순 변환, 로그 파싱, 대량 생성",
        "실패해도 버리고 다시 하면 되나?",
    ),
}


def grade_help_text() -> str:
    lines = ["판별표:"]
    for g in ("S+", "S", "A+", "A", "B", "C"):
        info = GRADE_DISCRIM[g]
        lines.append(f"  {g:<3} {info.task_class}  |  {info.decision_question}")
    return "\n".join(lines)


@dataclass(frozen=True)
class Profile:
    name: str
    model: str
    benchmark: float | None
    gate: Gate = "default"
    gate_reason: str | None = None


GRADE_TABLE: dict[Grade, list[Profile]] = {
    "S+": [
        Profile("kiro-opus", "Opus 5", 60.7),
        Profile("kiro-sol", "GPT-5.6 Sol", 58.9),
        Profile("codex-max", "GPT-5.6 Sol (max/ultra)", 58.9),
        Profile("opus", "Opus 5", 60.7),
        Profile(
            "fable",
            "Fable 5",
            59.9,
            gate="escalation",
            gate_reason=(
                "Opus 5 대비 2배 가격 — 2h+ 자율실행 / Opus5 실패 후 / "
                "고위험 1회성 / 서브에이전트 다수일 때만"
            ),
        ),
    ],
    "S": [
        Profile("codex-terra-max", "Terra (max)", 78.0),
        Profile("oc-kimi-k3", "Kimi K3", 57.1),
        Profile("grok-hi", "Grok 4.5", 53.8),
    ],
    "A+": [
        Profile("kiro-sonnet", "Sonnet 5", 53.4),
        Profile("codex-luna-ultra", "Luna (ultra)", 75.0),
        Profile("oc-glm", "GLM-5.2", 51.1),
        Profile("sonnet", "Sonnet 5", 53.4),
    ],
    "A": [
        Profile("oc-gflash", "Gemini 3.6 Flash", 50.1),
        Profile("oc-kimi-code", "Kimi K2.7 Code", None),
        Profile("oc-sonnet46", "Sonnet 4.6", 47.2),
    ],
    "B": [
        Profile("kiro-haiku", "Haiku 4.5", None),
        Profile("oc-dsflash", "DeepSeek V4 Flash", None),
    ],
    "C": [
        Profile("oc-omni", "OmniRoute free", None),
        Profile("kiro-cheap", "Qwen3 Coder", None),
        Profile(
            "oc-oss",
            "GPT-OSS 120B",
            None,
            gate="escalation",
            gate_reason="agy 3p 풀 소모 — 다른 C 후보 소진 시에만",
        ),
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
    if profile == "oc-omni":
        return "omniroute", None
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


@dataclass
class _EscalationEntry:
    profile: Profile
    provider_label: str
    provider_id: str
    gate_reason: str
    status_note: str | None


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


def _build_escalation_entry(
    profile: Profile,
    by_id: dict[str, ProviderResult],
    today: dt.date,
) -> _EscalationEntry:
    provider_id, group_name = profile_pool(profile.name)
    provider_label = _provider_label(provider_id, group_name)
    gate_reason = profile.gate_reason or ""

    result = by_id.get(provider_id)
    fallback_class: PoolClass = (
        result.pool_class if result and result.pool_class in ("preserve", "spend") else "preserve"
    )
    effective_class = get_policy(provider_id, fallback_class, today=today)[0]
    override = get_active_override(provider_id, today=today)

    status_notes: list[str] = []

    if effective_class == "exclude":
        reason_parts = [f"정책 제외 ({provider_id}"]
        if override is not None and override.until:
            reason_parts.append(f"until {override.until.isoformat()}")
        if override is not None and override.note:
            reason_parts.append(override.note)
        status_notes.append(", ".join(reason_parts) + ")")

    if result is None or result.error or result.warning or result.status != "ok":
        status_notes.append("측정 불가")
    else:
        matches = _matching_buckets(result, group_name)
        if not matches:
            status_notes.append("측정 불가")
        else:
            used_pct, _window, reset_at = max(matches, key=lambda m: m[0])
            cutoff = _usage_cutoff(effective_class)
            if used_pct >= cutoff:
                status_notes.append(f"{used_pct:g}% 소진 (reset {_reset_display(reset_at)})")
            else:
                status_notes.append(f"사용 {used_pct:g}% (reset {_reset_display(reset_at)})")

    return _EscalationEntry(
        profile=profile,
        provider_label=provider_label,
        provider_id=provider_id,
        gate_reason=gate_reason,
        status_note=" | ".join(status_notes) if status_notes else None,
    )


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
    escalation: list[_EscalationEntry] = []

    for profile in GRADE_TABLE[grade]:
        # Escalation profiles → separate section, not in normal ranked candidates.
        if profile.gate == "escalation":
            escalation.append(_build_escalation_entry(profile, by_id, today))
            continue

        provider_id, group_name = profile_pool(profile.name)
        provider_label = _provider_label(provider_id, group_name)

        # quota-0 free candidates: always available, no measurement needed.
        if profile.name in _FREE_PROFILES:
            included.append(
                _Candidate(
                    profile=profile,
                    provider_label=provider_label,
                    provider_id=provider_id,
                    window="free",
                    used_pct=0.0,
                    remaining_pct=100.0,
                    pool_class="spend",
                    reset_at=None,
                    hours_to_reset=None,
                    urgent=False,
                )
            )
            continue

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

    # free(quota-0) → reset 임박 → class(spend > preserve) → 잔여율(큰 순) → 표 순서(결정성)
    def sort_key(c: _Candidate) -> tuple[int, int, int, float, int]:
        free_order = 0 if c.profile.name in _FREE_PROFILES else 1
        imminent = 0 if c.urgent else 1
        class_order = 0 if c.pool_class == "spend" else 1
        profile_order = next((i for i, p in enumerate(GRADE_TABLE[grade]) if p.name == c.profile.name), 0)
        return (free_order, imminent, class_order, -c.remaining_pct, profile_order)

    included.sort(key=sort_key)

    lines: list[str] = []

    # Grade discrimination header — LLM-read table.
    info = GRADE_DISCRIM[grade]
    lines.append(f"{grade} | {info.task_class} | {info.decision_question}")

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

    if escalation:
        lines.append("⚠ 승급 후보 (조건 충족 시에만 · 근거를 이슈에 기록)")
        for entry in escalation:
            lines.append(f"  {entry.profile.name:<12} {entry.provider_label}  pool={entry.provider_id}")
            lines.append(f"    근거: {entry.gate_reason}")
            if entry.status_note:
                lines.append(f"    {entry.status_note}")

    return "\n".join(lines)
