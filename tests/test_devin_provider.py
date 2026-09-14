"""Devin SWE-2 Free-only provider and profile placement."""

from __future__ import annotations

import pytest

from scopefuel import cli
from scopefuel.providers import BUILTIN, devin
from scopefuel.recommend import (
    DEVIN_SWE2_ESTIMATE_REASON,
    DEVIN_SWE2_PLACEMENT_NOTE,
    ESTIMATED_EXTRAPOLATED_UNMEASURED_ANNOTATION,
    GRADE_TABLE,
    profile_pool,
)

FREE_NOTE = "free until ~2026-10-10"


def _fixture(fixture_text) -> str:
    return fixture_text("devin_models_list")


def _banner_fixture(fixture_text) -> str:
    return fixture_text("devin_banner")


def _without_swe2_free(text: str) -> str:
    """Remove only the Free tag from SWE-2 family model rows."""
    in_swe2 = False
    out: list[str] = []
    for line in text.splitlines(True):
        stripped = line.strip()
        if stripped.startswith("SWE-2 (swe-2)"):
            in_swe2 = True
            out.append(line)
            continue
        if in_swe2 and stripped and not line[:1].isspace() and "(" in stripped:
            in_swe2 = False
        if in_swe2 and "[262K context, Free]" in line:
            out.append(line.replace("[262K context, Free]", "[262K context]"))
        else:
            out.append(line)
    return "".join(out)


def _without_swe2_family(text: str) -> str:
    in_swe2 = False
    out: list[str] = []
    for line in text.splitlines(True):
        stripped = line.strip()
        if stripped.startswith("SWE-2 (swe-2)"):
            in_swe2 = True
            continue
        if in_swe2:
            if stripped and not line[:1].isspace() and "(" in stripped:
                in_swe2 = False
                out.append(line)
            continue
        out.append(line)
    return "".join(out)


def test_registry_exposes_devin_as_spend():
    assert BUILTIN["devin"].pool_class == "spend"


def test_parse_swe2_free_fixture_is_account_zero(fixture_text):
    result = devin.parse(_fixture(fixture_text))
    assert result.error is None
    assert result.id == "devin"
    assert result.pool_class == "spend"
    assert result.source == "cli:models list"
    assert result.note == FREE_NOTE
    assert len(result.buckets) == 1
    bucket = result.buckets[0]
    assert bucket.used_pct == 0.0
    assert bucket.scope.kind == "account"
    assert bucket.note == FREE_NOTE
    assert bucket.label == "swe-2"


def test_parse_strips_ansi_before_swe2_free_check():
    noisy = "\x1b[1mSWE-2 (swe-2)\x1b[0m\n  \x1b[32mswe-2-high\x1b[0m  SWE-2 High  [262K context, Free]\n"
    result = devin.parse(noisy)
    assert result.error is None
    assert result.buckets[0].used_pct == 0.0


def test_other_models_free_without_swe2_is_unknown(fixture_text):
    result = devin.parse(_without_swe2_family(_fixture(fixture_text)))
    assert result.error
    assert result.buckets == []
    assert result.verdict.mark == "degraded"


def test_swe2_free_tag_removed_is_unknown_not_zero(fixture_text):
    mutant = _without_swe2_free(_fixture(fixture_text))
    assert "SWE-2 (swe-2)" in mutant
    assert "SWE-2 High  [262K context, Free]" not in mutant
    assert "GLM-5.2 High  [200K context, Free]" in mutant
    result = devin.parse(mutant)
    assert result.error
    assert result.buckets == []
    assert result.note != FREE_NOTE
    assert result.verdict.mark == "degraded"


def test_unparsable_output_is_error_not_zero():
    result = devin.parse("Please log in to continue\n")
    assert result.error and result.buckets == []
    assert result.verdict.blocking_pct == 0


def test_fetch_missing_binary_is_error_with_hint(monkeypatch):
    monkeypatch.setattr(devin.shutil, "which", lambda _name: None)
    result = devin.fetch()
    assert result.error and result.hint
    assert result.buckets == []


def test_fetch_fake_binary_parses_fixture(tmp_path, monkeypatch, fixture_text):
    payload = _fixture(fixture_text)
    binary = tmp_path / "fake-devin"
    binary.write_text("#!/bin/sh\n" + "cat <<'EOF'\n" + payload + "EOF\n")
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))

    result = devin.fetch()
    assert result.error is None
    assert result.buckets[0].used_pct == 0.0
    assert result.note == FREE_NOTE


def test_fetch_rejects_nonzero_exit_even_with_swe2_free(tmp_path, monkeypatch, fixture_text):
    payload = _fixture(fixture_text)
    binary = tmp_path / "fake-devin-fail"
    binary.write_text("#!/bin/sh\ncat <<'EOF'\n" + payload + "EOF\nexit 2\n")
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))

    result = devin.fetch()
    assert result.error and "종료코드 2" in result.error
    assert result.buckets == []


