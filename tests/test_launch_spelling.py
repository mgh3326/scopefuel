"""#1331: --recommend prints the wrk-accepted launch spelling for devin rows.

bin/wrk rejects ``--effort`` for devin profiles — ``EFFORT_SUPPORTED=0``,
because the rung is baked into the profile/model id — so a line that printed
``devin-swe2 --effort high`` handed the reader a spelling that fails at spawn
(rc 2, "--effort is unsupported for profile 'devin-swe2'").

The text surface now prints the profile name wrk accepts, with the rung kept
as a non-command ``(effort <rung>)`` annotation; the JSON surface keeps every
field and gains ``launch``, the exact ``wrk spawn -m`` fragment. Rows of
launchers that take the flag are unchanged byte for byte.
"""

from __future__ import annotations

import datetime as dt

from scopefuel.launch import DEFAULT_LAUNCH_EFFORTS, launch_spelling
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    GRADE_TABLE,
    _launch_spelling,
    _text_label,
    recommend,
    recommend_dict,
)

TODAY = dt.date(2026, 10, 9)
NOW = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.UTC)


def _provider(provider_id: str) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class="preserve",
        buckets=[
            Bucket(
                label="7d",
                window="7d",
                used_pct=10.0,
                resets_at=(NOW + dt.timedelta(days=6)).isoformat(),
                scope=Scope("account"),
                horizon="week",  # type: ignore[arg-type]
            )
        ],
    )


PROVIDERS = [_provider(pid) for pid in ("devin", "claude", "codex", "grok", "kimi")]


def _ranked(output: str) -> list[str]:
    return [line for line in output.splitlines() if line[:1].isdigit()]


# --- the defect rows ---------------------------------------------------------


def test_devin_rung_rows_print_the_profile_only_spelling():
    out = recommend(PROVIDERS, "A+", today=TODAY, now=NOW)
    ranked = _ranked(out)
    swe2 = [line for line in ranked if line.split()[1] == "devin-swe2"]
    swe2_max = [line for line in ranked if line.split()[1] == "devin-swe2-max"]
    assert any("(effort high)" in line for line in swe2)
    assert any("(effort max)" in line for line in swe2_max)
    for line in swe2 + swe2_max:
        assert "--effort" not in line, line


def test_no_devin_line_anywhere_carries_the_effort_flag():
    """A mutant re-printing ``--effort`` on a devin row goes red at any grade."""
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        out = recommend(PROVIDERS, grade, today=TODAY, now=NOW)
        for line in _ranked(out):
            # Position-proof: a devin row and the flag may never co-occur.
            assert not ("devin-" in line and "--effort" in line), (grade, line)


def test_the_rung_stays_visible_as_a_non_command_annotation():
    """The grade context is not lost: the rung prints, just not as a flag."""
    out = recommend(PROVIDERS, "A+", today=TODAY, now=NOW)
    assert "devin-swe2 (effort high)" in out
    assert "devin-swe2-max (effort max)" in out


# --- launch spelling helper --------------------------------------------------


def test_launch_spelling_is_the_fragment_wrk_accepts():
    """The fragment is name-only on no-flag launchers, ``--effort`` on the rest."""
    by_rung = {
        (profile.name, profile.launcher_effort): profile
        for profiles in GRADE_TABLE.values()
        for profile in profiles
        if profile.launcher_effort
    }
    assert _launch_spelling(by_rung[("devin-swe2", "high")]) == "devin-swe2"
    assert _launch_spelling(by_rung[("devin-swe2-max", "max")]) == "devin-swe2-max"
    # Flag-taking launchers keep the --effort fragment.
    assert _launch_spelling(by_rung[("sonnet", "high")]) == "sonnet --effort high"
    assert _launch_spelling(by_rung[("grok-hi", "xhigh")]) == "grok-hi --effort xhigh"
    # A row with no rung prints the bare profile either way.
    bare = next(p for p in GRADE_TABLE["A+"] if p.name == "devin-swe2" and not p.launcher_effort)
    assert _launch_spelling(bare) == "devin-swe2"


def test_every_rung_row_on_a_no_flag_launcher_prints_the_bare_profile():
    """The predicate is launch.py's effort-flag table, not a name prefix:
    any future profile whose launcher drops the flag gets the same treatment."""
    assert launch_spelling("devin-swe2", "high") == "devin-swe2"
    assert launch_spelling("made-up-no-flag", "max") == "made-up-no-flag"
    assert "made-up-no-flag" not in DEFAULT_LAUNCH_EFFORTS
    # And every GRADE_TABLE rung row on a no-flag launcher loses the flag.
    for profiles in GRADE_TABLE.values():
        for profile in profiles:
            if profile.launcher_effort and profile.name not in DEFAULT_LAUNCH_EFFORTS:
                assert _launch_spelling(profile) == profile.name
                assert "(effort" in _text_label(profile)


def test_flag_launcher_text_labels_unchanged():
    """Other launchers' rows are byte-for-byte the old ``name --effort rung``."""
    out = recommend(PROVIDERS, "A", today=TODAY, now=NOW)
    assert any("grok-hi --effort xhigh" in line for line in _ranked(out))
    out_b = recommend(PROVIDERS, "B", today=TODAY, now=NOW)
    assert any("sonnet --effort high" in line for line in _ranked(out_b))


# --- JSON surface ------------------------------------------------------------


def test_json_rows_gain_the_launch_spelling_field():
    payload = recommend_dict(PROVIDERS, "A+", today=TODAY, now=NOW)
    seen = {(row["profile"], row["display"]): row["launch"] for row in payload["rows"]}
    assert seen[("devin-swe2", "devin-swe2 --effort high")] == "devin-swe2"
    assert seen[("devin-swe2-max", "devin-swe2-max --effort max")] == "devin-swe2-max"
    assert seen[("devin-swe2", "devin-swe2")] == "devin-swe2"
    # display is untouched — the old fields keep their values.
    for row in payload["rows"]:
        assert "launch" in row
