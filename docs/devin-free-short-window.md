# Devin SWE-2 free short window

The free short-window limit is observed only from the bounded, recent-unwrapped pane text of a Devin worker. Scopefuel does not query Devin for this signal. It stores the latest live observation in its XDG cache directory at devin-free-limit.json, protected by a file lock and atomic replacement.

When the local snapshot cache already contains an account-kind credential-free account fingerprint, the observation uses that identity. If it is unavailable, the record uses a hash of the current host name and Unix user id. The fallback is host-local and may not distinguish Devin accounts used by the same Unix user on that host. A later cache entry with a different account identity will not apply the old account-keyed record.

The existing quota-share channel publishes provider quota snapshots to a remote document endpoint. It has no typed pane-observation record and gate-time reader for this signal, so free short-window observations remain local. Limits therefore do not propagate between hosts. No network endpoint is added.

Install order is scopefuel first, then agent-skills. The scopefuel commit that first accepts observe devin-free-limit is the minimum version required by the worker observer. An older scopefuel rejects the command; the agent-skills observer logs that failure and continues the job lifecycle.
