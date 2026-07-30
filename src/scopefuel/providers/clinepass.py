"""ClinePass — 최소 completions 프로브의 rate-limit 응답 헤더.

ClinePass 는 전용 사용량 API가 없으므로 가장 저렴한 모델에 ``max_tokens=1`` 요청을
딱 한 번 보낸다. 성공 본문은 사용하지 않고 rate-limit 헤더만 읽는다. HTTP 4xx/5xx도
헤더를 제공할 수 있으므로 응답 상태와 무관하게 헤더를 먼저 적용한다.

일부 티어는 rate-limit 헤더를 전혀 보내지 않는다. 이는 조회 실패가 아니며 빈 버킷과
``no data`` note 로 보고한다. 자격증명 값과 응답 본문은 raw 결과에도 보관하지 않는다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import urllib.error
import urllib.request
from email.message import Message

from ..model import NOW_HORIZON_MAX_S, Bucket, ProviderResult, Scope

USAGE_URL = "https://api.cline.bot/api/v1/chat/completions"
MODEL = "cline-pass/deepseek-v4-flash"
OPENCODE_AUTH = pathlib.Path.home() / ".local" / "share" / "opencode" / "auth.json"
CLINE_API_KEY_FILE = pathlib.Path.home() / ".config" / "cline" / "api-key"
TIMEOUT_S = 20.0

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|[smhd])", re.IGNORECASE)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def fetch() -> ProviderResult:
    key, key_source = _api_key()
    credential = {"source": key_source, "present": bool(key)}
    if not key:
        return ProviderResult(
            id="clinepass",
            error="API key 없음",
            hint=(
                "CLINE_API_KEY, ~/.local/share/opencode/auth.json의 cline-pass.key, "
                "또는 ~/.config/cline/api-key 필요"
            ),
            source="completions-probe",
            raw={"credential": credential},
        )

    try:
        status, headers = _probe(key)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        return ProviderResult(
            id="clinepass",
            error=f"completions probe 실패 ({type(exc).__name__})",
            hint="ClinePass 네트워크/로그인 상태를 확인하세요",
            source="completions-probe",
            raw={"credential": credential},
        )

    rate_headers = _rate_limit_headers(headers)
    raw = {
        "status": status,
        "credential": credential,
        "rate_limit_headers": rate_headers,
    }
    buckets = _buckets(rate_headers)
    if not buckets:
        status_note = f", HTTP {status}" if status else ""
        return ProviderResult(
            id="clinepass",
            buckets=[],
            note=(
                f"no data — rate-limit 헤더 없음{status_note}; "
                "이 티어는 헤더를 제공하지 않을 수 있음"
            ),
            source="completions-probe",
            raw=raw,
        )

    note = f"HTTP {status} 응답의 rate-limit 헤더 적용" if status >= 400 else None
    return ProviderResult(
        id="clinepass",
        buckets=buckets,
        note=note,
        source="completions-probe",
        raw=raw,
    )


def _api_key() -> tuple[str | None, str | None]:
    if key := os.environ.get("CLINE_API_KEY", "").strip():
        return key, "env:CLINE_API_KEY"

    if OPENCODE_AUTH.exists():
        try:
            auth = json.loads(OPENCODE_AUTH.read_text())
        except (OSError, json.JSONDecodeError):
            auth = {}
        cline_pass = auth.get("cline-pass") if isinstance(auth, dict) else None
        if isinstance(cline_pass, dict) and (key := str(cline_pass.get("key") or "").strip()):
            return key, "opencode:cline-pass.key"

    if CLINE_API_KEY_FILE.exists():
        try:
            key = CLINE_API_KEY_FILE.read_text().strip()
        except OSError:
            key = ""
        if key:
            return key, "file:~/.config/cline/api-key"

    return None, None


def _probe(key: str) -> tuple[int, Message]:
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "stream": False,
        }
    ).encode()
    request = urllib.request.Request(
        USAGE_URL,
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
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            response.read()
            return response.status, response.headers
    except urllib.error.HTTPError as exc:
        # ClinePass 는 실패 응답에도 rate-limit 헤더를 실을 수 있다. 본문은 버린다.
        exc.read()
        return exc.code, exc.headers


def _rate_limit_headers(headers: Message) -> dict[str, str]:
    return {
        name.lower(): value.strip()
        for name, value in headers.items()
        if "ratelimit" in name.lower() or "rate-limit" in name.lower()
    }


def _buckets(headers: dict[str, str]) -> list[Bucket]:
    """remaining 헤더를 같은 이름의 limit/reset 헤더와 짝지어 버킷으로 바꾼다."""
    buckets: list[Bucket] = []
    for remaining_name, remaining_raw in sorted(headers.items()):
        marker = _remaining_marker(remaining_name)
        if marker is None:
            continue
        before, after = marker
        limit_name = f"{before}limit{after}"
        reset_name = f"{before}reset{after}"
        limit = _number(headers.get(limit_name))
        remaining = _number(remaining_raw)
        if limit is None or remaining is None or limit <= 0:
            continue

        reset_at, reset_seconds = _reset(headers.get(reset_name))
        window = _window_label(reset_seconds)
        kind = after.strip("-_") or "quota"
        used_pct = round(max(0.0, min(100.0, (limit - remaining) / limit * 100)), 3)
        buckets.append(
            Bucket(
                label=kind.replace("-", " "),
                window=window,
                used_pct=used_pct,
                resets_at=reset_at,
                scope=Scope("account"),
                horizon="now"
                if reset_seconds is not None and reset_seconds <= NOW_HORIZON_MAX_S
                else "week",
                note=f"remaining {remaining:g}/{limit:g}",
            )
        )
    return buckets


def _remaining_marker(name: str) -> tuple[str, str] | None:
    match = re.search(r"remaining", name)
    if not match:
        return None
    return name[: match.start()], name[match.end() :]


def _number(value: str | None) -> float | None:
    if not value or not (match := _NUMBER.search(value.replace(",", ""))):
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def _reset(value: str | None) -> tuple[str | None, float | None]:
    if not value:
        return None, None
    raw = value.strip()

    parts = _DURATION_PART.findall(raw)
    if parts and "".join(f"{amount}{unit}" for amount, unit in parts).lower() == re.sub(
        r"\s+", "", raw
    ).lower():
        factors = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
        seconds = sum(float(amount) * factors[unit.lower()] for amount, unit in parts)
        reset_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=seconds)
        return reset_at.isoformat(), seconds

    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None and parsed.tzinfo is not None:
        seconds = max(0.0, (parsed.astimezone(dt.UTC) - dt.datetime.now(dt.UTC)).total_seconds())
        return parsed.astimezone(dt.UTC).isoformat(), seconds

    numeric = _number(raw)
    if numeric is None:
        return None, None
    if numeric >= 1_000_000_000:
        reset_at = dt.datetime.fromtimestamp(numeric, dt.UTC)
        seconds = max(0.0, (reset_at - dt.datetime.now(dt.UTC)).total_seconds())
    else:
        seconds = max(0.0, numeric)
        reset_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=seconds)
    return reset_at.isoformat(), seconds


def _window_label(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{max(1, round(seconds))}s"
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))}m"
    if seconds < 86400:
        return f"{max(1, round(seconds / 3600))}h"
    return f"{max(1, round(seconds / 86400))}d"
