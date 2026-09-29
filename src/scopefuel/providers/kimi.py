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
import hashlib
import json
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
from dataclasses import dataclass
from pathlib import Path

from .. import proctrack
from ..model import PROBE_IN_PROGRESS, Bucket, ProviderResult, Scope, _parse_reset, _window_seconds
from ..policy import load_config

BINARY = os.environ.get("SCOPEFUEL_KIMI_BIN") or "kimi"
TIMEOUT_S = 30.0
STARTUP_DELAY_S = 0.4
READY_SETTLE_S = 0.5
IDLE_TIMEOUT_S = 8.0
USAGE_SETTLE_S = 0.5
PROBE_INPUT = "/usage\r"
PROBE_WORKDIR = Path.home() / ".local" / "share" / "scopefuel" / "kimi-probe-workdir"
# task #928 — kimi enforces workspace trust per exact directory: the probe's
# cwd is the fixed PROBE_WORKDIR whose trust entry is seeded once, so the
# dialog below must never render.  A fresh probe-* dir per run re-prompted
# forever and (on versions whose default answer is "Don't trust") exited the
# CLI before /usage was ever read.
# Whitespace-insensitive: a TUI may draw the dialog title with cursor-move
# escapes between words, which _clean strips without inserting spaces.
TRUST_MARKER = re.compile(r"trust\s*this\s*folder\?", re.IGNORECASE)
_READY_MARKERS = ("│ >", "Kimi K3 thinking")
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
# are only visible in the records the CLI itself writes per session.  Only
# structured error records count — transcript/tool-output blobs in wire.jsonl
# quote the same words and must never be mistaken for a lockout.
SESSIONS_DIR: Path | None = None  # test override; None resolves env/home per call


def _kimi_home() -> Path:
    # kimi honours KIMI_CODE_HOME for its data dir (#705 tester F6).
    home = os.environ.get("KIMI_CODE_HOME") or str(Path.home() / ".kimi-code")
    return Path(home).expanduser()


def _sessions_dir() -> Path:
    if SESSIONS_DIR is not None:
        return Path(SESSIONS_DIR)
    return _kimi_home() / "sessions"


# task #966 — clone homes.  wrk 의 effort 고정 kimi 프로필(kimi-k3-low 등)은
# bin/kimi-clone-home 이 만든 복제 홈을 KIMI_CODE_HOME 으로 받아 실행되므로, 그
# 실행의 403 은 복제 홈의 sessions/ 에 남고 ~/.kimi-code 스캔은 못 본다 — M1 에서
# 게이트를 통과시킨 바로 그 사각이다.  복제 홈의 기본 위치는
# ${XDG_DATA_HOME:-~/.local/share}/kimi-code-{low,high,max} 이고
# KIMI_CODE_{LOW,HIGH,MAX}_HOME 으로 옮길 수 있다.
_CLONE_HOME_ENVS = (
    ("KIMI_CODE_LOW_HOME", "kimi-code-low"),
    ("KIMI_CODE_HIGH_HOME", "kimi-code-high"),
    ("KIMI_CODE_MAX_HOME", "kimi-code-max"),
)


def _data_home() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base).expanduser()


def _clone_homes() -> list[Path]:
    homes = []
    for env, dirname in _CLONE_HOME_ENVS:
        override = os.environ.get(env)
        homes.append(Path(override).expanduser() if override else _data_home() / dirname)
    return homes


def _extra_homes() -> list[Path]:
    """``[kimi] extra_homes`` (config.toml) — 추가로 스캔할 kimi 홈들 (각 홈 아래 sessions/)."""
    section = load_config().get("kimi")
    homes = section.get("extra_homes") if isinstance(section, dict) else None
    if not isinstance(homes, list):
        return []
    return [Path(home).expanduser() for home in homes if isinstance(home, str) and home.strip()]


