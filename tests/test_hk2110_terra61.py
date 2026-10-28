"""hk 2110 (operator r1027terra): codex-terra61 — gpt-6.1-terra as unmeasured E6 rungs.

wrk adds codex-terra61 for gpt-6.1-terra (a separate agent-skills task) and
scopefuel judges it. It is a NEW model: no measurement exists, so each effort
rung the CLI accepts is a C-carrier unmeasured E6 row opened only by
``SCOPEFUEL_E6_ARM=codex-terra61@<effort>`` — the same mechanism as the other
E6 arm rungs. Nothing is inherited from the barred gpt-5.6-terra rows (operator
2026-10-12 noterra stays in force and their rows keep their history
byte-for-byte) or from sol: no grade, no placement, no benchmark score, no AA
number, and no benchmark is attached at all.

Unlike the pi anthropic-api credit rungs (hk 2079) these rungs are NOT
worker/tester-only: the pair experiment needs terra as a builder, so the
builder seat is allowed with the marker.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from scopefuel import bench, cli, launch
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    E6_ARM_ANNOTATION,
    E6_ARM_CARRIER_GRADES,
    E6_ARM_KEYS,
    E6_ARM_MARKER_ENV,
    E6_ARM_RUNGS,
    E6_WORKER_TESTER_ONLY_PROFILES,
    GRADE_TABLE,
    barred_model_id_for_profile,
    e6_arm_carrier_grade,
    e6_only_profile,
    e6_worker_tester_seat_denied,
    gate_check,
    profile_pool,
    provider_family,
    recommend,
    recommend_dict,
)

TODAY = dt.date(2026, 10, 27)
NOW = dt.datetime(2026, 10, 27, 12, 0, 0, tzinfo=dt.UTC)

PROFILE = "codex-terra61"
MODEL_ID = "gpt-6.1-terra"
RUNGS = ("low", "medium", "high", "xhigh", "max")
# The barred gpt-5.6-terra family — the new profile shares the terra name and
# nothing else.
TERRA56_PROFILES = {"codex-terra", "codex-terra-max"}
# Numbers and identities a mutant could copy from a measured row — none may
# appear on a terra61 row.
STOLEN_TEXT = (
    "gpt-5.6-terra",
    "gpt-6.1-sol",
    "gpt-6-1-sol",
    "62.0",
    "48.0",
    "46.3",
    "43.6",
    "42.8",
    "70.6",
)


def _provider(provider_id: str = "codex", used: float = 10.0, pool_class: str = "spend") -> ProviderResult:
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


def _gate_cli(monkeypatch, capsys, tmp_path, argv: list[str]):
    monkeypatch.setattr(cli, "registry", lambda: {"codex": lambda: _provider("codex")})
    record_path = tmp_path / "gate.json"
    rc = cli.main([*argv, "--no-cache", "--gate-output", str(record_path)])
    captured = capsys.readouterr()
    import json

    record = json.loads(record_path.read_text()) if record_path.exists() else {}
    return rc, captured.out, captured.err, record


def _terra61_rungs():
    return {row.launcher_effort: row for row in E6_ARM_RUNGS if row.name == PROFILE}


def _terra61_catalog_rows():
    return {entry.effort: entry for entry in bench.catalog_snapshot() if entry.profile == PROFILE}


# --- the rows: registered, unmeasured, no inheritance -------------------------


def test_every_cli_effort_is_a_registered_e6_rung():
    assert {(profile, rung) for rung in RUNGS for profile in (PROFILE,)} <= set(E6_ARM_KEYS)
    assert {rung for profile, rung in E6_ARM_KEYS if profile == PROFILE} == set(RUNGS)


def test_each_rung_is_a_catalog_row_at_the_c_carrier():
    rows = _terra61_catalog_rows()
    assert set(rows) == set(RUNGS), rows
    for rung, row in rows.items():
        assert row.grade == "C" == e6_arm_carrier_grade(PROFILE, rung)
        assert row.score is None
        assert row.pool == "codex"
        assert row.model_id == MODEL_ID
        assert row.gate == "default"
        assert row.benchmark_source is None
        assert row.benchmark_annotation.startswith(E6_ARM_ANNOTATION)
        assert "AA 미측정" in row.benchmark_annotation


def test_no_rung_inherits_a_grade_score_or_aa_mapping():
    """Asserted by field — the mutant that copies sol's grade, a 5.6-terra AA
    number, or any benchmark identity onto a terra61 row goes RED here."""
    placed = {(p.name, p.launcher_effort or "") for ps in GRADE_TABLE.values() for p in ps}
    assert not any(key[0] == PROFILE for key in placed), "terra61 has no placement rows"

    rungs = _terra61_rungs()
    assert set(rungs) == set(RUNGS)
    for rung, row in rungs.items():
        # The carrier is the shared default — an E6_ARM_CARRIER_GRADES entry is
        # where a grade inheritance would hide (that is the mutant's call site).
        assert (PROFILE, rung) not in E6_ARM_CARRIER_GRADES
        assert e6_arm_carrier_grade(PROFILE, rung) == "C"
        assert row.benchmark is None
        assert row.benchmark_source is None
        assert row.benchmark_model_id is None
        assert row.aa_model_id is None
        assert row.aa_agent_model_id is None
        assert row.gate == "default"
        annotation = row.benchmark_annotation or ""
        for stolen in STOLEN_TEXT:
            assert stolen not in annotation, (rung, stolen, annotation)
        assert "AA 미측정" in annotation


def test_terra61_is_a_new_family_not_the_barred_56_model():
    assert barred_model_id_for_profile(PROFILE) is None
    # The 5.6 family stays barred — the decision is untouched.
    assert barred_model_id_for_profile("codex-terra") == "gpt-5.6-terra"
    assert barred_model_id_for_profile("codex-terra-max") == "gpt-5.6-terra"


def test_pool_is_codex_and_family_is_openai():
    assert profile_pool(PROFILE) == ("codex", None)
    assert provider_family(PROFILE) == "openai"
    # hk 1729 semantics unchanged: ordinary codex profiles still report the pool.
    assert provider_family("codex-sol") == "codex"
    assert provider_family("codex-luna") == "codex"


def test_terra61_is_e6_only_but_not_worker_tester_only():
    assert e6_only_profile(PROFILE) is True
    # hk 2185: the pi/cmd credit rungs are ordinary inherited placements now,
    # so they are not E6-only any more.
    assert e6_only_profile("pi-haiku55") is False
    assert e6_only_profile("cmd-dsflash41") is False
    assert e6_only_profile("codex-sol") is False  # placed profiles are not
    assert e6_only_profile("opus") is False
    # hk 2110: unlike the pi credit rungs the builder seat is allowed.
    assert PROFILE not in E6_WORKER_TESTER_ONLY_PROFILES
    for seat in ("builder", "worker", "tester", None):
        assert e6_worker_tester_seat_denied(PROFILE, seat) is False


def test_the_seed_emits_the_terra61_rows_with_the_e6_deviation_ref(capsys):
    assert cli.main(["bench", "push-catalog", "--emit-seed", "--decided-by", "operator-desk"]) == 0
    import json

    rows = {(row["profile"], row["effort"]): row for row in json.loads(capsys.readouterr().out)["catalog"]}
    for rung in RUNGS:
        row = rows[(PROFILE, rung)]
        assert row["grade"] == "C"
        assert row["model_id"] == MODEL_ID
        assert row["score"] is None
        assert row["decided_by"] == "operator-desk"
        assert row["deviation_ref"]


# --- the gate: marker-gated, builder allowed ----------------------------------


@pytest.mark.parametrize("rung", RUNGS)
def test_gate_refuses_an_unmarked_rung_with_the_rung_in_the_reason(rung):
    """The no-marker case — a mutant that drops the marker requirement goes RED."""
    result = gate_check([_provider()], PROFILE, effort=rung, today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unmeasurable is False
    assert result.role_denied is False
    assert result.grade == "C"
    assert "e6_arm_required" in result.reason
    assert f"{PROFILE}@{rung}" in result.reason
    assert E6_ARM_MARKER_ENV in result.reason


@pytest.mark.parametrize("rung", RUNGS)
def test_gate_admits_the_rung_with_the_marker_for_a_builder(rung):
    result = gate_check(
        [_provider()],
        PROFILE,
        effort=rung,
        e6_arm=f"{PROFILE}@{rung}",
        role="builder",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is True, result.reason
    assert result.grade == "C"
    assert result.e6_arm == f"{PROFILE}@{rung}"
    assert f"[E6 arm, unmeasured C: {PROFILE}@{rung}]" in result.reason
    assert result.role_denied is False


def test_gate_cli_admits_max_for_a_builder_on_a_healthy_codex_pool(monkeypatch, capsys, tmp_path):
    """The brief's acceptance command, verbatim."""
    monkeypatch.setenv(E6_ARM_MARKER_ENV, "codex-terra61@max")
    rc, out, err, record = _gate_cli(
        monkeypatch,
        capsys,
        tmp_path,
        ["gate", "-m", "codex-terra61", "--effort", "max", "--role", "builder"],
    )
    assert rc == 0, err
    assert "[E6 arm, unmeasured C: codex-terra61@max]" in out.splitlines()[0]
    assert record["ok"] is True
    assert record["e6_arm"] == "codex-terra61@max"
    assert record["grade"] == "C"


