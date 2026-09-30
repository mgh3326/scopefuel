# Canonical catalog and host server mode (#593)

Model ids, grade placement, pool and gate live in handoffkeep's
`bench_catalog` (`/v1/bench/catalog`, schema v12, handoffkeep #592).
`scopefuel` reads it, and `agent-skills`' `bin/wrk` reads scopefuel.
`recommend.py GRADE_TABLE` is demoted to a **bundled offline snapshot**.

```
handoffkeep bench_catalog        canonical
  └─ scopefuel  read_catalog() → runtime_grade_table() / policy launch
       └─ bin/wrk  resolve_profile()   model id + default effort only
```

What stays in the launcher: **profile spellings and argv skeletons** — permission
flags, `--respect-workspace-trust`, `--dangerously-skip-permissions`, the CLI
alias `--model opus`, and the per-CLI argv assembly. The server owns four values
per `(profile, effort)`: `model_id`, the default effort rung, `pool`, `gate`.

## Freshness: the states, never a bare "fresh?"

| state | condition | behaviour |
|---|---|---|
| `server` | fetched this run **and passed the validity floor** | canonical |
| `cache` | `age < catalog_ttl_s` (1h), or older but the server is unreachable (or its catalog refused) and `age < catalog_stale_max_s` (24h) | canonical copy; a warning on the degraded path |
| `cache-stale` | server unreachable (or its catalog refused) and a cache exists with `age ≥ catalog_stale_max_s` | last-good cache keeps serving, labelled `catalog=cache-stale (age Nh; server unreachable)`. **Stale** — widening stays closed |
| `unsupported` | the endpoint answered 404 — the deployment has no catalog route | fall through to `/v1/bench/grades`, then the code table. **Not stale** |
| `snapshot` (stale) | no cache at all | bundled snapshot, labelled `catalog=stale (snapshot)` |

A local-backend host also reports `snapshot`, but is **not** stale: it has no
canon to be behind, by configuration.

### The validity floor (#954, the #667 class)

A fetched catalog is checked once, after the schema decode and before it may
replace the last-good cache. It is **refused as a whole** when it is empty, or
when any profile the bundled snapshot places is not mentioned in it at all —
*mentioned* means present as a row, live or retired, so a legitimate
server-side retirement still passes. A refused catalog is never written to the
cache; the host degrades exactly as if the server were unreachable (cache if
one exists, else the snapshot) with the reason on the label, e.g.
`catalog=stale (server catalog rejected: 1 row, missing 36 snapshot profiles)`.
Per-profile gaps below this floor stay the launcher's problem — the
`resolve_launch` snapshot fill-in remains the second line of defence.

Tunables live in `~/.config/scopefuel/config.toml`:

```toml
[bench]
# backend = "auto"          # default; see below
catalog_ttl_s = 3600        # serve the cache without a request below this age
catalog_stale_max_s = 86400 # beyond this, an unreachable server means "stale"
```

## Fail policy: fail-degraded-closed

Not fail-open and not fail-closed — the axis is *what the action does*, not
*whether the server answered*.

* **Reads and reproduction fail open.** An unreachable handoffkeep degrades to
  cache, then to the bundled snapshot. Full fail-closed would make one server
  outage a fleet stop, and the snapshot is a reviewed copy of the last canon, not
  an invented value.
* **Widening fails closed.** A server being down is never the reason something
  became easier to start (hk:doc 2558, "a down server is not free dispatch"):
  * `gate = consult_only` is never relaxed — `--operator-request` always required;
  * while `catalog=stale`, ordinary `default`-gate launches carry on unchanged
    (rc 0, labelled) — only a non-`default` gate additionally requires
    `--operator-request`, even where the snapshot says the gate is `default`,
    because the canon may have raised it since the last successful read;
  * a profile absent from the snapshot is refused (`rc 3`), never defaulted.
* **Disclosure is the precondition.** Every non-canonical path is labelled —
  `scopefuel --recommend` prints a `catalog=…` line, `policy launch --json`
  carries `catalog.source`/`catalog.stale`, and `wrk` stamps `catalog=stale` on
  the spawn brief header. Fail-open without a label is just fail-open.

**Known limit:** past `catalog_stale_max_s` the host keeps serving the
last-good cache for as long as the server is unreachable — there is no timeout
that ever swaps those rows. What the setting bounds now is only the *label*:
`catalog=cache` becomes `catalog=cache-stale` at the ceiling, and the
stale rules (non-`default` gates need `--operator-request`) apply throughout.
That trade is deliberate: the bundled snapshot is older than the cache and
cannot preserve a server-side demotion it never saw, so reverting to it past
the ceiling was the one failure mode that *resurrected* placements the canon
had already retracted. The residual limit: a cache written before the validity
floor existed can hold a partial canon; it keeps serving under the same
staleness rules, and `bench catalog status` still lists its uncovered
profiles. The bundled snapshot is now only the no-cache floor — a host with no
cache at all is the one place `catalog=stale (snapshot)` still means
"reviewed-at-merge-time placements". And a rejected server catalog is not
negative-cached — `fetched_at` is never refreshed on a rejection — so every
process refetches the canon on every call until the server is fixed; each call
prints one warning line and still completes.

## Switching a host to server mode

`[bench] backend` accepts `auto` (default), `handoffkeep`, `local`.

`auto` resolves to `handoffkeep` when **both** an endpoint URL and a token are
found, and the URL is safe to carry a bearer token; otherwise `local`.
Credentials are read from the environment first (`HANDOFFKEEP_URL`,
`HANDOFFKEEP_TOKEN`), then from the handoffkeep CLI's own
`~/.config/handoffkeep/config.env` (override with `HANDOFFKEEP_CONFIG`).

This is why the rollout needs **no per-host scopefuel edit**: every host that
already runs `handoffkeep` is already provisioned. The failure mode being
avoided is the quiet one — one machine left in local mode keeps dispatching from
its bundled table and nothing says so.

Check any host with:

```console
$ scopefuel bench catalog status
backend=handoffkeep reason=auto-credentials
credentials url=found token=found (env or /home/…/.config/handoffkeep/config.env)
catalog_ttl_s=3600 catalog_stale_max_s=86400
catalog=server (age 0.0h)
rows=59 profiles=37
```

The same report doubles as the fleet machine check:

```console
$ scopefuel bench catalog status --check   # rc 0 when the served view is the canon
```

`--check` exits 0 when the served view's source is `server`, or `cache`
inside `catalog_ttl_s` with an empty `detail` — a healthy host answers from
the cache between refetches without contacting the server at all, so a fresh
clean cache *is* the canon for the fleet check. It exits 2 — printing the
label that names the state — for a local backend, `cache-stale`, `snapshot`,
`unsupported`, and any cache serving only because the server refused its
catalog or was unreachable (a `detail`-carrying or TTL-expired cache). A host
silently left in local mode is a red line, not a quiet default.

Opt a host out with an explicit `[bench] backend = "local"`.

### Prerequisite: the endpoint must not carry the token in the clear

The bearer token may only be sent over `https`, or over `http` to
localhost/127.0.0.1/::1 (CWE-319). As of 2026-09-23 the deployment is
`http://100.122.100.56:8800` — a Tailscale address — so `auto` reports:

```
backend=local reason=auto-local-insecure-url
blocked: handoffkeep credentials exist but the URL is plaintext http to a
non-local host; serve it over https, or set [bench] allow_plaintext_catalog = true
for a private WireGuard/Tailscale tunnel
```

Auto-detection never enables plaintext on its own: "this link is private" is a
claim only the operator can make. Two ways forward, in order of preference:

1. **Serve handoffkeep over https** (`tailscale serve --https=443
   http://localhost:8800` gives a real `*.ts.net` certificate) and point
   `HANDOFFKEEP_URL` at it. Nothing else changes; `auto` then fires on every host.
2. **Opt in per host**, if TLS is not available yet:

   ```toml
   [bench]
   allow_plaintext_catalog = true   # only for a WireGuard-tunnelled tailnet address
   ```

   Since #697 the opt-in is per use (`allow_plaintext_catalog`,
   `allow_plaintext_quota_share`, `allow_plaintext_reps`); the deprecated
   `allow_plaintext_url` still works as an alias for all three, with a warning.

