#!/usr/bin/env python3
"""
handoff: session handoff helper for Claude Code / Cursor.

Modes (argv[1]):
  preCompact       Extract session context before compaction fires.
  postCompact      Emit a stderr nudge when the handoff is ready.
  userPromptSubmit Inject a pointer to the context file on the next prompt.
  status           Print a health check: CLI, env overrides, .ai-handoff/ state.

All env vars and output file formats are documented in docs/.
The hook always exits 0 - it never breaks the host session.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

__version__ = "0.3.1"


# ── Configuration ────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Config:
    """All runtime configuration in one place.

    Constructed once at startup via Config.from_env() and passed to every
    function that needs it. Tests pass Config(...) directly - no global mutation.
    """
    # Size budgets
    last_msg_max_chars: int = 24_000
    summary_max_chars: int = 16_000
    jsonl_payload_max: int = 48_000
    context_md_max: int = 4_000
    prompt_input_max_chars: int = 12_000
    custom_instructions_max: int = 4_000
    recent_turns_max: int = 30
    transcript_tail_bytes: int = 1_500_000
    transcript_tail_lines: int = 2_500

    # LLM
    llm_timeout_s: int = 90
    llm_model: str = ""
    extra_instructions: str = ""

    @classmethod
    def env_var_names(cls) -> tuple[str, ...]:
        """All env vars this config reads. Single source of truth for status display."""
        return (
            "HANDOFF_LLM_MODEL",
            "HANDOFF_LLM_TIMEOUT",
            "HANDOFF_EXTRA_INSTRUCTIONS",
            "HANDOFF_MAX_PROMPT_CHARS",
            "HANDOFF_CONTEXT_MD_MAX",
            "HANDOFF_MAX_RECENT_TURNS",
            "HANDOFF_TRANSCRIPT_TAIL_BYTES",
            "HANDOFF_TRANSCRIPT_TAIL_LINES",
            "HANDOFF_MAX_ASSISTANT",
            "HANDOFF_MAX_SUMMARY",
            "HANDOFF_MAX_CUSTOM_INSTR",
            "HANDOFF_MAX_JSONL_PAYLOAD",
        )

    @classmethod
    def from_env(cls) -> Config:
        def _int(key: str, default: int) -> int:
            try:
                return int(os.environ.get(key, str(default)))
            except ValueError:
                return default

        return cls(
            last_msg_max_chars      = _int("HANDOFF_MAX_ASSISTANT",         24_000),
            summary_max_chars       = _int("HANDOFF_MAX_SUMMARY",           16_000),
            jsonl_payload_max       = _int("HANDOFF_MAX_JSONL_PAYLOAD",     48_000),
            context_md_max          = _int("HANDOFF_CONTEXT_MD_MAX",         4_000),
            prompt_input_max_chars  = _int("HANDOFF_MAX_PROMPT_CHARS",      12_000),
            custom_instructions_max = _int("HANDOFF_MAX_CUSTOM_INSTR",       4_000),
            recent_turns_max        = _int("HANDOFF_MAX_RECENT_TURNS",           30),
            transcript_tail_bytes   = _int("HANDOFF_TRANSCRIPT_TAIL_BYTES", 1_500_000),
            transcript_tail_lines   = _int("HANDOFF_TRANSCRIPT_TAIL_LINES",  2_500),
            llm_timeout_s           = _int("HANDOFF_LLM_TIMEOUT",               90),
            llm_model               = os.environ.get("HANDOFF_LLM_MODEL",   "").strip(),
            extra_instructions      = os.environ.get("HANDOFF_EXTRA_INSTRUCTIONS", "").strip(),
        )


# ── Result types ─────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class ExtractionResult:
    """Outcome of one LLM extraction attempt.

    On success: data is populated, skip_reason is None.
    On failure: data is None, skip_reason is a short machine-readable token,
                cli_stderr may contain the raw error output from the claude CLI.
    """
    data: dict | None
    skip_reason: str | None
    cli_stderr: str | None = None

    @property
    def ok(self) -> bool:
        return self.data is not None


# ── Path helpers ─────────────────────────────────────────────────────────────

def _repo_root() -> Path:
    env = os.environ.get("HANDOFF_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def _ensure_out_dir(root: Path) -> Path:
    """Return (and create if needed) the .ai-handoff/ directory under root."""
    d = root / ".ai-handoff"
    d.mkdir(parents=True, exist_ok=True)
    return d


_SAFE_SID   = re.compile(r"[^A-Za-z0-9._-]")
_MULTI_DOT  = re.compile(r"\.{2,}")


def _sanitize_session_id(session_id: str) -> str:
    """Return a filename-safe version of a session ID.

    Replaces characters outside [A-Za-z0-9._-] with underscore, then
    collapses consecutive dots to prevent path traversal (.. → _).
    """
    safe = _SAFE_SID.sub("_", session_id)
    safe = _MULTI_DOT.sub("_", safe)
    return safe[:120] or "default"


def _context_md_path(out_dir: Path, session_id: str | None) -> Path:
    if session_id:
        return out_dir / f"context-{_sanitize_session_id(session_id)}.md"
    return out_dir / "context.md"


def _marker_path(out_dir: Path, session_id: str | None) -> Path:
    if session_id:
        return out_dir / f"pending-{_sanitize_session_id(session_id)}.marker"
    return out_dir / "pending.marker"


# ── Hook payload parsing ──────────────────────────────────────────────────────

def _read_stdin_json() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_stdin_parse_error": True, "_raw_snippet": raw[:8000]}


def _hook_label(data: dict, argv_event: str) -> str:
    return str(
        data.get("hook_event_name")
        or data.get("hookEventName")
        or data.get("event")
        or argv_event
    )


def _pick_message(data: dict) -> str:
    for key in ("last_assistant_message", "lastAssistantMessage", "assistant_message", "response"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _session_id(data: dict) -> str | None:
    sid = data.get("session_id") or data.get("sessionId")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    return None


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Transcript reading ────────────────────────────────────────────────────────

def _flatten_message_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                t = block.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(t.strip())
            elif bt == "tool_use":
                name = block.get("name") or "tool"
                parts.append(f"[tool:{name}]")
        return "\n".join(parts).strip()
    return ""


def _record_text(rec: dict) -> tuple[str, str]:
    rtype = rec.get("type")
    msg = rec.get("message")
    if not isinstance(msg, dict):
        if rtype == "summary" and isinstance(rec.get("summary"), str):
            return "summary", rec["summary"].strip()
        return "", ""
    role = str(msg.get("role") or rtype or "")
    text = _flatten_message_content(msg.get("content"))
    return role, text


def _read_transcript_tail(path: Path, config: Config) -> list[dict]:
    try:
        size = path.stat().st_size
    except OSError:
        return []
    try:
        with path.open("rb") as f:
            start = max(0, size - config.transcript_tail_bytes)
            f.seek(start)
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    out: list[dict] = []
    for line in lines[-config.transcript_tail_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _load_records(data: dict, config: Config) -> list[dict]:
    """Extract conversation records from the transcript path in hook data."""
    tp_raw = data.get("transcript_path") or data.get("transcriptPath")
    if not isinstance(tp_raw, str) or not tp_raw.strip():
        return []
    tpath = Path(tp_raw).expanduser()
    if not tpath.is_file():
        return []
    return _read_transcript_tail(tpath, config)


# ── Extraction prompt ─────────────────────────────────────────────────────────

_BASE_EXTRACTION_INSTRUCTIONS = """\
You are extracting durable session context from a transcript tail for a
handoff after context compaction. Return ONLY a JSON object, no prose, no
markdown fences.

