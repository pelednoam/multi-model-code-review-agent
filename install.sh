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

cat <<EOF

Installed multi-model code review agent into:
  $TARGET

Files written:
  .claude/agents/ensemble-review.md
  scripts/scrub_diff.py
  scripts/review_preflight.py
  scripts/review_until_converged.py
  scripts/validate_review_results.py
  docs/ensemble_review_result_schema.json

Next step: open Claude Code in that project and say one of:
  "review this"           -- quick mode (2 subagents, ~30s, free)
  "full review"           -- full ensemble (4 reviewers, ~3 min)
  "run until converged"   -- convergence loop with mandatory CI gate

If your project has project-specific signed manifests, edit
scripts/review_preflight.py to set SIGNED_MANIFESTS before first run.
EOF
