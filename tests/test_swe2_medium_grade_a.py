"""#787: ``devin-swe2-medium`` placed at A in the placement canon.

Operator decision 2026-09-27 via operator-desk (item 1), evidence
hk:doc 5177 item 2 — counted clean A PASSes srv:988/976/973 plus srv:995 and
srv:1076, builder-map effort inference accepted. #781 already stamped the
bundled catalog row at A (``launch.ARM_GRADE_OVERRIDES``); this task makes
``recommend.GRADE_TABLE`` — the table ``--recommend`` and the quota gate
actually judge — agree with it.

AC1 lives here: the A placement row with its provenance, and a fixture-delta
test proving every *other* GRADE_TABLE row is unchanged. AC2 (recommend/gate
at A, absent at A+/S/S+) is covered in ``test_arm_grade_overrides.py`` and
``test_devin_effort_variants.py``.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from collections import Counter

from scopefuel.recommend import (
    DEVIN_SWE2_MEDIUM_GRADE_ANNOTATION,
    GRADE_TABLE,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
# Rows of GRADE_TABLE captured at the pre-#787 head: the full field set per
# (grade, profile) row, so "no other row changes" is a comparison, not a
# spot check.
PRE_787_ROWS = FIXTURES / "grade_table_pre_787.json"


def _rows(table) -> list[dict]:
    return [
        {
            "grade": grade,
            **{k: v for k, v in dataclasses.asdict(p).items() if k not in _RUNTIME_ONLY_FIELDS},
        }
        for grade, profiles in table.items()
        for p in profiles
    ]


# #1318-2 round 2: catalog-evidence fields exist only on profiles built from a
# server/cache catalog — every bundled row keeps the defaults, so they carry no
# placement information the pre-#787 fixture could compare against.
# #1340 (dr-1340-1): the fixture predates the billing field entirely — it has
# no column to compare against, and the values are pinned by
# test_task1340_free_before_paid.py.
_RUNTIME_ONLY_FIELDS = {
    "catalog_backed",
    "catalog_annotation",
    "catalog_deviation_ref",
    "catalog_decided_by",
    "billing",
}


def test_devin_swe2_medium_is_a_single_a_row_with_provenance():
    (row,) = [p for p in GRADE_TABLE["A"] if p.name == "devin-swe2-medium"]
    assert row.model == "SWE-2 (medium)"
    assert row.benchmark is None  # reps-measured placement, not an AA-agent score
    assert row.gate == "default"
    assert row.launcher_effort is None  # devin's effort lives in the model id
    assert row.benchmark_annotation == DEVIN_SWE2_MEDIUM_GRADE_ANNOTATION
    for provenance in ("operator 2026-09-27", "hk:doc 5177", "srv:988", "srv:976", "srv:973"):
        assert provenance in row.benchmark_annotation
    # Nowhere else — the C rung is gone, no higher placement either.
    assert not any(
        p.name == "devin-swe2-medium" for grade in ("S+", "S", "A+", "B", "C") for p in GRADE_TABLE[grade]
    )


def test_no_other_grade_table_row_changed():
    """AC1: the delta against the pre-#787 table is this one move plus the
    #920 Sonnet 5.5 relabel (Sonnet 5 rows replaced by estimated 5.5 rows)
    plus the #1026 Sol 6.1 relabel (the two codex-sol S+ rows relabelled)
    plus the #1269 Haiku 5.5 refresh (the two Haiku 4.5 rows replaced by five
    estimated C-rung rows) plus #1297's five-row apply (sonnet@high -> B;
    devin-swe2@high and devin-swe2-max@max E6-graduated to A+; grok-hi@xhigh
    E6-graduated to A; oc-solar4 B -> A)."""
    pre = json.loads(PRE_787_ROWS.read_text())
    pre_counts = Counter(json.dumps(row, sort_keys=True) for row in pre)
    post_counts = Counter(json.dumps(row, sort_keys=True) for row in _rows(GRADE_TABLE))

    added = [json.loads(key) for key, n in (post_counts - pre_counts).items() for _ in range(n)]
    removed = [json.loads(key) for key, n in (pre_counts - post_counts).items() for _ in range(n)]

    def key(row: dict) -> tuple:
        return (row["grade"], row["name"], row["launcher_effort"])

    assert {key(row) for row in added} == {
        ("A", "devin-swe2-medium", None),
        ("S", "sonnet", "max"),
        ("A+", "sonnet", "xhigh"),
        ("C", "sonnet", "medium"),
        # #1026 (09-30 operator decision): the codex-sol rows relabelled
        # gpt-6-sol -> gpt-6.1-sol — same (grade, name, effort) keys.
        ("S+", "codex-sol", "max"),
        ("S+", "codex-sol", "xhigh"),
        # #1269 (10-08 Haiku 5.5 refresh): all five launchable rungs land at C
        # on the vendor TB4 curve.
        ("C", "haiku", "low"),
        ("C", "haiku", "medium"),
        ("C", "haiku", "high"),
        ("C", "haiku", "xhigh"),
        ("C", "haiku", "max"),
        # #1297 (10-08 operator applies, hk:task/1296 + hk:task/1297).
        ("B", "sonnet", "high"),
        ("A+", "devin-swe2", "high"),
        ("A+", "devin-swe2-max", "max"),
        ("A", "grok-hi", "xhigh"),
        ("A", "oc-solar4", None),
    }
    assert {key(row) for row in removed} == {
        ("C", "devin-swe2-medium", None),
        ("A+", "sonnet", "high"),
        ("A+", "sonnet", "xhigh"),
        ("A", "sonnet", "medium"),
        ("A", "sonnet", "low"),
        ("S+", "codex-sol", "max"),
        ("S+", "codex-sol", "xhigh"),
        # #1269: the two pre-refresh Haiku 4.5 rows are replaced — high is
        # demoted B -> C on purpose (vendor TB4 says 4.5 scored 0.0).
        ("B", "haiku", "high"),
        ("C", "haiku", "low"),
        # #1297: oc-solar4's B row is replaced by the promoted A row.
        ("B", "oc-solar4", None),
    }


def test_sol61_switch_changes_no_grade():
    """#1026 AC1: on each codex-sol row the relabel touched only the identity
    fields — model, aa ids, estimate_reason. Grade, score, gate, efforts and
    every annotation are byte-identical to the pre-switch row."""
    pre = json.loads(PRE_787_ROWS.read_text())
    post = _rows(GRADE_TABLE)
    identity_fields = {"model", "aa_agent_model_id", "aa_model_id", "estimate_reason"}

    for pre_row in pre:
        if pre_row["name"] != "codex-sol":
            continue
        post_rows = [
            row
            for row in post
            if (row["grade"], row["name"], row["launcher_effort"])
            == (pre_row["grade"], pre_row["name"], pre_row["launcher_effort"])
        ]
        assert len(post_rows) == 1, "the switch changes no grade"
        post_row = post_rows[0]
        for field, pre_value in pre_row.items():
            if field in identity_fields:
                continue
            assert post_row[field] == pre_value, "the switch changes no grade"
        assert post_row["model"] == f"GPT-6.1 Sol ({pre_row['launcher_effort']})"
        assert post_row["aa_agent_model_id"] == "gpt-6.1-sol"
        assert post_row["aa_model_id"] == "gpt-6-1-sol"
        assert "gpt-6.1-sol" in post_row["estimate_reason"]
