"""herdr 플러그인 매니페스트 계약: 이벤트 훅은 pane 당 1회짜리 하나만.

2026-09-04 실측: status_changed/focused 훅이 codex 상태 플래핑 때 pane×훅 만큼
자식 프로세스를 폭주시켜 herdr 서버(API 소켓)를 묶었다. 훅이 다시 늘면 RED.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "herdr-plugin.toml"
ALLOWED_EVENT_HOOKS = {"pane.agent_detected"}
FORBIDDEN_EVENT_HOOKS = {"pane.agent_status_changed", "pane.focused", "tab.focused", "workspace.focused"}


def _manifest() -> dict:
    return tomllib.loads(MANIFEST.read_text(encoding="utf-8"))


def test_event_hooks_are_exactly_the_once_per_pane_set() -> None:
    hooks = [event["on"] for event in _manifest().get("events", [])]
    assert hooks == sorted(ALLOWED_EVENT_HOOKS), hooks


def test_no_high_frequency_event_hooks() -> None:
    hooks = {event["on"] for event in _manifest().get("events", [])}
    assert not (hooks & FORBIDDEN_EVENT_HOOKS), hooks & FORBIDDEN_EVENT_HOOKS


def test_every_event_hook_runs_the_debounced_handler() -> None:
    for event in _manifest().get("events", []):
        assert event["command"] == ["scopefuel", "herdr-event"], event
