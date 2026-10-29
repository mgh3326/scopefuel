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
    ASTRA_FORBIDDEN_SEATS,
    ASTRA_ROLE_PROFILES,
    BARRED_MODEL_DECISION_REF,
    CREDIT_PLACED_SEAT_REASON,
    E6_ARM_RUNGS,
    E6_WORKER_TESTER_ONLY_PROFILES,
    E6_WORKER_TESTER_ONLY_REASON,
    EXPERIMENT_REQUEST_REF,
    GRADE_TABLE,
    PROFILE_ALIASES,
    REP_GRADES_ORDER,
    UNGRADED_TRIAL_PROFILES,
    Grade,
    _operator_request_ref_valid,
    astra_purpose_allowed,
    barred_model_id_for_profile,
    credit_builder_seat_denied,
    credit_effort_required_reason,
    credit_placed_seat_denied,
    e6_arm_carrier_grade,
    e6_arm_matches,
    e6_arm_rung_for,
    e6_only_profile,
    e6_registered_rungs,
    e6_worker_tester_seat_denied,
    is_barred_model_id,
    is_e6_unmeasured_key,
    normalize_effort,
    parse_e6_arm_marker,
    profile_pool,
    profile_subscription,
    provider_family,
    round_ext_role_allowed,
    worker_tester_rungs,
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
    # #920: alias sonnet serves claude-sonnet-5-5 since 2026-09-29 (hk:doc
    # report/2026-09-29/sonnet55-catalog-and-reps-status) — the catalog identity
    # follows the served model so new reps land under the exact new id while
    # pre-refresh claude-sonnet-5 reps stay attributed to the old model.
    "sonnet": "claude-sonnet-5-5",
    # #1269: alias haiku serves claude-haiku-5-5 since 2026-10-08 (hk:doc
    # brief/2026-10-08/haiku55-scopefuel-catalog) — the same refresh as #920
    # sonnet: new reps land under the exact new id while pre-refresh
    # claude-haiku-4.5 reps stay attributed to the old model.
    "haiku": "claude-haiku-5-5",
    "devin-swe2": "swe-2",
    "devin-glm52": "glm-5-2",
    "devin-swe17": "swe-1-7",
    "devin-ds41": "deepseek-v4-1-flash-high",
    # #635: devin effort lives in the model id, one profile per rung.
    "devin-swe2-medium": "swe-2-medium",
    "devin-swe2-max": "swe-2-max",
    "devin-ds41-max": "deepseek-v4-1-flash-max",
    # #1380: the paid fusion lane — Claude fusion rungs served on the devin
    # pool; the rung is baked into the model id like every devin spelling.
    "devin-fusion-opus55": "fusion-claude-opus-5-5-high-sidekick-swe-2-medium",
    "devin-fusion-sonnet55": "fusion-claude-sonnet-5-5-high-sidekick-swe-2-medium",
    # hk 1729: Fusion GPT-6.1 Sol lead on the Codex pool; effort is baked into
    # each model id and the SWE-2 medium sidekick is the same on both rows.
    "devin-fusion-sol61": "fusion-gpt-6-1-sol-high-sidekick-swe-2-medium",
    "devin-fusion-sol61-xhigh": "fusion-gpt-6-1-sol-xhigh-sidekick-swe-2-medium",
    # #1620: the paid Argon trial lane (phase 1) — Gemini 4 Argon rungs served
    # on the devin pool; the rung is baked into the model id. devin-argon is
    # the high default, the same shape devin-swe2 -> swe-2 (high) follows.
    "devin-argon": "gemini-4-argon-high",
    "devin-argon-medium": "gemini-4-argon-medium",
    "devin-argon-low": "gemini-4-argon-low",
    # Same model as kimi-k3; the differentiator is KIMI_CODE_HOME, not the model.
    "kimi-k3-low": "kimi-k3",
    "oc-qwen37-max": "qwen3.7-max",
    "oc-minimax-m3": "minimax-m3",
    "oc-solar4": "solar-pro4",
    # A router, not a model — this is the identity the launcher actually requests.
    "oc-omni": "omniroute/auto/coding",
    # hk 2079: the pi Anthropic API credit profiles. The id is the base model pi
    # receives; wrk appends the rung as the ``:<thinking>`` suffix.
    "pi-haiku55": "anthropic/claude-haiku-5-5",
    "pi-sonnet55": "anthropic/claude-sonnet-5-5",
    "pi-opus55": "anthropic/claude-opus-5-5",
    # hk 2141: the cmd Command Code profile. The id is the model cmd's
    # ``--model`` flag receives; wrk passes the rung as ``--effort``.
    "cmd-dsflash41": "deepseek/deepseek-v4.1-flash",
    # hk 2110: gpt-6.1-terra — the codex-terra61 E6 rungs all launch the same
    # model id. There is no AA mapping (the model is unmeasured), so the id
    # lives here and ``_snapshot_model_id`` picks it up for the catalog rows.
    "codex-terra61": "gpt-6.1-terra",
}

