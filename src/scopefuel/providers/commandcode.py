"""Command Code usage probe through the interactive CLI's /usage screen."""

from __future__ import annotations

import contextlib
import datetime as dt
import errno
import fcntl
import os
import pathlib
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import termios
import time

from .. import cache, proctrack
from ..model import PROBE_IN_PROGRESS, Bucket, ProviderResult, Scope

BINARY = os.environ.get("SCOPEFUEL_COMMANDCODE_BIN") or "cmd"
_DEFAULT_BINARY_NAME = "cmd"
# hk 2182 (a): cmd is not on scopefuel's PATH on every host — the desk install
# lives under ~/.hermes/node/bin. Resolution order: an explicit configured
# path (SCOPEFUEL_COMMANDCODE_BIN or [pools.commandcode] bin in config.toml),
# then PATH, then these known install dirs. Never a login shell.
KNOWN_BIN_DIRS = (
    "~/.hermes/node/bin",
    "~/.local/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)
SOURCE = "cli:/usage"
# hk 2160 r2: resolved per probe, not at import — importing this module with
# HOME unset must not raise, and a test/host may relocate HOME after import.
PROBE_WORKDIR: pathlib.Path | None = None
# hk 2182 (c): cmd's TUI never acts on an Enter written in the same os.write
# as the command text (m1b fixture hk:doc fixtures/hk2182/m1b-usage-screen —
# "/usage" sat in the input box for the full 30s). Write the text, wait for
# its echo, then submit Enter separately.
PROBE_INPUT = "/usage"
PROBE_SUBMIT = "\r"
# How long to wait for the "/usage" echo before submitting anyway — a TUI in
# raw mode may not echo at all, and submission must still happen.
ECHO_WAIT_S = 0.5
TIMEOUT_S = 30.0
TIMEOUT_MIN_S = 5.0
TIMEOUT_MAX_S = 60.0
STARTUP_DELAY_S = 0.4
READY_SETTLE_S = 0.5
PTY_ROWS = 50
PTY_COLS = 200
OUTER_TIMER_MARGIN_S = 8.0

_ANSI = re.compile(r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*(?:\x07|\x1B\\)|\([0-2])")
_PROMPT = re.compile(r"❯|›|Shift\+Tab")
_TRUST_QUESTION = re.compile(r"Do you trust the contents of this directory\?", re.I)
_LIMIT_TEXT = re.compile(
    r"\b(?:usage|rate|request)\s+limit(?:\s+(?:reached|exceeded))?\b|"
    r"\blimit\s+(?:reached|exceeded|exhausted)\b|\bquota\s+(?:exceeded|exhausted)\b",
    re.I,
)
_LIMIT_WINDOW = {
    "30d": re.compile(r"\b(?:monthly|this month|month)\b", re.I),
    "5h": re.compile(r"\b(?:5[- ]?hour|five[- ]?hour|5h)\b", re.I),
    "7d": re.compile(r"\b(?:weekly|week)\b", re.I),
}
_FIVE_HOUR_LABEL = re.compile(r"\b5[- ]?hour\b", re.I)
_WEEKLY_LABEL = re.compile(r"\bweekly\b", re.I)
_DAYS_TO_RENEW = re.compile(r"(?P<days>\d+(?:\.\d+)?)\s*days?\s+to\s+renew", re.I)
# Strict row labels for segment bounds — bare "5h"/"week" spellings would
# truncate a row at the next row's "resets in 5h"/"resets in 1 week".
_BOUNDARY_LABEL = {
    "30d": re.compile(r"\b(?:monthly|this month|month)\b", re.I),
    "5h": _FIVE_HOUR_LABEL,
    "7d": _WEEKLY_LABEL,
}
# The end of a completed window row (its reset phrase) — the left bound for
# the monthly row's value when the monthly row is not first on screen.
_RESET_BOUNDARY = re.compile(
    r"\bresets?\s+in\s+[^|/·\r\n]+|\d+(?:\.\d+)?\s*days?\s+to\s+renew",
    re.I,
)
# A window's own value (percent or limit wording) is searched only inside its
# own segment — up to the nearest following window label of either kind — and
# before that window's own reset marker, so a row missing its percentage
# fails closed instead of borrowing a later number.
_VALUE = re.compile(
    r"(?:(?P<used>\d+(?:\.\d+)?)\s*%|(?P<limit>(?:(?:usage|rate|request)\s+)?limit\b))",
    re.I,
)
_RESET_IN = re.compile(r"\bresets?\s+in\s+(?P<duration>[^|/·\r\n]+)", re.I)
_DURATION = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.I,
)


class ProbeTimeout(subprocess.TimeoutExpired):
    def __init__(self, cmd, timeout, *, stage: str, nbytes: int, detail: str | None = None) -> None:
        super().__init__(cmd, timeout)
        self.stage = stage
        self.nbytes = nbytes
        self.detail = detail


def _timeout_s() -> float:
    raw = os.environ.get("SCOPEFUEL_COMMANDCODE_TIMEOUT_S")
    try:
        configured = TIMEOUT_S if raw is None else float(raw)
    except ValueError:
        configured = TIMEOUT_S
    if not TIMEOUT_MIN_S <= configured <= TIMEOUT_MAX_S:
        configured = TIMEOUT_S
    delay, _interval = signal.getitimer(signal.ITIMER_REAL)
    remaining = delay - OUTER_TIMER_MARGIN_S if delay > 0 else float("inf")
    return max(min(configured, remaining), 0.0)


def _probe_workdir() -> pathlib.Path:
    if PROBE_WORKDIR is not None:
        return PROBE_WORKDIR
    return pathlib.Path.home() / ".local" / "share" / "scopefuel" / "commandcode-probe-workdir"


def _probe_lock_dir() -> pathlib.Path:
    return cache.cache_dir() / "probe-locks" / "commandcode"


def _explicit_binary() -> str | None:
    """The operator-declared cmd path, if any — env/config before any search."""

    if BINARY != _DEFAULT_BINARY_NAME:
        return BINARY
    try:
        from ..policy import load_config

        entry = (load_config().get("pools") or {}).get("commandcode")
        if isinstance(entry, dict):
            raw = entry.get("bin")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    except Exception:  # noqa: BLE001 - a broken config must not break the probe
        pass
    return None


def _resolve_binary() -> tuple[str | None, list[str]]:
    """Resolve the cmd binary; returns (path or None, every location tried).

    Order: explicit configured path → PATH → KNOWN_BIN_DIRS. An explicit path
    that does not exist is recorded and the search continues — a stale config
    must fail loud in the error's tried list, not silently measure nothing.
    """

    tried: list[str] = []
    if explicit := _explicit_binary():
        candidate = pathlib.Path(os.path.expanduser(explicit))
        tried.append(str(candidate))
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate), tried
    tried.append(f"PATH:{_DEFAULT_BINARY_NAME}")
    if found := shutil.which(_DEFAULT_BINARY_NAME):
        return found, tried
    for install_dir in KNOWN_BIN_DIRS:
        candidate = pathlib.Path(install_dir).expanduser() / _DEFAULT_BINARY_NAME
        tried.append(str(candidate))
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate), tried
    return None, tried


