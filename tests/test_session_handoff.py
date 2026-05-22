"""Tests for session_handoff.py"""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import session_handoff as sh
from session_handoff import Config, ExtractionResult


# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture()
def cfg() -> Config:
    """Default Config for tests - same as production defaults."""
    return Config()


@pytest.fixture()
def out_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".ai-handoff"
    d.mkdir()
    return d


@pytest.fixture()
def sample_extraction() -> dict:
    return {
        "feature_decisions": [
            {"choice": "use PreCompact hook", "reason": "fires before data is lost", "rejected": "Stop hook"}
        ],
        "constraints": ["no new dependencies", "always exit 0"],
        "open_loops": ["verify session_id is always present in hook payload"],
        "signals": ["~/.claude/settings.json", "HANDOFF_LLM_TIMEOUT=90"],
    }


# ── Config ────────────────────────────────────────────────────────────────────

class TestConfig:
    def test_defaults_are_sensible(self):
        cfg = Config()
        assert cfg.context_md_max == 4_000
        assert cfg.llm_timeout_s == 90
        assert cfg.llm_model == ""
        assert cfg.extra_instructions == ""

    def test_from_env_reads_env_vars(self):
        with patch.dict("os.environ", {"HANDOFF_CONTEXT_MD_MAX": "1000", "HANDOFF_LLM_MODEL": "haiku"}):
            cfg = Config.from_env()
        assert cfg.context_md_max == 1000
        assert cfg.llm_model == "haiku"

    def test_from_env_ignores_bad_int_and_uses_default(self):
        with patch.dict("os.environ", {"HANDOFF_CONTEXT_MD_MAX": "not-a-number"}):
            cfg = Config.from_env()
        assert cfg.context_md_max == 4_000

    def test_frozen_cannot_be_mutated(self):
        cfg = Config()
        with pytest.raises(Exception):
            cfg.context_md_max = 100  # type: ignore[misc]


# ── ExtractionResult ─────────────────────────────────────────────────────────

class TestExtractionResult:
    def test_ok_is_true_when_data_present(self):
        r = ExtractionResult(data={"a": 1}, skip_reason=None)
        assert r.ok is True

    def test_ok_is_false_when_data_none(self):
        r = ExtractionResult(data=None, skip_reason="timeout")
        assert r.ok is False

    def test_frozen_cannot_be_mutated(self):
        r = ExtractionResult(data=None, skip_reason="no_claude_cli")
        with pytest.raises(Exception):
            r.skip_reason = "other"  # type: ignore[misc]


# ── _render_context_md ───────────────────────────────────────────────────────

class TestRenderContextMd:
    def test_all_sections_present_when_within_budget(self, cfg, sample_extraction):
        text = sh._render_context_md(
            sample_extraction, ts="2026-01-01T00:00:00Z", hook_label="PreCompact",
            trigger="auto", config=cfg,
        )
        assert "## Feature decisions" in text
        assert "## Constraints" in text
        assert "## Open loops" in text
        assert "## Signals" in text

    def test_decisions_rendered_with_choice_reason_and_rejected(self, cfg, sample_extraction):
        text = sh._render_context_md(
            sample_extraction, ts="2026-01-01T00:00:00Z", hook_label="PreCompact",
            trigger="auto", config=cfg,
        )
        assert "use PreCompact hook" in text
        assert "fires before data is lost" in text
        assert "Stop hook" in text

    def test_truncation_drops_complete_sections_not_mid_content(self, sample_extraction):
        tight_cfg = Config(context_md_max=300)
        text = sh._render_context_md(
            sample_extraction, ts="2026-01-01T00:00:00Z", hook_label="PreCompact",
            trigger="auto", config=tight_cfg,
        )
        # Should never end with a partial heading or bullet
        assert not text.rstrip().endswith("##")
        assert not text.rstrip().endswith("-")

    def test_output_never_exceeds_budget(self, sample_extraction):
        tight_cfg = Config(context_md_max=500)
        text = sh._render_context_md(
            sample_extraction, ts="2026-01-01T00:00:00Z", hook_label="PreCompact",
            trigger="auto", config=tight_cfg,
        )
        assert len(text) <= 500 + 60  # tolerance for omission note

    def test_empty_sections_render_as_none_placeholder(self, cfg):
        extraction = {"feature_decisions": [], "constraints": [], "open_loops": [], "signals": []}
        text = sh._render_context_md(
            extraction, ts="2026-01-01T00:00:00Z", hook_label="PreCompact",
            trigger="auto", config=cfg,
        )
        assert "_(none)_" in text


