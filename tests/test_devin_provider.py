"""Devin SWE-2 Free-only provider and profile placement."""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scopefuel import cli, launch, proctrack, render
from scopefuel import recommend as recommend_mod
from scopefuel.providers import BUILTIN, devin
from scopefuel.recommend import (
    DEVIN_DS41_GRADE_ANNOTATION,
    DEVIN_SWE2_ESTIMATE_REASON,
    DEVIN_SWE2_PLACEMENT_NOTE,
    ESTIMATED_EXTRAPOLATED_UNMEASURED_ANNOTATION,
    GRADE_TABLE,
    UNMEASURED_ANNOTATION,
    profile_pool,
    recommend,
)

FREE_NOTE = "free until ~2026-10-10"
NEW_DEVIN_PROFILES = ("devin-glm52", "devin-swe17", "devin-ds41")
UNMEASURED_DEVIN_PROFILES = ("devin-glm52", "devin-swe17")


def _fixture(fixture_text) -> str:
    return fixture_text("devin_models_list")


def _banner_fixture(fixture_text) -> str:
    return fixture_text("devin_banner")


def _new_banner_fixture(fixture_text) -> str:
    return fixture_text("devin_banner_3000_11_1")


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


def test_parse_swe2_free_fixture_is_model_scoped_zero(fixture_text):
    result = devin.parse(_fixture(fixture_text))
    assert result.error is None
    assert result.id == "devin"
    assert result.pool_class == "spend"
    assert result.source == "cli:models list"
    assert result.note == FREE_NOTE
    assert len(result.buckets) == 1
    bucket = result.buckets[0]
    assert bucket.used_pct == 0.0
    # Free 태그는 SWE-2 패밀리 한정 — account 로 두면 30d 창이 '월' 쿼타 축으로
    # 렌더돼 /usage 에 없는 창이 생긴다(1381 desk 의 '월 0%' 오독).
    assert bucket.scope.kind == "model"
    assert bucket.scope.label == "swe-2"
    assert bucket.note == FREE_NOTE
    assert bucket.label == "swe-2"
    # N1: 쿼타 창이 아니므로 '30d'/'month' 를 싣지 않는다 — 표에 'month swe-2'
    # 행이 나오면 desk 의 '월 0%' 오독이 재현된다.
    assert bucket.window != "30d"
    assert bucket.horizon != "month"


def test_swe2_model_tag_does_not_render_a_month_axis(fixture_text):
    """N1: model-scope swe-2 Free 태그 행은 표에서 'month' 축으로 그려지지 않는다."""
    result = devin.parse(_fixture(fixture_text))
    out = render.table([result], color=False)
    assert "month" not in out
    assert "swe-2" in out


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


def _measured_devin(fixture_text):
    """#1381: models list 는 쿼타를 안 주므로 측정된 devin 풀은 PTY 세션 파서로 만든다."""
    return devin.parse_session(fixture_text("devin_usage"))


def test_gate_cli_ok_on_measured_quota_fixture(monkeypatch, capsys, fixture_text):
    """S6: PTY /usage 측정이 있으면 Free 태그와 함께 gate 가 열린다(복원)."""
    result = _measured_devin(fixture_text)
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: result})
    rc = cli.main(["gate", "-m", "devin-swe2", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 0
    assert "pool=devin" in out.out
    assert "class=spend" in out.out


def test_gate_cli_fail_closed_on_free_fixture_without_quota(monkeypatch, capsys, fixture_text):
    """모델 Free 태그만으로는 계정 쿼타를 증명할 수 없다 — 게이트는 fail-closed."""
    result = devin.parse(_fixture(fixture_text))
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: result})
    rc = cli.main(["gate", "-m", "devin-swe2", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 4
    assert "측정 불가" in out.err


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


def test_oserror_from_probe_is_unknown(tmp_path, monkeypatch):
    """An executable whose interpreter is missing fails execve → OSError.

    Patching devin.shutil.which/devin.subprocess.* would patch the shared
    modules globally and also neuter proctrack's own lsof lookup.
    """
    binary = tmp_path / "fake-devin-badexec"
    binary.write_text("#!/nonexistent/interpreter\n")
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))

    result = devin.fetch()
    assert result.error and "실행 실패" in result.error
    assert result.buckets == []


# -- PTY 세션(기동 배너 + /usage) 프로브 -------------------------------------
#
# 실측(2026-10-10, v3000.11.3): 배너 ``Pro · N% remaining`` 은 /usage Weekly 축이다
# (배너 리셋 1d 2h — daily 창은 24h 안에 리셋되므로 daily 일 수 없다; 같은 회차
# /usage Weekly 사용률 = 100-N). 옛 코드는 이 줄을 daily 로 잘못 붙였다.


def test_parse_session_banner_fixture_reads_weekly_and_marks_daily_unknown(fixture_text):
    """배너만 읽힌 세션: 배너 퍼센트는 weekly 잔여이고 daily 는 미측정(None)."""
    result = devin.parse_session(_banner_fixture(fixture_text))

    assert result.error is None
    assert result.plan == "Pro"
    assert result.source == devin.SOURCE_QUOTA
    labels = {b.label: b for b in result.buckets}
    assert labels["weekly"].used_pct == 0.0  # 100% remaining
    assert labels["weekly"].horizon == "week"
    assert labels["weekly"].window == "7d"
    assert labels["weekly"].scope.kind == "account"
    assert labels["weekly"].resets_at is not None
    assert labels["daily"].used_pct is None
    assert labels["daily"].horizon == "now"


def test_parse_session_observed_3000_11_1_fixture_reads_weekly(fixture_text):
    result = devin.parse_session(_new_banner_fixture(fixture_text))

    assert result.error is None
    assert result.plan == "Pro"
    labels = {b.label: b for b in result.buckets}
    assert labels["weekly"].used_pct == 8.0  # 92% remaining → used 8%
    assert labels["weekly"].resets_at is not None
    assert labels["daily"].used_pct is None


def test_parse_session_usage_fixture_reads_both_axes(fixture_text):
    """AC2: 정제된 /usage 캡처 픽스처에서 daily·weekly 사용률과 리셋을 읽는다."""
    before = dt.datetime.now(dt.UTC)
    result = devin.parse_session(fixture_text("devin_usage"))

    assert result.error is None
    assert result.plan == "Pro"
    labels = {b.label: b for b in result.buckets}
    assert labels["daily"].used_pct == 0.0
    # Daily: ``resets in 2h 7m`` — 상대 기간이라 파싱 시각 기준으로 검사한다.
    daily_reset = dt.datetime.fromisoformat(labels["daily"].resets_at)
    assert (
        before + dt.timedelta(hours=2, minutes=6)
        <= daily_reset
        <= dt.datetime.now(dt.UTC) + dt.timedelta(hours=2, minutes=8)
    )
    assert labels["weekly"].used_pct == 18.0
    # Weekly: ``resets Oct 11, 5:00 PM (UTC+9)`` — 절대 시각 그대로.
    assert labels["weekly"].resets_at == "2026-10-11T17:00:00+09:00"


