#!/usr/bin/env bash
# Guard: fail if any TRACKED file leaks confidential/internal content, high-confidence
# credential signatures, or references private working areas. Shinken is a PUBLIC,
# vendor-neutral open-source project.
#
# Public vendor PRODUCT facts (e.g. NVENC, NICE DCV, vGPU/MIG, GPU-TEE) are allowed
# and cited from public sources. This script deliberately contains NO private
# identifiers: specific names to block live in an untracked `scripts/deny-list.local`
# (one ERE pattern per line; '#' comments), which is git-ignored and never published.
#
# Usage:  scripts/check-no-internal.sh                 scan the tracked tree
#         scripts/check-no-internal.sh --self-test     prove the guard fires on fixtures
#         scripts/check-no-internal.sh --history RANGE scan added lines in every RANGE commit
set -uo pipefail

# Generic, public-safe forbidden patterns (no private identifiers here):
PATTERNS=(
  'CONFIDENTIAL'                     # confidentiality markers
  'DO NOT DISTRIBUTE'
  '[A-Za-z0-9._-]+\.atlassian\.net'  # internal wiki links
  '_canon\b'                         # the private design-canon working file (anchored so it does NOT match `_canonical`)
  '\]\([^)]*scratch/'                # markdown links into the git-ignored scratch/ area
  '\]\([^)]*internal/'               # markdown links into the git-ignored internal/ area
  'file://'                          # ANY file:// link — leaks a local username/path and is a dead link for readers
  '/Users/[A-Za-z0-9._-]+/'          # a macOS home dir leaks the maintainer's local username
  'Owner:[^@]*@'                     # an email address in an `Owner:` line — use a role-based owner instead
)

# High-confidence credential formats. Keep this deliberately narrower than a generic
# `password = ...` expression: public examples and schemas legitimately contain credential
# *field names*, while the signatures below strongly indicate an actual credential. The
# repository guard complements (but does not replace) provider-side secret redaction.
SECRET_PATTERNS=(
  'A(KIA|SIA)[0-9A-Z]{16}'
  'AWS_SECRET_ACCESS_KEY[^A-Za-z0-9/+=]{0,16}[A-Za-z0-9/+=]{40}'
  'Authorization:[[:space:]]*Bearer[[:space:]]+[A-Za-z0-9._~+/-]{20,}=*'
  '-----BEGIN( [A-Z0-9]+)* PRIVATE KEY-----'
  'gh[pousr]_[A-Za-z0-9]{30,}'
  'github_pat_[A-Za-z0-9_]{50,}'
  'glpat-[A-Za-z0-9_-]{20,}'
  'sk-(proj-)?[A-Za-z0-9_-]{20,}'
  'xox[baprs]-[A-Za-z0-9-]{20,}'
  'AIza[0-9A-Za-z_-]{35}'
)
PATTERNS+=("${SECRET_PATTERNS[@]}")