# ── _llm_extract ─────────────────────────────────────────────────────────────

class TestLlmExtract:
    def _make_proc(self, stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
        proc = MagicMock()
        proc.stdout = stdout
        proc.stderr = stderr
        proc.returncode = returncode
        return proc

    def _wrap(self, extraction: dict) -> str:
        return json.dumps({"result": json.dumps(extraction)})

    def test_returns_ok_result_on_valid_response(self, cfg, sample_extraction):
        proc = self._make_proc(self._wrap(sample_extraction))
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=proc):
            result = sh._llm_extract("prompt", cfg)
        assert result.ok
        assert result.data == sample_extraction
        assert result.cli_stderr is None

    def test_skip_reason_is_no_claude_cli_when_not_found(self, cfg):
        with patch("shutil.which", return_value=None):
            result = sh._llm_extract("prompt", cfg)
        assert result.skip_reason == "no_claude_cli"

    def test_skip_reason_is_timeout_on_timeout(self, cfg):
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=90)):
            result = sh._llm_extract("prompt", cfg)
        assert result.skip_reason == "timeout"

    def test_cli_stderr_captured_on_nonzero_exit(self, cfg):
        proc = self._make_proc("", returncode=1, stderr="rate limit exceeded")
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=proc):
            result = sh._llm_extract("prompt", cfg)
        assert not result.ok
        assert result.skip_reason == "exit_1"
        assert result.cli_stderr == "rate limit exceeded"

    def test_cli_stderr_is_none_when_empty(self, cfg):
        proc = self._make_proc("", returncode=1, stderr="")
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=proc):
            result = sh._llm_extract("prompt", cfg)
        assert result.cli_stderr is None

    def test_skip_reason_bad_envelope_json_on_unparseable_stdout(self, cfg):
        proc = self._make_proc("not json")
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=proc):
            result = sh._llm_extract("prompt", cfg)
        assert result.skip_reason == "bad_envelope_json"

    def test_skip_reason_bad_extraction_json_when_result_not_json(self, cfg):
        proc = self._make_proc(json.dumps({"result": "this is not json"}))
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=proc):
            result = sh._llm_extract("prompt", cfg)
        assert result.skip_reason == "bad_extraction_json"

    def test_handles_json_fenced_extraction(self, cfg, sample_extraction):
        fenced = f"```json\n{json.dumps(sample_extraction)}\n```"
        proc = self._make_proc(json.dumps({"result": fenced}))
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=proc):
            result = sh._llm_extract("prompt", cfg)
        assert result.ok
        assert result.data == sample_extraction

    def test_model_flag_included_when_configured(self):
        cfg_with_model = Config(llm_model="claude-haiku-4-5")
        empty = {"feature_decisions": [], "constraints": [], "open_loops": [], "signals": []}
        proc = MagicMock(stdout=json.dumps({"result": json.dumps(empty)}), stderr="", returncode=0)
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=proc) as mock_run:
            sh._llm_extract("prompt", cfg_with_model)
        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        assert "claude-haiku-4-5" in cmd


# ── _run_extraction ───────────────────────────────────────────────────────────

class TestRunExtraction:
    def test_returns_no_transcript_when_nothing_to_extract(self, cfg):
        result = sh._run_extraction([], "", cfg)
        assert result.skip_reason == "no_transcript"

    def test_custom_instructions_alone_trigger_extraction(self, cfg, sample_extraction):
        proc = MagicMock(
            stdout=json.dumps({"result": json.dumps(sample_extraction)}),
            stderr="", returncode=0,
        )
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=proc):
            result = sh._run_extraction([], "focus on DB queries", cfg)
        assert result.ok


# ── Marker lifecycle ──────────────────────────────────────────────────────────

