"""프로필별 업스트림 서빙 모델 검증 (ROB-1275).

급표는 무버전 ClinePass 슬러그에 점수를 박는데, 벤더는 같은 슬러그를 새 모델로 조용히
재지향한다 (실제로 `cline-pass/deepseek-v4-pro` 는 0813 정식 모델을 서빙 — 구 모델 측정치로
급을 매기고 기각까지 한 사고). 이 모듈은 ``Profile.request_slug`` 로 최소 요청(생성 1토큰
수준)을 보내 응답의 ``model`` 필드를 읽어 ``Profile.upstream_slug`` 기록값과 대조한다.

프로브는 ``probe(request_slug, key) -> str | None`` 시그니처를 갖는 callable 로 추상화한다.
ts단위 테스트는 이 callable 을 가짜로 갈아끼워 실제 네트워크를 치지 않는다. 키 값은 어떤
경로로도 노출하지 않는다 (`Authorization` 헤더에만 사용).
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import urllib.error
import urllib.request
from collections.abc import Callable

from .recommend import GRADE_TABLE

COMPLETIONS_URL = "https://api.cline.bot/api/v1/chat/completions"
TIMEOUT_S = 20.0
DEFAULT_AUTH = pathlib.Path.home() / ".local" / "share" / "opencode" / "auth.json"
# env 로 경로를 덮어쓸 수 있게 한다 — 테스트가 실제 홈을 건드리지 않는다.
AUTH_PATH_ENV = "SCOPEFUEL_CLINEPASS_AUTH"
DEFAULT_MAX_TOKENS = 1  # 최소 요청 — 생성 1토큰 수준

# 판정 종류: 기록값과 라이브값이 일치하면 match, 어긋나면 drift,
# 기록값(upstream_slug)이 없어 대조할 기준이 없거나 프로브 실패면 unknown.
Verdict = str


@dataclasses.dataclass(frozen=True)
class VerifyRow:
    """프로필 하나의 서빙 모델 대조 결과."""

    profile: str
    request_slug: str | None
    recorded: str | None
    live: str | None
    observed: str | None
    verdict: Verdict


@dataclasses.dataclass(frozen=True)
class VerifyReport:
    rows: tuple[VerifyRow, ...]

    @property
    def has_drift(self) -> bool:
        return any(row.verdict == "drift" for row in self.rows)


Probe = Callable[[str, str], str | None]


def _resolve_key() -> str | None:
    """ClinePass 키를 읽는다. 값은 반환되는 즉시 호출자 인증에만 쓰고 저장·출력하지 않는다."""
    path_raw = os.environ.get(AUTH_PATH_ENV)
    path = pathlib.Path(path_raw) if path_raw else DEFAULT_AUTH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get("cline-pass")
    if not isinstance(entry, dict):
        return None
    key = entry.get("key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return None


def probe(request_slug: str, key: str) -> str | None:
    """최소 chat completion 을 보내고 응답의 ``model`` 필드를 돌려준다.

    실패(네트워크/HTTP/JSON/Schema 불일치)는 전부 None — verify 는 이를 unknown 으로
    처리한다. 응답 본문 중 model 값 이외에는 아무것도 노출하지 않는다.
    """
    body = json.dumps(
        {
            "model": request_slug,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": DEFAULT_MAX_TOKENS,
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        COMPLETIONS_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "scopefuel",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    # 실측(2026-08-15): ClinePass 는 최상위가 아닌 data.model 에 서빙 모델을 실는 게이트웨이
    # 형식이다 (top-level model 은 별도 필드명으로 전달됨). data.model 도 함께 읽는다.
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("model")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    # 보수적으로 다른 경로는 믿지 않는다 (없으면 미확인).
    return None


def verify(
    probe_fn: Probe = probe,
    key_resolver: Callable[[], str | None] = _resolve_key,
) -> VerifyReport:
    """GRADE_TABLE 의 모든 ClinePass 프로필 슬러그를 라이브 프로브로 대조한다."""
    profiles = [profile for group in GRADE_TABLE.values() for profile in group if profile.request_slug]
    if not profiles:
        return VerifyReport(())
    key = key_resolver()
    if not key:
        # 키 없음 → 아무것도 측정할 수 없으므로 전부 unknown.
        rows = [
            VerifyRow(
                profile=profile.name,
                request_slug=profile.request_slug,
                recorded=profile.upstream_slug,
                live=None,
                observed=profile.upstream_observed,
                verdict="unknown",
            )
            for profile in profiles
        ]
        return VerifyReport(tuple(rows))

    rows: list[VerifyRow] = []
    for profile in profiles:
        live = probe_fn(profile.request_slug, key)
        if profile.upstream_slug is None or live is None:
            verdict = "unknown"
        elif live == profile.upstream_slug:
            verdict = "match"
        else:
            verdict = "drift"
        # live 수행 중 실제 키 값을 반환 구조에 보관하지 않는다 (Referential 정리).
        rows.append(
            VerifyRow(
                profile=profile.name,
                request_slug=profile.request_slug,
                recorded=profile.upstream_slug,
                live=live,
                observed=profile.upstream_observed,
                verdict=verdict,
            )
        )
    return VerifyReport(tuple(rows))


def _verdict_label(verdict: Verdict) -> str:
    return {"match": "match", "drift": "drift", "unknown": "unknown"}[verdict]


def format_report(report: VerifyReport) -> str:
    header = (
        "profile           | 요청 슬러그                          | 기록값                          "
        "| 라이브값                          | 판정"
    )
    lines = [header, "-" * 150]
    for row in report.rows:
        lines.append(
            f"{row.profile:<17}| {row.request_slug or '-':<38}| {row.recorded or '-':<38}| "
            f"{row.live or '-':<38}| {_verdict_label(row.verdict)}"
        )
    return "\n".join(lines)
