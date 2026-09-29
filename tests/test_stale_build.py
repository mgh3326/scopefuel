"""task #952 — installed-build staleness warning against origin/main.

All network and install-discovery seams are faked: no test reaches the real
GitHub API, git ls-remote, uv receipt, or dist-info. The conftest autouse
fixture already sets ``SCOPEFUEL_STALE_WARN=0``; tests opt in via ``stale_on``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import pathlib
import subprocess

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