def _scan_roots() -> list[Path]:
    """모든 kimi 홈의 sessions/ 루트 — 없는 디렉터리는 스캔부가 조용히 건너뛴다."""
    roots = [_sessions_dir()] + [home / "sessions" for home in (*_clone_homes(), *_extra_homes())]
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        # realpath dedupe: a symlinked alias of a clone home scans once —
        # double-scanning only double-counts files in --explain, but the
        # duplicate lines are themselves misleading diagnostics (#966 fix 1).
        key = os.path.realpath(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


_SESSION_LOG_MAX_AGE_S = 32 * 86400  # a lockout cannot outlive its 30d window
_SESSION_LOG_TAIL_BYTES = 1_048_576
_SESSION_LIMIT_ERR = re.compile(r"usage\s+limit", re.IGNORECASE)
# kimi-code.log records provider failures as ``<ISO>Z WARN llm request failed``
# lines with an errorMessage field — anchored, so quoted text cannot match.
_SESSION_LOG_ERR = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z\s+(?:WARN|ERROR)\b")
_SESSION_LOG_ERR_CTX = re.compile(r"errorName=APIStatusError|provider\.auth_error|statusCode=403")
# errorMessage may embed the JSON 403 body escaped — keep consuming escapes.
_SESSION_ERRMSG = re.compile(r'errorMessage="(?P<msg>(?:[^"\\]|\\.)*)"')
# ``--explain`` 진단에 실리는 오류 이름 — 본문이 아니라 필드 이름만.
# 필드 시작 경계(행 처음이거나 앞이 공백)·끝 경계(뒤가 공백/행 끝)와 길이 상한을
# 둔다 — 따옴표 안이나 초장문 값이 필드로 오인되지 않도록 (#966 fix rounds 1-2,
# tester BLOCKER 1). 따옴표 필드 본문 제외는 _log_lockout 이 먼저 잘라낸다.
_SESSION_ERR_NAME = re.compile(r"(?<!\S)errorName=(?P<name>[A-Za-z0-9_.]{1,64})(?!\S)")
# errorName 검색 전에 지우는 따옴표 필드 구간 — ``key="..."`` (escape-aware,
# _SESSION_ERRMSG 와 같은 본문 규칙). errorMessage 만이 아니라 어떤 따옴표
# 필드든 같은 삽입 경로가 된다 (#966 fix round 2, tester BLOCKER 1a).
_SESSION_QUOTED_FIELD = re.compile(r'\w+="(?:[^"\\]|\\.)*"')
# Window names are taken only from the error message itself, word-bounded —
# a bare "7d" substring appears in hex traceIds and classifies wrong.
_LOCKOUT_MONTHLY = re.compile(r"\bmonth", re.IGNORECASE)
_LOCKOUT_WEEKLY = re.compile(r"\bweek|7-day", re.IGNORECASE)
_LOCKOUT_SESSION = re.compile(r"5-hour|(?<!\d)5h\b|hour", re.IGNORECASE)
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


def _seed_workspace_trust(workdir: Path) -> None:
    """Seed kimi's workspace-trust entry for the fixed probe dir, once.

    Same scheme as ``kimi_seed_workspace_trust`` in agent-skills' bin/wrk
    (ROB-1307): kimi resolves a directory's trust by looking up
    ``<kimi home>/workspace-trust/wd_<sanitized>_<sha256(abs)[:12]>`` where
    ``sanitized`` is the basename lowercased, cut to 40 chars, with trailing
    non-alphanumerics dropped; the body is ``{"root": <abs>, "trustedAt":
    <epoch ms>}``.  kimi 2.1.1 ships no trust flag (``kimi --help``), so the
    file is the only non-interactive path.

    Deliberately fail-open like the wrk original: a seeding failure never
    blocks the probe — the CLI then renders its trust dialog and parse()
    reports the pool unmeasurable.  Only the probe's own fixed workdir is
    written — never HOME or a parent directory.
    """

    try:
        root = Path(os.path.realpath(workdir))
        home = Path(os.path.realpath(Path.home()))
        if root == home or root in home.parents:
            return  # never trust HOME or an ancestor of it
        sanitized = root.name.lower()[:40]
        sanitized = re.sub(r"[^a-z0-9]+$", "", sanitized)
        if not sanitized:
            return
        digest = hashlib.sha256(str(root).encode()).hexdigest()[:12]
        trust_dir = _kimi_home() / "workspace-trust"
        trust_file = trust_dir / f"wd_{sanitized}_{digest}"
        if trust_file.exists():
            return
        trust_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"root": str(root), "trustedAt": int(time.time() * 1000)},
            separators=(",", ":"),
        )
        # O_EXCL keeps an existing entry immutable even if another seeding
        # races between the existence check and this write (wrk's noclobber).
        fd = os.open(trust_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, f"{payload}\n".encode())
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return


