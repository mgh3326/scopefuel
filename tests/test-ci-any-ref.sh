#!/usr/bin/env bash
# Dry-run harness for .github/workflows/ci-any-ref.yml (hk 2208 port of hk 2190,
# with the hk 2199 concurrency fix baked in).
# Proves, without a real GitHub run:
#   (a) checkout is exactly the input ref with no merge
#   (b) the commit status is posted on the resolved target sha, not the dispatch sha
#   (c) a non-resolving ref is refused
#   (d) the permissions block is minimal
#   (e) the concurrency group is keyed on the RESOLVED sha plus the suite, never
#       inputs.ref alone
# and that mutants (merge main first; post on the dispatch sha; ci-ok for a
# partial suite; inline inputs.* in a run body; drop the sha from the group key;
# drop the suite from the group key) are RED.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"

python3 - "$root" <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
wf = (root / ".github/workflows/ci-any-ref.yml").read_text(encoding="utf-8")
ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def top_block(text: str, key: str) -> str:
    match = re.search(rf"(?m)^{key}:\n((?:  .*\n| *#.*\n|\s*\n)*)", text)
    assert match, f"missing top-level block {key!r}"
    return match.group(1)


def job_block(text: str, job: str) -> str:
    match = re.search(rf"(?ms)^  {job}:\n(.*?)(?=^  \w|\Z)", text)
    assert match, f"missing job {job!r}"
    return match.group(1)


