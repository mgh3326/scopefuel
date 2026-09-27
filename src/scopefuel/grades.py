"""task #735 — measured-rep grade proposals for catalog (profile, effort) rungs.

``scopefuel grades propose`` reads the representative-run store, evaluates each
catalog rung against the explicit rule below, and prints the proposed grade
moves with the evidence rep ids behind each one. It is strictly read-only: it
never writes the reps store, the catalog cache, or any other state.

``scopefuel grades apply`` consumes a proposal artifact written by a matching
``propose --json`` run, re-verifies the evidence fingerprint against the live
stores, and writes the updated catalog as C1 JSON — the shape
``bench push-catalog`` PUTs to the canon. Nothing here touches an installed
host; the artifact is what an operator propagates. A matching fingerprint is
not enough on its own: when the live stores are degraded (snapshot catalog,
unread reps canon, truncated rep window — see ``degraded_reasons``), apply
refuses to write unless the operator passes ``--allow-degraded <reason>`` and
that reason is stamped into the output. A proposal whose recorded results rest
on an anomalous rep is refused outright — anomalies are excluded until the
operator reviews them.

The rule v1.2 (operator decisions 2026-09-27 — the v1.1 rule plus the #777
effort-exactness fix; tunable via ``--min-passes``, ``--demote-window`` and
``--demote-rate``):

Evidence hygiene — applied per rep before any rung is evaluated, each excluded
row printed with its reason:

*   **Model match** — a rep counts toward a rung only when its recorded
    ``model_id`` is the rung's catalog ``model_id`` or a spelling the declared
    ``MODEL_EQUIVALENCE`` list maps onto it (documented renames only — floating
    CLI spellings and older generations never equate). A rep with no recorded
    model, or one recorded on a rung whose catalog row carries no model id,
    cannot be matched and never counts. Mismatches are reported, never counted:
    this is the guard that keeps an August ``codex``/``gpt-5`` rep off the
    post-09-22 ``gpt-6-sol`` rungs.
*   **Non-coding tasks** — reps whose ``task_ref`` matches a non-coding pattern
    (``NON_CODING_TASK_PATTERNS`` defaults such as the B0X-* trading slots,
    plus ``[grades].non_coding_task_patterns`` in config.toml and any
    ``--non-coding-task`` regex) are excluded and reported; a trading-slot or
    analysis run is not coding-grade evidence.
*   **Anomalous reps** — rows whose own fields contradict a real measurement
    (a FAIL with 0 rounds and 0 blockers; ``completed=0`` with zero blockers
    recorded; no completion flag at all) are listed as needing review and
    excluded until reviewed. ``apply`` refuses a proposal built on them.

*   **Effort exactness (v1.2)** — a rep is evidence for the rung it measured
    only. A rep *recorded* at one effort — directly or through a
    builder/alias spelling pin — counts on the catalog row at exactly that
    effort; when the catalog has no such row the rep is unrung, never
    re-judged by the profile's default row (the
    ``rung-effort=max -> judged-by-medium`` leak is gone). Only an *inferred*
    effort — no recorded effort and no pin — lands on the profile's default
    rung, flagged ``effort inferred``; effort-less rungs (catalog effort "")
    are judged by effort-less reps and by nothing else.

Task-level aggregation — inside a rung, several reps of the same task count
once: the surviving rep with the latest ``recorded_at`` is the task's final
disposition on that rung (ties break to the higher store id); earlier reps are
excluded and printed. A rep that failed evidence hygiene cannot be the task's
final rep — the latest *counted* rep stands.

*   A **PASS** is a rep with ``completed=1``, ``blockers_found=0``, and no
    post-merge failure marker in ``notes``. A rep that completed but the tester
    found blockers is a pass-with-fixes: shown, never counted.
*   A **FAIL** is a rep with ``completed=0`` (cap exhausted without completing)
    or a ``[rollback]`` / ``[post-merge-blocker]`` marker in ``notes``. These
    markers are the rep vocabulary's convention for a failure discovered *after*
    the rep's merge; nothing today carries them, but the check is structural.
*   **Promote** a rung to grade G — the highest such G — when it has at least
    ``min_passes`` PASSes on tasks of grade G and the rung carries **no** FAIL
    evidence at all (a rollback or cap-out undermines any pending promotion,
    whatever grade the failed task asked for).
*   **Demote** a rung on FAIL *rate* over its most recent ``demote_window``
    counted tasks — at least ``DEMOTE_MIN_FAILS`` FAILs at-or-below the
    placement inside that window reaching ``demote_fail_rate`` — **or one**
    FAIL carrying a post-merge marker at-or-below inside the same window. A
    lone cap-out is still not demotion, and FAILs older than the window count
    toward nothing but blocking promotion. The target is one step below the
    weakest failed grade (a FAIL on an ungraded task drops the rung one step
    below its placement). A FAIL on a task *above* the placement — marker
    included — is overreach evidence: it does not demote (the placement never
    claimed that level) but it blocks promotion.
*   **Conflicted**: when a demote trigger fires but the rung *also* holds
    ``min_passes`` clean PASSes at its current grade, the evidence contradicts
    itself — a demote trigger does not outweigh a measured body of
    at-placement passes. No move is proposed; both sides print for the
    operator.
*   Reps whose ``grade`` is empty are ungraded passes — shown, never counted:
    the rule claims "can do grade-G work", and a pass on a task of unrecorded
    difficulty cannot establish G. ``reps add`` requires ``--grade``; pre-E6
    rows stay ungraded until ``reps backfill`` writes an annotation row linked
    by the rep's ref — the original row is never rewritten, and every
    application is disclosed in the proposal.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import socket
from collections import Counter
from dataclasses import dataclass, field

from . import bench, launch
from .policy import load_config
from .recommend import PROFILE_ALIASES, profile_pool, profile_subscription

# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

RULE_VERSION = "1.2"
MIN_PASSES = 2

# Rule v1.1 demotion: a FAIL *rate* over the rung's most recent ``DEMOTE_WINDOW``
# counted tasks, not a raw all-history count (operator decision 2026-09-27 —
# the v1 two-FAIL rule let an anomalous row and duplicate tasks drive the
# opus@high demotion). Tunable via --demote-window/--demote-rate; a lone
# cap-out still never demotes, a post-merge marker inside the window still does.
DEMOTE_WINDOW = 5
DEMOTE_FAIL_RATE = 0.4
DEMOTE_MIN_FAILS = 2

# Weakest-to-strongest ladder; the index is the rung's strength rank.
GRADE_LADDER = ("C", "B", "A", "A+", "S", "S+")
GRADE_STRENGTH = {grade: index for index, grade in enumerate(GRADE_LADDER)}

# ---------------------------------------------------------------------------
# Model match (rule v1.1 AC1)
#
# A rep counts toward a rung only when its recorded ``model_id`` is the rung's
# catalog model id or a spelling this table maps onto it. The list is
# deliberately short and provable: every entry is a same-generation rename —
# a profile spelling whose launcher pins exactly that model (the devin-*
# launchers embed the model in the profile name), a provider-namespaced id, or
# a launcher whose recorded name is the model's own.
#
# Deliberately NOT equivalent: floating CLI spellings (``opus``, ``claude``,
# ``sonnet``, ``codex``, ``codex-sol``, ``codex-luna``, ``grok``, ``kimi-code``
# alone) and older generations (``gpt-5*``, ``gpt-5.6-sol``, ``grok-4.6``,
# ``claude-opus-5``) — the 2026-09-27 operator decision named the August
# ``codex -> codex-sol`` mapping that let pre-09-22 gpt-5-era reps land on the
# gpt-6-sol rungs. Those reps report as model mismatches, never count, and the
# operator reviews the mismatch list to extend this table.
# ---------------------------------------------------------------------------

MODEL_EQUIVALENCE: dict[str, frozenset[str]] = {
    # devin launcher spellings recorded as the model — the profile argv pins
    # exactly this model id.
    "swe-2": frozenset({"devin-swe2"}),
    "swe-2-medium": frozenset({"devin-swe2-medium"}),
    "swe-2-max": frozenset({"devin-swe2-max"}),
    "swe-1-7": frozenset({"devin-swe17"}),
    "glm-5-2": frozenset({"devin-glm52"}),
    "deepseek-v4-1-flash-high": frozenset({"devin-ds41"}),
    "deepseek-v4-1-flash-max": frozenset({"devin-ds41-max"}),
    # provider-namespaced / launcher spellings of the same model id.
    "solar-pro4": frozenset({"upstage/solar-pro4"}),
    "kimi-k3": frozenset({"kimi-code/k3"}),
    "gemini-3.7-flash": frozenset({"agy-flash", "agy-flash37"}),
    # profile spellings that pin one catalog model id.
    "gpt-6-luna": frozenset({"codex-luna-max"}),
}

# ---------------------------------------------------------------------------
# Non-coding tasks (rule v1.1 AC3)
#
# Task refs that never establish coding-grade evidence: the B0X-* trading-slot
# runs (auto_trader cycles and their -VERIFY rounds). ``[grades]
# non_coding_task_patterns`` in config.toml and ``--non-coding-task <regex>``
# extend the set — e.g. analysis/research task refs — and the effective list
# prints in every proposal.
# ---------------------------------------------------------------------------

# The default matches case-insensitively — slot names are a generated class,
# not an exact-case contract; configured/CLI regexes keep their own semantics.
NON_CODING_TASK_PATTERNS: tuple[str, ...] = (r"(?i)^B0X-",)

# ---------------------------------------------------------------------------
# Anomalous reps (rule v1.1 AC5) — rows whose own fields contradict a real
# measurement. They are excluded and listed as needing review; every legit
# FAIL in the fleet store carries blockers >= 1, so a FAIL with zero recorded
# blockers is never silently trusted.
# ---------------------------------------------------------------------------

_ANOMALY_LABELS = {
    "zero-round-fail": "FAIL with 0 rounds and 0 blockers — nothing was measured",
    "pass-shaped-fail": "completed=0 with zero blockers recorded — a pass-shaped FAIL",
    "no-completion-flag": "rep has no completed flag — outcome unrecorded",
}

# Post-merge failure vocabulary, recorded in rep notes. Bracketed and explicit
# so ordinary prose ("rolled back the lock", "no rollback") cannot trip it.
FAIL_MARKERS = re.compile(r"\[(rollback|post-merge-blocker|postmerge-blocker)\]", re.IGNORECASE)

# The existing notes convention for "this rep replaces that one" — e.g. rep 967
# carries ``supersedes id=964``. The cited id is excluded in the same store
# namespace as the rep carrying the note.
SUPERSEDES_RE = re.compile(r"supersedes id=(\d+)", re.IGNORECASE)

# Superseded duplicate reps the operator named for this first run, as
# ``(superseded, surviving)`` ref specs. A bare id binds the rep known by that
# id in *every* namespace the command can see — under the handoffkeep backend
# that is both ``srv:<id>`` and this host's ``local:<id>``, so an unmigrated
# local duplicate cannot slip past a server-scoped spec. ``local@<host>:<id>``
# names a local row that exists only on that host: the desktop's rows 80/81
# have never been migrated, so the pair binds only when the command runs on
# the host named ``home-desktop`` — this host's own local row 80 is a
# different rep and must not be excluded.
STATIC_SUPERSEDES: tuple[tuple[str, str], ...] = (
    ("993", "995"),
    ("996", "997"),
    ("local@home-desktop:80", "local@home-desktop:81"),
)


# ---------------------------------------------------------------------------
# Rep -> rung resolution (task #750)
#
# A rep records the *launcher spelling* it was spawned with; the catalog is
# keyed on (profile, effort) rungs, so evidence can only land through the maps
# below. Resolution has two halves — the profile basis and the effort basis —
# and both are disclosed per counted rep (``resolved=...`` in the output):
#
# * ``builder map`` — a wrk builder spelling (``_BUILDER_RUNGS``) carries its
#   base catalog profile and, where the spelling pins one, the pinned effort.
#   The table mirrors wrk's ``resolve_catalog_profile``/gate tables; the
#   fixture guard in tests/test_rep_rung_resolution.py fails when wrk adds a
#   builder spelling that is not mapped.
# * ``alias`` — a non-builder spelling names a catalog profile by another
#   name: wrk worker spellings (``_SPELLING_ALIASES``), legacy rep spellings
#   recorded by older tooling, and ``recommend.PROFILE_ALIASES`` entries such
#   as ``codex-max -> codex-sol``.
# * ``default effort`` — a rep on a catalog profile with no recorded effort
#   lands on the profile's catalog default rung (``launch._default_effort``)
#   and is marked ``effort inferred``: the rung was derived, never measured.
# * ``direct`` — the rep's own profile and recorded effort name the rung.
#
# Two precedence rules keep the derivation honest:
#
# * the recorded rep effort always wins over a spelling pin — the rep is the
#   record of what actually ran;
# * a spelling pin wins over the profile default — it is the rung the launch
#   consulted (``builder-sol`` consults codex-sol@high, not the max default).
#
# Effort exactness (v1.2, operator decision 2026-09-27): the resolved rung is
# judged only by a catalog row at *exactly* that effort. The affected paths
# are ``direct`` (the rep's recorded effort), ``builder map`` (the spelling's
# pin, or a recorded effort overriding it), and ``alias`` (same two cases):
# each demands an exact-effort live row or the rep is unrung — it is never
# re-judged by the profile's default row. The ``default effort`` path is
# unaffected: a rep that recorded no effort and carries no pin still lands on
# the profile's default rung, marked ``effort inferred``. Effort-less rungs
# (catalog effort "") are judged by effort-less reps and by nothing else — a
# rep recorded at any named effort never counts on them.
#
# A rep that still cannot be resolved — an unknown spelling, or a rung no live
# catalog row governs — is reported as unrung, never silently counted.
# ---------------------------------------------------------------------------

# wrk builder spellings -> (catalog profile, effort pin or ""). Mirror of
# bin/wrk resolve_catalog_profile()'s CATALOG_PROFILE/CATALOG_EFFORT_PIN plus
# the catalog-exempt builder spellings (devin-*/kimi-* take the same-named
# catalog profile; their launchers pass no effort flag, so the pin is "").
# decision 4088 B: builder-sol/captain-sol pin high — a builder seat never
# takes the max rung.
_BUILDER_RUNGS: dict[str, tuple[str, str]] = {
    "builder-opus": ("opus", ""),
    "captain-opus": ("opus", ""),
    "builder-opus-low": ("opus", "low"),
    "builder-opus-medium": ("opus", "medium"),
    "builder-sonnet-xhigh": ("sonnet", "xhigh"),
    "builder-sonnet-max": ("sonnet", "max"),
    "builder-sol": ("codex-sol", "high"),
    "captain-sol": ("codex-sol", "high"),
    "builder-sol-high": ("codex-sol", "high"),
    "builder-sol-max": ("codex-sol", "max"),
    "builder-sol-medium": ("codex-sol", "medium"),
    # #633: builder-luna is codex-luna admitted under --role builder at the
    # #594 E3 rung (xhigh).
    "builder-luna": ("codex-luna", "xhigh"),
    "builder-luna-max": ("codex-luna", "max"),
    "builder-terra-high": ("codex-terra", "high"),
    "builder-terra-xhigh": ("codex-terra", "xhigh"),
    "builder-terra-max": ("codex-terra", "max"),
    # Task-240 pilot + #737 E6 rungs: grok-hi@<rung>.
    "builder-grok": ("grok-hi", "xhigh"),
    "builder-grok-low": ("grok-hi", "low"),
    "builder-grok-medium": ("grok-hi", "medium"),
    "builder-grok-xhigh": ("grok-hi", "xhigh"),
    # #666 devin builders: the builder spellings name the catalog profile
    # directly — devin-swe2-max/-medium are profiles, not rungs of devin-swe2 —
    # and devin launchers carry no effort flag.
    "builder-devin": ("devin-swe2", ""),
    "builder-devin-medium": ("devin-swe2-medium", ""),
    "builder-devin-max": ("devin-swe2-max", ""),
    "builder-ds41": ("devin-ds41", ""),
    "builder-ds41-max": ("devin-ds41-max", ""),
    # kimi spellings are not in wrk's resolve_catalog_profile (catalog-exempt),
    # but the #704 gate consults the catalog at the pinned rung:
    # builder-kimi-<effort> measures kimi-k3@<effort>.
    "builder-kimi": ("kimi-k3", ""),
    "builder-kimi-high": ("kimi-k3", "high"),
    "builder-kimi-max": ("kimi-k3", "max"),
}

# Non-builder spelling -> (catalog profile, effort pin or ""). Two sources:
#
# * wrk worker spellings whose name is not the catalog profile's (``grok``
#   launches grok-hi, ``codex`` pins codex-sol@high, ``sonnet-med`` is sonnet);
# * legacy rep spellings recorded before the canonical names settled
#   (``claude-opus`` and the model-id spellings are the opus profile).
#
# Spellings that are already catalog profile names resolve directly and are
# deliberately absent — a bare ``opus``/``sonnet``/``kimi-k3-low`` rep needs
# no map. Ambiguous legacy spellings stay unmapped on purpose: ``claude``
# alone names no model, so those reps report as unrung rather than guess.
_SPELLING_ALIASES: dict[str, tuple[str, str]] = {
    # wrk worker spellings (resolve_catalog_profile)
    "codex": ("codex-sol", "high"),
    "codex-med": ("codex-terra", "medium"),
    "codex-luna-hi": ("codex-luna", "high"),
    "sonnet-med": ("sonnet", ""),
    "grok": ("grok-hi", ""),
    "grok-med": ("grok", "medium"),
    # kiro rung spellings
    "kiro-opus-xhigh": ("kiro-opus", "xhigh"),
    "kiro-opus-max": ("kiro-opus", "max"),
    "kiro-sol-xhigh": ("kiro-sol", "xhigh"),
    "kiro-sol-max": ("kiro-sol", "max"),
    # legacy rep spellings (pre-canonical names and model ids in old rows)
    "claude-opus": ("opus", ""),
    "claude-opus-5": ("opus", ""),
    "claude-opus5-verify": ("opus", ""),
    "sonnet-medium": ("sonnet", ""),
    "kimi-code": ("kimi-k3", ""),
    "agy-flash37": ("agy-flash", ""),
}

# Resolution kinds, printed per counted rep (AC2). Kind precedence: the
# profile basis wins — a builder-map or alias rep keeps that kind even when
# its effort was inferred (the ``effort inferred`` flag still prints).
_KIND_DIRECT = "direct"
_KIND_BUILDER_MAP = "builder map"
_KIND_ALIAS = "alias"
_KIND_DEFAULT_EFFORT = "default effort"


@dataclass(frozen=True)
class RungResolution:
    """How one rep's (profile, effort) rung was derived."""

    profile: str
    effort: str
    kind: str  # _KIND_*
    effort_inferred: bool  # effort came from the catalog default, not the rep or a pin
    detail: str  # e.g. "builder-grok -> grok-hi@xhigh" — the printed basis


