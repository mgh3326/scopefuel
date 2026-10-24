"""hk 1943 (K4) — per-area views over reps (b953 packet sections 2, 3 and 4b).

A rep's area is its task's area at aggregation time, read through the
handoffkeep transport and cached for the run — there is no rep schema
change, so a later fix to a task tag fixes all of its reps. A rep whose
task ref does not resolve to one hk task, or whose task carries no area,
lands in an ``unlinked`` or ``untagged`` row and is never guessed. Every
view here is read-only: nothing writes a rep, a catalog row or an
exception.

Two surfaces:

* ``reps compare --area A`` — the existing profile compare filtered to
  reps whose task carries that area (``TaskAreaLookup.area_for_ref``).
* ``grades areas`` — the packet's section-2 tables reproducible from the
  canon: area by model cells holding n, clean % (completed with 0
  blockers), mean rounds, and accepted % when ``--pa-reviews`` gives the
  K3 pa-review-scan output. Builder roles (impl, fix) and the tester role
  (verify) are separate tables, every table is inside one grade band (A
  with A+, S with S+), and a cell is marked eligible only — never
  "exclude" or "prefer" — when it holds 5 or more scored tasks.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from dataclasses import dataclass, field

from . import bench
from .http import HttpError

# ---------------------------------------------------------------------------
# Vocabulary and bands
# ---------------------------------------------------------------------------

# Pseudo-area rows: a rep whose task ref resolves to no single hk task is
# ``unlinked``; a resolved task with no area is ``untagged``. Both are counted
# in the tables, never guessed into a real area.
AREA_UNLINKED = "unlinked"
AREA_UNTAGGED = "untagged"
PSEUDO_AREAS = (AREA_UNTAGGED, AREA_UNLINKED)

BUILDER_ROLES = frozenset({"impl", "fix"})
TESTER_ROLE = "verify"

# Packet section 3: A and A+ are one band, S and S+ another; the remaining
# grades are their own band.
GRADE_BAND = {"S+": "S", "S": "S", "A+": "A", "A": "A", "B": "B", "C": "C"}
BAND_ORDER = ("S", "A", "B", "C")
_BAND_GRADES = {"S": ("S", "S+"), "A": ("A", "A+"), "B": ("B",), "C": ("C",)}

# Packet section 3's minimum-sample count: a cell is eligible at 5+ scored
# tasks in the band. The view shows eligibility only.
MIN_CELL_SCORED = 5

PA_OUTCOMES = frozenset({"accepted", "rework", "not-accepted", "excluded"})
_PA_ACCEPTED = frozenset({"accepted", "rework"})
_COUNTED_TRUE = {"1", "true", "yes", "counted"}
_COUNTED_FALSE = {"0", "false", "no", "uncounted"}

_MISSING = object()  # task id resolved but the server has no such task

# ---------------------------------------------------------------------------
# task_ref -> hk id
# ---------------------------------------------------------------------------

_BARE_TASK_ID_RE = re.compile(r"\d+")
_HK_TASK_ID_RE = re.compile(r"(?i)\bhk[\s:#-]*(\d+)\b")


def parse_hk_task_ref(task_ref: str | None) -> int | None:
    """The one hk task id a rep's task_ref names, or None.

    A bare positive integer is an hk id; so is exactly one ``hk``-tagged id
    (``hk 1943``, ``hk1943``, ``hk#1943``, ``hk-1943``, ``hk:1943``, case
    insensitive — the same id twice still resolves). A PR URL, a ROB-/MGH-/
    B0X- id, an empty ref, or a ref carrying several distinct hk ids resolves
    to nothing: it lands in ``unlinked``, never guessed.
    """

    if task_ref is None:
        return None
    text = task_ref.strip()
    if not text:
        return None
    if _BARE_TASK_ID_RE.fullmatch(text):
        return int(text) or None
    ids = {int(match.group(1)) for match in _HK_TASK_ID_RE.finditer(text)}
    ids.discard(0)
    return ids.pop() if len(ids) == 1 else None


# ---------------------------------------------------------------------------
# handoffkeep task transport — one fetch per task per run
# ---------------------------------------------------------------------------


class TaskAreaLookup:
    """Run-scoped hk task reads through the existing bench transport.

    ``GET /v1/tasks/{id}`` supplies each task's area; ``GET /v1/task-areas``
    supplies the closed vocabulary (K1). Every id is fetched at most once —
    the packet's "cached for the run" — and a 404 on the vocabulary means a
    pre-K1 server, which is disclosed as ``vocabulary() is None`` rather than
    an error. A task that 404s is ``unlinked``.
    """

    def __init__(self, backend: bench.BenchBackend):
        if backend.name != bench.BENCH_BACKEND_HANDOFFKEEP:
            raise bench.BenchBackendError(
                f"area views require the handoffkeep backend (resolved {backend.name}/{backend.reason}; "
                "needs HANDOFFKEEP_URL+HANDOFFKEEP_TOKEN)"
            )
        self._backend = backend
        self._tasks: dict[int, str | None] = {}
        self._missing: set[int] = set()
        self._vocabulary: tuple[str, ...] | None = None
        self._vocabulary_done = False

    def _get(self, path: str) -> dict:
        url = self._backend.url or ""
        bench._check_handoffkeep_scheme(url, allow_plaintext=self._backend.allow_plaintext_url, use="reps")
        payload = bench._handoffkeep_send(
            f"{url.rstrip('/')}{path}", token=self._backend.token or "", timeout=20.0
        )
        if not isinstance(payload, dict):
            raise bench.BenchBackendError("handoffkeep returned an invalid response")
        return payload

    def vocabulary(self) -> tuple[str, ...] | None:
        if self._vocabulary_done:
            return self._vocabulary
        self._vocabulary_done = True
        try:
            payload = self._get("/v1/task-areas")
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise
        values = payload.get("areas")
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise bench.BenchBackendError("handoffkeep returned invalid task-area data")
        self._vocabulary = tuple(values)
        return self._vocabulary

    def task_area(self, task_id: int) -> str | None | object:
        """The task's area, ``None`` when untagged, ``_MISSING`` when no such task."""

        if task_id in self._tasks:
            return self._tasks[task_id]
        if task_id in self._missing:
            return _MISSING
        try:
            payload = self._get(f"/v1/tasks/{task_id}")
        except HttpError as exc:
            if exc.status == 404:
                self._missing.add(task_id)
                return _MISSING
            raise
        area = payload.get("area")
        if area is not None and not isinstance(area, str):
            raise bench.BenchBackendError("handoffkeep returned invalid task data")
        self._tasks[task_id] = area or None
        return self._tasks[task_id]

    def resolve_ref(self, task_ref: str | None) -> tuple[int | None, str]:
        """(hk task id or None, area label) for one rep task_ref.

        The label is a real area or a pseudo-area; the id feeds task-level
        collapsing — a ref that parses to a 404 keeps its id (same task,
        same group) while an unparseable ref keeps only its text.
        """

        task_id = parse_hk_task_ref(task_ref)
        if task_id is None:
            return None, AREA_UNLINKED
        area = self.task_area(task_id)
        if area is _MISSING:
            return task_id, AREA_UNLINKED
        if area is None:
            return task_id, AREA_UNTAGGED
        return task_id, area

    def area_for_ref(self, task_ref: str | None) -> str:
        """The rep's area label for the ``reps compare --area`` filter."""

        return self.resolve_ref(task_ref)[1]


