# Reference

## Claude Code vs Cursor

|                               | Claude Code | Cursor |
|-------------------------------|-------------|--------|
| PreCompact (extract + write)  | yes - auto + manual `/compact` | yes - preCompact only |
| PostCompact nudge             | yes | no |
| Auto-inject on next prompt    | yes - UserPromptSubmit | no - manual `@`-mention |

**Cursor** gets the context file written automatically but no auto-injection.
After compaction, include it manually: `@.ai-handoff/context-<session_id>.md`

---

## Output files

All written to `.ai-handoff/` in your project root. Gitignored automatically.

| File | Description |
|------|-------------|
| `context-<session_id>.md` | LLM-extracted context. Written only on success. ≤4000 chars. |
| `latest.md` | Raw payload mirror + next-step hint. Always written, even on failure. Useful as a manual fallback: `@.ai-handoff/latest.md` |
| `snapshots.jsonl` | Append-only audit log. One line per hook event. |
| `pending-<session_id>.marker` | One-shot sentinel. Created on success, deleted after injection. |

### snapshots.jsonl fields

PreCompact lines include: `ts`, `hook_event_name`, `trigger`, `session_id`,
`transcript_path`, `context_md_written`, `context_md_skip_reason` (on failure).

UserPromptSubmit lines include: `pointer_injected`, `marker_ts`, `marker_trigger`.

### Skip reasons

| Reason | Meaning |
|--------|---------|
| `no_claude_cli` | `claude` not found on PATH |
| `no_transcript` | No transcript path in hook payload and no custom instructions |
| `timeout` | CLI call exceeded `HANDOFF_LLM_TIMEOUT` seconds |
| `exit_<N>` | CLI exited with code N |
| `empty_stdout` | CLI returned nothing |
| `bad_envelope_json` | CLI output was not valid JSON |
| `bad_extraction_json` | LLM returned JSON that didn't parse |
| `extraction_not_dict` | LLM returned a non-object JSON value |

---

## Configuration

All defaults work out of the box. Export in your shell profile - read at hook runtime.

### LLM

| Variable | Default | Description |
|----------|---------|-------------|
| `HANDOFF_LLM_MODEL` | Claude default | Model passed to `claude -p`. E.g. `claude-haiku-4-5` for faster/cheaper runs. |
| `HANDOFF_LLM_TIMEOUT` | `90` | Kill timeout in seconds. Auto-compactions can fan the hook 3× in parallel - keep above ~60. |

### Extraction

| Variable | Default | Description |
|----------|---------|-------------|
| `HANDOFF_EXTRA_INSTRUCTIONS` | _(none)_ | Extra hints appended to the extraction prompt. |

```bash
export HANDOFF_EXTRA_INSTRUCTIONS="Also capture any REST or GraphQL endpoint paths mentioned."
export HANDOFF_EXTRA_INSTRUCTIONS="Include any JIRA ticket IDs or GitHub issue numbers in signals."
```

### Size budgets

| Variable | Default | Description |
|----------|---------|-------------|
| `HANDOFF_MAX_PROMPT_CHARS` | `12000` | Chars of transcript fed to the LLM. |
| `HANDOFF_CONTEXT_MD_MAX` | `4000` | Max chars in the output context file. |
| `HANDOFF_MAX_RECENT_TURNS` | `30` | Max conversation turns analysed. |
| `HANDOFF_TRANSCRIPT_TAIL_BYTES` | `1500000` | Bytes read from end of transcript. |
| `HANDOFF_TRANSCRIPT_TAIL_LINES` | `2500` | Max lines parsed from that tail. |

### Path

| Variable | Description |
|----------|-------------|
| `HANDOFF_ROOT` / `CLAUDE_PROJECT_DIR` | Override the project root where `.ai-handoff/` is written. |

### Audit log budgets

Rarely needed. Cap how much raw data lands in `snapshots.jsonl`.

| Variable | Default | Description |
|----------|---------|-------------|
| `HANDOFF_MAX_ASSISTANT` | `24000` | Max chars of last assistant message in snapshot. |
| `HANDOFF_MAX_SUMMARY` | `16000` | Max chars of compact summary in snapshot. |
| `HANDOFF_MAX_CUSTOM_INSTR` | `4000` | Max chars of custom instructions in snapshot. |
| `HANDOFF_MAX_JSONL_PAYLOAD` | `48000` | Max chars of raw payload excerpt in snapshot. |

---

## Multiple sessions on the same repo

Each session writes its own `context-<session_id>.md` and `pending-<session_id>.marker`.
Sessions never overwrite each other's files.

The UserPromptSubmit hook matches the marker by session ID only. If no match is
found, nothing is injected - picking up another session's context is worse than
injecting nothing.

## Why transcript tail, not full transcript

Transcripts grow large. Reading the full transcript for every compaction would be
slow and exceed the LLM's input budget. The tail captures the most recent
conversation, which is where current decisions and open loops live.
