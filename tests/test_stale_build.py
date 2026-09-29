"""task #952 — installed-build staleness warning against origin/main.

All network and install-discovery seams are faked: no test reaches the real
GitHub API, git ls-remote, uv receipt, or dist-info. The conftest autouse
fixture already sets ``SCOPEFUEL_STALE_WARN=0``; tests opt in via ``stale_on``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import types

import pytest

from scopefuel import bench, cli, policy, stale_build
from scopefuel.model import Bucket, ProviderResult

INSTALLED = "58b4b33" + "0" * 33  # the M1 incident rev, padded to 40 hex
HEAD = "7f867fc" + "0" * 33
NEWER = "a" * 40

REINSTALL = stale_build.REINSTALL_COMMAND


def _vcs_direct_url(rev: str) -> str:
    return json.dumps(
        {
            "url": "git+https://github.com/mgh3326/scopefuel",
            "vcs_info": {"vcs": "git", "requested_revision": "main", "commit_id": rev},
        }
    )


def _dir_direct_url(path: str) -> str:
    return json.dumps({"url": f"file://{path}", "dir_info": {"editable": True}})


def _receipt_toml(rev: str) -> str:
    # The real uv-receipt.toml keeps requirements as an inline table list.
    return (
        "[tool]\n"
        'requirements = [{ name = "scopefuel", '
        f'git = "https://github.com/mgh3326/scopefuel?rev={rev}" }}]\n'
    )


def _completed(argv, rc: int, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, rc, out, "")


@pytest.fixture
def stale_on(monkeypatch, tmp_path):
    """Enable the check with every seam cut: no dist-info, no receipt, net offline.

    ``calls`` records every probe attempt so cache/budget tests can assert on
    how often — and with what timeout — the network would have been touched.
    """
    monkeypatch.setenv("SCOPEFUEL_STALE_WARN", "1")
    monkeypatch.setattr(stale_build, "_dist_direct_url", lambda: None)
    monkeypatch.setattr(stale_build, "_receipt_path", lambda: tmp_path / "absent-receipt.toml")
    calls: dict[str, list] = {"json": [], "run": []}

    def offline_get_json(url: str, timeout: float):
        calls["json"].append((url, timeout))
        raise RuntimeError("offline")

    def offline_run(argv: list[str], timeout: float):
        calls["run"].append((argv, timeout))
        return _completed(argv, 1)

    monkeypatch.setattr(stale_build, "_get_json", offline_get_json)
    monkeypatch.setattr(stale_build, "_run", offline_run)
    return calls


def _set_installed(monkeypatch, rev: str | None) -> None:
    monkeypatch.setattr(stale_build, "_dist_direct_url", lambda: _vcs_direct_url(rev) if rev else None)


def _net(monkeypatch, calls, *, head=HEAD, ahead_by=3):
    def fake_get_json(url: str, timeout: float):
        calls["json"].append((url, timeout))
        if url.endswith("/commits/main"):
            if head is None:
                raise RuntimeError("no head")
            return {"sha": head}
        if "/compare/" in url:
            if ahead_by is None:
                raise RuntimeError("compare failed")
            return {"ahead_by": ahead_by}
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(stale_build, "_get_json", fake_get_json)


# --------------------------------------------------------------------------- verdict lines


def test_behind_line_names_revs_count_and_reinstall():
    line = stale_build.Verdict("behind", INSTALLED, HEAD, 7).line
    assert line.startswith("warning:")
    assert INSTALLED[:7] in line and HEAD[:7] in line
    assert "7 commit(s)" in line
    assert REINSTALL in line


def test_rev_unknown_line_names_origin_and_reinstall():
    line = stale_build.Verdict("rev_unknown", None, HEAD, None).line
    assert "installed rev unknown" in line
    assert HEAD[:7] in line and REINSTALL in line


def test_differ_with_unknown_distance_still_names_both_revs():
    verdict = stale_build.Verdict("behind", INSTALLED, HEAD, None)
    assert INSTALLED[:7] in verdict.line and HEAD[:7] in verdict.line
    assert "differs" in verdict.line and REINSTALL in verdict.line


def test_as_field_shape():
    field = stale_build.Verdict("behind", INSTALLED, HEAD, 7).as_field()
    assert field["status"] == "behind" and field["behind"] == 7
    assert field["installed_rev"] == INSTALLED and field["origin_rev"] == HEAD
    assert field["reinstall"] == REINSTALL and field["message"].startswith("warning:")


# --------------------------------------------------------------------------- installed rev discovery


def test_installed_rev_from_direct_url_vcs(stale_on, monkeypatch):
    _set_installed(monkeypatch, INSTALLED)
    assert stale_build._installed_rev() == INSTALLED


def test_installed_rev_from_uv_receipt_when_no_direct_url(stale_on, monkeypatch, tmp_path):
    receipt = tmp_path / "uv-receipt.toml"
    receipt.write_text(_receipt_toml(INSTALLED))
    monkeypatch.setattr(stale_build, "_receipt_path", lambda: receipt)
    assert stale_build._installed_rev() == INSTALLED


def test_installed_rev_none_when_nothing_records_it(stale_on):
    assert stale_build._installed_rev() is None


def test_editable_direct_url_uses_checkout_head_not_receipt(stale_on, monkeypatch, tmp_path):
    """An editable install's rev is its own checkout HEAD — never the tool receipt's."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    receipt = tmp_path / "uv-receipt.toml"
    receipt.write_text(_receipt_toml(NEWER))  # a different install's rev — must not leak
    monkeypatch.setattr(stale_build, "_receipt_path", lambda: receipt)
    monkeypatch.setattr(stale_build, "_dist_direct_url", lambda: _dir_direct_url(str(checkout)))

    def fake_run(argv: list[str], timeout: float):
        if "rev-parse" in argv:
            return _completed(argv, 0, INSTALLED + "\n")
        return _completed(argv, 1)

    monkeypatch.setattr(stale_build, "_run", fake_run)
    assert stale_build._installed_rev() == INSTALLED


