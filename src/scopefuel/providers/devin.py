"""Devin CLI — PTY 세션 하나로 기동 배너(weekly 잔여)와 ``/usage``(daily·weekly)를 읽는다.

실측(2026-10-10, v3000.11.3)으로 다시 확인한 창 매핑: 기동 배너의
``Pro · N% remaining (resets in ...)`` 퍼센트는 ``/usage`` 의 **Weekly** 축이다.
배너의 리셋이 24h 를 넘어 그려지는 것(실측 ``resets in 1d 2h``)으로도 daily 창
(≤24h 안에 리셋)일 수 없음이 확인되고, 같은 회차 ``/usage`` Weekly 사용률과
``100 - N`` 이 일치했다. 옛 구현은 이 줄을 daily 로 잘못 붙이고 weekly 를
"CLI 에서 못 읽는다"고 둬, desk 가 ``일 18% / 월 0%`` 와 ``/usage`` 의
``Daily 0% / Weekly 18%`` 불일치를 봤다 — ``월`` 은 SWE-2 Free 버킷의 30d
창이 만든 가짜 쿼타 축이었다(쿼타가 아니라 모델 Free 태그 → model scope).

- 배너는 두 번 그려진다 — 첫 페인트는 버전과 composer placeholder, 몇 초 뒤
  커서이동+화면클리어 후 쿼타 상태줄이 그려지고 입력창도 다시 그려진다.
  입력은 ①쿼타 상태줄이 확인되고 ②그 상태줄 이후에 composer placeholder
  (``Ask Devin to build …``)가 다시 확인되며 ③상태줄 뒤 화면에 chevron 옵션
  목록(``❯ Yes, update`` 류)이나 ``?`` 질문 프롬프트가 없을 때만 보낸다 —
  첫 페인트의 입력창만 보고 치면 아직 쿼타 조회 전(placeholder 만으로는
  부족), 상태줄만 보고 치면 그 위의 업데이트·로그인 모달에 Enter 가
  들어간다(상태줄만으로도 부족). 어느 조건이든 빠지면 deadline 까지
  아무것도 치지 않고 끝낸다(fail-closed).
  ``/usage`` 를 보내고 축이 읽히면 ``/exit`` 로 끝낸다. 보내는 입력은 이
  둘뿐이다 — 쿼타를 쓰는 프롬프트는 절대 보내지 않는다.
- ``devin models list`` 는 SWE-2 패밀리 행에 Free 태그가 있을 때 model-scope
  버킷을 낸다(계정 쿼타 창이 아니므로 창 표시·게이트 축에서 제외).

못 읽는 축은 0%/100% 로 추정하지 않고 ``used_pct=None`` 으로 명시한다
(fail-closed): ``/usage`` 를 못 읽으면 배너가 증명한 축만 쓰고 나머지는 None.
배너와 ``/usage`` 가 같은 창에서 어긋나면 ``/usage`` 가 이기고 note 에 적는다.
Devin credential/config 파일은 열지 않고, 로그인·복구도 시도하지 않는다.
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
from ..model import PROBE_IN_PROGRESS, Bucket, ProviderResult, Scope

BINARY = os.environ.get("SCOPEFUEL_DEVIN_BIN") or "devin"
TIMEOUT_S = 30.0
BANNER_SETTLE_S = 0.5
USAGE_WAIT_S = 15.0
USAGE_SETTLE_S = 1.0
EXIT_WAIT_S = 3.0
# 명령 텍스트와 제출(Enter)을 나눠 보낸다 — 한 write 에 붙여 보내면 TUI 의
# 슬래시 팔레트가 뜨기 전에 Enter 가 처리돼 명령이 실행되지 않는다(실측:
# ``/usage`` 가 입력창에 찍히고만 끝남). TUI 가 focus reporting(?1004h)을
# 켜면 진짜 터미널처럼 FocusGained 를 한 번 보낸다 — 포커스 없는 입력을
# 무시하는 TUI 대비.
USAGE_INPUT = "/usage"
EXIT_INPUT = "/exit"
SUBMIT_INPUT = "\r"
FOCUS_IN = "\x1b[I"
FOCUS_REPORT_SEQ = "\x1b[?1004h"
USAGE_TYPE_SETTLE_S = 0.6
EXIT_TYPE_SETTLE_S = 0.4
SOURCE = "cli:models list"
SOURCE_QUOTA = "cli:banner+/usage"
FREE_NOTE = "free until ~2026-10-10"
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
# 기동 배너 두 번째 페인트의 구분자는 U+00B7 middle dot.
_BANNER_VERSION = r"v\d+\.\d+\.\d+"
_BANNER_PLAN = r"Pro"
_BANNER_REMAINING = r"(?P<remaining>\d+(?:\.\d+)?)[ \t]*% remaining"
_BANNER_RESET = r"(?P<reset>\d+(?:\.\d+)?[ \t]*[dhms](?:[ \t]+\d+(?:\.\d+)?[ \t]*[dhms])*)"
# 3000.10.31 and earlier kept the version, plan, and quota on one rendered line.
_BANNER_QUOTA_OLD = re.compile(
    rf"(?m)(?<!\S){_BANNER_VERSION}[ \t]+·[ \t]+(?P<plan>{_BANNER_PLAN})[ \t]+· "
    rf"{_BANNER_REMAINING} \(resets in {_BANNER_RESET}\)(?=[ \t]*(?:$|\n))"
)
# 3000.11.1 renders the version in the first paint and refreshes this complete
# plan/quota line separately. Keep the line boundary so unrelated percentages
# elsewhere in CLI output cannot complete the PTY probe.
_BANNER_QUOTA_NEW = re.compile(
    rf"(?m)^[ \t]*(?P<plan>{_BANNER_PLAN})[ \t]+·[ \t]+{_BANNER_REMAINING} "
    rf"\(resets in {_BANNER_RESET}\)[ \t]*$"
)
_DURATION_PART = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[dhms])", re.IGNORECASE)
# /usage 축 행은 물리 줄 단위다 — ``Daily``·``Weekly`` 레이블이 선행하는 그 줄
# 안에서만 퍼센트·리셋을 읽는다. 다른 줄(상태줄 리페인트·안내 문구 등)의 값은
# 어느 축에도 새어 들어오지 않는다.
_USAGE_ROW = re.compile(r"^[ \t]*(?P<axis>daily|weekly)\b", re.IGNORECASE)
# ``N% used`` 의 N 은 구분자 없는 숫자만 인정한다 — ``1,000% used``(천단위
# 구분), ``-5% used``(부호), ``N% remaining``(키워드 없음) 같은 형태는 사용률로
# 읽지 않는다. 숫자 앞에 [\d,.-] 가 붙어 있으면 거절.
_USAGE_USED = re.compile(r"(?<![\d,.\-])(?P<used>\d+(?:\.\d+)?)[ \t]*%[ \t]*used\b", re.IGNORECASE)
# 입력창이 그려졌다는 마커는 프롬프트 chevron 이 아니라 composer placeholder
# 다 — 실측 캡처(v3000.11.3)의 입력창 줄:
#   ``❭ Ask Devin to build features, fix bugs, or work on your code``
# ``❯`` 만으로 시작하는 줄은 로그인·업데이트 프롬프트의 옵션 목록일 수 있으므로
# 입력을 여는 근거로 쓰지 않는다(fail-closed).
_INPUT_READY = re.compile(r"(?m)^[ \t]*[❭❯][ \t]*Ask Devin to build")
# 상태줄 이후 화면의 chevron 옵션 목록(``❯ Yes, update`` 류)이나 ``?`` 로
# 시작하는 질문 프롬프트 — 그 화면은 composer 가 아니라 모달이므로 Enter 가
# 선택지를 누른다. composer placeholder 줄 자체는 옵션이 아니라서 제외한다.
_MODAL_OR_OPTION = re.compile(r"(?m)^[ \t]*(?:\?|[❭❯›][ \t]*(?!Ask Devin to build)\S)")
_USAGE_RESET = re.compile(
    r"resets?\s+in\s+(?P<reset>\d+(?:\.\d+)?[ \t]*[dhms](?:[ \t]+\d+(?:\.\d+)?[ \t]*[dhms])*)",
    re.IGNORECASE,
)
# /usage Weekly 는 상대 기간이 아니라 절대 시각으로 그린다:
# ``resets Oct 11, 5:00 PM (UTC+9)`` (실측 v3000.11.3). 연도는 안 적으므로 현재 연도.
_USAGE_RESET_ABS = re.compile(
    r"resets?\s+(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),[ \t]*"
    r"(?P<hm>\d{1,2}:\d{2})[ \t]*(?P<ampm>[AP]M)[ \t]*\(UTC(?P<tz>[+-]\d+(?::\d+)?)\)",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
# 연도 없는 절대 리셋의 미래쪽 지평 — 축 창 길이보다 먼 리셋은 그 창의 시각이
# 아니므로(weekly >8d, daily >25h) 후보에서 버린다.
_ABS_RESET_HORIZON = {"daily": dt.timedelta(hours=25), "weekly": dt.timedelta(days=8)}


def fetch() -> ProviderResult:
    """PTY 세션(daily·weekly 쿼타) + models list(SWE-2 Free 태그)를 합성한다.

    쿼타 프로브가 실패해도 models list 가 성공하면 fail-closed 로 daily·weekly
    used_pct=None 버킷을 붙여 반환한다(추정 금지). 둘 다 실패하면 error.
    """
    if shutil.which(BINARY) is None:
        return _failed(
            f"{BINARY} 실행 파일 없음",
            hint="Devin CLI 설치 후 다시 시도 (SCOPEFUEL_DEVIN_BIN 으로 경로 지정 가능)",
        )

    workdir = Path(PROBE_WORKDIR).expanduser()
    proctrack.log_probe_call(workdir, PROVIDER_ID)
    try:
        with proctrack.single_probe_lock(workdir) as acquired:
            if not acquired:
                # Not an error: another probe is already measuring this pool.
                return _failed(
                    f"{BINARY} 탐침이 이미 실행 중 — 이번 회차 건너뜀",
                    error_kind=PROBE_IN_PROGRESS,
                )
            quota = _quota_result()
            models = _fetch_models_list()
    except OSError as exc:
        return _failed(f"{BINARY} 실행 실패: {exc}")

    if quota.error is None:
        buckets = list(quota.buckets)
        source = SOURCE_QUOTA
        if models.error is None:
            buckets = buckets + models.buckets
            source = f"{SOURCE_QUOTA}+{SOURCE}"
        return ProviderResult(
            id=PROVIDER_ID,
            plan=quota.plan,
            buckets=buckets,
            note=quota.note,
            source=source,
            raw={"pty": quota.raw, "models_list": models.raw},
            pool_class="spend",
        )

    if models.error is None:
        return ProviderResult(
            id=PROVIDER_ID,
            plan=models.plan,
            buckets=[*models.buckets, *_unknown_quota_buckets()],
            note=models.note,
            warning=quota.error,
            source=models.source,
            raw=models.raw,
            pool_class="spend",
        )

    return models


def _fetch_models_list() -> ProviderResult:
    """`devin models list` — subprocess.run 의 timeout 은 직접 자식만 죽인다.

    자식이 백그라운드로 남긴 손자나, 부모가 SIGKILL 당했을 때의 자식 본인은
    살아남는다 — 2026-09-23 사고와 같은 모양으로, 실제로 재현됐다(#608
    검증 B2). 그래서 배너 프로브와 같은 proctrack 장치를 쓴다: 인스턴스
    디렉터리 cwd, 전용 세션, pgid 등록, 분리 리퍼.
    """

    workdir = Path(PROBE_WORKDIR).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    proctrack.kill_stale_probe_leftovers(workdir)
    instance_dir, owner_fd = proctrack.new_probe_dir(workdir)
    process: subprocess.Popen[str] | None = None
    reaper: subprocess.Popen[bytes] | None = None
    child_pgid: int | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - 사용자 PATH 의 devin, 인자는 고정
            [BINARY, "models", "list"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=instance_dir,
            close_fds=True,
            start_new_session=True,
            env=_child_env(),
        )
        with contextlib.suppress(OSError):
            child_pgid = os.getpgid(process.pid)
        if child_pgid is not None:
            proctrack.register(child_pgid, instance_dir)
        reaper = proctrack.spawn_reaper(instance_dir, ttl_s=TIMEOUT_S + 90.0)
        try:
            stdout, stderr = process.communicate(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return _failed(
                f"{BINARY} models list 가 {TIMEOUT_S:.0f}초 안에 끝나지 않음",
                hint="devin 을 직접 실행해 models list 가 나오는지 확인하세요",
            )
        if process.returncode != 0:
            return _failed(
                f"{BINARY} models list 종료코드 {process.returncode}",
                hint="devin 을 직접 실행해 로그인/네트워크 상태를 확인하세요",
                stdout=_clean(stdout + stderr),
            )
        return parse(stdout + stderr)
    except OSError as exc:
        return _failed(f"{BINARY} 실행 실패: {exc}")
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
                # A wait() that times out here must not escape the finally
                # block — every cleanup line below it still has to run.
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    process.wait(timeout=2.0)
        # The direct child exiting is not the end of the probe's descendants:
        # a CLI that backgrounds a helper and returns 0 leaves that helper
        # running. Sweep the instance directory unconditionally, by cwd,
        # before it is removed — once it is gone proctrack has no cwd left
        # to recognise them by.
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


def _quota_result() -> ProviderResult:
    try:
        raw = _probe_session()
    except subprocess.TimeoutExpired:
        return ProviderResult(
            id=PROVIDER_ID,
            error=f"{BINARY} PTY 세션이 {TIMEOUT_S:.0f}초 안에 쿼타 줄을 그리지 않음",
            hint="devin 을 직접 실행해 기동 배너와 /usage 가 그려지는지 확인하세요",
            source=SOURCE_QUOTA,
            pool_class="spend",
        )
    except OSError as exc:
        return ProviderResult(
            id=PROVIDER_ID,
            error=f"{BINARY} PTY 프로브 실행 실패: {exc}",
            source=SOURCE_QUOTA,
            pool_class="spend",
        )
    return parse_session(raw)


def _probe_session() -> str:
    """Run devin in a PTY: 기동 배너 → ``/usage`` → ``/exit``. 다른 입력은 없다.

    자식은 전용 세션(start_new_session)에 두므로 부모의 finally killpg 외의
    경로로 나가면 고아가 된다. 그 경로들을 막는 장치:

    - 프로브마다 workdir 아래 고유 인스턴스 디렉터리를 만들고 디렉터리
      자체에 flock 을 쥔 뒤 그것을 자식 cwd 로 쓴다. 생성자는 workdir 의
      .sweep.lock 을 쥔 채 mkdtemp→flock→rename 을 지나고, 시작 전 스윕은
      같은 락 아래서만 디렉터리를 열거한다 — 락 없는 pending 은 스윕에
      보이지 않으므로 생사를 나이로 추정할 일이 없다. 스윕은 workdir 루트의
      잔존자와 락이 풀린(=주인이 죽은) 인스턴스·pending 디렉터리 안만
      정리한다 — 락이 잡힌 살아있는 동시 프로브의 디렉터리는 건너뛴다
      (프로세스 판별자는 cwd 뿐 — 나이·CPU 로 고르면 장수 정상 워커를 죽인다).
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
        ready_at: float | None = None
        focus_sent = False
        usage_typed_at: float | None = None
        usage_sent_at: float | None = None
        usage_seen_at: float | None = None
        exit_typed_at: float | None = None
        exit_sent_at: float | None = None
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
            now = time.monotonic()
            raw = output.decode("utf-8", errors="replace")
            clean = _clean(raw)
            if banner_seen_at is None and _banner_quota_match(clean) is not None:
                banner_seen_at = now
            if _input_gate_open(clean):
                if ready_at is None:
                    ready_at = now
            else:
                ready_at = None
            if not focus_sent and FOCUS_REPORT_SEQ in raw:
                with contextlib.suppress(OSError):
                    os.write(master_fd, FOCUS_IN.encode())
                focus_sent = True
            # 입력은 쿼타 상태줄 + 그 이후에 다시 확인된 composer placeholder +
            # 상태줄 뒤 화면에 모달(chevron 옵션·질문 프롬프트) 부재 — 세 조건이
            # 모두 갖춰진 뒤에만 보낸다. 조건이 무너지면 ready_at 을 되돌려,
            # 상태줄 위에 그려진 업데이트 모달 같은 화면에는 절대 치지 않는다.
            # 조건은 출력 도착과 무관하게 매 반복 평가한다 — TUI 가 침묵하는
            # 사이에는 readable 이 비어 입력 시점을 영원히 못 잡는다.
            if usage_typed_at is None and ready_at is not None and now - ready_at >= BANNER_SETTLE_S:
                with contextlib.suppress(OSError):
                    os.write(master_fd, USAGE_INPUT.encode())
                usage_typed_at = now
            if (
                usage_typed_at is not None
                and usage_sent_at is None
                and now - usage_typed_at >= USAGE_TYPE_SETTLE_S
            ):
                with contextlib.suppress(OSError):
                    os.write(master_fd, SUBMIT_INPUT.encode())
                usage_sent_at = now
            if usage_sent_at is not None and usage_seen_at is None and _usage_pct_seen(clean):
                usage_seen_at = now
            usage_done = usage_sent_at is not None and (
                (usage_seen_at is not None and now - usage_seen_at >= USAGE_SETTLE_S)
                or now - usage_sent_at >= USAGE_WAIT_S
            )
            if usage_done and exit_typed_at is None:
                with contextlib.suppress(OSError):
                    os.write(master_fd, EXIT_INPUT.encode())
                exit_typed_at = now
            if (
                exit_typed_at is not None
                and exit_sent_at is None
                and now - exit_typed_at >= EXIT_TYPE_SETTLE_S
            ):
                with contextlib.suppress(OSError):
                    os.write(master_fd, SUBMIT_INPUT.encode())
                exit_sent_at = now
            if exit_sent_at is not None and (process.poll() is not None or now - exit_sent_at >= EXIT_WAIT_S):
                break
            if not readable and process.poll() is not None:
                break

        if time.monotonic() >= deadline and banner_seen_at is None and usage_seen_at is None:
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
                # A wait() that times out here must not escape the finally
                # block — every cleanup line below it still has to run.
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    process.wait(timeout=2.0)
        # The direct child exiting is not the end of the probe's descendants:
        # a CLI that backgrounds a helper and returns 0 leaves that helper
        # running. Sweep the instance directory unconditionally, by cwd,
        # before it is removed — once it is gone proctrack has no cwd left
        # to recognise them by.
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
            os.close(slave_fd)
        with contextlib.suppress(OSError):
            os.close(master_fd)