def _rendered_windows(result: devin.ProviderResult, now: dt.datetime | None = None) -> str:
    """desk 가 본 추천 사용률 문자열 — '일 18% / 월 0%' 와 같은 형태."""
    moment = now or dt.datetime.now(dt.UTC)
    matches = recommend_mod._matching_buckets(result, None)
    states = recommend_mod._window_states(matches, moment)
    constraint = recommend_mod._select_constraint(states)
    return recommend_mod._format_windows_display(states, constraint)


def test_parse_session_desk_case_renders_il_and_ju_not_wol(fixture_text):
    """AC2: desk 재현 — 배너 82% remaining + /usage Daily 0% Weekly 18% → 일 0% 주 18%.

    옛 매핑(배너=daily, swe-2 Free=account 30d)이면 '일 18% · 월 0%' 가 나온다.
    """
    result = devin.parse_session(fixture_text("devin_usage"))
    assert result.error is None

    rendered = _rendered_windows(result)
    assert rendered == "일 0% · 주 18% · 제약=일"
    assert "월" not in rendered


def test_parse_session_rejects_unrelated_partial_unknown_and_out_of_range_text(fixture_text):
    full = _new_banner_fixture(fixture_text)
    partial = full[: full.index("Pro · 92% remaining")]
    cases = [
        partial,
        "status: 92% remaining (resets in 5d 5h)",
        "Team · 92% remaining (resets in 5d 5h)",
        "Pro · 101% remaining (resets in 5d 5h)",
        "Pro · 92% remaining (resets in someday)",
    ]

    for text in cases:
        result = devin.parse_session(text)
        assert result.error is not None, text
        assert result.buckets == [], text


def test_parse_session_format_mismatch_does_not_guess_used_pct():
    """배너는 있으나 쿼타 세그먼트가 다른 형식이면 fail-closed."""
    mutated = "v3000.10.31 · Pro · quota 100 percent (resets in 1h 41m)"

    result = devin.parse_session(mutated)

    assert result.error is not None
    assert result.buckets == []


def test_parse_session_first_paint_only_is_fail_closed(fixture_text):
    """두 번째 페인트(쿼타 포함) 전까지 캡처된 출력은 fail-closed."""
    full = _banner_fixture(fixture_text)
    redraw_marker = "\x1b[7A"
    first_paint_only = full[: full.index(redraw_marker)]
    assert "remaining" not in first_paint_only

    result = devin.parse_session(first_paint_only)

    assert result.error is not None
    assert result.buckets == []


def test_parse_session_usage_wins_over_banner_on_same_window():
    """같은 창에서 배너와 /usage 가 어긋나면 /usage 가 이기고 note 에 남는다."""
    text = (
        "v3000.11.3\r\n"
        "Pro · 82% remaining (resets in 1d 2h)\r\n"
        "Daily 0% used (resets in 3h 12m)\r\n"
        "Weekly 40% used (resets in 1d 2h)\r\n"
    )

    result = devin.parse_session(text)

    assert result.error is None
    labels = {b.label: b for b in result.buckets}
    assert labels["weekly"].used_pct == 40.0
    assert "/usage 우선" in (result.note or "")


# -- S1: /usage 행 파서는 줄 단위다 — 아래 case 는 전부 None 또는 참값만 준다 --


@pytest.mark.parametrize(
    ("line", "axis"),
    [
        (" Daily 1,000% used", "daily"),  # 천단위 구분 — '000' 을 0.0 으로 읽으면 안 된다
        (" Daily -5% used", "daily"),  # 부호가 떨어져 5.0 이 되면 안 된다
        (" Daily 0% remaining", "daily"),  # 'used' 키워드 없이 remaining 은 사용률이 아니다
        (" Weekly 18% remaining", "weekly"),
        (" Weekly 230% used", "weekly"),  # 범위 밖
        (" Weekly (n/a)", "weekly"),  # 퍼센트 없음
    ],
)
def test_usage_row_malformed_percent_never_becomes_used(line, axis):
    """S1 섹션 D: 깨진 퍼센트·remaining 문구는 그 축을 만들지 않는다(None, 추정 금지)."""
    axes = devin._usage_axes(devin._clean(line))

    assert axis not in axes


def test_usage_repaint_or_other_line_never_feeds_a_row():
    """S1 섹션 D: 상태줄 리페인트·다른 줄의 퍼센트는 행의 축에 새어 들어오지 않는다."""
    text = (
        "Pro · 82% remaining (resets in 1d 2h)\r\n"
        " Daily 0% used  · resets in 2h 7m\r\n"
        " Weekly (n/a)\r\n"
        "Context 7% full\r\n"
        "Pro · 82% remaining (resets in 1d 2h)\r\n"  # 상태줄 리페인트
    )

    result = devin.parse_session(text)

    assert result.error is None
    labels = {b.label: b for b in result.buckets}
    assert labels["daily"].used_pct == 0.0
    # /usage Weekly 는 못 읽었으므로 배너가 증명한 참값(18%)로 둔다 —
    # 리페인트의 82% remaining 을 'used' 로 읽거나 '/usage 와 불일치' 노트를
    # 남겨서는 안 된다.
    assert labels["weekly"].used_pct == 18.0
    assert "불일치" not in (result.note or "")


def test_usage_stray_label_word_does_not_block_the_real_row():
    """S1 섹션 D: 'weekly' 가 들어간 안내 줄은 행이 아니다 — 진짜 행이 이긴다."""
    text = (
        "weekly counters refresh on Mondays\r\n"
        " Daily 0% used  · resets in 2h 7m\r\n"
        " Weekly 18% used  · resets Oct 11, 5:00 PM (UTC+9)\r\n"
    )

    result = devin.parse_session(text)

    assert result.error is None
    labels = {b.label: b for b in result.buckets}
    assert labels["daily"].used_pct == 0.0
    assert labels["weekly"].used_pct == 18.0


