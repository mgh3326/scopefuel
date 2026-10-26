"""hk 1990: catalog-only consultation and exact retired experiment admission."""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import io
import json
import pathlib
import re
import subprocess
import sys
import tarfile

import pytest

from scopefuel import bench, cli, launch, policy, recommend
from scopefuel.model import Bucket, ProviderResult, Scope

BASE = "3c96924e0bf4b551d52ed5314f858419761c1dbf"
NOW = dt.datetime(2026, 10, 25, 12, tzinfo=dt.UTC)
REF = "hk:task/1786"


def _providers(used=10):
    return [
        ProviderResult(
            id=pool,
            pool_class="spend",
            buckets=[
                Bucket(
                    label=window,
                    window=window,
                    used_pct=used,
                    resets_at=(NOW + dt.timedelta(hours=100)).isoformat(),
                    scope=Scope("account"),
                )
                for window in ("5h", "7d")
            ],
        )
        for pool in ("claude", "codex")
    ]


def _view(profile, effort, *, retired=False, gate="consult_only", source="server"):
    rows = [e for e in launch.snapshot_entries() if e.key != (profile, effort)]
    rows.append(
        bench.CatalogEntry(
            profile=profile,
            effort=effort,
            model_id="fixture-model",
            pool="codex" if profile.startswith("codex") else "claude",
            grade="C",
            score=25.0 if gate == "escalation" else None,
            gate=gate,
            retired_at="2026-10-24T00:00:00Z" if retired else None,
        )
    )
    return bench.CatalogView(entries=tuple(rows), source=source, backend="handoffkeep")


@pytest.fixture
def catalog_gate(monkeypatch):
    monkeypatch.setattr(recommend, "_observe_exhaustion", lambda *a, **k: None)
    monkeypatch.setattr(recommend, "_inflight_gate", lambda *a, **k: None)

    def run(view, profile, effort, marker=None, ref=None, used=10):
        monkeypatch.setattr(bench, "read_catalog", lambda **k: view)
        if marker is None:
            monkeypatch.delenv(recommend.E6_ARM_MARKER_ENV, raising=False)
        else:
            monkeypatch.setenv(recommend.E6_ARM_MARKER_ENV, marker)
        args = cli.build_parser(["claude", "codex"]).parse_args(
            ["gate", "-m", profile]
            + (["--effort", effort] if effort is not None else [])
            + (["--operator-request", ref] if ref else [])
        )
        table = bench._catalog_grade_table(view)
        assert table is not None
        return recommend.gate_check(
            _providers(used),
            profile,
            today=NOW.date(),
            now=NOW,
            grade_table=table,
            **cli._gate_args(args),
            **cli._gate_rung_args(args),
        )

    return run


LIVE_CASES = [
    pytest.param("haiku", "high", False, None, REF, True, id="consult-ref"),
    pytest.param("haiku", "high", False, None, None, False, id="consult-no-ref"),
    pytest.param("haiku", "xhigh", False, None, REF, True, id="consult-e6-ref"),
    pytest.param("haiku", "xhigh", False, None, None, False, id="consult-e6-no-ref"),
    pytest.param("fable", "xhigh", False, None, REF, True, id="fable-ref"),
    pytest.param("fable", "xhigh", False, None, None, False, id="fable-no-ref"),
]
RETIRED_CASES = []
for profile, effort in (("haiku", "xhigh"), ("opus", "high")):
    for name, marker, ref, ok in (
        ("experiment", f"{profile}@{effort}", REF, True),
        ("no-marker", None, REF, False),
        ("no-marker-no-ref", None, None, False),
        ("other-profile", "sonnet@high", REF, False),
        ("other-effort", f"{profile}@max", REF, False),
        ("other-task", f"{profile}@{effort}", "hk:task/1787", False),
        ("prefix-task", f"{profile}@{effort}", "hk:task/17860", False),
        ("doc-ref", f"{profile}@{effort}", "hk:doc/1786", False),
        ("no-ref", f"{profile}@{effort}", None, False),
        ("escalation-no-marker", None, "hk:task/461", False),
        ("case-ref", f"{profile}@{effort}", "HK:task/1786", False),
        ("space-ref", f"{profile}@{effort}", "hk:task/1786 ", False),
        ("leading-zero-ref", f"{profile}@{effort}", "hk:task/01786", False),
        ("normalized-marker", f" {profile.upper()} @ {effort.upper()} ", REF, True),
    ):
        RETIRED_CASES.append(pytest.param(profile, effort, True, marker, ref, ok, id=f"{profile}-{name}"))