def parse_session(text: str, now: dt.datetime | None = None) -> ProviderResult:
    """PTY 세션 출력을 daily·weekly 버킷으로 파싱한다. 읽힌 축만 쓴다(fail-closed).

    ``now`` 를 주면 상대·절대 리셋을 그 시각 기준으로 해석한다 — 기본은 실제
    시계다(테스트는 반드시 고정해야 연도 없는 절대 리셋이 미끄러지지 않는다).

    - 기동 배너 ``Pro · N% remaining`` 은 실측상 **weekly** 잔여다(리셋이 24h
      를 넘어 그려진다 — daily 창은 24h 안에 리셋된다).
    - ``/usage`` 의 ``Daily``·``Weekly`` 행이 각 축의 사용률·리셋을 준다.
    - 같은 창에서 배너와 ``/usage`` 가 어긋나면 ``/usage`` 가 이기고 note 에 적는다.
    - 어느 소스도 증명하지 못한 축은 ``used_pct=None`` — 0·100 추정 금지.
    """
    clean = _clean(text)
    banner = _banner_quota_match(clean)
    axes = _usage_axes(clean, now=now)
    usage_daily = axes.get("daily", {})
    usage_weekly = axes.get("weekly", {})

    banner_weekly_used: float | None = None
    banner_weekly_reset: str | None = None
    plan: str | None = None
    if banner is not None:
        plan = banner["plan"].strip()
        banner_weekly_used = round(100.0 - float(banner["remaining"]), 1)
        banner_weekly_reset = banner["reset"].strip()

    daily_used = usage_daily.get("used_pct")
    daily_reset_at = usage_daily.get("resets_at")
    weekly_used = usage_weekly.get("used_pct")
    weekly_reset_at = usage_weekly.get("resets_at") or _reset_iso(banner_weekly_reset, now=now)

    notes: list[str] = []
    if banner is not None:
        notes.append(f"배너 weekly remaining {100.0 - banner_weekly_used:g}%")
    else:
        notes.append("기동 배너 쿼타 줄 미출현")
    if weekly_used is not None:
        if banner_weekly_used is not None and abs(banner_weekly_used - weekly_used) > 0.05:
            notes.append(
                f"배너 weekly {banner_weekly_used:g}% 와 /usage {weekly_used:g}% 불일치 — /usage 우선"
            )
    else:
        weekly_used = banner_weekly_used
    if daily_used is None and weekly_used is None:
        return ProviderResult(
            id=PROVIDER_ID,
            error="PTY 세션에서 daily/weekly 쿼타를 읽지 못함(배너·/usage 모두 형식 불일치 또는 미출현)",
            hint="devin 을 직접 실행해 기동 배너와 /usage 출력이 그려지는지 확인하세요",
            source=SOURCE_QUOTA,
            raw={"stdout": clean},
            pool_class="spend",
        )
    if daily_used is None:
        notes.append("daily 미측정 — /usage Daily 축을 못 읽음")
    if weekly_used is None:
        notes.append("weekly 미측정 — 배너·/usage 어느 쪽도 증명 못함")

    daily = Bucket(
        label="daily",
        window="1d",
        used_pct=daily_used,
        resets_at=daily_reset_at,
        scope=Scope("account"),
        horizon="now",
        note="/usage Daily" if daily_used is not None else "미측정",
    )
    weekly = Bucket(
        label="weekly",
        window="7d",
        used_pct=weekly_used,
        resets_at=weekly_reset_at,
        scope=Scope("account"),
        horizon="week",
        note=("/usage Weekly" if usage_weekly.get("used_pct") is not None else "기동 배너 weekly")
        if weekly_used is not None
        else "미측정",
    )
    return ProviderResult(
        id=PROVIDER_ID,
        plan=plan,
        buckets=[daily, weekly],
        note=" · ".join(notes),
        source=SOURCE_QUOTA,
        raw={"stdout": clean},
        pool_class="spend",
    )


