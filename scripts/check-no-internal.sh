#!/usr/bin/env bash
# Guard: fail if any TRACKED file leaks confidential/internal content or references
# private working areas. Shinken is a PUBLIC, vendor-neutral open-source project.
#
# Public vendor PRODUCT facts (e.g. NVENC, NICE DCV, vGPU/MIG, GPU-TEE) are allowed
# and cited from public sources. This script deliberately contains NO private
# identifiers: specific names to block live in an untracked `scripts/deny-list.local`
# (one ERE pattern per line; '#' comments), which is git-ignored and never published.
#
# Usage:  scripts/check-no-internal.sh            scan the tracked tree
#         scripts/check-no-internal.sh --self-test  prove the guard fires on planted tokens
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
  for f in "${fixtures[@]}"; do
    printf '%s\n' "$f" | grep -qE "$re" || { echo "❌ self-test: guard MISSED: $f"; fail=1; }
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

hits="$(
  git ls-files |
    grep -vxF "scripts/check-no-internal.sh" |
    grep -vxF "$DENY_LOCAL" |
    tr '\n' '\0' |
    xargs -0 grep -InE "$re" 2>/dev/null || true
)"

if [[ -n "$hits" ]]; then
  echo "❌ guardrail violation — confidential/internal content or private-area references in tracked files:"
  echo "$hits"
  echo
  echo "This is a public repo. Move private material to the git-ignored internal/ dir and keep docs self-contained."
  exit 1
fi
echo "✅ no confidential/internal content or private-area references in tracked files"
