"""hk 1943 (K4) — task-area views over reps.

``scopefuel reps compare --area A`` filters the existing compare to reps whose
task carries that hk area, and ``scopefuel grades areas`` renders the per-area
tables from the b953 packet: area by model cells holding n, clean %, mean
rounds (and accepted % when PA reviews are given), split by role and grade
band, with a minimum-sample eligibility flag. A rep's area always comes from
its task's area at aggregation time — never guessed — so unresolvable refs
land in ``unlinked`` and tasks without a tag land in ``untagged``.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections import Counter

import pytest

from scopefuel import areas, bench, cli
from scopefuel.http import HttpError

AREAS = [
    "blender",
    "unity",
    "web-qa",
    "ui-design",
    "image",
    "web-frontend",
    "go-backend",
    "python",
    "trading",
    "shell-wrk",
    "docs",
    "research",
    "ops",
]


class FakeHK:
    """One fake handoffkeep: bench reps/catalog plus the K1 task routes."""

    def __init__(self):
        self.reps: list[dict] = []
        self.catalog: list[dict] = []
        self.tasks: dict[int, dict] = {}
        self.area_vocab: list[str] | None = list(AREAS)
        self.hits: Counter[tuple[str, str]] = Counter()
        self.offline = False

    def request_json(self, url, *, method="GET", headers=None, body=None, timeout=20.0, **_kw):
        assert headers is not None
        assert headers["Authorization"] == "Bearer test-token"
        path = urllib.parse.urlsplit(url).path
        self.hits[(method, path)] += 1
        if self.offline:
            raise OSError("offline")
        if path == "/v1/task-areas":
            if self.area_vocab is None:
                raise HttpError(404, "not found")
            return {"areas": list(self.area_vocab)}
        match = re.fullmatch(r"/v1/tasks/(\d+)", path)
        if match:
            task_id = int(match.group(1))
            if task_id not in self.tasks:
                raise HttpError(404, "not found")
            return json.loads(json.dumps(self.tasks[task_id]))
        if path == "/v1/bench/reps":
            return {"reps": json.loads(json.dumps(self.reps))}
        if path == "/v1/bench/catalog":
            return {"catalog": json.loads(json.dumps(self.catalog))}
        raise AssertionError(f"unexpected URL: {url}")


@pytest.fixture
def hk(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFFKEEP_URL", "https://hk.test")
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", "test-token")
    fake = FakeHK()
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    return fake


def _rep(
    rep_id: int,
    task_ref: str | None,
    *,
    profile: str = "devin-swe2",
    model_id: str | None = "swe-2",
    role: str = "impl",
    grade: str | None = "A",
    rounds: int | None = 2,
    blockers_found: int | None = 0,
    completed: int | None = 1,
    recorded_at: str = "2026-10-20T10:00:00Z",
) -> dict:
    return {
        "id": rep_id,
        "origin_id": rep_id,
        "profile": profile,
        "model_id": model_id,
        "task_ref": task_ref,
        "tier": "T1",
        "role": role,
        "rounds": rounds,
        "blockers_found": blockers_found,
        "completed": completed,
        "input_tokens": None,
        "output_tokens": None,
        "notes": None,
        "recorded_at": recorded_at,
        "effort": None,
        "grade": grade,
        "table_grade": None,
        "created_by": "test",
    }


def _cat_entry(
    profile: str, model_id: str, *, grade: str = "A", retired_at: str | None = None
) -> bench.CatalogEntry:
    return bench.CatalogEntry(
        profile=profile,
        effort="",
        model_id=model_id,
        pool="test",
        grade=grade,
        retired_at=retired_at,
        decided_by="test",
    )


def _view(*entries: bench.CatalogEntry) -> bench.CatalogView:
    return bench.CatalogView(
        entries=tuple(entries),
        source=bench.CATALOG_SOURCE_SERVER,
        backend=bench.BENCH_BACKEND_HANDOFFKEEP,
        reason="configured",
        age_s=0.0,
    )


def _table(report: areas.AreasReport, band: str, role: str) -> areas.AreaTable:
    return next(t for t in report.tables if t.band == band and t.role == role)


def _row(table: areas.AreaTable, area: str) -> areas.AreaRow | None:
    return next((r for r in table.rows if r.area == area), None)


def _cell(table: areas.AreaTable, area: str, model: str) -> areas.AreaCell | None:
    row = _row(table, area)
    if row is None:
        return None
    return next((c for c in row.cells if c.model == model), None)


def _task(hk_fake: FakeHK, task_id: int, area: str | None, **extra) -> None:
    hk_fake.tasks[task_id] = {"id": task_id, "area": area, "title": f"task {task_id}", **extra}


# ---------------------------------------------------------------------------
# task_ref -> hk id resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "want"),
    [
        ("1943", 1943),
        (" 1943 ", 1943),
        ("hk 1943", 1943),
        ("HK 1943", 1943),
        ("hk1943", 1943),
        ("hk#1943", 1943),
        ("hk-1943", 1943),
        ("hk:1943", 1943),
        ("rework of hk 1943", 1943),
        ("hk 1943 (hk 1943)", 1943),  # the same id twice still resolves to one
        ("ROB-1943", None),
        ("MGH-36", None),
        ("B0X-US-SLOT1", None),
        ("PR#42", None),
        ("https://github.com/mgh3326/scopefuel/pull/185", None),
        ("hk 1835 hk 1836", None),  # several hk ids — ambiguous, never guessed
        ("hk 1835 + hk1836", None),
        ("", None),
        (None, None),
        ("no task", None),
    ],
)
def test_parse_hk_task_ref(ref, want):
    assert areas.parse_hk_task_ref(ref) == want


# ---------------------------------------------------------------------------
# grades areas — assembly
# ---------------------------------------------------------------------------


def test_area_join_through_task(hk, tmp_path, isolated_cache):
    """A rep's area is its task's area at aggregation time."""
    _task(hk, 1001, "python")
    _task(hk, 1002, "trading")
    hk.reps = [
        _rep(1, "hk 1001", rounds=2),
        _rep(2, "1001", rounds=4),  # same task via a bare-id spelling
        _rep(3, "hk 1002", rounds=6),
    ]
    report = areas.gather_areas(view=_view(_cat_entry("devin-swe2", "swe-2")))
    table = _table(report, "A", "builder")
    cell = _cell(table, "python", "swe-2")
    assert cell is not None and cell.n == 1 and cell.mean_rounds == 4.0
    # Two reps of one task score once — the later recorded rep is final.
    assert _cell(table, "trading", "swe-2").n == 1


def test_unlinked_and_untagged_rows(hk, tmp_path, isolated_cache):
    """Unresolvable refs and untagged tasks land in their own rows, never guessed."""
    _task(hk, 1001, None)  # resolves, carries no area
    hk.reps = [
        _rep(1, "hk 1001"),
        _rep(2, "ROB-1194"),
        _rep(3, "https://github.com/mgh3326/scopefuel/pull/185"),
        _rep(4, "hk 9999"),  # resolves to an id but the server has no such task
        _rep(5, "hk 1835 hk 1836"),  # ambiguous — several hk ids
    ]
    report = areas.gather_areas(view=_view(_cat_entry("devin-swe2", "swe-2")))
    table = _table(report, "A", "builder")
    assert _cell(table, "untagged", "swe-2").n == 1
    assert _cell(table, "unlinked", "swe-2").n == 4
    # No real area row may carry them.
    assert all(_cell(table, name, "swe-2") is None for name in AREAS)


def test_current_only_filters_by_catalog_model_ids(hk, tmp_path, isolated_cache):
    """--current-only keeps reps whose model id a live catalog row carries.

    A retired row's model id counts as current only when a live row uses it
    (the include_retired fix): here ``old-gen`` appears on a retired row only,
    while ``shared`` rides both a retired and a live row.
    """
    _task(hk, 1001, "python")
    view = _view(
        _cat_entry("devin-swe2", "swe-2"),
        _cat_entry("old-profile", "old-gen", retired_at="2026-09-01T00:00:00Z"),
        _cat_entry("retired-twin", "shared", retired_at="2026-09-01T00:00:00Z"),
        _cat_entry("live-twin", "shared"),
    )
    hk.reps = [
        _rep(1, "hk 1001", model_id="swe-2"),
        _rep(2, "hk 1001", model_id="old-gen"),
        _rep(3, "hk 1001", model_id="shared"),
        _rep(4, "hk 1001", model_id=None),  # no recorded model — never current
        _rep(5, "hk 1001", model_id="ghost"),  # absent from the catalog entirely
    ]
    report = areas.gather_areas(view=view)
    assert report.current_only and report.excluded_non_current == 3
    table = _table(report, "A", "builder")
    cell = _cell(table, "python", "swe-2")
    assert cell is not None and cell.n == 1
    assert _cell(table, "python", "shared").n == 1
    assert _cell(table, "python", "old-gen") is None
    assert _cell(table, "python", "ghost") is None


def test_current_only_off_keeps_everything(hk, tmp_path, isolated_cache):
    _task(hk, 1001, "python")
    hk.reps = [_rep(1, "hk 1001", model_id="old-gen")]
    report = areas.gather_areas(
        view=_view(_cat_entry("devin-swe2", "swe-2")),
        current_only=False,
    )
    assert report.excluded_non_current == 0
    assert _cell(_table(report, "A", "builder"), "python", "old-gen").n == 1


def test_band_split(hk, tmp_path, isolated_cache):
    """A and A+ form one band, S and S+ another — the merge-bands mutant dies."""
    _task(hk, 1001, "python")
    _task(hk, 1002, "python")
    _task(hk, 1003, "python")
    hk.reps = [
        _rep(1, "hk 1001", grade="A"),
        _rep(2, "hk 1002", grade="A+"),
        _rep(3, "hk 1003", grade="S+"),
    ]
    report = areas.gather_areas(view=_view(_cat_entry("devin-swe2", "swe-2")))
    band_a = _table(report, "A", "builder")
    band_s = _table(report, "S", "builder")
    cell_a = _cell(band_a, "python", "swe-2")
    assert cell_a is not None and cell_a.n == 2  # the A and A+ tasks, not the S+ one
    cell_s = _cell(band_s, "python", "swe-2")
    assert cell_s is not None and cell_s.n == 1


def test_unbanded_and_other_roles_counted(hk, tmp_path, isolated_cache):
    _task(hk, 1001, "python")
    hk.reps = [
        _rep(1, "hk 1001", grade=None),
        _rep(2, "hk 1001", role="verify", grade=None),
        _rep(3, "hk 1001", role="orch"),
        _rep(4, "hk 1001", role="fix"),  # builder roles include fix
    ]
    report = areas.gather_areas(view=_view(_cat_entry("devin-swe2", "swe-2")))
    assert report.unbanded_builder == 1 and report.unbanded_tester == 1
    assert report.other_roles == 1
    assert _cell(_table(report, "A", "builder"), "python", "swe-2").n == 1


def test_clean_pct_and_mean_rounds(hk, tmp_path, isolated_cache):
    _task(hk, 1001, "python")
    _task(hk, 1002, "python")
    hk.reps = [
        _rep(1, "hk 1001", rounds=2, blockers_found=0, completed=1),  # clean
        _rep(2, "hk 1002", rounds=4, blockers_found=1, completed=1),  # not clean
        _rep(3, "hk 1003", rounds=None, blockers_found=2, completed=0),  # unlinked task
    ]
    report = areas.gather_areas(view=_view(_cat_entry("devin-swe2", "swe-2")))
    cell = _cell(_table(report, "A", "builder"), "python", "swe-2")
    assert cell.n == 2
    assert cell.clean_pct == 50.0
    assert cell.mean_rounds == 3.0
    unlinked = _cell(_table(report, "A", "builder"), "unlinked", "swe-2")
    assert unlinked.n == 1 and unlinked.mean_rounds is None


def test_eligibility_flag_at_4_and_5(hk, tmp_path, isolated_cache):
    """Packet section 3: a cell is eligible at 5+ scored tasks, not at 4."""
    for task_id in range(1001, 1006):
        _task(hk, task_id, "python")
    for task_id in range(2001, 2005):
        _task(hk, task_id, "trading")
    hk.reps = [_rep(i, f"hk {1000 + i}", grade="A") for i in range(1, 6)] + [
        _rep(10 + i, f"hk {2000 + i}", grade="A") for i in range(1, 5)
    ]
    report = areas.gather_areas(view=_view(_cat_entry("devin-swe2", "swe-2")))
    table = _table(report, "A", "builder")
    five = _cell(table, "python", "swe-2")
    four = _cell(table, "trading", "swe-2")
    assert five.n == 5 and five.eligible
    assert four.n == 4 and not four.eligible


def test_tester_reps_are_a_separate_table(hk, tmp_path, isolated_cache):
    _task(hk, 1001, "python")
    hk.reps = [
        _rep(1, "hk 1001", role="impl"),
        _rep(2, "hk 1001", role="verify"),
    ]
    report = areas.gather_areas(view=_view(_cat_entry("devin-swe2", "swe-2")))
    assert _cell(_table(report, "A", "builder"), "python", "swe-2").n == 1
    assert _cell(_table(report, "A", "tester"), "python", "swe-2").n == 1


def test_band_filter(hk, tmp_path, isolated_cache):
    _task(hk, 1001, "python")
    hk.reps = [_rep(1, "hk 1001", grade="A"), _rep(2, "hk 1001", grade="S")]
    report = areas.gather_areas(view=_view(_cat_entry("devin-swe2", "swe-2")), bands=("S",))
    assert {t.band for t in report.tables} == {"S"}


def test_grades_areas_requires_handoffkeep_backend(tmp_path, isolated_cache):
    with pytest.raises(bench.BenchError):
        areas.gather_areas(view=_view(_cat_entry("devin-swe2", "swe-2")))


# ---------------------------------------------------------------------------
# PA reviews
# ---------------------------------------------------------------------------


def _pa_file(tmp_path, rows, *, fmt="json") -> str:
    path = tmp_path / "pa-reviews.out"
    if fmt == "json":
        path.write_text(json.dumps({"tasks": rows}), encoding="utf-8")
    else:
        keys = ["task", "job", "outcome", "counted"]
        lines = ["\t".join(keys)]
        for row in rows:
            lines.append("\t".join(str(row.get(key, "")) for key in keys))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("fmt", ["json", "tsv"])
def test_pa_reviews_accepted_pct(hk, tmp_path, isolated_cache, fmt):
    """accepted % counts accepted + rework against not-accepted; excluded is uncounted."""
    for task_id in (1001, 1002, 1003, 1004):
        _task(hk, task_id, "blender")
    hk.reps = [
        _rep(1, "hk 1001", grade="A"),
        _rep(2, "hk 1002", grade="A"),
        _rep(3, "hk 1003", grade="A"),
        _rep(4, "hk 1004", grade="A"),
    ]
    pa = _pa_file(
        tmp_path,
        [
            {"task": 1001, "job": "b1", "outcome": "accepted"},
            {"task": 1002, "job": "b2", "outcome": "rework"},
            {"task": 1003, "job": "b3", "outcome": "not-accepted"},
            {"task": 1004, "job": "b4", "outcome": "excluded", "counted": False},
        ],
        fmt=fmt,
    )
    report = areas.gather_areas(view=_view(_cat_entry("devin-swe2", "swe-2")), pa_path=pa)
    cell = _cell(_table(report, "A", "builder"), "blender", "swe-2")
    assert cell.pa is not None
    assert cell.pa.scored == 3 and cell.pa.accepted == 2 and cell.pa.not_accepted == 1
    assert cell.pa.accepted_pct == pytest.approx(66.7, abs=0.1)
    assert report.pa_task_rows == 4
    assert len(report.pa_uncounted) == 1 and report.pa_uncounted[0].task == 1004
    assert report.pa_unattributed == []


def test_pa_task_with_two_reps_scores_once(hk, tmp_path, isolated_cache):
    """Mutant invariant: in PA mode a task scores once whatever its rep count."""
    _task(hk, 1001, "blender")
    hk.reps = [
        _rep(1, "hk 1001", grade="A", recorded_at="2026-10-10T10:00:00Z"),
        _rep(2, "hk 1001", grade="A", model_id="shared", recorded_at="2026-10-20T10:00:00Z"),
    ]
    pa = _pa_file(tmp_path, [{"task": 1001, "job": "b1", "outcome": "accepted"}])
    view = _view(_cat_entry("devin-swe2", "swe-2"), _cat_entry("twin", "shared"))
    report = areas.gather_areas(view=view, pa_path=pa)
    pa_cells = [
        cell
        for row in _table(report, "A", "builder").rows
        for cell in row.cells
        if cell.pa is not None and cell.pa.scored
    ]
    assert len(pa_cells) == 1 and pa_cells[0].pa.scored == 1
    # The final rep attributes the task — the later recorded one, model shared.
    assert pa_cells[0].model == "shared"


def test_pa_unattributed_rows(hk, tmp_path, isolated_cache):
    """A task with no impl/fix rep is listed as unattributed — never guessed."""
    _task(hk, 1001, "blender")
    _task(hk, 1002, None)
    hk.reps = [
        _rep(1, "hk 1001", role="verify"),  # a tester rep never attributes a builder review
        _rep(2, "hk 1002", grade="A"),
    ]
    pa = _pa_file(
        tmp_path,
        [
            {"task": 1001, "job": "b1", "outcome": "accepted"},
            {"task": 1002, "job": "b2", "outcome": "rework"},
            {"task": 1999, "job": "b3", "outcome": "not-accepted"},  # no such hk task
        ],
    )
    report = areas.gather_areas(view=_view(_cat_entry("devin-swe2", "swe-2")), pa_path=pa)
    assert len(report.pa_unattributed) == 3
    reasons = {row.task: row.reason for row in report.pa_unattributed}
    assert reasons[1001] == "no-impl-rep"
    assert reasons[1002] == "task-untagged"
    assert reasons[1999] == "task-unlinked"
    # The attributed-but-untagged task scores nowhere.
    assert all(
        cell.pa is None or not cell.pa.scored
        for row in _table(report, "A", "builder").rows
        for cell in row.cells
    )


def test_pa_reviews_bad_file_is_an_error(hk, tmp_path, isolated_cache):
    bad = tmp_path / "bad.json"
    bad.write_text('{"tasks": [{"task": "not-an-int", "outcome": "accepted"}]}', encoding="utf-8")
    with pytest.raises(bench.BenchError):
        areas.gather_areas(view=_view(), pa_path=str(bad))
    missing = tmp_path / "missing.tsv"
    with pytest.raises((bench.BenchError, OSError)):
        areas.gather_areas(view=_view(), pa_path=str(missing))


def test_pa_eligibility_uses_pa_counts(hk, tmp_path, isolated_cache):
    for task_id in range(1001, 1006):
        _task(hk, task_id, "blender")
    hk.reps = [_rep(i, f"hk {1000 + i}", grade="A") for i in range(1, 6)]
    pa = _pa_file(tmp_path, [{"task": 1000 + i, "job": f"b{i}", "outcome": "accepted"} for i in range(1, 6)])
    report = areas.gather_areas(view=_view(_cat_entry("devin-swe2", "swe-2")), pa_path=pa)
    cell = _cell(_table(report, "A", "builder"), "blender", "swe-2")
    assert cell.pa.scored == 5 and cell.pa.eligible


# ---------------------------------------------------------------------------
# JSON schema
# ---------------------------------------------------------------------------


def test_json_schema_stable(hk, tmp_path, isolated_cache):
    """The --json contract: fixed keys so K5/K6 can read the same shape."""
    _task(hk, 1001, "python")
    _task(hk, 1002, None)
    hk.reps = [
        _rep(1, "hk 1001", grade="A"),
        _rep(2, "hk 1002", grade="S", role="verify"),
        _rep(3, "ROB-9", grade="A"),
    ]
    pa = _pa_file(tmp_path, [{"task": 1001, "job": "b1", "outcome": "accepted"}])
    report = areas.gather_areas(view=_view(_cat_entry("devin-swe2", "swe-2")), pa_path=pa)
    payload = areas.areas_to_json(report)
    assert payload["version"] == 1
    assert set(payload) >= {
        "version",
        "backend",
        "catalog",
        "vocabulary",
        "current_only",
        "excluded_non_current",
        "bands",
        "unbanded",
        "pa_reviews",
    }
    band_a = payload["bands"]["A"]
    assert set(band_a) == {"builder", "tester"}
    table = band_a["builder"]
    assert table["models"] == ["swe-2"]
    row = next(r for r in table["rows"] if r["area"] == "python")
    cell = next(c for c in row["cells"] if c["model"] == "swe-2")
    assert set(cell) >= {"model", "n", "clean_pct", "mean_rounds", "eligible", "pa"}
    assert cell["n"] == 1 and cell["clean_pct"] == 100.0 and cell["pa"]["scored"] == 1
    assert payload["pa_reviews"]["unattributed"] == []
    assert payload["pa_reviews"]["task_rows"] == 1
    assert payload["unbanded"] == {"builder": 0, "tester": 0, "other": 0}


# ---------------------------------------------------------------------------
# reps compare --area
# ---------------------------------------------------------------------------


def test_compare_reps_area_filter(hk, tmp_path, isolated_cache):
    _task(hk, 1001, "python")
    _task(hk, 1002, "trading")
    hk.reps = [
        _rep(1, "hk 1001", grade="A", profile="devin-swe2"),
        _rep(2, "hk 1002", grade="A", profile="devin-swe2"),
        _rep(3, "ROB-1", grade="A", profile="devin-swe2"),
    ]
    lookup = areas.TaskAreaLookup(bench.bench_backend(use="reps"))
    comparisons = bench.compare_reps(grade="A", area="python", rep_area=lookup.area_for_ref)
    assert len(comparisons) == 1
    assert comparisons[0].profile == "devin-swe2" and comparisons[0].count == 1


def test_compare_area_cli(hk, tmp_path, isolated_cache, capsys):
    _task(hk, 1001, "python")
    hk.reps = [
        _rep(1, "hk 1001", grade="A", profile="devin-swe2"),
        _rep(2, "hk 1002", grade="A", profile="devin-swe2"),  # unlinked
    ]
    rc = cli.main(["reps", "compare", "--grade", "A", "--area", "python"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "count=1" in out


def test_compare_area_unknown_area_rejected(hk, tmp_path, isolated_cache, capsys):
    rc = cli.main(["reps", "compare", "--grade", "A", "--area", "pythno"])
    assert rc == 2
    assert "pythno" in capsys.readouterr().err


def test_compare_area_unlinked_pseudo_area(hk, tmp_path, isolated_cache, capsys):
    """--area unlinked|untagged inspect the pseudo rows the table shows."""
    _task(hk, 1001, None)
    hk.reps = [_rep(1, "hk 1001", grade="A"), _rep(2, "ROB-1", grade="A")]
    rc = cli.main(["reps", "compare", "--grade", "A", "--area", "unlinked"])
    assert rc == 0
    assert "count=1" in capsys.readouterr().out
    rc = cli.main(["reps", "compare", "--grade", "A", "--area", "untagged"])
    assert rc == 0
    assert "count=1" in capsys.readouterr().out


def test_grades_areas_cli_json(hk, tmp_path, isolated_cache, capsys, monkeypatch):
    _task(hk, 1001, "python")
    hk.reps = [_rep(1, "hk 1001", grade="A")]
    monkeypatch.setattr(
        bench,
        "read_catalog",
        lambda **kw: _view(_cat_entry("devin-swe2", "swe-2")),
    )
    rc = cli.main(["grades", "areas", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == 1 and "A" in payload["bands"]


def test_grades_areas_cli_text(hk, tmp_path, isolated_cache, capsys, monkeypatch):
    _task(hk, 1001, "python")
    hk.reps = [_rep(1, "hk 1001", grade="A", rounds=2)]
    monkeypatch.setattr(
        bench,
        "read_catalog",
        lambda **kw: _view(_cat_entry("devin-swe2", "swe-2")),
    )
    rc = cli.main(["grades", "areas"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "python" in out and "swe-2" in out and "band A" in out


def test_grades_areas_cli_requires_hk(tmp_path, isolated_cache, capsys):
    rc = cli.main(["grades", "areas"])
    assert rc == 2
    assert "handoffkeep" in capsys.readouterr().err
