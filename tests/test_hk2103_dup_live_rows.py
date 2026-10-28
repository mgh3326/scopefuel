"""hk 2103: launch agrees with the gate when duplicate live canon rows match a rung.

A served canon may hold two live rows for one (profile, effort) key — an
ordinary default row and a consult_only twin.  The gate collects every row for
the key (a consult_only twin refuses) while ``policy launch`` used to take
``matched[0]`` — the answer depended on row order.  Launch now refuses the
rung outright, and the catalog validity floor rejects a served canon that
carries duplicate live keys.

Also in this task (PR 195 CodeRabbit minors): ``policy set``/``policy list``
take every pool the catalog names — manual-only ones included — and a profile
whose rows are all retired reaches the "retired in the catalog" refusal even
when the E6 marker already names the rung.
"""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import io
import pathlib
import subprocess
import sys
import tarfile

import pytest
from test_bench_backend import FakeHandoffkeep, _set_backend

from scopefuel import bench, cli, launch, manual, policy
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.providers import BUILTIN
from scopefuel.recommend import E6_ARM_MARKER_ENV

# origin/main — the launch contract this task must keep byte-identical
# wherever a rung still holds exactly one live row.  Pinned at the main the
# branch last merged, so profiles the canon gained since the original PR base
# (cmd-dsflash41, codex-terra61) resolve under the same code on both sides.
BASE = "92eb842ce9f13c9badd1e2908adf58fcd1466aab"
NOW = dt.datetime.now(dt.UTC).replace(microsecond=0)
TODAY = NOW.date()
CREDIT_EXPIRES = (TODAY + dt.timedelta(days=30)).isoformat()
RETIRED_AT = "2026-10-24T00:00:00Z"
STILL_ACTIVE = TODAY + dt.timedelta(days=1)

# Three non-pi profiles plus the pi credit profile the hk 2079 round-5 note
# reported on.  An E6 unmeasured rung only shows two live rows once the marker
# keeps the ordinary row (the unmarked path drops it before the count check).
DUP_RUNGS = [
    pytest.param("sonnet", "high", None, id="sonnet@high"),
    pytest.param("haiku", "low", None, id="haiku@low"),
    pytest.param("codex-sol", "high", "codex-sol@high", id="codex-sol@high"),
    pytest.param("pi-sonnet55", "high", "pi-sonnet55@high", id="pi-sonnet55@high"),
]


def _result(pool: str, used: float = 10.0) -> ProviderResult:
    return ProviderResult(
        id=pool,
        pool_class="preserve",
        buckets=[Bucket(label="7d", window="7d", used_pct=used, scope=Scope("account"), horizon="week")],
    )


def _record_credit() -> None:
    manual.record_observation(
        pool="anthropic-api",
        window="credit",
        remaining_usd=150.0,
        total_usd=200.0,
        credit_expires_on=CREDIT_EXPIRES,
        measured_at=NOW,
        reason="Anthropic console credit balance",
        ttl_s=900.0,
        now=NOW,
    )


def _dup_view(profile: str, rung: str, *, twin_first: bool, **twin_changes) -> launch.CatalogView:
    """The bundled canon plus a second row for one (profile, effort) key."""
    rows = [entry.as_dict() for entry in bench.catalog_snapshot()]
    ordinary = next(row for row in rows if row["profile"] == profile and row["effort"] == rung)
    twin = {**ordinary, **twin_changes}
    if twin_first:
        rows.insert(rows.index(ordinary), twin)
    else:
        rows.append(twin)
    entries = bench._catalog_from_payload({"catalog": rows})
    return launch.CatalogView(entries=tuple(entries), source="server", backend="handoffkeep", age_s=0)


def _serve_canon(monkeypatch, view: launch.CatalogView) -> None:
    table = bench._catalog_grade_table(view)
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"claude": lambda: _result("claude"), "codex": lambda: _result("codex")},
    )
    monkeypatch.setattr(bench, "read_catalog", lambda **kwargs: view)
    monkeypatch.setattr(launch, "read_catalog", lambda **kwargs: view)
    monkeypatch.setattr(bench, "runtime_grade_table", lambda **kwargs: table)


@pytest.fixture
def policy_config(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_dir))
    return cfg_dir / "scopefuel" / "config.toml"