class TestMarkerLifecycle:
    def test_marker_path_uses_session_id(self, out_dir):
        path = sh._marker_path(out_dir, "abc-123")
        assert "abc-123" in path.name
        assert path.name.startswith("pending-")

    def test_marker_path_is_generic_when_no_session_id(self, out_dir):
        path = sh._marker_path(out_dir, None)
        assert path.name == "pending.marker"

    def test_unsafe_chars_in_session_id_are_sanitised(self, out_dir):
        path = sh._marker_path(out_dir, "session/with/../traversal")
        assert "/" not in path.name
        assert ".." not in path.name

    def test_no_fallback_to_other_session_markers(self, tmp_path, cfg):
        """UserPromptSubmit must not inject another session's context."""
        handoff = tmp_path / ".ai-handoff"
        handoff.mkdir()
        other_marker = handoff / "pending-other.marker"
        other_marker.write_text(json.dumps({"ts": "2026-01-01T00:00:00Z", "trigger": "auto"}))

        data = {"session_id": "mine"}
        with patch.dict("os.environ", {"HANDOFF_ROOT": str(tmp_path)}):
            code = sh._handle_user_prompt_submit("userPromptSubmit", data, cfg)

        assert code == 0
        assert other_marker.exists()  # untouched

    def test_marker_deleted_after_injection(self, tmp_path, cfg):
        """One-shot guarantee: marker must not survive past the first injection."""
        handoff = tmp_path / ".ai-handoff"
        handoff.mkdir()
        context_file = handoff / "context-mine.md"
        context_file.write_text("# Session context\n\n## Feature decisions\n\n- _(none)_\n")
        marker = handoff / "pending-mine.marker"
        marker.write_text(json.dumps({
            "ts": "2026-01-01T00:00:00Z",
            "trigger": "auto",
            "session_id": "mine",
            "context_md": str(context_file),
        }))

        data = {"session_id": "mine"}
        with patch.dict("os.environ", {"HANDOFF_ROOT": str(tmp_path)}):
            sh._handle_user_prompt_submit("userPromptSubmit", data, cfg)

        assert not marker.exists()


# ── Hook always exits 0 ───────────────────────────────────────────────────────

class TestHookAlwaysExitsZero:
    """The hook must never return non-zero - it must never break the host session."""

    def test_pre_compact_exits_zero_when_no_transcript(self, tmp_path, cfg):
        with patch.dict("os.environ", {"HANDOFF_ROOT": str(tmp_path)}):
            assert sh._handle_pre_compact("preCompact", {}, cfg) == 0

    def test_pre_compact_exits_zero_when_claude_cli_missing(self, tmp_path, cfg):
        data = {"transcript_path": str(tmp_path / "nonexistent.jsonl")}
        with patch.dict("os.environ", {"HANDOFF_ROOT": str(tmp_path)}), \
             patch("shutil.which", return_value=None):
            assert sh._handle_pre_compact("preCompact", data, cfg) == 0

    def test_post_compact_exits_zero(self, tmp_path, cfg):
        with patch.dict("os.environ", {"HANDOFF_ROOT": str(tmp_path)}):
            assert sh._handle_post_compact("postCompact", {"trigger": "auto"}, cfg) == 0

    def test_user_prompt_submit_exits_zero_with_no_marker(self, tmp_path, cfg):
        (tmp_path / ".ai-handoff").mkdir()
        with patch.dict("os.environ", {"HANDOFF_ROOT": str(tmp_path)}):
            assert sh._handle_user_prompt_submit("userPromptSubmit", {}, cfg) == 0


# ── _read_transcript_tail ─────────────────────────────────────────────────────

class TestReadTranscriptTail:
    def test_returns_empty_list_for_nonexistent_file(self, cfg, tmp_path):
        result = sh._read_transcript_tail(tmp_path / "missing.jsonl", cfg)
        assert result == []

    def test_parses_valid_jsonl(self, cfg, tmp_path):
        f = tmp_path / "transcript.jsonl"
        f.write_text(
            json.dumps({"type": "user",      "message": {"role": "user",      "content": "hello"}}) + "\n"
            + json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "world"}}) + "\n"
        )
        result = sh._read_transcript_tail(f, cfg)
        assert len(result) == 2

    def test_skips_malformed_lines_silently(self, cfg, tmp_path):
        f = tmp_path / "transcript.jsonl"
        f.write_text('{"valid": true}\nnot json\n{"also": "valid"}\n')
        result = sh._read_transcript_tail(f, cfg)
        assert len(result) == 2