# The rung a profile starts on when the caller pins no effort.  Only profiles
# whose launcher accepts ``--effort`` carry one; "" means the launcher passes no
# effort flag at all (kimi/devin/opencode/agy).
DEFAULT_LAUNCH_EFFORTS: dict[str, str] = {
    "opus": "high",
    "sonnet": "high",
    "haiku": "low",
    "codex-sol": "max",
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
    # hk 2079: pi's thinking level is the effort (wrk maps ``--effort`` onto the
    # model's ``:<thinking>`` suffix), so these profiles take the flag. The value
    # mirrors the claude profile of the same model and is only the one rung
    # ``--recommend`` offers: a bare pi spelling names no rung and is refused by
    # gate and launch (recommend.credit_effort_required_reason).
    "pi-haiku55": "low",
    "pi-sonnet55": "high",
    "pi-opus55": "high",
    # hk 2141: cmd's ``--effort`` takes the model's own levels (the
    # deepseek-v4.1-flash run answered "Supported: off, low, high, max"), so
    # this profile takes the flag. The value is only the one rung
    # ``--recommend`` offers: a bare cmd spelling names no rung and is refused
    # by gate and launch (recommend.credit_effort_required_reason).
    "cmd-dsflash41": "high",
    # hk 2110: the codex launcher takes --effort for codex-terra61, so the
    # profile belongs in the flag table (a bare request is refused
    # e6_arm_required before this default could ever launch a rung — the value
    # is the flag registration, not an admissible default).
    "codex-terra61": "max",
}


def launcher_accepts_effort_flag(profile: str) -> bool:
    """Whether bin/wrk accepts ``--effort`` for this profile.

    ``DEFAULT_LAUNCH_EFFORTS`` keys exactly the launchers that take an effort
    flag (wrk's ``EFFORT_SUPPORTED=1``); the devin/kimi/opencode/agy spellings
    bake the rung into the profile or model id, so wrk rejects the flag
    outright (``--effort is unsupported for profile '<name>'``).
    """
    return profile in DEFAULT_LAUNCH_EFFORTS


def launch_spelling(profile: str, effort: str | None) -> str:
    """The ``wrk spawn -m`` fragment a catalog rung launches as.

    A flag-taking launcher spells a rung ``<name> --effort <rung>``; on a
    no-flag launcher the rung is already part of the profile name, so the
    accepted spelling is the bare profile name.
    """
    if effort and launcher_accepts_effort_flag(profile):
        return f"{profile} --effort {effort}"
    return profile


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