def _usage_axes(clean: str, now: dt.datetime | None = None) -> dict[str, dict[str, object]]:
    """/usage 출력의 daily·weekly 축을 물리 줄 단위로 파싱한다.

    ``Daily``·``Weekly`` 레이블로 시작하는 각 줄을 한 축 행으로 보고 그 줄
    안에서만 ``N% used`` 와 리셋을 찾는다 — 레이블에서 다음 레이블까지의 임의
    구간을 한 축으로 보지 않으므로, 뒤에 다시 그려진 상태줄(``Pro · …``)이나
    다른 줄의 퍼센트가 이 축에 새어 들어오지 않는다. 같은 축에서는 첫 읽힌
    행이 이기고, ``% used`` 가 안 읽힌 행은 축을 만들지 않는다 — 절대
    0/100 으로 채우지 않는다.
    """
    axes: dict[str, dict[str, object]] = {}
    for line in clean.splitlines():
        label = _USAGE_ROW.match(line)
        if label is None:
            continue
        name = label["axis"].lower()
        if name in axes:
            continue
        used: float | None = None
        for candidate in _USAGE_USED.finditer(line):
            value = float(candidate["used"])
            if 0 <= value <= 100:
                used = value
                break
        if used is None:
            continue
        axes[name] = {"used_pct": used, "resets_at": _segment_reset_iso(line, now=now, axis=name)}
    return axes


