"""#1318 part 2 (operator decision A, hk 1321): rep-measured rows one grade up.

``--recommend G`` also lists every rep-measured row placed exactly one grade
above G, tagged ``[one-up <grade>]``; estimate-only, AA-agent-only, unmeasured
and E6 arm rows never move down.
"""

from __future__ import annotations

import datetime as dt
import json

from scopefuel import bench, cli
from scopefuel import recommend as recommend_mod
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    _GRADE_ORDER,
    GRADE_TABLE,
    Profile,
    Subscription,
    _alt_candidates,
    _one_up_profiles,
    is_rep_measured,
    profile_pool,
    recommend,
    recommend_dict,
)

NOW = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
GRADES = _GRADE_ORDER

# The bundled catalog's rep-measured rows, per grade — generated from the table,
# pinned here so a silent catalog drift fails loudly. Keyed by display label
# (name + effort rung): bare devin-swe2 sits at three grades as a non-rep row.
EXPECTED_REP_MEASURED = {
    "A+": {"devin-ds41", "devin-swe2 --effort high", "devin-swe2-max --effort max"},
    "A": {"devin-swe2-medium", "grok-hi --effort xhigh", "oc-solar4"},
    "B": {"sonnet --effort high"},
}


def _bucket(window: str = "7d", scope: Scope | None = None, used: float = 10.0) -> Bucket:
    return Bucket(
        label=window,
        window=window,
        used_pct=used,
        resets_at=(NOW + dt.timedelta(days=6)).isoformat(),
        scope=scope or Scope("account"),
        horizon="week",  # type: ignore[arg-type]
    )


def _pool_providers() -> list[ProviderResult]:
    """One measurable ProviderResult per (provider, group) the table routes to."""
    pool_scopes: dict[str, set[str | None]] = {}
    for profiles in GRADE_TABLE.values():
        for profile in profiles:
            provider_id, group_name = profile_pool(profile.name)
            assert provider_id, profile.name
            pool_scopes.setdefault(provider_id, set()).add(group_name)
    providers = []
    for provider_id, group_names in pool_scopes.items():
        providers.append(
            ProviderResult(
                id=provider_id,
                pool_class="preserve",
                buckets=[
                    _bucket(scope=Scope("account") if g is None else Scope("group", g)) for g in group_names
                ],
            )
        )
    return providers


def _label(profile: Profile) -> str:
    effort = f" --effort {profile.launcher_effort}" if profile.launcher_effort else ""
    return f"{profile.name}{effort}"


def _line_label(line: str) -> str:
    tokens = line.split(".", 1)[1].strip().split()
    while tokens and tokens[0].startswith("🔥"):
        tokens.pop(0)
    label = tokens[0]
    if len(tokens) >= 3 and tokens[1] == "--effort":
        label += f" --effort {tokens[2]}"
    return label


def _ranked(output: str) -> list[str]:
    """Ranked-line labels (``N. <name> [--effort e] …``)."""
    return [_line_label(line) for line in output.splitlines() if line[:1].isdigit()]


def _grade_index(grade: str) -> int:
    return GRADES.index(grade)  # type: ignore[arg-type]


# ── predicate & selected rows ────────────────────────────────────────────────


def test_rep_measured_predicate_selects_exactly_the_rep_evidence_rows():
    """The bundled catalog rows whose grade evidence names reps — nothing else."""
    selected = {
        grade: {_label(p) for p in profiles if is_rep_measured(p)} for grade, profiles in GRADE_TABLE.items()
    }
    assert selected == {grade: EXPECTED_REP_MEASURED.get(grade, set()) for grade in GRADES}


def test_predicate_ignores_estimates_aa_agent_and_unmeasured_rows():
    """The annotation is the grade evidence — not benchmark/estimate fields."""
    rep = Profile("x-rep", "M", None, benchmark_annotation="급 실측(A; reps 3/3)")
    assert is_rep_measured(rep)
    # An estimate score on top of a rep-decided grade is still rep-measured.
    rep_est = Profile(
        "x-rep-est",
        "M",
        43.0,
        benchmark_annotation="급 실측(B; evidence reps srv:1)",
        estimate_reason="vendor curve",
    )
    assert is_rep_measured(rep_est)
    for profile in (
        Profile("x-est", "M", 40.0, benchmark_annotation="추정(외삽)"),
        Profile("x-aa", "M", 60.0, benchmark_source="AA-agent"),
        Profile("x-none", "M", None),
        Profile("x-note", "M", 40.0, benchmark_annotation="보수 배치(B; reps 언급 없음)"),
    ):
        assert not is_rep_measured(profile), profile.name