def _probe_once() -> str:
    """Run ``kimi`` in a PTY, wait for its prompt, then probe usage once or twice.

    The child runs in its own session, so nothing the parent's own process
    group receives reaches it: if scopefuel is SIGKILLed mid-probe — which is
    how a polling caller's own timeout ends a slow round — the Python cleanup
    below never runs and the kimi child is reparented to init and keeps
    burning CPU. That is the 2026-09-23 desktop incident's shape (22 grok
    orphans, load 28); kimi's PTY probe shares it.

    The devices that bound it are proctrack's, already proven on the devin
    probe (be0c0a9) and the grok probe (#593) — with one #928 change: the
    child's cwd is the single fixed ``PROBE_WORKDIR``, not a per-probe
    ``probe-*`` dir, because kimi gates on workspace trust per exact
    directory.  The dir is scopefuel-private, so cwd still identifies only
    this provider's processes:

    * the fixed workdir as the child's cwd — cwd is the only identifier that
      cannot kill an unrelated long-lived worker;
    * a pre-probe sweep of leftovers from probes whose owner has died;
    * pgid + expected-cwd registration, so refresh's timeout handler can
      reclaim the child before ``os._exit``;
    * a detached reaper that watches for the parent's death — the last line
      of defence, and the only one that survives SIGKILL of this process.
      (The reaper's target is now the shared fixed dir: a dead parent's
      reaper could sweep a brand-new probe's child inside its ~1.6s sweep
      window — rare, and costs one unmeasurable round, never an orphan.)
    """

    workdir = Path(PROBE_WORKDIR).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    _seed_workspace_trust(workdir)
    proctrack.kill_stale_probe_leftovers(workdir)
    master_fd = slave_fd = -1
    process: subprocess.Popen[bytes] | None = None
    reaper: subprocess.Popen[bytes] | None = None
    child_pgid: int | None = None
    try:
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
        process = subprocess.Popen(  # noqa: S603 - fixed command/input; binary is explicit/env-configured
            [BINARY],
            cwd=workdir,
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
            proctrack.register(child_pgid, workdir)
        reaper = proctrack.spawn_reaper(workdir, ttl_s=TIMEOUT_S + 90.0)
        os.close(slave_fd)
        slave_fd = -1

        time.sleep(STARTUP_DELAY_S)

        output = bytearray()
        deadline = time.monotonic() + TIMEOUT_S
        last_data = time.monotonic()
        last_input = None
        usage_seen_at = None
        ready = False
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
                if TRUST_MARKER.search(clean):
                    # The seeded trust entry did not take (or the CLI changed
                    # its scheme) — never answer the dialog for it: a blind
                    # Enter accepts whatever is highlighted, which trusts
                    # whatever cwd it was launched in.  Bail out; parse()
                    # reports the marker as unmeasurable.
                    break
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
        # the workdir unconditionally, by cwd — the directory itself stays (it
        # is the fixed trusted dir), only the processes inside it are reaped.
        with contextlib.suppress(OSError):
            proctrack.kill_leftovers_at_cwd(workdir, nested=True)
        if child_pgid is not None:
            proctrack.unregister(child_pgid)
        if reaper is not None:
            if reaper.poll() is None:
                with contextlib.suppress(OSError):
                    reaper.kill()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                reaper.wait(timeout=2.0)
        # The workdir is the fixed trusted dir — it must survive the probe.
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
    return _prompt_ready(TRUST_MARKER.sub("", _clean(text)))


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
    if TRUST_MARKER.search(clean):
        # The probe hit kimi's workspace-trust dialog — /usage was never read.
        # Unmeasurable, never a fabricated percentage: a partially blocked
        # startup is not evidence about the pool in either direction.
        return ProviderResult(
            id="kimi",
            error="Kimi CLI 가 작업 디렉터리 trust 를 물어 /usage 출력이 없음",
            hint="kimi 를 직접 실행해 /usage 출력이 나오는지 확인하세요",
            source="cli:/usage",
            raw={"stdout": clean},
            pool_class="spend",
        )
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


def _wire_lockout(line: str) -> tuple[str, dt.datetime | None, str] | None:
    """Structured auth_error record from a wire.jsonl line, or None.

    Transcript and tool-output records quote the same words — only the
    protocol's error fields count: ``error.code == "provider.auth_error"`` or
    a top-level message that starts with ``[provider.auth_error]``.  The
    timestamp comes from the record's ``time`` field, never from ISO text
    embedded mid-blob.  The third element is the error *name* only —
    record contents never leave this function.
    """
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    message: object = None
    error = record.get("error")
    if isinstance(error, dict) and error.get("code") == "provider.auth_error":
        message = error.get("message")
    else:
        candidate = record.get("message")
        if isinstance(candidate, str) and candidate.startswith("[provider.auth_error]"):
            message = candidate
    if not isinstance(message, str) or not _SESSION_LIMIT_ERR.search(message):
        return None
    ts = None
    raw_time = record.get("time")
    if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool):
        try:
            ts = dt.datetime.fromtimestamp(raw_time / 1000, dt.UTC)
        except (OSError, OverflowError, ValueError):
            ts = None
    return message, ts, "provider.auth_error"


