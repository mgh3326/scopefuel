"""Grok (xAI) consumer subscription — read-only usage from CLI billing endpoint.

조사 결과 (2026-07-31, grok CLI 바이너리 + 직접 실측):

- grok CLI OAuth2 token(``~/.grok/auth.json``, 키 prefix ``https://auth.x.ai::``)으로
  호출하는 정본 endpoint:
  ``GET https://cli-chat-proxy.grok.com/v1/billing?format=credits``
  헤더: ``Authorization: Bearer <key>``, ``x-grok-client-mode: cli``
- 응답 ``config``:
  - ``currentPeriod.type`` = ``USAGE_PERIOD_TYPE_WEEKLY``
  - ``currentPeriod.end`` = 주간 리셋 시각
  - ``creditUsagePercent`` = 계정 주간 사용률 (0~100)
  - ``productUsage`` = 제품별 분해 (예: GrokChat, GrokBuild)
  - ``isUnifiedBillingUser`` = true 이면 Chat/Build/API 등이 단일 주간 풀 공유
- ``/rest/rate-limits`` 와 ``/rest/usage/free-usage-gates`` 는 더 이상 사용하지 않는다.
  전자는 웹앱 전용이라 CLI OAuth2 토큰으로 호출하면 ``oauth2-auth-forbidden`` 이다.

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

from ..model import Bucket, ProviderResult, Scope

AUTH = pathlib.Path.home() / ".grok" / "auth.json"
AUTH_KEY_PREFIX = "https://auth.x.ai::"
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
SUBSCRIPTIONS_URL = "https://grok.com/rest/subscriptions"
TIMEOUT_S = 20.0
USER_AGENT = "scopefuel"

SOURCE_BILLING = "cli-billing"
SOURCE_NO_DATA = "no-data"

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
        status, payload = _request_billing(token)
    except Exception as exc:
        return ProviderResult(
            id="grok",
            plan=plan,
            error=f"billing 조회 실패 ({type(exc).__name__})",
            hint="네트워크 또는 grok.com 상태를 확인하세요",
            source=SOURCE_NO_DATA,
            raw=_safe_raw(credential=credential, plan=plan),
        )

    if status in (401, 403):
        return ProviderResult(
            id="grok",
            plan=plan,
            warning=f"인증 실패 (HTTP {status}) — `grok login` 후 재시도",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, plan=plan, billing_status=status),
        )

    if status == 429:
        return ProviderResult(
            id="grok",
            plan=plan,
            error="billing HTTP 429",
            hint="잠시 후 다시 시도하세요",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, plan=plan, billing_status=status),
        )

    if status is None or status >= 400:
        return ProviderResult(
            id="grok",
            plan=plan,
            error=f"billing HTTP {status}" if status is not None else "billing 응답 없음",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, plan=plan, billing_status=status),
        )

    if status != 200 or not isinstance(payload, dict):
        return ProviderResult(
            id="grok",
            plan=plan,
            error="billing 응답 처리 실패",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, plan=plan, billing_status=status),
        )

    return _from_billing(payload, plan=plan, credential=credential)


def _from_billing(
    payload: dict,
    *,
    plan: str | None,
    credential: dict,
) -> ProviderResult:
    config = payload.get("config")
    if not isinstance(config, dict):
        return ProviderResult(
            id="grok",
            plan=plan,
            error="billing 응답에 config 없음",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, plan=plan),
        )

    current_period = config.get("currentPeriod")
    if not isinstance(current_period, dict):
        return ProviderResult(
            id="grok",
            plan=plan,
            error="billing 응답에 currentPeriod 없음",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, plan=plan),
        )

    period_type = current_period.get("type")
    if period_type != "USAGE_PERIOD_TYPE_WEEKLY":
        return ProviderResult(
            id="grok",
            plan=plan,
            warning=f"예상치 못한 주기 유형: {period_type}",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, plan=plan, **_redacted_billing_raw(config)),
        )

    resets_at = _parse_iso(current_period.get("end"))
    if not resets_at:
        return ProviderResult(
            id="grok",
            plan=plan,
            warning="currentPeriod.end 파싱 실패",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, plan=plan, **_redacted_billing_raw(config)),
        )

    credit_pct = _num(config.get("creditUsagePercent"))
    if credit_pct is None:
        return ProviderResult(
            id="grok",
            plan=plan,
            warning="creditUsagePercent 파싱 실패",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, plan=plan, **_redacted_billing_raw(config)),
        )
    if credit_pct < 0 or credit_pct > 100:
        return ProviderResult(
            id="grok",
            plan=plan,
            warning=f"creditUsagePercent 범위 밖 ({credit_pct:g})",
            source=SOURCE_BILLING,
            raw=_safe_raw(credential=credential, plan=plan, **_redacted_billing_raw(config)),
        )

    is_unified = config.get("isUnifiedBillingUser") is True
    account_note = "Chat/Build/API 단일 주간 풀" if is_unified else None

    buckets: list[Bucket] = [
        Bucket(
            label="weekly",
            window="7d",
            used_pct=credit_pct,
            resets_at=resets_at,
            scope=Scope("account"),
            horizon="week",
            note=account_note,
        )
    ]

    product_usage = config.get("productUsage")
    problems: list[str] = []
    seen_products: set[str] = set()
    if isinstance(product_usage, list):
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
                    window="7d",
                    used_pct=pct,
                    resets_at=resets_at,
                    scope=Scope("group", product),
                    horizon="week",
                    note="unified pool share" if is_unified else None,
                )
            )
    elif product_usage is not None:
        problems.append("productUsage 타입이 목록이 아님")

    note: str | None = None
    if problems:
        # weekly account bucket 은 credit_pct 검증 후 항상 추가되므로
        # buckets 비어 있음 경로는 도달 불가.
        note = "partial data — " + "; ".join(problems)

    return ProviderResult(
        id="grok",
        plan=plan,
        buckets=buckets,
        note=note,
        source=SOURCE_BILLING,
        raw=_safe_raw(credential=credential, plan=plan, **_redacted_billing_raw(config)),
    )


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
    if "creditUsagePercent" in config:
        out["creditUsagePercent"] = config["creditUsagePercent"]
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
        out[key] = value
    return out
