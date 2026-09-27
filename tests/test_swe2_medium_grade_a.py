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
    return [{"grade": grade, **dataclasses.asdict(p)} for grade, profiles in table.items() for p in profiles]


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
    """AC1: the delta against the pre-#787 table is exactly this one move."""
    pre = json.loads(PRE_787_ROWS.read_text())
    pre_counts = Counter(json.dumps(row, sort_keys=True) for row in pre)
    post_counts = Counter(json.dumps(row, sort_keys=True) for row in _rows(GRADE_TABLE))

    added = [json.loads(key) for key, n in (post_counts - pre_counts).items() for _ in range(n)]
    removed = [json.loads(key) for key, n in (pre_counts - post_counts).items() for _ in range(n)]
    (added_row,) = added
    (removed_row,) = removed
    assert added_row["name"] == "devin-swe2-medium" and added_row["grade"] == "A"
    assert removed_row["name"] == "devin-swe2-medium" and removed_row["grade"] == "C"
    # The annotation carries the new provenance; nothing else on the row moved.
    moved_fields = {k for k in added_row if added_row[k] != removed_row[k]}
    assert moved_fields == {"grade", "benchmark_annotation"}