def task_area_lookup(*, allow_plaintext_http: bool = False) -> TaskAreaLookup:
    """The lookup for the configured reps backend — handoffkeep or an error."""

    return TaskAreaLookup(bench.bench_backend(use="reps", allow_plaintext_http=allow_plaintext_http))


# ---------------------------------------------------------------------------
# PA reviews (K3 pa-review-scan output — an input file, no agent-skills import)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaReview:
    task: int
    job: str
    outcome: str  # accepted | rework | not-accepted | excluded
    counted: bool


@dataclass(frozen=True)
class PaUnattributed:
    task: int
    job: str
    outcome: str
    reason: str


def _pa_task_id(value: object, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise bench.BenchError(f"pa-reviews {where}: task must be a positive integer")
    return value


def _pa_outcome(value: object, *, where: str) -> str:
    if not isinstance(value, str) or value not in PA_OUTCOMES:
        raise bench.BenchError(f"pa-reviews {where}: outcome must be one of {', '.join(sorted(PA_OUTCOMES))}")
    return value


def _pa_counted(flag: object, outcome: str, *, where: str) -> bool:
    """Whether a PA row counts. ``excluded`` never does — the uncounted
    reading always wins; an explicit flag is honoured otherwise."""

    base = outcome != "excluded"
    if flag is None or flag == "":
        return base
    if isinstance(flag, bool):
        return flag and base
    if isinstance(flag, int):
        return bool(flag) and base
    text = str(flag).strip().lower()
    if text in _COUNTED_TRUE:
        return base
    if text in _COUNTED_FALSE:
        return False
    raise bench.BenchError(f"pa-reviews {where}: unrecognised counted value")


def _pa_from_json(payload: object) -> list[PaReview]:
    if isinstance(payload, dict):
        payload = payload.get("tasks")
    if not isinstance(payload, list):
        raise bench.BenchError('pa-reviews JSON must be a list or {"tasks": [...]}')
    rows: list[PaReview] = []
    for index, value in enumerate(payload):
        where = f"row {index}"
        if not isinstance(value, dict):
            raise bench.BenchError(f"pa-reviews {where}: must be an object")
        task = value.get("task", value.get("task_id"))
        outcome = _pa_outcome(value.get("outcome"), where=where)
        job = value.get("job")
        rows.append(
            PaReview(
                task=_pa_task_id(task, where=where),
                job=str(job) if job is not None else "",
                outcome=outcome,
                counted=_pa_counted(value.get("counted", value.get("scored")), outcome, where=where),
            )
        )
    return rows


def _pa_from_tsv(text: str) -> list[PaReview]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise bench.BenchError("pa-reviews file is empty")
    header = [name.strip() for name in lines[0].split("\t")]
    columns = {name: index for index, name in enumerate(header)}
    task_col = next((columns[name] for name in ("task", "task_id") if name in columns), None)
    if task_col is None or "outcome" not in columns:
        raise bench.BenchError("pa-reviews TSV needs task and outcome columns")
    outcome_col = columns["outcome"]
    job_col = columns.get("job")
    counted_col = columns.get("counted", columns.get("scored"))
    rows: list[PaReview] = []
    for index, line in enumerate(lines[1:], start=2):
        where = f"line {index}"
        fields = line.split("\t")
        if len(fields) <= max(task_col, outcome_col):
            raise bench.BenchError(f"pa-reviews {where}: too few columns")
        try:
            task = _pa_task_id(int(fields[task_col].strip()), where=where)
        except ValueError:
            raise bench.BenchError(f"pa-reviews {where}: task must be a positive integer") from None
        outcome = _pa_outcome(fields[outcome_col].strip(), where=where)
        flag = fields[counted_col].strip() if counted_col is not None and counted_col < len(fields) else None
        job = fields[job_col].strip() if job_col is not None and job_col < len(fields) else ""
        rows.append(
            PaReview(task=task, job=job, outcome=outcome, counted=_pa_counted(flag, outcome, where=where))
        )
    return rows


def parse_pa_reviews(path: str | pathlib.Path) -> list[PaReview]:
    """K3 pa-review-scan output: TSV (header line required) or JSON.

    Both shapes carry one final row per task: task id, job, outcome and an
    optional counted flag. A task repeated in the file keeps its last row —
    K3's own "the last line per job is final" at file level.
    """

    text = pathlib.Path(path).read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        rows = _pa_from_json(json.loads(text))
    else:
        rows = _pa_from_tsv(text)
    final: dict[int, PaReview] = {}
    order: list[int] = []
    for row in rows:
        if row.task not in final:
            order.append(row.task)
        final[row.task] = row
    return [final[task] for task in order]


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaCell:
    scored: int
    accepted: int
    not_accepted: int
    accepted_pct: float | None
    eligible: bool


@dataclass(frozen=True)
class AreaCell:
    model: str
    n: int
    clean_pct: float
    mean_rounds: float | None
    eligible: bool
    pa: PaCell | None = None


@dataclass(frozen=True)
class AreaTotal:
    n: int
    clean_pct: float
    mean_rounds: float | None
    eligible: bool
    pa: PaCell | None = None


@dataclass(frozen=True)
class AreaRow:
    area: str
    cells: tuple[AreaCell, ...]
    total: AreaTotal


@dataclass(frozen=True)
class AreaTable:
    band: str
    role: str  # "builder" | "tester"
    models: tuple[str, ...]
    rows: tuple[AreaRow, ...]


@dataclass
class AreasReport:
    backend_name: str
    backend_reason: str
    catalog_source: str
    vocabulary: tuple[str, ...] | None
    current_only: bool
    excluded_non_current: int
    tables: list[AreaTable] = field(default_factory=list)
    unbanded_builder: int = 0
    unbanded_tester: int = 0
    other_roles: int = 0
    pa_path: str | None = None
    pa_task_rows: int = 0
    pa_unattributed: list[PaUnattributed] = field(default_factory=list)
    pa_uncounted: list[PaReview] = field(default_factory=list)


def _counted(rep: bench.RepRecord) -> bool:
    return rep.pending_state is None and not bench.rep_ref(rep).startswith(("lost:", "origin:"))


def _role_group(rep: bench.RepRecord) -> str:
    if rep.role in BUILDER_ROLES:
        return "builder"
    if rep.role == TESTER_ROLE:
        return "tester"
    return "other"


def _ref_id(rep: bench.RepRecord) -> int:
    try:
        return int(bench.rep_ref(rep).rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return -1


def _recency(rep: bench.RepRecord) -> tuple:
    """Latest-rep ordering for one task: true instant, then store id."""

    stamp = bench._recorded_at_key(rep.recorded_at or "")
    if isinstance(stamp, dt.datetime):
        aware = stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.UTC)
        return (aware.timestamp(), "", _ref_id(rep))
    return (float("-inf"), str(stamp), _ref_id(rep))


def _task_key(rep: bench.RepRecord, task_id: int | None) -> tuple:
    if task_id is not None:
        return ("hk", task_id)
    text = (rep.task_ref or "").strip()
    if text:
        return ("ref", text)
    return ("rep", bench.rep_ref(rep))


def _finals(reps: list[tuple[bench.RepRecord, int | None]]) -> list[bench.RepRecord]:
    """One scored task per rep group — the latest rep is the task's outcome."""

    groups: dict[tuple, list[bench.RepRecord]] = {}
    for rep, task_id in reps:
        groups.setdefault(_task_key(rep, task_id), []).append(rep)
    return [max(group, key=_recency) for group in groups.values()]


def _cell_stats(finals: list[bench.RepRecord]) -> tuple[int, float, float | None]:
    n = len(finals)
    clean = sum(1 for rep in finals if rep.completed == 1 and rep.blockers_found == 0)
    rounds = [float(rep.rounds) for rep in finals if rep.rounds is not None]
    return n, (clean / n) * 100.0, (sum(rounds) / len(rounds) if rounds else None)


def _pa_cell(rows: list[PaReview]) -> PaCell:
    scored = len(rows)
    accepted = sum(1 for row in rows if row.outcome in _PA_ACCEPTED)
    not_accepted = scored - accepted
    return PaCell(
        scored=scored,
        accepted=accepted,
        not_accepted=not_accepted,
        accepted_pct=(accepted / scored) * 100.0 if scored else None,
        eligible=scored >= MIN_CELL_SCORED,
    )


def _model_order(view: bench.CatalogView, models: set[str]) -> list[str]:
    """Catalog order first (first row carrying the model), then the rest."""

    index: dict[str, int] = {}
    for position, entry in enumerate(view.entries):
        if entry.model_id and entry.model_id not in index:
            index[entry.model_id] = position
    return sorted(models, key=lambda m: (index.get(m, len(view.entries)), m))


def gather_areas(
    *,
    view: bench.CatalogView | None = None,
    current_only: bool = True,
    pa_path: str | None = None,
    bands: tuple[str, ...] | None = None,
    allow_plaintext_http: bool = False,
    path: pathlib.Path | str | None = None,
) -> AreasReport:
    """Assemble the per-area tables from the canon — read-only end to end."""

    backend = bench.bench_backend(use="reps", allow_plaintext_http=allow_plaintext_http)
    lookup = TaskAreaLookup(backend)
    vocabulary = lookup.vocabulary()
    if view is None:
        view = bench.read_catalog(commit_cache=False, allow_plaintext_http=allow_plaintext_http)

    selected = BAND_ORDER if bands is None else tuple(bands)
    for band in selected:
        if band not in GRADE_BAND.values():
            raise bench.BenchError(f"unknown band {band!r} — bands are {', '.join(BAND_ORDER)}")

    current_models = {entry.model_id for entry in view.entries if entry.model_id and not entry.retired_at}

    # (group, band, area label, model) -> [(rep, task_id)]; resolution happens
    # only for reps that reach a cell — unbanded/other-role reps never fetch.
    grouped: dict[tuple[str, str, str, str], list[tuple[bench.RepRecord, int | None]]] = {}
    rep_task_id: dict[int, int | None] = {}
    excluded_non_current = 0
    unbanded_builder = unbanded_tester = other_roles = 0
    reps = [rep for rep in bench.read_reps(path=path) if _counted(rep)]
    for rep in reps:
        if current_only and rep.model_id not in current_models:
            excluded_non_current += 1
            continue
        group = _role_group(rep)
        if group == "other":
            other_roles += 1
            continue
        band = GRADE_BAND.get(rep.grade or "")
        if band is None:
            if group == "builder":
                unbanded_builder += 1
            else:
                unbanded_tester += 1
            continue
        task_id, label = lookup.resolve_ref(rep.task_ref)
        rep_task_id[id(rep)] = task_id
        grouped.setdefault((group, band, label, rep.model_id or "-"), []).append((rep, task_id))

    # PA reviews: attribute each counted task outcome through the task's final
    # impl/fix rep — the task scores once whatever its rep count — and list
    # every row that cannot be placed rather than guessing.
    pa_rows = parse_pa_reviews(pa_path) if pa_path else []
    pa_stats: dict[tuple[str, str, str], list[PaReview]] = {}
    pa_unattributed: list[PaUnattributed] = []
    pa_uncounted: list[PaReview] = []

    # Builder reps by resolved task id for PA attribution.
    impl_by_task: dict[int, list[bench.RepRecord]] = {}
    for rep in reps:
        if rep.role in BUILDER_ROLES:
            task_id = rep_task_id.get(id(rep))
            if task_id is None:
                task_id = parse_hk_task_ref(rep.task_ref)
            if task_id is not None:
                impl_by_task.setdefault(task_id, []).append(rep)

    for row in pa_rows:
        if not row.counted or row.outcome == "excluded":
            pa_uncounted.append(row)
            continue
        candidates = impl_by_task.get(row.task, [])
        area = lookup.task_area(row.task)
        reason = ""
        final = None
        if area is _MISSING:
            reason = "task-unlinked"
        elif area is None:
            reason = "task-untagged"
        elif not candidates:
            reason = "no-impl-rep"
        else:
            final = max(candidates, key=_recency)
            if not final.model_id:
                reason = "rep-no-model"
            elif current_only and final.model_id not in current_models:
                reason = "rep-model-not-current"
            elif GRADE_BAND.get(final.grade or "") is None:
                reason = "rep-ungraded"
        if reason:
            pa_unattributed.append(
                PaUnattributed(task=row.task, job=row.job, outcome=row.outcome, reason=reason)
            )
            continue
        band = GRADE_BAND[final.grade]
        pa_stats.setdefault((band, area, final.model_id), []).append(row)

    tables: list[AreaTable] = []
    for band in selected:
        for group in ("builder", "tester"):
            models = _model_order(view, {model for (g, b, _a, model) in grouped if g == group and b == band})
            area_names: list[str] = (
                list(vocabulary)
                if vocabulary
                else sorted(
                    {
                        label
                        for (g, b, label, _m) in grouped
                        if g == group and b == band and label not in PSEUDO_AREAS
                    }
                )
            )
            area_names += [name for name in PSEUDO_AREAS]
            rows: list[AreaRow] = []
            for area_name in area_names:
                cells: list[AreaCell] = []
                row_reps: list[tuple[bench.RepRecord, int | None]] = []
                for model in models:
                    members = grouped.get((group, band, area_name, model), [])
                    row_reps.extend(members)
                    if not members:
                        continue
                    n, clean_pct, mean_rounds = _cell_stats(_finals(members))
                    pa = None
                    if group == "builder" and pa_path:
                        pa = _pa_cell(pa_stats.get((band, area_name, model), []))
                    cells.append(
                        AreaCell(
                            model=model,
                            n=n,
                            clean_pct=clean_pct,
                            mean_rounds=mean_rounds,
                            eligible=n >= MIN_CELL_SCORED,
                            pa=pa,
                        )
                    )
                if not cells:
                    continue
                total_n, total_clean, total_rounds = _cell_stats(_finals(row_reps))
                total_pa = (
                    _pa_cell(
                        [
                            row
                            for (b, a, _m), rows_ in pa_stats.items()
                            if b == band and a == area_name
                            for row in rows_
                        ]
                    )
                    if group == "builder" and pa_path
                    else None
                )
                rows.append(
                    AreaRow(
                        area=area_name,
                        cells=tuple(cells),
                        total=AreaTotal(
                            n=total_n,
                            clean_pct=total_clean,
                            mean_rounds=total_rounds,
                            eligible=total_n >= MIN_CELL_SCORED,
                            pa=total_pa,
                        ),
                    )
                )
            tables.append(AreaTable(band=band, role=group, models=tuple(models), rows=tuple(rows)))

    return AreasReport(
        backend_name=backend.name,
        backend_reason=backend.reason,
        catalog_source=view.source,
        vocabulary=vocabulary,
        current_only=current_only,
        excluded_non_current=excluded_non_current,
        tables=tables,
        unbanded_builder=unbanded_builder,
        unbanded_tester=unbanded_tester,
        other_roles=other_roles,
        pa_path=pa_path,
        pa_task_rows=len(pa_rows),
        pa_unattributed=pa_unattributed,
        pa_uncounted=pa_uncounted,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_pct(value: float | None) -> str:
    return f"{value:.0f}%" if value is not None else "-"


def _fmt_cell(cell: AreaCell | AreaTotal) -> str:
    n = f"{cell.n}{'*' if cell.eligible else ''}"
    if cell.pa is not None and cell.pa.scored:
        pa_part = f", acc {_fmt_pct(cell.pa.accepted_pct)}"
    elif cell.pa is not None:
        pa_part = ", acc -"
    else:
        pa_part = ""
    rounds = f"{cell.mean_rounds:.1f}" if cell.mean_rounds is not None else "-"
    return f"{n}: {_fmt_pct(cell.clean_pct)}{pa_part}, {rounds}"


def _fmt_table(table: AreaTable) -> list[str]:
    columns = list(table.models) + ["all"]
    rendered_rows: list[tuple[str, list[str]]] = []
    for row in table.rows:
        by_model = {cell.model: cell for cell in row.cells}
        rendered = [_fmt_cell(by_model[model]) if model in by_model else "-" for model in table.models]
        rendered.append(_fmt_cell(row.total))
        rendered_rows.append((row.area, rendered))
    area_width = max([len("area")] + [len(name) for name, _ in rendered_rows])
    widths = [
        max([len(name)] + [len(cells[index]) for _, cells in rendered_rows])
        for index, name in enumerate(columns)
    ]
    lines = [
        "  "
        + "area".ljust(area_width)
        + "  "
        + "  ".join(name.ljust(widths[index]) for index, name in enumerate(columns))
    ]
    for area_name, cells in rendered_rows:
        lines.append(
            "  "
            + area_name.ljust(area_width)
            + "  "
            + "  ".join(cells[index].ljust(widths[index]) for index in range(len(columns)))
        )
    if not table.rows:
        lines.append("  (no scored reps)")
    return lines


def render_areas(report: AreasReport) -> str:
    """The text view: one table per band per role group."""

    lines = [
        f"grades areas — reps from {report.backend_name} ({report.backend_reason}); "
        f"catalog={report.catalog_source}"
    ]
    current = "yes" if report.current_only else "no"
    lines.append(
        f"current-only: {current} — {report.excluded_non_current} rep(s) excluded on non-current models"
        if report.current_only
        else "current-only: no — all recorded model ids counted"
    )
    if report.vocabulary is None:
        lines.append("areas: server has no /v1/task-areas route — rows ordered by name")
    else:
        lines.append(f"areas: {len(report.vocabulary)} in the server vocabulary")

    for band in BAND_ORDER:
        band_tables = [table for table in report.tables if table.band == band]
        if not band_tables:
            continue
        grades = ", ".join(_BAND_GRADES[band])
        for table in band_tables:
            roles = "impl, fix" if table.role == "builder" else "verify"
            lines.append("")
            plural_t = "s" if len(_BAND_GRADES[band]) > 1 else ""
            plural_r = "s" if table.role == "builder" else ""
            lines.append(
                f"band {band} (task grade{plural_t} {grades}) — {table.role} reps (role{plural_r} {roles})"
            )
            lines += _fmt_table(table)

    lines.append("")
    lines.append(
        f"unbanded: {report.unbanded_builder} builder rep(s), {report.unbanded_tester} tester rep(s); "
        f"other roles: {report.other_roles} rep(s)"
    )
    lines.append(
        f"n* = eligible cell ({MIN_CELL_SCORED}+ scored tasks in the band); "
        "the view never says exclude or prefer"
    )

    if report.pa_path:
        lines.append("")
        lines.append(
            f"pa-reviews {report.pa_path}: {report.pa_task_rows} task outcome(s); "
            f"uncounted {len(report.pa_uncounted)}, unattributed {len(report.pa_unattributed)}"
        )
        for row in report.pa_uncounted:
            lines.append(f"  uncounted task={row.task} job={row.job or '-'} outcome={row.outcome}")
        for row in report.pa_unattributed:
            lines.append(
                f"  unattributed task={row.task} job={row.job or '-'} "
                f"outcome={row.outcome} reason={row.reason}"
            )
    return "\n".join(lines)


def areas_to_json(report: AreasReport) -> dict:
    """The stable --json shape for the area tables."""

    def pa_json(cell: PaCell | None) -> dict | None:
        if cell is None:
            return None
        return {
            "scored": cell.scored,
            "accepted": cell.accepted,
            "not_accepted": cell.not_accepted,
            "accepted_pct": cell.accepted_pct,
            "eligible": cell.eligible,
        }

    bands: dict[str, dict] = {}
    for table in report.tables:
        bands.setdefault(table.band, {})[table.role] = {
            "models": list(table.models),
            "rows": [
                {
                    "area": row.area,
                    "cells": [
                        {
                            "model": cell.model,
                            "n": cell.n,
                            "clean_pct": cell.clean_pct,
                            "mean_rounds": cell.mean_rounds,
                            "eligible": cell.eligible,
                            "pa": pa_json(cell.pa),
                        }
                        for cell in row.cells
                    ],
                    "all": {
                        "n": row.total.n,
                        "clean_pct": row.total.clean_pct,
                        "mean_rounds": row.total.mean_rounds,
                        "eligible": row.total.eligible,
                        "pa": pa_json(row.total.pa),
                    },
                }
                for row in table.rows
            ],
        }
    pa_reviews = None
    if report.pa_path:
        pa_reviews = {
            "path": report.pa_path,
            "task_rows": report.pa_task_rows,
            "uncounted": [
                {"task": row.task, "job": row.job, "outcome": row.outcome} for row in report.pa_uncounted
            ],
            "unattributed": [
                {"task": row.task, "job": row.job, "outcome": row.outcome, "reason": row.reason}
                for row in report.pa_unattributed
            ],
        }
    return {
        "version": 1,
        "backend": {"name": report.backend_name, "reason": report.backend_reason},
        "catalog": {"source": report.catalog_source},
        "vocabulary": list(report.vocabulary) if report.vocabulary is not None else None,
        "current_only": report.current_only,
        "excluded_non_current": report.excluded_non_current,
        "bands": bands,
        "unbanded": {
            "builder": report.unbanded_builder,
            "tester": report.unbanded_tester,
            "other": report.other_roles,
        },
        "pa_reviews": pa_reviews,
    }
