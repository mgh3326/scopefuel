"""ROB-1275 — 모델 서빙 drift 검증 (프로브는 가짜로 갈아끼워 네트워크 금지)."""

from __future__ import annotations

import json
import os

import pytest

from scopefuel import cli, modelsverify
from scopefuel.recommend import GRADE_TABLE


def _write_key(tmp_path, value: str) -> str:
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"cline-pass": {"type": "opencode", "key": value}}))
    os.environ["SCOPEFUEL_CLINEPASS_AUTH"] = str(auth)
    return str(auth)


def _key_less(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SCOPEFUEL_CLINEPASS_AUTH", str(tmp_path / "nope.json"))


def _by_profile() -> dict[str, object]:
    return {p.name: p for group in GRADE_TABLE.values() for p in group}


def test_recorded_upstream_slugs_match_spec():
    """명세 실측표: 관측 4행은 기록, 미관측 2행은 upstream_slug=None."""
    by_profile = _by_profile()
    assert by_profile["cc-qwen38"].request_slug == "cline-pass/qwen3.8-max"
    assert by_profile["cc-qwen38"].upstream_slug == "private/qwen3p8-max-wave"
    assert by_profile["cc-glm"].upstream_slug == "private/glm-5p2-wave"
    assert by_profile["oc-glm"].upstream_slug == "private/glm-5p2-wave"
    assert by_profile["oc-dsflash"].upstream_slug == "deepseek/deepseek-v4-flash"
    # 미관측 2행: request_slug 는 있지만 기록값 없음 → unknown 대조 기준.
    assert by_profile["oc-qwen37-max"].request_slug == "cline-pass/qwen3.7-max"
    assert by_profile["oc-qwen37-max"].upstream_slug is None
    assert by_profile["oc-minimax-m3"].request_slug == "cline-pass/minimax-m3"
    assert by_profile["oc-minimax-m3"].upstream_slug is None


def test_observed_rows_carry_same_observation_date():
    observed = [p for p in _by_profile().values() if p.upstream_slug]
    assert len(observed) == 4
    assert all(p.upstream_observed == "2026-08-15" for p in observed)


def test_missing_key_yields_all_unknown_without_probe(tmp_path, monkeypatch):
    _key_less(tmp_path, monkeypatch)
    called: list[str] = []

    def _fake_probe(slug, key):
        called.append(slug)
        return "should-not-be-called"

    report = modelsverify.verify(probe_fn=_fake_probe)
    assert not report.has_drift
    assert report.rows
    assert all(row.verdict == "unknown" for row in report.rows)
    assert called == []  # 키가 없으면 네트워크 프로브를 아예 하지 않는다


def test_match_and_drift_verdicts(tmp_path):
    _write_key(tmp_path, "synthetic-key")
    by_profile = _by_profile()

    def fake_probe(slug, key):
        assert key == "synthetic-key"  # 키는 프로브에만 전달, 로직은 값과 무관
        if slug == "cline-pass/qwen3.8-max":
            return "some-other-model"  # drift
        for p in by_profile.values():
            if p.request_slug == slug and p.upstream_slug is not None:
                return p.upstream_slug  # match
        return "whatever-unknown-model"  # 미관측 슬러그 → 라이브는 있어도 기준이 없음

    report = modelsverify.verify(probe_fn=fake_probe)
    row = {r.profile: r for r in report.rows}

    assert row["cc-qwen38"].verdict == "drift"
    assert row["cc-glm"].verdict == "match"
    assert row["oc-glm"].verdict == "match"
    assert row["oc-dsflash"].verdict == "match"
    assert row["oc-qwen37-max"].verdict == "unknown"
    assert row["oc-minimax-m3"].verdict == "unknown"
    assert report.has_drift is True
    # drift 행은 라이브 값을 노출해 사람이 어떤 모델인지 대조할 수 있어야 한다.
    assert row["cc-qwen38"].live == "some-other-model"


def test_no_drift_when_live_equals_recorded(tmp_path):
    _write_key(tmp_path, "synthetic-key")
    by_profile = _by_profile()

    def all_match(slug, key):
        for p in by_profile.values():
            if p.request_slug == slug and p.upstream_slug is not None:
                return p.upstream_slug
        return "anything"  # 기록값 없음 행은 unknown 이지만 drift 는 아님

    report = modelsverify.verify(probe_fn=all_match)
    assert not report.has_drift
    assert all(r.verdict != "drift" for r in report.rows)


def test_cli_verify_exit_1_on_drift(tmp_path, monkeypatch, capsys):
    _write_key(tmp_path, "synthetic-key")

    def drifting(slug, key):
        return "some-other-model"

    original = modelsverify.verify
    monkeypatch.setattr(modelsverify, "verify", lambda **kw: original(probe_fn=drifting))
    assert cli.main(["models", "verify"]) == 1
    out = capsys.readouterr().out
    assert "drift" in out and "라이브값" in out
    assert "synthetic-key" not in out  # 키 값은 어떤 출력에도 노출 금지


def test_cli_verify_exit_0_when_all_match(tmp_path, monkeypatch, capsys):
    _write_key(tmp_path, "synthetic-key")
    by_profile = _by_profile()

    def all_match(slug, key):
        for p in by_profile.values():
            if p.request_slug == slug and p.upstream_slug is not None:
                return p.upstream_slug
        return "anything"

    original = modelsverify.verify
    monkeypatch.setattr(modelsverify, "verify", lambda **kw: original(probe_fn=all_match))
    assert cli.main(["models", "verify"]) == 0
    assert "synthetic-key" not in capsys.readouterr().out


def test_format_report_never_exposes_key(tmp_path):
    _write_key(tmp_path, "synthetic-key")
    report = modelsverify.verify(probe_fn=lambda slug, key: "private/glm-5p2-wave")
    assert "synthetic-key" not in modelsverify.format_report(report)


def test_default_probe_performs_a_minimum_request(tmp_path):
    """probe 는 최소 요청(생성 1토큰)을 만들어야 한다 — 요청 구조 계약을 직접 검증."""
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"model":"private/served-model","choices":[]}'

    def fake_urlopen(request, **kwargs):
        captured["url"] = request.full_url
        captured["headers"] = {k: v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data)
        return Response()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(modelsverify.urllib.request, "urlopen", fake_urlopen)
    live_top = modelsverify.probe("cline-pass/qwen3.8-max", "synthetic-key")
    monkeypatch.undo()

    assert live_top == "private/served-model"
    assert captured["url"] == modelsverify.COMPLETIONS_URL
    assert captured["headers"]["Authorization"] == "Bearer synthetic-key"
    assert captured["body"]["model"] == "cline-pass/qwen3.8-max"
    assert captured["body"]["max_tokens"] == 1
    assert captured["body"]["stream"] is False


def test_probe_reads_nested_data_model_gateway_format():
    """실측 ClinePass 형식: 서빙 모델은 data.model 에 실린다 (top-level model 없음)."""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"data":{"model":"private/qwen3p8-max-wave","choices":[]}}'

    monkeypatch = pytest.MonkeyPatch()
    captured = {}

    def fake_urlopen(request, **kwargs):
        captured["hit"] = True
        return Response()

    monkeypatch.setattr(modelsverify.urllib.request, "urlopen", fake_urlopen)
    assert modelsverify.probe("cline-pass/qwen3.8-max", "synthetic-key") == "private/qwen3p8-max-wave"
    monkeypatch.undo()


def test_probe_returns_none_on_missing_model_field():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"data":{"choices":[]}}'

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(modelsverify.urllib.request, "urlopen", lambda *a, **k: Response())
    assert modelsverify.probe("cline-pass/qwen3.8-max", "synthetic-key") is None
    monkeypatch.undo()
