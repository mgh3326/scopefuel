"""hk 2205 — weekly pace promotion for EVERY paid pool, plus the two hk 2185
promotion defects (comments 8096 and 8097).

* (task) the pace-multiple trigger generalises from claude/codex to every paid
  pool — devin (including the ds41 rungs), grok, codex, claude, and any future
  paid pool that reports a weekly account bucket. At the same grade a paid
  pool at or above its threshold (``[settings] altlane_pace_multiple`` with a
  ``[pools.<pool>]`` override of the same key) yields to the under-used lanes:
  pi on anthropic-api, cmd on commandcode, and every paid pool measured below
  its own threshold. SWE-2 free stays FIRST whenever its grade fits and it has
  quota. Healthy pools keep today's order.
* (8096) the trigger honours PACE_EARLY_WINDOW_FRACTION — a 3x read at 2%
  elapsed does not promote.
* (8097 defect 1) ``cli._promotion_blocks`` counts a bounce only for
  completed=0 (any role) or the lane's OWN impl/fix work, keyed by
  (profile, rung, grade) — a verify/reviewer rep's blockers_found is the
  tester succeeding, never a block.
* (8097 defect 2) canon-placed alt-lane rungs with an inherited basis qualify
  for the one-up listing, and a one-up row is promotable (its placement caps
  it — a placed A+ covers A).

Each test docstring names the mutant it kills.
"""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import io
import pathlib
import subprocess
import sys
import tarfile

import pytest

from scopefuel import bench, cli, policy
from scopefuel import recommend as recommend_mod
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    ALTLANE_PROFILES,
    recommend_dict,
)

NOW = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
# The head this branch was cut from (hk 2185 merged) — the "today" order the
# golden compares to.
BASE_COMMIT = "9fd1c45b0552644d66485b01cf4fb5aa4f65d0aa"


def _bucket(window: str, used: float, reset_in_hours: float | None, *, scope: Scope | None = None) -> Bucket:
    reset = None if reset_in_hours is None else (NOW + dt.timedelta(hours=reset_in_hours)).isoformat()
    return Bucket(
        label=window,
        window=window,
        used_pct=used,
        resets_at=reset,
        scope=scope or Scope("account"),
        horizon="week",
    )


def _provider(pool: str, *buckets: Bucket, pool_class: str = "spend") -> ProviderResult:
    return ProviderResult(id=pool, pool_class=pool_class, buckets=list(buckets))  # type: ignore[arg-type]


def _weekly(
    pool: str, used: float, *, reset_in_hours: float = 84.0, pool_class: str = "spend"
) -> ProviderResult:
    """A pool measured on the weekly window; reset_in_hours=84 halves the 7d
    window, so the pace multiple is exactly ``used / 50``."""

    return _provider(pool, _bucket("7d", used, reset_in_hours), pool_class=pool_class)


def _credit(pool: str = "anthropic-api", *, used: float = 25.0) -> ProviderResult:
    return _provider(pool, _bucket("7d", used, 144.0))


def _healthy(*extra: ProviderResult) -> list[ProviderResult]:
    return [
        _weekly("claude", 40.0),
        _weekly("codex", 20.0, pool_class="preserve"),
        _credit("anthropic-api"),
        _credit("commandcode"),
        *extra,
    ]


def _swe2_free_buckets() -> list[Bucket]:
    return [
        Bucket(rung, "", 0.0, None, Scope("model", rung), "now", "Free tag observed")
        for rung in ("swe-2-high", "swe-2-medium", "swe-2-max")
    ]


def _candidates(payload: dict) -> list[dict]:
    return [row for row in payload["rows"] if row["kind"] == "candidate"]


def _names(payload: dict) -> list[str]:
    return [row["profile"] for row in _candidates(payload)]


