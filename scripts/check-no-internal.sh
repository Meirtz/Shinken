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
  '_canon'                           # the private design-canon working file
  '\]\([^)]*scratch/'                # markdown links into the git-ignored scratch/ area
  '\]\([^)]*internal/'               # markdown links into the git-ignored internal/ area
  'file://[^)" ]*/(scratch|internal)/'  # file:// links into private working areas
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
  fixture=$'leaked CONFIDENTIAL marker\nsee ](../scratch/_canon.md) and [x](internal/x.md)'
  if printf '%s\n' "$fixture" | grep -qE "$re"; then
    echo "✅ self-test: guard fires on planted tokens"
    exit 0
  fi
  echo "❌ self-test FAILED: guard did not fire on planted tokens"
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
