"""Pool-level policy overrides stored in XDG config TOML.

No config → BUILTIN behavior exactly. Overrides can expire; expired entries are
ignored and surfaced in `policy list` so a temporary tweak does not silently
become permanent policy.

``[pools.<p>] cutoff`` / ``on_exhaust`` (task #638) are operator-set config
values, not a bypass path — task #461's invariant stands: a request-time
``--operator-request`` can never skip quota/exclude/cutoff checks. The gate
applies the configured cutoff to every profile path exactly as it applied the
builtin one.

``[pools.<p>] subscribed = false`` / ``[profiles.<name>] subscribed = <bool>``
(task #742) mark a plan or profile as unsubscribed without deleting anything:
catalog rows, reps and grade history stay; recommend and gate exclude them;
list and catalog views keep showing the rows (marked). ``[profiles.<name>]``
overrides ``[pools.<p>]`` — an explicit profile value wins over the pool flag
in either direction, and the canonical profile name wins over alias spellings
of the same entity. A missing key has no opinion; the shipped default flags
nothing. Flipping the flag back to true restores eligibility.
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
    bench = config.get("bench")
    if isinstance(bench, dict) and bench:
        lines.append("[bench]")
        backend = bench.get("backend")
        if backend is not None:
            lines.append(f"backend = {_toml_string(str(backend))}")
        cache_ttl_s = bench.get("cache_ttl_s")
        if isinstance(cache_ttl_s, (int, float)) and not isinstance(cache_ttl_s, bool):
            lines.append(f"cache_ttl_s = {cache_ttl_s!r}")
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
            if "cutoff" in entry and entry["cutoff"] is not None:
                raw_cutoff = entry["cutoff"]
                # 숫자만 TOML 수치로 쓴다 — bool/문자열은 repr(`True`)이 TOML 을
                # 깨뜨리므로 문자열로 보존한다. 재독 시 get_cutoff 가 여전히 거부한다.
                if isinstance(raw_cutoff, bool) or not isinstance(raw_cutoff, (int, float)):
                    lines.append(f"cutoff = {_toml_string(str(raw_cutoff))}")
                else:
                    lines.append(f"cutoff = {raw_cutoff!r}")
            if "on_exhaust" in entry and entry["on_exhaust"] is not None:
                lines.append(f"on_exhaust = {_toml_string(str(entry['on_exhaust']))}")
            _write_subscribed(lines, entry)
            lines.append("")
    profiles = config.get("profiles")
    if isinstance(profiles, dict):
        for name in sorted(profiles):
            entry = profiles[name]
            if not isinstance(entry, dict) or not entry:
                continue
            lines.append(f"[profiles.{_toml_string(str(name))}]")
            _write_subscribed(lines, entry)
            lines.append("")
    text = "\n".join(lines).rstrip() + "\n" if lines else ""
    path.write_text(text, encoding="utf-8")
    if text:
        path.chmod(0o600)


def _write_subscribed(lines: list[str], entry: dict) -> None:
    """Serialize ``subscribed`` — bools as TOML bools, anything else as a string
    so a bad value round-trips visibly instead of being silently dropped."""
    raw = entry.get("subscribed")
    if raw is None:
        return
    if isinstance(raw, bool):
        lines.append(f"subscribed = {'true' if raw else 'false'}")
    else:
        lines.append(f"subscribed = {_toml_string(str(raw))}")


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


ON_EXHAUST_MODES = frozenset({"block", "operator-switch"})
DEFAULT_ON_EXHAUST = "block"


def get_cutoff(pool: str, default: float) -> tuple[float, str | None]:
    """``[pools.<p>] cutoff`` — 풀별 사용량 차단선(0~100). 미설정 시 ``default``.

    잘못된 값(비수치·bool·범위 밖·NaN/inf)은 거부하고 default 로 폴백한다 —
    오타가 차단선을 조용히 0 이나 100 으로 바꾸지 않게 하기 위한 fail-closed
    관례(``get_capacity_weight`` 와 동일: 폴백 + status 문자열 노출).
    """
    config = load_config()
    pools = config.get("pools") or {}
    entry = pools.get(pool)
    if not isinstance(entry, dict):
        return default, None
    raw = entry.get("cutoff")
    if raw is None:
        return default, None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default, f"invalid cutoff {raw!r} (기본값 {default:g}%로 폴백)"
    value = float(raw)
    if not _finite(value) or not 0.0 <= value <= 100.0:
        return default, f"invalid cutoff {raw!r} (기본값 {default:g}%로 폴백)"
    return value, None


def get_on_exhaust(pool: str) -> tuple[str, str | None]:
    """``[pools.<p>] on_exhaust`` — ``"block"``(기본) 또는 ``"operator-switch"``.

    ``operator-switch`` 면 차단선 도달 시 "계정 전환 필요" 알림을 운영자 경로로
    올린다(exhaust.observe). 그 외 값은 ``block`` 으로 폴백하고 status 로
    노출한다 — 오타가 알림을 켜거나 끄는 일이 없게 한다.
    """
    config = load_config()
    pools = config.get("pools") or {}
    entry = pools.get(pool)
    if not isinstance(entry, dict):
        return DEFAULT_ON_EXHAUST, None
    raw = entry.get("on_exhaust")
    if raw is None:
        return DEFAULT_ON_EXHAUST, None
    if not isinstance(raw, str) or raw not in ON_EXHAUST_MODES:
        return DEFAULT_ON_EXHAUST, f"invalid on_exhaust {raw!r} (block 으로 폴백)"
    return raw, None


# ---------------------------------------------------------------------------
# task #742 — 구독 해지(unsubscribed) 플래그
#
# ``[pools.<p>] subscribed = false``  → 그 풀의 모든 프로필이 구독 해지.
# ``[profiles.<name>] subscribed = <bool>`` → 프로필 수준 오버라이드(풀보다 우선).
# 키가 없으면 의견 없음(구독 유지). bool 이 아닌 값은 무효 — 무시하고 한 수준
# 아래로 폴백하면서 status 문자열을 남긴다(오타가 조용히 플래그를 켜거나 끄지
# 않게 하는 이 레이어의 기존 fail-open 관례와 같다).


def _pools_table(config: dict) -> dict:
    pools = config.get("pools")
    return pools if isinstance(pools, dict) else {}


def _profiles_table(config: dict) -> dict:
    profiles = config.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def get_subscribed(pool: str) -> tuple[bool, str | None]:
    """``[pools.<pool>] subscribed`` — 풀 수준 구독 플래그 (기본 True)."""
    entry = _pools_table(load_config()).get(pool)
    if not isinstance(entry, dict) or "subscribed" not in entry:
        return True, None
    raw = entry["subscribed"]
    if isinstance(raw, bool):
        return raw, None
    return True, f"invalid subscribed {raw!r} — bool 이 아니라 구독 유지로 폴백"


def get_profile_subscribed(profile: str) -> tuple[bool | None, str | None]:
    """``[profiles.<profile>] subscribed`` — 프로필 수준 오버라이드.

    (True|False, None) 명시 값, (None, None) 미설정(풀 수준으로 폴백),
    (None, status) bool 아닌 무효 값 — 호출자가 다음 수준으로 진행한다.
    """
    entry = _profiles_table(load_config()).get(profile)
    if not isinstance(entry, dict) or "subscribed" not in entry:
        return None, None
    raw = entry["subscribed"]
    if isinstance(raw, bool):
        return raw, None
    return None, f"invalid subscribed {raw!r} — pool 수준으로 폴백"


def set_subscribed(pool: str, value: bool | None) -> None:
    """Write ``[pools.<pool>] subscribed``. ``None`` removes just that key.

    An entry left empty by the removal is dropped entirely — an orphan
    ``[pools.<pool>]`` table would show in ``policy list`` as configured while
    carrying nothing.
    """
    config = load_config()
    pools = config.setdefault("pools", {})
    entry = dict(pools.get(pool) or {})
    if value is None:
        entry.pop("subscribed", None)
    else:
        entry["subscribed"] = value
    if entry:
        pools[pool] = entry
    else:
        pools.pop(pool, None)
        if not pools:
            config.pop("pools", None)
    _write_config(config)


def set_profile_subscribed(profile: str, value: bool | None) -> None:
    """Write ``[profiles.<profile>] subscribed``. ``None`` removes just that key
    (the profile falls back to the pool level). An explicit ``true`` overrides
    an unsubscribed pool; an explicit ``false`` overrides a subscribed pool —
    the two are not interchangeable with removal."""
    config = load_config()
    profiles = config.setdefault("profiles", {})
    entry = dict(profiles.get(profile) or {})
    if value is None:
        entry.pop("subscribed", None)
    else:
        entry["subscribed"] = value
    if entry:
        profiles[profile] = entry
    else:
        profiles.pop(profile, None)
    if not profiles:
        config.pop("profiles", None)
    _write_config(config)


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
    subscribed: bool
    subscribed_status: str | None


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
        subscribed, subscribed_status = get_subscribed(name)
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
                subscribed=subscribed,
                subscribed_status=subscribed_status,
            )
        )
        _ = weight_status  # weight_status 는 get_capacity_weight 폴백 사유; 열 표시는 값만 사용.
    return rows


@dataclass(frozen=True)
class ProfileSubscriptionRow:
    """``policy list`` 프로필 행 — ``[profiles.<name>] subscribed`` 오버라이드."""

    profile: str
    subscribed: bool | None  # None = 키는 있으나 값이 bool 이 아님(무효)
    status: str | None


def list_profile_subscriptions(known_profiles: set[str]) -> list[ProfileSubscriptionRow]:
    """Every ``[profiles.<name>]`` entry, marked when the name is not a known
    profile or alias spelling — an override nothing resolves to is a config
    bug worth surfacing, not a silent no-op."""
    profiles = _profiles_table(load_config())
    rows: list[ProfileSubscriptionRow] = []
    for name in sorted(profiles):
        value, status = get_profile_subscribed(name)
        if name not in known_profiles:
            status = f"unknown profile{'; ' + status if status else ''}"
        rows.append(ProfileSubscriptionRow(profile=name, subscribed=value, status=status))
    return rows
