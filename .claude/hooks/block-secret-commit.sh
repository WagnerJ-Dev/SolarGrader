#!/usr/bin/env bash
# PreToolUse(Bash) guard: block `git commit` when about-to-be-committed changes
# contain hardcoded secrets. Enforces CLAUDE.md principle #2 (env vars only).
#
# Scans `git diff HEAD` (staged + unstaged tracked changes — covers both a normal
# staged commit and `git commit -a`). Only inspects ADDED lines. Exit 2 blocks the
# tool call and surfaces stderr back to Claude. Exit 0 allows the commit.
set -uo pipefail

# Diff of everything tracked vs the last commit; exclude the secrets template,
# which intentionally lists key NAMES with empty values.
diff=$(git diff HEAD --no-color -- . ':(exclude).env.example' 2>/dev/null) || exit 0

# Added lines only (drop the +++ file headers).
added=$(printf '%s\n' "$diff" | grep '^+' | grep -v '^+++') || true
[ -z "$added" ] && exit 0

# secret-name = "literal-8+chars"  |  AWS key id  |  PEM private-key header
patterns='(api[_-]?key|secret|passwd|password|token|access[_-]?key|client[_-]?secret|auth_token)["'"'"' ]*[:=][[:space:]]*["'"'"'][^"'"'"' ]{8,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'

hits=$(printf '%s\n' "$added" | grep -niE "$patterns") || true

if [ -n "$hits" ]; then
  {
    echo "COMMIT BLOCKED — staged changes look like they contain a hardcoded secret."
    echo "Per CLAUDE.md principle #2, secrets must come from env vars / .env, never inline."
    echo "Offending added lines:"
    printf '%s\n' "$hits"
    echo "If this is a false positive, the user can commit manually or adjust the hook."
  } >&2
  exit 2
fi
exit 0