@pytest.fixture
def catalog_server(tmp_path, monkeypatch):
    """A handoffkeep that serves the catalog route, with a 1h catalog TTL."""

    data_home = _set_backend(tmp_path, monkeypatch)
    config_file = tmp_path / "config" / "scopefuel" / "config.toml"
    config_file.write_text(
        '[bench]\nbackend = "handoffkeep"\ncache_ttl_s = 21600\n'
        "catalog_ttl_s = 3600\ncatalog_stale_max_s = 86400\n",
        encoding="utf-8",
    )
    fake = FakeHandoffkeep()
    fake.catalog = [entry.as_dict() for entry in bench.catalog_snapshot()]
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    bench.reset_catalog_memo()
    return data_home, fake


def _age_catalog_cache(seconds: float) -> None:
    import sqlite3

    stamp = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds)).isoformat()
    conn = sqlite3.connect(bench.db_path())
    try:
        conn.execute("UPDATE bench_cache_meta SET fetched_at = ? WHERE scope = 'catalog'", (stamp,))
        conn.commit()
    finally:
        conn.close()
    bench.reset_catalog_memo()


# ---------------------------------------------------------------------------
# 1. the core rule: two live rows for one rung refuse at launch, as at the gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("profile", "effort", "marker"), DUP_RUNGS)
@pytest.mark.parametrize("twin_first", [False, True], ids=["ordinary-first", "consult-first"])
def test_duplicate_live_row_launch_refuses_like_the_gate(
    monkeypatch, capsys, profile, effort, marker, twin_first
):
    """Two live rows for one (profile, effort) key: launch refuses, gate refuses.

    The refusal names the profile, the rung and the row count.  Mutant: restore
    ``matched[0]`` without the count check and the ordinary-first launch goes
    RED (it admits with the first row's gate).
    """

    _record_credit()
    view = _dup_view(profile, effort, twin_first=twin_first, gate="consult_only")
    _serve_canon(monkeypatch, view)
    if marker is None:
        monkeypatch.delenv(E6_ARM_MARKER_ENV, raising=False)
    else:
        monkeypatch.setenv(E6_ARM_MARKER_ENV, marker)

    gate_rc = cli.main(["gate", "-m", profile, "--effort", effort, "--role", "worker", "--no-cache"])
    capsys.readouterr()
    launch_rc = cli.main(["policy", "launch", profile, "--effort", effort, "--role", "worker"])
    captured = capsys.readouterr()
    assert (gate_rc, launch_rc) == (3, 3), captured.out + captured.err
    text = captured.out + captured.err
    assert "live canon rows" in text
    assert profile in text
    assert effort in text
    assert "2" in text


def test_duplicate_refusal_names_the_fallback_rung_holding_the_rows(monkeypatch, capsys):
    """Requested rung absent, duplicate rows on the fallback rung: name the fallback.

    grok is placed at low/medium and silent about high, so
    ``--effort high`` falls back to the default placement (medium).  When the twin
    lives on that fallback rung the refusal must name the rung actually holding
    the duplicate rows, not the requested one.  Mutant: restore
    ``resolved_effort`` in the message and the refusal says rung 'high'.
    """

    view = _dup_view("grok", "medium", twin_first=False, gate="consult_only")
    _serve_canon(monkeypatch, view)
    monkeypatch.delenv(E6_ARM_MARKER_ENV, raising=False)

    rc = cli.main(["policy", "launch", "grok", "--effort", "high"])
    captured = capsys.readouterr()
    assert rc == 3
    text = captured.out + captured.err
    assert "live canon rows" in text
    assert "rung 'medium'" in text
    assert "rung 'high'" not in text


def test_two_rows_for_different_efforts_are_not_duplicates(monkeypatch, capsys):
    """Rows across efforts are not duplicates — the count is within the rung.

    Mutant: counting live rows across the profile's efforts instead of within
    the resolved rung makes both launches below go RED.
    """

    # devin-swe2 carries an effort-less row and a high row; add a third live
    # row at a *different* rung.  Both spellings still launch.
    view = _dup_view("devin-swe2", "", twin_first=False, effort="xhigh")
    _serve_canon(monkeypatch, view)
    monkeypatch.delenv(E6_ARM_MARKER_ENV, raising=False)

    assert cli.main(["policy", "launch", "devin-swe2"]) == 0
    assert cli.main(["policy", "launch", "devin-swe2", "--effort", "xhigh"]) == 0
    # And a profile that legitimately holds several live rungs is untouched.
    assert cli.main(["policy", "launch", "sonnet", "--effort", "high"]) == 0


