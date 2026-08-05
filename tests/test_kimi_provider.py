from __future__ import annotations

from scopefuel.providers import kimi


SAMPLE = (
    "\x1b[2KWeekly: 75% left (resets in 5d 12h)\r\n"
    "\x1b[2K5h: 30% left (resets in 2h 10m)\r\n"
)


def test_parse_cli_usage_maps_remaining_percent_and_resets():
    result = kimi.parse(SAMPLE)

    assert result.id == "kimi"
    assert result.pool_class == "spend"
    assert result.source == "cli:/usage"
    assert [(bucket.label, bucket.window, bucket.horizon, bucket.used_pct) for bucket in result.buckets] == [
        ("5h", "5h", "now", 70.0),
        ("weekly", "7d", "week", 25.0),
    ]
    assert all(bucket.resets_at for bucket in result.buckets)
    assert all(bucket.scope.kind == "account" for bucket in result.buckets)


def test_parse_rate_limit_is_an_immediate_error_without_retry():
    result = kimi.parse("HTTP 429 Too Many Requests\n")

    assert result.error == "Kimi CLI usage rate limited (HTTP 429/rate limit; retry 금지)"
    assert result.buckets == []
    assert result.raw == {"stdout": "HTTP 429 Too Many Requests\n"}


def test_fetch_uses_a_pty_and_sends_usage_once(tmp_path, monkeypatch):
    binary = tmp_path / "fake-kimi"
    binary.write_text(
        "#!/bin/sh\n"
        "printf 'Kimi Code\\r\\n'\n"
        "IFS= read -r command\n"
        "[ \"$command\" = '/usage' ] || exit 9\n"
        "printf 'Weekly: 80%% left (resets in 1d)\\r\\n5h: 50%% left (resets in 1h)\\r\\n'\n"
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.setattr(kimi, "BINARY", str(binary))

    result = kimi.fetch()

    assert result.error is None
    assert [(bucket.label, bucket.used_pct) for bucket in result.buckets] == [
        ("5h", 50.0),
        ("weekly", 20.0),
    ]
