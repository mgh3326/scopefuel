"""task #751 — policy writers must preserve every non-target byte of config.toml.

Incident 2026-09-26: ``policy set`` re-serialized the parsed dict, so
``[bench]`` (allow_plaintext_quota_share …), all comments, key order and
quoting were dropped on every write — quota sharing silently went off on
four hosts. These tests pin the new contract: writers change only the
target table/keys; everything else is byte-identical; unparseable files are
refused with zero writes.
"""

from __future__ import annotations

import datetime as dt
import re
import threading

import pytest

from scopefuel import cli, policy
from scopefuel.providers import BUILTIN

TODAY = dt.date(2026, 7, 31)
STILL_ACTIVE = dt.date.today() + dt.timedelta(days=30)

FIXTURE = """# scopefuel config — 운영자 메모. 이 주석은 항상 남아 있어야 한다.

[settings]
# 튜닝 값
reset_urgency_hours = 6    # 시간 단위

[bench]
backend = "handoffkeep"   # 정본 저장소
allow_plaintext_quota_share = true   # ssh 터널이라 평문 허용
cache_ttl_s = 300

[pools.claude]
class = "preserve"
until = "2026-08-03"      # quoted form stays quoted while untouched
note = "Pro 요금제"
price_usd = 200
# claude 뒷주석

[pools.codex]
boost = 1
until = "2026-08-05"
capacity_weight = 3.5

[pools.kiro]
plan = "team"

[profiles."codex-max"]
subscribed = false

[profiles.agy]
subscribed = true

[zzz]
other = "모르는 섹션도 그대로"   # unknown section survives
"""


@pytest.fixture
def policy_config(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_dir))
    path = cfg_dir / "scopefuel" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FIXTURE, encoding="utf-8")
    return path


def _mask_region(text: str, header: str) -> str:
    """Drop one table's lines (header through the line before the next
    header) so the rest can be compared byte-for-byte — also tolerates the
    region being absent (cleared). ``header`` matches the header line only."""
    out = []
    in_region = False
    for line in text.split("\n"):
        if re.match(r"\s*\[", line):
            in_region = bool(re.fullmatch(header, line.strip()))
        if not in_region:
            out.append(line)
    return "\n".join(out)


def _region(text: str, header: str) -> str:
    out = []
    in_region = False
    for line in text.split("\n"):
        if re.match(r"\s*\[", line):
            if in_region:
                break
            in_region = bool(re.fullmatch(header, line.strip()))
        if in_region:
            out.append(line)
    return "\n".join(out)


def _assert_non_target_identical(before: str, after: str, header: str) -> None:
    assert _mask_region(after, header) == _mask_region(before, header)


KIRO = r"\[pools\.kiro\]"
CODEX = r"\[pools\.codex\]"
CLAUDE = r"\[pools\.claude\]"
GROK = r"\[pools\.grok\]"
PROF_CODEX_MAX = r'\[profiles\."codex-max"\]'
PROF_NEW = r'\[profiles\."new-prof"\]'


# ---------------------------------------------------------------- set_policy


def test_set_policy_new_pool_preserves_everything(policy_config):
    policy.set_policy("grok", "exclude", until=STILL_ACTIVE, note="증分")
    text = policy_config.read_text(encoding="utf-8")
    _assert_non_target_identical(FIXTURE, text, GROK)
    # the incident keys are literally still there
    assert "allow_plaintext_quota_share = true   # ssh 터널이라 평문 허용" in text
    assert "운영자 메모" in text
    effective, _ = policy.get_policy("grok", "preserve", today=TODAY)
    assert effective == "exclude"


def test_set_policy_existing_pool_keeps_sibling_keys(policy_config):
    """class/until change but codex's other keys stay byte-identical."""
    policy.set_policy("codex", "exclude", until=STILL_ACTIVE)
    text = policy_config.read_text(encoding="utf-8")
    _assert_non_target_identical(FIXTURE, text, CODEX)
    region = _region(text, CODEX)
    assert "boost = 1" in region
    assert "capacity_weight = 3.5" in region
    assert 'class = "exclude"' in region
    assert f"until = {STILL_ACTIVE}" in region
    effective, _ = policy.get_policy("codex", "preserve", today=TODAY)
    assert effective == "exclude"
    assert policy.get_capacity_weight("codex") == (3.5, None)


def test_set_policy_only_boost_touches_only_boost(policy_config):
    """--boost N without a class positional must not touch class/until."""
    policy.set_policy("codex", None, boost=7, until=STILL_ACTIVE)
    text = policy_config.read_text(encoding="utf-8")
    _assert_non_target_identical(FIXTURE, text, CODEX)
    region = _region(text, CODEX)
    assert "boost = 7" in region
    assert "capacity_weight = 3.5" in region
    boost, _ = policy.get_boost("codex", today=TODAY)
    assert boost == 7


