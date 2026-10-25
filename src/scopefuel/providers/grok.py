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

from .. import cache, proctrack
from ..model import PROBE_IN_PROGRESS, Bucket, ProviderResult, Scope

BINARY = os.environ.get("SCOPEFUEL_GROK_BIN") or "grok"
# 실측 성공 경로 2.6초 대비 충분한 여유를 두되, PTY가 무한 대기하지 않게 한다.
# hk 1868: SCOPEFUEL_GROK_TIMEOUT_S 로 올릴 수 있다 — [10, 120] 밖은 거부된다.
# 이 값보다 바깥 타임아웃이 짧은 호출자(refresh 의 SCOPEFUEL_REFRESH_TIMEOUT_S,
# 기본 60)는 프로브보다 먼저 울리므로, 키울 때는 호출자 쪽도 같이 올려야 한다.
TIMEOUT_S = 30.0
TIMEOUT_MIN_S = 10.0
TIMEOUT_MAX_S = 120.0
# hk 1868: 타임아웃 연속 시 프로브 재시도를 늦추는 지수 backoff.
# window = min(BASE * 2**(n-1), CAP). 상태는 cache.backoff.json 에 pool 단위로
# 남아 프로세스를 넘나든다 — mbp 의 30초 주기 폴러(panewire·herdr-event)가 매번
# 새 grok 을 띄우는 것을 막는다.
BACKOFF_BASE_S = 60.0
BACKOFF_BASE_MIN_S = 10.0
BACKOFF_BASE_MAX_S = 600.0
BACKOFF_CAP_S = 1800.0
BACKOFF_CAP_MIN_S = 60.0
BACKOFF_CAP_MAX_S = 7200.0
STARTUP_DELAY_S = 0.4
READY_SETTLE_S = 0.5
USAGE_SETTLE_S = 0.5
PROBE_INPUT = "/usage\r"
PROBE_WORKDIR = pathlib.Path.home() / ".local" / "share" / "scopefuel" / "grok-probe-workdir"
SOURCE = "cli:/usage"

# hk 1868 — 타임아웃이 어느 단계에서 났는지 알리는 고정 단계 이름.
# 에러·힌트에는 이 이름과 바이트 수만 싣는다 — 화면 원문은 계정 정보를
# 담을 수 있어 절대 넣지 않는다.
STAGE_NO_OUTPUT = "no-output"  # 출력이 한 바이트도 없음
STAGE_NO_PROMPT = "no-prompt"  # 출력은 있으나 프롬프트(❯/Shift+Tab) 미출현
# hk 1964: grok 1.0.70 의 폴더 신뢰 질문이 떴고 프롬프트는 미출현 — 답은 프로브가
# 아니라 desk 의 몫이다(프로브는 어떤 grok 설정도 읽거나 쓰지 않는다).
STAGE_TRUST_PROMPT = "trust-prompt"
STAGE_NO_PANEL = "no-panel"  # /usage 전송 후 Weekly 패널이 전혀 안 그려짐
STAGE_UNPARSED = "unparsed"  # 패널 단서는 보였으나 정규식이 파싱하지 못함
STAGE_PROBE_IN_FLIGHT = "probe-in-flight"  # 다른 프로세스가 이 pool 을 측정 중
STAGE_BACKOFF = "backoff"  # 연속 타임아웃 backoff 창 안이라 시도 자체를 건너뜀


class ProbeTimeout(subprocess.TimeoutExpired):
    """TimeoutExpired carrying the furthest stage reached and the bytes seen."""

    def __init__(self, cmd, timeout, *, stage: str, nbytes: int) -> None:
        super().__init__(cmd, timeout)
        self.stage = stage
        self.nbytes = nbytes


# A/B 실측: 크기 미설정 PTY에서는 Grok TUI가 usage 패널을 렌더하지 않아 timeout한다.
PTY_ROWS = 50
PTY_COLS = 200

