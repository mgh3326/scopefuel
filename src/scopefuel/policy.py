"""Pool-level policy overrides stored in XDG config TOML.

No config → BUILTIN behavior exactly. Overrides can expire; expired entries are
ignored and surfaced in `policy list` so a temporary tweak does not silently
become permanent policy.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
from dataclasses import dataclass
from typing import Literal

from .model import PoolClass

NEAR_EXPIRY_DAYS = 3
DEFAULT_RESET_URGENCY_HOURS = 12.0
DEFAULT_IMMINENT_RESET_HOURS = 1.0
DEFAULT_IMMINENT_REMAINING_PCT = 5.0


def config_path() -> pathlib.Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (pathlib.Path.home() / ".config")
    return pathlib.Path(base) / "scopefuel" / "config.toml"


def load_config() -> dict:
    path = config_path()
    try:
        import tomllib

        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _parse_date(value: object) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def _normalize_class(value: object) -> PoolClass | None:
    if value in ("preserve", "spend", "exclude"):
        return value  # type: ignore[return-value]
    return None


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _write_config(config: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    settings = config.get("settings")
    if isinstance(settings, dict) and settings:
        lines.append("[settings]")
        for key in sorted(settings):
            value = settings[key]
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                lines.append(f"{key} = {value!r}")
            else:
                lines.append(f"{key} = {_toml_string(str(value))}")
        lines.append("")
    pools = config.get("pools")
    if isinstance(pools, dict):
        for name in sorted(pools):
            entry = pools[name]
            if not isinstance(entry, dict):
                continue
            lines.append(f"[pools.{name}]")
            raw_class = entry.get("class")
            if raw_class is not None:
                if raw_class in ("preserve", "spend", "exclude"):
                    lines.append(f'class = "{raw_class}"')
                else:
                    lines.append(f"class = {_toml_string(str(raw_class))}")
            if "until" in entry and entry["until"]:
                lines.append(f"until = {entry['until']}")

            if "note" in entry and entry["note"] is not None:
                lines.append(f"note = {_toml_string(str(entry['note']))}")

            if "boost" in entry and entry["boost"] is not None:
                lines.append(f"boost = {int(entry['boost'])}")

            if "plan" in entry and entry["plan"] is not None:
                lines.append(f"plan = {_toml_string(str(entry['plan']))}")
            if "price_usd" in entry and entry["price_usd"] is not None:
                lines.append(f"price_usd = {entry['price_usd']!r}")
            if "capacity_weight" in entry and entry["capacity_weight"] is not None:
                lines.append(f"capacity_weight = {entry['capacity_weight']!r}")
            lines.append("")
    text = "\n".join(lines).rstrip() + "\n" if lines else ""
    path.write_text(text, encoding="utf-8")
    if text:
        path.chmod(0o600)


@dataclass(frozen=True)
class ActiveOverride:
    """Active (non-expired) pool policy override."""

    pool_class: PoolClass
    until: dt.date
    note: str | None = None


class BoostError(ValueError):
    """Raised when a boost value in config is invalid (fail-closed)."""


def _normalize_boost(value: object) -> int | None:
    """int 만 허용. bool 은 int 하위형이지만 명시적으로 거부한다."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise BoostError(f"boost 는 정수여야 합니다 (bool 불가): {value!r}")
    if isinstance(value, int):
        return value
    raise BoostError(f"boost 는 정수여야 합니다: {value!r}")


@dataclass(frozen=True)
class ActiveBoost:
    """Active (non-expired) numeric boost override."""

    boost: int
    until: dt.date


