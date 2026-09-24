"""§2 edge envelopes for the Go/Python parity check (§7.4).

Writes tests/fixtures/quota_v2_parity_r3.json; regenerate with
`uv run python tests/gen_quota_v2_parity.py`. Expected values are assigned here
by hand from the §2 rule text, not by running a validator.
"""

import copy
import json
import pathlib
import sys

sys.path.insert(0, "tests")
import quota_v2_corpus as corpus

base = corpus.wire_envelope({"patch": {}})
rows = []


def add(rid, expect, change, form="post", note=""):
    env = copy.deepcopy(base)
    change(env)
    rows.append({"id": rid, "form": form, "note": note, "envelope": env, "expect": expect})


def setf(path, value):
    def change(env):
        target, key = env, path
        if path.startswith("b0."):
            target, key = env["buckets"][0], path[3:]
        if key.startswith("scope."):
            target, key = target["scope"], key[6:]
        target[key] = value

    return change


times = [
    ("2026-09-24T01:08:20.5Z", "accept"),
    ("2026-09-24T01:08:20.1234567890Z", "accept"),
    ("2026-09-24T10:08:20+09:00", "accept"),
    ("2026-09-24T01:08:20-00:00", "accept"),
    ("9999-12-31T23:59:59Z", "accept"),
    ("0001-01-01T00:00:00Z", "accept"),
    ("2026-09-24T01:08:20+24:00", "reject"),
    ("2026-09-24T01:08:20+23:60", "reject"),
    ("2026-09-24T01:08:20+00:60", "reject"),
    ("2026-09-24T24:00:00Z", "reject"),
    ("2026-09-24T23:59:60Z", "reject"),
    ("2026-02-30T01:00:00Z", "reject"),
    ("0000-01-01T00:00:00Z", "reject"),
    ("0000-12-31T23:00:00-02:00", "reject"),
    ("0001-01-01T00:00:00+01:00", "reject"),
    ("9999-12-31T23:59:59-01:00", "reject"),
    ("2026-09-24t01:08:20z", "reject"),
    ("2026-09-24 01:08:20Z", "reject"),
    ("２０２６-09-24T01:08:20Z", "reject"),
    ("2026-09-24T01:08:20", "reject"),
    ("2026-09-24T01:08:20.Z", "reject"),
    ("2026-09-24T01:08Z", "reject"),
]
for i, (value, expect) in enumerate(times):
    add(f"P-T{i:02d}", expect, setf("measured_at", value), note=f"measured_at={value}")
    add(f"P-R{i:02d}", expect, setf("b0.reset_at", value), note=f"reset_at={value}")
for i, (value, expect) in enumerate(
    [
        ("unknown", "accept"),
        ("2026-09-24T04:00:00Z", "accept"),
        ("2026-99-99T99:99:99Z", "reject"),
        ("2026-09-24T04:00:00.0Z", "reject"),
        ("2026-09-24T04:00:00+00:00", "reject"),
        ("", "reject"),
    ]
):
    add(f"P-I{i}", expect, setf("b0.window_instance", value), note=f"window_instance={value!r}")


def failure(ref):
    def change(env):
        env.update(status="rate_limited", buckets=[], error_ref=ref)

    return change