def test_receipt_without_rev_query_param_yields_none(stale_on, monkeypatch, tmp_path):
    receipt = tmp_path / "uv-receipt.toml"
    receipt.write_text(
        '[tool]\nrequirements = [{ name = "scopefuel", git = "https://github.com/mgh3326/scopefuel" }]\n'
    )
    monkeypatch.setattr(stale_build, "_receipt_path", lambda: receipt)
    assert stale_build._installed_rev() is None


# --------------------------------------------------------------------------- check paths


def test_behind_by_n_commits_warns(stale_on, monkeypatch):
    _set_installed(monkeypatch, INSTALLED)
    _net(monkeypatch, stale_on, ahead_by=7)
    verdict = stale_build.warning()
    assert verdict is not None and verdict.status == "behind" and verdict.behind == 7
    assert verdict.installed_rev == INSTALLED and verdict.origin_rev == HEAD


def test_up_to_date_is_silent(stale_on, monkeypatch):
    _set_installed(monkeypatch, HEAD)
    _net(monkeypatch, stale_on)
    assert stale_build.warning() is None


def test_installed_ahead_of_main_is_silent(stale_on, monkeypatch):
    """A build strictly ahead of main (ahead_by=0, revs differ) is not behind."""
    _set_installed(monkeypatch, NEWER)
    _net(monkeypatch, stale_on, ahead_by=0)
    assert stale_build.warning() is None


def test_unknown_installed_rev_warns_rev_unknown(stale_on, monkeypatch):
    _net(monkeypatch, stale_on)
    verdict = stale_build.warning()
    assert verdict is not None and verdict.status == "rev_unknown"
    assert verdict.installed_rev is None and verdict.origin_rev == HEAD


def test_differ_without_compare_still_warns(stale_on, monkeypatch):
    """Compare unreachable (e.g. rev GC'd) → still warn, distance honestly unknown."""
    _set_installed(monkeypatch, INSTALLED)
    _net(monkeypatch, stale_on, ahead_by=None)
    verdict = stale_build.warning()
    assert verdict is not None and verdict.status == "behind" and verdict.behind is None


def test_offline_probe_silently_skips(stale_on):
    assert stale_build.warning() is None
    assert stale_on["json"]  # it did try, then gave up quietly


def test_ls_remote_fallback_when_api_fails(stale_on, monkeypatch):
    _set_installed(monkeypatch, INSTALLED)

    def only_compare(url: str, timeout: float):
        if "/compare/" in url:
            return {"ahead_by": 2}
        raise RuntimeError("api down")

    monkeypatch.setattr(stale_build, "_get_json", only_compare)
    monkeypatch.setattr(
        stale_build, "_run", lambda argv, timeout: _completed(argv, 0, f"{HEAD}\trefs/heads/main\n")
    )
    verdict = stale_build.warning()
    assert verdict is not None and verdict.origin_rev == HEAD and verdict.behind == 2


# --------------------------------------------------------------------------- cache