def _active_boost(pool: str, today: dt.date) -> ActiveBoost | None | str:
    """Return ActiveBoost, None if no boost entry, or status string if present but unusable.

    boost 만료는 별도 필드가 아니라 기존 pool-level ``until`` 을 재사용한다
    (승인된 CLI 표면: ``policy set <pool> [class] --until <date> --boost <N|none>``).
    """
    config = load_config()
    pools = config.get("pools") or {}
    entry = pools.get(pool)
    if not isinstance(entry, dict):
        return None

    raw_boost = entry.get("boost")
    if raw_boost is None:
        return None

    try:
        boost = _normalize_boost(raw_boost)
    except BoostError as exc:
        return str(exc)
    if boost is None:
        return None

    raw_until = entry.get("until")
    if not raw_until:
        return "boost missing until"

    until = _parse_date(raw_until)
    if until is None:
        return f"invalid until {raw_until!r}"

    if until < today:
        return f"boost expired {until}"

    return ActiveBoost(boost, until)


def get_boost(pool: str, today: dt.date | None = None) -> tuple[int | None, str | None]:
    """Return effective numeric boost and optional status note for a pool.

    Expired/missing/invalid boost -> (None, status) so callers fall back to default sort.
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()  # noqa: UP017 -- avoid dt.UTC (py<3.11 AttributeError, ROB-1188)
    result = _active_boost(pool, today)
    if result is None:
        return None, None
    if isinstance(result, str):
        return None, result

    notes: list[str] = []
    if result.until <= today + dt.timedelta(days=NEAR_EXPIRY_DAYS):
        notes.append(f"expires {result.until}")
    return result.boost, "; ".join(notes) if notes else None


def _active_override(pool: str, today: dt.date) -> ActiveOverride | None | str:
    """Return ActiveOverride, None if no entry, or status string if present but unusable."""
    config = load_config()
    pools = config.get("pools") or {}
    entry = pools.get(pool)
    if not isinstance(entry, dict):
        return None

    # A boost-only entry is intentionally allowed to omit ``class``.  It must
    # inherit the provider's builtin class instead of surfacing as the corrupt
    # ``invalid class None`` override that used to be written by
    # ``policy set <pool> --boost N --until ...``.
    if "class" not in entry:
        return None

    pool_class = _normalize_class(entry.get("class"))
    if pool_class is None:
        return f"invalid class {entry.get('class')!r}"

    raw_until = entry.get("until")
    if not raw_until:
        return "missing until"

    until = _parse_date(raw_until)
    if until is None:
        return f"invalid until {raw_until!r}"

    if until < today:
        return f"expired {until}"

    note = entry.get("note")
    return ActiveOverride(pool_class, until, str(note) if note else None)


def get_policy(
    pool: str, builtin_class: PoolClass = "preserve", today: dt.date | None = None
) -> tuple[PoolClass, str | None]:
    """Return effective pool class and optional status note for a pool."""
    today = today or dt.datetime.now(dt.timezone.utc).date()  # noqa: UP017 -- avoid dt.UTC (py<3.11 AttributeError, ROB-1188)
    override = _active_override(pool, today)
    if override is None:
        return builtin_class, None
    if isinstance(override, str):
        return builtin_class, override

    notes: list[str] = []
    if override.until <= today + dt.timedelta(days=NEAR_EXPIRY_DAYS):
        notes.append(f"expires {override.until}")
    if override.note:
        notes.append(override.note)
    return override.pool_class, "; ".join(notes) if notes else None


def get_active_override(pool: str, today: dt.date | None = None) -> ActiveOverride | None:
    """Return the active override for a pool, or None if none/expired/invalid."""
    today = today or dt.datetime.now(dt.timezone.utc).date()  # noqa: UP017 -- avoid dt.UTC (py<3.11 AttributeError, ROB-1188)
    override = _active_override(pool, today)
    return override if isinstance(override, ActiveOverride) else None


def set_policy(
    pool: str,
    pool_class: PoolClass | None,
    *,
    until: dt.date | None = None,
    note: str | None = None,
    boost: int | None | Literal["__unset__"] = "__unset__",
) -> None:
    """Set pool class and/or numeric boost.

    ``pool_class`` may be None when the call only touches boost (``policy set
    <pool> --boost N``/``--boost none`` without a class positional). ``boost``
    left at the sentinel default leaves any existing boost untouched; pass an
    explicit ``int`` to set it (requires ``until``, shared with the pool-level
    class expiry — there is no separate boost-until field) or ``None`` to
    clear it. ``plan``/``price_usd``/``capacity_weight`` are read-only from
    this module's perspective — they are config.toml-only fields with no CLI
    setter (operator-edited).
    """
    if pool_class is not None and until is None:
        raise ValueError("until(만료일)은 필수입니다")
    if boost is not None and boost != "__unset__" and until is None:
        raise ValueError("boost 설정에는 --until(만료일)이 필요합니다")

    config = load_config()
    pools = config.setdefault("pools", {})
    entry: dict[str, object] = dict(pools.get(pool) or {})

    if pool_class is not None:
        entry["class"] = pool_class
        entry["until"] = until.isoformat() if until else None
        if note is not None:
            entry["note"] = note

    if boost != "__unset__":
        if boost is None:
            entry.pop("boost", None)
        else:
            entry["boost"] = boost
            entry["until"] = until.isoformat() if until else None

    pools[pool] = entry
    _write_config(config)


def clear_policy(pool: str) -> bool:
    config = load_config()
    pools = config.get("pools")
    if not isinstance(pools, dict) or pool not in pools:
        return False
    del pools[pool]
    if not pools:
        config.pop("pools", None)
    _write_config(config)
    return True


def get_reset_urgency_hours() -> float:
    """``[settings] reset_urgency_hours`` — back-compat default 12.0 when unset/invalid."""
    config = load_config()
    settings = config.get("settings")
    if not isinstance(settings, dict):
        return DEFAULT_RESET_URGENCY_HOURS
    value = settings.get("reset_urgency_hours")
    if value is None or isinstance(value, bool):
        return DEFAULT_RESET_URGENCY_HOURS
    try:
        hours = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_RESET_URGENCY_HOURS
    if hours <= 0:
        return DEFAULT_RESET_URGENCY_HOURS
    return hours


def _positive_setting(name: str, default: float) -> float:
    """``[settings]`` 의 양수 float 설정 하나를 읽는다. 미설정/무효/0 이하는 default 로 폴백."""
    config = load_config()
    settings = config.get("settings")
    if not isinstance(settings, dict):
        return default
    value = settings.get(name)
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


def get_imminent_reset_hours() -> float:
    """``[settings] imminent_reset_hours`` — 이 시간 이내 리셋이면 소멸 임박 후보(기본 1h)."""
    return _positive_setting("imminent_reset_hours", DEFAULT_IMMINENT_RESET_HOURS)


def get_imminent_remaining_pct() -> float:
    """``[settings] imminent_remaining_pct`` — 이 잔여율 이상이면 소멸이 유의미(기본 5%)."""
    return _positive_setting("imminent_remaining_pct", DEFAULT_IMMINENT_REMAINING_PCT)


class CapacityWeightError(ValueError):
    """Raised by config-writers; readers use ``get_capacity_weight`` status instead."""


def _positive_number(value: object, field: str, pool: str) -> float | None:
    """None 반환 = 유효하지 않음(호출자가 폴백 여부를 status 로 판단)."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    f = float(value)
    if not _finite(f) or f <= 0:
        return None
    return f


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def get_capacity_weight(pool: str) -> tuple[float, str | None]:
    """capacity_weight > price_usd/20 > 1.0.

    기존 config 오류 관례(``get_policy``의 invalid class/until)와 동일하게,
    잘못된·0 이하 값은 예외를 올리지 않고 1.0(builtin)으로 안전 폴백하며
    status 문자열로 원인을 노출한다 — 가중치 오류가 조용히 순위만 바꾸지 않게 한다.
    """
    config = load_config()
    pools = config.get("pools") or {}
    entry = pools.get(pool)
    if not isinstance(entry, dict):
        return 1.0, None

    if "capacity_weight" in entry and entry["capacity_weight"] is not None:
        raw = entry["capacity_weight"]
        value = _positive_number(raw, "capacity_weight", pool)
        if value is None:
            return 1.0, f"invalid capacity_weight {raw!r} (1.0 으로 폴백)"
        return value, None

    if "price_usd" in entry and entry["price_usd"] is not None:
        raw = entry["price_usd"]
        price = _positive_number(raw, "price_usd", pool)
        if price is None:
            return 1.0, f"invalid price_usd {raw!r} (1.0 으로 폴백)"
        return price / 20.0, None

    return 1.0, None