def test_fetch_timeout_is_degraded(tmp_path, monkeypatch):
    binary = tmp_path / "fake-devin-timeout"
    binary.write_text("#!/bin/sh\nsleep 2\n")
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))
    monkeypatch.setattr(devin, "TIMEOUT_S", 0.1)

    result = devin.fetch()
    assert result.error and "안에 끝나지 않음" in result.error
    assert result.buckets == []
    assert result.verdict.mark == "degraded"


def test_child_env_removes_herdr_integration_variables(monkeypatch):
    monkeypatch.setenv("HERDR_PANE_ID", "pane-1")
    child_env = devin._child_env()
    assert "HERDR_PANE_ID" not in child_env


def test_profile_pool_devin_swe2():
    assert profile_pool("devin-swe2") == ("devin", None)


def test_devin_swe2_grade_exposure_and_non_aa_provenance():
    names = {grade: [p.name for p in profiles] for grade, profiles in GRADE_TABLE.items()}
    assert "devin-swe2" in names["A+"]
    assert "devin-swe2" in names["A"]
    assert "devin-swe2" in names["B"]
    assert "devin-swe2" not in names["S+"]
    assert "devin-swe2" not in names["S"]
    assert "devin-swe2" not in names["C"]

    profile = next(p for p in GRADE_TABLE["A+"] if p.name == "devin-swe2")
    assert profile.model == "SWE-2 (high)"
    assert profile.benchmark is None
    assert profile.benchmark_source is None
    assert profile.benchmark_annotation == ESTIMATED_EXTRAPOLATED_UNMEASURED_ANNOTATION
    assert profile.placement_note == DEVIN_SWE2_PLACEMENT_NOTE
    assert "A+" in profile.placement_note and "reps 3건 전" in profile.placement_note
    assert profile.estimate_reason == DEVIN_SWE2_ESTIMATE_REASON
    assert "FrontierCode 50.0" in DEVIN_SWE2_ESTIMATE_REASON
    assert "Terminal-Bench 2.1 92.8" in DEVIN_SWE2_ESTIMATE_REASON
    assert "Terminal-Bench 4 27.3" in DEVIN_SWE2_ESTIMATE_REASON
    assert "비-AA" in DEVIN_SWE2_ESTIMATE_REASON
    assert "AA-agent" in DEVIN_SWE2_ESTIMATE_REASON


def test_gate_cli_ok_on_free_fixture(monkeypatch, capsys, fixture_text):
    result = devin.parse(_fixture(fixture_text))
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: result})
    rc = cli.main(["gate", "-m", "devin-swe2", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    assert "pool=devin" in out.out
    assert "used_pct=0.0" in out.out or "used_pct=0" in out.out
    assert "class=spend" in out.out


def test_gate_cli_exit_4_when_swe2_free_removed(monkeypatch, capsys, fixture_text):
    result = devin.parse(_without_swe2_free(_fixture(fixture_text)))
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: result})
    rc = cli.main(["gate", "-m", "devin-swe2", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 4
    assert "측정 불가" in out.err


def test_list_and_argparse_include_devin_swe2(capsys):
    assert cli.main(["--list-recommend-profiles"]) == 0
    names = capsys.readouterr().out.splitlines()
    assert "devin-swe2" in names

    with pytest.raises(SystemExit) as exc:
        cli.main(["gate", "-m", "not-devin-swe2"])
    assert exc.value.code == 2
    parser = cli.build_parser(["devin"])
    gate = parser._subparsers._group_actions[0].choices["gate"]
    profile_action = next(action for action in gate._actions if action.dest == "profile")
    assert "devin-swe2" in profile_action.choices


def test_fetch_invokes_models_list(tmp_path, monkeypatch, fixture_text):
    payload = _fixture(fixture_text)
    binary = tmp_path / "fake-devin-args"
    binary.write_text(
        '#!/bin/sh\n[ "$1" = models ] && [ "$2" = list ] || exit 9\ncat <<\'EOF\'\n' + payload + "EOF\n"
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))
    result = devin.fetch()
    assert result.error is None


def test_oserror_from_probe_is_unknown(monkeypatch):
    monkeypatch.setattr(devin.shutil, "which", lambda _name: "/tmp/fake-devin")

    def _boom(*_a, **_k):
        raise OSError("boom")

    monkeypatch.setattr(devin.subprocess, "run", _boom)
    result = devin.fetch()
    assert result.error and "실행 실패" in result.error
    assert result.buckets == []


# -- 기동 배너(daily quota) 프로브 -----------------------------------------


def test_parse_banner_real_fixture_reads_daily_and_marks_weekly_unknown(fixture_text):
    """AC5 정상 케이스: 실제 캡처 픽스처로 daily=0%(100% remaining), weekly=None."""
    result = devin.parse_banner(_banner_fixture(fixture_text))

    assert result.error is None
    assert result.plan == "Pro"
    assert result.source == devin.SOURCE_BANNER
    labels = {b.label: b for b in result.buckets}
    assert labels["daily"].used_pct == 0.0
    assert labels["daily"].horizon == "now"
    assert labels["daily"].window == "1d"
    assert labels["daily"].scope.kind == "account"
    assert labels["daily"].resets_at is not None
    assert labels["weekly"].used_pct is None
    assert labels["weekly"].horizon == "week"
    assert labels["weekly"].note == devin.WEEKLY_UNKNOWN_NOTE


def test_parse_banner_format_mismatch_does_not_guess_used_pct():
    """AC5 형식 불일치 케이스: 배너는 있으나 쿼타 세그먼트가 다른 형식이면 fail-closed."""
    mutated = "v3000.10.21 · Pro · quota 100 percent (resets in 1h 41m)"

    result = devin.parse_banner(mutated)

    assert result.error is not None
    assert result.buckets == []


def test_parse_banner_first_paint_only_is_fail_closed(fixture_text):
    """AC5 배너 부재 케이스: 두 번째 페인트(쿼타 포함) 전까지 캡처된 출력은 fail-closed."""
    full = _banner_fixture(fixture_text)
    redraw_marker = "\x1b[7A"
    first_paint_only = full[: full.index(redraw_marker)]
    assert "remaining" not in first_paint_only

    result = devin.parse_banner(first_paint_only)

    assert result.error is not None
    assert result.buckets == []


def _banner_probe_script(payload: str, *, banner_line: str) -> str:
    return (
        "#!/bin/sh\n"
        'if [ "$1" = "models" ] && [ "$2" = "list" ]; then\n'
        "  cat <<'EOF'\n" + payload + "EOF\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s\\r\\n' 'v3000.10.21'\n"
        "sleep 0.05\n"
        f"printf '%s\\r\\n' '{banner_line}'\n"
    )


_REDRAW_LINE = "\x1b[7A\x1b[Jv3000.10.21 · Pro · 100% remaining (resets in 1h 41m)"


def test_fetch_probes_banner_and_appends_swe2_bucket(tmp_path, monkeypatch, fixture_text):
    payload = _fixture(fixture_text)
    binary = tmp_path / "fake-devin-banner-and-models"
    binary.write_text(_banner_probe_script(payload, banner_line=_REDRAW_LINE))
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))

    result = devin.fetch()

    assert result.error is None
    assert result.warning is None
    assert result.plan == "Pro"
    assert result.source == "cli:banner+cli:models list"
    labels = [(b.label, b.used_pct, b.horizon) for b in result.buckets]
    assert ("daily", 0.0, "now") in labels
    assert ("weekly", None, "week") in labels
    assert ("swe-2", 0.0, "week") in labels


