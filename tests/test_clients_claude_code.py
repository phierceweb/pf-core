"""Tests for pf_core.clients.claude_code."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pf_core.clients._claude_code_wire import flatten_messages
from pf_core.clients.claude_code import (
    ClaudeCodeClient,
    ClaudeCodeError,
    DEFAULT_TIMEOUT_SECONDS,
    get_client,
    new_client,
    reset_client,
)
from pf_core.exceptions import AppError, ClientError


@pytest.fixture(autouse=True)
def _reset():
    reset_client()
    yield
    reset_client()


# ---------------------------------------------------------------------------
# flatten_messages
# ---------------------------------------------------------------------------


class TestFlattenMessages:
    def test_user_only(self):
        assert flatten_messages([{"role": "user", "content": "hi"}]) == "hi"

    def test_system_then_user(self):
        out = flatten_messages(
            [
                {"role": "system", "content": "You are a bot."},
                {"role": "user", "content": "Hello."},
            ]
        )
        assert out == "You are a bot.\n\n---\n\nHello."

    def test_multiple_user_messages_joined(self):
        out = flatten_messages(
            [
                {"role": "user", "content": "first"},
                {"role": "user", "content": "second"},
            ]
        )
        assert out == "first\n\nsecond"

    def test_multiple_system_messages_joined(self):
        out = flatten_messages(
            [
                {"role": "system", "content": "rule one"},
                {"role": "system", "content": "rule two"},
                {"role": "user", "content": "do thing"},
            ]
        )
        assert out == "rule one\n\nrule two\n\n---\n\ndo thing"

    def test_assistant_message_treated_as_body(self):
        out = flatten_messages(
            [
                {"role": "user", "content": "Q?"},
                {"role": "assistant", "content": "A."},
                {"role": "user", "content": "Q2?"},
            ]
        )
        assert "Q?" in out and "A." in out and "Q2?" in out

    def test_empty_messages_list(self):
        assert flatten_messages([]) == ""

    def test_none_messages(self):
        assert flatten_messages(None) == ""  # type: ignore[arg-type]

    def test_skips_empty_content(self):
        out = flatten_messages(
            [
                {"role": "system", "content": ""},
                {"role": "user", "content": "real content"},
            ]
        )
        assert out == "real content"

    def test_case_insensitive_role(self):
        out = flatten_messages(
            [
                {"role": "SYSTEM", "content": "S"},
                {"role": "User", "content": "U"},
            ]
        )
        assert out == "S\n\n---\n\nU"


# ---------------------------------------------------------------------------
# ClaudeCodeClient.chat
# ---------------------------------------------------------------------------


def _text_run(stdout: str) -> MagicMock:
    """A fake completed subprocess result with raw stdout + zero returncode."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout
    m.stderr = ""
    return m


def _envelope_run(**fields) -> MagicMock:
    """A fake `--output-format json` envelope on stdout."""
    envelope = {"result": "the answer", "is_error": False, "stop_reason": "end_turn"}
    envelope.update(fields)
    return _text_run(json.dumps(envelope))


def _ok_run(stdout: str = "ok response") -> MagicMock:
    """A successful run in envelope mode — ``stdout`` becomes the envelope's result."""
    return _envelope_run(result=stdout)


_FILE_FLAGS = ("--system-prompt-file", "--append-system-prompt-file")


def _argv(cmd: list[str]) -> list[str]:
    """``cmd`` without the binary, with each system-prompt temp file path as ``<file>``."""
    return ["<file>" if i and cmd[i - 1] in _FILE_FLAGS else arg for i, arg in enumerate(cmd)][1:]


