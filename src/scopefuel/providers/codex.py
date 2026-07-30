"""OpenAI Codex CLI — ChatGPT backend usage API.

`~/.codex/auth.json` 의 tokens.access_token + ChatGPT-Account-Id 헤더로
`GET /backend-api/wham/usage`. primary/secondary window 는 계정 스코프,
`additional_rate_limits[]`(예: GPT-5.3-Codex-Spark)는 그 모델 한정 스코프다.
"""

from __future__ import annotations

import json
import math
import pathlib

from ..http import request_json
from ..model import Bucket, ProviderResult, Scope, epoch_to_iso, horizon_for, window_label

AUTH = pathlib.Path.home() / ".codex" / "auth.json"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def fetch() -> ProviderResult:
    if not AUTH.exists():
        return ProviderResult(
            id="codex", error="자격증명 파일 없음", hint=f"{AUTH} 없음 — `codex login` 후 재시도"
        )
    tokens = json.loads(AUTH.read_text()).get("tokens") or {}
    token = (tokens.get("access_token") or "").strip()
    if not token:
        return ProviderResult(id="codex", error="access_token 없음", hint="`codex login` 필요")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "scopefuel",
    }
    if account_id := tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = account_id

    raw = request_json(USAGE_URL, headers=headers)

    def windows(
        rate_limit: dict | None, *, label_prefix: str = "", scope: Scope | None = None
    ) -> list[Bucket]:
        out: list[Bucket] = []
        for key in ("primary_window", "secondary_window"):
            win = (rate_limit or {}).get(key)
            if not win:
                continue
            seconds = win.get("limit_window_seconds")
            label = window_label(seconds)
            out.append(
                Bucket(
                    label=f"{label_prefix}{label}",
                    window=label,
                    used_pct=_num(win.get("used_percent")),
                    resets_at=epoch_to_iso(win.get("reset_at")),
                    scope=scope or Scope("account"),
                    horizon=horizon_for(seconds),
                )
            )
        return out

    buckets = windows(raw.get("rate_limit"))
    for extra in raw.get("additional_rate_limits") or []:
        name = extra.get("limit_name") or "?"
        buckets += windows(extra.get("rate_limit"), label_prefix=f"{name} ", scope=Scope("model", name))

    note = None
    if (raw.get("rate_limit") or {}).get("limit_reached"):
        note = "limit reached"
    credits = raw.get("credits") or {}
    if credits.get("has_credits"):
        note = f"{note + ' / ' if note else ''}credits {credits.get('balance')}"

    return ProviderResult(
        id="codex",
        plan=raw.get("plan_type"),
        buckets=buckets,
        note=note,
        source="wham-usage-api",
        raw=raw,
    )


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
        return f if math.isfinite(f) and 0 <= f <= 100 else None
    except (TypeError, ValueError, OverflowError):
        return None