R4_CASES = []
for effort in ("xhigh", "max"):
    for name, marker, ref, ok in (
        ("marker-ref", f"haiku@{effort}", REF, True),
        ("marker-only", f"haiku@{effort}", None, True),
        ("ref-only", None, REF, False),
    ):
        R4_CASES.append(
            pytest.param("haiku", effort, False, marker, ref, ok, "default", id=f"r4-{effort}-{name}")
        )
R4_CASES.append(
    pytest.param("haiku", "high", False, None, REF, True, "escalation", id="r4-measured-escalation-ref")
)
PARITY_CASES = [
    pytest.param(*case.values, "consult_only", id=case.id) for case in LIVE_CASES + RETIRED_CASES
] + R4_CASES


@pytest.mark.parametrize("profile,effort,retired,marker,ref,ok,gate_kind", PARITY_CASES)
@pytest.mark.parametrize("source", ["server", "cache-stale", "snapshot"])
def test_gate_launch_parity(catalog_gate, profile, effort, retired, marker, ref, ok, gate_kind, source):
    view = _view(profile, effort, retired=retired, source=source, gate=gate_kind)
    gate = catalog_gate(view, profile, effort, marker, ref)
    try:
        decision = launch.resolve_launch(
            profile, effort=effort, view=view, e6_arm=marker, operator_request=ref or False
        )
    except launch.LaunchError as exc:
        launch_ok = False
        reason = str(exc)
    else:
        launch_ok = True
    assert gate.ok == ok, gate.reason
    assert launch_ok == ok, reason if not launch_ok else decision
    assert gate.ok == launch_ok
    if retired and ok:
        record = cli._gate_record(gate, 0, NOW)
        assert record["experiment_retired"] == f"{profile}@{effort}"
        assert record["operator_request_ref"] == REF
        assert decision.as_dict()["experiment_retired"] == f"{profile}@{effort}"
        assert decision.as_dict()["operator_request_ref"] == REF
    elif ok and gate_kind == "default" and ref:
        assert cli._gate_record(gate, 0, NOW)["operator_request_ref"] == ref
        assert decision.as_dict()["operator_request_ref"] == ref
        assert f"operator_request_ref {ref}" in decision.render()
    elif not ok:
        expected = "retired" if retired else ("e6_arm_required" if gate_kind == "default" else "consult_only")
        if ref in ("HK:task/1786", "hk:task/1786 "):
            assert "operator_request_ref_invalid" in gate.reason
            assert "operator_request_ref_invalid" in reason
        else:
            assert expected in gate.reason
            assert expected in reason


def test_default_row_ref_still_not_applicable(catalog_gate):
    view = _view("opus", "high", gate="default")
    result = catalog_gate(view, "opus", "high", ref=REF)
    assert not result.ok
    assert result.reason.startswith("operator_request_not_applicable")


@pytest.mark.parametrize("retired,marker", [(False, None), (True, "haiku@high")])
def test_consult_and_experiment_keep_remaining_checks(catalog_gate, retired, marker):
    view = _view("haiku", "high", retired=retired)
    assert not catalog_gate(view, "haiku", "high", marker, REF, used=100).ok
    policy.set_policy("claude", "exclude", until=dt.date(2099, 1, 1))
    result = catalog_gate(view, "haiku", "high", marker, REF)
    assert not result.ok
    assert "정책 제외" in result.reason


