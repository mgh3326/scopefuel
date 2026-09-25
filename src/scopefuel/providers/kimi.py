"""Kimi Code CLI usage probe.

Kimi's supported quota surface is the interactive ``kimi`` CLI's ``/usage``
command.  It renders usage only when attached to a terminal, so this provider
uses a short-lived POSIX pseudo-terminal and never reads or updates Kimi's
credential/config files.  The CLI owns authentication and any upstream HTTP
details; scopefuel only parses the rendered quota summary.

The panel draws ``round(used_ratio * 100)`` verbatim per row (``N% used`` —
the CLI passes the managed ``GET /usages`` ``limit_5h``/``limit_7d``/
``limit_month_total`` ``used_ratio`` through unmodified), so ``% used`` is
read as used and ``% left`` as ``100 - remaining``.  ``used_ratio`` does not
reflect an enforcement lockout: a weekly-limited account renders ``0% used``
while real requests 403 (task #705).  The only local signal for that state is
the CLI's own session records (``sessions/*/session_*/logs/kimi-code.log`` and
``sessions/*/session_*/agents/*/wire.jsonl``), which carry timestamped
``provider.auth_error`` / ``403 ... usage limit`` entries.  A successful parse
is therefore crossed against those records: an observed limit error inside the
current window marks that window exhausted, and one that cannot be placed in a
window makes the reading unmeasurable rather than silently trusting ``0%``.
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
from ..model import PROBE_IN_PROGRESS, Bucket, ProviderResult, Scope, _parse_reset, _window_seconds

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
_RESET_IN = re.compile(r"resets\s+in\s+(?P<duration>(?:\d+(?:\.\d+)?\s*[dhms]\s*)+)", re.IGNORECASE)
_DURATION_PART = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[dhms])", re.IGNORECASE)
_RATE_LIMIT = re.compile(
    r"(?<![\w$.])429(?!\.\d)(?!\w)|\btoo\s+many\s+requests\b|\brate[- ]?limited\b",
    re.IGNORECASE,
)
# Quota-exhaustion / fetch-failure markers in the rendered panel.  The CLI's
# quota 403s say "You've reached your ... usage limit"; the /usage card itself
# renders "Failed to fetch usage: HTTP <code>" when the usages endpoint fails.
# Bare ``403`` is excluded when it reads as a money amount (``$403.20``), a
# token count (``403k``, ``(403 / 256k)``) or part of a longer word/hex string.
_QUOTA_LIMIT = re.compile(
    r"(?<![\w$.])403(?!\.\d)(?!\s*/)(?!\w)"
    r"|usage\s+limit|reached\s+your|failed\s+to\s+fetch\s+usage",
    re.IGNORECASE,
)
# ``5h`` needs a digit lookbehind: the monthly row's ``resets in 20d 15h 2m``
# hint would otherwise match ``"5h" in line`` and misclassify the row.
_SESSION_ROW = re.compile(r"(?<!\d)5h\b|hour", re.IGNORECASE)
_PLAN = re.compile(r"\b(?:plan|tier)\s*[:|]\s*(?P<plan>[A-Za-z][A-Za-z0-9+ -]*)", re.IGNORECASE)
_PERCENT_ANY = re.compile(r"\d+(?:\.\d+)?\s*%")

# task #705 — session-record lockout signal.  The /usage panel's used_ratio
# reads 0 even while the provider is answering 403 "usage limit"; those errors
# are only visible in the records the CLI itself writes per session.
SESSIONS_DIR = Path.home() / ".kimi-code" / "sessions"
_SESSION_LOG_MAX_AGE_S = 8 * 86400  # a lockout cannot outlive its 7d window
_SESSION_LOG_TAIL_BYTES = 1_048_576
_SESSION_LIMIT_ERR = re.compile(r"usage\s+limit", re.IGNORECASE)
_SESSION_ERR_AUTH = re.compile(r"403|auth_error|apistatuserror", re.IGNORECASE)
_SESSION_LOG_TS = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z")
_WIRE_TS = re.compile(r'"time"\s*:\s*(?P<ms>\d{10,13})')
_LOCKOUT_WINDOW = {"session": "5h", "weekly": "7d", "monthly": "30d"}


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
                    source="cli:/usage",
                    pool_class="spend",
                    error_kind=PROBE_IN_PROGRESS,
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

    result = parse(output)
    if result.error is None:
        result = _apply_observed_lockouts(result)
    return result


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
    """Parse the /usage panel's percentages into scopefuel used percentages.

    The panel renders one row per entry of the managed ``GET /usages``
    payload: ``5h limit``, ``Weekly limit`` and ``Monthly limit`` (the
    membership quota that freezes all usage on its own).  Each row's number is
    ``used_ratio`` verbatim when labelled ``% used`` and ``100 - remaining``
    when labelled ``% left``.  A quota row whose percentage carries neither
    qualifier is never guessed — like a quota 403 or a fetch failure it makes
    the whole reading unmeasurable, because a partially understood panel is
    never evidence of a healthy pool.
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
    unknown_rows: list[str] = []
    for line in clean.splitlines():
        lower = line.lower()
        if not _usage_percent_present(lower):
            # task #705 — a quota-looking row carrying a bare ``N%`` is
            # unmeasurable: without a left/used qualifier we cannot tell
            # remaining from used, and guessing 0% used is how the gate ended
            # up assigning a locked-out pool.
            if _PERCENT_ANY.search(line) and (
                "limit" in lower or _SESSION_ROW.search(lower) or "week" in lower or "month" in lower
            ):
                unknown_rows.append(line.strip())
            continue

        # Order matters: classify by the row's own label, and check ``month``
        # before the session marker — a monthly reset hint such as
        # ``resets in 20d 15h 2m`` contains the substring ``5h``/``15h``.
        if "month" in lower:
            kind, label, window, horizon = "monthly", "monthly", "30d", "month"
        elif "weekly" in lower:
            kind, label, window, horizon = "weekly", "weekly", "7d", "week"
        elif _SESSION_ROW.search(lower):
            kind, label, window, horizon = "session", "5h", "5h", "now"
        elif "limit" in lower:
            unknown_rows.append(line.strip())
            continue
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
    if unknown_rows:
        return ProviderResult(
            id="kimi",
            error=f"/usage 출력에 알 수 없는 quota 행이 있음: {unknown_rows[0][:120]}",
            hint="kimi 를 직접 실행해 /usage 출력이 나오는지 확인하세요",
            source="cli:/usage",
            raw={"stdout": clean},
            pool_class="spend",
        )
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
        note="Kimi CLI /usage 패널의 % used 를 used_pct로 읽음 (% left 는 100-remaining 환산)",
        source="cli:/usage",
        raw={"stdout": clean},
        pool_class="spend",
    )