## Seeding the catalog (one time, operator token)

`PUT /v1/bench/catalog` requires the **operator** credential
(`HANDOFFKEEP_TOKEN_operator`); other tokens get `403 operator_required`.

```console
# 1. generate the seed from the bundled snapshot — this is exactly today's table
$ scopefuel bench push-catalog --emit-seed \
    --decided-by operator-desk \
    --deviation-ref hk:doc/task/2026-09-23/scopefuel-catalog-server-canonical-wrk-policy-launch \
    > /tmp/catalog-seed.json

# 2. review it, then write it
$ HANDOFFKEEP_TOKEN="$HANDOFFKEEP_TOKEN_operator" scopefuel bench push-catalog /tmp/catalog-seed.json
catalog rows written: 59

# 3. confirm
$ scopefuel bench catalog status
$ scopefuel bench catalog list | head
```

`decided_by` is required caller-supplied provenance on this route — the server
rejects a blank one rather than filling it in.

When the seeding host's tunnel endpoint is plaintext http and the persistent
`allow_plaintext_catalog` opt-in is not written yet, `push-catalog` takes a
per-call `--allow-plaintext-http` (the same opt-in `reps migrate` carries): it
applies to that invocation only and is never recorded to the config file.

Ordering matters on every later release too: the validity floor requires the
server canon to mention every profile the bundled snapshot places. A scopefuel
release that *adds* a snapshot profile therefore makes upgraded hosts reject
the server canon until its row exists — push-catalog the new rows **before**
installing such a release (or upgraded hosts degrade to `catalog=stale` with
`server catalog rejected: N rows, missing 1 snapshot profile` until the push
lands).

