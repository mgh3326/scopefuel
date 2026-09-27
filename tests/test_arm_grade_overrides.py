"""#781: the per-arm grade override stamped on the bundled catalog.

Hosts read the bundled catalog snapshot (the server catalog is not in use, #712
on hold), so a ``grades apply`` artifact — a file — never reaches them. The
operator-approved arm grade therefore lives in code:
``launch.ARM_GRADE_OVERRIDES`` restates exactly the rows the decision named
(hk:doc 5177 item 2, 2026-09-27) with the decided grade and provenance, and
every other bundled row stays byte-identical.

#787 followed the override with the matching placement change:
``recommend.GRADE_TABLE`` now carries ``devin-swe2-medium`` at A as well, so on
bundled hosts the rung IS a recommend/gate candidate at the approved grade
(``test_the_stamped_rung_is_an_a_candidate_on_bundled_hosts``). The override's
remaining role is the decision provenance (``decided_by``/``decided_at``/
``deviation_ref``) stamped on the catalog row — its grade restatement is now a
no-op, and removing the entry would only strip provenance, so it stays.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json

from scopefuel import bench, cli, grades, launch
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    E6_ARM_KEYS,
    GRADE_TABLE,
    Profile,
    e6_arm_rung_for,
    gate_check,
    recommend,
)

OVERRIDE_KEY = ("devin-swe2-medium", "")
DECIDED_BY = "operator:2026-09-27 via operator-desk"
DECIDED_AT = "2026-09-27"
DEVIATION_REF = "hk:doc 5177 item 2 (evidence srv:973, srv:976, srv:988)"

TODAY = dt.date(2026, 9, 27)
NOW = dt.datetime(2026, 9, 27, 12, 0, 0, tzinfo=dt.UTC)


def _provider(provider_id: str, used: float = 10.0) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class="spend",  # type: ignore[arg-type]
        buckets=[
            Bucket(
                label="7d",
                window="7d",
                used_pct=used,
                resets_at=(dt.datetime.now(dt.UTC) + dt.timedelta(hours=100)).isoformat(),
                scope=Scope("account"),
                horizon="week",  # type: ignore[arg-type]
            )
        ],
    )


def _pre_change(entries_fn):
    """The bundled rows exactly as the pre-change code produced them."""

    return {entry.key: entry.as_dict() for entry in entries_fn()}


# --- AC1: the override table and the single-row delta ------------------------


def test_the_override_table_is_exactly_the_decided_entry():
    assert set(launch.ARM_GRADE_OVERRIDES) == {OVERRIDE_KEY}
    override = launch.ARM_GRADE_OVERRIDES[OVERRIDE_KEY]
    assert override.grade == "A"
    assert override.decided_by == DECIDED_BY
    assert override.decided_at == DECIDED_AT
    assert "hk:doc 5177" in override.deviation_ref
    for ref in ("srv:973", "srv:976", "srv:988"):
        assert ref in override.deviation_ref


def test_only_the_decided_row_differs_from_the_pre_change_snapshot(monkeypatch):
    post = bench.catalog_snapshot()
    monkeypatch.setattr(launch, "ARM_GRADE_OVERRIDES", {})
    pre = _pre_change(bench.catalog_snapshot)

    assert set(pre) == {entry.key for entry in post}, "the override may not add or drop a row"
    post_by_key = {entry.key: entry.as_dict() for entry in post}
    changed = [key for key in pre if pre[key] != post_by_key[key]]
    assert changed == [OVERRIDE_KEY]

    # The restated row: grade A with the full decision provenance — and nothing
    # else on the row moved. #787: GRADE_TABLE carries A too, so the override's
    # grade restatement is a no-op — without it the row is still A.
    assert pre[OVERRIDE_KEY]["grade"] == "A"
    row = post_by_key[OVERRIDE_KEY]
    assert row["grade"] == "A"
    assert row["decided_by"] == DECIDED_BY
    assert row["decided_at"] == DECIDED_AT
    assert row["deviation_ref"] == DEVIATION_REF
    for column, value in pre[OVERRIDE_KEY].items():
        if column not in ("grade", "decided_by", "decided_at", "deviation_ref"):
            assert row[column] == value, column


def test_every_e6_arm_row_is_unchanged(monkeypatch):
    post = launch.e6_arm_entries()
    monkeypatch.setattr(launch, "ARM_GRADE_OVERRIDES", {})
    pre = _pre_change(launch.e6_arm_entries)
    assert {entry.key: entry.as_dict() for entry in post} == pre
    assert all(entry.grade == "C" for entry in post)


def test_the_seed_emit_keeps_the_rows_own_decision_provenance(capsys):
    assert cli.main(["bench", "push-catalog", "--emit-seed", "--decided-by", "operator-desk"]) == 0
    rows = {(row["profile"], row["effort"]): row for row in json.loads(capsys.readouterr().out)["catalog"]}
    row = rows[OVERRIDE_KEY]
    assert row["grade"] == "A"
    assert row["decided_by"] == DECIDED_BY
    assert row["decided_at"] == DECIDED_AT
    assert row["deviation_ref"] == DEVIATION_REF
    # Rows without their own provenance still take the generic seed stamp.
    other = rows[("devin-swe2-max", "")]
    assert other["grade"] == "C"
    assert other["decided_by"] == "operator-desk"


# --- AC2: the catalog views show grade A with the provenance ------------------


def test_catalog_list_shows_the_row_at_a_with_provenance():
    text = bench.catalog_report()
    lines = {line.split()[1].split("@")[0]: line for line in text.splitlines()[1:]}
    row = lines["devin-swe2-medium"]
    assert row.startswith("A ")
    assert f"decided_by={DECIDED_BY}" in row
    assert f"decided_at={DECIDED_AT}" in row
    assert "hk:doc 5177" in row
    # No other bundled row carries decision provenance — nothing else prints it.
    assert sum("decided_by=" in line for line in text.splitlines()) == 1


def test_grades_rung_view_shows_current_a_with_provenance():
    (row,) = [entry for entry in bench.catalog_snapshot() if entry.key == OVERRIDE_KEY]
    result = grades.RungResult(
        key=OVERRIDE_KEY,
        row=row,
        counted=[],
        excluded=[],
        action="hold",
        target="A",
        passes_at={},
        ungraded_passes=[],
        unclean_passes=[],
        fails=[],
        evidence_refs=[],
        note="test",
    )
    first = grades.render_rung_detail(result)[0]
    assert first.startswith("rung devin-swe2-medium current=A")
    assert f"decided_by={DECIDED_BY}" in first
    assert f"decided_at={DECIDED_AT}" in first
    assert "hk:doc 5177" in first
    # A row without decision provenance renders no tag.
    assert grades._decided_tag(dataclasses.replace(row, decided_by=None)) == ""


def test_policy_launch_reports_the_stamped_grade():
    decision = launch.resolve_launch("devin-swe2-medium")
    assert decision.grade == "A"
    assert decision.model_id == "swe-2-medium"
    assert decision.pool == "devin"


# --- AC3: does an arm row stamped A become a recommend/gate candidate? -------
#
# On bundled-snapshot hosts since #787: YES — GRADE_TABLE places the rung at
# A, so ``--recommend`` and the quota gate judge it there, and the stamped
# catalog row and the placement canon agree. (Pre-#787 the answer was NO —
# the override restated the catalog row only and the placement table kept the
# rung at C.) On a server-canonical host the same stamped row is an ordinary
# placement at its grade (``_catalog_grade_table`` filters only unmeasured-C
# arm rows), so the two catalog paths agree too.


def test_the_stamped_rung_is_an_a_candidate_on_bundled_hosts():
    # #787 AC2: an A placement row, never a C one — and nowhere else.
    assert [p.name for p in GRADE_TABLE["A"]].count("devin-swe2-medium") == 1
    for grade in ("S+", "S", "A+", "B", "C"):
        assert not any(p.name == "devin-swe2-medium" for p in GRADE_TABLE[grade])
    table = bench.runtime_grade_table()
    assert any(p.name == "devin-swe2-medium" for p in table["A"])
    assert not any(p.name == "devin-swe2-medium" for p in table["C"])

    providers = [_provider("devin")]
    out_a = recommend(providers, "A", today=TODAY, now=NOW)
    assert "devin-swe2-medium" in out_a
    for grade in ("S+", "S", "A+"):
        out = recommend(providers, grade, today=TODAY, now=NOW)
        assert "devin-swe2-medium" not in out, grade

    result = gate_check(providers, "devin-swe2-medium", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.grade == "A"


def test_a_canon_row_stamped_above_c_would_be_an_ordinary_placement():
    """The other half of the AC3 finding: the measured->ordinary transition.

    ``_catalog_grade_table`` exempts only *unmeasured-C* arm rows; a canon row
    for an E6 rung stamped above C enters the table at its grade. The bundled
    snapshot never takes this path — it judges GRADE_TABLE — so the E6 arms'
    'never a recommendation' property is unchanged on bundled hosts.
    """

    view = bench.CatalogView(
        entries=(
            bench.CatalogEntry(
                profile="sonnet",
                effort="max",
                model_id="claude-sonnet-5",
                pool="claude",
                grade="A",
                score=50.0,
            ),
            bench.CatalogEntry(
                profile="sonnet",
                effort="high",
                model_id="claude-sonnet-5",
                pool="claude",
                grade="A+",
                score=55.0,
            ),
        ),
        source="server",
        backend="handoffkeep",
    )
    table = bench._catalog_grade_table(view)
    assert table is not None
    placed = {(p.name, p.launcher_effort or "") for p in table["A"]}
    assert ("sonnet", "max") in placed


def test_the_e6_arms_stay_marker_gated_on_bundled_hosts():
    """The never-a-recommendation behaviour for unmeasured arm rungs: unchanged."""

    providers = [_provider("claude")]
    result = gate_check(providers, "sonnet", effort="max", today=TODAY, now=NOW)
    assert result.ok is False
    assert "e6_arm_required" in result.reason
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        out = recommend(providers + [_provider("codex")], grade, today=TODAY, now=NOW)
        for profile, effort in E6_ARM_KEYS:
            for line in out.splitlines():
                if not line[:1].isdigit():
                    continue
                tokens = line.split()
                assert tokens[1] != profile or f"--effort {effort}" not in line, (grade, line)


def test_the_not_applied_rungs_keep_their_rows():
    """The decision reviewed but did NOT apply these — rows unchanged."""

    rows = {entry.key: entry for entry in bench.catalog_snapshot()}
    assert rows[("codex-terra", "medium")].grade == "A"  # its existing placement
    assert rows[("kimi-k3", "high")].grade == "C"
    assert rows[("oc-solar4", "")].grade == "B"
    for key in (("codex-terra", "medium"), ("kimi-k3", "high"), ("oc-solar4", "")):
        assert rows[key].decided_by is None
        assert rows[key].deviation_ref != DEVIATION_REF


def test_the_arm_rung_lookup_is_unaffected():
    """The override never creates an E6 arm — devin-swe2-medium is not one."""

    assert OVERRIDE_KEY not in E6_ARM_KEYS
    assert e6_arm_rung_for("devin-swe2-medium", "") is None
    assert isinstance(e6_arm_rung_for("sonnet", "max"), Profile)