# ── listing (AC1, AC2) ───────────────────────────────────────────────────────


def test_recommend_a_lists_the_aplus_rep_rows_tagged_one_up():
    out = recommend(_pool_providers(), "A", today=TODAY, now=NOW)
    ranked = _ranked(out)
    for label in ("devin-ds41", "devin-swe2 --effort high", "devin-swe2-max --effort max"):
        lines = [line for line in out.splitlines() if line[:1].isdigit() and label in line]
        # exactly one listing, always tagged — never an untagged copy at A
        assert len(lines) == 1 and "[one-up A+]" in lines[0], (label, out)
        assert label in ranked


def test_recommend_b_lists_the_a_rep_rows_tagged_one_up():
    out = recommend(_pool_providers(), "B", today=TODAY, now=NOW)
    for label in ("devin-swe2-medium", "grok-hi --effort xhigh", "oc-solar4"):
        lines = [line for line in out.splitlines() if line[:1].isdigit() and label in line]
        assert len(lines) == 1 and "[one-up A]" in lines[0], (label, out)


def test_recommend_c_lists_the_b_rep_row_tagged_one_up():
    out = recommend(_pool_providers(), "C", today=TODAY, now=NOW)
    lines = [line for line in out.splitlines() if line[:1].isdigit() and "sonnet --effort high" in line]
    assert len(lines) == 1 and "[one-up B]" in lines[0], out


def test_s_and_splus_have_no_one_up_rows():
    """AC2: nothing is rep-measured at S+ (no one-up at S) and nothing sits above S+."""
    for grade in ("S+", "S", "A+"):
        out = recommend(_pool_providers(), grade, today=TODAY, now=NOW)
        assert "[one-up" not in out, (grade, out)


# ── every-row invariants (AC3 / AC7a / AC7b) ─────────────────────────────────


def test_every_bundled_row_lists_only_at_its_placement_or_one_below():
    """AC3/AC7a/AC7b: a row's ranked listings stay within its placed grade —
    plus exactly one grade below, tagged, iff the placement is rep-measured.

    The rep-measured set is pinned by name (EXPECTED_REP_MEASURED), not
    re-derived from the predicate, so a predicate mutant widens or narrows
    ``seen`` against a frozen oracle and fails here.
    """
    providers = _pool_providers()
    outputs = {grade: recommend(providers, grade, today=TODAY, now=NOW) for grade in GRADES}
    ranked_at = {grade: _ranked(out) for grade, out in outputs.items()}
    pinned = {(grade, label) for grade, labels in EXPECTED_REP_MEASURED.items() for label in labels}

    # Rows share a display label across grades (bare devin-swe2 sits at A+, A
    # and B), so the allowed set unions every placement that emits the label.
    allowed_at: dict[str, set[str]] = {}
    rep_measured_labels: set[tuple[str, str | None]] = set()
    for grade, profiles in GRADE_TABLE.items():
        index = _grade_index(grade)
        below = GRADES[index + 1] if index + 1 < len(GRADES) else None
        for profile in profiles:
            if profile.gate != "default":
                continue  # escalation rows render in their own section
            label = _label(profile)
            allowed = allowed_at.setdefault(label, set())
            allowed.add(grade)
            if (grade, label) in pinned:
                if below is not None:
                    allowed.add(below)
                rep_measured_labels.add((label, below))
    for label, allowed in allowed_at.items():
        seen = {grade for grade in GRADES if label in ranked_at[grade]}
        assert seen <= allowed, (label, allowed, seen)

    # …and every rep-measured row really does take its one-up slot, tagged.
    for label, below in sorted(rep_measured_labels):
        if below is None:
            continue
        placed = GRADES[_grade_index(below) - 1]
        one_up_lines = [
            line for line in outputs[below].splitlines() if line[:1].isdigit() and _line_label(line) == label
        ]
        assert one_up_lines and all(f"[one-up {placed}]" in line for line in one_up_lines), (
            label,
            below,
            outputs[below],
        )


