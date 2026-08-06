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
    policy.set_policy("claude", "preserve", until=dt.date(2026, 8, 6), note="테스트")
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    assert cli.main(["policy", "list"]) == 0
    out = capsys.readouterr().out
    assert "claude" in out and "preserve" in out
    assert "2026-08-06" in out or "expires 2026-08-06" in out
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


def test_set_exclude_class_round_trip(policy_config, capsys, monkeypatch):
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    assert (
        cli.main(["policy", "set", "claude", "exclude", "--until", "2026-08-31", "--note", "Pro 요금제"]) == 0
    )
    assert policy.get_policy("claude", "preserve", today=TODAY)[0] == "exclude"
    out = capsys.readouterr().out
    assert "claude -> exclude" in out
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


# ------------------------------------------------------------------ ROB-1184: numeric boost


def test_boost_requires_until(policy_config):
    with pytest.raises(ValueError, match="until"):
        policy.set_policy("codex", None, boost=1)


def test_boost_set_and_get(policy_config):
    policy.set_policy("codex", "spend", until=dt.date(2026, 8, 5), boost=1)
    boost, status = policy.get_boost("codex", today=TODAY)
    assert boost == 1
    assert status is None


def test_boost_set_without_class_only_touches_boost(policy_config):
    policy.set_policy("codex", "spend", until=dt.date(2026, 8, 5), note="리셋권")
    policy.set_policy("codex", None, until=dt.date(2026, 8, 5), boost=1)
    effective, _ = policy.get_policy("codex", "preserve", today=TODAY)
    assert effective == "spend"
    boost, _ = policy.get_boost("codex", today=TODAY)
    assert boost == 1


def test_boost_only_new_pool_keeps_builtin_class_and_clean_policy_list(policy_config, capsys, monkeypatch):
    """ROB-1212: omitting class must not create an ``invalid class None`` row."""
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    assert cli.main(["policy", "set", "grok", "--boost", "2", "--until", "2026-08-10"]) == 0
    builtin_class = getattr(BUILTIN["grok"], "pool_class", "preserve")
    assert policy.get_policy("grok", builtin_class, today=TODAY) == (builtin_class, None)
    capsys.readouterr()

    assert cli.main(["policy", "list"]) == 0
    out = capsys.readouterr().out
    grok_line = next(line for line in out.splitlines() if line.startswith("grok"))
    assert f"{builtin_class}" in grok_line
    assert "[invalid class None]" not in grok_line
    assert "boost=2" in grok_line


def test_boost_none_clears_boost_without_touching_class(policy_config):
    """`policy set codex --boost none` — class/note 는 그대로, boost 만 해제."""
    policy.set_policy("codex", "spend", until=dt.date(2026, 8, 5), note="리셋권", boost=1)
    policy.set_policy("codex", None, boost=None)
    effective, status = policy.get_policy("codex", "preserve", today=TODAY)
    assert effective == "spend"
    assert status is not None and "리셋권" in status
    boost, _ = policy.get_boost("codex", today=TODAY)
    assert boost is None


def test_boost_bool_true_false_are_rejected_not_coerced_to_int(policy_config):
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    policy_config.write_text('[pools.codex]\nboost = true\nuntil = "2026-08-05"\n')
    boost, status = policy.get_boost("codex", today=TODAY)
    assert boost is None
    assert status is not None and "bool" in status


def test_boost_missing_until_falls_back(policy_config):
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    policy_config.write_text("[pools.codex]\nboost = 1\n")
    boost, status = policy.get_boost("codex", today=TODAY)
    assert boost is None
    assert "missing until" in status


def test_boost_expired_falls_back(policy_config):
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    policy_config.write_text('[pools.codex]\nboost = 1\nuntil = "2026-07-30"\n')
    boost, status = policy.get_boost("codex", today=TODAY)
    assert boost is None
    assert "expired" in status


