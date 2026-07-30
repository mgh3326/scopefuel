"""Claude Code (Anthropic) — OAuth usage API.

`~/.claude/.credentials.json` 의 access token 으로 `GET /api/oauth/usage` 를 호출한다.
`limits[]` 의 kind=weekly_scoped 항목이 **모델 한정** 한도(예: Fable 주간 100%)이며,
이걸 계정 한도와 섞으면 "계정이 막혔다"고 오독한다 — scope 로 구분해 둔다.

토큰 갱신은 하지 않는다(실행 중인 claude 세션이 갱신한다). 만료 시 힌트만 남긴다.
"""

from __future__ import annotations

import json
import math
import pathlib
import time

from ..http import request_json
from ..model import Bucket, ProviderResult, Scope

CREDENTIALS = pathlib.Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"


def fetch() -> ProviderResult:
    if not CREDENTIALS.exists():
        return ProviderResult(
            id="claude",
            error="자격증명 파일 없음",
            hint=f"{CREDENTIALS} 없음 — claude 로그인 후 다시 시도",
        )
    oauth = json.loads(CREDENTIALS.read_text()).get("claudeAiOauth") or {}
    token = (oauth.get("accessToken") or "").strip()
    if not token:
        return ProviderResult(id="claude", error="accessToken 없음", hint="claude 재로그인 필요")

    raw = request_json(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "anthropic-beta": BETA_HEADER,
            "User-Agent": "scopefuel",
        },
    )

    buckets: list[Bucket] = []
    five = raw.get("five_hour") or {}
    seven = raw.get("seven_day") or {}
    buckets.append(
        Bucket(
            label="5h",
            window="5h",
            used_pct=_num(five.get("utilization")),
            resets_at=five.get("resets_at"),
            scope=Scope("account"),
            horizon="now",
        )
    )
    buckets.append(
        Bucket(
            label="7d all",
            window="7d",
            used_pct=_num(seven.get("utilization")),
            resets_at=seven.get("resets_at"),
            scope=Scope("account"),
            horizon="week",
        )
    )
    for limit in raw.get("limits") or []:
        model = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
        if not model:
            continue  # session/weekly_all 은 위에서 이미 account 스코프로 담았다
        buckets.append(
            Bucket(
                label=f"7d {model}",
                window="7d",
                used_pct=_num(limit.get("percent")),
                resets_at=limit.get("resets_at"),
                scope=Scope("model", model),
                horizon="week",
                note="active" if limit.get("is_active") else None,
            )
        )

    note = None
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at / 1000 < time.time():
        note = "access token 만료 — 실행 중인 claude 세션이 갱신하거나 재로그인 필요"
    extra = raw.get("extra_usage") or {}
    if extra.get("is_enabled"):
        note = f"{note + ' / ' if note else ''}extra usage {extra.get('utilization')}%"

    return ProviderResult(
        id="claude",
        plan=oauth.get("subscriptionType"),
        buckets=buckets,
        note=note,
        source="oauth-usage-api",
        raw=raw,
    )


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
        return f if math.isfinite(f) and 0 <= f <= 100 else None
    except (TypeError, ValueError, OverflowError):
        return None