# ── _strip_json_fences ────────────────────────────────────────────────────────

class TestStripJsonFences:
    def test_returns_plain_json_unchanged(self):
        raw = '{"a": 1}'
        assert sh._strip_json_fences(raw) == raw

    def test_strips_json_code_fence(self):
        assert sh._strip_json_fences('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_strips_plain_code_fence(self):
        assert sh._strip_json_fences('```\n{"a": 1}\n```') == '{"a": 1}'


# ── Dispatch (_HANDLERS dict) ─────────────────────────────────────────────────

# ── _truncate ─────────────────────────────────────────────────────────────────

class TestTruncate:
    def test_returns_string_unchanged_when_within_limit(self):
        assert sh._truncate("hello", 10) == "hello"

    def test_clips_and_appends_marker_when_over_limit(self):
        result = sh._truncate("abcde", 3)
        assert result.startswith("abc")
        assert "truncated" in result

    def test_exact_limit_is_not_truncated(self):
        assert sh._truncate("abc", 3) == "abc"


# ── _payload_excerpt ──────────────────────────────────────────────────────────

class TestPayloadExcerpt:
    def test_returns_dict_when_payload_fits(self, cfg):
        data = {"key": "value", "trigger": "auto"}
        result = sh._payload_excerpt(data, cfg)
        assert isinstance(result, dict)
        assert result["key"] == "value"

    def test_returns_truncated_string_when_over_budget(self):
        tiny_cfg = Config(jsonl_payload_max=10)
        data = {"key": "a very long value that definitely exceeds the budget"}
        result = sh._payload_excerpt(data, tiny_cfg)
        assert isinstance(result, str)
        assert "truncated" in result

    def test_caps_at_40_keys(self, cfg):
        data = {str(i): i for i in range(100)}
        result = sh._payload_excerpt(data, cfg)
        assert isinstance(result, dict)
        assert len(result) <= 40


# ── _build_snapshot ───────────────────────────────────────────────────────────

class TestBuildSnapshot:
    def _ok_result(self, sample_extraction) -> ExtractionResult:
        return ExtractionResult(data=sample_extraction, skip_reason=None)

    def _fail_result(self, reason: str = "timeout", stderr: str | None = None) -> ExtractionResult:
        return ExtractionResult(data=None, skip_reason=reason, cli_stderr=stderr)

    def test_required_keys_always_present(self, cfg, out_dir, sample_extraction):
        snap = sh._build_snapshot(
            self._ok_result(sample_extraction), out_dir / "ctx.md",
            {}, "2026-01-01T00:00:00Z", "PreCompact", "preCompact", cfg,
        )
        for key in ("ts", "argv_event", "hook_event_name", "session_id", "context_md_written"):
            assert key in snap

    def test_context_md_written_true_when_path_given(self, cfg, out_dir, sample_extraction):
        snap = sh._build_snapshot(
            self._ok_result(sample_extraction), out_dir / "ctx.md",
            {}, "2026-01-01T00:00:00Z", "PreCompact", "preCompact", cfg,
        )
        assert snap["context_md_written"] is True

    def test_skip_reason_included_when_no_context_file(self, cfg, out_dir):
        snap = sh._build_snapshot(
            self._fail_result("timeout"), None,
            {}, "2026-01-01T00:00:00Z", "PreCompact", "preCompact", cfg,
        )
        assert snap["context_md_written"] is False
        assert snap["context_md_skip_reason"] == "timeout"

    def test_cli_stderr_included_when_present(self, cfg, out_dir):
        snap = sh._build_snapshot(
            self._fail_result("exit_1", stderr="rate limit"), None,
            {}, "2026-01-01T00:00:00Z", "PreCompact", "preCompact", cfg,
        )
        assert snap["cli_stderr"] == "rate limit"

    def test_cli_stderr_absent_when_none(self, cfg, out_dir):
        snap = sh._build_snapshot(
            self._fail_result("timeout", stderr=None), None,
            {}, "2026-01-01T00:00:00Z", "PreCompact", "preCompact", cfg,
        )
        assert "cli_stderr" not in snap

    def test_last_assistant_message_truncated_at_budget(self, out_dir, sample_extraction):
        tight_cfg = Config(last_msg_max_chars=10)
        data = {"last_assistant_message": "x" * 100}
        snap = sh._build_snapshot(
            ExtractionResult(data=sample_extraction, skip_reason=None),
            out_dir / "ctx.md", data, "2026-01-01T00:00:00Z", "PreCompact", "preCompact", tight_cfg,
        )
        assert "truncated" in snap["last_assistant_message"]
        assert len(snap["last_assistant_message"]) < 100

    def test_custom_instructions_truncated_at_budget(self, out_dir, sample_extraction):
        tight_cfg = Config(custom_instructions_max=5)
        data = {"custom_instructions": "focus on everything"}
        snap = sh._build_snapshot(
            ExtractionResult(data=sample_extraction, skip_reason=None),
            out_dir / "ctx.md", data, "2026-01-01T00:00:00Z", "PreCompact", "preCompact", tight_cfg,
        )
        assert "truncated" in snap["custom_instructions"]

    def test_payload_excerpt_used_when_no_message_or_summary(self, cfg, out_dir):
        data = {"trigger": "auto", "cwd": "/tmp"}
        snap = sh._build_snapshot(
            self._fail_result("no_transcript"), None,
            data, "2026-01-01T00:00:00Z", "PreCompact", "preCompact", cfg,
        )
        assert "payload_excerpt" in snap

    def test_payload_excerpt_absent_when_message_present(self, cfg, out_dir, sample_extraction):
        data = {"last_assistant_message": "done!"}
        snap = sh._build_snapshot(
            self._ok_result(sample_extraction), out_dir / "ctx.md",
            data, "2026-01-01T00:00:00Z", "PreCompact", "preCompact", cfg,
        )
        assert "payload_excerpt" not in snap


# ── _write_context_file ───────────────────────────────────────────────────────

class TestWriteContextFile:
    def test_returns_none_when_extraction_failed(self, cfg, out_dir):
        result = sh._write_context_file(
            ExtractionResult(data=None, skip_reason="timeout"),
            out_dir, "s1", "2026-01-01T00:00:00Z", "PreCompact", "auto", cfg,
        )
        assert result is None

    def test_writes_file_and_returns_path_on_success(self, cfg, out_dir, sample_extraction):
        path = sh._write_context_file(
            ExtractionResult(data=sample_extraction, skip_reason=None),
            out_dir, "s1", "2026-01-01T00:00:00Z", "PreCompact", "auto", cfg,
        )
        assert path is not None
        assert path.is_file()

    def test_written_file_contains_extraction_content(self, cfg, out_dir, sample_extraction):
        path = sh._write_context_file(
            ExtractionResult(data=sample_extraction, skip_reason=None),
            out_dir, "s1", "2026-01-01T00:00:00Z", "PreCompact", "auto", cfg,
        )
        assert path is not None
        content = path.read_text()
        assert "use PreCompact hook" in content
        assert "no new dependencies" in content

    def test_no_file_written_on_failure(self, cfg, out_dir):
        sh._write_context_file(
            ExtractionResult(data=None, skip_reason="no_transcript"),
            out_dir, "s1", "2026-01-01T00:00:00Z", "PreCompact", "auto", cfg,
        )
        assert not any(out_dir.iterdir())


# ── _write_latest_md ──────────────────────────────────────────────────────────

class TestWriteLatestMd:
    def test_file_always_written(self, cfg, out_dir):
        sh._write_latest_md(None, "timeout", {}, "2026-01-01T00:00:00Z", "PreCompact", out_dir, cfg)
        assert (out_dir / "latest.md").is_file()

    def test_contains_next_step_with_context_path_on_success(self, cfg, out_dir):
        ctx = out_dir / "context-s1.md"
        sh._write_latest_md(ctx, None, {}, "2026-01-01T00:00:00Z", "PreCompact", out_dir, cfg)
        content = (out_dir / "latest.md").read_text()
        assert "context-s1.md" in content
        assert "Next step" in content

    def test_contains_skip_reason_on_failure(self, cfg, out_dir):
        sh._write_latest_md(None, "no_claude_cli", {}, "2026-01-01T00:00:00Z", "PreCompact", out_dir, cfg)
        content = (out_dir / "latest.md").read_text()
        assert "no_claude_cli" in content
        assert "Next step" in content

    def test_compact_summary_section_present_when_provided(self, cfg, out_dir):
        data = {"compact_summary": "Summary of what happened"}
        sh._write_latest_md(None, "no_transcript", data, "2026-01-01T00:00:00Z", "PreCompact", out_dir, cfg)
        content = (out_dir / "latest.md").read_text()
        assert "Summary of what happened" in content

    def test_last_assistant_message_truncated_in_latest_md(self, out_dir):
        tight_cfg = Config(last_msg_max_chars=5)
        data = {"last_assistant_message": "a very long message"}
        sh._write_latest_md(None, "no_transcript", data, "2026-01-01T00:00:00Z", "PreCompact", out_dir, tight_cfg)
        content = (out_dir / "latest.md").read_text()
        assert "truncated" in content


# ── _handle_post_compact ──────────────────────────────────────────────────────

class TestHandlePostCompact:
    def test_emits_success_message_when_context_file_exists(self, tmp_path, cfg, capsys):
        handoff = tmp_path / ".ai-handoff"
        handoff.mkdir()
        (handoff / "context-s1.md").write_text("# ctx\n")
        data = {"trigger": "manual", "session_id": "s1"}
        with patch.dict("os.environ", {"HANDOFF_ROOT": str(tmp_path)}):
            code = sh._handle_post_compact("postCompact", data, cfg)
        assert code == 0
        captured = capsys.readouterr()
        assert "Handoff ready" in captured.err

    def test_emits_warning_when_context_file_missing(self, tmp_path, cfg, capsys):
        (tmp_path / ".ai-handoff").mkdir()
        data = {"trigger": "auto", "session_id": "no-such-session"}
        with patch.dict("os.environ", {"HANDOFF_ROOT": str(tmp_path)}):
            code = sh._handle_post_compact("postCompact", data, cfg)
        assert code == 0
        captured = capsys.readouterr()
        assert "no context file was written" in captured.err


# ── Config.env_var_names() ────────────────────────────────────────────────────

class TestConfigEnvVarNames:
    def test_returns_a_non_empty_tuple(self):
        names = Config.env_var_names()
        assert isinstance(names, tuple)
        assert len(names) > 0

    def test_all_names_are_uppercase_strings(self):
        for name in Config.env_var_names():
            assert isinstance(name, str)
            assert name == name.upper()

    def test_status_uses_same_list_as_config(self, tmp_path, cfg):
        """Smoke test: _handle_status must not crash when all env vars are set."""
        overrides = {k: "1" for k in Config.env_var_names()}
        (tmp_path / ".ai-handoff").mkdir()
        with patch.dict("os.environ", {**overrides, "HANDOFF_ROOT": str(tmp_path)}):
            code = sh._handle_status("status", {}, cfg)
        assert code == 0


# ── Dispatch (_HANDLERS dict) ─────────────────────────────────────────────────

class TestDispatch:
    def test_all_known_modes_have_handlers(self):
        for mode in ("preCompact", "postCompact", "userPromptSubmit", "status"):
            assert mode in sh._HANDLERS

    def test_aliases_resolve_to_canonical_modes(self):
        assert sh._normalize_mode("precompact")       == "preCompact"
        assert sh._normalize_mode("pre_compact")      == "preCompact"
        assert sh._normalize_mode("postcompact")      == "postCompact"
        assert sh._normalize_mode("userpromptsubmit") == "userPromptSubmit"

    def test_precompaction_is_not_an_alias(self):
        """'precompaction' was a noisy alias; it should no longer resolve."""
        assert sh._normalize_mode("precompaction") != "preCompact"

    def test_unknown_mode_is_returned_unchanged(self):
        assert sh._normalize_mode("totally-unknown") == "totally-unknown"
        assert "totally-unknown" not in sh._HANDLERS
