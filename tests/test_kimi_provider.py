from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re

from scopefuel.providers import kimi
from scopefuel.recommend import gate_check

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


# The panel the real CLI actually draws (from buildManagedUsageSection in the
# installed bundle): no parens around "resets in", and the hint always carries
# an hour component — "resets in 20d 15h 2m" contains the substring "15h".
REAL_EXHAUSTED_MONTHLY = (
    "Plan usage\n"
    "  5h limit       ░░░░    0% used   resets in 3h 12m\n"
    "  Weekly limit   ░░░░    0% used   resets in 4d 2h 1m\n"
    "  Monthly limit  ████  100% used   resets in 20d 15h 2m\n"
)


def test_parse_real_render_monthly_row_with_15h_reset_is_monthly():
    """#573 tester blocker B1: a monthly reset hint containing "5h"/"15h" must
    not classify the row as session — an exhausted monthly cap was dropped."""
    result = kimi.parse(REAL_EXHAUSTED_MONTHLY)

    assert result.error is None
    assert [(b.label, b.window, b.used_pct) for b in result.buckets] == [
        ("5h", "5h", 0.0),
        ("weekly", "7d", 0.0),
        ("monthly", "30d", 100.0),
    ]
    # Bare (paren-less) reset hints are parsed too.
    assert all(bucket.resets_at for bucket in result.buckets)


def test_parse_real_render_monthly_row_with_5h_reset_is_monthly():
    result = kimi.parse(
        "  5h limit       ░░░░    0% used   resets in 3h 12m\n"
        "  Weekly limit   ░░░░    0% used   resets in 4d 2h 1m\n"
        "  Monthly limit  ████  100% used   resets in 20d 5h 2m\n"
    )

    assert result.error is None
    assert [(b.label, b.used_pct) for b in result.buckets] == [
        ("5h", 0.0),
        ("weekly", 0.0),
        ("monthly", 100.0),
    ]


def test_parse_monthly_only_with_5h_in_reset_is_unmeasurable():
    # Monthly row at month-end can reset within hours ("resets in 5h 10m") —
    # it must not turn into a session bucket.
    result = kimi.parse("  Monthly limit  ████  100% used   resets in 5h 10m\n")

    assert result.error is not None
    assert result.buckets == []


def test_parse_dollar_429_does_not_trip_the_rate_limit_marker():
    result = kimi.parse(
        "5h limit: 0% used\n"
        "Weekly limit: 0% used\n"
        "Extra Usage\n"
        "  Used this month  $429.10\n"
        "  Balance          $70.90\n"
    )

    assert result.error is None
    assert len(result.buckets) == 2


def test_parse_context_token_counts_do_not_trip_the_403_marker():
    result = kimi.parse(
        "5h limit: 0% used\nWeekly limit: 0% used\nContext  (403 / 256k)\nSession tokens  403k\n"
    )

    assert result.error is None
    assert len(result.buckets) == 2


def test_parse_unknown_limit_row_is_unmeasurable():
    # A new quota dimension we cannot classify must not be silently dropped —
    # it may be the binding constraint.
    result = kimi.parse("5h limit: 0% used\nWeekly limit: 0% used\nDaily limit 100% used\n")

    assert result.error is not None
    assert "알 수 없는 quota 행" in result.error


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


# ------------------------------------------------------------------ #705
# Real v2.1.0 capture (one allowed live probe, 2026-09-25, identifiers
# redacted): the weekly quota was locked out at the provider — the account's
# sessions were failing with '403 weekly (7-day) usage limit' — while the
# /usage panel still rendered '0% used' for both windows.  Field semantics:
# the panel prints the server's used_ratio verbatim ('% used' = used; only the
# legacy '% left' spelling needs the 100-remaining inversion).

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PANEL_LOCKOUT = (FIXTURES / "kimi_usage_v210_weekly_lockout.txt").read_text()
SESSION_LOCKOUT_LOG = (FIXTURES / "kimi_session_weekly_lockout_log.txt").read_text().strip()


