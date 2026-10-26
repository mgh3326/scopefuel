from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import io
import json
import os
import tempfile
import threading
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
        self.patcher.setattr(cli, "_retired_rungs", lambda: frozenset())
        self.patcher.setattr(cli, "_consult_only_rungs", lambda: frozenset())
        self.patcher.setattr(cli, "_gate_policy_context", lambda *args, **kwargs: None)
        self.patcher.setattr(
            cli.bench, "read_catalog", lambda **kwargs: self.fail("gate must not read the catalog")
        )
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

    def test_deep_json_and_oversized_integer_fail_open(self):
        path = free_window.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for label, contents in (
            ("deep nesting", "[" * 300_000),
            ("oversized integer", "9" * 5_000),
        ):
            with self.subTest(label=label):
                path.write_text(contents, encoding="utf-8")
                with self.assertLogs("scopefuel.free_window", level="WARNING"):
                    self.assertIsNone(free_window.active_record(now=FIXED_NOW))

    def test_future_observation_is_rejected_and_future_record_cannot_win(self):
        with self.assertRaisesRegex(ValueError, "future"):
            free_window.record_observation(
                reset="2030-01-01T12:00:00Z",
                observed="2030-01-01T00:00:00Z",
                rung="devin-swe2",
                source="job:future-input",
                now=FIXED_NOW,
            )

        future = free_window.FreeLimitRecord(
            dt.datetime(2030, 1, 1, 12, tzinfo=UTC),
            dt.datetime(2030, 1, 1, 0, tzinfo=UTC),
            "devin-swe2",
            "job:future-record",
        )
        self.seed_record(future)
        self.assertIsNone(free_window.active_record(now=FIXED_NOW))

        self.assertTrue(
            free_window.record_observation(
                reset="2026-10-19T08:45:00Z",
                observed="2026-10-19T07:59:00Z",
                rung="devin-swe2-medium",
                source="job:real-current",
                now=FIXED_NOW,
            )
        )
        current = free_window.active_record(now=FIXED_NOW)
        self.assertIsNotNone(current)
        self.assertEqual(current.source, "job:real-current")
        payload = json.loads(free_window.state_path().read_text(encoding="utf-8"))
        self.assertEqual(len(payload["records"]), 1)

    def test_live_observation_notes_existing_refusal_for_free_profiles(self):
        self.freeze_cli()
        self.gate_stubs()
        original_active_record = free_window.active_record
        lookups = []

        def tracked_active_record(*, now=None):
            lookups.append(now)
            return original_active_record(now=now)

        self.patcher.setattr(free_window, "active_record", tracked_active_record)
        refusals = (
            ("role", "role_restricted: base refusal", {"role_denied": True}, 5),
            ("subscription", "unsubscribed: base refusal", {"unsubscribed": True}, 6),
            (
                "quota",
                "ordinary quota refusal",
                {"alternatives": ("devin-swe2-medium", "codex-luna")},
                3,
            ),
        )
        for profile in FREE_PROFILES:
            for label, reason, flags, expected_rc in refusals:
                with self.subTest(profile=profile, refusal=label):
                    result = recommend.GateResult(
                        ok=False,
                        profile=profile,
                        provider_id="devin",
                        grade="A",
                        reason=reason,
                        **flags,
                    )
                    self.patcher.setattr(
                        cli.recommend,
                        "gate_check",
                        lambda *args, result=result, **kwargs: result,
                    )
                    self.patcher.setattr(free_window, "active_record", tracked_active_record)
                    self._save_empty_records()
                    baseline = self.call_gate(profile)
                    self.assertEqual(baseline[0], expected_rc)
                    self.seed_record(
                        free_window.FreeLimitRecord(
                            FIXED_NOW + dt.timedelta(minutes=45),
                            FIXED_NOW - dt.timedelta(minutes=5),
                            "devin-swe2",
                            "job:known-limit",
                        )
                    )
                    observed = self.call_gate(profile)
                    self.assertEqual(observed[0], baseline[0])
                    self.assertEqual(observed[1], baseline[1])
                    self.assertIn(reason, observed[2])
                    self.assertIn(
                        "last observed free_rate_limited until 2026-10-19T08:45:00Z "
                        "(observed 2026-10-19T07:55:00Z via job:known-limit)",
                        observed[2],
                    )

    def _save_empty_records(self):
        free_window._save_records({})

    def test_paid_and_non_devin_gate_paths_do_not_read_free_window_state(self):
        self.freeze_cli()
        self.gate_stubs()
        original_active_record = free_window.active_record
        lookups = []

        def tracked_active_record(*, now=None):
            lookups.append(now)
            return original_active_record(now=now)

        self.patcher.setattr(free_window, "active_record", tracked_active_record)

        def allow(profile_name, *args, **kwargs):
            return recommend.GateResult(
                ok=True,
                profile=profile_name,
                provider_id="codex" if profile_name == "codex-luna" else "devin",
                grade="A",
                reason="base admitted",
                used_pct=0.0,
                pool_class="spend",
            )

        self.patcher.setattr(cli.recommend, "gate_check", allow)
        allowed = []
        for profile in (*PAID_PROFILES, "codex-luna"):
            before = len(lookups)
            baseline = self.call_gate(profile)
            allowed.append((profile, baseline))
            self.assertEqual(len(lookups), before, profile)
        self.seed_record(
            free_window.FreeLimitRecord(
                FIXED_NOW + dt.timedelta(minutes=45),
                FIXED_NOW - dt.timedelta(minutes=5),
                "devin-swe2",
                "job:known-limit",
            )
        )
        for profile, baseline in allowed:
            with self.subTest(profile=profile):
                before = len(lookups)
                self.assertEqual(self.call_gate(profile), baseline)
                self.assertEqual(len(lookups), before, profile)

        def refuse(profile_name, *args, **kwargs):
            return recommend.GateResult(
                ok=False,
                profile=profile_name,
                provider_id="codex" if profile_name == "codex-luna" else "devin",
                grade="A",
                reason="base quota refusal",
                alternatives=("devin-swe2", "codex-luna"),
            )

        self.patcher.setattr(cli.recommend, "gate_check", refuse)
        for profile in (*PAID_PROFILES, "codex-luna"):
            with self.subTest(refusal_profile=profile):
                self._save_empty_records()
                before = len(lookups)
                baseline = self.call_gate(profile)
                self.assertEqual(baseline[0], 3)
                self.assertIn("대안(A): devin-swe2, codex-luna", baseline[2])
                self.assertEqual(len(lookups), before, profile)
                self.seed_record(
                    free_window.FreeLimitRecord(
                        FIXED_NOW + dt.timedelta(minutes=45),
                        FIXED_NOW - dt.timedelta(minutes=5),
                        "devin-swe2",
                        "job:known-limit",
                    )
                )
                before_hold = len(lookups)
                self.assertEqual(self.call_gate(profile), baseline)
                self.assertEqual(len(lookups), before_hold, profile)

    def test_live_record_is_admitted_with_note_for_every_free_spelling(self):
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
            self.assertEqual(rc, 0, profile)
            self.assertIn(
                "last observed free_rate_limited until 2026-10-19T08:45:00Z "
                "(observed 2026-10-19T07:55:00Z via job:known-limit)",
                stdout,
                profile,
            )
            self.assertEqual(stderr, "", profile)

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
            rc, gate_stdout, gate_stderr = self.call_gate(profile)
            self.assertEqual(rc, 0, profile)
            self.assertIn("last observed free_rate_limited", gate_stdout, profile)
            self.assertIn("via job:paid-devin-observation", gate_stdout, profile)
            self.assertEqual(gate_stderr, "", profile)
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

    def test_live_record_does_not_change_unmeasurable_gate_status(self):
        self.freeze_cli()
        self.gate_stubs()
        reset = FIXED_NOW + dt.timedelta(minutes=45)
        self.patcher.setattr(cli, "collect", lambda *args, **kwargs: [])

        def run():
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = cli.main(["gate", "-m", "devin-swe2-medium", "--no-cache"])
            return rc, stdout.getvalue(), stderr.getvalue()

        baseline = run()
        self.assertEqual(baseline[0], 4)
        self.seed_record(
            free_window.FreeLimitRecord(
                reset,
                FIXED_NOW - dt.timedelta(minutes=5),
                "devin-swe2-max",
                "job:no-provider-data",
            )
        )
        observed = run()
        self.assertEqual(observed[0], baseline[0])
        self.assertIn("last observed free_rate_limited", observed[2])

    def test_recommendation_keeps_free_row_and_marks_it_for_fallback(self):
        profile_by_name = {
            profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows
        }
        table = {grade: [] for grade in recommend.GRADE_TABLE}
        table["A+"] = [
            profile_by_name["devin-swe2"],
            profile_by_name["devin-swe2-medium"],
            profile_by_name["codex-luna"],
        ]
        providers = [
            _devin_result(),
            ProviderResult(
                id="codex",
                buckets=[Bucket("week", "7d", 10.0, "2026-10-26T08:00:00Z", Scope("account"), "week")],
                pool_class="preserve",
            ),
        ]
        note = (
            "last observed free_rate_limited until 2026-10-19T08:45:00Z "
            "(observed 2026-10-19T07:55:00Z via job:known-limit)"
        )
        original_subscription = recommend.profile_subscription

        def mixed_subscription(name, provider_id=None):
            if name == "devin-swe2-medium":
                return recommend.Subscription(False, "profile", "profiles.devin-swe2-medium", "devin")
            return original_subscription(name, provider_id)

        self.patcher.setattr(recommend, "profile_subscription", mixed_subscription)
        baseline = recommend.recommend_dict(
            providers, "A+", today=FIXED_NOW.date(), now=FIXED_NOW, grade_table=table
        )
        limited = recommend.recommend_dict(
            providers,
            "A+",
            today=FIXED_NOW.date(),
            now=FIXED_NOW,
            grade_table=table,
            free_rate_limit=note,
        )
        baseline_rows = {row["profile"]: row for row in baseline["rows"]}
        limited_rows = {row["profile"]: row for row in limited["rows"]}
        self.assertEqual(baseline_rows["devin-swe2-medium"]["kind"], "unsubscribed")
        self.assertEqual(limited_rows["devin-swe2-medium"], baseline_rows["devin-swe2-medium"])
        self.assertEqual(baseline_rows["devin-swe2"]["kind"], "candidate")
        self.assertEqual(limited_rows["devin-swe2"]["kind"], "candidate")
        self.assertIn(note, limited_rows["devin-swe2"]["windows"])
        candidate_rows = [row for row in limited["rows"] if row["kind"] == "candidate"]
        self.assertEqual(candidate_rows[0]["profile"], "devin-swe2")
        self.assertEqual(candidate_rows[1]["profile"], "codex-luna")

    def test_recommendation_keeps_unsubscribed_unmeasurable_and_exhausted_reasons(self):
        profile_by_name = {
            profile.name: profile for rows in recommend.GRADE_TABLE.values() for profile in rows
        }
        table = {grade: [] for grade in recommend.GRADE_TABLE}
        table["A+"] = [profile_by_name["devin-swe2"], profile_by_name["codex-luna"]]
        codex = ProviderResult(
            id="codex",
            buckets=[Bucket("week", "7d", 10.0, "2026-10-26T08:00:00Z", Scope("account"), "week")],
            pool_class="preserve",
        )
        reason = (
            "free_rate_limited until 2026-10-19T08:45:00Z "
            "(observed 2026-10-19T07:55:00Z via job:known-limit; rung devin-swe2)"
        )
        original_subscription = recommend.profile_subscription
        self.patcher.setattr(
            recommend,
            "profile_subscription",
            lambda name, provider_id=None: (
                recommend.Subscription(False, "profile", "profiles.devin-swe2", "devin")
                if name == "devin-swe2"
                else original_subscription(name, provider_id)
            ),
        )
        self.patcher.setattr(recommend, "_observe_exhaustion", lambda *args, **kwargs: None)
        for label, providers, restore_subscription in (
            ("unsubscribed", [_devin_result(), codex], False),
            ("unmeasurable", [codex], True),
        ):
            with self.subTest(reason=label):
                if restore_subscription:
                    self.patcher.setattr(recommend, "profile_subscription", original_subscription)
                baseline = recommend.recommend_dict(
                    providers, "A+", today=FIXED_NOW.date(), now=FIXED_NOW, grade_table=table
                )
                limited = recommend.recommend_dict(
                    providers,
                    "A+",
                    today=FIXED_NOW.date(),
                    now=FIXED_NOW,
                    grade_table=table,
                    free_rate_limit=reason,
                )
                self.assertEqual(limited, baseline)
                self.assertNotEqual(baseline["rows"][0]["kind"], "free_rate_limited")

        saturated = _devin_result()
        saturated.buckets[2] = Bucket(
            "swe-2-high", "", 100.0, "2026-10-20T08:00:00Z", Scope("model", "swe-2-high"), "now"
        )
        self.patcher.setattr(recommend, "_pool_cutoff", lambda *args, **kwargs: (0.5, None))
        baseline = recommend.recommend_dict(
            [saturated, codex], "A+", today=FIXED_NOW.date(), now=FIXED_NOW, grade_table=table
        )
        limited = recommend.recommend_dict(
            [saturated, codex],
            "A+",
            today=FIXED_NOW.date(),
            now=FIXED_NOW,
            grade_table=table,
            free_rate_limit=reason,
        )
        self.assertEqual(limited, baseline)
        self.assertEqual(
            next(row for row in baseline["rows"] if row["profile"] == "devin-swe2")["kind"],
            "exhausted",
        )

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

    def test_recommend_lists_free_row_then_best_non_free_fallback(self):
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
        note = (
            "last observed free_rate_limited until 2026-10-19T08:45:00Z "
            "(observed 2026-10-19T07:55:00Z via job:known-limit)"
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
            free_rate_limit=note,
        )
        kinds = {(row["profile"], row["kind"]) for row in limited["rows"]}
        self.assertIn(("codex-luna", "candidate"), kinds)
        for name in FREE_PROFILES[:3]:
            self.assertIn((name, "candidate"), kinds)
        candidate_rows = [row for row in limited["rows"] if row["kind"] == "candidate"]
        self.assertEqual(candidate_rows[0]["profile"], "devin-swe2")
        self.assertEqual(candidate_rows[1]["profile"], "codex-luna")
        self.assertTrue(
            all(note in row["windows"] for row in candidate_rows if row["profile"] in FREE_PROFILES)
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
            free_rate_limit=note,
        )
        self.assertEqual(
            {row["kind"] for row in no_alternative["rows"]},
            {"candidate"},
        )

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
        candidate_rows = [row for row in rows if row["kind"] == "candidate"]
        self.assertEqual(candidate_rows[0]["profile"], "devin-swe2")
        self.assertEqual(candidate_rows[1]["profile"], "codex-luna")
        self.assertIn(
            "last observed free_rate_limited until 2026-10-19T08:45:00Z",
            candidate_rows[0]["windows"],
        )
        self.assertIn("observed 2026-10-19T07:55:00Z via job:known-limit", candidate_rows[0]["windows"])

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
            free_rate_limit=free_window.display_value(record),
        )["rows"]
        self.assertTrue(any(row["profile"] == "codex-luna" and row["kind"] == "candidate" for row in rows))
        self.assertEqual(
            [row["profile"] for row in rows if row["kind"] == "candidate"][:2],
            ["devin-swe2", "codex-luna"],
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli.main(["gate", "-m", "devin-swe2", "--no-cache"])
        self.assertEqual(rc, 0)
        self.assertIn("last observed free_rate_limited", stdout.getvalue())
        self.assertNotIn("정상 후보 전부 소진/측정불가", stderr.getvalue())

    def call_clear(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli.main(["observe", "devin-free-limit", "--clear", *extra])
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_clear_removes_live_record_and_gate_matches_no_record(self):
        self.freeze_cli()
        self.freeze_free_window()
        self.gate_stubs()
        baseline = {profile: self.call_gate(profile) for profile in FREE_PROFILES}
        record = free_window.FreeLimitRecord(
            FIXED_NOW + dt.timedelta(minutes=45),
            FIXED_NOW - dt.timedelta(minutes=5),
            "devin-swe2-medium",
            "job:clear-me",
        )
        self.seed_record(record)
        self.assertEqual(self.call_gate("devin-swe2")[0], 0)
        identity = free_window._identity_key()

        rc, stdout, stderr = self.call_clear()
        self.assertEqual(rc, 0)
        self.assertEqual(stderr, "")
        self.assertIn("2026-10-19T08:45:00Z", stdout)
        self.assertIn("2026-10-19T07:55:00Z", stdout)
        self.assertIn("job:clear-me", stdout)
        self.assertIn("devin-swe2-medium", stdout)
        self.assertNotIn(identity, stdout)
        self.assertNotIn("host-user-", stdout)
        self.assertNotIn("account-", stdout)
        self.assertIsNone(free_window.active_record(now=FIXED_NOW))
        self.assertEqual(json.loads(free_window.state_path().read_text())["records"], {})
        for profile in FREE_PROFILES:
            self.assertEqual(self.call_gate(profile), baseline[profile], profile)

    def test_clear_with_no_record_or_only_expired_exits_zero(self):
        self.freeze_free_window()
        rc, stdout, stderr = self.call_clear()
        self.assertEqual(rc, 0)
        self.assertEqual(stderr, "")
        self.assertIn("no live", stdout)
        self.assertFalse(free_window.state_path().exists())

        expired = free_window.FreeLimitRecord(
            FIXED_NOW - dt.timedelta(minutes=5),
            FIXED_NOW - dt.timedelta(minutes=55),
            "devin-swe2",
            "job:already-expired",
        )
        self.seed_record(expired)
        rc, stdout, _ = self.call_clear()
        self.assertEqual(rc, 0)
        self.assertIn("no live", stdout)
        self.assertEqual(json.loads(free_window.state_path().read_text())["records"], {})

    def test_clear_account_key_and_field_validation(self):
        self.freeze_free_window()
        key = "host-user-" + "ab" * 16
        other = free_window.FreeLimitRecord(
            FIXED_NOW + dt.timedelta(minutes=45),
            FIXED_NOW - dt.timedelta(minutes=5),
            "devin-swe2",
            "job:other-host",
        )
        path = free_window.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema": free_window.SCHEMA, "records": {key: other.as_dict()}}),
            encoding="utf-8",
        )
        rc, stdout, _ = self.call_clear()
        self.assertEqual(rc, 0)
        self.assertIn("no live", stdout)
        self.assertEqual(json.loads(path.read_text())["records"], {key: other.as_dict()})
        rc, stdout, _ = self.call_clear("--account-key", key)
        self.assertEqual(rc, 0)
        self.assertIn("job:other-host", stdout)
        self.assertNotIn(key, stdout)
        self.assertEqual(json.loads(path.read_text())["records"], {})

        rc, _, stderr = self.call_clear("--account-key", "bogus")
        self.assertEqual(rc, 2)
        self.assertNotEqual(stderr, "")

    def test_clear_rejects_mixed_flags_and_missing_fields(self):
        for argv, fragment in (
            (["observe", "devin-free-limit", "--clear", "--reset", "2026-10-19T09:00:00Z"], "--clear"),
            (
                [
                    "observe",
                    "devin-free-limit",
                    "--clear",
                    "--account-key",
                    "host-user-" + "ab" * 16,
                    "--rung",
                    "devin-swe2",
                ],
                "--clear",
            ),
            (["observe", "devin-free-limit", "--account-key", "host-user-" + "ab" * 16], "--account-key"),
            (["observe", "devin-free-limit", "--reset", "2026-10-19T09:00:00Z"], "required"),
        ):
            with self.subTest(argv=argv):
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as caught,
                ):
                    cli.main(argv)
                self.assertEqual(caught.exception.code, 2)
                self.assertIn(fragment, stderr.getvalue())

    def test_concurrent_clear_and_observe_leave_valid_state(self):
        self.freeze_free_window()
        record = free_window.FreeLimitRecord(
            FIXED_NOW + dt.timedelta(minutes=45),
            FIXED_NOW - dt.timedelta(minutes=5),
            "devin-swe2",
            "job:racy",
        )
        self.seed_record(record)

        def write(index):
            observed = FIXED_NOW - dt.timedelta(minutes=index)
            return free_window.record_observation(
                reset=free_window.format_utc(FIXED_NOW + dt.timedelta(minutes=50)),
                observed=free_window.format_utc(observed),
                rung="devin-swe2",
                source=f"job:racy-{index}",
                now=FIXED_NOW,
            )

        def clear(index):
            return free_window.clear_hold(now=FIXED_NOW)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: write(i) if i % 2 else clear(i), range(16)))
        payload = json.loads(free_window.state_path().read_text())
        self.assertEqual(payload["schema"], free_window.SCHEMA)
        record = free_window.active_record(now=FIXED_NOW)
        self.assertEqual(list(payload["records"]), [] if record is None else [free_window._identity_key()])

    def test_clear_waits_for_the_writer_lock(self):
        self.freeze_free_window()
        record = free_window.FreeLimitRecord(
            FIXED_NOW + dt.timedelta(minutes=45),
            FIXED_NOW - dt.timedelta(minutes=5),
            "devin-swe2",
            "job:lock-pinned",
        )
        self.seed_record(record)
        lock_handle = free_window.lock_path().open("a+")
        outcome = []

        def clear():
            outcome.append(free_window.clear_hold(now=FIXED_NOW))

        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            thread = threading.Thread(target=clear)
            thread.start()
            thread.join(2)
            self.assertTrue(thread.is_alive())
            self.assertEqual(outcome, [])
            self.assertIsNotNone(free_window.active_record(now=FIXED_NOW))
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsNone(free_window.active_record(now=FIXED_NOW))

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
            {
                "last observed free_rate_limited until 2026-10-19T08:45:00Z "
                "(observed 2026-10-19T07:55:00Z via job:known-limit)"
            },
        )
        args.json = False
        table = cli._render([result], args, FIXED_NOW)
        self.assertEqual(
            table.count(
                "free_short_window=last observed free_rate_limited until 2026-10-19T08:45:00Z "
                "(observed 2026-10-19T07:55:00Z via job:known-limit)"
            ),
            3,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