Schema:
{
  "feature_decisions": [{"choice": str, "reason": str, "rejected": str | null}],
  "constraints": [str],
  "open_loops": [str],
  "signals": [str]
}

Extraction rules:
- feature_decisions: a decision that shapes the FEATURE or IMPLEMENTATION.
  "choice" MUST reference a concrete artifact: file path, function name,
  hook event name, command, library, endpoint, env var, or a named option
  the assistant offered. Exclude generic style preferences ("be concise",
  "nice job"). Include "reason" if stated; set "rejected" to the alternative
  that was turned down, or null.
- constraints: hard rules shaping the work that are not a single
  implementation pick. E.g. "no new dependencies", "keep it idempotent",
  "must work without the claude CLI".
- open_loops: unresolved questions, pending TODOs, unverified hypotheses the
  next session must revisit.
- signals: verbatim - error messages, file paths, commands, version numbers,
  env var names, URLs. Quote them exactly; do not paraphrase.

Quality bar:
- Only include items with clear transcript evidence. Do not speculate.
- Use an empty list [] for any section with nothing relevant.
- Keep each string under 200 characters.
"""


def _build_extraction_instructions(config: Config) -> str:
    if not config.extra_instructions:
        return _BASE_EXTRACTION_INSTRUCTIONS
    return (
        _BASE_EXTRACTION_INSTRUCTIONS
        + f"\nAdditional instructions from HANDOFF_EXTRA_INSTRUCTIONS:\n{config.extra_instructions}\n"
    )


def _assemble_prompt(records: list[dict], custom_instructions: str, config: Config) -> str:
    turns: list[str] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        role, text = _record_text(rec)
        if not text or role not in ("user", "assistant"):
            continue
        turns.append(f"{role.upper()}: {text}")

    body = "\n\n".join(turns[-config.recent_turns_max:])
    if len(body) > config.prompt_input_max_chars:
        body = body[-config.prompt_input_max_chars:]

    parts = [_build_extraction_instructions(config)]
    if custom_instructions.strip():
        parts.append(f"\nUser's /compact instructions:\n{custom_instructions[:800]}\n")
    parts.append("\n--- TRANSCRIPT TAIL ---\n")
    parts.append(body)
    parts.append("\n--- END ---\n")
    return "".join(parts)


# ── Claude CLI call ───────────────────────────────────────────────────────────

def _strip_json_fences(s: str) -> str:
    s = s.strip()
    if not s.startswith("```"):
        return s
    parts = s.split("```")
    if len(parts) < 3:
        return s
    inner = parts[1]
    if inner.startswith("json\n"):
        inner = inner[len("json\n"):]
    elif inner.startswith("json"):
        inner = inner[len("json"):].lstrip()
    return inner.strip()


def _extract_result_text(envelope: object) -> str | None:
    if not isinstance(envelope, dict):
        return None
    result = envelope.get("result")
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        chunks: list[str] = []
        for block in result:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        if chunks:
            return "".join(chunks)
    return None


def _llm_extract(prompt_text: str, config: Config) -> ExtractionResult:
    """Call claude -p and parse the structured extraction.

    Always returns an ExtractionResult - never raises. On failure, data is None
    and skip_reason is a short machine-readable token. cli_stderr is populated
    whenever the CLI process ran and produced stderr output.
    """
    def _fail(reason: str, stderr: str | None = None) -> ExtractionResult:
        return ExtractionResult(data=None, skip_reason=reason, cli_stderr=stderr)

    claude_bin = shutil.which("claude")
    if not claude_bin:
        return _fail("no_claude_cli")

    cmd = [claude_bin, "-p", prompt_text, "--output-format", "json"]
    if config.llm_model:
        cmd += ["--model", config.llm_model]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=config.llm_timeout_s)
    except subprocess.TimeoutExpired:
        return _fail("timeout")
    except FileNotFoundError:
        return _fail("no_claude_cli")
    except Exception as exc:  # noqa: BLE001
        return _fail(f"exception:{type(exc).__name__}")

    stderr_snippet = (proc.stderr or "").strip()[:2000] or None

    if proc.returncode != 0:
        return _fail(f"exit_{proc.returncode}", stderr=stderr_snippet)

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return _fail("empty_stdout", stderr=stderr_snippet)

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return _fail("bad_envelope_json", stderr=stderr_snippet)

    model_text = _extract_result_text(envelope)
    if not isinstance(model_text, str) or not model_text.strip():
        return _fail("no_result_field", stderr=stderr_snippet)

    cleaned = _strip_json_fences(model_text)
    try:
        extraction = json.loads(cleaned)
    except json.JSONDecodeError:
        return _fail("bad_extraction_json", stderr=stderr_snippet)

    if not isinstance(extraction, dict):
        return _fail("extraction_not_dict", stderr=stderr_snippet)

    return ExtractionResult(data=extraction, skip_reason=None)


def _run_extraction(
    records: list[dict],
    custom_instructions: str,
    config: Config,
) -> ExtractionResult:
    """Assemble the prompt and call the LLM. Returns a failed result if there
    is nothing to extract."""
    if not records and not custom_instructions.strip():
        return ExtractionResult(data=None, skip_reason="no_transcript")
    prompt = _assemble_prompt(records, custom_instructions, config)
    return _llm_extract(prompt, config)


# ── Rendering ─────────────────────────────────────────────────────────────────

def _decision_line(d: object) -> str:
    if isinstance(d, str):
        return d.strip()
    if not isinstance(d, dict):
        return ""
    choice = str(d.get("choice") or "").strip()
    if not choice:
        return ""
    reason = str(d.get("reason") or "").strip()
    rejected = d.get("rejected")
    line = f"**{choice}**"
    if reason:
        line += f" - {reason}"
    if isinstance(rejected, str) and rejected.strip():
        line += f" _(rejected: {rejected.strip()})_"
    return line


def _plain_line(x: object) -> str:
    return x.strip() if isinstance(x, str) else ""


def _bullets(items: object, formatter: Callable[[object], str]) -> str:
    if not isinstance(items, list) or not items:
        return "- _(none)_\n"
    lines = [f"- {formatter(item)}" for item in items if formatter(item)]
    return ("\n".join(lines) + "\n") if lines else "- _(none)_\n"


# Section definitions: (heading, extractor_key, formatter)
# Order determines both render order and drop-priority (last = dropped first).
_SECTIONS: list[tuple[str, str, Callable[[object], str]]] = [
    ("## Feature decisions\n\n",  "feature_decisions", _decision_line),
    ("\n## Constraints\n\n",       "constraints",       _plain_line),
    ("\n## Open loops\n\n",        "open_loops",        _plain_line),
    ("\n## Signals\n\n",           "signals",           _plain_line),
]


def _render_context_md(
    extraction: dict,
    *,
    ts: str,
    hook_label: str,
    trigger: object,
    config: Config,
) -> str:
    """Render the four-section context file, truncating at section boundaries.

    If the full output exceeds config.context_md_max, sections are dropped from
    the bottom (lowest priority first: Signals → Open loops → Constraints) until
    the content fits. We never cut mid-section or mid-sentence.
    """
    header = (
        "# Session context (compact handoff)\n\n"
        f"_Captured `{ts}` · event `{hook_label}` · trigger `{trigger}`_\n\n"
    )

    budget = config.context_md_max - len(header) - 40  # reserve for omission note
    body_parts: list[str] = []
    dropped: list[str] = []

    for heading, key, formatter in _SECTIONS:
        section_text = heading + _bullets(extraction.get(key), formatter)
        if len("".join(body_parts)) + len(section_text) <= budget:
            body_parts.append(section_text)
        else:
            dropped.append(heading.strip().lstrip("#").strip())

    body = "".join(body_parts)
    if dropped:
        body += f"\n\n_Sections omitted to fit budget: {', '.join(dropped)}_\n"

    return header + body


_POINTER_TEMPLATE = """\
[handoff: prior session handoff available]