DENY_LOCAL="scripts/deny-list.local"
if [[ -f "$DENY_LOCAL" ]]; then
  while IFS= read -r line; do
    [[ -z "${line// /}" || "$line" == \#* ]] && continue
    PATTERNS+=("$line")
  done < "$DENY_LOCAL"
fi

re="$(
  IFS='|'
  printf '%s' "${PATTERNS[*]}"
)"

if [[ "${1:-}" == "--self-test" ]]; then
  # Each planted line must trip at least one pattern (CONFIDENTIAL, scratch/, internal/,
  # file://, a macOS home dir, and an email in an Owner line).
  fixtures=(
    'leaked CONFIDENTIAL marker'
    'see ](../scratch/_canon.md) and [x](internal/x.md)'
    'a [link](file:///Users/somebody/secret.md)'
    'a bare /Users/somebody/path leak'
    'Owner: person@example.com'
  )
  fail=0
  for i in "${!fixtures[@]}"; do
    printf '%s\n' "${fixtures[$i]}" | grep -qE "$re" || {
      echo "❌ self-test: internal-content fixture $i was missed"
      fail=1
    }
  done
  # Split the source literals so third-party secret scanners do not mistake these inert
  # fixtures for live credentials; Bash concatenates adjacent quoted words at runtime.
  secret_fixtures=(
    'A''KIAIOSFODNN7EXAMPLE'
    'AWS_SECRET_ACCESS''_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'Authorization: Bearer ''this-is-a-fake-token-1234567890'
    '-----BEGIN ''PRIVATE KEY-----'
    'ghp_''abcdefghijklmnopqrstuvwxyz1234567890'
  )
  for i in "${!secret_fixtures[@]}"; do
    printf '%s\n' "${secret_fixtures[$i]}" | grep -qE "$re" || {
      echo "❌ self-test: credential fixture $i was missed"
      fail=1
    }
  done
  # And the anchored _canon pattern must NOT trip on the legitimate identifier `_canonical`.
  if printf '%s\n' 'name = _canonical_value' | grep -qE "$re"; then
    echo "❌ self-test: guard false-positives on '_canonical'"; fail=1
  fi
  if [[ "$fail" == 0 ]]; then
    echo "✅ self-test: guard fires on planted tokens and not on '_canonical'"
    exit 0
  fi
  exit 1
fi

if [[ "${1:-}" == "--history" ]]; then
  range="${2:-}"
  if [[ -z "$range" ]]; then
    echo "usage: $0 --history <git-range>" >&2
    exit 2
  fi
  if ! git rev-list "$range" >/dev/null 2>&1; then
    echo "❌ cannot resolve git range: $range" >&2
    exit 2
  fi
  history_fail=0
  while IFS= read -r commit; do
    # A force-add can bypass .gitignore. Reject any path that was added by the commit
    # and matches the CURRENT repository ignore policy, even if a later commit deleted
    # it again. Report only the commit id: an internal filename can itself be sensitive.
    ignored_path_added=0
    while IFS= read -r path; do
      if git check-ignore --no-index -q -- "$path"; then
        ignored_path_added=1
        break
      fi
    done < <(git diff-tree --root -m --no-commit-id --name-only --diff-filter=A -r "$commit")
    if [[ "$ignored_path_added" == 1 ]]; then
      echo "❌ ignored/private path force-added in commit $commit (path redacted)"
      history_fail=1
    fi
    # Inspect only lines ADDED by this commit. Exclude this guard because its self-test
    # intentionally contains inert credential fixtures. Never print a matching line:
    # CI logs must not become a second disclosure channel.
    if git show --format= --no-ext-diff --no-color --unified=0 "$commit" -- . \
      ':(exclude)scripts/check-no-internal.sh' \
      ':(exclude)scripts/deny-list.local' |
      grep '^+' | grep -v '^+++' | grep -qE "$re"; then
      echo "❌ guardrail violation in added lines of commit $commit (content redacted)"
      history_fail=1
    fi
  done < <(git rev-list --reverse "$range")
  if [[ "$history_fail" == 0 ]]; then
    echo "✅ no confidential/internal content or credential signatures added in $range"
    exit 0
  fi
  exit 1
fi

# `.gitignore` is not an access-control mechanism: `git add -f` bypasses it, and a file
# that was already tracked stays tracked. Make the ignore policy enforceable in CI by
# rejecting any tracked path that still matches it. The exact public Dockerfile.cua is
# explicitly unignored; private CUA/provider variants remain blocked.
ignored_tracked="$(git ls-files -ci --exclude-standard)"
if [[ -n "$ignored_tracked" ]]; then
  ignored_count="$(printf '%s\n' "$ignored_tracked" | wc -l | tr -d ' ')"
  echo "❌ $ignored_count tracked path(s) violate .gitignore policy (paths redacted)"
  echo "Run 'git ls-files -ci --exclude-standard' locally, remove them from the index, and purge any sensitive history."
  exit 1
fi

hits="$(
  git grep -nIE "$re" -- . \
    ':(exclude)scripts/check-no-internal.sh' \
    ':(exclude)scripts/deny-list.local' 2>/dev/null |
    sed -E 's/^([^:]+:[0-9]+):.*/\1:[REDACTED]/' || true
)"

if [[ -n "$hits" ]]; then
  echo "❌ guardrail violation — confidential/internal content, credential signature, or private-area reference in tracked files:"
  echo "$hits"
  echo
  echo "This is a public repo. Revoke real credentials, purge them from Git history, and move private material to a git-ignored directory."
  exit 1
fi
echo "✅ no confidential/internal content, credential signatures, or private-area references in tracked files"