types = [
    ("status list", setf("status", []), "reject"),
    ("status number", setf("status", 1), "reject"),
    ("horizon list", setf("b0.horizon", []), "reject"),
    ("error_ref list", failure([]), "reject"),
    ("error_ref ok", failure("rate_limited:http_429"), "accept"),
    ("revision float", setf("source_binding_revision", 1.0), "reject"),
    ("revision int64 max", setf("source_binding_revision", 2**63 - 1), "accept"),
    ("revision int64 max+1", setf("source_binding_revision", 2**63), "reject"),
    ("revision zero", setf("source_binding_revision", 0), "reject"),
    ("unshared null", setf("unshared_limit_count", None), "reject"),
    ("unshared bool", setf("unshared_limit_count", True), "reject"),
    ("unshared int64 max+1", setf("unshared_limit_count", 2**63), "reject"),
    ("used_pct bool", setf("b0.used_pct", True), "reject"),
    ("used_pct string", setf("b0.used_pct", "10"), "reject"),
    ("used_pct int", setf("b0.used_pct", 10), "accept"),
    ("used_pct 100.0", setf("b0.used_pct", 100.0), "accept"),
    ("label number", setf("b0.label", 5), "reject"),
    ("label null", setf("b0.label", None), "accept"),
    ("schema null", setf("schema", None), "reject"),
    ("contract_rev null", setf("contract_rev", None), "reject"),
    ("source_slot_ref null", setf("source_slot_ref", None), "reject"),
    ("observation_id number", setf("observation_id", 7), "reject"),
    ("buckets null", setf("buckets", None), "reject"),
    ("buckets object", setf("buckets", {}), "reject"),
    ("scope null", setf("b0.scope", None), "reject"),
    ("scope.kind null", setf("b0.scope.kind", None), "reject"),
    ("lease_epoch 1", setf("lease_epoch", 1), "reject"),
    ("C1 in label", setf("b0.label", "5h\u0085"), "reject"),
    ("128-byte label", setf("b0.label", "가" * 42 + "ab"), "accept"),
    ("129-byte label", setf("b0.label", "가" * 43), "reject"),
    (
        "model ref 128 bytes",
        lambda e: e["buckets"][0].update(scope={"kind": "model", "ref": "a" * 128}),
        "accept",
    ),
    ("model ref list", lambda e: e["buckets"][0].update(scope={"kind": "model", "ref": []}), "reject"),
    # JSON \u escapes of UTF-16 surrogates: a pair is one character; a lone one is not UTF-8 (§2, no repair)
    ("label surrogate pair", setf("b0.label", "\U0001f600"), "accept"),
    ("label lone high surrogate", setf("b0.label", "\ud800"), "reject"),
    ("label lone low surrogate", setf("b0.label", "x\udc00"), "reject"),
    ("label two high surrogates", setf("b0.label", "\ud800\ud800"), "reject"),
    ("label reversed pair", setf("b0.label", "\ude00\ud83d"), "reject"),
    ("label backslash-u text (not an escape)", setf("b0.label", "\\ud800"), "accept"),
    (
        "model ref lone surrogate",
        lambda e: e["buckets"][0].update(scope={"kind": "model", "ref": "\ud800"}),
        "reject",
    ),
]
for i, (name, change, expect) in enumerate(types):
    add(f"P-Y{i:02d}", expect, change, note=name)

for key in list(base):
    add(f"P-M-{key}", "reject", lambda e, k=key: e.pop(k), note=f"envelope key {key} absent")
    add(
        f"P-C-{key}",
        "reject",
        lambda e, k=key: e.update({k.upper(): e.pop(k)}),
        note=f"envelope key {key} in upper case",
    )
for key in list(base["buckets"][0]):
    add(f"P-MB-{key}", "reject", lambda e, k=key: e["buckets"][0].pop(k), note=f"bucket key {key} absent")
add("P-MS-ref", "reject", lambda e: e["buckets"][0]["scope"].pop("ref"), note="scope.ref absent")
add("P-XS", "reject", lambda e: e["buckets"][0]["scope"].update(x=1), note="extra scope key")
add("P-XB", "reject", lambda e: e["buckets"][0].update(x=1), note="extra bucket key")


def many(env):
    template = env["buckets"][0]
    env["buckets"] = [dict(copy.deepcopy(template), limit_id=f"claude.extra_{i:03d}") for i in range(100)]


add("P-N100", "accept", many, note="100 buckets: the envelope has no bucket-count limit")


def received(env):
    env.update(received_at="2026-09-24T01:08:21Z")


add("P-S1", "accept", received, form="stored", note="stored form with received_at")
add("P-S2", "reject", received, form="post", note="POST form with received_at")
add(
    "P-S3",
    "reject",
    lambda e: e.update(received_at="2026-09-24T01:08:21"),
    form="stored",
    note="stored received_at without offset",
)

out = pathlib.Path("tests/fixtures/quota_v2_parity_r3.json")
# ASCII output: lone surrogates can only be written as \u escapes.
out.write_text(json.dumps(rows, ensure_ascii=True, indent=1) + "\n")
print(len(rows), "rows;", sum(r["expect"] == "accept" for r in rows), "accept")
