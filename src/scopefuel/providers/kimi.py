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
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import time
from pathlib import Path

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

_ANSI = re.compile(r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*(?:\x07|\x1B\\)|\([0-2])")
_PERCENT_LEFT = re.compile(r"(?P<remaining>\d+(?:\.\d+)?)\s*%\s+left", re.IGNORECASE)
_PERCENT_USED = re.compile(r"(?P<used>\d+(?:\.\d+)?)\s*%\s+used", re.IGNORECASE)
_RESET_IN = re.compile(r"\(\s*resets\s+in\s+(?P<duration>[^)]*)\)", re.IGNORECASE)
_DURATION_PART = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[dhms])", re.IGNORECASE)
_RATE_LIMIT = re.compile(r"\b(?:429|too\s+many\s+requests|rate[- ]?limited)\b", re.IGNORECASE)
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

    try:
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
    """Run ``kimi`` in a PTY, wait for its prompt, then probe usage once or twice."""

    probe_workdir = Path(PROBE_WORKDIR).expanduser()
    probe_workdir.mkdir(parents=True, exist_ok=True)
    master_fd, slave_fd = pty.openpty()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed command/input; binary is explicit/env-configured
            [BINARY],
            cwd=probe_workdir,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
            env=_child_env(),
        )
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
                if _RATE_LIMIT.search(clean):
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
                process.wait(timeout=2.0)
        if slave_fd >= 0:
            os.close(slave_fd)
        with contextlib.suppress(OSError):
            os.close(master_fd)


def _child_env() -> dict[str, str]:
    """Keep herdr integration variables out of the read-only quota subprocess."""

    env = os.environ.copy()
    for name in tuple(env):
        if name == "HERDR" or name.startswith("HERDR_"):
            del env[name]
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
    """Parse Kimi CLI remaining percentages into scopefuel used percentages."""

    clean = _clean(text)
    buckets_by_kind: dict[str, Bucket] = {}
    for line in clean.splitlines():
        lower = line.lower()
        if not _usage_percent_present(lower):
            continue

        if "weekly" in lower:
            kind, label, window, horizon = "weekly", "weekly", "7d", "week"
        elif "5h" in lower or "hour" in lower:
            kind, label, window, horizon = "session", "5h", "5h", "now"
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

    buckets = [buckets_by_kind[k] for k in ("session", "weekly") if k in buckets_by_kind]
    if not buckets:
        if _RATE_LIMIT.search(clean):
            error = "Kimi CLI usage rate limited (HTTP 429/rate limit; retry 금지)"
        else:
            error = "/usage 출력에서 Weekly/5h quota 줄을 찾지 못함"
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