def test_a_retired_twin_is_not_a_duplicate_live_row(monkeypatch, capsys):
    """A retired row next to a live row is not a duplicate — live rows only."""

    view = _dup_view("sonnet", "high", twin_first=False, retired_at=RETIRED_AT)
    _serve_canon(monkeypatch, view)
    monkeypatch.delenv(E6_ARM_MARKER_ENV, raising=False)
    assert cli.main(["policy", "launch", "sonnet", "--effort", "high"]) == 0


# ---------------------------------------------------------------------------
# 2. single-row keys: launch output is byte-identical to main
# ---------------------------------------------------------------------------


def _load_base(tmp_path):
    """The pre-change package, git-archived from BASE — the same seam test_task1990 uses."""

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
        "scopefuel_2103_base", pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_launch_is_byte_identical_to_main_for_every_snapshot_key(tmp_path, monkeypatch, capsys):
    """Every bundled-snapshot (profile, effort) key launches exactly as on main."""

    _load_base(tmp_path / "base")
    base_cli = importlib.import_module("scopefuel_2103_base.cli")
    base_bench = importlib.import_module("scopefuel_2103_base.bench")
    base_launch = importlib.import_module("scopefuel_2103_base.launch")

    view = launch.CatalogView(
        entries=bench.catalog_snapshot(), source="server", backend="handoffkeep", age_s=0
    )
    base_view = base_bench.CatalogView(
        entries=base_bench.catalog_snapshot(), source="server", backend="handoffkeep", age_s=0
    )
    monkeypatch.setattr(launch, "read_catalog", lambda **kwargs: view)
    monkeypatch.setattr(base_launch, "read_catalog", lambda **kwargs: base_view)

    checked = 0
    for entry in bench.catalog_snapshot():
        for role_argv in ([], ["--role", "worker"]):
            for marker in (None, f"{entry.profile}@{entry.effort}"):
                if marker is None:
                    monkeypatch.delenv(E6_ARM_MARKER_ENV, raising=False)
                else:
                    monkeypatch.setenv(E6_ARM_MARKER_ENV, marker)
                argv = ["policy", "launch", entry.profile, "--effort", entry.effort, *role_argv]
                rc = cli.main(argv)
                actual = capsys.readouterr()
                base_rc = base_cli.main(argv)
                expected = capsys.readouterr()
                assert (rc, actual.out, actual.err) == (base_rc, expected.out, expected.err), (
                    entry.key,
                    role_argv,
                    marker,
                )
                checked += 1
    assert checked > 0


# ---------------------------------------------------------------------------
# 3. the catalog validity floor rejects duplicate live keys
# ---------------------------------------------------------------------------


def test_server_catalog_rejection_names_the_duplicate_live_keys():
    """A served canon with two live rows for one key is not a canon."""

    rows = [entry.as_dict() for entry in bench.catalog_snapshot()]
    ordinary = next(row for row in rows if row["profile"] == "sonnet" and row["effort"] == "high")
    rows.append({**ordinary, "gate": "consult_only"})
    entries = bench._catalog_from_payload({"catalog": rows})
    rejection = bench._server_catalog_rejection(entries)
    assert rejection is not None
    assert "duplicate" in rejection
    assert "sonnet@high" in rejection

    # A retired twin is not a duplicate live key — the floor still passes.
    rows = [entry.as_dict() for entry in bench.catalog_snapshot()]
    ordinary = next(row for row in rows if row["profile"] == "sonnet" and row["effort"] == "high")
    rows.append({**ordinary, "gate": "consult_only", "retired_at": RETIRED_AT})
    entries = bench._catalog_from_payload({"catalog": rows})
    assert bench._server_catalog_rejection(entries) is None


