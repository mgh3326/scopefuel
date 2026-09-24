"""quota v2 contract r3 — property checks over generated inputs (fixed seeds).

Advice §9: generators must cover each category and the counts are asserted,
not left to seed luck. Seeds: QUOTA_V2_PROP_SEEDS=start:stop (default 0:200).
These check invariants the contract states in general form (PERM, MONO,
OLD-1, the §2 byte/control rule); exact codes are pinned by the corpus.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
import random
import sys
from collections import Counter

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import quota_v2_corpus as corpus  # noqa: E402

from scopefuel.quota_v2_contract import valid_post  # noqa: E402
from scopefuel.quota_v2_eval import Clock, evaluate  # noqa: E402


def seeds() -> range:
    start, stop = (int(x) for x in os.environ.get("QUOTA_V2_PROP_SEEDS", "0:200").split(":"))
    return range(start, stop)


CONTROL = ["\u0000", "\u0007", "\u001f", "\u007f", "\u0080", "\u0085", "\u009f"]
SAFE = ["a", "가", "é", "😀", " ", "-", "Z"]


def label_case(rng: random.Random) -> tuple[str, str, bool]:
    """A label near the byte boundaries, maybe with a control char; returns (category, value, valid)."""
    target = rng.choice([127, 128, 129, 256])
    unit = rng.choice(SAFE)
    text = ""
    while len((text + unit).encode()) <= target:
        text += unit
    category = f"len{target}"
    if rng.random() < 0.3:
        position = rng.randrange(len(text) + 1)
        text = text[:position] + rng.choice(CONTROL) + text[position:]
        category = "control"
    valid = (
        bool(text)
        and len(text.encode()) <= 128
        and not any(ord(c) <= 0x1F or ord(c) == 0x7F or 0x80 <= ord(c) <= 0x9F for c in text)
    )
    return category, text, valid


def test_generated_labels_follow_the_byte_and_control_rule():
    counts = Counter()
    base = corpus.wire_envelope({"patch": {}})
    for seed in seeds():
        rng = random.Random(seed)
        category, text, valid = label_case(rng)
        counts[category] += 1
        env = corpus.wire_envelope({"patch": {}})
        env["buckets"][0]["label"] = text
        assert valid_post(env) is valid, (seed, category, len(text.encode()))
        env["buckets"][0]["scope"] = {"kind": "model", "ref": text}
        assert valid_post(env) is valid, (seed, "scope.ref", len(text.encode()))
    assert valid_post(base)
    required = corpus.CORPUS["categories"]["label_id_length_127_128_129_256"]["count"]
    assert sum(counts.values()) >= required and all(
        counts[k] > 0 for k in ("len127", "len128", "len129", "control")
    )


# ------------------------------------------------------------------ histories

MACHINES = {
    "own": ("node-a", "slot-a", 2),
    "own_other_slot": ("node-a", "slot-b", 4),
    "other": ("node-b", "slot-b1", 5),
    "other2": ("node-c", "slot-c1", 7),
}
KINDS = ["success"] * 4 + ["rate_limited", "transport_error", "partial", "parse", "duplicate", "auth"]


def history(rng: random.Random) -> tuple[list[dict], Clock, Counter]:
    tags: Counter = Counter()
    clock = rng.choice([Clock(0.0, "fixture"), Clock(10.0, "fixture"), Clock(None, "unknown")])
    specs = []
    for index in range(rng.randint(1, 5)):
        kind = rng.choice(KINDS)
        src = rng.choice(["own", "own", "own_other_slot", "other", "other2"])
        t = rng.choice([300, 480, 490, 500, 500, 505, 560])
        spec = {"id": f"o{index}", "src": src, "t": t}
        if kind in ("success", "partial"):
            five = rng.choice([10, 10, 95])
            spec["vals"] = {"claude.five_hour": [five, 10800], "claude.seven_day": [20, 345600]}
            if kind == "partial":
                spec["vals"] = {"claude.five_hour": [five, 10800]}
                spec.update(status="partial", error_ref="partial:required_missing")
            if rng.random() < 0.2:
                spec["vals"]["claude.five_hour"] = {
                    "used": five,
                    "reset": 10800,
                    "observed": t - rng.choice([5, 200]),
                }
                tags["observed_at"] += 1
        elif kind == "duplicate":
            spec.update(status="parse_error", error_ref="parse_error:duplicate_limit")
        elif kind == "parse":
            spec.update(status="parse_error", error_ref="parse_error:schema")
        elif kind == "auth":
            spec.update(status="auth_error", error_ref="auth_error:http_401")
        else:
            spec.update(
                status=kind,
                error_ref="rate_limited:http_429" if kind == "rate_limited" else "transport_error:network",
            )
        specs.append(spec)
    times = Counter(s["t"] for s in specs)
    tags["same_instant"] += sum(1 for c in times.values() if c > 1)
    tags["cross_machine"] += len({MACHINES[s["src"]][0] for s in specs}) > 1
    tags["other_slot_same_machine"] += any(s["src"] == "own_other_slot" for s in specs)
    tags["auth"] += any(s.get("status") == "auth_error" for s in specs)
    tags["old_success_newer_error"] += any(
        a.get("status", "success") == "success"
        and b.get("status") in ("parse_error", "partial")
        and b["t"] > a["t"]
        for a in specs
        for b in specs
    )
    for spec in specs:
        machine, slot, revision = MACHINES[spec.pop("src")]
        spec["src"] = {"machine": machine, "slot": slot, "revision": revision}
    return specs, clock, tags


def run(specs: list[dict], clock: Clock, *, now: float = 600.0):
    case = {"now": now}
    observations = [corpus.observation(case, spec) for spec in specs]
    result = evaluate(
        corpus.snapshot(case, observations),
        "opus",
        now=corpus.now(case),
        clock=clock,
        support_list=["claude"],
        definitions=corpus.CORPUS_DEFINITIONS,
    )
    return result, observations


def key(result) -> tuple:
    return (
        result.code,
        result.ok,
        result.reason,
        tuple(sorted(result.selected)),
        tuple(sorted(result.excluded.items())),
    )


@pytest.mark.parametrize("seed", seeds())
def test_history_invariants(seed):
    rng = random.Random(seed)
    specs, clock, _tags = history(rng)
    result, observations = run(specs, clock)
    # PERM
    for order in itertools.permutations(range(len(specs))):
        assert key(run([specs[i] for i in order], clock)[0]) == key(result)
    # MONO: a provably later own-slot parse/duplicate/partial/auth never ends in allow
    latest = max(s["t"] for s in specs) + 25  # > 2Δ for Δ ≤ 10
    own = {"machine": "node-a", "slot": "slot-a", "revision": 2}
    for extra in (
        {"status": "parse_error", "error_ref": "parse_error:schema"},
        {"status": "parse_error", "error_ref": "parse_error:duplicate_limit"},
        {
            "status": "partial",
            "error_ref": "partial:required_missing",
            "vals": {"claude.five_hour": [1, 10800]},
        },
        {"status": "auth_error", "error_ref": "auth_error:http_401"},
    ):
        spec = {"id": "late", "src": own, "t": latest, **extra}
        assert not run([*specs, spec], clock, now=latest + 10)[0].ok
    # OLD-1: an allow never comes from a success that a non-success could follow
    if result.ok and clock.skew_bound_s is not None:
        by_id = Counter(result.selected)
        chosen = [o for o in observations if by_id[o["observation_id"]] and o["status"] == "success"]
        chosen_at = max(dt.datetime.fromisoformat(o["measured_at"].replace("Z", "+00:00")) for o in chosen)
        later_bad = [
            o
            for o in observations
            if o["status"] in ("parse_error", "partial")
            and dt.datetime.fromisoformat(o["measured_at"].replace("Z", "+00:00"))
            > chosen_at + dt.timedelta(seconds=2 * clock.skew_bound_s)
        ]
        assert not later_bad, (seed, result.code)


def test_history_generator_covers_every_category():
    total: Counter = Counter()
    for seed in seeds():
        total += history(random.Random(seed))[2]
    categories = corpus.CORPUS["categories"]
    assert total["same_instant"] >= categories["same_instant_permutation_duplicate"]["count"]
    assert total["cross_machine"] >= categories["clock_interval_overlap"]["count"]
    assert total["other_slot_same_machine"] >= categories["slot_identity"]["count"]
    assert total["old_success_newer_error"] >= categories["old_success_vs_newer_error"]["count"]
    assert total["observed_at"] >= categories["time_fields"]["count"]
    assert total["auth"] > 0
