#!/usr/bin/env bash
# Pre-commit hook: fail if any staged file contains an obvious API-key prefix.
# Patterns intentionally conservative — this catches accidental paste-ins, not
# every possible secret. Real defense is .gitignore + .env hygiene.
set -euo pipefail

PATTERNS=(
  'sk-or-v1-[A-Za-z0-9]{20,}'           # OpenRouter
  'sk-ant-api03-[A-Za-z0-9_-]{20,}'     # Anthropic
  'sk-proj-[A-Za-z0-9_-]{20,}'          # OpenAI project keys
  'AKIA[0-9A-Z]{16}'                    # AWS
  'ghp_[A-Za-z0-9]{30,}'                # GitHub personal-access tokens
  'glpat-[A-Za-z0-9_-]{20,}'            # GitLab
)

fail=0
for f in "$@"; do
  for pat in "${PATTERNS[@]}"; do
    if grep -E -q "$pat" "$f" 2>/dev/null; then
      echo "ERROR: $f contains a string matching '$pat'" >&2
      fail=1
    fi
  done
done

exit "$fail"