def test_gate_cli_refuses_without_the_marker_and_names_the_remedy(monkeypatch, capsys, tmp_path):
    rc, out, err, record = _gate_cli(
        monkeypatch,
        capsys,
        tmp_path,
        ["gate", "-m", "codex-terra61", "--effort", "max", "--role", "builder"],
    )
    assert rc == 3
    assert out == ""
    assert "e6_arm_required" in err
    assert "codex-terra61@max" in err
    assert f"{E6_ARM_MARKER_ENV}=codex-terra61@max" in err
    assert record["ok"] is False


def test_a_bare_profile_spelling_is_refused_without_a_marker(monkeypatch, capsys, tmp_path):
    """The profile exists only as unmeasured E6 rungs — a bare -m has no rung to
    judge, so the refusal names the marker and the registered rungs, never a
    quota answer."""
    rc, _out, err, _record = _gate_cli(monkeypatch, capsys, tmp_path, ["gate", "-m", "codex-terra61"])
    assert rc == 3
    assert "e6_arm_required" in err
    assert f"{E6_ARM_MARKER_ENV}=codex-terra61@<rung>" in err
    for rung in RUNGS:
        assert rung in err


def test_the_marker_alone_names_the_rung_for_the_gate(monkeypatch, capsys, tmp_path):
    """wrk calls `gate -m <profile>` with no --effort — the marker names it."""
    monkeypatch.setenv(E6_ARM_MARKER_ENV, "codex-terra61@max")
    rc, out, _err, _record = _gate_cli(monkeypatch, capsys, tmp_path, ["gate", "-m", "codex-terra61"])
    assert rc == 0
    assert "[E6 arm, unmeasured C: codex-terra61@max]" in out.splitlines()[0]


