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

import contextlib
import copy
import datetime as dt
import os
import pathlib
import re
import tomllib
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


class ConfigEditError(ValueError):
    """config.toml cannot be edited surgically — refuse, change nothing."""


# ---------------------------------------------------------------------------
# task #751 — minimal targeted TOML edits
#
# _write_config used to re-serialize the whole parsed dict: every comment,
# blank line, unknown key and unhandled section ([bench] kept only
# backend/cache_ttl_s) was destroyed on every write — the 2026-09-26 incident
# where `policy set` silently switched off quota sharing on four hosts.
# Writers now go through _edit_config: parse-check the file, splice only the
# target table/keys at line level, re-verify, then write atomically under an
# inter-process lock. Anything not confidently editable (parse errors, exotic
# shapes) raises ConfigEditError and the file is left byte-identical.
# tomlkit was considered and rejected: the project is intentionally
# zero-dependency (stdlib only), and the edit surface here is small enough to
# scan safely — tomllib validity is verified before any splice is applied.

_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_MISSING = object()
_DELETE = object()


def _skip_basic(s: str, pos: int) -> int | None:
    """pos at an opening ``"`` — return index just past the closing ``"``."""
    i = pos + 1
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    return None


_BASIC_ESCAPES = {'"', "\\", "b", "f", "n", "r", "t"}


