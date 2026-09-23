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
    CatalogEntry,
    CatalogView,
    read_catalog,
)
from .recommend import GRADE_TABLE, PROFILE_ALIASES, REP_GRADES_ORDER, profile_pool

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


def _normalize_effort(effort: str | None) -> str | None:
    """Fold an effort to the spelling the catalog is keyed on.

    Rung names are a closed lowercase vocabulary, so ``HIGH`` and ``"high "``
    name the same rung. Matching them raw let a caller miss an exact gated row
    and land on the profile's default placement instead — a gate bypass spelled
    with a capital letter.
    """

    if effort is None:
        return None
    normalized = effort.strip().lower()
    return normalized or None


def _known_rung(effort: str, rows: list[CatalogEntry]) -> bool:
    """Whether a rung name is one the system actually knows.

    Rung names are a closed vocabulary. A catalog may later add one this build
    has never heard of, so a rung the *catalog* carries for this profile counts
    too — but an arbitrary string does not. Without this, `policy launch opus
    --effort bogus` returned rc 0 and echoed "bogus" straight back, and a caller
    that trusted the answer would put it in the agent's argv.
    """

    return effort in CATALOG_EFFORT_RANKS or any(row.effort == effort for row in rows)


def _retired_rung(view: CatalogView, profile: str, effort: str) -> bool:
    return any(
        entry.profile == profile and entry.effort == effort and entry.retired_at for entry in view.entries
    )


def resolve_launch(
    profile: str,
    *,
    effort: str | None = None,
    operator_request: bool = False,
    path: str | None = None,
    view: CatalogView | None = None,
) -> LaunchDecision:
    """Resolve one launch against the canon, refusing to widen while stale.

    Raises :class:`LaunchError` rather than inventing a value: an unknown
    profile, a rung the catalog retired, or a gate the caller has not been
    cleared for. A server outage is not an argument for any of those (hk:doc
    2558 — a down server is not free dispatch).
    """

    view = read_catalog(path=path) if view is None else view
    canonical = PROFILE_ALIASES.get(profile, profile)
    rows = _live_rows(view, canonical)
    if not rows:
        raise LaunchError(
            f"profile '{profile}' is not in the catalog"
            + (" (catalog=stale — bundled snapshot only)" if view.stale else "")
        )

    requested = _normalize_effort(effort)
    if requested and not _known_rung(requested, rows):
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

    # The gate rules. consult_only always needs an explicit operator request.
    # A non-default gate under a stale catalog needs one too: the snapshot saying
    # "default" is not evidence, because the canon may have raised that gate
    # since — and an unreachable server must never be the reason a profile got
    # easier to start.
    if row.gate == GATE_CONSULT_ONLY and not operator_request:
        raise LaunchError(
            f"profile '{profile}' is consult_only; pass --operator-request"
            + (f" ({row.gate_reason})" if row.gate_reason else "")
        )
    if view.stale and row.gate != GATE_DEFAULT and not operator_request:
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
        catalog_source=view.source,
        catalog_stale=view.stale,
        catalog_age_s=view.age_s,
        operator_request=operator_request,
    )