def get_pool_plan(pool: str) -> str | None:
    config = load_config()
    pools = config.get("pools") or {}
    entry = pools.get(pool)
    if not isinstance(entry, dict):
        return None
    plan = entry.get("plan")
    return str(plan) if isinstance(plan, str) else None


def list_policies(
    known_pools: dict[str, PoolClass], today: dt.date | None = None
) -> list[tuple[str, PoolClass, str | None]]:
    """Return (pool, effective_class, status) for known pools plus unknown config entries."""
    today = today or dt.datetime.now(dt.timezone.utc).date()  # noqa: UP017 -- avoid dt.UTC (py<3.11 AttributeError, ROB-1188)
    config = load_config()
    pools = config.get("pools") or {}

    order = list(known_pools)
    seen = set(order)
    for name in sorted(pools):
        if name not in seen:
            order.append(name)

    out: list[tuple[str, PoolClass, str | None]] = []
    for name in order:
        builtin = known_pools.get(name, "preserve")
        effective, status = get_policy(name, builtin, today=today)
        if name not in known_pools:
            status = f"unknown pool{'; ' + status if status else ''}"
        out.append((name, effective, status))
    return out


@dataclass(frozen=True)
class PolicyRow:
    """``policy list`` 한 행 — configured pool fields와 그 출처([기본]/[설정])."""

    pool: str
    effective_class: PoolClass
    status: str | None
    class_configured: bool
    boost: int | None
    boost_status: str | None
    capacity_weight: float
    capacity_weight_configured: bool


