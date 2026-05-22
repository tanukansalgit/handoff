#!/usr/bin/env bash
# handoff entry point
# Resolves the repo root and delegates to session_handoff.py.
#
# Usage:
#   session-handoff.sh preCompact       (called by Claude Code PreCompact hook)
#   session-handoff.sh postCompact      (called by Claude Code PostCompact hook)
#   session-handoff.sh userPromptSubmit (called by Claude Code UserPromptSubmit hook)
#   session-handoff.sh status           (health check - prints CLI, env, .ai-handoff/ state)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export HANDOFF_ROOT="${CLAUDE_PROJECT_DIR:-${HANDOFF_ROOT:-$(pwd)}}"
exec python3 "$ROOT/scripts/session_handoff.py" "${1:-unknown}"