def test_retired_bare_launch_refused(monkeypatch, capsys):
    view = _view("haiku", "high", retired=True)
    monkeypatch.setattr(launch, "read_catalog", lambda **k: view)
    monkeypatch.setenv(recommend.E6_ARM_MARKER_ENV, "haiku@high")
    assert cli.main(["policy", "launch", "haiku", "--effort", "high", "--operator-request"]) == 3
    assert "retired" in capsys.readouterr().err


# hk 2021: launch checks the REF shape on every path, the same way the gate does.
REF_MESSAGE = "operator_request_ref_invalid: {ref!r} — hk:doc/<key> 또는 hk:task/<정수> 만 허용"
OVERLONG_REF = "hk:doc/" + "a" * (recommend.OPERATOR_REQUEST_REF_MAX - len("hk:doc/") + 1)
BOUNDARY_REF = "hk:doc/" + "a" * (recommend.OPERATOR_REQUEST_REF_MAX - len("hk:doc/"))
MALFORMED_REFS = [
    pytest.param("hk:task/1786\n", id="newline"),
    pytest.param("hk:task/1786\r", id="carriage-return"),
    pytest.param("hk:task/1786\t", id="tab"),
    pytest.param("hk:task/1786 x", id="space"),
    pytest.param("hk:task/1786\x07", id="control-byte"),
    pytest.param("hk:task/1786\x00", id="nul"),
    pytest.param("hk:task/1786 ", id="line-separator"),
    pytest.param("hk:task/1786 ", id="paragraph-separator"),
    pytest.param("hk:doc/Briefs/b953/x", id="uppercase-doc-key"),
    pytest.param("hk:task/1x", id="task-not-integer"),
    pytest.param(OVERLONG_REF, id="overlong"),
]
VALID_REFS = [
    pytest.param("hk:task/1786", id="task"),
    pytest.param("hk:doc/briefs/b953/x", id="doc"),
    pytest.param(BOUNDARY_REF, id="length-boundary"),
]
LAUNCH_PATHS = [
    pytest.param("haiku", "high", "consult_only", None, id="R1-consult"),
    pytest.param("haiku", "xhigh", "default", "haiku@xhigh", id="R4-e6"),
]


@pytest.mark.parametrize("ref", MALFORMED_REFS)
@pytest.mark.parametrize("profile,effort,gate_kind,marker", LAUNCH_PATHS)
def test_launch_refuses_malformed_ref_like_the_gate(catalog_gate, profile, effort, gate_kind, marker, ref):
    view = _view(profile, effort, gate=gate_kind)
    gate = catalog_gate(view, profile, effort, marker, ref)
    with pytest.raises(launch.LaunchError) as raised:
        launch.resolve_launch(profile, effort=effort, view=view, e6_arm=marker, operator_request=ref)
    message = REF_MESSAGE.format(ref=ref)
    assert str(raised.value) == message
    assert gate.reason == message