def test_e6_arm_rungs_are_never_one_up_or_ranked():
    """E6 arm rows live outside GRADE_TABLE and can never surface via one-up."""
    from scopefuel.recommend import E6_ARM_ANNOTATION

    for profiles in GRADE_TABLE.values():
        for profile in profiles:
            assert profile.benchmark_annotation != E6_ARM_ANNOTATION
    for grade in GRADES:
        for profile, _placed in _one_up_profiles(grade, GRADE_TABLE):
            assert profile.benchmark_annotation != E6_ARM_ANNOTATION
            assert E6_ARM_ANNOTATION not in (profile.benchmark_annotation or "")


# ── ordering (AC6 / AC7c) ────────────────────────────────────────────────────


def test_one_up_never_outranks_exact_at_equal_quota_and_boost():
    """Same pool, same quota, no boost — and the one-up row even has the better
    가성비 slot. The exact-grade row still lists first."""
    table = {grade: [] for grade in GRADES}
    table["A+"].append(
        Profile(
            "devin-oneup",
            "Up",
            90.0,
            benchmark_annotation="급 실측(A+; reps 3/3)",
            aa_model_id="m-up",
        )
    )
    table["A"].append(Profile("devin-exact", "Exact", 80.0, aa_model_id="m-exact"))
    providers = [ProviderResult(id="devin", pool_class="preserve", buckets=[_bucket()])]
    # The one-up row's pool slot wins the value reorder (18.0 > 8.0): without
    # the one-up sort key it would outrank the exact row at equal quota/boost.
    prices = {
        "m-up": bench.ModelPrice(
            model_id="m-up",
            price_1m_blended_3_to_1=5.0,
            price_1m_input_tokens=1.0,
            price_1m_output_tokens=4.0,
            captured_at="2026-10-09T00:00:00+00:00",
        ),
        "m-exact": bench.ModelPrice(
            model_id="m-exact",
            price_1m_blended_3_to_1=10.0,
            price_1m_input_tokens=1.0,
            price_1m_output_tokens=4.0,
            captured_at="2026-10-09T00:00:00+00:00",
        ),
    }
    ranked = _ranked(recommend(providers, "A", today=TODAY, now=NOW, grade_table=table, model_prices=prices))
    assert ranked == ["devin-exact", "devin-oneup"], ranked


def test_sol_profiles_never_appear_outside_splus():
    """The _SOL_PROFILES rule stands — Sol is S+-only even as a one-up row."""
    table = {grade: [] for grade in GRADES}
    table["S+"].append(
        Profile(
            "codex-sol",
            "Sol",
            90.0,
            benchmark_annotation="급 실측(S+; reps 3/3)",
            catalog_pool="codex",
        )
    )
    providers = [ProviderResult(id="codex", pool_class="preserve", buckets=[_bucket()])]
    out = recommend(providers, "S", today=TODAY, now=NOW, grade_table=table)
    assert "codex-sol" not in out


# ── folds behave like exact-grade rows ───────────────────────────────────────


def test_unsubscribed_one_up_row_is_excluded_not_ranked(monkeypatch):
    real = recommend_mod.profile_subscription

    def fake(name, provider_id=None):
        if name == "devin-swe2-medium":
            return Subscription(False, "profile", "profiles.devin-swe2-medium", "devin")
        return real(name, provider_id)

    monkeypatch.setattr(recommend_mod, "profile_subscription", fake)
    out = recommend(_pool_providers(), "B", today=TODAY, now=NOW)
    ranked = _ranked(out)
    assert "devin-swe2-medium" not in ranked
    assert any(line.startswith("✗") and "devin-swe2-medium[one-up A]" in line for line in out.splitlines()), (
        out
    )


def test_policy_excluded_one_up_row_folds_tagged(monkeypatch):
    """Policy-excluded pools are display-suppressed while candidates exist —
    one-up rows follow the same rule. The emergency block (no candidates left)
    is the one place a policy-excluded row prints, tagged there too."""
    monkeypatch.setattr(recommend_mod, "get_policy", lambda *args, **kwargs: ("exclude", None))
    out = recommend(_pool_providers(), "A", today=TODAY, now=NOW)
    ranked = _ranked(out)
    assert not any(name.startswith("devin-") for name in ranked), out
    assert any("devin-swe2" in line and "[one-up A+]" in line for line in out.splitlines()), out


