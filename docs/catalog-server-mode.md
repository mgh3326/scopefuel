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

## Freshness: three states, never two

| state | condition | behaviour |
|---|---|---|
| `server` | fetched this run | canonical |
| `cache` | `age < catalog_ttl_s` (1h), or older but the server is unreachable and `age < catalog_stale_max_s` (24h) | canonical copy; a warning on the unreachable path |
| `unsupported` | the endpoint answered 404 — the deployment has no catalog route | fall through to `/v1/bench/grades`, then the code table. **Not stale** |
| `snapshot` (stale) | no cache, or `age ≥ catalog_stale_max_s` | bundled snapshot, labelled `catalog=stale` everywhere |

A local-backend host also reports `snapshot`, but is **not** stale: it has no
canon to be behind, by configuration.

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

**Known limit:** a stale host reverts to the snapshot's *placements*. If an
operator had demoted a profile server-side, an outage restores the pre-demotion
placement — and `catalog_stale_max_s` does **not** bound how long that lasts. It
only bounds how long the *cache* is still trusted; past it the host switches to
the bundled snapshot and stays there for as long as the server is unreachable.
What the setting buys is that the switch happens, visibly, rather than a stale
cache being served indefinitely. The snapshot cannot preserve a demotion it never
saw, so the mitigation is the label, not the timeout: a host reporting
`catalog=stale` is dispatching from reviewed-at-merge-time placements, and
restoring the canon is the only thing that restores the demotion.

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
catalog=server
rows=49 profiles=34
```

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
catalog rows written: 49

# 3. confirm
$ scopefuel bench catalog status
$ scopefuel bench catalog list | head
```

`decided_by` is required caller-supplied provenance on this route — the server
rejects a blank one rather than filling it in.

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