def validate(wf_text: str, ci_text: str) -> None:
    on_block = top_block(wf_text, "on")
    triggers = re.findall(r"(?m)^  (\w[\w-]*):", on_block)
    assert triggers == ["workflow_dispatch"], f"ci-any-ref must be dispatch-only, got {triggers}"

    assert re.search(r"(?m)^      ref:\n(?:        .*\n)*        required: true", wf_text), \
        "input ref must be required"
    assert re.search(r"(?m)^      suite:\n(?:        .*\n)*        default: all", wf_text), \
        "input suite must default to all"

    perms = top_block(wf_text, "permissions")
    perm_keys = dict(re.findall(r"(?m)^  (\w[\w-]*): (\w+)", perms))
    assert perm_keys == {"contents": "read", "statuses": "write"}, \
        f"workflow permissions must be exactly contents:read + statuses:write, got {perm_keys}"

    def perm_decls(text: str) -> list[tuple[int, str, dict]]:
        """All `permissions:` declarations: (indent, form, {scope: level})."""
        decls = []
        for match in re.finditer(r"(?m)^([ \t]*)permissions: *([^#\n]*)$", text):
            indent, tail = len(match.group(1)), match.group(2).strip()
            if tail.startswith("{"):
                inner = tail.strip("{} ")
                decls.append((indent, "inline", dict(
                    kv.split(":") for kv in
                    (pair.strip() for pair in inner.split(",") if pair.strip()))))
            elif tail:
                decls.append((indent, "scalar", {tail.replace("-all", ""): "all"}))
            else:
                rest = text[match.end() + 1:]
                body = re.match(rf"(?: {{{indent + 1},}}[^\n]*\n?)*", rest).group(0)
                decls.append((indent, "block", dict(re.findall(
                    rf"(?m)^ {{{indent + 2}}}(\w[\w-]*): (\w+)", body))))
        return decls

    LEVELS = {"none": 0, "read": 1, "write": 2, "all": 3, "write-all": 3, "read-all": 3}

    # Permission ceiling: the callee may only hold a subset of what the calling
    # job passes down, or the run fails validation at startup.
    callee_decls = perm_decls(ci_text)
    callee_top = next((d for i, _f, d in callee_decls if i == 0), {})
    callee_jobs = [d for i, _f, d in callee_decls if i > 0]
    ci_call = re.search(
        r"(?m)^  ci:\n(?:    .*\n|      .*\n|        .*\n)*?    uses: \./\.github/workflows/ci\.yml",
        wf_text,
    )
    assert ci_call, "ci job must call ci.yml"
    caller_grants = dict(re.findall(
        r"(?m)^      (\w[\w-]*): (\w+)",
        re.search(r"(?m)^    permissions:\n((?:      .*\n)+)", ci_call.group(0)).group(1),
    ))
    union_needed = {k: max(LEVELS[d[k]] for d in [callee_top, *callee_jobs]) for k in
                    {k for d in [callee_top, *callee_jobs] for k in d}}
    for scope, need in union_needed.items():
        assert scope in caller_grants, f"calling job must grant the callee's {scope} scope"
        assert LEVELS[caller_grants[scope]] >= need, f"grant level too low for {scope}"
    assert caller_grants.get("statuses") in (None, "none"), \
        "calling job must not need statuses:write"

    # Refusal: a failed resolve must skip ci and the status job.
    assert "needs: resolve" in ci_call.group(0)
    status_if = re.search(r"(?ms)^  status:.*?^    if: (.*)$", wf_text)
    assert status_if and "needs.resolve.result == 'success'" in status_if.group(1), \
        "status job must be gated on resolve success"
    assert "needs: [resolve, ci]" in wf_text

    # Concurrency (hk 2199): the group is keyed on the RESOLVED sha plus the
    # suite on the ci and status jobs, never on inputs.ref alone. The resolve
    # job may keep a short per-input group.
    assert re.search(r"(?m)^concurrency:", wf_text) is None, \
        "the concurrency group must live on the ci/status jobs, not top level"
    for job in ("ci", "status"):
        block = job_block(wf_text, job)
        group = re.search(r"(?ms)^    concurrency:\n      group: ([^\n]+)", block)
        assert group, f"{job} job must carry a concurrency group"
        group = group.group(1)
        assert "needs.resolve.outputs.sha" in group, \
            f"{job} concurrency group must key on the resolved sha, got {group!r}"
        assert "inputs.suite" in group, \
            f"{job} concurrency group must key on the suite, got {group!r}"
        assert "inputs.ref" not in group, \
            f"{job} concurrency group must not key on inputs.ref, got {group!r}"
        assert re.search(r"(?m)^      cancel-in-progress: true", block), \
            f"{job} job must keep cancel-in-progress"

    assert "uses: ./.github/workflows/ci.yml" in wf_text, \
        "must reuse ci.yml via workflow_call, not copy jobs"
    assert "ref: ${{ needs.resolve.outputs.sha }}" in wf_text, \
        "reusable call must pass the resolved sha"
    assert "suite: ${{ inputs.suite }}" in wf_text

    # status target: must be the resolved sha, never github.sha / the dispatch sha.
    assert "statuses/${TARGET_SHA}" in wf_text, "status must post to statuses/<target sha>"
    assert "TARGET_SHA: ${{ needs.resolve.outputs.sha }}" in wf_text
    status_section = wf_text.split("status:", 1)[1]
    assert "target_url" in status_section, "status must carry the run URL"
    assert "GITHUB_SHA" not in status_section and "github.sha" not in status_section \
        and "github.event" not in status_section, \
        "status job must not derive its target from github.sha"

    # context: only a full run (suite=all) may post the gate context ci-ok.
    assert 'context="$CONTEXT"' in status_section, "status must post a computed context"
    assert re.search(
        r'if \[\[ "\$SUITE" == all \]\]; then CONTEXT=ci-ok; else CONTEXT="ci-ok-\$SUITE"',
        status_section,
    ), "partial suites must post ci-ok-<suite>, never ci-ok"
    assert 'context="ci-ok"' not in status_section, \
        "context must not be unconditionally ci-ok"

    # check-run surface: the ci-ok job is renamed for partial suites so a
    # dispatch can never mint a check named exactly 'ci-ok' either.
    ci_ok_job = re.search(r"(?ms)^  ci-ok:\n(.*?)(?=^  \w|\Z)", ci_text)
    assert ci_ok_job, "ci.yml must keep the ci-ok job"
    assert "format('ci-ok ({0})', inputs.suite)" in ci_ok_job.group(1), \
        "partial suites must name the check 'ci-ok (<suite>)'"
    assert not re.search(r"(?m)^    name: ci-ok\s*$", ci_ok_job.group(1)), \
        "the ci-ok job name must be conditional on suite"

    # no ${{ inputs.* }} expansion inside any run: body (template injection).
    for name, text in (("ci-any-ref.yml", wf_text), ("ci.yml", ci_text)):
        for match in re.finditer(r"(?m)^([ ]*)run: *([^\n]*)$", text):
            indent, tail = len(match.group(1)), match.group(2).strip()
            assert "${{ inputs." not in tail, \
                f"{name}: inputs.* must reach run bodies via env, not expansion"
            if tail.startswith(("|", ">")):
                rest = text[match.end() + 1:]
                body = re.match(rf"(?: {{{indent + 1},}}[^\n]*\n?)*", rest).group(0)
                assert "${{ inputs." not in body, \
                    f"{name}: inputs.* must reach run bodies via env, not expansion"

    # ref must reach the runner via env, never expanded into shell text.
    assert "INPUT_REF: ${{ inputs.ref }}" in wf_text
    assert 'resolve-ci-ref "$INPUT_REF"' in wf_text
    assert 'resolve-ci-ref "${{ inputs.ref }}"' not in wf_text, \
        "inputs.ref must not be expanded into the run script"

    # exact checkout: every checkout in ci.yml honors inputs.ref; no merge anywhere.
    n_checkout = len(re.findall(r"uses: actions/checkout@v[\w.:-]+", ci_text))
    n_ref = len(re.findall(r"ref: \$\{\{ inputs\.ref \}\}", ci_text))
    assert n_checkout > 0 and n_checkout == n_ref, \
        f"every checkout must pin inputs.ref ({n_ref}/{n_checkout})"
    for text in (ci_text, wf_text):
        assert not re.search(r"\bgit (merge(?!-base)|pull|rebase|reset)\b", text), \
            "workflow must not merge or move the checkout onto main"
    assert "workflow_call:" in ci_text
    assert "actions/checkout" not in wf_text.split("ci:", 1)[1].split("status:", 1)[0], \
        "the reusable call must not re-checkout"


