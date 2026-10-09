#!/usr/bin/env bash
# =============================================================================
# install.sh — install this repo's tracked git hooks into the local .git/hooks.
# Git hooks are never cloned, so EVERY checkout must run this once to activate
# the owner's authorship + quality gates:
#
#     bash tools/hooks/install.sh
#
# CI enforces the same checks independently and cannot be bypassed.
# =============================================================================
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS_DIR="$(git rev-parse --git-common-dir)/hooks"
mkdir -p "$HOOKS_DIR"

for h in commit-msg pre-commit pre-push; do
  cp "$SRC/$h" "$HOOKS_DIR/$h"
  chmod +x "$HOOKS_DIR/$h"
  echo "installed: $HOOKS_DIR/$h"
done

echo "Git hooks installed — owner-alone authorship + lint/format/type/secret/test gates active."