def _base_recommend(tmp_path):
    """The pre-change recommend module, loaded from ``BASE_COMMIT``."""

    repo = pathlib.Path(__file__).resolve().parents[1]
    if subprocess.run(["git", "cat-file", "-e", BASE_COMMIT], cwd=repo, capture_output=True).returncode:
        subprocess.run(
            ["git", "fetch", "--depth=1", "origin", BASE_COMMIT],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    archive = subprocess.run(
        ["git", "archive", BASE_COMMIT, "src"], cwd=repo, check=True, capture_output=True
    ).stdout
    target = tmp_path / "base2205"
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(target, filter="data")
    package = target / "src" / "scopefuel"
    spec = importlib.util.spec_from_file_location(
        "scopefuel_hk2205_base", package / "__init__.py", submodule_search_locations=[str(package)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return importlib.import_module("scopefuel_hk2205_base.recommend")


class _Rep:
    def __init__(self, profile, grade, completed, blockers, role="impl", effort=None):
        self.profile = profile
        self.grade = grade
        self.completed = completed
        self.blockers_found = blockers
        self.role = role
        self.effort = effort


# ---------------------------------------------------------------------------
# AC1/AC2: every paid pool yields; below-threshold pools promote; SWE-2 first
# ---------------------------------------------------------------------------


def test_devin_at_214x_yields_and_grok_at_118x_does_not():
    """AC1 — Mutant: exempt devin from the rule -> this goes RED."""

    # devin 75% used with 59h elapsed of 168h -> 2.14x; grok 59% at half window -> 1.18x.
    providers = _healthy(_weekly("devin", 75.0, reset_in_hours=109.0), _weekly("grok", 59.0))
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    names = _names(payload)
    promoted = {row["profile"]: row["promotion"] for row in _candidates(payload) if row.get("promotion")}
    assert promoted
    assert all(reason == "promoted: devin 7d pace 2.14x" for reason in promoted.values())
    # devin's own paid rows are the yielding set — never promoted.
    assert "devin-ds41" in names and "devin-ds41" not in promoted
    assert names.index("devin-ds41") > max(names.index(name) for name in promoted if name in names)
    # grok stays in order — not triggered — but its rows ARE under-used, so
    # they promote above the yielding devin rows.
    assert "grok-hi" in promoted


def test_swe2_free_stays_first_when_its_pool_yields():
    """AC2 — Mutant: rank SWE-2 below a promoted pool -> this goes RED.

    devin's account axis is hot (1.8x) so devin's PAID rows yield, but the
    SWE-2 Free model-scope rows are not part of the yielding set — they keep
    the front of the list.
    """

    devin = ProviderResult(
        id="devin",
        pool_class="spend",
        buckets=[_bucket("7d", 90.0, 84.0), *_swe2_free_buckets()],
    )
    providers = _healthy(devin)
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW, swe2_result=devin)
    rows = _candidates(payload)
    free_names = {"devin-swe2", "devin-swe2-medium", "devin-swe2-max"}
    first_promoted = next(i for i, row in enumerate(rows) if row.get("promotion"))
    free_rows = [i for i, row in enumerate(rows) if row["profile"] in free_names]
    assert free_rows and max(free_rows) < first_promoted
    # The yielding pool's paid rows still land after every promoted row.
    paid_devin = [i for i, row in enumerate(rows) if row["profile"] == "devin-ds41"]
    assert paid_devin and min(paid_devin) > max(i for i, row in enumerate(rows) if row.get("promotion"))


def test_swe2_free_stays_first_with_a_preserve_class_hot_pool():
    """Tester r1 B1 — Mutant: let a promoted row land above SWE-2 free -> RED.

    devin is hot AND a second pool is hot on the preserve class. A preserve
    pool has no pace headroom, so its measured row outranks the unmeasured
    SWE-2 free rows; the promoted block must still stay behind them.
    """

    devin = ProviderResult(
        id="devin",
        pool_class="spend",
        buckets=[_bucket("7d", 75.0, 109.0), *_swe2_free_buckets()],  # 2.14x
    )
    providers = [
        _weekly("claude", 75.0, reset_in_hours=98.5, pool_class="preserve"),  # ~1.8x preserve
        _weekly("codex", 20.0, pool_class="preserve"),
        devin,
        _weekly("grok", 25.0),
        _credit("anthropic-api"),
        _credit("commandcode"),
    ]
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW, swe2_result=devin)
    rows = _candidates(payload)
    free_names = {"devin-swe2", "devin-swe2-medium", "devin-swe2-max"}
    first_promoted = next(i for i, row in enumerate(rows) if row.get("promotion"))
    free_rows = [i for i, row in enumerate(rows) if row["profile"] in free_names]
    assert free_rows and max(free_rows) < first_promoted


def test_a_group_scoped_pool_never_decides_by_bucket_order():
    """Tester r1 B2 — Mutant: read the first 7d bucket whatever its scope -> RED.

    agy reports one 7d bucket per group (gemini, 3p). Neither bucket order may
    change the verdict: a pool with no account-scope weekly bucket is unknown,
    never a trigger and never "under-used".
    """

    def agy(*buckets: Bucket) -> ProviderResult:
        return ProviderResult(id="agy", pool_class="preserve", buckets=list(buckets))

    cool = _bucket("7d", 4.0, 120.0, scope=Scope("group", "gemini"))  # 0.2x
    hot = _bucket("7d", 50.0, 120.0, scope=Scope("group", "3p"))  # 1.8x
    for order in ((cool, hot), (hot, cool)):
        providers = _healthy(_weekly("devin", 30.0), agy(*order))
        assert recommend_mod.altlane_promotions(providers, NOW, grade="A") == {}, order
        payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
        assert not any(row.get("promotion") for row in _candidates(payload)), order


def test_a_pool_with_no_weekly_account_bucket_is_never_under_used():
    """Mutant: count an unmeasured pool as under-used -> this goes RED."""

    five_h = _provider("grok", _bucket("5h", 95.0, 1.0))  # only a 5h window
    providers = _healthy(_weekly("devin", 75.0, reset_in_hours=109.0), five_h)
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    grok_rows = [row for row in _candidates(payload) if row["provider"] == "grok"]
    assert not any(row.get("promotion") for row in grok_rows)


def test_an_early_window_pool_is_neither_trigger_nor_under_used():
    """Mutant: guard the trigger but still count early-window pools as below -> RED."""

    providers = _healthy(_weekly("devin", 75.0, reset_in_hours=109.0))  # devin 2.14x yields
    providers[1] = _provider("codex", _bucket("7d", 9.0, 165.0), pool_class="preserve")  # ~2% elapsed
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    codex_rows = [row for row in _candidates(payload) if row["provider"] == "codex"]
    assert not any(row.get("promotion") for row in codex_rows)


def test_every_healthy_pool_keeps_the_pre_change_order(tmp_path):
    """AC3 golden — Mutant: reorder a healthy pool -> this goes RED.

    Captured against BASE_COMMIT (hk 2185 merged). When every pool is healthy
    nothing triggers, so no row carries a promotion and the pre-change
    candidate sequence is a subsequence of the new one — the only additions
    are the defect-2 canon one-up listings.
    """

    base = _base_recommend(tmp_path)
    providers = _healthy(_weekly("devin", 30.0), _weekly("grok", 25.0))
    base_names: dict[str, list[str]] = {}
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        out = base.recommend(providers, grade, today=TODAY, now=NOW, bench_scores=None, model_prices={})
        base_names[grade] = []
        for line in out.splitlines():
            if not line[:1].isdigit():
                continue
            tokens = line.split(". ", 1)[1].split()
            while tokens and tokens[0].startswith("🔥"):
                tokens.pop(0)
            # The one-up/borrowed tag rides the name token: "devin-ds41[one-up A+]".
            base_names[grade].append(tokens[0].split("[")[0])
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        payload = recommend_dict(providers, grade, today=TODAY, now=NOW)
        assert not any(row.get("promotion") for row in _candidates(payload)), grade
        names = _names(payload)
        # Every pre-change row keeps its relative order; the only added names
        # are canon alt-lane one-up rows.
        remaining = list(names)
        for name in base_names[grade]:
            assert name in remaining, (grade, name, names)
            remaining = remaining[remaining.index(name) + 1 :]
        added = set(names) - set(base_names[grade])
        assert added <= set(ALTLANE_PROFILES), (grade, added)


# ---------------------------------------------------------------------------
# 8096: the early-window guard
# ---------------------------------------------------------------------------


def test_a_3x_ratio_at_2_percent_elapsed_does_not_promote():
    """AC4 — Mutant: ignore the early-window fraction -> this goes RED.

    reset_in_hours=164.6 leaves 3.36h elapsed of 168h (~2%); 10% used reads as
    a 3x multiple — the noisy first 5% must not trigger.
    """

    providers = _healthy()
    providers[0] = _provider("claude", _bucket("7d", 10.0, 164.6))
    assert recommend_mod.altlane_promotions(providers, NOW, grade="A") == {}
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    assert not any(row.get("promotion") for row in _candidates(payload))


def test_a_ratio_just_past_the_early_window_still_triggers():
    """The same 3x read at 6% elapsed DOES trigger — the guard is the
    fraction, not the multiple."""

    providers = _healthy()
    providers[0] = _provider("claude", _bucket("7d", 18.0, 158.0))  # ~6% elapsed, 3x
    assert recommend_mod.altlane_promotions(providers, NOW, grade="A")


# ---------------------------------------------------------------------------
# 8097 defect 1: rep roles in _promotion_blocks
# ---------------------------------------------------------------------------


def test_a_verify_reps_blockers_do_not_block_the_lane(monkeypatch):
    """AC5 — Mutant: treat verify blockers as a block -> this goes RED.

    Rep srv:1817's shape: pi-sonnet55, grade A, role=verify, blockers_found=3
    — the tester found blockers in someone else's PR. It must not keep
    pi-sonnet55@high from promotion at A.
    """

    reps = [_Rep("pi-sonnet55", "A", 1, 3, role="verify")]
    monkeypatch.setattr(bench, "read_reps", lambda **kwargs: reps)
    assert cli._promotion_blocks() == frozenset()
    providers = _healthy()
    providers[0] = _weekly("claude", 75.0)
    providers[1] = _weekly("codex", 80.0, reset_in_hours=140.0, pool_class="preserve")  # ~2.8x
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW, promotion_blocks=cli._promotion_blocks)
    names = _names(payload)
    row = next(row for row in _candidates(payload) if row["launch"] == "pi-sonnet55 --effort high")
    assert row["promotion"].startswith("promoted:")
    assert names.index("pi-sonnet55") < names.index("sonnet")
    assert names.index("pi-sonnet55") < names.index("codex-luna")