def list_policy_rows(known_pools: dict[str, PoolClass], today: dt.date | None = None) -> list[PolicyRow]:
    """``list_policies`` 확장 — boost·capacity_weight·설정 출처를 함께 반환한다.

    ``class_configured`` 는 class 하나만이 아니라 해당 pool table이 config.toml에
    명시적으로 존재하는지를 나타낸다. 따라서 boost/capacity_weight/price_usd/note
    등 class 이외의 설정만 있어도 [설정]으로 표시한다.
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()  # noqa: UP017 -- avoid dt.UTC (py<3.11 AttributeError, ROB-1188)
    config = load_config()
    pools = config.get("pools") or {}

    order = list(known_pools)
    seen = set(order)
    for name in sorted(pools):
        if name not in seen:
            order.append(name)

    rows: list[PolicyRow] = []
    for name in order:
        builtin = known_pools.get(name, "preserve")
        effective, status = get_policy(name, builtin, today=today)
        if name not in known_pools:
            status = f"unknown pool{'; ' + status if status else ''}"
        entry = pools.get(name)
        class_configured = isinstance(entry, dict)
        boost, boost_status = get_boost(name, today=today)
        boost_configured = isinstance(entry, dict) and entry.get("boost") is not None
        weight, weight_status = get_capacity_weight(name)
        weight_configured = isinstance(entry, dict) and (
            entry.get("capacity_weight") is not None or entry.get("price_usd") is not None
        )
        # boost 무효(만료 등)라도 "설정한 적 있음"은 유지하되, get_boost 의 실패 사유를 status 에 병합.
        merged_boost_status = boost_status
        if boost_configured and boost is None and boost_status is None:
            merged_boost_status = None
        rows.append(
            PolicyRow(
                pool=name,
                effective_class=effective,
                status=status,
                class_configured=class_configured,
                boost=boost,
                boost_status=merged_boost_status if boost_configured else None,
                capacity_weight=weight,
                capacity_weight_configured=weight_configured,
            )
        )
        _ = weight_status  # weight_status 는 get_capacity_weight 폴백 사유; 열 표시는 값만 사용.
    return rows
