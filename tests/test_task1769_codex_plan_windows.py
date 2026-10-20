"""hk 1769 (b953) — codex required windows follow the stored plan_type.

2026-10-20 wham usage API fleet evidence (hk 1767 report): plus returns
{5h, 7d}, prolite returns {7d} only, free returns a single 30-day window
(the "30d" band in model.window_label, ~2592000 s). A plan in
manual.CODEX_PLAN_REQUIRED_WINDOWS is strict — a required window with no
valid bucket is unmeasurable (gate rc 4, named reason), never
admit-on-measured. Absent/unlisted plans keep the {5h, 7d} floor with the
미측정 tag byte-identically, plus one warning so the table gets extended.

Mutant invariants:
- a known plan never admits when one of its required windows is missing;
- a known plan never requires a window it does not have;
- an unknown plan's decision never differs from base.
"""

from __future__ import annotations

import datetime as dt

from scopefuel import cli, manual
from scopefuel.model import Bucket, ProviderResult, Scope, window_label
from scopefuel.recommend import gate_check, recommend, recommend_dict

TODAY = dt.date(2026, 7, 31)
NOW = dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=dt.UTC)

BASE_TAGGED_REASON = "codex-max pool=codex 사용 10% class=preserve [필수 창 미측정: 5h — 측정된 창으로 판정]"


def _reset_almost_full(window: str) -> str:
    hours = {"5h": 4.9, "1d": 23.5, "7d": 167.0, "30d": 719.0}.get(window, 167.0)
    return (NOW + dt.timedelta(hours=hours)).isoformat()


def _codex(plan: str | None, *buckets: tuple[str, float]) -> ProviderResult:
    return ProviderResult(
        id="codex",
        plan=plan,
        pool_class="preserve",
        buckets=[
            Bucket(
                label=window,
                window=window,
                used_pct=used,
                resets_at=_reset_almost_full(window),
                scope=Scope("account"),
                horizon="now" if window == "5h" else "week",
            )
            for window, used in buckets
        ],
    )


def _gate_cli(monkeypatch, capsys, result: ProviderResult) -> tuple[int, str, str]:
    monkeypatch.setattr(cli, "registry", lambda: {"codex": lambda: result})
    rc = cli.main(["gate", "-m", "codex-max", "--no-cache"])
    out = capsys.readouterr()
    return rc, out.out, out.err


def _ordinary_codex_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["provider"] == "codex" and row["kind"] != "escalation"]


# ---------------------------------------------------------------- window_label


def test_window_label_30d_band():
    """The 30d band covers the ~2592000 s window the free plan returns."""
    assert window_label(2592000) == "30d"
    assert window_label(2591999) == "30d"  # 2026-10-20 evidence: ±1 s still 30d
    assert window_label(2591941) == "30d"
    # Outside the ±60 s band the generic hour form is kept — nothing else moves.
    assert window_label(2592070) == "720.019h"
    assert window_label(18000) == "5h"
    assert window_label(86400) == "1d"
    assert window_label(604800) == "7d"


# ------------------------------------------------------------------ plus