def _catalog_default_effort(profile: str, rows: list[bench.CatalogEntry] | None) -> str:
    """The effort of the profile's catalog default rung (launch semantics).

    ``launch._default_effort`` over the profile's ordinary rows answers the
    rung a bare ``policy launch <profile>`` starts on; "" is returned when the
    profile has no ordinary placement to infer from.
    """

    ordinary = [row for row in rows or () if not row.retired_at and not launch._unmeasured_e6_row(row)]
    if not ordinary:
        return ""
    catalog_effort, _ = launch._default_effort(profile, ordinary)
    return catalog_effort


def _resolve_rep_rung(
    rep: bench.RepRecord, catalog_rows: dict[str, list[bench.CatalogEntry]]
) -> RungResolution:
    """Resolve a rep to its catalog rung, recording the basis used.

    Profile basis order: builder map, then non-builder aliases, then
    ``recommend.PROFILE_ALIASES``, then the spelling taken literally (direct).
    Effort basis order: the rep's recorded effort, then the spelling's pin,
    then the profile's catalog default (marked ``effort inferred``).
    """

    spelling = rep.profile
    effort_inferred = False
    if spelling in _BUILDER_RUNGS:
        profile, pin = _BUILDER_RUNGS[spelling]
        kind = _KIND_BUILDER_MAP
    elif spelling in _SPELLING_ALIASES:
        profile, pin = _SPELLING_ALIASES[spelling]
        kind = _KIND_ALIAS
    elif spelling in PROFILE_ALIASES:
        profile, pin = PROFILE_ALIASES[spelling], ""
        kind = _KIND_ALIAS
    else:
        profile, pin = spelling, ""
        kind = _KIND_DIRECT

    if rep.effort:
        effort = rep.effort
    elif pin:
        effort = pin
    else:
        effort = _catalog_default_effort(profile, catalog_rows.get(profile))
        effort_inferred = True
        if kind == _KIND_DIRECT:
            kind = _KIND_DEFAULT_EFFORT

    rung_label = f"{profile}{'@' + effort if effort else ''}"
    detail = (
        f"{spelling} -> {rung_label}"
        if spelling != profile or effort_inferred
        else f"{rung_label} (as recorded)"
    )
    return RungResolution(
        profile=profile,
        effort=effort,
        kind=kind,
        effort_inferred=effort_inferred,
        detail=detail,
    )


def _grading_entries(view: bench.CatalogView) -> tuple[bench.CatalogEntry, ...]:
    """The rung universe propose/apply evaluate.

    The canon can be only partially seeded — today a profile joins it when the
    operator pushes a decided row. For a profile the canon never mentions (a
    retired row still counts as mentioned — the canon has spoken), the bundled
    snapshot carries the reviewed placement, exactly the coverage rule
    ``bench._catalog_grade_table`` applies for recommend/policy. Evidence on
    those profiles resolves to the snapshot's rungs — labelled, so a snapshot
    placement is never indistinguishable from a canon row. The snapshot's
    unmeasured E6 rungs are part of it: builder-spelling reps measuring an E6
    arm land on those rows and grade them.
    """

    covered = {entry.profile for entry in view.entries}
    return view.entries + tuple(entry for entry in bench.catalog_snapshot() if entry.profile not in covered)


def _judging_row(rows: list[bench.CatalogEntry], effort: str) -> bench.CatalogEntry | None:
    """The catalog row that governs a rung — the exact-effort row only.

    An exact (profile, effort) row always judges its own rung, including an
    unmeasured E6 row: a rep recorded on that rung is exactly the arm's
    measurement. There is no cross-effort fallback (v1.2, operator decision
    2026-09-27): a rep recorded or pinned at one effort never counts on a
    rung of a different effort — a rung the catalog never placed is unrung,
    not adopted by the profile's default row. A retired exact-effort row is
    still a real placement record, so its evidence is unrung too rather than
    flowing to a live sibling.
    """

    exact = [row for row in rows if row.effort == effort]
    if not exact:
        return None
    # A retired row is a real placement record, not an absent one: the rung
    # was placed and then withdrawn, so its evidence is unrung — it must not
    # flow to a live sibling.
    return exact[0] if not exact[0].retired_at else None


