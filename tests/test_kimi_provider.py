from __future__ import annotations

import pathlib

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


# ------------------------------------------------------------------ #573
# The /usage panel renders one row per managed /usages entry — 5h, weekly,
# AND monthly (the membership quota that freezes all usage on its own). The
# parser used to drop the monthly row and ignore quota-403 text printed next
# to otherwise-healthy rows, so an exhausted pool surfaced as "5h 0% · 주 0%".


def test_parse_monthly_limit_row_is_a_third_account_bucket():
    result = kimi.parse(
        "5h limit: 0% used (resets in 3h)\nWeekly limit: 12% used (resets in 4d)\nMonthly limit: 47% used\n"
    )

    assert result.error is None
    assert [(b.label, b.window, b.horizon, b.used_pct) for b in result.buckets] == [
        ("5h", "5h", "now", 0.0),
        ("weekly", "7d", "week", 12.0),
        ("monthly", "30d", "month", 47.0),
    ]
    assert all(bucket.scope.kind == "account" for bucket in result.buckets)


def test_parse_quota_403_text_is_an_error_even_with_healthy_rows():
    # The incident rendering: rows read 0% used while the account was blocked.
    result = kimi.parse(
        "5h limit: 0% used\n"
        "Weekly limit: 0% used\n"
        "403 You've reached your 5-hour usage limit. Your quota will reset "
        "when the current 5-hour window ends.\n"
    )

    assert result.error is not None
    assert "사용 한도" in result.error
    assert "5-hour usage limit" in result.error
    assert result.buckets == []


def test_parse_weekly_usage_limit_text_is_an_error():
    result = kimi.parse(
        "5h limit: 0% used\n"
        "Weekly limit: 0% used\n"
        "403 You've reached your weekly usage limit for this billing cycle.\n"
    )

    assert result.error is not None
    assert result.buckets == []


def test_parse_failed_to_fetch_usage_is_an_error():
    result = kimi.parse("Failed to fetch usage: HTTP 403\n")

    assert result.error is not None
    assert result.buckets == []


def test_parse_monthly_only_output_is_unmeasurable():
    # Shape change: only the membership row rendered — the 5h/weekly windows
    # are invisible, so the reading must not become a usable measurement.
    result = kimi.parse("Monthly limit: 100% used\n")

    assert result.error is not None
    assert "quota 줄을 찾지 못함" in result.error
    assert result.buckets == []


def test_parse_changed_panel_shape_is_unmeasurable_not_zero():
    result = kimi.parse("Plan usage\n  No usage data available.\n")

    assert result.error is not None
    assert result.buckets == []


def test_parse_dollar_amounts_do_not_trip_the_403_marker():
    result = kimi.parse(
        "5h limit: 0% used\n"
        "Weekly limit: 0% used\n"
        "Extra Usage\n"
        "  Used this month  $403.20\n"
        "  Monthly limit    Unlimited\n"
        "  Balance          $96.80\n"
    )

    assert result.error is None
    assert [(b.label, b.used_pct) for b in result.buckets] == [
        ("5h", 0.0),
        ("weekly", 0.0),
    ]


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


def test_fetch_sets_pty_winsize_and_columns_lines_env(tmp_path, monkeypatch):
    binary = tmp_path / "fake-kimi-winsize"
    binary.write_text(
        "#!/bin/sh\n"
        "printf 'Kimi Code\\r\\n'\n"
        'printf \'LINES=%s COLUMNS=%s\\r\\n\' "$LINES" "$COLUMNS"\n'
        "sleep 0.1\n"
        "printf '│ >\\r\\n'\n"
        "IFS= read -r command\n"
        "[ \"$command\" = '/usage' ] || exit 9\n"
        "printf 'Weekly: 80%% left (resets in 1d)\\r\\n5h: 50%% left (resets in 1h)\\r\\n'\n"
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(kimi, "BINARY", str(binary))
    ioctl_calls = []
    original_ioctl = kimi.fcntl.ioctl

    def recording_ioctl(fd, request, argument):
        ioctl_calls.append((fd, request, argument))
        return original_ioctl(fd, request, argument)

    monkeypatch.setattr(kimi.fcntl, "ioctl", recording_ioctl)

    result = kimi.fetch()

    assert result.error is None
    assert result.buckets[0].used_pct == 50.0
    assert ioctl_calls
    assert ioctl_calls[0][1] == kimi.termios.TIOCSWINSZ
    assert kimi.struct.unpack("HHHH", ioctl_calls[0][2]) == (50, 200, 0, 0)
    assert "LINES=50" in result.raw["stdout"] or "COLUMNS=200" in result.raw["stdout"]


def test_fetch_auto_accepts_trust_in_a_per_probe_instance_dir(tmp_path, monkeypatch):
    """Each probe gets a fresh flocked ``probe-*`` instance dir as child cwd.

    #608: the child's cwd moved from the shared workdir to a per-probe
    instance directory so proctrack can identify its descendants. The fake's
    ``.trusted`` marker therefore does not survive between probes — the trust
    prompt reappears and is auto-accepted every run.
    """
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
    assert "Trust this folder?" in second_output
    assert "TRUST_ACCEPTED" in second_output
    cwds = [
        pathlib.Path(line.removeprefix("CWD=").strip())
        for line in (first_output + second_output).splitlines()
        if line.startswith("CWD=")
    ]
    assert len(cwds) == 2
    for cwd in cwds:
        assert cwd.resolve().parent == workdir.resolve()
        assert cwd.name.startswith("probe-")
    # Probe exit removes the instance dirs; only the sweep lock file remains.
    assert [p for p in workdir.iterdir() if p.name.startswith("probe-")] == []
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
    assert child_env["COLUMNS"] == "200"
    assert child_env["LINES"] == "50"