validate(wf, ci)


def expect_red(wf_text: str, ci_text: str, name: str) -> None:
    try:
        validate(wf_text, ci_text)
    except AssertionError:
        return
    raise AssertionError(f"mutant({name}) went undetected")


# Mutant 1: merge main into the checkout -> must be RED.
mutant_merge = ci.replace(
    "          ref: ${{ inputs.ref }}",
    "          ref: ${{ inputs.ref }}\n      - run: git merge origin/main",
    1,
)
expect_red(wf, mutant_merge, "merge-main")

# Mutant 2: post the status on the dispatch sha -> must be RED.
mutant_status = wf.replace(
    "TARGET_SHA: ${{ needs.resolve.outputs.sha }}",
    "TARGET_SHA: ${{ github.sha }}",
)
expect_red(mutant_status, ci, "status-on-dispatch-sha")

# Mutant 3: post context ci-ok for every suite -> must be RED.
mutant_ctx = re.sub(
    r'if \[\[ "\$SUITE" == all \]\]; then CONTEXT=ci-ok; else CONTEXT="ci-ok-\$SUITE"; fi',
    "CONTEXT=ci-ok",
    wf,
)
expect_red(mutant_ctx, ci, "ci-ok-for-every-suite")

# Mutant 3b: the check-run name unconditional ci-ok -> must be RED.
mutant_name = ci.replace(
    "name: ${{ (inputs.suite || 'all') == 'all' && 'ci-ok' || format('ci-ok ({0})', inputs.suite) }}",
    "name: ci-ok",
)
expect_red(wf, mutant_name, "check-run-always-ci-ok")

# Mutant 4: inline inputs.* in a run body -> must be RED.
mutant_inline = wf.replace(
    'resolve-ci-ref "$INPUT_REF"',
    'resolve-ci-ref "${{ inputs.ref }}"',
)
expect_red(mutant_inline, ci, "inline-inputs-in-run")

# Mutant 5: drop the sha from the group key -> must be RED.
mutant_no_sha = wf.replace(
    "ci-any-ref-${{ needs.resolve.outputs.sha }}-${{ inputs.suite }}",
    "ci-any-ref-${{ inputs.suite }}",
)
expect_red(mutant_no_sha, ci, "drop-sha-from-group")

# Mutant 6: drop the suite from the group key -> must be RED.
mutant_no_suite = wf.replace(
    "ci-any-ref-${{ needs.resolve.outputs.sha }}-${{ inputs.suite }}",
    "ci-any-ref-${{ needs.resolve.outputs.sha }}",
)
expect_red(mutant_no_suite, ci, "drop-suite-from-group")

# Mutant 7: key the group on inputs.ref again -> must be RED.
mutant_ref_key = wf.replace(
    "ci-any-ref-${{ needs.resolve.outputs.sha }}-${{ inputs.suite }}",
    "ci-any-ref-${{ inputs.ref }}-${{ inputs.suite }}",
)
expect_red(mutant_ref_key, ci, "group-on-inputs-ref")

print("structural checks ok (incl. 7 mutants)")
PY

# --- live-ish harness: resolver + checkout simulation against a fixture repo ---
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Fixture: base commit A, then main advances to B. Target sha = A.
fixture="$tmp/fixture"
git init -q -b main "$fixture"
(
  cd "$fixture"
  git config user.email t@t && git config user.name t
  echo base > file.txt
  git add file.txt && git commit -qm base
  echo newer > newer.txt
  git add newer.txt && git commit -qm main-advance
)
SHA_A="$(git -C "$fixture" rev-parse HEAD~1)"
SHA_B="$(git -C "$fixture" rev-parse HEAD)"

