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
import math
import re
from dataclasses import dataclass, field
from typing import Literal

SCHEMA = "scopefuel.v1"

ScopeKind = Literal["account", "model", "group"]
Horizon = Literal["now", "week", "month"]
Mark = Literal["ok", "warn", "crit", "degraded"]
PoolClass = Literal["preserve", "spend"]

WARN_PCT = 75.0
CRIT_PCT = 90.0
WASTE_PCT = 70.0
WASTE_WINDOW_S = 24 * 3600

# 이 이하의 창은 "지금"으로 본다 (5h 창 = 21600초).
NOW_HORIZON_MAX_S = 6 * 3600

_WINDOW_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>[dhms])$")
_WINDOW_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


@dataclass(frozen=True)
class Pace:
    """시간 창 대비 사용 속도와 창을 모두 쓰기 위한 권장 사용률."""

    ratio: float | None
    full_use_rate: float | None
    full_use_rate_unit: str | None


def _window_seconds(window: str | None) -> float | None:
    if not window:
        return None
    match = _WINDOW_RE.fullmatch(window.strip())
    if not match:
        return None
    return float(match["value"]) * _WINDOW_SECONDS[match["unit"]]


def _parse_reset(iso: str | None) -> dt.datetime | None:
    if not iso:
        return None
    try:
        parsed = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC)


def pace_for(bucket: Bucket, *, now: dt.datetime | None = None) -> Pace:
    """계산할 수 없을 때는 0을 추측하지 않고 모두 None으로 둔다."""
    if not _is_valid_used_pct(bucket.used_pct):
        return Pace(None, None, None)
    window_seconds = _window_seconds(bucket.window)
    reset_at = _parse_reset(bucket.resets_at)
    if window_seconds is None or window_seconds <= 0 or reset_at is None:
        return Pace(None, None, None)

    now = now or dt.datetime.now(dt.UTC)
    now = now.replace(tzinfo=dt.UTC) if now.tzinfo is None else now.astimezone(dt.UTC)
    time_to_reset = (reset_at - now).total_seconds()
    elapsed_fraction = (window_seconds - time_to_reset) / window_seconds
    # stale cache / clock skew can put either endpoint outside the active window.
    if not 0 < elapsed_fraction <= 1 or time_to_reset <= 0:
        return Pace(None, None, None)

    if window_seconds < 86400:
        rate_divisor, unit = 3600, "%/h"
    else:
        rate_divisor, unit = 86400, "%/일"
    remaining_duration_units = time_to_reset / rate_divisor
    if remaining_duration_units <= 0:
        return Pace(None, None, None)
    return Pace(
        ratio=(bucket.used_pct / 100) / elapsed_fraction,
        full_use_rate=bucket.remaining_pct / remaining_duration_units,
        full_use_rate_unit=unit,
    )


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

    @property
    def pace(self) -> Pace:
        return pace_for(self)

    def pace_at(self, now: dt.datetime | None = None) -> Pace:
        return pace_for(self, now=now)

    def as_dict(self, pool_class: PoolClass = "preserve", *, now: dt.datetime | None = None) -> dict:
        pace = self.pace_at(now)
        return {
            "label": self.label,
            "window": self.window,
            "horizon": self.horizon,
            "used_pct": self.used_pct,
            "remaining_pct": self.remaining_pct,
            "pace": pace.ratio,
            "full_use_rate": pace.full_use_rate,
            "full_use_rate_unit": pace.full_use_rate_unit,
            "resets_at": self.resets_at,
            "scope": self.scope.as_dict(),
            "severity": mark_for(self.used_pct, pool_class),
            "note": self.note,
        }


@dataclass
class Verdict:
    """'실제로 무엇이 막히는가'의 판정."""

    now_pct: float | None
    week_pct: float | None
    month_pct: float | None
    blocking_pct: float
    basis: Literal["account", "group", "none"]
    mark: Mark
    exhausted: list[Bucket] = field(default_factory=list)
    groups: dict[str, float] = field(default_factory=dict)
    waste: bool = False
    waste_advice: str | None = None

    def as_dict(self) -> dict:
        out = {
            "now_pct": self.now_pct,
            "week_pct": self.week_pct,
            "blocking_pct": self.blocking_pct,
            "basis": self.basis,
            "mark": self.mark,
            "groups": self.groups,
            "exhausted": [b.as_dict() for b in self.exhausted],
            "waste": self.waste,
        }
        if self.month_pct is not None:
            out["month_pct"] = self.month_pct
        if self.waste_advice is not None:
            out["waste_advice"] = self.waste_advice
        return out