def test_an_impl_bounce_or_any_failed_rep_blocks(monkeypatch):
    """AC6 — Mutant: never read rep evidence / accept verify-only roles -> RED."""

    reps = [
        _Rep("pi-sonnet55", "A", 1, 2, role="impl", effort="high"),  # bounce of own work
        _Rep("pi-haiku55", "B", 0, 0, role="verify"),  # completed=0 blocks any role
        _Rep("cmd-dsflash41", "A", 0, 0, role="verify", effort="high"),
    ]
    monkeypatch.setattr(bench, "read_reps", lambda **kwargs: reps)
    assert cli._promotion_blocks() == frozenset(
        {
            ("pi-sonnet55", "high", "A"),
            ("pi-haiku55", None, "B"),
            ("cmd-dsflash41", "high", "A"),
        }
    )
    providers = _healthy()
    providers[0] = _weekly("claude", 75.0)
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW, promotion_blocks=cli._promotion_blocks)
    high = next(row for row in _candidates(payload) if row["launch"] == "pi-sonnet55 --effort high")
    assert "promotion" not in high
    one_up = next(row for row in _candidates(payload) if row["profile"] == "cmd-dsflash41")
    assert "promotion" not in one_up
    # A sibling rung of the same profile with no failure still promotes.
    xhigh = next(row for row in _candidates(payload) if row["launch"] == "pi-sonnet55 --effort xhigh")
    assert xhigh.get("promotion")


