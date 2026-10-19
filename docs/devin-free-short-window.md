# Devin SWE-2 free short window

The free short-window limit is observed only from the bounded, recent-unwrapped pane text of a Devin worker. Scopefuel does not query Devin for this signal. It stores the latest live observation in its XDG cache directory at devin-free-limit.json, protected by a file lock and atomic replacement.

The current Devin provider result does not expose a stable account fingerprint, so the observer normally uses a hash of the current host name and Unix user id. If a local snapshot already contains an account-kind credential-free fingerprint, the observation uses that identity instead. The fallback is host-local and may not distinguish Devin accounts used by the same Unix user on that host. A later account-keyed record will not match the old host-user key.

The existing quota-share channel publishes provider quota snapshots to a remote document endpoint keyed by account fingerprint, and it publishes or reads only when an account-kind fingerprint is available. Devin currently supplies no stable account fingerprint, so quota-share cannot bind this pane observation to a Devin account; adding an observation record type alone would not make it safe to share. A credential-free Devin account identity and a typed observation writer and gate-time reader would both be needed for cross-host propagation. No network endpoint is added.

Install order is scopefuel first, then agent-skills. The scopefuel commit that first accepts observe devin-free-limit is the minimum version required by the worker observer. An older scopefuel rejects the command; the agent-skills observer logs that failure and continues the job lifecycle.
