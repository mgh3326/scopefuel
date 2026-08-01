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
import json
import os
import pathlib
from dataclasses import dataclass
from typing import Literal

from .bench import ModelScore, display_effort, normalize_aa_model_id
from .model import PoolClass, ProviderResult, _is_valid_used_pct, _parse_reset, _window_seconds
from .policy import (
    get_active_override,
    get_boost,
    get_capacity_weight,
    get_imminent_remaining_pct,
    get_imminent_reset_hours,
    get_policy,
    get_reset_urgency_hours,
)

Grade = Literal["S+", "S", "A+", "A", "B", "C"]
Gate = Literal["default", "escalation"]

# spend 풀: 이 사용률 미만이면 후보. preserve 는 90.
PRESERVE_EXCLUDE_PCT = 90.0
SPEND_EXCLUDE_PCT = 99.0
# spend 풀이 리셋까지 이 시간 이내이면 정렬 최상위 + 🔥 (fallback — pace 계산 불가 시에만).
RESET_URGENCY_HOURS = 12.0

# ROB-1191 ② continuous score component weights (explainable fixed constants).
# total = capacity_term + WASTE_WEIGHT * waste_term + THRU_WEIGHT * throughput_term
# Waste is weighted above capacity so reset-expiry risk outranks raw remaining%
# (otherwise a nearly-full far-reset pool always beats a soon-to-expire remainder).
SCORE_WASTE_WEIGHT = 50.0
SCORE_THRU_WEIGHT = 0.25
# Short windows (≤6h) contribute the throughput-margin term.
SHORT_WINDOW_MAX_S = 6 * 3600
# Pace-unknown constraint fallback: treat as less constraining than any finite TTE,
# ordered by used_pct (higher usage = more constrained). Deterministic & conservative.
_TTE_UNKNOWN_RANK = 1
_TTE_KNOWN_RANK = 0

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
    "5h": "5h",
    "1d": "일",
    "7d": "주",
    "30d": "월",
}

# Claude settings.json (read-only). Overridable for tests via CLAUDE_SETTINGS_PATH.
_DEFAULT_CLAUDE_SETTINGS = pathlib.Path.home() / ".claude" / "settings.json"

OC_OMNI_ESCALATION_REASON = (
    "비결정적 — 실행 모델이 요청마다 다름(실측: big-pickle 162콜·deepseek-v4-flash 21콜·"
    "죽은 후보 3종). 다른 C 후보가 전부 소진·측정불가일 때만"
)


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
    benchmark_source: str | None = None
    benchmark_metric: str | None = None
    benchmark_harness: str | None = None
    benchmark_effort: str | None = None
    benchmark_model_id: str | None = None
    # ROB-1190 ②-3/②-4: source-specific AA lookup keys. AA-agent wins when present;
    # AA-model is queried only when the mapped AA-agent has no score.
    aa_agent_model_id: str | None = None
    aa_model_id: str | None = None


def _openrouter_benchmark(score: float, model_id: str) -> dict[str, object]:
    return {
        "benchmark_source": "openrouter",
        "benchmark_metric": "coding",
        "benchmark_harness": None,
        "benchmark_effort": None,
        "benchmark_model_id": model_id,
    }


def _aa_agent_benchmark(
    score: float, model_id: str, effort: str, *, harness: str = "codex"
) -> dict[str, object]:
    return {
        "benchmark_source": "AA-agent",
        "benchmark_metric": "agentic",
        "benchmark_harness": harness,
        "benchmark_effort": effort,
        "benchmark_model_id": model_id,
    }


