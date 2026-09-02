"""Herdr pane quota bridge: event envelope, argv contract, and debounce."""

from __future__ import annotations

import json
import pathlib
import stat

from scopefuel import cli
from scopefuel.model import Bucket, ProviderResult, Scope


def _fake_herdr(tmp_path):
    log = tmp_path / "herdr-argv.jsonl"
    shim = tmp_path / "herdr"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['HERDR_TEST_LOG'], 'a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return shim, log


def _claude() -> ProviderResult:
    return ProviderResult(
        id="claude",
        plan="max",
        buckets=[
            Bucket(label="5h", window="5h", used_pct=6, horizon="now"),
            Bucket(label="7d", window="7d", used_pct=97, horizon="week"),
            Bucket(
                label="7d Fable",
                window="7d",
                used_pct=100,
                horizon="week",
                scope=Scope("model", "Fable"),
            ),
        ],
    )


def test_herdr_event_reports_display_only_token_and_debounces(tmp_path, monkeypatch):
    shim, log = _fake_herdr(tmp_path)
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        return _claude()

    monkeypatch.setattr(cli, "registry", lambda: {"claude": fetch})
    monkeypatch.setenv("HERDR_BIN_PATH", str(shim))
    monkeypatch.setenv("HERDR_TEST_LOG", str(log))
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(
        "HERDR_PLUGIN_EVENT_JSON",
        json.dumps({"type": "pane.agent_detected", "data": {"pane_id": "wB:p2R", "agent": "claude"}}),
    )

    assert cli.main(["herdr-event"]) == 0
    assert cli.main(["herdr-event"]) == 0
    assert calls == 1
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0] == [
        "pane",
        "report-metadata",
        "--source",
        "scopefuel.gauge",
        "wB:p2R",
        "--token",
        "scopefuel_quota=claude·max · now 6% · wk 97% · [Fable 100%] · credential=default",
        "--ttl-ms",
        "60000",
    ]


def test_herdr_event_ignores_unknown_agent_without_fetch_or_report(tmp_path, monkeypatch):
    shim, log = _fake_herdr(tmp_path)
    monkeypatch.setattr(cli, "registry", lambda: {"claude": lambda: (_ for _ in ()).throw(AssertionError())})
    monkeypatch.setenv("HERDR_BIN_PATH", str(shim))
    monkeypatch.setenv("HERDR_TEST_LOG", str(log))
    monkeypatch.setenv("HERDR_PLUGIN_EVENT_JSON", json.dumps({"data": {"pane_id": "w1:p1", "agent": "bash"}}))

    assert cli.main(["herdr-event"]) == 0
    assert not log.exists()


def test_manifest_keeps_existing_overlay_and_actions_and_adds_all_event_hooks():
    text = (pathlib.Path(__file__).parent.parent / "herdr-plugin.toml").read_text()
    assert 'placement = "overlay"' in text
    assert 'id = "check"' in text and 'id = "check-now"' in text
    for event in ("pane.agent_detected", "pane.agent_status_changed", "pane.focused"):
        assert f'on = "{event}"' in text