def _log_lockout(line: str) -> tuple[str, dt.datetime | None, str] | None:
    """A provider quota failure from a kimi-code.log line, or None.

    Requires the anchored ``<ISO>Z WARN/ERROR`` record shape plus an auth/403
    context marker, and reads the window only from the ``errorMessage`` value —
    text quoted elsewhere in the file cannot qualify.  The third element is the
    error *name* only (errorName= value or the matched context marker) —
    record contents never leave this function.
    """
    head = _SESSION_LOG_ERR.match(line)
    ctx = _SESSION_LOG_ERR_CTX.search(line) if head is not None else None
    if head is None or ctx is None:
        return None
    msg_match = _SESSION_ERRMSG.search(line)
    message = msg_match["msg"] if msg_match else ""
    if not _SESSION_LIMIT_ERR.search(message):
        return None
    try:
        ts: dt.datetime | None = dt.datetime.fromisoformat(head["ts"] + "+00:00")
    except ValueError:
        ts = None
    # The error name may only come from the record's own unquoted fields —
    # text inside ANY quoted field value is record content and must never
    # reach --explain (#966 fix rounds 1-2: 'errorName=<token>' smuggled via
    # errorMessage, then via an arbitrary quoted field, was printed as the
    # error name).  Every key="..." span is cut before the search; an
    # over-long or quoted value cannot match at all, so the fixed context
    # marker applies — a truncated prefix is never printed.
    # Round 3 (fail closed): a leftover quote after the strip means the line's
    # quoting is unparseable (unterminated or single-quoted) — then the error
    # name cannot be trusted, so the fixed context marker applies.
    rest = _SESSION_QUOTED_FIELD.sub("", line)
    name = None if ('"' in rest or "'" in rest) else _SESSION_ERR_NAME.search(rest)
    return message, ts, name["name"] if name else ctx.group(0)


def _lockout_window(message: str) -> str | None:
    """Which quota window a 'usage limit' error message names, or None when it
    does not say — an unplaceable error is unmeasurable, not guesswork."""
    if _LOCKOUT_MONTHLY.search(message):
        return "monthly"
    if _LOCKOUT_WEEKLY.search(message):
        return "weekly"
    if _LOCKOUT_SESSION.search(message):
        return "session"
    return None


