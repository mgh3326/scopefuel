"""#692: E6 effort-ladder rungs — catalog rows, the one E6 arm marker, and the gate.

E6 (#594, plan hk:doc plan/2026-09-25/e6-effort-ladder) measures one model at
several efforts on real tasks. The rungs it needs that the catalog did not carry
are catalog *rows*, not placements: grade C, no benchmark, never a recommendation
candidate, never a launcher default, and reachable only through the explicit arm
marker ``SCOPEFUEL_E6_ARM=<profile>@<effort>``. Without the marker the gate
refuses the rung with a reason naming it, and ``policy launch`` keeps its ordinary
fallback so every existing spelling (``wrk -m codex`` pins codex-sol@high,
``-m builder-grok`` pins grok-hi@xhigh) is unchanged.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

import pytest

from scopefuel import bench, cli, launch
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    E6_ARM_ANNOTATION,
    E6_ARM_GRADE,
    E6_ARM_KEYS,
    E6_ARM_MARKER_ENV,
    E6_ARM_RUNGS,
    GRADE_TABLE,
    Profile,
    e6_arm_rung_for,
    gate_check,
    parse_e6_arm_marker,
    profile_pool,
    recommend,
)

TODAY = dt.date(2026, 9, 25)
NOW = dt.datetime(2026, 9, 25, 12, 0, 0, tzinfo=dt.UTC)

# (profile, effort) -> (pool, catalog model id, the AA number scopefuel stores)
NEW_RUNGS: dict[tuple[str, str], tuple[str, str, str]] = {
    ("sonnet", "max"): ("claude", "claude-sonnet-5", "max 38.2"),
    ("codex-sol", "high"): ("codex", "gpt-6-sol", "high 42.8"),
    ("kimi-k3", "high"): ("kimi", "kimi-k3", "default 61.0"),
    ("kimi-k3", "max"): ("kimi", "kimi-k3", "max 43.6"),
    ("grok-hi", "xhigh"): ("grok", "grok-4.7", "high 46.3"),
}
# The rungs the E6 arms use that the catalog already placed — no new row may
# appear for these, or the existing spelling's resolution would change.
EXISTING_RUNGS: dict[tuple[str, str], str] = {
    ("opus", "low"): "S",
    ("opus", "medium"): "S+",
    ("sonnet", "xhigh"): "A+",
    ("codex-sol", "xhigh"): "S+",
    ("codex-sol", "max"): "S+",
    ("codex-luna", "xhigh"): "A+",
    ("codex-terra", "high"): "A+",
    ("codex-terra", "xhigh"): "A+",
    ("grok", "medium"): "A+",
}


def _provider(provider_id: str, used: float = 10.0, pool_class: str = "preserve") -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class=pool_class,  # type: ignore[arg-type]
        buckets=[
            Bucket(
                label="7d",
                window="7d",
                used_pct=used,
                resets_at=(dt.datetime.now(dt.UTC) + dt.timedelta(hours=100)).isoformat(),
                scope=Scope("account"),
                horizon="week",  # type: ignore[arg-type]
            )
        ],
    )


def _gate_cli(monkeypatch, capsys, tmp_path, provider_id: str, argv: list[str]) -> tuple[int, str, str, dict]:
    monkeypatch.setattr(
        cli, "registry", lambda: {provider_id: lambda: _provider(provider_id, pool_class="spend")}
    )
    record_path = tmp_path / "gate.json"
    rc = cli.main([*argv, "--no-cache", "--gate-output", str(record_path)])
    captured = capsys.readouterr()
    record = json.loads(record_path.read_text())
    return rc, captured.out, captured.err, record


# --- AC1: catalog rows -------------------------------------------------------


def test_the_new_rungs_are_exactly_the_missing_ones():
    assert set(E6_ARM_KEYS) == set(NEW_RUNGS)


def test_no_new_row_appears_for_a_rung_that_already_existed():
    """A row for these keys would change an existing spelling's resolution."""

    catalog_keys = {(entry.profile, entry.effort) for entry in bench.catalog_snapshot()}
    for profile, effort in EXISTING_RUNGS:
        assert (profile, effort) in catalog_keys
        assert (profile, effort) not in E6_ARM_KEYS