# Fake gh: resolves refs from a map; unknown refs fail like the real API.
mkdir -p "$tmp/bin"
cat > "$tmp/bin/gh" <<EOF
#!/usr/bin/env bash
arg="\$*"
case "\$arg" in
  "api repos/o/r/commits/$SHA_A --jq .sha") echo "$SHA_A" ;;
  "api repos/o/r/commits/old-branch --jq .sha") echo "$SHA_A" ;;
  "api repos/o/r/statuses/"*) echo "\$arg" > "$tmp/status-call" ;;
  *) echo "Not Found" >&2; exit 1 ;;
esac
EOF
chmod +x "$tmp/bin/gh"
export PATH="$tmp/bin:$PATH"

# (c) resolution: sha, branch name, and a refusal.
[[ "$("$root/scripts/resolve-ci-ref" "$SHA_A" o/r)" == "$SHA_A" ]]
[[ "$("$root/scripts/resolve-ci-ref" old-branch o/r)" == "$SHA_A" ]]
if "$root/scripts/resolve-ci-ref" does-not-exist o/r >/dev/null 2>&1; then
  echo "non-resolving ref was not refused" >&2; exit 1
fi

# (a) exact checkout simulation: clone, checkout resolved sha -> detached at A.
git clone -q "$fixture" "$tmp/co" 2>/dev/null
git -C "$tmp/co" -c advice.detachedHead=false checkout -q "$SHA_A"
[[ "$(git -C "$tmp/co" rev-parse HEAD)" == "$SHA_A" ]]
[[ -f "$tmp/co/file.txt" && ! -f "$tmp/co/newer.txt" ]]  # old base content, not main's

# mutant sanity: merging main first moves HEAD off A.
git -C "$tmp/co" -c advice.detachedHead=false checkout -qb mutant "$SHA_A"
git -C "$tmp/co" merge -qm mutant "$SHA_B" >/dev/null
[[ "$(git -C "$tmp/co" rev-parse HEAD)" != "$SHA_A" ]]

# Context by suite: eval the workflow's own CONTEXT selection line.
# shellcheck disable=SC2016  # single-quoted grep pattern is intentional
ctx_line="$(grep -oF 'if [[ "$SUITE" == all ]]; then CONTEXT=ci-ok; else CONTEXT="ci-ok-$SUITE"; fi' "$root/.github/workflows/ci-any-ref.yml")"
[[ -n "$ctx_line" ]]
for suite in all test; do
  CONTEXT="$(SUITE="$suite" bash -c "$ctx_line; printf '%s' \"\$CONTEXT\"")"
  if [[ "$suite" == all ]]; then
    [[ "$CONTEXT" == ci-ok ]]
  else
    [[ "$CONTEXT" == "ci-ok-$suite" && "$CONTEXT" != ci-ok ]]
  fi
done

# (e) group key: eval the workflow's own group template with two shas/suites.
group_line="$(grep -oF 'ci-any-ref-${{ needs.resolve.outputs.sha }}-${{ inputs.suite }}' "$root/.github/workflows/ci-any-ref.yml" | head -1)"
[[ -n "$group_line" ]]
group_key() {  # $1 sha, $2 suite
  local sha="$1" suite="$2"
  printf '%s' "${group_line//'${{ needs.resolve.outputs.sha }}'/$sha}" |
    sed "s/\${{ inputs\.suite }}/$suite/"
}
[[ "$(group_key "$SHA_A" all)" == "$(group_key "$SHA_A" all)" ]]       # same sha+suite -> one key
[[ "$(group_key "$SHA_A" all)" != "$(group_key "$SHA_A" test)" ]]     # different suite -> different key
[[ "$(group_key "$SHA_A" all)" != "$(group_key "$SHA_B" all)" ]]      # different sha -> different key

# (b) status target: simulate the status step against the resolved sha.
TARGET_SHA="$SHA_A"
gh api "repos/o/r/statuses/${TARGET_SHA}" -f state=success -f context=ci-ok -f target_url=run-url >/dev/null
grep -q "statuses/$SHA_A" "$tmp/status-call"
if grep -q "statuses/$SHA_B" "$tmp/status-call"; then
  echo "status was posted on the dispatch sha, not the target" >&2; exit 1
fi

echo "harness checks ok"
