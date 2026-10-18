"""hk 1632: a round-extension reference is the key, never a purpose string."""

import json

import pytest
from test_gate import NOW, TODAY, _healthy_s_plus_providers, _result

from scopefuel import cli, launch, recommend

REFS = [
    "hk:doc/round-extension/b1632-sol",
    "hk:doc/round-ext/b1632-sol",
    "hk:doc/parkingarcade/round-ext/b1632-sol",
]


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