def test_policy_excluded_one_up_row_silent_while_candidates_exist(monkeypatch):
    real = recommend_mod.get_policy

    def fake(provider_id, builtin_class="preserve", today=None):
        if provider_id == "devin":
            return "exclude", None
        return real(provider_id, builtin_class, today=today)

    monkeypatch.setattr(recommend_mod, "get_policy", fake)
    out = recommend(_pool_providers(), "A", today=TODAY, now=NOW)
    ranked = _ranked(out)
    assert not any(name.startswith("devin-") for name in ranked), out
    # same suppression as exact-grade rows: no ✗ fold while candidates exist
    assert not any("devin-swe2" in line for line in out.splitlines()), out


# ── JSON / dict surface (AC5) ────────────────────────────────────────────────


def test_recommend_dict_marks_one_up_rows():
    payload = recommend_dict(_pool_providers(), "A", today=TODAY, now=NOW)
    rows = {row["profile"]: row for row in payload["rows"]}
    for name in ("devin-ds41", "devin-swe2", "devin-swe2-max"):
        row = rows[name]
        assert row["kind"] == "candidate" and row["one_up"] is True
        assert row["placed_grade"] == "A+"
    exact = rows["devin-swe2-medium"]
    assert exact["one_up"] is False and exact["placed_grade"] == "A"
    assert payload["grade"] == "A"


def test_recommend_json_cli_marks_one_up(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {
            name: (lambda provider_id=name: ProviderResult(id=provider_id, buckets=[_bucket()]))
            for name in {profile_pool(p.name)[0] for profiles in GRADE_TABLE.values() for p in profiles}
        },
    )
    assert cli.main(["--recommend", "A", "--json", "--no-cache"]) == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    rows = {row["profile"]: row for row in payload["rows"]}
    assert rows["devin-swe2"]["one_up"] is True and rows["devin-swe2"]["placed_grade"] == "A+"
    assert rows["devin-swe2-medium"]["one_up"] is False


# ── consumers (AC5): gate alternatives keep same-grade semantics ─────────────


def test_alt_candidates_skips_one_up_rows():
    providers = _pool_providers()
    alternatives = _alt_candidates(providers, "A", "grok-hi", TODAY, NOW, 24.0)
    # one-up rows are not same-grade alternatives (bare devin-swe2 IS — it has
    # its own A row, so it stays; the A+-only spellings stay out)
    for name in ("devin-ds41", "devin-swe2-max"):
        assert name not in alternatives
    assert "devin-swe2-medium" in alternatives


def test_one_up_listing_does_not_widen_gate_admission():
    """A one-up row still gates under its own pool; the recommend listing never
    feeds the verdict — only the alternatives list, which now filters them."""
    providers = _pool_providers()
    result = recommend_mod.gate_check(
        providers,
        "grok-hi",
        today=TODAY,
        now=NOW,
        urgency_hours=24.0,
    )
    assert result.ok
    # gate reads recommend only for alternatives — one-up names stay out of it.
    assert "devin-ds41" not in result.alternatives
    assert "devin-swe2-max" not in result.alternatives


# ── round 2: authoritative catalog grade evidence (B1) ───────────────────────


def _server_view(rows=None):
    """A server-source CatalogView built from catalog-row dicts."""
    rows = rows or [entry.as_dict() for entry in bench.catalog_snapshot()]
    entries = bench._catalog_from_payload({"catalog": rows})
    return bench.CatalogView(tuple(entries), source="server", backend="handoffkeep")


def test_server_rep_promotion_evidence_is_honored():
    """A grades-apply promote stamp (deviation_ref + decided_by) makes the row
    rep-measured even though its annotation stays an estimate."""
    rows = [entry.as_dict() for entry in bench.catalog_snapshot()]
    for row in rows:
        if row["profile"] == "sonnet" and row["effort"] == "medium":
            row.update(
                grade="B",
                deviation_ref="hk:task/999; promote evidence srv:9001,srv:9002,srv:9003",
                decided_by="operator-desk",
                decided_at="2026-10-09T00:00:00Z",
            )
    table = bench._catalog_grade_table(_server_view(rows))
    assert table is not None
    profile = next(p for p in table["B"] if p.name == "sonnet" and p.launcher_effort == "medium")
    assert is_rep_measured(profile), ("server rep promotion lost", profile)