def _segment_reset_iso(segment: str, now: dt.datetime | None = None, axis: str | None = None) -> str | None:
    """축 행 안의 리셋 — 상대 기간(resets in …) 또는 절대 시각(resets Oct 11, …)."""
    moment = now or dt.datetime.now(dt.UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    duration = _USAGE_RESET.search(segment)
    if duration is not None:
        return _reset_iso(duration["reset"].strip(), now=moment)
    absolute = _USAGE_RESET_ABS.search(segment)
    if absolute is None:
        return None
    try:
        month = _MONTHS[absolute["mon"].lower()[:3]]
        hour, minute = (int(part) for part in absolute["hm"].split(":"))
        if absolute["ampm"].upper() == "PM" and hour != 12:
            hour += 12
        if absolute["ampm"].upper() == "AM" and hour == 12:
            hour = 0
        tz_text = absolute["tz"]
        sign = 1 if tz_text.startswith("+") else -1
        hours, _, minutes = tz_text[1:].partition(":")
        offset = dt.timedelta(hours=sign * (int(hours) + int(minutes or 0) / 60))
        tzinfo = dt.timezone(offset)
        # 연도가 없는 절대 시각 — 라벨의 tz 기준으로 올해·작년·내년 후보를 만들어
        # 그 축 창의 지평(지난쪽 하루 ~ 창 길이) 안의 후보 중 지금에 가장 가까운
        # 것을 고른다(연말에 ``resets Jan 1, …`` 가 작년으로 해석되는 것을 막는다).
        # 지평 밖 후보만 남으면 지어낸 시각이 되므로 None 으로 둔다 — 하루 이상
        # 지난 리셋이 다음 해 같은 날짜로 점프하는 것을 막는다.
        local_now = moment.astimezone(tzinfo)
        day = int(absolute["day"])
        earliest = local_now - dt.timedelta(days=1)
        latest = local_now + _ABS_RESET_HORIZON.get(axis or "", dt.timedelta(days=8))
        candidates = []
        for year in (local_now.year - 1, local_now.year, local_now.year + 1):
            with contextlib.suppress(ValueError):
                candidate = dt.datetime(year, month, day, hour, minute, tzinfo=tzinfo)
                if earliest <= candidate <= latest:
                    candidates.append(candidate)
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(c - local_now)).isoformat()
    except (KeyError, ValueError):
        return None


