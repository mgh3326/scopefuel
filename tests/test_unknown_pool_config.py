"""Retired pool sections must not register a provider or stop the CLI."""

from scopefuel import cli, policy
from scopefuel.providers import BUILTIN


def test_unknown_pool_is_ignored_with_warning(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"
    config.write_text("[pools.retired_provider]\nclass = 'spend'\nsubscribed = true\n")
    monkeypatch.setattr(policy, "config_path", lambda: config)

    assert cli.main(["--list-providers"]) == 0
    captured = capsys.readouterr()
    assert "unknown pool section ignored" in captured.err
    assert "retired_provider" not in captured.out
    for name in BUILTIN:
        assert name in captured.out
