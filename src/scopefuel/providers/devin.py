"""Devin CLI — `devin models list` 에서 SWE-2 Free 태그만 읽는다.

쿼타 소스는 CLI 모델 목록뿐이다. Devin credential/config 파일은 열지 않고,
로그인·복구도 시도하지 않는다. SWE-2 패밀리 행에 `Free` 태그가 있을 때만
account 버킷 used_pct=0 을 낸다. 다른 모델의 Free, Fusion 이름에 섞인 SWE-2,
출력 해석 실패, binary/프로세스 실패는 버킷 없이 error 로 fail-closed 한다.

유료 Devin 모델(SWE-1.x/Fusion/Claude/OpenAI/Gemini 등)은 이 provider 범위 밖이다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from ..model import Bucket, ProviderResult, Scope

BINARY = os.environ.get("SCOPEFUEL_DEVIN_BIN") or "devin"
TIMEOUT_S = 30.0
SOURCE = "cli:models list"
FREE_NOTE = "free until ~2026-10-10"
PROVIDER_ID = "devin"

_ANSI = re.compile(r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*(?:\x07|\x1B\\)|\([0-2])")
# `SWE-2 (swe-2)` 패밀리 헤더. SWE-1.7 / Lightning / Fusion 제목은 제외.
_SWE2_FAMILY = re.compile(r"^SWE-2\s+\(swe-2\)\s*$")
_FAMILY_HEADER = re.compile(r"^\S.*\([^)]+\)\s*$")
_BRACKET_TAGS = re.compile(r"\[([^\[\]]*)\]\s*$")


def fetch() -> ProviderResult:
    if shutil.which(BINARY) is None:
        return _failed(
            f"{BINARY} 실행 파일 없음",
            hint="Devin CLI 설치 후 다시 시도 (SCOPEFUEL_DEVIN_BIN 으로 경로 지정 가능)",
        )
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


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name == "HERDR" or name.startswith("HERDR_"):
            del env[name]
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
