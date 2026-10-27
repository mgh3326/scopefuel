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
SOURCE = "cli:/usage"
PROBE_WORKDIR = pathlib.Path.home() / ".local" / "share" / "scopefuel" / "commandcode-probe-workdir"
PROBE_INPUT = "/usage\r"
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
    r"\blimit\s+(?:reached|exceeded|exhausted)\b|\bquota\s+exhausted\b",
    re.I,
)
_LIMIT_WINDOW = {
    "30d": re.compile(r"\b(?:monthly|this month|month)\b", re.I),
    "5h": re.compile(r"\b(?:5[- ]?hour|five[- ]?hour|5h)\b", re.I),
    "7d": re.compile(r"\b(?:weekly|week)\b", re.I),
}
_MONTHLY = re.compile(
    r"(?:(?P<used>\d+(?:\.\d+)?)\s*%\s*used\b|"
    r"(?P<limit>usage|rate|request)\s+limit[\s\S]{0,120}?)"
    r"[\s\S]{0,180}?"
    r"(?P<days>\d+(?:\.\d+)?)\s*days?\s+to\s+renew",
    re.I,
)
_FIVE_HOUR = re.compile(
    r"\b5[- ]?hour\b[\s\S]{0,100}?(?:(?P<used>\d+(?:\.\d+)?)\s*%|(?P<limit>usage|rate|request)\s+limit)",
    re.I,
)
_WEEKLY = re.compile(
    r"\bweekly\b[\s\S]{0,100}?(?:(?P<used>\d+(?:\.\d+)?)\s*%|(?P<limit>usage|rate|request)\s+limit)",
    re.I,
)
_RESET_IN = re.compile(r"\bresets?\s+in\s+(?P<duration>[^|/·\r\n]+)", re.I)
_DURATION = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.I,
)


class ProbeTimeout(subprocess.TimeoutExpired):
    def __init__(self, cmd, timeout, *, stage: str, nbytes: int) -> None:
        super().__init__(cmd, timeout)
        self.stage = stage
        self.nbytes = nbytes


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


def _probe_lock_dir() -> pathlib.Path:
    return cache.cache_dir() / "probe-locks" / "commandcode"


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


def _probe_once() -> str:
    workdir = pathlib.Path(PROBE_WORKDIR).expanduser()
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
            raise ProbeTimeout([BINARY, "--no-session"], timeout_s, stage="no-output", nbytes=0)
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
        child_env = os.environ.copy()
        child_env.setdefault("TERM", "xterm-256color")
        process = subprocess.Popen(
            [BINARY, "--no-session"],
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
                if not sent and _PROMPT.search(screen):
                    os.write(master_fd, PROBE_INPUT.encode())
                    sent = True
                elif not sent and _TRUST_QUESTION.search(screen):
                    raise ProbeTimeout(
                        [BINARY, "--no-session"], timeout_s, stage="trust-prompt", nbytes=len(output)
                    )
                if sent:
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

        screen = _clean(output.decode("utf-8", errors="replace"))
        if time.monotonic() >= deadline and not (sent and _complete(screen)):
            raise ProbeTimeout(
                [BINARY, "--no-session"],
                timeout_s,
                stage="unparsed" if sent else "no-prompt",
                nbytes=len(output),
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


def _segment_after(text: str, start: int, next_window: str | None = None) -> str:
    end = len(text)
    if next_window is not None:
        match = _LIMIT_WINDOW[next_window].search(text, start + 1)
        if match is not None:
            end = match.start()
    return text[start:end]


def _complete(text: str) -> bool:
    try:
        _parse_buckets(text, now=dt.datetime.now(dt.UTC))
        return True
    except ValueError:
        return False


def _parse_buckets(text: str, *, now: dt.datetime) -> list[Bucket]:
    clean = _clean(text)
    monthly = _MONTHLY.search(clean)
    five_hour = _FIVE_HOUR.search(clean)
    weekly = _WEEKLY.search(clean)
    if monthly is None or five_hour is None or weekly is None:
        raise ValueError("one or more usage windows are missing")

    monthly_reset = _reset_for_duration(f"{monthly['days']} days", now=now)
    five_segment = _segment_after(clean, five_hour.start(), "7d")
    weekly_segment = _segment_after(clean, weekly.start())
    five_reset_match = _RESET_IN.search(five_segment)
    weekly_reset_match = _RESET_IN.search(weekly_segment)
    if five_reset_match is None or weekly_reset_match is None:
        raise ValueError("a short-window reset is missing")
    five_reset = _reset_for_duration(five_reset_match["duration"], now=now)
    weekly_reset = _reset_for_duration(weekly_reset_match["duration"], now=now)

    values = {
        "30d": _percent(monthly["used"] or "100"),
        "5h": _percent(five_hour["used"] or "100"),
        "7d": _percent(weekly["used"] or "100"),
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
    if shutil.which(BINARY) is None:
        return ProviderResult(
            id="commandcode",
            error="cmd 실행 파일 없음",
            error_kind="transport",
            source=SOURCE,
            pool_class="spend",
        )
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
            screen = _probe_once()
    except ProbeTimeout as exc:
        return ProviderResult(
            id="commandcode",
            error=f"cmd /usage PTY probe timeout (stage={exc.stage}, bytes={exc.nbytes})",
            error_kind="probe_timeout",
            source=SOURCE,
            pool_class="spend",
            raw={"source": SOURCE, "stage": exc.stage, "bytes": exc.nbytes},
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


fetch.pool_class = "spend"
