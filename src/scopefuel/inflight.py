"""In-flight job sum for shared quota pools (hk 1787, b953).

hk 1767 found three codex jobs admitted back-to-back that then overlapped for
~42 minutes: each gate run judged one local snapshot and never saw the other
in-flight jobs. The arbiter ``quota_pool`` row is a record, not a reservation,
and each host has its own arbiter ``state.db`` — a local count alone misses
jobs running on sibling hosts under the same account. This module adds the
missing sum: local live records plus cross-host published sets, keyed by
``account_fp`` — ``quota/<pool>/<account_fp>/inflight/<host>``.

- Local set: ``arbiter status --json`` (the binary resolves from
  ``ARBITER_BIN`` — the name wrk uses — then ``arbiter`` on PATH). The arbiter
  opens its own DB read-only; this module never opens ``state.db``.
- Publish: after a gate read a local set for a pool with a known account_fp,
  the host's live list goes up under its own key — even when empty, so a
  release clears the entry at the host's next gate run. Same transport,
  endpoint and fail-open rules as ``quota_share.publish_result``, and the same
  ``_safe_host`` sanitiser. Payload has a fixed key set — job ids and start
  times only, no profile names, no tokens.
- Read: every doc under the prefix except this host's own. A doc counts only
  when it parses with the fixed schema, pool/account_fp match, and
  ``published_at`` is at most ``MAX_FUTURE_SKEW_S`` in the future; a job counts
  only when its ``started_at`` is within ``INFLIGHT_MAX_AGE_S`` of now. The
  doc's own age never drops it — a host that has not gated since a release
  keeps that job counted until it ages out, which overcounts, and overcounting
  is the safe side.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import shutil
import socket
import subprocess
import urllib.parse
from dataclasses import dataclass, field

from . import bench, quota_share

SCHEMA = "scopefuel.quota-inflight.v1"
DOC_KIND = "note"
DOC_SESSION = "scopefuel-quota-share"
# A live record older than this is stale bookkeeping, not an in-flight job —
# wrk records survive crashed panes, so the sum needs a ceiling.
INFLIGHT_MAX_AGE_S = 8 * 3600.0
# The local read rides on the gate path — bound it like quota_share's calls.
ARBITER_TIMEOUT_S = 5.0
LIST_LIMIT = 100

_DOC_PREFIX = "/v1/documents"
_ENV_ARBITER = "ARBITER_BIN"

_PAYLOAD_KEYS = frozenset({"schema", "pool", "account_fp", "host", "published_at", "jobs"})


def _local_host() -> str:
    return quota_share._safe_host(socket.gethostname())


def _arbiter_bin() -> str | None:
    """The arbiter binary: ``ARBITER_BIN`` first (wrk's knob), then PATH."""
    explicit = os.environ.get(_ENV_ARBITER)
    if explicit:
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else None
    return shutil.which("arbiter")


def _run_arbiter(argv: list[str]) -> subprocess.CompletedProcess:
    """The one subprocess seam — tests spy on this, never on real arbiter."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=ARBITER_TIMEOUT_S)


def local_jobs(pool: str) -> list[dict] | None:
    """Live ``quota_pool_records`` rows for ``pool``, or None when unreadable.

    A released row is gone from the table, so every surviving row counts. The
    job being gated has no record yet, so it never counts itself. Any failure
    — missing binary, timeout, non-zero rc, unreadable JSON — folds to None:
    the caller degrades to the base decision and tags the count unknown.
    """
    binary = _arbiter_bin()
    if binary is None:
        return None
    try:
        proc = _run_arbiter([binary, "status", "--json"])
    except Exception:
        return None
    if proc.returncode != 0 or not isinstance(proc.stdout, str):
        return None
    try:
        doc = json.loads(proc.stdout)
    except (ValueError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    rows = doc.get("quota_pool_records")
    if not isinstance(rows, list):
        return None
    jobs: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("pool") != pool:
            continue
        job_id = row.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            continue
        started_at = row.get("started_at")
        jobs.append({"job_id": job_id, "started_at": started_at if isinstance(started_at, str) else None})
    return jobs


def doc_key(pool: str, account_fp: str, host: str) -> str:
    return f"quota/{pool}/{account_fp}/inflight/{host}"


def _request(method: str, suffix: str, document: dict | None = None) -> dict | None:
    """One authenticated hk call — same endpoint/transport/fail-open as quota_share."""
    if not quota_share.enabled():
        return None
    endpoint = quota_share._endpoint()
    if endpoint is None:
        return None
    url, token = endpoint
    try:
        payload = bench._handoffkeep_send(
            f"{url}{_DOC_PREFIX}{suffix}",
            token=token,
            method=method,
            body=document,
            timeout=quota_share.REQUEST_TIMEOUT_S,
            fetch=quota_share.request_json,
        )
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def publish_jobs(
    pool: str,
    account_fp: str,
    jobs: list[dict],
    *,
    host: str | None = None,
    now: float | None = None,
) -> bool:
    """PUT this host's live set. Never raises — a failed publish costs nothing.

    The payload has exactly the fixed key set; an empty job list is published
    so a release clears the host's entry at its next gate run.
    """
    try:
        host = host or _local_host()
        epoch = now if isinstance(now, (int, float)) and math.isfinite(now) else None
        if epoch is None:
            epoch = dt.datetime.now(dt.UTC).timestamp()
        key = doc_key(pool, account_fp, host)
        document = {
            "key": key,
            "kind": DOC_KIND,
            "session": DOC_SESSION,
            "job": "",
            "body": json.dumps(
                {
                    "schema": SCHEMA,
                    "pool": pool,
                    "account_fp": account_fp,
                    "host": host,
                    "published_at": dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat(),
                    "jobs": [{"job_id": job["job_id"], "started_at": job.get("started_at")} for job in jobs],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
        response = _request("PUT", f"/{urllib.parse.quote(key, safe='')}", document)
        return isinstance(response, dict) and isinstance(response.get("document"), dict)
    except Exception:
        return False


def _parse_ts(value: object) -> dt.datetime | None:
    """ISO-8601 → aware datetime. Naive or unparseable is rejected."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


@dataclass
class JobView:
    job_id: str
    counted: bool
    reason: str = ""


@dataclass
class DocView:
    """One remote doc as read — for ``inflight show`` and the gate's own count."""

    key: str
    host: str | None
    published_at: str | None
    jobs: list[JobView] = field(default_factory=list)
    dropped_reason: str | None = None  # whole-doc drop (self, schema, skew…)

    @property
    def counted(self) -> int:
        return sum(1 for job in self.jobs if job.counted)


@dataclass
class RemoteRead:
    counted_ids: set[str]
    docs: list[DocView]

    @property
    def count(self) -> int:
        return len(self.counted_ids)


def _doc_view(key: str, document: dict, pool: str, account_fp: str, host: str, now: float) -> DocView:
    """Validate one document against the fixed schema and count its jobs.

    Every filter lives here in order: self, parse, schema, pool, account_fp,
    published_at future-skew — then per-job started_at age and dedupe.
    """
    view = DocView(key=key, host=None, published_at=None)
    if key.rsplit("/", 1)[-1] == host:
        view.dropped_reason = "self"
        return view
    body = document.get("body")
    try:
        snap = json.loads(body) if isinstance(body, str) else None
    except (ValueError, RecursionError):
        snap = None
    if not isinstance(snap, dict) or set(snap) != _PAYLOAD_KEYS or snap.get("schema") != SCHEMA:
        view.dropped_reason = "schema"
        return view
    view.host = snap["host"] if isinstance(snap.get("host"), str) else None
    if view.host == host:
        view.dropped_reason = "self"
        return view
    view.published_at = snap["published_at"] if isinstance(snap.get("published_at"), str) else None
    if snap.get("pool") != pool:
        view.dropped_reason = "pool_mismatch"
        return view
    if snap.get("account_fp") != account_fp:
        view.dropped_reason = "account_mismatch"
        return view
    published = _parse_ts(snap.get("published_at"))
    if published is None:
        view.dropped_reason = "invalid_published_at"
        return view
    if published.timestamp() - now > quota_share.MAX_FUTURE_SKEW_S:
        view.dropped_reason = "future_skew"
        return view
    jobs = snap.get("jobs")
    if not isinstance(jobs, list):
        view.dropped_reason = "invalid_jobs"
        return view
    for job in jobs:
        job_id = job.get("job_id") if isinstance(job, dict) else None
        if not isinstance(job_id, str) or not job_id:
            view.jobs.append(JobView(str(job_id) if job_id is not None else "", False, "invalid_job_id"))
            continue
        started = _parse_ts(job.get("started_at"))
        if started is None:
            view.jobs.append(JobView(job_id, False, "invalid_started_at"))
        elif abs(now - started.timestamp()) > INFLIGHT_MAX_AGE_S:
            view.jobs.append(JobView(job_id, False, "aged"))
        else:
            view.jobs.append(JobView(job_id, True))
    return view


def remote_jobs(
    pool: str,
    account_fp: str,
    *,
    host: str | None = None,
    now: float | None = None,
) -> RemoteRead | None:
    """Remote in-flight jobs for one account_fp, or None on a list failure.

    The own-host doc is dropped before counting; job ids dedupe across docs.
    A remote doc's own age never drops it — stale hosts overcount, which is
    the safe side.
    """
    host = host or _local_host()
    epoch = (
        now if isinstance(now, (int, float)) and math.isfinite(now) else dt.datetime.now(dt.UTC).timestamp()
    )
    prefix = f"quota/{pool}/{account_fp}/inflight/"
    payload = _request("GET", f"?{urllib.parse.urlencode({'prefix': prefix, 'limit': LIST_LIMIT})}")
    if payload is None:
        return None
    documents = payload.get("documents")
    if not isinstance(documents, list):
        return None
    counted_ids: set[str] = set()
    views: list[DocView] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        key = document.get("key")
        view = _doc_view(key if isinstance(key, str) else "", document, pool, account_fp, host, epoch)
        views.append(view)
        if view.dropped_reason is not None:
            continue
        for job in view.jobs:
            if job.counted and job.job_id in counted_ids:
                job.counted = False
                job.reason = "duplicate"
            elif job.counted:
                counted_ids.add(job.job_id)
    return RemoteRead(counted_ids=counted_ids, docs=views)


def account_fingerprint(result: object) -> str | None:
    """The account fingerprint a gate run publishes/reads under.

    Same resolution as ``quota_share.publish_result``: ``publish_fp`` wins so
    codex — which keeps its local fp gates inert — still names its account.
    Token-kind fingerprints never key documents.
    """
    if result is None:
        return None
    fp = getattr(result, "publish_fp", None) or getattr(result, "account_fp", None)
    kind = getattr(result, "publish_fp_kind", None) or getattr(result, "account_fp_kind", None)
    if not isinstance(fp, str) or not fp or kind != "account":
        return None
    return fp


def show(pool: str, *, now: float | None = None) -> dict:
    """Read-only observation for ``scopefuel inflight show``. Never publishes.

    Lists every inflight doc under ``quota/<pool>/`` and reports the local
    set plus, per account_fp, each doc's counted and dropped jobs and the
    resulting n = local live + counted remote jobs.
    """
    host = _local_host()
    epoch = (
        now if isinstance(now, (int, float)) and math.isfinite(now) else dt.datetime.now(dt.UTC).timestamp()
    )
    local = local_jobs(pool)
    prefix = f"quota/{pool}/"
    payload = _request("GET", f"?{urllib.parse.urlencode({'prefix': prefix, 'limit': LIST_LIMIT})}")
    documents = payload.get("documents") if isinstance(payload, dict) else None
    accounts: dict[str, dict] = {}
    if isinstance(documents, list):
        for document in documents:
            if not isinstance(document, dict):
                continue
            key = document.get("key")
            if not isinstance(key, str):
                continue
            parts = key.split("/")
            if len(parts) != 5 or parts[0] != "quota" or parts[1] != pool or parts[3] != "inflight":
                continue
            fp = parts[2]
            accounts.setdefault(fp, []).append(document)
    report_accounts = []
    for fp in sorted(accounts):
        counted_ids: set[str] = set()
        views = []
        for document in accounts[fp]:
            key = document.get("key") if isinstance(document.get("key"), str) else ""
            view = _doc_view(key, document, pool, fp, host, epoch)
            if view.dropped_reason is None:
                for job in view.jobs:
                    if job.counted and job.job_id in counted_ids:
                        job.counted = False
                        job.reason = "duplicate"
                    elif job.counted:
                        counted_ids.add(job.job_id)
            views.append(view)
        local_n = len(local) if local is not None else 0
        report_accounts.append(
            {
                "account_fp": fp,
                "docs": views,
                "remote_count": len(counted_ids),
                "n": local_n + len(counted_ids) if local is not None else None,
            }
        )
    return {
        "pool": pool,
        "host": host,
        "local": local,
        "accounts": report_accounts,
        "listed": isinstance(documents, list),
    }