def test_usage_daily_row_without_used_stays_none_even_with_banner():
    """Daily 행에 % used 가 없으면 daily 는 None — 배너 퍼센트로 채우지 않는다."""
    text = (
        "Pro · 82% remaining (resets in 1d 2h)\r\n"
        " Daily (n/a)\r\n"
        " Weekly 18% used  · resets Oct 11, 5:00 PM (UTC+9)\r\n"
    )

    result = devin.parse_session(text)

    assert result.error is None
    labels = {b.label: b for b in result.buckets}
    assert labels["daily"].used_pct is None
    assert labels["weekly"].used_pct == 18.0


# -- S4: 연도 없는 절대 리셋은 라벨 tz 에서 year±1 후보를 비교한다 --------------


def test_usage_absolute_reset_dec31_to_jan1_is_next_year():
    """Dec 31 에 `resets Jan 1` 은 작년(364일 전)이 아니라 내년이다."""
    row = " Weekly 18% used  · resets Jan 1, 5:00 PM (UTC+9)"
    now = dt.datetime(2026, 12, 31, 12, 0, tzinfo=dt.UTC)
    assert devin._segment_reset_iso(row, now=now) == "2027-01-01T17:00:00+09:00"


def test_usage_absolute_reset_dec28_to_jan3_is_next_year():
    """Dec 28 에 `resets Jan 3` 도 마찬가지로 내년 후보가 이긴다."""
    row = " Weekly 18% used  · resets Jan 3, 5:00 PM (UTC+9)"
    now = dt.datetime(2026, 12, 28, 12, 0, tzinfo=dt.UTC)
    assert devin._segment_reset_iso(row, now=now) == "2027-01-03T17:00:00+09:00"


def test_usage_absolute_reset_same_year_when_reset_is_upcoming():
    """연중 평범한 경우는 올해 후보가 이긴다 (Oct 10 → Oct 11)."""
    row = " Weekly 18% used  · resets Oct 11, 5:00 PM (UTC+9)"
    now = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC)
    assert devin._segment_reset_iso(row, now=now) == "2026-10-11T17:00:00+09:00"


def _pty_tui_script(
    *,
    models_payload: str | None = None,
    banner_text: str | None = None,
    banner_fixture: str | None = None,
    usage_lines: list[str] | None = None,
    input_log: Path | None = None,
    answer_usage: bool = True,
) -> str:
    """가짜 devin TUI: 상태줄을 그리고 /usage·/exit 입력 두 개만 받는다."""
    lines = ["#!/bin/sh"]
    if models_payload is not None:
        lines += [
            'if [ "$1" = "models" ] && [ "$2" = "list" ]; then',
            "  cat <<'EOF'\n" + models_payload + "EOF\n",
            "  exit 0",
            "fi",
        ]
    if banner_text is not None:
        lines.append(f"printf '%s\\r\\n' '{banner_text}'")
    elif banner_fixture is not None:
        lines.append("cat <<'EOF'\n" + banner_fixture + "EOF\n")
    logged = f" >> {input_log}" if input_log else ""
    lines += [
        "IFS= read -r command",
        f"printf '%s\\n' \"$command\"{logged}",
        # 프로브가 ?1004h 를 본 뒤 보내는 FocusGained(ESC[I)가 같은 줄에 붙을
        # 수 있으므로 prefix 가 아니라 부분 문자열로 잡는다.
        'case "$command" in *usage*) ;; *) exit 9;; esac',
    ]
    if answer_usage:
        for line in usage_lines or []:
            lines.append(f"printf '%s\\r\\n' '{line}'")
    lines += [
        "IFS= read -r command",
        f"printf '%s\\n' \"$command\"{logged}",
        'case "$command" in *exit*) ;; *) exit 9;; esac',
        "exit 0",
    ]
    return "\n".join(lines) + "\n"


_BANNER_82 = "Pro · 82% remaining (resets in 1d 2h)"
# 실측 캡처 형식(v3000.11.3): Daily 는 상대 기간, Weekly 는 절대 시각+TZ 라벨.
_USAGE_DESK = [
    " Daily   ■■■■■■■■■■■■■■■■■■■■  0% used  · resets in 2h 7m",
    " Weekly  ■■■■■■■■■■■■■■■■■■■■  18% used  · resets Oct 11, 5:00 PM (UTC+9)",
]

_STUB_TUI = Path(__file__).with_name("devin_stub_tui.py")


def _stub_tui_binary(
    tmp_path: Path, monkeypatch, *, mode: str = "normal", models_payload: str | None = None
) -> Path:
    """TUI-faithful stub (devin_stub_tui.py): raw stdin reads.

    슬래시 팔레트는 텍스트+Enter 가 한 write 로 붙어 오면 삼키고, require_focus
    모드는 ESC[I(FocusGained) 전 입력을 무시한다 — 텍스트/Enter 분리 전송과
    포커스 응답이 없으면 프로브가 멈추는 실제 TUI 동작을 그대로 흉내낸다.
    모든 read 는 타임스탬프와 함께 STUB_LOG 에 남는다.
    """
    target = tmp_path / "fake-devin-tui"
    shutil.copy(_STUB_TUI, target)
    target.chmod(0o755)
    log = tmp_path / "stub-input.log"
    monkeypatch.setenv("STUB_MODE", mode)
    monkeypatch.setenv("STUB_LOG", str(log))
    if models_payload is not None:
        models = tmp_path / "models.txt"
        models.write_text(models_payload)
        monkeypatch.setenv("STUB_MODELS", str(models))
    monkeypatch.setattr(devin, "BINARY", str(target))
    return log


def _stub_reads(log: Path) -> list[bytes]:
    """스텁이 stdin 에서 읽은 바이트 청크들(시간순)."""
    chunks: list[bytes] = []
    for line in log.read_text().splitlines():
        if " read " in line:
            chunks.append(ast.literal_eval(line.split(" read ", 1)[1]))
    return chunks


def _stub_lines(log: Path, tag: str) -> list[str]:
    return [line for line in log.read_text().splitlines() if f" {tag}" in line]


def test_fetch_probes_session_and_appends_swe2_bucket(tmp_path, monkeypatch, fixture_text):
    _stub_tui_binary(tmp_path, monkeypatch, models_payload=_fixture(fixture_text))

    result = devin.fetch()

    assert result.error is None
    assert result.warning is None
    assert result.plan == "Pro"
    assert result.source == "cli:banner+/usage+cli:models list"
    labels = [(b.label, b.used_pct, b.horizon) for b in result.buckets]
    assert ("daily", 0.0, "now") in labels
    assert ("weekly", 18.0, "week") in labels
    assert ("swe-2", 0.0, "now") in labels


