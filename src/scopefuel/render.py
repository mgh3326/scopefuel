"""출력 렌더러 — 표(사람) / 한 줄(statusline·pane) / JSON(에이전트 계약)."""

from __future__ import annotations

import datetime as dt

from .cache import format_age
from .model import WARN_PCT, Bucket, ProviderResult, _is_valid_used_pct, iso_to_local, overall_mark

MARK_TEXT = {"ok": "ok", "warn": "WARN", "crit": "CRIT", "degraded": "DEGRADED"}
MARK_COLOR = {"ok": "\033[32m", "warn": "\033[33m", "crit": "\033[31m", "degraded": "\033[33m"}
RESET = "\033[0m"


def _mark(mark: str, color: bool) -> str:
    text = MARK_TEXT[mark]
    return f"{MARK_COLOR[mark]}{text}{RESET}" if color else text


def _waste_mark(color: bool) -> str:
    text = "WASTE"
    return f"\033[35m{text}{RESET}" if color else text


def _pct(value: float | None) -> str:
    """사용률 표시. '사용' 접두사로 잔량(남은 %)과 혼동되지 않게 한다."""
    if value is None or not _is_valid_used_pct(value):
        return "?"
    return f"사용 {value:g}%"


def _display_id(result: ProviderResult) -> str:
    if result.id == "clinepass" and result.source:
        return f"{result.id} [{result.source}]"
    return result.id


def _pace(bucket: Bucket | None, *, now: dt.datetime | None = None) -> str:
    if bucket is None:
        return ""
    value = bucket.pace_at(now).ratio
    return "" if value is None else f" {value:.1f}x"


def _table_pace(bucket: Bucket, *, now: dt.datetime | None = None) -> str:
    pace = bucket.pace_at(now)
    if pace.ratio is None:
        return ""
    rate = (
        f"  완소진 {pace.full_use_rate:.1f}{pace.full_use_rate_unit}"
        if pace.full_use_rate is not None
        else ""
    )
    return f"  pace {pace.ratio:.2f}x{rate}"


def _bucket_for(
    result: ProviderResult, *, scope_name: str | None = None, horizon: str | None = None
) -> Bucket | None:
    matches = [
        b
        for b in result.buckets
        if _is_valid_used_pct(b.used_pct)
        and (scope_name is None or b.scope.label == scope_name)
        and (horizon is None or b.horizon == horizon)
    ]
    return max(matches, key=lambda b: b.used_pct or 0.0, default=None)


def _basis_text(result: ProviderResult, verdict) -> str:
    if result.warning:
        return result.warning
    if verdict.basis == "account":
        basis = f"지금(5h급) {_pct(verdict.now_pct)} · 이번주 {_pct(verdict.week_pct)}"
        if verdict.month_pct is not None:
            basis += f" · 이번달 {_pct(verdict.month_pct)}"
        return basis
    if verdict.basis == "group":
        per = " / ".join(f"{g} {_pct(v)}" for g, v in sorted(verdict.groups.items()))
        return f"그룹별 독립: {per}"
    return "한도 정보 없음"