def test_server_annotation_updates_are_honored():
    """The server's own benchmark_annotation wins over the bundled template's."""
    rows = [entry.as_dict() for entry in bench.catalog_snapshot()]
    for row in rows:
        if row["profile"] == "sonnet" and row["effort"] == "medium":
            row.update(
                grade="B",
                benchmark_annotation="급 실측(B; evidence reps srv:9001, srv:9002, srv:9003)",
            )
    table = bench._catalog_grade_table(_server_view(rows))
    assert table is not None
    profile = next(p for p in table["B"] if p.name == "sonnet" and p.launcher_effort == "medium")
    assert is_rep_measured(profile), ("server annotation discarded", profile.benchmark_annotation)


def test_server_unmeasured_revocation_is_honored():
    """A server-demoted-to-estimate row must not keep the template's rep
    annotation — the stale bundled text never decides."""
    rows = [entry.as_dict() for entry in bench.catalog_snapshot()]
    for row in rows:
        if row["profile"] == "sonnet" and row["effort"] == "high":
            row.update(
                benchmark_annotation="추정(외삽)",
                deviation_ref="hk:task/999; estimate placement; no counted reps",
            )
    table = bench._catalog_grade_table(_server_view(rows))
    assert table is not None
    profile = next(p for p in table["B"] if p.name == "sonnet" and p.launcher_effort == "high")
    assert not is_rep_measured(profile), (
        "server estimate-only row falsely lowered",
        profile.benchmark_annotation,
    )


def test_seeded_server_catalog_keeps_the_bundled_rep_set():
    """The bundled snapshot's own canon seed selects exactly the same rows."""
    table = bench._catalog_grade_table(_server_view())
    assert table is not None
    selected = {
        (grade, _label(profile))
        for grade, profiles in table.items()
        for profile in profiles
        if is_rep_measured(profile)
    }
    assert selected == {(grade, label) for grade, labels in EXPECTED_REP_MEASURED.items() for label in labels}


# ── round 2: exact-first regardless of benchmark presence (B2) ───────────────


def test_exact_unmeasured_ahead_at_equal_quota_and_boost(monkeypatch):
    """Exact rows stay ahead of one-up rows at equal quota and boost even when
    the exact row has no numeric benchmark and the one-up row does."""
    table = {grade: [] for grade in GRADES}
    table["A+"].append(Profile("devin-oneup", "Measured", 80.0, benchmark_annotation="급 실측(A+; reps 3/3)"))
    table["A"].append(Profile("sonnet", "Exact", None))
    providers = _pool_providers()
    monkeypatch.setattr(recommend_mod, "get_policy", lambda *a, **k: ("preserve", None))
    monkeypatch.setattr(recommend_mod, "get_boost", lambda *a, **k: (None, None))
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW, grade_table=table)
    rows = [row for row in payload["rows"] if row["kind"] == "candidate"]
    assert rows[0]["score"] == rows[1]["score"] and rows[0]["boost"] == rows[1]["boost"]
    assert rows[0]["profile"] == "sonnet", [
        (row["profile"], row["one_up"], row["score"], row["boost"]) for row in rows
    ]


def test_unscored_one_up_never_outranks_scored_exact_even_boosted(monkeypatch):
    """The unmeasured-last rule reaches one-up rows too: an unscored one-up row
    stays behind a scored exact row even when its pool is boosted."""
    table = {grade: [] for grade in GRADES}
    table["A+"].append(Profile("devin-oneup", "Measured", None, benchmark_annotation="급 실측(A+; reps 3/3)"))
    table["A"].append(Profile("sonnet", "Exact", 50.0))
    providers = _pool_providers()
    monkeypatch.setattr(recommend_mod, "get_policy", lambda *a, **k: ("preserve", None))
    monkeypatch.setattr(
        recommend_mod, "get_boost", lambda pool, *a, **k: (1, None) if pool == "devin" else (None, None)
    )
    payload = recommend_dict(providers, "A", today=TODAY, now=NOW, grade_table=table)
    rows = [row for row in payload["rows"] if row["kind"] == "candidate"]
    assert rows[0]["profile"] == "sonnet", [row["profile"] for row in rows]