def _line_timestamp(line: str) -> dt.datetime | None:
    """Timestamp of one session-record line: ISO prefix (kimi-code.log) or a
    wire.jsonl ``"time"`` epoch-ms field."""
    log_match = _SESSION_LOG_TS.search(line)
    if log_match:
        try:
            return dt.datetime.fromisoformat(log_match["ts"] + "+00:00")
        except ValueError:
            pass
    wire_match = _WIRE_TS.search(line)
    if wire_match:
        try:
            return dt.datetime.fromtimestamp(int(wire_match["ms"]) / 1000, dt.UTC)
        except (OSError, OverflowError, ValueError):
            return None
    return None


def _lockout_window(line: str) -> str | None:
    """Which quota window a 'usage limit' error names, or None when the line
    does not say — an unplaceable error is unmeasurable, not guesswork."""
    lower = line.lower()
    if "month" in lower:
        return "monthly"
    if "week" in lower or "7-day" in lower or "7d" in lower:
        return "weekly"
    if _SESSION_ROW.search(lower):
        return "session"
    return None


def _scan_quota_errors(path: Path, *, fallback_ts: dt.datetime) -> list[tuple[dt.datetime, str | None]]:
    """(timestamp, window-kind) pairs for provider usage-limit errors in one file."""
    try:
        with path.open("rb") as fh:
            if path.stat().st_size > _SESSION_LOG_TAIL_BYTES:
                fh.seek(-_SESSION_LOG_TAIL_BYTES, os.SEEK_END)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    hits = []
    for line in data.splitlines():
        if not (_SESSION_LIMIT_ERR.search(line) and _SESSION_ERR_AUTH.search(line)):
            continue
        hits.append((_line_timestamp(line) or fallback_ts, _lockout_window(line)))
    return hits


