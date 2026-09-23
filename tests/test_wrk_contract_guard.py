"""ROB-591 / AC3+AC6: bidirectional drift guard, scopefuel side.

Asserts the REAL ``GRADE_TABLE`` against a checked-in snapshot of the
agent-skills ``bin/wrk`` launcher's model-ID contract (``resolve_profile()``
Sol/Luna/Grok/Opus rows, ROB-591 catalog-refresh). This is a plain snapshot,
not a live cross-repo read — agent-skills has its own mirror-image guard
(``tests/test-model-contract-guard.sh``) asserting the real ``bin/wrk``
argv against a snapshot of this same GRADE_TABLE.

Update discipline: whenever ``bin/wrk``'s Sol/Luna/Opus/Grok launcher model
IDs change, update the ``WRK_*`` constants below in the same commit/PR — this
guard's whole purpose is to fail loudly when the two repos drift apart, so it
must never be "fixed" by relaxing an assertion without a matching
agent-skills-side change.
"""

from __future__ import annotations

from scopefuel.recommend import CONSULT_ONLY_PROFILES, GRADE_TABLE

# --- checked-in bin/wrk catalog contract (counterpart: agent-skills repo,
#     bin/wrk resolve_profile(), ROB-591 rows) -------------------------------
# ROB-591 scope note: the refresh is Codex Sol (codex-sol) and the four scored
# Luna efforts (max/xhigh/high/medium) only. kiro-sol and the C-grade Luna low
# row are explicitly OUT of scope and must stay on the pre-refresh 5.6 IDs —
# see test_wrk_contract_kiro_sol_and_luna_low_stay_on_5_6_baseline below.
WRK_SOL_MODEL_ID = "gpt-6-sol"  # codex-sol (max, xhigh)
WRK_LUNA_MODEL_ID = "gpt-6-luna"  # codex-luna: max/xhigh/high/medium only, not low
WRK_GROK_MODEL_ID = "grok-4.7"  # generic grok fallback argv
# The launcher passes the Claude Code CLI *alias* "opus" (--model opus), not
# this literal ID — recorded here only so a reader can find both halves of
# the mapping; bin/wrk's own contract fixture documents the alias side.
WRK_OPUS_MODEL_ID = "claude-opus-5-5"
WRK_ROLLBACK_SOL_MODEL_ID = "gpt-5.6-sol"  # codex-sol56 AND the out-of-scope kiro-sol
WRK_ROLLBACK_LUNA_MODEL_ID = "gpt-5.6-luna"  # codex-luna56 AND the out-of-scope C-grade low

_GROK_NAMES = frozenset({"grok", "grok-hi"})
_OPUS_LAUNCHER_NAME = "opus"  # not kiro-opus, which stays on the old claude-opus-5 ID


def _all_profiles():
    return [profile for profiles in GRADE_TABLE.values() for profile in profiles]


def test_wrk_contract_sol_rows_match_gpt_6_sol():
    """Detects reverting the codex-sol GRADE_TABLE rows back to gpt-5.6-sol.

    Scoped to codex-sol only (name == "codex-sol") — kiro-sol shares the Sol
    family but is explicitly out of this refresh's scope and is asserted
    separately below, so a kiro-sol-only regression can never mask a real
    codex-sol regression (or vice versa) under one assertion.
    """
    sol_rows = [p for p in _all_profiles() if p.name == "codex-sol"]
    assert sol_rows, "no codex-sol rows found in GRADE_TABLE"
    assert len(sol_rows) == 2, f"expected codex-sol max + xhigh rows, found {len(sol_rows)}"
    for profile in sol_rows:
        assert profile.aa_agent_model_id == WRK_SOL_MODEL_ID, (
            f"{profile.name} --effort {profile.launcher_effort or '-'}: "
            f"aa_agent_model_id={profile.aa_agent_model_id!r}, expected {WRK_SOL_MODEL_ID!r} "
            "(bin/wrk's four Sol profiles all run this ID — see tests/test-model-contract-guard.sh "
            "in the agent-skills repo)"
        )
        assert profile.aa_model_id == WRK_SOL_MODEL_ID, (
            f"{profile.name} --effort {profile.launcher_effort or '-'}: "
            f"aa_model_id={profile.aa_model_id!r}, expected {WRK_SOL_MODEL_ID!r}"
        )


