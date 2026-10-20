"""OpenAI Codex CLI — ChatGPT backend usage API.

`~/.codex/auth.json` 의 tokens.access_token + ChatGPT-Account-Id 헤더로
`GET /backend-api/wham/usage`. primary/secondary window 는 계정 스코프,
`additional_rate_limits[]`(예: GPT-5.3-Codex-Spark)는 그 모델 한정 스코프다.

hk 1753 (b953): 계정 지문은 ``tokens.account_id``(ChatGPT 계정 uuid)에 묶는다 —
claude 의 oauthAccount.accountUuid 와 같은 역할이다. uuid 가 없는 자격은 토큰
해시 폴백("token")으로 내린다 — 게시·원격 읽기 대상이 아니다.
id_token(JWT, 이메일 포함)은 절대 파싱하지 않는다 — 계정 라벨도 여기서
만들지 않는다.

지문은 ``publish_fp`` 에만 싣는다 — ``account_fp`` 를 채우면 로컬 지문
게이트(stale 수용 일치·원격 폴백)가 codex 에도 열려 비-Fusion 프로필의
호스트-로컬 판정이 달라진다. 게시만 하고 로컬 판정은 바꾸지 않는 것이
계약이다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib

from ..http import request_json
from ..model import Bucket, ProviderResult, Scope, epoch_to_iso, horizon_for, window_label

AUTH = pathlib.Path.home() / ".codex" / "auth.json"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def _load_auth() -> dict | None:
    """auth.json 본문 dict — 파일이 없거나 JSON 이 아니면 None.

    dict 가 아닌 최상위 값은 빈 dict 로 취급한다 — 파일은 있는데 tokens 를
    못 쓰는 상태는 '자격증명 파일 없음' 이 아니라 'access_token 없음' 이다.
    """
    try:
        data = json.loads(_auth_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else {}


def _account_identity(tokens: dict) -> tuple[str | None, str | None]:
    """계정 지문과 묶임 근거 — ``(publish_fp, kind)`` (hk 1753).

    ``account_id``(ChatGPT 계정 uuid)는 토큰 회전·호스트를 넘어 같은 계정을
    가리킨다 — 교차호스트 게시가 가능한 "account" 지문이다.
    access_token 해시는 "token" 폴백 — 회전마다 바뀌어 게시하면 orphan 문서만
    남으므로 quota_share 는 그 지문을 게시하지 않는다(claude 와 같은 규칙).
    """
    account_id = tokens.get("account_id")
    if isinstance(account_id, str) and account_id.strip():
        return (
            hashlib.sha256(f"codex-account|{account_id}".encode()).hexdigest()[:16],
            "account",
        )
    token = tokens.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return None, None
    return hashlib.sha256(f"codex-token|{token}".encode()).hexdigest()[:16], "token"


def _session_fp(tokens: dict) -> str | None:
    """측정 세션 지문 — 어느 토큰 세션이 관측했는지의 provenance (게시 페이로드 전용)."""
    token = tokens.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return None
    return hashlib.sha256(f"codex-session|{token}".encode()).hexdigest()[:16]


def fetch() -> ProviderResult:
    auth = _auth_path()
    data = _load_auth()
    if data is None:
        return ProviderResult(
            id="codex", error="자격증명 파일 없음", hint=f"{auth} 없음 — `codex login` 후 재시도"
        )
    tokens = data.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    fp, fp_kind = _account_identity(tokens)
    session_fp = _session_fp(tokens)
    token = tokens.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return ProviderResult(id="codex", error="access_token 없음", hint="`codex login` 필요")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "scopefuel",
    }
    if account_id := tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = account_id

    raw = request_json(USAGE_URL, headers=headers)

    def windows(
        rate_limit: dict | None, *, label_prefix: str = "", scope: Scope | None = None
    ) -> list[Bucket]:
        out: list[Bucket] = []
        for key in ("primary_window", "secondary_window"):
            win = (rate_limit or {}).get(key)
            if not win:
                continue
            seconds = win.get("limit_window_seconds")
            label = window_label(seconds)
            out.append(
                Bucket(
                    label=f"{label_prefix}{label}",
                    window=label,
                    used_pct=_num(win.get("used_percent")),
                    resets_at=epoch_to_iso(win.get("reset_at")),
                    scope=scope or Scope("account"),
                    horizon=horizon_for(seconds),
                )
            )
        return out

    buckets = windows(raw.get("rate_limit"))
    for extra in raw.get("additional_rate_limits") or []:
        name = extra.get("limit_name") or "?"
        buckets += windows(extra.get("rate_limit"), label_prefix=f"{name} ", scope=Scope("model", name))

    note = None
    if (raw.get("rate_limit") or {}).get("limit_reached"):
        note = "limit reached"
    credits = raw.get("credits") or {}
    if credits.get("has_credits"):
        note = f"{note + ' / ' if note else ''}credits {credits.get('balance')}"

    return ProviderResult(
        id="codex",
        plan=raw.get("plan_type"),
        buckets=buckets,
        note=note,
        source="wham-usage-api",
        raw=raw,
        publish_fp=fp,
        publish_fp_kind=fp_kind,
        session_fp=session_fp,
    )


def _auth_path() -> pathlib.Path:
    configured = os.environ.get("CODEX_HOME")
    return pathlib.Path(configured) / "auth.json" if configured else AUTH


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
        return f if math.isfinite(f) and 0 <= f <= 100 else None
    except (TypeError, ValueError, OverflowError):
        return None