# ---------------------------------------------------------------------------
# 8097 defect 2: canon one-up listing and promotion
# ---------------------------------------------------------------------------


def test_cmd_dsflash41_is_a_one_up_row_at_a_and_promotable():
    """AC7 — Mutants: exclude inherited rows from one-up (RED); exclude
    one_up rows from promotion (RED)."""

    providers = _healthy()
    providers[0] = _weekly("claude", 75.0)
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    row = next(row for row in _candidates(payload) if row["profile"] == "cmd-dsflash41")
    assert row["one_up"] is True and row["placed_grade"] == "A+"
    assert row["promotion"].startswith("promoted:")
    names = _names(payload)
    # It ranks above the hot primaries like the measured one-up rows do.
    assert names.index("cmd-dsflash41") < names.index("sonnet")


def test_the_cmd_host_filter_still_applies_to_a_one_up_row():
    """No commandcode measurement -> no cmd row, promoted or not."""

    providers = [_weekly("claude", 75.0), _credit("anthropic-api")]
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    assert "cmd-dsflash41" not in _names(payload)


# ---------------------------------------------------------------------------
# AC8: the per-pool threshold override
# ---------------------------------------------------------------------------


def test_a_per_pool_threshold_override_needs_no_code_edit():
    """AC8 — Mutant: read only the global key -> this goes RED."""

    policy.config_path().parent.mkdir(parents=True, exist_ok=True)
    policy.config_path().write_text("[pools.devin]\naltlane_pace_multiple = 3.0\n")
    assert policy.get_altlane_pace_multiple("devin") == pytest.approx(3.0)
    assert policy.get_altlane_pace_multiple("claude") == pytest.approx(1.2)
    providers = _healthy(_weekly("devin", 75.0, reset_in_hours=109.0))  # 2.14x — under the 3.0 override
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    assert not any(row.get("promotion") for row in _candidates(payload))
    # The same pool at ~3.0x still yields under its own override.
    providers[-1] = _weekly("devin", 95.0, reset_in_hours=115.0)
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    assert any(row.get("promotion") for row in _candidates(payload))