def test_boost_none_removes_only_the_boost_line(policy_config):
    policy.set_policy("codex", None, boost=None)
    text = policy_config.read_text(encoding="utf-8")
    _assert_non_target_identical(FIXTURE, text, CODEX)
    region = _region(text, CODEX)
    assert "boost" not in region
    assert 'until = "2026-08-05"' in region  # quoted until untouched
    assert "capacity_weight = 3.5" in region


# --------------------------------------------------------------- clear_policy


def test_clear_policy_removes_only_that_table(policy_config):
    assert policy.clear_policy("codex") is True
    text = policy_config.read_text(encoding="utf-8")
    _assert_non_target_identical(FIXTURE, text, CODEX)
    assert "[pools.codex]" not in text
    assert "capacity_weight = 3.5" not in text
    # neighbouring tables intact
    assert 'until = "2026-08-03"      # quoted form stays quoted while untouched' in text
    assert 'plan = "team"' in text


def test_set_then_clear_restores_file_byte_for_byte(policy_config):
    policy.set_policy("grok", "spend", until=STILL_ACTIVE, note="tmp")
    assert policy.clear_policy("grok") is True
    assert policy_config.read_text(encoding="utf-8") == FIXTURE


def test_clear_policy_absent_returns_false_and_writes_nothing(policy_config):
    assert policy.clear_policy("grok") is False
    assert policy_config.read_text(encoding="utf-8") == FIXTURE


# ------------------------------------------------------------- --subscribed


def test_pool_subscribed_round_trip_preserves(policy_config):
    policy.set_subscribed("kiro", False)
    text = policy_config.read_text(encoding="utf-8")
    _assert_non_target_identical(FIXTURE, text, KIRO)
    assert "subscribed = false" in _region(text, KIRO)
    assert 'plan = "team"' in _region(text, KIRO)
    assert policy.get_subscribed("kiro") == (False, None)

    policy.set_subscribed("kiro", None)
    text = policy_config.read_text(encoding="utf-8")
    assert "subscribed" not in _region(text, KIRO)
    assert 'plan = "team"' in _region(text, KIRO)
    _assert_non_target_identical(FIXTURE, text, KIRO)


def test_pool_subscribed_remove_last_key_drops_table(policy_config):
    """#742 parity: a pool table emptied by the removal is dropped entirely."""
    policy.set_subscribed("grok", False)
    assert "subscribed = false" in _region(policy_config.read_text(), GROK)
    policy.set_subscribed("grok", None)
    text = policy_config.read_text(encoding="utf-8")
    assert "[pools.grok]" not in text
    _assert_non_target_identical(FIXTURE, text, GROK)


def test_profile_subscribed_quoted_name_round_trip(policy_config):
    policy.set_profile_subscribed("codex-max", True)
    text = policy_config.read_text(encoding="utf-8")
    _assert_non_target_identical(FIXTURE, text, PROF_CODEX_MAX)
    region = _region(text, PROF_CODEX_MAX)
    assert "subscribed = true" in region
    assert policy.get_profile_subscribed("codex-max") == (True, None)


def test_profile_subscribed_new_quoted_table(policy_config):
    policy.set_profile_subscribed("new-prof", False)
    text = policy_config.read_text(encoding="utf-8")
    region = _region(text, PROF_NEW)
    assert "subscribed = false" in region
    _assert_non_target_identical(FIXTURE, text, PROF_NEW)

    policy.set_profile_subscribed("new-prof", None)
    text = policy_config.read_text(encoding="utf-8")
    assert "new-prof" not in text
    _assert_non_target_identical(FIXTURE, text, PROF_NEW)


def test_profile_subscribed_clear_restores_file(policy_config):
    policy.set_profile_subscribed("agy", False)
    policy.set_profile_subscribed("agy", None)
    text = policy_config.read_text(encoding="utf-8")
    # agy table had ONLY subscribed — old parity drops the emptied table,
    # so the file differs from the fixture only by that region.
    _assert_non_target_identical(FIXTURE, text, r"\[profiles\.agy\]")
    assert "[profiles.agy]" not in text


# ------------------------------------------------------- parse-error refusal


def _write_broken(policy_config):
    policy_config.write_text("[pools.claude\nclass = ", encoding="utf-8")