# ---------------------------------------------------------------------------
# Evidence model
# ---------------------------------------------------------------------------

_KIND_PASS = "pass"  # completed, zero blockers, no marker
_KIND_PASS_UNGRADED = "pass-ungraded"  # a pass whose task grade was not recorded
_KIND_PASS_UNCLEAN = "pass-unclean"  # completed but blockers were found
_KIND_FAIL = "fail"  # completed=0 without a post-merge marker
_KIND_FAIL_POSTMERGE = "fail-postmerge"  # a [rollback] / [post-merge-blocker] notes marker
_KIND_UNKNOWN = "unknown"  # rep row with no completed flag

_KIND_LABELS = {
    _KIND_PASS: "PASS",
    _KIND_PASS_UNGRADED: "PASS(ungraded)",
    _KIND_PASS_UNCLEAN: "PASS(blockers)",
    _KIND_FAIL: "FAIL",
    _KIND_FAIL_POSTMERGE: "FAIL(post-merge)",
    _KIND_UNKNOWN: "?",
}


@dataclass(frozen=True)
class EvidenceRep:
    """One rep inside the evaluation, with its store ref and disposition."""

    ref: str  # srv:<id> | local:<id>
    rep: bench.RepRecord
    kind: str = ""
    rung: tuple[str, str] = ()  # the measured rung (catalog spelling)
    row_key: tuple[str, str] | None = None  # the catalog row governing the rung
    excluded: str = ""  # non-empty reason when not counted
    grade_backfilled: bool = False  # rep.grade came from a rep_grade_annotations row
    resolution: str = ""  # direct | builder map | alias | default effort ("" when unrung)
    effort_inferred: bool = False  # rung effort came from the catalog default
    resolution_detail: str = ""  # the printed basis; the unrung reason when row_key is None
    anomaly: str = ""  # _ANOMALY_LABELS key when the row contradicts itself (v1.1)
    # why the row is out: migrated | superseded | anomaly | model-mismatch |
    # non-coding | same-task | alias-duplicate
    exclusion_tags: tuple[str, ...] = ()


def _classify(rep: bench.RepRecord) -> str:
    notes = rep.notes or ""
    if FAIL_MARKERS.search(notes):
        return _KIND_FAIL_POSTMERGE
    if rep.completed == 0:
        return _KIND_FAIL
    if rep.completed == 1:
        # blockers_found=None means "not recorded", not "zero" — a pass whose
        # blocker field was never measured cannot count as a clean PASS.
        if rep.blockers_found != 0:
            return _KIND_PASS_UNCLEAN
        # A grade string outside the ladder (hand-edited store, a server row
        # written by a newer rule) can establish no rung grade — the rep shows
        # its raw task-grade in the evidence line but counts as ungraded.
        return _KIND_PASS if rep.grade in GRADE_STRENGTH else _KIND_PASS_UNGRADED
    return _KIND_UNKNOWN


_REF_RE = re.compile(r"^(?:(srv|local)(?:@([^:]+))?:)?(\d+)$")


def _rep_anomaly(rep: bench.RepRecord) -> str:
    """The anomaly rule a rep trips, or "" — v1.1 AC5.

    A post-merge marker FAIL is exempt: a rollback can legitimately record zero
    rounds and zero blockers — the failure was found after merge, not in a run.
    Every other FAIL with no failure evidence of its own is a contradiction,
    not a measurement.
    """

    if rep.completed is None:
        return "no-completion-flag"
    if rep.completed == 0 and not FAIL_MARKERS.search(rep.notes or ""):
        if rep.rounds == 0 and rep.blockers_found == 0:
            return "zero-round-fail"
        if rep.blockers_found == 0:
            return "pass-shaped-fail"
    return ""


def _normalize_model_id(value: str | None) -> str:
    """Exact-identity canonicalization: strip only. Case-fold is deliberately
    NOT applied — folding is an undeclared equivalence (a rep 'vendor/model-a'
    would count on a distinct 'vendor/Model-A' rung), the same class of
    silent merge AC1 exists to prevent. Spelling variants must be declared in
    MODEL_EQUIVALENCE to count."""

    return (value or "").strip()


def _model_disposition(rep: bench.RepRecord, judging: bench.CatalogEntry) -> str:
    """ "" when the rep's model may count on this rung; a reason otherwise.

    Strict by construction (v1.1 AC1): the rung's catalog ``model_id`` is the
    only identity a rep can match, plus the declared ``MODEL_EQUIVALENCE``
    renames — exact string identity after stripping, no case folding. A rep
    with no recorded model, or a rung with no catalog model id, can never be
    proven to match — fail-closed, reported, never counted.
    """

    rung_model = _normalize_model_id(judging.model_id)
    rep_model = _normalize_model_id(rep.model_id)
    if not rep_model:
        rung_note = rung_model or "unrecorded"
        return f"model not recorded — cannot prove the rep ran the rung's model ({rung_note})"
    if not rung_model:
        return f"rung model not recorded on the catalog row — cannot match rep model '{rep.model_id}'"
    if rep_model == rung_model or rep_model in MODEL_EQUIVALENCE.get(rung_model, ()):
        return ""
    return f"model mismatch: rep model '{rep.model_id}' vs rung model '{judging.model_id}'"


def _non_coding_patterns(cli_patterns: list[str] | None = None) -> tuple[re.Pattern[str], ...]:
    """The effective non-coding task-ref patterns: defaults, config, CLI.

    ``[grades].non_coding_task_patterns`` in config.toml is the operator's
    persistent list (e.g. analysis/research refs); ``--non-coding-task`` adds
    run-scoped regexes. A bad pattern fails closed with a clear error rather
    than silently matching nothing.
    """

    raw: list[str] = list(NON_CODING_TASK_PATTERNS)
    grades_cfg = load_config().get("grades")
    if isinstance(grades_cfg, dict):
        configured = grades_cfg.get("non_coding_task_patterns")
        if isinstance(configured, list):
            raw.extend(item for item in configured if isinstance(item, str) and item.strip())
    raw.extend(cli_patterns or ())
    compiled = []
    for pattern in raw:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise bench.BenchError(f"invalid non-coding task pattern {pattern!r}: {exc}") from exc
    return tuple(compiled)


def _non_coding_reason(rep: bench.RepRecord, patterns: tuple[re.Pattern[str], ...]) -> str:
    """The non-coding exclusion reason for a rep's task_ref, or ""."""

    task = (rep.task_ref or "").strip()
    if not task:
        return ""
    for pattern in patterns:
        if pattern.search(task):
            return f"non-coding task {task} (pattern {pattern.pattern})"
    return ""


def resolve_refs(spec: str, *, backend_name: str, host: str) -> tuple[str, ...]:
    """Parse an exclusion ref spec to the concrete evidence refs it binds.

    ``srv:N`` and ``local:N`` pin a namespace. ``local@<host>:N`` names a
    local row only on that host — it binds nothing anywhere else, so a
    host-scoped exclusion can never eat an unrelated rep that happens to
    share the rowid. A bare ``N`` is the id the fleet cites the rep by:
    under the handoffkeep backend it binds both ``srv:N`` and ``local:N`` so
    an unmigrated local copy cannot double-count beside its server twin;
    under the local backend only ``local:N`` exists.
    """

    match = _REF_RE.match(spec.strip())
    if not match:
        return ()
    namespace, at_host, raw_id = match.groups()
    if at_host is not None:
        if namespace != "local" or at_host != host:
            return ()
        return (f"local:{raw_id}",)
    if namespace is not None:
        return (f"{namespace}:{raw_id}",)
    if backend_name == bench.BENCH_BACKEND_HANDOFFKEEP:
        return (f"srv:{raw_id}", f"local:{raw_id}")
    return (f"local:{raw_id}",)


@dataclass(frozen=True)
class Exclusion:
    old_spec: str
    new_spec: str
    old_refs: tuple[str, ...]  # every concrete ref the spec binds on this host
    new_ref: str | None
    origin: str  # "static" | "cli" | "notes:<carrier ref>"

    def status(self, refs: set[str]) -> str:
        if not self.old_refs:
            return f"{self.old_spec} (superseded by {self.new_spec}) unresolvable on this host"
        bound = [ref for ref in self.old_refs if ref in refs]
        if not bound:
            return (
                f"{self.old_spec} (superseded by {self.new_spec}) not in evidence; survivor {self.new_spec}"
            )
        return f"{', '.join(bound)} superseded by {self.new_ref or self.new_spec}"


# ---------------------------------------------------------------------------
# Evidence collection — which store is read is disclosed, never assumed.
# ---------------------------------------------------------------------------


@dataclass
class RepsEvidence:
    backend: str
    backend_reason: str
    host: str
    remote_count: int
    local_count: int
    window_incomplete: bool
    rows: list[EvidenceRep]
    exclusions: list[Exclusion] = field(default_factory=list)
    # The effective non-coding task-ref regexes (defaults + config + CLI) and
    # the subset the CLI passed — apply must replay the CLI part exactly.
    non_coding_patterns: list[str] = field(default_factory=list)
    cli_non_coding: list[str] = field(default_factory=list)
    # local_id -> server_id for this host's migrated rows, plus the fetched
    # remote set. Both drive the exclusion pass: a ``supersedes id=N`` note
    # written before migration names a *local* id, and the exclusion must
    # follow the row onto its server copy.
    migrated: dict[int, int] = field(default_factory=dict)
    remote_items: list[bench._RemoteRep] = field(default_factory=list)
    # (ref, grade) pairs overlaid from rep_grade_annotations — disclosed so a
    # backfilled grade is never indistinguishable from a recorded one.
    annotations_applied: list[tuple[str, str]] = field(default_factory=list)

    @property
    def counted(self) -> list[EvidenceRep]:
        return [row for row in self.rows if not row.excluded]


def _matching_remote(rep: bench.RepRecord, index: dict, host: str) -> bench._RemoteRep | None:
    """The remote row this local rep migrated to, mirroring _rep_present_remote."""

    for item in index.get(bench._rep_content_key(rep), ()):
        remote_host = bench._migrate_src_host(item.record.notes)
        if remote_host == host and item.origin_id == bench._migrate_origin_id(host, rep.profile, rep.id):
            return item
        candidate = (
            item.record
            if remote_host is None
            else dataclasses.replace(item.record, notes=bench._unstamp_rep_notes(item.record.notes))
        )
        if bench._same_rep_row(rep, candidate):
            return item
    return None


