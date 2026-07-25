"""scopefuel 데이터 모델 — 스코프(scope)와 지평(horizon)이 1급 개념이다.

이 도구의 존재 이유: 한도를 하나의 숫자로 뭉개면 오독한다.
- scope : 무엇이 막히는가.  account(계정 전체) / model(그 모델만) / group(그 그룹만)
- horizon: 언제의 이야기인가. now(5시간급 창 — 지금 작업을 띄울 수 있나)
                              week(주간급 창 — 이번 주 예산이 남았나)

두 축을 분리하지 않으면 "특정 모델 하나가 소진됐다"를 "계정이 막혔다"로,
"주간 97%"를 "지금 일할 수 없다"로 잘못 읽는다. 실제로 그렇게 오독한 사례가 이 도구의 출발점이다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

SCHEMA = "scopefuel.v1"

ScopeKind = Literal["account", "model", "group"]
Horizon = Literal["now", "week"]
Mark = Literal["ok", "warn", "crit"]

WARN_PCT = 75.0
CRIT_PCT = 90.0

# 이 이하의 창은 "지금"으로 본다 (5h 창 = 21600초).
NOW_HORIZON_MAX_S = 6 * 3600


@dataclass(frozen=True)
class Scope:
    kind: ScopeKind
    name: str | None = None

    def as_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name}

    @property
    def label(self) -> str:
        return self.name or self.kind


@dataclass
class Bucket:
    """하나의 한도 창. used_pct 는 0~100, 모르면 None (추측하지 않는다)."""

    label: str
    window: str  # "5h", "7d", "1d" 등 표시용
    used_pct: float | None
    resets_at: str | None = None  # ISO8601
    scope: Scope = field(default_factory=lambda: Scope("account"))
    horizon: Horizon = "week"
    note: str | None = None

    @property
    def remaining_pct(self) -> float | None:
        return None if self.used_pct is None else max(0.0, 100.0 - self.used_pct)

    @property
    def mark(self) -> Mark:
        return mark_for(self.used_pct)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "window": self.window,
            "horizon": self.horizon,
            "used_pct": self.used_pct,
            "remaining_pct": self.remaining_pct,
            "resets_at": self.resets_at,
            "scope": self.scope.as_dict(),
            "severity": self.mark,
            "note": self.note,
        }


@dataclass
class Verdict:
    """'실제로 무엇이 막히는가'의 판정."""

    now_pct: float | None
    week_pct: float | None
    blocking_pct: float
    basis: Literal["account", "group", "none"]
    mark: Mark
    exhausted: list[Bucket] = field(default_factory=list)
    groups: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "now_pct": self.now_pct,
            "week_pct": self.week_pct,
            "blocking_pct": self.blocking_pct,
            "basis": self.basis,
            "mark": self.mark,
            "groups": self.groups,
            "exhausted": [b.as_dict() for b in self.exhausted],
        }


@dataclass
class ProviderResult:
    id: str
    plan: str | None = None
    buckets: list[Bucket] = field(default_factory=list)
    note: str | None = None
    error: str | None = None
    hint: str | None = None
    source: str | None = None  # 어떤 경로로 얻었는지 (예: "local-server", "cloud")
    fetched_at: float | None = None
    age_s: float | None = None
    stale: bool = False
    raw: dict | None = None

    @property
    def verdict(self) -> Verdict:
        return verdict_for(self.buckets)

    def as_dict(self, include_raw: bool = False) -> dict:
        out = {
            "id": self.id,
            "status": "error" if self.error else ("stale" if self.stale else "ok"),
            "plan": self.plan,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "age_s": None if self.age_s is None else round(self.age_s, 1),
            "stale": self.stale,
            "error": self.error,
            "hint": self.hint,
            "note": self.note,
            "buckets": [b.as_dict() for b in self.buckets],
            "verdict": self.verdict.as_dict(),
        }
        if include_raw:
            out["raw"] = self.raw
        return out


def mark_for(used_pct: float | None) -> Mark:
    if used_pct is None:
        return "ok"
    if used_pct >= CRIT_PCT:
        return "crit"
    if used_pct >= WARN_PCT:
        return "warn"
    return "ok"


def horizon_for(window_seconds: float | None) -> Horizon:
    if window_seconds is None:
        return "week"
    return "now" if window_seconds <= NOW_HORIZON_MAX_S else "week"


def window_label(seconds: float | None) -> str:
    if not seconds:
        return "?"
    for secs, label in ((604800, "7d"), (86400, "1d"), (18000, "5h")):
        if abs(seconds - secs) < 60:
            return label
    return f"{seconds / 3600:g}h"


def verdict_for(buckets: list[Bucket]) -> Verdict:
    """차단 판정. account 스코프만 전체를 막는다.

    - account 행이 있으면 그 최대치가 blocking (model/group 스코프는 제외).
    - account 행이 없는 provider(예: agy)는 그룹이 서로 독립이므로,
      '가장 여유 있는 그룹'이 얼마나 찼는지를 전체 차단 지표로 본다
      (한 그룹이 막혀도 다른 그룹으로 계속 작업할 수 있으므로).
    """
    known = [b for b in buckets if b.used_pct is not None]
    account = [b for b in known if b.scope.kind == "account"]
    groups: dict[str, float] = {}
    for bucket in known:
        if bucket.scope.kind == "group":
            key = bucket.scope.label
            groups[key] = max(groups.get(key, 0.0), bucket.used_pct or 0.0)

    if account:
        blocking = max(b.used_pct or 0.0 for b in account)
        basis: Literal["account", "group", "none"] = "account"
    elif groups:
        blocking = min(groups.values())
        basis = "group"
    else:
        blocking, basis = 0.0, "none"

    def axis(horizon: Horizon) -> float | None:
        pool = [b.used_pct or 0.0 for b in known if b.horizon == horizon and b.scope.kind != "model"]
        return max(pool) if pool else None

    exhausted = [b for b in known if b.scope.kind != "account" and (b.used_pct or 0.0) >= CRIT_PCT]
    return Verdict(
        now_pct=axis("now"),
        week_pct=axis("week"),
        blocking_pct=blocking,
        basis=basis,
        mark=mark_for(blocking),
        exhausted=exhausted,
        groups=groups,
    )


def overall_mark(results: list[ProviderResult]) -> Mark:
    ranking = {"ok": 0, "warn": 1, "crit": 2}
    worst: Mark = "ok"
    for result in results:
        if result.error:
            continue
        if ranking[result.verdict.mark] > ranking[worst]:
            worst = result.verdict.mark
    return worst


def iso_to_local(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return iso


def epoch_to_iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    seconds = epoch / 1000 if epoch > 1e12 else epoch
    return dt.datetime.fromtimestamp(seconds, dt.UTC).isoformat()