The previous segment of this conversation was compacted. A structured handoff
was extracted right before compaction and saved at:
  {path}

It contains:
  • Feature decisions (with what was rejected and why)
  • Constraints (hard rules shaping the work)
  • Open loops (unresolved questions / pending TODOs)
  • Signals (verbatim error messages, file paths, env vars, commands)

Captured: {ts}  |  trigger: {trigger}

Use the Read tool to load this file ONLY if the user's next request may depend
on context from before compaction. Otherwise ignore this note entirely.
"""


def _pointer_text(context_md: Path, marker_meta: dict) -> str:
    return _POINTER_TEMPLATE.format(
        path=str(context_md),
        ts=marker_meta.get("ts", "unknown"),
        trigger=marker_meta.get("trigger", "unknown"),
    )


# ── Audit log helpers ─────────────────────────────────────────────────────────

def _truncate(s: str, max_chars: int) -> str:
    """Return s unchanged if it fits; otherwise clip and append an ellipsis marker."""
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "…(truncated)"


def _payload_excerpt(data: dict, config: Config) -> object:
    """Build a compact audit payload from hook data when no message/summary is present.

    Returns a dict when it fits in the budget, a truncated string otherwise.
    Caps at 40 keys to keep the log scannable.
    """
    slim = {k: data[k] for k in list(data)[:40]}
    dumped = json.dumps(slim, ensure_ascii=False, default=str)
    if len(dumped) <= config.jsonl_payload_max:
        return json.loads(dumped)
    return dumped[: config.jsonl_payload_max] + "…(truncated)"


# ── PreCompact sub-steps ──────────────────────────────────────────────────────

def _write_context_file(
    result: ExtractionResult,
    out_dir: Path,
    session_id: str | None,
    ts: str,
    hook_label: str,
    trigger: object,
    config: Config,
) -> Path | None:
    """Write the context .md file if extraction succeeded. Returns the path or None."""
    if not result.ok:
        return None
    assert result.data is not None  # guaranteed by result.ok
    content = _render_context_md(
        result.data, ts=ts, hook_label=hook_label, trigger=trigger, config=config
    )
    path = _context_md_path(out_dir, session_id)
    path.write_text(content, encoding="utf-8")
    return path


def _write_marker(
    context_md_path: Path,
    out_dir: Path,
    session_id: str | None,
    ts: str,
    hook_label: str,
    trigger: object,
) -> None:
    """Write the one-shot marker that triggers injection on the next prompt."""
    body = {
        "ts": ts,
        "trigger": trigger,
        "hook_event_name": hook_label,
        "session_id": session_id,
        "context_md": str(context_md_path),
    }
    _marker_path(out_dir, session_id).write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8"
    )


def _build_snapshot(
    result: ExtractionResult,
    context_md_path: Path | None,
    data: dict,
    ts: str,
    hook_label: str,
    argv_event: str,
    config: Config,
) -> dict:
    """Build the dict for one line of snapshots.jsonl."""
    message         = _pick_message(data)
    compact_summary = data.get("compact_summary")
    tp_raw          = data.get("transcript_path") or data.get("transcriptPath")
    transcript_path = tp_raw if isinstance(tp_raw, str) and tp_raw.strip() else None

    log: dict = {
        "ts":                 ts,
        "argv_event":         argv_event,
        "hook_event_name":    hook_label,
        "trigger":            data.get("trigger"),
        "cwd":                data.get("cwd"),
        "session_id":         _session_id(data),
        "transcript_path":    transcript_path,
        "context_md_written": context_md_path is not None,
    }

    if context_md_path is None:
        log["context_md_skip_reason"] = result.skip_reason or "unknown"
        if result.cli_stderr:
            log["cli_stderr"] = result.cli_stderr

    if message:
        log["last_assistant_message"] = _truncate(message, config.last_msg_max_chars)

    if isinstance(compact_summary, str) and compact_summary.strip():
        log["compact_summary"] = _truncate(compact_summary, config.summary_max_chars)

    custom = str(data.get("custom_instructions") or "")
    if custom.strip():
        log["custom_instructions"] = _truncate(custom, config.custom_instructions_max)

    if not message and not compact_summary:
        log["payload_excerpt"] = _payload_excerpt(data, config)

    return log


def _append_snapshot(log: dict, out_dir: Path) -> None:
    """Append one audit line to snapshots.jsonl."""
    with (out_dir / "snapshots.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(log, ensure_ascii=False) + "\n")


def _write_latest_md(
    context_md_path: Path | None,
    skip_reason: str | None,
    data: dict,
    ts: str,
    hook_label: str,
    out_dir: Path,
    config: Config,
) -> None:
    """Write latest.md - always, even when extraction fails."""
    message = _pick_message(data)
    compact_summary = data.get("compact_summary")
    trigger = data.get("trigger")

    parts: list[str] = [
        "# handoff - session snapshot\n\n",
        f"**Captured:** `{ts}`  \n",
        f"**Event:** `{hook_label}`\n\n",
    ]
    if trigger is not None:
        parts.append(f"## Compaction trigger\n\n`{trigger}`\n\n")
    if message:
        show = message[: config.last_msg_max_chars]
        parts.append("## Last assistant message\n\n")
        parts.append(show)
        if len(message) > config.last_msg_max_chars:
            parts.append("\n\n…(truncated)")
        parts.append("\n\n")
    if isinstance(compact_summary, str) and compact_summary.strip():
        parts.append("## Compact summary\n\n")
        parts.append(compact_summary)
        parts.append("\n\n")
    if context_md_path is not None:
        parts.append(
            "## Next step\n\n"
            f"Claude Code will auto-inject a pointer to **`.ai-handoff/{context_md_path.name}`** "
            "on your next prompt. Or include it manually with "
            f"`@.ai-handoff/{context_md_path.name}`\n"
        )
    else:
        parts.append(
            "## Next step\n\n"
            f"Context file was skipped (`{skip_reason}`). Check `.ai-handoff/snapshots.jsonl` "
            "for the reason, or include `.ai-handoff/latest.md` manually in your next prompt.\n"
        )
    (out_dir / "latest.md").write_text("".join(parts), encoding="utf-8")


# ── Handlers ──────────────────────────────────────────────────────────────────

def _handle_pre_compact(argv_event: str, data: dict, config: Config) -> int:
    out_dir    = _ensure_out_dir(_repo_root())
    ts         = _utc_now()
    hook_label = _hook_label(data, argv_event)
    session_id = _session_id(data)

    records             = _load_records(data, config)
    custom_instructions = str(data.get("custom_instructions") or "")
    result              = _run_extraction(records, custom_instructions, config)

    context_md_path = _write_context_file(result, out_dir, session_id, ts, hook_label, data.get("trigger"), config)

    if context_md_path is not None:
        _write_marker(context_md_path, out_dir, session_id, ts, hook_label, data.get("trigger"))
    else:
        if result.cli_stderr:
            sys.stderr.write(f"[handoff] claude CLI stderr: {result.cli_stderr}\n")
        sys.stderr.write(f"[handoff] context file skipped: {result.skip_reason}\n")

    snapshot = _build_snapshot(result, context_md_path, data, ts, hook_label, argv_event, config)
    _append_snapshot(snapshot, out_dir)
    _write_latest_md(context_md_path, result.skip_reason, data, ts, hook_label, out_dir, config)
    return 0


def _handle_post_compact(argv_event: str, data: dict, config: Config) -> int:
    out_dir    = _ensure_out_dir(_repo_root())
    trigger    = data.get("trigger") or "unknown"
    session_id = _session_id(data)
    ctx_path   = _context_md_path(out_dir, session_id)

    if ctx_path.is_file():
        sys.stderr.write(
            f"[handoff] Compaction ({trigger}) done. "
            f"Handoff ready at .ai-handoff/{ctx_path.name} - "
            "will be injected on your next prompt automatically.\n"
        )
    else:
        sys.stderr.write(
            f"[handoff] Compaction ({trigger}) done, but no context file was written. "
            "See .ai-handoff/snapshots.jsonl for the skip reason.\n"
        )
    return 0


def _handle_user_prompt_submit(argv_event: str, data: dict, config: Config) -> int:
    out_dir    = _ensure_out_dir(_repo_root())
    session_id = _session_id(data)
    marker     = _marker_path(out_dir, session_id)

    # No fallback to other sessions' markers - picking up the wrong session's
    # context is worse than injecting nothing.
    if not marker.is_file():
        return 0

    try:
        meta = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            meta = {}
    except (OSError, json.JSONDecodeError):
        meta = {}

    context_md_str = meta.get("context_md")
    context_md = Path(context_md_str) if context_md_str else out_dir / "context.md"

    if not context_md.is_file():
        # Context file was never written (extraction failed) or was manually deleted.
        # Delete the stale marker so it doesn't fire again on every subsequent prompt.
        try:
            marker.unlink()
        except OSError:
            pass
        return 0

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _pointer_text(context_md, meta),
        }
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    sys.stdout.flush()

    # Injection succeeded - delete the marker (one-shot guarantee).
    try:
        marker.unlink()
    except OSError:
        pass

    # Audit write is best-effort: the injection is already done, so a failure
    # here (e.g. disk full) must not surface as an error to the host session.
    try:
        _append_snapshot(
            {
                "ts": _utc_now(),
                "argv_event": argv_event,
                "hook_event_name": _hook_label(data, argv_event),
                "session_id": session_id,
                "pointer_injected": True,
                "marker_ts": meta.get("ts"),
                "marker_trigger": meta.get("trigger"),
            },
            out_dir,
        )
    except Exception:  # noqa: BLE001
        pass

    return 0


def _handle_status(argv_event: str, data: dict, config: Config) -> int:
    """Print a health check to stdout (pipeable/redirectable)."""
    lines: list[str] = [f"handoff v{__version__}\n", "─" * 40 + "\n"]

    claude_bin = shutil.which("claude")
    lines.append(
        f"✓  claude CLI     {claude_bin}\n"
        if claude_bin
        else "✗  claude CLI     not found - install from https://claude.ai/code\n"
    )
    lines.append(f"✓  python3        {sys.executable} ({sys.version.split()[0]})\n")

    overrides = {k: os.environ[k] for k in Config.env_var_names() if k in os.environ}
    if overrides:
        lines.append("\nEnv overrides:\n")
        for k, v in overrides.items():
            lines.append(f"   {k}={v!r}\n")
    else:
        lines.append("   (no env overrides - using defaults)\n")

    out_dir_path = _repo_root() / ".ai-handoff"
    lines.append(f"\n.ai-handoff/ at: {out_dir_path}\n")
    if out_dir_path.is_dir():
        files = sorted(out_dir_path.iterdir())
        if files:
            for f in files:
                lines.append(f"   {f.name}  ({f.stat().st_size:,} bytes)\n")
        else:
            lines.append("   (empty - no compaction has fired yet)\n")
    else:
        lines.append("   (directory does not exist yet - no compaction has fired)\n")

    sys.stdout.write("".join(lines))
    sys.stdout.flush()
    return 0


# ── Dispatch ──────────────────────────────────────────────────────────────────

_ALIASES = {
    "precompact":         "preCompact",
    "pre_compact":        "preCompact",
    "postcompact":        "postCompact",
    "post_compact":       "postCompact",
    "userpromptsubmit":   "userPromptSubmit",
    "user_prompt_submit": "userPromptSubmit",
}

_HANDLERS = {
    "preCompact":       _handle_pre_compact,
    "postCompact":      _handle_post_compact,
    "userPromptSubmit": _handle_user_prompt_submit,
    "status":           _handle_status,
}


def _normalize_mode(argv_event: str) -> str:
    return _ALIASES.get(argv_event.lower(), argv_event)


def main() -> int:
    config     = Config.from_env()
    argv_event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    mode       = _normalize_mode(argv_event)
    data       = _read_stdin_json()

    handler = _HANDLERS.get(mode)

    try:
        if handler:
            return handler(argv_event, data, config)
        sys.stderr.write(
            f"[handoff] unknown mode '{argv_event}'. "
            f"Valid modes: {', '.join(_HANDLERS)}\n"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - never break the host session
        sys.stderr.write(
            f"[handoff] {mode} handler crashed: {type(exc).__name__}: {exc}\n"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
