"""#1620 (operator decision hk:doc owner/fleet/decision/2026-10-18/devin-argon-trial,
phase 1, approved by operator-desk 10-18 r1018b953go): the paid Devin Argon
trial lane — spawn plumbing only.

Three catalog rows join the devin pool — ``devin-argon`` (launch id
``gemini-4-argon-high``), ``devin-argon-medium`` (``gemini-4-argon-medium``)
and ``devin-argon-low`` (``gemini-4-argon-low``), each $1 / 1M input on the
10-18 ``devin models list``. They serve Gemini models under the hood, so the
tester-separation provider family is ``google`` (never anthropic, never the
devin-swe2 family) while the quota pool stays devin (the Devin Pro account
Daily/Weekly axes) and billing is paid.

The operator placed NO grade: the spellings are gate-able catalog rows
(``launch.UNGRADED_TRIAL_SNAPSHOT``, inert grade C) that never enter
``GRADE_TABLE`` — ``--recommend`` offers none at any grade — and ``gate -m``
judges each on the account Daily/Weekly axis (the D3 not-in-table path),
never on a SWE-2 Free model-scope bucket. Worker/tester-only on the wrk
side; no _BUILDER_RUNGS entries exist.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from scopefuel import bench, cli, launch
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    DEVIN_ARGON_ANNOTATION,
    GRADE_TABLE,
    PROFILE_ALIASES,
    SWE2_MODEL_SCOPE_PROFILES,
    SWE2_PROFILE_RUNG,
    UNGRADED_TRIAL_PROFILES,
    UNGRADED_TRIAL_PROFILE_NAMES,
    gate_check,
    is_rep_measured,
    profile_pool,
    provider_family,
    recommend,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

NOW = dt.datetime(2026, 10, 18, 12, 0, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
_RESET = "2026-10-20T00:00:00+00:00"

ARGON_PROFILES = ("devin-argon", "devin-argon-medium", "devin-argon-low")
# profile -> devin model id (the launcher's --model argument), per the
# recorded ``devin models list`` (Gemini 4 Argon family).
ARGON_IDS = {
    "devin-argon": "gemini-4-argon-high",
    "devin-argon-medium": "gemini-4-argon-medium",
    "devin-argon-low": "gemini-4-argon-low",
}
ARGON_INPUT_PRICE = 1.0
ARGON_BLENDED_PRICE = 2.0  # (3*1 + 5) / 4 — the #920 3:1 formula


def _account(label: str, window: str, used: float | None, horizon: str) -> Bucket:
    return Bucket(
        label=label,
        window=window,
        used_pct=used,
        resets_at=_RESET,
        scope=Scope("account"),
        horizon=horizon,  # type: ignore[arg-type]
    )


def _swe2_bucket(rung: str, used: float = 0.0) -> Bucket:
    return Bucket(
        label=rung,
        window="",
        used_pct=used,
        resets_at=None,
        scope=Scope("model", rung),
        horizon="now",
        note="Free tag observed in devin models list at 2026-10-18T12:00:00Z",
    )


def _probe(daily: float, weekly: float = 10.0) -> ProviderResult:
    """Devin pool: SWE-2 Free buckets at 0.0 plus the account axes."""
    return ProviderResult(
        id="devin",
        pool_class="spend",
        buckets=[
            _account("daily", "1d", daily, "now"),
            _account("weekly", "7d", weekly, "week"),
            *(_swe2_bucket(rung) for rung in ("swe-2-high", "swe-2-medium", "swe-2-max")),
        ],
    )


def _gate(result: ProviderResult, profile: str):
    return gate_check([result], profile, today=TODAY, now=NOW)


# ── AC: policy launch --json ─────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(ARGON_PROFILES))
def test_policy_launch_json_reports_argon_id_paid_family_devin_pool(capsys, name):
    """The launch verdict prints the argon model id, billing paid, family
    google and pool devin — catalog from the bundled template. The grade is
    the inert C carrier, never a placement."""
    assert cli.main(["policy", "launch", name, "--json"]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    decision = json.loads(out.out)
    assert decision["profile"] == name
    assert decision["model_id"] == ARGON_IDS[name]
    assert decision["billing"] == "paid"
    assert decision["family"] == "google"
    assert decision["pool"] == "devin"
    assert decision["grade"] == "C"  # inert carrier — NOT a placement
    assert decision["gate"] == "default"
    assert decision["effort"] == ""


# ── rows: no grade anywhere, spelling, billing, unmeasured ───────────────────


def test_argon_rows_are_in_no_grade_bucket():
    """The operator placed no grade — GRADE_TABLE must not carry a spelling."""
    for name in ARGON_PROFILES:
        placements = [
            grade for grade, profiles in GRADE_TABLE.items() if any(p.name == name for p in profiles)
        ]
        assert placements == [], (name, placements)


@pytest.mark.parametrize("name", sorted(ARGON_PROFILES))
def test_argon_profile_row_is_unmeasured_paid(name):
    (profile,) = [p for p in UNGRADED_TRIAL_PROFILES if p.name == name]
    assert profile.benchmark is None
    assert profile.benchmark_source is None
    assert profile.launcher_effort is None  # devin takes no effort flag
    assert profile.gate == "default"
    assert profile.billing == "paid"
    assert profile.benchmark_annotation == DEVIN_ARGON_ANNOTATION
    assert profile.benchmark_annotation.startswith("미측정")
    assert not is_rep_measured(profile)
    assert profile_pool(name) == ("devin", None)


def test_ungraded_trial_names_are_exactly_the_argon_spellings():
    assert UNGRADED_TRIAL_PROFILE_NAMES == frozenset(ARGON_PROFILES)


def test_argon_annotation_claims_no_rep_evidence():
    assert DEVIN_ARGON_ANNOTATION.startswith("미측정")
    assert "급 실측(" not in DEVIN_ARGON_ANNOTATION
    assert "1620" in DEVIN_ARGON_ANNOTATION


# ── family is the google value ────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(ARGON_PROFILES))
def test_argon_family_is_google_never_anthropic_never_swe2(name):
    """Family asserted by equality — an anthropic or devin-swe2 mutant is red."""
    assert provider_family(name) == "google"
    assert provider_family(name) != provider_family("agy-flash")  # agy pool, not google
    assert provider_family(name) != provider_family("opus")  # not anthropic
    assert provider_family(name) != provider_family("devin-swe2")  # not swe2
    assert profile_pool(name)[0] == "devin"  # pool stays devin


@pytest.mark.parametrize("name", sorted(ARGON_PROFILES))
def test_launch_decision_family_is_google_while_pool_is_devin(name):
    decision = launch.resolve_launch(name)
    assert decision.family == "google"
    assert decision.pool == "devin"
    assert decision.billing == "paid"
    assert decision.grade == "C"


# ── launch spelling ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(ARGON_PROFILES))
def test_argon_launch_spelling_is_the_bare_profile(name):
    """bin/wrk rejects --effort for devin spellings — the accepted fragment is
    the bare profile name, same shape as devin-ds41."""
    assert launch.launch_spelling(name, None) == name
    assert launch.launch_spelling(name, "high") == name
    assert not launch.launcher_accepts_effort_flag(name)


# ── model ids: launch map, equivalence, snapshot, fixture ────────────────────


def test_launch_model_ids_carry_the_argon_uids():
    for name, uid in ARGON_IDS.items():
        assert launch.LAUNCH_MODEL_IDS[name] == uid


def test_grades_model_equivalence_maps_argon_ids_to_profiles():
    from scopefuel.grades import MODEL_EQUIVALENCE

    for name, uid in ARGON_IDS.items():
        assert MODEL_EQUIVALENCE[uid] == frozenset({name})


def test_argon_snapshot_rows_carry_the_inert_c_grade():
    """One (profile, "") catalog row each — gate default, inert grade C."""
    for name, uid in ARGON_IDS.items():
        rows = [entry for entry in launch.snapshot_entries() if entry.profile == name]
        assert len(rows) == 1
        (row,) = rows
        assert row.effort == ""
        assert row.grade == "C"  # inert carrier — NOT a placement
        assert row.model_id == uid
        assert row.pool == "devin"
        assert row.score is None
        assert row.gate == "default"


def test_argon_model_ids_are_real_devin_model_uids(fixture_text):
    """The launch ids resolve to priced rows in the recorded models list —
    no Free tag exists on any Argon row, which is why billing=paid is honest."""
    listed = {}
    for line in fixture_text("devin_models_list").splitlines():
        if line.startswith("  ") and "[" in line:
            listed[line.split()[0]] = line
    for name, uid in ARGON_IDS.items():
        assert uid in listed
        assert "$1 / 1M Input" in listed[uid]
        assert "Free" not in listed[uid]


def test_price_seeds_carry_the_argon_prices(tmp_path):
    """Input, output AND blended — pinned separately (a 2.0->20.0 mutant is red)."""
    prices = bench.read_prices(path=tmp_path / "missing.db")
    for name, uid in ARGON_IDS.items():
        price = prices[uid]
        assert price.price_1m_input_tokens == ARGON_INPUT_PRICE
        assert price.price_1m_output_tokens == ARGON_INPUT_PRICE * 5
        assert price.price_1m_blended_3_to_1 == ARGON_BLENDED_PRICE


# ── --recommend never offers an argon spelling ────────────────────────────────


def test_recommend_never_lists_an_argon_spelling(fixture_text):
    """No grade was placed: the spellings are absent at every grade, C
    included — the inert catalog carrier must not surface as a candidate."""
    providers = [_measured_devin(fixture_text)]
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        out = recommend(providers, grade, explain=True, today=TODAY, now=NOW)
        for name in ARGON_PROFILES:
            assert name not in out, (grade, name, out)


def test_catalog_grade_table_skips_the_argon_rows():
    """Even a canon seeded with the argon rows cannot make them candidates."""
    view = bench.CatalogView(entries=bench.catalog_snapshot(), source="server", backend="handoffkeep")
    table = bench._catalog_grade_table(view)
    assert table is not None
    placed = {p.name for profiles in table.values() for p in profiles}
    for name in ARGON_PROFILES:
        assert name not in placed


def _measured_devin(fixture_text) -> ProviderResult:
    """#1381: models list gives no quota — a measured devin pool comes from the
    PTY session parser."""
    from scopefuel.providers import devin

    return devin.parse_session(fixture_text("devin_usage"), now=NOW)


# ── gate: account Daily/Weekly, fail closed — never a swe-2 bucket ────────────


@pytest.mark.parametrize("name", sorted(ARGON_PROFILES))
def test_argon_denies_when_account_daily_is_over_cutoff(name):
    """SWE-2 Free buckets at 0.0 must not admit a paid Argon spelling."""
    result = _gate(_probe(daily=99.0), name)
    assert result.ok is False
    assert result.unmeasurable is False
    assert result.gate_basis is None
    assert result.used_pct == 99.0
    assert "model-scope" not in result.reason


@pytest.mark.parametrize("name", sorted(ARGON_PROFILES))
def test_argon_admits_when_account_daily_is_under_cutoff(name):
    result = _gate(_probe(daily=10.0), name)
    assert result.ok is True
    assert result.profile == name
    assert result.provider_id == "devin"
    assert result.gate_basis is None
    assert "basis=model-scope" not in result.reason


def test_no_argon_spelling_is_a_swe2_model_scope_name():
    """The routing mutant: adding an argon spelling to the SWE-2 allowlist
    would let it pass on a Free bucket while the account is exhausted."""
    for name in ARGON_PROFILES:
        assert name not in SWE2_MODEL_SCOPE_PROFILES
        assert name not in SWE2_PROFILE_RUNG
        assert name not in SWE2_PROFILE_RUNG.values()


def test_argon_gate_choices_exist_and_are_not_recommend_profiles(capsys):
    parser = cli.build_parser(["devin"])
    gate = parser._subparsers._group_actions[0].choices["gate"]
    profile_action = next(action for action in gate._actions if action.dest == "profile")
    for name in ARGON_PROFILES:
        assert name in profile_action.choices
    assert cli.main(["--list-recommend-profiles"]) == 0
    listed = set(capsys.readouterr().out.splitlines())
    for name in ARGON_PROFILES:
        assert name not in listed


# ── no builder seat ───────────────────────────────────────────────────────────


def test_no_builder_alias_exists_for_the_argon_rows():
    """#1620: worker/tester-only — no _BUILDER_RUNGS entries and no alias maps
    onto an argon name (both directions pinned, the #1380 shape)."""
    from scopefuel.grades import _BUILDER_RUNGS

    for name in ARGON_PROFILES:
        assert not any(target[0] == name for target in _BUILDER_RUNGS.values())
        assert name not in PROFILE_ALIASES.values()
    assert not any(name.startswith("builder-argon") for name in _BUILDER_RUNGS)


# ── wrk contract (part B mirror) ──────────────────────────────────────────────


def test_wrk_contract_exempt_carries_the_argon_spellings():
    """agent-skills' bin/wrk keeps the argon spellings fixed-argv like every
    devin row — the exempt table is what part B must match by name."""
    import test_wrk_contract_guard

    for name in ARGON_PROFILES:
        assert name in test_wrk_contract_guard.WRK_CATALOG_EXEMPT
        assert "#1620" in test_wrk_contract_guard.WRK_CATALOG_EXEMPT[name]