@pytest.mark.parametrize(
    "writer",
    [
        lambda: policy.set_policy("codex", "spend", until=STILL_ACTIVE),
        lambda: policy.clear_policy("codex"),
        lambda: policy.set_subscribed("codex", False),
        lambda: policy.set_profile_subscribed("codex-max", True),
    ],
    ids=["set_policy", "clear_policy", "set_subscribed", "set_profile_subscribed"],
)
def test_parse_error_refuses_and_changes_nothing(policy_config, writer):
    _write_broken(policy_config)
    before = policy_config.read_text(encoding="utf-8")
    with pytest.raises(policy.ConfigEditError):
        writer()
    assert policy_config.read_text(encoding="utf-8") == before


# ------------------------------------------------------- CLI-level behaviour


def test_cli_set_preserves_fixture(policy_config, capsys, monkeypatch):
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    rc = cli.main(["policy", "set", "codex", "exclude", "--until", str(STILL_ACTIVE), "--note", "x"])
    assert rc == 0
    text = policy_config.read_text(encoding="utf-8")
    _assert_non_target_identical(FIXTURE, text, CODEX)
    assert "allow_plaintext_quota_share = true" in text


def test_cli_parse_error_is_clean_refusal(policy_config, capsys, monkeypatch):
    _write_broken(policy_config)
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    rc = cli.main(["policy", "set", "codex", "spend", "--until", str(STILL_ACTIVE)])
    assert rc == 2
    assert "config" in capsys.readouterr().err.lower()
    assert policy_config.read_text(encoding="utf-8") == "[pools.claude\nclass = "


# ----------------------------------------------------------------- the rest


def test_write_creates_file_when_missing(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_dir))
    path = cfg_dir / "scopefuel" / "config.toml"
    policy.set_policy("claude", "preserve", until=STILL_ACTIVE)
    text = path.read_text(encoding="utf-8")
    assert "[pools.claude]" in text
    assert 'class = "preserve"' in text
    assert f"until = {STILL_ACTIVE}" in text
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_concurrent_writers_do_not_clobber(policy_config):
    """Two writers racing on different pools must both land (inter-process lock)."""
    errors: list[BaseException] = []

    def work(pool):
        try:
            for i in range(5):
                policy.set_subscribed(pool, i % 2 == 0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(p,)) for p in ("grok", "kiro")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    text = policy_config.read_text(encoding="utf-8")
    import tomllib

    parsed = tomllib.loads(text)
    assert "grok" in parsed["pools"] and "kiro" in parsed["pools"]
    assert parsed["pools"]["kiro"]["plan"] == "team"


def test_noop_write_leaves_file_untouched(policy_config):
    """subscribed none on a pool without the key is a no-op — file not even written."""
    policy.set_subscribed("kiro", None)
    assert policy_config.read_text(encoding="utf-8") == FIXTURE


# ------------------------------------------------- tester blockers (B1/B2/B3)

CRLF_FIXTURE = FIXTURE.replace("\n", "\r\n")


def _crlf_file(policy_config):
    policy_config.write_bytes(CRLF_FIXTURE.encode("utf-8"))


def test_crlf_file_stays_byte_identical(policy_config):
    """B1: universal-newline read/write flattened CRLF on every write."""
    _crlf_file(policy_config)
    before = policy_config.read_bytes()
    policy.set_policy("grok", "spend", until=STILL_ACTIVE)
    mid = policy_config.read_bytes()
    assert mid.count(b"\n") == mid.count(b"\r\n")  # no bare LF anywhere
    assert b'backend = "handoffkeep"   # \xec\xa0\x95\xeb\xb3\xb8' in mid
    policy.clear_policy("grok")
    assert policy_config.read_bytes() == before


@pytest.mark.parametrize(
    "writer,header",
    [
        (lambda: policy.set_policy("codex", "exclude", until=STILL_ACTIVE), CODEX),
        (lambda: policy.clear_policy("claude"), CLAUDE),
        (lambda: policy.set_subscribed("kiro", False), KIRO),
        (lambda: policy.set_profile_subscribed("codex-max", True), PROF_CODEX_MAX),
    ],
    ids=["set_policy", "clear_policy", "set_subscribed", "set_profile_subscribed"],
)
def test_crlf_preserved_for_all_writers(policy_config, writer, header):
    _crlf_file(policy_config)
    before = policy_config.read_bytes()
    writer()
    after = policy_config.read_bytes()
    assert after.count(b"\n") == after.count(b"\r\n")
    for marker in (
        'backend = "handoffkeep"   # 정본 저장소',
        "allow_plaintext_quota_share = true   # ssh 터널이라 평문 허용",
        'other = "모르는 섹션도 그대로"   # unknown section survives',
    ):
        assert (marker + "\r").encode("utf-8") in after
    # every non-target line, line endings included, is unchanged
    assert _mask_region(after.decode("utf-8"), header) == _mask_region(before.decode("utf-8"), header)