@pytest.mark.parametrize(("key", "expected"), sorted(NEW_RUNGS.items()))
def test_each_new_rung_is_a_catalog_row_of_grade_c(key, expected):
    pool, model_id, aa_reference = expected
    rows = [entry for entry in bench.catalog_snapshot() if entry.key == key]
    assert len(rows) == 1, rows
    (row,) = rows
    assert row.grade == E6_ARM_GRADE
    assert row.grade == "C"
    assert row.score is None
    assert row.pool == pool
    assert row.model_id == model_id
    assert row.gate == "default"
    assert row.benchmark_source is None
    assert row.benchmark_annotation.startswith(E6_ARM_ANNOTATION)
    assert aa_reference in row.benchmark_annotation
    assert "#594" in row.benchmark_annotation


@pytest.mark.parametrize("key", sorted(NEW_RUNGS))
def test_each_new_rung_keeps_the_profile_pool_and_aa_mapping(key):
    profile, _ = key
    row = e6_arm_rung_for(profile, key[1])
    assert row is not None
    assert profile_pool(profile) == (NEW_RUNGS[key][0], None)
    assert row.benchmark is None
    assert row.aa_model_id or row.aa_agent_model_id


def test_new_rungs_stay_out_of_the_placement_snapshot():
    """The snapshot mirrors the placement canon (Sol S+-only) — rows, not placements."""

    placement_keys = {entry.key for entry in launch.snapshot_entries()}
    assert not (set(E6_ARM_KEYS) & placement_keys)
    # The catalog view does carry them, so `bench catalog list` shows the rung.
    assert set(E6_ARM_KEYS) <= {entry.key for entry in bench.read_catalog().entries}


def test_the_seed_emits_the_e6_rows_with_their_own_deviation_ref(capsys):
    assert cli.main(["bench", "push-catalog", "--emit-seed", "--decided-by", "operator-desk"]) == 0
    rows = {(row["profile"], row["effort"]): row for row in json.loads(capsys.readouterr().out)["catalog"]}
    for key in NEW_RUNGS:
        row = rows[key]
        assert row["grade"] == "C"
        assert row["decided_by"] == "operator-desk"
        assert "e6-effort-ladder" in row["deviation_ref"]


@pytest.mark.parametrize("key", sorted(NEW_RUNGS))
def test_each_new_rung_resolves_to_the_right_pool_and_effort_with_the_marker(key):
    profile, effort = key
    pool, model_id, _ = NEW_RUNGS[key]
    decision = launch.resolve_launch(profile, effort=effort, e6_arm=f"{profile}@{effort}")
    assert decision.pool == pool
    assert decision.effort == effort
    assert decision.model_id == model_id
    assert decision.grade == "C"
    assert decision.e6_arm == f"{profile}@{effort}"


def test_a_canon_silent_about_the_rung_resolves_the_bundled_row_for_this_profile():
    """Two E6 rungs share an effort name — the bundled fallback must key on the pair.

    codex-sol@high and kimi-k3@high both exist; an effort-only lookup handed the
    kimi arm the codex row (pool and model id included). The canon here is a
    server view that does not carry the E6 rows yet.
    """

    view = bench.CatalogView(
        entries=(
            bench.CatalogEntry(
                profile="kimi-k3", effort="", model_id="kimi-k3", pool="kimi", grade="S", score=61.0
            ),
            bench.CatalogEntry(
                profile="codex-sol", effort="max", model_id="gpt-6-sol", pool="codex", grade="S+", score=67.0
            ),
        ),
        source="server",
        backend="handoffkeep",
    )
    decision = launch.resolve_launch("kimi-k3", effort="high", e6_arm="kimi-k3@high", view=view)
    assert decision.pool == "kimi"
    assert decision.model_id == "kimi-k3"
    assert decision.grade == "C"
    assert decision.e6_arm == "kimi-k3@high"


