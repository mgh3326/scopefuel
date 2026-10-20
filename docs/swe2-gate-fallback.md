# SWE-2 Measurement Fallback (hk 1731)

Only the six names in `SWE2_PROFILE_RUNG` use an observed own-rung model
bucket. Current missing, duplicated, invalid, or sibling-only buckets retain
account judgment: a rung may have become paid. Policy exclusion is unchanged.

## Fixture Reproduction

Frozen base: `ceb7985`. Account Daily 100, Weekly 59, spend cutoff 99.
Entrypoint: `gate_check(..., "builder-devin")`; exit codes use the CLI mapping.
All three Free model buckets are present except in the missing-list case.

| Fixture | Base rc | Base reason / basis | Fixed rc / basis |
| --- | --- | --- | --- |
| Account PTY warning | 4 | provider error/degraded; no basis | 0 / own model |
| Stale transport, matched account, age 120s | 3 | account 100, exhausted; no model basis | 4 / measurement unavailable, or 0 after successful refresh |
| Status ok, age 120s (TTL 60s) | 3 | account 100, exhausted; no model basis | 4 / measurement unavailable, or 0 after successful refresh |
| Models-list error (status error) | 4 | provider error/degraded; no basis | 4 / named measurement unavailable |
| Current missing models list | 3 | account 100, exhausted; no model basis | 3 / account (unchanged) |
| Stale auth/unmatched account proof | 4 | provider error/degraded; no basis | 4 / named measurement unavailable |

Stale and aged snapshots reproduce the unwanted account fallback. A current
missing-list refusal is intentional. These fixtures do not prove which branch
ran during the historical refusal.

## Warning Classes

Case-insensitive warning markers identify model-side failures:

- `models list`, `models_list`: fetching the model inventory failed or timed out.
- `model row`, `model-scope`: model-row parsing or scope attribution degraded.
- `swe-2`: a warning identifies this model family or its rung observation.
- `free tag`: the observation of the billing tag itself is uncertain.
- Korean `모델`, `free 태그`: equivalent model/list/tag parser warnings.

Only these model-related warnings disqualify otherwise-current observed model
buckets. Account quota/exhaustion, banner-versus-usage mismatch, and account
PTY warnings do not. A note alone is not a warning. Provider errors, stale
flags, past-TTL ages, and other non-ok statuses remain unavailable. Manual
account observations cannot assert a current Free tag.

## Refresh And Consumers

CLI gate and recommendation perform at most one synchronous refresh when a
needed SWE-2 observation is stale or past TTL. Pure evaluation never probes.
The existing refresh worker retains its lock, backoff, provider timeout, and
tracked-child cleanup. A new isolated worker session also has a parent wait
bound and tracked-session cleanup. A zero exit from a skipped refresh cannot
reuse an old cache entry: the entry must have been successfully fetched during
this attempt and be inside TTL.

The `--_swe2` worker reports each skip cause on a distinct exit code — another
refresh worker holds the pool lock, the provider single-probe lock reports a
probe already in progress, or host backoff — so the parent tells a skip apart
from a run without parsing free text. Plain `refresh` keeps rc 0 for skips.
On a lock-held or probe-in-progress skip the parent waits for the in-flight
probe: a short fixed-interval poll of the cache file only, never a network
call, accepting the first devin entry fetched strictly after the pre-wait
entry, with no newer `last_error_at`, inside TTL, passing the same
current-probe checks. Each poll also re-checks host backoff: a backoff
already on disk or recorded by the in-flight probe ends the wait at once as
`state=backoff`. Otherwise the wait is bounded by the worker's own hard cap
plus a margin (`_timeout_seconds() + SWE2_IN_FLIGHT_WAIT_MARGIN_S`, named
`_in_flight_wait_s`) — the devin fetch runs two sequential probe phases under
one lock, so the provider timeout alone under-covers it. On expiry the
refusal stays rc 4 with `state=in-flight-timeout`. A backoff skip never
waits: rc 4 at once with `state=backoff` and the remaining seconds in the
reason.

The internal `--_swe2` worker mode permits an unrelated account warning only
when usable model buckets were observed. Ordinary refresh behavior is unchanged.
The warning remains in the cache; paid consumers still see it as degraded.
Recommendation uses the automatic model observation separately from manual
account display results, so paid/manual rows are not changed by this fix.

Unavailability is `swe2_measurement_unavailable`, rc 4, with
`basis=measurement-unavailable` in the reason, never paid-axis rc 3.
The CLI must not launder it through manual/trusted-snapshot fallback.
In particular, the hk 1381 escape hatch (a banner/PTY error plus a complete
manual weekly account observation) no longer opens any of the six SWE-2
names. It returns named rc 4 because manual weekly usage cannot prove a Free
model tag. Paid Devin, including ds41, retains that manual path unchanged.
Read-only inspection of `agent-skills/bin/wrk`, `quota_gate`, found that
`3|4) exit "$rc"` propagates both as hard refusals. CLI-provided alternatives
remain advisory; rc 4 is not admission.

The hk 1690/1715 hold still replaces only an otherwise-admissible free-rung
decision with rc 3 `free_rate_limited`; it does not disguise missing measurement
or override other refusals.

## Verification

Only changed/added test files run locally, individually capped at
600 seconds and 2 GiB, with temporary HOME and every XDG directory. No real
provider or cache is used. Fixture GateResult JSON for all non-SWE2 catalog
names plus Argon trial names is byte-identical to the frozen base across
current/warning/stale observations and Daily 10/100, including alternatives.
Non-SWE2 gates deliberately retain the old recommendation-based alternative
fold to avoid changing their refusal or escalation eligibility.

The full suite belongs to CI. Final-head independent testing must also attack
all inherited hunks, including any not required by an acceptance criterion.

The initial CI exposed legacy tests expecting stale/manual account admission
for SWE-2. Generic account-cache and manual contracts now explicitly use paid
Devin, while new SWE-2 tests assert unavailability instead. The actual provider
warning fixture now asserts Free admission. Only the SWE-2 failure row of the
old gate golden changed; paid rows are untouched. The global test guard stubs
the new refresh helper unless a test explicitly supplies fixture behavior.
Absent-provider recommendation folds retain their original generic unavailable
diagnostic; no refresh was attempted and no model/account admission is possible.
