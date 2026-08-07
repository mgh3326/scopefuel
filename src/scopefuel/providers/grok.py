"""Grok consumer usage probe via the interactive CLI's ``/usage`` command."""

from __future__ import annotations

import contextlib
import datetime as dt
import errno
import fcntl
import json
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

from ..model import Bucket, ProviderResult, Scope

BINARY = os.environ.get("SCOPEFUEL_GROK_BIN") or "grok"
# 실측 성공 경로 2.6초 대비 충분한 여유를 두되, PTY가 무한 대기하지 않게 한다.
TIMEOUT_S = 30.0
STARTUP_DELAY_S = 0.4
READY_SETTLE_S = 0.5
USAGE_SETTLE_S = 0.5
PROBE_INPUT = "/usage\r"
PROBE_WORKDIR = pathlib.Path.home() / ".local" / "share" / "scopefuel" / "grok-probe-workdir"
SOURCE = "cli:/usage"
KST = dt.timezone(dt.timedelta(hours=9), name="Asia/Seoul")
# A/B 실측: 크기 미설정 PTY에서는 Grok TUI가 usage 패널을 렌더하지 않아 timeout한다.
PTY_ROWS = 50
PTY_COLS = 200

_ANSI = re.compile(r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*(?:\x07|\x1B\\)|\([0-2])")
_WEEKLY = re.compile(r"Weekly\s+limit\s*:\s*(?P<used>\d+(?:\.\d+)?)\s*%", re.I)
_RESET = re.compile(
    r"Next\s+reset\s*:\s*(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})",
    re.I,
)
# TUI 프롬프트 뒤에 테두리/상태줄이 계속 그려지므로 문자열 끝 anchor를 쓰지 않는다.
_PROMPT = re.compile(r"❯|Shift\+Tab")


def _credential_meta() -> dict[str, bool]:
    """Expose only boolean auth presence metadata; never return credential values."""
    auth = pathlib.Path.home() / ".grok" / "auth.json"
    if not auth.exists():
        return {"credential_present": False}
    try:
        data = json.loads(auth.read_text())
    except (OSError, json.JSONDecodeError):
        return {"credential_present": False}
    if not isinstance(data, dict):
        return {"credential_present": False}
    return {"credential_present": bool(data)}


def fetch() -> ProviderResult:
    credential = _credential_meta()
    if shutil.which(BINARY) is None:
        return _degraded(f"{BINARY} 실행 파일 없음", credential)
    try:
        output = _probe_once()
    except subprocess.TimeoutExpired:
        return _degraded(f"{BINARY} /usage 가 {TIMEOUT_S:.0f}초 안에 끝나지 않음", credential)
    except OSError as exc:
        return _degraded(f"{BINARY} 실행 실패: {exc}", credential)
    result = parse(output)
    result.raw = _safe_raw(credential=credential, stdout=_redact_output(_clean(output)))
    return result


def _degraded(error: str, credential: dict[str, bool], stdout: str | None = None) -> ProviderResult:
    return ProviderResult(
        id="grok",
        error=error,
        hint="grok 를 직접 실행해 /usage 출력이 나오는지 확인하세요",
        source=SOURCE,
        raw=_safe_raw(credential=credential, stdout=_redact_output(stdout) if stdout else None),
    )


def _probe_once() -> str:
    """Run grok in a dedicated process group and send ``/usage`` after its prompt."""
    workdir = pathlib.Path(PROBE_WORKDIR).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed command; binary is explicit/env-configured
            [BINARY],
            cwd=workdir,
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
        sent = False
        usage_seen_at: float | None = None
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
                if not sent and _PROMPT.search(clean):
                    os.write(master_fd, PROBE_INPUT.encode())
                    sent = True
                    last_data = time.monotonic()
                if sent and _WEEKLY.search(clean) and _RESET.search(clean):
                    usage_seen_at = usage_seen_at or time.monotonic()
                    if time.monotonic() - usage_seen_at >= USAGE_SETTLE_S:
                        break
                continue

            if process.poll() is not None:
                break
            if sent and usage_seen_at is None and time.monotonic() - last_data >= TIMEOUT_S:
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
    env = os.environ.copy()
    for name in tuple(env):
        if name == "HERDR" or name.startswith("HERDR_"):
            del env[name]
    env["COLUMNS"] = str(PTY_COLS)
    env["LINES"] = str(PTY_ROWS)
    return env


def parse(text: str, *, now: dt.datetime | None = None) -> ProviderResult:
    """Parse the CLI weekly limit; failure is explicit and never monthly."""
    clean = _clean(text)
    weekly = _WEEKLY.search(clean)
    reset = _RESET.search(clean)
    if weekly is None or reset is None:
        return ProviderResult(
            id="grok",
            error="/usage 출력에서 Weekly limit 또는 Next reset을 찾지 못함",
            hint="grok 를 직접 실행해 /usage 출력 형식을 확인하세요",
            source=SOURCE,
        )
    try:
        used = float(weekly["used"])
        if not 0 <= used <= 100:
            raise ValueError
        resets_at = _reset_iso(reset, now=now)
    except (TypeError, ValueError):
        return ProviderResult(
            id="grok", error="/usage 주간 사용률 또는 리셋 시각이 유효하지 않음", source=SOURCE
        )
    return ProviderResult(
        id="grok",
        buckets=[
            Bucket(
                label="weekly",
                window="7d",
                used_pct=used,
                resets_at=resets_at,
                scope=Scope("account"),
                horizon="week",
            )
        ],
        note="PTY /usage 주간 한도; Next reset은 CLI가 KST로 렌더한 시각",
        source=SOURCE,
    )


def _reset_iso(match: re.Match[str], *, now: dt.datetime | None = None) -> str:
    current = now or dt.datetime.now(KST)
    current = current.astimezone(KST) if current.tzinfo else current.replace(tzinfo=KST)
    month = dt.datetime.strptime(match["month"], "%B").month
    candidate = dt.datetime(
        current.year, month, int(match["day"]), int(match["hour"]), int(match["minute"]), tzinfo=KST
    )
    if candidate <= current:
        candidate = candidate.replace(year=current.year + 1)
    return candidate.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _clean(text: str) -> str:
    return _ANSI.sub("", text).replace("\r", "")


def _safe_raw(**parts: object) -> dict:
    out: dict = {}
    for key, value in parts.items():
        if value is None:
            continue
        if key == "credential" and isinstance(value, dict):
            out[key] = {name: item for name, item in value.items() if isinstance(item, bool)}
        else:
            out[key] = value
    return out


def _redact_output(text: str) -> str:
    """Keep diagnostics useful while removing known credential/identity values."""
    auth = pathlib.Path.home() / ".grok" / "auth.json"
    try:
        data = json.loads(auth.read_text())
    except (OSError, json.JSONDecodeError):
        return text
    secrets: set[str] = set()
    if isinstance(data, dict):
        for entry in data.values():
            if isinstance(entry, dict):
                for name in ("key", "email", "user_id", "team_id"):
                    value = entry.get(name)
                    if isinstance(value, str) and value:
                        secrets.add(value)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return text