# #1620 (operator decision hk:doc owner/fleet/decision/2026-10-18/devin-argon-trial,
# phase 1, approved by operator-desk 10-18 r1018b953go): the paid Argon trial
# lane is gate-able but carries NO grade placement — the operator placed none,
# and phase 1 is spawn plumbing only. The catalog grade column is NOT NULL, so
# the rows carry C as an inert carrier — the weakest rung and the same "not a
# placement" value the E6 measurement rows use — never an actual placement:
# the profiles are absent from GRADE_TABLE and
# ``bench._catalog_grade_table`` skips them, so ``--recommend`` can never offer
# one even when the canon is seeded with these rows. ``gate=default`` means
# ``gate -m`` judges each spelling on the devin pool's account Daily/Weekly
# axis (the D3 not-in-table path) — never on a SWE-2 Free model-scope bucket.
UNGRADED_TRIAL_SNAPSHOT: tuple[CatalogEntry, ...] = tuple(
    CatalogEntry(
        profile=profile.name,
        effort="",
        model_id=LAUNCH_MODEL_IDS[profile.name],
        pool=profile_pool(profile.name)[0],
        grade="C",
        score=None,
        gate=GATE_DEFAULT,
        benchmark_annotation=profile.benchmark_annotation,
    )
    for profile in UNGRADED_TRIAL_PROFILES
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
    entries = [_arm_grade_override(entry) for _, entry in best.values()]
    entries.extend(CONSULT_ONLY_SNAPSHOT)
    # #1620: the ungraded Argon trial rows ride the same append — launchable
    # and gate-able, never placements.
    entries.extend(UNGRADED_TRIAL_SNAPSHOT)
    return tuple(sorted(entries, key=lambda e: (e.profile, CATALOG_EFFORT_RANKS.get(e.effort, 99))))


# #692: the E6 plan the measurement rungs exist for (hk:doc plan/2026-09-25/e6-effort-ladder).
E6_ARM_DEVIATION_REF = "hk:doc plan/2026-09-25/e6-effort-ladder (#594 E6)"


@dataclass(frozen=True)
class ArmGradeOverride:
    """An operator-approved grade move stamped on one bundled arm row (#781).

    Hosts read the bundled catalog snapshot — the server catalog is not in use
    (#712 on hold) — so a ``grades apply`` artifact never reaches them.  This
    entry is the code-side record of the approved grade: the bundled catalog
    row carries the decided grade and the decision provenance exactly the way
    the pushed canon row would have carried them.
    """

    grade: Grade
    decided_by: str
    decided_at: str
    deviation_ref: str


# #781: the arm rungs' approved grades — exactly the rungs the operator decision
# named (hk:doc 5177 item 2, evidence reps srv:973, srv:976, srv:988).  The other
# rungs the same decision reviewed — codex-terra@medium, kimi-k3@high, oc-solar4
# — were NOT applied and keep their rows unchanged.
#
# #787: ``recommend.GRADE_TABLE`` now carries ``devin-swe2-medium`` at A too —
# the placement canon follows the same decision, so the override's *grade*
# restatement is a no-op.  The entry stays anyway: it is the only carrier of
# the decision provenance (``decided_by``/``decided_at``/``deviation_ref``) on
# the bundled catalog row.  Removing it would keep grade A (from GRADE_TABLE)
# but strip the provenance from ``bench catalog list``, the grades rung
# detail, and the canon seed emit — an observable change, so the row remains.
# The ``E6_ARM_RUNGS`` measurement rungs still stay marker-gated
# non-candidates; a server-canonical host places a stamped row at its grade
# (``bench._catalog_grade_table`` filters only unmeasured-C arm rows — the
# measured->ordinary transition).
#
# #1297 (operator 2026-10-08 via operator-desk): five more rows the desk applied
# on the served canon get the same stamp so a full seed push cannot revert the
# decisions — sonnet@high C -> B (hk:task/1297, decided_at
# 2026-10-08T11:52:05.815922Z), and hk:task/1296's four (decided_at
# 2026-10-08T13:03:23.617077Z): devin-swe2@high and devin-swe2-max@max to A+,
# grok-hi@xhigh to A, oc-solar4 B -> A. The three E6 rungs graduated out of
# ``E6_ARM_RUNGS`` into ``recommend.GRADE_TABLE`` — measured rungs are ordinary
# placements — so every grade restatement here is again a no-op; the entries
# carry the decision provenance exactly as the applied canon rows do.
ARM_GRADE_OVERRIDES: dict[tuple[str, str], ArmGradeOverride] = {
    ("devin-swe2-medium", ""): ArmGradeOverride(
        grade="A",
        decided_by="operator:2026-09-27 via operator-desk",
        decided_at="2026-09-27",
        deviation_ref="hk:doc 5177 item 2 (evidence srv:973, srv:976, srv:988)",
    ),
    # hk 1788 (b953-hk1786): sonnet@high B -> A — the propose report grades it
    # rule 1.2 on 8 clean A passes and 0 FAIL (action promote, target A). The
    # bundled catalog row carries the new decision provenance so a full seed
    # push cannot revert it; the superseded #1297 stamp is superseded here too.
    ("sonnet", "high"): ArmGradeOverride(
        grade="A",
        decided_by="b953-hk1786",
        decided_at="2026-10-21",
        deviation_ref="hk:doc/reports/b953/hk1786-propose-sonnet-high",
    ),
    ("devin-swe2", "high"): ArmGradeOverride(
        grade="A+",
        decided_by="operator:2026-10-08 via operator-desk",
        decided_at="2026-10-08T13:03:23.617077Z",
        deviation_ref=(
            "hk:task/1296 (evidence srv:686, srv:687, srv:693, srv:730, srv:733, "
            "srv:737, srv:738, srv:745, srv:750, srv:759, srv:828)"
        ),
    ),
    ("devin-swe2-max", "max"): ArmGradeOverride(
        grade="A+",
        decided_by="operator:2026-10-08 via operator-desk",
        decided_at="2026-10-08T13:03:23.617077Z",
        deviation_ref="hk:task/1296 (evidence srv:1225, srv:1300, srv:1306, srv:1321, srv:1375, srv:1383)",
    ),
    ("grok-hi", "xhigh"): ArmGradeOverride(
        grade="A",
        decided_by="operator:2026-10-08 via operator-desk",
        decided_at="2026-10-08T13:03:23.617077Z",
        deviation_ref="hk:task/1296 (evidence srv:1396, srv:1397)",
    ),
    ("oc-solar4", ""): ArmGradeOverride(
        grade="A",
        decided_by="operator:2026-10-08 via operator-desk",
        decided_at="2026-10-08T13:03:23.617077Z",
        deviation_ref="hk:task/1296 (evidence srv:705, srv:707)",
    ),
    # hk 1788 (hk 1786 phase 0, hk:doc/briefs/b953/tier-coverage): the four new
    # E6 measurement rungs. The grade restatement is each rung's own carrier
    # (A+ for sonnet@max — a C carrier would be refused non-monotonic under
    # sonnet@xhigh's A+; C for the other three) — the stamp exists to carry
    # the decision provenance on the bundled catalog rows, the #787 shape.
    ("codex-sol", "low"): ArmGradeOverride(
        grade="C",
        decided_by="b953-hk1786",
        decided_at="2026-10-21",
        deviation_ref="hk:doc/briefs/b953/tier-coverage",
    ),
    ("haiku", "xhigh"): ArmGradeOverride(
        grade="C",
        decided_by="b953-hk1786",
        decided_at="2026-10-21",
        deviation_ref="hk:doc/briefs/b953/tier-coverage",
    ),
    ("haiku", "max"): ArmGradeOverride(
        grade="C",
        decided_by="b953-hk1786",
        decided_at="2026-10-21",
        deviation_ref="hk:doc/briefs/b953/tier-coverage",
    ),
    ("sonnet", "max"): ArmGradeOverride(
        grade="A+",
        decided_by="b953-hk1786",
        decided_at="2026-10-21",
        deviation_ref="hk:doc/briefs/b953/tier-coverage",
    ),
}


def _arm_grade_override(entry: CatalogEntry) -> CatalogEntry:
    """Restate one bundled row with its approved arm grade and provenance (#781)."""

    override = ARM_GRADE_OVERRIDES.get(entry.key)
    if override is None:
        return entry
    return dataclasses.replace(
        entry,
        grade=override.grade,
        deviation_ref=override.deviation_ref,
        decided_at=override.decided_at,
        decided_by=override.decided_by,
    )


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
        _arm_grade_override(
            CatalogEntry(
                profile=row.name,
                effort=row.launcher_effort or "",
                model_id=_snapshot_model_id(row.name, row),
                pool=profile_pool(row.name)[0],
                grade=e6_arm_carrier_grade(row.name, row.launcher_effort),
                score=None,
                gate=GATE_DEFAULT,
                benchmark_source=row.benchmark_source,
                benchmark_annotation=row.benchmark_annotation,
                deviation_ref=E6_ARM_DEVIATION_REF,
            )
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
    # #1340+#1380: the launch billing class the bundled table holds for this
    # profile ("free" | "paid" | "unknown") — the recommend rank's paid-within
    # pool flag, disclosed on the launch verdict so the launcher sees the same
    # cost class without reading GRADE_TABLE itself. Not a wire column: a
    # profile with no bundled row reports "unknown", the neutral class.
    billing: str = "unknown"
    # #1380: the tester-separation provider family of the underlying model —
    # recommend.provider_family(), NOT the quota pool. The paid devin fusion
    # rows report "claude" (they serve Claude under the hood) while pool stays
    # "devin" (the spend is Devin credits).
    family: str = ""

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
            f"family {self.family or '-'}",
            f"billing {self.billing}",
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


@dataclass(frozen=True, kw_only=True)
class _RoundExtensionBuilderLaunch(LaunchDecision):
    """Only exceptional builder responses attest the hk 1672 role policy."""

    round_extension_builder: dict[str, str]


@dataclass(frozen=True, kw_only=True)
class _RequestedE6Launch(LaunchDecision):
    """A marker-admitted E6 request retains its durable audit reference."""

    operator_request_ref: str

    def render(self) -> str:
        return super().render() + f"\noperator_request_ref {self.operator_request_ref}"


@dataclass(frozen=True, kw_only=True)
class _ExperimentRetiredLaunch(LaunchDecision):
    """Only a retired experiment admission adds its rung and durable REF."""

    experiment_retired: str
    operator_request_ref: str

    def render(self) -> str:
        return (
            super().render()
            + f"\nexperiment_retired {self.experiment_retired}"
            + f"\noperator_request_ref {self.operator_request_ref}"
        )


def _live_rows(view: CatalogView, profile: str) -> list[CatalogEntry]:
    return [e for e in view.entries if e.profile == profile and not e.retired_at]


def _unmeasured_e6_row(row: CatalogEntry) -> bool:
    """A C-graded E6 measurement row (#692) — a catalog row, never a placement.

    Once the canon places the rung above C the row is an ordinary placement and
    this returns False, which is what lets a measured rung become a candidate
    again.
    """

    return is_e6_unmeasured_key(row.profile, row.effort) and row.grade == e6_arm_carrier_grade(
        row.profile, row.effort
    )


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


def _consult_only_satisfied(profile: str, *, operator_request: bool | str, purpose: str | None) -> bool:
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

    if profile in ASTRA_ROLE_PROFILES and (
        profile in ASTRA_FORBIDDEN_SEATS or (purpose or "").strip().casefold() == "round-extension"
    ):
        return astra_purpose_allowed(profile, purpose, operator_request)
    if operator_request:
        return True
    if profile not in ASTRA_ROLE_PROFILES:
        return False
    return astra_purpose_allowed(profile, purpose)


def _retired_rung(view: CatalogView, profile: str, effort: str) -> bool:
    return any(
        entry.profile == profile and entry.effort == effort and entry.retired_at for entry in view.entries
    )


def _bundled_billing(profile_name: str) -> str:
    """The launch billing class the bundled table holds for this profile.

    ``recommend.Profile.billing`` is bundled-table data — the wire carries no
    billing column (its #1340 docstring is explicit), so the launch verdict
    reports the same flag the recommend rank reads. A profile with no bundled
    row at all (a server-introduced name) reports "unknown", the neutral
    class that never moves a row.
    """

    for profiles in GRADE_TABLE.values():
        for profile in profiles:
            if profile.name == profile_name:
                return profile.billing
    # #1620: the ungraded trial lane rows hold their billing flag outside
    # GRADE_TABLE — the launch verdict still reports "paid", not "unknown".
    for profile in UNGRADED_TRIAL_PROFILES:
        if profile.name == profile_name:
            return profile.billing
    return "unknown"


def resolve_launch(
    profile: str,
    *,
    effort: str | None = None,
    operator_request: bool | str = False,
    purpose: str | None = None,
    role: str | None = None,
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

    barred_model_id = barred_model_id_for_profile(profile)
    if barred_model_id:
        raise LaunchError(
            f"profile '{profile}' refused: {barred_model_id} is barred by {BARRED_MODEL_DECISION_REF}"
        )

    if not round_ext_role_allowed(profile, role, purpose, operator_request):
        raise LaunchError(
            "role_restricted: astra/fable builder/tester/reviewer requires round-extension + round-ext ref; "
            "worker/captain and removed astra aliases forbidden"
        )

    if (
        profile in ASTRA_ROLE_PROFILES
        and (profile in ASTRA_FORBIDDEN_SEATS or (purpose or "").strip().casefold() == "round-extension")
        and not astra_purpose_allowed(profile, purpose, operator_request)
    ):
        raise LaunchError(
            f"role_restricted: {profile}: round-extension requires a round-ext document ref; "
            "builder/captain astra forbidden"
        )

    # hk 2021: the REF shape is checked on every path, after the astra/fable role
    # checks above (the gate runs them in this order too). The refusal repr-quotes
    # the REF, so a newline or control byte cannot start a second output line.
    if isinstance(operator_request, str) and not _operator_request_ref_valid(operator_request):
        raise LaunchError(
            f"operator_request_ref_invalid: {operator_request!r} — hk:doc/<key> 또는 hk:task/<정수> 만 허용"
        )

    view = read_catalog(path=path) if view is None else view
    canonical = PROFILE_ALIASES.get(profile, profile)
    rows = _live_rows(view, canonical)

    requested = normalize_effort(effort)
    marker = parse_e6_arm_marker(e6_arm)
    if requested is None and canonical in E6_WORKER_TESTER_ONLY_PROFILES:
        # hk 2079: pi's thinking level is the effort, so a credit profile is launched
        # with an explicit effort and nothing else names a rung — not the marker, not
        # a launcher default, and never the canon's best-graded sibling. The same line
        # the gate prints.
        raise LaunchError(credit_effort_required_reason(canonical))
    # hk 2185: the credit rung vocabulary is closed. A canon row under an
    # unregistered key (off/default/bogus) must not become launchable just
    # because the canon carries it — pi/cmd would reject the effort anyway.
    if (
        canonical in E6_WORKER_TESTER_ONLY_PROFILES
        and requested is not None
        and requested not in worker_tester_rungs(canonical)
    ):
        raise LaunchError(
            f"profile '{profile}': not placed at effort '{requested}' "
            f"(placed rungs: {'|'.join(worker_tester_rungs(canonical))})"
        )
    if (
        requested is None
        and marker is not None
        and marker.profile == canonical
        and e6_only_profile(canonical)
    ):
        # hk 2110: an E6-only profile has no placement default to name a rung —
        # the marker is the only way a bare ``policy launch codex-terra61`` can
        # name one at all (the same asymmetry the gate has). On a placed
        # profile the ambient marker must NOT re-point a bare request onto its
        # rung: the default placement still answers (leaked-env safety).
        requested = marker.effort
    experiment_effort = requested or (marker.effort if marker and marker.profile == canonical else None)
    experiment_row = next(
        (entry for entry in view.entries if entry.key == (canonical, experiment_effort) and entry.retired_at),
        None,
    )
    experiment_retired = (
        f"{canonical}@{experiment_effort}"
        if experiment_row is not None
        and operator_request == EXPERIMENT_REQUEST_REF
        and e6_arm_matches(marker, canonical, experiment_effort)  # hk1990-experiment
        else None
    )
    if experiment_retired:
        requested = experiment_effort
        # Local launch projection only: retirement/placement in the catalog is unchanged.
        rows.append(dataclasses.replace(experiment_row, gate=GATE_CONSULT_ONLY, retired_at=None))
    if any(is_barred_model_id(row.model_id) for row in rows):
        barred = next(row.model_id for row in rows if is_barred_model_id(row.model_id))
        raise LaunchError(f"profile '{profile}' refused: {barred} is barred by {BARRED_MODEL_DECISION_REF}")
    e6_row = e6_arm_rung_for(canonical, requested)
    e6_open = e6_row is not None and e6_arm_matches(marker, canonical, requested)
    if (
        not e6_open
        and operator_request
        and any(
            row.effort == requested and row.gate != GATE_CONSULT_ONLY and _unmeasured_e6_row(row)
            for row in rows
        )
    ):
        # R4: a REF cannot turn an unmarked measurement request into a fallback launch.
        raise LaunchError(
            f"e6_arm_required: {canonical}@{requested} requires SCOPEFUEL_E6_ARM={canonical}@{requested}"
        )
    if not e6_open:
        # #692: a C-graded E6 measurement rung is not a placement, so no unmarked
        # request may be answered by one. Dropping the row (rather than refusing)
        # keeps every existing spelling exactly as it was: ``wrk -m codex`` pins
        # codex-sol@high and ``-m builder-grok`` pins grok-hi@xhigh, and a spawn
        # that never asked for an E6 arm must not start failing. A row the canon
        # placed above C is a placement and stays.
        #
        # The whole profile's unmeasured rows go, not just the requested rung:
        # ``_default_effort`` walks the ordinary rows, so once the canon retires
        # or gates the preferred rung a leftover C row became the "best-graded
        # ordinary rung" — an unmarked ``policy launch grok-hi`` resolved the
        # unmeasured xhigh rung and the canon's own retirement stopped refusing.
        # hk 2079: the credit profiles are the exception — a consult_only gate on
        # an unmeasured credit row does not lift the marker and seat rules.
        # hk 2110 generalizes: the exception applies only to profiles that also
        # have placements — an E6-only profile's unmeasured row stays refused
        # under any gate spelling.
        rows = [
            row
            for row in rows
            if (row.gate == GATE_CONSULT_ONLY and not e6_only_profile(canonical))
            or not _unmeasured_e6_row(row)
        ]

    from_snapshot = False
    if not rows:
        mentioned = any(entry.profile == canonical for entry in view.entries)
        if mentioned and e6_only_profile(canonical) and not e6_open:
            # hk 2079 + hk 2110: an E6-only profile has no placement to fall
            # back on — name the marker it needs. hk 2103: when e6_open already
            # names the rung but the live set is empty, every row was retired;
            # use the "retired in the catalog" refusal below instead of asking
            # for a marker that is already set.
            raise LaunchError(
                f"e6_arm_required: {canonical}@{requested} requires SCOPEFUEL_E6_ARM={canonical}@{requested}"
                if requested is not None
                else f"e6_arm_required: {canonical} 은 미측정 E6 측정 런그뿐이다 (#594) — "
                f"SCOPEFUEL_E6_ARM={canonical}@<rung> 표식이 등록된 런그 "
                f"({', '.join(e6_registered_rungs(canonical))}) 하나를 가리켜야 연다"
            )
        if mentioned:
            # The catalog carries this profile and every rung of it is retired —
            # or (with the E6 rows dropped above) it carries no placement for it
            # at all. Either way the canon has spoken and the answer is no.
            raise LaunchError(f"profile '{profile}' is retired in the catalog")
        snapshot_rows = [e for e in snapshot_entries() if e.profile == canonical]
        if not snapshot_rows and e6_open:
            # hk 2079: a profile that exists only as E6 rows (the pi credit
            # profiles) has no placement in the bundled snapshot either. A served
            # canon that has not been seeded with it yet is silent, and the marker
            # names the rung — the bundled measurement row answers, exactly as it
            # does for an unseeded rung of a placed profile below.
            snapshot_rows = [
                entry for entry in e6_arm_entries() if (entry.profile, entry.effort) == (canonical, requested)
            ]
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
    matched_effort = resolved_effort
    if not matched and e6_open and not _retired_rung(view, canonical, resolved_effort):
        # The canon is silent about this rung (a server that has not been seeded
        # with the E6 rows yet). The bundled measurement row is the answer — the
        # marker named it, and its grade C is the statement being made. Keyed on
        # the (profile, effort) pair, not the effort alone: two E6 rungs share an
        # effort name (codex-sol@high and kimi-k3@high), and an effort-only match
        # would hand a kimi arm the codex row. A rung the canon retired is *not*
        # revived by the marker — retirement is the canon closing the rung.
        matched = [
            entry
            for entry in e6_arm_entries()
            if (entry.profile, entry.effort) == (canonical, resolved_effort)
        ]
    if not matched:
        if _retired_rung(view, canonical, resolved_effort):
            raise LaunchError(f"profile '{profile}' rung '{resolved_effort}' is retired in the catalog")
        if e6_only_profile(canonical):
            # hk 2079 + hk 2110: an E6-only profile has no default placement for
            # an unknown or unmeasured rung to fall back on — borrowing a
            # measured sibling row would graduate it. The rung needs its own
            # row or the E6 marker.
            raise LaunchError(
                f"e6_arm_required: {canonical}@{resolved_effort} requires "
                f"SCOPEFUEL_E6_ARM={canonical}@{resolved_effort}"
            )
        # The catalog enumerates rungs to say where each one is *placed*, not to
        # list which rungs the CLI accepts — ``wrk -m codex`` runs Sol at effort
        # high, a rung the grade table has never placed. A rung the catalog says
        # nothing about takes the profile's default placement, which is never
        # more permissive than that default row already is.
        matched = [row for row in rows if row.effort == fallback_effort]
        matched_effort = fallback_effort
        if not matched:
            available = ", ".join(sorted(r.effort or "(default)" for r in rows))
            raise LaunchError(
                f"profile '{profile}' has no '{resolved_effort}' rung in the catalog (have: {available})"
            )
    if len(matched) > 1:
        # hk 2103: two live canon rows for one rung are ambiguous — the gate
        # collects every row for the key (a consult_only twin refuses), so
        # launch must not quietly take the first. Fail closed for every
        # profile, not only the credit profiles.
        raise LaunchError(
            f"profile '{profile}' has {len(matched)} live canon rows for rung '{matched_effort}'; "
            "refusing to pick one"
        )
    row = matched[0]
    # The arm is opened only when the resolved row *is* the unmeasured rung the
    # marker named — a canon row placed above C is an ordinary row, and the marker
    # must not claim it as an E6 admission.
    opened_arm = (
        f"{canonical}@{resolved_effort}"
        if e6_open
        and row.grade == e6_arm_carrier_grade(row.profile, row.effort)
        and (row.profile, row.effort) == (canonical, resolved_effort)
        else None
    )

    # hk 2079: the anthropic-api credit rows are worker/tester only until reps
    # exist — any other seat is refused even with the E6 marker. hk 2185: the
    # rows are ordinary inherited placements now, so the same seat rule applies
    # without a marker (a placement is not a measurement).
    # op-1028-seattask: a placed rung may also take the builder seat; an E6
    # marker carrier keeps the worker/tester-only rule.
    if opened_arm is not None and e6_worker_tester_seat_denied(canonical, role):
        raise LaunchError(f"role_restricted: {opened_arm} — {E6_WORKER_TESTER_ONLY_REASON}")
    if opened_arm is None and credit_placed_seat_denied(canonical, role):
        raise LaunchError(f"role_restricted: {canonical}@{resolved_effort} — {CREDIT_PLACED_SEAT_REASON}")
    # A rung the catalog does not place takes a sibling's row (the fallback
    # above); that borrowed row must not open a builder seat (hk 2188 r3, 219-a).
    own_row = row if (row.profile, row.effort) == (canonical, resolved_effort) else None
    if opened_arm is None and credit_builder_seat_denied(canonical, role, own_row):
        raise LaunchError(f"role_restricted: {canonical}@{resolved_effort} — {E6_WORKER_TESTER_ONLY_REASON}")

    # task #742 — 구독 해지는 게이트 규칙보다 먼저 선다. 플래그는 config 상태라
    # 카탈로그·snapshot·arm 어떤 경로로도 동일하게 닫히고 --operator-request 가
    # 열지 못한다. 행 자체는 지우지 않는다 — pool 판정은 gate 와 같은 축
    # (profile_pool 이름표 우선, 없으면 카탈로그 행의 pool)으로 본다.
    subscription = profile_subscription(canonical, profile_pool(canonical)[0] or row.pool)
    if not subscription.subscribed:
        raise LaunchError(f"profile '{profile}' is unsubscribed ({subscription.reason})")

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
    # A consult_only row that reached this point was admitted by the check
    # above (an operator request, or an astra identity with an allowed
    # purpose). consult_only is the narrowest gate — the canon cannot have
    # moved anywhere more restrictive — so a stale view cannot widen it and
    # must not refuse it again here (#1444: under staleness an astra launch
    # otherwise had no flag combination left that the quota gate also admits).
    if (
        (view.stale or from_snapshot)
        and row.gate != GATE_DEFAULT
        and row.gate != GATE_CONSULT_ONLY
        and not operator_request
    ):
        raise LaunchError(
            f"profile '{profile}' has gate={row.gate} and the catalog is stale; "
            "pass --operator-request (a stale catalog cannot widen a gate)"
        )

    decision = LaunchDecision(
        profile=canonical,
        model_id=row.model_id,
        effort=resolved_effort,
        pool=(
            profile_pool(canonical)[0]
            if canonical in E6_WORKER_TESTER_ONLY_PROFILES
            else row.pool or profile_pool(canonical)[0]
        ),
        gate=row.gate,
        grade=row.grade,
        gate_reason=row.gate_reason,
        catalog_source=CATALOG_SOURCE_SNAPSHOT if from_snapshot else view.source,
        catalog_stale=view.stale or from_snapshot,
        catalog_age_s=view.age_s,
        operator_request=bool(operator_request),
        e6_arm=opened_arm,
        billing=_bundled_billing(canonical),
        family=provider_family(canonical),
    )
    if experiment_retired:
        return _ExperimentRetiredLaunch(
            **dataclasses.asdict(decision),
            experiment_retired=experiment_retired,
            operator_request_ref=EXPERIMENT_REQUEST_REF,
        )
    if opened_arm and isinstance(operator_request, str):
        return _RequestedE6Launch(**dataclasses.asdict(decision), operator_request_ref=operator_request)
    if profile in {"codex-astra", "fable"} and (role or "").strip().casefold() == "builder":
        return _RoundExtensionBuilderLaunch(
            **dataclasses.asdict(decision),
            round_extension_builder={
                "policy": "hk1672/v1",
                "profile": canonical,
                "role": "builder",
                "purpose": "round-extension",
                "ref": str(operator_request),
            },
        )
    return decision