def test_boost_cli_set_and_none_round_trip(policy_config, capsys, monkeypatch):
    from scopefuel import cli
    from scopefuel.providers import BUILTIN

    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    assert (
        cli.main(
            ["policy", "set", "codex", "spend", "--until", "2026-08-05", "--boost", "1", "--note", "리셋권"]
        )
        == 0
    )
    boost, _ = policy.get_boost("codex", today=TODAY)
    assert boost == 1
    capsys.readouterr()

    assert cli.main(["policy", "set", "codex", "--boost", "none"]) == 0
    boost, _ = policy.get_boost("codex", today=TODAY)
    assert boost is None
    effective, _ = policy.get_policy("codex", "preserve", today=TODAY)
    assert effective == "spend"  # class 는 그대로


def test_boost_cli_numeric_without_until_errors(policy_config, capsys, monkeypatch):
    from scopefuel import cli
    from scopefuel.providers import BUILTIN

    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    with pytest.raises(SystemExit) as exc:
        cli.main(["policy", "set", "codex", "--boost", "1"])
    assert exc.value.code == 2
    assert "--until" in capsys.readouterr().err


def test_boost_cli_rejects_true_false_literal(policy_config, capsys, monkeypatch):
    from scopefuel import cli
    from scopefuel.providers import BUILTIN

    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    with pytest.raises(SystemExit):
        cli.main(["policy", "set", "codex", "--until", "2026-08-05", "--boost", "true"])


# ------------------------------------------------------------------ ROB-1184: [settings] reset_urgency_hours


def test_reset_urgency_hours_default_when_unset(policy_config):
    assert policy.get_reset_urgency_hours() == 12.0


def test_reset_urgency_hours_reads_settings(policy_config):
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    policy_config.write_text("[settings]\nreset_urgency_hours = 6\n")
    assert policy.get_reset_urgency_hours() == 6.0


def test_reset_urgency_hours_invalid_falls_back_to_default(policy_config):
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    policy_config.write_text('[settings]\nreset_urgency_hours = "nope"\n')
    assert policy.get_reset_urgency_hours() == 12.0


def test_reset_urgency_hours_zero_or_negative_falls_back(policy_config):
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    policy_config.write_text("[settings]\nreset_urgency_hours = -1\n")
    assert policy.get_reset_urgency_hours() == 12.0


# ------------------------------------------------------------------ ROB-1184: capacity_weight


def _use_capacity_fixture(policy_config):
    import pathlib
    import shutil

    fixture = pathlib.Path(__file__).parent / "fixtures" / "pool_capacity_weight.toml"
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture, policy_config)


def test_capacity_weight_explicit_value_wins_over_price_usd(policy_config):
    """[pools.claude] 는 price_usd=200 만 있음 → weight = 200/20 = 10.0."""
    _use_capacity_fixture(policy_config)
    weight, status = policy.get_capacity_weight("claude")
    assert weight == 10.0
    assert status is None


def test_capacity_weight_explicit_capacity_weight_field(policy_config):
    """[pools.codex] 는 capacity_weight=3.5 명시 → price_usd 무관하게 그 값 사용."""
    _use_capacity_fixture(policy_config)
    weight, status = policy.get_capacity_weight("codex")
    assert weight == 3.5
    assert status is None


def test_capacity_weight_zero_falls_back_to_default_with_status(policy_config):
    """[pools.kiro] capacity_weight=0 → 무효 → 1.0 폴백 + status 노출."""
    _use_capacity_fixture(policy_config)
    weight, status = policy.get_capacity_weight("kiro")
    assert weight == 1.0
    assert status is not None and "invalid capacity_weight" in status


def test_capacity_weight_negative_price_usd_falls_back(policy_config):
    """[pools.grok] price_usd=-10 → 무효 → 1.0 폴백 + status 노출."""
    _use_capacity_fixture(policy_config)
    weight, status = policy.get_capacity_weight("grok")
    assert weight == 1.0
    assert status is not None and "invalid price_usd" in status


def test_capacity_weight_non_numeric_falls_back(policy_config):
    """[pools.agy] capacity_weight="not-a-number" → 무효 → 1.0 폴백 + status 노출."""
    _use_capacity_fixture(policy_config)
    weight, status = policy.get_capacity_weight("agy")
    assert weight == 1.0
    assert status is not None and "invalid capacity_weight" in status


def test_capacity_weight_defaults_to_1_when_no_pool_entry(policy_config):
    _use_capacity_fixture(policy_config)
    weight, status = policy.get_capacity_weight("unconfigured-pool")
    assert weight == 1.0
    assert status is None