def gather_reps(
    *,
    view: bench.CatalogView,
    exclusions: list[tuple[str, str]] | None = None,
    non_coding: list[str] | None = None,
    allow_plaintext_http: bool = False,
    path=None,
    host: str | None = None,
) -> RepsEvidence:
    """Read every evidence row the rule consumes, each tagged by store.

    The reps source is the resolved bench backend — the handoffkeep canon when
    configured (server rows, refs ``srv:<id>``) merged with this host's local
    rows not yet migrated (refs ``local:<id>``); a local row already present on
    the server is the same rep and counts once via the server's ref. On a
    local backend the local table is the whole source and the backend's reason
    string says why the canon was not read (e.g. an insecure-url auto-fallback)
    — that reason is printed in every proposal so a partial read is never
    silent.
    """

    resolved_host = host or socket.gethostname()
    backend = bench.bench_backend(use="reps", allow_plaintext_http=allow_plaintext_http)
    non_coding_patterns = _non_coding_patterns(non_coding)

    rows: list[EvidenceRep] = []
    remote_items: list[bench._RemoteRep] = []
    migrated: dict[int, int] = {}
    remote_count = 0
    local_count = 0
    window_incomplete = False

    if backend.name == bench.BENCH_BACKEND_HANDOFFKEEP:
        remote_items = bench._fetch_reps(backend, query={"limit": bench._MIGRATE_REP_WINDOW})
        remote_count = len(remote_items)
        window_incomplete = remote_count >= bench._MIGRATE_REP_WINDOW
        local_reps = bench._read_local_reps_for_push(path=path)
        local_count = len(local_reps)
        index = bench._rep_content_index(remote_items)
        for rep in local_reps:
            match = _matching_remote(rep, index, resolved_host)
            if match is not None:
                migrated[rep.id] = match.server_id or 0
        for item in remote_items:
            rows.append(EvidenceRep(ref=f"srv:{item.server_id}", rep=item.record))
        for rep in local_reps:
            if rep.id in migrated:
                # Already migrated — the server's row is the rep; the local
                # copy is a duplicate and is recorded as excluded evidence.
                rows.append(
                    EvidenceRep(
                        ref=f"local:{rep.id}",
                        rep=rep,
                        excluded=f"already migrated (srv:{migrated[rep.id]})",
                    )
                )
            else:
                rows.append(EvidenceRep(ref=f"local:{rep.id}", rep=rep))
    else:
        for rep in bench._read_local_reps_for_push(path=path):
            local_count += 1
            rows.append(EvidenceRep(ref=f"local:{rep.id}", rep=rep))

    evidence = RepsEvidence(
        backend=backend.name,
        backend_reason=backend.reason,
        host=resolved_host,
        remote_count=remote_count,
        local_count=local_count,
        window_incomplete=window_incomplete,
        rows=rows,
        migrated=migrated,
        remote_items=remote_items,
        non_coding_patterns=[p.pattern for p in non_coding_patterns],
        cli_non_coding=list(non_coding or ()),
    )
    _apply_grade_annotations(evidence, path=path)
    _finish_rows(evidence, view, exclusions or [], non_coding_patterns)
    return evidence


def _apply_grade_annotations(evidence: RepsEvidence, *, path=None) -> None:
    """Overlay ``reps backfill`` annotations onto ungraded evidence rows.

    The rep row itself is never rewritten — the annotation is applied to the
    in-memory copy the rule sees, and every application is disclosed via
    ``evidence.annotations_applied``. A rep that already carries a grade keeps
    it: an annotation can fill a gap, never override the original record.
    An annotation written against a local ref follows the row across
    migration through the migrated map, so a backfill does not die when the
    rep it named moves to ``srv:``.
    """
    annotations = bench.read_rep_grade_annotations(path=path)
    if not annotations:
        return
    resolved = {ref: a.grade for ref, a in annotations.items()}
    for local_id, server_id in evidence.migrated.items():
        local_ref, srv_ref = f"local:{local_id}", f"srv:{server_id}"
        if local_ref in resolved and srv_ref not in resolved:
            resolved[srv_ref] = resolved[local_ref]
        elif srv_ref in resolved and local_ref not in resolved:
            resolved[local_ref] = resolved[srv_ref]
    for index, row in enumerate(evidence.rows):
        if row.rep.grade:
            continue
        grade = resolved.get(row.ref)
        if grade is None:
            continue
        evidence.rows[index] = dataclasses.replace(
            row, rep=dataclasses.replace(row.rep, grade=grade), grade_backfilled=True
        )
        evidence.annotations_applied.append((row.ref, grade))


def _notes_spec(row: EvidenceRep, cited: str, evidence: RepsEvidence) -> tuple[str, str, str, list[str]]:
    """The exclusion spec a ``supersedes id=N`` note binds for one carrier.

    The cited id is always a rowid — but in which store depends on where the
    note was written. A local carrier cites a local id. A server row carrying
    a ``[src:<host>]`` migration stamp wrote the note *before* migration, so
    id=N is a local id on that host: the spec binds ``local@<host>:N`` plus
    the migrated server copy (found through the deterministic origin id).
    A server-native carrier cites a server id. And when this host's cited
    local row has already migrated, its live server copy is bound too —
    otherwise the supersede intent dies at the migration boundary and the
    duplicate counts.
    """

    origin = f"notes:{row.ref}"
    namespace = row.ref.partition(":")[0]
    if namespace == "local":
        extra = [f"srv:{srv}"] if (srv := evidence.migrated.get(int(cited))) else []
        return f"local:{cited}", row.ref, origin, extra
    src = bench._migrate_src_host(row.rep.notes)
    if src is None:
        return f"srv:{cited}", row.ref, origin, []
    # The cited row lived on host ``src``; its migrated copy — if any —
    # carries the deterministic origin id of (src, its own profile, N).
    extra = [
        f"srv:{item.server_id}"
        for item in evidence.remote_items
        if item.server_id
        and bench._migrate_src_host(item.record.notes) == src
        and item.origin_id == bench._migrate_origin_id(src, item.record.profile, int(cited))
    ]
    return f"local@{src}:{cited}", row.ref, origin, extra


def _finish_rows(
    evidence: RepsEvidence,
    view: bench.CatalogView,
    exclusions: list[tuple[str, str]],
    non_coding: tuple[re.Pattern[str], ...] = (),
) -> None:
    """Kind, rung, judging row, and exclusion disposition for every row.

    Exclusion precedence, first reason wins: an already-migrated local copy or
    a superseded id, then the v1.1 hygiene gates — anomalous rows (needs
    review), non-coding tasks, and model mismatches against the judging row's
    catalog model. Every applicable gate is recorded in ``exclusion_tags``
    even when an earlier reason printed.
    """

    catalog_rows: dict[str, list[bench.CatalogEntry]] = {}
    for entry in _grading_entries(view):
        catalog_rows.setdefault(entry.profile, []).append(entry)

    specs: list[tuple[str, str, str, list[str]]] = [
        (old, new, "static", []) for old, new in STATIC_SUPERSEDES
    ]
    specs += [(old, new, "cli", []) for old, new in exclusions]
    # Notes-declared supersedes are part of the store itself — a rep carrying
    # ``supersedes id=N`` excludes the cited id, following it across the
    # migration boundary when the note predates the move.
    for row in evidence.rows:
        if row.excluded:
            continue
        for cited in SUPERSEDES_RE.findall(row.rep.notes or ""):
            specs.append(_notes_spec(row, cited, evidence))

    by_ref = {row.ref for row in evidence.rows}
    excluded: dict[str, str] = {}
    for old_spec, new_spec, origin, extra in specs:
        old_refs = tuple(
            dict.fromkeys(
                resolve_refs(old_spec, backend_name=evidence.backend, host=evidence.host) + tuple(extra)
            )
        )
        new_refs = resolve_refs(new_spec, backend_name=evidence.backend, host=evidence.host)
        new_ref = next((ref for ref in new_refs if ref in by_ref), new_refs[0] if new_refs else None)
        item = Exclusion(old_spec, new_spec, old_refs, new_ref, origin)
        evidence.exclusions.append(item)
        for ref in old_refs:
            if ref in by_ref and ref not in excluded:
                excluded[ref] = f"superseded by {item.new_ref or new_spec}"

    for index, row in enumerate(evidence.rows):
        kind = _classify(row.rep)
        resolution = _resolve_rep_rung(row.rep, catalog_rows)
        rung = (resolution.profile, resolution.effort)
        candidates = catalog_rows.get(rung[0])
        judging = _judging_row(candidates, rung[1]) if candidates else None
        reason = row.excluded or excluded.get(row.ref, "")
        tags: list[str] = []
        if row.excluded:
            tags.append("migrated")
        if row.ref in excluded:
            tags.append("superseded")
        # v1.1: the anomaly label is a *shape* property of the rep, computed for
        # every row — a migrated or superseded row that is anomaly-shaped stays
        # labeled, so apply's refusal cannot be bypassed by citing its excluded
        # twin's ref. The needs-review list, though, keeps only rows that would
        # otherwise count (an excluded row needs no review to stay out).
        anomaly = _rep_anomaly(row.rep)
        if not reason:
            if anomaly:
                reason = f"anomalous rep — needs review: {_ANOMALY_LABELS[anomaly]}"
                tags.append("anomaly")
            nc = _non_coding_reason(row.rep, non_coding)
            if nc:
                reason = reason or nc
                tags.append("non-coding")
            if judging is not None:
                mismatch = _model_disposition(row.rep, judging)
                if mismatch:
                    reason = reason or mismatch
                    tags.append("model-mismatch")
            if not (row.rep.task_ref or "").strip():
                # AC2's one-task-one-count needs a task identity — a rep with
                # none cannot prove it is a distinct measurement (the same
                # cannot-prove rule as an unrecorded model), so it is reported
                # rather than counted independently.
                reason = reason or ("task ref not recorded — cannot prove the rep is a distinct measurement")
                tags.append("no-task-ref")
        if judging is None:
            # Unrung — reported, never counted. The detail names the blockage:
            # an unknown spelling has no rows at all; an exact-effort row that
            # is retired, or a profile left with only unmeasured E6/retired
            # rows, has no live row to judge it; and a measured effort with no
            # exact-effort row can never borrow the profile's other rungs.
            if not candidates:
                detail = f"no catalog profile for '{resolution.profile}'"
            elif any(e.effort == rung[1] and e.retired_at for e in candidates):
                detail = f"rung retired: {resolution.detail}"
            elif rung[1] and not resolution.effort_inferred:
                detail = (
                    f"no live catalog row for {resolution.detail} — a rep measured at "
                    f"'{rung[1]}' counts only on that effort's rung"
                )
            else:
                detail = f"no live catalog row for {resolution.detail}"
            resolved_kind, inferred = "", False
        else:
            detail = resolution.detail
            resolved_kind, inferred = resolution.kind, resolution.effort_inferred
        evidence.rows[index] = dataclasses.replace(
            row,
            kind=kind,
            rung=rung,
            row_key=judging.key if judging else None,
            excluded=reason,
            resolution=resolved_kind,
            effort_inferred=inferred,
            resolution_detail=detail,
            anomaly=anomaly,
            exclusion_tags=tuple(tags),
        )


# ---------------------------------------------------------------------------
# Degraded inputs — apply refuses them without an explicit override
# ---------------------------------------------------------------------------