def table(results: list[ProviderResult], *, color: bool = True, now: dt.datetime | None = None) -> str:
    lines: list[str] = []
    for result in results:
        display_id = _display_id(result)
        if result.error:
            lines.append(f"{display_id:<7} -- {result.error}")
            if result.hint:
                lines.append(f"        힌트: {result.hint}")
            lines.append("")
            continue

        verdict = result.verdict_at(now)
        plan = f" [{result.plan}]" if result.plan else ""
        basis = _basis_text(result, verdict)
        age = format_age(result.age_s)
        stamp = f"  ({age}{', 캐시' if result.stale else ''})" if age else ""

        if result.warning or result.stale:
            mark_text = _mark(verdict.mark, color)
        elif verdict.waste and result.pool_class == "spend":
            mark_text = _waste_mark(color)
            basis = f"{verdict.waste_advice}"
        elif result.pool_class == "spend" and verdict.basis != "none":
            mark_text = _mark(verdict.mark, color)
            progress = "소진 진행"
            if verdict.blocking_pct >= WARN_PCT:
                progress += f" · blocking {_pct(verdict.blocking_pct).replace('사용 ', '')}"
            basis = f"{progress} · {basis}"
        else:
            mark_text = _mark(verdict.mark, color)

        lines.append(f"{display_id}{plan}  [{mark_text}] {basis}{stamp}")

        for bucket in result.buckets:
            tags = [t for t in (bucket.note,) if t]
            if bucket.scope.kind == "model":
                tags.append("이 모델만")
            elif bucket.scope.kind == "group" and verdict.basis != "group":
                tags.append("이 그룹만")
            tag_s = f"  [{', '.join(tags)}]" if tags else ""
            horizon = {"now": "now ", "week": "week", "month": "month"}[bucket.horizon]
            lines.append(
                f"  {horizon} {bucket.label:<24} {_pct(bucket.used_pct):>11}"
                f"   reset {iso_to_local(bucket.resets_at)}{_table_pace(bucket, now=now)}{tag_s}"
            )

        for bucket in verdict.exhausted:
            others = (
                "다른 모델은 계정 한도 범위에서 사용 가능"
                if verdict.basis == "account"
                else "다른 그룹은 사용 가능"
            )
            lines.append(
                f"  ! {bucket.scope.label} 소진({_pct(bucket.used_pct)}, "
                f"reset {iso_to_local(bucket.resets_at)}) — {others}"
            )
        if result.note:
            lines.append(f"  ! {result.note}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def brief(
    results: list[ProviderResult],
    *,
    color: bool = True,
    horizon: str = "both",
    now: dt.datetime | None = None,
) -> str:
    """한 줄 요약. herdr pane / statusline / 알림용.

    예: [CRIT] claude now 사용 6% week 사용 97%(Fable소진)
        | codex week 사용 82% | agy gemini 사용 7% / 3p 사용 57%
    """
    chunks: list[str] = []
    worst = overall_mark(results, now=now)
    for result in results:
        display_id = _display_id(result)
        if result.error:
            err_msg = result.error.splitlines()[0]
            chunks.append(f"{display_id} n/a({err_msg})")
            continue
        if result.warning and not result.stale:
            chunks.append(f"{display_id} warn({result.warning})")
            continue
        verdict = result.verdict_at(now)
        parts: list[str] = []
        if result.warning:
            parts.append(f"warn({result.warning})")
        if verdict.basis == "group":
            parts += [
                f"{g} 사용 {v:g}%{_pace(_bucket_for(result, scope_name=g), now=now)}"
                for g, v in sorted(verdict.groups.items())
            ]
        else:
            if horizon in ("now", "both") and verdict.now_pct is not None:
                parts.append(
                    f"now 사용 {verdict.now_pct:g}%{_pace(_bucket_for(result, horizon='now'), now=now)}"
                )
            if horizon in ("week", "both") and verdict.week_pct is not None:
                parts.append(
                    f"week 사용 {verdict.week_pct:g}%{_pace(_bucket_for(result, horizon='week'), now=now)}"
                )
            if horizon == "both" and verdict.month_pct is not None:
                parts.append(
                    f"month 사용 {verdict.month_pct:g}%{_pace(_bucket_for(result, horizon='month'), now=now)}"
                )
        if not parts:
            # 요청한 지평에 데이터가 없으면 빈칸을 남기지 않고 있는 축을 보여준다.
            other = verdict.week_pct if horizon == "now" else verdict.now_pct
            label = "week" if horizon == "now" else "now"
            parts.append(f"{label} 사용 {other:g}%" if other is not None else "n/a")
        if verdict.exhausted:
            parts.append("(" + ",".join(f"{b.scope.label}소진" for b in verdict.exhausted) + ")")
        if not result.stale:
            if verdict.waste and result.pool_class == "spend":
                parts.append("(WASTE: 리셋 전 소진 권장)")
            elif result.pool_class == "spend" and verdict.basis != "none":
                parts.append("(소진 진행)")
        if result.stale:
            parts.append(f"[{format_age(result.age_s)}]")
        chunks.append(f"{display_id} " + " ".join(parts))
    return f"[{_mark(worst, color)}] " + " | ".join(chunks)