def test_fetch_probes_observed_3000_11_1_fixture_and_appends_swe2_bucket(tmp_path, monkeypatch, fixture_text):
    payload = _fixture(fixture_text)
    banner = _new_banner_fixture(fixture_text)
    binary = tmp_path / "fake-devin-new-banner-and-models"
    binary.write_text(
        _pty_tui_script(
            models_payload=payload,
            banner_fixture=banner,
            usage_lines=_USAGE_DESK,
        )
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))

    result = devin.fetch()

    assert result.error is None
    assert result.warning is None
    assert result.plan == "Pro"
    assert result.source == "cli:banner+/usage+cli:models list"
    labels = {(b.label, b.used_pct, b.horizon) for b in result.buckets}
    assert ("daily", 0.0, "now") in labels
    assert ("weekly", 18.0, "week") in labels
    assert ("swe-2", 0.0, "now") in labels


def test_fetch_session_failure_falls_back_to_models_list_with_warning(tmp_path, monkeypatch, fixture_text):
    """PTY 세션 실패(형식 불일치) + models list 성공 → fail-closed warning + None 축."""
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
    assert ("swe-2", 0.0, "now") in labels
    assert ("daily", None, "now") in labels
    assert ("weekly", None, "week") in labels


def test_probe_session_sends_usage_and_exit_and_reads_both(tmp_path, monkeypatch, fixture_text):
    """AC3: 한 PTY 세션이 정확히 /usage·/exit 만 보내고, winsize 를 걸고, 자식을 정리한다."""
    workdir = tmp_path / "probe-workdir"
    log = _stub_tui_binary(tmp_path, monkeypatch, models_payload=_fixture(fixture_text))
    monkeypatch.setattr(devin, "PROBE_WORKDIR", workdir)
    ioctl_calls = []
    original_ioctl = devin.fcntl.ioctl

    def recording_ioctl(fd, request, argument):
        ioctl_calls.append((fd, request, argument))
        return original_ioctl(fd, request, argument)

    monkeypatch.setattr(devin.fcntl, "ioctl", recording_ioctl)

    result = devin.fetch()

    assert result.error is None
    labels = {b.label: b for b in result.buckets}
    assert labels["daily"].used_pct == 0.0
    assert labels["weekly"].used_pct == 18.0
    # 텍스트와 Enter 는 별도 write 다 — 한 write 로 붙여 보내는 뮤턴트는
    # 스텁 팔레트가 삼켜 /usage 가 실행되지 않는다(RED).
    assert _stub_reads(log) == [b"/usage", b"\r", b"/exit", b"\r"]
    assert ioctl_calls
    assert ioctl_calls[0][1] == devin.termios.TIOCSWINSZ
    assert devin.struct.unpack("HHHH", ioctl_calls[0][2]) == (50, 200, 0, 0)
    assert proctrack.pids_with_cwd(workdir, nested=True) == []


def test_probe_usage_unanswered_keeps_banner_axis_and_daily_none(tmp_path, monkeypatch, fixture_text):
    """AC3 fail-closed: /usage 미응답 → 배너가 증명한 weekly 만 쓰고 daily 는 None."""
    log = _stub_tui_binary(
        tmp_path, monkeypatch, mode="no_usage_answer", models_payload=_fixture(fixture_text)
    )
    monkeypatch.setattr(devin, "USAGE_WAIT_S", 0.5)
    monkeypatch.setattr(devin, "EXIT_WAIT_S", 0.5)

    result = devin.fetch()

    assert result.error is None
    labels = {b.label: b for b in result.buckets}
    assert labels["weekly"].used_pct == 18.0  # 배너 82% remaining → used 18
    assert labels["daily"].used_pct is None
    assert "daily 미측정" in (result.note or "")
    assert _stub_reads(log) == [b"/usage", b"\r", b"/exit", b"\r"]


def test_probe_sends_no_input_when_no_status_line_or_input_marker(tmp_path, monkeypatch, fixture_text):
    """S2: 상태줄도 입력창 ❯ 마커도 없으면 어떤 입력도 치지 않는다(blind input 금지).

    로그인·업데이트 프롬프트에 Enter 가 들어가는 것을 막는다 — 기다리다가
    deadline 에 fail-closed 로 끝난다.
    """
    log = _stub_tui_binary(tmp_path, monkeypatch, mode="no_banner", models_payload=_fixture(fixture_text))
    monkeypatch.setattr(devin, "TIMEOUT_S", 1.0)

    result = devin.fetch()

    assert result.error is None
    assert result.warning is not None  # 쿼타 프로브 fail-closed → models 만
    labels = {b.label: b for b in result.buckets}
    assert labels["daily"].used_pct is None
    assert labels["weekly"].used_pct is None
    assert _stub_reads(log) == []


def test_probe_sends_usage_after_input_box_marker_without_banner(tmp_path, monkeypatch, fixture_text):
    """S2: 상태줄은 안 보여도 입력창 ❯ 마커가 확인되면 /usage 를 보낸다."""
    log = _stub_tui_binary(tmp_path, monkeypatch, mode="prompt_only", models_payload=_fixture(fixture_text))

    result = devin.fetch()

    assert result.error is None
    labels = {b.label: b for b in result.buckets}
    assert labels["daily"].used_pct == 0.0
    assert labels["weekly"].used_pct == 18.0
    assert "배너" in (result.note or "")
    assert _stub_reads(log) == [b"/usage", b"\r", b"/exit", b"\r"]


def test_probe_answers_focus_reporting_before_typing(tmp_path, monkeypatch, fixture_text):
    """?1004h 가 켜진 TUI 에는 FocusGained(ESC[I)를 먼저 한 번 보낸다."""
    log = _stub_tui_binary(tmp_path, monkeypatch, mode="focus_adv", models_payload=_fixture(fixture_text))

    result = devin.fetch()

    assert result.error is None
    labels = {b.label: b for b in result.buckets}
    assert labels["daily"].used_pct == 0.0
    reads = _stub_reads(log)
    assert reads[0] == b"\x1b[I"
    assert reads[1:] == [b"/usage", b"\r", b"/exit", b"\r"]


def test_probe_types_after_focus_gained_when_tui_gates_input(tmp_path, monkeypatch, fixture_text):
    """require_focus 모드 — FocusGained 없이 온 입력은 TUI 가 무시한다.

    프로브가 ESC[I 를 안 보내는 뮤턴트는 여기서 RED 가 된다(입력 무시 →
    /usage 미응답 → daily None).
    """
    log = _stub_tui_binary(tmp_path, monkeypatch, mode="require_focus", models_payload=_fixture(fixture_text))

    result = devin.fetch()

    assert result.error is None
    labels = {b.label: b for b in result.buckets}
    assert labels["daily"].used_pct == 0.0
    assert labels["weekly"].used_pct == 18.0
    assert _stub_lines(log, "ignored-no-focus") == []


