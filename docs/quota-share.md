# Cross-host quota snapshots via handoffkeep (#653/#654/#659)

Each Claude account is measured in **one place**. The measuring host publishes a
sanitized snapshot to the existing handoffkeep document store. Other hosts still
run their own measurement first; they consult the remote snapshot only when the
local measurement is unavailable, expired, or in backoff. The trigger was
ClaudeBar on m1b multiplying usage-API polls until the account's token/IP drew
HTTP 429s while the same account answered 200 elsewhere — the limit is per
token, session or IP, not per account, so the fix is to stop multiplying calls.

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
- `measured_by`: `{host, session_fp, account_fp, account_label}` — per-value
  provenance (AC5). `account_fp`/`account_label` (added in #659) record *which
  account* measured the value; a document whose `measured_by.account_fp`
  disagrees with the body `account_fp` is refused by readers.
- `account_fp` — the reader's accept key

**No token-like field is ever written.** The body carries only measurements and
irreversible fingerprints, and the payload key set is fixed in
`quota_share._payload` — that client-side key set is the only defence. Do not
rely on handoffkeep's `guard.Reject` as a second line: it matches
`sk-ant-(api03-)?…` API keys but **not** Claude OAuth tokens
(`sk-ant-oat01-…`/`sk-ant-ort01-…`), which would pass the server guard.
`account_label` is a display label only — `safe_label()` drops any value
containing `@` and strips quotes/control characters before it can be rendered.

## Fingerprints

- `account_fp` = `sha256("claude-account|<subscriptionType>|<accountUuid>")[:16]`,
  `account_fp_kind = "account"`. The account UUID is read from
  `oauthAccount.accountUuid` at the **top level of `.claude.json` in the same
  config context as the token** (`$CLAUDE_CONFIG_DIR/.claude.json` when that env
  is set, `~/.claude.json` otherwise) — it is *not* inside `claudeAiOauth` in
  the credentials file. It is stable across hosts and OAuth sessions; a token
  hash is *not*, because each host holds a different access token (AC3).
- **Same-context binding (#659 AC1):** the token source and the UUID source are
  always the same config context. The credentials file is
  `$CLAUDE_CONFIG_DIR/.credentials.json`; the Keychain fallback reads the
  per-context item `Claude Code-credentials-<sha256(context)[:8]>` (matching
  Claude Code ≥2.1.x: `CLAUDE_SECURESTORAGE_CONFIG_DIR` wins when set — empty
  means unsuffixed — otherwise `CLAUDE_CONFIG_DIR` verbatim, NFC-normalized;
  unsuffixed `Claude Code-credentials` only in the default context). A token
  from one context is therefore never combined with a UUID from another — when
  the context's `.claude.json` has no UUID the measurement degrades to the
  token fallback below instead of borrowing another context's identity. The
  same rule applies when `CLAUDE_SECURESTORAGE_CONFIG_DIR` differs from
  `CLAUDE_CONFIG_DIR`: the credentials file (`$SSCD/.credentials.json` per
  Claude's `aw()`) and the Keychain item then both belong to the
  secure-storage context while the UUID still comes from `$CCD/.claude.json`,
  so the UUID binding is refused and the fingerprint degrades to the token
  fallback (fail-closed — nothing is published or read remotely). Setting
  SSCD to a different directory therefore disables remote sharing on that
  host.
- Hosts where `accountUuid` is unreadable in *that* context fall back to the
  legacy `sha256("<subscriptionType>|<accessToken>")[:16]` fingerprint
  (`account_fp_kind = "token"`). That keeps host-local stale acceptance (#576)
  working — but a token-hash fingerprint is **never published and never used
  for remote reads** (#659/N-1): the key is bound to this host's current token,
  so every token rotation would orphan a `quota/claude/<fp>/latest` document
  nobody can read, and the fingerprint cannot prove which account it belongs
  to. The skip is visible on the result (`hk 공유 건너뜀 — …account uuid 없음`
  note, `account_fp_kind` in `--json`).
- `session_fp` = `sha256("claude-session|<accessToken>")[:16]`. Provenance only:
  it records *which* token-session measured a value, so one session's 429 is
  visibly that session's event — it never establishes account identity.
- One-time transition note (#654→#659): the first publish after upgrade changes
  the key for hosts that were previously publishing token-hash fingerprints —
  old `quota/claude/<token-fp>/latest` documents simply age out unread (readers
  only look up their own current fingerprint's key).

## Account display (#659 AC2)

Every measurement carries an account tag — the first 8 characters of the
fingerprint plus a safe label, e.g. `c0605596 (My Org)`. The label is read from
the same context's `.claude.json` (`organizationName` → `displayName` →
`fullName`) and passed through `safe_label()` — emails (`@`), quotes, control
characters are stripped and the result is capped at 32 characters. Emails,
tokens and UUIDs are never displayed. The tag appears in the pool table
(`account <tag>`), the one-line brief (`claude@<tag>`), the gate's first line
(`account="<tag>"`), and remote results carry it in
`remote measured (<host>) · account <tag>`.

## Reading rules (AC2)

A remote snapshot is consulted **only** when the local measurement is
unavailable (error), expired (stale), or in host-local backoff — never instead
of a fresh local value. It is accepted only when `measured_at` is ≤ 15 minutes
old (with 60 s of NTP-skew slack), the fingerprint probe of the *current* local
credentials equals the document's `account_fp`, and the document's
`measured_by.account_fp` (when present) agrees. Readers whose fingerprint is
token-hash fallback (`account_fp_kind = "token"`) never consult remote
snapshots — no uuid-bound document can match their key anyway. Remote results
are labelled `remote measured (<host>) · account <tag>` — `source=remote`,
distinct from the manual self-report (`source=operator`, `자기신고 · 미검증`) —
and classified by the same pace-based rules as local values; they are not
pinned to `preserve` and are never written into the local cache, backoff
state, or v2 store. `host` on the wire is whitelisted to
`[A-Za-z0-9._-]` on both write and read — anything else becomes `unknown`
(#659/N-4). Names longer than 64 chars (e.g. GitHub macOS runners'
`<uuid>-<hex>.local`) are not dropped: they are truncated to
`<first 55>-<sha256(host)[:8]>` so provenance survives and distinct long
names keep distinct labels.

## Publishing rules

Publishing is a side effect of a successful `collect`/`refresh` run — no new
scheduler or timer (AC6: the measuring host's usage-API call count is
unchanged). Publish failures are fail-open: the measuring host behaves exactly
as before. Results without an **account-bound** fingerprint are not published
(token-hash fallback is refused — see N-1 above).

## Deployment

Prerequisites per host (#659 AC7):

- **`[bench] allow_plaintext_url = true`** in the scopefuel config — the
  deployed handoffkeep endpoint is a plaintext Tailscale URL
  (`http://100.122.100.56`); without the opt-in the bearer token is never sent
  over plaintext and sharing silently stays off. Operator decision: enable on
  Mac, Pi, desktop, NCP.
- **`~/.claude.json` readable** (or `$CLAUDE_CONFIG_DIR/.claude.json` for custom
  contexts) — needed for `oauthAccount.accountUuid` so the fingerprint is
  account-bound; without it the host publishes/reads nothing remotely.
- **handoffkeep `config.env` present** (credential discovery for the `bench`
  backend). **m1b currently lacks `config.env`** — install it before expecting
  reads there.
- Re-login (not refresh — #616) is the remedy for expired tokens.
- Known limitation: staging/custom-OAuth Claude contexts store the account file
  as `.claude-staging-oauth.json` / `.claude-custom-oauth.json`; only the
  production `.claude.json` name is read. Claude's legacy
  `~/.claude/.config.json` precedence (`ct()`) is likewise not honoured —
  both cases fail closed (token-fallback fingerprint, no sharing).
- Residual: within one context, a stale `.credentials.json` residue and
  `.claude.json` can disagree about the account (#654 K2) — the file is read
  first, so a leftover token pairs with that context's UUID. Claude Code
  deletes the plaintext file after a successful Keychain write, so residue is
  rare; the guard above covers the cross-context case only.

- **Measuring host:** this Mac (and/or `mbp-server` once it runs a build that
  includes this change). Whichever host has healthy, non-expired credentials
  should keep running `scopefuel`/`refresh` as usual — every success
  republishes `latest`.
- **hk key and ACL:** `quota/claude/<account_fp>/latest` under the existing
  handoffkeep document API (`PUT`/`GET /v1/documents/{key}`, bearer auth).
  There is **no per-key ACL** — any holder of the shared `bench` service token
  can PUT or GET `quota/…` documents. That is accepted for now: the body is
  non-secret by construction and the fingerprint is an irreversible hash, but
  it means a leaked bench token could also *write* forged snapshots. If that
  matters, revoke/rotate the token rather than relying on document ACLs.
- **m1b and Pi start reading** simply by running this build: `collect` already
  falls back to the remote snapshot when local measurement is unavailable,
  expired, or in backoff. Nothing to schedule — reading is a side effect of
  their normal `scopefuel` runs. Each host needs its own
  `~/.claude.json` (`oauthAccount.accountUuid`) for the fingerprint to match
  across different OAuth tokens; without it the host falls back to a token-hash
  fingerprint and remote reads simply won't match. Expired
  `~/.claude/.credentials.json` files on those hosts no longer produce
  misleading 401/429 noise: `expiresAt` is checked before any call and reported
  as `token expired` (#653). Automatic token refresh remains prohibited (#616)
  — re-login is manual.
- **Witness:** on m1b/Pi, `scopefuel gate -m opus` prints
  `class=spend source=remote source_label="remote measured (<host>) · account <fp8> (<label>)"`
  while the local token is expired/429-ing; a snapshot older than 15 minutes is
  refused and the gate falls back to local failure/manual rules.
- **Two-account witness (staged dry-run until a second account exists):** run a
  second context with `CLAUDE_CONFIG_DIR=<other>` logged into the other
  account. Expect: each context publishes only to its own
  `quota/claude/<fp>/latest` key; a reader in context A shows
  `account="<fp_a8> …"` and never consumes B's document (its GET key differs
  and `account_fp`/`measured_by.account_fp` must agree). Test equivalent:
  `tests/test_quota_share.py::test_multi_account_publish_and_read_isolation`.
- **Rollback:** `SCOPEFUEL_QUOTA_SHARE=off` disables publishing and reading on
  that host with no other change. Removing the env override restores it.
  Deleting the `quota/...` documents stops readers from finding snapshots.
