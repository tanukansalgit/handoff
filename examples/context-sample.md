# Session context (compact handoff)

_Captured `2026-05-21T14:32:07Z` · event `PreCompact` · trigger `auto`_

## Feature decisions

- **`scripts/session-handoff.sh` delegates to `session_handoff.py`** - keeps the shell script minimal; all logic in Python where it's testable _(rejected: putting logic directly in bash)_
- **`UserPromptSubmit` hook injects a pointer, not the full context** - avoids prepending 4000 chars to every prompt; Claude reads the file only when needed _(rejected: inline full context into additionalContext)_
- **Per-session context files (`context-<session_id>.md`)** - concurrent sessions on the same repo don't overwrite each other _(rejected: single `context.md` shared across sessions)_
- **`HANDOFF_EXTRA_INSTRUCTIONS` env var for custom extraction hints** - lets teams extend extraction without forking the prompt _(rejected: hardcoding domain-specific instructions in the base prompt)_

## Constraints

- Hook must always exit 0 - never break the host session even if extraction fails
- No new Python dependencies - stdlib only
- Context file written only on LLM success - no stale or partial files
- Marker deleted immediately after injection - subsequent prompts are clean

## Open loops

- Verify that `UserPromptSubmit` fires before the model call, not after (need to test with a slow LLM response)
- Check whether `CLAUDE_PROJECT_DIR` is reliably set in all Claude Code hook invocations or only in some
- Consider adding a `--dry-run` flag to `session-handoff.sh` for easier testing without a real compaction

## Signals

- `~/.claude/settings.json` - global hooks config location
- `subprocess.TimeoutExpired` - caught separately from `FileNotFoundError` in `_llm_extract`
- `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "..."}}` - exact JSON shape required by Claude Code for prompt injection
- `HANDOFF_LLM_TIMEOUT=90` - default; lower values cause timeouts on parallel auto-compactions
- `.ai-handoff/pending-<session_id>.marker` - sentinel file format