def test_plus_missing_5h_is_unmeasurable_with_named_reason():
    """plus requires {5h, 7d}: a 5h window the API did not return is rc 4.

    Base admitted on the 7d percent with the 미측정 tag; a listed plan never
    admits on the remaining windows.
    """
    result = gate_check([_codex("plus", ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is True
    assert result.missing_windows == ("5h",)
    assert "codex_required_window_missing:5h" in result.reason
    assert "plan=plus" in result.reason


def test_plus_missing_5h_cli_exit_4(monkeypatch, capsys):
    providers_result = _codex("plus", ("7d", 10.0))
    monkeypatch.setattr(cli, "registry", lambda: {"codex": lambda: providers_result})
    rc = cli.main(["gate", "-m", "codex-max", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 4
    assert "codex_required_window_missing:5h" in out.err
    assert "plan=plus" in out.err


def test_plus_5h_at_100_still_denies():
    """A measured window over the cutoff denies exactly as base (rc 3)."""
    result = gate_check([_codex("plus", ("5h", 100.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is False
    assert "소진" in result.reason
    assert "codex_required_window_missing" not in result.reason


def test_plus_full_coverage_admits_without_tag():
    result = gate_check([_codex("plus", ("5h", 10.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.missing_windows == ()
    assert "미측정" not in result.reason


# ----------------------------------------------------------------- prolite


def test_prolite_7d_only_admits_without_tag():
    """prolite has no 5h window at all — requiring it hid every gate behind a tag."""
    result = gate_check([_codex("prolite", ("7d", 64.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.missing_windows == ()
    assert "미측정" not in result.reason


def test_prolite_missing_7d_is_unmeasurable():
    """prolite requires {7d}: a 5h-only reading does not satisfy it."""
    result = gate_check([_codex("prolite", ("5h", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is True
    assert result.missing_windows == ("7d",)
    assert "codex_required_window_missing:7d" in result.reason
    assert "plan=prolite" in result.reason


# -------------------------------------------------------------------- free


def test_free_30d_admits_without_tag():
    """free's only window is the 30-day one — no 5h/7d floor, no tag."""
    result = gate_check([_codex("free", ("30d", 74.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.missing_windows == ()
    assert "미측정" not in result.reason


def test_free_missing_30d_is_unmeasurable():
    """A free reading without its 30-day window is not admissible on 5h+7d."""
    result = gate_check([_codex("free", ("5h", 10.0), ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is True
    assert result.missing_windows == ("30d",)
    assert "codex_required_window_missing:30d" in result.reason
    assert "plan=free" in result.reason


# ------------------------------------------------------ unknown / absent plan


def test_plan_none_output_is_base_byte_for_byte():
    result = gate_check([_codex(None, ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.missing_windows == ("5h",)
    assert result.reason == BASE_TAGGED_REASON


def test_plan_pro_output_is_base_byte_for_byte():
    result = gate_check([_codex("pro", ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.missing_windows == ("5h",)
    assert result.reason == BASE_TAGGED_REASON


def test_plan_team_output_is_base_byte_for_byte():
    result = gate_check([_codex("team", ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.missing_windows == ("5h",)
    assert result.reason == BASE_TAGGED_REASON


def test_unknown_plan_trailing_warning_names_the_plan(monkeypatch, capsys):
    """The warning prints after the decision line — stdout stays base."""
    rc, out, err = _gate_cli(monkeypatch, capsys, _codex("pro", ("7d", 10.0)))
    assert rc == 0
    assert "profile=codex-max pool=codex used_pct=10.0 class=preserve" in out.splitlines()[0]
    assert out.splitlines()[1] == BASE_TAGGED_REASON
    warnings = [line for line in err.splitlines() if "plan_type" in line]
    assert len(warnings) == 1
    assert warnings[0].startswith("warning: codex plan_type 'pro'")
    assert "CODEX_PLAN_REQUIRED_WINDOWS" in warnings[0]


def test_absent_plan_on_succeeded_reading_warns(monkeypatch, capsys):
    rc, out, err = _gate_cli(monkeypatch, capsys, _codex(None, ("7d", 10.0)))
    assert rc == 0
    warnings = [line for line in err.splitlines() if "plan_type" in line]
    assert len(warnings) == 1
    assert "absent" in warnings[0]


def test_no_warning_on_provider_error(monkeypatch, capsys):
    """Error and no-bucket paths keep base output exactly — no warning."""
    rc, out, err = _gate_cli(monkeypatch, capsys, ProviderResult(id="codex", plan=None, error="HTTP 503"))
    assert rc == 4
    assert "plan_type" not in err


def test_no_warning_on_no_bucket_path(monkeypatch, capsys):
    rc, out, err = _gate_cli(monkeypatch, capsys, _codex("pro"))
    assert rc == 4
    assert "plan_type" not in err


def test_deny_stderr_line1_is_base_reason(monkeypatch, capsys):
    """On deny paths the reason keeps stderr line 1; the warning trails."""
    rc, out, err = _gate_cli(monkeypatch, capsys, _codex("pro", ("5h", 100.0)))
    assert rc == 3
    lines = err.splitlines()
    assert lines[0] == "100% 사용 · 0% 남음 · 차단선 90% — 소진 (class=preserve)"
    assert "plan_type" not in lines[0]
    assert any(line.startswith("warning: codex plan_type 'pro'") for line in lines[1:])


def test_strict_refusal_precedes_cutoff():
    """F2 pin: missing required window on a listed plan is unmeasurable (rc4)
    even when a measured window is over the cutoff — exhaustion is not
    reported on an incomplete measurement."""
    result = gate_check([_codex("plus", ("5h", 100.0))], "codex-max", today=TODAY, now=NOW)
    assert result.unmeasurable is True
    assert "codex_required_window_missing:7d" in result.reason
    assert "소진" not in result.reason


def test_strict_reason_exact_text():
    result = gate_check([_codex("plus", ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert result.reason == ("codex_required_window_missing:5h plan=plus [필수 창 미측정: 5h]")


def test_d3_profile_not_in_grade_table_is_strict():
    """The D3 path (profile outside GRADE_TABLE) applies the same floor."""
    result = gate_check([_codex("plus", ("7d", 10.0))], "claudex", today=TODAY, now=NOW)
    assert result.provider_id == "codex"
    assert result.unmeasurable is True
    assert "codex_required_window_missing:5h" in result.reason


def test_plan_table_never_moves_other_providers():
    """A plan value must not change claude/kimi floors (mutant M21)."""
    assert manual.required_windows_for(ProviderResult(id="claude", plan="free")) == frozenset({"5h", "7d"})
    assert manual.required_windows_for(ProviderResult(id="kimi", plan="prolite")) == frozenset({"5h", "7d"})


def test_plan_comparison_is_normalized_equality_not_substring():
    """'Plus' normalises onto the plus row; 'prol' and 'teamplus' do not."""
    strict = gate_check([_codex(" Plus ", ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
    assert strict.unmeasurable is True
    assert "plan=plus" in strict.reason

    for plan in ("prol", "teamplus", "plusx"):
        base = gate_check([_codex(plan, ("7d", 10.0))], "codex-max", today=TODAY, now=NOW)
        assert base.ok is True
        assert base.reason == BASE_TAGGED_REASON


def test_manual_fallback_follows_the_stored_plan():
    """mutant M09 pin: manual coverage is plan-aware on resolve_result."""
    measured = NOW - dt.timedelta(minutes=2)
    automatic = _codex("free")
    automatic.error = "network unreachable"
    automatic.error_kind = "transport"
    manual.record_observation(
        pool="codex",
        used_pct=74.0,
        window="30d",
        measured_at=measured,
        reason="hk1769 free fallback coverage",
    )
    resolution = manual.resolve_result(automatic, now=NOW)
    assert resolution.applied is True

    automatic2 = _codex("plus")
    automatic2.error = "network unreachable"
    automatic2.error_kind = "transport"
    resolution2 = manual.resolve_result(automatic2, now=NOW)
    assert resolution2.applied is False
    assert "5h" in (resolution2.summary or {}).get("missing_windows", [])


# ------------------------------------------------------------ recommend agrees


def _recommend_codex_rows(providers: list[ProviderResult]) -> list[dict]:
    rows = recommend_dict(providers, "S+", today=TODAY, now=NOW)["rows"]
    return _ordinary_codex_rows(rows)


def test_recommend_excludes_plus_missing_5h_as_unmeasurable():
    rows = _recommend_codex_rows([_codex("plus", ("7d", 10.0))])
    assert rows
    assert all(row["kind"] == "unmeasurable" for row in rows)
    assert all("codex_required_window_missing:5h" in row["reason"] for row in rows)


def test_recommend_includes_plus_full_coverage():
    rows = _recommend_codex_rows([_codex("plus", ("5h", 10.0), ("7d", 10.0))])
    assert rows
    assert all(row["kind"] == "candidate" for row in rows)


def test_recommend_includes_prolite_7d_only():
    rows = _recommend_codex_rows([_codex("prolite", ("7d", 64.0))])
    assert rows
    assert all(row["kind"] == "candidate" for row in rows)


def test_recommend_excludes_prolite_missing_7d():
    rows = _recommend_codex_rows([_codex("prolite", ("5h", 10.0))])
    assert rows
    assert all(row["kind"] == "unmeasurable" for row in rows)
    assert all("codex_required_window_missing:7d" in row["reason"] for row in rows)


def test_recommend_includes_free_30d():
    rows = _recommend_codex_rows([_codex("free", ("30d", 74.0))])
    assert rows
    assert all(row["kind"] == "candidate" for row in rows)


def test_recommend_unknown_plan_stays_admitted_on_measured():
    """Base admitted unknown plans on measured windows — recommend keeps that."""
    for plan in (None, "pro"):
        rows = _recommend_codex_rows([_codex(plan, ("7d", 10.0))])
        assert rows
        assert all(row["kind"] == "candidate" for row in rows)


def test_recommend_plus_5h_at_100_stays_exhausted():
    rows = _recommend_codex_rows([_codex("plus", ("5h", 100.0), ("7d", 10.0))])
    assert rows
    assert all(row["kind"] == "exhausted" for row in rows)


def test_recommend_escalation_entry_names_the_strict_refusal():
    """The codex-sol@xhigh escalation line reports the missing window too."""
    out = recommend([_codex("plus", ("7d", 10.0))], "S+", today=TODAY, now=NOW)
    escalation_section = out.split("승급 후보", 1)[1]
    assert "codex_required_window_missing:5h" in escalation_section
