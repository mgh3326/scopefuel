"""#593 (was ROB-591): cross-repo drift guard, scopefuel side.

#591 guarded a *duplicated model table*: bin/wrk carried its own Sol/Luna/Grok
model IDs and this file asserted GRADE_TABLE against a checked-in snapshot of
them. #593 removes the duplication — wrk now reads model ids and default efforts
from ``scopefuel policy launch`` — so guarding those literals would guard values
bin/wrk no longer holds.

The guard's target moves to what the new arrangement can actually break:

1. **Every launcher spelling that consumes the catalog resolves** — a spelling
   mapped to a profile the catalog does not carry would make wrk fall back to a
   value it no longer has, silently, on every spawn.
2. **The catalog-exempt spellings stay exempt** — the rollback pins exist to hold
   a superseded model; if they ever followed the canon the rollback lever would
   do nothing.
3. **The snapshot still answers with the values bin/wrk used to hardcode** — the
   migration must be value-preserving on day one.

agent-skills has the mirror-image guard (``tests/test-model-contract-guard.sh``)
asserting the real ``bin/wrk`` argv against this same contract. Update both in
one PR: the whole point is to fail loudly when the two repos drift.
"""

from __future__ import annotations

import pytest

from scopefuel import launch
from scopefuel.recommend import CONSULT_ONLY_PROFILES, GRADE_TABLE

# --- checked-in bin/wrk contract -------------------------------------------
# Launcher spelling -> (catalog profile, pinned effort or None). Mirrors
# ``resolve_catalog_profile()`` in the agent-skills repo's bin/wrk.
WRK_CATALOG_SPELLINGS: dict[str, tuple[str, str | None]] = {
    "opus": ("opus", None),
    "builder-opus": ("opus", None),
    "captain-opus": ("opus", None),
    "sonnet": ("sonnet", None),
    "sonnet-med": ("sonnet", None),
    "haiku": ("haiku", None),
    "fable": ("fable", None),
    "codex": ("codex-sol", "high"),
    "codex-sol": ("codex-sol", None),
    "codex-max": ("codex-sol", None),
    "builder-sol": ("codex-sol", None),
    "captain-sol": ("codex-sol", None),
    "codex-terra": ("codex-terra", "medium"),
    "codex-med": ("codex-terra", "medium"),
    "codex-terra-max": ("codex-terra-max", None),
    "codex-luna": ("codex-luna", "medium"),
    "codex-luna-hi": ("codex-luna", "high"),
    "codex-luna-max": ("codex-luna-max", None),
    "codex-astra": ("codex-astra", None),
    "kiro-opus": ("kiro-opus", None),
    "kiro-opus-xhigh": ("kiro-opus", "xhigh"),
    "kiro-opus-max": ("kiro-opus", "max"),
    "kiro-sonnet": ("kiro-sonnet", None),
    "kiro-sol": ("kiro-sol", None),
    "kiro-sol-xhigh": ("kiro-sol", "xhigh"),
    "kiro-sol-max": ("kiro-sol", "max"),
    "kiro-cheap": ("kiro-cheap", None),
    "kiro-haiku": ("kiro-haiku", None),
    "grok": ("grok-hi", None),
    "grok-hi": ("grok-hi", None),
    "grok-med": ("grok", "medium"),
    "builder-grok": ("grok-hi", "xhigh"),
    "cc-qwen38": ("cc-qwen38", None),
    "cc-glm": ("cc-glm", None),
}

# Spellings bin/wrk deliberately keeps literal. Each needs a reason, because an
# entry added here silently removes a profile from the canon's reach.
WRK_CATALOG_EXEMPT: dict[str, str] = {
    "codex-sol56": "rollback pin to the pre-refresh gpt-5.6-sol",
    "codex-luna56": "rollback pin to the pre-refresh gpt-5.6-luna",
    "grok45": "rollback pin to grok-4.5",
    "grok45-med": "rollback pin to grok-4.5",
    "grok46": "rollback pin to grok-4.6",
    "grok46-med": "rollback pin to grok-4.6",
    "agy-flash36": "rollback pin to gemini-3.6-flash-high",
    "agy-flash37": "explicit synonym pin to gemini-3.7-flash-high",
    "agy-flash": "the agy CLI encodes the effort into the model name, not a model id",
    "kiro": "no model argument — the Kiro default model",
    "kiro-luna": "ungraded experiment; not a catalog profile",
    "kiro-glm": "ungraded experiment; not a catalog profile",
    "kiro-deepseek": "ungraded experiment; not a catalog profile",
    "kiro-minimax": "ungraded experiment; not a catalog profile",
    "kiro-minimax21": "ungraded experiment; not a catalog profile",
    "kimi-k3": "the kimi CLI takes neither a model id from us nor --effort",
    "kimi-k27": "ditto",
    "kimi-k27-code": "ditto",
    "kimi-k3-low": "differentiated by KIMI_CODE_HOME, not by a model argument",
    "builder-kimi": "ditto",
    "devin-swe2": "fixed --model swe-2 argv; no effort flag",
    "builder-devin": "ditto",
    "devin-glm52": "fixed argv",
    "devin-swe17": "fixed argv",
    "devin-ds41": "fixed argv",
    "cc-dsflash": "experimental alias; the CLI is given the alias, not a model id",
    "cc-dspro": "ditto",
    "cc-glm53": "ditto",
    "oc-kimi-code": "opencode provider-prefixed slug, not a catalog model id",
    "oc-glm": "ditto",
    "oc-kimi-k3": "ditto",
    "oc-dsflash": "ditto",
    "oc-gflash": "ditto",
    "oc-sonnet46": "ditto",
    "oc-oss": "ditto",
    "oc-omni": "ditto",
    "oc-qwen37-max": "ditto",
    "oc-minimax-m3": "ditto",
    "oc-solar4": "ditto",
}