def test_cache_hit_skips_probe_within_ttl(stale_on, monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr(stale_build, "_now", lambda: clock[0])
    _set_installed(monkeypatch, INSTALLED)
    _net(monkeypatch, stale_on, ahead_by=4)
    first = stale_build.warning()
    assert first is not None and first.behind == 4
    calls_after_first = len(stale_on["json"])
    assert calls_after_first == 2  # head + compare

    second = stale_build.warning()
    assert len(stale_on["json"]) == calls_after_first  # no new probe
    assert second is not None and second.behind == 4  # verdict restored from cache


def test_cache_expiry_reprobes(stale_on, monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr(stale_build, "_now", lambda: clock[0])
    _set_installed(monkeypatch, INSTALLED)
    _net(monkeypatch, stale_on)
    stale_build.warning()
    calls_after_first = len(stale_on["json"])

    clock[0] += stale_build.CHECK_TTL_S + 1
    stale_build.warning()
    assert len(stale_on["json"]) > calls_after_first


def test_changed_installed_rev_reprobes_within_ttl(stale_on, monkeypatch):
    """A fresh reinstall must not inherit the old rev's cached warning."""
    clock = [1_000.0]
    monkeypatch.setattr(stale_build, "_now", lambda: clock[0])
    _set_installed(monkeypatch, INSTALLED)
    _net(monkeypatch, stale_on)
    assert stale_build.warning() is not None

    _set_installed(monkeypatch, HEAD)  # operator reinstalled to main
    assert stale_build.warning() is None


def test_cached_skip_stays_silent_within_ttl(stale_on, monkeypatch):
    """An offline probe caches 'skipped' — it does not retry on every gate call."""
    clock = [1_000.0]
    monkeypatch.setattr(stale_build, "_now", lambda: clock[0])
    assert stale_build.warning() is None
    calls_after_first = len(stale_on["json"])
    assert stale_build.warning() is None
    assert len(stale_on["json"]) == calls_after_first


# --------------------------------------------------------------------------- budget + never-raise


def test_probe_timeouts_stay_within_budget(stale_on, monkeypatch):
    _set_installed(monkeypatch, INSTALLED)
    _net(monkeypatch, stale_on)
    stale_build.warning()
    assert stale_on["json"]
    assert all(0 <= timeout <= stale_build.PROBE_BUDGET_S for _, timeout in stale_on["json"])


def test_slow_head_probe_starves_the_compare_call(stale_on, monkeypatch):
    """A probe that burns the whole budget leaves nothing for the compare call —
    the check degrades to 'differ, distance unknown' instead of overrunning."""
    ticks = iter([100.0, 100.0, 103.0, 103.0, 103.0, 103.0])
    monkeypatch.setattr(stale_build, "_monotonic", lambda: next(ticks))
    _set_installed(monkeypatch, INSTALLED)

    def slow_head(url: str, timeout: float):
        if url.endswith("/commits/main"):
            return {"sha": HEAD}
        raise AssertionError("compare should never run — deadline already spent")

    monkeypatch.setattr(stale_build, "_get_json", slow_head)
    verdict = stale_build.warning()
    assert verdict is not None and verdict.behind is None


def test_warning_never_raises(stale_on, monkeypatch):
    monkeypatch.setattr(stale_build, "_installed_rev", lambda: 1 / 0)
    assert stale_build.warning() is None
    monkeypatch.setattr(stale_build, "_check", lambda: 1 / 0)
    assert stale_build.warning() is None


def test_disabled_env_skips_without_probing(stale_on, monkeypatch):
    monkeypatch.setenv("SCOPEFUEL_STALE_WARN", "0")
    assert stale_build.warning() is None
    assert stale_on["json"] == [] and stale_on["run"] == []


# --------------------------------------------------------------------------- CLI integration

HEALTHY_CODEX = ProviderResult(
    id="codex",
    pool_class="preserve",
    buckets=[Bucket(label="7d", window="7d", used_pct=10.0)],
)


def _stub_codex(monkeypatch):
    monkeypatch.setattr(cli, "registry", lambda: {"codex": lambda: HEALTHY_CODEX})


def _verdict() -> stale_build.Verdict:
    return stale_build.Verdict("behind", INSTALLED, HEAD, 7)


def test_gate_accept_warns_on_stderr_keeps_stdout_contract(monkeypatch, capsys):
    _stub_codex(monkeypatch)
    monkeypatch.setattr(stale_build, "warning", _verdict)
    assert cli.main(["gate", "-m", "codex-max", "--no-cache"]) == 0
    out = capsys.readouterr()
    first = out.out.splitlines()[0]
    assert first.startswith("profile=codex-max") and "pool=" in first  # wrk parses this line
    warning_lines = [ln for ln in out.err.splitlines() if ln.startswith("warning:")]
    assert len(warning_lines) == 1
    assert "7 commit(s)" in warning_lines[0] and REINSTALL in warning_lines[0]


def test_gate_deny_still_denies_with_warning(monkeypatch, capsys):
    """The warning is additive: a deny stays rc 3 with its reason on stderr."""
    policy.set_policy("codex", "exclude", until=dt.date(2099, 8, 31), note="t")
    _stub_codex(monkeypatch)
    monkeypatch.setattr(stale_build, "warning", _verdict)
    assert cli.main(["gate", "-m", "codex-max", "--no-cache"]) == 3
    out = capsys.readouterr()
    assert "warning: scopefuel build stale" in out.err


def test_gate_never_fails_because_the_check_fails(monkeypatch, capsys):
    """Assertion-RED mutant: if the check could raise into the gate this test
    fails by assertion (rc stays None), never by the mutant's own exception."""

    def boom():
        raise RuntimeError("mutant: the stale check is broken")

    _stub_codex(monkeypatch)
    monkeypatch.setattr(stale_build, "warning", boom)
    rc = None
    with contextlib.suppress(Exception):
        rc = cli.main(["gate", "-m", "codex-max", "--no-cache"])
    assert rc == 0
    out = capsys.readouterr()
    assert out.out.splitlines()[0].startswith("profile=codex-max")


def test_gate_tolerates_wrong_typed_verdict(monkeypatch, capsys):
    """A check returning garbage is treated as no verdict — never a crash."""
    _stub_codex(monkeypatch)
    monkeypatch.setattr(stale_build, "warning", lambda: "not-a-verdict")
    assert cli.main(["gate", "-m", "codex-max", "--no-cache"]) == 0
    out = capsys.readouterr()
    assert "warning: scopefuel build stale" not in out.err


def test_brief_stdout_stays_one_line_warning_on_stderr(monkeypatch, capsys):
    _stub_codex(monkeypatch)
    monkeypatch.setattr(stale_build, "warning", _verdict)
    assert cli.main(["--brief", "--no-cache"]) == 0
    out = capsys.readouterr()
    assert len(out.out.splitlines()) == 1  # statusline contract
    assert "warning: scopefuel build stale" in out.err
    assert "7 commit(s)" in out.err


def test_json_carries_stale_build_field(monkeypatch, capsys):
    _stub_codex(monkeypatch)
    monkeypatch.setattr(stale_build, "warning", _verdict)
    assert cli.main(["--json", "--no-cache"]) == 0
    payload = json.loads(capsys.readouterr().out)
    field = payload["stale_build"]
    assert field["status"] == "behind" and field["behind"] == 7
    assert field["installed_rev"] == INSTALLED and field["origin_rev"] == HEAD
    assert field["reinstall"] == REINSTALL


def test_json_field_null_when_up_to_date(monkeypatch, capsys):
    _stub_codex(monkeypatch)
    monkeypatch.setattr(stale_build, "warning", lambda: None)
    assert cli.main(["--json", "--no-cache"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stale_build"] is None


def test_recommend_output_includes_warning_line(monkeypatch, capsys):
    monkeypatch.setattr(bench, "DOTENV_PATH", pathlib.Path("/nonexistent.env"))
    _stub_codex(monkeypatch)
    monkeypatch.setattr(stale_build, "warning", _verdict)
    assert cli.main(["--recommend", "S+", "--no-cache"]) == 0
    out = capsys.readouterr()
    warning_lines = [ln for ln in out.err.splitlines() if ln.startswith("warning: scopefuel build stale")]
    assert len(warning_lines) == 1
    assert "7 commit(s)" in warning_lines[0] and REINSTALL in warning_lines[0]


def test_default_table_output_warns_on_stderr(monkeypatch, capsys):
    _stub_codex(monkeypatch)
    monkeypatch.setattr(stale_build, "warning", _verdict)
    assert cli.main(["--no-cache", "--no-color"]) == 0
    out = capsys.readouterr()
    assert "warning: scopefuel build stale" in out.err


def test_no_warning_when_current(monkeypatch, capsys):
    _stub_codex(monkeypatch)
    monkeypatch.setattr(stale_build, "warning", lambda: None)
    assert cli.main(["gate", "-m", "codex-max", "--no-cache"]) == 0
    out = capsys.readouterr()
    assert "warning: scopefuel build stale" not in out.err


# ---------------------------------------------------------------------------
# task #956 — catalog-aware wording (AC1/AC2), one line per command (AC3)
# ---------------------------------------------------------------------------


def _memoize_catalog(source: str) -> None:
    bench._CATALOG_MEMO[("path", "backend", "endpoint")] = bench.CatalogView(entries=(), source=source)


def test_server_catalog_view_qualifies_the_warning(monkeypatch, capsys):
    """catalog=server: the rows are current — only the launcher code is stale."""
    _stub_codex(monkeypatch)
    _memoize_catalog("server")
    monkeypatch.setattr(stale_build, "warning", _verdict)
    assert cli.main(["gate", "-m", "codex-max", "--no-cache"]) == 0
    err = capsys.readouterr().err
    assert "catalog rows come from the server (catalog=server)" in err
    assert "only the launcher code is stale" in err
    assert err.count("scopefuel build stale") == 1


def test_cache_sourced_catalog_view_qualifies_the_warning(monkeypatch):
    _stub_codex(monkeypatch)
    _memoize_catalog("cache-stale")
    monkeypatch.setattr(stale_build, "warning", _verdict)
    verdict = cli._stale_build_verdict()
    assert verdict is not None
    assert "catalog rows come from the server (catalog=cache-stale)" in verdict.line
    assert verdict.as_field()["catalog_source"] == "cache-stale"


def test_snapshot_catalog_view_keeps_the_bundled_wording(monkeypatch):
    """catalog=snapshot: the bundled catalog is what a reinstall refreshes —
    the warning is exactly #952's wording."""
    _stub_codex(monkeypatch)
    _memoize_catalog("snapshot")
    monkeypatch.setattr(stale_build, "warning", _verdict)
    verdict = cli._stale_build_verdict()
    assert verdict is not None
    assert verdict.line == stale_build.Verdict("behind", INSTALLED, HEAD, 7).line
    assert "catalog rows come from the server" not in verdict.line


def test_no_memoized_view_keeps_the_bundled_wording(monkeypatch):
    _stub_codex(monkeypatch)
    bench.reset_catalog_memo()
    monkeypatch.setattr(stale_build, "warning", _verdict)
    verdict = cli._stale_build_verdict()
    assert verdict is not None
    assert verdict.line == stale_build.Verdict("behind", INSTALLED, HEAD, 7).line
    assert verdict.as_field()["catalog_source"] is None


def test_json_carries_catalog_source(monkeypatch, capsys):
    _stub_codex(monkeypatch)
    _memoize_catalog("server")
    monkeypatch.setattr(stale_build, "warning", _verdict)
    assert cli.main(["--json", "--no-cache"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stale_build"]["catalog_source"] == "server"


def test_json_catalog_source_null_without_memoized_view(monkeypatch, capsys):
    _stub_codex(monkeypatch)
    bench.reset_catalog_memo()
    monkeypatch.setattr(stale_build, "warning", _verdict)
    assert cli.main(["--json", "--no-cache"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stale_build"]["catalog_source"] is None


def test_warning_never_triggers_a_catalog_read(stale_on, monkeypatch):
    """The memo is only inspected — the warning path must not read the catalog."""
    calls = []

    def counting_read_catalog(**kwargs):
        calls.append(kwargs)
        raise AssertionError("read_catalog must not run from the warning path")

    monkeypatch.setattr(bench, "read_catalog", counting_read_catalog)
    monkeypatch.setattr(stale_build, "warning", _verdict)
    assert cli._stale_build_verdict() is not None
    assert stale_build.warning() is not None
    assert calls == []


def test_one_warning_line_per_command_path(monkeypatch, capsys):
    """gate accept, --brief and --recommend each print the stale line once."""
    monkeypatch.setattr(bench, "DOTENV_PATH", pathlib.Path("/nonexistent.env"))
    _stub_codex(monkeypatch)
    monkeypatch.setattr(stale_build, "warning", _verdict)

    assert cli.main(["gate", "-m", "codex-max", "--no-cache"]) == 0
    assert capsys.readouterr().err.count("scopefuel build stale") == 1

    assert cli.main(["--brief", "--no-cache"]) == 0
    assert capsys.readouterr().err.count("scopefuel build stale") == 1

    assert cli.main(["--recommend", "S+", "--no-cache"]) == 0
    assert capsys.readouterr().err.count("scopefuel build stale") == 1


def test_gate_deny_prints_the_stale_line_exactly_once(monkeypatch, capsys):
    policy.set_policy("codex", "exclude", until=dt.date(2099, 8, 31), note="t")
    _stub_codex(monkeypatch)
    monkeypatch.setattr(stale_build, "warning", _verdict)
    assert cli.main(["gate", "-m", "codex-max", "--no-cache"]) == 3
    assert capsys.readouterr().err.count("scopefuel build stale") == 1


# ---------------------------------------------------------------------------
# task #956 N3 — the probe bound is wall-clock, not per-operation (AC4)
# ---------------------------------------------------------------------------


def test_hung_probe_returns_within_the_wall_clock_budget(stale_on, monkeypatch):
    """A socket op that hangs past the budget must not stall warning() —
    urllib's per-operation timeout can otherwise multiply PROBE_BUDGET_S."""
    _set_installed(monkeypatch, INSTALLED)

    def sleepy_get_json(url: str, timeout: float):
        stale_on["json"].append((url, timeout))
        time.sleep(3.0)  # longer than PROBE_BUDGET_S
        raise RuntimeError("the socket never answered")

    monkeypatch.setattr(stale_build, "_get_json", sleepy_get_json)
    start = time.monotonic()
    verdict = stale_build.warning()
    elapsed = time.monotonic() - start
    assert elapsed < 2.5, f"warning() overran the wall-clock budget: {elapsed:.2f}s"
    assert verdict is None
    cached = stale_build._read_cache()
    assert cached is not None and cached["status"] == "skipped"

    # The abandoned worker finishes its sleep after we returned — it must not
    # write the cache over the caller's skip record.
    time.sleep(1.5)
    cached = stale_build._read_cache()
    assert cached is not None and cached["status"] == "skipped"
    assert all(t is threading.main_thread() or t.daemon for t in threading.enumerate())


def test_prompt_probe_still_yields_the_behind_verdict(stale_on, monkeypatch):
    """The bounding wrapper does not change a probe that answers in time."""
    _set_installed(monkeypatch, INSTALLED)
    _net(monkeypatch, stale_on, ahead_by=5)
    verdict = stale_build.warning()
    assert verdict is not None and verdict.status == "behind" and verdict.behind == 5


# ---------------------------------------------------------------------------
# task #956 N4 — a future-dated checked_at is not fresh forever (AC5)
# ---------------------------------------------------------------------------


def _seed_cache(checked_at: float) -> None:
    stale_build._write_cache(
        {
            "schema": stale_build._CACHE_SCHEMA,
            "checked_at": checked_at,
            "installed_rev": INSTALLED,
            "origin_rev": HEAD,
            "behind": 3,
            "status": "behind",
        }
    )


def test_future_checked_at_reprobes_and_rewrites(stale_on, monkeypatch):
    """A checked_at a day in the future (clock step, bad write) is not fresh —
    the probe runs and the cache is rewritten with the real time."""
    clock = [1_000.0]
    monkeypatch.setattr(stale_build, "_now", lambda: clock[0])
    _set_installed(monkeypatch, INSTALLED)
    _seed_cache(checked_at=clock[0] + 86400.0)
    _net(monkeypatch, stale_on, ahead_by=9)

    verdict = stale_build.warning()
    assert stale_on["json"]  # the probe ran despite the "fresh" timestamp
    assert verdict is not None and verdict.behind == 9
    cached = stale_build._read_cache()
    assert cached is not None and cached["checked_at"] == clock[0]


def test_checked_at_within_skew_tolerance_is_still_fresh(stale_on, monkeypatch):
    """30s into the future is ordinary clock skew — the cached verdict stands."""
    clock = [1_000.0]
    monkeypatch.setattr(stale_build, "_now", lambda: clock[0])
    _set_installed(monkeypatch, INSTALLED)
    _seed_cache(checked_at=clock[0] + 30.0)

    verdict = stale_build.warning()
    assert verdict is not None and verdict.behind == 3  # restored from cache
    assert stale_on["json"] == [] and stale_on["run"] == []  # no probe


# ---------------------------------------------------------------------------
# task #971 — one cache writer per check: only the calling thread writes
# ---------------------------------------------------------------------------


def _fast_budget(monkeypatch, budget: float = 0.3, grace: float = 0.05) -> None:
    monkeypatch.setattr(stale_build, "PROBE_BUDGET_S", budget)
    monkeypatch.setattr(stale_build, "_PROBE_GRACE_S", grace)


def _log_writes(monkeypatch) -> list[tuple[str, str]]:
    """Wrap _write_cache so every call records (writing thread, entry status)."""
    log: list[tuple[str, str]] = []
    real_write = stale_build._write_cache

    def logging_write(entry):
        real_write(entry)
        who = "main" if threading.current_thread() is threading.main_thread() else "worker"
        log.append((who, entry["status"]))

    monkeypatch.setattr(stale_build, "_write_cache", logging_write)
    return log


def test_fast_probe_writes_the_cache_once_on_the_calling_thread(stale_on, monkeypatch):
    """AC1: a normal fast probe — exactly one write, by the caller."""
    _set_installed(monkeypatch, INSTALLED)
    _net(monkeypatch, stale_on, ahead_by=3)
    log = _log_writes(monkeypatch)
    assert stale_build.warning() is not None
    assert log == [("main", "behind")], log


def test_expired_probe_writes_skipped_once_and_the_worker_adds_nothing(stale_on, monkeypatch):
    """AC1: a probe that overruns the budget — the caller writes skipped once;
    the abandoned worker finishing late adds no second write."""
    _set_installed(monkeypatch, INSTALLED)
    _fast_budget(monkeypatch, budget=0.2, grace=0.05)
    release = threading.Event()

    def hang(url: str, timeout: float):
        release.wait(10)
        return {"sha": HEAD}

    monkeypatch.setattr(stale_build, "_get_json", hang)
    log = _log_writes(monkeypatch)
    assert stale_build.warning() is None
    assert log == [("main", "skipped")], log
    release.set()
    time.sleep(0.5)
    assert log == [("main", "skipped")], log
    assert stale_build._read_cache()["status"] == "skipped"


def _install_race_harness(monkeypatch, *, mode: str) -> list[tuple[str, str]]:
    """Force an ordering inside the join-timeout / expired.set() window.

    mode="A": the worker completes its whole probe — a real verdict — in the
              gap between the caller's join timeout and ``expired.set()``
              taking effect.
    mode="B": the worker passes its first ``expired.is_set()`` check (reads
              False), then the caller sets the flag and writes ``skipped``
              while the worker is still mid-probe.
    Returns the ordered write log [(thread, status)].
    """
    _fast_budget(monkeypatch)
    release = threading.Event()
    worker_checked = threading.Event()
    caller_done = threading.Event()
    worker_done = threading.Event()
    log: list[tuple[str, str]] = []

    def blocking_get_json(url: str, timeout: float):
        if url.endswith("/commits/main"):
            release.wait(10)
            return {"sha": HEAD}
        return {"ahead_by": 4}

    monkeypatch.setattr(stale_build, "_get_json", blocking_get_json)

    real_write = stale_build._write_cache

    def logging_write(entry):
        real_write(entry)
        who = "main" if threading.current_thread() is threading.main_thread() else "worker"
        log.append((who, entry["status"]))
        if who == "main":
            caller_done.set()

    monkeypatch.setattr(stale_build, "_write_cache", logging_write)

    real_probe = stale_build._probe

    def probe_spy(*args):
        try:
            return real_probe(*args)
        finally:
            worker_done.set()

    monkeypatch.setattr(stale_build, "_probe", probe_spy)

    class HookEvent(threading.Event):
        def set(self):
            release.set()
            if mode == "A":
                worker_done.wait(10)
            else:
                worker_checked.wait(10)
            super().set()

        def is_set(self):
            r = super().is_set()
            if mode == "B" and threading.current_thread() is not threading.main_thread() and not r:
                worker_checked.set()
                caller_done.wait(10)
            return r

    monkeypatch.setattr(
        stale_build, "threading", types.SimpleNamespace(Event=HookEvent, Thread=threading.Thread)
    )
    return log


def test_race_A_worker_finishing_in_the_expired_gap_still_writes_nothing(stale_on, monkeypatch):
    """AC1 (race A restated): the worker completes a real probe between the
    caller's join timeout and expired.set() — the skipped record is the only
    write and the late result is discarded."""
    _set_installed(monkeypatch, INSTALLED)
    log = _install_race_harness(monkeypatch, mode="A")
    assert stale_build.warning() is None
    assert log == [("main", "skipped")], log
    assert stale_build._read_cache()["status"] == "skipped"


def test_race_B_worker_past_its_check_when_skipped_lands_writes_nothing(stale_on, monkeypatch):
    """AC1 (race B restated — the #956 xfail, now passing): the worker already
    read ``expired`` as False when the caller writes skipped; with the write
    owned by the calling thread there is no second write and the cache stays
    skipped."""
    _set_installed(monkeypatch, INSTALLED)
    log = _install_race_harness(monkeypatch, mode="B")
    assert stale_build.warning() is None
    time.sleep(0.3)
    assert log == [("main", "skipped")], log
    assert stale_build._read_cache()["status"] == "skipped"


def test_expired_probe_that_later_succeeds_never_overwrites_skipped(stale_on, monkeypatch):
    """AC2: the abandoned worker eventually gets a *real* answer (behind) —
    the cache must stay skipped. Worker-side ``is_set`` is pinned False so the
    probe runs its full sequence to a real verdict — exactly the interleaving
    the old check-then-write lost."""
    _set_installed(monkeypatch, INSTALLED)
    _fast_budget(monkeypatch, budget=0.2, grace=0.05)

    def slow_then_ok(url: str, timeout: float):
        time.sleep(0.6)
        if url.endswith("/commits/main"):
            return {"sha": HEAD}
        return {"ahead_by": 4}

    monkeypatch.setattr(stale_build, "_get_json", slow_then_ok)
    # deadline passed by the time the sleep ends: keep _remaining positive so
    # the worker can complete the sequence and reach a real verdict
    monkeypatch.setattr(
        stale_build,
        "_remaining",
        lambda deadline: 0.2 if threading.current_thread() is threading.main_thread() else 5.0,
    )

    class BlindEvent(threading.Event):
        def is_set(self):
            if threading.current_thread() is not threading.main_thread():
                return False
            return super().is_set()

    monkeypatch.setattr(
        stale_build, "threading", types.SimpleNamespace(Event=BlindEvent, Thread=threading.Thread)
    )
    assert stale_build.warning() is None
    assert stale_build._read_cache()["status"] == "skipped"
    time.sleep(1.5)  # the worker has now finished both calls
    assert stale_build._read_cache()["status"] == "skipped"


def test_in_bound_probe_caches_current(stale_on, monkeypatch):
    """AC3: a timely probe that finds the install current caches 'current'."""
    _set_installed(monkeypatch, HEAD)
    _net(monkeypatch, stale_on, ahead_by=0)
    assert stale_build.warning() is None
    cached = stale_build._read_cache()
    assert cached is not None and cached["status"] == "current"
    assert cached["installed_rev"] == HEAD and cached["origin_rev"] == HEAD


def test_in_bound_probe_caches_behind_with_the_count(stale_on, monkeypatch):
    """AC3: a timely behind verdict lands in the cache with its distance."""
    _set_installed(monkeypatch, INSTALLED)
    _net(monkeypatch, stale_on, ahead_by=4)
    assert stale_build.warning().behind == 4
    cached = stale_build._read_cache()
    assert cached is not None and cached["status"] == "behind" and cached["behind"] == 4


def test_in_bound_probe_caches_rev_unknown(stale_on, monkeypatch):
    """AC3: an unknown installed rev lands in the cache as 'rev_unknown'."""
    _net(monkeypatch, stale_on)
    assert stale_build.warning().status == "rev_unknown"
    cached = stale_build._read_cache()
    assert cached is not None and cached["status"] == "rev_unknown"
    assert cached["installed_rev"] is None and cached["origin_rev"] == HEAD


def test_write_cache_uses_a_unique_temp_file_per_write(stale_on, monkeypatch):
    """AC4: two consecutive writes must not share a temp path — concurrent
    scopefuel processes can otherwise interleave on stale_build.tmp."""
    seen: list[str] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        seen.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    entry = {"schema": stale_build._CACHE_SCHEMA, "checked_at": 1.0, "status": "skipped"}
    stale_build._write_cache(entry)
    stale_build._write_cache(entry)
    cache_dir = stale_build._cache_path().parent
    assert len(seen) == 2 and seen[0] != seen[1], seen
    assert all(pathlib.Path(name).parent == cache_dir for name in seen)
    assert all(name.endswith(".tmp") for name in seen)
    assert stale_build._cache_path().stat().st_mode & 0o777 == 0o600
    assert list(cache_dir.glob("*.tmp")) == []


def test_write_cache_failure_cleans_the_temp_file(stale_on, monkeypatch):
    """AC4: a failed os.replace must not break the check or leave a temp file."""
    seen: list[str] = []

    def boom(src, dst):
        seen.append(str(src))
        raise OSError("replace denied")

    monkeypatch.setattr(os, "replace", boom)
    stale_build._write_cache({"schema": stale_build._CACHE_SCHEMA, "status": "skipped"})
    cache_dir = stale_build._cache_path().parent
    assert seen, "the write must have reached os.replace"
    assert list(cache_dir.glob("*.tmp")) == []
    assert not stale_build._cache_path().exists()


@pytest.mark.parametrize("bad", ["abc", None, True, [], {}, "12x", int("1" + "0" * 400)])
def test_unusable_checked_at_reprobes(stale_on, monkeypatch, bad):
    """AC5: an unparsable checked_at — including an integer too big for a
    float (OverflowError) — is not fresh: the probe runs exactly once."""
    _set_installed(monkeypatch, INSTALLED)
    stale_build._write_cache(
        {
            "schema": stale_build._CACHE_SCHEMA,
            "checked_at": bad,
            "installed_rev": INSTALLED,
            "origin_rev": HEAD,
            "behind": 3,
            "status": "behind",
        }
    )
    _net(monkeypatch, stale_on, ahead_by=8)
    verdict = stale_build.warning()
    assert stale_on["json"], f"checked_at={bad!r} must trigger a probe"
    assert len(stale_on["json"]) == 2  # one probe: head + compare
    assert verdict is not None and verdict.behind == 8


# ---------------------------------------------------------------------------
# task #981 — every unreadable cache reads as absent; the write cleans up
# after itself; an expired probe makes no further network call
# ---------------------------------------------------------------------------


def _plant_cache(content: bytes) -> None:
    path = stale_build._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.mark.parametrize(
    "content",
    [
        b'{"schema": "%s", "checked_at": 1%s}' % (stale_build._CACHE_SCHEMA.encode(), b"0" * 5000),
        b"\xff\xfe\x00garbage",
        b"[" * 100000,
    ],
    ids=["int-over-4300-digits", "invalid-utf8", "deep-nesting"],
)
def test_unreadable_cache_reprobes_and_rewrites(stale_on, monkeypatch, content):
    """AC1: a cache file the reader cannot parse — a >4300-digit int
    (ValueError), invalid UTF-8 (UnicodeDecodeError), 100000-deep nesting
    (RecursionError) — reads as absent: warning() runs the probe exactly once
    and the write afterwards replaces the bad file with a valid record."""
    _set_installed(monkeypatch, INSTALLED)
    _plant_cache(content)
    _net(monkeypatch, stale_on, ahead_by=8)
    verdict = stale_build.warning()
    assert len(stale_on["json"]) == 2, f"expected exactly one probe (head + compare): {stale_on['json']}"
    assert verdict is not None and verdict.behind == 8
    cached = stale_build._read_cache()
    assert cached is not None and cached["schema"] == stale_build._CACHE_SCHEMA
    assert cached["status"] == "behind" and cached["behind"] == 8


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def _spy_mkstemp(monkeypatch) -> list[int]:
    """Wrap tempfile.mkstemp so os.write/os.close fakes can recognise the
    cache's own fd and leave every other fd on the real implementation."""
    made: list[int] = []
    real_mkstemp = tempfile.mkstemp

    def spy(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        made.append(fd)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", spy)
    return made


def test_write_failure_leaks_no_fd_and_leaves_no_temp(stale_on, monkeypatch):
    """AC2 (#1000 shape): os.write raising must not leak the mkstemp fd — and
    the temp file is unlinked like every other write failure."""
    made = _spy_mkstemp(monkeypatch)
    real_write = os.write

    def boom(fd: int, data) -> int:
        if fd in made:
            raise OSError("write")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", boom)
    before = _open_fds()
    for _ in range(20):
        stale_build._write_cache({"schema": stale_build._CACHE_SCHEMA, "status": "skipped"})
    assert _open_fds() == before, "fd from mkstemp leaked when os.write raises"
    assert list(stale_build._cache_path().parent.glob("*.tmp")) == []


def test_unserializable_entry_raises_before_any_temp_file_exists(stale_on):
    """AC3: an entry json.dumps cannot serialize raises TypeError *before*
    mkstemp runs — the cache dir never holds a temp file for it."""
    cache_dir = stale_build._cache_path().parent
    cache_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(TypeError):
        stale_build._write_cache({"schema": stale_build._CACHE_SCHEMA, "bad": object()})
    assert list(cache_dir.iterdir()) == [], "serialization failed after a temp file existed"


def test_expired_worker_makes_no_compare_call_after_release(stale_on, monkeypatch):
    """AC4: once the caller flags the deadline as expired, the abandoned worker
    cuts the sequence short — it must not issue the compare call whose result
    nobody will read. (Covering test adopted from the #971 tester.)"""
    _set_installed(monkeypatch, INSTALLED)
    _fast_budget(monkeypatch, budget=0.2, grace=0.05)
    release = threading.Event()
    urls: list[str] = []

    def get(url: str, timeout: float):
        urls.append(url)
        if url.endswith("/commits/main"):
            release.wait(10)
            return {"sha": HEAD}
        return {"ahead_by": 9}

    monkeypatch.setattr(stale_build, "_get_json", get)
    # keep _remaining positive for the worker so only `expired` can stop it
    monkeypatch.setattr(
        stale_build,
        "_remaining",
        lambda d: 0.2 if threading.current_thread() is threading.main_thread() else 5.0,
    )
    assert stale_build.warning() is None
    release.set()
    time.sleep(0.4)
    assert [u for u in urls if "/compare/" in u] == [], urls
    assert stale_build._read_cache()["status"] == "skipped"


# ---------------------------------------------------------------------------
# task #990 — each expired check is pinned by its own test
# ---------------------------------------------------------------------------


def test_expired_at_the_head_check_records_skipped(stale_on, monkeypatch):
    """SHOULD-1 (outer check): a worker that finds ``expired`` already set when
    the head resolves reports skipped — without that check the abandoned
    probe's late result lands in the cache as a real verdict."""
    _set_installed(monkeypatch, INSTALLED)
    _net(monkeypatch, stale_on, ahead_by=9)
    real_probe = stale_build._probe

    class Expired:
        def is_set(self) -> bool:
            return True

        def set(self) -> None:
            pass

    monkeypatch.setattr(stale_build, "_probe", lambda inst, dl, ev: real_probe(inst, dl, Expired()))
    assert stale_build.warning() is None
    assert stale_build._read_cache()["status"] == "skipped"


def test_expired_between_the_checks_never_calls_compare(stale_on, monkeypatch):
    """SHOULD-1 (inner check), adopted from the #981 tester's window test:
    ``expired.is_set`` reads False at the head check but True by the compare
    guard — the sequence must stop before the compare call."""
    _set_installed(monkeypatch, INSTALLED)
    _net(monkeypatch, stale_on, ahead_by=9)
    real_probe = stale_build._probe

    class Flip:
        def __init__(self) -> None:
            self.n = 0

        def is_set(self) -> bool:
            self.n += 1
            return self.n >= 2  # head check reads False, compare guard True

        def set(self) -> None:
            pass

    monkeypatch.setattr(stale_build, "_probe", lambda inst, dl, ev: real_probe(inst, dl, Flip()))
    stale_build.warning()
    assert [url for url, _ in stale_on["json"] if "/compare/" in url] == [], stale_on["json"]


# ---------------------------------------------------------------------------
# task #1000 — the mkstemp fd has one owner and one close: an os.write loop
# writes the whole payload, no fdopen/fstat guard exists
# ---------------------------------------------------------------------------


def test_partial_os_write_still_lands_the_full_record(stale_on, monkeypatch):
    """AC1 (#1000): os.write may return a short count — the write loop must
    keep going until every byte of the payload is on disk. A mutant that
    issues a single os.write leaves a truncated, unparseable record."""
    made = _spy_mkstemp(monkeypatch)
    real_write = os.write

    def one_byte_at_a_time(fd: int, data) -> int:
        if fd not in made:
            return real_write(fd, data)
        return real_write(fd, data[:1])  # worst-case partial write

    monkeypatch.setattr(os, "write", one_byte_at_a_time)
    entry = {
        "schema": stale_build._CACHE_SCHEMA,
        "checked_at": 1.0,
        "installed_rev": INSTALLED,
        "origin_rev": HEAD,
        "behind": 3,
        "status": "behind",
    }
    stale_build._write_cache(entry)
    assert stale_build._read_cache() == entry


def test_os_write_failure_closes_the_fd_exactly_once(stale_on, monkeypatch):
    """AC2 (#1000): os.write raising OSError does not escape, leaves no temp
    file, and the mkstemp fd is closed exactly once — by the finally that
    owns it, never again in the error path."""
    made = _spy_mkstemp(monkeypatch)
    closed: list[int] = []
    real_close = os.close
    real_write = os.write

    def boom_write(fd: int, data) -> int:
        if fd in made:
            raise OSError("write denied")
        return real_write(fd, data)

    def tracking_close(fd: int) -> None:
        if fd in made:
            closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "write", boom_write)
    monkeypatch.setattr(os, "close", tracking_close)
    stale_build._write_cache({"schema": stale_build._CACHE_SCHEMA, "status": "skipped"})
    assert made, "mkstemp never ran"
    assert closed == made, f"each mkstemp fd must be closed exactly once: {closed} vs {made}"
    assert list(stale_build._cache_path().parent.glob("*.tmp")) == []
    assert not stale_build._cache_path().exists()


def test_reused_fd_number_is_never_closed_by_write_cache(stale_on, monkeypatch):
    """AC3 (#1000, the #990 reuse scenario restated): with no fdopen there is
    no seam that can release the mkstemp fd early, so the only os.close runs
    in the finally while this block still owns it. The tracking close plants
    a foreign fd on the released number — a stale second close would kill
    it, and a missed re-check would leave nothing to observe."""
    made = _spy_mkstemp(monkeypatch)
    closed: list[int] = []
    foreign: list[int] = []
    real_close = os.close
    real_write = os.write

    def boom_write(fd: int, data) -> int:
        if fd in made:
            raise OSError("write denied")
        return real_write(fd, data)

    def tracking_close(fd: int) -> None:
        if fd in made:
            closed.append(fd)
        real_close(fd)
        if fd in made:
            # A foreign fd immediately reuses the released number — any close
            # by number after this point would hit it.
            foreign.append(os.open(os.devnull, os.O_RDONLY))

    monkeypatch.setattr(os, "write", boom_write)
    monkeypatch.setattr(os, "close", tracking_close)
    stale_build._write_cache({"schema": stale_build._CACHE_SCHEMA, "status": "skipped"})
    try:
        assert closed == made, closed
        assert foreign, "the released number was never reused — harness broken"
        for f in foreign:
            os.fstat(f)  # OSError if _write_cache closed the foreign fd
    finally:
        for f in foreign:
            real_close(f)