## Post-merge: record opus at S+ (absorbs #591 AC4)

Run **after** this PR is merged, installed, and the host is in server mode.
`bench grades set` needs the handoffkeep backend; before #593 it failed with
"bench grades require backend = handoffkeep" because every host was in local
mode with no `[bench]` section at all.

```console
# 0. the host must be in server mode
$ scopefuel bench catalog status
backend=handoffkeep reason=auto-credentials      # or reason=configured

# 1. record the placement on the legacy grades route. The server mirrors this
#    into the catalog's *profile-default* row (effort ""), which is what a
#    pre-catalog client reads.
$ scopefuel bench grades set \
    --profile opus \
    --grade S+ \
    --deviation-ref hk:doc/note/2026-09-23/model-refresh-opus55-gpt6-grok47

# 2. 🔴 the grades route moves ONLY that default row. runtime_grade_table() places
#    opus from its per-rung catalog rows (high/xhigh/medium/max), so once the
#    catalog is seeded the rungs must be written on the catalog route too, or
#    `--recommend` will not move. Write the rungs you intend to place:
$ cat > /tmp/opus-s-plus.json <<'JSON'
{"catalog": [
  {"profile": "opus", "effort": "high",  "model_id": "claude-opus-5-5", "pool": "claude",
   "grade": "S+", "gate": "default", "decided_by": "operator-desk",
   "deviation_ref": "hk:doc/note/2026-09-23/model-refresh-opus55-gpt6-grok47"},
  {"profile": "opus", "effort": "xhigh", "model_id": "claude-opus-5-5", "pool": "claude",
   "grade": "S+", "gate": "default", "decided_by": "operator-desk",
   "deviation_ref": "hk:doc/note/2026-09-23/model-refresh-opus55-gpt6-grok47"}
]}
JSON
$ HANDOFFKEEP_TOKEN="$HANDOFFKEEP_TOKEN_operator" scopefuel bench push-catalog /tmp/opus-s-plus.json

# 3. confirm — server and code columns must agree, with no ⚠ drift line
$ scopefuel bench grades list
profile server table
opus server=S+ table=S+
$ scopefuel bench catalog list | grep '^S+ opus'

# 4. confirm the launcher consumes it
$ scopefuel policy launch opus --json
{"catalog":{"source":"server","stale":false,...},"effort":"high","model_id":"claude-opus-5-5",...}
```

Steps 1 and 2 write to handoffkeep. Nothing in the #593 PRs performs a server write.

## Quota measurement note

Three machines each poll the same Claude usage API, which started returning
HTTP 429 on 2026-09-23. Centralising quota measurement the way placement is now
centralised would remove the duplication; that is out of #593's scope and tracked
with #578.
