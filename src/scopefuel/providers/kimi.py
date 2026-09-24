"""Kimi Code CLI usage probe.

Kimi's supported quota surface is the interactive ``kimi`` CLI's ``/usage``
command.  It renders usage only when attached to a terminal, so this provider
uses a short-lived POSIX pseudo-terminal and never reads or updates Kimi's
credential/config files.  The CLI owns authentication and any upstream HTTP
details; scopefuel only parses the rendered quota summary.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import errno
import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path

from .. import proctrack
from ..model import Bucket, ProviderResult, Scope

BINARY = os.environ.get("SCOPEFUEL_KIMI_BIN") or "kimi"
TIMEOUT_S = 30.0
STARTUP_DELAY_S = 0.4
READY_SETTLE_S = 0.5
IDLE_TIMEOUT_S = 8.0
USAGE_SETTLE_S = 0.5
PROBE_INPUT = "/usage\r"
PROBE_WORKDIR = Path.home() / ".local" / "share" / "scopefuel" / "kimi-probe-workdir"
TRUST_MARKER = "Trust this folder?"
_READY_MARKERS = ("│ >", "Kimi K3 thinking", TRUST_MARKER)
# A/B 실측(grok): 크기 미설정 PTY에서는 TUI가 usage 패널을 렌더하지 않아 timeout한다.
# kimi 도 동일 PTY 경로이므로 같은 크기를 선제 적용한다.
PTY_ROWS = 50
PTY_COLS = 200

_ANSI = re.compile(r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*(?:\x07|\x1B\\)|\([0-2])")
_PERCENT_LEFT = re.compile(r"(?P<remaining>\d+(?:\.\d+)?)\s*%\s+left", re.IGNORECASE)
_PERCENT_USED = re.compile(r"(?P<used>\d+(?:\.\d+)?)\s*%\s+used", re.IGNORECASE)
_RESET_IN = re.compile(r"\(\s*resets\s+in\s+(?P<duration>[^)]*)\)", re.IGNORECASE)
_DURATION_PART = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[dhms])", re.IGNORECASE)
_RATE_LIMIT = re.compile(r"\b(?:429|too\s+many\s+requests|rate[- ]?limited)\b", re.IGNORECASE)
# Quota-exhaustion / fetch-failure markers in the rendered panel.  The CLI's
# quota 403s say "You've reached your ... usage limit"; the /usage card itself
# renders "Failed to fetch usage: HTTP <code>" when the usages endpoint fails.
# Bare ``403`` is excluded when it reads as a money amount (``$403.20``).
_QUOTA_LIMIT = re.compile(
    r"(?<![\d$.])403(?!\.\d)|usage\s+limit|reached\s+your|failed\s+to\s+fetch\s+usage",
    re.IGNORECASE,
)
_PLAN = re.compile(r"\b(?:plan|tier)\s*[:|]\s*(?P<plan>[A-Za-z][A-Za-z0-9+ -]*)", re.IGNORECASE)


def fetch() -> ProviderResult:
    """Read Kimi usage once; errors are reported without retrying the CLI."""

    if shutil.which(BINARY) is None:
        return ProviderResult(
            id="kimi",
            error=f"{BINARY} 실행 파일 없음",
            hint="Kimi Code CLI 설치 후 다시 시도 (SCOPEFUEL_KIMI_BIN 으로 경로 지정 가능)",
            source="cli:/usage",
            pool_class="spend",
        )

    workdir = Path(PROBE_WORKDIR).expanduser()
    proctrack.log_probe_call(workdir, "kimi")
    try:
        with proctrack.single_probe_lock(workdir) as acquired:
            if not acquired:
                # Not an error: another probe is already measuring this pool.
                return ProviderResult(
                    id="kimi",
                    error=f"{BINARY} 탐침이 이미 실행 중 — 이번 회차 건너뜀",
                    hint="kimi 를 직접 실행해 /usage 출력이 나오는지 확인하세요",
                    source="cli:/usage",
                    pool_class="spend",
                )
            output = _probe_once()
    except subprocess.TimeoutExpired:
        return ProviderResult(
            id="kimi",
            error=f"{BINARY} /usage 가 {TIMEOUT_S:.0f}초 안에 끝나지 않음",
            hint="kimi 를 직접 실행해 /usage 출력이 나오는지 확인하세요",
            source="cli:/usage",
            pool_class="spend",
        )
    except OSError as exc:
        return ProviderResult(
            id="kimi",
            error=f"{BINARY} 실행 실패: {exc}",
            source="cli:/usage",
            pool_class="spend",
        )

    return parse(output)


def _probe_once() -> str:
    """Run ``kimi`` in a PTY, wait for its prompt, then probe usage once or twice.

    The child runs in its own session, so nothing the parent's own process
    group receives reaches it: if scopefuel is SIGKILLed mid-probe — which is
    how a polling caller's own timeout ends a slow round — the Python cleanup
    below never runs and the kimi child is reparented to init and keeps
    burning CPU. That is the 2026-09-23 desktop incident's shape (22 grok
    orphans, load 28); kimi's PTY probe shares it.

    The four devices that bound it are proctrack's, already proven on the
    devin probe (be0c0a9) and the grok probe (#593):

    * a per-probe instance directory, flocked, used as the child's cwd — cwd
      is the only identifier that cannot kill an unrelated long-lived worker;
    * a pre-probe sweep of leftovers from probes whose owner has died;
    * pgid + expected-cwd registration, so refresh's timeout handler can
      reclaim the child before ``os._exit``;
    * a detached reaper that watches for the parent's death — the last line
      of defence, and the only one that survives SIGKILL of this process.
    """

    workdir = Path(PROBE_WORKDIR).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    proctrack.kill_stale_probe_leftovers(workdir)
    instance_dir, owner_fd = proctrack.new_probe_dir(workdir)
    master_fd = slave_fd = -1
    process: subprocess.Popen[bytes] | None = None
    reaper: subprocess.Popen[bytes] | None = None
    child_pgid: int | None = None
    try:
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
        process = subprocess.Popen(  # noqa: S603 - fixed command/input; binary is explicit/env-configured
            [BINARY],
            cwd=instance_dir,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
            env=_child_env(),
        )
        with contextlib.suppress(OSError):
            child_pgid = os.getpgid(process.pid)
        if child_pgid is not None:
            proctrack.register(child_pgid, instance_dir)
        reaper = proctrack.spawn_reaper(instance_dir, ttl_s=TIMEOUT_S + 90.0)
        os.close(slave_fd)
        slave_fd = -1

        time.sleep(STARTUP_DELAY_S)

        output = bytearray()
        deadline = time.monotonic() + TIMEOUT_S
        last_data = time.monotonic()
        last_input = None
        usage_seen_at = None
        ready = False
        trust_sent = False
        sends = 0
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
                last_data = time.monotonic()
                clean = _clean(output.decode("utf-8", errors="replace"))
                if not trust_sent and TRUST_MARKER in clean:
                    os.write(master_fd, b"\r")
                    trust_sent = True
                    last_input = time.monotonic()
                    last_data = last_input
                    continue
                if not ready and _normal_prompt_ready(clean):
                    ready = True
                if _RATE_LIMIT.search(clean) or _QUOTA_LIMIT.search(clean):
                    break
                if ready and usage_seen_at is None and _usage_panel_seen(clean):
                    usage_seen_at = time.monotonic()
                if usage_seen_at is not None and time.monotonic() - usage_seen_at >= USAGE_SETTLE_S:
                    break
                continue

            if process.poll() is not None:
                break
            now = time.monotonic()
            if not ready:
                continue
            if usage_seen_at is not None:
                if now - usage_seen_at >= USAGE_SETTLE_S:
                    break
                continue
            if sends == 0 and now - last_data >= READY_SETTLE_S:
                os.write(master_fd, PROBE_INPUT.encode())
                sends = 1
                last_input = now
                last_data = now
                continue
            if sends == 1 and now - last_data >= IDLE_TIMEOUT_S:
                os.write(master_fd, PROBE_INPUT.encode())
                sends = 2
                last_input = now
                last_data = now
            elif sends == 2 and last_input is not None and now - last_input >= IDLE_TIMEOUT_S:
                break

        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired([BINARY], TIMEOUT_S)
        return output.decode("utf-8", errors="replace")
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
                if process_group is not None:
                    with contextlib.suppress(OSError):
                        os.killpg(process_group, signal.SIGKILL)
                else:
                    process.kill()
                # A wait() that times out here used to escape the finally block,
                # skipping every cleanup line below it — including closing the
                # pty — and replacing the real TimeoutExpired with its own.
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    process.wait(timeout=2.0)
        # The direct child exiting is not the end of the probe's descendants. A
        # CLI that backgrounds a helper and returns 0 leaves that helper running,
        # and the block above skips entirely because ``process.poll()`` is not
        # None — the success path leaked where the timeout path did not. Sweep
        # the instance directory unconditionally, by cwd, before it is removed:
        # once it is gone proctrack has no cwd left to recognise them by.
        with contextlib.suppress(OSError):
            proctrack.kill_leftovers_at_cwd(instance_dir, nested=True)
        if child_pgid is not None:
            proctrack.unregister(child_pgid)
        if reaper is not None:
            if reaper.poll() is None:
                with contextlib.suppress(OSError):
                    reaper.kill()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                reaper.wait(timeout=2.0)
        with contextlib.suppress(OSError):
            os.close(owner_fd)
        shutil.rmtree(instance_dir, ignore_errors=True)
        if slave_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(slave_fd)
        with contextlib.suppress(OSError):
            os.close(master_fd)


def _child_env() -> dict[str, str]:
    """Keep herdr integration variables out of the read-only quota subprocess."""

    env = os.environ.copy()
    for name in tuple(env):
        if name == "HERDR" or name.startswith("HERDR_"):
            del env[name]
    env["COLUMNS"] = str(PTY_COLS)
    env["LINES"] = str(PTY_ROWS)
    return env


def _prompt_ready(text: str) -> bool:
    clean = _clean(text)
    return any(marker in clean for marker in _READY_MARKERS)


def _normal_prompt_ready(text: str) -> bool:
    return _prompt_ready(_clean(text).replace(TRUST_MARKER, ""))


def _usage_panel_seen(text: str) -> bool:
    clean = _clean(text)
    lines = [line.lower() for line in clean.splitlines()]
    return any("weekly" in line and _usage_percent_present(line) for line in lines) and any(
        ("5h" in line or "hour" in line) and _usage_percent_present(line) for line in lines
    )


def _usage_percent_present(line: str) -> bool:
    return _PERCENT_LEFT.search(line) is not None or _PERCENT_USED.search(line) is not None


def parse(text: str) -> ProviderResult:
    """Parse Kimi CLI remaining percentages into scopefuel used percentages.

    The panel renders one row per entry of the managed ``GET /usages``
    payload: ``5h limit``, ``Weekly limit`` and ``Monthly limit`` (the
    membership quota that freezes all usage on its own).  A quota 403 or a
    fetch failure can appear next to otherwise healthy-looking rows, so any
    such marker makes the whole reading unmeasurable — a partially rendered
    panel is never evidence of a healthy pool.
    """

    clean = _clean(text)
    if _RATE_LIMIT.search(clean):
        return ProviderResult(
            id="kimi",
            error="Kimi CLI usage rate limited (HTTP 429/rate limit; retry 금지)",
            hint="kimi 를 직접 실행해 /usage 출력이 나오는지 확인하세요",
            source="cli:/usage",
            raw={"stdout": clean},
            pool_class="spend",
        )
    limit_line = next((line.strip() for line in clean.splitlines() if _QUOTA_LIMIT.search(line)), "")
    if limit_line:
        return ProviderResult(
            id="kimi",
            error=f"Kimi CLI /usage 출력에 사용 한도 도달·조회 오류 표시가 있음: {limit_line[:160]}",
            hint="kimi 를 직접 실행해 /usage 출력이 나오는지 확인하세요",
            source="cli:/usage",
            raw={"stdout": clean},
            pool_class="spend",
        )

    buckets_by_kind: dict[str, Bucket] = {}
    for line in clean.splitlines():
        lower = line.lower()
        if not _usage_percent_present(lower):
            continue

        if "weekly" in lower:
            kind, label, window, horizon = "weekly", "weekly", "7d", "week"
        elif "5h" in lower or "hour" in lower:
            kind, label, window, horizon = "session", "5h", "5h", "now"
        elif "month" in lower:
            kind, label, window, horizon = "monthly", "monthly", "30d", "month"
        else:
            continue

        left_match = _PERCENT_LEFT.search(line)
        used_match = _PERCENT_USED.search(line)
        if left_match is None and used_match is None:
            continue
        remaining = float(left_match["remaining"]) if left_match else None
        used = float(used_match["used"]) if used_match else 100.0 - remaining  # type: ignore[operator]
        if not 0 <= used <= 100:
            continue

        reset_match = _RESET_IN.search(line)
        duration = reset_match["duration"].strip() if reset_match else None
        buckets_by_kind.setdefault(
            kind,
            Bucket(
                label=label,
                window=window,
                used_pct=round(used, 1),
                resets_at=_reset_iso(duration),
                scope=Scope("account"),
                horizon=horizon,  # type: ignore[arg-type]
                note=(f"remaining {remaining:g}%" if remaining is not None else f"used {used:g}%")
                + (f"; resets in {duration}" if duration else ""),
            ),
        )

    buckets = [buckets_by_kind[k] for k in ("session", "weekly", "monthly") if k in buckets_by_kind]
    if "session" not in buckets_by_kind and "weekly" not in buckets_by_kind:
        error = "/usage 출력에서 Weekly/5h quota 줄을 찾지 못함"
        if "monthly" in buckets_by_kind:
            error += " (monthly 행만으로는 5h/weekly 소진 여부를 판정할 수 없음)"
        return ProviderResult(
            id="kimi",
            error=error,
            hint="kimi 를 직접 실행해 /usage 출력이 나오는지 확인하세요",
            source="cli:/usage",
            raw={"stdout": clean},
            pool_class="spend",
        )

    plan_match = _PLAN.search(clean)
    return ProviderResult(
        id="kimi",
        plan=plan_match["plan"].strip() if plan_match else None,
        buckets=buckets,
        note="Kimi CLI /usage의 남은 비율을 used_pct로 변환",
        source="cli:/usage",
        raw={"stdout": clean},
        pool_class="spend",
    )


def _clean(text: str) -> str:
    return _ANSI.sub("", text).replace("\r", "\n")


def _reset_iso(duration: str | None) -> str | None:
    if not duration:
        return None
    total_seconds = 0.0
    for match in _DURATION_PART.finditer(duration):
        value = float(match["value"])
        total_seconds += value * {"d": 86400, "h": 3600, "m": 60, "s": 1}[match["unit"].lower()]
    if total_seconds <= 0:
        return None
    return (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=total_seconds)).isoformat()