def _scan_quota_errors(path: Path, *, fallback_ts: dt.datetime) -> list[tuple[dt.datetime, str | None, str]]:
    """(timestamp, window-kind, error-name) tuples for usage-limit errors in one file."""
    try:
        with path.open("rb") as fh:
            if path.stat().st_size > _SESSION_LOG_TAIL_BYTES:
                fh.seek(-_SESSION_LOG_TAIL_BYTES, os.SEEK_END)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    extractor = _wire_lockout if path.name == "wire.jsonl" else _log_lockout
    hits = []
    for line in data.splitlines():
        hit = extractor(line)
        if hit is None:
            continue
        message, ts, err = hit
        hits.append((ts or fallback_ts, _lockout_window(message), err))
    return hits


@dataclass
class _RootScan:
    """한 sessions 루트의 스캔 결과 — 경로·개수·시각만, 기록 본문은 절대 없다."""

    root: Path
    exists: bool
    files: int = 0  # 패턴에 맞은 기록 파일 수
    scanned: int = 0  # 그중 신선도 창(_SESSION_LOG_MAX_AGE_S) 안의 파일 수
    newest_mtime: dt.datetime | None = None


@dataclass
class _LockoutHit:
    """창 종류별 최신 관측 — 시각과 오류 이름뿐, 본문 없음."""

    ts: dt.datetime
    error: str


def _lockout_scan(now: dt.datetime) -> tuple[list[_RootScan], dict[str | None, _LockoutHit]]:
    """모든 kimi 홈의 sessions/ 를 스캔해 루트별 결과와 창별 최신 hit 을 돌려준다."""
    min_mtime = now.timestamp() - _SESSION_LOG_MAX_AGE_S
    roots: list[_RootScan] = []
    latest: dict[str | None, _LockoutHit] = {}
    for root in _scan_roots():
        rep = _RootScan(root=root, exists=root.is_dir())
        roots.append(rep)
        if not rep.exists:
            continue
        for pattern in ("*/*/logs/kimi-code.log", "*/*/agents/*/wire.jsonl"):
            for path in root.glob(pattern):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                rep.files += 1
                mtime = dt.datetime.fromtimestamp(stat.st_mtime, dt.UTC)
                if rep.newest_mtime is None or mtime > rep.newest_mtime:
                    rep.newest_mtime = mtime
                if stat.st_mtime < min_mtime:
                    continue
                rep.scanned += 1
                for ts, kind, err in _scan_quota_errors(path, fallback_ts=mtime):
                    cur = latest.get(kind)
                    if cur is None or ts > cur.ts:
                        latest[kind] = _LockoutHit(ts=ts, error=err)
    return roots, latest


def _observed_lockouts(now: dt.datetime) -> dict[str | None, dt.datetime]:
    """Latest observed provider usage-limit error per window kind.

    Reads only the timestamped records the CLI already writes (never
    credentials or config) — across every kimi home this host may have run
    under (primary, clone homes, configured extras).  Files untouched for
    longer than the longest lockout window cannot describe a current window
    and are skipped.
    """
    return {kind: hit.ts for kind, hit in _lockout_scan(now)[1].items()}


