"""hk 1632: a round-extension reference is the key, never a purpose string."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from test_gate import NOW, TODAY, _healthy_s_plus_providers, _result

from scopefuel import cli, launch, recommend
from scopefuel.bench import CATALOG_SOURCE_SNAPSHOT, CatalogView


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    for name in (
        "HOME",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
    ):
        monkeypatch.setenv(name, str(tmp_path / name))


REFS = [
    "hk:doc/round-extension/b1632-sol",
    "hk:doc/round-ext/b1632-sol",
    "hk:doc/parkingarcade/round-ext/b1632-sol",
]

# Read-only hk doc list snapshot, 2026-10-18: all ten existing round-ext keys.
REAL_KEYS = [
    "round-ext/b1391",
    "round-ext/b1391-r5",
    "round-ext/b1631-sol",
    "round-extension/865-watch-kick-filter-20260928-0948",
    "round-extension/900-investor-flow-json-20260928-1923",
    "parkingarcade/round-ext/b142-cx",
    "parkingarcade/round-ext/b1575-ds",
    "parkingarcade/round-ext/b1591-sol",
    "parkingarcade/round-ext/b1598-cx",
    "parkingarcade/round-ext/b1619-op",
]


def test_real_keys_fit_generic_and_round_extension_grammar():
    assert len(REAL_KEYS) == 10
    for key in REAL_KEYS:
        ref = "hk:doc/" + key
        assert recommend.ROUND_EXT_REF.fullmatch(ref)
        assert recommend._operator_request_ref_valid(ref)


@pytest.mark.parametrize("prefix", ["round-ext", "round-extension", "parkingarcade/round-ext"])
@pytest.mark.parametrize("job", ["B1632-Sol", ".job", "_job", "-job", ".", "..", "j" * 128])
def test_invalid_job_grammar_agrees_with_generic_validator(prefix, job):
    ref = f"hk:doc/{prefix}/{job}"
    assert recommend.ROUND_EXT_REF.fullmatch(ref) is None
    assert not recommend._operator_request_ref_valid(ref)
    result = recommend.gate_check(
        _healthy_s_plus_providers(),
        "codex-astra",
        today=TODAY,
        now=NOW,
        purpose="round-extension",
        operator_request=ref,
    )
    assert result.role_denied
    with pytest.raises(launch.LaunchError, match="role_restricted"):
        launch.resolve_launch("codex-astra", purpose="round-extension", operator_request=ref)


@pytest.mark.parametrize("prefix", ["round-ext", "round-extension", "parkingarcade/round-ext"])
def test_ref_length_boundary_is_shared(prefix):
    base = f"hk:doc/{prefix}/"
    ref = base + "j" * (recommend.OPERATOR_REQUEST_REF_MAX - len(base))
    assert recommend.ROUND_EXT_REF.fullmatch(ref)
    assert recommend._operator_request_ref_valid(ref)
    assert recommend.ROUND_EXT_REF.fullmatch(ref + "j") is None
    assert not recommend._operator_request_ref_valid(ref + "j")


@pytest.mark.parametrize("profile", ["codex-astra", "fable"])
@pytest.mark.parametrize("role", ["tester", "reviewer"])
@pytest.mark.parametrize("purpose", [None, "director", "architect", "operator-request"])
def test_tester_and_reviewer_cannot_use_counsel_grants(profile, role, purpose):
    for ref in (None, "hk:doc/ordinary/request", REFS[0]):
        result = recommend.gate_check(
            _healthy_s_plus_providers(),
            profile,
            today=TODAY,
            now=NOW,
            role=role,
            purpose=purpose,
            operator_request=ref,
        )
        assert result.role_denied
        with pytest.raises(launch.LaunchError, match="role_restricted"):
            launch.resolve_launch(profile, role=role, purpose=purpose, operator_request=ref or False)


@pytest.mark.parametrize("profile", ["codex-astra", "fable"])
def test_counsel_base_behavior_and_round_extension_seats(profile, capsys):
    assert recommend.gate_check(
        _healthy_s_plus_providers(),
        profile,
        today=TODAY,
        now=NOW,
        role="counsel",
        purpose="architect",
        operator_request="hk:doc/ordinary/request" if profile == "fable" else None,
    ).ok
    launch.resolve_launch(profile, role="counsel", purpose="architect", operator_request=profile == "fable")
    for role in ("tester", "reviewer"):
        assert (
            cli.main(
                [
                    "policy",
                    "launch",
                    profile,
                    "--role",
                    role,
                    "--purpose",
                    "round-extension",
                    "--operator-request",
                    REFS[0],
                    "--json",
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert recommend.gate_check(
            _healthy_s_plus_providers(),
            profile,
            today=TODAY,
            now=NOW,
            role=role,
            purpose="round-extension",
            operator_request=REFS[0],
        ).ok


@pytest.mark.parametrize("profile", ["codex-astra", "fable"])
def test_cli_tester_architect_without_round_extension_is_role_denied(profile, monkeypatch):
    monkeypatch.setattr(cli, "registry", lambda: {"codex": lambda: _result("codex", 10)})
    assert (
        cli.main(
            [
                "gate",
                "-m",
                profile,
                "--role",
                "tester",
                "--purpose",
                "architect",
                "--operator-request",
                REFS[0],
                "--no-cache",
            ]
        )
        == 5
    )
    assert (
        cli.main(
            [
                "policy",
                "launch",
                profile,
                "--role",
                "tester",
                "--purpose",
                "architect",
                "--operator-request",
                REFS[0],
                "--json",
            ]
        )
        == 3
    )


@pytest.mark.parametrize("ref", REFS)
@pytest.mark.parametrize("profile", ["codex-astra", "gpt-6-astra"])
def test_astra_round_extension_reaches_quota(profile, ref):
    for used, ok in [(10, True), (99, False)]:
        result = recommend.gate_check(
            [_result("codex", used, pool_class="preserve")],
            profile,
            today=TODAY,
            now=NOW,
            purpose="round-extension",
            operator_request=ref,
        )
        assert result.role_denied is False
        assert result.ok is ok
        assert result.operator_request_ref == ref


@pytest.mark.parametrize(
    "ref",
    [
        None,
        "hk:task/5",
        "hk:doc/other/b1632-sol",
        "hk:doc/ParkingArcade/round-ext/b1632-sol",
        "hk:doc/round-ext/b1632-sol/extra",
        "hk:doc/round-ext/b1632-sol\n",
    ],
)
def test_astra_missing_or_wrong_reference_role_denied(ref):
    result = recommend.gate_check(
        _healthy_s_plus_providers(),
        "codex-astra",
        today=TODAY,
        now=NOW,
        purpose="round-extension",
        operator_request=ref,
    )
    assert result.role_denied is True
    with pytest.raises(launch.LaunchError, match="role_restricted"):
        launch.resolve_launch("codex-astra", purpose="round-extension", operator_request=ref or False)


@pytest.mark.parametrize("profile", ["builder-astra", "captain-astra"])
@pytest.mark.parametrize(
    "purpose", [None, "director", "architect", "operator-request", "round-extension", "worker"]
)
def test_astra_builder_seats_never_admitted(profile, purpose):
    result = recommend.gate_check(
        _healthy_s_plus_providers(), profile, today=TODAY, now=NOW, purpose=purpose, operator_request=REFS[0]
    )
    assert result.role_denied is True
    with pytest.raises(launch.LaunchError, match="role_restricted"):
        launch.resolve_launch(profile, purpose=purpose, operator_request=REFS[0])


@pytest.mark.parametrize("ref", REFS)
@pytest.mark.parametrize("profile", ["codex-astra", "fable"])
def test_policy_launch_retains_reference(profile, ref, capsys):
    assert (
        cli.main(
            ["policy", "launch", profile, "--purpose", "round-extension", "--operator-request", ref, "--json"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["gate"] == "consult_only"


def test_boolean_operator_request_cannot_unlock_astra_round_extension():
    with pytest.raises(launch.LaunchError, match="role_restricted"):
        launch.resolve_launch("codex-astra", purpose="round-extension", operator_request=True)


def test_fable_purpose_alone_never_unlocks():
    result = recommend.gate_check(
        _healthy_s_plus_providers(), "fable", today=TODAY, now=NOW, purpose="round-extension"
    )
    assert result.ok is False
    assert "consult_only" in result.reason
    with pytest.raises(launch.LaunchError, match="consult_only"):
        launch.resolve_launch("fable", purpose="round-extension")
    assert (
        recommend.gate_check(
            _healthy_s_plus_providers(),
            "fable",
            today=TODAY,
            now=NOW,
            purpose="round-extension",
            operator_request=REFS[0],
        ).ok
        is True
    )


def test_shared_permission_definition():
    assert launch.astra_purpose_allowed is recommend.astra_purpose_allowed
    assert launch.round_ext_role_allowed is recommend.round_ext_role_allowed
    assert {"director", "architect", "operator-request"} == recommend.ASTRA_ALLOWED_PURPOSES


def test_gate_audit_keeps_purpose_and_ref(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "registry", lambda: {"codex": lambda: _result("codex", 10, pool_class="preserve")}
    )
    path = tmp_path / "gate.json"
    assert (
        cli.main(
            [
                "gate",
                "-m",
                "codex-astra",
                "--purpose",
                "round-extension",
                "--operator-request",
                REFS[0],
                "--gate-output",
                str(path),
                "--no-cache",
            ]
        )
        == 0
    )
    record = json.loads(path.read_text())
    assert record["purpose"] == "round-extension"
    assert record["operator_request_ref"] == REFS[0]


@pytest.mark.parametrize("profile", ["codex-astra", "fable"])
@pytest.mark.parametrize("ref", REFS)
def test_builder_requires_exact_purpose_and_reference(profile, ref, capsys):
    result = recommend.gate_check(
        _healthy_s_plus_providers(),
        profile,
        today=TODAY,
        now=NOW,
        role="builder",
        purpose="round-extension",
        operator_request=ref,
    )
    assert result.ok and not result.role_denied
    assert (
        cli.main(
            [
                "policy",
                "launch",
                profile,
                "--role",
                "builder",
                "--purpose",
                "round-extension",
                "--operator-request",
                ref,
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload.get("round_extension_builder") == {
        "policy": "hk1672/v1",
        "profile": profile,
        "role": "builder",
        "purpose": "round-extension",
        "ref": ref,
    }
    assert payload["model_id"] == {"codex-astra": "gpt-6-astra", "fable": "claude-fable-5-1"}[profile]
    assert payload["effort"] == ("xhigh" if profile == "codex-astra" else "")


@pytest.mark.parametrize("profile", ["codex-astra", "fable"])
@pytest.mark.parametrize(
    "purpose",
    [None, "builder", "director", "architect", "operator-request", "round-extension", " Round-Extension "],
)
@pytest.mark.parametrize("ref", [None, True, "hk:doc/ordinary/request", REFS[0]])
def test_builder_counsel_purposes_never_unlock(profile, purpose, ref):
    if purpose == "round-extension" and ref == REFS[0]:
        return
    result = recommend.gate_check(
        _healthy_s_plus_providers(),
        profile,
        today=TODAY,
        now=NOW,
        role="builder",
        purpose=purpose,
        operator_request=ref,
    )
    assert result.role_denied
    with pytest.raises(launch.LaunchError, match="role_restricted"):
        launch.resolve_launch(profile, role="builder", purpose=purpose, operator_request=ref or False)


@pytest.mark.parametrize("profile", ["codex-astra", "fable"])
@pytest.mark.parametrize("role", ["worker", "captain"])
@pytest.mark.parametrize("purpose", [None, "director", "architect", "operator-request", "round-extension"])
@pytest.mark.parametrize("ref", [None, "hk:doc/ordinary/request", REFS[0]])
def test_worker_and_captain_roles_always_denied(profile, role, purpose, ref):
    result = recommend.gate_check(
        _healthy_s_plus_providers(),
        profile,
        today=TODAY,
        now=NOW,
        role=role,
        purpose=purpose,
        operator_request=ref,
    )
    assert result.role_denied
    with pytest.raises(launch.LaunchError, match="role_restricted"):
        launch.resolve_launch(profile, role=role, purpose=purpose, operator_request=ref or False)


@pytest.mark.parametrize("profile", ["gpt-6-astra", "claude-fable-5-1"])
@pytest.mark.parametrize("ref", REFS)
def test_builder_model_id_spellings_never_unlock(profile, ref):
    result = recommend.gate_check(
        _healthy_s_plus_providers(),
        profile,
        today=TODAY,
        now=NOW,
        role="builder",
        purpose="round-extension",
        operator_request=ref,
    )
    assert result.role_denied
    with pytest.raises(launch.LaunchError, match="role_restricted"):
        launch.resolve_launch(profile, role="builder", purpose="round-extension", operator_request=ref)


def test_ordinary_defaults_and_recommendations_unchanged_except_fusion_sol_rows():
    baseline = json.loads((Path(__file__).parent / "fixtures/round_extension_ordinary.json").read_text())
    view = CatalogView(entries=launch.snapshot_entries(), source=CATALOG_SOURCE_SNAPSHOT)
    for profile, expected in baseline["launch"].items():
        actual = json.dumps(
            asdict(
                launch.resolve_launch(
                    profile.split("@")[0], effort=profile.split("@")[1] if "@" in profile else None, view=view
                )
            ),
            sort_keys=True,
        )
        assert actual.encode() == json.dumps(expected, sort_keys=True).encode(), profile
        decision = launch.resolve_launch(
            profile.split("@")[0],
            effort=profile.split("@")[1] if "@" in profile else None,
            view=view,
        )
        assert (
            json.dumps(decision.as_dict(), sort_keys=True).encode()
            == json.dumps(
                baseline["launch_wire"][profile],
                sort_keys=True,
            ).encode()
        ), profile
        assert decision.render().encode() == baseline["launch_text"][profile].encode(), profile
    for grade, expected in baseline["recommend"].items():
        actual = recommend.recommend(
            _healthy_s_plus_providers(), grade, today=TODAY, now=NOW, bench_scores=[]
        )
        # hk 1729 adds two unmeasured B rows. Their placement is asserted in
        # test_task1729_fusion_sol; removing just those rows preserves this
        # baseline check for every prior recommendation line.
        actual = "\n".join(
            line
            for line in actual.splitlines()
            if "devin-fusion-sol61" not in line
        )
        assert actual.encode() == expected.encode(), grade
        assert not any(name in actual for name in ("codex-astra", "fable", "builder-astra", "builder-fable"))


def test_ordinary_purpose_only_refusal_keeps_consult_diagnostics():
    for profile in ("codex-astra", "fable"):
        for purpose in ("builder", "worker", "captain"):
            result = recommend.gate_check(
                _healthy_s_plus_providers(),
                profile,
                today=TODAY,
                now=NOW,
                purpose=purpose,
            )
            assert not result.ok
            assert result.role_denied if profile == "codex-astra" else "consult_only" in result.reason
            with pytest.raises(launch.LaunchError, match="consult_only"):
                launch.resolve_launch(profile, purpose=purpose)
