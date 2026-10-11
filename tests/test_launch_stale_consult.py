"""hk 1444: a satisfied consult_only row must not be re-refused by the stale rule.

While the catalog view is stale, ``codex-astra`` had no flag combination that
passed both surfaces: ``policy launch``'s stale rule refused the consult_only
row that an allowed ``--purpose`` had already admitted, and the quota gate
refuses ``--operator-request`` on astra by #527 design (astra's key is the
purpose axis). consult_only is the narrowest gate, so a stale view cannot
widen it — the launch side now stops re-refusing what the consult_only check
already admitted. recommend.py is untouched.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest
from test_bench_backend import FakeHandoffkeep, _set_backend
from test_bench_catalog import _seed_rows

from scopefuel import bench, cli, launch, recommend
from scopefuel.model import Bucket, ProviderResult, Scope

TODAY = dt.date(2026, 10, 11)
NOW = dt.datetime(2026, 10, 11, 12, 0, 0, tzinfo=dt.UTC)

# Past the 24h catalog_stale_max_s ceiling so the last-good cache serves as
# source="cache-stale" (the same stamp test_policy_launch.py's fixture uses).
_STALE_AGE_S = 200_000


@pytest.fixture(params=[bench.CATALOG_SOURCE_CACHE_STALE, bench.CATALOG_SOURCE_SNAPSHOT])
def stale_view(request, tmp_path, monkeypatch):
    """A canon-configured host whose served catalog is stale, each way it can be.

    ``cache-stale``: the last-good cache is older than ``catalog_stale_max_s``
    and the server is unreachable. ``snapshot``: no cache exists at all, so the
    bundled snapshot serves. Both are ``view.stale`` on a non-local backend.
    """

    _set_backend(tmp_path, monkeypatch)
    fake = FakeHandoffkeep()
    fake.catalog = _seed_rows()
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    if request.param == bench.CATALOG_SOURCE_CACHE_STALE:
        bench.reset_catalog_memo()
        assert bench.read_catalog().source == bench.CATALOG_SOURCE_SERVER
        stamp = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=_STALE_AGE_S)).isoformat()
        conn = sqlite3.connect(bench.db_path())
        try:
            conn.execute("UPDATE bench_cache_meta SET fetched_at = ? WHERE scope = 'catalog'", (stamp,))
            conn.commit()
        finally:
            conn.close()
    fake.offline = True
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert view.source == request.param
    assert view.stale is True
    return view


@pytest.fixture
def fresh_view(tmp_path, monkeypatch):
    """The same canon, served live by the fake server — nothing here is stale."""

    _set_backend(tmp_path, monkeypatch)
    fake = FakeHandoffkeep()
    fake.catalog = _seed_rows()
    monkeypatch.setattr(bench, "request_json", fake.request_json)
    bench.reset_catalog_memo()
    view = bench.read_catalog()
    assert view.source == bench.CATALOG_SOURCE_SERVER
    assert view.stale is False
    return view


def _view_without(*profiles: str) -> bench.CatalogView:
    """A fresh server view that does not carry these profiles at all.

    ``resolve_launch`` fills such profiles from the bundled snapshot
    (``from_snapshot``) — the stale rule's second leg, which must behave the
    same way even though the view itself is not stale.
    """

    entries = tuple(e for e in bench.catalog_snapshot() if e.profile not in profiles)
    return bench.CatalogView(
        entries=entries,
        source=bench.CATALOG_SOURCE_SERVER,
        backend=bench.BENCH_BACKEND_HANDOFFKEEP,
    )


def _provider(
    provider_id: str, used: float, pool_class: str = "preserve", window: str = "7d"
) -> ProviderResult:
    hours = {"5h": 4.9, "1d": 23.5, "7d": 167.0, "30d": 719.0}[window]
    return ProviderResult(
        id=provider_id,
        pool_class=pool_class,  # type: ignore[arg-type]
        buckets=[
            Bucket(
                label=window,
                window=window,
                used_pct=used,
                resets_at=(NOW + dt.timedelta(hours=hours)).isoformat(),
                scope=Scope("account"),
                horizon="week",  # type: ignore[arg-type]
            )
        ],
    )


def _healthy_providers() -> list[ProviderResult]:
    """Healthy pools for every profile this file judges (claude/codex + a spare)."""

    return [
        _provider("claude", 10.0),
        _provider("codex", 10.0),
        _provider("kiro", 10.0, pool_class="spend", window="30d"),
    ]


def _gate(
    profile: str,
    *,
    purpose: str | None = None,
    operator_request: bool = False,
    effort: str | None = None,
) -> recommend.GateResult:
    """The quota gate on the same request shape the launch used.

    The launch flag is a bare bool; the gate consumes the durable REF form the
    spawner would carry (``hk:task/<id>`` — always a valid ref here).
    """

    return recommend.gate_check(
        _healthy_providers(),
        profile,
        today=TODAY,
        now=NOW,
        purpose=purpose,
        operator_request="hk:task/1444" if operator_request else None,
        effort=effort,
    )


def _refused_because_of_the_request(result: recommend.GateResult) -> bool:
    """Whether the refusal exists only because ``--operator-request`` was passed.

    Parsed fields, not reason text: the request-validation refusals
    (``operator_request_not_applicable``, ``operator_request_ref_invalid``) are
    the only refusals that carry ``operator_request_ref`` on a healthy pool
    without being a role denial, an unmeasurable answer, or a subscription
    refusal.
    """

    return (
        not result.ok
        and result.operator_request_ref is not None
        and not result.role_denied
        and not result.unmeasurable
        and not result.unsubscribed
    )


# --- pinned case 1: stale catalog + astra + allowed purpose launches ---------


@pytest.mark.parametrize("purpose", ["operator-request", "architect", "director"])
def test_stale_catalog_astra_allowed_purpose_launches(stale_view, purpose):
    """The bug: this exact call was refused by the stale rule on main."""

    decision = launch.resolve_launch("codex-astra", purpose=purpose, view=stale_view)
    assert decision.model_id == "gpt-6-astra"
    assert decision.effort == "xhigh"
    assert decision.gate == "consult_only"
    assert decision.catalog_stale is True
    assert decision.operator_request is False


@pytest.mark.parametrize("purpose", ["operator-request", "architect", "director"])
def test_astra_resolved_from_snapshot_rows_on_a_partial_canon_launches(purpose):
    """The stale rule's other leg: ``from_snapshot``. A fresh server catalog
    that does not carry astra resolves it from the bundled snapshot — the
    consult_only admission holds there too."""

    view = _view_without("codex-astra")
    assert view.stale is False  # the server answered; freshness is not the issue
    decision = launch.resolve_launch("codex-astra", purpose=purpose, view=view)
    assert decision.catalog_source == bench.CATALOG_SOURCE_SNAPSHOT
    assert decision.catalog_stale is True  # from_snapshot marks the same caveat
    assert decision.model_id == "gpt-6-astra"


def test_stale_astra_operator_request_still_launches(stale_view):
    """``--operator-request`` already passed the stale rule on main (the rule
    exempts it) — this pin exists so the fix is not credited with that, and so
    a regression that broke it would be caught."""

    decision = launch.resolve_launch("codex-astra", operator_request=True, view=stale_view)
    assert decision.model_id == "gpt-6-astra"
    assert decision.catalog_stale is True


def test_stale_astra_purpose_launch_end_to_end_via_cli(stale_view, capsys):
    """The wrk call shape: ``policy launch`` against the memoised stale view."""

    rc = cli.main(["policy", "launch", "codex-astra", "--purpose", "operator-request", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["model_id"] == "gpt-6-astra"
    assert payload["gate"] == "consult_only"
    assert payload["catalog"]["stale"] is True


# --- pinned case 2: unsatisfied consult_only is still refused ----------------


@pytest.mark.parametrize("purpose", [None, "", "  ", "worker", "builder", "arch", "operator_request"])
def test_stale_catalog_astra_unsatisfied_is_refused_by_the_consult_only_check(stale_view, purpose):
    """Refusal must come from the consult_only check — 'is consult_only',
    not 'the catalog is stale' (the stale message carries the same word)."""

    with pytest.raises(launch.LaunchError, match="is consult_only"):
        launch.resolve_launch("codex-astra", purpose=purpose, view=stale_view)


# --- pinned case 3: escalation under stale still needs --operator-request ----


def test_stale_escalation_rung_is_unchanged(stale_view):
    with pytest.raises(launch.LaunchError, match="catalog is stale"):
        launch.resolve_launch("opus", effort="max", view=stale_view)
    decision = launch.resolve_launch("opus", effort="max", operator_request=True, view=stale_view)
    assert decision.gate == "escalation"
    assert decision.catalog_stale is True


def test_snapshot_resolved_escalation_row_is_unchanged():
    """The from_snapshot leg for a non-default gate that is not consult_only."""

    view = _view_without("oc-omni")
    with pytest.raises(launch.LaunchError, match="catalog is stale"):
        launch.resolve_launch("oc-omni", view=view)
    assert launch.resolve_launch("oc-omni", operator_request=True, view=view).gate == "escalation"


# --- pinned case 4: fable (consult_only, not astra) unchanged ----------------


def test_stale_fable_consult_only_semantics_unchanged(stale_view):
    with pytest.raises(launch.LaunchError, match="is consult_only"):
        launch.resolve_launch("fable", view=stale_view)
    with pytest.raises(launch.LaunchError, match="is consult_only"):
        launch.resolve_launch("fable", purpose="architect", view=stale_view)
    decision = launch.resolve_launch("fable", operator_request=True, view=stale_view)
    assert decision.model_id == "claude-fable-5-1"
    assert decision.gate == "consult_only"


# --- pinned cases 5-6: launch <-> gate agreement, fresh-catalog regression ----

# (profile, effort, purpose, operator_request, expected under a degraded view,
#  expected under a fresh view). "degraded" = cache-stale, snapshot, or
#  snapshot-resolved — the stale rule treats all three the same.
_CASES: list[tuple[str, str | None, str | None, bool, str, str]] = [
    ("codex-astra", None, "operator-request", False, "ok", "ok"),
    ("codex-astra", None, "architect", False, "ok", "ok"),
    ("codex-astra", None, "director", False, "ok", "ok"),
    ("codex-astra", None, None, False, "is consult_only", "is consult_only"),
    ("codex-astra", None, "worker", False, "is consult_only", "is consult_only"),
    ("opus", "max", None, False, "catalog is stale", "ok"),
    ("opus", "max", None, True, "ok", "ok"),
    ("fable", None, None, False, "is consult_only", "is consult_only"),
    ("fable", None, None, True, "ok", "ok"),
    ("fable", None, "architect", False, "is consult_only", "is consult_only"),
]


def _launch_outcome(
    profile: str, *, view: bench.CatalogView, effort: str | None, purpose: str | None, request: bool
):
    try:
        return launch.resolve_launch(
            profile, effort=effort, operator_request=request, purpose=purpose, view=view
        )
    except launch.LaunchError as exc:
        return exc


@pytest.mark.parametrize(("profile", "effort", "purpose", "op_request", "degraded", "fresh"), _CASES)
def test_stale_view_launch_and_gate_do_not_contradict(
    stale_view, profile, effort, purpose, op_request, degraded, fresh
):
    """A launch that passes is never one the gate must refuse *for the
    operator-request reason* — the deadlock this task removes."""

    outcome = _launch_outcome(profile, view=stale_view, effort=effort, purpose=purpose, request=op_request)
    if degraded == "ok":
        assert isinstance(outcome, launch.LaunchDecision)
        assert outcome.catalog_stale is True
    else:
        assert isinstance(outcome, launch.LaunchError)
        assert degraded in str(outcome)
    gate = _gate(profile, purpose=purpose, operator_request=op_request, effort=effort)
    if isinstance(outcome, launch.LaunchDecision):
        assert not _refused_because_of_the_request(gate), gate.reason


@pytest.mark.parametrize(("profile", "effort", "purpose", "op_request", "degraded", "fresh"), _CASES)
def test_fresh_catalog_outcomes_unchanged(fresh_view, profile, effort, purpose, op_request, degraded, fresh):
    """Every case above behaves exactly as on main when the canon is live."""

    outcome = _launch_outcome(profile, view=fresh_view, effort=effort, purpose=purpose, request=op_request)
    if fresh == "ok":
        assert isinstance(outcome, launch.LaunchDecision)
        assert outcome.catalog_stale is False
    else:
        assert isinstance(outcome, launch.LaunchError)
        assert fresh in str(outcome)
    gate = _gate(profile, purpose=purpose, operator_request=op_request, effort=effort)
    if isinstance(outcome, launch.LaunchDecision):
        assert not _refused_because_of_the_request(gate), gate.reason
