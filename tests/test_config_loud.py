"""task #1024 — a broken config.toml must be loud, not silently empty.

``load_config`` used to return ``{}`` on a TOML decode error (and on any
OSError) with no sign of it: one bad edit — e.g. a second ``[bench]`` header
added next to the #712 B plaintext opt-ins — dropped every setting on the
host and quietly demoted it to local mode. These tests pin the disclosure:
one stderr warning per process naming the file and the parse error, a
``blocked:`` line on ``bench catalog status`` with ``--check`` failing on it,
and the per-call ``bench push-catalog --allow-plaintext-http`` opt-in.
"""

from __future__ import annotations

import json
import os

import pytest
from test_bench_backend import FakeHandoffkeep, _set_backend
from test_bench_catalog import _row, _seed_rows

from scopefuel import bench, cli, policy

DUP_TABLE = '[bench]\ncache_ttl_s = 21600\n[bench]\nbackend = "local"\n'


def _config_file(tmp_path):
    path = tmp_path / "config" / "scopefuel" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _warnings(captured_err: str) -> list[str]:
    return [line for line in captured_err.splitlines() if line.startswith("scopefuel: warning:")]


def test_duplicate_table_warns_once_and_stdout_is_unchanged(tmp_path, capsys):
    """AC1 — and the M1/M2 invariants.

    M1 "a config that cannot be parsed is never silent" (mutant: drop the
    warning) and M2 "the warning is printed once per process" (mutant: warn on
    every load) both turn the count assertion RED.
    """

    config = _config_file(tmp_path)

    # Baseline: an empty config — same {} as a broken one yields.
    config.write_text("", encoding="utf-8")
    assert cli.main(["bench", "catalog", "list"]) == 0
    baseline = capsys.readouterr()
    assert _warnings(baseline.err) == []

    config.write_text(DUP_TABLE, encoding="utf-8")
    for _ in range(3):
        assert policy.load_config() == {}
    # One CLI command that loads config from several modules: ``catalog list``
    # resolves the backend in bench.py and subscription marks through
    # recommend.py -> policy.py. The whole run may add no further warning.
    assert cli.main(["bench", "catalog", "list"]) == 0
    after = capsys.readouterr()

    warnings = _warnings(after.err)
    assert len(warnings) == 1, (
        "a config that cannot be parsed is never silent, and the warning is printed once per process"
    )
    warning = warnings[0]
    assert str(config) in warning
    assert "could not be parsed" in warning
    assert "at line" in warning and "column" in warning
    assert "every setting in it is ignored until it is fixed" in warning
    assert after.out == baseline.out


@pytest.mark.parametrize(
    "bad",
    [
        '[bench]\nbackend = "handoffkeep\n',  # unterminated string
        "[bench]\ncache_ttl_s = \n",  # missing value
        "[bench]\nreset_at = 1979-05-27T32:00:00Z\n",  # invalid datetime
    ],
    ids=["unterminated-string", "missing-value", "invalid-datetime"],
)
def test_other_decode_errors_warn_the_same(tmp_path, capsys, bad):
    """AC2: every TOMLDecodeError takes the loud path, not just duplicates."""

    config = _config_file(tmp_path)
    config.write_text(bad, encoding="utf-8")

    assert policy.load_config() == {}
    warnings = _warnings(capsys.readouterr().err)
    assert len(warnings) == 1
    assert str(config) in warnings[0]
    assert "could not be parsed" in warnings[0]


def test_missing_config_stays_silent(tmp_path, capsys):
    """AC2: no config file is not a problem — most hosts run on defaults."""

    for _ in range(2):
        assert policy.load_config() == {}
    assert policy.config_problem() is None
    assert _warnings(capsys.readouterr().err) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through chmod 000")
