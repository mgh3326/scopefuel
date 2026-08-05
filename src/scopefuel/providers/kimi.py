"""Kimi Code CLI usage probe.

Kimi's supported quota surface is the interactive ``kimi`` CLI's ``/usage``
command.  It renders usage only when attached to a terminal, so this provider
uses a short-lived POSIX pseudo-terminal and never reads or updates Kimi's
credential/config files.  The CLI owns authentication and any upstream HTTP
details; scopefuel only parses the rendered quota summary.
"""

from __future__ import annotations

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

from ..model import Bucket, ProviderResult, Scope

BINARY = os.environ.get("SCOPEFUEL_KIMI_BIN") or "kimi"
TIMEOUT_S = 30.0
STARTUP_DELAY_S = 0.4
IDLE_TIMEOUT_S = 2.0
PROBE_INPUT = "/usage\r"

_ANSI = re.compile(
    r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*(?:\x07|\x1B\\)|\([0-2])"
)
_PERCENT_LEFT = re.compile(r"(?P<remaining>\d+(?:\.\d+)?)\s*%\s+left", re.IGNORECASE)
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
    """Run ``kimi`` in a PTY, send ``/usage``, then stop after output settles."""

    master_fd, slave_fd = pty.openpty()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed command/input; binary is explicit/env-configured
            [BINARY],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = -1

        time.sleep(STARTUP_DELAY_S)
        os.write(master_fd, PROBE_INPUT.encode())

        output = bytearray()
        deadline = time.monotonic() + TIMEOUT_S
        last_data = time.monotonic()
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
                continue

            if process.poll() is not None:
                break
            if output and time.monotonic() - last_data >= IDLE_TIMEOUT_S:
                break

        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired([BINARY], TIMEOUT_S)
        return output.decode("utf-8", errors="replace")
    finally:
        if process is not None and process.poll() is None:
            try:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=2.0)
            except (subprocess.TimeoutExpired, OSError):
                process.kill()
                process.wait(timeout=2.0)
        if slave_fd >= 0:
            os.close(slave_fd)
        try:
            os.close(master_fd)
        except OSError:
            pass


def parse(text: str) -> ProviderResult:
    """Parse Kimi CLI remaining percentages into scopefuel used percentages."""

    clean = _clean(text)
    buckets_by_kind: dict[str, Bucket] = {}
    for line in clean.splitlines():
        lower = line.lower()
        if "% left" not in lower:
            continue

        if "weekly" in lower:
            kind, label, window, horizon = "weekly", "weekly", "7d", "week"
        elif "5h" in lower or "hour" in lower:
            kind, label, window, horizon = "session", "5h", "5h", "now"
        else:
            continue

        match = _PERCENT_LEFT.search(line)
        if match is None:
            continue
        remaining = float(match["remaining"])
        if not 0 <= remaining <= 100:
            continue

        reset_match = _RESET_IN.search(line)
        duration = reset_match["duration"].strip() if reset_match else None
        buckets_by_kind.setdefault(
            kind,
            Bucket(
                label=label,
                window=window,
                used_pct=round(100.0 - remaining, 1),
                resets_at=_reset_iso(duration),
                scope=Scope("account"),
                horizon=horizon,  # type: ignore[arg-type]
                note=f"remaining {remaining:g}%" + (f"; resets in {duration}" if duration else ""),
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