def test_a_served_catalog_with_a_duplicate_key_falls_back(catalog_server, capsys):
    """The bad canon is refused at the floor and the cached/bundled fallback serves."""

    _, fake = catalog_server
    ordinary = next(row for row in fake.catalog if row["profile"] == "sonnet" and row["effort"] == "high")
    fake.catalog.append({**ordinary, "gate": "consult_only"})

    view = bench.read_catalog()
    assert view.source == "snapshot"
    assert "server catalog rejected" in view.label
    assert "sonnet@high" in view.label
    assert "server catalog rejected" in capsys.readouterr().err


def test_a_rejected_duplicate_catalog_keeps_the_last_good_cache(catalog_server, capsys):
    """Same rejection, with a committed cache: the cache stands in, labelled."""

    _, fake = catalog_server
    bench.read_catalog()  # prime the last-good cache (the full seed)
    _age_catalog_cache(3601)  # past the 1h TTL, inside the 24h stale ceiling
    ordinary = next(row for row in fake.catalog if row["profile"] == "sonnet" and row["effort"] == "high")
    fake.catalog.append({**ordinary, "gate": "consult_only"})
    bench.reset_catalog_memo()

    view = bench.read_catalog()
    assert view.source == "cache"
    assert "server catalog rejected" in view.label
    assert "server catalog rejected" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 4. policy set/list take every pool the catalog names (PR 195 CodeRabbit minor)
# ---------------------------------------------------------------------------


def test_policy_set_accepts_a_manual_only_pool_and_list_shows_it(policy_config, capsys, monkeypatch):
    """Mutant: restore ``choices=available`` and this set call goes RED."""

    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    assert cli.main(["policy", "set", "anthropic-api", "exclude", "--until", str(STILL_ACTIVE)]) == 0
    assert policy.get_policy("anthropic-api", "spend", today=TODAY)[0] == "exclude"
    assert cli.main(["policy", "list"]) == 0
    out = capsys.readouterr().out
    line = next(row for row in out.splitlines() if row.startswith("anthropic-api"))
    assert "exclude" in line


def test_policy_list_shows_a_manual_only_pool_with_no_policy(policy_config, capsys, monkeypatch):
    """A pool with no configured policy still lists — at its builtin class."""

    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    assert cli.main(["policy", "list"]) == 0
    out = capsys.readouterr().out
    line = next(row for row in out.splitlines() if row.startswith("anthropic-api"))
    # The manual-only credit pool is spend-classed by construction.
    assert "spend" in line


def test_policy_set_still_refuses_an_unknown_pool(policy_config, capsys, monkeypatch):
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    with pytest.raises(SystemExit) as exc:
        cli.main(["policy", "set", "nosuchpool", "spend", "--until", str(STILL_ACTIVE)])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# 5. all-retired + armed marker: the "retired in the catalog" refusal wins
# ---------------------------------------------------------------------------


def test_an_all_retired_e6_only_profile_with_the_marker_gets_the_retired_refusal(monkeypatch, capsys):
    """The canon retired every row — the answer is 'retired in the catalog',
    not 'set the marker' (the marker is already set).

    codex-terra61 is the remaining E6-only shape: the pi credit profiles are
    placed rungs since hk 2188, so an all-retired pi canon already reads
    "retired" with or without the marker.  Mutant: drop ``and not e6_open``
    and the marker-set case goes RED.
    """

    rows = [
        (
            {**entry.as_dict(), "retired_at": RETIRED_AT}
            if entry.profile == "codex-terra61"
            else entry.as_dict()
        )
        for entry in bench.catalog_snapshot()
    ]
    entries = bench._catalog_from_payload({"catalog": rows})
    view = launch.CatalogView(entries=tuple(entries), source="server", backend="handoffkeep", age_s=0)
    _serve_canon(monkeypatch, view)

    monkeypatch.setenv(E6_ARM_MARKER_ENV, "codex-terra61@high")
    rc = cli.main(["policy", "launch", "codex-terra61", "--effort", "high", "--role", "worker"])
    captured = capsys.readouterr()
    assert rc == 3
    assert "retired in the catalog" in captured.out + captured.err
    assert "e6_arm_required" not in captured.out + captured.err

    # Without the marker the same canon still arms the caller: e6_arm_required.
    monkeypatch.delenv(E6_ARM_MARKER_ENV, raising=False)
    rc = cli.main(["policy", "launch", "codex-terra61", "--effort", "high", "--role", "worker"])
    captured = capsys.readouterr()
    assert rc == 3
    assert "e6_arm_required" in captured.out + captured.err