@pytest.mark.parametrize("ref", MALFORMED_REFS)
def test_policy_launch_refusal_is_one_exact_line_without_ref_text(monkeypatch, capsys, ref):
    view = _view("haiku", "high")
    monkeypatch.setattr(launch, "read_catalog", lambda **k: view)
    monkeypatch.delenv(recommend.E6_ARM_MARKER_ENV, raising=False)
    assert cli.main(["policy", "launch", "haiku", "--effort", "high", "--operator-request", ref]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {REF_MESSAGE.format(ref=ref)}\n"


@pytest.mark.parametrize("ref", [*VALID_REFS, pytest.param("hk:task/1786\n", id="malformed")])
@pytest.mark.parametrize("profile,effort,gate_kind,marker", LAUNCH_PATHS)
def test_gate_and_launch_agree_on_every_ref(catalog_gate, profile, effort, gate_kind, marker, ref):
    view = _view(profile, effort, gate=gate_kind)
    gate = catalog_gate(view, profile, effort, marker, ref)
    try:
        launch.resolve_launch(profile, effort=effort, view=view, e6_arm=marker, operator_request=ref)
    except launch.LaunchError:
        launch_ok = False
    else:
        launch_ok = True
    assert gate.ok == launch_ok


@pytest.mark.parametrize("ref", VALID_REFS)
@pytest.mark.parametrize("profile,effort,gate_kind,marker", LAUNCH_PATHS)
def test_valid_ref_still_launches_on_r1_and_r4(profile, effort, gate_kind, marker, ref):
    view = _view(profile, effort, gate=gate_kind)
    decision = launch.resolve_launch(profile, effort=effort, view=view, e6_arm=marker, operator_request=ref)
    assert decision.operator_request is True
    if marker:
        assert decision.e6_arm == marker
        assert f"operator_request_ref {ref}" in decision.render()


def test_valid_ref_still_launches_on_round_extension_builder():
    ref = "hk:doc/round-ext/b1632-sol"
    view = _view("codex-astra", "high", gate="consult_only")
    decision = launch.resolve_launch(
        "codex-astra",
        effort="high",
        view=view,
        role="builder",
        purpose="round-extension",
        operator_request=ref,
    )
    assert decision.as_dict()["round_extension_builder"]["ref"] == ref


def test_bare_operator_request_flag_unchanged(monkeypatch, capsys):
    view = _view("haiku", "high")
    monkeypatch.setattr(launch, "read_catalog", lambda **k: view)
    monkeypatch.delenv(recommend.E6_ARM_MARKER_ENV, raising=False)
    assert cli.main(["policy", "launch", "haiku", "--effort", "high", "--operator-request"]) == 0
    assert "gate consult_only" in capsys.readouterr().out


# The astra/fable role check runs before the REF shape check (as in the gate), so a
# malformed REF on that path keeps its role_restricted refusal and must not echo the REF.
ROLE_PATH_REFS = [
    pytest.param("hk:doc/round-ext/b1632-sol\nINJECTED", id="newline"),
    pytest.param("hk:doc/round-ext/b1632-sol\rINJECTED", id="carriage-return"),
    pytest.param("hk:doc/round-ext/b1632-sol\x07INJECTED", id="control-byte"),
]


@pytest.mark.parametrize("ref", ROLE_PATH_REFS)
def test_role_restricted_path_does_not_echo_malformed_ref(monkeypatch, capsys, ref):
    view = _view("codex-astra", "high", gate="consult_only")
    monkeypatch.setattr(launch, "read_catalog", lambda **k: view)
    monkeypatch.delenv(recommend.E6_ARM_MARKER_ENV, raising=False)
    with pytest.raises(launch.LaunchError) as raised:
        launch.resolve_launch(
            "codex-astra", effort="high", role="builder", purpose="round-extension", operator_request=ref
        )
    message = str(raised.value)
    assert message.startswith("role_restricted")
    assert "INJECTED" not in message and "\n" not in message and "\r" not in message
    assert (
        cli.main(
            [
                "policy",
                "launch",
                "codex-astra",
                "--effort",
                "high",
                "--role",
                "builder",
                "--purpose",
                "round-extension",
                "--operator-request",
                ref,
            ]
        )
        == 3
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "INJECTED" not in captured.err


def _load_base(tmp_path):
    repo = pathlib.Path(__file__).resolve().parents[1]
    if subprocess.run(["git", "cat-file", "-e", BASE], cwd=repo, capture_output=True).returncode:
        subprocess.run(
            ["git", "fetch", "--depth=1", "origin", BASE], cwd=repo, check=True, capture_output=True
        )
    archive = subprocess.run(
        ["git", "archive", BASE, "src"], cwd=repo, check=True, capture_output=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp_path, filter="data")
    pkg = tmp_path / "src/scopefuel"
    spec = importlib.util.spec_from_file_location(
        "scopefuel_1990_base", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return importlib.import_module(f"{spec.name}.recommend"), importlib.import_module(f"{spec.name}.bench")


def _drop_aplus_opus_low_text(text):
    """Base A+ text minus the borrowed opus --effort low listing (hk 2007, r1025p1apply).

    The ranked row is dropped and later ranks renumbered; the folded excluded
    entry for the same rung is dropped and the fold count corrected. Returns the
    transformed text and whether anything was dropped.
    """
    dropped = False
    dropped_rank = None
    kept = []
    for line in text.split("\n"):
        match = re.match(r"^(\d+)\. (.*)$", line)
        body = match.group(2) if match else ""
        while body.startswith("🔥 "):  # the CLI's hot-rung marker, as in _line_label
            body = body[len("🔥 ") :]
        if match and body.startswith("opus --effort low") and "[borrowed S]" in line:
            assert dropped_rank is None, text  # at most one such row per listing
            dropped_rank = int(match.group(1))
            dropped = True
            continue
        fold = re.match(r"^✗ 제외 \((\d+)\): (.*)$", line)
        if fold:
            items = fold.group(2).split(" · ")
            kept_items = [item for item in items if not item.startswith("opus[borrowed S]@low")]
            if len(kept_items) != len(items):
                assert kept_items, text
                dropped = True
                count = int(fold.group(1)) - (len(items) - len(kept_items))
                line = f"✗ 제외 ({count}): " + " · ".join(kept_items)
        kept.append(line)
    renumbered = []
    for line in kept:
        match = re.match(r"^(\d+)\. (.*)$", line)
        if dropped_rank is not None and match and int(match.group(1)) > dropped_rank:
            line = f"{int(match.group(1)) - 1}. {match.group(2)}"
        renumbered.append(line)
    return "\n".join(renumbered), dropped


def _drop_aplus_opus_low_payload(payload):
    """Base A+ payload minus the borrowed opus --effort low row and its excluded entry."""
    rows = [
        row
        for row in payload["rows"]
        if not (
            row["profile"] == "opus" and row.get("borrowed") is True and row["display"] == "opus --effort low"
        )
    ]
    excluded = [
        entry
        for entry in payload["excluded"]
        if not (entry["profile"] == "opus" and entry["effort"] == "low")
    ]
    dropped = len(rows) != len(payload["rows"]) or len(excluded) != len(payload["excluded"])
    return {**payload, "rows": rows, "excluded": excluded}, dropped


def test_recommend_all_grades_text_json_byte_identical(tmp_path, monkeypatch):
    from test_task1787_inflight_sum import _matrix
    from test_task1795_borrowed_rungs import _pool_providers

    base_recommend, base_bench = _load_base(tmp_path / "base")
    monkeypatch.setattr(base_recommend, "_observe_exhaustion", lambda *a, **k: None)
    monkeypatch.setattr(recommend, "_observe_exhaustion", lambda *a, **k: None)
    views = [None, _view("haiku", "high", retired=True), _view("opus", "high", retired=True)]
    a_plus_dropped = 0
    for view in views:
        current_table = recommend.GRADE_TABLE if view is None else bench._catalog_grade_table(view)
        base_table = base_recommend.GRADE_TABLE if view is None else base_bench._catalog_grade_table(view)
        for providers in [*[providers for providers, _ in _matrix()], _pool_providers()]:
            for grade in recommend.GRADE_TABLE:
                outputs = []
                for module, table in ((recommend, current_table), (base_recommend, base_table)):
                    kwargs = dict(
                        today=NOW.date(), now=NOW, bench_scores=[], model_prices={}, grade_table=table
                    )
                    text = module.recommend(providers, grade, **kwargs)
                    payload = module.recommend_dict(providers, grade, **kwargs)
                    outputs.append((text, json.dumps(payload, ensure_ascii=False)))
                expected = outputs[1]
                if grade == "A+":
                    # hk 2007: A+ is base minus exactly the borrowed opus --effort low
                    # row (text: renumbered ranks; JSON: the row dropped). Not a skip.
                    expected_text, text_dropped = _drop_aplus_opus_low_text(expected[0])
                    expected_payload, json_dropped = _drop_aplus_opus_low_payload(json.loads(expected[1]))
                    a_plus_dropped += int(text_dropped or json_dropped)
                    expected = (expected_text, json.dumps(expected_payload, ensure_ascii=False))
                assert outputs[0] == expected, (view, grade)
                if view is not None:
                    retired = {(e.profile, e.effort) for e in view.entries if e.retired_at}
                    assert not any(
                        (p.name, p.launcher_effort or "") in retired
                        for ps in current_table.values()
                        for p in ps
                    )
    # The A+ exemption must actually be exercised: the base listed the row somewhere.
    assert a_plus_dropped > 0


def test_cli_recommend_all_grades_byte_identical(tmp_path, monkeypatch, capsys):
    import types

    _load_base(tmp_path / "base")
    base_cli = importlib.import_module("scopefuel_1990_base.cli")
    base_bench = importlib.import_module("scopefuel_1990_base.bench")

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    view = _view("haiku", "high", retired=True)
    for module, catalog in ((cli, bench), (base_cli, base_bench)):
        table = catalog._catalog_grade_table(view)
        monkeypatch.setattr(module, "dt", types.SimpleNamespace(datetime=Clock, UTC=dt.UTC))
        monkeypatch.setattr(module, "collect", lambda *a, **k: _providers())
        monkeypatch.setattr(module, "registry", lambda: {})
        monkeypatch.setattr(module, "_refresh_swe2_result", lambda *a, **k: None)
        monkeypatch.setattr(module.manual, "apply_for_display", lambda results, **k: results)
        monkeypatch.setattr(module.free_window, "active_record", lambda **k: None)
        monkeypatch.setattr(catalog, "read_scores", lambda **k: [])
        monkeypatch.setattr(catalog, "read_prices", lambda **k: {})
        monkeypatch.setattr(catalog, "read_catalog", lambda **k: view)
        monkeypatch.setattr(catalog, "runtime_grade_table", lambda table=table, **k: table)
    a_plus_dropped = 0
    for grade in recommend.GRADE_TABLE:
        for output in ([], ["--json"]):
            argv = ["--recommend", grade, *output]
            assert cli.main(argv) == 0
            actual = capsys.readouterr()
            assert base_cli.main(argv) == 0
            expected = capsys.readouterr()
            if grade != "A+":
                assert actual == expected, argv
                continue
            # hk 2007: A+ is base minus exactly the borrowed opus --effort low row.
            assert actual.err == expected.err, argv
            if output:
                payload, dropped = _drop_aplus_opus_low_payload(json.loads(expected.out))
                assert json.loads(actual.out) == payload, argv
            else:
                text, dropped = _drop_aplus_opus_low_text(expected.out)
                assert actual.out == text, argv
            a_plus_dropped += int(dropped)
    assert a_plus_dropped > 0


def test_catalog_key_failure_is_empty(monkeypatch):
    def unavailable(**kwargs):
        raise bench.BenchError("fixture unavailable")

    monkeypatch.setattr(bench, "read_catalog", unavailable)
    assert cli._retired_rungs() == frozenset()
    assert cli._consult_only_rungs() == frozenset()
    assert cli._gate_policy_context("haiku", None, None) is None


def test_retired_experiment_render_and_unmeasurable(catalog_gate, monkeypatch):
    view = _view("opus", "high", retired=True)
    decision = launch.resolve_launch(
        "opus", effort="high", view=view, e6_arm="opus@high", operator_request=REF
    )
    assert "experiment_retired opus@high" in decision.render()
    assert f"operator_request_ref {REF}" in decision.render()
    monkeypatch.setattr(sys.modules[__name__], "_providers", lambda used=10: [])
    result = catalog_gate(view, "opus", "high", "opus@high", REF)
    assert not result.ok and result.unmeasurable


@pytest.mark.parametrize(
    "profile,effort,marker,matched",
    [
        ("codex-max", "high", "codex-sol@high", True),
        ("codex-sol", "high", "codex-max@high", True),
        ("codex-luna", "max", "codex-luna-max@max", False),
        ("codex-luna-max", "max", "codex-luna-max@max", True),
    ],
)
def test_retired_aliases_do_not_widen(catalog_gate, profile, effort, marker, matched):
    canonical = recommend.PROFILE_ALIASES.get(profile, profile)
    view = _view(canonical, effort, retired=True)
    for ref, expected in ((REF, matched), ("hk:task/17860", False), (None, False)):
        gate = catalog_gate(view, profile, effort, marker, ref)
        try:
            decision = launch.resolve_launch(
                profile, effort=effort, view=view, e6_arm=marker, operator_request=ref or False
            )
        except launch.LaunchError:
            launch_ok = False
        else:
            launch_ok = True
            assert decision.profile == canonical
        assert gate.ok == launch_ok == expected


def test_retired_experiment_keeps_barred_model_guard():
    from dataclasses import replace

    view = _view("opus", "high", retired=True)
    view = replace(
        view,
        entries=tuple(
            replace(e, model_id="gpt-5.6-terra") if e.key == ("opus", "high") else e for e in view.entries
        ),
    )
    with pytest.raises(launch.LaunchError, match="barred"):
        launch.resolve_launch("opus", effort="high", view=view, e6_arm="opus@high", operator_request=REF)


def test_retired_marker_selects_same_rung_without_effort(monkeypatch):
    view = _view("haiku", "high", retired=True)
    monkeypatch.setattr(bench, "read_catalog", lambda **k: view)
    monkeypatch.setenv(recommend.E6_ARM_MARKER_ENV, "haiku@high")
    result = recommend.gate_check(
        _providers(),
        "haiku",
        today=NOW.date(),
        now=NOW,
        operator_request=REF,
        retired_rungs=cli._retired_rungs(),
        e6_arm="haiku@high",
        grade_table=bench._catalog_grade_table(view),
    )
    decision = launch.resolve_launch("haiku", view=view, e6_arm="haiku@high", operator_request=REF)
    assert result.ok
    assert decision.effort == "high"
    assert decision.experiment_retired == "haiku@high"


def test_bare_launch_flag_preserves_following_options():
    args = cli.build_parser(["claude", "codex"]).parse_args(
        [
            "policy",
            "launch",
            "fable",
            "--operator-request",
            "--purpose",
            "architect",
            "--role",
            "counsel",
            "--effort",
            "xhigh",
        ]
    )
    assert args.operator_request is True
    assert (args.purpose, args.role, args.effort) == ("architect", "counsel", "xhigh")


@pytest.fixture
def implicit_providers(monkeypatch):
    from dataclasses import replace

    providers = _providers()
    providers += [replace(providers[0], id=pool) for pool in ("grok", "devin")]
    monkeypatch.setattr(sys.modules[__name__], "_providers", lambda used=10: providers)


@pytest.mark.parametrize("profile", ["grok-hi", "devin-swe2", "devin-swe2-max"])
@pytest.mark.parametrize("projection", ["retired", "consult_only"])
def test_implicit_projection_selects_live_effort(catalog_gate, implicit_providers, profile, projection):
    from dataclasses import replace

    entries = tuple(
        replace(e, retired_at="2026-10-24T00:00:00Z")
        if projection == "retired"
        else replace(e, gate="consult_only")
        for e in launch.snapshot_entries()
        if e.key == (profile, "")
    )
    assert len(entries) == 1
    view = bench.CatalogView(
        entries=tuple(e for e in launch.snapshot_entries() if e.key != (profile, "")) + entries,
        source="server",
        backend="handoffkeep",
    )
    gate = catalog_gate(view, profile, None)
    decision = launch.resolve_launch(profile, view=view)
    assert gate.ok, gate.reason
    assert decision.effort
    assert decision.gate == "default"


@pytest.mark.parametrize("catalog_effort", ["", "high"])
@pytest.mark.parametrize("ref", [None, REF])
def test_implicit_consult_selection_remains_ref_gated(catalog_gate, catalog_effort, ref):
    view = _view("haiku", catalog_effort)
    view = bench.CatalogView(
        entries=tuple(e for e in view.entries if e.profile != "haiku" or e.effort == catalog_effort),
        source="server",
        backend="handoffkeep",
    )
    gate = catalog_gate(view, "haiku", None, ref=ref)
    assert gate.ok == bool(ref), gate.reason
    if ref:
        assert launch.resolve_launch("haiku", view=view, operator_request=ref).gate == "consult_only"
    else:
        assert "consult_only" in gate.reason
        with pytest.raises(launch.LaunchError, match="consult_only"):
            launch.resolve_launch("haiku", view=view)


@pytest.mark.parametrize("profile", ["haiku", "grok-hi"])
@pytest.mark.parametrize("marker,ref", [(None, None), (None, REF), ("unlisted", None), ("unlisted", REF)])
def test_implicit_fully_retired_profile_stays_closed(catalog_gate, implicit_providers, profile, marker, ref):
    from dataclasses import replace

    view = bench.CatalogView(
        entries=tuple(
            replace(e, retired_at="2026-10-24T00:00:00Z") if e.profile == profile else e
            for e in launch.snapshot_entries()
        ),
        source="server",
        backend="handoffkeep",
    )
    marker = f"{profile}@{marker}" if marker else None
    gate = catalog_gate(view, profile, None, marker, ref)
    assert not gate.ok
    assert "retired" in gate.reason
    with pytest.raises(launch.LaunchError, match="retired"):
        launch.resolve_launch(profile, view=view, e6_arm=marker, operator_request=ref or False)


def test_fully_retired_profile_keeps_exact_experiment(catalog_gate):
    from dataclasses import replace

    view = _view("haiku", "xhigh", retired=True)
    view = bench.CatalogView(
        entries=tuple(
            replace(e, retired_at="2026-10-24T00:00:00Z") if e.profile == "haiku" else e for e in view.entries
        ),
        source="server",
        backend="handoffkeep",
    )
    gate = catalog_gate(view, "haiku", None, "haiku@xhigh", REF)
    decision = launch.resolve_launch("haiku", view=view, e6_arm="haiku@xhigh", operator_request=REF)
    assert gate.ok, gate.reason
    assert gate.experiment_retired == decision.experiment_retired == "haiku@xhigh"


def test_implicit_live_snapshot_gate_matches_base(tmp_path, monkeypatch, catalog_gate, implicit_providers):
    base_recommend, base_bench = _load_base(tmp_path / "base")
    base_cli = importlib.import_module("scopefuel_1990_base.cli")
    view = bench.CatalogView(entries=launch.snapshot_entries(), source="server", backend="handoffkeep")
    monkeypatch.setattr(base_bench, "read_catalog", lambda **k: view)
    monkeypatch.setattr(base_recommend, "_observe_exhaustion", lambda *a, **k: None)
    monkeypatch.setattr(base_recommend, "_inflight_gate", lambda *a, **k: None)
    table = base_bench._catalog_grade_table(view)
    assert table is not None
    for profile in sorted({e.profile for e in view.entries}):
        actual = catalog_gate(view, profile, None)
        args = base_cli.build_parser(["claude", "codex"]).parse_args(["gate", "-m", profile])
        expected = base_recommend.gate_check(
            _providers(),
            profile,
            today=NOW.date(),
            now=NOW,
            grade_table=table,
            **base_cli._gate_args(args),
            **base_cli._gate_rung_args(args),
        )
        assert (actual.ok, actual.reason, actual.grade) == (expected.ok, expected.reason, expected.grade), (
            profile
        )


def test_implicit_policy_context_never_invents_an_ordinary_rung(monkeypatch):
    view = bench.CatalogView(entries=(), source="server", backend="handoffkeep")
    monkeypatch.setattr(bench, "read_catalog", lambda **k: view)
    assert cli._gate_policy_context("haiku", None, None) is None
    view = bench.CatalogView(
        entries=tuple(e for e in launch.snapshot_entries() if e.key == ("haiku", "xhigh")),
        source="server",
        backend="handoffkeep",
    )
    assert cli._gate_policy_context("haiku", None, None) is None