def degraded_reasons(view: bench.CatalogView, evidence: RepsEvidence) -> list[str]:
    """Every degraded-input reason ``apply`` must refuse without an override.

    A matching digest only proves propose and apply read the same stores —
    never that those stores were the canon. The degraded states:

    * the catalog came from the bundled snapshot (server unreachable, or a
      local backend — including the ``auto-local-insecure-url`` fallback), or
      the server has no catalog route at all and the snapshot stood in;
    * the reps backend resolved local while handoffkeep credentials exist —
      the per-use insecure-url path leaves the server reps unread;
    * the server rep window came back full, so rows older than the window —
      FAIL evidence included — may be missing from the evaluation.

    A server-*cache* catalog is not degraded: it is a copy the canon itself
    served inside the operator-configured staleness budget.
    """

    reasons: list[str] = []
    if view.source == bench.CATALOG_SOURCE_SNAPSHOT:
        reasons.append(f"catalog source is the bundled snapshot, not the canon ({view.label})")
    elif view.source == bench.CATALOG_SOURCE_UNSUPPORTED:
        reasons.append("server has no catalog route — the bundled snapshot stood in for the canon")
    if evidence.backend == bench.BENCH_BACKEND_LOCAL and evidence.backend_reason == "auto-local-insecure-url":
        reasons.append(
            "reps read the local table only — handoffkeep credentials exist but the "
            "insecure-url opt-in left the server reps unread"
        )
    if evidence.window_incomplete:
        reasons.append(
            f"server rep window full ({bench._MIGRATE_REP_WINDOW}) — older FAIL evidence may be missing"
        )
    return reasons


# ---------------------------------------------------------------------------
# Grade backfill — annotations linked by rep ref; originals never touched
# ---------------------------------------------------------------------------


@dataclass
class BackfillReport:
    """The plan ``reps backfill`` computed — what --apply would/did write."""

    mapping: dict[str, str]
    planned: list[bench.RepGradeAnnotation] = field(default_factory=list)
    already_annotated: list[tuple[str, str]] = field(default_factory=list)  # (ref, grade)
    skipped_graded: list[tuple[str, str, str]] = field(default_factory=list)  # (ref, task_ref, grade)
    conflicting_annotations: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (ref, existing grade, mapped grade)
    missing_tasks: list[str] = field(default_factory=list)
    applied: int = 0


def backfill_rep_grades(
    *,
    mapping: dict[str, str],
    apply: bool = False,
    source: str = "reps backfill",
    host: str | None = None,
    allow_plaintext_http: bool = False,
    path=None,
) -> BackfillReport:
    """Annotate ungraded reps with their task's grade — never a rewrite.

    Every rep the canonical evidence view counts (server ref when migrated,
    local ref otherwise) whose ``task_ref`` is a mapping key and whose
    ``grade`` is empty gets a ``rep_grade_annotations`` row linked by that
    ref. ``gather_reps`` then overlays the grade, so proposals evaluate the
    rep as graded while the original row stays byte-identical. An already
    graded rep, an existing annotation, and a task with no counted reps are
    reported, never silently changed.
    """
    if not isinstance(mapping, dict) or not mapping:
        raise bench.BenchError("backfill mapping must be a non-empty task-ref -> grade object")
    clean: dict[str, str] = {}
    for task_ref, grade in mapping.items():
        if not isinstance(task_ref, str) or not task_ref.strip():
            raise bench.BenchError("backfill mapping task refs must be non-blank strings")
        if grade not in bench.REP_GRADES:
            raise bench.BenchError(
                f"backfill grade for task {task_ref} must be one of {', '.join(bench.REP_GRADES)}"
            )
        clean[task_ref.strip()] = grade

    # An empty catalog view still runs the full canonical-ref + exclusion
    # machinery — only counted rows can earn an annotation.
    empty_view = bench.CatalogView(
        entries=(),
        source=bench.CATALOG_SOURCE_SNAPSHOT,
        backend=bench.BENCH_BACKEND_LOCAL,
        reason="reps backfill",
    )
    evidence = gather_reps(view=empty_view, host=host, allow_plaintext_http=allow_plaintext_http, path=path)
    report = BackfillReport(mapping=clean)
    by_task: dict[str, list[EvidenceRep]] = {}
    for row in evidence.counted:
        if row.rep.task_ref:
            by_task.setdefault(row.rep.task_ref, []).append(row)

    for task_ref, grade in clean.items():
        rows = by_task.get(task_ref, [])
        if not rows:
            report.missing_tasks.append(task_ref)
            continue
        for row in rows:
            if row.rep.grade and not row.grade_backfilled:
                report.skipped_graded.append((row.ref, task_ref, row.rep.grade))
            elif row.grade_backfilled:
                # An effective annotation already governs this row — including
                # one that reached it through migration fan-out (local:<id> ->
                # srv:<id>). Judging by the row's post-overlay grade keeps a
                # re-backfill at a different grade a reported conflict on the
                # canonical ref instead of a second annotation that splits the
                # rep's grade across the migration boundary.
                if row.rep.grade == grade:
                    report.already_annotated.append((row.ref, grade))
                else:
                    report.conflicting_annotations.append((row.ref, row.rep.grade, grade))
            else:
                report.planned.append(
                    bench.RepGradeAnnotation(
                        rep_ref=row.ref,
                        grade=grade,
                        task_ref=task_ref,
                        source=source,
                        recorded_at=bench._utc_now(),
                    )
                )
    if apply and report.planned:
        report.applied = bench.write_rep_grade_annotations(report.planned, path=path)
    return report


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class RungResult:
    key: tuple[str, str]  # the catalog row key being evaluated
    row: bench.CatalogEntry
    counted: list[EvidenceRep]
    excluded: list[EvidenceRep]
    action: str  # promote | demote | blocked | hold | insufficient
    target: str
    passes_at: dict[str, list[str]]
    ungraded_passes: list[str]
    unclean_passes: list[str]
    fails: list[tuple[str, str | None]]
    evidence_refs: list[str]
    note: str
    # True when the row being evaluated is a bundled-snapshot stand-in for a
    # profile the canon never mentions — labelled so a snapshot placement is
    # never indistinguishable from a canon row.
    snapshot_row: bool = False

    def label(self) -> str:
        return f"{self.key[0]}{'@' + self.key[1] if self.key[1] else ''}"

    def as_dict(self) -> dict[str, object]:
        return {
            "profile": self.key[0],
            "effort": self.key[1],
            "current": self.row.grade,
            "action": self.action,
            "target": self.target,
            "row_source": "snapshot" if self.snapshot_row else "canon",
            "passes_at": {grade: list(refs) for grade, refs in self.passes_at.items()},
            "ungraded_passes": list(self.ungraded_passes),
            "unclean_passes": list(self.unclean_passes),
            "fails": [list(item) for item in self.fails],
            "evidence": list(self.evidence_refs),
            "note": self.note,
        }


def _evaluate_row(
    row: bench.CatalogEntry,
    counted: list[EvidenceRep],
    excluded: list[EvidenceRep],
    min_passes: int,
    demote_window: int,
    demote_fail_rate: float,
) -> RungResult:
    passes_at: dict[str, list[str]] = {}
    ungraded: list[str] = []
    unclean: list[str] = []
    fails: list[tuple[str, str | None]] = []
    postmerge: set[str] = set()
    for item in counted:
        if item.kind == _KIND_PASS:
            assert item.rep.grade  # _classify only returns PASS for graded reps
            passes_at.setdefault(item.rep.grade, []).append(item.ref)
        elif item.kind == _KIND_PASS_UNGRADED:
            ungraded.append(item.ref)
        elif item.kind in (_KIND_FAIL, _KIND_FAIL_POSTMERGE):
            fails.append((item.ref, item.rep.grade))
            if item.kind == _KIND_FAIL_POSTMERGE:
                postmerge.add(item.ref)
        else:
            unclean.append(item.ref)

    current = row.grade
    if current not in GRADE_STRENGTH:
        rung = f"{row.profile}{'@' + row.effort if row.effort else ''}"
        raise bench.BenchError(
            f"catalog row {rung} carries grade {current!r} outside the "
            f"ladder {GRADE_LADDER} — the catalog is corrupt, nothing is proposed"
        )
    cur_s = GRADE_STRENGTH[current]

    # Rule v1.1 demotion: a FAIL *rate* over the rung's most recent
    # demote_window counted tasks — at least DEMOTE_MIN_FAILS at-or-below
    # FAILs inside that window reaching demote_fail_rate — OR one post-merge
    # marker FAIL at-or-below inside the same window. Recency is the window
    # itself: counted reps order by recorded_at (ties by store id) and a FAIL
    # older than the newest demote_window of them is stale — it still blocks
    # promotion but no longer demotes. A FAIL whose task grade sits outside
    # the ladder cannot establish "the rung failed at G" — fail-closed: it
    # counts like an ungraded FAIL. A marker FAIL on a task *above* the
    # placement stays overreach evidence (promotion-blocking, never demoting).
    recent = sorted(counted, key=_recency_key)[-demote_window:]
    recent_refs = {item.ref for item in recent}
    demote_fails = [
        (ref, g)
        for ref, g in fails
        if ref in recent_refs and (g is None or GRADE_STRENGTH.get(g, -1) <= cur_s)
    ]
    marker_demote = [(ref, g) for ref, g in demote_fails if ref in postmerge]
    fail_rate = len(demote_fails) / len(recent) if recent else 0.0
    rate_hit = len(demote_fails) >= DEMOTE_MIN_FAILS and fail_rate >= demote_fail_rate
    if marker_demote or rate_hit:
        if len(passes_at.get(current, ())) >= min_passes:
            # Contradictory evidence: the rung fails at-or-below its grade yet
            # still measures clean passes at it. Not a silent hold — both sides
            # print so the operator judges the outlier.
            return RungResult(
                key=row.key,
                row=row,
                counted=counted,
                excluded=excluded,
                action="conflicted",
                target=current,
                passes_at=passes_at,
                ungraded_passes=ungraded,
                unclean_passes=unclean,
                fails=fails,
                evidence_refs=sorted(ref for ref, _ in fails),
                note=(
                    f"demote trigger at-or-below {current} but {len(passes_at[current])} "
                    f"clean PASSes at {current} — evidence conflicts"
                ),
            )
        # The rung could not do work at or below the grade it claims. Target:
        # one step below the weakest failed claim — an ungraded FAIL is treated
        # as a failure at the placement itself (fail-closed).
        target_idx = min(GRADE_STRENGTH[g] - 1 if g in GRADE_STRENGTH else cur_s - 1 for _, g in demote_fails)
        target_idx = max(target_idx, 0)
        target = GRADE_LADDER[target_idx]
        if marker_demote:
            note = (
                f"post-merge FAIL evidence at-or-below placement "
                f"({len(marker_demote)} marker, {len(demote_fails)} window FAILs)"
            )
        else:
            note = (
                f"{len(demote_fails)}/{len(recent)} counted tasks failed at-or-below {current} "
                f"in the rung's most recent {len(recent)} (window {demote_window}; "
                f"rate {fail_rate:.0%} >= {demote_fail_rate:.0%})"
            )
        if target == current:
            note += "; already at floor C"
        return RungResult(
            key=row.key,
            row=row,
            counted=counted,
            excluded=excluded,
            action="demote",
            target=target,
            passes_at=passes_at,
            ungraded_passes=ungraded,
            unclean_passes=unclean,
            fails=fails,
            evidence_refs=sorted(ref for ref, _ in demote_fails),
            note=note,
        )

    if fails:
        # No demote trigger, but a FAIL of any kind still bars promotion: a
        # lone at-or-below cap-out is below the demotion rate, FAILs older
        # than the window are stale, and overreach FAILs sit above the
        # placement.
        above = [ref for ref, g in fails if g in GRADE_STRENGTH and GRADE_STRENGTH[g] > cur_s]
        stale = [
            (ref, g)
            for ref, g in fails
            if ref not in recent_refs and (g is None or GRADE_STRENGTH.get(g, -1) <= cur_s)
        ]
        parts = []
        if demote_fails:
            parts.append(
                f"{len(demote_fails)} FAIL(s) at-or-below {current} in the most recent "
                f"{len(recent)} counted (demotion needs >= {DEMOTE_MIN_FAILS} at "
                f"rate >= {demote_fail_rate:.0%} or a post-merge marker)"
            )
        if stale:
            parts.append(
                f"{len(stale)} FAIL(s) at-or-below {current} older than the "
                f"most recent {len(recent)}-task window"
            )
        if above:
            parts.append(f"{len(above)} FAIL(s) above the placement")
        return RungResult(
            key=row.key,
            row=row,
            counted=counted,
            excluded=excluded,
            action="blocked",
            target=current,
            passes_at=passes_at,
            ungraded_passes=ungraded,
            unclean_passes=unclean,
            fails=fails,
            evidence_refs=sorted(ref for ref, _ in fails),
            note="promotion blocked by FAIL evidence — " + "; ".join(parts),
        )

    qualifying = [g for g, refs in passes_at.items() if len(refs) >= min_passes]
    if qualifying:
        best = max(qualifying, key=lambda g: GRADE_STRENGTH[g])
        refs = sorted(passes_at[best])
        if GRADE_STRENGTH[best] > cur_s:
            return RungResult(
                key=row.key,
                row=row,
                counted=counted,
                excluded=excluded,
                action="promote",
                target=best,
                passes_at=passes_at,
                ungraded_passes=ungraded,
                unclean_passes=unclean,
                fails=fails,
                evidence_refs=refs,
                note=f">={min_passes} clean PASSes at grade {best}",
            )
        return RungResult(
            key=row.key,
            row=row,
            counted=counted,
            excluded=excluded,
            action="hold",
            target=current,
            passes_at=passes_at,
            ungraded_passes=ungraded,
            unclean_passes=unclean,
            fails=fails,
            evidence_refs=refs,
            note=f"evidence confirms placement (best qualifying grade {best} <= {current})",
        )

    return RungResult(
        key=row.key,
        row=row,
        counted=counted,
        excluded=excluded,
        action="insufficient",
        target=current,
        passes_at=passes_at,
        ungraded_passes=ungraded,
        unclean_passes=unclean,
        fails=fails,
        evidence_refs=[],
        note="no grade reaches the PASS threshold",
    )