class TestJsonEnvelope:
    """`--output-format json` is what makes truncation reportable at all: the
    envelope's top-level stop_reason is the model's, the text-mode stdout has
    no equivalent."""

    def _chat(self, mock_which, mock_run, run, **client_kwargs):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = run
        client = ClaudeCodeClient(**client_kwargs)
        return client.chat(messages=[{"role": "user", "content": "hi"}])

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_requests_json_output_before_print(self, mock_which, mock_run):
        self._chat(mock_which, mock_run, _envelope_run())
        cmd = mock_run.call_args.args[0]
        assert cmd.count("--output-format") == 1
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert cmd[-1] == "--print"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_content_comes_from_the_result_field(self, mock_which, mock_run):
        content, usage = self._chat(mock_which, mock_run, _envelope_run(result="  the answer\n"))
        assert content == "the answer"
        assert usage["finish_reason"] == "end_turn"

    @pytest.mark.parametrize("stop_reason", ["max_tokens", "model_context_window_exceeded"])
    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_truncation_reasons_normalise_to_length(self, mock_which, mock_run, stop_reason):
        content, usage = self._chat(
            mock_which, mock_run, _envelope_run(result="half an ans", stop_reason=stop_reason)
        )
        assert content == "half an ans"
        assert usage["finish_reason"] == "length"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_unknown_stop_reason_passes_through(self, mock_which, mock_run):
        """The CLI types stop_reason as a free string, not an enum."""
        _content, usage = self._chat(mock_which, mock_run, _envelope_run(stop_reason="whatever"))
        assert usage["finish_reason"] == "whatever"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_null_stop_reason_reports_nothing(self, mock_which, mock_run):
        content, usage = self._chat(mock_which, mock_run, _envelope_run(stop_reason=None))
        assert content == "the answer"
        assert "finish_reason" not in usage

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_error_envelope_is_not_trusted_for_stop_reason(self, mock_which, mock_run):
        with pytest.raises(ClaudeCodeError, match="something went wrong"):
            self._chat(
                mock_which,
                mock_run,
                _envelope_run(result="something went wrong", is_error=True, stop_reason="end_turn"),
            )

    @pytest.mark.parametrize(
        "stdout",
        ["just some prose\n", '{"stop_reason": "max_tokens"}', '"plain string"'],
        ids=["non-json", "envelope-without-result", "json-scalar"],
    )
    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_unreadable_envelope_raises_not_corrupts(self, mock_which, mock_run, stdout):
        """The client asked for JSON; raw stdout is machine output, never content."""
        with pytest.raises(ClaudeCodeError, match="output-format"):
            self._chat(mock_which, mock_run, _text_run(stdout))

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_error_envelope_with_zero_rc_raises_not_content(self, mock_which, mock_run):
        """An is_error result is a failure report, never the answer."""
        with pytest.raises(ClaudeCodeError, match="Failed to authenticate"):
            self._chat(
                mock_which,
                mock_run,
                _envelope_run(
                    result="Failed to authenticate: OAuth session expired", is_error=True
                ),
            )

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_nonzero_rc_error_quotes_the_envelope_not_empty_stderr(self, mock_which, mock_run):
        """JSON mode reports errors on stdout; the raise must carry that detail."""
        run = _envelope_run(result="Failed to authenticate: OAuth session expired", is_error=True)
        run.returncode = 1
        with pytest.raises(ClaudeCodeError, match="OAuth session expired"):
            self._chat(mock_which, mock_run, run)

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_verbose_suppresses_envelope_mode(self, mock_which, mock_run):
        """--verbose changes stdout's shape, so the client stays in text mode."""
        content, usage = self._chat(
            mock_which, mock_run, _text_run("the answer\n"), extra_args=["--verbose"]
        )
        cmd = mock_run.call_args.args[0]
        assert "--output-format" not in cmd
        assert content == "the answer"
        assert "finish_reason" not in usage

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_transcript_array_unwraps_via_final_result_entry(self, mock_which, mock_run):
        stdout = json.dumps(
            [
                {"type": "system", "subtype": "init"},
                {"type": "assistant", "message": {}},
                {"type": "result", "result": "the answer", "stop_reason": "max_tokens"},
            ]
        )
        content, usage = self._chat(mock_which, mock_run, _text_run(stdout))
        assert content == "the answer"
        assert usage["finish_reason"] == "length"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_consumer_supplied_output_format_wins(self, mock_which, mock_run):
        content, usage = self._chat(
            mock_which,
            mock_run,
            _text_run("stream text\n"),
            tools=["Bash"],
            extra_args=["--allowedTools", "Bash", "--output-format", "text"],
        )
        cmd = mock_run.call_args.args[0]
        assert cmd.count("--output-format") == 1
        assert cmd[cmd.index("--output-format") + 1] == "text"
        assert content == "stream text"
        assert "finish_reason" not in usage

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_consumer_supplied_output_format_equals_form_wins(self, mock_which, mock_run):
        self._chat(
            mock_which, mock_run, _text_run("stream text"), extra_args=["--output-format=text"]
        )
        cmd = mock_run.call_args.args[0]
        assert [a for a in cmd if a.startswith("--output-format")] == ["--output-format=text"]

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_collision_guard_does_not_parse_a_json_envelope(self, mock_which, mock_run):
        """Their format, their stdout: honour it verbatim rather than guessing."""
        content, _usage = self._chat(
            mock_which,
            mock_run,
            _envelope_run(result="the answer"),
            extra_args=["--output-format", "json"],
        )
        assert json.loads(content)["result"] == "the answer"