def test_an_unmarked_default_never_falls_onto_a_c_row():
    """The default walk must not pick an unmeasured E6 row.

    With the canon carrying the E6 rows (the seed does), a bare
    `policy launch grok-hi` would otherwise resolve the C xhigh row as the
    "best-graded ordinary rung" as soon as the canon retires or gates the
    preferred rung — and the canon's own retirement would stop refusing.
    """

    seeded = {
        (entry.profile, entry.effort): entry
        for entry in bench.catalog_snapshot()
        if entry.profile == "grok-hi"
    }
    view = bench.CatalogView(
        entries=tuple(seeded.values()),
        source="cache",
        backend="handoffkeep",
    )
    decision = launch.resolve_launch("grok-hi", view=view)
    # grok-hi is keyed on its profile-default row: the reported effort is the
    # launcher's flag, exactly as before #692.
    assert decision.effort == "high"
    assert decision.grade == "S"
    assert decision.e6_arm is None

    # The canon retires the profile-default row: the answer is the canon's
    # statement, not the leftover C measurement row.
    retired_view = bench.CatalogView(
        entries=tuple(
            replace(entry, retired_at="2026-09-25T00:00:00Z") if entry.effort == "" else entry
            for entry in seeded.values()
        ),
        source="cache",
        backend="handoffkeep",
    )
    with pytest.raises(launch.LaunchError, match="retired"):
        launch.resolve_launch("grok-hi", view=retired_view)


def test_a_marker_does_not_revive_a_rung_the_canon_retired(monkeypatch, capsys):
    seeded = {entry.key: entry for entry in bench.catalog_snapshot() if entry.profile == "sonnet"}
    view = bench.CatalogView(
        entries=tuple(
            replace(entry, retired_at="2026-09-25T00:00:00Z") if entry.effort == "max" else entry
            for entry in seeded.values()
        ),
        source="cache",
        backend="handoffkeep",
    )
    monkeypatch.setattr(launch, "read_catalog", lambda **kwargs: view)
    monkeypatch.setenv(E6_ARM_MARKER_ENV, "sonnet@max")
    rc = cli.main(["policy", "launch", "sonnet", "--effort", "max"])
    assert rc == 3
    assert "retired" in capsys.readouterr().err


def test_a_measured_canon_row_gets_no_e6_arm_label():
    """A canon row placed above C is ordinary — the marker must not claim it."""

    view = bench.CatalogView(
        entries=(
            bench.CatalogEntry(
                profile="sonnet",
                effort="max",
                model_id="claude-sonnet-5",
                pool="claude",
                grade="B",
                score=45.0,
            ),
        ),
        source="server",
        backend="handoffkeep",
    )
    decision = launch.resolve_launch("sonnet", effort="max", e6_arm="sonnet@max", view=view)
    assert decision.grade == "B"
    assert decision.e6_arm is None


def test_recommend_never_lists_a_new_rung_at_any_grade():
    providers = [_provider("claude", 0.0, pool_class="spend"), _provider("codex", 0.0, pool_class="spend")]
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        out = recommend(providers, grade, today=TODAY, now=NOW)
        for profile, effort in NEW_RUNGS:
            for line in out.splitlines():
                if not line[:1].isdigit():
                    continue
                tokens = line.split()
                assert tokens[1] != profile or f"--effort {effort}" not in line, (grade, line)
        assert "미측정(#594 E6" not in out, (grade, out)


