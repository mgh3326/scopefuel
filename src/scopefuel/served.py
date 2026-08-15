"""Upstream serving-model recording + drift verification (ROB-1275).

Unversioned ClinePass slugs (e.g. ``cline-pass/deepseek-v4-flash``) are silently
re-anchored to new models by the vendor. A grade is only meaningful against the
*baseline observed at measurement time* recorded on each ``Profile``
(``upstream_model`` / ``upstream_as_of``).  This module probes the live upstream
with a minimal request and compares the echo ``model`` field against that
baseline so a silent re-anchor turns into an operator-visible ``drift``.

Designed so the network boundary is injectable: the render/exit logic takes a
``probe`` callable, so unit tests never touch a real socket.
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass

from . import http
from .providers.clinepass import COMPLETIONS_URL
from .recommend import GRADE_TABLE, Profile, profile_pool

# Path override for tests; otherwise the same opencode auth.json the clinepass
# provider reads.
_DEFAULT_OPENCODE_AUTH = pathlib.Path.home() / ".local" / "share" / "opencode" / "auth.json"
OPENCODE_AUTH_ENV = "SCOPEFUEL_OPENCODE_AUTH"

VerdictStatus = str  # "match" | "drift" | "unknown"


def opencode_auth_path() -> pathlib.Path:
    override = os.environ.get(OPENCODE_AUTH_ENV)
    return pathlib.Path(override) if override else _DEFAULT_OPENCODE_AUTH


def clinepass_key(path: str | os.PathLike | None = None) -> str | None:
    """Return the ClinePass API key from ``cline-pass.key`` in opencode auth.json.

    Returns None when the file is missing/unreadable or the entry is absent/present
    with no value.  The method never reveals the key itself.
    """
    auth = pathlib.Path(path) if path is not None else opencode_auth_path()
    try:
        data = json.loads(auth.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get("cline-pass")
    if not isinstance(entry, dict):
        return None
    key = str(entry.get("key") or "").strip()
    return key or None


def served_profiles() -> list[Profile]:
    """ClinePass profiles that carry an unversioned request slug to verify."""
    return [
        p
        for profiles in GRADE_TABLE.values()
        for p in profiles
        if p.served_slug and profile_pool(p.name)[0] == "clinepass"
    ]


@dataclass(frozen=True)
class Verdict:
    profile: str
    request_slug: str
    recorded: str | None
    recorded_as_of: str | None
    live: str | None
    status: VerdictStatus
    note: str | None = None


PROBE_BODY_TEMPLATE: dict[str, object] = {
    "model": "",  # filled per request
    "messages": [{"role": "user", "content": "p"}],
    "max_tokens": 1,
    "stream": False,
}


def probe_upstream(
    key: str, request_slug: str, *, endpoint: str = COMPLETIONS_URL, timeout: float = 20.0
) -> str | None:
    """One minimal completion request; return the echo ``model`` or None on failure.

    Returns None for network/HTTP errors and for responses that omit a ``model``
    field.  The key is used only in the Authorization header and never echoed.
    """
    body = dict(PROBE_BODY_TEMPLATE)
    body["model"] = request_slug
    try:
        data = http.request_json(
            endpoint,
            method="POST",
            body=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
    except (http.HttpError, OSError, ValueError):
        return None
    # ClinePass wraps the OpenAI envelope under ``data``; accept either location.
    raw = http.dig(data, ["data", "model"])
    if raw is None:
        raw = data.get("model")
    return str(raw).strip() if isinstance(raw, str) and raw.strip() else None


def classify(profile: Profile, live: str | None) -> Verdict:
    """Compare one live echo against the recorded upstream baseline."""
    if not profile.upstream_model:
        return Verdict(profile.name, profile.served_slug, None, profile.upstream_as_of, live, "unknown")
    if live is None:
        return Verdict(
            profile.name,
            profile.served_slug,
            profile.upstream_model,
            profile.upstream_as_of,
            None,
            "unknown",
            note="라이브 미관측(요청 실패/응답 model 없음)",
        )
    status = "match" if profile.upstream_model == live else "drift"
    return Verdict(
        profile.name, profile.served_slug, profile.upstream_model, profile.upstream_as_of, live, status
    )


def run_verification(
    profiles: list[Profile] | None = None,
    *,
    key: str | None = None,
    probe=probe_upstream,
) -> tuple[list[Verdict], int]:
    """Probe each profile and return (verdicts, exit_code).

    exit_code is 1 when at least one profile drifts (recorded baseline non-empty
    and disagrees with the live echo), else 0.  Profile rows with no baseline or
    a failed probe classify as ``unknown`` and never trip the exit code.
    """
    targets = served_profiles() if profiles is None else profiles
    verdicts: list[Verdict] = []
    for profile in targets:
        assert profile.served_slug is not None
        live = probe(key, profile.served_slug) if key else None
        verdicts.append(classify(profile, live))
    exit_code = 1 if any(v.status == "drift" for v in verdicts) else 0
    return verdicts, exit_code


def render(verdicts: list[Verdict], *, key_present: bool) -> str:
    lines = [f"ClinePass 업스트림 서빙 모델 대조 (auth: {'설정됨' if key_present else '없음'})"]
    lines.append(f"{'프로필':<14} {'요청 슬러그':<30} {'기록':<28} {'기준일':<12} {'라이브':<28} {'판정':<8}")
    for v in verdicts:
        lines.append(
            f"{v.profile:<14} {v.request_slug:<30} {(v.recorded or '-'):<28} "
            f"{(v.recorded_as_of or '-'):<12} {(v.live or '-'):<28} {v.status}"
        )
    for v in verdicts:
        if v.note:
            lines.append(f"  - {v.profile}: {v.note}")
    return "\n".join(lines)
