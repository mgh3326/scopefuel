"""출력 렌더러 — 표(사람) / 한 줄(statusline·pane) / JSON(에이전트 계약)."""

from __future__ import annotations

from .cache import format_age
from .model import ProviderResult, iso_to_local, overall_mark

MARK_TEXT = {"ok": "ok", "warn": "WARN", "crit": "CRIT", "degraded": "DEGRADED"}
MARK_COLOR = {"ok": "\033[32m", "warn": "\033[33m", "crit": "\033[31m", "degraded": "\033[33m"}
RESET = "\033[0m"


def _mark(mark: str, color: bool) -> str:
    text = MARK_TEXT[mark]
    return f"{MARK_COLOR[mark]}{text}{RESET}" if color else text


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{value:g}%"


def table(results: list[ProviderResult], *, color: bool = True) -> str:
    lines: list[str] = []
    for result in results:
        if result.error:
            lines.append(f"{result.id:<7} -- {result.error}")
            if result.hint:
                lines.append(f"        힌트: {result.hint}")
            lines.append("")
            continue

        verdict = result.verdict
        plan = f" [{result.plan}]" if result.plan else ""
        if verdict.basis == "account":
            basis = f"지금(5h급) {_pct(verdict.now_pct)} · 이번주 {_pct(verdict.week_pct)}"
        elif verdict.basis == "group":
            per = " / ".join(f"{g} {v:g}%" for g, v in sorted(verdict.groups.items()))
            basis = f"그룹별 독립: {per}"
        else:
            basis = "한도 정보 없음"
        age = format_age(result.age_s)
        stamp = f"  ({age}{', 캐시' if result.stale else ''})" if age else ""
        lines.append(f"{result.id}{plan}  [{_mark(verdict.mark, color)}] {basis}{stamp}")

        for bucket in result.buckets:
            tags = [t for t in (bucket.note,) if t]
            if bucket.scope.kind == "model":
                tags.append("이 모델만")
            elif bucket.scope.kind == "group" and verdict.basis != "group":
                tags.append("이 그룹만")
            tag_s = f"  [{', '.join(tags)}]" if tags else ""
            horizon = "now " if bucket.horizon == "now" else "week"
            lines.append(
                f"  {horizon} {bucket.label:<24} {_pct(bucket.used_pct):>6}"
                f"   reset {iso_to_local(bucket.resets_at)}{tag_s}"
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


def brief(results: list[ProviderResult], *, color: bool = True, horizon: str = "both") -> str:
    """한 줄 요약. herdr pane / statusline / 알림용.

    예: [CRIT] claude now 6% week 97%(Fable소진) | codex week 82% | agy gemini 7% / 3p 57%
    """
    chunks: list[str] = []
    worst = overall_mark(results)
    for result in results:
        if result.error:
            err_msg = result.error.splitlines()[0]
            chunks.append(f"{result.id} n/a({err_msg})")
            continue
        verdict = result.verdict
        parts: list[str] = []
        if verdict.basis == "group":
            parts += [f"{g} {v:g}%" for g, v in sorted(verdict.groups.items())]
        else:
            if horizon in ("now", "both") and verdict.now_pct is not None:
                parts.append(f"now {verdict.now_pct:g}%")
            if horizon in ("week", "both") and verdict.week_pct is not None:
                parts.append(f"week {verdict.week_pct:g}%")
        if not parts:
            # 요청한 지평에 데이터가 없으면 빈칸을 남기지 않고 있는 축을 보여준다.
            other = verdict.week_pct if horizon == "now" else verdict.now_pct
            label = "week" if horizon == "now" else "now"
            parts.append(f"{label} {other:g}%" if other is not None else "n/a")
        if verdict.exhausted:
            parts.append("(" + ",".join(f"{b.scope.label}소진" for b in verdict.exhausted) + ")")
        if result.stale:
            parts.append(f"[{format_age(result.age_s)}]")
        chunks.append(f"{result.id} " + " ".join(parts))
    return f"[{_mark(worst, color)}] " + " | ".join(chunks)