GRADE_TABLE: dict[Grade, list[Profile]] = {
    "S+": [
        Profile(
            "kiro-opus",
            "Opus 5 (xhigh)",
            67.0,
            **_aa_agent_benchmark(67.0, "claude-opus-5", "xhigh", harness="claude-code"),
            aa_agent_model_id="claude-opus-5",
            aa_model_id="claude-opus-5",
        ),
        Profile(
            "kiro-sol",
            "GPT-5.6 Sol",
            58.9,
            **_openrouter_benchmark(58.9, "gpt-5.6-sol"),
            aa_agent_model_id="gpt-5.6-sol",
            aa_model_id="gpt-5-6-sol",
        ),
        Profile(
            "codex-max",
            "GPT-5.6 Sol (max)",
            67.0,
            gate_reason="측정 없음 — 장기 오케스트레이션 전용, reps 로 검증 예정",
            **_aa_agent_benchmark(67.0, "gpt-5.6-sol", "max"),
            aa_agent_model_id="gpt-5.6-sol",
            aa_model_id="gpt-5-6-sol",
        ),
        Profile(
            "opus",
            "Opus 5",
            60.7,
            # Operator intent: advertised/default effort is xhigh (wrk + AA-agent).
            **{**_openrouter_benchmark(60.7, "opus-5"), "benchmark_effort": "xhigh"},
            aa_agent_model_id="claude-opus-5",
            aa_model_id="claude-opus-5",
        ),
        Profile(
            "fable",
            "Fable 5",
            59.9,
            gate="escalation",
            gate_reason=(
                "Opus 5 대비 2배 가격 — 2h+ 자율실행 / Opus5 실패 후 / "
                "고위험 1회성 / 서브에이전트 다수일 때만"
            ),
            **_openrouter_benchmark(59.9, "fable-5"),
            aa_agent_model_id="claude-fable-5",
            aa_model_id="claude-fable-5",
        ),
    ],
    "S": [
        Profile(
            "codex-terra-max",
            "Terra (max)",
            62.0,
            **_aa_agent_benchmark(62.0, "gpt-5.6-terra", "max"),
            aa_agent_model_id="gpt-5.6-terra",
            aa_model_id="gpt-5-6-terra",
        ),
        Profile(
            "oc-kimi-k3",
            "Kimi K3",
            57.1,
            **_openrouter_benchmark(57.1, "kimi-k3"),
            aa_agent_model_id="kimi-k3",
            aa_model_id="kimi-k3",
        ),
        Profile(
            "grok-hi",
            "Grok 4.5",
            53.8,
            **_openrouter_benchmark(53.8, "grok-4.5"),
            aa_agent_model_id="grok-4.5",
            aa_model_id="grok-4-5",
        ),
    ],
    "A+": [
        Profile(
            "kiro-sonnet",
            "Sonnet 5",
            53.4,
            **_openrouter_benchmark(53.4, "sonnet-5"),
            aa_model_id="claude-sonnet-5",
        ),
        Profile(
            "codex-luna-max",
            "Luna (max)",
            59.0,
            **_aa_agent_benchmark(59.0, "gpt-5.6-luna", "max"),
            aa_agent_model_id="gpt-5.6-luna",
            aa_model_id="gpt-5-6-luna",
        ),
        Profile(
            "oc-glm",
            "GLM-5.2",
            51.1,
            **_openrouter_benchmark(51.1, "glm-5.2"),
            aa_agent_model_id="glm-5.2",
            aa_model_id="glm-5-2",
        ),
        Profile(
            "sonnet",
            "Sonnet 5",
            53.4,
            # Align with wrk DEFAULT_EFFORT=high and ~/.claude/settings.json effortLevel.
            **{
                **_openrouter_benchmark(53.4, "sonnet-5"),
                "benchmark_effort": "high",
            },
            aa_model_id="claude-sonnet-5",
        ),
    ],
    "A": [
        Profile(
            "oc-gflash",
            "Gemini 3.6 Flash",
            50.1,
            **_openrouter_benchmark(50.1, "gemini-3.6-flash"),
            aa_model_id="gemini-3-6-flash",
        ),
        Profile("oc-kimi-code", "Kimi K2.7 Code", None),
        Profile(
            "oc-sonnet46",
            "Sonnet 4.6",
            47.2,
            **_openrouter_benchmark(47.2, "sonnet-4.6"),
            aa_agent_model_id="claude-sonnet-4.6",
            aa_model_id="claude-sonnet-4-6",
        ),
    ],
    "B": [
        Profile("kiro-haiku", "Haiku 4.5", None, aa_model_id="claude-4-5-haiku"),
        Profile("oc-dsflash", "DeepSeek V4 Flash", None, aa_model_id="deepseek-v4-flash"),
    ],
    "C": [
        Profile("kiro-cheap", "Qwen3 Coder", None, aa_model_id="qwen3-coder-next"),
        Profile(
            "oc-omni",
            "OmniRoute free",
            None,
            gate="escalation",
            gate_reason=OC_OMNI_ESCALATION_REASON,
        ),
        Profile(
            "oc-oss",
            "GPT-OSS 120B",
            None,
            gate="escalation",
            gate_reason="agy 3p 풀 소모 — 다른 C 후보 소진 시에만",
            aa_model_id="gpt-oss-120b",
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


@dataclass(frozen=True)
class _WindowState:
    """Per-window usage snapshot used for multi-window cutoff + constraint selection."""

    window: str  # raw provider window id, e.g. "5h", "7d"
    used_pct: float
    remaining_pct: float
    reset_at: str | None
    hours_to_reset: float | None
    burn_rate: float | None  # %/h when measurable
    time_to_exhaust: float | None  # hours; None when completely unmeasurable

    @property
    def display_window(self) -> str:
        return _WINDOW_LABEL.get(self.window, self.window)


@dataclass
class _Candidate:
    profile: Profile
    provider_label: str
    provider_id: str
    windows: list[_WindowState]
    constraint: _WindowState
    windows_display: str
    used_pct: float  # constraining window
    remaining_pct: float
    pool_class: PoolClass
    reset_at: str | None
    hours_to_reset: float | None
    urgent: bool
    boost: int | None
    weight: float
    effective_remaining: float
    # Continuous score components (ROB-1191 ②) — higher total ranks first under boost.
    capacity_term: float = 0.0
    waste_term: float = 0.0
    throughput_term: float = 0.0
    score: float = 0.0
    imminent_exhaustion: bool = False


@dataclass
class _Excluded:
    profile: Profile
    reason: str
    provider_id: str = ""
    kind: str = "other"  # "exhausted" | "unmeasurable" | "other"


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


def _burn_rate_pct_per_hour(window: str, used_pct: float, hours_to_reset: float) -> float | None:
    """실측 소모속도(%/h) = used_pct / 경과시간(h). 창 길이·리셋 파싱 불가 시 None."""
    window_seconds = _window_seconds(window)
    if window_seconds is None or window_seconds <= 0:
        return None
    elapsed_hours = (window_seconds / 3600.0) - hours_to_reset
    if elapsed_hours <= 0:
        return None
    if used_pct <= 0:
        return 0.0
    return used_pct / elapsed_hours


def _time_to_exhaust_hours(
    remaining_pct: float,
    burn_rate: float | None,
    hours_to_reset: float | None,
) -> float | None:
    """Hours until the window is exhausted at current burn rate.

    Fallback when pace is unmeasurable (conservative + deterministic):
    - remaining ≤ 0 → already exhausted (0)
    - hours_to_reset known → use that as an upper bound on usable time
      (assumes quota is gone by reset even without a measured burn rate)
    - otherwise None (completely unknown; ranked after any known TTE)
    """
    if remaining_pct <= 0:
        return 0.0
    if burn_rate is not None and burn_rate > 0:
        return remaining_pct / burn_rate
    if hours_to_reset is not None:
        return hours_to_reset
    return None


def _is_pace_urgent(remaining_pct: float, burn_rate: float | None, hours_to_reset: float) -> bool | None:
    """잔여%/소모속도 > reset까지 남은 시간 → 리셋 전에 다 못 쓴다 → 상향 대상.

    반환: True/False = pace 로 결정, None = pace 계산 불가(폴백 필요).
    """
    if burn_rate is None or burn_rate <= 0:
        return None
    time_to_exhaust = remaining_pct / burn_rate
    return time_to_exhaust > hours_to_reset


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


def _window_states(
    matches: list[tuple[float, str, str | None]],
    now: dt.datetime,
) -> list[_WindowState]:
    states: list[_WindowState] = []
    for used_pct, window, reset_at in matches:
        remaining = 100.0 - used_pct
        hours = _hours_to_reset(reset_at, now)
        burn = _burn_rate_pct_per_hour(window, used_pct, hours) if hours is not None else None
        tte = _time_to_exhaust_hours(remaining, burn, hours)
        states.append(
            _WindowState(
                window=window,
                used_pct=used_pct,
                remaining_pct=remaining,
                reset_at=reset_at,
                hours_to_reset=hours,
                burn_rate=burn,
                time_to_exhaust=tte,
            )
        )
    return states


def _constraint_sort_key(state: _WindowState) -> tuple[int, float, float, str]:
    """Smaller key = more constraining. Known TTE first; fallback by used_pct desc."""
    if state.time_to_exhaust is not None:
        return (_TTE_KNOWN_RANK, state.time_to_exhaust, -state.used_pct, state.window)
    # Pace/reset unknown: higher used_pct is more constrained; never invent a TTE.
    return (_TTE_UNKNOWN_RANK, 0.0, -state.used_pct, state.window)


def _select_constraint(states: list[_WindowState]) -> _WindowState:
    return min(states, key=_constraint_sort_key)


def _any_window_over_cutoff(states: list[_WindowState], cutoff: float) -> _WindowState | None:
    """Return the worst over-cutoff window (highest used_pct), or None if all under."""
    over = [s for s in states if s.used_pct >= cutoff]
    if not over:
        return None
    return max(over, key=lambda s: (s.used_pct, s.window))


def _format_windows_display(states: list[_WindowState], constraint: _WindowState) -> str:
    """e.g. '5h 31% · 주 46% · 제약=5h'"""
    # Stable display order: short windows first, then by window id.
    ordered = sorted(
        states,
        key=lambda s: (
            _window_seconds(s.window) if _window_seconds(s.window) is not None else 10**12,
            s.window,
        ),
    )
    parts = [f"{s.display_window} {s.used_pct:g}%" for s in ordered]
    parts.append(f"제약={constraint.display_window}")
    return " · ".join(parts)


def _score_components(
    *,
    states: list[_WindowState],
    constraint: _WindowState,
    weight: float,
    pool_class: PoolClass,
    urgency_hours: float,
) -> tuple[float, float, float, float]:
    """Return (capacity, waste, throughput, total) continuous score terms.

    (a) capacity — constraining-window remaining × capacity_weight
    (b) waste — spend-only unused remainder that current pace will not consume before reset
        (pace unknown + reset within urgency_hours → entire remainder counted as at-risk)
    (c) throughput — remaining on the tightest short window (draw-rate headroom)
    """
    capacity = weight * constraint.remaining_pct

    waste = 0.0
    if pool_class == "spend":
        br = constraint.burn_rate
        hours = constraint.hours_to_reset
        if br is not None and br > 0 and hours is not None:
            waste = max(0.0, constraint.remaining_pct - br * hours)
        elif hours is not None and br == 0.0:
            # Measured zero burn with remaining → entire remainder at risk of expiry.
            waste = constraint.remaining_pct
        elif hours is not None and br is None and hours <= urgency_hours:
            # Pace unmeasurable: conservative fallback matches 🔥 threshold.
            waste = constraint.remaining_pct

    short = [
        s for s in states if (secs := _window_seconds(s.window)) is not None and secs <= SHORT_WINDOW_MAX_S
    ]
    # Tightest short window by remaining (lower remaining = less throughput headroom).
    throughput = min(s.remaining_pct for s in short) if short else constraint.remaining_pct

    total = capacity + SCORE_WASTE_WEIGHT * waste + SCORE_THRU_WEIGHT * throughput
    return capacity, waste, throughput, total


def _read_claude_settings_effort() -> str | None:
    """Read-only Claude settings effortLevel. Never writes. Returns None if unreadable."""
    path_raw = os.environ.get("CLAUDE_SETTINGS_PATH")
    path = pathlib.Path(path_raw) if path_raw else _DEFAULT_CLAUDE_SETTINGS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    level = data.get("effortLevel")
    if isinstance(level, str) and level.strip():
        return level.strip()
    return None


def resolve_display_effort(profile: Profile) -> tuple[str | None, str]:
    """Resolve the single effort row to display for a profile.

    Order:
    1. Profile.benchmark_effort (declared)
    2. Claude profiles: read-only settings.json effortLevel
    3. Unconfirmable → (None, "unknown") — caller must show 미지정, no best-score pick

    Returns (effort_or_None, provenance) where provenance is profile|settings|unknown.
    """
    if profile.benchmark_effort:
        return profile.benchmark_effort, "profile"
    provider_id, _ = profile_pool(profile.name)
    if provider_id == "claude":
        settings_effort = _read_claude_settings_effort()
        if settings_effort:
            return settings_effort, "settings"
    return None, "unknown"


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
    now: dt.datetime | None = None,
) -> _EscalationEntry:
    provider_id, group_name = profile_pool(profile.name)
    provider_label = _provider_label(provider_id, group_name)
    gate_reason = profile.gate_reason or ""
    now = now or dt.datetime.now(dt.UTC)

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
            states = _window_states(matches, now)
            constraint = _select_constraint(states)
            cutoff = _usage_cutoff(effective_class)
            over = _any_window_over_cutoff(states, cutoff)
            if over is not None:
                status_notes.append(f"{over.used_pct:g}% 소진 (reset {_reset_display(over.reset_at)})")
            else:
                status_notes.append(f"사용 {_format_windows_display(states, constraint)}")

    return _EscalationEntry(
        profile=profile,
        provider_label=provider_label,
        provider_id=provider_id,
        gate_reason=gate_reason,
        status_note=" | ".join(status_notes) if status_notes else None,
    )


@dataclass(frozen=True)
class GateResult:
    """``scopefuel gate`` 판정 결과."""

    ok: bool
    profile: str
    provider_id: str
    grade: Grade | None
    reason: str  # exit 0 이면 사람이 읽는 요약, 아니면 차단/측정불가 사유
    used_pct: float | None = None
    pool_class: PoolClass | None = None
    unmeasurable: bool = False
    alternatives: tuple[str, ...] = ()


def _find_profile(profile_name: str) -> tuple[Grade, Profile] | None:
    for grade, profiles in GRADE_TABLE.items():
        for profile in profiles:
            if profile.name == profile_name:
                return grade, profile
    return None


def _alt_candidates(
    providers: list[ProviderResult],
    grade: Grade,
    exclude_profile: str,
    today: dt.date,
    now: dt.datetime,
    urgency_hours: float,
) -> tuple[str, ...]:
    """같은 grade 안에서 exclude_profile 을 뺀 사용 가능한 정상(비-escalation) 후보 이름."""
    out = recommend(providers, grade, today=today, now=now, urgency_hours=urgency_hours)
    names: list[str] = []
    for line in out.splitlines():
        if not line[:1].isdigit():
            continue
        # "N. [🔥] name  label ..." 또는 "N. name  label ..."
        rest = line.split(".", 1)[1].strip()
        token = rest.split()[1] if rest.startswith("🔥") else rest.split()[0]
        if token != exclude_profile:
            names.append(token)
    return tuple(names)


# escalation 자격 충족 후에도 quota provider 측정을 요구하지 않고 즉시 통과시키는 프로필.
# 명시적 무료 레인(oc-omni)에 한정 — 다른 escalation 프로필로 일반화하지 않는다.
_ESCALATION_SKIPS_QUOTA_CHECK = frozenset({"oc-omni"})


def gate_check(
    providers: list[ProviderResult],
    profile_name: str,
    today: dt.date | None = None,
    now: dt.datetime | None = None,
    *,
    urgency_hours: float | None = None,
) -> GateResult:
    """profile 하나에 대한 스폰 가능 여부 판정. unknown profile 은 호출자(CLI)가 먼저 걸러낸다.

    escalation 프로필은 "같은 grade 정상 대안이 전부 비가용"이라는 자격을 먼저 확인한다.
    자격 충족은 추가 자격일 뿐 기본 쿼타/정책 검사의 우회가 아니므로, 자격 충족 후에도
    (``oc-omni`` 같은 명시적 무료 레인을 제외하고) 해당 프로필 자체의 provider 측정·
    유효 class·exclude·raw cutoff 를 정상 프로필과 동일하게 검사한다.
    """
    today = today or dt.datetime.now(dt.UTC).date()
    now = now or dt.datetime.now(dt.UTC)
    urgency_hours = urgency_hours if urgency_hours is not None else get_reset_urgency_hours()

    found = _find_profile(profile_name)
    provider_id, group_name = profile_pool(profile_name)
    if found is None:
        return GateResult(
            ok=False,
            profile=profile_name,
            provider_id=provider_id,
            grade=None,
            reason=f"unknown profile: {profile_name}",
            unmeasurable=True,
        )
    grade, profile = found
    by_id = {r.id: r for r in providers}
    result = by_id.get(provider_id)

    if profile.gate == "escalation":
        # 1) escalation 자격: 같은 grade 의 다른 정상 후보가 전부 소진·측정불가일 때만 진행.
        alts = _alt_candidates(providers, grade, profile_name, today, now, urgency_hours)
        if alts:
            return GateResult(
                ok=False,
                profile=profile_name,
                provider_id=provider_id,
                grade=grade,
                reason=(
                    f"{profile_name} 은 escalation 후보 — {profile.gate_reason or ''} "
                    f"(다른 {grade} 후보가 아직 가용하므로 사용 불가)"
                ),
                alternatives=alts,
            )
        # 2) 명시적 무료 레인만 quota provider 측정 없이 즉시 통과. 나머지는 아래 일반
        #    검사(측정불가/exclude/cutoff)를 그대로 통과해야 한다 — escalation 은 게이트를
        #    우회하지 않는, 정상 후보 소진 시에만 열리는 "추가 자격"이다.
        if profile_name in _ESCALATION_SKIPS_QUOTA_CHECK:
            return GateResult(
                ok=True,
                profile=profile_name,
                provider_id=provider_id,
                grade=grade,
                reason=f"{profile_name} escalation 자격 충족 — {profile.gate_reason or ''}",
            )

    if result is None or result.error or result.warning or result.status != "ok":
        alts = _alt_candidates(providers, grade, profile_name, today, now, urgency_hours)
        return GateResult(
            ok=False,
            profile=profile_name,
            provider_id=provider_id,
            grade=grade,
            reason=f"{provider_id} 측정 불가 (provider error/degraded)",
            unmeasurable=True,
            alternatives=alts,
        )

    matches = _matching_buckets(result, group_name)
    if not matches:
        alts = _alt_candidates(providers, grade, profile_name, today, now, urgency_hours)
        return GateResult(
            ok=False,
            profile=profile_name,
            provider_id=provider_id,
            grade=grade,
            reason=f"{provider_id} bucket 측정 불가 (scope 불일치 또는 값 없음)",
            unmeasurable=True,
            alternatives=alts,
        )

    states = _window_states(matches, now)
    constraint = _select_constraint(states)
    used_pct = constraint.used_pct
    fallback_class: PoolClass = (
        result.pool_class if result.pool_class in ("preserve", "spend") else "preserve"
    )
    effective_class = get_policy(provider_id, fallback_class, today=today)[0]
    override = get_active_override(provider_id, today=today)

    if effective_class == "exclude":
        alts = _alt_candidates(providers, grade, profile_name, today, now, urgency_hours)
        reason_parts = [f"정책 제외 ({provider_id}"]
        if override is not None and override.until:
            reason_parts.append(f"until {override.until.isoformat()}")
        if override is not None and override.note:
            reason_parts.append(override.note)
        return GateResult(
            ok=False,
            profile=profile_name,
            provider_id=provider_id,
            grade=grade,
            reason=", ".join(reason_parts) + ")",
            used_pct=used_pct,
            pool_class=effective_class,
            alternatives=alts,
        )

    cutoff = _usage_cutoff(effective_class)
    over = _any_window_over_cutoff(states, cutoff)
    if over is not None:
        alts = _alt_candidates(providers, grade, profile_name, today, now, urgency_hours)
        return GateResult(
            ok=False,
            profile=profile_name,
            provider_id=provider_id,
            grade=grade,
            reason=f"{over.used_pct:g}% 소진 (cutoff {cutoff:g}%, class={effective_class})",
            used_pct=over.used_pct,
            pool_class=effective_class,
            alternatives=alts,
        )

    if profile.gate == "escalation":
        return GateResult(
            ok=True,
            profile=profile_name,
            provider_id=provider_id,
            grade=grade,
            reason=(
                f"{profile_name} escalation 자격 충족 + pool={provider_id} 사용 {used_pct:g}% "
                f"class={effective_class} — {profile.gate_reason or ''}"
            ),
            used_pct=used_pct,
            pool_class=effective_class,
        )

    return GateResult(
        ok=True,
        profile=profile_name,
        provider_id=provider_id,
        grade=grade,
        reason=f"{profile_name} pool={provider_id} 사용 {used_pct:g}% class={effective_class}",
        used_pct=used_pct,
        pool_class=effective_class,
    )


def _format_benchmark_parts(
    *,
    score: float,
    source: str,
    metric: str,
    harness: str | None,
    effort: str | None,
) -> str:
    return (
        f"{score:.1f}({source}; metric={metric}; harness={harness or 'n/a'}; effort={display_effort(effort)})"
    )


def _format_benchmark_score(score: ModelScore) -> str:
    assert score.score is not None
    return _format_benchmark_parts(
        score=score.score,
        source=score.source,
        metric=score.metric,
        harness=score.harness,
        effort=score.effort,
    )


def recommend(
    providers: list[ProviderResult],
    grade: Grade,
    today: dt.date | None = None,
    now: dt.datetime | None = None,
    *,
    urgency_hours: float | None = None,
    bench_scores: list[ModelScore] | None = None,
    explain: bool = False,
    hide_excluded: bool = False,
) -> str:
    today = today or dt.datetime.now(dt.UTC).date()
    now = now or dt.datetime.now(dt.UTC)
    urgency_hours = urgency_hours if urgency_hours is not None else get_reset_urgency_hours()
    by_id = {r.id: r for r in providers}
    included: list[_Candidate] = []
    excluded: list[_Excluded] = []
    policy_excluded: list[_PolicyExcluded] = []
    escalation: list[_EscalationEntry] = []

    for profile in GRADE_TABLE[grade]:
        # Escalation profiles → separate section, not in normal ranked candidates.
        if profile.gate == "escalation":
            escalation.append(_build_escalation_entry(profile, by_id, today, now=now))
            continue

        provider_id, group_name = profile_pool(profile.name)
        provider_label = _provider_label(provider_id, group_name)

        result = by_id.get(provider_id)
        if result is None or result.error or result.warning or result.status != "ok":
            excluded.append(_Excluded(profile, "측정 불가", provider_id=provider_id, kind="unmeasurable"))
            continue

        matches = _matching_buckets(result, group_name)
        if not matches:
            excluded.append(_Excluded(profile, "측정 불가", provider_id=provider_id, kind="unmeasurable"))
            continue

        states = _window_states(matches, now)
        constraint = _select_constraint(states)
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
        over = _any_window_over_cutoff(states, cutoff)
        if over is not None:
            excluded.append(
                _Excluded(
                    profile,
                    f"{over.used_pct:g}% 소진 (reset {_reset_display(over.reset_at)})",
                    provider_id=provider_id,
                    kind="exhausted",
                )
            )
            continue

        used_pct = constraint.used_pct
        remaining_pct = constraint.remaining_pct
        reset_at = constraint.reset_at
        hours = constraint.hours_to_reset
        burn_rate = constraint.burn_rate

        urgent = False
        if effective_class == "spend" and remaining_pct > 0 and hours is not None:
            pace_urgent = _is_pace_urgent(remaining_pct, burn_rate, hours)
            # pace 계산 가능하면 그 값을 쓰고, 소모속도 0/부재/파싱불가면 시간 임계값 폴백.
            urgent = pace_urgent if pace_urgent is not None else hours <= urgency_hours

        boost, _boost_status = get_boost(provider_id, today=today)
        weight, _weight_status = get_capacity_weight(provider_id)
        effective_remaining = weight * remaining_pct
        capacity_term, waste_term, throughput_term, score = _score_components(
            states=states,
            constraint=constraint,
            weight=weight,
            pool_class=effective_class,
            urgency_hours=urgency_hours,
        )

        # ROB-1188 fix 4: 리셋까지 아주 짧게(기본 1h) 남았고 잔여가 유의미한 임계(기본 5%)
        # 이상이면 "확실히 소멸"이 "며칠 단위 의도(boost)"보다 시급하다 — boost 를 역전.
        imminent_exhaustion = (
            effective_class == "spend"
            and hours is not None
            and hours <= get_imminent_reset_hours()
            and remaining_pct >= get_imminent_remaining_pct()
        )

        included.append(
            _Candidate(
                profile=profile,
                provider_label=provider_label,
                provider_id=provider_id,
                windows=states,
                constraint=constraint,
                windows_display=_format_windows_display(states, constraint),
                used_pct=used_pct,
                remaining_pct=remaining_pct,
                pool_class=effective_class,
                reset_at=reset_at,
                hours_to_reset=hours,
                urgent=urgent,
                boost=boost,
                weight=weight,
                effective_remaining=effective_remaining,
                capacity_term=capacity_term,
                waste_term=waste_term,
                throughput_term=throughput_term,
                score=score,
                imminent_exhaustion=imminent_exhaustion,
            )
        )

    # 0) 소멸 임박 역전 → 1) numeric boost(하드 오버라이드) → 2) continuous score(큰 순)
    # → 3) 표 순서(결정성). Binary 🔥 urgency 정렬 키는 연속 점수로 대체(표시는 유지).
    def sort_key(c: _Candidate) -> tuple[int, int, int, float, int]:
        imminent_first = 0 if c.imminent_exhaustion else 1
        boost_present = 0 if c.boost is not None else 1
        boost_value = c.boost if c.boost is not None else 0
        profile_order = next((i for i, p in enumerate(GRADE_TABLE[grade]) if p.name == c.profile.name), 0)
        return (
            imminent_first,
            boost_present,
            boost_value,
            -c.score,
            profile_order,
        )

    included.sort(key=sort_key)

    def _compact_bench(score: float, source: str, harness: str | None, effort: str | None) -> str:
        effort_s = display_effort(effort)
        if harness:
            return f"{score:.1f}({source}/{harness}/{effort_s})"
        return f"{score:.1f}({source}/{effort_s})"

    def benchmark_cell(profile: Profile) -> str:
        # ROB-1191 ④: single effort row only. No best-of multi-effort guess.
        def _not_retired(score: ModelScore) -> bool:
            return score.effort != "ultra"

        def _agent_candidates() -> list[ModelScore]:
            agent_lookup_id = profile.aa_agent_model_id or (
                profile.benchmark_model_id if profile.benchmark_source == "AA-agent" else None
            )
            return [
                score
                for score in bench_scores or []
                if agent_lookup_id is not None
                and score.model_id == agent_lookup_id
                and score.score is not None
                and score.source == "AA-agent"
                and _not_retired(score)
            ]

        def _model_candidates() -> list[ModelScore]:
            model_fallback_id = profile.aa_model_id or (
                profile.benchmark_model_id if profile.benchmark_source == "AA-model" else None
            )
            return [
                score
                for score in bench_scores or []
                if model_fallback_id is not None
                and normalize_aa_model_id(score.model_id) == normalize_aa_model_id(model_fallback_id)
                and score.score is not None
                and score.source == "AA-model"
                and _not_retired(score)
            ]

        def _other_candidates() -> list[ModelScore]:
            return [
                score
                for score in bench_scores or []
                if profile.benchmark_model_id is not None
                and score.model_id == profile.benchmark_model_id
                and score.score is not None
                and score.source not in ("AA-agent", "AA-model")
                and _not_retired(score)
            ]

        target_effort, _provenance = resolve_display_effort(profile)

        def _pick_single(scores: list[ModelScore]) -> str | None:
            if not scores:
                return None
            if target_effort is not None:
                matched = [s for s in scores if (s.effort or "") == target_effort]
                if not matched:
                    return None
                matched.sort(key=lambda s: (s.harness or "", s.metric or ""))
                s = matched[0]
                assert s.score is not None
                return _compact_bench(s.score, s.source, s.harness, s.effort)
            # Effort unconfirmed: only show when a single effort value exists (no best-of).
            efforts = {s.effort for s in scores}
            if len(efforts) != 1:
                return "미지정"
            scores_sorted = sorted(scores, key=lambda s: (s.harness or "", s.metric or "", s.source))
            s = scores_sorted[0]
            assert s.score is not None
            return _compact_bench(s.score, s.source, s.harness, s.effort)

        for pool in (_agent_candidates, _model_candidates, _other_candidates):
            picked = _pick_single(pool())
            if picked is not None:
                return picked

        if profile.benchmark is None or profile.benchmark_source is None or profile.benchmark_metric is None:
            return "미지정" if target_effort is None else ""
        if target_effort is not None and (profile.benchmark_effort or "") not in ("", target_effort):
            return "미지정"
        if target_effort is None and profile.benchmark_effort is None:
            # Static single openrouter-style row with no effort dimension → show unspecified once.
            return _compact_bench(
                profile.benchmark,
                profile.benchmark_source,
                profile.benchmark_harness,
                profile.benchmark_effort,
            )
        if target_effort is None:
            return "미지정"
        return _compact_bench(
            profile.benchmark,
            profile.benchmark_source,
            profile.benchmark_harness,
            profile.benchmark_effort,
        )

    def _fold_policy_excluded(items: list[_PolicyExcluded]) -> list[str]:
        """③ fold policy-excluded profiles by pool into one line per pool."""
        by_pool: dict[str, list[_PolicyExcluded]] = {}
        for item in items:
            by_pool.setdefault(item.provider_id, []).append(item)
        lines_out: list[str] = []
        for provider_id, group in by_pool.items():
            names = "·".join(i.profile.name for i in group)
            sample = group[0]
            until_s = f"until {sample.until.isoformat()}" if sample.until is not None else ""
            note_s = sample.note or ""
            tail_parts = [p for p in (until_s, note_s) if p]
            tail = (": " + " — ".join(tail_parts)) if tail_parts else ""
            lines_out.append(f"✗ {provider_id} 풀 제외 — 이 급에서 {len(group)}개({names}){tail}")
        return lines_out

    def _fold_excluded(items: list[_Excluded]) -> list[str]:
        """③ fold exhausted/unmeasurable by pool; keep kinds separate."""
        # Group key: (kind, provider_id, reason) so different cutoffs don't merge incorrectly.
        groups: dict[tuple[str, str, str], list[_Excluded]] = {}
        for item in items:
            key = (item.kind, item.provider_id or item.profile.name, item.reason)
            groups.setdefault(key, []).append(item)
        lines_out: list[str] = []
        for (kind, provider_id, reason), group in groups.items():
            if len(group) == 1 and not group[0].provider_id:
                lines_out.append(f"✗ {group[0].profile.name:<12} {reason}")
                continue
            names = "·".join(i.profile.name for i in group)
            if kind == "exhausted":
                label = f"{provider_id} 풀 소진"
            elif kind == "unmeasurable":
                label = f"{provider_id} 측정 불가"
            else:
                label = provider_id or group[0].profile.name
            if kind == "unmeasurable":
                lines_out.append(f"✗ {label} — 이 급에서 {len(group)}개({names})")
            else:
                lines_out.append(f"✗ {label} — 이 급에서 {len(group)}개({names}). {reason}")
        return lines_out

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
            benchmark = benchmark_cell(cand.profile)
            bench = f"  벤치 {benchmark}" if benchmark else ""
            usage = cand.windows_display
            if cand.imminent_exhaustion and cand.hours_to_reset is not None:
                lines.append(
                    f"{rank}. 🔥🔥 {cand.profile.name:<10} {cand.provider_label} {usage}  "
                    f"{cand.pool_class:<7}"
                    f"잔여 {cand.remaining_pct:g}% · 리셋 {_format_hours(cand.hours_to_reset)}"
                    f"  소멸 임박 우선 (boost 역전){bench}"
                )
            elif cand.urgent and cand.hours_to_reset is not None:
                lines.append(
                    f"{rank}. 🔥 {cand.profile.name:<10} {cand.provider_label} {usage}  "
                    f"{cand.pool_class:<7}"
                    f"잔여 {cand.remaining_pct:g}% · 리셋 {_format_hours(cand.hours_to_reset)}{bench}"
                )
            else:
                lines.append(
                    f"{rank}. {cand.profile.name:<12} {cand.provider_label} "
                    f"{usage}  {cand.pool_class:<7}{bench}"
                )
            if explain:
                lines.append(
                    f"    score={cand.score:.2f} "
                    f"(capacity={cand.capacity_term:.2f}=w{cand.weight:g}×rem{cand.remaining_pct:g} "
                    f"+ waste×{SCORE_WASTE_WEIGHT:g}={cand.waste_term:.2f} "
                    f"+ thru×{SCORE_THRU_WEIGHT:g}={cand.throughput_term:.2f}; "
                    f"제약={cand.constraint.display_window})"
                )
        if not hide_excluded:
            lines.extend(_fold_policy_excluded(policy_excluded))

    if not hide_excluded:
        lines.extend(_fold_excluded(excluded))

    if escalation:
        lines.append("⚠ 승급 후보 (조건 충족 시에만 · 근거를 이슈에 기록)")
        for entry in escalation:
            lines.append(f"  {entry.profile.name:<12} {entry.provider_label}  pool={entry.provider_id}")
            lines.append(f"    근거: {entry.gate_reason}")
            if entry.status_note:
                lines.append(f"    {entry.status_note}")

    return "\n".join(lines)
