"""task #742 — ``subscribed`` flag on pools and profiles.

The flag never deletes a row: catalog rows, reps and grade history stay and
list/catalog views keep showing them (marked); recommend, gate and launch
refuse them; flipping the flag back restores eligibility. Every test pairs a
flagged assertion with an unflagged (or restored) control so a mutant that
drops the filter, ignores a precedence level, or eats the marker goes RED.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from scopefuel import bench, cli, grades, launch, policy, recommend, render
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import Profile, gate_check, profile_subscription

TODAY = dt.date(2026, 7, 31)
NOW = dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=dt.UTC)


def _reset_almost_full(window: str) -> str:
    hours = {"5h": 4.9, "1d": 23.5, "7d": 167.0, "30d": 719.0}.get(window, 167.0)
    return (NOW + dt.timedelta(hours=hours)).isoformat()


def _result(
    provider_id: str,
    used: float,
    pool_class: str = "spend",
    scope: Scope | None = None,
    window: str = "7d",
) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class=pool_class,  # type: ignore[arg-type]
        buckets=[
            Bucket(
                label=window,
                window=window,
                used_pct=used,
                resets_at=_reset_almost_full(window),
                scope=scope or Scope("account"),
                horizon="week",  # type: ignore[arg-type]
            )
        ],
    )


def _healthy_s_plus_providers() -> list[ProviderResult]:
    return [
        _result("claude", 10.0, pool_class="preserve"),
        _result("codex", 10.0, pool_class="preserve"),
        _result("kiro", 10.0, pool_class="spend", window="30d"),
    ]


def _candidate_names(out: str) -> list[str]:
    """Names on numbered candidate lines (fold/✗ lines are never candidates)."""
    names = []
    for line in out.splitlines():
        if not line[:1].isdigit():
            continue
        rest = line.split(".", 1)[1].strip()
        names.append(rest.split()[1] if rest.startswith("🔥") else rest.split()[0])
    return names


# ---------------------------------------------------------------------------
# config layer — read/write/clear, precedence, defaults
# ---------------------------------------------------------------------------


def test_default_config_flags_nothing():
    """Shipped default: no config file means every pool/profile is subscribed."""
    assert policy.get_subscribed("kiro") == (True, None)
    assert policy.get_profile_subscribed("kiro-sol") == (None, None)
    sub = profile_subscription("kiro-sol")
    assert sub.subscribed is True
    assert sub.source == "default"


def test_pool_flag_roundtrip_and_clear():
    policy.set_subscribed("kiro", False)
    assert policy.get_subscribed("kiro") == (False, None)
    # true is an explicit value, not the same as clearing the key.
    policy.set_subscribed("kiro", True)
    assert policy.get_subscribed("kiro") == (True, None)
    assert "subscribed = true" in policy.config_path().read_text()
    policy.set_subscribed("kiro", None)
    assert policy.get_subscribed("kiro") == (True, None)
    assert "kiro" not in policy.config_path().read_text()


def test_pool_flag_preserves_sibling_keys():
    """--subscribed must not clobber an existing class/boost on the same pool."""
    policy.set_policy("kiro", "exclude", until=dt.date(2099, 8, 31), note="keep me")
    policy.set_subscribed("kiro", False)
    effective, _ = policy.get_policy("kiro", "spend", today=TODAY)
    assert effective == "exclude"  # class survived the subscribed write
    assert policy.get_subscribed("kiro") == (False, None)
    policy.set_subscribed("kiro", None)
    effective, _ = policy.get_policy("kiro", "spend", today=TODAY)
    assert effective == "exclude"  # removing only the subscribed key


def test_profile_flag_roundtrip_and_clear():
    policy.set_profile_subscribed("kiro-sol", False)
    assert policy.get_profile_subscribed("kiro-sol") == (False, None)
    policy.set_profile_subscribed("kiro-sol", True)
    assert policy.get_profile_subscribed("kiro-sol") == (True, None)
    policy.set_profile_subscribed("kiro-sol", None)
    assert policy.get_profile_subscribed("kiro-sol") == (None, None)


def test_profile_true_overrides_pool_false():
    policy.set_subscribed("kiro", False)
    policy.set_profile_subscribed("kiro-sol", True)
    sub = profile_subscription("kiro-sol", "kiro")
    assert sub.subscribed is True
    assert sub.source == "profile"
    # A sibling with no override still falls to the pool flag.
    assert profile_subscription("kiro-haiku", "kiro").subscribed is False


def test_profile_false_overrides_pool_true():
    policy.set_subscribed("kiro", True)
    policy.set_profile_subscribed("kiro-haiku", False)
    sub = profile_subscription("kiro-haiku", "kiro")
    assert sub.subscribed is False
    assert sub.source == "profile"
    assert sub.key == "profiles.kiro-haiku"
    assert profile_subscription("kiro-sol", "kiro").subscribed is True


def test_profile_clear_falls_back_to_pool():
    policy.set_subscribed("kiro", False)
    policy.set_profile_subscribed("kiro-sol", True)
    assert profile_subscription("kiro-sol", "kiro").subscribed is True
    policy.set_profile_subscribed("kiro-sol", None)
    assert profile_subscription("kiro-sol", "kiro").subscribed is False


def test_nonbool_value_is_invalid_and_falls_through():
    """A typo must not silently flip the flag — invalid values are ignored,
    reported via the status field, and the next level decides."""
    path = policy.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[pools.kiro]\nsubscribed = "yes"\n', encoding="utf-8")
    value, status = policy.get_subscribed("kiro")
    assert value is True
    assert status and "invalid" in status
    # Invalid at profile level falls through to the pool level.
    path.write_text(
        '[profiles.kiro-sol]\nsubscribed = "no"\n[pools.kiro]\nsubscribed = false\n',
        encoding="utf-8",
    )
    sub = profile_subscription("kiro-sol", "kiro")
    assert sub.subscribed is False
    assert sub.source == "pool"
    assert sub.status and "invalid" in sub.status


def test_alias_spelling_resolves_to_same_entity():
    """codex-max is an alias of codex-sol: flagging the alias flags the
    canonical name and vice versa; the canonical key wins when both speak."""
    policy.set_profile_subscribed("codex-max", False)
    assert profile_subscription("codex-sol", "codex").subscribed is False
    assert profile_subscription("codex-max", "codex").subscribed is False
    # Canonical key outranks the alias key (canonical is checked first).
    policy.set_profile_subscribed("codex-sol", True)
    sub = profile_subscription("codex-sol", "codex")
    assert sub.subscribed is True
    assert sub.key == "profiles.codex-sol"


# ---------------------------------------------------------------------------
# policy CLI surface + policy list
# ---------------------------------------------------------------------------


def test_policy_cli_pool_flag_set_and_none(capsys):
    assert cli.main(["policy", "set", "kiro", "--subscribed", "off"]) == 0
    assert "subscribed=False" in capsys.readouterr().out
    assert policy.get_subscribed("kiro") == (False, None)
    assert cli.main(["policy", "set", "kiro", "--subscribed", "none"]) == 0
    assert policy.get_subscribed("kiro") == (True, None)


def test_policy_cli_set_requires_something(capsys):
    with pytest.raises(SystemExit):
        cli.main(["policy", "set", "kiro"])
    assert "하나는 지정" in capsys.readouterr().err


def test_policy_cli_profile_flag(capsys):
    assert cli.main(["policy", "profile", "kiro-opus", "off"]) == 0
    assert policy.get_profile_subscribed("kiro-opus") == (False, None)
    assert cli.main(["policy", "profile", "kiro-opus", "clear"]) == 0
    assert policy.get_profile_subscribed("kiro-opus") == (None, None)


def test_policy_list_marks_pool_and_lists_profile_overrides(capsys, monkeypatch):
    policy.set_subscribed("kiro", False)
    policy.set_profile_subscribed("kiro-opus", True)
    policy.set_profile_subscribed("no-such-profile", False)
    assert cli.main(["policy", "list"]) == 0
    out = capsys.readouterr().out
    kiro_line = next(line for line in out.splitlines() if line.startswith("kiro"))
    assert "unsubscribed" in kiro_line
    assert "profiles:" in out
    opus_line = next(line for line in out.splitlines() if line.strip().startswith("kiro-opus"))
    assert "subscribed=true" in opus_line
    bogus_line = next(line for line in out.splitlines() if line.strip().startswith("no-such-profile"))
    assert "unknown profile" in bogus_line


# ---------------------------------------------------------------------------
# recommend — candidates, escalation, emergency candidates, fold line
# ---------------------------------------------------------------------------


def test_recommend_pool_flag_removes_profiles_and_folds():
    out = recommend.recommend(_healthy_s_plus_providers(), "S+", today=TODAY, now=NOW)
    names = _candidate_names(out)
    # Positive control: unflagged, both kiro S+ profiles are listed.
    assert "kiro-sol" in names
    policy.set_subscribed("kiro", False)
    out = recommend.recommend(_healthy_s_plus_providers(), "S+", today=TODAY, now=NOW)
    names = _candidate_names(out)
    assert "kiro-sol" not in names
    assert "kiro-opus" not in names
    assert "codex-sol" in names  # sibling pools unaffected
    assert "kiro 풀 구독 해지" in out
    assert "구독 해지" in out


def test_recommend_profile_flag_marks_fold_with_profile_name():
    policy.set_profile_subscribed("kiro-sol", False)
    out = recommend.recommend(_healthy_s_plus_providers(), "S+", today=TODAY, now=NOW)
    names = _candidate_names(out)
    assert "kiro-sol" not in names
    assert "kiro-opus" in names  # only the flagged profile dropped
    assert "프로필 kiro-sol 구독 해지" in out


def test_recommend_pool_flag_never_emergency_candidate():
    """The ⚠ 비상 후보 block renders policy_excluded rows — an unsubscribed
    profile must never ride that path even when nothing else is left."""
    policy.set_policy("codex", "exclude", until=dt.date(2099, 8, 31), note="operator")
    policy.set_subscribed("kiro", False)
    providers = [
        ProviderResult(id="claude", error="HTTP 503"),  # nothing measurable
        _result("codex", 10.0, pool_class="preserve"),  # measured — reaches the exclude check
    ]
    out = recommend.recommend(providers, "S+", today=TODAY, now=NOW)
    assert "비상 후보" in out  # codex rows surface as emergency candidates
    # The emergency block ends where the fold lines begin — inside the block
    # itself no unsubscribed name may appear. Fold lines below may still name
    # them (that is the visible marker, not a candidacy).
    lines = out.splitlines()
    idx = next(i for i, line in enumerate(lines) if "비상 후보" in line)
    block: list[str] = []
    for line in lines[idx + 1 :]:
        if line.startswith("✗") or line.startswith("⚠"):
            break
        block.append(line)
    assert block, "emergency-candidate rows expected"
    assert all("kiro" not in line for line in block)


def test_recommend_unsubscribed_escalation_row_absent():
    """An escalation-gated row on an unsubscribed pool never reaches the
    escalation section either."""
    table = {
        "C": [
            Profile("kiro-cheap", "Kiro cheap", 10.0),
            Profile("oc-omni", "Omni", 5.0, gate="escalation", gate_reason="esc"),
        ],
        "S+": [],
        "S": [],
        "A+": [],
        "A": [],
        "B": [],
    }
    providers = [
        _result("kiro", 10.0, pool_class="spend", window="30d"),
        _result("omniroute", 10.0, pool_class="spend", window="30d"),
    ]
    # Control: unflagged, oc-omni renders in the escalation section.
    control = recommend.recommend(providers, "C", today=TODAY, now=NOW, grade_table=table)
    assert "승급 후보" in control
    assert "oc-omni" in control
    policy.set_subscribed("omniroute", False)
    out = recommend.recommend(providers, "C", today=TODAY, now=NOW, grade_table=table)
    assert "oc-omni" not in _candidate_names(out)
    assert "승급 후보" not in out  # the section never lists it
    assert "omniroute 풀 구독 해지" in out


def test_gate_alternatives_never_suggest_unsubscribed():
    """A refusal on one profile must not name an unsubscribed same-grade
    profile as the alternative."""
    policy.set_subscribed("kiro", False)
    providers = [
        _result("codex", 95.0, pool_class="preserve"),  # codex-max exhausted
        _result("kiro", 5.0, pool_class="spend", window="30d"),
        _result("claude", 95.0, pool_class="preserve"),  # opus also exhausted
    ]
    result = gate_check(providers, "codex-max", today=TODAY, now=NOW)
    assert result.ok is False
    for name in result.alternatives:
        assert not name.startswith("kiro")


# ---------------------------------------------------------------------------
# gate — exit 6, reason, precedence over quota/operator-request/E6
# ---------------------------------------------------------------------------


def test_gate_pool_flag_refuses_before_quota():
    """The flag refuses even a fully healthy pool — and the reason is not a
    quota answer (no used_pct, no alternatives)."""
    policy.set_subscribed("kiro", False)
    result = gate_check(_healthy_s_plus_providers(), "kiro-sol", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unsubscribed is True
    assert result.unmeasurable is False
    assert result.role_denied is False
    assert result.used_pct is None
    assert result.alternatives == ()
    assert "unsubscribed" in result.reason
    assert "pools.kiro" in result.reason


def test_gate_profile_flag_refuses_with_profile_key():
    policy.set_profile_subscribed("kiro-sol", False)
    result = gate_check(_healthy_s_plus_providers(), "kiro-sol", today=TODAY, now=NOW)
    assert result.unsubscribed is True
    assert "profiles.kiro-sol" in result.reason
    # The sibling profile still passes normally.
    other = gate_check(_healthy_s_plus_providers(), "kiro-opus", today=TODAY, now=NOW)
    assert other.unsubscribed is False


def test_gate_operator_request_never_reopens_unsubscribed():
    """#461 stands: a request-time flag cannot skip the subscription check —
    the refusal lands before operator-request validation runs."""
    policy.set_subscribed("kiro", False)
    result = gate_check(
        _healthy_s_plus_providers(),
        "kiro-sol",
        today=TODAY,
        now=NOW,
        operator_request="hk:task/742",
        requested_by="operator",
    )
    assert result.unsubscribed is True
    assert result.ok is False


def test_gate_cli_exit_6_and_stderr_not_quota(monkeypatch, capsys):
    policy.set_subscribed("kiro", False)
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"kiro": lambda: _result("kiro", 10.0, window="30d")},
    )
    rc = cli.main(["gate", "-m", "kiro-sol", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 6
    assert "unsubscribed" in out.err
    assert "구독 해지" in out.err
    assert "대안" not in out.err  # no same-grade alternatives line


def test_gate_cli_exit_6_alias_spelling(monkeypatch, capsys):
    """Flagging [profiles.codex-max] refuses `gate -m codex-sol` — the alias
    and the canonical name are the same entity."""
    policy.set_profile_subscribed("codex-max", False)
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"codex": lambda: _result("codex", 10.0, pool_class="preserve")},
    )
    rc = cli.main(["gate", "-m", "codex-sol", "--no-cache"])
    out = capsys.readouterr()
    assert rc == 6
    assert "unsubscribed" in out.err


def test_gate_output_record_carries_unsubscribed(monkeypatch, capsys, tmp_path):
    policy.set_subscribed("kiro", False)
    record_path = tmp_path / "gate.json"
    monkeypatch.setattr(
        cli,
        "registry",
        lambda: {"kiro": lambda: _result("kiro", 10.0, window="30d")},
    )
    rc = cli.main(["gate", "-m", "kiro-sol", "--no-cache", "--gate-output", str(record_path)])
    assert rc == 6
    record = json.loads(record_path.read_text())
    assert record["unsubscribed"] is True
    assert record["exit_code"] == 6


def test_gate_restore_returns_normal_verdict():
    """Flipping the flag back restores eligibility with the history intact —
    the refusal was a flag, never a deletion."""
    policy.set_subscribed("kiro", False)
    blocked = gate_check(_healthy_s_plus_providers(), "kiro-sol", today=TODAY, now=NOW)
    assert blocked.unsubscribed is True
    policy.set_subscribed("kiro", True)
    restored = gate_check(_healthy_s_plus_providers(), "kiro-sol", today=TODAY, now=NOW)
    assert restored.unsubscribed is False
    assert restored.ok is True


def test_gate_unsubscribed_takes_precedence_over_exhausted_pool():
    """Even a 100%-used pool answers 'unsubscribed', not '소진' — the reader
    must never be told a reset will help."""
    policy.set_subscribed("kiro", False)
    providers = [_result("kiro", 99.9, pool_class="spend", window="30d")]
    result = gate_check(providers, "kiro-sol", today=TODAY, now=NOW)
    assert result.unsubscribed is True
    assert "소진" not in result.reason


def test_gate_unsubscribed_takes_precedence_over_provider_error():
    policy.set_subscribed("kiro", False)
    providers = [ProviderResult(id="kiro", error="HTTP 503")]
    result = gate_check(providers, "kiro-sol", today=TODAY, now=NOW)
    assert result.unsubscribed is True
    assert result.unmeasurable is False


def test_gate_unsubscribed_before_e6_rung_resolution():
    """The flag refuses before rung/E6 logic runs — an arm marker naming the
    rung cannot reopen an unsubscribed pool."""
    policy.set_subscribed("grok", False)
    result = gate_check(
        [_result("grok", 10.0)],
        "grok-hi",
        today=TODAY,
        now=NOW,
        effort="xhigh",
        e6_arm="grok-hi@xhigh",
    )
    assert result.unsubscribed is True
    assert result.e6_arm is None  # never reached the rung-resolution block


# ---------------------------------------------------------------------------
# launch — refuse on every path, marker never reopens
# ---------------------------------------------------------------------------


def test_launch_refuses_unsubscribed_profile():
    policy.set_subscribed("kiro", False)
    with pytest.raises(launch.LaunchError, match="unsubscribed"):
        launch.resolve_launch("kiro-sol")


def test_launch_refuses_via_alias_flag():
    """Flagging the alias closes the canonical spelling on the launch path."""
    policy.set_profile_subscribed("codex-max", False)
    with pytest.raises(launch.LaunchError, match="unsubscribed"):
        launch.resolve_launch("codex-sol")
    with pytest.raises(launch.LaunchError, match="unsubscribed"):
        launch.resolve_launch("codex-max")


def test_launch_operator_request_never_reopens_unsubscribed():
    policy.set_subscribed("claude", False)
    with pytest.raises(launch.LaunchError, match="unsubscribed"):
        launch.resolve_launch("opus", operator_request=True)


def test_launch_e6_arm_never_reopens_unsubscribed():
    """An E6 arm marker widens exactly one C rung — it is not a way to admit a
    profile whose subscription flag says no."""
    policy.set_subscribed("grok", False)
    with pytest.raises(launch.LaunchError, match="unsubscribed"):
        launch.resolve_launch("grok-hi", effort="xhigh", e6_arm="grok-hi@xhigh")


def test_launch_restore_reopens():
    policy.set_subscribed("kiro", False)
    with pytest.raises(launch.LaunchError):
        launch.resolve_launch("kiro-sol")
    policy.set_subscribed("kiro", True)
    decision = launch.resolve_launch("kiro-sol")
    assert decision.profile == "kiro-sol"


# ---------------------------------------------------------------------------
# list/catalog/status surfaces — rows stay, marked
# ---------------------------------------------------------------------------


def test_list_recommend_profiles_marks_unsubscribed(capsys):
    policy.set_subscribed("kiro", False)
    assert cli.main(["--list-recommend-profiles"]) == 0
    out = capsys.readouterr().out
    sol_line = next(line for line in out.splitlines() if line.startswith("kiro-sol"))
    assert sol_line.split()[0] == "kiro-sol"  # first token stays the name
    assert "[unsubscribed]" in sol_line
    opus_line = next(line for line in out.splitlines() if line.startswith("opus"))
    assert "[unsubscribed]" not in opus_line


def test_catalog_report_marks_unsubscribed_rows():
    policy.set_subscribed("kiro", False)
    text = bench.catalog_report()
    kiro_rows = [line for line in text.splitlines() if "kiro-" in line]
    assert kiro_rows, "catalog rows must still be listed"
    assert all("unsubscribed" in line for line in kiro_rows)
    # Pool rows are kept — this is a marker, not a deletion.
    assert "pool=kiro" in text


def test_catalog_report_profile_override_unmarks_one_row():
    policy.set_subscribed("kiro", False)
    policy.set_profile_subscribed("kiro-opus", True)
    text = bench.catalog_report()
    for line in text.splitlines():
        cols = line.split()
        if len(cols) > 1 and cols[1].split("@")[0] == "kiro-opus":
            assert "unsubscribed" not in line
    assert any("kiro-sol" in line and "unsubscribed" in line for line in text.splitlines())


def test_status_table_marks_unsubscribed_pool():
    results = [_result("kiro", 10.0, window="30d"), _result("codex", 10.0)]
    plain = render.table(results, color=False, now=NOW)
    assert "[unsubscribed]" not in plain
    policy.set_subscribed("kiro", False)
    marked = render.table(results, color=False, now=NOW)
    kiro_line = next(line for line in marked.splitlines() if line.startswith("kiro"))
    assert "[unsubscribed]" in kiro_line
    codex_line = next(line for line in marked.splitlines() if line.startswith("codex"))
    assert "[unsubscribed]" not in codex_line


# ---------------------------------------------------------------------------
# grades / reps — history keeps working, marked not suppressed
# ---------------------------------------------------------------------------


def _entry(profile: str, effort: str, grade: str, **overrides) -> bench.CatalogEntry:
    fields = {
        "profile": profile,
        "effort": effort,
        "model_id": "model-x",
        "pool": "test",
        "grade": grade,
    }
    fields.update(overrides)
    return bench.CatalogEntry(**fields)


def _view(*entries: bench.CatalogEntry) -> bench.CatalogView:
    return bench.CatalogView(
        entries=tuple(entries),
        source=bench.CATALOG_SOURCE_SNAPSHOT,
        backend=bench.BENCH_BACKEND_LOCAL,
        reason="auto-local",
    )


def test_grades_propose_evaluates_and_marks_unsubscribed_rung(tmp_path, isolated_cache):
    """A flagged rung keeps its evidence and still produces proposals — the
    flag marks the line, it does not suppress the grade record."""
    view = _view(_entry("grok-hi", "xhigh", "C", pool="grok"))
    for ref, grade in (("t1", "A+"), ("t2", "A+")):
        bench.add_rep(
            profile="builder-grok",
            model_id="grok-4.7",
            task_ref=ref,
            tier="T1",
            role="impl",
            rounds=1,
            blockers_found=0,
            completed=1,
            recorded_at="2026-09-20T10:00:00Z",
            effort="xhigh",
            grade=grade,
        )
    evidence = grades.gather_reps(view=view, host="test-host")
    proposal = grades.evaluate(evidence, view)
    result = next(r for r in proposal.results if r.key == ("grok-hi", "xhigh"))
    assert result.action == "promote"  # evaluated exactly as before
    clean = grades.render_proposal(proposal, view)
    assert "[unsubscribed]" not in clean  # control: unflagged renders unmarked
    policy.set_subscribed("grok", False)
    marked = grades.render_proposal(proposal, view)
    promote_line = next(line for line in marked.splitlines() if "grok-hi" in line and "->" in line)
    assert "[unsubscribed]" in promote_line
    # Evidence rows stay in the render — the flag hides nothing.
    assert "local:" in marked


def test_reps_add_list_unaffected_by_flag(capsys):
    """reps write/read never consult the flag — history is not dispatch."""
    policy.set_subscribed("kiro", False)
    rc = cli.main(
        [
            "reps",
            "add",
            "--profile",
            "kiro-sol",
            "--model",
            "gpt-5.6-sol",
            "--task",
            "t742",
            "--tier",
            "T2",
            "--role",
            "impl",
            "--grade",
            "A+",
            "--rounds",
            "1",
            "--blockers-found",
            "0",
            "--completed",
            "1",
        ]
    )
    assert rc == 0
    assert cli.main(["reps", "list", "--profile", "kiro-sol"]) == 0
    out = capsys.readouterr().out
    assert "kiro-sol" in out


# ---------------------------------------------------------------------------
# assertion-RED mutants — proving the checks are actually wired
# ---------------------------------------------------------------------------


def test_mutantrd_filter_removed_would_recommend_unsubscribed():
    """Removing the recommend filter puts kiro rows back in the candidate
    lines: a mutant that drops the check fails this test."""
    policy.set_subscribed("kiro", False)
    out = recommend.recommend(_healthy_s_plus_providers(), "S+", today=TODAY, now=NOW)
    assert "kiro-sol" not in _candidate_names(out)


def test_mutantrd_gate_without_check_would_say_ok():
    """Dropping the gate check turns this refusal into an allow (healthy
    pool) — the flag must be what produces the refusal."""
    policy.set_subscribed("kiro", False)
    result = gate_check(_healthy_s_plus_providers(), "kiro-sol", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.unsubscribed is True


def test_mutantrd_launch_without_check_would_resolve():
    """Without the launch check this resolves a launch decision — the flag
    must be what raises."""
    policy.set_subscribed("kiro", False)
    with pytest.raises(launch.LaunchError):
        launch.resolve_launch("kiro-sol")


def test_mutantrd_profile_override_ignored_would_stay_closed():
    """A mutant that reads only the pool flag never sees this restore."""
    policy.set_subscribed("kiro", False)
    policy.set_profile_subscribed("kiro-sol", True)
    result = gate_check(_healthy_s_plus_providers(), "kiro-sol", today=TODAY, now=NOW)
    assert result.unsubscribed is False
    assert result.ok is True
