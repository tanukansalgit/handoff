#!/usr/bin/env bash
# handoff uninstaller
#
# Removes only the hooks this tool added from ~/.claude/settings.json.
# Leaves all other settings untouched. Safe to run even if the hooks
# were never installed.

set -euo pipefail

INSTALL_DIR="${HANDOFF_HOME:-$HOME/.handoff}"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓${NC}  $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC}  $*"; }
step() { echo -e "\n${BOLD}$*${NC}"; }

echo ""
echo -e "${BOLD}handoff uninstaller${NC}"
echo "────────────────────────────────────────────────"

step "1/2  Removing hooks from ~/.claude/settings.json"

if [ ! -f "$CLAUDE_SETTINGS" ]; then
    warn "Settings file not found at $CLAUDE_SETTINGS - nothing to remove."
else
    python3 - "$INSTALL_DIR" "$CLAUDE_SETTINGS" << 'PYTHON'
import json, sys
from pathlib import Path

install_dir   = Path(sys.argv[1]).expanduser().resolve()
settings_path = Path(sys.argv[2]).expanduser()
script        = str(install_dir / "scripts" / "session-handoff.sh")

try:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as e:
    print(f"       Could not read settings: {e}")
    sys.exit(0)

hooks = settings.get("hooks", {})
removed = []

for event in ("PreCompact", "PostCompact", "UserPromptSubmit"):
    original = hooks.get(event, [])
    filtered = [
        block for block in original
        if not any(
            h.get("command", "").startswith(f'"{script}"') or
            h.get("command", "").startswith(script)
            for h in block.get("hooks", [])
        )
    ]
    if len(filtered) < len(original):
        removed.append(event)
        if filtered:
            hooks[event] = filtered
        else:
            del hooks[event]

if not removed:
    print("       No handoff hooks found - settings unchanged.")
    sys.exit(0)

# Backup before writing
bak = settings_path.with_suffix(".json.bak")
settings_path.rename(bak)
settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"       Removed hooks for: {', '.join(removed)}")
print(f"       Backup saved to: {bak}")
PYTHON
    ok "Hooks removed"
fi

step "2/2  Install directory"

if [ -d "$INSTALL_DIR" ]; then
    echo "     Found install at $INSTALL_DIR"
    read -r -p "     Remove it? [y/N] " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        rm -rf "$INSTALL_DIR"
        ok "Removed $INSTALL_DIR"
    else
        warn "Skipped - directory kept at $INSTALL_DIR"
    fi
else
    warn "Install directory not found at $INSTALL_DIR - skipping."
fi

echo ""
echo "────────────────────────────────────────────────"
echo -e "  ${GREEN}✓${NC}  handoff hooks removed."
echo "     Your .ai-handoff/ directories in project roots are untouched."
echo "     Delete them manually if you no longer need the session files."
echo ""