def _binary_missing_result(tried: list[str]) -> ProviderResult:
    return ProviderResult(
        id="commandcode",
        error=f"cmd 실행 파일 없음 — 찾아본 경로: {', '.join(tried)}",
        error_kind="transport",
        source=SOURCE,
        pool_class="spend",
    )


def _ensure_workdir(workdir: pathlib.Path) -> None:
    try:
        if not (workdir / ".git").exists():
            subprocess.run(
                ["git", "init", "-q"],
                cwd=workdir,
                timeout=10.0,
                capture_output=True,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _probe_once(binary: str) -> str:
    workdir = _probe_workdir().expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    _ensure_workdir(workdir)
    proctrack.kill_stale_probe_leftovers(workdir)
    instance_dir, owner_fd = proctrack.new_probe_dir(workdir)
    timeout_s = _timeout_s()
    master_fd = slave_fd = -1
    process: subprocess.Popen[bytes] | None = None
    reaper: subprocess.Popen[bytes] | None = None
    child_pgid: int | None = None
    try:
        if timeout_s <= 0:
            raise ProbeTimeout([binary, "--no-session"], timeout_s, stage="no-output", nbytes=0)
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
        child_env = os.environ.copy()
        child_env.setdefault("TERM", "xterm-256color")
        process = subprocess.Popen(
            [binary, "--no-session"],
            cwd=instance_dir,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
            env=child_env,
        )
        with contextlib.suppress(OSError):
            child_pgid = os.getpgid(process.pid)
        if child_pgid is not None:
            proctrack.register(child_pgid, instance_dir)
        reaper = proctrack.spawn_reaper(instance_dir, ttl_s=timeout_s + 90.0)
        os.close(slave_fd)
        slave_fd = -1
        time.sleep(STARTUP_DELAY_S)

        output = bytearray()
        deadline = time.monotonic() + timeout_s
        sent = False
        sent_at = 0.0
        sent_mark = 0
        submitted = False
        panel_seen_at: float | None = None
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master_fd, 8192)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        break
                    raise
                if not chunk:
                    break
                output.extend(chunk)
                screen = _clean(output.decode("utf-8", errors="replace"))
                # The trust dialog's selection marker matches _PROMPT, so the
                # question must be checked first: PROBE_INPUT is never written
                # while the dialog is on screen (each probe runs in a fresh
                # instance dir, so any probe could hit it).
                if _TRUST_QUESTION.search(screen):
                    raise ProbeTimeout(
                        [binary, "--no-session"], timeout_s, stage="trust-prompt", nbytes=len(output)
                    )
                if not sent and _PROMPT.search(screen):
                    os.write(master_fd, PROBE_INPUT.encode())
                    sent = True
                    sent_at = time.monotonic()
                    sent_mark = len(output)
                if sent and not submitted:
                    # Submit only once the typed text has echoed (or a slash
                    # menu may render it as a selectable row); ECHO_WAIT_S is
                    # the fallback for a TUI that never echoes.
                    tail = _clean(output[sent_mark:].decode("utf-8", errors="replace"))
                    if "/usage" in tail or time.monotonic() - sent_at >= ECHO_WAIT_S:
                        os.write(master_fd, PROBE_SUBMIT.encode())
                        submitted = True
                if submitted:
                    try:
                        _parse_buckets(screen, now=dt.datetime.now(dt.UTC))
                    except ValueError:
                        pass
                    else:
                        panel_seen_at = panel_seen_at or time.monotonic()
                        if time.monotonic() - panel_seen_at >= READY_SETTLE_S:
                            break
                continue
            if process.poll() is not None:
                break
            if panel_seen_at is not None and time.monotonic() - panel_seen_at >= READY_SETTLE_S:
                break
            if sent and not submitted and time.monotonic() - sent_at >= ECHO_WAIT_S:
                os.write(master_fd, PROBE_SUBMIT.encode())
                submitted = True

        screen = _clean(output.decode("utf-8", errors="replace"))
        if time.monotonic() >= deadline and not (submitted and _complete(screen)):
            raise ProbeTimeout(
                [binary, "--no-session"],
                timeout_s,
                stage="unparsed" if sent else "no-prompt",
                nbytes=len(output),
                # hk 2182 (c): an unparsed screen fails closed naming exactly
                # which part of the /usage panel was missing.
                detail=_missing_detail(screen) if sent else None,
            )
        return screen
    finally:
        if process is not None and process.poll() is None:
            process_group = None
            with contextlib.suppress(OSError):
                process_group = os.getpgid(process.pid)
            try:
                if process_group is not None:
                    os.killpg(process_group, signal.SIGTERM)
                process.wait(timeout=2.0)
            except (subprocess.TimeoutExpired, OSError):
                if process_group is None:
                    with contextlib.suppress(OSError):
                        process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    process.wait(timeout=2.0)
            if process_group is not None:
                with contextlib.suppress(OSError):
                    os.killpg(process_group, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    process.wait(timeout=2.0)
                _wait_group_gone(process_group)
        with contextlib.suppress(OSError):
            proctrack.kill_leftovers_at_cwd(instance_dir, nested=True)
        if child_pgid is not None:
            proctrack.unregister(child_pgid)
        if reaper is not None and reaper.poll() is None:
            with contextlib.suppress(OSError):
                reaper.kill()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                reaper.wait(timeout=2.0)
        for fd in (master_fd, slave_fd, owner_fd):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        with contextlib.suppress(OSError):
            shutil.rmtree(instance_dir)


def _wait_group_gone(pgid: int) -> None:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except OSError:
            return
        time.sleep(0.05)


def _clean(text: str) -> str:
    return _ANSI.sub("", text).replace("\r", " ").replace("\x00", " ")


def _duration_seconds(value: str) -> float:
    matches = list(_DURATION.finditer(value))
    if not matches:
        raise ValueError("reset duration is missing")
    total = 0.0
    end = 0
    for match in matches:
        if value[end : match.start()].strip(" ,"):
            raise ValueError("reset duration has unsupported text")
        amount = float(match["value"])
        unit = match["unit"].casefold()
        if unit.startswith("d"):
            multiplier = 86400
        elif unit.startswith("h"):
            multiplier = 3600
        elif unit.startswith("m"):
            multiplier = 60
        else:
            multiplier = 1
        total += amount * multiplier
        end = match.end()
    if value[end:].strip(" ,") or not total > 0:
        raise ValueError("reset duration is invalid")
    return total


def _reset_for_duration(value: str, *, now: dt.datetime) -> str:
    seconds = _duration_seconds(value)
    return (now.astimezone(dt.UTC) + dt.timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _percent(value: str) -> float:
    parsed = float(value)
    if not 0 <= parsed <= 100:
        raise ValueError("usage percentage is outside 0..100")
    return parsed


def _complete(text: str) -> bool:
    try:
        _parse_buckets(text, now=dt.datetime.now(dt.UTC))
        return True
    except ValueError:
        return False


def _missing_detail(text: str) -> str | None:
    """Why a screen fails the three-window parse — the fail-closed reason."""

    try:
        _parse_buckets(text, now=dt.datetime.now(dt.UTC))
    except ValueError as exc:
        return str(exc)
    return None


def _segment_end(clean: str, start: int, own: str) -> int:
    """The next window-label boundary after ``start`` — order independent."""

    ends = [
        hit.start()
        for window, pattern in _BOUNDARY_LABEL.items()
        if window != own
        for hit in pattern.finditer(clean)
        if hit.start() > start
    ]
    return min(ends, default=len(clean))


def _short_window(clean: str, label: re.Match[str], own: str) -> tuple[re.Match[str], re.Match[str]]:
    """A short window's reset and its own value within its bounded segment.

    The segment ends at the nearest following window label of either kind;
    the value must come before that window's own "resets in" marker.
    """

    segment = clean[label.start() : _segment_end(clean, label.start(), own)]
    reset = _RESET_IN.search(segment)
    if reset is None:
        raise ValueError("a short-window reset is missing")
    value = _VALUE.search(segment, 0, reset.start())
    if value is None:
        raise ValueError("a usage window has no percentage of its own")
    return reset, value


def _parse_buckets(text: str, *, now: dt.datetime) -> list[Bucket]:
    clean = _clean(text)
    five_hour = _FIVE_HOUR_LABEL.search(clean)
    weekly = _WEEKLY_LABEL.search(clean)
    renew = _DAYS_TO_RENEW.search(clean)
    if five_hour is None or weekly is None or renew is None:
        raise ValueError("one or more usage windows are missing")

    # The monthly row's value must precede its own "days to renew", and its
    # left bound is the end of the previous completed row (its reset phrase)
    # — a monthly row that is not first cannot borrow an earlier number.
    monthly_left = max(
        (hit.end() for hit in _RESET_BOUNDARY.finditer(clean) if hit.end() <= renew.start()),
        default=0,
    )
    monthly_value = _VALUE.search(clean, monthly_left, renew.start())
    if monthly_value is None:
        raise ValueError("the monthly usage window has no percentage of its own")
    monthly_reset = _reset_for_duration(f"{renew['days']} days", now=now)

    five_reset_match, five_value = _short_window(clean, five_hour, "5h")
    weekly_reset_match, weekly_value = _short_window(clean, weekly, "7d")
    five_reset = _reset_for_duration(five_reset_match["duration"], now=now)
    weekly_reset = _reset_for_duration(weekly_reset_match["duration"], now=now)

    values = {
        "30d": _percent(monthly_value["used"] or "100"),
        "5h": _percent(five_value["used"] or "100"),
        "7d": _percent(weekly_value["used"] or "100"),
    }
    resets = {"30d": monthly_reset, "5h": five_reset, "7d": weekly_reset}
    if _LIMIT_TEXT.search(clean):
        limit = _LIMIT_TEXT.search(clean)
        assert limit is not None
        distances = {}
        for window, pattern in _LIMIT_WINDOW.items():
            hits = list(pattern.finditer(clean))
            if hits:
                distances[window] = min(
                    0
                    if hit.start() <= limit.start() < hit.end()
                    else min(abs(hit.start() - limit.start()), abs(hit.end() - limit.start()))
                    for hit in hits
                )
        nearest = min(distances.values(), default=None)
        matches = [window for window, distance in distances.items() if distance == nearest]
        if len(matches) != 1:
            raise ValueError("limit wording does not identify a usage window")
        values[matches[0]] = 100.0

    metadata = (
        ("monthly", "30d", "month"),
        ("5-hour", "5h", "now"),
        ("weekly", "7d", "week"),
    )
    return [
        Bucket(
            label=label,
            window=window,
            used_pct=values[window],
            resets_at=resets[window],
            scope=Scope("account"),
            horizon=horizon,
        )
        for label, window, horizon in metadata
    ]


def parse(text: str, *, now: dt.datetime | None = None) -> ProviderResult:
    current = now or dt.datetime.now(dt.UTC)
    try:
        buckets = _parse_buckets(text, now=current)
    except (TypeError, ValueError) as exc:
        return ProviderResult(
            id="commandcode",
            error=f"cmd /usage 화면을 완전히 파싱하지 못함: {exc}",
            error_kind="parse_error",
            source=SOURCE,
        )
    return ProviderResult(
        id="commandcode",
        buckets=buckets,
        note="PTY /usage monthly, 5-hour, and weekly windows",
        source=SOURCE,
        pool_class="spend",
        raw={"source": SOURCE, "screen_parsed": True},
    )


def fetch() -> ProviderResult:
    """The synchronous PTY probe — run only by a caller that owns the budget.

    collect() never reaches this path: ``fetch.detached_probe`` below is what
    gate/recommend/--json/status call, so a caller that can be killed mid-run
    (the panewire daemon's ~15s polls) never starts or holds the probe. The
    detached ``scopefuel refresh commandcode --_worker`` path invokes this
    directly with its own 60s budget.
    """

    binary, tried = _resolve_binary()
    if binary is None:
        return _binary_missing_result(tried)
    try:
        with proctrack.single_probe_lock(_probe_lock_dir()) as acquired:
            if not acquired:
                return ProviderResult(
                    id="commandcode",
                    error="cmd /usage 탐침이 이미 실행 중",
                    error_kind=PROBE_IN_PROGRESS,
                    source=SOURCE,
                    pool_class="spend",
                )
            screen = _probe_once(binary)
    except ProbeTimeout as exc:
        error = f"cmd /usage PTY probe timeout (stage={exc.stage}, bytes={exc.nbytes})"
        if exc.detail:
            error += f" — missing: {exc.detail}"
        raw = {"source": SOURCE, "stage": exc.stage, "bytes": exc.nbytes}
        if exc.detail:
            raw["missing"] = exc.detail
        return ProviderResult(
            id="commandcode",
            error=error,
            error_kind="probe_timeout",
            source=SOURCE,
            pool_class="spend",
            raw=raw,
        )
    except OSError as exc:
        return ProviderResult(
            id="commandcode",
            error=f"cmd /usage PTY probe failed: {exc.__class__.__name__}",
            error_kind="transport",
            source=SOURCE,
            pool_class="spend",
        )
    result = parse(screen)
    result.raw = {"source": SOURCE, "screen_parsed": result.error is None, "screen_bytes": len(screen)}
    return result


def _spawn_background_refresh() -> None:
    """Detach ``scopefuel refresh commandcode --_worker`` — owns its own budget.

    Best-effort: a spawn failure degrades to the same cached/manual serving
    this round already produced; the next short caller tries again.
    """

    try:
        from .. import refresh

        refresh.spawn_detached("commandcode")
    except Exception:  # noqa: BLE001 - never let a refresh spawn break a caller
        pass


def _deferred_fetch() -> ProviderResult:
    """hk 2182 (b): the short-budget path — never starts or starves the probe.

    A daemon-poll caller is killed long before the ~30s PTY probe could land,
    so an inline probe only ever produces corpses: nothing is cached and the
    lock is almost always held by the next poll. Instead this serves what is
    already known — collect() turns the PROBE_IN_PROGRESS marker into the
    fresh/stale snapshot, and the manual credit fallback covers a bare miss —
    and, when no live probe owns the lock, starts at most ONE detached
    background refresh which writes the cache when it finishes.
    """

    if proctrack.probe_in_flight(_probe_lock_dir()):
        return ProviderResult(
            id="commandcode",
            error="cmd /usage 탐침이 이미 실행 중 — 이번 회차 건너뜀",
            error_kind=PROBE_IN_PROGRESS,
            source=SOURCE,
            pool_class="spend",
        )
    binary, tried = _resolve_binary()
    if binary is None:
        return _binary_missing_result(tried)
    _spawn_background_refresh()
    return ProviderResult(
        id="commandcode",
        error="cmd /usage 탐침을 백그라운드에서 시작 — 이번 회차는 직전 값 사용",
        error_kind=PROBE_IN_PROGRESS,
        source=SOURCE,
        pool_class="spend",
        raw={"source": SOURCE, "background_refresh": True},
    )


fetch.pool_class = "spend"
# collect() dispatches short-budget callers here instead of fetch(); the
# synchronous probe stays reachable only through ``scopefuel refresh
# commandcode`` (foreground or the detached --_worker it spawns).
fetch.detached_probe = _deferred_fetch