@dataclass
class ProviderResult:
    id: str
    plan: str | None = None
    buckets: list[Bucket] = field(default_factory=list)
    note: str | None = None
    error: str | None = None
    warning: str | None = None
    hint: str | None = None
    source: str | None = None  # 어떤 경로로 얻었는지 (예: "local-server", "cloud")
    fetched_at: float | None = None
    age_s: float | None = None
    stale: bool = False
    raw: dict | None = None
    pool_class: PoolClass = "preserve"

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        if self.stale:
            return "stale"
        return "warning" if self.warning else "ok"

    def verdict_at(self, now: dt.datetime | None = None) -> Verdict:
        v = verdict_for(self.buckets, self.pool_class, now=now)
        if v.mark == "ok" and (self.error or self.stale or self.warning):
            return Verdict(
                now_pct=v.now_pct,
                week_pct=v.week_pct,
                month_pct=v.month_pct,
                blocking_pct=v.blocking_pct,
                basis=v.basis,
                mark="warn" if self.warning and not self.error and not self.stale else "degraded",
                exhausted=v.exhausted,
                groups=v.groups,
                waste=False,
                waste_advice=None,
            )
        return v

    @property
    def verdict(self) -> Verdict:
        return self.verdict_at()

    def as_dict(self, include_raw: bool = False, *, now: dt.datetime | None = None) -> dict:
        verdict = self.verdict_at(now)
        out = {
            "id": self.id,
            "status": self.status,
            "plan": self.plan,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "age_s": None if self.age_s is None else round(self.age_s, 1),
            "stale": self.stale,
            "error": self.error,
            "hint": self.hint,
            "note": self.note,
            "pool_class": self.pool_class,
            "buckets": [b.as_dict(self.pool_class, now=now) for b in self.buckets],
            "verdict": verdict.as_dict(),
        }
        if self.warning is not None:
            out["warning"] = self.warning
        if include_raw:
            out["raw"] = self.raw
        return out


def mark_for(used_pct: float | None, pool_class: PoolClass = "preserve") -> Mark:
    if used_pct is None:
        return "ok"
    if pool_class == "spend":
        return "ok"
    if used_pct >= CRIT_PCT:
        return "crit"
    if used_pct >= WARN_PCT:
        return "warn"
    return "ok"


def _is_valid_used_pct(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    try:
        f = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(f) and 0 <= f <= 100


def waste_for(
    buckets: list[Bucket], pool_class: PoolClass = "preserve", *, now: dt.datetime | None = None
) -> tuple[bool, str | None]:
    """spend 풀에서 reset 전 24h 미만, 사용률 70% 미만인 버킷이 있으면 WASTE."""
    if pool_class != "spend":
        return False, None
    now = now or dt.datetime.now(dt.UTC)
    now = now.replace(tzinfo=dt.UTC) if now.tzinfo is None else now.astimezone(dt.UTC)
    waste_buckets: list[str] = []
    for bucket in buckets:
        if not _is_valid_used_pct(bucket.used_pct):
            continue
        assert isinstance(bucket.used_pct, (int, float))
        if bucket.used_pct >= WASTE_PCT:
            continue
        reset_at = _parse_reset(bucket.resets_at)
        if reset_at is None:
            continue
        remaining_s = (reset_at - now).total_seconds()
        if 0 < remaining_s < WASTE_WINDOW_S:
            waste_buckets.append(f"{bucket.label} 사용 {bucket.used_pct:g}%")
    if not waste_buckets:
        return False, None
    return True, "리셋 전 소진 권장: " + ", ".join(waste_buckets)


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


def verdict_for(
    buckets: list[Bucket],
    pool_class: PoolClass = "preserve",
    *,
    now: dt.datetime | None = None,
) -> Verdict:
    """차단 판정. account 스코프만 전체를 막는다.

    - account 행이 있으면 그 최대치가 blocking (model/group 스코프는 제외).
    - account 행이 없는 provider(예: agy)는 그룹이 서로 독립이므로,
      '가장 여유 있는 그룹'이 얼마나 찼는지를 전체 차단 지표로 본다
      (한 그룹이 막혀도 다른 그룹으로 계속 작업할 수 있으므로).
    - spend 풀은 고사용을 차단으로 보지 않는다.
    """
    known = [b for b in buckets if _is_valid_used_pct(b.used_pct)]
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

    exhausted = (
        []
        if pool_class == "spend"
        else [b for b in known if b.scope.kind != "account" and (b.used_pct or 0.0) >= CRIT_PCT]
    )
    waste, waste_advice = waste_for(buckets, pool_class, now=now)
    return Verdict(
        now_pct=axis("now"),
        week_pct=axis("week"),
        month_pct=axis("month"),
        blocking_pct=blocking,
        basis=basis,
        mark=mark_for(blocking, pool_class),
        exhausted=exhausted,
        groups=groups,
        waste=waste,
        waste_advice=waste_advice,
    )


def overall_mark(results: list[ProviderResult]) -> Mark:
    ranking = {"ok": 0, "warn": 1, "degraded": 2, "crit": 3}
    worst: Mark = "ok"
    for result in results:
        mark = result.verdict.mark
        if ranking[mark] > ranking[worst]:
            worst = mark
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
