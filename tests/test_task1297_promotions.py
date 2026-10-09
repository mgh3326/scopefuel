"""#1297: the bundled catalog carries the 10-08 operator decisions.

The operator approved five grade moves on 2026-10-08 and desk applied them on
the served canon; the bundled catalog records the same rows so a future
``bench push-catalog --emit-seed`` cannot silently revert the decision
(#787 devin-swe2-medium C -> A is the precedent).

Decisions (decided_by=operator:2026-10-08 via operator-desk):

- sonnet@high          C -> B   (hk:task/1297, decided_at 11:52:05.815922Z)
- devin-swe2@high      C -> A+  (hk:task/1296, decided_at 13:03:23.617077Z)
- devin-swe2-max@max   C -> A+  (hk:task/1296)
- grok-hi@xhigh        C -> A   (hk:task/1296)
- oc-solar4            B -> A   (hk:task/1296)

The three rung rows were unmeasured E6 arms; promotion graduates them to
ordinary GRADE_TABLE placements — the #920 sonnet@max path — so their
catalog rows move from ``e6_arm_entries`` to ``snapshot_entries`` with the
decision provenance stamped by ``launch.ARM_GRADE_OVERRIDES``. NOT applied:
kimi-k3@high stays an unmeasured C arm (the server refused it as
non-monotonic).
"""

from __future__ import annotations

import datetime as dt
import json

from scopefuel import cli, launch
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import E6_ARM_KEYS, GRADE_TABLE, recommend

TODAY = dt.date(2026, 10, 8)
NOW = dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=dt.UTC)

# (profile, effort) -> (decided grade, task ref, one evidence rep)
SERVED: dict[tuple[str, str], tuple[str, str, str]] = {
    ("sonnet", "high"): ("B", "hk:task/1297", "srv:1224"),
    ("devin-swe2", "high"): ("A+", "hk:task/1296", "srv:686"),
    ("devin-swe2-max", "max"): ("A+", "hk:task/1296", "srv:1225"),
    ("grok-hi", "xhigh"): ("A", "hk:task/1296", "srv:1396"),
    ("oc-solar4", ""): ("A", "hk:task/1296", "srv:705"),
}

DECIDED_BY = "operator:2026-10-08 via operator-desk"
DECIDED_AT = {
    ("sonnet", "high"): "2026-10-08T11:52:05.815922Z",
    ("devin-swe2", "high"): "2026-10-08T13:03:23.617077Z",
    ("devin-swe2-max", "max"): "2026-10-08T13:03:23.617077Z",
    ("grok-hi", "xhigh"): "2026-10-08T13:03:23.617077Z",
    ("oc-solar4", ""): "2026-10-08T13:03:23.617077Z",
}


def _provider(provider_id: str) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class="spend",  # type: ignore[arg-type]
        buckets=[
            Bucket(
                label="7d",
                window="7d",
                used_pct=10.0,
                resets_at=(dt.datetime.now(dt.UTC) + dt.timedelta(hours=100)).isoformat(),
                scope=Scope("account"),
                horizon="week",  # type: ignore[arg-type]
            )
        ],
    )


def test_each_bundled_row_matches_the_served_decision():
    """The mutant pin: reverting any one of the five rows fails here."""

    rows = {entry.key: entry for entry in launch.snapshot_entries()}
    for key, (grade, task_ref, evidence) in SERVED.items():
        entry = rows[key]
        assert entry.grade == grade, (key, entry.grade)
        assert entry.decided_by == DECIDED_BY
        assert entry.decided_at == DECIDED_AT[key]
        assert task_ref in entry.deviation_ref
        assert evidence in entry.deviation_ref
        assert entry.gate == "default"


def test_each_row_is_an_ordinary_placement_at_its_grade():
    for key, (grade, _, _) in SERVED.items():
        profile, effort = key
        placed = [(p.name, p.launcher_effort or "") for p in GRADE_TABLE[grade]]
        assert key in placed
        # Exactly one placement row per promoted rung.
        assert placed.count(key) == 1
        assert key not in E6_ARM_KEYS  # graduated arms are ordinary now


