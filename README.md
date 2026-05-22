<div align="center">

# handoff

**AI session memory that survives context compaction.**

Claude Code compacts your context window and drops the engineering layer -  
which approach you chose, what you rejected, the error you were debugging, what's still open.  
handoff hooks into that event and bridges it into your next prompt. Automatically.

[![CI](https://github.com/YOUR_GITHUB_USERNAME/handoff/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_GITHUB_USERNAME/handoff/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![Claude Code](https://img.shields.io/badge/Claude_Code-hooks-orange.svg)](https://claude.ai/code)
[![Cursor](https://img.shields.io/badge/Cursor-compatible-purple.svg)](https://cursor.sh)

</div>

---

## The gap Claude's compaction leaves

When the context window fills up, Claude writes a summary that looks roughly like this:

> *"We've been building a session handoff tool for Claude Code. The tool uses three hooks: PreCompact, PostCompact, and UserPromptSubmit. We implemented the Python script and the bash wrapper. The installer wires global hooks into `~/.claude/settings.json`. We discussed handling multiple concurrent sessions using per-session files."*

This is a **narrative recap** - optimized to answer *"what did this conversation cover?"*

It is not optimized to answer *"where exactly did we stop, and what do I need to know to pick up right now?"*

| | Claude auto-summarization | handoff |
|---|---|---|
| **Purpose** | What did this conversation cover? | Where exactly did we stop? |
| **Decisions** | Mentioned in passing, generalized | Explicit - choice, reason, what was rejected |
| **Current state** | Implied, often compressed | Specific file, function, next action |
| **Error messages** | Paraphrased or dropped entirely | Verbatim, quoted exactly |
| **Rejected approaches** | Almost never included | First-class field on every decision |
| **Open questions** | Rarely surfaced | Dedicated section, never dropped |

The right column is what you actually need to recover a session fast. Without it, you spend the first 10 minutes of the next session re-establishing context you already established.

---

## Install

> **Prerequisites:** [`claude` CLI](https://claude.ai/code) · Python 3.8+ · Git

> [!NOTE]
> The PreCompact hook runs an LLM call synchronously before compaction fires.
> On a fast connection this takes **5–15 seconds**. The default timeout is 90 seconds.
> If the call fails for any reason the hook exits cleanly and compaction continues normally.
> To reduce the timeout: `export HANDOFF_LLM_TIMEOUT=30`

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/handoff.git
cd handoff
bash install.sh
```

Clone it wherever you keep your projects. The installer copies the runtime scripts
to `~/.handoff` and wires three global hooks into `~/.claude/settings.json`.
Works across every project automatically - no per-repo setup.

**Verify:**
```bash
# Inside Claude Code, trigger a manual compaction:
/compact
# You'll see PreCompact and PostCompact hooks fire in the transcript.
# Your next message will silently include the bridged context.
```

**Check status anytime:**
```bash
~/.handoff/scripts/session-handoff.sh status
```

**Uninstall:**
```bash
bash ~/.handoff/uninstall.sh
```

**Run tests (from your cloned repo):**
```bash
python3 -m pytest tests/ -v
```

---

## How it works

Three hooks fire around every compaction event:

```
┌─────────────────────────────────────────────────────────┐
│                   context fills up                       │
└────────────────────────┬────────────────────────────────┘
                         │
              ┌──────────▼──────────┐
              │   PreCompact hook   │
              │                     │
              │  1. read transcript │
              │  2. claude -p       │  ← LLM extracts 4 sections
              │  3. write context   │
              │  4. drop marker     │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │   compaction runs   │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │  PostCompact hook   │  ← stderr nudge: "handoff ready"
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────────────┐
              │  UserPromptSubmit hook      │
              │                             │
              │  marker exists?             │
              │    yes → inject pointer     │  ← one-shot, then marker deleted
              │    no  → no-op              │
              └──────────┬──────────────────┘
                         │
              ┌──────────▼──────────┐
              │  Claude reads file  │  ← grounded session, no re-asking
              └─────────────────────┘
```

The injection is **one-shot**: fires once on the first prompt after compaction,
then the marker is deleted. Subsequent prompts are clean.

---

## What gets extracted

Four fixed sections, extracted from the transcript tail right before compaction fires:

**Feature decisions** - implementation choices tied to a concrete artifact.
File path, function name, hook event, command, library. Includes what was
rejected and why. Generic conversational preferences are excluded.

**Constraints** - hard rules shaping the work. Things like `"no new dependencies"`,
`"must be idempotent"`, `"Cursor only, not Stop hook"`.

**Open loops** - unresolved questions and pending TODOs the next session must revisit.

**Signals** - verbatim: error messages, file paths, env var names, version numbers,
commands that matter. Quoted exactly, never paraphrased.

### Example output

Here is what a real extracted file looks like:

```markdown
# Session context (compact handoff)

_Captured 2026-05-21T14:32:07Z · event PreCompact · trigger auto_

## Feature decisions

- **`UserPromptSubmit` injects a pointer, not full context** - avoids prepending
  4000 chars to every prompt; Claude reads the file only when needed
  _(rejected: inline full context into additionalContext)_
- **per-session files `context-<session_id>.md`** - concurrent sessions on the
  same repo never overwrite each other _(rejected: single shared context.md)_

## Constraints

- Hook must always exit 0 - never break the host session even if extraction fails
- No new Python dependencies - stdlib only

## Open loops

- Verify UserPromptSubmit fires before the model call, not after
- Check whether CLAUDE_PROJECT_DIR is reliably set in all hook invocations

## Signals

- `~/.claude/settings.json` - global hooks config location
- `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "..."}}` - exact JSON shape for prompt injection
- `HANDOFF_LLM_TIMEOUT=90` - lower values cause timeouts on parallel auto-compactions
```

Full example: [`examples/context-sample.md`](examples/context-sample.md)

---

## Cursor support

Run the same installer. You get PreCompact extraction automatically, but
no auto-injection - Cursor doesn't expose a `UserPromptSubmit` hook.

After compaction, include the context file manually in your next message:

```
@.ai-handoff/context-<session_id>.md
```

The file is always written. You just add one `@`-mention.

---

## Output files

All written to `.ai-handoff/` in your project root. Gitignored automatically.

| File | When written | Description |
|------|-------------|-------------|
| `context-<session_id>.md` | PreCompact (success only) | LLM-extracted context. ≤4000 chars. |
| `latest.md` | PreCompact (always) | Raw payload mirror + next-step hint. Fallback if extraction fails. |
| `snapshots.jsonl` | Every hook event | Append-only audit log with skip reasons on failure. |
| `pending-<session_id>.marker` | After successful extraction | One-shot sentinel, deleted after injection. |

---

## Configuration

All defaults work out of the box.

| Variable | Default | Description |
|----------|---------|-------------|
| `HANDOFF_LLM_MODEL` | Claude default | Model for extraction. E.g. `claude-haiku-4-5` for faster/cheaper runs. |
| `HANDOFF_LLM_TIMEOUT` | `90` | Kill timeout in seconds. Keep above 60 - parallel auto-compactions fan the hook 3×. |
| `HANDOFF_EXTRA_INSTRUCTIONS` | _(none)_ | Extra extraction hints appended to the prompt. See below. |
| `HANDOFF_MAX_PROMPT_CHARS` | `12000` | Transcript chars fed to the LLM. |
| `HANDOFF_CONTEXT_MD_MAX` | `4000` | Max output chars. |
| `HANDOFF_MAX_RECENT_TURNS` | `30` | Max conversation turns analysed. |

Full reference including size budgets and audit log tunables: [`docs/reference.md`](docs/reference.md)

**`HANDOFF_EXTRA_INSTRUCTIONS`** lets you extend extraction without forking:

```bash
# Capture API endpoints
export HANDOFF_EXTRA_INSTRUCTIONS="Also capture any REST or GraphQL endpoint paths in signals."

# Capture ticket references
export HANDOFF_EXTRA_INSTRUCTIONS="Include any JIRA or GitHub issue numbers in signals."
```

Full reference: [`docs/reference.md`](docs/reference.md)

---

## Failure handling

If `claude` is missing, times out, or returns bad output:

- Context file is **skipped** - no stale file written
- Skip reason logged to `snapshots.jsonl`
- No marker dropped → nothing injected on next prompt
- `latest.md` always written as a raw fallback

**The hook always exits 0. It never breaks your session.**

---

## Reference

Full reference - all env vars, output file formats, skip reasons, multi-session behaviour: [`docs/reference.md`](docs/reference.md)

---

<div align="center">

Built as part of the [Filling the Gaps](https://github.com/YOUR_GITHUB_USERNAME) series -  
tools that fix the gaps in AI-assisted engineering.

</div>