def test_a_canon_carrying_the_e6_rows_does_not_recommend_them():
    def entry(profile: str, effort: str, grade: str) -> bench.CatalogEntry:
        return bench.CatalogEntry(
            profile=profile,
            effort=effort,
            model_id="claude-sonnet-5" if profile == "sonnet" else "gpt-6-sol",
            pool="claude" if profile == "sonnet" else "codex",
            grade=grade,
            benchmark_annotation=E6_ARM_ANNOTATION,
        )

    view = bench.CatalogView(
        entries=tuple(entry(profile, effort, "C") for profile, effort in NEW_RUNGS)
        + (entry("sonnet", "high", "A+"),),
        source="server",
        backend="handoffkeep",
    )
    table = bench._catalog_grade_table(view)
    assert table is not None
    placed = {(p.name, p.launcher_effort or "") for profiles in table.values() for p in profiles}
    assert not (set(E6_ARM_KEYS) & placed)
    assert ("sonnet", "high") in placed


# --- AC2: the gate admits only an explicit E6 arm ----------------------------


@pytest.mark.parametrize("key", sorted(NEW_RUNGS))
def test_gate_refuses_an_unmarked_rung_with_the_rung_in_the_reason(key):
    profile, effort = key
    provider_id = NEW_RUNGS[key][0]
    result = gate_check(
        [_provider(provider_id, pool_class="spend")],
        profile,
        effort=effort,
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert result.unmeasurable is False
    assert result.role_denied is False
    assert result.grade == "C"
    assert "e6_arm_required" in result.reason
    assert f"{profile}@{effort}" in result.reason
    assert E6_ARM_MARKER_ENV in result.reason


@pytest.mark.parametrize("key", sorted(NEW_RUNGS))
def test_gate_admits_the_rung_with_the_marker_and_tags_the_allow_reason(key):
    profile, effort = key
    provider_id = NEW_RUNGS[key][0]
    result = gate_check(
        [_provider(provider_id, pool_class="spend")],
        profile,
        effort=effort,
        e6_arm=f"{profile}@{effort}",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True, result.reason
    assert result.grade == "C"
    assert result.e6_arm == f"{profile}@{effort}"
    assert f"[E6 arm, unmeasured C: {profile}@{effort}]" in result.reason


def test_gate_cli_refuses_and_names_the_remedy(monkeypatch, capsys, tmp_path):
    rc, out, err, record = _gate_cli(
        monkeypatch, capsys, tmp_path, "claude", ["gate", "-m", "sonnet", "--effort", "max"]
    )
    assert rc == 3
    assert "e6_arm_required" in err
    assert "sonnet@max" in err
    assert f"{E6_ARM_MARKER_ENV}=sonnet@max" in err
    assert "대안(C) 없음" not in err  # not a quota/alternatives refusal
    assert record["ok"] is False
    assert record["e6_arm"] == "sonnet@max"
    assert out == ""


def test_gate_cli_admits_with_the_marker_and_prints_the_visible_tag(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(E6_ARM_MARKER_ENV, "sonnet@max")
    rc, out, _err, record = _gate_cli(
        monkeypatch, capsys, tmp_path, "claude", ["gate", "-m", "sonnet", "--effort", "max"]
    )
    assert rc == 0
    first_line = out.splitlines()[0]
    assert "[E6 arm, unmeasured C: sonnet@max]" in first_line
    assert record["ok"] is True
    assert record["e6_arm"] == "sonnet@max"
    assert record["grade"] == "C"


def test_the_marker_alone_names_the_rung_for_the_gate(monkeypatch, capsys, tmp_path):
    """The spawn path: wrk calls `gate -m <profile>` and passes no --effort."""

    monkeypatch.setenv(E6_ARM_MARKER_ENV, "sonnet@max")
    rc, out, _err, _record = _gate_cli(monkeypatch, capsys, tmp_path, "claude", ["gate", "-m", "sonnet"])
    assert rc == 0
    assert "[E6 arm, unmeasured C: sonnet@max]" in out.splitlines()[0]


@pytest.mark.parametrize("marker", ["SONNET@MAX", " sonnet@max ", "Sonnet@Max"])
def test_the_marker_is_normalized_like_an_effort_spelling(monkeypatch, capsys, tmp_path, marker):
    monkeypatch.setenv(E6_ARM_MARKER_ENV, marker)
    rc, out, _err, _record = _gate_cli(monkeypatch, capsys, tmp_path, "claude", ["gate", "-m", "sonnet"])
    assert rc == 0
    assert "[E6 arm, unmeasured C: sonnet@max]" in out.splitlines()[0]


def test_the_marker_accepts_the_launcher_alias(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(E6_ARM_MARKER_ENV, "codex-max@high")
    rc, out, _err, _record = _gate_cli(
        monkeypatch, capsys, tmp_path, "codex", ["gate", "-m", "codex-max", "--effort", "high"]
    )
    assert rc == 0
    assert "[E6 arm, unmeasured C: codex-sol@high]" in out.splitlines()[0]


@pytest.mark.parametrize(
    "marker",
    [
        "sonnet@high",  # a placed rung, not an E6 rung
        "sonnet",  # no rung named
        "sonnet@",  # empty rung
        "@max",  # empty profile
        "opus@low",  # a different profile's rung
        "sonnet@max@max",  # not a rung name
    ],
)
def test_a_marker_that_does_not_name_this_rung_opens_nothing(marker):
    result = gate_check(
        [_provider("claude", pool_class="spend")],
        "sonnet",
        effort="max",
        e6_arm=marker,
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert "e6_arm_required" in result.reason
    assert "sonnet@max" in result.reason


def test_the_marker_parser_folds_spelling_and_rejects_a_value_that_names_no_rung():
    assert parse_e6_arm_marker(None) is None
    assert parse_e6_arm_marker("") is None
    assert parse_e6_arm_marker("sonnet") is None
    assert parse_e6_arm_marker("sonnet@") is None
    assert parse_e6_arm_marker("@max") is None
    assert parse_e6_arm_marker(" Sonnet@MAX ").label == "sonnet@max"
    assert parse_e6_arm_marker("codex-max@high").label == "codex-sol@high"


def test_the_marker_cannot_widen_across_profiles():
    """A marker names one profile's rung — a cross-profile forgery opens nothing.

    kimi-k3@max is a real E6 rung; naming it must not open sonnet@max (the
    profile comparison is what stops it).
    """

    result = gate_check(
        [_provider("claude", pool_class="spend")],
        "sonnet",
        effort="max",
        e6_arm="kimi-k3@max",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert "e6_arm_required" in result.reason
    assert "sonnet@max" in result.reason


def test_the_marker_selects_the_rung_it_names_for_a_placed_rung_too():
    """The marker is also the gate's rung selector, and it only ever narrows.

    wrk does not pass --effort to the gate, so for arm B's escalation-gated
    opus@low the marker is what lets the gate see that rung at all (and then the
    ordinary --operator-request path applies). It never widens: the default
    judgement of opus is the S+ high rung.
    """

    plain = gate_check([_provider("claude", pool_class="spend")], "opus", today=TODAY, now=NOW)
    assert plain.grade == "S+"
    marked = gate_check(
        [_provider("claude", pool_class="spend")], "opus", e6_arm="opus@low", today=TODAY, now=NOW
    )
    assert marked.grade == "S"
    assert "escalation" in marked.reason
    assert marked.e6_arm is None


def test_a_retired_e6_rung_is_not_revived_by_the_marker():
    """Retiring the rung is the canon closing it — the marker is not a way back."""

    retired = frozenset({("sonnet", "max")})
    result = gate_check(
        [_provider("claude", pool_class="spend")],
        "sonnet",
        effort="max",
        e6_arm="sonnet@max",
        retired_rungs=retired,
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert "retired" in result.reason
    assert "rung 'max'" in result.reason


def test_a_marker_for_a_placed_rung_leaves_that_rung_ordinary():
    """sonnet@high is a placement: the marker neither opens nor closes anything."""

    result = gate_check(
        [_provider("claude", pool_class="spend")],
        "sonnet",
        effort="high",
        e6_arm="sonnet@high",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True
    assert result.grade == "A+"
    assert result.e6_arm is None
    assert "E6 arm" not in result.reason


def test_the_gate_judges_the_rung_that_was_requested():
    """`gate -m opus --effort low` is the low rung's judgement, not the profile's best."""

    result = gate_check([_provider("claude", pool_class="spend")], "opus", effort="low", today=TODAY, now=NOW)
    assert result.grade == "S"
    assert "escalation" in result.reason
    plain = gate_check([_provider("claude", pool_class="spend")], "opus", today=TODAY, now=NOW)
    assert plain.grade == "S+"


def test_a_measured_canon_row_lifts_the_e6_restriction():
    """The restriction is 'unmeasured C' — once the canon measures the rung it is ordinary."""

    table = {grade: list(profiles) for grade, profiles in GRADE_TABLE.items()}
    table["B"].append(
        Profile("sonnet", "Sonnet 5 (max)", 45.0, launcher_effort="max", benchmark_effort="max")
    )
    result = gate_check(
        [_provider("claude", pool_class="spend")],
        "sonnet",
        effort="max",
        today=TODAY,
        now=NOW,
        grade_table=table,
    )
    assert result.ok is True
    assert result.grade == "B"
    assert result.e6_arm is None


# --- AC3: existing behavior is untouched -------------------------------------


def test_an_unmarked_request_keeps_the_default_placement():
    """`wrk -m codex` pins codex-sol@high; the C row must not answer for it."""

    decision = launch.resolve_launch("codex-sol", effort="high")
    assert decision.model_id == "gpt-6-sol"
    assert decision.effort == "high"
    assert decision.gate == "default"
    assert decision.grade == "S+"
    assert decision.e6_arm is None


def test_builder_grok_pin_still_resolves_without_a_marker():
    decision = launch.resolve_launch("grok-hi", effort="xhigh")
    assert decision.model_id == "grok-4.7"
    assert decision.effort == "xhigh"
    assert decision.grade == "S"
    assert decision.e6_arm is None


def test_an_unmarked_new_rung_keeps_todays_fallback():
    decision = launch.resolve_launch("sonnet", effort="max")
    assert decision.effort == "max"
    assert decision.grade == "A+"  # sonnet@high's placement, as before #692
    assert decision.e6_arm is None


def test_the_marker_does_not_change_a_request_that_names_no_rung(monkeypatch, capsys):
    """wrk calls `policy launch <profile>` with no --effort on the sonnet spelling."""

    monkeypatch.setenv(E6_ARM_MARKER_ENV, "sonnet@max")
    assert cli.main(["policy", "launch", "sonnet", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["effort"] == "high"
    assert payload["grade"] == "A+"
    assert payload["e6_arm"] is None


@pytest.mark.parametrize(("key", "grade"), sorted(EXISTING_RUNGS.items()))
def test_existing_rung_grades_are_unchanged(key, grade):
    profile, effort = key
    decision = launch.resolve_launch(profile, effort=effort, e6_arm=f"{profile}@{effort}")
    assert decision.grade == grade
    assert decision.e6_arm is None


def test_the_plain_gate_output_carries_no_e6_field(monkeypatch, capsys, tmp_path):
    rc, out, _err, record = _gate_cli(monkeypatch, capsys, tmp_path, "claude", ["gate", "-m", "sonnet"])
    assert rc == 0
    assert "E6 arm" not in out
    assert "e6_arm" not in record


def test_every_e6_row_is_unmeasured_and_grade_c():
    for row in E6_ARM_RUNGS:
        assert row.benchmark is None
        assert row.benchmark_source is None
        assert row.launcher_effort
        assert row.benchmark_annotation is not None


# --- #716: an explicitly named placed rung is a placement judgement ----------


def _healthy_providers() -> list[ProviderResult]:
    """Every pool the S+ ladder can draw on, healthy and inside quota — a
    refusal here can never hide behind "no same-grade alternative exists"."""
    return [_provider(pid, pool_class="spend") for pid in ("claude", "codex", "grok", "kimi", "kiro")]


def _gate_cli_pools(monkeypatch, capsys, tmp_path, argv, used=None):
    """`scopefuel gate` with every pool registered; ``used`` overrides a pool's
    used_pct (e.g. {"codex": 99.5} puts the codex spend pool over cutoff)."""
    used_pct = {"claude": 10.0, "codex": 10.0, "grok": 10.0, "kimi": 10.0, "kiro": 10.0}
    used_pct.update(used or {})
    registry = {
        pid: (lambda _pid=pid: _provider(_pid, used_pct[_pid], pool_class="spend")) for pid in used_pct
    }
    monkeypatch.setattr(cli, "registry", lambda: registry)
    record_path = tmp_path / "gate.json"
    rc = cli.main([*argv, "--no-cache", "--gate-output", str(record_path)])
    captured = capsys.readouterr()
    record = json.loads(record_path.read_text()) if record_path.exists() else {}
    return rc, captured.out, captured.err, record


def test_a_placed_rung_named_by_effort_is_admitted_despite_same_grade_alternatives():
    """#716 root case. codex-sol@xhigh is a real placement row (S+); naming it
    with --effort judges that placement by quota rules — the escalation
    alternatives-refusal is a default-resolution concept, not a verdict on a
    rung the caller named."""
    result = gate_check(_healthy_providers(), "codex-sol", effort="xhigh", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.grade == "S+"
    assert "codex-sol@xhigh" in result.reason
    assert "escalation 후보" not in result.reason


def test_the_reproduction_commands_all_admit(monkeypatch, capsys, tmp_path):
    """The three reproduction commands from #716, verbatim."""
    rc, out, _err, record = _gate_cli_pools(monkeypatch, capsys, tmp_path, ["gate", "-m", "codex-max"])
    assert rc == 0
    assert record["ok"] is True

    rc, out, _err, record = _gate_cli_pools(
        monkeypatch, capsys, tmp_path, ["gate", "-m", "codex-sol", "--effort", "xhigh"]
    )
    assert rc == 0
    assert record["ok"] is True
    assert record["grade"] == "S+"
    assert "codex-sol@xhigh" in out  # the judged rung is named on the allow line's reason
    assert "escalation" not in record["alternatives"]

    rc, out, _err, record = _gate_cli_pools(
        monkeypatch, capsys, tmp_path, ["gate", "-m", "codex-max", "--effort", "xhigh"]
    )
    assert rc == 0
    assert record["ok"] is True
    # codex-max is a pure spelling alias — the judged rung is the canonical one.
    assert "codex-sol@xhigh" in out


def test_codex_max_effort_max_names_the_pinned_rung(monkeypatch, capsys, tmp_path):
    """Alias rule: codex-max is codex-sol@max — spelling it out lands on the
    same placement row and is admitted by quota, not as a special case."""
    rc, out, _err, record = _gate_cli_pools(
        monkeypatch, capsys, tmp_path, ["gate", "-m", "codex-max", "--effort", "max"]
    )
    assert rc == 0
    assert "codex-sol@max" in out


def test_grok_hi_high_is_admitted_by_effort(monkeypatch, capsys, tmp_path):
    rc, _out, _err, record = _gate_cli_pools(
        monkeypatch, capsys, tmp_path, ["gate", "-m", "grok-hi", "--effort", "high"]
    )
    assert rc == 0
    assert record["grade"] == "S"


def test_codex_max_effort_high_is_the_c_rung_refusal_with_a_named_remedy(monkeypatch, capsys, tmp_path):
    """The pinned-alias conflict case: codex-max --effort high lands on
    codex-sol@high — an unmeasured C E6 rung — so the gate refuses, naming the
    rung and the exact remedy. Never a silent alternative list."""
    rc, _out, err, record = _gate_cli_pools(
        monkeypatch, capsys, tmp_path, ["gate", "-m", "codex-max", "--effort", "high"]
    )
    assert rc == 3
    assert record["grade"] == "C"
    assert "codex-sol@high" in err
    assert "SCOPEFUEL_E6_ARM=codex-sol@high" in err
    assert "대안(" not in err  # refused on the rung, not folded into an alt list


def test_an_explicit_rung_refusal_is_quota_based_and_drops_the_alias_self(monkeypatch, capsys, tmp_path):
    """Placement judgement means quota rules: with the codex pool over cutoff
    the rung is refused on quota — and the alternatives never echo the alias's
    own canonical rows (pre-#716 `codex-max` listed `codex-sol` against itself)."""
    rc, _out, err, record = _gate_cli_pools(
        monkeypatch,
        capsys,
        tmp_path,
        ["gate", "-m", "codex-max", "--effort", "xhigh"],
        used={"codex": 99.5},
    )
    assert rc == 3
    assert "codex-sol@xhigh" in err  # the refusal still names the judged rung
    assert "소진" in err or "cutoff" in err  # quota reason, not the ladder
    alt_lines = [line for line in err.splitlines() if line.startswith("대안")]
    assert alt_lines, err
    assert "codex-sol" not in alt_lines[0]
    assert "opus" in alt_lines[0]


def test_alias_spelling_is_not_listed_as_its_own_alternative(monkeypatch, capsys, tmp_path):
    """Isolation for the exclusion bug on the plain path: `gate -m codex-max`
    refused on quota must not offer codex-sol — the same rows — as an out."""
    rc, _out, err, _record = _gate_cli_pools(
        monkeypatch, capsys, tmp_path, ["gate", "-m", "codex-max"], used={"codex": 99.5}
    )
    assert rc == 3
    alt_lines = [line for line in err.splitlines() if line.startswith("대안")]
    assert alt_lines, err
    assert "codex-sol" not in alt_lines[0]


def test_the_marker_alone_keeps_the_named_placed_rungs_own_gate():
    """#716 asymmetry, pinned: --effort is an explicit per-command nomination
    judged as a placement; the ambient SCOPEFUEL_E6_ARM only *selects* the rung —
    the row's own gate (here: escalation) still applies. An env var that leaks
    into child contexts must never widen admission."""
    result = gate_check(
        _healthy_providers(),
        "codex-max",
        e6_arm="codex-sol@xhigh",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert "escalation 후보" in result.reason
    assert "codex-sol@xhigh" in result.reason  # the rung is still named for audit


def test_an_explicit_effort_wins_over_a_marker_naming_a_different_rung():
    """Selection precedence pin: --effort names the judged rung; a marker
    pointing at a different rung of the same profile cannot override it."""
    result = gate_check(
        _healthy_providers(),
        "codex-max",
        effort="xhigh",
        e6_arm="codex-sol@high",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True
    assert result.grade == "S+"  # xhigh's placement — not the marker's C rung
    assert result.e6_arm is None
    assert "codex-sol@xhigh" in result.reason


def test_an_effort_the_table_does_not_know_still_falls_back_to_the_default():
    """Unchanged #692 fallback: a spelled rung with no row answers the profile's
    default placement (grok-hi has no @medium row — only the C E6 rung at xhigh)."""
    result = gate_check(_healthy_providers(), "grok-hi", effort="medium", today=TODAY, now=NOW)
    assert result.ok is True
    assert result.grade == "S"


def test_the_recommend_ladder_is_untouched_by_the_gate_fix():
    """--recommend unchanged: the rung still sits in the escalation section —
    the fix is in the gate, not the ladder."""
    out = recommend(_healthy_providers(), "S+", today=TODAY, now=NOW)
    assert "승급 후보" in out
    escalation_section = out.split("승급 후보", 1)[1]
    assert "codex-sol --effort xhigh" in escalation_section
