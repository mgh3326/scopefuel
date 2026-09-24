"""Cross-host quota snapshots via handoffkeep documents (tasks #653/#654).

모델은 "계정당 측정 1곳 → NCP(handoffkeep) 저장 → 다른 호스트가 읽는다"다.
같은 Claude 계정을 여러 호스트·앱(m1b 의 ClaudeBar 등)이 각자 폴링해 생긴
usage-API 429 를 구조적으로 없앤다.

- 측정 호스트는 성공한 결과를 ``quota/<pool>/<account_fp>/latest`` 에 PUT 한다 —
  스케줄러가 아니라 collect()/refresh 의 부산물이며, 실패는 fail-open 이다.
- 읽는 호스트는 로컬 측정이 불가(오류)·만료(stale)·backoff 일 때만 읽고,
  계정 지문 일치·``REMOTE_MAX_AGE_S`` 이내만 받아들인다. 원격 값은 로컬
  측정이 아니므로 로컬 캐시·backoff·v2 저장소에 쓰지 않는다.
- 페이로드는 비밀이 아닌 값뿐이다 — used_pct·reset·측정 시각·측정 호스트·
  계정/세션 지문(비가역 해시). 토큰·자격 원문은 절대 싣지 않는다 — 이 모듈의
  고정 키 집합이 유일한 방어선이다(hk ``guard.Reject`` 는 ``sk-ant-api03`` 형
  키만 잡고 OAuth 토큰 형태는 통과시킨다).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import socket
import urllib.parse

from . import bench
from .http import request_json
from .model import (
    Bucket,
    PoolClass,
    ProviderResult,
    Scope,
    _is_valid_used_pct,
    account_tag,
    safe_label,
)
from .policy import load_config

SCHEMA = "scopefuel.quota-share.v1"
DOC_KIND = "note"
DOC_SESSION = "scopefuel-quota-share"
# 15분 이내의 원격 측정만 받아들인다 — 측정 주기(TTL 180s)의 몇 배 안에서
# '신선' 과 '낡음' 의 경계다.
REMOTE_MAX_AGE_S = 15 * 60.0
# 원격 시계가 로컬보다 미래로 나가도 이 정도는 NTP 드리프트로 본다.
MAX_FUTURE_SKEW_S = 60.0
# collect 경로 위에서 도는 부가 호출이라 짧게 묶는다 — hk 장애가 조회를 늦추지 않게.
REQUEST_TIMEOUT_S = 5.0
# ProviderResult.source 표식 — manual.SOURCE("operator", 자기신고)와 구분된다.
REMOTE_SOURCE = "remote"
ENV_DISABLE = "SCOPEFUEL_QUOTA_SHARE"

_DOC_PREFIX = "/v1/documents/"

# task #659 — 게이트 첫 줄은 기계 파싱되므로 wire 의 host 는 이 문자 집합만 허용한다
# (공백·인용부호·줄바꿈이 섞인 host 는 key=val 주입으로 줄을 위조할 수 있다 — N-4).
_HOST_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def enabled() -> bool:
    """기본 켜짐 — hk 자격이 없으면 엔드포인트 해석 단계에서 조용히 꺼진다."""
    return os.environ.get(ENV_DISABLE, "").strip().lower() not in {"off", "0", "disabled", "false", "no"}


def key_for(pool: str, account_fp: str) -> str:
    """스냅샷 문서 키. hk 는 선행 '/'·'..'·NUL 을 거부한다."""
    return f"quota/{pool}/{account_fp}/latest"


def remote_label(host: str, account: str = "") -> str:
    """원격 값의 표시 라벨 — 'remote measured (host) · account <fp8 (label)>'.

    account 는 ``model.account_tag`` 출력 — 어느 계정의 측정인지 읽는 호스트가
    바로 보게 한다(task #659 AC2).
    """
    label = f"remote measured ({host})"
    return f"{label} · account {account}" if account else label


def remote_eligible(result: ProviderResult) -> bool:
    """로컬 측정이 쓸 수 없는 상태일 때만 원격 폴백 후보다.

    error(측정 불가)·stale(만료)·backoff_until(backoff 창)이 대상이다.
    신선한 성공이나 경고만 있는 결과는 로컬 관측을 그대로 둔다.
    """
    return bool(result.error or result.stale or result.backoff_until is not None)


def _endpoint() -> tuple[str, str] | None:
    """handoffkeep 엔드포인트 — bench(#593)와 같은 자격 해석을 공유한다.

    평문 URL 은 명시적 opt-in([bench] allow_plaintext_url) 없이 쓰지 않는다 —
    bearer 토큰을 평문으로 보내는 경로는 없다(CWE-319).
    """
    url, token = bench._handoffkeep_credentials()
    if not url or not token:
        return None
    config = load_config()
    bench_cfg = config.get("bench") if isinstance(config, dict) else None
    allow = isinstance(bench_cfg, dict) and bench_cfg.get("allow_plaintext_url") is True
    if not bench._plaintext_allowed(url, allow_plaintext=allow):
        return None
    return url.rstrip("/"), token


def _request(method: str, key: str, document: dict | None = None) -> dict | None:
    """인증된 문서 호출 1회. 어떤 실패든 None 으로 접는다(fail-open)."""
    endpoint = _endpoint()
    if endpoint is None:
        return None
    url, token = endpoint
    headers = {"Authorization": f"Bearer {token}"}
    if document is not None:
        headers["Content-Type"] = "application/json"
    try:
        payload = request_json(
            f"{url}{_DOC_PREFIX}{urllib.parse.quote(key, safe='')}",
            method=method,
            headers=headers,
            body=document,
            timeout=REQUEST_TIMEOUT_S,
        )
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _safe_host(host: object) -> str:
    """표시·기록용 호스트 라벨 — 허용 문자 집합 밖이면 'unknown' 으로 접는다."""
    return host if isinstance(host, str) and _HOST_RE.fullmatch(host) else "unknown"


def _measured_by(host: str, session_fp: str | None, result: ProviderResult) -> dict:
    """측정 주체 provenance — 어느 호스트·세션·계정이 관측했는지 (AC2/AC5)."""
    return {
        "host": host,
        "session_fp": session_fp,
        "account_fp": result.account_fp,
        "account_label": safe_label(result.account_label),
    }


def _bucket_payload(bucket: Bucket, measured_by: dict) -> dict:
    return {
        "label": bucket.label,
        "window": bucket.window,
        "horizon": bucket.horizon,
        "used_pct": bucket.used_pct if _is_valid_used_pct(bucket.used_pct) else None,
        "resets_at": bucket.resets_at,
        "scope": bucket.scope.as_dict(),
        "note": bucket.note,
        # 값마다 측정 주체를 붙인다 — 한 호스트/세션의 429 가 다른 호스트의
        # 계정 판정으로 번지지 않는다는 것을 관측 가능하게 하는 provenance(AC5).
        "measured_by": dict(measured_by),
    }


def _payload(pool: str, result: ProviderResult, host: str, session_fp: str | None, epoch: float) -> dict:
    measured_by = _measured_by(host, session_fp, result)
    return {
        "schema": SCHEMA,
        "pool": pool,
        "account_fp": result.account_fp,
        "measured_at": dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat(),
        "measured_at_epoch": float(epoch),
        "measured_by": measured_by,
        "source": result.source,
        "plan": result.plan,
        "buckets": [_bucket_payload(b, measured_by) for b in result.buckets],
    }


def publish_result(pool: str, result: ProviderResult, *, now: float | None = None) -> bool:
    """성공한 측정의 정제 스냅샷을 hk 문서로 게시한다. 절대 raise 하지 않는다.

    계정 지문이 없는 결과는 게시하지 않는다 — 지문 없는 값을 읽는 쪽에서
    계정 일치를 증명할 수 없으므로 쓰는 것도 fail-closed 다.
    """
    try:
        if not enabled() or result.error or result.warning:
            return False
        fp = result.account_fp
        if not isinstance(fp, str) or not fp:
            return False
        # task #659 — 계정 uuid 에 묶인 지문만 게시한다. 토큰 해시 폴백 지문은
        # 이 호스트의 현재 토큰에 묶여 있어 회전마다 아무도 못 읽는 orphan
        # 문서를 남기고(N-1), 그 지문이 어느 계정인지 증명할 수 없다 —
        # config 컨텍스트가 어긋난 토큰+uuid 조합의 오귀속을 막는 게이트다.
        # 건너뛴 사유는 ProviderResult.account_fp_kind="token" 과
        # docs/quota-share.md 에 명시된다.
        if result.account_fp_kind != "account":
            return False
        if not any(_is_valid_used_pct(b.used_pct) for b in result.buckets):
            return False
        epoch = result.fetched_at if isinstance(result.fetched_at, int | float) else now
        if not isinstance(epoch, int | float) or not math.isfinite(epoch):
            return False
        session_fp = result.session_fp if isinstance(result.session_fp, str) else None
        key = key_for(pool, fp)
        document = {
            "key": key,
            "kind": DOC_KIND,
            "session": DOC_SESSION,
            "job": "",
            "body": json.dumps(
                _payload(pool, result, _safe_host(socket.gethostname()), session_fp, float(epoch)),
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
        response = _request("PUT", key, document)
        return isinstance(response, dict) and isinstance(response.get("document"), dict)
    except Exception:
        return False


def _current_identity(fetcher: object, local: ProviderResult) -> tuple[str | None, str | None]:
    """이 호스트의 *현재* (계정 지문, 묶임 근거) — 원격 조회의 키이자 일치 검증 값.

    ``current_account_identity`` probe 가 있으면 자격을 한 번만 읽는다
    (Keychain 호출도 1회 — N-8). 없으면 개별 probe → 로컬 결과 필드 순으로
    폴백한다. 지문을 낼 수 없으면 이 호스트가 어느 계정인지 증명할 수 없으므로
    거부 — 저장된 옛 지문으로 다른 계정의 스냅샷을 읽는 것을 막는다.
    """
    probe = getattr(fetcher, "current_account_identity", None)
    if callable(probe):
        fp, kind = probe()
    else:
        fp_probe = getattr(fetcher, "current_account_fp", None)
        kind_probe = getattr(fetcher, "current_account_fp_kind", None)
        fp = fp_probe() if callable(fp_probe) else local.account_fp
        kind = kind_probe() if callable(kind_probe) else local.account_fp_kind
    fp = fp if isinstance(fp, str) and fp else None
    return fp, kind if kind in ("account", "token") else None


def _snapshot(document: dict | None) -> dict | None:
    """hk 문서 → 검증된 스냅샷 dict. 형식이 조금이라도 다르면 None."""
    if not isinstance(document, dict):
        return None
    body = document.get("body")
    if not isinstance(body, str):
        return None
    try:
        snap = json.loads(body)
    except ValueError:
        return None
    if not isinstance(snap, dict) or snap.get("schema") != SCHEMA:
        return None
    measured = snap.get("measured_at_epoch")
    if isinstance(measured, bool) or not isinstance(measured, (int, float)) or not math.isfinite(measured):
        return None
    buckets = snap.get("buckets")
    if not isinstance(buckets, list) or not all(isinstance(b, dict) for b in buckets):
        return None
    by = snap.get("measured_by")
    host = by.get("host") if isinstance(by, dict) else None
    session_fp = by.get("session_fp") if isinstance(by, dict) else None
    account_label = by.get("account_label") if isinstance(by, dict) else None
    by_fp = by.get("account_fp") if isinstance(by, dict) else None
    if isinstance(by_fp, str) and by_fp and by_fp != snap.get("account_fp"):
        # provenance 가 문서의 accept-key 지문과 어긋나면 위조 의심 — 거부.
        return None
    return {
        "pool": snap.get("pool"),
        "account_fp": snap.get("account_fp"),
        "measured_at_epoch": float(measured),
        "host": _safe_host(host),
        "session_fp": session_fp if isinstance(session_fp, str) and session_fp else None,
        "account_label": safe_label(account_label),
        "plan": snap.get("plan") if isinstance(snap.get("plan"), str) else None,
        "buckets": buckets,
    }


def _bucket(data: dict) -> Bucket:
    scope = data.get("scope") if isinstance(data.get("scope"), dict) else {}
    kind = scope.get("kind") if scope.get("kind") in ("account", "model", "group") else "account"
    name = scope.get("name")
    horizon = data.get("horizon")
    return Bucket(
        label=str(data.get("label") or "?"),
        window=str(data.get("window") or "?"),
        used_pct=data.get("used_pct") if _is_valid_used_pct(data.get("used_pct")) else None,
        resets_at=data.get("resets_at") if isinstance(data.get("resets_at"), str) else None,
        scope=Scope(kind, name if isinstance(name, str) else None),
        horizon=horizon if horizon in ("now", "week", "month") else "week",
        note=data.get("note") if isinstance(data.get("note"), str) else None,
    )


def remote_result(
    pool: str,
    fetcher: object,
    local: ProviderResult,
    policy_class: PoolClass,
    *,
    now: float,
) -> ProviderResult | None:
    """조건을 다 갖춘 원격 스냅샷이면 결과를 돌려주고, 아니면 None.

    조건(전부 필수): 기능이 켜져 있고, 이 호스트의 계정 지문이 계산되고,
    문서가 존재·검증되고, pool·지문이 일치하고, 측정이 REMOTE_MAX_AGE_S
    이내다. class 는 로컬 측정과 같은 경로(호출자의 policy_class)를 쓴다 —
    발행자의 pool_class 를 신뢰해 복사하지 않는다.
    """
    try:
        if not enabled():
            return None
        fp, kind = _current_identity(fetcher, local)
        if fp is None:
            return None
        # task #659 — 토큰 해시 폴백 지문으로는 읽지 않는다: 게시되는 문서는 전부
        # uuid-bound 키라 맞을 문서가 없고, 있더라도(구형 orphan) 계정 정체성을
        # 증명할 수 없다.
        if kind == "token":
            return None
        snap = _snapshot(_request("GET", key_for(pool, fp)))
        if snap is None or snap["pool"] != pool or snap["account_fp"] != fp:
            return None
        measured_at = snap["measured_at_epoch"]
        age = now - measured_at
        if age > REMOTE_MAX_AGE_S or age < -MAX_FUTURE_SKEW_S:
            return None
        buckets = [_bucket(b) for b in snap["buckets"]]
        if not any(_is_valid_used_pct(b.used_pct) for b in buckets):
            return None
        return ProviderResult(
            id=pool,
            plan=snap["plan"],
            buckets=buckets,
            note=remote_label(snap["host"], account_tag(fp, snap["account_label"], "account")),
            source=REMOTE_SOURCE,
            fetched_at=measured_at,
            age_s=max(0.0, age),
            stale=False,
            pool_class=policy_class,
            account_fp=fp,
            account_fp_match=True,
            account_fp_kind="account",
            account_label=snap["account_label"],
            session_fp=snap["session_fp"],
            # 대체된 로컬 실패는 감사로 남긴다 — 원격 성공이 '로컬이 건강하다'
            # 는 뜻이 아니므로.
            last_error=local.last_error or local.error,
            last_error_at=local.last_error_at,
            backoff_until=local.backoff_until,
        )
    except Exception:
        return None