def test_probe_out_of_range_percentages_are_unreadable_not_guessed(tmp_path, monkeypatch, fixture_text):
    """AC3: 범위 밖 퍼센트는 읽히지 않은 것으로 둔다 — 0/100 추정 금지."""
    _stub_tui_binary(tmp_path, monkeypatch, mode="malformed", models_payload=_fixture(fixture_text))
    monkeypatch.setattr(devin, "USAGE_WAIT_S", 0.5)
    monkeypatch.setattr(devin, "EXIT_WAIT_S", 0.5)

    result = devin.fetch()

    assert result.error is None
    labels = {b.label: b for b in result.buckets}
    assert labels["daily"].used_pct is None
    # 범위 밖 /usage Weekly 는 무시하고 배너가 증명한 값을 쓴다.
    assert labels["weekly"].used_pct == 18.0


def test_probe_session_raises_timeout_expired_on_silent_tui(tmp_path, monkeypatch):
    """S3: 침묵 TUI 는 입력을 한 번도 치지 않고 TimeoutExpired 분기로 끝난다."""
    _stub_tui_binary(tmp_path, monkeypatch, mode="hang")
    monkeypatch.setattr(devin, "TIMEOUT_S", 0.5)

    with pytest.raises(subprocess.TimeoutExpired):
        devin._probe_session()


def test_probe_exit_ignored_still_cleans_up_grandchild(tmp_path, monkeypatch, fixture_text):
    """/exit 을 무시하고 손자를 남긴 TUI 도 프로브가 자기 인스턴스를 쓴다."""
    workdir = tmp_path / "probe-workdir"
    log = _stub_tui_binary(tmp_path, monkeypatch, mode="ignore_exit", models_payload=_fixture(fixture_text))
    monkeypatch.setattr(devin, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(devin, "EXIT_WAIT_S", 0.3)

    result = devin.fetch()

    assert result.error is None
    grandchild = [line for line in log.read_text().splitlines() if line.startswith("grandchild ")]
    assert grandchild
    pid = int(grandchild[0].split()[-1])
    try:
        assert _wait_gone([pid], timeout=10) == []
        assert proctrack.pids_with_cwd(workdir, nested=True) == []
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def test_probe_session_sets_pty_winsize_and_columns_lines_env(tmp_path, monkeypatch):
    binary = tmp_path / "fake-devin-winsize"
    binary.write_text(
        "#!/bin/sh\n"
        'printf \'LINES=%s COLUMNS=%s TERM=%s\\r\\n\' "$LINES" "$COLUMNS" "$TERM"\n'
        f"printf '%s\\r\\n' '{_BANNER_82}'\n"
        "IFS= read -r command\n"
        'case "$command" in /usage*) ;; *) exit 9;; esac\n'
        "printf '%s\\r\\n' 'Daily 0% used (resets in 3h 12m)'\n"
        "printf '%s\\r\\n' 'Weekly 18% used (resets in 1d 2h)'\n"
        "IFS= read -r command\n"
        'case "$command" in /exit*) ;; *) exit 9;; esac\n'
        "exit 0\n"
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))
    ioctl_calls = []
    original_ioctl = devin.fcntl.ioctl

    def recording_ioctl(fd, request, argument):
        ioctl_calls.append((fd, request, argument))
        return original_ioctl(fd, request, argument)

    monkeypatch.setattr(devin.fcntl, "ioctl", recording_ioctl)

    output = devin._probe_session()
    result = devin.parse_session(output)

    assert result.error is None
    labels = {b.label: b for b in result.buckets}
    assert labels["weekly"].used_pct == 18.0
    assert labels["daily"].used_pct == 0.0
    assert ioctl_calls
    assert ioctl_calls[0][1] == devin.termios.TIOCSWINSZ
    assert devin.struct.unpack("HHHH", ioctl_calls[0][2]) == (50, 200, 0, 0)
    assert "LINES=50" in output
    assert "COLUMNS=200" in output
    assert "TERM=xterm-256color" in output


def test_probe_timeout_is_reported_as_error(tmp_path, monkeypatch):
    binary = tmp_path / "fake-devin-hang"
    binary.write_text("#!/bin/sh\nsleep 2\n")
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))
    monkeypatch.setattr(devin, "TIMEOUT_S", 0.1)

    result = devin.fetch()

    assert result.error and "안에" in result.error
    assert result.buckets == []


def test_probe_timeout_is_finite():
    assert math.isfinite(devin.TIMEOUT_S)
    assert devin.TIMEOUT_S > 0


def test_unreadable_session_keeps_provider_and_gate_fail_closed(tmp_path, monkeypatch, capsys, fixture_text):
    payload = _fixture(fixture_text)
    binary = tmp_path / "fake-devin-unreadable-banner"
    binary.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = models ] && [ "$2" = list ]; then\n'
        "  cat <<'EOF'\n" + payload + "EOF\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s\\r\\n' 'status: 92% used'\n"
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))

    result = devin.fetch()

    assert result.error is None
    assert result.warning is not None
    assert not any(bucket.label == "daily" and bucket.used_pct is not None for bucket in result.buckets)
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: result})
    rc = cli.main(["gate", "-m", "devin-swe2", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 4
    assert "측정 불가" in out.err


def test_child_env_sets_term_and_pty_dimensions(monkeypatch):
    monkeypatch.setenv("HERDR_PANE_ID", "pane-1")
    child_env = devin._child_env()
    assert "HERDR_PANE_ID" not in child_env
    assert child_env["COLUMNS"] == "200"
    assert child_env["LINES"] == "50"
    assert child_env["TERM"] == "xterm-256color"


# -- 프로브 자식 수명: 고아 방지 ----------------------------------------------


def test_probe_start_sweeps_stale_workdir_leftover(tmp_path, monkeypatch, fixture_text):
    """이전에 죽은 부모가 남긴 workdir 잔존자는 다음 프로브 시작 시 선제 정리된다."""
    workdir = tmp_path / "probe-workdir"
    workdir.mkdir()
    leftover = subprocess.Popen(["sleep", "60"], cwd=workdir, start_new_session=True)
    payload = _fixture(fixture_text)
    binary = tmp_path / "fake-devin-banner-sweep"
    binary.write_text(
        _pty_tui_script(models_payload=payload, banner_text=_BANNER_82, usage_lines=_USAGE_DESK)
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))
    monkeypatch.setattr(devin, "PROBE_WORKDIR", workdir)

    result = devin.fetch()

    assert result.error is None
    assert leftover.wait(timeout=5) is not None
    assert proctrack.pids_with_cwd(workdir, nested=True) == []


def test_probe_start_sweeps_stale_instance_dir_leftover(tmp_path, monkeypatch, fixture_text):
    """주인이 죽은 인스턴스 디렉터리 안의 잔존자도 다음 프로브 시작 시 정리된다."""
    workdir = tmp_path / "probe-workdir"
    instance, fd = proctrack.new_probe_dir(workdir)
    leftover = subprocess.Popen(["sleep", "60"], cwd=instance, start_new_session=True)
    os.close(fd)  # 주인 사망 — 커널이 디렉터리 락을 푼다
    payload = _fixture(fixture_text)
    binary = tmp_path / "fake-devin-banner-stale-inst"
    binary.write_text(
        _pty_tui_script(models_payload=payload, banner_text=_BANNER_82, usage_lines=_USAGE_DESK)
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))
    monkeypatch.setattr(devin, "PROBE_WORKDIR", workdir)

    result = devin.fetch()

    assert result.error is None
    assert leftover.wait(timeout=5) is not None
    assert proctrack.pids_with_cwd(workdir, nested=True) == []


