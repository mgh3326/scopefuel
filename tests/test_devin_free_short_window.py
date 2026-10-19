from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from scopefuel import cli, free_window, recommend
from scopefuel.model import Bucket, ProviderResult, Scope

UTC = dt.UTC
FIXED_NOW = dt.datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
FREE_PROFILES = (
    "devin-swe2",
    "devin-swe2-medium",
    "devin-swe2-max",
    "builder-devin",
    "builder-devin-medium",
    "builder-devin-max",
)
PAID_PROFILES = (
    "devin-ds41",
    "devin-ds41-max",
    "devin-fusion-opus55",
    "devin-fusion-sonnet55",
    "devin-argon",
    "devin-argon-medium",
    "devin-argon-low",
)


class Patcher:
    def __init__(self):
        self.undo = []

    def setattr(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.undo.append(lambda: setattr(obj, name, old))

    def setenv(self, name, value):
        old = os.environ.get(name)
        os.environ[name] = value
        self.undo.append(
            lambda: os.environ.__setitem__(name, old) if old is not None else os.environ.pop(name, None)
        )

    def delenv(self, name):
        old = os.environ.pop(name, None)
        self.undo.append(lambda: os.environ.__setitem__(name, old) if old is not None else None)

    def restore(self):
        for action in reversed(self.undo):
            action()
        self.undo.clear()


class _FrozenDateTime(dt.datetime):
    fixed = FIXED_NOW

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.fixed.replace(tzinfo=None)
        return cls.fixed.astimezone(tz)


def _devin_result() -> ProviderResult:
    return ProviderResult(
        id="devin",
        buckets=[
            Bucket("daily", "1d", 10.0, "2026-10-20T08:00:00Z", Scope("account"), "now"),
            Bucket("weekly", "7d", 10.0, "2026-10-26T08:00:00Z", Scope("account"), "week"),
            Bucket("swe-2-high", "", 0.0, None, Scope("model", "swe-2-high"), "now", "Free tag observed"),
            Bucket("swe-2-medium", "", 0.0, None, Scope("model", "swe-2-medium"), "now", "Free tag observed"),
            Bucket("swe-2-max", "", 0.0, None, Scope("model", "swe-2-max"), "now", "Free tag observed"),
        ],
        source="test stub",
        pool_class="spend",
    )


class DevinFreeShortWindowTests(unittest.TestCase):
    def test_corrupt_timezone_overflow_is_ignored(self):
        for stamp in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-12:00"):
            with self.subTest(stamp=stamp):
                payload = {
                    "schema": free_window.SCHEMA,
                    "records": {
                        "host-user-corrupt": {
                            "reset_at": stamp,
                            "observed_at": stamp,
                            "rung": "devin-swe2",
                            "source": "job:corrupt-time",
                        }
                    },
                }
                free_window.state_path().parent.mkdir(parents=True, exist_ok=True)
                free_window.state_path().write_text(json.dumps(payload))
                with self.assertLogs("scopefuel.free_window", level="WARNING"):
                    self.assertIsNone(free_window.active_record(now=FIXED_NOW))
                with self.assertRaises(ValueError):
                    free_window.parse_aware(stamp)

    def setUp(self):
        self.patcher = Patcher()
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        for name in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
            path = root / name.lower()
            path.mkdir(parents=True, exist_ok=True)
            self.patcher.setenv(name, str(path))
        self.patcher.delenv("SCOPEFUEL_CACHE")

    def tearDown(self):
        self.patcher.restore()
        self.temp.cleanup()

    def freeze_cli(self, moment=FIXED_NOW):
        class Frozen(_FrozenDateTime):
            fixed = moment

        self.patcher.setattr(cli, "dt", SimpleNamespace(datetime=Frozen, UTC=UTC))

    def freeze_free_window(self, moment=FIXED_NOW):
        class Frozen(_FrozenDateTime):
            fixed = moment

        self.patcher.setattr(
            free_window,
            "dt",
            SimpleNamespace(datetime=Frozen, UTC=UTC, timedelta=dt.timedelta),
        )

    def seed_record(self, record):
        path = free_window.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": free_window.SCHEMA,
            "records": {free_window._identity_key(): record.as_dict()},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def gate_stubs(self, result=None):
        provider = result or _devin_result()
        self.patcher.setattr(cli, "registry", lambda: {"devin": lambda: provider})
        self.patcher.setattr(cli, "collect", lambda *args, **kwargs: [provider])
        self.patcher.setattr(cli.bench, "read_scores", lambda: [])
        self.patcher.setattr(cli.bench, "read_prices", lambda: {})
        self.patcher.setattr(cli.bench, "runtime_grade_table", lambda: recommend.GRADE_TABLE)
        self.patcher.setattr(cli, "_retired_e6_rungs", lambda: frozenset())
        self.patcher.setattr(cli, "_shadow_gate", lambda *args, **kwargs: None)
        self.patcher.setattr(cli, "_emit_stale_build_warning", lambda: None)

    def call_gate(self, profile):
        self.gate_stubs()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli.main(["gate", "-m", profile, "--no-cache"])
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_observe_cli_normalizes_timezone_and_never_builds_provider_registry(self):
        self.freeze_free_window()
        calls = []
        self.patcher.setattr(free_window.cache, "_load", lambda: calls.append("cache") or {})
        self.patcher.setattr(cli, "registry", lambda: self.fail("observer must not construct providers"))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli.main(
                [
                    "observe",
                    "devin-free-limit",
                    "--reset",
                    "2026-10-19T17:50:00+09:00",
                    "--observed",
                    "2026-10-19T17:00:00+09:00",
                    "--rung",
                    "devin-swe2",
                    "--source",
                    "job:w1690-impl",
                ]
            )
        record = free_window.active_record(now=FIXED_NOW)
        self.assertEqual(rc, 0)
        self.assertIn("recorded", stdout.getvalue())
        self.assertIsNotNone(record)
        self.assertEqual(record.reset_at, dt.datetime(2026, 10, 19, 8, 50, tzinfo=UTC))
        self.assertEqual(record.observed_at, FIXED_NOW)
        self.assertEqual(calls, ["cache", "cache"])
        self.assertEqual(stderr.getvalue(), "")

    def test_observe_rejects_naive_times_and_expired_records_are_not_saved(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            free_window.record_observation(
                reset="2026-10-19T09:00:00",
                observed="2026-10-19T08:00:00Z",
                rung="devin-swe2",
                source="job:naive",
                now=FIXED_NOW,
            )
        saved = free_window.record_observation(
            reset="2026-10-19T07:59:00Z",
            observed="2026-10-19T07:00:00Z",
            rung="devin-swe2",
            source="job:expired",
            now=FIXED_NOW,
        )
        self.assertFalse(saved)
        self.assertIsNone(free_window.active_record(now=FIXED_NOW))

    def test_cached_account_identity_is_used_without_reading_credentials(self):
        self.patcher.setattr(
            free_window.cache,
            "_load",
            lambda: {"devin": {"result": {"account_fp": "opaque-fingerprint", "account_fp_kind": "account"}}},
        )
        self.patcher.setattr(free_window.socket, "gethostname", lambda: self.fail("host fallback not used"))
        self.assertTrue(free_window._identity_key().startswith("account-"))

    def test_concurrent_writers_keep_newest_and_atomic_file_is_private_and_complete(self):
        def write(index):
            observed = FIXED_NOW - dt.timedelta(minutes=20 - index)
            reset = observed + dt.timedelta(minutes=50)
            return free_window.record_observation(
                reset=free_window.format_utc(reset),
                observed=free_window.format_utc(observed),
                rung="devin-swe2-medium" if index % 2 else "devin-swe2",
                source=f"job:parallel-{index}",
                now=FIXED_NOW,
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(write, range(12)))
        path = free_window.state_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = free_window.active_record(now=FIXED_NOW)
        self.assertIsNotNone(record)
        self.assertEqual(record.observed_at, FIXED_NOW - dt.timedelta(minutes=9))
        self.assertEqual(record.source, "job:parallel-11")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(path.parent.glob(path.name + ".*.tmp")), [])
        self.assertEqual(payload["schema"], free_window.SCHEMA)

    def test_corrupt_file_logs_and_fails_open_for_admission(self):
        self.freeze_cli()
        self.gate_stubs()
        path = free_window.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            self.assertLogs("scopefuel.free_window", level="WARNING") as records,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            rc = cli.main(["gate", "-m", "devin-swe2", "--no-cache"])
        self.assertEqual(rc, 0)
        self.assertNotIn("free_rate_limited", stdout.getvalue() + stderr.getvalue())
        self.assertIn("corrupt Devin free-window state", "\n".join(records.output))

    def test_live_record_refuses_every_free_spelling_and_paid_outputs_match_base(self):
        self.freeze_cli()
        self.gate_stubs()
        baseline_paid = {profile: self.call_gate(profile) for profile in PAID_PROFILES}
        baseline_free = {profile: self.call_gate(profile) for profile in FREE_PROFILES}
        self.assertTrue(all(result[0] == 0 for result in baseline_free.values()))

        observed = FIXED_NOW - dt.timedelta(minutes=5)
        reset = FIXED_NOW + dt.timedelta(minutes=45)
        self.seed_record(free_window.FreeLimitRecord(reset, observed, "devin-swe2", "job:known-limit"))

        for profile in FREE_PROFILES:
            rc, stdout, stderr = self.call_gate(profile)
            self.assertEqual(rc, 3, profile)
            self.assertIn("free_rate_limited", stderr, profile)
            self.assertIn("until 2026-10-19T08:45:00Z", stderr, profile)
            self.assertIn("observed 2026-10-19T07:55:00Z via job:known-limit", stderr, profile)
            self.assertNotIn("used_pct=0.0", stdout)

        for profile in PAID_PROFILES:
            actual = self.call_gate(profile)
            self.assertEqual(actual, baseline_paid[profile], profile)

    def test_paid_devin_observation_records_actual_rung_but_only_gates_free_profiles(self):
        self.freeze_cli()
        self.freeze_free_window()
        self.gate_stubs()
        baseline_paid = {profile: self.call_gate(profile) for profile in PAID_PROFILES}
        observed = "2026-10-19T07:55:00Z"
        reset = "2026-10-19T08:45:00Z"
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli.main(
                [
                    "observe",
                    "devin-free-limit",
                    "--reset",
                    reset,
                    "--observed",
                    observed,
                    "--rung",
                    "devin-ds41",
                    "--source",
                    "job:paid-devin-observation",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertIn("recorded", stdout.getvalue())
        record = free_window.active_record(now=FIXED_NOW)
        self.assertIsNotNone(record)
        self.assertEqual(record.rung, "devin-ds41")

        for profile in FREE_PROFILES:
            rc, _, gate_stderr = self.call_gate(profile)
            self.assertEqual(rc, 3, profile)
            self.assertIn("free_rate_limited", gate_stderr, profile)
            self.assertIn("rung devin-ds41", gate_stderr, profile)
        for profile in PAID_PROFILES:
            self.assertEqual(self.call_gate(profile), baseline_paid[profile], profile)
        self.assertFalse(free_window.is_free_profile("devin-ds41"))

    def test_expired_yesterday_and_after_reset_gate_outputs_match_base(self):
        self.freeze_cli()
        self.gate_stubs()
        baseline = self.call_gate("devin-swe2")
        baseline_builder = self.call_gate("builder-devin-max")
        self.seed_record(
            free_window.FreeLimitRecord(
                FIXED_NOW - dt.timedelta(days=1),
                FIXED_NOW - dt.timedelta(days=1, minutes=50),
                "devin-swe2-max",
                "job:yesterday",
            )
        )
        self.assertEqual(self.call_gate("devin-swe2"), baseline)

        reset = FIXED_NOW + dt.timedelta(minutes=50)
        self.seed_record(free_window.FreeLimitRecord(reset, FIXED_NOW, "devin-swe2", "job:reset"))
        self.freeze_cli(reset)
        reset_baselines = {profile: self.call_gate(profile) for profile in FREE_PROFILES}
        self.assertEqual(reset_baselines["devin-swe2"], baseline)
        self.assertEqual(reset_baselines["builder-devin-max"], baseline_builder)
        for profile in FREE_PROFILES:
            self.seed_record(free_window.FreeLimitRecord(reset, FIXED_NOW, "devin-swe2", "job:reset"))
            self.assertEqual(self.call_gate(profile), reset_baselines[profile])

    def test_live_record_refuses_when_provider_quota_is_missing(self):
        self.freeze_cli()
        self.gate_stubs()
        reset = FIXED_NOW + dt.timedelta(minutes=45)
        self.seed_record(
            free_window.FreeLimitRecord(
                reset,
                FIXED_NOW - dt.timedelta(minutes=5),
                "devin-swe2-max",
                "job:no-provider-data",
            )
        )
        self.patcher.setattr(cli, "collect", lambda *args, **kwargs: [])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli.main(["gate", "-m", "devin-swe2-medium", "--no-cache"])
        self.assertEqual(rc, 3)
        self.assertIn("free_rate_limited until 2026-10-19T08:45:00Z", stderr.getvalue())

    def _recommend_setup(self):
        profile_by_name = {
            profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows
        }
        custom_table = {grade: [] for grade in recommend.GRADE_TABLE}
        custom_table["A+"] = [profile_by_name[name] for name in FREE_PROFILES[:3]] + [
            profile_by_name["codex-luna"]
        ]
        results = [
            _devin_result(),
            ProviderResult(
                id="codex",
                buckets=[Bucket("week", "7d", 10.0, "2026-10-26T08:00:00Z", Scope("account"), "week")],
                pool_class="preserve",
            ),
        ]
        return custom_table, results

    def test_recommend_demotes_free_rows_and_drops_them_without_an_alternative(self):
        profile_by_name = {
            profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows
        }
        custom_table = {grade: [] for grade in recommend.GRADE_TABLE}
        custom_table["A+"] = [profile_by_name[name] for name in FREE_PROFILES[:3]] + [
            profile_by_name["codex-luna"]
        ]
        providers = [
            _devin_result(),
            ProviderResult(
                id="codex",
                buckets=[Bucket("week", "7d", 10.0, "2026-10-26T08:00:00Z", Scope("account"), "week")],
                pool_class="preserve",
            ),
        ]
        reason = (
            "free_rate_limited until 2026-10-19T08:45:00Z "
            "(observed 2026-10-19T07:55:00Z via job:known-limit; rung devin-swe2)"
        )
        base = recommend.recommend_dict(
            providers, "A+", today=FIXED_NOW.date(), now=FIXED_NOW, grade_table=custom_table
        )
        limited = recommend.recommend_dict(
            providers,
            "A+",
            today=FIXED_NOW.date(),
            now=FIXED_NOW,
            grade_table=custom_table,
            free_rate_limit=reason,
        )
        kinds = {(row["profile"], row["kind"]) for row in limited["rows"]}
        self.assertIn(("codex-luna", "candidate"), kinds)
        for name in FREE_PROFILES[:3]:
            self.assertIn((name, "free_rate_limited"), kinds)
        self.assertTrue(
            all(row.get("reason") == reason for row in limited["rows"] if row["kind"] == "free_rate_limited")
        )
        self.assertTrue(
            any(row["profile"] == "devin-swe2" and row["kind"] == "candidate" for row in base["rows"])
        )

        limited_only = {grade: [] for grade in recommend.GRADE_TABLE}
        limited_only["A+"] = [profile_by_name[name] for name in FREE_PROFILES[:3]]
        no_alternative = recommend.recommend_dict(
            [_devin_result()],
            "A+",
            today=FIXED_NOW.date(),
            now=FIXED_NOW,
            grade_table=limited_only,
            free_rate_limit=reason,
        )
        self.assertFalse(any(row["kind"] == "candidate" for row in no_alternative["rows"]))
        self.assertEqual({row["kind"] for row in no_alternative["rows"]}, {"free_rate_limited"})

    def test_cli_recommendation_is_byte_identical_without_or_after_live_record(self):
        self.freeze_cli()
        custom_table, results = self._recommend_setup()
        self.patcher.setattr(cli, "registry", lambda: {"devin": object(), "codex": object()})
        self.patcher.setattr(cli, "collect", lambda *args, **kwargs: results)
        self.patcher.setattr(cli.manual, "apply_for_display", lambda providers, now: providers)
        self.patcher.setattr(cli.bench, "read_scores", lambda: [])
        self.patcher.setattr(cli.bench, "read_prices", lambda: {})
        self.patcher.setattr(cli.bench, "runtime_grade_table", lambda: custom_table)
        self.patcher.setattr(cli.bench, "read_catalog", lambda: SimpleNamespace(label="catalog=stale"))
        self.patcher.setattr(cli, "_emit_stale_build_warning", lambda: None)

        def run(json_output=False):
            args = ["--recommend", "A+"]
            if json_output:
                args.insert(0, "--json")
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = cli.main(args)
            return rc, stdout.getvalue(), stderr.getvalue()

        baseline = run()
        baseline_json = run(json_output=True)
        reset = FIXED_NOW + dt.timedelta(minutes=50)
        self.seed_record(free_window.FreeLimitRecord(reset, FIXED_NOW, "devin-swe2", "job:expired-at-reset"))
        self.freeze_cli(reset)
        self.assertEqual(run(), baseline)
        self.assertEqual(run(json_output=True), baseline_json)

        self.freeze_cli(FIXED_NOW)
        self.seed_record(
            free_window.FreeLimitRecord(
                FIXED_NOW + dt.timedelta(minutes=45),
                FIXED_NOW - dt.timedelta(minutes=5),
                "devin-swe2",
                "job:known-limit",
            )
        )
        active_json = run(json_output=True)
        payload = json.loads(active_json[1])
        rows = payload["rows"]
        self.assertTrue(any(row["profile"] == "codex-luna" and row["kind"] == "candidate" for row in rows))
        limited_rows = [row for row in rows if row["kind"] == "free_rate_limited"]
        self.assertEqual({row["profile"] for row in limited_rows}, set(FREE_PROFILES[:3]))
        self.assertTrue(
            all("free_rate_limited until 2026-10-19T08:45:00Z" in row["reason"] for row in limited_rows)
        )

    def test_free_limit_does_not_claim_healthy_alternatives_are_exhausted(self):
        self.freeze_cli()
        self.gate_stubs()
        table, providers = self._recommend_setup()
        self.patcher.setattr(cli, "collect", lambda *args, **kwargs: providers)
        self.patcher.setattr(cli.bench, "runtime_grade_table", lambda: table)
        record = free_window.FreeLimitRecord(
            FIXED_NOW + dt.timedelta(minutes=45), FIXED_NOW, "devin-swe2", "job:free-only"
        )
        self.seed_record(record)
        rows = recommend.recommend_dict(
            providers,
            "A+",
            today=FIXED_NOW.date(),
            now=FIXED_NOW,
            grade_table=table,
            free_rate_limit=free_window.reason(record),
        )["rows"]
        self.assertTrue(any(row["profile"] == "codex-luna" and row["kind"] == "candidate" for row in rows))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli.main(["gate", "-m", "devin-swe2", "--no-cache"])
        self.assertEqual(rc, 3)
        self.assertIn("free_rate_limited", stderr.getvalue())
        self.assertNotIn("정상 후보 전부 소진/측정불가", stderr.getvalue())

    def test_json_and_table_add_short_window_without_changing_used_pct(self):
        self.freeze_cli()
        self.patcher.setattr(cli, "_stale_build_verdict", lambda: None)
        result = _devin_result()
        args = SimpleNamespace(raw=False, json=True, brief=False, no_color=True, horizon="both")
        rendered = json.loads(cli._render([result], args, FIXED_NOW))
        buckets = rendered["providers"][0]["buckets"]
        self.assertEqual([bucket["used_pct"] for bucket in buckets[-3:]], [0.0, 0.0, 0.0])
        self.assertEqual({bucket["free_short_window"] for bucket in buckets[-3:]}, {"not observed"})

        self.seed_record(
            free_window.FreeLimitRecord(
                FIXED_NOW + dt.timedelta(minutes=45),
                FIXED_NOW - dt.timedelta(minutes=5),
                "devin-swe2",
                "job:known-limit",
            )
        )
        limited = json.loads(cli._render([result], args, FIXED_NOW))
        self.assertEqual(
            {bucket["free_short_window"] for bucket in limited["providers"][0]["buckets"][-3:]},
            {"limited until 2026-10-19T08:45:00Z via job:known-limit"},
        )
        args.json = False
        table = cli._render([result], args, FIXED_NOW)
        self.assertEqual(
            table.count("free_short_window=limited until 2026-10-19T08:45:00Z via job:known-limit"), 3
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
