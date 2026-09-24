# Cross-host quota snapshots via handoffkeep (#653/#654)

Each Claude account is measured in **one place**. The measuring host publishes a
sanitized snapshot to the existing handoffkeep document store, and every other
host reads it instead of polling the usage API itself. The trigger was ClaudeBar
on m1b multiplying usage-API polls until the account's token/IP drew HTTP 429s
while the same account answered 200 elsewhere — the limit is per token, session
or IP, not per account, so the fix is to stop multiplying calls.

```
measuring host (this Mac / mbp-server)
  └─ scopefuel collect/refresh success → PUT /v1/documents/quota/claude/<account_fp>/latest
reader hosts (m1b, Pi, …)
  └─ local measurement fails / expires / is in backoff → GET the same key
       └─ accept only if measured_at ≤ 15 min AND account fingerprint matches
```

## Payload

`quota/<pool>/<account_fp>/latest`, kind `note`, session `scopefuel-quota-share`:

- `buckets[]`: `used_pct`, `resets_at`, `window`, `horizon`, `scope`, `note`
- `measured_at` / `measured_at_epoch`
- `measured_by`: `{host, session_fp}` — per-value provenance (AC5)
- `account_fp` — the reader's accept key

**No token-like field is ever written.** The body carries only measurements and
irreversible fingerprints; handoffkeep's `guard.Reject` additionally refuses
credential-shaped bodies, so a regression that serialised a token would fail at
the server too.

## Fingerprints

- `account_fp` = `sha256("claude-account|<subscriptionType>|<accountUuid>")[:16]`.
  The account UUID (`claudeAiOauth.oauthAccount.accountUuid`) is stable across
  hosts and OAuth sessions; a token hash is *not*, because each host holds a
  different access token (AC3). Credentials without an `accountUuid` produce no
  fingerprint — fail closed, never a silent token-hash fallback.
- `session_fp` = `sha256("claude-session|<accessToken>")[:16]`. Provenance only:
  it records *which* token-session measured a value, so one session's 429 is
  visibly that session's event — it never establishes account identity.

## Reading rules (AC2)

A remote snapshot is consulted **only** when the local measurement is
unavailable (error), expired (stale), or in host-local backoff — never instead
of a fresh local value. It is accepted only when `measured_at` is ≤ 15 minutes
old (with 60 s of NTP-skew slack) and the fingerprint probe of the *current*
local credentials equals the document's `account_fp`. Remote results are
labelled `remote measured (<host>)` — `source=remote`, distinct from the manual
self-report (`source=operator`, `자기신고 · 미검증`) — and classified by the same
pace-based rules as local values; they are not pinned to `preserve` and are
never written into the local cache, backoff state, or v2 store.

## Publishing rules

Publishing is a side effect of a successful `collect`/`refresh` run — no new
scheduler or timer (AC6: the measuring host's usage-API call count is
unchanged). Publish failures are fail-open: the measuring host behaves exactly
as before. Results without an account fingerprint are not published.

## Deployment

- **Measuring host:** this Mac (and/or `mbp-server` once it runs a build that
  includes this change). Whichever host has healthy, non-expired credentials
  should keep running `scopefuel`/`refresh` as usual — every success
  republishes `latest`.
- **hk key and ACL:** `quota/claude/<account_fp>/latest` under the existing
  handoffkeep document API (`PUT`/`GET /v1/documents/{key}`, bearer auth). The
  body is non-secret by construction; readers and writers share the same
  service token already used by `bench`.
- **m1b and Pi start reading** simply by running this build: `collect` already
  falls back to the remote snapshot when local measurement is unavailable,
  expired, or in backoff. Nothing to schedule — reading is a side effect of
  their normal `scopefuel` runs. Expired `~/.claude/.credentials.json` files on
  those hosts no longer produce misleading 401/429 noise: `expiresAt` is
  checked before any call and reported as `token expired` (#653). Automatic
  token refresh remains prohibited (#616) — re-login is manual.
- **Witness:** on m1b/Pi, `scopefuel gate -m opus` prints
  `class=spend source=remote source_label="remote measured (<host>)"` while the
  local token is expired/429-ing; a snapshot older than 15 minutes is refused
  and the gate falls back to local failure/manual rules.
- **Rollback:** `SCOPEFUEL_QUOTA_SHARE=off` disables publishing and reading on
  that host with no other change. Removing the env override restores it.
  Deleting the `quota/...` documents stops readers from finding snapshots.
