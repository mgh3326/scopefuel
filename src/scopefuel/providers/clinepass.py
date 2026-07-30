"""ClinePass — 대시보드와 같은 usage-limits API.

정상 경로는 토큰을 소비하지 않는 ``GET /users/me/plan/usage-limits`` 한 번이다.
응답의 five_hour/weekly/monthly 사용률을 account 버킷으로 변환한다.

endpoint가 배포 차이로 존재하지 않는 404/405/501에서만 기존 completions rate-limit
프로브를 호환 폴백으로 한 번 시도한다. 인증 실패는 어느 경로든 warning으로 분리하고,
자격증명 값·오류 응답 본문·계정 식별자는 raw 결과에도 보관하지 않는다.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import re
import urllib.error
import urllib.request
from email.message import Message

from ..model import NOW_HORIZON_MAX_S, Bucket, ProviderResult, Scope

USAGE_URL = "https://api.cline.bot/api/v1/users/me/plan/usage-limits"
COMPLETIONS_URL = "https://api.cline.bot/api/v1/chat/completions"
FALLBACK_MODEL = "cline-pass/deepseek-v4-flash"
OPENCODE_AUTH = pathlib.Path.home() / ".local" / "share" / "opencode" / "auth.json"
CLINE_API_KEY_FILE = pathlib.Path.home() / ".config" / "cline" / "api-key"
TIMEOUT_S = 20.0
AUTH_FAILURE_STATUSES = frozenset({401, 403})
FALLBACK_STATUSES = frozenset({404, 405, 501})
INVALID_CREDENTIAL = "invalid credential format"
PRIMARY_SOURCE = "primary(usage-limits)"
FALLBACK_SOURCE = "fallback(probe)"
LIMIT_ORDER = ("five_hour", "weekly", "monthly")
LIMIT_MAP = {
    "five_hour": ("5h", "now"),
    "weekly": ("7d", "week"),
    "monthly": ("30d", "month"),
}
_CREDENTIAL_SOURCES = frozenset(
    {"env:CLINE_API_KEY", "opencode:cline-pass.key", "file:~/.config/cline/api-key"}
)
_FALLBACK_RATE_HEADERS = frozenset(
    {
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-tokens",
    }
)

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
            source=PRIMARY_SOURCE,
            raw=_safe_raw(credential=credential),
        )
    if not _valid_credential(key):
        return _invalid_credential(credential, source=PRIMARY_SOURCE)

    try:
        status, payload_bytes = _request_usage(key)
    except ValueError:
        return _invalid_credential(credential, source=PRIMARY_SOURCE)
    except Exception as exc:
        return ProviderResult(
            id="clinepass",
            error=f"usage-limits 조회 실패 ({type(exc).__name__})",
            hint="ClinePass 네트워크/로그인 상태를 확인하세요",
            source=PRIMARY_SOURCE,
            raw=_safe_raw(credential=credential),
        )

    if status in AUTH_FAILURE_STATUSES:
        return _warning_result(
            warning=f"인증 실패 (HTTP {status}) — 키를 확인하세요",
            credential=credential,
            source=PRIMARY_SOURCE,
            status=status,
        )
    if status in FALLBACK_STATUSES:
        return _fetch_fallback(key, credential, primary_status=status)
    if status >= 400:
        return ProviderResult(
            id="clinepass",
            error=f"usage-limits HTTP {status}",
            hint="ClinePass 서비스 상태를 확인하세요",
            source=PRIMARY_SOURCE,
            raw=_safe_raw(status=status, credential=credential),
        )

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:
        return ProviderResult(
            id="clinepass",
            error=f"usage-limits 응답 처리 실패 ({type(exc).__name__})",
            hint="ClinePass 응답 JSON 형식을 확인하세요",
            source=PRIMARY_SOURCE,
            raw=_safe_raw(status=status, credential=credential),
        )

    if not isinstance(payload, dict):
        return ProviderResult(
            id="clinepass",
            error="usage-limits 응답이 JSON object가 아님",
            source=PRIMARY_SOURCE,
            raw=_safe_raw(status=status, credential=credential),
        )

    try:
        buckets, note, safe_limits, validation_warnings = _usage_buckets(payload)
    except Exception as exc:
        return ProviderResult(
            id="clinepass",
            error=f"usage-limits 버킷 처리 실패 ({type(exc).__name__})",
            hint="ClinePass limits 응답 형식을 확인하세요",
            source=PRIMARY_SOURCE,
            raw=_safe_raw(status=status, credential=credential),
        )

    raw = _safe_raw(
        status=status,
        credential=credential,
        limits=safe_limits,
        success=payload.get("success"),
    )
    if payload.get("success") is False:
        return ProviderResult(
            id="clinepass",
            error="usage-limits 응답 success=false",
            source=PRIMARY_SOURCE,
            raw=raw,
        )
    if validation_warnings:
        return _warning_result(
            warning=f"데이터 이상 — {'; '.join(validation_warnings)}",
            credential=credential,
            source=PRIMARY_SOURCE,
            status=status,
            buckets=buckets,
            note=note,
            limits=safe_limits,
        )

    return ProviderResult(
        id="clinepass",
        buckets=buckets,
        note=note,
        source=PRIMARY_SOURCE,
        raw=raw,
    )


def _request_usage(key: str) -> tuple[int, bytes]:
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "scopefuel",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        exc.read()  # 오류 본문은 계정 정보를 포함할 수 있어 폐기한다.
        return exc.code, b""


def _usage_buckets(
    payload: dict,
) -> tuple[list[Bucket], str | None, list[dict[str, object]], list[str]]:
    data = payload.get("data")
    raw_limits = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(raw_limits, list):
        return [], "no data — usage-limits 응답에 limits 배열 없음", [], []

    by_type: dict[str, dict] = {}
    validation_warnings: list[str] = []
    for item in raw_limits:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if isinstance(kind, str) and kind in LIMIT_MAP:
            if kind in by_type:
                validation_warnings.append(f"{kind}: duplicate type (첫 값 유지)")
            else:
                by_type[kind] = item

    buckets: list[Bucket] = []
    safe_limits: list[dict[str, object]] = []
    for kind in LIMIT_ORDER:
        item = by_type.get(kind)
        if item is None:
            continue
        window, horizon = LIMIT_MAP[kind]
        used_pct, percent_problem = _percent_used(item.get("percentUsed"))
        resets_at = _iso_reset(item.get("resetsAt"))
        problems: list[str] = []
        if percent_problem:
            problems.append(percent_problem)
            validation_warnings.append(f"{kind}: {percent_problem}")
        if resets_at is None:
            problems.append("resetsAt 없음/비-tz ISO8601")
        elif _is_past_reset(resets_at):
            reset_problem = "resetsAt 과거(stale)"
            problems.append(reset_problem)
            validation_warnings.append(f"{kind}: {reset_problem}")
        buckets.append(
            Bucket(
                label=kind,
                window=window,
                used_pct=used_pct,
                resets_at=resets_at,
                scope=Scope("account"),
                horizon=horizon,  # type: ignore[arg-type]
                note=" / ".join(problems) or None,
            )
        )
        safe_limits.append({"type": kind, "percentUsed": used_pct, "resetsAt": resets_at})

    if not buckets:
        reason = "limits 비어 있음" if not raw_limits else "알려진 limit type 없음"
        return [], f"no data — usage-limits {reason}", [], []

    missing = [kind for kind in LIMIT_ORDER if kind not in by_type]
    note = f"partial data — limits 누락: {', '.join(missing)}" if missing else None
    return buckets, note, safe_limits, validation_warnings


def _percent_used(value: object) -> tuple[float | None, str | None]:
    if isinstance(value, bool):
        return None, "percentUsed 숫자 아님"
    if isinstance(value, str):
        return None, "percentUsed 숫자 문자열 허용 안 함"
    if not isinstance(value, (int, float)):
        return None, "percentUsed 숫자 아님"
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return None, "percentUsed 숫자 한도 초과"
    if not math.isfinite(parsed):
        return None, "percentUsed 유한 숫자 아님"
    if parsed < 0:
        return None, f"percentUsed 음수 ({parsed:g})"
    if parsed > 100:
        return None, f"percentUsed 100 초과 ({parsed:g})"
    return parsed, None


def _iso_reset(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC).isoformat()


def _is_past_reset(value: str) -> bool:
    return dt.datetime.fromisoformat(value) < dt.datetime.now(dt.UTC)


def _warning_result(
    *,
    warning: str,
    credential: dict[str, object],
    source: str,
    status: int,
    fallback_status: int | None = None,
    buckets: list[Bucket] | None = None,
    note: str | None = None,
    limits: list[dict[str, object]] | None = None,
) -> ProviderResult:
    return ProviderResult(
        id="clinepass",
        buckets=buckets or [],
        warning=warning,
        note=note,
        source=source,
        raw=_safe_raw(
            status=status,
            fallback_status=fallback_status,
            credential=credential,
            limits=limits,
            buckets=buckets if limits is None else None,
        ),
    )


def _fetch_fallback(key: str, credential: dict[str, object], *, primary_status: int) -> ProviderResult:
    try:
        status, headers = _probe(key)
    except ValueError:
        return _invalid_credential(credential, source=FALLBACK_SOURCE)
    except Exception as exc:
        return ProviderResult(
            id="clinepass",
            error=f"completions fallback 실패 ({type(exc).__name__})",
            hint=f"usage-limits HTTP {primary_status}; ClinePass 서비스 상태를 확인하세요",
            source=FALLBACK_SOURCE,
            raw=_safe_raw(status=primary_status, credential=credential),
        )

    if status in AUTH_FAILURE_STATUSES:
        return _warning_result(
            warning=f"인증 실패 (HTTP {status}) — 키를 확인하세요",
            credential=credential,
            source=FALLBACK_SOURCE,
            status=primary_status,
            fallback_status=status,
        )

    try:
        rate_headers = _rate_limit_headers(headers)
        buckets = _buckets(rate_headers)
    except Exception as exc:
        return ProviderResult(
            id="clinepass",
            error=f"completions fallback 헤더 처리 실패 ({type(exc).__name__})",
            source=FALLBACK_SOURCE,
            raw=_safe_raw(
                status=primary_status,
                fallback_status=status,
                credential=credential,
            ),
        )

    raw = _safe_raw(
        status=primary_status,
        fallback_status=status,
        credential=credential,
        buckets=buckets,
    )
    if not (200 <= status < 300):
        return _warning_result(
            warning=f"completions fallback HTTP {status} — 정상으로 간주하지 않음",
            credential=credential,
            source=FALLBACK_SOURCE,
            status=primary_status,
            fallback_status=status,
            buckets=buckets,
            note=f"usage-limits HTTP {primary_status}; fallback 응답 실패",
        )
    if not buckets:
        return ProviderResult(
            id="clinepass",
            note=(
                f"no data — usage-limits HTTP {primary_status}; "
                f"completions fallback rate-limit 헤더 없음 (HTTP {status})"
            ),
            source=FALLBACK_SOURCE,
            raw=raw,
        )
    return ProviderResult(
        id="clinepass",
        buckets=buckets,
        note=f"usage-limits HTTP {primary_status}; completions fallback 적용",
        source=FALLBACK_SOURCE,
        raw=raw,
    )


def _valid_credential(key: str) -> bool:
    """Authorization 헤더에 안전한 공백 없는 printable ASCII만 허용한다."""
    return bool(key) and all(0x21 <= ord(char) <= 0x7E for char in key)


def _invalid_credential(credential: dict[str, object], *, source: str) -> ProviderResult:
    return ProviderResult(
        id="clinepass",
        error=INVALID_CREDENTIAL,
        hint="ClinePass API key 형식을 확인하세요",
        source=source,
        raw=_safe_raw(credential=credential),
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
            "model": FALLBACK_MODEL,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "stream": False,
        }
    ).encode()
    request = urllib.request.Request(
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
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            response.read()
            return response.status, response.headers
    except urllib.error.HTTPError as exc:
        # ClinePass 는 실패 응답에도 rate-limit 헤더를 실을 수 있다. 본문은 버린다.
        exc.read()
        return exc.code, exc.headers


def _rate_limit_headers(headers: Message | None) -> dict[str, str]:
    if headers is None:
        return {}
    return {
        name.lower(): value.strip()
        for name, value in headers.items()
        if name.lower() in _FALLBACK_RATE_HEADERS
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
                horizon="now" if reset_seconds is not None and reset_seconds <= NOW_HORIZON_MAX_S else "week",
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
    if not value or not (match := _NUMBER.fullmatch(value.replace(",", "").strip())):
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
    if (
        parts
        and "".join(f"{amount}{unit}" for amount, unit in parts).lower() == re.sub(r"\s+", "", raw).lower()
    ):
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
        try:
            reset_at = dt.datetime.fromtimestamp(numeric, dt.UTC)
        except (OverflowError, OSError, ValueError):
            return None, None
        seconds = max(0.0, (reset_at - dt.datetime.now(dt.UTC)).total_seconds())
    else:
        seconds = max(0.0, numeric)
        try:
            reset_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=seconds)
        except OverflowError:
            return None, None
    return reset_at.isoformat(), seconds


def _safe_raw(
    *,
    credential: dict[str, object],
    status: int | None = None,
    fallback_status: int | None = None,
    limits: list[dict[str, object]] | None = None,
    buckets: list[Bucket] | None = None,
    success: object = None,
) -> dict[str, object]:
    """두 응답 경로 모두 같은 allowlist로 raw를 재구성한다."""
    credential_source = credential.get("source")
    safe_credential = {
        "source": credential_source if credential_source in _CREDENTIAL_SOURCES else None,
        "present": bool(credential.get("present")),
    }
    raw: dict[str, object] = {"credential": safe_credential}
    if status is not None:
        raw["status"] = status
    if fallback_status is not None:
        raw["fallback_status"] = fallback_status
    if limits is not None:
        raw["data"] = {
            "limits": [
                {
                    "type": item["type"],
                    "percentUsed": item["percentUsed"],
                    "resetsAt": item["resetsAt"],
                }
                for item in limits
                if item.get("type") in LIMIT_MAP
            ]
        }
    if buckets is not None:
        raw["data"] = {
            "buckets": [
                {
                    "label": bucket.label,
                    "window": bucket.window,
                    "used_pct": bucket.used_pct,
                    "resets_at": bucket.resets_at,
                }
                for bucket in buckets
            ]
        }
    if isinstance(success, bool):
        raw["success"] = success
    return raw


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