def _classify_lockout(
    kind: str | None, ts: dt.datetime, buckets: list[Bucket], now: dt.datetime
) -> tuple[str, Bucket | None]:
    """한 관측 오류의 처분 — _apply_observed_lockouts 와 --explain 이 같은 규칙을 공유한다.

    ``exhausted`` — 현재 창 안의 잠금(덮어쓸 bucket 과 함께).
    ``stale`` — 이미 리셋된 지난 창의 잠금, 무시.
    ``unmeasurable`` — 현재일 수 있는데 패널이 창을 못 잡는다, 실패 폐쇄.
    ``ignored`` — 창 이름이 없고 현재 창일 수 없는(24h 이상 지난) 오류.
    """
    window = _LOCKOUT_WINDOW.get(kind or "")
    if window is None:
        # The error text does not name a window — if it is plausibly
        # current the whole reading is unmeasurable rather than guessed.
        return ("unmeasurable", None) if ts >= now - dt.timedelta(hours=24) else ("ignored", None)
    bucket = next((b for b in buckets if b.window == window), None)
    window_s = _window_seconds(window) or 0.0
    reset_dt = _parse_reset(bucket.resets_at) if bucket is not None else None
    if reset_dt is not None and bucket is not None:
        window_start = reset_dt - dt.timedelta(seconds=window_s)
        # stale: the 403 belongs to a window that already reset
        return ("stale", bucket) if ts < window_start else ("exhausted", bucket)
    if ts >= now - dt.timedelta(seconds=window_s):
        # The panel cannot bound the window this error belongs to.
        return "unmeasurable", bucket
    return "stale", bucket


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
        decision, bucket = _classify_lockout(kind, ts, result.buckets, now)
        if decision == "unmeasurable":
            return unmeasurable(kind, ts)
        if decision != "exhausted" or bucket is None:
            continue
        # task #966 — 덮어쓴 창은 locked 표시를 얻어 소진 판정이 severity·verdict·
        # gate 까지 같은 답으로 전파된다(spend 풀의 고사용 면제를 건너뛴다).
        # 패널의 'used N%' 노트는 거짓이므로 관측 문장으로 대체하고
        # 'resets in …' 힌트만 남긴다 — 'used 0%' 옆의 소진 표기는 또 다른 오독이다.
        bucket.used_pct = 100.0
        bucket.locked = True
        _head, sep, tail = (bucket.note or "").partition(";")
        bucket.note = f"provider 'usage limit' 오류 관측 {ts.isoformat()} — 패널 수치 대신 소진 처리" + (
            sep + tail if sep else ""
        )
    return result


def explain_lockout_scan(result: ProviderResult | None = None, *, now: dt.datetime | None = None) -> str:
    """``kimi lockout scan`` 진단 블록 (--explain).

    각 스캔 루트(경로·존재·파일 수·최신 mtime), 창별 최신 hit 의 ISO 시각과
    오류 이름, 그리고 판정 결과를 보여준다. 세션 기록 본문은 어떤 형태로도
    출력하지 않는다 — 데스크가 '왜 못 잡았나'를 답하는 명령이므로.
    """
    now = now or dt.datetime.now(dt.UTC)
    roots, hits = _lockout_scan(now)
    lines = ["kimi lockout scan"]
    for rep in roots:
        if not rep.exists:
            lines.append(f"  root {rep.root} exists=no")
            continue
        newest = rep.newest_mtime.isoformat() if rep.newest_mtime is not None else "-"
        lines.append(f"  root {rep.root} exists=yes files={rep.files} scanned={rep.scanned} newest={newest}")
    if not hits:
        lines.append("  decision: no usage-limit records")
        return "\n".join(lines)
    # 판정은 현재 읽힌 결과의 창에 대해 재계산한다 — hit 시각·창 규칙은 같다.
    buckets = result.buckets if result is not None and result.error is None else []
    decisions: list[tuple[str | None, str]] = []
    for kind, hit in sorted(hits.items(), key=lambda item: str(item[0])):
        decision, _bucket = _classify_lockout(kind, hit.ts, buckets, now)
        decisions.append((kind, decision))
        lines.append(
            f"  hit {kind or 'unknown'} newest={hit.ts.isoformat()} error={hit.error} decision={decision}"
        )
    first_block = next(
        (f"unmeasurable ({kind or 'unknown'})" for kind, d in decisions if d == "unmeasurable"),
        None,
    )
    if first_block is not None:
        lines.append(f"  decision: {first_block}")
    elif exhausted := [str(kind) for kind, d in decisions if d == "exhausted"]:
        lines.append(f"  decision: exhausted {', '.join(exhausted)}")
    else:
        lines.append("  decision: no current lockout")
    return "\n".join(lines)


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