class TestEnvelopeTokenCounts:
    """The envelope's ``usage`` block is the only token source this backend
    has — without it a ``limit_tokens`` budget can never trip for claude_code
    runs (cost_usd is structurally 0.0)."""

    _USAGE = {
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_read_input_tokens": 5000,
        "cache_creation_input_tokens": 70,
    }

    def _chat(self, mock_which, mock_run, run, **client_kwargs):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = run
        client = ClaudeCodeClient(**client_kwargs)
        return client.chat(messages=[{"role": "user", "content": "hi"}])

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_envelope_usage_populates_token_counts(self, mock_which, mock_run):
        _content, usage = self._chat(mock_which, mock_run, _envelope_run(usage=self._USAGE))
        assert usage["prompt_tokens"] == 1200
        assert usage["completion_tokens"] == 340
        assert usage["cache_read_tokens"] == 5000
        assert usage["cache_write_tokens"] == 70
        assert usage["reasoning_tokens"] == 0
        assert usage["cost_usd"] == 0.0  # subscription billing unchanged

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_envelope_without_usage_falls_back_to_zeros(self, mock_which, mock_run):
        """Older CLIs emit no usage block — zeros, never a crash."""
        _content, usage = self._chat(mock_which, mock_run, _envelope_run())
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            assert usage[key] == 0

    @patch("pf_core.clients.claude_code._log")
    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_envelope_without_usage_warns(self, mock_which, mock_run, mock_log):
        """Zeros from an envelope are a surprise — a tokens budget cannot gate
        the call, so the condition has to be visible in the log."""
        self._chat(mock_which, mock_run, _envelope_run())
        assert [c.args[0] for c in mock_log.warning.call_args_list] == [
            "claude_code_envelope_without_usage"
        ]

    @patch("pf_core.clients.claude_code._log")
    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_envelope_with_usage_does_not_warn(self, mock_which, mock_run, mock_log):
        self._chat(
            mock_which,
            mock_run,
            _envelope_run(usage={"input_tokens": 10, "output_tokens": 3}),
        )
        assert mock_log.warning.call_args_list == []

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_partial_usage_defaults_missing_fields_to_zero(self, mock_which, mock_run):
        _content, usage = self._chat(
            mock_which, mock_run, _envelope_run(usage={"input_tokens": 10, "output_tokens": 3})
        )
        assert usage["prompt_tokens"] == 10
        assert usage["completion_tokens"] == 3
        assert usage["cache_read_tokens"] == 0
        assert usage["cache_write_tokens"] == 0

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_non_integer_usage_values_read_as_zero(self, mock_which, mock_run):
        _content, usage = self._chat(
            mock_which,
            mock_run,
            _envelope_run(usage={"input_tokens": "lots", "output_tokens": None}),
        )
        assert usage["prompt_tokens"] == 0
        assert usage["completion_tokens"] == 0

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_text_mode_keeps_zero_tokens(self, mock_which, mock_run):
        """Consumer-suppressed envelope = no token source; zeros as before."""
        _content, usage = self._chat(
            mock_which,
            mock_run,
            _text_run("the answer\n"),
            extra_args=["--output-format", "text"],
        )
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            assert usage[key] == 0

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_transcript_array_reads_usage_from_final_result_entry(self, mock_which, mock_run):
        stdout = json.dumps(
            [
                {"type": "system", "subtype": "init"},
                {
                    "type": "result",
                    "result": "the answer",
                    "stop_reason": None,
                    "usage": self._USAGE,
                },
            ]
        )
        _content, usage = self._chat(mock_which, mock_run, _text_run(stdout))
        assert usage["prompt_tokens"] == 1200
        assert usage["completion_tokens"] == 340

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_envelope_tokens_recorded_and_summed_by_budget_usage(
        self, mock_which, mock_run, pf_engine
    ):
        """The chain the parse exists for: chat() usage -> LlmRunRepo.record ->
        current_usage()['tokens'] — the figure a limit_tokens budget enforces."""
        from pf_core.budget.check import current_usage
        from pf_core.llm.tracking import LlmRunRepo, clear_resolver_caches, metadata

        metadata.create_all(pf_engine)
        clear_resolver_caches()
        try:
            _content, usage = self._chat(mock_which, mock_run, _envelope_run(usage=self._USAGE))
            LlmRunRepo().record(
                agent_type="summarizer",
                model="sonnet",
                provider="claude_code",
                usage=usage,
            )
            budget = {"id": 1, "period": "daily", "scope_kind": "global", "scope_value": None}
            summed = current_usage(budget)
            # prompt + completion + cache read + cache write
            assert summed["tokens"] == 1200 + 340 + 5000 + 70
            assert summed["usd"] == 0.0
            assert summed["calls"] == 1
        finally:
            clear_resolver_caches()
            metadata.drop_all(pf_engine)