def test_sigkilled_probe_parent_leaves_no_workdir_orphans(tmp_path):
    """부모 SIGKILL → 분리 리퍼가 인스턴스 디렉터리 잔존자를 쓴다(정리 경로는 전혀 못 돈다)."""
    workdir = tmp_path / "probe-workdir"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    survivor = subprocess.Popen(["sleep", "60"], cwd=elsewhere, start_new_session=True)
    fake = tmp_path / "fake-devin-hang"
    # SIGHUP 무시 — PTY master close 의 hangup 으로는 죽지 않아야 리퍼 기여가 증명된다.
    fake.write_text("#!/bin/sh\ntrap '' HUP\nexec sleep 60\n")
    fake.chmod(fake.stat().st_mode | 0o111)
    helper = tmp_path / "probe_parent.py"
    helper.write_text(
        "from scopefuel.providers import devin\n"
        f"devin.BINARY = {str(fake)!r}\n"
        f"devin.PROBE_WORKDIR = {str(workdir)!r}\n"
        "devin._probe_session()\n"
    )
    parent = subprocess.Popen([sys.executable, str(helper)], cwd=Path.cwd())
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not proctrack.pids_with_cwd(workdir, nested=True):
            time.sleep(0.1)
        assert proctrack.pids_with_cwd(workdir, nested=True), "probe child never appeared in workdir"

        parent.send_signal(signal.SIGKILL)
        parent.wait(timeout=5)

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and proctrack.pids_with_cwd(workdir, nested=True):
            time.sleep(0.2)
        assert proctrack.pids_with_cwd(workdir, nested=True) == []
        assert survivor.poll() is None
    finally:
        for proc in (survivor, parent):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        proctrack.kill_leftovers_at_cwd(workdir, nested=True)


def _hang_probe_helper(path: Path, fake: Path, workdir: Path) -> Path:
    helper = path
    helper.write_text(
        "from scopefuel.providers import devin\n"
        f"devin.BINARY = {str(fake)!r}\n"
        f"devin.PROBE_WORKDIR = {str(workdir)!r}\n"
        "devin._probe_session()\n"
    )
    return helper


