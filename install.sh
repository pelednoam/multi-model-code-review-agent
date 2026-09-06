#!/usr/bin/env bash
# Install the multi-model code review agent into a target project.
#
# Usage:
#   ./install.sh                    # install into the current directory
#   ./install.sh /path/to/project   # install into the specified directory
#
# Why this exists: Claude Code's auto-mode classifier blocks writes into
# `.claude/agents/` because that path permanently modifies how the project's
# Claude Code behaves on every future session. The classifier is right to
# block it -- but it means the agent itself cannot install itself. Run this
# script once, manually, and every future Claude Code session in the target
# project will pick up the agent automatically.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$PWD}"

if [ ! -d "$TARGET" ]; then
  echo "ERROR: target directory does not exist: $TARGET" >&2
  exit 1
fi

mkdir -p "$TARGET/.claude/agents" "$TARGET/scripts" "$TARGET/docs"

cp "$REPO_ROOT/docs/ensemble-review.md"                  "$TARGET/.claude/agents/"
cp "$REPO_ROOT/scripts/scrub_diff.py"                    "$TARGET/scripts/"
cp "$REPO_ROOT/scripts/review_preflight.py"              "$TARGET/scripts/"
cp "$REPO_ROOT/scripts/review_until_converged.py"        "$TARGET/scripts/"
cp "$REPO_ROOT/scripts/validate_review_results.py"       "$TARGET/scripts/"
cp "$REPO_ROOT/docs/ensemble_review_result_schema.json"  "$TARGET/docs/"

# Sub-packages: helpers split out of the two entry points so each file
# stays under 300 lines. Entry points re-export everything for back compat.
# Use rsync with --exclude when available so we never copy stale bytecode;
# fall back to cp -R + find-delete otherwise.
if command -v rsync >/dev/null 2>&1; then
  rsync -a --exclude __pycache__ "$REPO_ROOT/scripts/preflight/"   "$TARGET/scripts/preflight/"
  rsync -a --exclude __pycache__ "$REPO_ROOT/scripts/review_loop/" "$TARGET/scripts/review_loop/"
else
  cp -R "$REPO_ROOT/scripts/preflight"   "$TARGET/scripts/preflight"
  cp -R "$REPO_ROOT/scripts/review_loop" "$TARGET/scripts/review_loop"
  find "$TARGET/scripts/preflight" "$TARGET/scripts/review_loop" \
    -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
fi

cp "$REPO_ROOT/docs/codex-sandbox.md"                    "$TARGET/docs/" 2>/dev/null || true

# Codex is the one reviewer whose read-only-ness depends on the HOST rather than on a flag
# we pass, so check it at install time instead of letting a review silently run with a
# reviewer that can edit the code. Non-fatal: codex is optional, and the review simply drops
# that slot. See docs/codex-sandbox.md for both causes and their fixes.
if command -v codex >/dev/null 2>&1; then
  echo
  echo "Checking that the codex sandbox is genuinely read-only on this host..."
  _d=$(mktemp -d); echo original > "$_d/canary.txt"
  _ran=$(codex sandbox -- sh -c "echo SANDBOX_OK" 2>&1 || true)
  codex sandbox -- sh -c "echo changed > $_d/canary.txt" >/dev/null 2>&1 || true
  _after=$(cat "$_d/canary.txt" 2>/dev/null || echo "")
  if ! printf '%s' "$_ran" | grep -q SANDBOX_OK; then
    echo "  WARNING: codex's sandbox does not start on this host."
    echo "  A sandbox that fails to launch leaves a canary file untouched, which looks"
    echo "  exactly like a correctly refused write - so this cannot be ignored."
    case "$_ran" in
      *"uid map"*|*RTM_NEWADDR*|*userns*)
        echo "  Cause looks like blocked unprivileged user namespaces (Ubuntu 23.10+)."
        echo "  Fix: docs/codex-sandbox.md (a bwrap-scoped AppArmor profile)." ;;
      *) echo "  Detail: $(printf '%s' "$_ran" | tail -1)" ;;
    esac
    echo "  The codex reviewer slot will be skipped until this is fixed."
  elif [ "$_after" != "original" ]; then
    echo "  WARNING: codex's sandbox ALLOWED a write - it is not read-only here."
    echo "  Check approvals_reviewer in ~/.codex/config.toml: \"auto_review\" escalates"
    echo "  past -s read-only. See docs/codex-sandbox.md."
    echo "  The codex reviewer slot will be skipped until this is fixed."
  else
    echo "  OK: codex sandbox runs and refuses writes."
  fi
  rm -rf "$_d"
fi

cat <<EOF

Installed multi-model code review agent into:
  $TARGET

Files written:
  .claude/agents/ensemble-review.md
  scripts/scrub_diff.py
  scripts/review_preflight.py
  scripts/review_until_converged.py
  scripts/validate_review_results.py
  scripts/preflight/        (helpers for the preflight audit)
  scripts/review_loop/      (helpers for the convergence loop)
  docs/ensemble_review_result_schema.json
  docs/codex-sandbox.md      (why codex needs a host check, and how to fix it)

Next step: open Claude Code in that project and say one of:
  "review this"           -- quick mode (2 subagents, ~30s, free)
  "full review"           -- full ensemble (4 reviewers, ~3 min)
  "run until converged"   -- convergence loop with mandatory CI gate

If your project has project-specific signed manifests, edit
scripts/review_preflight.py to set SIGNED_MANIFESTS before first run.
EOF
