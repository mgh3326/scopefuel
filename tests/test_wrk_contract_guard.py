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
WRK_SOL_MODEL_ID = "gpt-6-sol"  # codex, codex-sol, codex-max, builder-sol/captain-sol
WRK_LUNA_MODEL_ID = "gpt-6-luna"  # codex-luna, codex-luna-hi, codex-luna-max
WRK_GROK_MODEL_ID = "grok-4.7"  # generic grok fallback argv
# The launcher passes the Claude Code CLI *alias* "opus" (--model opus), not
# this literal ID — recorded here only so a reader can find both halves of
# the mapping; bin/wrk's own contract fixture documents the alias side.
WRK_OPUS_MODEL_ID = "claude-opus-5-5"
WRK_ROLLBACK_SOL_MODEL_ID = "gpt-5.6-sol"  # codex-sol56
WRK_ROLLBACK_LUNA_MODEL_ID = "gpt-5.6-luna"  # codex-luna56

_SOL_NAMES = frozenset({"codex-sol", "kiro-sol"})
_LUNA_NAMES = frozenset({"codex-luna", "codex-luna-max"})
_GROK_NAMES = frozenset({"grok", "grok-hi"})
_OPUS_LAUNCHER_NAME = "opus"  # not kiro-opus, which stays on the old claude-opus-5 ID


def _all_profiles():
    return [profile for profiles in GRADE_TABLE.values() for profile in profiles]


def test_wrk_contract_sol_rows_match_gpt_6_sol():
    """Detects reverting any Sol GRADE_TABLE row back to gpt-5.6-sol."""
    sol_rows = [p for p in _all_profiles() if p.name in _SOL_NAMES]
    assert sol_rows, "no Sol-family rows found in GRADE_TABLE"
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
    """Detects reverting any Luna GRADE_TABLE row back to gpt-5.6-luna."""
    luna_rows = [p for p in _all_profiles() if p.name in _LUNA_NAMES]
    assert luna_rows, "no Luna-family rows found in GRADE_TABLE"
    for profile in luna_rows:
        assert profile.aa_agent_model_id == WRK_LUNA_MODEL_ID, (
            f"{profile.name} --effort {profile.launcher_effort or '-'}: "
            f"aa_agent_model_id={profile.aa_agent_model_id!r}, expected {WRK_LUNA_MODEL_ID!r} "
            "(bin/wrk's three Luna profiles all run this ID)"
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
