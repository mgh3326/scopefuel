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

from .model import PoolClass

NEAR_EXPIRY_DAYS = 3


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


def _active_override(pool: str, today: dt.date) -> ActiveOverride | None | str:
    """Return ActiveOverride, None if no entry, or status string if present but unusable."""
    config = load_config()
    pools = config.get("pools") or {}
    entry = pools.get(pool)
    if not isinstance(entry, dict):
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
    today = today or dt.datetime.now(dt.UTC).date()
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
    today = today or dt.datetime.now(dt.UTC).date()
    override = _active_override(pool, today)
    return override if isinstance(override, ActiveOverride) else None


def set_policy(
    pool: str,
    pool_class: PoolClass,
    *,
    until: dt.date | None = None,
    note: str | None = None,
) -> None:
    if until is None:
        raise ValueError("until(만료일)은 필수입니다")
    config = load_config()
    pools = config.setdefault("pools", {})
    entry: dict[str, object] = {"class": pool_class, "until": until.isoformat()}
    if note is not None:
        entry["note"] = note
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


def list_policies(
    known_pools: dict[str, PoolClass], today: dt.date | None = None
) -> list[tuple[str, PoolClass, str | None]]:
    """Return (pool, effective_class, status) for known pools plus unknown config entries."""
    today = today or dt.datetime.now(dt.UTC).date()
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
