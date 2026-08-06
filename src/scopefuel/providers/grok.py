"""Grok (xAI) consumer subscription — read-only usage from CLI billing endpoint.

조사 결과 (2026-07-31, grok CLI 바이너리 + 직접 실측):

- grok CLI OAuth2 token(``~/.grok/auth.json``, 키 prefix ``https://auth.x.ai::``)으로
  호출하는 정본 endpoint:
  ``GET https://cli-chat-proxy.grok.com/v1/billing``
  헤더: ``Authorization: Bearer <key>``, ``x-grok-client-mode: cli``
- 응답 ``config``:
  - ``creditUsagePercent`` = 계정 사용률 (있으면 1차 사용)
  - ``monthlyLimit`` / ``used`` = creditUsagePercent 부재 시 월간 사용률 계산
  - ``billingPeriodEnd`` = 월간 리셋 시각
  - ``productUsage`` = 제품별 분해 (예: GrokChat, GrokBuild)
- ``monthlyLimit``/``used`` 단위는 미상이며 비율만 신뢰한다. 주간 주기 개념은
  응답에 남을 수 있지만 주간 사용률은 측정하지 않는다.

토큰·user/team 식별자·이메일·JWT claim 값은 raw/로그/픽스처에 넣지 않는다.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import urllib.error
import urllib.request

from ..model import Bucket, ProviderResult, Scope

AUTH = pathlib.Path.home() / ".grok" / "auth.json"
AUTH_KEY_PREFIX = "https://auth.x.ai::"
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing"
TIMEOUT_S = 20.0
USER_AGENT = "scopefuel"

SOURCE_BILLING = "cli-billing"
SOURCE_NO_DATA = "no-data"
MONTHLY_WINDOW = "30d"
WEEKLY_USAGE_NOTE = "주간 주기 개념은 있으나 주간 사용률은 측정 불가"


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

    try:
        status, payload = _request_billing(token)
    except Exception as exc:
        return ProviderResult(
            id="grok",
            error=f"billing 조회 실패 ({type(exc).__name__})",
            hint="네트워크 또는 grok.com 상태를 확인하세요",
            source=SOURCE_NO_DATA,
            raw=_safe_raw(credential=credential),
        )

    if status in (401, 403):
        return ProviderResult(
            id="grok",
            warning=f"인증 실패 (HTTP {status}) — `grok login` 후 재시도",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, billing_status=status),
        )

    if status == 429:
        return ProviderResult(
            id="grok",
            error="billing HTTP 429",
            hint="잠시 후 다시 시도하세요",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, billing_status=status),
        )

    if status is None or status >= 400:
        return ProviderResult(
            id="grok",
            error=f"billing HTTP {status}" if status is not None else "billing 응답 없음",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, billing_status=status),
        )

    if status != 200 or not isinstance(payload, dict):
        return ProviderResult(
            id="grok",
            error="billing 응답 처리 실패",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, billing_status=status),
        )

    return _from_billing(payload, credential=credential)


def _from_billing(
    payload: dict,
    *,
    credential: dict,
) -> ProviderResult:
    config = payload.get("config")
    if not isinstance(config, dict):
        return ProviderResult(
            id="grok",
            error="billing 응답에 config 없음",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential),
        )

    billing_period_end = _parse_iso(config.get("billingPeriodEnd"))
    if not billing_period_end:
        return ProviderResult(
            id="grok",
            error="billing 응답에 유효한 billingPeriodEnd 없음",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, **_redacted_billing_raw(config)),
        )

    monthly_limit = _num(config.get("monthlyLimit"))
    if monthly_limit is None or monthly_limit <= 0:
        return ProviderResult(
            id="grok",
            error="monthlyLimit 없음 또는 0 이하 — 월간 사용률 계산 불가",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, **_redacted_billing_raw(config)),
        )

    usage_source = "creditUsagePercent" if "creditUsagePercent" in config else "monthlyLimit/used"
    usage_value = _num(config.get("creditUsagePercent"))
    over_limit_note: str | None = None
    if usage_value is None and usage_source == "creditUsagePercent":
        return ProviderResult(
            id="grok",
            error="creditUsagePercent 파싱 실패",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, usage_source=usage_source, **_redacted_billing_raw(config)),
        )
    if usage_value is None:
        used = _num(config.get("used"))
        if used is None or used < 0:
            return ProviderResult(
                id="grok",
                error="monthlyLimit/used 파싱 실패 — 월간 사용률 계산 불가",
                source=SOURCE_BILLING,
                raw=_safe_raw(
                    credential=credential, usage_source=usage_source, **_redacted_billing_raw(config)
                ),
            )
        usage_value = used / monthly_limit * 100
        if usage_value > 100:
            over_limit_note = (
                "used가 monthlyLimit을 초과해 사용률을 100%로 클램프 (한도 초과는 소진으로 처리)"
            )

    if usage_value < 0 or (usage_source == "creditUsagePercent" and usage_value > 100):
        return ProviderResult(
            id="grok",
            error=f"{usage_source} 범위 밖 ({usage_value:g})",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, usage_source=usage_source, **_redacted_billing_raw(config)),
        )
    usage_pct = min(100.0, usage_value)

    is_unified = config.get("isUnifiedBillingUser") is True
    account_note = "Chat/Build/API 단일 월간 풀" if is_unified else None

    buckets: list[Bucket] = [
        Bucket(
            label="monthly",
            window=MONTHLY_WINDOW,
            used_pct=usage_pct,
            resets_at=billing_period_end,
            scope=Scope("account"),
            horizon="month",
            note=account_note,
        )
    ]

    product_usage = config.get("productUsage")
    problems: list[str] = []
    seen_products: set[str] = set()
    if "productUsage" not in config:
        problems.append("productUsage 필드 누락")
    elif isinstance(product_usage, list):
        for item in product_usage:
            if not isinstance(item, dict):
                problems.append("productUsage 항목이 객체가 아님")
                continue
            product = item.get("product")
            if not isinstance(product, str) or not product:
                problems.append("productUsage 항목에 product 없음")
                continue
            if product in seen_products:
                problems.append(f"{product}: duplicate product (첫 값 유지)")
                continue
            seen_products.add(product)
            pct = _num(item.get("usagePercent"))
            if pct is None:
                problems.append(f"{product}: usagePercent 파싱 실패")
                continue
            if pct < 0 or pct > 100:
                problems.append(f"{product}: usagePercent 범위 밖 ({pct:g})")
                continue
            buckets.append(
                Bucket(
                    label=product,
                    window=MONTHLY_WINDOW,
                    used_pct=pct,
                    resets_at=billing_period_end,
                    scope=Scope("group", product),
                    horizon="month",
                    note="unified pool share" if is_unified else None,
                )
            )
    elif product_usage is not None:
        problems.append("productUsage 타입이 목록이 아님")
    note_parts = [
        f"사용률 source={usage_source}",
        "monthlyLimit/used 단위 미상 — 비율만 신뢰",
        WEEKLY_USAGE_NOTE,
    ]
    if over_limit_note:
        note_parts.append(over_limit_note)
    if problems:
        note_parts.append("partial data — " + "; ".join(problems))

    return ProviderResult(
        id="grok",
        buckets=buckets,
        note="; ".join(note_parts),
        source=SOURCE_BILLING,
        raw=_safe_raw(credential=credential, usage_source=usage_source, **_redacted_billing_raw(config)),
    )


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


def _request_billing(token: str) -> tuple[int | None, dict | None]:
    status, raw = _request(
        token,
        BILLING_URL,
        method="GET",
        extra_headers={"x-grok-client-mode": "cli"},
    )
    if status is None:
        return None, None
    if not raw:
        return status, None
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return status, None
    if isinstance(parsed, dict):
        return status, parsed
    return status, None


def _request(
    token: str,
    url: str,
    *,
    method: str,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int | None, bytes]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        return _num(value.get("val"))
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


def _parse_iso(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _redacted_billing_raw(config: dict) -> dict:
    """사용량 관련 필드만 남긴다. 식별자·계정 필드는 버린다.

    currentPeriod/productUsage 는 nested 객체이므로 통째로 복사하지 않고
    실제로 쓰는 하위 필드만 project 해 미래 identifier 누설을 막는다.
    """
    out: dict = {}
    for key in ("creditUsagePercent", "monthlyLimit", "used", "billingPeriodEnd"):
        if key in config:
            out[key] = config[key]
    if "isUnifiedBillingUser" in config:
        out["isUnifiedBillingUser"] = config["isUnifiedBillingUser"]

    current_period = config.get("currentPeriod")
    if isinstance(current_period, dict):
        out["currentPeriod"] = {k: current_period.get(k) for k in ("type", "end") if k in current_period}

    product_usage = config.get("productUsage")
    if isinstance(product_usage, list):
        out["productUsage"] = [
            {k: item.get(k) for k in ("product", "usagePercent") if k in item}
            for item in product_usage
            if isinstance(item, dict)
        ]

    return out


def _safe_raw(**parts: object) -> dict:
    """credential 값·계정 식별자가 끼어들지 않도록 명시 필드만 담는다."""
    out: dict = {}
    for key, value in parts.items():
        if value is None:
            continue
        if key == "credential" and isinstance(value, dict):
            out[key] = {name: item for name, item in value.items() if isinstance(item, bool)}
            continue
        out[key] = value
    return out
