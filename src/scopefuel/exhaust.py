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


def _save_state(state: dict) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False))
        tmp.chmod(0o600)
        tmp.replace(path)
    except OSError:
        pass  # dedup 기록 실패는 게이트를 막지 않는다 — 다음 관측이 재시도한다


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
) -> str:
    parts = [f"scopefuel.exhaust pool={pool}"]
    if window:
        parts.append(f"window={window}")
    head = " ".join(parts)
    detail = f"{pool} 계정 전환 필요"
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
    now: dt.datetime | None = None,
    sink: Callable[[str], bool] | None = None,
) -> str | None:
    """소진 관측을 기록하고 operator-switch 풀이면 알림 상태를 반환한다.

    반환값은 게이트 사유에 그대로 붙는 표시 조각이다:

    - ``None`` — on_exhaust 미설정/기본(block)이거나 비소진 관측(파일 무접촉)
    - ``"... 알림 발송"`` — 이번 관측에서 발송 성공
    - ``"... 기통지(중복 억제)"`` — 같은 소진 에피소드의 재관측
    - ``"... 발송 실패"`` — 싱크 실패(``attempted_at`` 만 기록, 재시도는 쿨다운 뒤)
    - ``"invalid on_exhaust ..."`` — 설정 오타는 block 으로 폴백됨을 노출
    """
    mode, mode_status = get_on_exhaust(pool)
    if mode != "operator-switch":
        return mode_status

    now = now or dt.datetime.now(dt.UTC)
    with _locked_state() as state:
        entry = state.get(pool)
        if not exhausted:
            if entry is not None:
                state.pop(pool)
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
            delivered = bool(send(_message(pool, used_pct, cutoff, window, reset_at)))
        except Exception:
            delivered = False
        if not delivered:
            state[pool] = {
                "attempted_at": now.isoformat(),
                "window": window,
                "reset_at": reset_at,
                "used_pct": used_pct,
            }
            _save_state(state)
            return f"{pool} 계정 전환 필요 — 알림 발송 실패"
        state[pool] = {
            "notified_at": now.isoformat(),
            "window": window,
            "reset_at": reset_at,
            "used_pct": used_pct,
        }
        _save_state(state)
        return f"{pool} 계정 전환 필요 — operator-desk 알림 발송"
