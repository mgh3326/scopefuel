"""Grok (xAI) consumer subscription — read-only usage from grok.com REST.

조사 결과 (2026-07-31, 공개 SPA 번들 + 최소 인증 읽기):

- 웹앱이 쓰는 **주 사용량 소스**는 ``POST /rest/rate-limits``
  (SPA ``rateLimitsApi.rateLimitsGetRateLimits``). body: ``requestKind`` / ``modelName``.
  응답 필드: ``remainingQueries``, ``totalQueries``, ``windowSizeSeconds``,
  ``waitTimeSeconds``, 선택적으로 tokens·effort 하위 창.
- ``GET /rest/usage/free-usage-gates`` 는 무료 티어 product gate
  (chat/imagine/voice/build 의 allowance/remaining 문자열).
- ``GET /rest/subscriptions`` 는 플랜 tier/status 만 준다 (사용률 없음).
- CLI 가 저장하는 OAuth2 access token(``~/.grok/auth.json``, 키 prefix
  ``https://auth.x.ai::``) 으로 ``/rest/rate-limits`` 를 치면 서버가
  ``oauth2-auth-forbidden`` 로 거부한다. 추론(completions) 으로 rate-limit
  헤더를 찍는 우회는 하지 않는다 — 안전 메타데이터가 없으면 graceful no-data.

토큰·user/team 식별자·이메일·JWT claim 값은 raw/로그/픽스처에 넣지 않는다.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import re
import urllib.error
import urllib.request

from ..model import Bucket, ProviderResult, Scope, horizon_for, window_label

AUTH = pathlib.Path.home() / ".grok" / "auth.json"
AUTH_KEY_PREFIX = "https://auth.x.ai::"
BASE = "https://grok.com"
RATE_LIMITS_URL = f"{BASE}/rest/rate-limits"
FREE_GATES_URL = f"{BASE}/rest/usage/free-usage-gates"
SUBSCRIPTIONS_URL = f"{BASE}/rest/subscriptions"
TIMEOUT_S = 20.0
USER_AGENT = "scopefuel"

# SPA toast / client 가 쓰는 requestKind 후보. OAuth2 금지면 첫 호출에서 중단한다.
REQUEST_KINDS = ("DEFAULT", "DEEPSEARCH", "THINK")
SOURCE_RATE_LIMITS = "rate-limits"
SOURCE_FREE_GATES = "free-usage-gates"
SOURCE_NO_DATA = "no-data"
OAUTH2_FORBIDDEN_MARKERS = (
    "oauth2-auth-forbidden",
    "cannot be performed by oauth2 token",
)

_TIER_RE = re.compile(r"^SUBSCRIPTION_TIER_(.+)$")
_STATUS_ACTIVE = "SUBSCRIPTION_STATUS_ACTIVE"


def fetch() -> ProviderResult:
    token, auth_meta = _load_token()
    credential = {"source": "file:~/.grok/auth.json", "present": bool(token), **auth_meta}
    if not token:
        return ProviderResult(
            id="grok",
            error="자격증명 없음",
            hint=f"{AUTH} 에 {AUTH_KEY_PREFIX}* 항목의 key 가 필요 — `grok login` 후 재시도",
            source=SOURCE_NO_DATA,
            raw=_safe_raw(credential=credential),
        )

    plan = _fetch_plan(token)

    try:
        rate_status, rate_payload, rate_err_msg = _post_rate_limits(token, {"requestKind": "DEFAULT"})
    except Exception as exc:
        return ProviderResult(
            id="grok",
            plan=plan,
            error=f"rate-limits 조회 실패 ({type(exc).__name__})",
            hint="네트워크 또는 grok.com 상태를 확인하세요",
            source=SOURCE_RATE_LIMITS,
            raw=_safe_raw(credential=credential, plan=plan),
        )

    if rate_status == 200 and isinstance(rate_payload, dict):
        return _from_rate_limits(
            token,
            plan=plan,
            credential=credential,
            first_kind="DEFAULT",
            first_payload=rate_payload,
        )

    if rate_status in (401, 403):
        if rate_status == 403 and _is_oauth2_forbidden(rate_err_msg):
            return _free_gates_or_no_data(
                token,
                plan=plan,
                credential=credential,
                rate_status=rate_status,
                rate_note="CLI OAuth2 token 은 /rest/rate-limits 호출이 거부됨 (oauth2-auth-forbidden)",
            )
        return ProviderResult(
            id="grok",
            plan=plan,
            warning=f"인증 실패 (HTTP {rate_status}) — `grok login` 후 재시도",
            source=SOURCE_RATE_LIMITS,
            raw=_safe_raw(credential=credential, plan=plan, rate_limits_status=rate_status),
        )

    if rate_status == 429:
        return ProviderResult(
            id="grok",
            plan=plan,
            error="rate-limits HTTP 429",
            hint="잠시 후 다시 시도하세요",
            source=SOURCE_RATE_LIMITS,
            raw=_safe_raw(credential=credential, plan=plan, rate_limits_status=rate_status),
        )

    if rate_status is not None and rate_status >= 400:
        # 501/404 등 — free gates 폴백 후 no-data
        return _free_gates_or_no_data(
            token,
            plan=plan,
            credential=credential,
            rate_status=rate_status,
            rate_note=f"rate-limits HTTP {rate_status}",
        )

    return ProviderResult(
        id="grok",
        plan=plan,
        error="rate-limits 응답 처리 실패",
        source=SOURCE_RATE_LIMITS,
        raw=_safe_raw(credential=credential, plan=plan, rate_limits_status=rate_status),
    )


def _from_rate_limits(
    token: str,
    *,
    plan: str | None,
    credential: dict,
    first_kind: str,
    first_payload: dict,
) -> ProviderResult:
    """DEFAULT 성공 시 추가 kind 를 best-effort 로 모아 account 버킷으로 매핑."""
    payloads: dict[str, dict] = {first_kind: first_payload}
    for kind in REQUEST_KINDS:
        if kind == first_kind:
            continue
        try:
            status, payload, _msg = _post_rate_limits(token, {"requestKind": kind})
        except Exception:
            continue
        if status == 200 and isinstance(payload, dict):
            payloads[kind] = payload
        elif status in (401, 403):
            break

    buckets: list[Bucket] = []
    problems: list[str] = []
    redacted_windows: dict[str, dict] = {}
    for kind, payload in payloads.items():
        redacted_windows[kind] = _redact_rate_limit_payload(payload)
        kind_buckets, kind_problems = _buckets_from_rate_limit(payload, kind=kind)
        buckets.extend(kind_buckets)
        problems.extend(kind_problems)

    if not buckets:
        note = "no data — rate-limits 응답에서 사용률을 계산할 수 없음"
        if problems:
            note = f"{note}: {'; '.join(problems)}"
        return ProviderResult(
            id="grok",
            plan=plan,
            buckets=[],
            note=note,
            source=SOURCE_RATE_LIMITS,
            raw=_safe_raw(
                credential=credential,
                plan=plan,
                rate_limits=redacted_windows,
            ),
        )

    note = None
    warning = None
    if problems:
        if any(b.used_pct is not None for b in buckets):
            note = "partial data — " + "; ".join(problems)
        else:
            warning = "데이터 이상 — " + "; ".join(problems)
    return ProviderResult(
        id="grok",
        plan=plan,
        buckets=buckets,
        note=note,
        warning=warning,
        source=SOURCE_RATE_LIMITS,
        raw=_safe_raw(
            credential=credential,
            plan=plan,
            rate_limits=redacted_windows,
        ),
    )


def _free_gates_or_no_data(
    token: str,
    *,
    plan: str | None,
    credential: dict,
    rate_status: int | None,
    rate_note: str,
) -> ProviderResult:
    try:
        status, payload = _get_json(token, FREE_GATES_URL)
    except Exception as exc:
        return ProviderResult(
            id="grok",
            plan=plan,
            note=f"no data — {rate_note}; free-usage-gates 조회 실패 ({type(exc).__name__})",
            source=SOURCE_NO_DATA,
            raw=_safe_raw(
                credential=credential,
                plan=plan,
                rate_limits_status=rate_status,
            ),
        )

    if status in (401, 403):
        return ProviderResult(
            id="grok",
            plan=plan,
            warning=f"인증 실패 (HTTP {status}) — `grok login` 후 재시도",
            source=SOURCE_FREE_GATES,
            raw=_safe_raw(
                credential=credential,
                plan=plan,
                rate_limits_status=rate_status,
                free_gates_status=status,
            ),
        )
    if status != 200 or not isinstance(payload, dict):
        return ProviderResult(
            id="grok",
            plan=plan,
            note=f"no data — {rate_note}; free-usage-gates HTTP {status}",
            source=SOURCE_NO_DATA,
            raw=_safe_raw(
                credential=credential,
                plan=plan,
                rate_limits_status=rate_status,
                free_gates_status=status,
            ),
        )

    buckets, problems, usable = _buckets_from_free_gates(payload)
    redacted = _redact_free_gates(payload)
    if not usable:
        return ProviderResult(
            id="grok",
            plan=plan,
            buckets=[],
            note=(
                f"no data — {rate_note}; free-usage-gates 는 allowance=0 (유료 구독이거나 free gate 미적용)"
            ),
            source=SOURCE_NO_DATA,
            raw=_safe_raw(
                credential=credential,
                plan=plan,
                rate_limits_status=rate_status,
                free_gates=redacted,
            ),
        )

    note = f"{rate_note}; free-usage-gates 사용"
    if problems:
        note = f"{note}; partial: {'; '.join(problems)}"
    return ProviderResult(
        id="grok",
        plan=plan,
        buckets=buckets,
        note=note,
        source=SOURCE_FREE_GATES,
        raw=_safe_raw(
            credential=credential,
            plan=plan,
            rate_limits_status=rate_status,
            free_gates=redacted,
        ),
    )


def _buckets_from_rate_limit(payload: dict, *, kind: str) -> tuple[list[Bucket], list[str]]:
    buckets: list[Bucket] = []
    problems: list[str] = []
    label = kind.lower()
    main, problem = _window_bucket(
        payload,
        label=label,
        scope=Scope("account"),
    )
    if main is not None:
        buckets.append(main)
    if problem:
        problems.append(f"{label}: {problem}")

    for effort_key, effort_label in (
        ("lowEffortRateLimits", f"{label}-low-effort"),
        ("highEffortRateLimits", f"{label}-high-effort"),
    ):
        nested = payload.get(effort_key)
        if not isinstance(nested, dict):
            continue
        # SPA effort 하위 객체는 remainingQueries 만 주는 경우가 많다.
        # total 없이 used_pct 를 추측하지 않으며, 계산 가능한 쌍이 있을 때만 버킷을 만든다.
        rem_q, tot_q = _num(nested.get("remainingQueries")), _num(nested.get("totalQueries"))
        rem_t, tot_t = _num(nested.get("remainingTokens")), _num(nested.get("totalTokens"))
        has_pair = (rem_q is not None and tot_q is not None) or (rem_t is not None and tot_t is not None)
        if not has_pair:
            continue
        effort_bucket, effort_problem = _window_bucket(
            nested,
            label=effort_label,
            scope=Scope("model", effort_label),
            parent_window=payload.get("windowSizeSeconds"),
        )
        if effort_bucket is not None:
            buckets.append(effort_bucket)
        if effort_problem:
            problems.append(f"{effort_label}: {effort_problem}")
    return buckets, problems


def _window_bucket(
    payload: dict,
    *,
    label: str,
    scope: Scope,
    parent_window: object | None = None,
) -> tuple[Bucket | None, str | None]:
    remaining_q = _num(payload.get("remainingQueries"))
    total_q = _num(payload.get("totalQueries"))
    remaining_t = _num(payload.get("remainingTokens"))
    total_t = _num(payload.get("totalTokens"))
    wait_s = _num(payload.get("waitTimeSeconds"))
    window_s = _num(payload.get("windowSizeSeconds"))
    if window_s is None:
        window_s = _num(parent_window)

    used_pct: float | None = None
    problem: str | None = None
    unit = "queries"
    if total_q is not None and remaining_q is not None:
        used_pct, problem = _used_pct_from_remaining(remaining_q, total_q, unit="queries")
    elif total_t is not None and remaining_t is not None:
        unit = "tokens"
        used_pct, problem = _used_pct_from_remaining(remaining_t, total_t, unit="tokens")
    elif remaining_q is None and remaining_t is None and total_q is None and total_t is None:
        # 완전 빈 창 — 버킷 자체를 만들지 않음
        return None, None
    else:
        problem = "remaining/total 쌍 없음 — used_pct 계산 불가"

    if used_pct is None and problem is None and (remaining_q is not None or total_q is not None):
        problem = "queries 불완전 — used_pct 계산 불가"

    window = window_label(window_s) if window_s is not None else "?"
    horizon = horizon_for(window_s)
    resets_at = _resets_at(payload, wait_s)
    note_parts = []
    if unit == "tokens" and used_pct is not None:
        note_parts.append("tokens 기준")
    if problem:
        note_parts.append(problem)

    return (
        Bucket(
            label=label,
            window=window,
            used_pct=used_pct,
            resets_at=resets_at,
            scope=scope,
            horizon=horizon,
            note="; ".join(note_parts) if note_parts else None,
        ),
        problem,
    )


def _used_pct_from_remaining(remaining: float, total: float, *, unit: str) -> tuple[float | None, str | None]:
    if total <= 0:
        return None, f"{unit} total<=0"
    if remaining < 0:
        return None, f"{unit} remaining 음수"
    if remaining > total:
        return None, f"{unit} remaining>total"
    used = total - remaining
    pct = used / total * 100.0
    if not math.isfinite(pct):
        return None, f"{unit} used_pct 비유한"
    if pct < 0 or pct > 100:
        return None, f"{unit} used_pct 범위 밖 ({pct:g})"
    return pct, None


def _resets_at(payload: dict, wait_s: float | None) -> str | None:
    next_at = payload.get("nextAvailableAt")
    if isinstance(next_at, str) and next_at.strip():
        parsed = _parse_iso(next_at.strip())
        if parsed is not None:
            return parsed
        return None
    if wait_s is None:
        return None
    if wait_s < 0 or not math.isfinite(wait_s):
        return None
    # waitTimeSeconds=0 은 "지금 사용 가능" — reset 시각을 꾸며내지 않는다
    if wait_s == 0:
        return None
    now = dt.datetime.now(dt.UTC)
    return (now + dt.timedelta(seconds=wait_s)).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> str | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _buckets_from_free_gates(payload: dict) -> tuple[list[Bucket], list[str], bool]:
    buckets: list[Bucket] = []
    problems: list[str] = []
    usable = False
    for product in ("chat", "imagine", "voice", "build"):
        gate = payload.get(product)
        if not isinstance(gate, dict):
            continue
        allowance = _num(gate.get("allowance"))
        remaining = _num(gate.get("remaining"))
        if allowance is None or remaining is None:
            problems.append(f"{product}: allowance/remaining 파싱 실패")
            continue
        if allowance <= 0:
            continue
        usable = True
        used_pct, problem = _used_pct_from_remaining(remaining, allowance, unit=product)
        if problem:
            problems.append(f"{product}: {problem}")
        buckets.append(
            Bucket(
                label=product,
                window="?",
                used_pct=used_pct,
                resets_at=None,
                scope=Scope("group", product),
                horizon="week",
                note=problem,
            )
        )
    return buckets, problems, usable


def _fetch_plan(token: str) -> str | None:
    try:
        status, payload = _get_json(token, SUBSCRIPTIONS_URL)
    except Exception:
        return None
    if status != 200 or not isinstance(payload, dict):
        return None
    subs = payload.get("subscriptions")
    if not isinstance(subs, list) or not subs:
        return None
    # 활성 구독 우선
    chosen = None
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        if sub.get("status") == _STATUS_ACTIVE:
            chosen = sub
            break
        if chosen is None:
            chosen = sub
    if not isinstance(chosen, dict):
        return None
    tier = chosen.get("tier")
    if not isinstance(tier, str):
        return None
    match = _TIER_RE.match(tier)
    if match:
        return match.group(1).lower().replace("_", " ")
    return tier.lower()


def _load_token() -> tuple[str | None, dict]:
    """auth.json 에서 auth.x.ai 항목의 key 만 읽는다. 식별자 값은 반환하지 않는다."""
    meta = {
        "auth_key_prefix_match": False,
        "key_field_present": False,
        "user_id_field_present": False,
        "team_id_field_present": False,
    }
    if not AUTH.exists():
        return None, meta
    try:
        data = json.loads(AUTH.read_text())
    except (OSError, json.JSONDecodeError):
        return None, meta
    if not isinstance(data, dict):
        return None, meta
    for key, entry in data.items():
        if not (isinstance(key, str) and key.startswith(AUTH_KEY_PREFIX)):
            continue
        meta["auth_key_prefix_match"] = True
        if not isinstance(entry, dict):
            continue
        meta["user_id_field_present"] = "user_id" in entry and bool(entry.get("user_id"))
        meta["team_id_field_present"] = "team_id" in entry and bool(entry.get("team_id"))
        token = entry.get("key")
        if isinstance(token, str) and token.strip():
            meta["key_field_present"] = True
            return token.strip(), meta
        meta["key_field_present"] = "key" in entry
        return None, meta
    return None, meta


def _post_rate_limits(token: str, body: dict) -> tuple[int | None, dict | None, str | None]:
    status, raw = _request(token, RATE_LIMITS_URL, method="POST", body=body)
    if status is None:
        return None, None, None
    msg = None
    payload = None
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return status, None, None
        if isinstance(parsed, dict):
            payload = parsed
            message = parsed.get("message")
            if isinstance(message, str):
                msg = message
    return status, payload, msg


def _get_json(token: str, url: str) -> tuple[int | None, dict | list | None]:
    status, raw = _request(token, url, method="GET")
    if status is None:
        return None, None
    if not raw:
        return status, None
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return status, None
    if isinstance(parsed, (dict, list)):
        return status, parsed
    return status, None


def _request(
    token: str,
    url: str,
    *,
    method: str,
    body: dict | None = None,
) -> tuple[int | None, bytes]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _is_oauth2_forbidden(message: str | None) -> bool:
    if not message:
        return False
    lower = message.lower()
    return any(marker in lower for marker in OAUTH2_FORBIDDEN_MARKERS)


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
        except (OverflowError, ValueError):
            return None
        return f if math.isfinite(f) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            f = float(text)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _redact_rate_limit_payload(payload: dict) -> dict:
    """사용량 수치 필드만 남긴 합성 가능 형태. 식별자 키는 버린다."""
    keep = (
        "windowSizeSeconds",
        "remainingQueries",
        "waitTimeSeconds",
        "totalQueries",
        "remainingTokens",
        "totalTokens",
        "preGenerationDelayMs",
        "nextAvailableAt",
    )
    out: dict = {k: payload.get(k) for k in keep if k in payload}
    for effort in ("lowEffortRateLimits", "highEffortRateLimits"):
        nested = payload.get(effort)
        if isinstance(nested, dict):
            out[effort] = {
                k: nested.get(k)
                for k in ("cost", "waitTimeSeconds", "remainingQueries", "windowSizeSeconds", "totalQueries")
                if k in nested
            }
    return out


def _redact_free_gates(payload: dict) -> dict:
    out: dict = {}
    for product in ("chat", "imagine", "voice", "build"):
        gate = payload.get(product)
        if isinstance(gate, dict):
            out[product] = {k: gate.get(k) for k in ("allowance", "remaining") if k in gate}
    return out


def _safe_raw(**parts: object) -> dict:
    """credential 값·계정 식별자가 끼어들지 않도록 명시 필드만 담는다."""
    out: dict = {}
    for key, value in parts.items():
        if value is None:
            continue
        out[key] = value
    return out
