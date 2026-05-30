#!/usr/bin/env bash
# Guard: fail if confidential / company-internal content leaks into TRACKED files.
#
# Shinken is a PUBLIC, vendor-neutral open-source project. Public vendor *product*
# facts (e.g. NVENC, NICE DCV, vGPU/MIG, GPU-TEE) are allowed and cited from public
# docs. Internal platform names, internal URLs, and confidential markers are NOT.
# Private design references live only in the git-ignored internal/ directory.
set -uo pipefail

# Targeted patterns (kept narrow to avoid false positives on common words):
patterns='nvidia\.atlassian\.net|console\.astra\.nvidia\.com|omnistation\.nvidia\.com|agent-security-readiness\.nvidia\.com|NVIDIA CONFIDENTIAL|DO NOT DISTRIBUTE|\binternal-tool\b|\binternal-tool\b'

self='scripts/check-no-internal.sh'
files=$(git ls-files | grep -vF "$self")
hits=$(printf '%s\n' "$files" | tr '\n' '\0' | xargs -0 grep -InE "$patterns" 2>/dev/null)

if [ -n "$hits" ]; then
  echo "❌ internal/confidential content detected in tracked files:"
  echo "$hits"
  echo
  echo "This is a public repo. Move internal references to the git-ignored internal/ dir."
  exit 1
fi
echo "✅ no internal/confidential markers in tracked files"
