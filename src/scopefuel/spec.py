"""선언형 provider 스펙 로더 — 새 LLM 코딩 플랜 추가를 '코드 없이 TOML 한 장'으로.

대부분의 provider는 같은 모양이다: 로컬 자격증명 파일에서 토큰을 읽어 HTTP 한 번 치고,
응답 JSON의 어떤 경로에서 사용률/리셋시각을 꺼낸다. 그 패턴은 스펙으로 표현하고,
그 틀을 벗어나는 것(프로세스 탐색, OAuth 갱신, 다단계 호출)만 Python 플러그인으로 만든다.

스펙 탐색 경로 (뒤가 우선):
  1. 패키지 내장 specs/
  2. ~/.config/scopefuel/providers/*.toml
  3. $SCOPEFUEL_SPEC_DIR/*.toml
같은 id 를 정의하면 나중 것이 이깁니다 — 내장 provider의 엔드포인트가 깨졌을 때
릴리스를 기다리지 않고 스펙으로 덮어쓸 수 있게 하려는 의도입니다.

포맷은 docs/adding-a-provider.md 참고.
"""

from __future__ import annotations

import json
import os
import pathlib
import tomllib
from collections.abc import Callable

from .http import dig, request_json
from .model import Bucket, ProviderResult, Scope, horizon_for

SPEC_DIRS = [
    pathlib.Path(__file__).parent / "specs",
    pathlib.Path.home() / ".config" / "scopefuel" / "providers",
]

_WINDOW_SECONDS = {"5h": 18000, "1d": 86400, "7d": 604800, "30d": 2592000}


class SpecError(RuntimeError):
    pass


def _resolve_token(spec: dict) -> str:
    creds = spec.get("credentials") or {}
    if env_name := creds.get("token_env"):
        token = os.environ.get(env_name)
        if not token:
            raise SpecError(f"환경변수 {env_name} 없음")
        return token
    file_path = creds.get("file")
    if not file_path:
        raise SpecError("credentials.file 또는 credentials.token_env 가 필요합니다")
    path = pathlib.Path(os.path.expanduser(file_path))
    if not path.exists():
        raise SpecError(f"자격증명 파일 없음: {path}")
    data = json.loads(path.read_text())
    token = dig(data, creds.get("token_path"))
    if not isinstance(token, str) or not token:
        raise SpecError(f"{path} 에서 token_path={creds.get('token_path')} 를 찾지 못했습니다")
    return token.strip()


def _fmt(template: object, **ctx: object) -> str | None:
    if not isinstance(template, str):
        return None
    return template.format(**ctx)


def _resets_at(value: object, kind: str) -> str | None:
    if value is None:
        return None
    if kind == "epoch":
        from .model import epoch_to_iso

        try:
            return epoch_to_iso(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    return str(value)


def _used_pct(source: object, bucket_spec: dict) -> float | None:
    raw = dig(source, bucket_spec.get("used_pct_path"))
    if raw is None and (frac_path := bucket_spec.get("remaining_fraction_path")):
        frac = dig(source, frac_path)
        if frac is None:
            return None
        try:
            return round((1.0 - float(frac)) * 100, 1)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    if raw is None:
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _build_bucket(bucket_spec: dict, source: object, **ctx: object) -> Bucket:
    window = str(bucket_spec.get("window") or "?")
    horizon = bucket_spec.get("horizon") or horizon_for(_WINDOW_SECONDS.get(window))
    scope_kind = bucket_spec.get("scope", "account")
    scope_name = _fmt(bucket_spec.get("scope_name"), **ctx) if bucket_spec.get("scope_name") else None
    return Bucket(
        label=_fmt(bucket_spec.get("label"), **ctx) or window,
        window=window,
        used_pct=_used_pct(source, bucket_spec),
        resets_at=_resets_at(
            dig(source, bucket_spec.get("resets_at_path")), bucket_spec.get("resets_at_kind", "iso")
        ),
        scope=Scope(scope_kind, scope_name),  # type: ignore[arg-type]
        horizon=horizon,  # type: ignore[arg-type]
        note=bucket_spec.get("note"),
    )


def make_provider(spec: dict) -> Callable[[], ProviderResult]:
    """스펙 dict → fetch 함수."""
    provider_id = spec.get("id")
    if not provider_id:
        raise SpecError("스펙에 id 가 없습니다")
    request_spec = spec.get("request") or {}
    if not request_spec.get("url"):
        raise SpecError(f"{provider_id}: request.url 이 필요합니다")

    def fetch() -> ProviderResult:
        token = _resolve_token(spec)
        headers = {k: v.format(token=token) for k, v in (request_spec.get("headers") or {}).items()}
        raw = request_json(
            request_spec["url"],
            method=request_spec.get("method", "GET"),
            headers=headers,
            body=request_spec.get("body"),
            timeout=float(request_spec.get("timeout", 20)),
        )
        buckets: list[Bucket] = []
        for bucket_spec in spec.get("buckets") or []:
            if for_each := bucket_spec.get("for_each"):
                items = dig(raw, for_each) or []
                if isinstance(items, dict):
                    items = [{"key": k, **v} if isinstance(v, dict) else {"key": k} for k, v in items.items()]
                for item in items:
                    buckets.append(_build_bucket(bucket_spec, item, item=item, token=""))
            else:
                buckets.append(_build_bucket(bucket_spec, raw, token=""))
        plan = dig(raw, spec.get("plan_path"))
        return ProviderResult(
            id=provider_id,
            plan=str(plan) if isinstance(plan, str) else None,
            buckets=buckets,
            source=f"spec:{spec.get('source_file', 'inline')}",
            raw=raw,
        )

    return fetch


def discover_specs() -> dict[str, Callable[[], ProviderResult]]:
    dirs = list(SPEC_DIRS)
    if extra := os.environ.get("SCOPEFUEL_SPEC_DIR"):
        dirs.append(pathlib.Path(os.path.expanduser(extra)))
    found: dict[str, Callable[[], ProviderResult]] = {}
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.toml")):
            try:
                spec = tomllib.loads(path.read_text())
            except (OSError, tomllib.TOMLDecodeError):
                continue  # 깨진 스펙 하나가 도구 전체를 막지 않는다
            spec["source_file"] = path.name
            try:
                found[str(spec.get("id"))] = make_provider(spec)
            except SpecError:
                continue
    found.pop("None", None)
    return found
