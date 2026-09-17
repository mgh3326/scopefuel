"""Devin CLI — 기동 배너의 daily quota와 `devin models list`의 SWE-2 Free 태그를 읽는다.

두 소스는 서로 다른 신호다:

- 기동 배너(PTY 프로브, 입력 없음)는 일간(daily) 쿼타를 직접 렌더한다
  (``v3000.10.21 · Pro · 100% remaining (resets in 1h 41m)``). 배너는 두 번
  그려진다 — 첫 페인트는 버전만, 몇 초 뒤 커서이동+화면클리어 후 같은 줄이
  쿼타까지 포함해 다시 그려진다. 그 두 번째 페인트를 기다린다.
- ``devin models list`` 는 SWE-2 패밀리 행에 Free 태그가 있을 때만 별도
  account 버킷을 낸다(과금 모델은 이 provider 범위 밖).

주간(weekly) 쿼타는 배너에도 models list 에도 없다 — Devin 웹 콘솔 전용이다.
못 읽는 축은 0%/100% 로 추정하지 않고 ``used_pct=None`` 으로 명시한다
(fail-closed). Devin credential/config 파일은 열지 않고, 로그인·복구도 시도하지 않는다.
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

BINARY = os.environ.get("SCOPEFUEL_DEVIN_BIN") or "devin"
TIMEOUT_S = 30.0
BANNER_SETTLE_S = 0.5
SOURCE = "cli:models list"
SOURCE_BANNER = "cli:banner"
FREE_NOTE = "free until ~2026-10-10"
WEEKLY_UNKNOWN_NOTE = "배너에 없음 — Devin 웹 콘솔에서만 확인"
PROVIDER_ID = "devin"
PROBE_WORKDIR = Path.home() / ".local" / "share" / "scopefuel" / "devin-probe-workdir"
# A/B 실측(grok/kimi): 크기 미설정 PTY에서는 TUI가 배너/패널을 렌더하지 않아 timeout한다.
PTY_ROWS = 50
PTY_COLS = 200

_ANSI = re.compile(r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*(?:\x07|\x1B\\)|\([0-2])")
# `SWE-2 (swe-2)` 패밀리 헤더. SWE-1.7 / Lightning / Fusion 제목은 제외.
_SWE2_FAMILY = re.compile(r"^SWE-2\s+\(swe-2\)\s*$")
_FAMILY_HEADER = re.compile(r"^\S.*\([^)]+\)\s*$")
_BRACKET_TAGS = re.compile(r"\[([^\[\]]*)\]\s*$")
# 기동 배너 두 번째 페인트: "v3000.10.21 · Pro · 100% remaining (resets in 1h 41m)".
# 구분자는 U+00B7 middle dot. plan 문자셋은 "·" 를 포함하지 않아 백트래킹으로
# 다음 "·" 앞에서 자연히 멈춘다.
_BANNER_QUOTA = re.compile(
    r"· (?P<plan>[A-Za-z][A-Za-z0-9+ .-]*) · "
    r"(?P<remaining>\d+(?:\.\d+)?)\s*% remaining \(resets in (?P<reset>[^)]+)\)"
)
_DURATION_PART = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[dhms])", re.IGNORECASE)


def fetch() -> ProviderResult:
    """기동 배너(daily) + models list(SWE-2 Free)를 합성한다.

    배너가 실패해도 models list 가 성공하면 fail-closed 로 weekly-None 버킷을
    붙여 반환한다(추정 금지). 둘 다 실패하면 error.
    """
    if shutil.which(BINARY) is None:
        return _failed(
            f"{BINARY} 실행 파일 없음",
            hint="Devin CLI 설치 후 다시 시도 (SCOPEFUEL_DEVIN_BIN 으로 경로 지정 가능)",
        )

    banner = _banner_result()
    models = _fetch_models_list()

    if banner.error is None:
        buckets = list(banner.buckets)
        source = SOURCE_BANNER
        if models.error is None:
            buckets = buckets + models.buckets
            source = f"{SOURCE_BANNER}+{SOURCE}"
        return ProviderResult(
            id=PROVIDER_ID,
            plan=banner.plan,
            buckets=buckets,
            note=banner.note,
            source=source,
            raw={"banner": banner.raw, "models_list": models.raw},
            pool_class="spend",
        )

    if models.error is None:
        return ProviderResult(
            id=PROVIDER_ID,
            plan=models.plan,
            buckets=[*models.buckets, _weekly_unknown_bucket()],
            note=models.note,
            warning=banner.error,
            source=models.source,
            raw=models.raw,
            pool_class="spend",
        )

    return models


def _fetch_models_list() -> ProviderResult:
    try:
        proc = subprocess.run(  # noqa: S603 - 사용자 PATH 의 devin, 인자는 고정
            [BINARY, "models", "list"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        return _failed(
            f"{BINARY} models list 가 {TIMEOUT_S:.0f}초 안에 끝나지 않음",
            hint="devin 을 직접 실행해 models list 가 나오는지 확인하세요",
        )
    except OSError as exc:
        return _failed(f"{BINARY} 실행 실패: {exc}")

    if proc.returncode != 0:
        return _failed(
            f"{BINARY} models list 종료코드 {proc.returncode}",
            hint="devin 을 직접 실행해 로그인/네트워크 상태를 확인하세요",
            stdout=_clean(proc.stdout + proc.stderr),
        )
    return parse(proc.stdout + proc.stderr)


def _banner_result() -> ProviderResult:
    try:
        raw = _probe_banner()
    except subprocess.TimeoutExpired:
        return ProviderResult(
            id=PROVIDER_ID,
            error=f"{BINARY} 기동 배너가 {TIMEOUT_S:.0f}초 안에 쿼타 줄을 그리지 않음",
            hint="devin 을 직접 실행해 기동 배너에 쿼타 줄이 그려지는지 확인하세요",
            source=SOURCE_BANNER,
            pool_class="spend",
        )
    except OSError as exc:
        return ProviderResult(
            id=PROVIDER_ID,
            error=f"{BINARY} 배너 프로브 실행 실패: {exc}",
            source=SOURCE_BANNER,
            pool_class="spend",
        )
    return parse_banner(raw)


def _probe_banner() -> str:
    """Run devin's startup banner in a PTY. 입력은 보내지 않는다 — 배너가 자기가 갱신한다.

    자식은 전용 세션(start_new_session)에 두므로 부모의 finally killpg 외의
    경로로 나가면 고아가 된다. 그 경로들을 막는 장치:

    - 프로브마다 workdir 아래 고유 인스턴스 디렉터리를 만들고 디렉터리
      자체에 flock 을 쥔 뒤 그것을 자식 cwd 로 쓴다. 시작 전 스윕은 workdir
      루트의 잔존자와 락이 풀린(=주인이 죽은) 인스턴스·pending 디렉터리
      안만 정리한다 — 락이 잡힌 살아있는 동시 프로브의 디렉터리는 건너뛰고,
      락 없는 젊은 pending(기동 유예 안)도 건너뛴다(프로세스 판별자는 cwd
      뿐 — 나이·CPU 로 고르면 장수 정상 워커를 죽인다).
    - 자식 pgid 와 기대 cwd 를 proctrack 에 등록해 refresh 타임아웃 핸들러가
      os._exit 전에 회수한다 — killpg 대신 그룹원 각각의 cwd 를 신호 직전에
      재확인해, 기대 디렉터리 안에 있는 구성원에만 신호한다.
    - 부모 사망을 감시하는 분리 리퍼가 자기 인스턴스 디렉터리만 스윕한다 —
      부모가 SIGKILL/SIGHUP 로 죽어 파이썬 정리 경로가 전혀 못 돌 때의
      최종 방어선이며, 다른 프로브의 디렉터리는 모른다.
    """

    probe_workdir = Path(PROBE_WORKDIR).expanduser()
    probe_workdir.mkdir(parents=True, exist_ok=True)
    proctrack.kill_stale_probe_leftovers(probe_workdir)
    instance_dir, owner_fd = proctrack.new_probe_dir(probe_workdir)
    master_fd = slave_fd = -1
    process: subprocess.Popen[bytes] | None = None
    reaper: subprocess.Popen[bytes] | None = None
    child_pgid: int | None = None
    try:
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
        process = subprocess.Popen(  # noqa: S603 - fixed command/argv; binary is explicit/env-configured
            [BINARY, "--respect-workspace-trust", "false"],
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

        output = bytearray()
        deadline = time.monotonic() + TIMEOUT_S
        banner_seen_at: float | None = None
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
                clean = _clean(output.decode("utf-8", errors="replace"))
                if banner_seen_at is None and _BANNER_QUOTA.search(clean):
                    banner_seen_at = time.monotonic()
                if banner_seen_at is not None and time.monotonic() - banner_seen_at >= BANNER_SETTLE_S:
                    break
                continue

            if process.poll() is not None:
                break
            if banner_seen_at is not None and time.monotonic() - banner_seen_at >= BANNER_SETTLE_S:
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
            os.close(slave_fd)
        with contextlib.suppress(OSError):
            os.close(master_fd)


def parse_banner(text: str) -> ProviderResult:
    """기동 배너의 daily quota 세그먼트를 파싱한다. 못 읽으면 fail-closed(error)."""
    clean = _clean(text)
    match = _BANNER_QUOTA.search(clean)
    if match is None:
        return ProviderResult(
            id=PROVIDER_ID,
            error="기동 배너에서 쿼타 세그먼트를 찾지 못함(형식 불일치 또는 미출현)",
            hint="devin 을 직접 실행해 배너가 두 번째 페인트까지 그려지는지 확인하세요",
            source=SOURCE_BANNER,
            raw={"stdout": clean},
            pool_class="spend",
        )

    try:
        remaining = float(match["remaining"])
    except (TypeError, ValueError):
        remaining = None
    if remaining is None or not 0 <= remaining <= 100:
        return ProviderResult(
            id=PROVIDER_ID,
            error="기동 배너의 remaining 값이 유효 범위를 벗어남",
            source=SOURCE_BANNER,
            raw={"stdout": clean},
            pool_class="spend",
        )

    plan = match["plan"].strip()
    reset_duration = match["reset"].strip()
    used = round(100.0 - remaining, 1)
    daily = Bucket(
        label="daily",
        window="1d",
        used_pct=used,
        resets_at=_reset_iso(reset_duration),
        scope=Scope("account"),
        horizon="now",
        note=f"remaining {remaining:g}%; resets in {reset_duration}",
    )
    return ProviderResult(
        id=PROVIDER_ID,
        plan=plan,
        buckets=[daily, _weekly_unknown_bucket()],
        note=f"기동 배너 daily quota (plan={plan})",
        source=SOURCE_BANNER,
        raw={"stdout": clean},
        pool_class="spend",
    )


def _weekly_unknown_bucket() -> Bucket:
    return Bucket(
        label="weekly",
        window="7d",
        used_pct=None,
        resets_at=None,
        scope=Scope("account"),
        horizon="week",
        note=WEEKLY_UNKNOWN_NOTE,
    )


def parse(text: str) -> ProviderResult:
    """SWE-2 패밀리의 Free 태그만 인정한다. 못 읽으면 0% 를 지어내지 않는다."""
    clean = _clean(text)
    block = _swe2_family_block(clean)
    if block is None:
        return _failed(
            "models list 에서 SWE-2 행을 찾지 못함",
            hint="devin models list 출력에 SWE-2 (swe-2) 패밀리가 있는지 확인하세요",
            stdout=clean,
        )

    free_rows = [line for line in _model_rows(block) if _has_free_tag(line)]
    if not free_rows:
        return _failed(
            "SWE-2 행에 Free 태그가 없음",
            hint="유료 SWE-2 는 이 provider 범위 밖이다 — 추정 used_pct 를 넣지 않는다",
            stdout=clean,
        )

    return ProviderResult(
        id=PROVIDER_ID,
        buckets=[
            Bucket(
                label="swe-2",
                window="30d",
                used_pct=0.0,
                resets_at=None,
                scope=Scope("account"),
                horizon="week",
                note=FREE_NOTE,
            )
        ],
        note=FREE_NOTE,
        source=SOURCE,
        raw={"stdout": clean},
        pool_class="spend",
    )


def _swe2_family_block(clean: str) -> str | None:
    lines = clean.splitlines()
    start = None
    for index, line in enumerate(lines):
        if _SWE2_FAMILY.match(line):
            start = index
            break
    if start is None:
        return None
    collected = [lines[start]]
    for line in lines[start + 1 :]:
        if line and not line[:1].isspace() and _FAMILY_HEADER.match(line):
            break
        collected.append(line)
    return "\n".join(collected)


def _model_rows(block: str) -> list[str]:
    rows: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or _SWE2_FAMILY.match(stripped):
            continue
        if stripped.lower().startswith("aliases:"):
            continue
        if line[:1].isspace():
            rows.append(line)
    return rows


def _has_free_tag(line: str) -> bool:
    match = _BRACKET_TAGS.search(line.rstrip())
    if match is None:
        return False
    return any(part.strip() == "Free" for part in match.group(1).split(","))


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


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name == "HERDR" or name.startswith("HERDR_"):
            del env[name]
    env["COLUMNS"] = str(PTY_COLS)
    env["LINES"] = str(PTY_ROWS)
    env["TERM"] = "xterm-256color"
    return env


def _failed(error: str, *, hint: str | None = None, stdout: str | None = None) -> ProviderResult:
    raw = {"stdout": stdout} if stdout else None
    return ProviderResult(
        id=PROVIDER_ID,
        error=error,
        hint=hint,
        source=SOURCE,
        raw=raw,
        pool_class="spend",
    )
