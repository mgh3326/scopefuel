"""ROB-1275: upstream serving-model recording + drift verification."""

from __future__ import annotations

import json

import pytest

from scopefuel import recommend, served


def _profile(**overrides):
    """Build a minimal Profile with serving fields set."""
    base = {
        "name": "oc-dsflash",
        "model": "DeepSeek V4 Flash",
        "benchmark": None,
        "served_slug": "cline-pass/deepseek-v4-flash",
        "upstream_model": "deepseek/deepseek-v4-flash",
        "upstream_as_of": "2026-08-15",
    }
    base.update(overrides)
    return recommend.Profile(**base)


def test_six_clinepass_profiles_carry_a_served_slug():
    served_slugs = {p.name: p.served_slug for p in served.served_profiles()}
    assert served_slugs == {
        "cc-qwen38": "cline-pass/qwen3.8-max",
        "cc-glm": "cline-pass/glm-5.2",
        "oc-glm": "cline-pass/glm-5.2",
        "oc-dsflash": "cline-pass/deepseek-v4-flash",
        "oc-qwen37-max": "cline-pass/qwen3.7-max",
        "oc-minimax-m3": "cline-pass/minimax-m3",
    }


def test_observed_baselines_and_dates_are_recorded():
    by_name = {p.name: p for p in served.served_profiles()}
    assert by_name["cc-qwen38"].upstream_model == "private/qwen3p8-max-wave"
    assert by_name["oc-dsflash"].upstream_model == "deepseek/deepseek-v4-flash"
    assert by_name["oc-glm"].upstream_model == "private/glm-5p2-wave"
    for p in served.served_profiles():
        if p.upstream_model:
            assert p.upstream_as_of == "2026-08-15"
        else:
            assert p.upstream_as_of is None


def test_classify_match_drift_and_unknown():
    assert served.classify(_profile(), "deepseek/deepseek-v4-flash").status == "match"
    assert served.classify(_profile(), "deepseek/deepseek-v4-pro").status == "drift"
    unknown = served.classify(_profile(upstream_model=None), "deepseek/anything")
    assert unknown.status == "unknown" and unknown.recorded is None
    probe_failed = served.classify(_profile(), None)
    assert probe_failed.status == "unknown" and probe_failed.live is None


def test_classify_unknown_when_baseline_unrecorded():
    for name in ("oc-qwen37-max", "oc-minimax-m3"):
        profile = next(p for p in served.served_profiles() if p.name == name)
        assert profile.upstream_model is None and profile.upstream_as_of is None


def test_run_verification_probes_and_returns_exit_code():
    probe = lambda key, slug: "deepseek/deepseek-v4-pro"  # noqa: E731
    verdicts, exit_code = served.run_verification([_profile()], key="k", probe=probe)
    assert verdicts[0].status == "drift"
    assert exit_code == 1


def test_run_verification_exit_0_when_all_match():
    probe = lambda key, slug: "deepseek/deepseek-v4-flash"  # noqa: E731
    verdicts, exit_code = served.run_verification([_profile()], key="k", probe=probe)
    assert verdicts[0].status == "match"
    assert exit_code == 0


def test_run_verification_exit_0_on_unknowns():
    probe = lambda key, slug: None  # noqa: E731
    verdicts, exit_code = served.run_verification([_profile()], key="k", probe=probe)
    assert verdicts[0].status == "unknown"
    assert exit_code == 0


def test_run_verification_skips_probe_when_no_key():
    probe = lambda key, slug: "should-not-run"  # noqa: E731
    verdicts, exit_code = served.run_verification([_profile()], key=None, probe=probe)
    assert verdicts[0].live is None
    assert exit_code == 0


def test_run_verification_drift_anywhere_wins_others_match():
    probe = lambda key, slug: "deepseek/deepseek-v4-pro"  # noqa: E731
    targets = [_profile(), _profile(name="other", upstream_model="x")]
    verdicts, exit_code = served.run_verification(targets, key="k", probe=probe)
    assert verdicts[0].status == "drift" and verdicts[1].status == "drift"
    assert exit_code == 1


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"cline-pass": {"key": "abc123"}}, "abc123"),
        ({"cline-pass": {"key": ""}}, None),
        ({"cline-pass": {"key": "  "}}, None),
        ({"cline-pass": {}}, None),
        ({"cline-pass": "not-a-dict"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_clinepass_key_scrape(entry, expected, tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    if entry is None:
        auth.write_text("{", encoding="utf-8")
    else:
        auth.write_text(json.dumps(entry), encoding="utf-8")
    assert served.clinepass_key(auth) == expected


def test_clinepass_key_missing_file(tmp_path):
    assert served.clinepass_key(tmp_path / "nope.json") is None


def test_auth_path_env_override(tmp_path, monkeypatch):
    target = tmp_path / "env-auth.json"
    monkeypatch.setenv("SCOPEFUEL_OPENCODE_AUTH", str(target))
    assert served.opencode_auth_path() == target


def test_custom_endpoint_no_network(tmp_path, monkeypatch):
    """probe_upstream must hit the given endpoint; guard against any real socket."""
    calls = {}

    def fake_request_json(url, *, method, headers, body, timeout):
        calls["url"] = url
        assert headers["Authorization"] == "Bearer sekret"
        assert body["model"] == "cline-pass/deepseek-v4-flash"
        return {"data": {"model": "deepseek/deepseek-v4-flash"}, "success": True}

    monkeypatch.setattr(served.http, "request_json", fake_request_json)
    assert served.probe_upstream("sekret", "cline-pass/deepseek-v4-flash", endpoint="https://ex.invalid") == (
        "deepseek/deepseek-v4-flash"
    )
    assert calls["url"] == "https://ex.invalid"


def test_probe_reads_top_level_model_when_not_wrapped(monkeypatch):
    monkeypatch.setattr(served.http, "request_json", lambda *a, **kw: {"model": "deepseek/deepseek-v4-flash"})
    assert served.probe_upstream("k", "cline-pass/deepseek-v4-flash", endpoint="https://ex.invalid") == (
        "deepseek/deepseek-v4-flash"
    )


def test_probe_returns_none_on_http_error(monkeypatch):
    def boom(*a, **kw):
        raise served.http.HttpError(401)

    monkeypatch.setattr(served.http, "request_json", boom)
    assert served.probe_upstream("k", "cline-pass/deepseek-v4-flash", endpoint="https://ex.invalid") is None


def test_probe_returns_none_when_model_missing(monkeypatch):
    monkeypatch.setattr(served.http, "request_json", lambda *a, **kw: {"choices": []})
    assert served.probe_upstream("k", "cline-pass/deepseek-v4-flash", endpoint="https://ex.invalid") is None


def test_render_shows_verdict_columns():
    lines = served.render(
        [served.Verdict("oc-dsflash", "cline-pass/deepseek-v4-flash", "a", "2026-08-15", "b", "drift")],
        key_present=True,
    ).splitlines()
    assert lines[0].startswith("ClinePass 업스트림 서빙 모델 대조")
    assert "drift" in lines[2]
    assert "oc-dsflash" in lines[2]


def test_models_verify_exit_code(capsys, monkeypatch):
    from scopefuel import cli

    monkeypatch.setattr(served, "run_verification", lambda **kw: ([], 0))
    monkeypatch.setattr(served, "clinepass_key", lambda *a: None)
    assert cli.main(["models", "verify"]) == 0
