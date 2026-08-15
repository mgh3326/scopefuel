"""Grade-based model recommendation using policy + quota headroom.

The static profile/model/benchmark table is the authoritative source from the
operator relay (OpenRouter rankings 2026-07-31). Profile-to-pool routing matches
``~/bin/herdr-spawn`` QUOTA GUARD, including the three CLIProxy exceptions:

- ``oc-gflash`` routes to ``agy/gemini``
- ``oc-sonnet46`` and ``oc-oss`` route to ``agy/3p``
- all other remaining ``oc-*`` profiles route to ``clinepass``
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

# AA-agent 지수(33~67)의 새 급 경계: S+ / S / A+ / A / B / C.
GRADE_BOUNDARIES: dict[Grade, int] = {"S+": 65, "S": 61, "A+": 55, "A": 48, "B": 40}
# 상위 경계(65·61·55)는 임의값 — 해당 구간 12개가 1점 간격이라 자연 절단점이 없음.
# 하위(48·40)만 최대 간격과 일치.

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
# ROB-1210 r2: progressively brake a candidate as its short-window headroom enters the knee.
BRAKE_KNEE_PCT = 50.0
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
    "kimi": "Kimi",
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
    boundary_text = (
        " · ".join(f"{grade}≥{minimum}" for grade, minimum in GRADE_BOUNDARIES.items()) + " · C<40"
    )
    lines = [f"AA-agent 경계: {boundary_text}", "판별표:"]
    for g in ("S+", "S", "A+", "A", "B", "C"):
        info = GRADE_DISCRIM[g]
        lines.append(f"  {g:<3} {info.task_class}  |  {info.decision_question}")
    lines.append(ESTIMATE_PROVENANCE_LEGEND)
    return "\n".join(lines)


@dataclass(frozen=True)
class Profile:
    name: str
    model: str
    benchmark: float | None
    gate: Gate = "default"
    gate_reason: str | None = None
    launcher_effort: str | None = None
    benchmark_annotation: str | None = None
    # True when the displayed benchmark is derived from AA-model only data;
    # such a row must not fall back to the raw AA-model DB value at runtime.
    model_only: bool = False
    # ROB-1202: one-line justification for an estimated (내삽/외삽) benchmark value.
    estimate_reason: str | None = None
    # Explicitly disclose a conservative grade placement when the displayed estimate
    # falls in a higher numeric range than the operational grade.
    placement_note: str | None = None
    benchmark_source: str | None = None
    benchmark_metric: str | None = None
    benchmark_harness: str | None = None
    benchmark_effort: str | None = None
    benchmark_model_id: str | None = None
    # ROB-1190 ②-3/②-4: source-specific AA lookup keys. AA-agent wins when present;
    # AA-model is queried only when the mapped AA-agent has no score.
    aa_agent_model_id: str | None = None
    aa_model_id: str | None = None


_HARNESS_LABELS = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "grok-build": "Grok Build",
    "kimi-code-cli": "Kimi Code CLI",
    "opencode": "OpenCode",
}

OPUS_MAX_ESCALATION_REASON = "더 깊은 탐색이 필요하거나 xhigh 실패 후 재시도할 때"
CODEX_SOL_XHIGH_ESCALATION_REASON = "쿼타 절약·속도 우선"
UNMEASURED_ANNOTATION = "미측정"
# ROB-1202: "추정" 표시를 근거 계열로 세분화한다.
# 내삽(interpolation) — 상하로 대조 가능한 기준점(실측 곡선 등) 사이에서 추정.
# 외삽(extrapolation) — 단일 기준점에서 투사하거나 대조 기준점이 아예 없는 추정.
ESTIMATED_INTERPOLATED_ANNOTATION = "추정(내삽)"
ESTIMATED_EXTRAPOLATED_ANNOTATION = "추정(외삽)"
# 값이 아예 없는(benchmark=None) 항목이 "추정이며 동시에 미측정"임을 명시하는 결합 표기.
ESTIMATED_EXTRAPOLATED_UNMEASURED_ANNOTATION = "추정(외삽·미측정)"
HAIKU_ESTIMATE_ANNOTATION = ESTIMATED_EXTRAPOLATED_UNMEASURED_ANNOTATION
MODEL_ONLY_ANNOTATION = "모델지수만 있음(에이전트 미측정)"
# ROB-1212: a model-only value is a reference for ordering, never an AA-agent
# grade score.  These annotations make the derived score and the missing
# execution harness visible in the recommendation output.
MODEL_ONLY_INTERPOLATED_ANNOTATION = "추정(내삽·harness-이식)"
MODEL_ONLY_EXTRAPOLATED_ANNOTATION = "추정(외삽·harness-이식)"
ESTIMATE_PROVENANCE_LEGEND = (
    "추정(내삽)=상하 대조 가능한 기준점 사이에서 추정 · 추정(외삽)=단일/무 기준점에서 투사한 추정 "
    "· (·미측정)=AA-agent 실행 실측 없음"
)

SONNET_ESTIMATE_REASON = (
    "Opus 5 동일 effort 실측 곡선 대비 고정 오프셋(-8~-10점) — 상하 effort 모두 대조 가능"
)
KIRO_HAIKU_ESTIMATE_REASON = "Haiku 계열 AA-agent 실측 전무 — 대조 가능한 기준점 없이 단일 추정"
HAIKU_LOW_ESTIMATE_REASON = "Haiku 계열 AA-agent 실측 전무 — 단일 추정, 미측정"
HAIKU_HIGH_ESTIMATE_REASON = "Haiku 계열 AA-agent 실측 전무 — low 추정치에서 상방으로 투사, 미측정"
KIMI_K3_LOW_ESTIMATE_REASON = (
    "AA-model coding_index 72.0(kimi-k3/low, bench.db 실측)을 kimi-k3 default 앵커"
    "(76.2→61.0, AA-agent 실측) / glm-5.2 앵커(68.8→43.0, _MODEL_ONLY_ANCHORS) 사이에서 내삽: "
    "43.0 + (72.0-68.8)/(76.2-68.8)*(61.0-43.0) = 50.8; "
    "harness-이식 아님 — 동일 모델·동일 하네스(kimi-code-cli)의 하위 effort 단계일 뿐"
)
# ROB-1244: 기본 모델 4.5→4.6 전환. 4.6 은 AA-agent 미발표라 전부 추정 —
# 동일 계열·동일 하네스(grok-build) 비율 스케일: 4.5 high 실측 64.0 × (76.8/72.4) = 67.9.
# S+ 범위(≥65)지만 에이전트 미측정이라 한 단계 보수(S). AA-agent 발표 시 bench sync 로 복원.
GROK_HI_ESTIMATE_REASON = (
    "grok-4.6: 4.5 high 실측 64.0 × 동일 하네스 coding 비율(76.8/72.4) = 67.9 — "
    "S+ 범위지만 에이전트 미측정이라 S 보수 배치"
)
GROK_HI_PLACEMENT_NOTE = "보수 배치(S; 비율 스케일 67.9 는 S+ 범위 — AA-agent 발표 시 승급 재검토)"
GROK_MEDIUM_ESTIMATE_REASON = (
    "grok-4.6 high 추정(67.9)에서 4.5 effort 곡선 비율(56/64)로 투사 = 59.4 — 하위 effort 미측정"
)
GROK_LOW_ESTIMATE_REASON = (
    "grok-4.6 high 추정(67.9)에서 4.5 effort 곡선 비율(49/64)로 투사 = 52.0 — "
    "점수상 A 범위지만 미측정(추정 위의 추정)이라 B 보수 배치"
)
GROK_LOW_PLACEMENT_NOTE = "보수 배치(B; 점수상 A 범위지만 추정 위의 추정)"
CROSS_GRADE_MEASURED_REASON = "동급 후보가 미측정 추정일 때의 상위 급 실측 대안"


def _profile_actual_harness(profile: Profile) -> str | None:
    """Return the execution harness known from the profile route."""

    if profile.name.startswith("oc-"):
        return "opencode"
    if profile.name.startswith("kimi-"):
        return "kimi-code-cli"
    if profile.name.startswith("codex-"):
        return "codex"
    if profile.name in {"opus", "sonnet", "fable", "haiku"}:
        return "claude-code"
    if profile.name.startswith("grok"):
        return "grok-build"
    return None


def _harness_label(harness: str) -> str:
    return _HARNESS_LABELS.get(harness.casefold(), harness)


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


def _aa_model_benchmark(score: float, model_id: str, metric: str = "coding_index") -> dict[str, object]:
    return {
        "benchmark_source": "AA-model",
        "benchmark_metric": metric,
        "benchmark_harness": None,
        "benchmark_effort": None,
        "benchmark_model_id": model_id,
    }


# ROB-1212: DB-derived calibration curve for model-only rows.  Each pair is
# (AA-model coding_index, AA-agent agentic score).  The rows below are the
# conservative, single-effort anchor values read from bench.db; Opus has a
# 62–67 AA-agent range across its measured efforts, so the full range is kept
# in the table for auditability.
_MODEL_ONLY_ANCHORS: tuple[tuple[float, float], ...] = (
    (63.0, 38.0),  # claude-sonnet-4.6 / medium / claude-code
    (68.8, 43.0),  # glm-5.2 / default / claude-code
    (72.4, 64.0),  # grok-4.5 / high / grok-build
    (76.2, 61.0),  # kimi-k3 / default / kimi-code-cli
    (76.5, 62.0),  # claude-opus-5 / medium / claude-code
    (76.5, 63.0),  # claude-opus-5 / high / claude-code
    (76.5, 66.0),  # claude-opus-5 / max / claude-code
    (77.0, 67.0),  # claude-opus-5 / xhigh / claude-code
)


def _interpolate_model_only_score(model_score: float) -> float:
    """Map an AA-model value onto the observed AA-agent scale.

    This helper is intentionally only used to materialize static profile
    estimates.  It never participates in runtime ranking and does not turn a
    model score into a measured boundary value.  Outside the anchor range it
    uses the nearest segment; the caller must mark that result as extrapolated
    and apply the additional conservative placement rule.
    """

    anchors = sorted(_MODEL_ONLY_ANCHORS)
    if model_score <= anchors[0][0]:
        lower, upper = anchors[0], anchors[1]
    elif model_score >= anchors[-1][0]:
        lower, upper = anchors[-2], anchors[-1]
    else:
        lower = upper = anchors[0]
        for candidate_lower, candidate_upper in zip(anchors, anchors[1:], strict=False):
            if candidate_lower[0] <= model_score <= candidate_upper[0]:
                lower, upper = candidate_lower, candidate_upper
                if candidate_lower[0] != candidate_upper[0]:
                    break
        if lower[0] == upper[0]:
            # Duplicate model values are only Opus effort rows.  Use the
            # conservative lower agent anchor if a caller lands exactly there.
            same_score = [agent for score, agent in anchors if score == model_score]
            return round(min(same_score), 1)

    fraction = (model_score - lower[0]) / (upper[0] - lower[0])
    return round(lower[1] + fraction * (upper[1] - lower[1]), 1)


# Source values are retained only as calibration inputs.  The Profile score
# fields below contain the derived values, never these AA-model originals.
_QWEN37_DERIVED_SCORE = _interpolate_model_only_score(66.0)
_MINIMAX_M3_DERIVED_SCORE = _interpolate_model_only_score(58.6)


@dataclass(frozen=True)
class RetiredProfile:
    """Metadata for retired profiles: no longer recommended but still available for designated spawn."""

    name: str
    retired_date: str  # ISO date
    reason: str
    provider_id: str
    group_name: str | None = None


RETIRED_PROFILES: dict[str, RetiredProfile] = {
    "oc-oss": RetiredProfile(
        name="oc-oss",
        retired_date="2026-08-06",
        reason=(
            "gpt-oss-120b (model-index 30.4) superseded by oc-sonnet46 (AA-agent 38.0); "
            "multiple C-tier alternatives available"
        ),
        provider_id="agy",
        group_name="3p",
    ),
    "oc-gflash": RetiredProfile(
        name="oc-gflash",
        retired_date="2026-08-06",
        reason=(
            "opencode/CLIProxy 경유 배선이 에이전트 루프를 지속하지 못함 — "
            "같은 모델(gemini-3.6-flash-high)을 동일 브리프·clean worktree로 실측 2회: "
            "oc-gflash는 도구 호출 1건 후 idle 복귀를 반복해 커밋 0·변경 0; "
            "agy 네이티브(agy-flash)는 동일 실행에서 약 4분, +450줄, 수용조건 전항 통과. "
            "모델 품질 문제가 아니라 opencode/CLIProxy 배선 문제이며, "
            "agy-flash(agy 네이티브)로 대체 신설함."
        ),
        provider_id="agy",
        group_name="gemini",
    ),
    "oc-kimi-code": RetiredProfile(
        name="oc-kimi-code",
        retired_date="2026-08-06",
        reason=(
            "kimi 네이티브 프로필(kimi-k3·kimi-k3-low, pool=kimi)과 중복 — "
            "같은 모델을 ClinePass 크레딧으로 소비할 이유가 없어 "
            "고유 모델에 크레딧을 배분하기 위함. "
            "제거 시점 미상, 기록만 소급"
        ),
        provider_id="clinepass",
        group_name=None,
    ),
    "oc-kimi-k3": RetiredProfile(
        name="oc-kimi-k3",
        retired_date="2026-08-06",
        reason=(
            "kimi 네이티브 프로필(kimi-k3·kimi-k3-low, pool=kimi)과 중복 — "
            "같은 모델을 ClinePass 크레딧으로 소비할 이유가 없어 "
            "고유 모델에 크레딧을 배분하기 위함. "
            "제거 시점 미상, 기록만 소급"
        ),
        provider_id="clinepass",
        group_name=None,
    ),
}


# 배치 규칙 (두 갈래) — 근거가 무엇이냐에 따라 점수 처리가 다르다:
# 1. AA-agent 실측: 점수를 그대로 급 점수로 사용. 하네스가 실행 하네스와 달라도
#    표기만 harness-이식 으로 하고 점수·급은 변경하지 않음 (예: oc-sonnet46, oc-glm).
# 2. AA-model 지수만 (에이전트 미측정): _MODEL_ONLY_ANCHORS 로 내삽/외삽 후
#    한 단계 보수 하향으로 배치 (예: oc-qwen37-max, oc-minimax-m3).
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
            "codex-sol",
            "GPT-5.6 Sol (max)",
            67.0,
            **_aa_agent_benchmark(67.0, "gpt-5.6-sol", "max"),
            launcher_effort="max",
            aa_agent_model_id="gpt-5.6-sol",
            aa_model_id="gpt-5-6-sol",
        ),
        Profile(
            "opus",
            "Opus 5 (xhigh)",
            67.0,
            **_aa_agent_benchmark(67.0, "claude-opus-5", "xhigh", harness="claude-code"),
            launcher_effort="xhigh",
            aa_agent_model_id="claude-opus-5",
            aa_model_id="claude-opus-5",
        ),
        Profile(
            "opus",
            "Opus 5 (max)",
            66.0,
            gate="escalation",
            gate_reason=OPUS_MAX_ESCALATION_REASON,
            **_aa_agent_benchmark(66.0, "claude-opus-5", "max", harness="claude-code"),
            launcher_effort="max",
            aa_agent_model_id="claude-opus-5",
            aa_model_id="claude-opus-5",
        ),
        Profile(
            "codex-sol",
            "GPT-5.6 Sol (xhigh)",
            65.0,
            gate="escalation",
            gate_reason=CODEX_SOL_XHIGH_ESCALATION_REASON,
            **_aa_agent_benchmark(65.0, "gpt-5.6-sol", "xhigh"),
            launcher_effort="xhigh",
            aa_agent_model_id="gpt-5.6-sol",
            aa_model_id="gpt-5-6-sol",
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
            "opus",
            "Opus 5 (high)",
            63.0,
            **_aa_agent_benchmark(63.0, "claude-opus-5", "high", harness="claude-code"),
            launcher_effort="high",
            aa_agent_model_id="claude-opus-5",
            aa_model_id="claude-opus-5",
        ),
        Profile(
            "opus",
            "Opus 5 (medium)",
            62.0,
            **_aa_agent_benchmark(62.0, "claude-opus-5", "medium", harness="claude-code"),
            launcher_effort="medium",
            aa_agent_model_id="claude-opus-5",
            aa_model_id="claude-opus-5",
        ),
        Profile(
            "codex-terra-max",
            "Terra (max)",
            62.0,
            **_aa_agent_benchmark(62.0, "gpt-5.6-terra", "max"),
            aa_agent_model_id="gpt-5.6-terra",
            aa_model_id="gpt-5-6-terra",
        ),
        Profile(
            "kimi-k3",
            "Kimi K3",
            61.0,
            **_aa_agent_benchmark(61.0, "kimi-k3", "default", harness="kimi-code-cli"),
            aa_agent_model_id="kimi-k3",
        ),
        Profile(
            "grok-hi",
            "Grok 4.6",
            67.9,
            benchmark_annotation=ESTIMATED_EXTRAPOLATED_ANNOTATION,
            model_only=True,
            estimate_reason=GROK_HI_ESTIMATE_REASON,
            placement_note=GROK_HI_PLACEMENT_NOTE,
            benchmark_effort="high",
            aa_agent_model_id="grok-4.6",
            aa_model_id="grok-4-6",
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
            "codex-terra",
            "Terra (xhigh)",
            57.0,
            **_aa_agent_benchmark(57.0, "gpt-5.6-terra", "xhigh"),
            launcher_effort="xhigh",
            aa_agent_model_id="gpt-5.6-terra",
            aa_model_id="gpt-5-6-terra",
        ),
        Profile(
            "opus",
            "Opus 5 (low)",
            57.0,
            gate="escalation",
            gate_reason="비용효율 — Sonnet high(추정) 우선; 쿼타 여유 시",
            **_aa_agent_benchmark(57.0, "claude-opus-5", "low", harness="claude-code"),
            launcher_effort="low",
            aa_agent_model_id="claude-opus-5",
            aa_model_id="claude-opus-5",
        ),
        Profile(
            "sonnet",
            "Sonnet 5 (high)",
            55.0,
            launcher_effort="high",
            benchmark_effort="high",
            benchmark_annotation=ESTIMATED_INTERPOLATED_ANNOTATION,
            estimate_reason=SONNET_ESTIMATE_REASON,
        ),
        Profile(
            "sonnet",
            "Sonnet 5 (xhigh)",
            58.0,
            gate="escalation",
            gate_reason="쿼타 여유 시",
            launcher_effort="xhigh",
            benchmark_effort="xhigh",
            benchmark_annotation=ESTIMATED_INTERPOLATED_ANNOTATION,
            estimate_reason=SONNET_ESTIMATE_REASON,
        ),
        Profile(
            "grok",
            "Grok 4.6 (medium)",
            59.4,
            launcher_effort="medium",
            benchmark_effort="medium",
            benchmark_annotation=ESTIMATED_EXTRAPOLATED_ANNOTATION,
            estimate_reason=GROK_MEDIUM_ESTIMATE_REASON,
            aa_agent_model_id="grok-4.6",
        ),
        Profile(
            "codex-terra",
            "Terra (high)",
            56.0,
            **_aa_agent_benchmark(56.0, "gpt-5.6-terra", "high"),
            launcher_effort="high",
            aa_agent_model_id="gpt-5.6-terra",
            aa_model_id="gpt-5-6-terra",
        ),
        Profile(
            "codex-luna",
            "Luna (xhigh)",
            55.0,
            **_aa_agent_benchmark(55.0, "gpt-5.6-luna", "xhigh"),
            launcher_effort="xhigh",
            aa_agent_model_id="gpt-5.6-luna",
            aa_model_id="gpt-5-6-luna",
        ),
        # ROB-1251: AA v1.3 실측 57@opencode(high) — 내삽 45.3을 대체.
        # harness-이식: opencode 측정 → agy 네이티브 실행.
        # ROB-1222에서 agy 네이티브가 우리 opencode/CLIProxy 배선보다 강함을 실측 —
        # 이식 방향상 57은 하한에 가까움. 자체 실측 1건(3.7, rounds=0) 부함.
        Profile(
            "agy-flash",
            "Gemini 3.7 Flash",
            57.0,
            **_aa_agent_benchmark(57.0, "gemini-3.7-flash", "high", harness="opencode"),
            aa_agent_model_id="gemini-3.7-flash",
            aa_model_id="gemini-3-7-flash",
        ),
        # ROB-1251: AA v1.3 실측 55@codex — 내삽 44.7을 대체. $0.14/M 최저가 A+.
        # harness-이식: codex 측정 → opencode 실행.
        # DeepSeek V4 Pro는 AA v1.3 실측 31@claude-code — Flash(55)보다 낮고 단가 12배. 배선 후보 아님.
        Profile(
            "oc-dsflash",
            "DeepSeek V4 Flash",
            55.0,
            **_aa_agent_benchmark(55.0, "deepseek-v4-flash", "max", harness="codex"),
            aa_agent_model_id="deepseek-v4-flash",
            aa_model_id="deepseek-v4-flash",
        ),
    ],
    "A": [
        Profile(
            "codex-sol",
            "Sol (low)",
            54.0,
            gate="escalation",
            gate_reason="비용효율 — Luna high(51)은 Sol low(54)와 3점 차이 이내이며 25배 저렴",
            **_aa_agent_benchmark(54.0, "gpt-5.6-sol", "low"),
            launcher_effort="low",
            aa_agent_model_id="gpt-5.6-sol",
            aa_model_id="gpt-5-6-sol",
        ),
        Profile(
            "codex-luna",
            "Luna (high)",
            51.0,
            **_aa_agent_benchmark(51.0, "gpt-5.6-luna", "high"),
            launcher_effort="high",
            aa_agent_model_id="gpt-5.6-luna",
            aa_model_id="gpt-5-6-luna",
        ),
        Profile(
            "codex-terra",
            "Terra (medium)",
            48.0,
            **_aa_agent_benchmark(48.0, "gpt-5.6-terra", "medium"),
            launcher_effort="medium",
            aa_agent_model_id="gpt-5.6-terra",
            aa_model_id="gpt-5-6-terra",
        ),
        Profile(
            "sonnet",
            "Sonnet 5 (medium)",
            52.0,
            launcher_effort="medium",
            benchmark_effort="medium",
            benchmark_annotation=ESTIMATED_INTERPOLATED_ANNOTATION,
            estimate_reason=SONNET_ESTIMATE_REASON,
        ),
        Profile(
            "sonnet",
            "Sonnet 5 (low)",
            48.0,
            launcher_effort="low",
            benchmark_effort="low",
            benchmark_annotation=ESTIMATED_INTERPOLATED_ANNOTATION,
            estimate_reason=SONNET_ESTIMATE_REASON,
        ),
        # ROB-1212 follow-up: kimi-k3 low effort has no AA-agent measurement in
        # bench.db, only an AA-model coding_index=72.0 row.  Derived via the
        # same _MODEL_ONLY_ANCHORS interpolation pattern used by the C-grade
        # model-only rows below, anchored on kimi-k3's own default-effort AA-agent
        # point and glm-5.2 rather than the nearest unrelated anchor (grok-4.5) —
        # this is a same-model/same-harness effort projection, not a harness port,
        # so it uses the plain interpolated annotation instead of the
        # harness-이식 variant.
        Profile(
            "kimi-k3-low",
            "Kimi K3 (low)",
            50.8,
            benchmark_effort="low",
            benchmark_annotation=ESTIMATED_INTERPOLATED_ANNOTATION,
            model_only=True,
            estimate_reason=KIMI_K3_LOW_ESTIMATE_REASON,
            aa_model_id="kimi-k3",
        ),
    ],
    "B": [
        # ROB-1201: measured Luna medium (42) belongs to B (40–47).
        Profile(
            "codex-luna",
            "Luna (medium)",
            42.0,
            **_aa_agent_benchmark(42.0, "gpt-5.6-luna", "medium"),
            launcher_effort="medium",
            aa_agent_model_id="gpt-5.6-luna",
            aa_model_id="gpt-5-6-luna",
        ),
        # ROB-1223: AA-agent 실측 43.0(claude-code/default) — opencode 하네스 이식.
        # oc-sonnet46 과 동일 표기 방식. 실측 기반이므로 보수 하향 없음.
        Profile(
            "oc-glm",
            "GLM-5.2",
            43.0,
            **_aa_agent_benchmark(43.0, "glm-5.2", "default", harness="claude-code"),
            aa_agent_model_id="glm-5.2",
            aa_model_id="glm-5-2",
        ),
        Profile(
            "kiro-haiku",
            "Haiku 4.5",
            35.0,
            benchmark_annotation=ESTIMATED_EXTRAPOLATED_ANNOTATION,
            estimate_reason=KIRO_HAIKU_ESTIMATE_REASON,
        ),
        # ROB-1202: estimated + unmeasured Grok low (no lower-effort anchor to compare against).
        Profile(
            "grok",
            "Grok 4.6 (low)",
            52.0,
            launcher_effort="low",
            benchmark_effort="low",
            benchmark_annotation=ESTIMATED_EXTRAPOLATED_ANNOTATION,
            estimate_reason=GROK_LOW_ESTIMATE_REASON,
            placement_note=GROK_LOW_PLACEMENT_NOTE,
            aa_agent_model_id="grok-4.6",
        ),
        # ROB-1202: extrapolated/unmeasured Haiku high estimate — not a Haiku medium placement.
        Profile(
            "haiku",
            "Claude Haiku 4.5",
            44.0,
            launcher_effort="high",
            benchmark_effort="high",
            benchmark_annotation=HAIKU_ESTIMATE_ANNOTATION,
            estimate_reason=HAIKU_HIGH_ESTIMATE_REASON,
        ),
    ],
    "C": [
        Profile(
            "codex-luna",
            "Luna (low)",
            None,
            launcher_effort="low",
            benchmark_effort="low",
            benchmark_annotation=UNMEASURED_ANNOTATION,
            aa_agent_model_id="gpt-5.6-luna",
        ),
        Profile("kiro-cheap", "Qwen3 Coder", None, aa_model_id="qwen3-coder-next"),
        Profile(
            "oc-sonnet46",
            "Sonnet 4.6",
            38.0,
            **_aa_agent_benchmark(38.0, "claude-sonnet-4.6", "medium", harness="claude-code"),
            aa_agent_model_id="claude-sonnet-4.6",
            aa_model_id="claude-sonnet-4-6",
        ),
        # ROB-1201: estimated Haiku low is a C-tier exception, not a B candidate.
        Profile(
            "haiku",
            "Claude Haiku 4.5",
            35.0,
            launcher_effort="low",
            benchmark_effort="low",
            benchmark_annotation=HAIKU_ESTIMATE_ANNOTATION,
            estimate_reason=HAIKU_LOW_ESTIMATE_REASON,
        ),
        Profile(
            "oc-qwen37-max",
            "qwen3.7-max",
            _QWEN37_DERIVED_SCORE,
            benchmark_annotation=MODEL_ONLY_INTERPOLATED_ANNOTATION,
            model_only=True,
            estimate_reason=(
                "AA-model coding_index 66.0 — 63.0→38.0 / 68.8→43.0 내삽 = 40.6; "
                "harness 미측정이라 harness-이식, 한 단계 보수 배치(C)"
            ),
            placement_note="보수 배치(C; 내삽 결과 B 범위에서 한 단계 하향)",
            aa_model_id="qwen3-7-max",
        ),
        Profile(
            "oc-minimax-m3",
            "minimax-m3",
            _MINIMAX_M3_DERIVED_SCORE,
            benchmark_annotation=MODEL_ONLY_EXTRAPOLATED_ANNOTATION,
            model_only=True,
            estimate_reason=(
                "AA-model coding_index 58.6 — 최저 앵커 63.0→38.0 밖, "
                "63.0→38.0 / 68.8→43.0 기울기로 외삽 = 34.2; "
                "harness 미측정, 추가 보수 배치(C)"
            ),
            placement_note="보수 배치(C; 앵커 밖 외삽)",
            aa_model_id="minimax-m3",
        ),
        Profile(
            "oc-omni",
            "OmniRoute free",
            None,
            gate="escalation",
            gate_reason=OC_OMNI_ESCALATION_REASON,
        ),
    ],
}


_GRADE_ORDER: tuple[Grade, ...] = ("S+", "S", "A+", "A", "B", "C")
_GRADE_BOUNDARY_EXEMPT_ANNOTATIONS = frozenset(
    {
        UNMEASURED_ANNOTATION,
        ESTIMATED_INTERPOLATED_ANNOTATION,
        ESTIMATED_EXTRAPOLATED_ANNOTATION,
        MODEL_ONLY_INTERPOLATED_ANNOTATION,
        MODEL_ONLY_EXTRAPOLATED_ANNOTATION,
        HAIKU_ESTIMATE_ANNOTATION,
        MODEL_ONLY_ANNOTATION,
    }
)


def _grade_range(grade: Grade) -> tuple[float | None, float | None]:
    """Return the inclusive lower/exclusive upper score range for a grade."""
    if grade == "C":
        return None, float(GRADE_BOUNDARIES["B"])
    lower = float(GRADE_BOUNDARIES[grade])
    index = _GRADE_ORDER.index(grade)
    upper = None if index == 0 else float(GRADE_BOUNDARIES[_GRADE_ORDER[index - 1]])
    return lower, upper


def _boundary_checked(profile: Profile) -> bool:
    """Only measured AA-agent scores are comparable to the operational grade cuts.

    Estimated, unmeasured, and model-only rows are explicit exceptions. OpenRouter
    and AA-model values are reference scores, not the AA-agent execution scores
    used for these cuts.
    """
    return (
        profile.benchmark is not None
        and profile.benchmark_source == "AA-agent"
        and not profile.model_only
        and profile.benchmark_annotation not in _GRADE_BOUNDARY_EXEMPT_ANNOTATIONS
    )


def validate_grade_table(table: dict[Grade, list[Profile]] | None = None) -> None:
    """Fail loudly when a measured profile is placed outside its grade range."""
    table = GRADE_TABLE if table is None else table
    violations: list[str] = []
    for grade, profiles in table.items():
        lower, upper = _grade_range(grade)
        for profile in profiles:
            if (
                profile.benchmark is not None
                and profile.benchmark_source == "AA-model"
                and not profile.model_only
                and profile.benchmark_annotation not in _GRADE_BOUNDARY_EXEMPT_ANNOTATIONS
            ):
                violations.append(
                    f"{profile.name} has raw AA-model {profile.benchmark:g} as a grade score; "
                    "derive it or mark it model-only"
                )
                continue
            if not _boundary_checked(profile):
                continue
            assert profile.benchmark is not None
            in_range = (lower is None or profile.benchmark >= lower) and (
                upper is None or profile.benchmark < upper
            )
            if not in_range:
                violations.append(
                    f"{profile.name} --effort {profile.launcher_effort or '-'} "
                    f"score={profile.benchmark:g} in {grade} "
                    f"(expected {lower if lower is not None else '-inf'}"
                    f"–{upper if upper is not None else 'inf'})"
                )
    if violations:
        raise ValueError("measured grade boundary violation: " + "; ".join(violations))


validate_grade_table()


# wrk의 기존 codex-max 표기는 계속 gate에서 받되, 급표의 정본 표기는 canonical codex-sol로 둔다.
PROFILE_ALIASES: dict[str, str] = {"codex-max": "codex-sol"}


def _profile_label(profile: Profile) -> str:
    if profile.launcher_effort:
        return f"{profile.name} --effort {profile.launcher_effort}"
    return profile.name


def _benchmark_annotation(profile: Profile) -> str | None:
    if profile.model_only:
        annotation = profile.benchmark_annotation or MODEL_ONLY_ANNOTATION
        if MODEL_ONLY_ANNOTATION in annotation:
            return f"{annotation} · {profile.model}"
        return f"{annotation} · {profile.model} · {MODEL_ONLY_ANNOTATION}"
    if profile.benchmark_annotation == MODEL_ONLY_ANNOTATION:
        return f"{profile.model} · {profile.benchmark_annotation}"
    return profile.benchmark_annotation


def profile_pool(profile: str) -> tuple[str, str | None]:
    """Return (provider_id, group_name_if_group_scope)."""
    if profile in ("opus", "sonnet", "fable", "haiku"):
        return "claude", None
    if profile.startswith("codex") or profile == "claudex":
        return "codex", None
    if profile in ("agy", "agy-flash", "agy-flash-med", "agy-pro"):
        return "agy", "gemini"
    if profile in ("agy-sonnet", "agy-opus", "agy-oss"):
        return "agy", "3p"
    if profile.startswith("kiro"):
        return "kiro", None
    if profile.startswith("kimi-"):
        return "kimi", None
    if profile == "oc-gflash":
        return "agy", "gemini"
    if profile in ("oc-sonnet46", "oc-oss"):
        return "agy", "3p"
    if profile == "oc-omni":
        return "omniroute", None
    if profile.startswith("oc-"):
        return "clinepass", None
    if profile in ("grok", "grok-hi", "grok-med", "grok45", "grok45-med", "grok46", "grok46-med"):
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
    budget: _WindowState
    throughput_window: _WindowState
    brake: float
    brake_window: _WindowState | None
    windows_display: str
    used_pct: float  # budget window
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


def _budget_sort_key(state: _WindowState) -> tuple[int, float, str]:
    """Larger key = longer budget window; an unknown duration is longest."""
    seconds = _window_seconds(state.window)
    if seconds is None:
        # Provider labels such as ``week``/``?`` do not expose seconds.  They
        # are budget windows by policy; the label is only a deterministic tie-break.
        return (1, 0.0, state.window)
    return (0, seconds, state.window)


def _select_budget(states: list[_WindowState]) -> _WindowState:
    """Select the longest-duration window used for budget and expiry scoring."""
    return max(states, key=_budget_sort_key)


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
    budget: _WindowState | None = None,
) -> tuple[float, float, float, float]:
    """Return (capacity, waste, throughput, total) continuous score terms.

    (a) capacity — budget-window remaining × capacity_weight
    (b) waste — spend-only budget-window remainder that current pace will not consume before reset
        (pace unknown + reset within urgency_hours → entire remainder counted as at-risk)
    (c) throughput — remaining on the tightest short window (draw-rate headroom)
    """
    if budget is None:
        budget = _select_budget(states)

    capacity = weight * budget.remaining_pct

    waste = 0.0
    if pool_class == "spend":
        br = budget.burn_rate
        hours = budget.hours_to_reset
        if br is not None and br > 0 and hours is not None:
            waste = max(0.0, budget.remaining_pct - br * hours)
        elif hours is not None and br == 0.0:
            # Measured zero burn with remaining → entire remainder at risk of expiry.
            waste = budget.remaining_pct
        elif hours is not None and br is None and hours <= urgency_hours:
            # Pace unmeasurable: conservative fallback matches 🔥 threshold.
            waste = budget.remaining_pct

    throughput_window = _select_throughput_window(states, constraint)
    throughput = throughput_window.remaining_pct

    base_score = capacity + SCORE_WASTE_WEIGHT * waste + SCORE_THRU_WEIGHT * throughput
    brake, _brake_window = _brake_factor(states)
    total = base_score * brake
    return capacity, waste, throughput, total


def _select_shortest_window(states: list[_WindowState]) -> _WindowState | None:
    """Return the tightest window from the same short-window set as throughput."""
    short = [
        s for s in states if (secs := _window_seconds(s.window)) is not None and secs <= SHORT_WINDOW_MAX_S
    ]
    # Existing throughput semantics: lower remaining means less headroom.
    return min(short, key=lambda s: s.remaining_pct) if short else None


def _select_throughput_window(states: list[_WindowState], fallback: _WindowState) -> _WindowState:
    """Return the existing shortest-window throughput source, with its old fallback."""
    return _select_shortest_window(states) or fallback


def _brake_factor(states: list[_WindowState]) -> tuple[float, _WindowState | None]:
    """Return (short-window brake, source window); no short window means no brake."""
    short = _select_shortest_window(states)
    if short is None:
        return 1.0, None
    return min(1.0, short.remaining_pct / BRAKE_KNEE_PCT), short


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


def _profile_has_benchmark_score(profile: Profile, bench_scores: list[ModelScore] | None) -> bool:
    """Return whether the profile has a static or uniquely displayable score."""

    if profile.benchmark is not None:
        return True
    if not bench_scores:
        return False

    def not_retired(score: ModelScore) -> bool:
        return score.score is not None and score.effort != "ultra"

    agent_lookup_id = profile.aa_agent_model_id or (
        profile.benchmark_model_id if profile.benchmark_source == "AA-agent" else None
    )
    agent_scores = [
        score
        for score in bench_scores
        if agent_lookup_id is not None
        and score.model_id == agent_lookup_id
        and score.source == "AA-agent"
        and not_retired(score)
    ]

    model_fallback_id = profile.aa_model_id or (
        profile.benchmark_model_id if profile.benchmark_source == "AA-model" else None
    )
    codex_profile = profile_pool(profile.name)[0] == "codex"
    registered_model_metric = profile.benchmark_metric if profile.benchmark_source == "AA-model" else None
    model_scores = [
        score
        for score in bench_scores
        if model_fallback_id is not None
        and normalize_aa_model_id(score.model_id) == normalize_aa_model_id(model_fallback_id)
        and score.source == "AA-model"
        and (registered_model_metric is None or score.metric == registered_model_metric)
        and not (codex_profile and score.effort is None)
        and not_retired(score)
    ]
    other_scores = [
        score
        for score in bench_scores
        if profile.benchmark_model_id is not None
        and score.model_id == profile.benchmark_model_id
        and score.source not in ("AA-agent", "AA-model")
        and not_retired(score)
    ]

    target_effort, _ = resolve_display_effort(profile)
    for scores in (agent_scores, model_scores, other_scores):
        if not scores:
            continue
        if target_effort is not None:
            return any(score.effort == target_effort for score in scores)
        return len({score.effort for score in scores}) == 1
    return False


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
    reason_override: str | None = None,
) -> _EscalationEntry:
    provider_id, group_name = profile_pool(profile.name)
    provider_label = _provider_label(provider_id, group_name)
    gate_reason = reason_override if reason_override is not None else (profile.gate_reason or "")
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


def _cross_grade_measured_alternatives(grade: Grade) -> list[tuple[Profile, str]]:
    """Find measured upper-grade options for an unmeasured estimate in this grade."""

    grade_index = _GRADE_ORDER.index(grade)
    if grade_index == 0:
        return []
    existing_escalation_pools = {
        profile_pool(profile.name)[0] for profile in GRADE_TABLE[grade] if profile.gate == "escalation"
    }
    if not existing_escalation_pools:
        return []

    estimated_by_provider: dict[str, Profile] = {}
    for profile in GRADE_TABLE[grade]:
        if (
            profile.gate == "default"
            and profile.benchmark_source is None
            and profile.benchmark_annotation is not None
            and profile.estimate_reason is not None
        ):
            provider_id, _ = profile_pool(profile.name)
            estimated_by_provider.setdefault(provider_id, profile)

    alternatives: list[tuple[Profile, str]] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    # ROB-1218: adjacent grade only. Scanning every upper grade degenerated —
    # C-grade work (haiku low, est. 35) listed opus xhigh (S+, measured 67) and
    # kimi-k3 (S, 61) as "alternatives", a 26-32 point jump that no C task wants.
    # The rule exists for the near-miss case it was built for (A+ grok medium,
    # estimated -> S grok-hi, measured 64), which is one grade up by construction.
    for upper_grade in _GRADE_ORDER[grade_index - 1 : grade_index]:
        for profile in GRADE_TABLE[upper_grade]:
            provider_id, _ = profile_pool(profile.name)
            source_profile = estimated_by_provider.get(provider_id)
            if (
                source_profile is None
                or provider_id in existing_escalation_pools
                or profile.benchmark is None
                or profile.benchmark_source != "AA-agent"
                or profile.benchmark_annotation is not None
            ):
                continue
            key = (profile.name, profile.launcher_effort, profile.benchmark_model_id)
            if key in seen:
                continue
            seen.add(key)
            reason = (
                f"{CROSS_GRADE_MEASURED_REASON} — {grade}의 {_profile_label(source_profile)}은 "
                f"{source_profile.benchmark_annotation}, {upper_grade}의 {_profile_label(profile)}은 "
                f"AA-agent 실측 {profile.benchmark:.1f}"
            )
            alternatives.append((profile, reason))
    return alternatives


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
    profile_name = PROFILE_ALIASES.get(profile_name, profile_name)
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
        # D3: Profile not in GRADE_TABLE — check quota cutoff only (no escalation logic).
        # If provider_id is unknown too, return unmeasurable.
        if not provider_id:
            return GateResult(
                ok=False,
                profile=profile_name,
                provider_id=provider_id,
                grade=None,
                reason=f"unknown profile: {profile_name}",
                unmeasurable=True,
            )
        # Profile known to profile_pool but not in GRADE_TABLE: check quota only.
        by_id = {r.id: r for r in providers}
        result = by_id.get(provider_id)
        if result is None or result.error or result.warning or result.status != "ok":
            return GateResult(
                ok=False,
                profile=profile_name,
                provider_id=provider_id,
                grade=None,
                reason=f"{provider_id} 측정 불가 (provider error/degraded)",
                unmeasurable=True,
            )
        matches = _matching_buckets(result, group_name)
        if not matches:
            return GateResult(
                ok=False,
                profile=profile_name,
                provider_id=provider_id,
                grade=None,
                reason=f"{provider_id} bucket 측정 불가 (scope 불일치 또는 값 없음)",
                unmeasurable=True,
            )
        states = _window_states(matches, now)
        constraint = _select_constraint(states)
        used_pct = constraint.used_pct
        fallback_class: PoolClass = (
            result.pool_class if result.pool_class in ("preserve", "spend") else "preserve"
        )
        effective_class = get_policy(provider_id, fallback_class, today=today)[0]
        cutoff = _usage_cutoff(effective_class)
        over = _any_window_over_cutoff(states, cutoff)
        if over is not None:
            return GateResult(
                ok=False,
                profile=profile_name,
                provider_id=provider_id,
                grade=None,
                reason=f"{over.used_pct:g}% 소진 (cutoff {cutoff:g}%, class={effective_class})",
                used_pct=over.used_pct,
                pool_class=effective_class,
            )
        return GateResult(
            ok=True,
            profile=profile_name,
            provider_id=provider_id,
            grade=None,
            reason=f"{profile_name} pool={provider_id} 사용 {used_pct:g}% class={effective_class}",
            used_pct=used_pct,
            pool_class=effective_class,
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


def _format_benchmark_measurements(
    *, time_per_task_min: float | None, cost_per_task_usd: float | None
) -> str:
    parts: list[str] = []
    if time_per_task_min is not None:
        parts.append(f"{time_per_task_min:.1f}분")
    if cost_per_task_usd is not None:
        parts.append(f"${cost_per_task_usd:g}/작업")
    return " · ".join(parts)


def _format_benchmark_score(score: ModelScore) -> str:
    assert score.score is not None
    rendered = _format_benchmark_parts(
        score=score.score,
        source=score.source,
        metric=score.metric,
        harness=score.harness,
        effort=score.effort,
    )
    measurements = _format_benchmark_measurements(
        time_per_task_min=score.time_per_task_min,
        cost_per_task_usd=score.cost_per_task_usd,
    )
    return f"{rendered} · {measurements}" if measurements else rendered


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
        budget = _select_budget(states)
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

        used_pct = budget.used_pct
        remaining_pct = budget.remaining_pct
        reset_at = budget.reset_at
        hours = budget.hours_to_reset
        burn_rate = budget.burn_rate

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
            budget=budget,
        )
        throughput_window = _select_throughput_window(states, constraint)
        brake, brake_window = _brake_factor(states)

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
                budget=budget,
                throughput_window=throughput_window,
                brake=brake,
                brake_window=brake_window,
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

    for profile, reason in _cross_grade_measured_alternatives(grade):
        escalation.append(
            _build_escalation_entry(
                profile,
                by_id,
                today,
                now=now,
                reason_override=reason,
            )
        )

    # 0) 소멸 임박 역전 → 1) numeric boost(하드 오버라이드) → 2) continuous score(큰 순)
    # → 3) 표 순서(결정성). Binary 🔥 urgency 정렬 키는 연속 점수로 대체(표시는 유지).
    # Benchmark가 없는 항목은 quota boost/urgency와 무관하게 급 내 마지막으로 보낸다.
    def sort_key(c: _Candidate) -> tuple[int, int, int, int, float, int]:
        benchmark_missing = 0 if _profile_has_benchmark_score(c.profile, bench_scores) else 1
        imminent_first = 0 if c.imminent_exhaustion else 1
        boost_present = 0 if c.boost is not None else 1
        boost_value = c.boost if c.boost is not None else 0
        profile_order = next((i for i, p in enumerate(GRADE_TABLE[grade]) if p.name == c.profile.name), 0)
        return (
            benchmark_missing,
            imminent_first,
            boost_present,
            boost_value,
            -c.score,
            profile_order,
        )

    included.sort(key=sort_key)

    def _compact_bench(
        profile: Profile,
        score: float,
        source: str,
        harness: str | None,
        effort: str | None,
        time_per_task_min: float | None = None,
        cost_per_task_usd: float | None = None,
        *,
        suppress_estimate: bool = False,
    ) -> str:
        # Kimi's approved AA-agent row is the CLI's model-default effort.  The
        # operator-facing cell names the measured harness, not a tunable effort.
        effort_s = None if effort == "default" else display_effort(effort)
        if harness:
            effort_part = f"/{effort_s}" if effort_s else ""
            rendered = f"{score:.1f}({source}/{harness}{effort_part})"
        else:
            rendered = f"{score:.1f}({source}/{effort_s})"

        # ROB-1202 rework r1: a live AA-agent execution measurement contradicts a static
        # "estimated/unmeasured" annotation — once a real measurement exists, suppress it
        # (the cross-harness transfer note below is unrelated and still applies).
        annotation = None if suppress_estimate else _benchmark_annotation(profile)
        if annotation:
            rendered += f" · {annotation}"
        if profile.placement_note and not suppress_estimate:
            rendered += f" · {profile.placement_note}"

        measurements = _format_benchmark_measurements(
            time_per_task_min=time_per_task_min,
            cost_per_task_usd=cost_per_task_usd,
        )
        if measurements:
            rendered += f" · {measurements}"

        actual_harness = _profile_actual_harness(profile)
        if (
            source == "AA-agent"
            and actual_harness is not None
            and harness is not None
            and harness.casefold() != actual_harness.casefold()
        ):
            rendered += (
                " ⚠️ harness-이식 추정"
                f" · 💡 {profile.model} 는 {_harness_label(harness)} 에서 측정됨"
                " — 네이티브 하네스 검토 권장"
            )
        return rendered

    def benchmark_cell(profile: Profile) -> tuple[str, bool]:
        """Return (rendered cell, live_measured) — live_measured means a real AA-agent
        execution measurement (not the static profile placeholder) was selected, so any
        static estimate annotation/reason must not be shown alongside it."""

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
            if profile.model_only or profile.aa_agent_model_id is None:
                # The raw AA-model row is a calibration input only.  A
                # materialized derived score is displayed from Profile below;
                # profiles without an agent mapping must not silently turn an
                # AA-model fallback into a grade score when bench.db exists.
                return []
            model_fallback_id = profile.aa_model_id or (
                profile.benchmark_model_id if profile.benchmark_source == "AA-model" else None
            )
            codex_profile = profile_pool(profile.name)[0] == "codex"
            registered_model_metric = (
                profile.benchmark_metric if profile.benchmark_source == "AA-model" else None
            )
            return [
                score
                for score in bench_scores or []
                if model_fallback_id is not None
                and normalize_aa_model_id(score.model_id) == normalize_aa_model_id(model_fallback_id)
                and score.score is not None
                and score.source == "AA-model"
                and (registered_model_metric is None or score.metric == registered_model_metric)
                and not (codex_profile and score.effort is None)
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

        def _pick_single(scores: list[ModelScore], *, suppress_estimate: bool) -> str | None:
            if not scores:
                return None
            if target_effort is not None:
                matched = [s for s in scores if (s.effort or "") == target_effort]
                if not matched:
                    return None
                matched.sort(key=lambda s: (s.harness or "", s.metric or ""))
                s = matched[0]
                assert s.score is not None
                return _compact_bench(
                    profile,
                    s.score,
                    s.source,
                    s.harness,
                    s.effort,
                    s.time_per_task_min,
                    s.cost_per_task_usd,
                    suppress_estimate=suppress_estimate,
                )
            # Effort unconfirmed: only show when a single effort value exists (no best-of).
            efforts = {s.effort for s in scores}
            if len(efforts) != 1:
                return "미지정"
            scores_sorted = sorted(scores, key=lambda s: (s.harness or "", s.metric or "", s.source))
            s = scores_sorted[0]
            assert s.score is not None
            return _compact_bench(
                profile,
                s.score,
                s.source,
                s.harness,
                s.effort,
                s.time_per_task_min,
                s.cost_per_task_usd,
                suppress_estimate=suppress_estimate,
            )

        for pool, is_agent_pool in (
            (_agent_candidates, True),
            (_model_candidates, False),
            (_other_candidates, False),
        ):
            picked = _pick_single(pool(), suppress_estimate=is_agent_pool)
            if picked is not None:
                return picked, is_agent_pool and picked != "미지정"

        if profile.benchmark is None and profile.benchmark_annotation:
            return _benchmark_annotation(profile) or "미지정", False

        if (
            profile.benchmark is not None
            and profile.benchmark_source is None
            and profile.benchmark_annotation
        ):
            annotation = _benchmark_annotation(profile) or profile.benchmark_annotation
            rendered = f"{profile.benchmark:.1f}({annotation})"
            if profile.placement_note:
                rendered += f" · {profile.placement_note}"
            return rendered, False

        if profile.benchmark is None or profile.benchmark_source is None or profile.benchmark_metric is None:
            return ("미지정" if target_effort is None else ""), False
        if target_effort is not None and (profile.benchmark_effort or "") not in ("", target_effort):
            return "미지정", False
        if target_effort is None and profile.benchmark_effort is None:
            # Static single openrouter-style row with no effort dimension → show unspecified once.
            return (
                _compact_bench(
                    profile,
                    profile.benchmark,
                    profile.benchmark_source,
                    profile.benchmark_harness,
                    profile.benchmark_effort,
                ),
                False,
            )
        if target_effort is None:
            return "미지정", False
        return (
            _compact_bench(
                profile,
                profile.benchmark,
                profile.benchmark_source,
                profile.benchmark_harness,
                profile.benchmark_effort,
            ),
            False,
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

    def _suppress_policy_excluded(items: list[_PolicyExcluded]) -> bool:
        """ROB-1219: a deliberately excluded pool is noise, not information.

        `class = exclude` is an operator decision that the pool is out of rotation
        for a stated period (kiro: subscription ended 2026-08-01, re-evaluate after
        the 2026-09-01 free-credit reset). Repeating a "✗ pool excluded" line in
        every grade tells the reader nothing they did not already decide. The
        record lives on in `policy list` (class, until, note) and in GRADE_TABLE,
        so restoring the pool is `scopefuel policy clear <pool>` — no code change.

        Suppression is display-only and never applies when the grade has no usable
        candidate: the emergency-candidate block below still surfaces the pool.
        """
        return bool(items)

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
            benchmark, live_measured = benchmark_cell(cand.profile)
            bench = f"  벤치 {benchmark}" if benchmark else ""
            profile_label = _profile_label(cand.profile)
            usage = cand.windows_display
            if cand.imminent_exhaustion and cand.hours_to_reset is not None:
                lines.append(
                    f"{rank}. 🔥🔥 {profile_label:<24} {cand.provider_label} {usage}  "
                    f"{cand.pool_class:<7}"
                    f"잔여 {cand.remaining_pct:g}% · 리셋 {_format_hours(cand.hours_to_reset)}"
                    f"  소멸 임박 우선 (boost 역전){bench}"
                )
            elif cand.urgent and cand.hours_to_reset is not None:
                lines.append(
                    f"{rank}. 🔥 {profile_label:<24} {cand.provider_label} {usage}  "
                    f"{cand.pool_class:<7}"
                    f"잔여 {cand.remaining_pct:g}% · 리셋 {_format_hours(cand.hours_to_reset)}{bench}"
                )
            else:
                lines.append(
                    f"{rank}. {profile_label:<24} {cand.provider_label} {usage}  {cand.pool_class:<7}{bench}"
                )
            if explain:
                brake_explain = ""
                if cand.brake < 1.0 and cand.brake_window is not None:
                    brake_explain = (
                        f" × brake={cand.brake:.2f} "
                        f"({cand.brake_window.display_window} 잔여 {cand.brake_window.remaining_pct:g}% "
                        f"< knee {BRAKE_KNEE_PCT:g}%)"
                    )
                lines.append(
                    f"    score={cand.score:.2f} "
                    f"(capacity={cand.capacity_term:.2f}=w{cand.weight:g}×rem{cand.remaining_pct:g} "
                    f"(budget={cand.budget.display_window}) "
                    f"+ waste×{SCORE_WASTE_WEIGHT:g}={cand.waste_term:.2f} "
                    f"(budget={cand.budget.display_window}) "
                    f"+ thru×{SCORE_THRU_WEIGHT:g}={cand.throughput_term:.2f} "
                    f"(short={cand.throughput_window.display_window}){brake_explain}; "
                    f"제약={cand.constraint.display_window})"
                )
                if cand.profile.estimate_reason and not live_measured:
                    lines.append(f"    추정근거: {cand.profile.estimate_reason}")
        if not hide_excluded and not _suppress_policy_excluded(policy_excluded):
            lines.extend(_fold_policy_excluded(policy_excluded))

    if not hide_excluded:
        lines.extend(_fold_excluded(excluded))

    if escalation:
        lines.append("⚠ 승급 후보 (조건 충족 시에만 · 근거를 이슈에 기록)")
        for entry in escalation:
            lines.append(
                f"  {_profile_label(entry.profile):<24} {entry.provider_label}  pool={entry.provider_id}"
            )
            lines.append(f"    근거: {entry.gate_reason}")
            if entry.status_note:
                lines.append(f"    {entry.status_note}")
            if explain and entry.profile.estimate_reason:
                lines.append(f"    추정근거: {entry.profile.estimate_reason}")

    return "\n".join(lines)