def _unescape_basic(body: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        e = body[i + 1]
        if e in _BASIC_ESCAPES:
            out.append({"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}.get(e, e))
            i += 2
        elif e == "u":
            out.append(chr(int(body[i + 2 : i + 6], 16)))
            i += 6
        elif e == "U":
            out.append(chr(int(body[i + 2 : i + 10], 16)))
            i += 10
        else:
            raise ConfigEditError(f"unsupported escape \\{e} in key")
    return "".join(out)


def _parse_key(s: str, pos: int) -> tuple[list[str], int]:
    """Parse a dotted key starting at pos. Returns (segments, pos after key)."""
    parts: list[str] = []
    n = len(s)
    while True:
        while pos < n and s[pos] in " \t":
            pos += 1
        if pos >= n:
            raise ConfigEditError("key parse: unexpected end of line")
        c = s[pos]
        if c == '"':
            end = _skip_basic(s, pos)
            if end is None:
                raise ConfigEditError("key parse: unterminated quoted segment")
            parts.append(_unescape_basic(s[pos + 1 : end - 1]))
            pos = end
        elif c == "'":
            end = s.find("'", pos + 1)
            if end < 0:
                raise ConfigEditError("key parse: unterminated literal segment")
            parts.append(s[pos + 1 : end])
            pos = end + 1
        else:
            m = _BARE_KEY_RE.match(s, pos)
            if not m:
                raise ConfigEditError(f"key parse: bad segment at {pos}")
            parts.append(m.group(0))
            pos = m.end()
        while pos < n and s[pos] in " \t":
            pos += 1
        if pos < n and s[pos] == ".":
            pos += 1
            continue
        return parts, pos


def _find_ml_close(line: str, pos: int, delim: str) -> int | None:
    """Index of the closing delimiter of a multiline string, or None."""
    if delim == "'''":
        idx = line.find("'''", pos)
        return idx if idx >= 0 else None
    i = pos
    while True:
        i = line.find('"""', i)
        if i < 0:
            return None
        backslashes = 0
        j = i - 1
        while j >= 0 and line[j] == "\\":
            backslashes += 1
            j -= 1
        if backslashes % 2 == 0:
            return i
        i += 1


def _after_ml_close(line: str, idx: int, delim: str) -> int:
    """Position past the closing delim plus up to two extra same-char quotes
    (tomllib treats ``x'''''`` as content ``x''`` + close)."""
    pos = idx + 3
    q = delim[0]
    extra = 0
    while pos < len(line) and line[pos] == q and extra < 2:
        pos += 1
        extra += 1
    return pos


def _scan_value_rest(line: str, pos: int, st: dict) -> tuple[str, object]:
    """Scan value characters on ONE line starting at pos.

    ``st['depth']`` is the running bracket depth across lines.
    Returns ('done', col) when the statement's value ends at col on this line,
    ('ml', delim) when it enters a multiline string that does not close here,
    or ('cont',) when the line ends inside brackets.
    """
    n = len(line)
    ve = pos  # position just past the last non-whitespace value char
    while pos < n:
        c = line[pos]
        if c in " \t\r":
            pos += 1
            continue
        if c == "#":
            return ("done", ve) if st["depth"] == 0 else ("cont",)
        if c == '"':
            if line.startswith('"""', pos):
                end = _find_ml_close(line, pos + 3, '"""')
                if end is None:
                    return ("ml", '"""')
                pos = _after_ml_close(line, end, '"""')
                ve = pos
                continue
            end = _skip_basic(line, pos)
            if end is None:
                raise ConfigEditError("unterminated basic string in value")
            pos = end
            ve = pos
            continue
        if c == "'":
            if line.startswith("'''", pos):
                end = _find_ml_close(line, pos + 3, "'''")
                if end is None:
                    return ("ml", "'''")
                pos = _after_ml_close(line, end, "'''")
                ve = pos
                continue
            end = line.find("'", pos + 1)
            if end < 0:
                raise ConfigEditError("unterminated literal string in value")
            pos = end + 1
            ve = pos
            continue
        pos += 1
        if c in "[{":
            st["depth"] += 1
        elif c in "]}":
            st["depth"] -= 1
            if st["depth"] < 0:
                raise ConfigEditError("unbalanced brackets in value")
            if st["depth"] == 0:
                return ("done", pos)
        ve = pos
    if st["depth"] == 0:
        return ("done", ve)
    return ("cont",)


@dataclass
class _Region:
    path: tuple[str, ...]
    aot: bool
    start: int  # header line index
    end: int  # exclusive — next header or EOF


@dataclass
class _Stmt:
    path: tuple[str, ...]  # absolute path = enclosing region path + written key
    rel: tuple[str, ...]  # the key as written on the line
    region: int  # index into regions, -1 for top level
    start: int
    end: int  # exclusive
    eq: int  # column of '=' on the first line
    val_end: int  # column on the last line where the value ends


def _parse_header(s: str) -> tuple[tuple[str, ...], bool]:
    aot = s.startswith("[[")
    pos = 2 if aot else 1
    segs, pos = _parse_key(s, pos)
    close = "]]" if aot else "]"
    if not s.startswith(close, pos):
        raise ConfigEditError("table header: missing close bracket")
    rest = s[pos + len(close) :].strip()
    if rest and not rest.startswith("#"):
        raise ConfigEditError("table header: trailing content")
    return tuple(segs), aot


def _scan_doc(lines: list[str]) -> tuple[list[_Region], list[_Stmt]]:
    """Classify lines into table regions and key/value statements.

    Only called after tomllib has validated the text, so any inconsistency
    here is our own bug — we raise ConfigEditError rather than guess.
    """
    regions: list[_Region] = []
    stmts: list[_Stmt] = []
    mode = "free"  # free | value | ml
    delim = ""
    st = {"depth": 0}
    cur_path: tuple[str, ...] = ()
    cur_region = -1
    pending: tuple[tuple[str, ...], int, int] | None = None  # (rel, start, eq)

    def finish_stmt(i: int, val_end: int) -> None:
        nonlocal pending
        assert pending is not None
        rel, start, eq = pending
        stmts.append(_Stmt(cur_path + rel, rel, cur_region, start, i + 1, eq, val_end))
        pending = None

    for i, line in enumerate(lines):
        if mode == "ml":
            end = _find_ml_close(line, 0, delim)
            if end is None:
                continue
            pos = _after_ml_close(line, end, delim)
            if st["depth"]:
                mode = "value"
                r = _scan_value_rest(line, pos, st)
                if r[0] == "done":
                    finish_stmt(i, r[1])
                    mode = "free"
                elif r[0] == "ml":
                    delim = r[1]
                    mode = "ml"
            else:
                finish_stmt(i, pos)
                mode = "free"
            continue
        if mode == "value":
            r = _scan_value_rest(line, 0, st)
            if r[0] == "done":
                finish_stmt(i, r[1])
                mode = "free"
            elif r[0] == "ml":
                delim = r[1]
                mode = "ml"
            continue
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            if regions:
                regions[-1].end = i
            path, aot = _parse_header(stripped)
            regions.append(_Region(path, aot, i, len(lines)))
            cur_path = path
            cur_region = len(regions) - 1
            continue
        rel, eq = _parse_key(line, 0)
        if eq >= len(line) or line[eq] != "=":
            raise ConfigEditError("statement: expected '='")
        pending = (tuple(rel), i, eq)
        r = _scan_value_rest(line, eq + 1, st)
        if r[0] == "done":
            finish_stmt(i, r[1])
        elif r[0] == "ml":
            delim = r[1]
            mode = "ml"
        else:
            mode = "value"
    if pending is not None or mode != "free":
        raise ConfigEditError("unterminated value at end of file")
    return regions, stmts


def _key_seg_text(seg: str) -> str:
    return seg if _BARE_KEY_RE.fullmatch(seg) else _toml_string(seg)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    raise ConfigEditError(f"cannot serialize {value!r} as TOML")


def _get_path(tree: dict, path: tuple[str, ...]) -> object:
    cur: object = tree
    for seg in path:
        if not isinstance(cur, dict) or seg not in cur:
            return _MISSING
        cur = cur[seg]
    return cur


def _strip_path(tree: dict, path: tuple[str, ...]) -> None:
    """Remove path from a parsed dict, pruning emptied parent tables."""
    stack: list[tuple[dict, str]] = []
    cur: object = tree
    for seg in path[:-1]:
        if not isinstance(cur, dict) or seg not in cur:
            return
        stack.append((cur, seg))
        cur = cur[seg]
    if not isinstance(cur, dict):
        return
    cur.pop(path[-1], None)
    for parent, seg in reversed(stack):
        child = parent.get(seg)
        if isinstance(child, dict) and not child:
            parent.pop(seg, None)
        else:
            break


def _norm(value: object) -> object:
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


class _Doc:
    """Line-level view of config.toml — edits splice only the touched lines."""

    def __init__(self, text: str, config: dict) -> None:
        self.lines = text.split("\n")
        self.config = config
        self.touched: set[tuple[str, ...]] = set()
        # dominant line ending — new lines must carry the same terminator so
        # a CRLF file stays CRLF byte-for-byte (#751 B1).
        self.crlf = sum(1 for line in self.lines if line.endswith("\r")) * 2 > max(1, len(self.lines) - 1)
        self._rescan()

    def _eol(self, line: str) -> str:
        return line + "\r" if self.crlf else line

    def _rescan(self) -> None:
        self.regions, self.stmts = _scan_doc(self.lines)

    def text(self) -> str:
        return "\n".join(self.lines)

    def find_region(self, path: tuple[str, ...]) -> _Region | None:
        for region in self.regions:
            if region.path == path and not region.aot:
                return region
        return None

    def _find_stmt(self, path: tuple[str, ...]) -> _Stmt | None:
        for stmt in self.stmts:
            if stmt.path == path:
                return stmt
        return None

    def _check_not_aot(self, path: tuple[str, ...]) -> None:
        for region in self.regions:
            if region.aot and path[: len(region.path)] == region.path:
                raise ConfigEditError(f"{'.'.join(region.path)} is an array-of-tables — refusing")

    def set_value(self, path: tuple[str, ...], value: object, *, quote_table: bool = False) -> None:
        """Set leaf key ``path[-1]`` under table ``path[:-1]``.

        ``quote_table`` forces the last segment of a newly created table
        header to quoted form (``[profiles."x"]`` — the old writer always
        quoted profile names; pool names keep the bare spelling)."""
        self._check_not_aot(path)
        text_value = _toml_value(value)
        stmt = self._find_stmt(path)
        if stmt is not None:
            first = self.lines[stmt.start]
            new_line = first[: stmt.eq + 1] + " " + text_value + self.lines[stmt.end - 1][stmt.val_end :]
            self.lines[stmt.start : stmt.end] = [new_line]
            self._rescan()
            return
        parent, key = path[:-1], path[-1]
        region = self.find_region(parent)
        if region is not None:
            region_idx = self.regions.index(region)
            members = [s for s in self.stmts if s.region == region_idx]
            indent = ""
            for member in members:
                raw = self.lines[member.start]
                indent = raw[: len(raw) - len(raw.lstrip())]
                break
            idx = max((member.end for member in members), default=region.start + 1)
            self.lines[idx:idx] = [self._eol(f"{indent}{_key_seg_text(key)} = {text_value}")]
            self._rescan()
            return
        siblings = [s for s in self.stmts if s.path[:-1] == parent]
        if siblings:
            last = siblings[-1]
            dotted = ".".join(_key_seg_text(seg) for seg in last.rel[:-1] + (key,))
            self.lines[last.end : last.end] = [self._eol(f"{dotted} = {text_value}")]
            self._rescan()
            return
        if _get_path(self.config, parent) is not _MISSING:
            raise ConfigEditError(f"{'.'.join(parent)} exists but is not a table — refusing")
        # brand-new table: append after the last region under the same top key
        idx = len(self.lines)
        for other in reversed(self.regions):
            if other.path and other.path[0] == parent[0]:
                idx = other.end
                break
        segs = [_key_seg_text(s) for s in parent]
        if quote_table:
            segs[-1] = _toml_string(parent[-1])
        header = "[" + ".".join(segs) + "]"
        block = [header, f"{_key_seg_text(key)} = {text_value}"]
        before = [""] if idx > 0 and self.lines[idx - 1].strip() else []
        after = [""] if idx < len(self.lines) and self.lines[idx].strip() else []
        self.lines[idx:idx] = [self._eol(line) for line in before + block + after]
        self._rescan()

    def remove_key(self, path: tuple[str, ...]) -> bool:
        self._check_not_aot(path)
        stmt = self._find_stmt(path)
        if stmt is None:
            return False
        del self.lines[stmt.start : stmt.end]
        self._rescan()
        return True

    def delete_table(self, path: tuple[str, ...]) -> bool:
        """Remove table region(s) under path plus every dotted statement."""
        self._check_not_aot(path)
        found = False
        while True:
            region = next(
                (r for r in self.regions if r.path[: len(path)] == path),
                None,
            )
            if region is None:
                break
            del self.lines[region.start : region.end]
            self._rescan()
            found = True
        while True:
            victim = next((s for s in self.stmts if s.path[: len(path)] == path), None)
            if victim is None:
                break
            del self.lines[victim.start : victim.end]
            self._rescan()
            found = True
        return found


def _locked(path: pathlib.Path):
    """Serialize config writes across processes (fcntl.flock on a sidecar)."""

    @contextlib.contextmanager
    def _ctx():
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        try:
            import fcntl
        except ImportError:  # pragma: no cover — non-POSIX fallback
            fcntl = None  # type: ignore[assignment]
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    return _ctx()


def _atomic_write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(text.encode("utf-8"))
    if text:
        tmp.chmod(0o600)
    os.replace(tmp, path)


def _edit_config(edit_fn):
    """Lock, read, parse-check, apply line-level edits, verify, write atomically.

    ``edit_fn(doc) -> (result, expect)`` where ``expect`` maps an absolute key
    path to the subtree it must parse to after the write (``None`` = absent).
    Raises ConfigEditError and changes nothing when the file cannot be edited
    safely or the edit would produce invalid/divergent TOML.
    """
    path = config_path()
    if path.is_symlink():
        path = path.resolve()
    with _locked(path):
        try:
            text = path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            text = ""
        if text:
            try:
                config0 = tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigEditError(f"{path}: TOML parse error — refusing to rewrite ({exc})") from exc
        else:
            config0 = {}
        doc = _Doc(text, config0)
        result, expect = edit_fn(doc)

        def check_expect(tree: dict, label: str) -> None:
            for want_path, want in expect.items():
                got = _get_path(tree, want_path)
                if want is None:
                    if got is not _MISSING:
                        raise ConfigEditError(f"{label}: {'.'.join(want_path)} still present")
                elif _norm(got if got is not _MISSING else None) != _norm(want):
                    raise ConfigEditError(f"{label}: {'.'.join(want_path)} did not land as intended")

        new_text = doc.text()
        if new_text == text:
            # No lines changed — legitimate only when the expectation already
            # holds in the file. Otherwise the target sits inside a shape the
            # editor cannot splice (e.g. an inline table) and returning success
            # would silently lie about the write (#751 B2).
            check_expect(config0, "refusing no-op edit")
            return result
        try:
            config1 = tomllib.loads(new_text)
        except tomllib.TOMLDecodeError as exc:  # pragma: no cover — our own bug
            raise ConfigEditError(f"internal: edit produced invalid TOML ({exc})") from exc
        before, after = copy.deepcopy(config0), copy.deepcopy(config1)
        for touched in doc.touched:
            _strip_path(before, touched)
            _strip_path(after, touched)
        if _norm(before) != _norm(after):
            raise ConfigEditError("internal: edit modified non-target content")
        check_expect(config1, "internal")
        _atomic_write(path, new_text)
        return result


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
    subscribed: bool | None | Literal["__unset__"] = "__unset__",
) -> None:
    """Set pool class and/or numeric boost and/or the subscribed flag.

    ``pool_class`` may be None when the call only touches boost (``policy set
    <pool> --boost N``/``--boost none`` without a class positional). ``boost``
    left at the sentinel default leaves any existing boost untouched; pass an
    explicit ``int`` to set it (requires ``until``, shared with the pool-level
    class expiry — there is no separate boost-until field) or ``None`` to
    clear it. ``subscribed`` follows the same rule: the sentinel leaves the
    key alone, ``True``/``False`` writes it, ``None`` removes just that key —
    merged into the same locked edit so a combined ``policy set`` applies as
    one atomic transaction (#751 B3). ``plan``/``price_usd``/
    ``capacity_weight`` are read-only from this module's perspective — they
    are config.toml-only fields with no CLI setter (operator-edited).
    """
    if pool_class is not None and until is None:
        raise ValueError("until(만료일)은 필수입니다")
    if boost is not None and boost != "__unset__" and until is None:
        raise ValueError("boost 설정에는 --until(만료일)이 필요합니다")

    ops: dict[str, object] = {}
    if pool_class is not None:
        ops["class"] = pool_class
        ops["until"] = until
        if note is not None:
            ops["note"] = note
    if boost != "__unset__":
        if boost is None:
            ops["boost"] = _DELETE
        else:
            ops["boost"] = boost
            ops["until"] = until
    if subscribed != "__unset__":
        ops["subscribed"] = _DELETE if subscribed is None else subscribed
    if not ops:
        return
    base = ("pools", pool)

    def edit(doc: _Doc):
        for key, value in ops.items():
            if value is _DELETE:
                doc.remove_key(base + (key,))
            else:
                doc.set_value(base + (key,), value)
        # semantic expectation: same result the old dict-level write produced
        entry: dict[str, object] = dict((doc.config.get("pools") or {}).get(pool) or {})
        for key, value in ops.items():
            if value is _DELETE:
                entry.pop(key, None)
            else:
                entry[key] = value
        if not entry:
            # an entry left empty by the removal is dropped entirely — the
            # same rule _set_subscribed_impl applies.
            doc.delete_table(base)
        doc.touched.add(base)
        return None, {base: entry or None}

    _edit_config(edit)


def clear_policy(pool: str) -> bool:
    base = ("pools", pool)

    def edit(doc: _Doc):
        entry = (doc.config.get("pools") or {}).get(pool)
        if not isinstance(entry, dict):
            return False, {}
        if not doc.delete_table(base):
            raise ConfigEditError(
                f"pools.{pool}: 인라인 형태의 설정은 안전하게 지울 수 없어 변경하지 않았습니다"
            )
        doc.touched.add(base)
        return True, {base: None}

    return _edit_config(edit)


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
    _set_subscribed_impl("pools", pool, value)


def _set_subscribed_impl(root: str, name: str, value: bool | None) -> None:
    base = (root, name)

    def edit(doc: _Doc):
        if value is None:
            doc.remove_key(base + ("subscribed",))
        else:
            doc.set_value(base + ("subscribed",), value, quote_table=(root == "profiles"))
        table = doc.config.get(root) or {}
        entry: dict[str, object] = dict(table.get(name) or {})
        if value is None:
            entry.pop("subscribed", None)
        else:
            entry["subscribed"] = value
        if not entry:
            # an entry left empty by the removal is dropped entirely — same
            # rule the old dict-level writer applied.
            doc.delete_table(base)
        doc.touched.add(base)
        return None, {base: entry or None}

    _edit_config(edit)


def set_profile_subscribed(profile: str, value: bool | None) -> None:
    """Write ``[profiles.<profile>] subscribed``. ``None`` removes just that key
    (the profile falls back to the pool level). An explicit ``true`` overrides
    an unsubscribed pool; an explicit ``false`` overrides a subscribed pool —
    the two are not interchangeable with removal."""
    _set_subscribed_impl("profiles", profile, value)


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