def test_a_malformed_per_pool_override_falls_back_to_the_settings_key():
    policy.config_path().parent.mkdir(parents=True, exist_ok=True)
    policy.config_path().write_text('[pools.devin]\naltlane_pace_multiple = "nope"\n')
    assert policy.get_altlane_pace_multiple("devin") == pytest.approx(1.2)


# ---------------------------------------------------------------------------
# Limits: the 2185 guardrails all stand under the generalised rule
# ---------------------------------------------------------------------------


def test_s_plus_never_promotes_even_when_devin_is_hot():
    providers = _healthy(_weekly("devin", 95.0, reset_in_hours=100.0))
    assert recommend_mod.altlane_promotions(providers, NOW, grade="S+") == {}
    payload = recommend_dict(providers, "S+", today=TODAY, now=NOW)
    assert not any(row.get("promotion") for row in _candidates(payload))


def test_a_tester_role_keeps_the_family_split_under_the_generalised_rule():
    providers = _healthy(_weekly("devin", 95.0, reset_in_hours=100.0))
    assert recommend_mod.altlane_promotions(providers, NOW, grade="A", role="tester") == {}
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW, role="tester")
    assert not any(row.get("promotion") for row in _candidates(payload))


def test_promotion_is_never_an_exclusion():
    """The yielding pool's rows stay listed as fallbacks."""

    providers = _healthy(_weekly("devin", 75.0, reset_in_hours=109.0))
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW)
    names = _names(payload)
    assert "devin-ds41" in names
    assert "devin-swe2" in names