def _session_log_at(ts: dt.datetime) -> str:
    """The fixture WARN line re-dated: the lockout timestamp is relative to now."""
    return re.sub(r"^\S+", ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", SESSION_LOCKOUT_LOG)


def _write_session_log(root: pathlib.Path, body: str, *, when: dt.datetime) -> None:
    path = root / "wd_test" / "session_x" / "logs" / "kimi-code.log"
    path.parent.mkdir(parents=True)
    path.write_text(body)
    os.utime(path, (when.timestamp(), when.timestamp()))


def test_parse_real_panel_fixture_reads_used_pct_verbatim():
    """The captured panel literally reports '0% used' — parse reads it as used,
    not remaining.  The lockout signal lives in session records, not here."""
    result = kimi.parse(PANEL_LOCKOUT)

    assert result.error is None
    assert [(b.label, b.window, b.used_pct) for b in result.buckets] == [
        ("5h", "5h", 0.0),
        ("weekly", "7d", 0.0),
    ]


def test_parse_zero_percent_left_is_fully_used_and_gate_refuses():
    """Exhausted in the 'remaining' spelling: 0% left means used_pct 100, and a
    spend pool over the 99% cutoff is refused.  Mutant guard — dropping the
    100-remaining inversion in parse() turns this RED (0.0 would pass)."""
    result = kimi.parse("5h limit: 0% left (resets in 3h)\nWeekly limit: 0% left (resets in 5d)\n")

    assert [b.used_pct for b in result.buckets] == [100.0, 100.0]
    gate = gate_check([result], "kimi-k3")
    assert gate.ok is False
    assert gate.unmeasurable is False
    assert "소진" in gate.reason


def test_parse_half_remaining_is_half_used():
    result = kimi.parse("5h limit: 50% left (resets in 3h)\nWeekly limit: 50% left (resets in 5d)\n")

    assert [b.used_pct for b in result.buckets] == [50.0, 50.0]


def test_parse_ambiguous_bare_percent_row_fails_closed():
    """A quota row whose percentage has no left/used qualifier cannot be read —
    it must poison the whole reading instead of being dropped as a silent 0."""
    result = kimi.parse(
        "5h limit      ░░░░  40%   resets in 2h\nWeekly limit  ░░░░  40% used   resets in 5d\n"
    )

    assert result.error is not None
    assert result.buckets == []
    gate = gate_check([result], "kimi-k3")
    assert gate.ok is False
    assert gate.unmeasurable is True


def test_observed_weekly_lockout_marks_weekly_exhausted(tmp_path, monkeypatch):
    """#705 incident shape end to end: panel says 0% used, session log shows a
    weekly-limit 403 inside the current window -> weekly is exhausted and the
    gate refuses with the 소진 reason, not a clean pass."""
    monkeypatch.setattr(kimi, "SESSIONS_DIR", tmp_path)
    now = dt.datetime.now(dt.UTC)
    _write_session_log(tmp_path, _session_log_at(now - dt.timedelta(hours=1)) + "\n", when=now)

    checked = kimi._apply_observed_lockouts(kimi.parse(PANEL_LOCKOUT), now=now)

    assert checked.error is None
    assert [(b.label, b.used_pct) for b in checked.buckets] == [("5h", 0.0), ("weekly", 100.0)]
    assert "usage limit" in checked.buckets[1].note
    gate = gate_check([checked], "kimi-k3")
    assert gate.ok is False
    assert "소진" in gate.reason


def test_lockout_from_a_prior_window_is_stale_and_ignored(tmp_path, monkeypatch):
    """A 403 older than the current window start belongs to a window that has
    already reset — it must not mark the pool exhausted."""
    monkeypatch.setattr(kimi, "SESSIONS_DIR", tmp_path)
    now = dt.datetime.now(dt.UTC)
    _write_session_log(tmp_path, _session_log_at(now - dt.timedelta(days=9)) + "\n", when=now)

    checked = kimi._apply_observed_lockouts(kimi.parse(PANEL_LOCKOUT), now=now)

    assert checked.error is None
    assert [b.used_pct for b in checked.buckets] == [0.0, 0.0]


def test_lockout_error_without_matching_panel_window_is_unmeasurable(tmp_path, monkeypatch):
    """A monthly-limit 403 with no monthly row on the panel cannot be bounded —
    fail closed as unmeasurable rather than ignore it."""
    monkeypatch.setattr(kimi, "SESSIONS_DIR", tmp_path)
    now = dt.datetime.now(dt.UTC)
    ts = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    _write_session_log(
        tmp_path,
        f"{ts}Z WARN  llm request failed  errorName=APIStatusError "
        'errorMessage="403 You\'ve reached your monthly usage limit." statusCode=403\n',
        when=now,
    )

    checked = kimi._apply_observed_lockouts(kimi.parse(PANEL_LOCKOUT), now=now)

    assert checked.error is not None
    assert "측정 불가" in checked.error
    gate = gate_check([checked], "kimi-k3")
    assert gate.ok is False
    assert gate.unmeasurable is True


def test_lockout_error_without_a_window_name_is_unmeasurable(tmp_path, monkeypatch):
    monkeypatch.setattr(kimi, "SESSIONS_DIR", tmp_path)
    now = dt.datetime.now(dt.UTC)
    ts = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    _write_session_log(
        tmp_path,
        f"{ts}Z WARN  llm request failed  errorName=APIStatusError "
        'errorMessage="403 usage limit reached" statusCode=403\n',
        when=now,
    )

    checked = kimi._apply_observed_lockouts(kimi.parse(PANEL_LOCKOUT), now=now)

    assert checked.error is not None
    assert "측정 불가" in checked.error


def test_wire_jsonl_lockout_marks_exhausted(tmp_path, monkeypatch):
    """The same signal in the protocol record (epoch-ms 'time' field)."""
    monkeypatch.setattr(kimi, "SESSIONS_DIR", tmp_path)
    now = dt.datetime.now(dt.UTC)
    ms = int((now - dt.timedelta(hours=1)).timestamp() * 1000)
    path = tmp_path / "wd_test" / "session_x" / "agents" / "main"
    path.mkdir(parents=True)
    wire = path / "wire.jsonl"
    wire.write_text(
        '{"type":"turn.ended","agentId":"main","turnId":0,"reason":"failed",'
        '"error":{"code":"provider.auth_error","message":"403 You\'ve reached your '
        'weekly (7-day) usage limit."},"time":' + str(ms) + "}\n"
    )

    checked = kimi._apply_observed_lockouts(kimi.parse(PANEL_LOCKOUT), now=now)

    assert checked.error is None
    assert [(b.label, b.used_pct) for b in checked.buckets] == [("5h", 0.0), ("weekly", 100.0)]


def test_no_session_records_leaves_result_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(kimi, "SESSIONS_DIR", tmp_path)
    result = kimi.parse(PANEL_LOCKOUT)

    checked = kimi._apply_observed_lockouts(result, now=dt.datetime.now(dt.UTC))

    assert checked is result
    assert [b.used_pct for b in checked.buckets] == [0.0, 0.0]


def test_wire_transcript_records_are_not_lockouts(tmp_path, monkeypatch):
    """#705 tester F1: wire.jsonl embeds whole conversation and tool-result
    records — text quoting a 403 usage-limit message (or this repo's own
    files) must never be read as an observed provider lockout."""
    monkeypatch.setattr(kimi, "SESSIONS_DIR", tmp_path)
    now = dt.datetime.now(dt.UTC)
    ms = int((now - dt.timedelta(hours=1)).timestamp() * 1000)
    path = tmp_path / "wd_test" / "session_x" / "agents" / "main"
    path.mkdir(parents=True)
    (path / "wire.jsonl").write_text(
        json.dumps(
            {
                "type": "agent.message.appended",
                "agentId": "main",
                "message": {
                    "role": "assistant",
                    "content": "other panes saw: " + SESSION_LOCKOUT_LOG,
                },
                "time": ms,
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "context.append_loop_event",
                "agentId": "main",
                "payload": {
                    "tool": "cat",
                    "output": "src/scopefuel/providers/kimi.py mentions 403 usage limit and monthly windows",
                },
                "time": ms,
            }
        )
        + "\n"
    )

    checked = kimi._apply_observed_lockouts(kimi.parse(PANEL_LOCKOUT), now=now)

    assert checked.error is None
    assert [b.used_pct for b in checked.buckets] == [0.0, 0.0]


def test_log_line_outside_the_anchored_shape_is_not_a_lockout(tmp_path, monkeypatch):
    """#705 tester F1: only '<ISO>Z WARN/ERROR llm request failed' lines with an
    APIStatusError/auth_error context and a usage-limit errorMessage count —
    a quoted or malformed line does not."""
    monkeypatch.setattr(kimi, "SESSIONS_DIR", tmp_path)
    now = dt.datetime.now(dt.UTC)
    ts = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    _write_session_log(
        tmp_path,
        # no WARN/ERROR anchor — a pasted quote of a failure
        f"{ts}Z INFO  panel note: 403 You've reached your weekly usage limit\n"
        # WARN but no auth/403 context marker and no errorMessage field
        f"{ts}Z WARN  something else: usage limit 403\n",
        when=now,
    )

    checked = kimi._apply_observed_lockouts(kimi.parse(PANEL_LOCKOUT), now=now)

    assert checked.error is None
    assert [b.used_pct for b in checked.buckets] == [0.0, 0.0]


def test_wire_5h_lockout_with_7d_in_trace_id_stays_session(tmp_path, monkeypatch):
    """#705 tester F2: a bare '7d' substring inside a hex traceId must not
    reclassify a 5-hour lockout as weekly."""
    monkeypatch.setattr(kimi, "SESSIONS_DIR", tmp_path)
    now = dt.datetime.now(dt.UTC)
    ms = int((now - dt.timedelta(hours=1)).timestamp() * 1000)
    path = tmp_path / "wd_test" / "session_x" / "agents" / "main"
    path.mkdir(parents=True)
    (path / "wire.jsonl").write_text(
        '{"type":"turn.ended","agentId":"main","turnId":0,"reason":"failed",'
        '"traceId":"ab7d9f4e7d11","error":{"code":"provider.auth_error","message":'
        "\"403 You've reached your 5-hour usage limit. Your quota will reset when "
        'the current 5-hour window ends."},"time":' + str(ms) + "}\n"
    )

    checked = kimi._apply_observed_lockouts(kimi.parse(PANEL_LOCKOUT), now=now)

    assert checked.error is None
    assert [(b.label, b.used_pct) for b in checked.buckets] == [("5h", 100.0), ("weekly", 0.0)]


def test_lockout_between_window_start_and_now_minus_window_is_stale(tmp_path, monkeypatch):
    """#705 tester F4: the window anchor is resets_at - window, not now -
    window.  Fixture weekly resets in ~5d9h, so the current window started
    ~1.6d ago; an error 2d ago predates it and must be ignored (under the
    now-minus-window mutant it would wrongly mark weekly exhausted)."""
    monkeypatch.setattr(kimi, "SESSIONS_DIR", tmp_path)
    now = dt.datetime.now(dt.UTC)
    _write_session_log(tmp_path, _session_log_at(now - dt.timedelta(days=2)) + "\n", when=now)

    checked = kimi._apply_observed_lockouts(kimi.parse(PANEL_LOCKOUT), now=now)

    assert checked.error is None
    assert [b.used_pct for b in checked.buckets] == [0.0, 0.0]


def test_fetch_applies_session_lockouts(tmp_path, monkeypatch):
    """#705 tester F3: fetch() runs the lockout cross-check — kills the mutant
    that returns parse(output) without _apply_observed_lockouts."""
    binary = tmp_path / "fake-kimi-lockout"
    binary.write_text(
        "#!/bin/sh\n"
        "printf 'Kimi Code\\r\\n'\n"
        "sleep 0.1\n"
        "printf '│ >\\r\\n'\n"
        "IFS= read -r command\n"
        "[ \"$command\" = '/usage' ] || exit 9\n"
        "printf '5h limit       0%% used   resets in 3m\\r\\n"
        "Weekly limit   0%% used   resets in 5d\\r\\n'\n"
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(kimi, "BINARY", str(binary))
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(kimi, "SESSIONS_DIR", sessions)
    now = dt.datetime.now(dt.UTC)
    _write_session_log(sessions, _session_log_at(now - dt.timedelta(hours=1)) + "\n", when=now)

    result = kimi.fetch()

    assert result.error is None
    assert [(b.label, b.used_pct) for b in result.buckets] == [("5h", 0.0), ("weekly", 100.0)]
    gate = gate_check([result], "kimi-k3")
    assert gate.ok is False
    assert "소진" in gate.reason