def _usage_pct_seen(clean: str) -> bool:
    return any(axis["used_pct"] is not None for axis in _usage_axes(clean).values())


def _banner_quota_match(clean: str) -> re.Match[str] | None:
    """Return only a complete, in-range quota line shared by probe and parser."""
    for pattern in (_BANNER_QUOTA_OLD, _BANNER_QUOTA_NEW):
        for match in pattern.finditer(clean):
            try:
                remaining = float(match["remaining"])
            except (TypeError, ValueError):
                continue
            if 0 <= remaining <= 100:
                return match
    return None


def _input_gate_open(clean: str) -> bool:
    """/usage 를 쳐도 되는 화면인가 — 세 조건이 모두 갖춰져야 연다(fail-closed).

    ① 쿼타 상태줄이 보이고(계정·쿼타 조회가 끝난 화면),
    ② 그 상태줄 이후에 composer placeholder 가 다시 그려졌으며(placeholder 는
      첫 페인트에서 상태줄보다 먼저 나오므로, 상태줄 뒤에도 보여야 지금
      composer 가 살아 있는 입력창이다),
    ③ 상태줄 뒤 화면에 chevron 옵션 목록이나 ``?`` 질문 프롬프트가 없다
      (상태줄 위에 열린 업데이트·로그인 모달에 Enter 가 들어가는 것을 막는다).
    """
    banner = _banner_quota_match(clean)
    if banner is None:
        return False
    tail = clean[banner.end() :]
    if _INPUT_READY.search(tail) is None:
        return False
    return _MODAL_OR_OPTION.search(tail) is None