_ANSI = re.compile(r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*(?:\x07|\x1B\\)|\([0-2])")
# grok 4.5 는 "Weekly limit: 0%" 한 줄이었지만 4.6 CLI 는 /usage 를 플로팅 패널로 그린다:
# "Weekly limit (SuperGrok)" 레이블, 진행바, "58%" 가 각각 다른 위치에 페인트되고
# 리셋 줄은 "Resets: August 14, 04:58" 로 바뀌었다(구형 "Next reset:" 도 계속 허용).
# 커서 이동 시퀀스를 걷어내면 텍스트가 페인트 순서대로 이어지므로, 레이블 뒤 첫 퍼센트를
# 비탐욕 160자 창 안에서 찾는다 — 창 제한은 다른 탭(Context usage)의 퍼센트 오인 방지다.
_WEEKLY = re.compile(
    r"Weekly\s+limit(?:\s*\(SuperGrok\))?\s*:?[\s\S]{0,160}?(?P<used>\d+(?:\.\d+)?)\s*%",
    re.I,
)
_RESET = re.compile(
    r"(?:Next\s+reset|Resets?)\s*:\s*(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})",
    re.I,
)
# TUI 프롬프트 뒤에 테두리/상태줄이 계속 그려지므로 문자열 끝 anchor를 쓰지 않는다.
_PROMPT = re.compile(r"❯|Shift\+Tab")
# hk 1964 — grok 1.0.70 폴더 신뢰 질문의 첫 문장(operator-desk 캡처 문구, hk 1964
# comment 7229). 화면에는 이 밑에 경로와 Yes/No 행이 커서 이동으로 그려지지만,
# ANSI 를 걷어낸 텍스트의 이 문장 하나만 본다.
_TRUST_QUESTION = re.compile(r"Do you trust the contents of this directory\?")


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
    workdir = pathlib.Path(PROBE_WORKDIR).expanduser()
    now = time.time()
    until = _probe_backoff_until()
    if until > now:
        # 연속 타임아웃 backoff 창 — grok 을 띄우지 않는다. 캐시가 있는 호출
        # 경로(collect·refresh)는 fetch 에 닿기 전에 같은 파일을 보고 이미
        # 건너뛴다; 여기는 직접 fetch 호출자를 위한 같은 게이트다.
        return _degraded(
            f"{BINARY} /usage 탐침 backoff — 다음 시도 {_iso(until)} (stage={STAGE_BACKOFF})",
            credential,
            hint=f"stage={STAGE_BACKOFF} — 연속 타임아웃으로 {_iso(until)} 전까지 grok 을 띄우지 않음",
            error_kind="probe_timeout",
            backoff_until=until,
            probe={"stage": STAGE_BACKOFF},
        )
    proctrack.log_probe_call(workdir, "grok")
    try:
        with proctrack.single_probe_lock(_probe_lock_dir()) as acquired:
            if not acquired:
                # Not an error: another probe is already measuring this pool.
                # Reporting it as degraded keeps the caller from treating a
                # deliberate skip as a measurement; the marker kind lets the
                # cache/gate layer keep the last good snapshot instead (#639).
                return _degraded(
                    f"{BINARY} 탐침이 이미 실행 중 — 이번 회차 건너뜀 (stage={STAGE_PROBE_IN_FLIGHT})",
                    credential,
                    hint=(
                        f"stage={STAGE_PROBE_IN_FLIGHT} — 다른 프로세스가 이 pool 을 지금 "
                        "측정 중; 끝나면 캐시가 갱신됨"
                    ),
                    error_kind=PROBE_IN_PROGRESS,
                )
            output = _probe_once()
    except ProbeTimeout as exc:
        next_allowed = _note_probe_timeout(time.time(), detail=f"stage={exc.stage}")
        if exc.stage == STAGE_TRUST_PROMPT:
            # hk 1964: 힌트는 workdir 경로만 싣는다 — 화면 원문·계정 정보 없음.
            error = (
                f"{BINARY} 가 폴더 신뢰 질문을 띄워 /usage 를 보내지 못함 "
                f"(stage={exc.stage}, bytes={exc.nbytes})"
            )
            hint = (
                f"stage={exc.stage} — 이 호스트에서 grok 이 {workdir} 폴더를 "
                "신뢰해야 함 (desk 조치 — 프로브는 신뢰 질문에 답하지 않음; "
                f"다음 허용 {_iso(next_allowed)})"
            )
        else:
            error = (
                f"{BINARY} /usage 가 {exc.timeout:.0f}초 안에 끝나지 않음 "
                f"(stage={exc.stage}, bytes={exc.nbytes})"
            )
            hint = (
                f"stage={exc.stage} — grok 를 직접 실행해 /usage 출력이 나오는지 확인하세요 "
                f"(다음 허용 {_iso(next_allowed)})"
            )
        return _degraded(
            error,
            credential,
            hint=hint,
            error_kind="probe_timeout",
            backoff_until=next_allowed,
            probe={"stage": exc.stage, "bytes": exc.nbytes},
        )
    except subprocess.TimeoutExpired:
        return _degraded(f"{BINARY} /usage 가 {_probe_timeout_s():.0f}초 안에 끝나지 않음", credential)
    except OSError as exc:
        return _degraded(f"{BINARY} 실행 실패: {exc}", credential)
    result = parse(output)
    if result.error is None:
        _clear_probe_backoff()
    result.raw = _safe_raw(credential=credential, stdout=_redact_output(_clean(output)))
    return result


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat().replace("+00:00", "Z")


def _bounded_env(name: str, default: float, lo: float, hi: float) -> float:
    """Env-override seconds; values outside ``[lo, hi]`` are refused to the default."""

    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if lo <= value <= hi else default


# hk 1868 r3 — refresh 워커는 SIGALRM 을 _timeout_seconds()(기본 60s)로 무장하고,
# 그게 울리면 워커가 stage 를 보고하기 전에 잘린다. 실효 프로브 타임아웃은
# 무장된 바깥 타이머의 남은 시간에서 teardown 마진을 뺀 값으로 눌러 쓴다 —
# 마진은 TERM 유예+KILL wait+pgid poll(~6s)과 backoff 기록을 덮는다.
# 타이머 없는 호출자(status·herdr-event)는 getitimer 가 0 을 돌려 무보정.
OUTER_TIMER_MARGIN_S = 10.0


def _outer_timer_budget_s() -> float:
    """Remaining budget of the caller's armed ITIMER_REAL, or inf when none."""

    delay, _interval = signal.getitimer(signal.ITIMER_REAL)
    return delay - OUTER_TIMER_MARGIN_S if delay > 0 else float("inf")


def _probe_timeout_s() -> float:
    configured = _bounded_env("SCOPEFUEL_GROK_TIMEOUT_S", TIMEOUT_S, TIMEOUT_MIN_S, TIMEOUT_MAX_S)
    return max(min(configured, _outer_timer_budget_s()), 0.0)


def _backoff_base_s() -> float:
    return _bounded_env(
        "SCOPEFUEL_GROK_BACKOFF_BASE_S", BACKOFF_BASE_S, BACKOFF_BASE_MIN_S, BACKOFF_BASE_MAX_S
    )


def _backoff_cap_s() -> float:
    return _bounded_env("SCOPEFUEL_GROK_BACKOFF_CAP_S", BACKOFF_CAP_S, BACKOFF_CAP_MIN_S, BACKOFF_CAP_MAX_S)


def _probe_lock_dir() -> pathlib.Path:
    """Per-pool probe lock dir inside scopefuel's cache dir.

    The lock file is an flock on ``probe-locks/grok/.probe.lock`` — the kernel
    releases it when its holder dies, so a dead pid frees the lock and no
    pidfile is trusted on its own. cache_dir() follows SCOPEFUEL_CACHE, so a
    credential-scoped caller (herdr-event) keeps its own lock scope.
    """

    return cache.cache_dir() / "probe-locks" / "grok"


def _probe_backoff_until() -> float:
    """Next allowed probe attempt (epoch) for this pool, or 0 when none is set."""

    state = cache._load_backoff().get("pools", {}).get("grok")
    if not isinstance(state, dict):
        return 0.0
    try:
        return float(state.get("next_allowed_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def _note_probe_timeout(now: float, *, detail: str | None = None) -> float:
    """Persist one probe timeout and return the next allowed attempt (epoch).

    Per-pool state lives in cache.backoff.json — the same file collect() and
    refresh's worker consult before fetching — so every caller path sees the
    window without a second mechanism. consecutive doubles the window
    (base·2^(n-1)) up to the cap; a successful parse clears the entry.
    """

    with cache._exclusive_cache_lock():
        state = cache._load_backoff()
        prev = state["pools"].get("grok")
        consecutive = int(prev.get("consecutive") or 0) + 1 if isinstance(prev, dict) else 1
        until = now + min(_backoff_base_s() * (2 ** (consecutive - 1)), _backoff_cap_s())
        entry = dict(prev) if isinstance(prev, dict) else {}
        entry.update(
            {
                "consecutive": consecutive,
                "next_allowed_at": until,
                "last_timeout_at": now,
                "updated_at": now,
                "last_error": detail or "probe timeout",
                "last_error_kind": "probe_timeout",
            }
        )
        state["pools"]["grok"] = entry
        cache._save_backoff(state)
    return until


def _clear_probe_backoff() -> None:
    """The first successful parse resets the consecutive-timeout count to 0."""

    with cache._exclusive_cache_lock():
        state = cache._load_backoff()
        if state["pools"].pop("grok", None) is not None:
            cache._save_backoff(state)


def _degraded(
    error: str,
    credential: dict[str, bool],
    stdout: str | None = None,
    error_kind: str | None = None,
    hint: str | None = None,
    backoff_until: float | None = None,
    probe: dict | None = None,
) -> ProviderResult:
    return ProviderResult(
        id="grok",
        error=error,
        hint=hint or "grok 를 직접 실행해 /usage 출력이 나오는지 확인하세요",
        source=SOURCE,
        raw=_safe_raw(
            credential=credential,
            stdout=_redact_output(stdout) if stdout else None,
            probe=probe,
        ),
        error_kind=error_kind,
        backoff_until=backoff_until,
    )


# hk 1964 — workdir git init 은 몇 백 밀리초면 끝난다; 상한만 둔다. git 이
# 없거나 init 이 실패해도 프로브는 계속한다 — 신뢰 질문이 뜨면 읽기 루프가
# stage=trust-prompt 로 보고한다.
GIT_INIT_TIMEOUT_S = 10.0


def _ensure_workdir_git_root(workdir: pathlib.Path) -> None:
    """Keep ``workdir`` the root of a git repository — never raises.

    Since grok 1.0.70 the CLI asks its folder-trust question in any folder
    it does not trust, and trust is inherited only within the same git
    repository. The probe's per-run ``probe-*`` instance dirs live under
    PROBE_WORKDIR, so keeping the workdir its own git root lets a single
    desk-side trust entry for it cover every instance dir on that host.
    A workdir nested inside another repository still gets its own ``.git``
    — it must be its own root. This never commits, never touches another
    repository or any git config, and never reads or writes grok's
    trusted_folders file or answers the trust question — that entry stays
    a desk action. A missing git or a failed init only means the question
    may still appear, which the read loop reports as stage=trust-prompt.
    """

    try:
        if (workdir / ".git").exists():
            return
        subprocess.run(  # noqa: S603,S607 - fixed argv; git resolved from PATH
            ["git", "init", "-q"],
            cwd=workdir,
            timeout=GIT_INIT_TIMEOUT_S,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _probe_once() -> str:
    """Run grok in a dedicated process group and send ``/usage`` after its prompt.

    The child runs in its own session, so nothing the parent's own process group
    receives reaches it: if scopefuel is SIGKILLed mid-probe — which is how a
    polling caller's own timeout ends a slow round — the Python cleanup below
    never runs and the grok child is reparented to init and keeps burning CPU.
    That is the 2026-09-23 desktop incident (22 orphans, load 28).

    The four devices that bound it are proctrack's, already proven on the devin
    probe (be0c0a9) and applied here:

    * a per-probe instance directory, flocked, used as the child's cwd — cwd is
      the only identifier that cannot kill an unrelated long-lived worker;
    * a pre-probe sweep of leftovers from probes whose owner has died;
    * pgid + expected-cwd registration, so refresh's timeout handler can reclaim
      the child before ``os._exit``;
    * a detached reaper that watches for the parent's death — the last line of
      defence, and the only one that survives SIGKILL of this process.
    """

    workdir = pathlib.Path(PROBE_WORKDIR).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    _ensure_workdir_git_root(workdir)
    proctrack.kill_stale_probe_leftovers(workdir)
    instance_dir, owner_fd = proctrack.new_probe_dir(workdir)
    timeout_s = _probe_timeout_s()
    master_fd = slave_fd = -1
    process: subprocess.Popen[bytes] | None = None
    reaper: subprocess.Popen[bytes] | None = None
    child_pgid: int | None = None
    try:
        if timeout_s <= 0:
            # 바깥 타이머가 teardown 마진조차 남기지 않았다 — 띄워도 SIGALRM 이
            # 먼저 울려 stage 가 못 나가니, 스폰 자체를 건너뛰고 즉시 보고한다.
            raise ProbeTimeout([BINARY], timeout_s, stage=STAGE_NO_OUTPUT, nbytes=0)
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
        process = subprocess.Popen(  # noqa: S603 - fixed command; binary is explicit/env-configured
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
        reaper = proctrack.spawn_reaper(instance_dir, ttl_s=timeout_s + 90.0)
        os.close(slave_fd)
        slave_fd = -1
        time.sleep(STARTUP_DELAY_S)

        output = bytearray()
        deadline = time.monotonic() + timeout_s
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
                elif not sent and _TRUST_QUESTION.search(clean):
                    # hk 1964: grok 1.0.70 의 폴더 신뢰 질문 — 프로브가 답할 수
                    # 없는 화면이므로 전체 타임아웃을 태우지 않고 즉시 끊는다.
                    # 프롬프트가 함께 보이면 위의 프롬프트 검사가 이긴다.
                    # 아래 finally 가 자식을 오늘과 같이 정리한다.
                    raise ProbeTimeout(
                        [BINARY],
                        timeout_s,
                        stage=STAGE_TRUST_PROMPT,
                        nbytes=len(output),
                    )
                if sent and _WEEKLY.search(clean) and _RESET.search(clean):
                    usage_seen_at = usage_seen_at or time.monotonic()
                    if time.monotonic() - usage_seen_at >= USAGE_SETTLE_S:
                        break
                continue

            if process.poll() is not None:
                break
            # hk 1965: 패널 마지막 청크가 도착한 뒤 CLI 가 조용해지면 readable
            # 분기가 더는 돌지 않는다 — settle 을 여기서도 재검사해야 한다.
            # 그렇지 않으면 완성된 패널을 들고도 데드라인까지 돌아 unparsed
            # 타임아웃이 됐다(desktop/pi 측정: 실패분이 정상보다 ~2.4KB 짧음).
            if usage_seen_at is not None and time.monotonic() - usage_seen_at >= USAGE_SETTLE_S:
                break
            if sent and usage_seen_at is None and time.monotonic() - last_data >= timeout_s:
                break

        if time.monotonic() >= deadline:
            # 데드라인에 닿았어도 패널이 완성돼 있으면 파싱으로 넘긴다 — 로드된
            # 호스트는 조용한 꼬리가 예산을 넘길 수 있다. unparsed 는 패널
            # 단서만 있고 미완성인 진짜 파싱 실패에만 남는다.
            clean = _clean(output.decode("utf-8", errors="replace"))
            if not (sent and _WEEKLY.search(clean) and _RESET.search(clean)):
                # The finally below kills the whole registered process group
                # (SIGTERM, then SIGKILL after a short wait) before the exception
                # leaves — the reaper stays only as the last line of defence for
                # a SIGKILLed parent.
                raise ProbeTimeout(
                    [BINARY],
                    timeout_s,
                    stage=_timeout_stage(output, sent=sent, usage_seen_at=usage_seen_at),
                    nbytes=len(output),
                )
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
                if process_group is None:
                    with contextlib.suppress(OSError):
                        process.kill()
                # A wait() that times out here used to escape the finally block,
                # skipping every cleanup line below it — including closing the
                # pty — and replacing the real TimeoutExpired with its own.
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    process.wait(timeout=2.0)
            # The leader obeying SIGTERM proves nothing about group members
            # that ignore it — a straggler keeps the pgid alive, so the group
            # SIGKILL is unconditional; ESRCH just means it was already gone.
            if process_group is not None:
                with contextlib.suppress(OSError):
                    os.killpg(process_group, signal.SIGKILL)
                # Reap the leader first so its zombie cannot pin the pgid,
                # then wait on the pgid itself: stragglers are not our
                # children and cannot be wait()ed, but SIGKILL cannot be caught.
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    process.wait(timeout=2.0)
                _wait_group_gone(process_group)
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


def _wait_group_gone(process_group: int, *, timeout_s: float = 2.0) -> None:
    """Wait until the probe's own process group is empty after its SIGKILL.

    Stray descendants are not this process's children, so they cannot be
    wait()ed — poll the pgid instead. SIGKILL cannot be caught, so a live
    member disappears in milliseconds; ESRCH ends the wait.
    """

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            os.killpg(process_group, 0)
        except OSError:
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(0.02)


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name == "HERDR" or name.startswith("HERDR_"):
            del env[name]
    env["COLUMNS"] = str(PTY_COLS)
    env["LINES"] = str(PTY_ROWS)
    return env


def _timeout_stage(output: bytearray, *, sent: bool, usage_seen_at: float | None) -> str:
    """Classify how far a timed-out probe got — a stage name, never screen text.

    "Panel seen but not parsed" covers two real shapes: the full _WEEKLY+_RESET
    pair matched but the deadline hit before USAGE_SETTLE_S elapsed, and a
    fragment of the panel (either regex) matched without ever completing —
    e.g. a CLI update that renames the reset line.
    """

    if not output:
        return STAGE_NO_OUTPUT
    if not sent:
        clean = _clean(output.decode("utf-8", errors="replace"))
        if _TRUST_QUESTION.search(clean):
            return STAGE_TRUST_PROMPT
        return STAGE_NO_PROMPT
    if usage_seen_at is not None:
        return STAGE_UNPARSED
    clean = _clean(output.decode("utf-8", errors="replace"))
    if _WEEKLY.search(clean) or _RESET.search(clean):
        return STAGE_UNPARSED
    return STAGE_NO_PANEL


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
        note="PTY /usage 주간 한도; Next reset은 CLI가 호스트 로컬 타임존으로 렌더한 시각",
        source=SOURCE,
    )


def _reset_iso(match: re.Match[str], *, now: dt.datetime | None = None) -> str:
    """Read the panel reset in the probe host's local zone (hk 1965).

    grok renders the reset in the host's TZ — oci (TZ=UTC) shows "October 29,
    19:58" where a KST host shows "October 30, 04:58" for the same instant.
    A fixed KST frame put oci's reset 9 hours early. The naive candidate is
    converted with astimezone(), which takes the local zone at that date, so
    DST hosts still land on the right instant.
    """
    if now is None:
        current = dt.datetime.now()
    elif now.tzinfo is None:
        current = now
    else:
        current = now.astimezone().replace(tzinfo=None)
    month = dt.datetime.strptime(match["month"], "%B").month
    candidate = dt.datetime(current.year, month, int(match["day"]), int(match["hour"]), int(match["minute"]))
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
