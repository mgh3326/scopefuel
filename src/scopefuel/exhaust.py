"""on_exhaust="operator-switch" — 풀 소진 에피소드당 1회 운영자 알림 (task #638).

``[pools.<p>] on_exhaust = "operator-switch"`` 인 풀이 차단선에 도달하면
"계정 전환 필요" 를 operator-desk 레인 이벤트로 올린다. 경로 결정 근거는
hk:doc review/2026-09-24/638-ac-review §5 — 시간 민감 알림이라 pane 에 능동
도착하는 lane event 를 택했다(hk doc 은 폴링이 필요하다).

dedup 은 에피소드 단위다: 소진 진입 시 1회 기록하고, 풀이 차단선 아래로
관측되면(계정 전환·리셋) 플래그를 해제한다 — 새 소진은 다시 알린다. 발송
실패는 ``attempted_at`` 만 기록해 ``RETRY_COOLDOWN_S`` 안의 재시도를 억제하고
그 이후 관측에서 재시도한다 — 실패를 "발송됨"으로 기록하면 알림이 조용히
유실되고, 아무것도 기록하지 않으면 wedged 데몬이 관측 경로(recommend 내
대안 평가 포함)마다 발송을 곱한다. 발송 자체는 best-effort: panewire
부재·지연·실패가 게이트 판정을 바꾸지 않는다(5s timeout — wrk 의 emit 가드와
동일).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import pathlib
import shutil
import subprocess
from collections.abc import Callable

from . import cache
from .policy import get_on_exhaust

NOTIFY_LANE = "operator-desk"
NOTIFY_OWNER = "scopefuel"
EMIT_TIMEOUT_S = 5.0
# 발송 실패 후 재시도까지의 최소 간격. gate 한 번이 recommend/대안 평가를 거쳐
# 같은 풀의 소진을 여러 번 관측하므로, 이 창 안의 관측은 싱크를 다시 치지 않는다.
RETRY_COOLDOWN_S = 60.0
_STATE_NAME = "exhaust-notify.json"


def _state_path() -> pathlib.Path:
    return cache.cache_dir() / _STATE_NAME


@contextlib.contextmanager
def _locked_state() -> object:
    """dedup 상태의 read-modify-write 를 커널 락으로 직렬화한다."""
    path = _state_path()
    lock = path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                state = {}
            yield state if isinstance(state, dict) else {}
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _save_state(state: dict) -> bool:
    """dedup 상태를 기록한다. 실패해도 게이트를 막지 않고 False 를 돌려준다."""
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False))
        tmp.chmod(0o600)
        tmp.replace(path)
    except OSError:
        return False  # dedup 기록 실패는 게이트를 막지 않는다 — 다음 관측이 재시도한다
    return True


# 상태 파일을 쓸 수 없을 때의 in-process dedup — 파일이 정본이지만, 기록 실패를
# "발송됨"으로 오인해 같은 에피소드에서 재발송하는 것보다 같은 프로세스 안의
# 억제가 낫다(CodeRabbit PR#77 Major).
_EPHEMERAL: dict[str, dict] = {}


def _clear_if_recorded(key: str) -> None:
    """비-operator-switch 모드에서의 회복 관측도 기록된 에피소드를 지운다.

    block 모드로 돌아간 동안의 회복이 notified_at 을 남기면, 모드를 다시
    operator-switch 로 바꿨을 때 새 소진 알림이 억제된다. 파일에 해당 키가
    없으면 락도 잡지 않는다 — 건강한 관측 경로의 무접촉을 유지한다.
    """
    _EPHEMERAL.pop(key, None)
    try:
        raw = json.loads(_state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict) or key not in raw:
        return
    try:
        with _locked_state() as state:
            if state.pop(key, None) is not None:
                _save_state(state)
    except OSError:
        pass  # 상태 저장소 접근 실패 — 지우지 못해도 게이트를 막지 않는다


def emit_lane_event(text: str) -> bool:
    """기본 싱크 — operator-desk lane event. 실패해도 예외 없이 False."""
    panewire = shutil.which(os.environ.get("PANEWIRE_BIN", "panewire"))
    if panewire is None:
        return False
    try:
        completed = subprocess.run(
            [
                panewire,
                "emit",
                "--kind",
                "lane.event",
                "--lane",
                NOTIFY_LANE,
                "--owner-lane",
                NOTIFY_OWNER,
                "--sink",
                "--text",
                text,
            ],
            check=False,
            timeout=EMIT_TIMEOUT_S,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _message(
    pool: str,
    used_pct: float | None,
    cutoff: float | None,
    window: str | None,
    reset_at: str | None,
    scope: str | None,
) -> str:
    parts = [f"scopefuel.exhaust pool={pool}"]
    if scope:
        parts.append(f"scope={scope}")
    if window:
        parts.append(f"window={window}")
    head = " ".join(parts)
    detail = f"{pool} 계정 전환 필요"
    if scope:
        detail = f"{pool} ({scope}) 계정 전환 필요"
    if used_pct is not None and cutoff is not None:
        detail += f" — {used_pct:g}% 사용 · {100.0 - used_pct:g}% 남음 · 차단선 {cutoff:g}%"
    if reset_at:
        detail += f" (reset {reset_at})"
    return f"{head} — {detail}"


def _parse_iso(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def observe(
    pool: str,
    *,
    exhausted: bool,
    used_pct: float | None = None,
    cutoff: float | None = None,
    window: str | None = None,
    reset_at: str | None = None,
    scope: str | None = None,
    now: dt.datetime | None = None,
    sink: Callable[[str], bool] | None = None,
) -> str | None:
    """소진 관측을 기록하고 operator-switch 풀이면 알림 상태를 반환한다.

    ``scope`` 는 같은 풀 안의 독립 사용 범위(agy 의 group, herdr 의 credential)
    다 — 에피소드 키가 ``pool@scope`` 로 분리돼 한 범위의 회복 관측이 다른
    범위의 소진 기록을 지우지 않는다(CodeRabbit PR#77 Major).

    반환값은 게이트 사유에 그대로 붙는 표시 조각이다:

    - ``None`` — on_exhaust 미설정/기본(block)이거나 비소진 관측(파일 무접촉)
    - ``"... 알림 발송"`` — 이번 관측에서 발송 성공
    - ``"... 기통지(중복 억제)"`` — 같은 소진 에피소드의 재관측
    - ``"... 발송 실패"`` — 싱크 실패(``attempted_at`` 만 기록, 재시도는 쿨다운 뒤)
    - ``"invalid on_exhaust ..."`` — 설정 오타는 block 으로 폴백됨을 노출
    """
    mode, mode_status = get_on_exhaust(pool)
    key = f"{pool}@{scope}" if scope else pool
    if mode != "operator-switch":
        if not exhausted:
            _clear_if_recorded(key)
        return mode_status

    now = now or dt.datetime.now(dt.UTC)
    try:
        with _locked_state() as state:
            entry = state.get(key)
            if entry is None:
                entry = _EPHEMERAL.get(key)
            if not exhausted:
                _EPHEMERAL.pop(key, None)
                if key in state:
                    state.pop(key)
                    _save_state(state)
                return None
            if isinstance(entry, dict):
                if entry.get("notified_at"):
                    return f"{pool} 계정 전환 필요 — 기통지(중복 억제)"
                attempted = _parse_iso(entry.get("attempted_at"))
                if attempted is not None and (now - attempted).total_seconds() < RETRY_COOLDOWN_S:
                    return f"{pool} 계정 전환 필요 — 발송 실패(재시도 대기)"

            send = sink if sink is not None else emit_lane_event
            try:
                delivered = bool(send(_message(pool, used_pct, cutoff, window, reset_at, scope)))
            except Exception:
                delivered = False
            record = {"window": window, "reset_at": reset_at, "used_pct": used_pct}
            record["notified_at" if delivered else "attempted_at"] = now.isoformat()
            state[key] = record
            saved = _save_state(state)
            if not saved:
                _EPHEMERAL[key] = record
            lost = "" if saved else "(기록 실패)"
            if not delivered:
                return f"{pool} 계정 전환 필요 — 알림 발송 실패{lost}"
            return f"{pool} 계정 전환 필요 — operator-desk 알림 발송{lost}"
    except OSError:
        # 락·디렉토리 접근 실패는 게이트 판정을 바꾸지 않는다 — 발송 없이 실패
        # 상태만 노출한다(CodeRabbit PR#77 Major: 저장소 장애가 게이트를 중단시키면 안 됨).
        if not exhausted:
            return None
        return f"{pool} 계정 전환 필요 — 알림 발송 실패(상태 저장소 접근 불가)"