def test_unreadable_config_warns(tmp_path, capsys):
    """AC2: a file that exists but cannot be read is loud too."""

    config = _config_file(tmp_path)
    config.write_text('[bench]\nbackend = "local"\n', encoding="utf-8")
    config.chmod(0)
    try:
        assert policy.load_config() == {}
        problem = policy.config_problem()
    finally:
        config.chmod(0o600)

    warnings = _warnings(capsys.readouterr().err)
    assert len(warnings) == 1
    assert str(config) in warnings[0]
    assert "could not be read" in warnings[0]
    assert problem is not None and "could not be read" in problem


def test_catalog_status_names_the_broken_config_and_check_fails(tmp_path, monkeypatch, capsys):
    """AC3 — and the M3 invariant "‐‐check never passes on a broken config"
    (mutant: ignore the parse state in --check → rc 0, this goes RED)."""

    _set_backend(tmp_path, monkeypatch)  # https creds + [bench] backend=handoffkeep
    fake = FakeHandoffkeep()
    fake.catalog = _seed_rows()
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    bench.reset_catalog_memo()
    config = tmp_path / "config" / "scopefuel" / "config.toml"

    # The equivalent valid config (same keys, no duplicate table) passes.
    assert cli.main(["bench", "catalog", "status", "--check"]) == 0
    capsys.readouterr()

    config.write_text(config.read_text(encoding="utf-8") + DUP_TABLE, encoding="utf-8")
    assert cli.main(["bench", "catalog", "status"]) == 0
    out = capsys.readouterr().out
    assert any(
        line.startswith("blocked:") and "could not be parsed" in line and "config.toml" in line
        for line in out.splitlines()
    )

    # --check exits 2 even though the memoized view is a passing server view.
    rc = cli.main(["bench", "catalog", "status", "--check"])
    captured = capsys.readouterr()
    assert rc == 2, "--check never passes on a broken config"
    assert "check failed:" in captured.err
    assert "could not be parsed" in captured.err


def test_push_catalog_allow_plaintext_http_is_per_call(tmp_path, monkeypatch, capsys):
    """AC4: the flag carries one push over a plaintext tunnel, persists nothing."""

    monkeypatch.setenv("HANDOFFKEEP_URL", "http://hk.invalid:8800")
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", "test-token")
    fake = FakeHandoffkeep()
    fake.catalog = []
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    config = _config_file(tmp_path)
    config.write_text('[bench]\nbackend = "handoffkeep"\ncache_ttl_s = 21600\n', encoding="utf-8")
    before = config.read_bytes()

    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps({"catalog": [_row("opus", "high", "claude-opus-5-5", "claude", "S+")]}),
        encoding="utf-8",
    )

    # No flag, no persistent opt-in: today's refusal, nothing sent.
    assert cli.main(["bench", "push-catalog", str(seed)]) == 2
    err = capsys.readouterr().err
    assert "allow_plaintext_catalog" in err
    assert fake.hits[("PUT", "catalog")] == 0

    assert cli.main(["bench", "push-catalog", str(seed), "--allow-plaintext-http"]) == 0
    assert "catalog rows written: 1" in capsys.readouterr().out
    assert fake.hits[("PUT", "catalog")] == 1

    # Per call and non-persistent: the file is byte-identical afterwards.
    assert config.read_bytes() == before


def test_push_catalog_plaintext_auto_mode_still_refuses_without_the_flag(tmp_path, monkeypatch, capsys):
    """AC4 second refusal shape: auto resolution demotes to local and the push
    says so — the flag is the only thing that changes the answer."""

    monkeypatch.setenv("HANDOFFKEEP_URL", "http://hk.invalid:8800")
    monkeypatch.setenv("HANDOFFKEEP_TOKEN", "test-token")
    fake = FakeHandoffkeep()
    fake.catalog = []
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    config = _config_file(tmp_path)
    config.write_text("[bench]\ncache_ttl_s = 21600\n", encoding="utf-8")

    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps({"catalog": [_row("opus", "high", "claude-opus-5-5", "claude", "S+")]}),
        encoding="utf-8",
    )
    assert cli.main(["bench", "push-catalog", str(seed)]) == 2
    assert "requires the handoffkeep backend" in capsys.readouterr().err
    assert fake.hits[("PUT", "catalog")] == 0