def _unknown_quota_buckets() -> list[Bucket]:
    """쿼타 프로브가 통째로 실패했을 때의 fail-closed 축 — 값을 지어내지 않는다."""
    return [
        Bucket(
            label="daily",
            window="1d",
            used_pct=None,
            resets_at=None,
            scope=Scope("account"),
            horizon="now",
            note="미측정 — PTY 프로브 실패",
        ),
        Bucket(
            label="weekly",
            window="7d",
            used_pct=None,
            resets_at=None,
            scope=Scope("account"),
            horizon="week",
            note="미측정 — PTY 프로브 실패",
        ),
    ]


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
                # Free 태그는 쿼타 창이 아니다 — '30d'·'month' 로 그리면 '월' 축
                # 오독이 다시 생긴다(1381: desk 의 '월 0%'). 창을 비우고 non-month
                # 지평만 둔다; model scope 이므로 어느 축에도 합산되지 않는다.
                window="",
                used_pct=0.0,
                resets_at=None,
                scope=Scope("model", "swe-2"),
                horizon="now",
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


def _reset_iso(duration: str | None, now: dt.datetime | None = None) -> str | None:
    if not duration:
        return None
    total_seconds = 0.0
    for match in _DURATION_PART.finditer(duration):
        value = float(match["value"])
        total_seconds += value * {"d": 86400, "h": 3600, "m": 60, "s": 1}[match["unit"].lower()]
    if total_seconds <= 0:
        return None
    moment = now or dt.datetime.now(dt.UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return (moment + dt.timedelta(seconds=total_seconds)).isoformat()


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name == "HERDR" or name.startswith("HERDR_"):
            del env[name]
    env["COLUMNS"] = str(PTY_COLS)
    env["LINES"] = str(PTY_ROWS)
    env["TERM"] = "xterm-256color"
    return env


def _failed(
    error: str,
    *,
    hint: str | None = None,
    stdout: str | None = None,
    error_kind: str | None = None,
) -> ProviderResult:
    raw = {"stdout": stdout} if stdout else None
    return ProviderResult(
        id=PROVIDER_ID,
        error=error,
        hint=hint,
        source=SOURCE,
        raw=raw,
        pool_class="spend",
        error_kind=error_kind,
    )
