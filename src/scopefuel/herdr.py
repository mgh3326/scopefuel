"""Display-only Herdr pane metadata bridge.

The plugin hook receives ``HERDR_PLUGIN_EVENT_JSON``.  Herdr 0.8.2 puts the
event fields below ``data``; accepting a top-level object as well keeps local
hook tests and older plugin hosts harmless.  This module deliberately reports
only a custom token: it never changes an agent, pane title, action, or overlay.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import subprocess
import time
from collections.abc import Callable

from .cache import cache_dir, collect
from .model import ProviderResult

DEBOUNCE_S = 60.0
TOKEN_NAME = "scopefuel_quota"
SOURCE = "scopefuel.gauge"

_AGENT_PROVIDERS = {
    "claude": "claude",
    "claude-code": "claude",
    "codex": "codex",
    "agy": "agy",
    "antigravity": "agy",
    "grok": "grok",
    "kimi": "kimi",
}


def _event_data(raw: str | None) -> dict[str, object] | None:
    try:
        event = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    data = event.get("data", event)
    return data if isinstance(data, dict) else None


def _provider(data: dict[str, object]) -> str | None:
    for key in ("agent", "display_agent"):
        value = data.get(key)
        if isinstance(value, str):
            normalized = value.strip().lower().replace(" ", "-")
            if normalized in _AGENT_PROVIDERS:
                return _AGENT_PROVIDERS[normalized]
    return None


def _event_env(data: dict[str, object]) -> dict[str, str]:
    """Read only location selectors supplied by the pane event, never secrets."""
    candidate = data.get("env", data.get("environment", {}))
    if not isinstance(candidate, dict):
        return {}
    allowed = {"HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "GROK_HOME"}
    return {key: value for key, value in candidate.items() if key in allowed and isinstance(value, str)}


def _credential(provider: str, env: dict[str, str]) -> tuple[str, dict[str, str]]:
    keys = {
        "claude": ("CLAUDE_CONFIG_DIR", "HOME"),
        "codex": ("CODEX_HOME", "HOME"),
        "grok": ("GROK_HOME", "HOME"),
    }.get(provider, ("HOME",))
    identity = "|".join(f"{key}={env[key]}" for key in keys if env.get(key))
    if not identity:
        return "default", {}
    # A short non-reversible ID distinguishes pane accounts without publishing
    # their home/configuration path into Herdr metadata.
    scoped = dict(env)
    if provider == "claude" and "CLAUDE_CONFIG_DIR" not in scoped and scoped.get("HOME"):
        scoped["CLAUDE_CONFIG_DIR"] = str(pathlib.Path(scoped["HOME"]) / ".claude")
    if provider == "codex" and "CODEX_HOME" not in scoped and scoped.get("HOME"):
        scoped["CODEX_HOME"] = str(pathlib.Path(scoped["HOME"]) / ".codex")
    return hashlib.sha256(identity.encode()).hexdigest()[:10], scoped


def _state_path() -> pathlib.Path:
    configured = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    base = pathlib.Path(configured) if configured else cache_dir() / "herdr"
    base.mkdir(parents=True, exist_ok=True)
    return base / "pane-events.json"


@contextlib.contextmanager
def _locked_state() -> object:
    path = _state_path()
    lock = path.with_suffix(".lock")
    with lock.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                state = {}
            yield state if isinstance(state, dict) else {}
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _save_state(state: dict[str, object]) -> None:
    path = _state_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False))
    temporary.replace(path)


def _pct(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:g}%"


def _label(result: ProviderResult, credential: str) -> str:
    if result.error:
        return f"{result.id} unavailable · credential={credential}"
    verdict = result.verdict_at()
    identity = f"{result.id}·{result.plan}" if result.plan else result.id
    parts = [identity]
    if verdict.now_pct is not None:
        parts.append(f"now {_pct(verdict.now_pct)}")
    if verdict.week_pct is not None:
        parts.append(f"wk {_pct(verdict.week_pct)}")
    model_limits = [
        f"{bucket.scope.name} {_pct(bucket.used_pct)}"
        for bucket in result.buckets
        if bucket.scope.kind == "model" and bucket.scope.name and bucket.horizon == "week"
    ]
    if model_limits:
        parts.append("[" + ", ".join(model_limits) + "]")
    parts.append(f"credential={credential}")
    return " · ".join(parts)[:80]


def _observe_exhaustion(provider: str, result: ProviderResult, credential: str) -> None:
    """task #638 — 실행 중 잡의 풀 소진도 gate 와 같은 operator-switch 알림을 올린다.

    pane 이벤트로 갱신된 스냅샷이 확인된 account 소진을 보이면 ``exhaust.observe``
    에 넘긴다. 미설정 풀은 observe 가 조기 반환해 파일도 건드리지 않는다.
    stale·error 스냅샷과 리셋 시각이 지난 버킷은 소진 근거로 쓰지 않는다 —
    오래된 측정이 "계정 전환 필요" 를 오발하지 않게 한다(CodeRabbit PR#77).
    """
    import datetime as dt

    from . import exhaust, manual
    from .model import _is_valid_used_pct, _parse_reset

    if result.stale or result.error:
        return
    now = dt.datetime.now(dt.UTC)
    breach = manual.confirmed_automatic_cutoff(result, now=now)
    over_bucket = None
    if breach is not None:
        cutoff = breach[1]
        candidates = [
            bucket
            for bucket in result.buckets
            if bucket.scope.kind == "account"
            and _is_valid_used_pct(bucket.used_pct)
            and float(bucket.used_pct) >= cutoff
            and (bucket.resets_at is None or (reset := _parse_reset(bucket.resets_at)) is None or reset > now)
        ]
        over_bucket = max(candidates, key=lambda b: float(b.used_pct), default=None)
    exhaust.observe(
        provider,
        exhausted=over_bucket is not None,
        used_pct=breach[0] if breach is not None else None,
        cutoff=breach[1] if breach is not None else None,
        window=over_bucket.window if over_bucket else None,
        reset_at=over_bucket.resets_at if over_bucket else None,
        scope=credential if credential != "default" else None,
        now=now,
    )


def _report(pane_id: str, label: str) -> int:
    herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
    try:
        completed = subprocess.run(
            [
                herdr,
                "pane",
                "report-metadata",
                "--source",
                SOURCE,
                pane_id,
                "--token",
                f"{TOKEN_NAME}={label}",
                "--ttl-ms",
                str(int(DEBOUNCE_S * 1000)),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return 1
    return 0 if completed.returncode == 0 else completed.returncode


def handle_event(fetchers: dict[str, Callable[[], ProviderResult]]) -> int:
    data = _event_data(os.environ.get("HERDR_PLUGIN_EVENT_JSON"))
    if data is None:
        return 2
    pane_id = data.get("pane_id")
    provider = _provider(data)
    if not isinstance(pane_id, str) or not pane_id or provider not in fetchers:
        return 0  # A non-provider pane is intentionally not altered.

    credential, pane_env = _credential(provider, _event_env(data))
    key = f"{pane_id}:{provider}:{credential}"
    now = time.monotonic()
    with _locked_state() as state:
        prior = state.get(key)
        if isinstance(prior, dict) and now - float(prior.get("at", 0)) < DEBOUNCE_S:
            label = prior.get("label")
            if isinstance(label, str):
                return _report(pane_id, label)

        fetch_env = dict(pane_env)
        # The normal cache remains the default-account cache.  An explicitly
        # attributed pane gets the identical cache implementation in a separate
        # state file, preventing an account-A snapshot serving account-B.
        if credential != "default":
            cache_file = _state_path().parent / f"snapshot-{provider}-{credential}.json"
            fetch_env["SCOPEFUEL_CACHE"] = str(cache_file)
        old_env = {key: os.environ.get(key) for key in fetch_env}
        try:
            os.environ.update(fetch_env)
            result = collect({provider: fetchers[provider]}, [provider], ttl_s=DEBOUNCE_S)[0]
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        _observe_exhaustion(provider, result, credential)
        label = _label(result, credential)
        state[key] = {"at": now, "label": label}
        _save_state(state)
        return _report(pane_id, label)