def test_inline_table_subscribed_remove_refuses_not_noop(policy_config):
    """B2: removal inside an inline table used to silently report success."""
    policy_config.write_text(
        '[pools]\ncodex = { subscribed = true, plan = "team", price_usd = 200 }\n'
        '\n[bench]\nbackend = "keep"\n',
        encoding="utf-8",
    )
    before = policy_config.read_bytes()
    with pytest.raises(policy.ConfigEditError):
        policy.set_subscribed("codex", None)
    assert policy_config.read_bytes() == before


def test_inline_table_profile_subscribed_remove_refuses(policy_config):
    policy_config.write_text(
        '[profiles]\n"codex-max" = { subscribed = true, label = "x" }\n',
        encoding="utf-8",
    )
    before = policy_config.read_bytes()
    with pytest.raises(policy.ConfigEditError):
        policy.set_profile_subscribed("codex-max", None)
    assert policy_config.read_bytes() == before


def test_cli_subscribed_none_on_inline_table_is_refusal(policy_config, capsys, monkeypatch):
    policy_config.write_text('[pools]\ncodex = { subscribed = true, plan = "team" }\n', encoding="utf-8")
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    before = policy_config.read_bytes()
    rc = cli.main(["policy", "set", "codex", "--subscribed", "none"])
    assert rc == 2
    assert policy_config.read_bytes() == before


def test_cli_combined_set_is_single_atomic_write(policy_config, monkeypatch):
    """B3: class+subscribed used to commit in two separate writes."""
    calls = []
    original = policy._atomic_write

    def spy(path, text):
        calls.append(path)
        original(path, text)

    monkeypatch.setattr(policy, "_atomic_write", spy)
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    rc = cli.main(["policy", "set", "codex", "exclude", "--until", str(STILL_ACTIVE), "--subscribed", "off"])
    assert rc == 0
    assert len(calls) == 1
    import tomllib

    parsed = tomllib.loads(policy_config.read_text(encoding="utf-8"))
    assert parsed["pools"]["codex"]["class"] == "exclude"
    assert parsed["pools"]["codex"]["subscribed"] is False


def test_cli_combined_set_failure_persists_nothing(policy_config, monkeypatch):
    def boom(path, text):
        raise OSError("injected failure")

    monkeypatch.setattr(policy, "_atomic_write", boom)
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    with pytest.raises(OSError):
        cli.main(["policy", "set", "codex", "exclude", "--until", str(STILL_ACTIVE), "--subscribed", "off"])
    assert policy_config.read_text(encoding="utf-8") == FIXTURE


def test_pool_subscribed_none_via_set_policy(policy_config):
    """The merged --subscribed none path drops an emptied pool table."""
    policy.set_policy("kiro", None, subscribed=False)
    assert "subscribed = false" in _region(policy_config.read_text(), KIRO)
    policy.set_policy("kiro", None, subscribed=None)
    text = policy_config.read_text(encoding="utf-8")
    # kiro had plan = "team" — table survives, only the key is gone
    assert "subscribed" not in _region(text, KIRO)
    assert 'plan = "team"' in _region(text, KIRO)


# ----------------------------------------- round-2 blocker: EOF line endings

CRLF_BENCH_ONLY = b"[bench]\r\nkeep = true\r\n"


def test_new_table_at_crlf_eof(policy_config):
    """Appending a new table must leave a real \\r\\n, not a lone \\r."""
    policy_config.write_bytes(CRLF_BENCH_ONLY)
    policy.set_profile_subscribed("new name", True)
    after = policy_config.read_bytes()
    assert after.endswith(b"subscribed = true\r\n")
    assert after.count(b"\n") == after.count(b"\r\n")
    import tomllib

    assert tomllib.loads(after.decode("utf-8"))["profiles"]["new name"]["subscribed"] is True
    policy.set_policy("grok", "spend", until=STILL_ACTIVE)
    after = policy_config.read_bytes()
    assert after.count(b"\n") == after.count(b"\r\n")
    assert b'[profiles."new name"]\r\nsubscribed = true\r\n' in after


def test_new_table_at_crlf_eof_no_final_newline(policy_config):
    policy_config.write_bytes(b"[bench]\r\nkeep = true")
    policy.set_profile_subscribed("new name", True)
    after = policy_config.read_bytes()
    assert after.count(b"\n") == after.count(b"\r\n")
    assert after.startswith(b"[bench]\r\nkeep = true\r\n")
    import tomllib

    assert tomllib.loads(after.decode("utf-8"))["profiles"]["new name"]["subscribed"] is True