def test_wrk_contract_luna_rows_match_gpt_6_luna():
    """Detects reverting any of the four scored Luna GRADE_TABLE rows
    (max/xhigh/high/medium) back to gpt-5.6-luna.

    Scoped by effort, not just name=="codex-luna" — the C-grade low row shares
    that name but is explicitly out of scope (benchmark=None, asserted
    separately below on the 5.6 baseline instead).
    """
    luna_rows = [
        p
        for p in _all_profiles()
        if p.name in ("codex-luna", "codex-luna-max") and p.launcher_effort != "low"
    ]
    assert luna_rows, "no scored Luna rows found in GRADE_TABLE"
    assert len(luna_rows) == 4, f"expected 4 scored Luna rows (max/xhigh/high/medium), found {len(luna_rows)}"
    for profile in luna_rows:
        assert profile.aa_agent_model_id == WRK_LUNA_MODEL_ID, (
            f"{profile.name} --effort {profile.launcher_effort or '-'}: "
            f"aa_agent_model_id={profile.aa_agent_model_id!r}, expected {WRK_LUNA_MODEL_ID!r} "
            "(bin/wrk's three Luna profiles all run this ID)"
        )


def test_wrk_contract_kiro_sol_and_luna_low_stay_on_5_6_baseline():
    """ROB-591 scope boundary: kiro-sol and the C-grade codex-luna low row are
    NOT part of this refresh (bin/wrk never routes either through a gpt-6-*
    ID) — they must still carry the pre-refresh 5.6 IDs."""
    kiro_sol = next(p for p in _all_profiles() if p.name == "kiro-sol")
    assert kiro_sol.aa_agent_model_id == WRK_ROLLBACK_SOL_MODEL_ID, (
        f"kiro-sol: aa_agent_model_id={kiro_sol.aa_agent_model_id!r}, expected "
        f"{WRK_ROLLBACK_SOL_MODEL_ID!r} — kiro-sol is out of the ROB-591 refresh scope"
    )

    luna_low = next(p for p in _all_profiles() if p.name == "codex-luna" and p.launcher_effort == "low")
    assert luna_low.aa_agent_model_id == WRK_ROLLBACK_LUNA_MODEL_ID, (
        f"codex-luna --effort low: aa_agent_model_id={luna_low.aa_agent_model_id!r}, expected "
        f"{WRK_ROLLBACK_LUNA_MODEL_ID!r} — this row is out of the ROB-591 refresh scope"
    )


def test_wrk_contract_grok_rows_match_grok_4_7():
    """Detects reverting the Grok family back to grok-4.6/grok-4-6."""
    grok_rows = [p for p in _all_profiles() if p.name in _GROK_NAMES]
    assert grok_rows, "no Grok-family rows found in GRADE_TABLE"
    for profile in grok_rows:
        assert profile.aa_agent_model_id == WRK_GROK_MODEL_ID, (
            f"{profile.name} --effort {profile.launcher_effort or '-'}: "
            f"aa_agent_model_id={profile.aa_agent_model_id!r}, expected {WRK_GROK_MODEL_ID!r} "
            "(bin/wrk's generic grok fallback argv runs this ID)"
        )


def test_wrk_contract_opus_launcher_rows_match_claude_opus_5_5():
    """The "opus" launcher-name rows (not kiro-opus) map to the new Opus 5.5 ID."""
    opus_rows = [p for p in _all_profiles() if p.name == _OPUS_LAUNCHER_NAME]
    assert opus_rows, "no 'opus' launcher rows found in GRADE_TABLE"
    for profile in opus_rows:
        assert profile.aa_agent_model_id == WRK_OPUS_MODEL_ID, (
            f"opus --effort {profile.launcher_effort or '-'}: "
            f"aa_agent_model_id={profile.aa_agent_model_id!r}, expected {WRK_OPUS_MODEL_ID!r} "
            "(bin/wrk emits the CLI alias --model opus, which this ID is behind)"
        )


def test_wrk_contract_fable_is_consult_only_not_a_grade_table_entry():
    """ROB-591: fable is bin/wrk's explicit-consult-only profile (claude-fable-5-1
    argv) — it must never be a GRADE_TABLE recommendation candidate."""
    assert not any(p.name == "fable" for p in _all_profiles())
    assert "fable" in CONSULT_ONLY_PROFILES
