#!/usr/bin/env bash
# handoff installer
#
# Two modes:
#   LOCAL  - run from inside a cloned repo: bash install.sh
#   REMOTE - pipe from GitHub:  curl -sSL <raw-url>/install.sh | bash
#
# Safe to re-run - idempotent, never duplicates hooks, backs up settings.json.

set -euo pipefail

REPO_URL="${CONTEXT_BRIDGE_REPO:-https://github.com/YOUR_GITHUB_USERNAME/handoff.git}"
INSTALL_DIR="${HANDOFF_HOME:-$HOME/.handoff}"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

# ── Colours ─────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC}  $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC}  $*"; }
err()  { echo -e "${RED}  ✗${NC}  $*" >&2; }
step() { echo -e "\n${BOLD}$*${NC}"; }

echo ""
echo -e "${BOLD}handoff${NC}  -  AI session memory across compaction"
echo "────────────────────────────────────────────────"

# ── Detect local vs remote install ──────────────────────────────────────────
#
# If this script is running from inside a repo that already has scripts/,
# just copy from here instead of cloning. This is the normal flow after
# `git clone … && bash install.sh`.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"
LOCAL_MODE=false
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/scripts/session-handoff.sh" ]; then
    LOCAL_MODE=true
fi

# ── Step 1: Install files ────────────────────────────────────────────────────
step "1/4  Files"

if [ "$LOCAL_MODE" = true ]; then
    echo "     Installing from local clone: $SCRIPT_DIR"
    if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
        mkdir -p "$INSTALL_DIR"
        cp -r "$SCRIPT_DIR/scripts" "$INSTALL_DIR/"
        # Copy optional extras if present
        [ -f "$SCRIPT_DIR/README.md" ]      && cp "$SCRIPT_DIR/README.md"      "$INSTALL_DIR/"
        [ -f "$SCRIPT_DIR/uninstall.sh" ]   && cp "$SCRIPT_DIR/uninstall.sh"   "$INSTALL_DIR/"
        [ -d "$SCRIPT_DIR/docs" ]           && cp -r "$SCRIPT_DIR/docs"        "$INSTALL_DIR/"
        [ -d "$SCRIPT_DIR/examples" ]       && cp -r "$SCRIPT_DIR/examples"    "$INSTALL_DIR/"
    fi
    ok "Installed from local clone"
elif [ -d "$INSTALL_DIR/.git" ]; then
    echo "     Found existing install at $INSTALL_DIR - updating"
    git -C "$INSTALL_DIR" pull --ff-only --quiet
    ok "Updated to latest"
else
    echo "     Cloning to $INSTALL_DIR …"
    if ! git clone --quiet "$REPO_URL" "$INSTALL_DIR" 2>/dev/null; then
        err "git clone failed. Set CONTEXT_BRIDGE_REPO to your fork URL, or clone manually first."
        echo "     Example:  git clone $REPO_URL ~/.handoff && bash ~/.handoff/install.sh"
        exit 1
    fi
    ok "Cloned"
fi

chmod +x "$INSTALL_DIR/scripts/session-handoff.sh"
ok "Script is executable"

# ── Step 2: Python 3 ────────────────────────────────────────────────────────
step "2/4  Python 3"

if command -v python3 &>/dev/null; then
    PY_VER=$(python3 -c "import sys; print('%d.%d' % sys.version_info[:2])")
    ok "python3 found ($PY_VER)"
else
    err "python3 not found. Install Python 3.8+ and re-run."
    exit 1
fi

# ── Step 3: claude CLI ───────────────────────────────────────────────────────
step "3/4  claude CLI"

if command -v claude &>/dev/null; then
    ok "claude CLI found ($(command -v claude))"
else
    warn "claude CLI not found on PATH"
    echo "       LLM extraction will be skipped until you install it:"
    echo "       https://claude.ai/code"
    echo "       latest.md will still be written as a raw fallback."
fi

# ── Step 4: Claude Code global hooks ────────────────────────────────────────
step "4/4  Claude Code global hooks  (~/.claude/settings.json)"

python3 - "$INSTALL_DIR" "$CLAUDE_SETTINGS" << 'PYTHON'
import json, sys
from pathlib import Path

install_dir   = Path(sys.argv[1]).expanduser().resolve()
settings_path = Path(sys.argv[2]).expanduser()
script        = str(install_dir / "scripts" / "session-handoff.sh")

def cmd(event_arg):
    return f'"{script}" {event_arg}'

our_hooks = {
    "PreCompact": [
        {"matcher": "auto",   "hooks": [{"type": "command", "command": cmd("preCompact")}]},
        {"matcher": "manual", "hooks": [{"type": "command", "command": cmd("preCompact")}]},
    ],
    "PostCompact": [
        {"matcher": "auto",   "hooks": [{"type": "command", "command": cmd("postCompact")}]},
        {"matcher": "manual", "hooks": [{"type": "command", "command": cmd("postCompact")}]},
    ],
    "UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": cmd("userPromptSubmit")}]},
    ],
}

settings_path.parent.mkdir(parents=True, exist_ok=True)
if settings_path.is_file():
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("       ⚠  Could not parse existing settings - backing up and starting fresh")
        settings_path.rename(settings_path.with_suffix(".json.bak"))
        settings = {}
else:
    settings = {}

hooks   = settings.setdefault("hooks", {})
added   = []
skipped = []

for event, entries in our_hooks.items():
    existing     = hooks.get(event, [])
    existing_cmds = {
        h.get("command", "")
        for block in existing
        for h in block.get("hooks", [])
    }
    our_cmd = entries[0]["hooks"][0]["command"]
    if our_cmd in existing_cmds:
        skipped.append(event)
    else:
        hooks[event] = existing + entries
        added.append(event)

if settings_path.is_file():
    settings_path.rename(settings_path.with_suffix(".json.bak"))

settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")

if added:
    print(f"       Added:   {', '.join(added)}")
if skipped:
    print(f"       Already configured: {', '.join(skipped)}")
print(f"       Settings: {settings_path}")
PYTHON

ok "Claude Code hooks configured"

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────────"
echo -e "  ${GREEN}✓${NC}  Installed to   ${BOLD}$INSTALL_DIR${NC}"
echo -e "  ${GREEN}✓${NC}  Hooks wired in ${BOLD}~/.claude/settings.json${NC}"
echo ""
echo "  Hooks fire automatically on every project - no per-repo setup needed."
echo ""
echo "  Verify:  open Claude Code, run /compact"
echo "           PreCompact and PostCompact hooks should appear in the transcript."
echo "           Your next message will include the bridged context automatically."
echo ""
echo -e "  ${BOLD}Cursor (one step per workspace):${NC}"
echo "  Add to .cursor/settings.json in your project:"
echo ""
echo "    {"
echo "      \"hooks\": {"
echo "        \"preCompact\": {"
echo "          \"command\": \"\\\"$INSTALL_DIR/scripts/session-handoff.sh\\\" preCompact\""
echo "        }"
echo "      }"
echo "    }"
echo ""
echo "  Note: Cursor exposes only PreCompact - extraction runs automatically,"
echo "  but there is no auto-injection. After compaction, include the context"
echo "  file manually in your next message:  @.ai-handoff/context-<session>.md"
echo ""
echo -e "  ${BOLD}Check status anytime:${NC}"
echo "    $INSTALL_DIR/scripts/session-handoff.sh status"
echo ""
echo -e "  To update later: re-run this script."
echo ""