def _observed_lockouts(now: dt.datetime) -> dict[str | None, dt.datetime]:
    """Latest observed provider usage-limit error per window kind.

    Reads only the timestamped records the CLI already writes (never
    credentials or config).  Files untouched for longer than the longest
    lockout window cannot describe a current window and are skipped.
    """
    root = Path(SESSIONS_DIR).expanduser()
    if not root.is_dir():
        return {}
    min_mtime = now.timestamp() - _SESSION_LOG_MAX_AGE_S
    latest: dict[str | None, dt.datetime] = {}
    for pattern in ("*/*/logs/kimi-code.log", "*/*/agents/*/wire.jsonl"):
        for path in root.glob(pattern):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < min_mtime:
                continue
            fallback = dt.datetime.fromtimestamp(stat.st_mtime, dt.UTC)
            for ts, kind in _scan_quota_errors(path, fallback_ts=fallback):
                if kind not in latest or ts > latest[kind]:
                    latest[kind] = ts
    return latest


def _apply_observed_lockouts(result: ProviderResult, *, now: dt.datetime | None = None) -> ProviderResult:
    """Cross a successful /usage parse against observed provider lockouts.

    The panel's ``used_ratio`` does not reflect an enforcement lockout (#705):
    the exhausted account renders ``0% used``.  When kimi's session records
    show a 'usage limit' error inside the window the panel reports, that
    window is exhausted regardless of the rendered number.  An error that
    cannot be placed in a current window fails closed as unmeasurable; one
    older than the window start is stale and ignored.
    """
    now = now or dt.datetime.now(dt.UTC)
    observed = _observed_lockouts(now)
    if not observed:
        return result

    def unmeasurable(kind: str | None, ts: dt.datetime) -> ProviderResult:
        return ProviderResult(
            id="kimi",
            error=(
                f"Kimi 세션 기록에 usage-limit 오류 관측({kind or '창 불명'}, "
                f"{ts.isoformat()})됐으나 /usage 패널의 현재 창과 대응할 수 없어 측정 불가"
            ),
            hint="kimi 를 직접 실행해 /usage 출력이 나오는지 확인하세요",
            source="cli:/usage",
            raw=result.raw,
            pool_class="spend",
        )

    for kind, ts in sorted(observed.items(), key=lambda item: str(item[0])):
        window = _LOCKOUT_WINDOW.get(kind or "")
        if window is None:
            # The error text does not name a window — if it is plausibly
            # current the whole reading is unmeasurable rather than guessed.
            if ts >= now - dt.timedelta(hours=24):
                return unmeasurable(kind, ts)
            continue
        bucket = next((b for b in result.buckets if b.window == window), None)
        window_s = _window_seconds(window) or 0.0
        reset_dt = _parse_reset(bucket.resets_at) if bucket is not None else None
        if reset_dt is not None and bucket is not None:
            window_start = reset_dt - dt.timedelta(seconds=window_s)
            if ts < window_start:
                continue  # stale: the 403 belongs to a window that already reset
            bucket.used_pct = 100.0
            bucket.note = (
                (bucket.note + " · ") if bucket.note else ""
            ) + f"provider 'usage limit' 오류 관측 {ts.isoformat()} — 패널 수치 대신 소진 처리"
            continue
        if ts >= now - dt.timedelta(seconds=window_s):
            # The panel cannot bound the window this error belongs to.
            return unmeasurable(kind, ts)
    return result


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
