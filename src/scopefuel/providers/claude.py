"""Claude Code (Anthropic) — OAuth usage API.

`~/.claude/.credentials.json` 의 access token 으로 `GET /api/oauth/usage` 를 호출한다.
`limits[]` 의 kind=weekly_scoped 항목이 **모델 한정** 한도(예: Fable 주간 100%)이며,
이걸 계정 한도와 섞으면 "계정이 막혔다"고 오독한다 — scope 로 구분해 둔다.

자격증명은 항상 파일에 있지는 않다. macOS 의 claude 는 로그인 방식에 따라 파일 대신
Keychain(`Claude Code-credentials`)에만 토큰을 두며, 그 경우 파일은 아예 생기지 않는다.
파일만 보면 로그인된 계정을 "미로그인"으로 오판해 gate 가 fail-closed 로 풀 전체를
막아버린다 — 파일을 우선하되 없으면 Keychain 으로 폴백한다.

토큰 갱신은 하지 않는다(실행 중인 claude 세션이 갱신한다 — 자동 갱신은 #616 으로 금지).
expiresAt 가 이미 지난 자격으로는 usage API 를 호출하지 않는다 — 호출 전에
"token expired" 로 실패시킨다(task #653: 만료가 401/429 로 오분류되던 경로 제거).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import subprocess
import sys
import time
import unicodedata

from ..http import HttpError, classify_error, request_json
from ..model import Bucket, ProviderResult, Scope, safe_label
from ..quota_v2_contract import Attempt, claude_attempt

CREDENTIALS = pathlib.Path.home() / ".claude" / ".credentials.json"
CLAUDE_JSON = pathlib.Path.home() / ".claude.json"
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


def _keychain_service() -> str:
    """Claude Code 가 자격을 두는 Keychain 아이템 이름 — cli ``AD()`` 와 동일 규칙.

    config 컨텍스트마다 다른 아이템이다 (설치된 2.1.281 바이너리에서 확인):

    - ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` 가 *설정돼 있으면* 그 값(NFC 정규화)의
      sha256 앞 8자를 접미사로 쓴다 — 빈 문자열이면 접미사 없음.
    - 그게 없으면 ``CLAUDE_CONFIG_DIR`` 가 있을 때 그 **원문**(NFC 정규화 — 절대
      경로로 resolve 하지 않는다: 같은 문자열로 뜬 claude 프로세스와 같은
      아이템을 가리켜야 한다)을 해시해 접미사로 쓴다.
    - 둘 다 없으면 무접미사 ``Claude Code-credentials``.

    task #659 — 이 이름을 따라야 토큰 출처와 ``.claude.json`` uuid 출처가 항상
    같은 config 컨텍스트에 묶인다. ``CLAUDE_CONFIG_DIR`` 아래에서 무접미사
    기본 아이템을 읽으면 다른 컨텍스트의 토큰과 이 컨텍스트의 uuid 를 섞어
    측정을 잘못된 계정 지문으로 게시하게 된다(#654 S-A).
    """
    secure = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    source = secure if secure is not None else os.environ.get("CLAUDE_CONFIG_DIR")
    if not source:
        return KEYCHAIN_SERVICE
    digest = hashlib.sha256(unicodedata.normalize("NFC", source).encode()).hexdigest()[:8]
    return f"{KEYCHAIN_SERVICE}-{digest}"


def _read_keychain() -> str | None:
    """macOS Keychain 의 자격증명 blob. 실패는 전부 '없음'으로 접는다(폴백이므로).

    읽는 아이템은 ``_keychain_service()`` 가 정한 현재 config 컨텍스트의 것뿐
    이다 — 다른 컨텍스트의 아이템(기본 무접미사 포함)은 절대 읽지 않는다.
    """
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", _keychain_service(), "-w"],
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


def _claude_json_path() -> pathlib.Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return pathlib.Path(configured) / ".claude.json" if configured else CLAUDE_JSON


def _account_uuid() -> str | None:
    """계정 uuid — ``~/.claude.json`` 최상위의 ``oauthAccount.accountUuid``.

    자격 파일/Keychain 의 ``claudeAiOauth`` 에는 토큰·만료·플랜만 있고 계정
    식별자는 없다 — uuid 는 Claude Code 주 설정 파일에만 있다(읽기 전용).
    """
    try:
        payload = json.loads(_claude_json_path().read_text())
    except (OSError, ValueError):
        return None
    account = payload.get("oauthAccount") if isinstance(payload, dict) else None
    uuid = account.get("accountUuid") if isinstance(account, dict) else None
    return uuid.strip() if isinstance(uuid, str) and uuid.strip() else None


def _account_identity(oauth: dict) -> tuple[str | None, str | None]:
    """계정 지문과 그 묶임 근거 — ``(account_fp, kind)`` (task #653/#654/#659).

    정본은 ``oauthAccount.accountUuid`` 다 — accessToken 은 로그인 회차·호스트마다
    다르므로 토큰 해시를 정본으로 쓰면 같은 계정이 '다른 계정'으로 오판돼 원격
    스냅샷 공유(AC2)가 깨진다. uuid 는 토큰과 **같은 config 컨텍스트**에서만
    읽는다 — ``_claude_json_path()`` 와 토큰 출처(파일·컨텍스트별 Keychain
    아이템)가 모두 ``CLAUDE_CONFIG_DIR`` 에 묶여 있으므로 둘이 어긋나는 조합은
    만들어지지 않는다(#659 AC1).

    kind:

    - ``"account"`` — uuid-bound 지문. hk 게시·교차호스트 원격 읽기가 가능하다.
    - ``"token"`` — 같은 컨텍스트에서 uuid 를 읽지 못한 호스트의 토큰 해시
      폴백. #576 로컬 stale 일치 전용이다 — 게시하면 토큰 회전마다 아무도
      못 읽는 orphan 문서가 생기고(N-1), 무엇보다 그 지문이 어느 계정인지
      증명할 수 없다.
    - ``None`` — 토큰도 없다.

    자문 2558 의 '계정/구독 변경 의심' 계약: uuid 경로에서는 *계정이* 바뀌면
    지문이 바뀐다(토큰 회전으로는 안 바뀐다 — 회전 후 stale 스냅샷이 같은
    계정으로 수용되는 것은 의도된 semantic 이다). uuid 가 없으면 토큰 지문
    이므로 토큰 회전이 곧 지문 변경이다.
    """
    plan = str(oauth.get("subscriptionType") or "")
    uuid = _account_uuid()
    if uuid:
        return hashlib.sha256(f"claude-account|{plan}|{uuid}".encode()).hexdigest()[:16], "account"
    token = (oauth.get("accessToken") or "").strip()
    if not token:
        return None, None
    return hashlib.sha256(f"{plan}|{token}".encode()).hexdigest()[:16], "token"


def _account_fp(oauth: dict) -> str | None:
    """계정 지문 — ``_account_identity`` 의 지문 부분만."""
    return _account_identity(oauth)[0]


def _account_label() -> str | None:
    """계정의 안전한 표시 라벨 — 같은 컨텍스트의 ``.claude.json`` 에서 읽는다.

    ``oauthAccount`` 의 ``organizationName`` → ``displayName`` → ``fullName``
    순으로 첫 유효값을 쓴다. 이메일·토큰·uuid 원문은 절대 쓰지 않는다 —
    ``safe_label`` 이 '@' 포함 값과 인용부호·제어문자를 걸러낸다.
    """
    try:
        payload = json.loads(_claude_json_path().read_text())
    except (OSError, ValueError):
        return None
    account = payload.get("oauthAccount") if isinstance(payload, dict) else None
    if not isinstance(account, dict):
        return None
    for key in ("organizationName", "displayName", "fullName"):
        label = safe_label(account.get(key))
        if label:
            return label
    return None


def _session_fp(oauth: dict) -> str | None:
    """측정 세션 지문 — 어느 토큰 세션이 관측했는지의 provenance.

    hk 스냅샷의 ``measured_by`` 에만 실린다. 비가역 해시라 원문을 복원할 수
    없고, 계정 지문과 달리 토큰 회전을 따라 바뀐다 — 그게 목적이다(AC5: 한
    세션의 429 가 다른 세션의 계정 판정으로 번지지 않는다는 관측 가능성).
    """
    token = (oauth.get("accessToken") or "").strip()
    if not token:
        return None
    return hashlib.sha256(f"claude-session|{token}".encode()).hexdigest()[:16]


def _expiry_epoch(oauth: dict) -> float | None:
    """expiresAt 를 epoch 초로. claude 자격은 밀리초이지만 초 단위도 받는다."""
    raw = oauth.get("expiresAt")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if not math.isfinite(raw) or raw <= 0:
        return None
    return raw / 1000 if raw > 1e12 else raw


def current_account_fp() -> str | None:
    """네트워크 없이 로컬 자격 파일만으로 지문을 계산한다 (backoff·원격 조회의 계정 검증용)."""
    loaded = _load_oauth()
    return None if loaded is None else _account_fp(loaded[0])


def current_account_fp_kind() -> str | None:
    """현재 지문의 묶임 근거 — "account" | "token" | None (원격 읽기 게이트용)."""
    loaded = _load_oauth()
    return None if loaded is None else _account_identity(loaded[0])[1]


def fetch() -> ProviderResult:
    loaded = _load_oauth()
    if loaded is None:
        return ProviderResult(
            id="claude",
            error="자격증명 없음",
            error_kind="credentials",
            hint=(
                f"{_credentials_path()} 없음, Keychain('{_keychain_service()}')에서도 못 읽음 "
                "— claude 로그인 후 다시 시도"
            ),
        )
    oauth, origin = loaded
    token = oauth["accessToken"].strip()
    fp, fp_kind = _account_identity(oauth)
    label = _account_label()
    session_fp = _session_fp(oauth)

    # task #653 — 만료는 호출 전에 판정한다. 이미 지난 자격으로 친 401 은
    # rate limit 으로도 '측정 불가' 로도 기록되면 안 된다 — 원인은 '만료'다.
    expiry = _expiry_epoch(oauth)
    if expiry is not None and expiry <= time.time():
        return ProviderResult(
            id="claude",
            error="token expired — claude access token 만료",
            error_kind="token_expired",
            account_fp=fp,
            account_fp_kind=fp_kind,
            account_label=label,
            session_fp=session_fp,
            hint="expiresAt 과거 — 실행 중인 claude 세션이 갱신하거나 재로그인 필요",
            v2_attempt=Attempt("auth_error", "auth_error:token_expired"),
        )

    http_status: list[int] = []
    try:
        raw = request_json(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "anthropic-beta": BETA_HEADER,
                "User-Agent": "scopefuel",
            },
            status_out=http_status,
        )
    except Exception as exc:
        # 429·5xx·네트워크·인증 실패를 분류해 게이트가 "속도 제한"과
        # "측정 불가"를 구분한다(실패 결과에도 지문을 실어 stale 수용 판정에 쓴다).
        kind, status, retry_after = classify_error(exc)
        seen = exc.status if isinstance(exc, HttpError) else (http_status[-1] if http_status else None)
        # task #653 — expiresAt 가 미래인데도 서버가 401 expired 를 돌려주면
        # (로컬 시계 드리프트·서버 측 회전) 그것도 '만료'로 보고한다.
        if isinstance(exc, HttpError) and exc.status == 401 and "expired" in (exc.body or "").lower():
            kind = "token_expired"
        return ProviderResult(
            id="claude",
            error=str(exc),
            error_kind=kind,
            http_status=status,
            retry_after_s=retry_after,
            account_fp=fp,
            account_fp_kind=fp_kind,
            account_label=label,
            session_fp=session_fp,
            hint="usage API 속도 제한" if kind == "rate_limited" else None,
            v2_attempt=(
                Attempt("auth_error", "auth_error:token_expired")
                if kind == "token_expired"
                else claude_attempt(http_status=seen, exc=exc)
            ),
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
    extra = raw.get("extra_usage") or {}
    if extra.get("is_enabled"):
        note = f"extra usage {extra.get('utilization')}%"
    if fp_kind == "token":
        # task #659 — 게시 거부 사유를 결과에 명시한다(AC1): 이 컨텍스트의
        # .claude.json 에서 uuid 를 못 읽었으므로 지문이 토큰 해시 폴백이라
        # hk 스냅샷은 게시·구독되지 않는다(N-1 orphan 방지).
        skip = "hk 공유 건너뜀 — 이 config 컨텍스트의 .claude.json 에 account uuid 없음"
        note = f"{note} · {skip}" if note else skip

    return ProviderResult(
        id="claude",
        plan=oauth.get("subscriptionType"),
        buckets=buckets,
        note=note,
        source="oauth-usage-api" if origin == "file" else f"oauth-usage-api+{origin}",
        raw=raw,
        http_status=200,
        account_fp=fp,
        account_fp_kind=fp_kind,
        account_label=label,
        session_fp=session_fp,
        v2_attempt=claude_attempt(http_status=http_status[-1] if http_status else 200, body=raw),
    )


fetch.current_account_fp = current_account_fp  # noqa: B010 — 로컬 전용 probe
fetch.current_account_fp_kind = current_account_fp_kind  # noqa: B010 — 지문 묶임 근거 probe


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
        return f if math.isfinite(f) and 0 <= f <= 100 else None
    except (TypeError, ValueError, OverflowError):
        return None
