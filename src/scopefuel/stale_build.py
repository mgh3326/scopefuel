"""task #952 — warn when the installed build lags origin/main.

scopefuel ships its catalog bundled in the package (the server catalog is on
hold), so a host only picks up catalog changes by reinstalling — and a stale
install is otherwise invisible.  This module answers two questions cheaply:

* **What rev is installed?**  PEP 610 ``direct_url.json`` in the running
  distribution's dist-info is primary — it is the record of *this* build:
  ``vcs_info.commit_id`` for a git install.  For an editable/local install
  (``dir_info``) the rev is the checkout's own ``git rev-parse HEAD``.  When
  no ``direct_url.json`` exists at all, the uv tool receipt
  (``~/.local/share/uv/tools/scopefuel/uv-receipt.toml``) records the
  resolved ``git=...?rev=`` — but only for a distribution that carries no
  direct_url, since a receipt describes a *different* environment than an
  editable install and must not be attributed to it.

* **Where is origin/main?**  The GitHub REST API
  (``GET /repos/{REPO}/commits/main``) answers the head sha, and
  ``GET /repos/{REPO}/compare/{installed}...{head}`` answers how many
  commits the install is behind.  ``git ls-remote`` is the head fallback.
  The repo is public, so no credential is needed.

Contract with every caller:

* the verdict is cached for ``CHECK_TTL_S`` (>= 1h) so a spawn-time gate
  almost always costs a file read;
* the network probe is bounded by ``PROBE_BUDGET_S`` (~2s) total;
* offline / unreachable / unparseable → silent skip (``warning()`` → None);
* ``warning()`` never raises — a broken check must never fail a gate.

``SCOPEFUEL_STALE_WARN=0`` (or ``off``/``false``/``no``) disables the whole
check — the ops kill switch and the test isolation knob.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import pathlib
import re
import subprocess
import tempfile
import threading
import time
import tomllib
import urllib.parse
from dataclasses import dataclass
from typing import Any

from . import http
from .cache import cache_dir

REPO = "mgh3326/scopefuel"
_API = "https://api.github.com"
REINSTALL_COMMAND = "uv tool install --force git+https://github.com/mgh3326/scopefuel@main"
CHECK_TTL_S = 3600.0
PROBE_BUDGET_S = 2.0
# urllib's timeout is per socket operation (connect, then each read), so the
# probe sequence as a whole is bounded by a wall-clock deadline plus a small
# grace for thread scheduling — not by summing per-op timeouts.
_PROBE_GRACE_S = 0.1
# A checked_at further than this in the future (clock step, bad write) is not
# fresh — it would otherwise satisfy ``now - checked_at < CHECK_TTL_S``
# forever. Same tolerance idea as bench's _CACHE_CLOCK_SKEW_S.
_CHECKED_AT_SKEW_S = 60.0
DISABLE_ENV = "SCOPEFUEL_STALE_WARN"
_DIST_NAME = "scopefuel"
_CACHE_SCHEMA = "scopefuel.stale_build.v1"

_STATUS_BEHIND = "behind"
_STATUS_REV_UNKNOWN = "rev_unknown"
_STATUS_CURRENT = "current"
_STATUS_SKIPPED = "skipped"  # probe could not answer — cached so we do not retry every call

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)


def _short(rev: str | None) -> str:
    return rev[:7] if rev else "unknown"


@dataclass(frozen=True)
class Verdict:
    """A warning-worthy verdict — only ``behind``/``rev_unknown`` instances exist."""

    status: str  # _STATUS_BEHIND | _STATUS_REV_UNKNOWN
    installed_rev: str | None
    origin_rev: str | None
    behind: int | None
    # Provenance of the catalog view already resolved in this process, set by
    # the caller (cli never reads the catalog for this). "server"/"cache*"
    # means the rows are current and only the launcher code is stale;
    # "snapshot"/"unsupported"/None means the bundled catalog is what a
    # reinstall refreshes, so the wording stays #952's.
    catalog_source: str | None = None

    @property
    def line(self) -> str:
        """The one-line human warning (stderr surface for text outputs)."""
        if self.status == _STATUS_REV_UNKNOWN:
            detail = f"installed rev unknown; origin/main is {_short(self.origin_rev)}"
        elif self.behind is None:
            detail = (
                f"installed {_short(self.installed_rev)} differs from origin/main "
                f"{_short(self.origin_rev)} (commit distance unknown)"
            )
        else:
            detail = (
                f"installed {_short(self.installed_rev)} is {self.behind} commit(s) "
                f"behind origin/main {_short(self.origin_rev)}"
            )
        qualifier = ""
        if self.catalog_source == "server" or (
            isinstance(self.catalog_source, str) and self.catalog_source.startswith("cache")
        ):
            qualifier = (
                f"; catalog rows come from the server (catalog={self.catalog_source}),"
                " only the launcher code is stale"
            )
        return f"warning: scopefuel build stale — {detail}{qualifier}; reinstall: {REINSTALL_COMMAND}"

    def as_field(self) -> dict[str, Any]:
        """The --json payload form (schema=scopefuel.v1 additive field)."""
        return {
            "status": self.status,
            "installed_rev": self.installed_rev,
            "origin_rev": self.origin_rev,
            "behind": self.behind,
            "catalog_source": self.catalog_source,
            "reinstall": REINSTALL_COMMAND,
            "message": self.line,
        }


# ---------------------------------------------------------------------------
# injectable seams (tests fake these — no real network or receipt reads)
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _monotonic() -> float:
    return time.monotonic()


def _get_json(url: str, timeout: float) -> dict[str, Any]:
    return http.request_json(
        url,
        headers={"User-Agent": "scopefuel", "Accept": "application/vnd.github+json"},
        timeout=timeout,
    )


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=False, text=True
    )


def _dist_direct_url() -> str | None:
    try:
        return importlib.metadata.distribution(_DIST_NAME).read_text("direct_url.json")
    except Exception:
        return None


def _receipt_path() -> pathlib.Path:
    tools_dir = os.environ.get("UV_TOOL_DIR") or str(
        pathlib.Path.home() / ".local" / "share" / "uv" / "tools"
    )
    return pathlib.Path(tools_dir) / _DIST_NAME / "uv-receipt.toml"


# ---------------------------------------------------------------------------
# installed rev — PEP 610 direct_url.json (git commit or the checkout itself),
# else the uv tool receipt; a direct_url never defers to the receipt, which
# describes a different environment and must not be attributed to this one
# ---------------------------------------------------------------------------


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def _local_checkout_rev(path: str | None) -> str | None:
    """``git rev-parse HEAD`` of an editable/local install — local-only, best-effort.

    Runs outside the network deadline but is a <20ms operation on any sane
    filesystem; the 0.3s cap keeps a wedged mount from adding meaningfully to
    the ~2s budget.
    """

    if not path or not pathlib.Path(path).is_dir():
        return None
    try:
        completed = _run(["git", "-C", path, "rev-parse", "HEAD"], timeout=0.3)
    except Exception:
        return None
    rev = completed.stdout.strip() if completed.returncode == 0 else ""
    return rev if _valid_sha(rev) else None


def _direct_url_rev() -> tuple[str, str | None] | None:
    """("git", rev) | ("dir", checkout path) | ("other", None); None = no direct_url."""

    raw = _dist_direct_url()
    if raw is None:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return ("other", None)
    if not isinstance(info, dict):
        return ("other", None)
    rev = (info.get("vcs_info") or {}).get("commit_id")
    if _valid_sha(rev):
        return ("git", rev)
    dir_info = info.get("dir_info")
    if isinstance(dir_info, dict):
        path = urllib.parse.unquote(urllib.parse.urlsplit(info.get("url") or "").path)
        return ("dir", path or None)
    return ("other", None)


def _receipt_rev() -> str | None:
    try:
        data = tomllib.loads(_receipt_path().read_text())
    except Exception:
        return None
    requirements = data.get("tool", {}).get("requirements") if isinstance(data, dict) else None
    for requirement in requirements or []:
        if not isinstance(requirement, dict) or requirement.get("name") != _DIST_NAME:
            continue
        git = requirement.get("git")
        if not git:
            continue
        revs = urllib.parse.parse_qs(urllib.parse.urlsplit(git).query).get("rev")
        if revs and _valid_sha(revs[0]):
            return revs[0]
    return None


def _installed_rev() -> str | None:
    direct = _direct_url_rev()
    if direct is None:
        return _receipt_rev()
    kind, value = direct
    if kind == "git":
        return value
    if kind == "dir":
        return _local_checkout_rev(value)
    return None


# ---------------------------------------------------------------------------
# origin/main probe — bounded by the caller's deadline
# ---------------------------------------------------------------------------


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - _monotonic())


def _api_head_rev(deadline: float) -> str | None:
    timeout = _remaining(deadline)
    if timeout <= 0:
        return None
    try:
        data = _get_json(f"{_API}/repos/{REPO}/commits/main", timeout=timeout)
    except Exception:
        return None
    sha = data.get("sha") if isinstance(data, dict) else None
    return sha if _valid_sha(sha) else None


def _lsremote_head_rev(deadline: float) -> str | None:
    timeout = _remaining(deadline)
    if timeout <= 0:
        return None
    try:
        completed = _run(
            ["git", "ls-remote", f"https://github.com/{REPO}.git", "refs/heads/main"],
            timeout=timeout,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    fields = completed.stdout.split()
    return fields[0] if fields and _valid_sha(fields[0]) else None


def _origin_rev(deadline: float) -> str | None:
    return _api_head_rev(deadline) or _lsremote_head_rev(deadline)


def _commits_ahead(installed: str, head: str, deadline: float) -> int | None:
    """Commits on origin/main the install lacks (compare.ahead_by); None when unknown."""

    if not (_valid_sha(installed) and _valid_sha(head)):
        return None
    timeout = _remaining(deadline)
    if timeout <= 0:
        return None
    try:
        data = _get_json(f"{_API}/repos/{REPO}/compare/{installed}...{head}", timeout=timeout)
    except Exception:
        return None
    ahead = data.get("ahead_by") if isinstance(data, dict) else None
    return int(ahead) if isinstance(ahead, int) and not isinstance(ahead, bool) and ahead >= 0 else None


# ---------------------------------------------------------------------------
# verdict cache — TTL keeps the hot path a file read
# ---------------------------------------------------------------------------


def _cache_path() -> pathlib.Path:
    return cache_dir() / "stale_build.json"


def _read_cache() -> dict[str, Any] | None:
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, ValueError, RecursionError):
        # Every read or parse failure reads as absent — JSONDecodeError and
        # UnicodeDecodeError are ValueErrors, a >4300-digit int is a plain
        # ValueError, deep nesting is a RecursionError — so _check re-probes
        # and the next write replaces the bad file.
        return None
    return data if isinstance(data, dict) and data.get("schema") == _CACHE_SCHEMA else None


def _write_cache(entry: dict[str, Any]) -> None:
    path = _cache_path()
    tmp: pathlib.Path | None = None
    try:
        # Serialize before mkstemp so an unserializable entry fails before any
        # temp file exists (a dumps error is a TypeError, not an OSError).
        payload = json.dumps(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent, prefix="stale_build.", suffix=".tmp")
        tmp = pathlib.Path(name)
        try:
            fh = os.fdopen(fd, "w")
        except OSError:
            # fdopen never took ownership of the mkstemp fd — close it here.
            os.close(fd)
            raise
        with fh:
            fh.write(payload)
        tmp.chmod(0o600)
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()
        # a cache write failure must not break the check


def _cache_verdict(cached: dict[str, Any]) -> Verdict | None:
    status = cached.get("status")
    if status not in (_STATUS_BEHIND, _STATUS_REV_UNKNOWN):
        return None
    origin_rev = cached.get("origin_rev")
    installed_rev = cached.get("installed_rev")
    behind = cached.get("behind")
    return Verdict(
        status=status,
        installed_rev=installed_rev if isinstance(installed_rev, str) else None,
        origin_rev=origin_rev if isinstance(origin_rev, str) else None,
        behind=behind if isinstance(behind, int) and not isinstance(behind, bool) else None,
    )


def _disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() in {"0", "off", "false", "no"}


def _cache_entry(
    installed: str | None,
    head: str | None,
    verdict: Verdict | None,
    status: str,
    now: float,
) -> dict[str, Any]:
    return {
        "schema": _CACHE_SCHEMA,
        "checked_at": now,
        "installed_rev": installed,
        "origin_rev": head,
        "behind": verdict.behind if verdict else (0 if status == _STATUS_CURRENT else None),
        "status": status,
    }


def _probe(
    installed: str | None,
    deadline: float,
    expired: threading.Event,
) -> tuple[str, str | None, Verdict | None]:
    """The probe sequence itself — origin head, then compare distance.

    Returns ``(status, head, verdict)`` for the caller to record; ``_probe``
    never touches the cache — the calling thread in ``_bounded_probe`` is the
    only writer — so a worker abandoned at the wall-clock bound cannot
    overwrite the caller's skipped record. ``expired`` is set by the caller
    when the bound ran out: the abandoned worker then cuts the sequence short
    instead of finishing calls nobody will read.
    """

    head = _origin_rev(deadline)
    status: str
    verdict: Verdict | None
    if head is None or expired.is_set():
        status, verdict = _STATUS_SKIPPED, None
    elif installed is None:
        status, verdict = _STATUS_REV_UNKNOWN, Verdict(_STATUS_REV_UNKNOWN, None, head, None)
    elif installed == head:
        status, verdict = _STATUS_CURRENT, None
    else:
        behind = None if expired.is_set() else _commits_ahead(installed, head, deadline)
        if behind == 0:
            # Installed strictly ahead of main (e.g. an unreleased local build
            # installed by rev) — ahead, never behind: no warning.
            status, verdict = _STATUS_CURRENT, None
        else:
            status, verdict = _STATUS_BEHIND, Verdict(_STATUS_BEHIND, installed, head, behind)

    return status, head, verdict


def _bounded_probe(installed: str | None, now: float) -> Verdict | None:
    """Run the whole probe under one wall-clock bound.

    ``_remaining(deadline)`` hands each socket operation the leftover budget,
    but urllib's timeout is per operation (connect, then each read) — one
    probe could otherwise take several times PROBE_BUDGET_S. The probe runs on
    a daemon thread so a wedged socket cannot keep the process alive.

    The calling thread is the only cache writer — exactly one write per call:
    if the worker is still alive at the deadline the caller flags ``expired``
    (the abandoned thread then stops early and never writes), records the
    attempt as skipped and returns; otherwise the caller writes the entry
    built from the worker's result. A worker that raised writes nothing — the
    cache simply keeps whatever it held, as before.
    """

    deadline = _monotonic() + PROBE_BUDGET_S
    expired = threading.Event()
    box: dict[str, tuple[str, str | None, Verdict | None] | None] = {"result": None}

    def run() -> None:
        try:
            box["result"] = _probe(installed, deadline, expired)
        except Exception:
            box["result"] = None

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=_remaining(deadline) + _PROBE_GRACE_S)
    if worker.is_alive():
        expired.set()
        _write_cache(_cache_entry(installed, None, None, _STATUS_SKIPPED, now))
        return None
    result = box["result"]
    if result is None:
        return None
    status, head, verdict = result
    _write_cache(_cache_entry(installed, head, verdict, status, now))
    return verdict


def _check() -> Verdict | None:
    now = _now()
    installed = _installed_rev()
    cached = _read_cache()
    try:
        # A checked_at beyond the skew tolerance (clock step, bad write) is not
        # fresh — otherwise ``now - checked_at < TTL`` stays true forever and
        # the verdict would never be re-probed.
        checked_at = float(cached.get("checked_at") or 0) if cached is not None else 0.0
        fresh = (
            cached is not None and checked_at <= now + _CHECKED_AT_SKEW_S and now - checked_at < CHECK_TTL_S
        )
    except (TypeError, ValueError, OverflowError):
        fresh = False
    if fresh and cached is not None and cached.get("installed_rev") == installed:
        # The install did not move since the probe — the cached verdict stands.
        # (A changed installed rev re-probes: a fresh install must not inherit
        # the old rev's "behind" warning.)
        return _cache_verdict(cached)

    return _bounded_probe(installed, now)


def warning() -> Verdict | None:
    """The stale-build verdict when a warning should print, else None.

    Never raises: the disabled flag, every cache read, and every probe call
    funnel through here so a broken check degrades to silence rather than
    breaking the command that hosts it.
    """
    if _disabled():
        return None
    try:
        return _check()
    except Exception:
        return None