def test_capacity_weight_defaults_to_1_when_pool_has_no_price_fields(policy_config):
    """[pools.clinepass] 는 plan 만 있고 price_usd/capacity_weight 없음 → 1.0."""
    _use_capacity_fixture(policy_config)
    weight, status = policy.get_capacity_weight("clinepass")
    assert weight == 1.0
    assert status is None


def test_get_pool_plan_reads_config(policy_config):
    _use_capacity_fixture(policy_config)
    assert policy.get_pool_plan("clinepass") == "team"
    assert policy.get_pool_plan("unconfigured-pool") is None


def test_capacity_weight_bool_is_rejected_like_boost(policy_config):
    """bool 은 int/float 의 하위형이지만 capacity_weight/price_usd 에도 숫자로 수용하지 않는다."""
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    policy_config.write_text("[pools.codex]\ncapacity_weight = true\n")
    weight, status = policy.get_capacity_weight("codex")
    assert weight == 1.0
    assert status is not None and "invalid capacity_weight" in status


# ------------------------------------------------------------------ ROB-1188: dt.UTC AttributeError


def test_get_boost_no_args_does_not_raise(policy_config):
    """dt.UTC 대신 dt.timezone.utc 를 써서 3.11 미만/일부 경로에서도 AttributeError 가 나지 않는다."""
    boost, status = policy.get_boost("codex")
    assert boost is None
    assert status is None


def test_operator_reproduction_exact_call_does_not_raise(policy_config):
    """운영자가 직접 재현한 정확한 호출 형태 — dt.datetime.now(dt.UTC) AttributeError 재현 대상."""
    from scopefuel.policy import get_boost

    result = get_boost("codex")  # noqa: F841 -- 예외가 안 나는 것 자체가 회귀 검증
    assert result == (None, None)


def test_get_capacity_weight_no_args_does_not_raise(policy_config):
    weight, status = policy.get_capacity_weight("codex")
    assert weight == 1.0
    assert status is None


def test_get_policy_no_args_does_not_raise(policy_config):
    effective, status = policy.get_policy("codex")
    assert effective == "preserve"
    assert status is None


# ------------------------------------------------------------------ ROB-1188: policy list boost/weight


def test_policy_list_shows_builtin_tag_when_unconfigured(policy_config, capsys, monkeypatch):
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    assert cli.main(["policy", "list"]) == 0
    out = capsys.readouterr().out
    for pool in BUILTIN:
        line = next(line_ for line_ in out.splitlines() if line_.startswith(pool))
        assert "[기본]" in line
        assert "spend" in line  # ROB-1188: 6개 풀 기본값은 spend


def test_policy_list_shows_configured_tag_and_boost_and_weight(policy_config, capsys, monkeypatch):
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    policy.set_policy("codex", "spend", until=dt.date(2026, 8, 10), boost=1, note="리셋권")
    assert cli.main(["policy", "list"]) == 0
    out = capsys.readouterr().out
    codex_line = next(line for line in out.splitlines() if line.startswith("codex"))
    assert "[설정]" in codex_line
    assert "boost=1" in codex_line
    claude_line = next(line for line in out.splitlines() if line.startswith("claude"))
    assert "[기본]" in claude_line
    assert "boost=-" in claude_line


def test_policy_list_shows_capacity_weight_when_configured(policy_config, capsys, monkeypatch):
    import pathlib
    import shutil

    fixture = pathlib.Path(__file__).parent / "fixtures" / "pool_capacity_weight.toml"
    policy_config.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture, policy_config)
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    assert cli.main(["policy", "list"]) == 0
    out = capsys.readouterr().out
    claude_line = next(line for line in out.splitlines() if line.startswith("claude"))
    assert "capacity_weight=10" in claude_line  # price_usd=200 -> 200/20=10.0
    assert "[설정]" in claude_line  # fixture 는 claude class=preserve 를 명시
    codex_line = next(line for line in out.splitlines() if line.startswith("codex"))
    assert "capacity_weight=3.5" in codex_line
    assert "[설정]" in codex_line  # class 가 없어도 capacity_weight 명시 설정이면 [설정]