@dataclass
class Proposal:
    results: list[RungResult]  # every catalog row that had any evidence
    unrung: list[EvidenceRep]  # counted reps whose rung has no catalog row
    evidence: RepsEvidence
    min_passes: int
    demote_window: int = DEMOTE_WINDOW
    demote_fail_rate: float = DEMOTE_FAIL_RATE
    digest: str = ""
    # The rung universe actually evaluated: canon rows plus bundled snapshot
    # rows for profiles the canon never mentions (see _grading_entries).
    catalog_entries: tuple[bench.CatalogEntry, ...] = ()
    # Profiles whose rungs stand in from the snapshot — disclosed, since a
    # snapshot placement is a reviewed default, not a canon decision.
    snapshot_profiles: frozenset[str] = frozenset()

    def changes(self) -> list[RungResult]:
        return [r for r in self.results if r.action in ("promote", "demote") and r.target != r.row.grade]


def _same_run_key(rep: bench.RepRecord) -> tuple | None:
    """Identity of one measured run, regardless of profile spelling.

    The raw-content dedup key includes the recorded profile spelling, so the
    same run stored as ``codex`` on one host and ``codex-sol`` on another
    survives it. After rung resolution those rows are visibly one rep; this
    key is everything about the run itself — notes and token counts stay out
    (migration stamps and partial records legitimately differ between the
    two recordings). ``model_id`` is required because it is optional in the
    store — None == None would let unrelated profiles merge on task and
    timestamp alone.
    """

    if not rep.task_ref or not rep.recorded_at or not rep.model_id:
        return None
    return (
        rep.model_id,
        rep.task_ref,
        rep.tier,
        rep.role,
        rep.rounds,
        rep.blockers_found,
        rep.completed,
        rep.grade,
        bench._recorded_at_key(rep.recorded_at),
    )


def _collapse_preference(item: EvidenceRep) -> tuple:
    """Which copy of a same-run duplicate is kept — the documented
    precedence: a rep-recorded effort measured the rung, a spelling pin is
    the rung the launcher consulted, a catalog default is inference only;
    the server copy is authoritative over a local one; the lower id is the
    older record.
    """

    return (
        not item.rep.effort,
        item.effort_inferred,
        item.ref.startswith("local:"),
        int(item.ref.rsplit(":", 1)[1]),
    )


def _collapse_alias_duplicates(evidence: RepsEvidence) -> None:
    """One measured run under two spellings counts once.

    Two rows with the same run identity resolve to the same rung only after
    the spelling layer — raw dedup cannot see them. Collapse them here:
    keep the preferred row and mark the rest excluded with a disclosed
    reason, never silently dropped. Rows whose same-run duplicates resolved
    to *different* rungs collapse the same way; the loser keeps its own
    rung's excluded line naming the kept row's rung.

    Only groups whose spellings differ collapse — two rows identical in
    every field including the spelling are fleet-semantics separate reps
    (the store has always counted each row once), not an alias artifact.
    """

    groups: dict[tuple, list[int]] = {}
    for index, item in enumerate(evidence.rows):
        if item.excluded or item.row_key is None:
            continue
        key = _same_run_key(item.rep)
        if key is not None:
            groups.setdefault(key, []).append(index)
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        if len({(evidence.rows[i].rep.profile, evidence.rows[i].rep.effort) for i in indexes}) < 2:
            continue
        keep = min((evidence.rows[i] for i in indexes), key=_collapse_preference)
        for index in indexes:
            item = evidence.rows[index]
            if item is keep:
                continue
            if item.rung == keep.rung:
                reason = f"alias-duplicate of {keep.ref} (same run resolved via different spellings)"
            else:
                reason = (
                    f"duplicate rep under a different spelling (resolved {item.rung[0]}@{item.rung[1]}; "
                    f"kept {keep.ref} resolved {keep.rung[0]}@{keep.rung[1]})"
                )
            evidence.rows[index] = dataclasses.replace(
                item, excluded=reason, exclusion_tags=item.exclusion_tags + ("alias-duplicate",)
            )


def _ref_id(ref: str) -> int:
    """The numeric tail of a store ref — the later record's tie-break."""

    try:
        return int(ref.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return -1


def _recency_key(item: EvidenceRep) -> tuple:
    """Chronological key for counted reps — true instant, then ref id.

    ``bench._recorded_at_key`` can return a parsed datetime or a raw string.
    Aware datetimes convert to the UTC epoch so ``+09:00`` wall-clock fields
    never outrank an earlier ``Z`` instant; naive stamps are read as UTC
    (the store's convention); unparseable stamps sort oldest by raw text.
    The ref id breaks the remaining ties deterministically (a later store
    row is the later record).
    """

    stamp = bench._recorded_at_key(item.rep.recorded_at or "")
    if isinstance(stamp, dt.datetime):
        aware = stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.UTC)
        return (aware.timestamp(), "", _ref_id(item.ref))
    return (float("-inf"), str(stamp), _ref_id(item.ref))


def _collapse_task_repeats(evidence: RepsEvidence) -> None:
    """One task counts once per rung — the latest counted rep stands (v1.1 AC2).

    Duplicate reps of the same task on the same rung (re-runs, retries, or a
    store bug like #677/#568 counted twice) are not independent measurements:
    the surviving rep with the latest ``recorded_at`` is the task's final
    disposition on that rung, ties breaking to the higher store id. Only
    counted candidates compete — a rep already excluded by the hygiene gates
    cannot be the task's final rep, so an anomalous latest row cannot silence
    an earlier valid measurement. The same task on *different* rungs keeps
    counting: each rung's claim is measured separately.
    """

    groups: dict[tuple[str, tuple[str, str]], list[int]] = {}
    for index, item in enumerate(evidence.rows):
        if item.excluded or item.row_key is None:
            continue
        task = (item.rep.task_ref or "").strip()
        if task:
            groups.setdefault((task, item.row_key), []).append(index)
    for (task, _key), indexes in groups.items():
        if len(indexes) < 2:
            continue
        final = max((evidence.rows[i] for i in indexes), key=_recency_key)
        for index in indexes:
            item = evidence.rows[index]
            if item is final:
                continue
            reason = (
                f"same task '{task}' counted once — final rep is {final.ref} "
                f"(latest recorded_at {final.rep.recorded_at})"
            )
            evidence.rows[index] = dataclasses.replace(
                item, excluded=reason, exclusion_tags=item.exclusion_tags + ("same-task",)
            )


def evaluate(
    evidence: RepsEvidence,
    view: bench.CatalogView,
    *,
    min_passes: int = MIN_PASSES,
    demote_window: int = DEMOTE_WINDOW,
    demote_fail_rate: float = DEMOTE_FAIL_RATE,
) -> Proposal:
    _collapse_alias_duplicates(evidence)
    _collapse_task_repeats(evidence)
    grouped: dict[tuple[str, str], list[EvidenceRep]] = {}
    excluded_by_row: dict[tuple[str, str], list[EvidenceRep]] = {}
    unrung: list[EvidenceRep] = []
    for item in evidence.rows:
        if item.excluded:
            if item.row_key is not None:
                excluded_by_row.setdefault(item.row_key, []).append(item)
            continue
        if item.row_key is None:
            unrung.append(item)
            continue
        grouped.setdefault(item.row_key, []).append(item)

    entries = _grading_entries(view)
    covered = {entry.profile for entry in view.entries}
    # "Stand-in" is only meaningful against a real canon read: under a
    # snapshot/unsupported view every row is already the fallback universe.
    snapshot_profiles = (
        frozenset(entry.profile for entry in entries if entry.profile not in covered)
        if view.source in (bench.CATALOG_SOURCE_SERVER, bench.CATALOG_SOURCE_CACHE)
        else frozenset()
    )
    by_key = {entry.key: entry for entry in entries}
    results: list[RungResult] = []
    for key in sorted(by_key):
        row = by_key[key]
        if row.retired_at:
            continue
        counted = grouped.get(key, [])
        excluded = excluded_by_row.get(key, [])
        if not counted and not excluded:
            continue
        result = _evaluate_row(row, counted, excluded, min_passes, demote_window, demote_fail_rate)
        if key[0] in snapshot_profiles:
            result.snapshot_row = True
        results.append(result)

    proposal = Proposal(
        results=results,
        unrung=unrung,
        evidence=evidence,
        min_passes=min_passes,
        demote_window=demote_window,
        demote_fail_rate=demote_fail_rate,
        catalog_entries=entries,
        snapshot_profiles=snapshot_profiles,
    )
    proposal.digest = _input_digest(proposal)
    return proposal


