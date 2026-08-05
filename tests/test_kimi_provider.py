from __future__ import annotations

from scopefuel.providers import kimi

SAMPLE = "\x1b[2KWeekly: 75% left (resets in 5d 12h)\r\n\x1b[2K5h: 30% left (resets in 2h 10m)\r\n"


def test_parse_cli_usage_maps_remaining_percent_and_resets():
    result = kimi.parse(SAMPLE)

    assert result.id == "kimi"
    assert result.pool_class == "spend"
    assert result.source == "cli:/usage"
    assert [(bucket.label, bucket.window, bucket.horizon, bucket.used_pct) for bucket in result.buckets] == [
        ("5h", "5h", "now", 70.0),
        ("weekly", "7d", "week", 25.0),
    ]
    assert all(bucket.resets_at for bucket in result.buckets)
    assert all(bucket.scope.kind == "account" for bucket in result.buckets)


def test_parse_cli_usage_accepts_managed_plan_used_percent():
    result = kimi.parse("Weekly limit: 8% used (resets in 5d 12h)\n5h limit: 35% used (resets in 2h 10m)\n")

    assert [(bucket.label, bucket.used_pct) for bucket in result.buckets] == [
        ("5h", 35.0),
        ("weekly", 8.0),
    ]


def test_parse_rate_limit_is_an_immediate_error_without_retry():
    result = kimi.parse("HTTP 429 Too Many Requests\n")

    assert result.error == "Kimi CLI usage rate limited (HTTP 429/rate limit; retry 금지)"
    assert result.buckets == []
    assert result.raw == {"stdout": "HTTP 429 Too Many Requests\n"}


def test_fetch_uses_a_pty_and_sends_usage_once(tmp_path, monkeypatch):
    binary = tmp_path / "fake-kimi"
    binary.write_text(
        "#!/bin/sh\n"
        "printf 'Kimi Code\\r\\n'\n"
        "sleep 0.1\n"
        "printf '│ >\\r\\n'\n"
        "IFS= read -r command\n"
        "[ \"$command\" = '/usage' ] || exit 9\n"
        "printf 'Weekly: 80%% left (resets in 1d)\\r\\n5h: 50%% left (resets in 1h)\\r\\n'\n"
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(kimi, "BINARY", str(binary))

    result = kimi.fetch()

    assert result.error is None
    assert [(bucket.label, bucket.used_pct) for bucket in result.buckets] == [
        ("5h", 50.0),
        ("weekly", 20.0),
    ]


def test_fetch_auto_accepts_trust_and_reuses_fixed_workdir(tmp_path, monkeypatch):
    binary = tmp_path / "fake-kimi-trust"
    binary.write_text(
        "#!/bin/sh\n"
        "printf 'CWD=%s\\r\\n' \"$PWD\"\n"
        "if [ ! -f .trusted ]; then\n"
        "  printf 'Trust this folder?\\r\\n  ❯ Trust this folder\\r\\n'\n"
        "  IFS= read -r trust_input\n"
        '  [ -z "$trust_input" ] || exit 8\n'
        "  : > .trusted\n"
        "  printf 'TRUST_ACCEPTED\\r\\n'\n"
        "else\n"
        "  printf 'TRUST_ALREADY_ACCEPTED\\r\\n'\n"
        "fi\n"
        "printf '│ >\\r\\n'\n"
        "IFS= read -r command\n"
        "[ \"$command\" = '/usage' ] || exit 9\n"
        "printf 'Weekly: 80%% left (resets in 1d)\\r\\n5h: 50%% left (resets in 1h)\\r\\n'\n"
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    workdir = tmp_path / "provider-workdir"
    monkeypatch.setattr(kimi, "BINARY", str(binary))
    monkeypatch.setattr(kimi, "PROBE_WORKDIR", workdir)

    first_output = kimi._probe_once()
    second_output = kimi._probe_once()
    first = kimi.parse(first_output)
    second = kimi.parse(second_output)

    assert first.error is None
    assert second.error is None
    assert "Trust this folder?" in first_output
    assert "TRUST_ACCEPTED" in first_output
    assert "Trust this folder?" not in second_output
    assert "TRUST_ALREADY_ACCEPTED" in second_output
    assert f"CWD={workdir}" in first_output
    assert f"CWD={workdir}" in second_output
    assert [(bucket.label, bucket.used_pct) for bucket in second.buckets] == [
        ("5h", 50.0),
        ("weekly", 20.0),
    ]


def test_child_env_removes_herdr_integration_variables(monkeypatch):
    monkeypatch.setenv("HERDR_PANE_ID", "pane-1")
    monkeypatch.setenv("HERDR_AGENT_STATE", "working")

    child_env = kimi._child_env()

    assert "HERDR_PANE_ID" not in child_env
    assert "HERDR_AGENT_STATE" not in child_env