def test_fetch_banner_failure_falls_back_to_models_list_with_warning(tmp_path, monkeypatch, fixture_text):
    """배너 실패(형식 불일치) + models list 성공 → fail-closed 로 warning + weekly-None."""
    payload = _fixture(fixture_text)
    binary = tmp_path / "fake-devin-no-banner"
    binary.write_text("#!/bin/sh\n" + "cat <<'EOF'\n" + payload + "EOF\n")
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))

    result = devin.fetch()

    assert result.error is None
    assert result.warning is not None
    assert result.source == "cli:models list"
    labels = [(b.label, b.used_pct, b.horizon) for b in result.buckets]
    assert ("swe-2", 0.0, "week") in labels
    assert ("weekly", None, "week") in labels


def test_probe_banner_sets_pty_winsize_and_columns_lines_env(tmp_path, monkeypatch):
    binary = tmp_path / "fake-devin-winsize"
    binary.write_text(
        "#!/bin/sh\n"
        'printf \'LINES=%s COLUMNS=%s TERM=%s\\r\\n\' "$LINES" "$COLUMNS" "$TERM"\n'
        "printf '%s\\r\\n' 'v3000.10.21'\n"
        "sleep 0.05\n"
        f"printf '%s\\r\\n' '{_REDRAW_LINE}'\n"
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))
    ioctl_calls = []
    original_ioctl = devin.fcntl.ioctl

    def recording_ioctl(fd, request, argument):
        ioctl_calls.append((fd, request, argument))
        return original_ioctl(fd, request, argument)

    monkeypatch.setattr(devin.fcntl, "ioctl", recording_ioctl)

    output = devin._probe_banner()
    result = devin.parse_banner(output)

    assert result.error is None
    assert result.buckets[0].used_pct == 0.0
    assert ioctl_calls
    assert ioctl_calls[0][1] == devin.termios.TIOCSWINSZ
    assert devin.struct.unpack("HHHH", ioctl_calls[0][2]) == (50, 200, 0, 0)
    assert "LINES=50" in output
    assert "COLUMNS=200" in output
    assert "TERM=xterm-256color" in output


def test_banner_probe_timeout_is_reported_as_error(tmp_path, monkeypatch):
    binary = tmp_path / "fake-devin-hang"
    binary.write_text("#!/bin/sh\nsleep 2\n")
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))
    monkeypatch.setattr(devin, "TIMEOUT_S", 0.1)

    result = devin.fetch()

    assert result.error and "안에" in result.error
    assert result.buckets == []


def test_child_env_sets_term_and_pty_dimensions(monkeypatch):
    monkeypatch.setenv("HERDR_PANE_ID", "pane-1")
    child_env = devin._child_env()
    assert "HERDR_PANE_ID" not in child_env
    assert child_env["COLUMNS"] == "200"
    assert child_env["LINES"] == "50"
    assert child_env["TERM"] == "xterm-256color"