def test_concurrent_probes_never_kill_each_others_child(tmp_path):
    """같은 workdir 의 동시 프로브: 시작 스윕도, 죽은 쪽의 리퍼도 다른 쪽 자식에 닿지 않는다."""
    workdir = tmp_path / "probe-workdir"
    workdir.mkdir()
    fake = tmp_path / "fake-devin-hang"
    fake.write_text("#!/bin/sh\ntrap '' HUP\nexec sleep 60\n")
    fake.chmod(fake.stat().st_mode | 0o111)

    def spawn(tag: str) -> subprocess.Popen:
        helper = _hang_probe_helper(tmp_path / f"probe_{tag}.py", fake, workdir)
        return subprocess.Popen([sys.executable, str(helper)], cwd=Path.cwd())

    def wait_children(count: int, timeout: float = 15) -> list[int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pids = proctrack.pids_with_cwd(workdir, nested=True)
            if len(pids) >= count:
                return pids
            time.sleep(0.1)
        return proctrack.pids_with_cwd(workdir, nested=True)

    a = spawn("a")
    b = None
    try:
        kids_a = wait_children(1)
        assert len(kids_a) == 1, "probe A child never appeared"
        child_a = kids_a[0]

        b = spawn("b")
        kids_ab = wait_children(2)
        assert len(kids_ab) == 2, "probe B child never appeared"
        # B 시작 스윕이 A 의 살아있는 자식을 죽이지 않았다(고정 workdir 공유에도).
        assert child_a in kids_ab
        child_b = next(pid for pid in kids_ab if pid != child_a)

        a.send_signal(signal.SIGKILL)
        a.wait(timeout=5)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and child_a in proctrack.pids_with_cwd(workdir, nested=True):
            time.sleep(0.2)
        # A 리퍼가 자기 인스턴스만 쓸었다 — B 자식은 살아 있다.
        assert child_a not in proctrack.pids_with_cwd(workdir, nested=True)
        assert child_b in proctrack.pids_with_cwd(workdir, nested=True)
        assert b.poll() is None
    finally:
        for proc in (a, b):
            if proc is not None and proc.poll() is None:
                proc.kill()
            if proc is not None:
                proc.wait(timeout=5)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and proctrack.pids_with_cwd(workdir, nested=True):
            time.sleep(0.2)
        proctrack.kill_leftovers_at_cwd(workdir, nested=True)


# -- models list 프로브 자식 수명·잠금 (#608 — B2: subprocess.run 시대의 누수) --


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pids, timeout: float = 10.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = [pid for pid in pids if _alive(pid)]
        if not remaining:
            return []
        time.sleep(0.05)
    return [pid for pid in pids if _alive(pid)]


def _models_hanging_fake(tmp_path: Path) -> Path:
    """A devin that never returns from `models list`."""

    fake = tmp_path / "fake-devin-models-hang"
    fake.write_text("#!/bin/sh\nexec sleep 120\n")
    fake.chmod(fake.stat().st_mode | 0o111)
    return fake


def test_models_list_timeout_kills_the_child_and_its_group(tmp_path, monkeypatch):
    """subprocess.run(timeout=) killed only the direct child; the tracked
    Popen takes the whole group down with it."""

    workdir = tmp_path / "probe-workdir"
    monkeypatch.setattr(devin, "BINARY", str(_models_hanging_fake(tmp_path)))
    monkeypatch.setattr(devin, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(devin, "TIMEOUT_S", 1.0)

    result = devin._fetch_models_list()

    assert result.error and "끝나지 않음" in result.error
    assert _wait_gone(proctrack.pids_with_cwd(workdir, nested=True)) == [], (
        "a timed-out models list left a devin child running"
    )


def test_sigkilled_models_list_parent_leaves_no_orphan(tmp_path):
    """B2's exact reproduction: SIGKILL the parent mid-`models list` — only the
    detached reaper can end the child."""

    workdir = tmp_path / "probe-workdir"
    workdir.mkdir(parents=True)
    fake = _models_hanging_fake(tmp_path)

    helper = tmp_path / "models_probe_helper.py"
    helper.write_text(
        "from scopefuel.providers import devin\n"
        f"devin.BINARY = {str(fake)!r}\n"
        f"devin.PROBE_WORKDIR = {str(workdir)!r}\n"
        "devin.TIMEOUT_S = 120.0\n"
        "devin._fetch_models_list()\n"
    )
    probe = subprocess.Popen([sys.executable, str(helper)])
    try:
        deadline = time.monotonic() + 20
        children: list[int] = []
        while time.monotonic() < deadline:
            children = proctrack.pids_with_cwd(workdir, nested=True)
            if children:
                break
            time.sleep(0.1)
        assert children, "the fake devin models child never started"

        os.kill(probe.pid, signal.SIGKILL)
        probe.wait(timeout=10)

        assert _wait_gone(children, timeout=30) == [], (
            "a SIGKILLed fetch orphaned its devin models list child"
        )
    finally:
        if probe.poll() is None:
            probe.kill()
            probe.wait(timeout=5)
        for pid in proctrack.pids_with_cwd(workdir, nested=True):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def test_banner_probe_backgrounded_grandchild_does_not_survive(tmp_path, monkeypatch):
    """_probe_session's finally sweeps the instance dir too — a helper the CLI
    backgrounds before printing its quota line must not outlive the probe
    (#608 S7: the sweep mutant survived the suite without this test)."""

    workdir = tmp_path / "probe-workdir"
    pidfile = tmp_path / "grandchild.pid"
    fake = tmp_path / "fake-devin-banner-backgrounder"
    fake.write_text(
        f"#!/bin/sh\nsleep 120 &\necho $! > {pidfile}\n"
        "printf '%s\\r\\n' 'v3000.10.31'\nsleep 0.05\n"
        f"printf '%s\\r\\n' '{_BANNER_82}'\nexit 0\n"
    )
    fake.chmod(fake.stat().st_mode | 0o111)

    monkeypatch.setattr(devin, "BINARY", str(fake))
    monkeypatch.setattr(devin, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(devin, "TIMEOUT_S", 3.0)
    monkeypatch.setattr(devin, "BANNER_SETTLE_S", 0.05)

    devin._probe_session()

    pid = int(pidfile.read_text())
    try:
        assert _wait_gone([pid], timeout=10) == [], (
            "a backgrounded grandchild outlived a successful banner probe"
        )
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def test_models_list_backgrounded_grandchild_does_not_survive(tmp_path, monkeypatch):
    """A CLI that backgrounds a helper and exits 0 leaks it without the cwd sweep."""

    workdir = tmp_path / "probe-workdir"
    pidfile = tmp_path / "grandchild.pid"
    fake = tmp_path / "fake-devin-backgrounder"
    fake.write_text(f"#!/bin/sh\nsleep 120 &\necho $! > {pidfile}\nexit 0\n")
    fake.chmod(fake.stat().st_mode | 0o111)

    monkeypatch.setattr(devin, "BINARY", str(fake))
    monkeypatch.setattr(devin, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(devin, "TIMEOUT_S", 2.0)

    devin._fetch_models_list()

    pid = int(pidfile.read_text())
    try:
        assert _wait_gone([pid], timeout=10) == [], (
            "a backgrounded grandchild outlived a successful models list"
        )
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def test_a_second_fetch_is_skipped_while_one_is_running(tmp_path, monkeypatch):
    """devin had no probe lock at all — overlapping fetch() spawned one CLI
    pair per caller."""

    workdir = tmp_path / "probe-workdir"
    monkeypatch.setattr(devin, "PROBE_WORKDIR", workdir)
    monkeypatch.setattr(devin, "BINARY", str(_models_hanging_fake(tmp_path)))

    entered = []

    def _never_called(*_args):
        entered.append(True)
        raise AssertionError("the second fetch must not start a devin")

    with proctrack.single_probe_lock(workdir) as acquired:
        assert acquired is True
        monkeypatch.setattr(devin, "_quota_result", _never_called)
        monkeypatch.setattr(devin, "_fetch_models_list", _never_called)
        result = devin.fetch()

    assert entered == []
    assert result.error and "이미 실행 중" in result.error


def test_fetch_writes_one_caller_log_line(tmp_path, monkeypatch, fixture_text):
    """devin's two sub-probes are one probe attempt — one audit line."""

    workdir = tmp_path / "probe-workdir"
    payload = _fixture(fixture_text)
    binary = tmp_path / "fake-devin-banner-and-models"
    binary.write_text(
        _pty_tui_script(models_payload=payload, banner_text=_BANNER_82, usage_lines=_USAGE_DESK)
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(devin, "BINARY", str(binary))
    monkeypatch.setattr(devin, "PROBE_WORKDIR", workdir)

    result = devin.fetch()
    assert result.error is None

    lines = (workdir / "probe-calls.log").read_text().splitlines()
    assert len(lines) == 1
    assert "probe=devin" in lines[0]
    assert f"pid={os.getpid()}" in lines[0]
    assert "via=" in lines[0] and "fetch" in lines[0]


# -- task295: devin 계정 풀 공유 3종 등재 ------------------------------------


def test_task295_remaining_unmeasured_devin_profiles_stay_c_only():
    names = {grade: [p.name for p in profiles] for grade, profiles in GRADE_TABLE.items()}
    for name in UNMEASURED_DEVIN_PROFILES:
        assert name in names["C"]
        for grade in ("S+", "S", "A+", "A", "B"):
            assert name not in names[grade], (name, grade)

        profile = next(p for p in GRADE_TABLE["C"] if p.name == name)
        assert profile.benchmark is None
        assert profile.benchmark_annotation == UNMEASURED_ANNOTATION
        assert profile.estimate_reason is None
        assert profile.placement_note is None
        assert profile.aa_agent_model_id is None
        assert profile.aa_model_id is None
        assert profile.benchmark_model_id is None
        assert profile.launcher_effort is None
        assert profile.benchmark_effort is None


def test_task295_recommend_c_lists_remaining_devin_profiles_as_unmeasured(fixture_text):
    """쿼타 미측정 devin 풀은 순위가 아니라 측정 불가 제외 행으로 나온다 (fail-closed)."""
    providers = [devin.parse(_fixture(fixture_text))]
    out = recommend(providers, "C")
    ranked_names = _ranked_names(out)
    excluded = next((line for line in out.splitlines() if line.startswith("✗ devin 측정 불가")), None)
    assert excluded is not None, out
    for name in UNMEASURED_DEVIN_PROFILES:
        assert name not in ranked_names, (name, out)
        assert name in excluded, (name, out)


def _ranked_names(out: str) -> list[str]:
    # 행이 🔥 마커로 시작할 수 있어 위치가 아닌 토큰 멤버십으로 잡는다 (#1381,
    # test_devin_effort_variants 와 같은 패턴).
    tokens = {token for line in out.splitlines() if line[:1].isdigit() for token in line.split()}
    return [token for token in tokens if token.startswith("devin-")]


def test_task631_ds41_measured_grade_is_in_both_snapshot_consumers(capsys, fixture_text):
    placements = [
        grade for grade, profiles in GRADE_TABLE.items() if any(p.name == "devin-ds41" for p in profiles)
    ]
    assert placements == ["A+"]
    profile = next(p for p in GRADE_TABLE["A+"] if p.name == "devin-ds41")
    assert profile.benchmark is None  # operational reps are not an AA-agent score
    assert profile.benchmark_source is None
    assert profile.benchmark_annotation == DEVIN_DS41_GRADE_ANNOTATION
    assert "reps 3/3" in profile.benchmark_annotation
    assert "BLOCKER 1건" in profile.benchmark_annotation
    assert "hk:doc 2227" in profile.benchmark_annotation

    rows = [entry for entry in launch.snapshot_entries() if entry.profile == "devin-ds41"]
    assert len(rows) == 1
    assert rows[0].grade == "A+"
    assert rows[0].score is None
    assert rows[0].benchmark_annotation == DEVIN_DS41_GRADE_ANNOTATION
    assert rows[0].model_id == "deepseek-v4-1-flash-high"
    assert rows[0].pool == "devin"

    assert cli.main(["policy", "launch", "devin-ds41", "--json"]) == 0
    launched = json.loads(capsys.readouterr().out)
    assert launched["grade"] == "A+"
    assert launched["model_id"] == "deepseek-v4-1-flash-high"
    assert launched["catalog"]["source"] == "snapshot"

    providers = [_measured_devin(fixture_text)]
    aplus = recommend(providers, "A+")
    c_grade = recommend(providers, "C")
    assert "hk:doc 2227" in aplus
    # Match the row token, not a substring: #635's devin-ds41-max stays in C.
    assert "devin-ds41" in _ranked_names(aplus)
    assert "devin-ds41" not in _ranked_names(c_grade)


def test_task631_other_unscored_c_rows_keep_their_placements():
    expected = {"codex-luna", "kiro-cheap", "oc-omni", "devin-glm52", "devin-swe17"}
    # #635 effort variants are unmeasured C rows of their own (high rung is only
    # a reference) — except devin-swe2-medium, which #787 moved to an unscored
    # A row on the operator-approved reps measurement (hk:doc 5177 item 2).
    # devin-swe2-max's effort-less row stays C; its @max rung was #1296-promoted.
    expected |= {"devin-swe2-max", "devin-ds41-max"}
    actual = {p.name for p in GRADE_TABLE["C"] if p.benchmark is None}
    assert actual == expected
    medium = next(p for p in GRADE_TABLE["A"] if p.name == "devin-swe2-medium")
    assert medium.benchmark is None
    snapshot = {
        entry.profile: entry
        for entry in launch.snapshot_entries()
        if entry.profile in expected
        and (entry.effort == "low" if entry.profile == "codex-luna" else entry.effort == "")
    }
    assert set(snapshot) == expected
    assert all(entry.grade == "C" and entry.score is None for entry in snapshot.values())
    assert snapshot["oc-omni"].gate == "escalation"


def test_task295_profile_pool_shares_devin_pool():
    for name in NEW_DEVIN_PROFILES:
        assert profile_pool(name) == ("devin", None)


def test_task295_gate_cli_ok_on_new_devin_profiles(monkeypatch, capsys, fixture_text):
    """S6: PTY /usage 측정이 있으면 새 devin 프로필 게이트도 열린다(복원)."""
    result = _measured_devin(fixture_text)
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: result})

    parser = cli.build_parser(["devin"])
    gate = parser._subparsers._group_actions[0].choices["gate"]
    profile_action = next(action for action in gate._actions if action.dest == "profile")
    for name in NEW_DEVIN_PROFILES:
        assert name in profile_action.choices

        rc = cli.main(["gate", "-m", name, "--no-cache"])
        out = capsys.readouterr()
        assert rc == 0, (name, out)
        assert "pool=devin" in out.out, (name, out)


def test_task295_gate_cli_fail_closed_on_new_devin_profiles_without_quota(monkeypatch, capsys, fixture_text):
    """models list 만으로는 쿼타 미측정 — 새 devin 프로필 게이트도 fail-closed."""
    result = devin.parse(_fixture(fixture_text))
    monkeypatch.setattr(cli, "registry", lambda: {"devin": lambda: result})

    parser = cli.build_parser(["devin"])
    gate = parser._subparsers._group_actions[0].choices["gate"]
    profile_action = next(action for action in gate._actions if action.dest == "profile")
    for name in NEW_DEVIN_PROFILES:
        assert name in profile_action.choices

        rc = cli.main(["gate", "-m", name, "--no-cache"])
        out = capsys.readouterr()
        assert rc == 4, (name, out)
        assert "측정 불가" in out.err, (name, out)


def test_task295_list_recommend_profiles_include_new_devin_profiles(capsys):
    assert cli.main(["--list-recommend-profiles"]) == 0
    names = capsys.readouterr().out.splitlines()
    for name in NEW_DEVIN_PROFILES:
        assert name in names
