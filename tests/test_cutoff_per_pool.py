"""task #638 — [pools.<p>] cutoff / on_exhaust per-pool config."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from scopefuel import exhaust, policy
from scopefuel.model import Bucket, ProviderResult, Scope
from scopefuel.recommend import (
    PRESERVE_EXCLUDE_PCT,
    SPEND_EXCLUDE_PCT,
    gate_check,
    recommend,
)

TODAY = dt.date(2026, 7, 31)
NOW = dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=dt.UTC)


def _reset_almost_full(window: str) -> str:
    hours = {"5h": 4.9, "1d": 23.5, "7d": 167.0, "30d": 719.0}.get(window, 167.0)
    return (NOW + dt.timedelta(hours=hours)).isoformat()


def _result(
    provider_id: str,
    used: float,
    pool_class: str = "spend",
    window: str = "7d",
    scope: Scope | None = None,
    reset_at: str | None = None,
) -> ProviderResult:
    return ProviderResult(
        id=provider_id,
        pool_class=pool_class,  # type: ignore[arg-type]
        buckets=[
            Bucket(
                label=window,
                window=window,
                used_pct=used,
                resets_at=reset_at or _reset_almost_full(window),
                scope=scope or Scope("account"),
                horizon="week",  # type: ignore[arg-type]
            )
        ],
    )


def _live_result(provider_id: str, used: float) -> ProviderResult:
    """실제 시계 기준 미래 reset — herdr 경로는 실시간(now=now())으로 판정한다."""
    reset_at = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=3)).isoformat()
    return _result(provider_id, used, reset_at=reset_at)


def _write_config(tmp_path, text: str) -> None:
    path = tmp_path / "config" / "scopefuel" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _fake_sink(calls: list[str]):
    def _send(text: str, event_id: str = "") -> bool:
        calls.append(text)
        return True

    return _send


# ------------------------------------------------------------------ cutoff


def test_default_cutoff_99_unchanged_no_config():
    """기본값 회귀: 설정 없으면 spend 99% 차단선 그대로."""
    result = gate_check([_result("codex", SPEND_EXCLUDE_PCT)], "codex-terra", today=TODAY, now=NOW)
    assert result.ok is False
    assert result.used_pct == SPEND_EXCLUDE_PCT
    assert "99% 사용 · 1% 남음 · 차단선 99%" in result.reason

    ok = gate_check([_result("codex", SPEND_EXCLUDE_PCT - 0.1)], "codex-terra", today=TODAY, now=NOW)
    assert ok.ok is True


def test_no_config_no_notify_state_file(tmp_path, monkeypatch):
    """설정 없음 → 알림 싱크 미호출, dedup 상태 파일도 생기지 않는다(무접촉)."""
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(calls))
    result = gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    assert result.ok is False
    assert calls == []
    assert result.exhaust_notice is None
    assert not (tmp_path / "exhaust-notify.json").exists()


def test_codex_cutoff_100_passes_99_blocks_100(tmp_path):
    """codex cutoff=100 — 99% 는 통과, 100% 는 차단."""
    _write_config(tmp_path, "[pools.codex]\ncutoff = 100\n")
    ok = gate_check([_result("codex", 99.5)], "codex-terra", today=TODAY, now=NOW)
    assert ok.ok is True

    blocked = gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    assert blocked.ok is False
    assert "100% 사용 · 0% 남음 · 차단선 100%" in blocked.reason


def test_pool_cutoff_lowers_threshold(tmp_path):
    """반대 방향도 동작 — cutoff=80 이면 85% 에서 차단."""
    _write_config(tmp_path, "[pools.codex]\ncutoff = 80\n")
    blocked = gate_check([_result("codex", 85.0)], "codex-terra", today=TODAY, now=NOW)
    assert blocked.ok is False
    assert "차단선 80%" in blocked.reason


@pytest.mark.parametrize("raw", ["150", "-1", '"abc"', "true", "nan", "inf"])
def test_invalid_cutoff_rejected_falls_back_to_default(tmp_path, raw):
    """오타·범위 밖·비수치 cutoff 는 거부 — 조용히 0/100 이 되지 않고 기본값 폴백."""
    _write_config(tmp_path, f"[pools.codex]\ncutoff = {raw}\n")
    cutoff, status = policy.get_cutoff("codex", SPEND_EXCLUDE_PCT)
    assert cutoff == SPEND_EXCLUDE_PCT
    assert status is not None and "invalid cutoff" in status

    # 게이트도 기본 99% 차단선으로 판정하고 폴백 사유를 노출한다.
    result = gate_check([_result("codex", 99.5)], "codex-terra", today=TODAY, now=NOW)
    assert result.ok is False
    assert "invalid cutoff" in result.reason


def test_cutoff_boundary_values_accepted(tmp_path):
    """0 과 100 은 유효 경계값."""
    _write_config(tmp_path, "[pools.codex]\ncutoff = 0\n[pools.grok]\ncutoff = 100\n")
    assert policy.get_cutoff("codex", SPEND_EXCLUDE_PCT) == (0.0, None)
    assert policy.get_cutoff("grok", SPEND_EXCLUDE_PCT) == (100.0, None)


def test_config_round_trip_preserves_cutoff_and_on_exhaust(tmp_path):
    """policy set 재작성이 cutoff/on_exhaust 를 조용히 버리지 않는다."""
    _write_config(tmp_path, '[pools.codex]\ncutoff = 100\non_exhaust = "operator-switch"\n')
    policy.set_policy("codex", "spend", until=dt.date(2099, 1, 1))
    config = policy.load_config()
    entry = config["pools"]["codex"]
    assert entry["cutoff"] == 100
    assert entry["on_exhaust"] == "operator-switch"
    assert policy.get_on_exhaust("codex") == ("operator-switch", None)


def test_config_round_trip_invalid_cutoff_stays_invalid(tmp_path):
    """bool cutoff 는 재작성 시에도 TOML 을 깨지 않고 무효 상태를 유지한다."""
    _write_config(tmp_path, "[pools.codex]\ncutoff = true\n")
    policy.set_policy("codex", "spend", until=dt.date(2099, 1, 1))
    cutoff, status = policy.get_cutoff("codex", SPEND_EXCLUDE_PCT)
    assert cutoff == SPEND_EXCLUDE_PCT
    assert status is not None


# ------------------------------------------------------------------ on_exhaust


def test_operator_switch_notifies_once_per_episode(tmp_path, monkeypatch):
    """소진 진입 1회 발송 → 같은 에피소드 재관측 억제 → 회복 후 재소진은 재발송."""
    _write_config(tmp_path, '[pools.codex]\ncutoff = 100\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(calls))

    blocked = gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    assert blocked.ok is False
    assert len(calls) == 1
    assert "계정 전환 필요" in calls[0]
    assert "codex" in calls[0]
    assert "알림 발송" in blocked.reason
    assert blocked.exhaust_notice is not None

    # 같은 소진 창의 재판정 — 중복 발송 없음.
    again = gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    assert again.ok is False
    assert len(calls) == 1
    assert "기통지" in again.reason

    # 계정 전환/리셋으로 차단선 아래 관측 → 플래그 해제.
    ok = gate_check([_result("codex", 5.0)], "codex-terra", today=TODAY, now=NOW)
    assert ok.ok is True

    # 새 소진 에피소드 → 다시 1회 발송.
    blocked2 = gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    assert blocked2.ok is False
    assert len(calls) == 2


def test_operator_switch_block_mode_default_silent(tmp_path, monkeypatch):
    """on_exhaust 미설정(기본 block) — 차단은 하되 알림 없음."""
    _write_config(tmp_path, "[pools.codex]\ncutoff = 100\n")
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(calls))
    result = gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    assert result.ok is False
    assert calls == []
    assert "계정 전환" not in result.reason


def test_on_exhaust_invalid_falls_back_to_block(tmp_path, monkeypatch):
    """on_exhaust 오타는 block 폴백 — 잘못된 값으로 알림이 나가지 않는다."""
    _write_config(tmp_path, '[pools.codex]\ncutoff = 100\non_exhaust = "operaotr-switch"\n')
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(calls))
    result = gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    assert result.ok is False
    assert calls == []
    assert "invalid on_exhaust" in result.reason


def test_notify_failure_cooldown_then_retry(tmp_path, monkeypatch):
    """발송 실패는 attempted_at 만 기록 — 쿨다운 안 재관측은 억제, 이후 재시도.

    gate 한 번이 대안 평가(recommend)를 거쳐 같은 소진을 여러 번 관측하므로,
    실패 싱크가 호출 수를 곱하지 않음을 함께 고정한다.
    """
    _write_config(tmp_path, '[pools.codex]\ncutoff = 100\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", lambda text, event_id="": calls.append(text) or False)

    first = gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    assert first.ok is False
    assert "발송 실패" in first.reason
    assert len(calls) == 1  # 한 gate 호출 안의 재관측은 싱크를 다시 치지 않는다

    # 쿨다운 안 — 발송 없이 "재시도 대기" 상태만 표시.
    soon = gate_check(
        [_result("codex", 100.0)],
        "codex-terra",
        today=TODAY,
        now=NOW + dt.timedelta(seconds=30),
    )
    assert soon.ok is False
    assert len(calls) == 1
    assert "재시도 대기" in soon.reason

    # 쿨다운 경과 — 재시도가 나간다.
    retry_calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(retry_calls))
    later = gate_check(
        [_result("codex", 100.0)],
        "codex-terra",
        today=TODAY,
        now=NOW + dt.timedelta(seconds=61),
    )
    assert later.ok is False
    assert len(retry_calls) == 1
    assert "발송 실패" not in later.reason


def test_sink_exception_does_not_break_gate(tmp_path, monkeypatch):
    """싱크가 예외를 던져도 게이트 판정은 그대로."""
    _write_config(tmp_path, '[pools.codex]\ncutoff = 100\non_exhaust = "operator-switch"\n')

    def boom(text: str, event_id: str = "") -> bool:
        raise RuntimeError("wedged daemon")

    monkeypatch.setattr(exhaust, "emit_lane_event", boom)
    result = gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    assert result.ok is False
    assert "발송 실패" in result.reason


# ------------------------------------------------------------------ display


def test_exhaust_display_shows_used_remaining_cutoff(tmp_path):
    """범위3 문구: 'N% 사용 · M% 남음 · 차단선 K%' — 남은 양이 오해 없이 보인다."""
    out = recommend([_result("codex", 99.0)], "A+", today=TODAY, now=NOW)
    line = next(line for line in out.splitlines() if line.startswith("✗") and "codex" in line)
    assert "99% 사용" in line
    assert "1% 남음" in line
    assert "차단선 99%" in line

    gate = gate_check([_result("codex", 99.0)], "codex-terra", today=TODAY, now=NOW)
    assert "99% 사용 · 1% 남음 · 차단선 99%" in gate.reason


def test_preserve_cutoff_unchanged_at_90(tmp_path):
    """preserve class 기본 90% — per-pool cutoff 가 건드리지 않는다."""
    blocked = gate_check(
        [_result("claude", PRESERVE_EXCLUDE_PCT, pool_class="preserve")],
        "opus",
        today=TODAY,
        now=NOW,
    )
    assert blocked.ok is False
    assert "차단선 90%" in blocked.reason


# ------------------------------------------------------------------ #461 불변


def test_operator_request_does_not_bypass_configured_cutoff(tmp_path):
    """#461: --operator-request 는 설정된 cutoff 도 우회하지 못한다.

    escalation 프로필의 유효한 요청이 '대안 가용' 거부를 건너뛰어도
    (escalation_override=True) 설정된 차단선 검사는 그대로 적용된다.
    """
    from scopefuel.recommend import Profile

    _write_config(tmp_path, "[pools.codex]\ncutoff = 100\n")
    table = {
        "S+": [
            Profile("kiro-normal", "Test Normal", 60.0),
            Profile("codex-esc", "Test Escalation", 55.0, gate="escalation", gate_reason="t"),
        ],
        "S": [],
        "A+": [],
        "A": [],
        "B": [],
        "C": [],
    }
    providers = [
        _result("codex", 100.0),  # configured cutoff 도달
        _result("kiro", 10.0, pool_class="spend", window="30d"),  # 정상 대안 가용
    ]
    result = gate_check(
        providers,
        "codex-esc",
        today=TODAY,
        now=NOW,
        operator_request="hk:task/461",
        requested_by="operator",
        grade_table=table,
    )
    assert result.ok is False
    assert result.escalation_override is True  # '대안 가용' 거부만 건너뜀
    assert "차단선 100%" in result.reason


# ------------------------------------------------------------------ manual fallback guard


def test_manual_confirmed_cutoff_honors_pool_config(tmp_path):
    """manual fallback 가드도 같은 풀별 cutoff 를 쓴다."""
    from scopefuel import manual

    _write_config(tmp_path, "[pools.codex]\ncutoff = 100\n")
    assert manual.confirmed_automatic_cutoff(_result("codex", 99.5), now=NOW) is None
    breach = manual.confirmed_automatic_cutoff(_result("codex", 100.0), now=NOW)
    assert breach == (100.0, 100.0)


# ------------------------------------------------------------------ exhaust.observe 단위


def test_observe_episode_transitions(tmp_path):
    """dedup 상태 전이: 소진→기록 / 재소진→억제 / 회복→해제 / 재소진→재발송."""
    _write_config(tmp_path, '[pools.codex]\non_exhaust = "operator-switch"\n')
    calls: list[str] = []

    status = exhaust.observe(
        "codex", exhausted=True, used_pct=100.0, cutoff=100.0, now=NOW, sink=_fake_sink(calls)
    )
    assert status is not None and "알림 발송" in status
    assert len(calls) == 1

    status = exhaust.observe(
        "codex", exhausted=True, used_pct=100.0, cutoff=100.0, now=NOW, sink=_fake_sink(calls)
    )
    assert status is not None and "기통지" in status
    assert len(calls) == 1

    assert exhaust.observe("codex", exhausted=False, now=NOW, sink=_fake_sink(calls)) is None

    status = exhaust.observe(
        "codex", exhausted=True, used_pct=100.0, cutoff=100.0, now=NOW, sink=_fake_sink(calls)
    )
    assert status is not None and "알림 발송" in status
    assert len(calls) == 2


def test_scope_keyed_episodes_do_not_cross_clear(tmp_path):
    """같은 풀의 다른 scope(agy group 등)는 독립 에피소드 — 한쪽 회복이 다른 쪽을 지우지 않는다."""
    _write_config(tmp_path, '[pools.agy]\non_exhaust = "operator-switch"\n')
    calls: list[str] = []

    status = exhaust.observe(
        "agy",
        exhausted=True,
        used_pct=100.0,
        cutoff=99.0,
        scope="gemini",
        now=NOW,
        sink=_fake_sink(calls),
    )
    assert "알림 발송" in status
    # 다른 scope 의 건강 관측은 gemini 에피소드를 건드리지 않는다.
    assert exhaust.observe("agy", exhausted=False, scope="3p", now=NOW, sink=_fake_sink(calls)) is None
    # gemini 재관측 — 기통지 억제(재발송 없음)가 유지된다.
    status = exhaust.observe(
        "agy",
        exhausted=True,
        used_pct=100.0,
        cutoff=99.0,
        scope="gemini",
        now=NOW,
        sink=_fake_sink(calls),
    )
    assert "기통지" in status
    assert len(calls) == 1
    assert "scope=gemini" in calls[0]


def test_state_storage_failure_does_not_abort_gate(tmp_path, monkeypatch):
    """락/디렉토리 접근 실패는 게이트를 중단시키지 않는다 — 차단 + 실패 상태 표시."""
    _write_config(tmp_path, '[pools.codex]\ncutoff = 100\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(calls))

    def broken_lock():
        raise OSError("read-only fs")

    monkeypatch.setattr(exhaust, "_locked_state", broken_lock)
    result = gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    assert result.ok is False
    assert calls == []  # 저장소 없이는 dedup 보장 불가 — 발송하지 않는다
    assert "저장소 접근 불가" in result.reason


def test_state_save_failure_reports_record_loss(tmp_path, monkeypatch):
    """발송 성공 + 상태 기록 실패 → '기록 실패' 표시 + 같은 프로세스 안 억제."""
    _write_config(tmp_path, '[pools.codex]\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    sink = _fake_sink(calls)
    monkeypatch.setattr(exhaust, "_save_state", lambda state: False)

    status = exhaust.observe("codex", exhausted=True, used_pct=100.0, cutoff=99.0, now=NOW, sink=sink)
    assert "알림 발송(기록 실패)" in status
    assert len(calls) == 1

    # 파일엔 없지만 ephemeral dedup 이 같은 에피소드 재발송을 막는다.
    status = exhaust.observe("codex", exhausted=True, used_pct=100.0, cutoff=99.0, now=NOW, sink=sink)
    assert "기통지" in status
    assert len(calls) == 1
    exhaust._EPHEMERAL.clear()


def test_block_mode_recovery_clears_episode(tmp_path):
    """block 모드 동안의 회복 관측도 에피소드를 지운다 — 모드 재설정 후 재알림 가능."""
    _write_config(tmp_path, '[pools.codex]\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    sink = _fake_sink(calls)
    exhaust.observe("codex", exhausted=True, used_pct=100.0, cutoff=99.0, now=NOW, sink=sink)
    assert len(calls) == 1

    # on_exhaust 를 block(미설정)으로 바꾼 뒤 회복 관측 — 기록이 지워진다.
    _write_config(tmp_path, "[pools.codex]\ncutoff = 99\n")
    assert exhaust.observe("codex", exhausted=False, now=NOW, sink=sink) is None

    # operator-switch 재설정 → 새 소진은 다시 알린다(기통지 억제에 갇히지 않음).
    _write_config(tmp_path, '[pools.codex]\non_exhaust = "operator-switch"\n')
    status = exhaust.observe("codex", exhausted=True, used_pct=100.0, cutoff=99.0, now=NOW, sink=sink)
    assert "알림 발송" in status
    assert len(calls) == 2


def test_invalid_cutoff_status_visible_on_success(tmp_path):
    """무효 cutoff 는 통과 판정의 reason 에도 폴백 상태가 노출된다."""
    _write_config(tmp_path, "[pools.codex]\ncutoff = 150\n")
    result = gate_check([_result("codex", 50.0)], "codex-terra", today=TODAY, now=NOW)
    assert result.ok is True
    assert "invalid cutoff" in result.reason


def test_observe_state_file_shape(tmp_path):
    """dedup 상태는 소진 풀·창·시각을 기록한다(감사 가능)."""
    _write_config(tmp_path, '[pools.codex]\non_exhaust = "operator-switch"\n')
    exhaust.observe(
        "codex",
        exhausted=True,
        used_pct=100.0,
        cutoff=100.0,
        window="7d",
        reset_at="2026-08-01T00:00:00+00:00",
        now=NOW,
        sink=_fake_sink([]),
    )
    state = json.loads((tmp_path / "exhaust-notify.json").read_text())
    entry = state["codex"]
    assert entry["window"] == "7d"
    assert entry["reset_at"] == "2026-08-01T00:00:00+00:00"
    assert entry["notified_at"] == NOW.isoformat()


# ------------------------------------------------------------------ emit argv 계약


def _fake_panewire(tmp_path, monkeypatch, *, duplicate: bool = False):
    import stat

    argv_log = tmp_path / "panewire-argv.jsonl"
    shim = tmp_path / "panewire"
    body = (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "with open(os.environ['PANEWIRE_TEST_LOG'], 'a') as f:\n"
        "    f.write(json.dumps(argv) + '\\n')\n"
    )
    if duplicate:
        body += "sys.stderr.write('emit: duplicate event_id\\n')\nsys.exit(2)\n"
    else:
        body += (
            "if '--event-id' not in argv or not argv[argv.index('--event-id') + 1]:\n"
            "    sys.exit(2)\n"
            "sys.exit(0)\n"
        )
    shim.write_text(body)
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PANEWIRE_BIN", str(shim))
    monkeypatch.setenv("PANEWIRE_TEST_LOG", str(argv_log))
    return argv_log


def test_emit_lane_event_argv_contract(tmp_path, monkeypatch):
    """lane.event 는 --event-id 필수 — 스텁으로 argv 계약 회귀를 고정한다 (B1)."""
    argv_log = _fake_panewire(tmp_path, monkeypatch)
    assert exhaust.emit_lane_event("본문", "scopefuel.exhaust:codex:7d:-") is True
    argv = json.loads(argv_log.read_text().splitlines()[0])
    assert argv[:2] == ["emit", "--kind"]
    assert "lane.event" in argv and "--event-id" in argv
    assert "--sink" in argv and "--owner-lane" in argv
    # 빈 event-id 는 rc=2 → False (silent success 가 아니다)
    assert exhaust.emit_lane_event("본문", "") is False


def test_emit_lane_event_duplicate_event_id_counts_as_delivered(tmp_path, monkeypatch):
    """'duplicate event_id' rc=2 = 첫 발송이 이미 기록됨 → 전달 성공으로 친다."""
    _fake_panewire(tmp_path, monkeypatch, duplicate=True)
    assert exhaust.emit_lane_event("본문", "dup-id") is True


# ------------------------------------------------------------------ 경로별 관측 커버리지


def test_d3_gate_path_also_notifies(tmp_path, monkeypatch):
    """GRADE_TABLE 밖 프로필(D3) 경로도 같은 알림 상태 기계를 탄다."""
    _write_config(tmp_path, '[pools.agy]\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(calls))
    result = gate_check(
        [_result("agy", 100.0, window="30d", scope=Scope("group", "3p"))],
        "oc-oss",
        today=TODAY,
        now=NOW,
    )
    assert result.ok is False
    assert len(calls) == 1
    assert "agy (3p) 계정 전환 필요" in calls[0]


def test_recommend_render_path_notifies(tmp_path, monkeypatch):
    """recommend 렌더 평가 경로의 소진 관측도 알림을 올린다."""
    _write_config(tmp_path, '[pools.codex]\ncutoff = 100\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(calls))
    out = recommend([_result("codex", 100.0)], "A+", today=TODAY, now=NOW)
    assert len(calls) == 1
    assert "차단선 100%" in out


def test_recommend_respects_configured_cutoff(tmp_path):
    """recommend 렌더도 configured cutoff 를 쓴다 — 50 설정 시 60% 에서 제외."""
    _write_config(tmp_path, "[pools.codex]\ncutoff = 50\n")
    out = recommend([_result("codex", 60.0)], "A+", today=TODAY, now=NOW)
    line = next(line for line in out.splitlines() if "codex" in line and "소진" in line)
    assert "차단선 50%" in line


def test_escalation_entry_uses_configured_cutoff_and_notifies(tmp_path, monkeypatch):
    """escalation 엔트리 평가 경로도 configured cutoff + 알림을 적용한다."""
    from scopefuel.recommend import Profile

    _write_config(tmp_path, '[pools.codex]\ncutoff = 50\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(calls))
    table = {g: [] for g in ("S+", "S", "A", "B", "C")}
    table["A+"] = [Profile("codex-sol", "Sol", 65.0, gate="escalation", gate_reason="t")]
    out = recommend([_result("codex", 60.0)], "A+", today=TODAY, now=NOW, grade_table=table)
    assert "차단선 50%" in out
    assert len(calls) == 1


def test_gate_audit_record_includes_exhaust_notice(tmp_path, monkeypatch):
    """--gate-output 감사 레코드에 알림 상태가 들어간다."""
    from scopefuel import cli

    _write_config(tmp_path, '[pools.codex]\ncutoff = 100\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(calls))
    result = gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    record = cli._gate_record(result, 3, NOW)
    assert record["exhaust_notice"] == result.exhaust_notice
    assert "알림 발송" in record["exhaust_notice"]


# ------------------------------------------------------------------ herdr 경로


def _herdr_env(tmp_path, monkeypatch, provider: str = "codex"):
    from scopefuel import herdr

    monkeypatch.setattr(herdr, "_report", lambda pane_id, label: 0)
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "herdr-state"))
    monkeypatch.setenv(
        "HERDR_PLUGIN_EVENT_JSON",
        json.dumps({"data": {"pane_id": "wB:p9Z", "agent": provider}}),
    )
    return herdr


def test_herdr_event_notifies_exhausted_operator_switch_pool(tmp_path, monkeypatch):
    """실행 중 잡 경로: pane 이벤트의 소진 관측이 같은 알림 상태 기계를 탄다."""
    herdr = _herdr_env(tmp_path, monkeypatch)
    _write_config(tmp_path, '[pools.codex]\ncutoff = 100\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(calls))

    assert herdr.handle_event({"codex": lambda: _live_result("codex", 100.0)}) == 0
    assert len(calls) == 1
    assert "codex 계정 전환 필요" in calls[0]

    # 같은 에피소드 재관측 — 디바운스 아래라도 중복 발송 없음.
    herdr._save_state({})  # debounce 리셋 대신 상태 파일을 비워 재관측을 강제
    assert herdr.handle_event({"codex": lambda: _live_result("codex", 100.0)}) == 0
    assert len(calls) == 1


def test_herdr_stale_result_neither_notifies_nor_clears(tmp_path, monkeypatch):
    """stale 스냅샷은 알림 근거가 아니다 — 에피소드를 만들지도 지우지도 않는다."""
    import dataclasses

    herdr = _herdr_env(tmp_path, monkeypatch)
    _write_config(tmp_path, '[pools.codex]\ncutoff = 100\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    sink = _fake_sink(calls)

    # 선행 에피소드를 직접 만든다.
    exhaust.observe("codex", exhausted=True, used_pct=100.0, cutoff=100.0, now=NOW, sink=sink)
    assert len(calls) == 1

    stale = dataclasses.replace(_live_result("codex", 10.0), stale=True)
    assert herdr.handle_event({"codex": lambda: stale}) == 0
    assert len(calls) == 1  # stale 관측은 발송도 해제도 하지 않는다

    # 다음 fresh 소진 관측은 기통지 억제가 유지된다(에피소드가 안 지워졌다).
    # collect 의 TTL 캐시가 첫 호출의 stale 스냅샷을 돌려주지 않게 지운다.
    (tmp_path / "snapshots.json").unlink(missing_ok=True)
    herdr._save_state({})
    assert herdr.handle_event({"codex": lambda: _live_result("codex", 100.0)}) == 0
    assert len(calls) == 1


def test_herdr_expired_reset_does_not_notify(tmp_path, monkeypatch):
    """리셋 시각이 지난 버킷은 소진 근거가 아니다 — 오래된 측정이 알림을 오발하지 않는다."""
    import dataclasses

    herdr = _herdr_env(tmp_path, monkeypatch)
    _write_config(tmp_path, '[pools.codex]\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(calls))

    expired = _live_result("codex", 100.0)
    expired.buckets[0] = dataclasses.replace(expired.buckets[0], resets_at="2026-08-07T11:00:00+00:00")
    assert herdr.handle_event({"codex": lambda: expired}) == 0
    assert not calls  # 만료된 reset 의 소진 관측은 발송하지 않는다


def test_herdr_credential_scope_in_event_id(tmp_path, monkeypatch):
    """N6: herdr 경로는 credential 을 에피소드 scope 로 쓴다 — 계정별 독립 에피소드."""
    herdr = _herdr_env(tmp_path, monkeypatch)
    _write_config(tmp_path, '[pools.codex]\non_exhaust = "operator-switch"\n')
    ids: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", lambda text, event_id="": ids.append(event_id) or True)
    monkeypatch.setenv(
        "HERDR_PLUGIN_EVENT_JSON",
        json.dumps({"data": {"pane_id": "wB:p9Z", "agent": "codex", "env": {"CODEX_HOME": "/acct2"}}}),
    )
    assert herdr.handle_event({"codex": lambda: _live_result("codex", 100.0)}) == 0
    assert len(ids) == 1
    assert ids[0].startswith("scopefuel.exhaust:codex@"), "credential scope 가 키에 들어가야 한다"


# --------------------------------------------------- 실 발송 차단 (15:2x 사고 대책)


def test_autouse_fixture_intercepts_default_sink(tmp_path):
    """conftest autouse 픽스처(mutant a 대상): 기본 싱크는 기록 스텁으로 치환된다.

    픽스처를 지우면 emit_lane_event 는 원래 함수라 ``_test_stub`` 속성이 없고,
    발송 시도가 ``_BLOCKED_EMIT_CALLS`` 에 기록되지 않아 이 테스트는 RED.
    """
    assert getattr(exhaust.emit_lane_event, "_test_stub", False)
    _write_config(tmp_path, '[pools.codex]\non_exhaust = "operator-switch"\n')
    gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    assert exhaust._BLOCKED_EMIT_CALLS, "발송 시도가 차단 스텁에 기록돼야 한다"


def test_pytest_guard_blocks_real_panewire(tmp_path, monkeypatch):
    """PYTEST_CURRENT_TEST 가드(mutant b 대상): PATH 의 panewire 가 가짜여도
    PANEWIRE_BIN 미지정이면 실 emit 은 subprocess 를 만들지 않는다.

    가드를 지우면 PATH 첫 자리의 가짜 panewire 가 마커 파일을 남겨 RED.
    """
    import importlib
    import os
    import stat

    marker = tmp_path / "panewire-called"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "panewire"
    shim.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("PANEWIRE_BIN", raising=False)

    real = importlib.reload(exhaust)  # autouse 스텁 우회 — 원래 함수를 직접 검증
    try:
        assert real.emit_lane_event("probe", "t638-guard-probe") is False
        assert not marker.exists(), "테스트에서 실 panewire subprocess 가 실행됐다"
    finally:
        importlib.reload(exhaust)


# ------------------------------------------------------------- 에피소드 event id (R2-1)


def test_reentry_same_reset_window_gets_new_event_id(tmp_path, monkeypatch):
    """R2-1: 회복 → 같은 reset 창 재소진은 새 에피소드 → 새 event id 로 재발송한다.

    같은 id 를 재사용하면 panewire 의 duplicate dedupe 가 알림을 조용히 삼킨다.
    """
    _write_config(tmp_path, '[pools.codex]\non_exhaust = "operator-switch"\n')
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        exhaust, "emit_lane_event", lambda text, event_id="": sent.append((text, event_id)) or True
    )
    gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    gate_check([_result("codex", 10.0)], "codex-terra", today=TODAY, now=NOW)  # 회복
    gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)  # 같은 reset 재소진
    assert len(sent) == 2
    assert sent[0][1] != sent[1][1], "새 에피소드는 새 event id 를 써야 한다"


def test_event_id_contains_key_window_reset_and_episode(tmp_path, monkeypatch):
    """N12: event id 형식 — key·window·reset·에피소드 토큰을 전부 담는다."""
    _write_config(tmp_path, '[pools.codex]\non_exhaust = "operator-switch"\n')
    ids: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", lambda text, event_id="": ids.append(event_id) or True)
    gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    reset = _reset_almost_full("7d")
    assert len(ids) == 1
    assert ids[0].startswith(f"scopefuel.exhaust:codex:7d:{reset}:")
    assert len(ids[0].rsplit(":", 1)[1]) >= 8, "에피소드 토큰이 있어야 한다"


def test_same_episode_retry_reuses_event_id(tmp_path, monkeypatch):
    """발송 실패 후 재시도는 같은 event id — panewire dedupe 가 멱등성을 보장한다."""
    _write_config(tmp_path, '[pools.codex]\non_exhaust = "operator-switch"\n')
    ids: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", lambda text, event_id="": ids.append(event_id) or False)
    gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=NOW)
    later = NOW + dt.timedelta(seconds=exhaust.RETRY_COOLDOWN_S + 1)
    gate_check([_result("codex", 100.0)], "codex-terra", today=TODAY, now=later)
    assert len(ids) == 2 and ids[0] == ids[1]


# ------------------------------------------------------------- 호출부 배선 (scope/cutoff)


def test_gate_scope_wired_into_event_id(tmp_path, monkeypatch):
    """N7: gate 호출부가 scope(group_name)를 observe 에 넘긴다 — agy 그룹 분리."""
    _write_config(tmp_path, '[pools.agy]\non_exhaust = "operator-switch"\n')
    ids: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", lambda text, event_id="": ids.append(event_id) or True)
    agy = ProviderResult(
        id="agy",
        pool_class="spend",
        buckets=[
            Bucket(
                label="gemini",
                window="7d",
                used_pct=100.0,
                resets_at=_reset_almost_full("7d"),
                scope=Scope("group", "gemini"),
                horizon="week",
            )
        ],
    )
    gate_check([agy], "agy-flash", today=TODAY, now=NOW)
    assert len(ids) == 1
    assert ids[0].startswith("scopefuel.exhaust:agy@gemini:")


def test_d3_gate_path_uses_configured_cutoff(tmp_path, monkeypatch):
    """M14: D3(급표 밖 프로필) 경로도 configured cutoff 를 쓴다."""
    _write_config(tmp_path, '[pools.codex]\ncutoff = 90\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(calls))
    result = gate_check([_result("codex", 95.0)], "codex-fable", today=TODAY, now=NOW)
    assert result.ok is False
    assert "차단선 90%" in result.reason
    assert len(calls) == 1


def test_d3_pass_shows_invalid_cutoff_status(tmp_path):
    """N13: D3 통과 경로도 무효 cutoff 상태를 노출한다."""
    _write_config(tmp_path, '[pools.codex]\ncutoff = "85"\n')
    result = gate_check([_result("codex", 87.0)], "codex-fable", today=TODAY, now=NOW)
    assert result.ok is True
    assert "invalid cutoff" in result.reason


def test_escalation_pass_shows_invalid_cutoff_status(tmp_path):
    """N14: escalation 평가 통과 경로도 무효 cutoff 상태를 노출한다."""
    from scopefuel.recommend import Profile

    _write_config(tmp_path, '[pools.codex]\ncutoff = "bogus"\n')
    table = {g: [] for g in ("S+", "S", "A", "B", "C")}
    table["A+"] = [Profile("codex-sol", "Sol", 65.0, gate="escalation", gate_reason="t")]
    out = recommend([_result("codex", 60.0)], "A+", today=TODAY, now=NOW, grade_table=table)
    assert "invalid cutoff" in out


def test_recommend_render_path_observes_pool(tmp_path, monkeypatch):
    """M21: escalation 엔트리가 관측하지 않는 풀(kiro)도 렌더 경로가 관측한다."""
    _write_config(tmp_path, '[pools.kiro]\non_exhaust = "operator-switch"\n')
    calls: list[str] = []
    monkeypatch.setattr(exhaust, "emit_lane_event", _fake_sink(calls))
    out = recommend([_result("kiro", 100.0)], "A+", today=TODAY, now=NOW)
    assert "소진" in out
    assert len(calls) == 1