def test_the_seed_emits_the_served_rows(capsys):
    assert cli.main(["bench", "push-catalog", "--emit-seed", "--decided-by", "operator-desk"]) == 0
    rows = {(row["profile"], row["effort"]): row for row in json.loads(capsys.readouterr().out)["catalog"]}
    for key, (grade, task_ref, _) in SERVED.items():
        row = rows[key]
        assert row["grade"] == grade
        assert row["decided_by"] == DECIDED_BY
        assert row["decided_at"] == DECIDED_AT[key]  # already RFC3339 on the wire
        assert task_ref in row["deviation_ref"]


def test_kimi_k3_high_stays_an_unapplied_arm():
    """The server refused kimi-k3@high as non-monotonic — it stays an E6 arm."""

    assert ("kimi-k3", "high") in E6_ARM_KEYS
    assert ("kimi-k3", "high") in {entry.key for entry in launch.e6_arm_entries()}
    assert ("kimi-k3", "high") not in {entry.key for entry in launch.snapshot_entries()}


def test_recommend_lists_each_rung_at_its_decided_grade_and_one_up():
    """#1297 decided placements + #1318-2 one-up listing.

    Every promoted rung is rep-measured (its annotation names reps evidence), so
    under the operator-A rule it also lists exactly one grade below its decided
    grade — tagged ``[one-up <grade>]`` — and nowhere else.
    """

    providers = [_provider(pid) for pid in ("claude", "devin", "grok", "upstage", "codex")]
    lines_at = {}
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        out = recommend(providers, grade, today=TODAY, now=NOW)
        lines_at[grade] = [line for line in out.splitlines() if line[:1].isdigit()]

    one_below = {"A+": "A", "A": "B", "B": "C"}
    expected_lines = {
        "A+": ["devin-swe2 --effort high", "devin-swe2-max --effort max"],
        "A": ["grok-hi --effort xhigh", "oc-solar4"],
        "B": ["sonnet --effort high"],
    }
    for grade, names in expected_lines.items():
        for name in names:
            decided = [line for line in lines_at[grade] if name in line]
            assert decided and all("[one-up" not in line for line in decided), (grade, name)
            for other in ("S+", "S", "A+", "A", "B", "C"):
                if other == grade:
                    continue
                hits = [line for line in lines_at[other] if name in line]
                if other == one_below[grade]:
                    assert hits and all(f"[one-up {grade}]" in line for line in hits), (
                        other,
                        name,
                    )
                else:
                    assert not hits, (other, name)


def test_launch_answers_for_each_promoted_row():
    """Bare and explicit-effort launch results after the apply."""

    # sonnet: DEFAULT_LAUNCH_EFFORTS pins high — bare and explicit agree.
    for decision in (
        launch.resolve_launch("sonnet"),
        launch.resolve_launch("sonnet", effort="high"),
    ):
        assert decision.effort == "high"
        assert decision.grade == "B"
        assert decision.model_id == "claude-sonnet-5-5"
        assert decision.gate == "default"

    # devin-swe2: effort "" stays the default (tied A+ rows); @high explicit.
    decision = launch.resolve_launch("devin-swe2")
    assert (decision.effort, decision.grade) == ("", "A+")
    decision = launch.resolve_launch("devin-swe2", effort="high")
    assert (decision.effort, decision.grade) == ("high", "A+")

    # devin-swe2-max: the A+ max rung is the best-graded ordinary row, so the
    # bare launch follows the canon onto it.
    decision = launch.resolve_launch("devin-swe2-max")
    assert (decision.effort, decision.grade) == ("max", "A+")
    decision = launch.resolve_launch("devin-swe2-max", effort="max")
    assert (decision.effort, decision.grade) == ("max", "A+")

    # grok-hi: bare keeps the launcher default rung (high, S); xhigh explicit.
    decision = launch.resolve_launch("grok-hi")
    assert (decision.effort, decision.grade) == ("high", "S")
    decision = launch.resolve_launch("grok-hi", effort="xhigh")
    assert (decision.effort, decision.grade) == ("xhigh", "A")


def test_policy_launch_sonnet_high_json_reports_b(capsys):
    """AC1: the bundled snapshot answers the promoted grade."""

    assert cli.main(["policy", "launch", "sonnet", "--effort", "high", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["grade"] == "B"
    assert payload["model_id"] == "claude-sonnet-5-5"
    assert payload["gate"] == "default"
    assert payload["catalog"]["source"] == "snapshot"
