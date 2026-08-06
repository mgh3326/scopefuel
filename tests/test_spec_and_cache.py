"""선언형 스펙 확장성 + 캐시 stale 폴백."""

from __future__ import annotations

import json
import time

from scopefuel import cache, spec
from scopefuel.model import Bucket, ProviderResult, Scope


def _write_creds(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"tokens": {"access_token": "tok"}}))
    return path


def test_spec_provider_builds_account_and_model_buckets(tmp_path, monkeypatch, fixture_json):
    """codex 형태의 응답을 코드 없이 TOML 스펙만으로 파싱할 수 있어야 한다."""
    creds = _write_creds(tmp_path)
    captured = {}

    def fake_request(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return fixture_json("codex_usage")

    monkeypatch.setattr(spec, "request_json", fake_request)

    provider = spec.make_provider(
        {
            "id": "myplan",
            "plan_path": ["plan_type"],
            "credentials": {"file": str(creds), "token_path": ["tokens", "access_token"]},
            "request": {"url": "https://example.test/usage", "headers": {"Authorization": "Bearer {token}"}},
            "buckets": [
                {
                    "label": "7d",
                    "window": "7d",
                    "scope": "account",
                    "used_pct_path": ["rate_limit", "primary_window", "used_percent"],
                    "resets_at_path": ["rate_limit", "primary_window", "reset_at"],
                    "resets_at_kind": "epoch",
                },
                {
                    "for_each": ["additional_rate_limits"],
                    "label": "{item[limit_name]} 7d",
                    "window": "7d",
                    "scope": "model",
                    "scope_name": "{item[limit_name]}",
                    "used_pct_path": ["rate_limit", "primary_window", "used_percent"],
                },
            ],
        }
    )
    result = provider()

    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert result.plan == "pro"
    assert [(b.label, b.scope.kind, b.used_pct) for b in result.buckets] == [
        ("7d", "account", 82.0),
        ("GPT-5.3-Codex-Spark 7d", "model", 0.0),
    ]
    assert result.buckets[0].horizon == "week"
    assert result.verdict.blocking_pct == 82.0


def test_spec_supports_remaining_fraction(tmp_path, monkeypatch):
    creds = _write_creds(tmp_path)
    monkeypatch.setattr(spec, "request_json", lambda *a, **k: {"q": {"remainingFraction": 0.42731}})
    provider = spec.make_provider(
        {
            "id": "frac",
            "credentials": {"file": str(creds), "token_path": ["tokens", "access_token"]},
            "request": {"url": "https://example.test/u"},
            "buckets": [
                {"label": "5h", "window": "5h", "remaining_fraction_path": ["q", "remainingFraction"]}
            ],
        }
    )
    bucket = provider().buckets[0]
    assert bucket.used_pct == 57.3
    assert bucket.horizon == "now"  # window=5h → now 로 자동 추론


def test_spec_supports_iso_reset_kind_by_default(tmp_path, monkeypatch):
    creds = _write_creds(tmp_path)
    reset = "2026-07-30T05:00:00Z"
    monkeypatch.setattr(spec, "request_json", lambda *a, **k: {"usage": 12, "reset": reset})
    provider = spec.make_provider(
        {
            "id": "iso",
            "credentials": {"file": str(creds), "token_path": ["tokens", "access_token"]},
            "request": {"url": "https://example.test/u"},
            "buckets": [
                {
                    "label": "5h",
                    "window": "5h",
                    "used_pct_path": ["usage"],
                    "resets_at_path": ["reset"],
                }
            ],
        }
    )

    bucket = provider().buckets[0]
    assert bucket.resets_at == reset


def test_spec_dir_discovery(tmp_path, monkeypatch):
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    (spec_dir / "myplan.toml").write_text(
        "id = 'myplan'\n"
        "[credentials]\nfile = '~/nope.json'\ntoken_path = ['t']\n"
        "[request]\nurl = 'https://example.test/u'\n"
    )
    monkeypatch.setenv("SCOPEFUEL_SPEC_DIR", str(spec_dir))
    assert "myplan" in spec.discover_specs()


def test_broken_spec_does_not_break_discovery(tmp_path, monkeypatch):
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    (spec_dir / "bad.toml").write_text("this is not toml = = =")
    (spec_dir / "ok.toml").write_text(
        "id = 'good'\n[credentials]\ntoken_env = 'X'\n[request]\nurl = 'https://example.test/u'\n"
    )
    monkeypatch.setenv("SCOPEFUEL_SPEC_DIR", str(spec_dir))
    found = spec.discover_specs()
    assert "good" in found and "bad" not in found


def test_cache_falls_back_to_stale_snapshot_with_age(monkeypatch):
    """agy 세션을 다 정리해도 마지막 값을 나이와 함께 쓸 수 있어야 한다."""
    good = ProviderResult(
        id="agy",
        buckets=[
            Bucket(
                label="gemini 5h", window="5h", used_pct=7.4, scope=Scope("group", "gemini"), horizon="now"
            )
        ],
    )
    fetchers = {"agy": lambda: good}
    first = cache.collect(fetchers, ["agy"], now=1000.0)
    assert first[0].stale is False

    def boom():
        raise RuntimeError("agy 세션이 실행 중이 아님")

    later = cache.collect({"agy": boom}, ["agy"], now=1000.0 + 600, ttl_s=60)
    assert later[0].stale is True
    assert later[0].age_s == 600
    assert later[0].buckets[0].used_pct == 7.4
    assert "캐시" in (later[0].note or "")
    assert cache.format_age(600) == "10분 전"


def test_cache_refuses_ancient_snapshot(monkeypatch):
    good = ProviderResult(id="x", buckets=[Bucket(label="5h", window="5h", used_pct=1.0)])
    cache.collect({"x": lambda: good}, ["x"], now=0.0)

    def boom():
        raise RuntimeError("dead")

    result = cache.collect({"x": boom}, ["x"], now=cache.STALE_MAX_S + 10)[0]
    assert result.error == "dead"
    assert result.buckets == []


def test_warning_result_is_not_cached(monkeypatch):
    calls = 0

    def warning():
        nonlocal calls
        calls += 1
        return ProviderResult(id="clinepass", warning="인증 실패 (HTTP 401)")

    first = cache.collect({"clinepass": warning}, ["clinepass"], now=1000.0)
    second = cache.collect({"clinepass": warning}, ["clinepass"], now=1001.0)

    assert calls == 2
    assert first[0].status == second[0].status == "warning"


def test_cache_collects_misses_in_parallel_and_preserves_name_order():
    names = ["slow", "fast", "middle"]
    delays = {"slow": 0.20, "fast": 0.05, "middle": 0.10}

    def make_fetcher(name):
        def fetch():
            time.sleep(delays[name])
            return ProviderResult(id=name)

        return fetch

    started = time.monotonic()
    results = cache.collect({name: make_fetcher(name) for name in names}, names, use_cache=False)
    elapsed = time.monotonic() - started

    assert [result.id for result in results] == names
    assert elapsed < 0.30  # 직렬이면 약 0.35초, 병렬이면 max(delay)에 가깝다.


def test_cache_fetch_exception_does_not_cancel_other_providers():
    def boom():
        raise RuntimeError("broken")

    results = cache.collect(
        {"bad": boom, "good": lambda: ProviderResult(id="good")},
        ["bad", "good"],
        use_cache=False,
    )

    assert results[0].id == "bad"
    assert results[0].error == "broken"
    assert results[1].id == "good"
    assert results[1].error is None


def test_cache_hit_does_not_call_fetcher():
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        return ProviderResult(id="claude")

    cache.collect({"claude": fetch}, ["claude"], now=1000.0)
    cached = cache.collect({"claude": fetch}, ["claude"], now=1000.0 + 179.0)

    assert calls == 1
    assert cached[0].stale is False


def test_provider_ttls_and_default_are_used_at_boundaries():
    names = [*cache.PROVIDER_TTL_S, "unknown"]
    calls = {name: 0 for name in names}

    def make_fetcher(name):
        def fetch():
            calls[name] += 1
            return ProviderResult(id=name)

        return fetch

    fetchers = {name: make_fetcher(name) for name in names}
    for name in names:
        ttl = cache.PROVIDER_TTL_S.get(name, cache.DEFAULT_TTL_S)
        cache.collect(fetchers, [name], now=1000.0)
        before = cache.collect(fetchers, [name], now=1000.0 + ttl - 1)
        assert before[0].stale is False
        assert calls[name] == 1
        cache.collect(fetchers, [name], now=1000.0 + ttl + 1)
        assert calls[name] == 2


def test_cache_ttl_override_applies_to_every_provider():
    names = ["claude", "grok", "unknown"]
    calls = {name: 0 for name in names}

    def make_fetcher(name):
        def fetch():
            calls[name] += 1
            return ProviderResult(id=name)

        return fetch

    fetchers = {name: make_fetcher(name) for name in names}
    cache.collect(fetchers, names, now=1000.0)
    cache.collect(fetchers, names, now=1000.0 + 31.0, ttl_s=30.0)

    assert calls == {name: 2 for name in names}
