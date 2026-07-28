"""Kiro CLI — `kiro-cli` 에 `/usage` 를 물려 TUI 출력을 읽는다.

왜 API 가 아니라 CLI 인가:

Kiro 는 `AmazonCodeWhispererService.GetUsageLimits` 라는 API 로 같은 값을 주지만,
토큰이 JSON 파일이 아니라 **sqlite**(`~/Library/Application Support/kiro-cli/data.sqlite3`
의 `auth_kv`)에 들어 있고 만료 시 갱신이 필요하다. scopefuel 은 읽기 전용 계측기라
남의 토큰 저장소를 열고 갱신 흐름까지 떠안는 것보다, 이미 인증을 해결한 CLI 에게
물어보는 편이 경계가 깨끗하다. 대신 **출력 포맷 변화에 취약**하다 — 파싱이 깨지면
0 을 채우지 않고 error 로 보고한다.

호출 비용이 7 초쯤 되므로 코어의 60 초 캐시(TTL)에 기대는 것을 전제로 한다.

관측된 출력(kiro-cli 2.15.0):

    Estimated Usage | resets on 2026-08-01 | KIRO PRO MAX
    Credits (0.50 of 5000 covered in plan)

보너스/애드온 크레딧이 있는 계정은 아래 줄이 더 붙는다(ClaudeBar 가 기록한 형태,
이 저장소에서는 PRO MAX 계정뿐이라 실물 미확인):

    🎁 Bonus credits: 122.54/500 credits used, expires in 29 days

**스코프 판단**: 플랜 크레딧이 다 차도 애드온이 남아 있으면 작업은 계속된다.
그래서 두 풀을 각각 account 버킷으로 내보내면 "막혔다"는 오판이 된다(verdict 는
account 버킷의 최대값을 차단으로 본다). 두 풀이 있으면 **합산 한 줄**만 account 로
내보내고 내역은 note 에 적는다.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess

from ..model import Bucket, ProviderResult, Scope

BINARY = os.environ.get("SCOPEFUEL_KIRO_BIN") or "kiro-cli"
PROBE_INPUT = "/usage\n/quit\n"
TIMEOUT_S = 30.0

# 플랜 크레딧은 월 단위로 리셋된다. scopefuel 의 창 표기에는 '월'이 없어 30d 로 적는다
# (pace 계산이 달 길이만큼 어긋날 수 있다 — 리셋 시각 자체는 실제 값을 쓴다).
PLAN_WINDOW = "30d"

_ANSI = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]|\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)")
_PLAN_CREDITS = re.compile(r"Credits\s*\(\s*([\d.,]+)\s+of\s+([\d.,]+)", re.IGNORECASE)
_BONUS_CREDITS = re.compile(r"Bonus credits:\s*([\d.,]+)\s*/\s*([\d.,]+)", re.IGNORECASE)
_BONUS_EXPIRY = re.compile(r"expires in (\d+) days?", re.IGNORECASE)
_RESET_ISO = re.compile(r"resets on (\d{4})-(\d{2})-(\d{2})")
_RESET_MMDD = re.compile(r"resets on (\d{1,2})/(\d{1,2})")
_PLAN_NAME = re.compile(r"\|\s*KIRO ([A-Z+ ]+?)\s*$", re.MULTILINE)
_EXPIRED = re.compile(r"Token expired|AccessDenied", re.IGNORECASE)


def fetch() -> ProviderResult:
    if shutil.which(BINARY) is None:
        return ProviderResult(
            id="kiro",
            error=f"{BINARY} 실행 파일 없음",
            hint="kiro-cli 설치 후 다시 시도 (SCOPEFUEL_KIRO_BIN 으로 경로 지정 가능)",
        )
    result = _probe_once()
    if result.error and _EXPIRED.search((result.raw or {}).get("stdout", "")):
        # 만료된 액세스 토큰은 CLI 호출 자체가 갱신한다(실측: 1회 실패 → 재호출 성공).
        # 갱신은 CLI 몫이고 scopefuel 은 여전히 아무것도 쓰지 않는다.
        retried = _probe_once()
        if not retried.error:
            return retried
        retried.hint = "kiro-cli 를 직접 실행해 로그인 상태를 확인하세요 (kiro-cli login)"
        return retried
    return result


def _probe_once() -> ProviderResult:
    try:
        proc = subprocess.run(  # noqa: S603 - 사용자 PATH 의 kiro-cli, 입력은 고정 문자열
            [BINARY],
            input=PROBE_INPUT,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return ProviderResult(
            id="kiro",
            error=f"{BINARY} /usage 가 {TIMEOUT_S:.0f}초 안에 끝나지 않음",
            hint="kiro-cli 를 직접 실행해 로그인/네트워크 상태를 확인하세요",
        )
    except OSError as exc:
        return ProviderResult(id="kiro", error=f"{BINARY} 실행 실패: {exc}")

    return parse(proc.stdout + proc.stderr)


def parse(text: str) -> ProviderResult:
    """`/usage` 출력 → ProviderResult. 못 읽은 값은 0 으로 채우지 않는다."""
    clean = _ANSI.sub("", text)

    plan_match = _PLAN_CREDITS.search(clean)
    if not plan_match:
        hint = (
            "kiro-cli 로그인이 필요해 보입니다 (kiro-cli login)"
            if re.search(r"log ?in|sign ?in", clean, re.IGNORECASE)
            else "kiro-cli 를 직접 실행해 /usage 출력이 나오는지 확인하세요"
        )
        return ProviderResult(
            id="kiro",
            error="/usage 출력에서 크레딧 줄을 찾지 못함",
            hint=hint,
            source="cli:/usage",
            raw={"stdout": clean},
        )

    plan_used, plan_limit = _num(plan_match[1]), _num(plan_match[2])
    resets_at = _reset_iso(clean)
    note: str | None = None

    used, limit = plan_used, plan_limit
    if bonus := _BONUS_CREDITS.search(clean):
        # 애드온은 플랜 소진 뒤에 쓰인다 → 차단 판정은 두 풀의 합으로 봐야 맞다.
        bonus_used, bonus_limit = _num(bonus[1]), _num(bonus[2])
        used, limit = plan_used + bonus_used, plan_limit + bonus_limit
        expiry = _BONUS_EXPIRY.search(clean)
        note = (
            f"플랜 {_fmt(plan_used)}/{_fmt(plan_limit)} + 애드온 "
            f"{_fmt(bonus_used)}/{_fmt(bonus_limit)}" + (f" (애드온 {expiry[1]}일 후 만료)" if expiry else "")
        )

    return ProviderResult(
        id="kiro",
        plan=_plan_name(clean),
        buckets=[
            Bucket(
                label="credits",
                window=PLAN_WINDOW,
                used_pct=None if limit <= 0 else min(100.0, used / limit * 100.0),
                resets_at=resets_at,
                scope=Scope("account"),
                horizon="week",
                note=note,
            )
        ],
        note="Kiro 는 5시간급 창이 없다 — 월 크레딧 한 줄뿐이다",
        source="cli:/usage",
        raw={"stdout": clean},
    )


def _num(raw: str) -> float:
    return float(raw.replace(",", ""))


def _fmt(value: float) -> str:
    return f"{value:g}"


def _plan_name(clean: str) -> str | None:
    match = _PLAN_NAME.search(clean)
    return match[1].strip().lower() if match else None


def _reset_iso(clean: str) -> str | None:
    """`resets on 2026-08-01` (2.15.0) 과 `resets on 03/01` (구버전) 둘 다 받는다.

    CLI 는 **날짜만** 준다. 시각은 알 수 없어 로컬 자정으로 둔다 — CLI 화면에 뜬 날짜와
    같은 날로 보이게 하는 선택이다. 30일 창에서 이 오차는 pace 계산에 거의 영향이 없다.
    """
    if iso := _RESET_ISO.search(clean):
        return _at_local_midnight(int(iso[1]), int(iso[2]), int(iso[3]))
    if mmdd := _RESET_MMDD.search(clean):
        today = dt.datetime.now().astimezone().date()
        month, day = int(mmdd[1]), int(mmdd[2])
        year = today.year
        try:
            if dt.date(year, month, day) < today:  # 지나간 날짜면 내년 것
                year += 1
        except ValueError:
            return None
        return _at_local_midnight(year, month, day)
    return None


def _at_local_midnight(year: int, month: int, day: int) -> str | None:
    try:
        return dt.datetime(year, month, day).astimezone().isoformat()
    except ValueError:
        return None
