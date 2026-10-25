"""hk 2007 — r1025p1apply: opus@low no longer lists at A+.

Operator decision (hk 1786, r1025p1apply): at A+ the claude seat is sonnet@xhigh
from now on, and opus@low is not used at A+. S is unchanged. The A+ listing came
only from ``BORROWED_RUNGS``, so removing that entry removes opus@low from
``--recommend A+`` and nowhere else: its placement stays S.

Mutants this file kills (stated as invariants):
* keeping the ``("opus", "low", "A+", ...)`` entry in ``BORROWED_RUNGS`` makes the
  A+ text and JSON cases RED;
* dropping opus@low from the S placement makes the S case RED.
"""

from __future__ import annotations

import json

from test_task1795_borrowed_rungs import (
    NOW,
    TODAY,
    _bucket,
    _canon_table,
    _pool_providers,
    _ranked,
)

from scopefuel import cli
from scopefuel import recommend as recommend_mod
from scopefuel.model import ProviderResult
from scopefuel.recommend import GRADE_TABLE, recommend, recommend_dict


def _is_opus_low(entry: dict) -> bool:
    return entry.get("profile") == "opus" and "low" in json.dumps(entry, ensure_ascii=False)


def test_aplus_text_has_no_opus_low_line_snapshot():
    out = recommend(_pool_providers(), "A+", today=TODAY, now=NOW)
    assert not any("opus --effort low" in line for line in out.splitlines()), out
    assert "[borrowed S]" not in out, out


def test_aplus_text_has_no_opus_low_line_canon():
    table = _canon_table()
    out = recommend(_pool_providers(table), "A+", today=TODAY, now=NOW, grade_table=table)
    assert not any("opus --effort low" in line for line in out.splitlines()), out
    assert "[borrowed S]" not in out, out


def test_aplus_json_has_no_opus_low_row_in_any_section():
    payload = recommend_dict(_pool_providers(), "A+", today=TODAY, now=NOW)
    sections = {"rows": payload["rows"], "excluded": payload["excluded"]}
    for name, entries in sections.items():
        assert not any(_is_opus_low(entry) for entry in entries), (name, payload)
    assert not any(row.get("borrowed") for row in payload["rows"]), payload


def test_aplus_cli_json_has_no_opus_low_row(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {
            name: (lambda provider_id=name: ProviderResult(id=provider_id, buckets=[_bucket()]))
            for name in {recommend_mod.resolved_pool(p)[0] for ps in GRADE_TABLE.values() for p in ps}
        },
    )
    assert cli.main(["--recommend", "A+", "--json", "--no-cache"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert not any(_is_opus_low(entry) for entry in payload["rows"] + payload["excluded"]), payload


def test_s_keeps_opus_low_as_an_ordinary_untagged_row():
    out = recommend(_pool_providers(), "S", today=TODAY, now=NOW)
    lines = [line for line in _ranked(out) if "opus --effort low" in line]
    assert len(lines) == 1, out
    assert "[borrowed" not in lines[0] and "[one-up" not in lines[0], lines[0]

    payload = recommend_dict(_pool_providers(), "S", today=TODAY, now=NOW)
    row = next(r for r in payload["rows"] if r["profile"] == "opus" and "low" in str(r["display"]))
    assert row["placed_grade"] == "S"
    assert row["kind"] == "candidate"
    assert not row.get("borrowed")


def test_opus_low_placement_is_s_and_not_aplus():
    assert any(p.name == "opus" and p.launcher_effort == "low" for p in GRADE_TABLE["S"])
    assert not any(p.name == "opus" and p.launcher_effort == "low" for p in GRADE_TABLE["A+"])