# The values bin/wrk hardcoded before #593. The migration must reproduce them
# exactly on day one — a drift here is a profile silently re-pointed.
WRK_PRE_593_RESOLUTION: dict[str, tuple[str, str]] = {
    "codex": ("gpt-6-sol", "high"),
    "codex-sol": ("gpt-6-sol", "max"),
    "codex-max": ("gpt-6-sol", "max"),
    "codex-terra": ("gpt-5.6-terra", "medium"),
    "codex-terra-max": ("gpt-5.6-terra", "max"),
    "codex-luna": ("gpt-6-luna", "medium"),
    "codex-luna-hi": ("gpt-6-luna", "high"),
    "codex-luna-max": ("gpt-6-luna", "max"),
    "codex-astra": ("gpt-6-astra", "xhigh"),  # #527: xhigh, NOT the old max
    "kiro-opus": ("claude-opus-5", "xhigh"),
    "kiro-sonnet": ("claude-sonnet-5", "high"),
    "kiro-sol": ("gpt-5.6-sol", "high"),
    "kiro-cheap": ("qwen3-coder-next", "medium"),
    "kiro-haiku": ("claude-haiku-4.5", "high"),
    "grok": ("grok-4.7", "high"),
    "grok-hi": ("grok-4.7", "high"),
    "grok-med": ("grok-4.7", "medium"),
    "opus": ("claude-opus-5-5", "high"),
    # bin/wrk emits the Claude CLI alias for these ("--model sonnet"), so the
    # model id below is the catalog's identity rather than the launcher's argv —
    # the catalog route requires a non-blank model_id on every row, and these
    # spellings are catalog-exempt on the wrk side (see WRK_CATALOG_EXEMPT).
    "sonnet": ("claude-sonnet-5", "high"),
    "haiku": ("claude-haiku-4.5", "low"),
    "fable": ("claude-fable-5-1", ""),
}

_OPERATOR_REQUESTED = {"fable", "codex-astra"}


def _all_profiles():
    return [profile for profiles in GRADE_TABLE.values() for profile in profiles]


@pytest.mark.parametrize("spelling", sorted(WRK_CATALOG_SPELLINGS))
def test_every_catalog_consuming_wrk_spelling_resolves(spelling):
    """ "wrk's spellings ⊆ catalog profiles" — the #591 guard's replacement target."""

    profile, pinned = WRK_CATALOG_SPELLINGS[spelling]
    decision = launch.resolve_launch(
        profile,
        effort=pinned,
        operator_request=profile in _OPERATOR_REQUESTED,
    )
    assert decision.profile
    if pinned:
        assert decision.effort == pinned


@pytest.mark.parametrize("spelling", sorted(WRK_PRE_593_RESOLUTION))
def test_the_migration_reproduces_the_pre_593_launcher_values(spelling):
    profile, pinned = WRK_CATALOG_SPELLINGS[spelling]
    model_id, effort = WRK_PRE_593_RESOLUTION[spelling]
    decision = launch.resolve_launch(
        profile,
        effort=pinned,
        operator_request=profile in _OPERATOR_REQUESTED,
    )
    assert decision.model_id == model_id, f"{spelling}: model id drifted from bin/wrk"
    assert decision.effort == effort, f"{spelling}: default effort drifted from bin/wrk"


def test_exempt_and_catalog_spellings_do_not_overlap():
    overlap = set(WRK_CATALOG_SPELLINGS) & set(WRK_CATALOG_EXEMPT)
    assert not overlap, f"a spelling cannot be both catalog-driven and exempt: {sorted(overlap)}"


def test_every_exemption_carries_a_reason():
    assert all(reason.strip() for reason in WRK_CATALOG_EXEMPT.values())


def test_fable_is_consult_only_not_a_grade_table_entry():
    """Unchanged from #591: fable is launchable on an explicit operator request,
    never a recommendation candidate."""

    assert not any(p.name == "fable" for p in _all_profiles())
    assert "fable" in CONSULT_ONLY_PROFILES
    with pytest.raises(launch.LaunchError):
        launch.resolve_launch("fable")


def test_sol_profiles_stay_s_plus_in_the_snapshot():
    for entry in launch.snapshot_entries():
        if entry.profile in ("codex-sol", "kiro-sol"):
            assert entry.grade == "S+", f"{entry.profile} left S+ in the bundled snapshot"