def _input_digest(proposal: Proposal) -> str:
    """Fingerprint the full evaluation input — reps, exclusions, rules, catalog.

    ``apply`` recomputes this digest against live stores and refuses to write
    when it differs: a proposal can only be applied to exactly the evidence it
    was computed from, so a stale artifact or a changed rep store fails loud
    instead of writing an unproven change. The catalog serialized here is the
    evaluated rung universe (canon plus snapshot stand-ins), not only what the
    backend returned.
    """

    evidence = proposal.evidence
    payload = {
        "rule_version": RULE_VERSION,
        "min_passes": proposal.min_passes,
        "demote_window": proposal.demote_window,
        "demote_fail_rate": proposal.demote_fail_rate,
        "non_coding_patterns": evidence.non_coding_patterns,
        "backend": evidence.backend,
        "catalog": [entry.as_dict() for entry in sorted(proposal.catalog_entries, key=lambda e: e.key)],
        "exclusions": [{"old": e.old_spec, "new": e.new_spec} for e in evidence.exclusions],
        "reps": [
            {
                "ref": row.ref,
                "rep": row.rep.as_dict(),
                "kind": row.kind,
                "rung": list(row.rung),
                "row_key": list(row.row_key) if row.row_key else None,
                "excluded": row.excluded or None,
                "grade_backfilled": row.grade_backfilled,
                "resolution": row.resolution or None,
                "effort_inferred": row.effort_inferred or None,
                "anomaly": row.anomaly or None,
                "exclusion_tags": list(row.exclusion_tags),
            }
            for row in evidence.rows
        ],
        "annotations_applied": [list(item) for item in evidence.annotations_applied],
        "results": [r.as_dict() for r in proposal.results],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_evidence(item: EvidenceRep) -> str:
    rep = item.rep
    bits = [
        item.ref,
        f"task={rep.task_ref or '-'}",
        f"tier={rep.tier or '-'}",
        _KIND_LABELS.get(item.kind, item.kind),
    ]
    if rep.grade:
        bits.append(f"task-grade={rep.grade}{'(backfilled)' if item.grade_backfilled else ''}")
    bits.append(f"rounds={rep.rounds if rep.rounds is not None else '-'}")
    bits.append(f"blockers={rep.blockers_found if rep.blockers_found is not None else '-'}")
    if item.resolution:
        # The printed basis (AC2): which rule resolved the rung, and whether
        # the effort was measured on the rep or inferred from the profile's
        # catalog default. An inferred effort is never presented as measured.
        detail = f"{item.resolution_detail}{' (effort inferred)' if item.effort_inferred else ''}"
        bits.append(f"resolved={item.resolution}:{detail}")
    elif item.resolution_detail:
        bits.append(f"unresolved:{item.resolution_detail}")
    return " ".join(bits)


def parse_rung_spec(spec: str) -> tuple[str, str]:
    """``profile[@effort]`` in catalog spelling -> a rung key."""

    profile, _, effort = spec.strip().partition("@")
    return profile.strip(), effort.strip()


def _unsubscribed_tag(profile: str, pool: str | None) -> str:
    """#742 — " [unsubscribed]" marker for proposal lines.

    The flag never suppresses evidence or history: propose keeps evaluating a
    flagged rung and marks it, so flipping ``subscribed`` back to true restores
    an untouched record rather than a gap.
    """
    resolved = profile_pool(profile)[0] or pool or None
    return "" if profile_subscription(profile, resolved).subscribed else " [unsubscribed]"


def render_rung_detail(result: RungResult) -> list[str]:
    """Every evidence row for one rung — the audit view a --rung query wants."""

    lines = [
        f"rung {result.label()} current={result.row.grade}"
        f"{_unsubscribed_tag(result.key[0], result.row.pool)}:"
    ]
    for item in sorted(result.counted + result.excluded, key=lambda x: (x.ref.partition(":")[0], x.ref)):
        suffix = f"  [excluded: {item.excluded}]" if item.excluded else ""
        lines.append(f"  {_fmt_evidence(item)}{suffix}")
    summary = [f"{grade}:{len(refs)}" for grade, refs in result.passes_at.items()]
    if result.ungraded_passes:
        summary.append(f"ungraded-pass:{len(result.ungraded_passes)}")
    if result.unclean_passes:
        summary.append(f"pass-with-blockers:{len(result.unclean_passes)}")
    if result.fails:
        summary.append(f"fails:{len(result.fails)}")
    if summary:
        lines.append(f"  counts: {' '.join(summary)}")
    lines.append(f"  => {result.action} {result.row.grade} -> {result.target} ({result.note})")
    return lines


def render_proposal(
    proposal: Proposal, view: bench.CatalogView, *, focus: tuple[str, str] | None = None
) -> str:
    evidence = proposal.evidence
    lines = [
        "scopefuel grades propose — measured-rep grade proposals (read-only)",
        f"rule v{RULE_VERSION}: promote needs >= {proposal.min_passes} clean PASSes on "
        "tasks of grade G with zero FAIL evidence; demote on "
        f">= {DEMOTE_MIN_FAILS} FAILs at-or-below the placement reaching "
        f"{proposal.demote_fail_rate:.0%} of the rung's most recent "
        f"{proposal.demote_window} counted tasks, or one post-merge marker FAIL "
        "at-or-below in that window; any counted FAIL still blocks promotion. "
        "Evidence hygiene: rep model must match the rung's catalog model "
        "(declared renames only), non-coding task refs are excluded, anomalous "
        "reps are excluded pending review, and each task counts once per rung "
        "(the surviving rep with the latest recorded_at). A rep counts only "
        "on its resolved effort's own rung — recorded or pinned efforts with "
        "no exact row are unrung; effort-less reps land on the profile "
        "default (effort inferred).",
        f"reps source={evidence.backend} (reason={evidence.backend_reason}) host={evidence.host} "
        f"rows: server={evidence.remote_count} local={evidence.local_count}",
    ]
    lines.append("non-coding task patterns (excluded, reported): " + ", ".join(evidence.non_coding_patterns))
    if evidence.window_incomplete:
        lines.append(
            f"  warning: server rep window full ({bench._MIGRATE_REP_WINDOW}) — "
            "remote completeness not proven; treat proposal as partial"
        )
    if evidence.backend == bench.BENCH_BACKEND_LOCAL:
        lines.append("  note: local backend — server reps were not read on this host")
    if evidence.annotations_applied:
        refs = ", ".join(f"{ref}={grade}" for ref, grade in evidence.annotations_applied)
        lines.append(f"  backfilled grades applied from annotations: {refs}")
    degraded = degraded_reasons(view, evidence)
    if degraded:
        lines.append(
            "  degraded input — apply refuses this proposal without --allow-degraded: " + "; ".join(degraded)
        )
    lines.append(view.label)
    if proposal.snapshot_profiles:
        lines.append(
            "  note: canon covers no row for these profiles — bundled snapshot "
            "placements stand in (results marked [snapshot-placement]): "
            + ", ".join(sorted(proposal.snapshot_profiles))
        )

    counted = [row for row in evidence.rows if not row.excluded and row.row_key is not None]
    if counted:
        by_kind = Counter(row.resolution for row in counted)
        inferred = sum(1 for row in counted if row.effort_inferred)
        kinds = " ".join(
            f"{kind}={by_kind.get(kind, 0)}"
            for kind in (_KIND_DIRECT, _KIND_BUILDER_MAP, _KIND_DEFAULT_EFFORT, _KIND_ALIAS)
            if by_kind.get(kind, 0)
        )
        lines.append(
            f"resolution basis ({len(counted)} counted reps): {kinds} — effort inferred on {inferred}"
        )

    if evidence.exclusions:
        lines.append("exclusions:")
        refs = {row.ref for row in evidence.rows}
        for item in evidence.exclusions:
            lines.append(f"  {item.status(refs)}")

    anomalous = [row for row in evidence.rows if "anomaly" in row.exclusion_tags]
    if anomalous:
        lines.append(f"needs review — anomalous reps ({len(anomalous)}, excluded until reviewed):")
        for row in anomalous:
            lines.append(f"  {_fmt_evidence(row)}  [{row.anomaly}]")

    unrung_excluded = [
        row
        for row in evidence.rows
        if row.excluded and row.row_key is None and "anomaly" not in row.exclusion_tags
    ]
    if unrung_excluded:
        lines.append("excluded evidence with no catalog rung (reported, never counted):")
        for row in unrung_excluded:
            lines.append(f"  {_fmt_evidence(row)}  [excluded: {row.excluded}]")

    changes = proposal.changes()
    changed_keys = {r.key for r in changes}
    holds = [r for r in proposal.results if r.key not in changed_keys]
    if changes:
        lines.append("proposals:")
        for r in changes:
            lines.append(
                f"  {r.action} {r.label()} {r.row.grade} -> {r.target}"
                f"{_unsubscribed_tag(r.key[0], r.row.pool)}"
                f"{' [snapshot-placement]' if r.snapshot_row else ''}"
            )
            lines.append(f"    rule: {r.note}")
            # AC2: every counted rep prints its resolution basis, not only the
            # refs that drove the proposed change.
            for item in r.counted:
                lines.append(f"    evidence {_fmt_evidence(item)}")
            for item in r.excluded:
                lines.append(f"    excluded {item.ref} ({item.excluded})")
    else:
        lines.append("proposals: none")

    if holds:
        lines.append("rungs with evidence but no change:")
        for r in holds:
            lines.append(
                f"  {r.action:<12} {r.label()} current={r.row.grade} — {r.note}"
                f"{_unsubscribed_tag(r.key[0], r.row.pool)}"
                f"{' [snapshot-placement]' if r.snapshot_row else ''}"
            )
            summary = [f"{grade}:{len(refs)}" for grade, refs in r.passes_at.items()]
            if r.ungraded_passes:
                summary.append(f"ungraded-pass:{len(r.ungraded_passes)}")
            if r.unclean_passes:
                summary.append(f"pass-with-blockers:{len(r.unclean_passes)}")
            if r.fails:
                summary.append(f"fails:{len(r.fails)}")
            if summary:
                lines.append(f"    counts: {' '.join(summary)}")
            # AC2: every counted rep prints how its rung was resolved, not
            # only the refs that drove a proposed change.
            for item in r.counted:
                lines.append(f"    evidence {_fmt_evidence(item)}")
            for item in r.excluded:
                lines.append(f"    excluded {item.ref} ({item.excluded})")

    if proposal.unrung:
        lines.append("unrung evidence (rep rung could not be resolved — reported, not counted):")
        for item in proposal.unrung:
            suffix = _unsubscribed_tag(item.rung[0], None) if item.rung else ""
            lines.append(f"  {_fmt_evidence(item)}{suffix}")

    if focus is not None:
        focused = [r for r in proposal.results if r.key == focus]
        if focused:
            lines.append("focused rung:")
            lines.extend(render_rung_detail(focused[0]))
        else:
            lines.append(
                f"focused rung: {focus[0]}{'@' + focus[1] if focus[1] else ''} "
                "has no catalog row or no evidence"
            )

    refs = {row.ref for row in evidence.rows}
    missing = [
        item.status(refs) for item in evidence.exclusions if not any(ref in refs for ref in item.old_refs)
    ]
    if missing:
        lines.append("missing exclusion targets (never silently ignored):")
        lines.extend(f"  {line}" for line in missing)

    counted_rows = [row for row in evidence.rows if not row.excluded and row.row_key is not None]
    excluded_rows = [row for row in evidence.rows if row.excluded]
    tag_counts = Counter(tag for row in excluded_rows for tag in row.exclusion_tags)
    tag_summary = " ".join(f"{tag}={tag_counts[tag]}" for tag in sorted(tag_counts)) or "none"
    lines.append(
        f"evidence summary: {len(counted_rows)} counted, {len(excluded_rows)} excluded "
        f"({tag_summary}), {len(proposal.unrung)} unrung"
    )
    lines.append(f"proposal-digest={proposal.digest}")
    return "\n".join(lines)


def proposal_to_json(proposal: Proposal, view: bench.CatalogView) -> dict:
    return {
        "rule_version": RULE_VERSION,
        "min_passes": proposal.min_passes,
        "digest": proposal.digest,
        # The inputs apply must re-derive the evaluation with — only
        # operator-supplied pairs; static pairs come from the code and
        # notes-declared supersedes from the store itself.
        "params": {
            "cli_exclusions": [
                [item.old_spec, item.new_spec]
                for item in proposal.evidence.exclusions
                if item.origin == "cli"
            ],
            "cli_non_coding": list(proposal.evidence.cli_non_coding),
            "demote_window": proposal.demote_window,
            "demote_fail_rate": proposal.demote_fail_rate,
        },
        "non_coding_patterns": list(proposal.evidence.non_coding_patterns),
        "anomalies": [
            {"ref": row.ref, "rule": row.anomaly, "task_ref": row.rep.task_ref}
            for row in proposal.evidence.rows
            if row.anomaly
        ],
        "reps": {
            "backend": proposal.evidence.backend,
            "backend_reason": proposal.evidence.backend_reason,
            "host": proposal.evidence.host,
            "remote_count": proposal.evidence.remote_count,
            "local_count": proposal.evidence.local_count,
            "window_incomplete": proposal.evidence.window_incomplete,
        },
        "catalog_source": view.source,
        "snapshot_fill_profiles": sorted(proposal.snapshot_profiles),
        "resolution_counts": dict(
            Counter(
                row.resolution
                for row in proposal.evidence.rows
                if not row.excluded and row.row_key is not None
            )
        ),
        "effort_inferred": sum(
            1
            for row in proposal.evidence.rows
            if not row.excluded and row.row_key is not None and row.effort_inferred
        ),
        "degraded": degraded_reasons(view, proposal.evidence),
        "exclusions": [
            {
                "old": item.old_spec,
                "new": item.new_spec,
                "old_refs": list(item.old_refs),
                "new_ref": item.new_ref,
                "origin": item.origin,
            }
            for item in proposal.evidence.exclusions
        ],
        "results": [r.as_dict() for r in proposal.results],
        "unrung": [_fmt_evidence(item) for item in proposal.unrung],
    }


# ---------------------------------------------------------------------------
# Apply — writes the updated catalog artifact; never touches a host.
# ---------------------------------------------------------------------------


def _recorded_result_refs(item: dict) -> list:
    """Every rep ref a recorded result claims as evidence, however shaped.

    ``as_dict()`` writes ``evidence`` (flat refs), ``passes_at`` (grade ->
    refs), ``ungraded_passes``/``unclean_passes`` (flat refs) and ``fails``
    ([ref, grade] pairs) — all of them are the artifact's claim of what the
    change rests on.
    """

    refs: list = []
    for ref in item.get("evidence") or ():
        refs.append(ref)
    for key in ("ungraded_passes", "unclean_passes"):
        for ref in item.get(key) or ():
            refs.append(ref)
    passes = item.get("passes_at")
    if isinstance(passes, dict):
        for group in passes.values():
            if isinstance(group, list):
                refs.extend(group)
    for pair in item.get("fails") or ():
        if isinstance(pair, list | tuple) and pair:
            refs.append(pair[0])
    return refs


def apply_proposals(
    proposal_file: dict,
    *,
    decided_by: str,
    deviation_ref: str,
    allow_plaintext_http: bool = False,
    allow_degraded: str | None = None,
    path=None,
) -> tuple[list[bench.CatalogEntry], Proposal, bench.CatalogView]:
    """Verify a proposal artifact against live stores, then stamp the catalog.

    The proposal's params (rules + exclusions) drive a fresh evaluation; the
    resulting digest must equal the artifact's, or the evidence changed since
    it was computed and nothing is written. Returns the full updated catalog,
    the live proposal, and the catalog view it was computed against.

    A digest match still says nothing about *what* was read: when the live
    inputs are degraded (``degraded_reasons``), the write is refused unless
    the operator passes an explicit ``allow_degraded`` reason, which is then
    stamped into every changed row's ``deviation_ref``.
    """

    if not isinstance(proposal_file, dict):
        raise bench.BenchError("proposal artifact must be a JSON object")
    min_passes = proposal_file.get("min_passes")
    if not isinstance(min_passes, int) or min_passes < 1:
        raise bench.BenchError("proposal artifact has no valid min_passes")
    params = proposal_file.get("params")
    if params is not None and not isinstance(params, dict):
        raise bench.BenchError("proposal artifact params must be a JSON object")
    params = params or {}
    raw_exclusions = params.get("cli_exclusions")
    if raw_exclusions is None:
        raw_exclusions = []
    if not isinstance(raw_exclusions, list):
        raise bench.BenchError("proposal artifact cli_exclusions must be a JSON array")
    results = proposal_file.get("results")
    if results is None:
        results = []
    if not isinstance(results, list):
        raise bench.BenchError("proposal artifact results must be a JSON array")
    exclusions = [
        (str(pair[0]), str(pair[1]))
        for pair in raw_exclusions
        if isinstance(pair, list | tuple) and len(pair) == 2
    ]
    raw_non_coding = params.get("cli_non_coding")
    if raw_non_coding is None:
        raw_non_coding = []
    if not isinstance(raw_non_coding, list):
        raise bench.BenchError("proposal artifact cli_non_coding must be a JSON array")
    non_coding = [str(pattern) for pattern in raw_non_coding if isinstance(pattern, str)]
    demote_window = params.get("demote_window", DEMOTE_WINDOW)
    if not isinstance(demote_window, int) or isinstance(demote_window, bool) or demote_window < 1:
        raise bench.BenchError("proposal artifact has no valid demote_window")
    demote_fail_rate = params.get("demote_fail_rate", DEMOTE_FAIL_RATE)
    if (
        not isinstance(demote_fail_rate, int | float)
        or isinstance(demote_fail_rate, bool)
        or not 0 < demote_fail_rate <= 1
    ):
        raise bench.BenchError("proposal artifact has no valid demote_fail_rate")
    view = bench.read_catalog(path=path, commit_cache=False, allow_plaintext_http=allow_plaintext_http)
    evidence = gather_reps(
        view=view,
        exclusions=exclusions,
        non_coding=non_coding,
        allow_plaintext_http=allow_plaintext_http,
        path=path,
    )
    degraded = degraded_reasons(view, evidence)
    if degraded:
        override = (allow_degraded or "").strip()
        if not override:
            raise bench.BenchError(
                "grades apply refuses degraded input: "
                + "; ".join(degraded)
                + " — rerun `grades propose` against the canon, or pass "
                "--allow-degraded <reason> to record why the override is safe"
            )
        deviation_ref = f"{deviation_ref}; degraded-override: {override}"
    live = evaluate(
        evidence,
        view,
        min_passes=min_passes,
        demote_window=demote_window,
        demote_fail_rate=demote_fail_rate,
    )
    if proposal_file.get("digest") != live.digest:
        raise bench.BenchError(
            "proposal digest does not match the live evidence — rerun `grades propose` "
            "(the reps store, exclusions, or catalog changed since the artifact was written)"
        )
    # v1.2: an artifact that names the same (profile, effort) result more than
    # once is refused BEFORE the per-key map is built — the map's last-write-
    # wins is exactly what let a forged earlier duplicate (the r4 local:999
    # carrier) hide behind a later clean one. Every item that claims a rung
    # key counts, whatever action it carries; a legit artifact lists each rung
    # exactly once.
    seen: set[tuple[str, str]] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        profile, effort = item.get("profile"), item.get("effort")
        if not isinstance(profile, str) or not isinstance(effort, str):
            continue
        key = (profile, effort)
        if key in seen:
            raise bench.BenchError(
                f"proposal artifact lists rung {profile}@{effort or '(default)'} "
                "more than once — the artifact was forged or hand-edited; "
                "rerun `grades propose`"
            )
        seen.add(key)
    recorded = {
        (item["profile"], item["effort"]): item
        for item in results
        if isinstance(item, dict)
        and item.get("action") in ("promote", "demote")
        and isinstance(item.get("profile"), str)
        and isinstance(item.get("effort"), str)
    }
    # v1.1: an anomalous rep is excluded pending review — a proposal artifact
    # that names one as evidence was forged or built on an unreviewed anomaly,
    # and apply refuses it outright (the digest check alone cannot see the
    # artifact's claimed evidence).
    anomaly_refs = {row.ref for row in live.evidence.rows if row.anomaly}
    live_by_key = {result.key: result for result in live.results}
    for key, item in recorded.items():
        # v1.2: the recorded rung must be a rung the live evaluation actually
        # produced — a key absent from live results means the artifact was
        # forged or built on a different store, and empty evidence lists must
        # not bypass the check. The live result's action and target must match
        # the record too: the digest binds live evidence, not the artifact's
        # claims, so a forged promote on a rung live holds cannot stand.
        live_result = live_by_key.get(key)
        if live_result is None:
            raise bench.BenchError(
                f"proposal artifact records a result for rung {key[0]}@{key[1] or '(default)'} "
                "absent from the live results — the artifact was forged or built "
                "on a different store; rerun `grades propose`"
            )
        if item.get("action") != live_result.action or item.get("target") != live_result.target:
            raise bench.BenchError(
                f"proposal artifact disagrees with live evaluation for {live_result.label()} — "
                "rerun `grades propose`"
            )
        claimed = {ref for ref in _recorded_result_refs(item) if isinstance(ref, str)}
        bad = sorted(claimed & anomaly_refs)
        if bad:
            raise bench.BenchError(
                f"proposal for {key[0]}@{key[1]} rests on anomalous rep(s) "
                f"{', '.join(bad)} — excluded pending review; rerun `grades propose` "
                "after the operator reviews the anomaly"
            )
        # The stored digest binds live evidence, not the artifact's claimed
        # refs: every ref the artifact names must be a ref the live result for
        # this rung actually rests on. A ghost (a ref absent from the store) or
        # a borrowed ref from another rung means the artifact was forged after
        # recording — fail closed.
        live_refs = {ref for ref in _recorded_result_refs(live_result.as_dict())}
        ghosts = sorted(ref for ref in claimed if ref not in live_refs)
        if ghosts:
            raise bench.BenchError(
                f"proposal for {key[0]}@{key[1]} names rep(s) "
                f"{', '.join(ghosts)} absent from its live evidence — the artifact "
                "was forged or built on a different store; rerun `grades propose`"
            )
    for result in live.changes():
        want = recorded.get(result.key)
        if want is None or want.get("target") != result.target:
            raise bench.BenchError(
                f"proposal artifact disagrees with live evaluation for {result.label()} — "
                "rerun `grades propose`"
            )

    changed = {r.key: r for r in live.changes()}
    now = bench._utc_now()
    entries: list[bench.CatalogEntry] = []
    # The evaluated universe, not only the backend's rows: a promote/demote on
    # a snapshot stand-in rung must land in the artifact so push-catalog can
    # write the row into the canon.
    for entry in live.catalog_entries:
        result = changed.get(entry.key)
        if result is None:
            entries.append(entry)
            continue
        entries.append(
            dataclasses.replace(
                entry,
                grade=result.target,
                decided_by=decided_by,
                decided_at=now,
                deviation_ref=(f"{deviation_ref}; {result.action} evidence {','.join(result.evidence_refs)}"),
            )
        )
    return entries, live, view