class TestChatHappyPath:
    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_returns_content_and_usage_dict(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("hello\n")
        client = ClaudeCodeClient()
        content, usage = client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="anthropic/whatever",
        )
        assert content == "hello"  # stripped
        # Usage dict must carry the same keys as OpenRouterClient.chat
        # so callers can swap clients without code changes.
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "cost_usd",
            "duration_ms",
            "system_fingerprint",
        ):
            assert key in usage
        assert usage["prompt_tokens"] == 0
        assert usage["completion_tokens"] == 0
        assert usage["cost_usd"] == 0.0
        assert usage["duration_ms"] >= 0
        assert usage["finish_reason"] == "end_turn"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_user_content_goes_on_stdin_and_system_in_a_file(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        seen = {}

        def run(cmd, **kwargs):
            with open(cmd[cmd.index("--system-prompt-file") + 1], encoding="utf-8") as fh:
                seen["system"] = fh.read()
            return _ok_run("ok")

        mock_run.side_effect = run
        client = ClaudeCodeClient()
        client.chat(
            messages=[
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "summarize this"},
            ]
        )
        cmd = mock_run.call_args.args[0]
        assert cmd[-1] == "--print"
        assert mock_run.call_args.kwargs["input"] == "summarize this"
        assert seen["system"] == "be brief"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_large_prompt_uses_stdin_not_argv(self, mock_which, mock_run):
        """A multi-megabyte prompt must not appear in argv, where it would
        trip ARG_MAX (E2BIG)."""
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        big = "x" * 2_000_000  # 2 MB — well past macOS ARG_MAX (~256 KB)
        client = ClaudeCodeClient()
        client.chat(messages=[{"role": "user", "content": big}])
        cmd = mock_run.call_args.args[0]
        assert all(len(arg) < 1000 for arg in cmd), "prompt must not be in argv"
        assert mock_run.call_args.kwargs["input"] == big

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_extra_args_inserted_before_print(self, mock_which, mock_run, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(tools=["Bash"], extra_args=["--allowedTools", "Bash"])
        client.chat(messages=[{"role": "user", "content": "x"}])
        # The client's own flags lead; extra_args follow, before --print.
        assert _argv(mock_run.call_args.args[0]) == [
            "--safe-mode",
            "--no-session-persistence",
            "--tools",
            "Bash",
            "--system-prompt-file",
            "<file>",
            "--allowedTools",
            "Bash",
            "--output-format",
            "json",
            "--print",
        ]

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_temperature_etc_ignored(self, mock_which, mock_run, monkeypatch):
        """The CLI doesn't honor sampling params — they're accepted for API
        parity but must not appear in the subprocess command. ``model`` is
        handled by TestModelOverride below; this test covers the others."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()
        client.chat(
            messages=[{"role": "user", "content": "x"}],
            temperature=0.9,
            max_tokens=2000,
            top_p=0.5,
            response_format={"type": "json_object"},
        )
        cmd = mock_run.call_args.args[0]
        joined = " ".join(cmd)
        assert "0.9" not in joined
        assert "json_object" not in joined
        assert "--model" not in cmd  # no model anywhere → no flag


# ---------------------------------------------------------------------------
# Env isolation — API key must not leak into the subprocess
# ---------------------------------------------------------------------------


class TestEnvIsolation:
    """This transport authenticates via the active Claude Max session, NOT an
    API key (the module docstring's "consumes no API credits" promise). A
    stray ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN in the parent environment
    must be stripped from the child env, or ``claude --print`` silently
    switches to (billable, and possibly invalid) external API-key auth."""

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_api_key_and_auth_token_stripped(self, mock_which, mock_run, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-leak")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-must-not-leak")
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()
        client.chat(messages=[{"role": "user", "content": "x"}])
        env = mock_run.call_args.kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_other_env_vars_preserved(self, mock_which, mock_run, monkeypatch):
        """Only the two auth credentials are stripped — everything else
        (PATH, HOME, the user's session vars) must pass through, or the child
        ``claude`` process can't find its binary or its session config."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-leak")
        monkeypatch.setenv("PF_CORE_ENV_CANARY", "keep-me")
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()
        client.chat(messages=[{"role": "user", "content": "x"}])
        env = mock_run.call_args.kwargs["env"]
        assert env.get("PF_CORE_ENV_CANARY") == "keep-me"
        assert "PATH" in env

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_no_key_present_is_harmless(self, mock_which, mock_run, monkeypatch):
        """With no key set the strip is a no-op — chat() still passes an
        explicit env and runs normally (the fix must not depend on a key
        being present)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()
        content, _ = client.chat(messages=[{"role": "user", "content": "x"}])
        assert content == "ok"
        env = mock_run.call_args.kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in env


# ---------------------------------------------------------------------------
# Safe-mode isolation — ambient project context must not reach the subprocess
# ---------------------------------------------------------------------------


class TestSafeModeIsolation:
    """``--safe-mode`` keeps the cwd's CLAUDE.md, skills, hooks and plugins out of the
    call; on by default, ``isolate=False`` opts out."""

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_safe_mode_on_by_default(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        assert "--safe-mode" in cmd

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_safe_mode_is_leading_flag(self, mock_which, mock_run):
        """``--safe-mode`` leads the argv (right after the binary), before
        extra_args / --model / --print — a mode flag, conventionally first."""
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        assert cmd[1] == "--safe-mode"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_isolate_false_omits_safe_mode(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(isolate=False)
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        assert "--safe-mode" not in cmd

    def test_isolate_defaults_true(self):
        assert ClaudeCodeClient().isolate is True

    def test_isolate_false_attribute(self):
        assert ClaudeCodeClient(isolate=False).isolate is False

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_safe_mode_coexists_with_extra_args_and_model(self, mock_which, mock_run, monkeypatch):
        """``--safe-mode`` strips ambient customizations; explicit flags (--tools,
        --allowedTools, --model) still apply."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(
            tools=["Bash"], extra_args=["--allowedTools", "Bash"], model="haiku"
        )
        client.chat(messages=[{"role": "user", "content": "x"}])
        assert _argv(mock_run.call_args.args[0]) == [
            "--safe-mode",
            "--no-session-persistence",
            "--tools",
            "Bash",
            "--system-prompt-file",
            "<file>",
            "--allowedTools",
            "Bash",
            "--model",
            "haiku",
            "--output-format",
            "json",
            "--print",
        ]

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_safe_mode_and_env_strip_coexist(self, mock_which, mock_run, monkeypatch):
        """The two isolations are independent and both apply: ``--safe-mode``
        in argv AND the API key stripped from the child env."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-leak")
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        env = mock_run.call_args.kwargs["env"]
        assert "--safe-mode" in cmd
        assert "ANTHROPIC_API_KEY" not in env

    def test_get_client_isolate_defaults_true(self, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        assert get_client().isolate is True

    def test_get_client_passes_isolate(self, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        assert get_client(isolate=False).isolate is False

    def test_new_client_passes_isolate(self, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        assert new_client(isolate=False).isolate is False


class TestSessionPersistence:
    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_no_persistence_by_default(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        ClaudeCodeClient().chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        assert cmd.count("--no-session-persistence") == 1

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_flag_follows_safe_mode(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        ClaudeCodeClient().chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        assert cmd[1:3] == ["--safe-mode", "--no-session-persistence"]

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_flag_leads_without_isolation(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        ClaudeCodeClient(isolate=False).chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        assert cmd[1] == "--no-session-persistence"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_persist_session_true_omits_flag(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        ClaudeCodeClient(persist_session=True).chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        assert "--no-session-persistence" not in cmd

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_preflight_does_not_persist(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        ClaudeCodeClient().preflight()
        cmd = mock_run.call_args.args[0]
        assert "--no-session-persistence" in cmd

    @pytest.mark.parametrize(
        "extra",
        [
            ["--resume", "abc"],
            ["--resume=abc"],
            ["-r", "abc"],
            ["--continue"],
            ["-c"],
            ["--session-id", "0d9e1b6a-0000-4000-8000-000000000000"],
        ],
    )
    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_resuming_extra_args_keep_persistence(self, mock_which, mock_run, extra):
        """Without the transcript there is nothing for the next call to resume."""
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        ClaudeCodeClient(extra_args=extra).chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        assert "--no-session-persistence" not in cmd

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_unrelated_extra_args_still_suppress_persistence(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        ClaudeCodeClient(extra_args=["--continue-on-error"]).chat(
            messages=[{"role": "user", "content": "x"}]
        )
        cmd = mock_run.call_args.args[0]
        assert "--no-session-persistence" in cmd

    def test_persist_session_defaults_false(self):
        assert ClaudeCodeClient().persist_session is False

    def test_get_client_passes_persist_session(self, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        assert get_client(persist_session=True).persist_session is True

    def test_new_client_passes_persist_session(self, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        assert new_client(persist_session=True).persist_session is True


class TestChatErrors:
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_missing_binary_raises(self, mock_which):
        mock_which.return_value = None
        client = ClaudeCodeClient()
        with pytest.raises(ClaudeCodeError, match="not found on PATH"):
            client.chat(messages=[{"role": "user", "content": "x"}])

    @patch("pf_core.clients.claude_code.shutil.which")
    def test_missing_binary_uses_configured_name(self, mock_which):
        mock_which.return_value = None
        client = ClaudeCodeClient(binary="claude-beta")
        with pytest.raises(ClaudeCodeError, match="claude-beta"):
            client.chat(messages=[{"role": "user", "content": "x"}])

    @patch("pf_core.clients.claude_code.shutil.which")
    def test_empty_messages_raises(self, mock_which):
        mock_which.return_value = "/usr/local/bin/claude"
        client = ClaudeCodeClient()
        with pytest.raises(ClaudeCodeError, match="no usable user content"):
            client.chat(messages=[])

    @patch("pf_core.clients.claude_code.shutil.which")
    def test_only_system_no_user_raises(self, mock_which):
        mock_which.return_value = "/usr/local/bin/claude"
        client = ClaudeCodeClient()
        with pytest.raises(ClaudeCodeError, match="no usable user content"):
            client.chat(messages=[{"role": "system", "content": "rules"}])

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_timeout_raises(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=600)
        client = ClaudeCodeClient(timeout=600)
        with pytest.raises(ClaudeCodeError, match="timed out after 600s"):
            client.chat(messages=[{"role": "user", "content": "x"}])

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_per_call_timeout_overrides_default(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
        client = ClaudeCodeClient(timeout=600)
        with pytest.raises(ClaudeCodeError, match="timed out after 30s"):
            client.chat(messages=[{"role": "user", "content": "x"}], timeout=30)
        # Verify the call site used the per-call value, not the instance default
        assert mock_run.call_args.kwargs["timeout"] == 30

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_nonzero_exit_raises(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        m = MagicMock()
        m.returncode = 2
        m.stdout = ""
        m.stderr = "credentials missing"
        mock_run.return_value = m
        client = ClaudeCodeClient()
        with pytest.raises(ClaudeCodeError, match=r"exited 2"):
            client.chat(messages=[{"role": "user", "content": "x"}])

    def test_error_class_is_client_error_subclass(self):
        """Consumers catching pf_core.exceptions.ClientError (or AppError)
        should also catch ClaudeCodeError — parity with the other backends."""
        assert issubclass(ClaudeCodeError, ClientError)
        assert issubclass(ClaudeCodeError, AppError)


# ---------------------------------------------------------------------------
# A3 — retry on transient failure
# ---------------------------------------------------------------------------


class TestRetry:
    """ClaudeCodeClient(retry=N) retries non-zero exits and timeouts; a missing
    binary or empty messages raise at once."""

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_default_retry_is_zero_no_retry_on_failure(self, mock_which, mock_run, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = "transient blip"
        mock_run.return_value = m
        client = ClaudeCodeClient()  # retry default = 0
        with pytest.raises(ClaudeCodeError):
            client.chat(messages=[{"role": "user", "content": "x"}])
        assert mock_run.call_count == 1  # no retry

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_retry_one_succeeds_on_second_attempt(self, mock_which, mock_run, monkeypatch):
        """First attempt fails, second succeeds → returns content."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        fail = MagicMock()
        fail.returncode = 1
        fail.stdout = ""
        fail.stderr = "transient"
        mock_run.side_effect = [fail, _ok_run("recovered")]
        client = ClaudeCodeClient(retry=1)
        content, _usage = client.chat(messages=[{"role": "user", "content": "x"}])
        assert content == "recovered"
        assert mock_run.call_count == 2

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_retry_one_exhausted_raises_after_two_attempts(self, mock_which, mock_run, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        fail = MagicMock()
        fail.returncode = 2
        fail.stdout = ""
        fail.stderr = "still failing"
        mock_run.return_value = fail
        client = ClaudeCodeClient(retry=1)
        with pytest.raises(ClaudeCodeError):
            client.chat(messages=[{"role": "user", "content": "x"}])
        assert mock_run.call_count == 2

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_retry_two_makes_three_attempts(self, mock_which, mock_run, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        fail = MagicMock()
        fail.returncode = 1
        fail.stdout = ""
        fail.stderr = "x"
        mock_run.return_value = fail
        client = ClaudeCodeClient(retry=2)
        with pytest.raises(ClaudeCodeError):
            client.chat(messages=[{"role": "user", "content": "x"}])
        assert mock_run.call_count == 3

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_retry_also_handles_timeout(self, mock_which, mock_run, monkeypatch):
        """Timeouts can be transient (model warm-up, network blip) too —
        retry covers them as well as non-zero exits."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=600),
            _ok_run("recovered"),
        ]
        client = ClaudeCodeClient(retry=1)
        content, _ = client.chat(messages=[{"role": "user", "content": "x"}])
        assert content == "recovered"
        assert mock_run.call_count == 2

    @patch("pf_core.clients.claude_code.shutil.which")
    def test_missing_binary_not_retried(self, mock_which, monkeypatch):
        """Missing binary is a deterministic config error — retrying is
        wasted. Raise immediately even with retry>0."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = None
        client = ClaudeCodeClient(retry=3)
        with pytest.raises(ClaudeCodeError, match="not found on PATH"):
            client.chat(messages=[{"role": "user", "content": "x"}])

    @patch("pf_core.clients.claude_code.shutil.which")
    def test_empty_messages_not_retried(self, mock_which, monkeypatch):
        """Empty messages is a deterministic input error — not retryable."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        client = ClaudeCodeClient(retry=3)
        with pytest.raises(ClaudeCodeError, match="no usable user content"):
            client.chat(messages=[])

    def test_retry_via_get_client(self, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        c = get_client(retry=2)
        assert c.retry == 2

    def test_retry_default_zero(self, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        c = ClaudeCodeClient()
        assert c.retry == 0

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_preflight_inherits_retry(self, mock_which, mock_run, monkeypatch):
        """Preflight uses the same chat() path, so it benefits from
        retry — a transient auth blip won't trip a false-positive
        preflight failure when retry > 0."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        fail = MagicMock()
        fail.returncode = 1
        fail.stdout = ""
        fail.stderr = "transient"
        mock_run.side_effect = [fail, _ok_run("ok")]
        client = ClaudeCodeClient(retry=1)
        client.preflight()  # should NOT raise
        assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# A2 — preflight check
# ---------------------------------------------------------------------------


class TestPreflight:
    """ClaudeCodeClient.preflight(): fail-fast auth check before a batch."""

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_succeeds_returns_none(self, mock_which, mock_run, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()
        assert client.preflight() is None

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_uses_short_prompt(self, mock_which, mock_run, monkeypatch):
        """Preflight should send a tiny prompt — it's a smoke test, not
        a real workload. Anything more than a few chars is wasted CPU
        and tokens (and risks slow-LLM false alarms on the timeout)."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()
        client.preflight()
        prompt = mock_run.call_args.kwargs["input"]
        assert len(prompt) < 10

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_uses_configured_model(self, mock_which, mock_run, monkeypatch):
        """Preflight should exercise the same model the per-call chat()s
        will use — so a model misconfig surfaces in preflight, not in
        the first batch call."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(model="haiku")
        client.preflight()
        cmd = mock_run.call_args.args[0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "haiku"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_default_timeout_is_short(self, mock_which, mock_run, monkeypatch):
        """Default preflight timeout is short — the whole point is to
        fail fast, not spend 10 minutes hung on a logged-out session."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(timeout=600)  # long instance default
        client.preflight()
        # subprocess.run was called with the preflight's short timeout,
        # not the instance default
        assert mock_run.call_args.kwargs["timeout"] < 60

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_per_call_timeout_respected(self, mock_which, mock_run, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()
        client.preflight(timeout=5)
        assert mock_run.call_args.kwargs["timeout"] == 5

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_nonzero_exit_raises_with_login_remediation(self, mock_which, mock_run, monkeypatch):
        """Auth failure (non-zero exit) raises ClaudeCodeError with
        actionable ``<binary> /login`` text — the operator can act on
        the message without reading source."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = "Not logged in · Please run /login"
        mock_run.return_value = m
        client = ClaudeCodeClient()
        with pytest.raises(ClaudeCodeError, match=r"/login"):
            client.preflight()

    @patch("pf_core.clients.claude_code.shutil.which")
    def test_missing_binary_raises_under_preflight_wrapper(self, mock_which, monkeypatch):
        """When ``claude`` binary isn't on PATH, preflight surfaces it
        as a preflight failure (not a raw "not found" error). The
        underlying not-found message stays in the cause chain."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = None
        client = ClaudeCodeClient()
        with pytest.raises(ClaudeCodeError, match=r"preflight"):
            client.preflight()

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_timeout_wraps_into_preflight_error(self, mock_which, mock_run, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
        client = ClaudeCodeClient()
        with pytest.raises(ClaudeCodeError, match=r"preflight"):
            client.preflight()

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_error_carries_preflight_context_flag(self, mock_which, mock_run, monkeypatch):
        """The raised ClaudeCodeError has ``preflight: True`` in
        context so log filters distinguish preflight failures from
        per-call failures (different operational meaning — preflight
        means "don't even start the batch")."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = "auth error"
        mock_run.return_value = m
        client = ClaudeCodeClient()
        with pytest.raises(ClaudeCodeError) as excinfo:
            client.preflight()
        assert excinfo.value.context.get("preflight") is True


# ---------------------------------------------------------------------------
# A1 — model override (per-call --model flag)
# ---------------------------------------------------------------------------


class TestModelOverride:
    """ClaudeCodeClient honors ``model=`` (constructor or per-call), falls back to
    ``PF_CORE_CLAUDE_CODE_MODEL``, and adds no ``--model`` flag when none is set."""

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_no_model_anywhere_no_flag(self, mock_which, mock_run, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        assert "--model" not in cmd

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_instance_model_added_to_cmd(self, mock_which, mock_run, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(model="haiku")
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "haiku"
        assert cmd.index("--print") > idx  # --model lands before --print

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_per_call_model_overrides_instance(self, mock_which, mock_run, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(model="haiku")
        client.chat(messages=[{"role": "user", "content": "x"}], model="opus")
        cmd = mock_run.call_args.args[0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "opus"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_chat_default_model_falls_through_to_instance(self, mock_which, mock_run, monkeypatch):
        """``chat()`` without a ``model=`` kwarg uses the instance default
        (signature default is the empty string, which means 'no override')."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(model="haiku")
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "haiku"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_per_call_model_with_no_instance_default(self, mock_which, mock_run, monkeypatch):
        """Per-call override works even when the instance has no default."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()  # no model
        client.chat(messages=[{"role": "user", "content": "x"}], model="opus")
        cmd = mock_run.call_args.args[0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "opus"

    def test_env_var_resolves_at_init(self, monkeypatch):
        monkeypatch.setenv("PF_CORE_CLAUDE_CODE_MODEL", "haiku")
        client = ClaudeCodeClient()
        assert client.model == "haiku"

    def test_explicit_arg_wins_over_env_var(self, monkeypatch):
        monkeypatch.setenv("PF_CORE_CLAUDE_CODE_MODEL", "haiku")
        client = ClaudeCodeClient(model="opus")
        assert client.model == "opus"

    def test_no_env_no_arg_resolves_to_none(self, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        client = ClaudeCodeClient()
        assert client.model is None

    def test_get_client_passes_model(self, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        c = get_client(model="haiku")
        assert c.model == "haiku"

    def test_get_client_env_fallback(self, monkeypatch):
        monkeypatch.setenv("PF_CORE_CLAUDE_CODE_MODEL", "sonnet")
        c = get_client()
        assert c.model == "sonnet"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_openrouter_style_model_translated(self, mock_which, mock_run, monkeypatch):
        """`get_routed_client` claims backend transparency: callers should
        not have to know whether they're hitting OpenRouter or Claude Code.
        OpenRouter wants ``provider/model`` (e.g. ``anthropic/claude-3.7-sonnet``);
        Claude Code's ``--model`` wants the bare id. We strip the prefix
        so a single model string in the consumer's config works on both
        backends."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(model="anthropic/claude-3.7-sonnet")
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-3.7-sonnet"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_per_call_openrouter_style_translated(self, mock_which, mock_run, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()
        client.chat(
            messages=[{"role": "user", "content": "x"}],
            model="anthropic/claude-haiku-4-5",
        )
        cmd = mock_run.call_args.args[0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-haiku-4-5"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_bare_model_id_not_translated(self, mock_which, mock_run, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(model="claude-haiku-4-5-20251001")
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-haiku-4-5-20251001"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_multiple_slashes_takes_last_segment(self, mock_which, mock_run, monkeypatch):
        """Defensive: if a model string somehow has multiple slashes,
        take the segment after the last one (Claude Code's ids never
        contain slashes)."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(model="foo/bar/baz")
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "baz"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_empty_after_translation_drops_flag(self, mock_which, mock_run, monkeypatch):
        """A malformed string like ``"anthropic/"`` translates to ``""``;
        treat that as "no model override" and drop the flag rather than
        passing ``--model`` with an empty value."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(model="anthropic/")
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        assert "--model" not in cmd

    def test_self_model_attribute_preserves_original_string(self, monkeypatch):
        """``client.model`` keeps the as-passed string (translation only
        happens at cmd-build time). This keeps the singleton cache key
        predictable and lets debuggers see exactly what the caller passed."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        client = ClaudeCodeClient(model="anthropic/claude-3.7-sonnet")
        assert client.model == "anthropic/claude-3.7-sonnet"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_model_lands_after_extra_args(self, mock_which, mock_run, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(
            tools=["Bash"], extra_args=["--allowedTools", "Bash"], model="haiku"
        )
        client.chat(messages=[{"role": "user", "content": "x"}])
        assert _argv(mock_run.call_args.args[0]) == [
            "--safe-mode",
            "--no-session-persistence",
            "--tools",
            "Bash",
            "--system-prompt-file",
            "<file>",
            "--allowedTools",
            "Bash",
            "--model",
            "haiku",
            "--output-format",
            "json",
            "--print",
        ]


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_client_caches(self):
        a = get_client()
        b = get_client()
        assert a is b

    def test_reset_drops_singleton(self):
        a = get_client()
        reset_client()
        b = get_client()
        assert a is not b

    def test_first_call_args_used(self):
        c = get_client(timeout=42, binary="claude-canary")
        assert c.timeout == 42
        assert c.binary == "claude-canary"

    def test_other_args_get_their_own_client(self):
        first = get_client(timeout=42)
        second = get_client(timeout=99)
        assert second is not first
        assert (first.timeout, second.timeout) == (42, 99)
        assert get_client(timeout=99) is second

    def test_default_timeout_when_none_passed(self):
        c = get_client()
        assert c.timeout == DEFAULT_TIMEOUT_SECONDS

    def test_different_models_get_different_singletons(self, monkeypatch):
        """Each model gets its own singleton, so one process can pin different models."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        haiku = get_client(model="haiku")
        sonnet = get_client(model="sonnet")
        assert haiku is not sonnet
        assert haiku.model == "haiku"
        assert sonnet.model == "sonnet"

    def test_same_model_returns_same_singleton(self, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        a = get_client(model="haiku")
        b = get_client(model="haiku")
        assert a is b

    def test_no_model_and_explicit_model_are_different_singletons(self, monkeypatch):
        """`get_client()` (no model) and `get_client(model='haiku')` are
        two distinct cache slots even when the env happens to resolve
        the no-model path to 'haiku'. Predictable > clever."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        no_model = get_client()
        haiku = get_client(model="haiku")
        assert no_model is not haiku
        assert no_model.model is None
        assert haiku.model == "haiku"

    def test_reset_drops_all_per_model_singletons(self, monkeypatch):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        haiku = get_client(model="haiku")
        sonnet = get_client(model="sonnet")
        reset_client()
        assert get_client(model="haiku") is not haiku
        assert get_client(model="sonnet") is not sonnet
