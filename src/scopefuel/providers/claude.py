"""Claude Code (Anthropic) — OAuth usage API.

`~/.claude/.credentials.json` 의 access token 으로 `GET /api/oauth/usage` 를 호출한다.
`limits[]` 의 kind=weekly_scoped 항목이 **모델 한정** 한도(예: Fable 주간 100%)이며,
이걸 계정 한도와 섞으면 "계정이 막혔다"고 오독한다 — scope 로 구분해 둔다.

자격증명은 항상 파일에 있지는 않다. macOS 의 claude 는 로그인 방식에 따라 파일 대신
Keychain(`Claude Code-credentials`)에만 토큰을 두며, 그 경우 파일은 아예 생기지 않는다.
파일만 보면 로그인된 계정을 "미로그인"으로 오판해 gate 가 fail-closed 로 풀 전체를
막아버린다 — 파일을 우선하되 없으면 Keychain 으로 폴백한다.

토큰 갱신은 하지 않는다(실행 중인 claude 세션이 갱신한다). 만료 시 힌트만 남긴다.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import subprocess
import sys
import time

from ..http import request_json
from ..model import Bucket, ProviderResult, Scope

CREDENTIALS = pathlib.Path.home() / ".claude" / ".credentials.json"
KEYCHAIN_SERVICE = os.environ.get("SCOPEFUEL_CLAUDE_KEYCHAIN_SERVICE", "Claude Code-credentials")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"


def _read_file() -> str | None:
    try:
        return _credentials_path().read_text()
    except OSError:
        return None


def _credentials_path() -> pathlib.Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return pathlib.Path(configured) / ".credentials.json" if configured else CREDENTIALS


def _read_keychain() -> str | None:
    """macOS Keychain 의 자격증명 blob. 실패는 전부 '없음'으로 접는다(폴백이므로)."""
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _load_oauth() -> tuple[dict, str] | None:
    """(claudeAiOauth, 출처) — 파일 우선, 없거나 쓸 수 없으면 Keychain."""
    # reader 를 지연 호출한다 — 파일이 쓸 수 있으면 Keychain 을 건드리지 않는다.
    for reader, origin in ((_read_file, "file"), (_read_keychain, "keychain")):
        blob = reader()
        if not blob:
            continue
        try:
            payload = json.loads(blob)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        oauth = payload.get("claudeAiOauth")
        if isinstance(oauth, dict) and (oauth.get("accessToken") or "").strip():
            return oauth, origin
    return None


def fetch() -> ProviderResult:
    loaded = _load_oauth()
    if loaded is None:
        return ProviderResult(
            id="claude",
            error="자격증명 없음",
            hint=(
                f"{_credentials_path()} 없음, Keychain('{KEYCHAIN_SERVICE}')에서도 못 읽음 "
                "— claude 로그인 후 다시 시도"
            ),
        )
    oauth, origin = loaded
    token = oauth["accessToken"].strip()

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
        source="oauth-usage-api" if origin == "file" else f"oauth-usage-api+{origin}",
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
