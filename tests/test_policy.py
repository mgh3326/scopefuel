"""ROB-1181-A — pool-level policy config TOML + expiry."""

from __future__ import annotations

import datetime as dt

import pytest

from scopefuel import cli, policy
from scopefuel.providers import BUILTIN

TODAY = dt.date(2026, 7, 31)


@pytest.fixture
def policy_config(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_dir))
    return cfg_dir / "scopefuel" / "config.toml"


def test_config_path_uses_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert policy.config_path() == tmp_path / "cfg" / "scopefuel" / "config.toml"


def test_missing_config_means_builtin_behavior():
    effective, status = policy.get_policy("claude", "preserve", today=TODAY)
    assert effective == "preserve"
    assert status is None


def test_set_and_get_override(policy_config):
    policy.set_policy("claude", "preserve", until=dt.date(2026, 8, 3), note="테스트")
    effective, status = policy.get_policy("claude", "preserve", today=TODAY)
    assert effective == "preserve"
    assert "2026-08-03" in status
    assert "테스트" in status


def test_set_without_until_is_rejected(policy_config):
    with pytest.raises(ValueError, match="until"):
        policy.set_policy("claude", "spend")


def test_expired_override_is_ignored_and_surfaces_status(policy_config):
    policy.set_policy("claude", "spend", until=dt.date(2026, 7, 30), note="old")
    effective, status = policy.get_policy("claude", "preserve", today=TODAY)
    assert effective == "preserve"
    assert "expired" in status


def test_clear_removes_override(policy_config):
    policy.set_policy("claude", "spend", until=dt.date(2026, 8, 3))
    assert policy.clear_policy("claude") is True
    effective, _ = policy.get_policy("claude", "preserve", today=TODAY)
    assert effective == "preserve"


def test_clear_missing_returns_false(policy_config):
    assert policy.clear_policy("claude") is False


def test_list_shows_builtin_and_overrides(policy_config, capsys, monkeypatch):
    policy.set_policy("claude", "preserve", until=dt.date(2026, 8, 3), note="테스트")
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    assert cli.main(["policy", "list"]) == 0
    out = capsys.readouterr().out
    assert "claude" in out and "preserve" in out
    assert "2026-08-03" in out or "expires 2026-08-03" in out
    assert "테스트" in out


def test_set_command_rejects_missing_until(capsys, monkeypatch):
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    with pytest.raises(SystemExit) as exc:
        cli.main(["policy", "set", "claude", "preserve"])
    assert exc.value.code == 2
    assert "--until" in capsys.readouterr().err


def test_set_and_clear_command_round_trip(policy_config, capsys, monkeypatch):
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    assert cli.main(["policy", "set", "claude", "preserve", "--until", "2026-08-03", "--note", "테스트"]) == 0
    assert policy.get_policy("claude", "preserve", today=TODAY)[0] == "preserve"
    assert cli.main(["policy", "clear", "claude"]) == 0
    assert policy.get_policy("claude", "preserve", today=TODAY)[0] == "preserve"


def test_invalid_class_in_config_is_ignored(policy_config):
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    policy_config.write_text('[pools.claude]\nclass = "gold"\nuntil = "2026-08-03"\n')
    effective, status = policy.get_policy("claude", "preserve", today=TODAY)
    assert effective == "preserve"
    assert "invalid class" in status


def test_missing_until_in_config_is_ignored(policy_config):
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    policy_config.write_text('[pools.claude]\nclass = "spend"\n')
    effective, status = policy.get_policy("claude", "preserve", today=TODAY)
    assert effective == "preserve"
    assert "missing until" in status


def test_write_config_missing_class_no_reuse_or_corruption(policy_config):
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    policy_config.write_text(
        '[pools.claude]\nclass = "preserve"\nuntil = "2026-08-03"\n'
        '[pools.orphan]\nuntil = "2026-08-03"\nnote = "no class"\n'
    )
    config = policy.load_config()
    # Must not raise and must keep both entries intact.
    assert "claude" in config["pools"]
    assert "orphan" in config["pools"]
    assert config["pools"]["orphan"].get("class") is None


def test_clear_orphan_configured_pool(policy_config, capsys, monkeypatch):
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    policy_config.write_text('[pools.orphan]\nclass = "spend"\nuntil = "2026-08-03"\n')
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    assert cli.main(["policy", "clear", "orphan"]) == 0
    out = capsys.readouterr().out
    assert "orphan policy cleared" in out
    assert "orphan" not in policy.load_config().get("pools", {})


def test_clear_unknown_name_not_in_config_errors(policy_config, capsys, monkeypatch):
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    rc = cli.main(["policy", "clear", "not-a-pool"])
    assert rc == 2
    assert "not-a-pool" in capsys.readouterr().err