def test_new_key_in_last_table_crlf_no_final_newline(policy_config):
    policy_config.write_bytes(b"[pools.codex]\r\nplan = 5")
    policy.set_subscribed("codex", True)
    after = policy_config.read_bytes()
    assert after == b"[pools.codex]\r\nplan = 5\r\nsubscribed = true\r\n"


def test_new_key_in_last_table_lf_no_final_newline(policy_config):
    policy_config.write_bytes(b"[pools.codex]\nplan = 5")
    policy.set_subscribed("codex", True)
    assert policy_config.read_bytes() == b"[pools.codex]\nplan = 5\nsubscribed = true\n"


def test_cli_profile_on_at_crlf_eof(policy_config, monkeypatch):
    policy_config.write_bytes(CRLF_BENCH_ONLY)
    monkeypatch.setattr(cli, "registry", lambda: dict(BUILTIN))
    rc = cli.main(["policy", "profile", "new name", "on"])
    assert rc == 0
    after = policy_config.read_bytes()
    assert after.count(b"\n") == after.count(b"\r\n")
    assert b'[profiles."new name"]\r\nsubscribed = true\r\n' in after


# ------------------------- round-2 blocker part 2: deletion at end of file
#
# Deleting the last table/key must keep the surviving last line's terminator —
# the survivor was mid-file and owned a newline; a CRLF file may never end on
# a lone \r, and an LF file must not lose its final \n.

BENCH_KEEP_CRLF = b'[bench]\r\nbackend = "keep" # untouched\r\n'
BENCH_KEEP_LF = b'[bench]\nbackend = "keep" # untouched\n'
POOL_CRLF = b'[pools.codex]\r\nclass = "spend"\r\nuntil = 2026-12-31\r\n'
POOL_LF = b'[pools.codex]\nclass = "spend"\nuntil = 2026-12-31\n'
POOL_SUB_CRLF = b"[pools.codex]\r\nsubscribed = false\r\n"
POOL_SUB_LF = b"[pools.codex]\nsubscribed = false\n"


@pytest.mark.parametrize(
    ("prefix", "pool"),
    [
        (BENCH_KEEP_CRLF, POOL_CRLF),
        (BENCH_KEEP_CRLF, POOL_CRLF[:-2]),
        (BENCH_KEEP_LF, POOL_LF),
        (BENCH_KEEP_LF, POOL_LF[:-1]),
        (BENCH_KEEP_CRLF, POOL_SUB_CRLF),
        (BENCH_KEEP_CRLF, POOL_SUB_CRLF[:-2]),
        (BENCH_KEEP_LF, POOL_SUB_LF),
        (BENCH_KEEP_LF, POOL_SUB_LF[:-1]),
    ],
)
def test_delete_table_at_eof(policy_config, prefix, pool):
    policy_config.write_bytes(prefix + pool)
    if b"subscribed" in pool:
        policy.set_subscribed("codex", None)
    else:
        assert policy.clear_policy("codex")
    assert policy_config.read_bytes() == prefix


def test_remove_key_at_eof_no_final_newline(policy_config):
    """Removing the file's last line keeps the survivor's terminator."""
    policy_config.write_bytes(b'[pools.codex]\r\nclass = "spend"\r\nuntil = 2026-12-31')
    policy.clear_policy("codex")
    assert policy_config.read_bytes() == b""


def test_replace_value_last_line_no_final_newline(policy_config):
    """Replacing the file's last line keeps its unterminated shape."""
    until = str(STILL_ACTIVE).encode()
    policy_config.write_bytes(b'[pools.codex]\r\nclass = "hold"\r\nuntil = ' + until)
    policy.set_policy("codex", "spend", until=STILL_ACTIVE)
    assert policy_config.read_bytes() == b'[pools.codex]\r\nclass = "spend"\r\nuntil = ' + until
    policy_config.write_bytes(b'[pools.codex]\nclass = "hold"\nuntil = ' + until)
    policy.set_policy("codex", "spend", until=STILL_ACTIVE)
    assert policy_config.read_bytes() == b'[pools.codex]\nclass = "spend"\nuntil = ' + until


def test_clear_at_eof_set_clear_restores(policy_config):
    """set then clear at EOF still restores the original bytes."""
    policy_config.write_bytes(BENCH_KEEP_LF)
    policy.set_policy("codex", "spend", until=STILL_ACTIVE)
    after_set = policy_config.read_bytes()
    assert after_set.startswith(BENCH_KEEP_LF)
    policy.clear_policy("codex")
    assert policy_config.read_bytes() == BENCH_KEEP_LF
