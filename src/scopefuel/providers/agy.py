"""Google Antigravity CLI (`agy`) — 로컬 language server 우선, 클라우드 폴백.

두 경로가 서로 보완한다:

1. local-server: 실행 중인 `agy` 프로세스가 곧 language server다(임의 포트에 HTTPS+HTTP).
   `RetrieveUserQuotaSummary` 가 **weekly + 5h** 를 그룹 단위로 준다. CSRF 토큰 불필요.
   단점: agy 세션이 하나도 없으면 못 쓴다.
2. cloud: `~/.gemini/antigravity-cli/antigravity-oauth-token` → cloudcode-pa 의
   `loadCodeAssist` → `fetchAvailableModels`. agy 가 안 떠 있어도 되지만 **5h 창만** 온다.

⚠ 모델별 분해는 어느 경로로도 불가하다. cloud 응답은 모델 이름별로 행이 나오지만
값이 그룹 공유다(gemini 계열 전부 동일 fraction, claude/gpt-oss 전부 동일 fraction —
local 의 그룹값과 일치). 그래서 여기서도 그룹으로 접어서 보고한다. 재시도해도 달라지지 않는다.
`GetCommandModelConfigs` 는 CLI 에서 501, `GetCascadeModelConfigs` 는 빈 응답, `RetrieveUserQuota` 는 404다.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess

from ..http import HttpError, request_json
from ..model import Bucket, ProviderResult, Scope

TOKEN_PATHS = [
    pathlib.Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token",
    pathlib.Path.home() / ".config" / "antigravity-cli" / "antigravity-oauth-token",
]
CLOUD_ENDPOINTS = ["https://cloudcode-pa.googleapis.com"]
RPC_PATH = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
RPC_BODY = {
    "metadata": {
        "ideName": "antigravity",
        "extensionName": "antigravity",
        "ideVersion": "unknown",
        "locale": "en",
    }
}
GROUP_ALIASES = {"Gemini Models": "gemini", "Claude and GPT models": "3p"}


def fetch() -> ProviderResult:
    local_error: str | None = None
    try:
        raw = _fetch_local()
        if raw is not None:
            return _from_local(raw)
        local_error = "agy 세션이 실행 중이 아님"
    except Exception as exc:  # 로컬 실패는 치명적이지 않다 — 클라우드로 넘어간다
        local_error = f"local: {exc}"

    try:
        return _from_cloud(local_error)
    except HttpError as exc:
        hint = "agy 를 한 번 실행해 토큰을 갱신하세요" if exc.status == 401 else None
        return ProviderResult(id="agy", error=f"{local_error} / cloud: {exc}", hint=hint)
    except Exception as exc:
        return ProviderResult(id="agy", error=f"{local_error} / cloud: {exc}")


# ------------------------------------------------------------------ local


def _listening_ports() -> list[int]:
    try:
        pids = subprocess.run(
            ["/usr/bin/pgrep", "-x", "agy"], capture_output=True, text=True, timeout=5
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []
    ports: list[int] = []
    for pid in pids:
        try:
            out = subprocess.run(
                ["/usr/sbin/lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", pid],
                capture_output=True,
                text=True,
                timeout=8,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        ports += sorted({int(p) for p in re.findall(r":(\d+) \(LISTEN\)", out)})
    return ports


def _fetch_local() -> dict | None:
    for port in _listening_ports():
        # agy 는 HTTPS(gRPC) 포트와 HTTP 포트를 함께 열기 때문에 둘 다 시도한다.
        for scheme, insecure in (("http", False), ("https", True)):
            try:
                return request_json(
                    f"{scheme}://127.0.0.1:{port}{RPC_PATH}",
                    method="POST",
                    headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1"},
                    body=RPC_BODY,
                    timeout=4,
                    insecure=insecure,
                )
            except Exception:
                continue
    return None


def _from_local(raw: dict) -> ProviderResult:
    buckets: list[Bucket] = []
    for group in (raw.get("response") or {}).get("groups") or []:
        display = group.get("displayName", "?")
        name = GROUP_ALIASES.get(display, display)
        for bucket in group.get("buckets") or []:
            frac = bucket.get("remainingFraction")
            window = bucket.get("window") or "?"
            buckets.append(
                Bucket(
                    label=f"{name} {window}",
                    window="7d" if window == "weekly" else window,
                    used_pct=None if frac is None else round((1 - float(frac)) * 100, 1),
                    resets_at=bucket.get("resetTime"),
                    scope=Scope("group", name),
                    horizon="now" if window == "5h" else "week",
                )
            )
    return ProviderResult(id="agy", buckets=buckets, source="local-server", raw=raw)


# ------------------------------------------------------------------ cloud


def _cloud_token() -> str:
    for path in TOKEN_PATHS:
        if path.exists():
            token = ((json.loads(path.read_text()).get("token") or {}).get("access_token") or "").strip()
            if token:
                return token
    raise RuntimeError("antigravity-oauth-token 을 찾지 못했습니다")


def _from_cloud(local_error: str | None) -> ProviderResult:
    token = _cloud_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "antigravity/1.11.5 windows/amd64",
    }
    last: Exception | None = None
    for base in CLOUD_ENDPOINTS:
        try:
            loaded = request_json(
                f"{base}/v1internal:loadCodeAssist",
                method="POST",
                headers=headers,
                body={"metadata": {"ideType": "ANTIGRAVITY"}},
            )
            project = loaded.get("cloudaicompanionProject")
            if isinstance(project, dict):
                project = project.get("id")
            if not project:
                last = RuntimeError(f"{base}: cloudaicompanionProject 없음")
                continue
            raw = request_json(
                f"{base}/v1internal:fetchAvailableModels",
                method="POST",
                headers=headers,
                body={"project": project},
            )
            return ProviderResult(
                id="agy",
                buckets=_cloud_buckets(raw),
                note=f"cloud 경로 — 5h 창만 제공{f' ({local_error})' if local_error else ''}",
                source="cloud",
                raw=raw,
            )
        except Exception as exc:
            last = exc
    raise last or RuntimeError("cloud 경로 실패")


def _cloud_buckets(raw: dict) -> list[Bucket]:
    """모델 행을 (remainingFraction, resetTime) 기준으로 묶어 그룹 버킷으로 되돌린다."""
    models = raw.get("models")
    items = models.items() if isinstance(models, dict) else []
    clusters: dict[tuple[float, str], list[str]] = {}
    for name, data in items:
        quota = (data or {}).get("quotaInfo") or {}
        frac, reset = quota.get("remainingFraction"), quota.get("resetTime")
        if frac is None or not reset:
            continue  # tab_/chat_ 류의 비-쿼타 항목
        clusters.setdefault((round(float(frac), 6), str(reset)), []).append(str(name))

    buckets: list[Bucket] = []
    for (frac, reset), names in sorted(clusters.items(), key=lambda kv: -kv[0][0]):
        group = "gemini" if any(n.startswith("gemini") for n in names) else "3p"
        buckets.append(
            Bucket(
                label=f"{group} 5h",
                window="5h",
                used_pct=round((1 - frac) * 100, 1),
                resets_at=reset,
                scope=Scope("group", group),
                horizon="now",
                note=f"{len(names)}개 모델 공유",
            )
        )
    return buckets
