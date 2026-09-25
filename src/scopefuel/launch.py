"""Launch resolution: what model id and effort a profile actually starts with.

#593 moves model ids, grade placement, pool and gate into the handoffkeep
catalog (``/v1/bench/catalog``, schema v12).  This module is the single place
that answers a launcher's question — "profile X, which model, which effort?" —
from that canon, and falls back to the bundled snapshot when the canon is out
of reach.

Two things deliberately stay here rather than on the server:

* **Profile spellings and argv skeletons** belong to the launcher (``bin/wrk``).
  The catalog knows ``codex-sol``; it does not know that the Codex CLI wants
  ``--yolo -m <id> -c model_reasoning_effort=<effort>``.
* **The default effort rung** for a profile.  handoffkeep's catalog is keyed on
  ``(profile, effort)`` and has no first-class "this rung is the default"
  column, so :data:`DEFAULT_LAUNCH_EFFORTS` carries the launcher's default and
  the catalog overrides it only by *moving grades* — see
  :func:`_default_effort`.  A catalog column for this is the clean fix and is
  recorded as follow-up work.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .bench import (
    CATALOG_EFFORT_RANKS,
    CATALOG_SOURCE_SNAPSHOT,
    CatalogEntry,
    CatalogView,
    read_catalog,
)
from .recommend import (
    ASTRA_ALLOWED_PURPOSES,
    ASTRA_ROLE_PROFILES,
    E6_ARM_GRADE,
    E6_ARM_RUNGS,
    GRADE_TABLE,
    PROFILE_ALIASES,
    REP_GRADES_ORDER,
    e6_arm_matches,
    e6_arm_rung_for,
    normalize_effort,
    parse_e6_arm_marker,
    profile_pool,
)

GATE_DEFAULT = "default"
GATE_ESCALATION = "escalation"
GATE_CONSULT_ONLY = "consult_only"


class LaunchError(ValueError):
    """A launch cannot be resolved, and guessing a value is not an option."""


# Launch model ids that ``GRADE_TABLE`` does not carry, or carries in the
# benchmark namespace rather than the launcher's.  ``aa_agent_model_id`` is the
# benchmark identity; these are the strings the CLI is actually handed, and they
# mirror ``bin/wrk resolve_profile()`` exactly (the contract guard asserts it).
LAUNCH_MODEL_IDS: dict[str, str] = {
    "kiro-sonnet": "claude-sonnet-5",
    "kiro-cheap": "qwen3-coder-next",
    "kiro-haiku": "claude-haiku-4.5",
    # Profiles whose launcher passes a CLI alias ("--model sonnet") or a fixed
    # argv rather than a model id. They still need an identity here: the catalog
    # route requires a non-blank model_id, so a row without one is rejected and
    # the whole seed batch with it. The launcher keeps its alias either way —
    # these spellings are catalog-exempt on the wrk side.
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4.5",
    "devin-swe2": "swe-2",
    "devin-glm52": "glm-5-2",
    "devin-swe17": "swe-1-7",
    "devin-ds41": "deepseek-v4-1-flash-high",
    # #635: devin effort lives in the model id, one profile per rung.
    "devin-swe2-medium": "swe-2-medium",
    "devin-swe2-max": "swe-2-max",
    "devin-ds41-max": "deepseek-v4-1-flash-max",
    # Same model as kimi-k3; the differentiator is KIMI_CODE_HOME, not the model.
    "kimi-k3-low": "kimi-k3",
    "oc-qwen37-max": "qwen3.7-max",
    "oc-minimax-m3": "minimax-m3",
    "oc-solar4": "solar-pro4",
    # A router, not a model — this is the identity the launcher actually requests.
    "oc-omni": "omniroute/auto/coding",
}

# The rung a profile starts on when the caller pins no effort.  Only profiles
# whose launcher accepts ``--effort`` carry one; "" means the launcher passes no
# effort flag at all (kimi/devin/opencode/agy).
DEFAULT_LAUNCH_EFFORTS: dict[str, str] = {
    "opus": "high",
    "sonnet": "high",
    "haiku": "low",
    "codex-sol": "max",
    "codex-terra": "medium",
    "codex-terra-max": "max",
    "codex-luna": "medium",
    "codex-luna-max": "max",
    "codex-astra": "xhigh",
    "grok": "medium",
    "grok-hi": "high",
    "kiro-opus": "xhigh",
    "kiro-sonnet": "high",
    "kiro-sol": "high",
    "kiro-cheap": "medium",
    "kiro-haiku": "high",
    "cc-qwen38": "high",
    "cc-glm": "high",
}

# Launchable profiles that are deliberately absent from ``GRADE_TABLE``: they are
# never recommendation candidates, but a launcher still has to know their model
# id, and an explicit operator request still has to be able to start them.
_FABLE_GATE_REASON = (
    "Opus 대비 2배 가격 — 운영자 명시 요청(consult-only)일 때만. "
    "hk:doc note/2026-09-23/model-refresh-opus55-gpt6-grok47"
)
_ASTRA_GATE_REASON = (
    "astra 는 director 판정·architect 자문·운영자 요청 조언에 한정 "
    "(hk:doc decision/2026-09-21/astra-allowed-purposes-approved)"
)

CONSULT_ONLY_SNAPSHOT: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        profile="fable",
        effort="",
        model_id="claude-fable-5-1",
        pool="claude",
        # Fable sat at S+/escalation until #591 moved it to consult-only; the
        # grade is inert on a consult_only row (never a candidate, never
        # boundary-validated) and is kept only so the server's NOT NULL grade
        # column carries the last operator placement rather than a new claim.
        grade="S+",
        score=None,
        gate=GATE_CONSULT_ONLY,
        gate_reason=_FABLE_GATE_REASON,
    ),
    CatalogEntry(
        profile="codex-astra",
        effort="",
        model_id="gpt-6-astra",
        pool="codex",
        grade="S+",
        score=None,
        gate=GATE_CONSULT_ONLY,
        gate_reason=_ASTRA_GATE_REASON,
    ),
)


def _snapshot_model_id(profile_name: str, profile) -> str:
    override = LAUNCH_MODEL_IDS.get(profile_name)
    if override:
        return override
    return profile.aa_agent_model_id or profile.benchmark_model_id or ""


def snapshot_entries() -> tuple[CatalogEntry, ...]:
    """The bundled offline catalog, derived from the reviewed code tables.

    One row per ``(profile, effort)`` in ``GRADE_TABLE``.  A profile listed at
    several grades on the same effort (``devin-swe2`` at A+/A/B) collapses to its
    best grade here, because the canonical store is keyed on ``(profile,
    effort)`` and cannot express the multi-grade listing; the code table keeps it
    and stays authoritative for recommendation output while the catalog is out
    of reach.

    These are *placements*.  The E6 measurement rungs live in
    :func:`e6_arm_entries` instead: they are catalog rows, not placements, and
    the placement snapshot must keep its invariants (Sol stays S+-only).
    """

    best: dict[tuple[str, str], tuple[int, CatalogEntry]] = {}
    for grade, profiles in GRADE_TABLE.items():
        rank = REP_GRADES_ORDER.index(grade)
        for profile in profiles:
            effort = profile.launcher_effort or ""
            key = (profile.name, effort)
            entry = CatalogEntry(
                profile=profile.name,
                effort=effort,
                model_id=_snapshot_model_id(profile.name, profile),
                pool=profile_pool(profile.name)[0],
                grade=grade,
                score=profile.benchmark,
                gate=profile.gate,
                gate_reason=profile.gate_reason,
                benchmark_source=profile.benchmark_source,
                benchmark_annotation=profile.benchmark_annotation,
            )
            existing = best.get(key)
            if existing is None or rank < existing[0]:
                best[key] = (rank, entry)
    entries = [entry for _, entry in best.values()]
    entries.extend(CONSULT_ONLY_SNAPSHOT)
    return tuple(sorted(entries, key=lambda e: (e.profile, CATALOG_EFFORT_RANKS.get(e.effort, 99))))


# #692: the E6 plan the measurement rungs exist for (hk:doc plan/2026-09-25/e6-effort-ladder).
E6_ARM_DEVIATION_REF = "hk:doc plan/2026-09-25/e6-effort-ladder (#594 E6)"


def e6_arm_entries() -> tuple[CatalogEntry, ...]:
    """The E6 measurement rungs as catalog rows (#692).

    Catalog rows, not placements: grade C with no score, never a recommendation
    candidate, never a launcher default.  They are deliberately *not* part of
    :func:`snapshot_entries` — that snapshot mirrors the reviewed placement canon
    and must keep Sol S+-only — so ``bench.catalog_snapshot()`` merges them into
    the catalog view (``bench catalog list``, ``read_catalog``, the canon seed)
    while the placement snapshot stays untouched.
    """

    return tuple(
        CatalogEntry(
            profile=row.name,
            effort=row.launcher_effort or "",
            model_id=_snapshot_model_id(row.name, row),
            pool=profile_pool(row.name)[0],
            grade=E6_ARM_GRADE,
            score=None,
            gate=GATE_DEFAULT,
            benchmark_source=row.benchmark_source,
            benchmark_annotation=row.benchmark_annotation,
            deviation_ref=E6_ARM_DEVIATION_REF,
        )
        for row in E6_ARM_RUNGS
    )


@dataclass(frozen=True)
class LaunchDecision:
    """What a launcher needs, plus where the answer came from."""

    profile: str
    model_id: str
    effort: str
    pool: str
    gate: str
    grade: str
    gate_reason: str | None
    catalog_source: str
    catalog_stale: bool
    catalog_age_s: float | None
    operator_request: bool = False
    # #692: the E6 measurement rung this launch was resolved as ("<profile>@<effort>"),
    # or None for every ordinary launch. Only an E6 arm marker opens one.
    e6_arm: str | None = None

    def as_dict(self) -> dict[str, object]:
        value = dataclasses.asdict(self)
        value["catalog"] = {
            "source": self.catalog_source,
            "stale": self.catalog_stale,
            "age_s": self.catalog_age_s,
        }
        for dropped in ("catalog_source", "catalog_stale", "catalog_age_s"):
            value.pop(dropped)
        return value

    def render(self) -> str:
        lines = [
            f"profile {self.profile}",
            f"model_id {self.model_id or '-'}",
            f"effort {self.effort or '-'}",
            f"pool {self.pool or '-'}",
            f"gate {self.gate}",
            f"grade {self.grade}",
        ]
        if self.gate_reason:
            lines.append(f"gate_reason {self.gate_reason}")
        if self.e6_arm:
            lines.append(f"e6_arm {self.e6_arm}")
        suffix = " (stale)" if self.catalog_stale else ""
        lines.append(f"catalog {self.catalog_source}{suffix}")
        return "\n".join(lines)


def _live_rows(view: CatalogView, profile: str) -> list[CatalogEntry]:
    return [e for e in view.entries if e.profile == profile and not e.retired_at]


def _default_effort(profile: str, rows: list[CatalogEntry]) -> tuple[str, str]:
    """Pick the rung a bare ``policy launch <profile>`` starts on.

    Returns ``(catalog_effort, reported_effort)``.  They differ for a profile the
    catalog keys only on its profile-default row (``effort=""``) while the
    launcher still passes an effort flag — ``grok-hi`` is one: the catalog has a
    single row, the CLI still wants ``--effort high``.

    The launcher's own default wins while the catalog still places it at the
    profile's best grade on a ``default`` gate.  If the canon demotes that rung,
    retires it, or moves it behind a gate, the default follows the catalog to the
    best-graded ordinary rung (cheapest rung on a tie) — which is what makes
    "change the server, the launcher follows" true for effort and not only for
    model ids.
    """

    preferred = DEFAULT_LAUNCH_EFFORTS.get(profile, "")
    ordinary = [row for row in rows if row.gate == GATE_DEFAULT]

    # The launcher's rung wins while the canon still carries it as an ordinary
    # rung.  A *grade* change on that rung is a statement about which tasks the
    # profile may take (``--recommend``/gate), not about which rung ``-m opus``
    # starts on, so it must not silently re-point the launcher.
    if preferred and any(row.effort == preferred for row in ordinary):
        return preferred, preferred

    pool = ordinary or rows
    best_rank = min(REP_GRADES_ORDER.index(row.grade) for row in pool)
    best = [row for row in pool if REP_GRADES_ORDER.index(row.grade) == best_rank]

    # The profile is keyed on its default row only: keep the launcher's rung.
    if any(row.effort == "" for row in best):
        return "", preferred
    # The launcher's rung is gone, retired or now gated — follow the canon to the
    # best-graded ordinary rung, cheapest rung on a tie.
    chosen = min(best, key=lambda row: CATALOG_EFFORT_RANKS.get(row.effort, 99)).effort
    return chosen, chosen


def _known_rung(effort: str, entries: tuple[CatalogEntry, ...] | list[CatalogEntry]) -> bool:
    """Whether a rung name is one the system actually knows.

    Rung names are a closed vocabulary. A catalog may later add one this build
    has never heard of, so a rung the *catalog* carries for this profile counts
    too — but an arbitrary string does not. Without this, `policy launch opus
    --effort bogus` returned rc 0 and echoed "bogus" straight back, and a caller
    that trusted the answer would put it in the agent's argv.

    Retired rows count as known so the caller is told the rung was *retired*
    rather than that it never existed — both refuse, but only one is true.
    """

    return effort in CATALOG_EFFORT_RANKS or any(entry.effort == effort for entry in entries)


def _consult_only_satisfied(profile: str, *, operator_request: bool, purpose: str | None) -> bool:
    """Whether a ``consult_only`` row may proceed.

    An explicit operator request always satisfies it. For an **astra** identity
    only, a declared purpose does too — task #527 made a bare
    ``wrk spawn -m codex-astra`` the architect counsel path (wrk injects
    ``--purpose architect``) and the quota gate admits it, so requiring an
    operator request on top would break an approved invocation. The vocabulary
    is #527's, imported rather than restated: two copies of a permission list
    drift, and the drift direction is "more allowed than intended".

    This applies to astra and nothing else. ``fable``'s consult_only stays
    satisfiable only by ``--operator-request`` — #527 AC⑤ forbids relaxing that
    escalation gate, and a purpose string must never become a second key to it.
    """

    if operator_request:
        return True
    if profile not in ASTRA_ROLE_PROFILES:
        return False
    return (purpose or "").strip().casefold() in ASTRA_ALLOWED_PURPOSES


def _retired_rung(view: CatalogView, profile: str, effort: str) -> bool:
    return any(
        entry.profile == profile and entry.effort == effort and entry.retired_at for entry in view.entries
    )


def resolve_launch(
    profile: str,
    *,
    effort: str | None = None,
    operator_request: bool = False,
    purpose: str | None = None,
    path: str | None = None,
    view: CatalogView | None = None,
    e6_arm: str | None = None,
) -> LaunchDecision:
    """Resolve one launch against the canon, refusing to widen while stale.

    Raises :class:`LaunchError` rather than inventing a value: an unknown
    profile, a rung the catalog retired, or a gate the caller has not been
    cleared for. A server outage is not an argument for any of those (hk:doc
    2558 — a down server is not free dispatch).

    ``e6_arm``(#692) is the raw ``SCOPEFUEL_E6_ARM=<profile>@<effort>`` marker the
    spawner set. A C-graded E6 measurement rung is resolved only when the marker
    names exactly that rung; without it the request keeps the ordinary fallback
    (today's answer for a rung the catalog never placed), so no existing spelling
    changes. Once the canon carries the rung with a measured grade the row is
    ordinary again and the marker is inert.
    """

    view = read_catalog(path=path) if view is None else view
    canonical = PROFILE_ALIASES.get(profile, profile)
    rows = _live_rows(view, canonical)
    from_snapshot = False
    if not rows:
        mentioned = any(entry.profile == canonical for entry in view.entries)
        if mentioned:
            # The catalog carries this profile and every rung of it is retired.
            # That is a statement, and the answer is no.
            raise LaunchError(f"profile '{profile}' is retired in the catalog")
        snapshot_rows = [e for e in snapshot_entries() if e.profile == canonical]
        if not snapshot_rows or view.source == CATALOG_SOURCE_SNAPSHOT:
            raise LaunchError(
                f"profile '{profile}' is not in the catalog"
                + (" (catalog=stale — bundled snapshot only)" if view.stale else "")
            )
        # A partially seeded catalog: canonical for what it covers, silent about
        # this profile. The grade table keeps such profiles so a half-seeded
        # catalog cannot empty it — so launching them has to work too, or
        # ``--recommend`` proposes what nothing can start. They resolve from the
        # bundled snapshot under the stale rules: the canon has not spoken about
        # this profile, so nothing here may widen a gate.
        rows = snapshot_rows
        from_snapshot = True

    requested = normalize_effort(effort)
    marker = parse_e6_arm_marker(e6_arm)
    e6_row = e6_arm_rung_for(canonical, requested)
    e6_open = e6_row is not None and e6_arm_matches(marker, canonical, requested)
    if e6_row is not None and not e6_open:
        # #692: a C-graded E6 measurement rung is not a placement, so without the
        # marker it must not answer for the request. Dropping the row (rather than
        # refusing) keeps every existing spelling exactly as it was: ``wrk -m
        # codex`` pins codex-sol@high and ``-m builder-grok`` pins grok-hi@xhigh,
        # and a spawn that never asked for an E6 arm must not start failing. A row
        # the canon placed above C is a placement and stays.
        rows = [
            row
            for row in rows
            if (row.profile, row.effort) != (canonical, requested) or row.grade != E6_ARM_GRADE
        ]
    profile_entries = (
        rows if from_snapshot else [entry for entry in view.entries if entry.profile == canonical]
    )
    if requested and not _known_rung(requested, profile_entries):
        known = ", ".join(sorted(r for r in CATALOG_EFFORT_RANKS if r))
        raise LaunchError(f"profile '{profile}': unknown effort rung '{requested}' (known: {known})")
    if requested:
        resolved_effort = requested
        fallback_effort, _ = _default_effort(canonical, rows)
    else:
        fallback_effort, resolved_effort = _default_effort(canonical, rows)

    # The row whose gate and grade apply is the row for the rung actually being
    # launched. Resolving the rung first and only then looking it up is what
    # keeps `policy launch opus` (no --effort, default rung high) from landing on
    # the profile-default row's permissive gate while the canon has gated the
    # high rung specifically.
    matched = [row for row in rows if row.effort == resolved_effort]
    if not matched and e6_open:
        # The canon is silent about this rung (a server that has not been seeded
        # with the E6 rows yet). The bundled measurement row is the answer — the
        # marker named it, and its grade C is the statement being made.
        matched = [entry for entry in e6_arm_entries() if entry.effort == resolved_effort]
    if not matched:
        if _retired_rung(view, canonical, resolved_effort):
            raise LaunchError(f"profile '{profile}' rung '{resolved_effort}' is retired in the catalog")
        # The catalog enumerates rungs to say where each one is *placed*, not to
        # list which rungs the CLI accepts — ``wrk -m codex`` runs Sol at effort
        # high, a rung the grade table has never placed. A rung the catalog says
        # nothing about takes the profile's default placement, which is never
        # more permissive than that default row already is.
        matched = [row for row in rows if row.effort == fallback_effort]
        if not matched:
            available = ", ".join(sorted(r.effort or "(default)" for r in rows))
            raise LaunchError(
                f"profile '{profile}' has no '{resolved_effort}' rung in the catalog (have: {available})"
            )
    row = matched[0]
    # The arm is opened only when the resolved row *is* the unmeasured rung the
    # marker named — a canon row placed above C is an ordinary row, and the marker
    # must not claim it as an E6 admission.
    opened_arm = (
        f"{canonical}@{resolved_effort}"
        if e6_open and row.grade == E6_ARM_GRADE and (row.profile, row.effort) == (canonical, resolved_effort)
        else None
    )

    # The gate rules. consult_only always needs an explicit operator request.
    # A non-default gate under a stale catalog needs one too: the snapshot saying
    # "default" is not evidence, because the canon may have raised that gate
    # since — and an unreachable server must never be the reason a profile got
    # easier to start.
    if row.gate == GATE_CONSULT_ONLY and not _consult_only_satisfied(
        canonical, operator_request=operator_request, purpose=purpose
    ):
        remedy = (
            "declare an allowed --purpose (" + ", ".join(sorted(ASTRA_ALLOWED_PURPOSES)) + ")"
            " or pass --operator-request"
            if canonical in ASTRA_ROLE_PROFILES
            else "pass --operator-request"
        )
        raise LaunchError(
            f"profile '{profile}' is consult_only; {remedy}"
            + (f" ({row.gate_reason})" if row.gate_reason else "")
        )
    if (view.stale or from_snapshot) and row.gate != GATE_DEFAULT and not operator_request:
        raise LaunchError(
            f"profile '{profile}' has gate={row.gate} and the catalog is stale; "
            "pass --operator-request (a stale catalog cannot widen a gate)"
        )

    return LaunchDecision(
        profile=canonical,
        model_id=row.model_id,
        effort=resolved_effort,
        pool=row.pool or profile_pool(canonical)[0],
        gate=row.gate,
        grade=row.grade,
        gate_reason=row.gate_reason,
        catalog_source=CATALOG_SOURCE_SNAPSHOT if from_snapshot else view.source,
        catalog_stale=view.stale or from_snapshot,
        catalog_age_s=view.age_s,
        operator_request=operator_request,
        e6_arm=opened_arm,
    )