@pytest.mark.parametrize(
    "marker",
    [
        "codex-terra61@low",  # a different rung of the same profile
        "codex-terra61",  # no rung named
        "codex-terra61@",  # empty rung
        "@max",  # empty profile
        "codex-sol@high",  # a different profile's rung
        "kimi-k3@max",
        "codex-terra61@max@max",  # not a rung name
    ],
)
def test_a_marker_that_does_not_name_this_rung_opens_nothing(marker):
    result = gate_check(
        [_provider()],
        PROFILE,
        effort="max",
        e6_arm=marker,
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert "e6_arm_required" in result.reason
    assert "codex-terra61@max" in result.reason


@pytest.mark.parametrize("marker", ["CODEX-TERRA61@MAX", " codex-terra61@max ", "Codex-Terra61@Max"])
def test_the_marker_is_normalized_like_an_effort_spelling(monkeypatch, capsys, tmp_path, marker):
    monkeypatch.setenv(E6_ARM_MARKER_ENV, marker)
    rc, out, _err, _record = _gate_cli(monkeypatch, capsys, tmp_path, ["gate", "-m", "codex-terra61"])
    assert rc == 0
    assert "[E6 arm, unmeasured C: codex-terra61@max]" in out.splitlines()[0]


# --- consult_only still keeps the marker rule (tester r2) ---------------------


def _terra61_consult_only_view(rung: str = "max") -> launch.CatalogView:
    """A served canon whose only change is one terra61 row gated consult_only."""
    entries = tuple(
        replace(entry, gate="consult_only") if entry.key == (PROFILE, rung) else entry
        for entry in bench.catalog_snapshot()
    )
    return launch.CatalogView(entries=entries, source=bench.CATALOG_SOURCE_CACHE, age_s=0)


@pytest.mark.parametrize("rung", RUNGS)
@pytest.mark.parametrize("role", [None, "builder", "worker", "tester"])
def test_a_consult_only_terra61_rung_keeps_the_marker_rule(rung, role):
    """tester r2: a canon consult_only flag must not lift the E6 marker — the
    same rule hk 2079 pins for the credit rows. Without the marker every seat
    is refused e6_arm_required; the consult spelling is not a bypass."""
    result = gate_check(
        [_provider()],
        PROFILE,
        role=role,
        effort=rung,
        operator_request="hk:task/2110",
        consult_only_rungs=frozenset({(PROFILE, rung)}),
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert "e6_arm_required" in result.reason
    assert f"{PROFILE}@{rung}" in result.reason


@pytest.mark.parametrize("rung", RUNGS)
def test_a_consult_only_terra61_rung_admits_a_marked_builder(rung):
    """With the exact marker the consult gate opens for the builder seat —
    terra61 is not worker/tester-only (unlike the pi credit rows)."""
    result = gate_check(
        [_provider()],
        PROFILE,
        role="builder",
        effort=rung,
        e6_arm=f"{PROFILE}@{rung}",
        operator_request="hk:task/2110",
        consult_only_rungs=frozenset({(PROFILE, rung)}),
        today=TODAY,
        now=NOW,
    )
    # consult admission reports no candidate placement (the fable quota path) —
    # the asserted part is that the marker, not the consult spelling, opened it.
    assert result.ok is True, result.reason
    assert result.e6_arm == f"{PROFILE}@{rung}"


def test_gate_cli_consult_only_terra61_rung_refuses_without_the_marker(monkeypatch, capsys, tmp_path):
    """The tester r2 repro, verbatim: canon flips codex-terra61@max to
    consult_only, an operator ref is given, the marker is absent — the gate
    must refuse e6_arm_required, not admit on the consult spelling."""
    view = _terra61_consult_only_view("max")
    monkeypatch.setattr(bench, "read_catalog", lambda **kwargs: view)
    monkeypatch.setattr(bench, "runtime_grade_table", lambda **kwargs: bench._catalog_grade_table(view))
    monkeypatch.delenv(E6_ARM_MARKER_ENV, raising=False)
    rc, out, err, record = _gate_cli(
        monkeypatch,
        capsys,
        tmp_path,
        [
            "gate",
            "-m",
            "codex-terra61",
            "--effort",
            "max",
            "--operator-request",
            "hk:task/2110",
            "--role",
            "builder",
        ],
    )
    assert rc == 3, out + err
    assert "e6_arm_required" in err
    assert record["ok"] is False


def test_a_retired_terra61_rung_is_not_revived_by_the_marker():
    result = gate_check(
        [_provider()],
        PROFILE,
        effort="max",
        e6_arm="codex-terra61@max",
        retired_rungs=frozenset({(PROFILE, "max")}),
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert "retired" in result.reason


def test_a_measured_canon_row_lifts_the_e6_restriction():
    """Once the canon measures the rung it is ordinary — the restriction is
    'unmeasured at its carrier', not the profile name."""
    table = {grade: list(profiles) for grade, profiles in GRADE_TABLE.items()}
    from scopefuel.recommend import Profile

    table["B"].append(
        Profile(PROFILE, "GPT-6.1 Terra (max)", 45.0, launcher_effort="max", benchmark_effort="max")
    )
    result = gate_check([_provider()], PROFILE, effort="max", today=TODAY, now=NOW, grade_table=table)
    assert result.ok is True
    assert result.grade == "B"
    assert result.e6_arm is None


# --- policy launch ------------------------------------------------------------


def test_policy_launch_prints_model_effort_pool_and_family(monkeypatch, capsys):
    """The brief's acceptance: model_id gpt-6.1-terra, effort max, pool codex."""
    monkeypatch.setenv(E6_ARM_MARKER_ENV, "codex-terra61@max")
    assert cli.main(["policy", "launch", "codex-terra61", "--effort", "max"]) == 0
    lines = capsys.readouterr().out.splitlines()
    for expected in (
        "profile codex-terra61",
        "model_id gpt-6.1-terra",
        "effort max",
        "pool codex",
        "family openai",
        "grade C",
        "e6_arm codex-terra61@max",
    ):
        assert expected in lines


@pytest.mark.parametrize("rung", RUNGS)
def test_resolve_launch_admits_each_rung_with_the_marker(rung):
    decision = launch.resolve_launch(PROFILE, effort=rung, e6_arm=f"{PROFILE}@{rung}")
    assert decision.profile == PROFILE
    assert decision.model_id == MODEL_ID
    assert decision.effort == rung
    assert decision.pool == "codex"
    assert decision.family == "openai"
    assert decision.grade == "C"
    assert decision.gate == "default"
    assert decision.e6_arm == f"{PROFILE}@{rung}"


def test_resolve_launch_admits_a_builder_seat_with_the_marker():
    decision = launch.resolve_launch(PROFILE, effort="max", e6_arm=f"{PROFILE}@max", role="builder")
    assert decision.e6_arm == f"{PROFILE}@max"
    assert decision.model_id == MODEL_ID


def test_policy_launch_refuses_without_the_marker(capsys):
    assert cli.main(["policy", "launch", "codex-terra61", "--effort", "max"]) == 3
    err = capsys.readouterr().err
    assert "e6_arm_required" in err
    assert "codex-terra61@max" in err


def test_a_bare_launch_spelling_is_refused_without_a_marker(capsys):
    assert cli.main(["policy", "launch", "codex-terra61"]) == 3
    err = capsys.readouterr().err
    assert "e6_arm_required" in err
    assert "codex-terra61" in err


def test_the_marker_alone_names_the_rung_for_launch(monkeypatch, capsys):
    """Same asymmetry as the gate: the marker names the rung when --effort is absent."""
    monkeypatch.setenv(E6_ARM_MARKER_ENV, "codex-terra61@xhigh")
    assert cli.main(["policy", "launch", "codex-terra61"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "effort xhigh" in lines
    assert "model_id gpt-6.1-terra" in lines
    assert "e6_arm codex-terra61@xhigh" in lines


def test_a_canon_silent_about_terra61_resolves_the_bundled_rung():
    """A served canon not yet seeded with the rows is silent — the bundled
    measurement row answers, exactly as for the other E6 rungs."""
    view = bench.CatalogView(
        entries=tuple(entry for entry in launch.snapshot_entries() if entry.profile != PROFILE),
        source="server",
        backend="handoffkeep",
    )
    decision = launch.resolve_launch(PROFILE, effort="max", e6_arm=f"{PROFILE}@max", view=view)
    assert decision.e6_arm == f"{PROFILE}@max"
    assert decision.model_id == MODEL_ID
    assert decision.catalog_stale is True


def test_a_retired_terra61_rung_refuses_the_marker_launch():
    entries = tuple(
        replace(entry, retired_at="2026-10-27T00:00:00Z") if entry.key == (PROFILE, "max") else entry
        for entry in bench.catalog_snapshot()
    )
    view = bench.CatalogView(entries=entries, source="cache", backend="handoffkeep")
    with pytest.raises(launch.LaunchError, match="retired"):
        launch.resolve_launch(PROFILE, effort="max", e6_arm=f"{PROFILE}@max", view=view)


# --- recommend never ranks a terra61 rung --------------------------------------


def test_recommend_never_ranks_a_terra61_rung_without_the_marker():
    providers = [_provider(pid, 0.0) for pid in ("claude", "codex", "grok", "kimi", "kiro")]
    for grade in ("S+", "S", "A+", "A", "B", "C"):
        out = recommend(providers, grade, today=TODAY, now=NOW)
        for line in out.splitlines():
            if line[:1].isdigit():
                assert PROFILE not in line, (grade, line)
        payload = recommend_dict(providers, grade, today=TODAY, now=NOW)
        assert not any(row["profile"] == PROFILE for row in payload["rows"]), grade
    # The rungs do show — folded as unmeasured, at C only.
    payload = recommend_dict(providers, "C", today=TODAY, now=NOW)
    folded = {
        (entry["profile"], entry["effort"]) for entry in payload["excluded"] if entry["profile"] == PROFILE
    }
    assert folded == {(PROFILE, rung) for rung in RUNGS}
    assert all(
        "E6 unmeasured" in entry["reason"] for entry in payload["excluded"] if entry["profile"] == PROFILE
    )


def test_a_canon_carrying_the_terra61_rows_does_not_recommend_them():
    view = bench.CatalogView(
        entries=tuple(_terra61_catalog_rows().values())
        + (
            bench.CatalogEntry(
                profile="codex-sol", effort="max", model_id="gpt-6.1-sol", pool="codex", grade="S+"
            ),
        ),
        source="server",
        backend="handoffkeep",
    )
    table = bench._catalog_grade_table(view)
    assert table is not None
    placed = {(p.name, p.launcher_effort or "") for ps in table.values() for p in ps}
    assert not any(key[0] == PROFILE for key in placed)


# --- gpt-5.6-terra rows stay byte-for-byte ------------------------------------


def test_gpt56_terra_catalog_rows_are_byte_for_byte_unchanged():
    """The history stays: model id, grade, score, gate and benchmark fields of
    every 5.6-terra catalog row pinned verbatim."""
    rows = {entry.key: entry for entry in bench.catalog_snapshot() if entry.profile in TERRA56_PROFILES}
    expected = {
        ("codex-terra", "xhigh"): dict(
            model_id="gpt-5.6-terra",
            pool="codex",
            grade="A+",
            score=57.0,
            gate="default",
            benchmark_source="AA-agent",
        ),
        ("codex-terra", "high"): dict(
            model_id="gpt-5.6-terra",
            pool="codex",
            grade="A+",
            score=56.0,
            gate="default",
            benchmark_source="AA-agent",
        ),
        ("codex-terra", "medium"): dict(
            model_id="gpt-5.6-terra",
            pool="codex",
            grade="A",
            score=48.0,
            gate="default",
            benchmark_source="AA-agent",
        ),
        ("codex-terra-max", ""): dict(
            model_id="gpt-5.6-terra",
            pool="codex",
            grade="S",
            score=62.0,
            gate="default",
            benchmark_source="AA-agent",
        ),
    }
    assert set(rows) == set(expected), sorted(rows)
    for key, fields in expected.items():
        row = rows[key]
        actual = row.as_dict()
        for field, value in fields.items():
            assert actual[field] == value, (key, field, actual[field])


def test_gpt56_terra_spellings_are_still_refused():
    """The barred family is untouched — every 5.6 spelling still refuses."""
    for spelling in ("codex-terra", "codex-terra-max", "codex-med", "builder-terra-max"):
        with pytest.raises(launch.LaunchError, match="refused"):
            launch.resolve_launch(spelling)


def test_terra61_keys_do_not_collide_with_terra56_alias_keys():
    """E6_ARM_KEYS names canonical (profile, effort) pairs — the barred aliases
    must not alias-resolve onto terra61 and vice versa."""
    for key in E6_ARM_KEYS:
        assert key[0] != "codex-terra"
        assert key[0] != "codex-terra-max"
    from scopefuel.recommend import PROFILE_ALIASES

    assert PROFILE_ALIASES.get(PROFILE, PROFILE) == PROFILE
