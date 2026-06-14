"""Tests for pf_core.clients.claude_code."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pf_core.clients.claude_code import (
    ClaudeCodeClient,
    ClaudeCodeError,
    DEFAULT_TIMEOUT_SECONDS,
    _flatten_messages,
    get_client,
    new_client,
    reset_client,
)
from pf_core.exceptions import AppError


@pytest.fixture(autouse=True)
def _reset():
    reset_client()
    yield
    reset_client()


# ---------------------------------------------------------------------------
# _flatten_messages
# ---------------------------------------------------------------------------


class TestFlattenMessages:
    def test_user_only(self):
        assert _flatten_messages([{"role": "user", "content": "hi"}]) == "hi"

    def test_system_then_user(self):
        out = _flatten_messages([
            {"role": "system", "content": "You are a bot."},
            {"role": "user", "content": "Hello."},
        ])
        assert out == "You are a bot.\n\n---\n\nHello."

    def test_multiple_user_messages_joined(self):
        out = _flatten_messages([
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ])
        assert out == "first\n\nsecond"

    def test_multiple_system_messages_joined(self):
        out = _flatten_messages([
            {"role": "system", "content": "rule one"},
            {"role": "system", "content": "rule two"},
            {"role": "user", "content": "do thing"},
        ])
        assert out == "rule one\n\nrule two\n\n---\n\ndo thing"

    def test_assistant_message_treated_as_body(self):
        out = _flatten_messages([
            {"role": "user", "content": "Q?"},
            {"role": "assistant", "content": "A."},
            {"role": "user", "content": "Q2?"},
        ])
        assert "Q?" in out and "A." in out and "Q2?" in out

    def test_empty_messages_list(self):
        assert _flatten_messages([]) == ""

    def test_none_messages(self):
        assert _flatten_messages(None) == ""  # type: ignore[arg-type]

    def test_skips_empty_content(self):
        out = _flatten_messages([
            {"role": "system", "content": ""},
            {"role": "user", "content": "real content"},
        ])
        assert out == "real content"

    def test_case_insensitive_role(self):
        out = _flatten_messages([
            {"role": "SYSTEM", "content": "S"},
            {"role": "User", "content": "U"},
        ])
        assert out == "S\n\n---\n\nU"


# ---------------------------------------------------------------------------
# ClaudeCodeClient.chat
# ---------------------------------------------------------------------------


def _ok_run(stdout: str = "ok response") -> MagicMock:
    """A fake completed subprocess result with stdout + zero returncode."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout
    m.stderr = ""
    return m


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
            "prompt_tokens", "completion_tokens",
            "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens", "cost_usd", "duration_ms",
            "system_fingerprint",
        ):
            assert key in usage
        assert usage["prompt_tokens"] == 0
        assert usage["completion_tokens"] == 0
        assert usage["cost_usd"] == 0.0
        assert usage["duration_ms"] >= 0

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_passes_flattened_prompt_to_subprocess(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient()
        client.chat(messages=[
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "summarize this"},
        ])
        cmd = mock_run.call_args.args[0]
        # Prompt is piped via stdin, not argv — argv has an OS hard limit
        # (ARG_MAX) that large prompts trip over. `--print` is the final
        # argv element; there is no positional prompt after it.
        assert cmd[-1] == "--print"
        assert mock_run.call_args.kwargs["input"] == "be brief\n\n---\n\nsummarize this"

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_large_prompt_uses_stdin_not_argv(self, mock_which, mock_run):
        """A multi-megabyte prompt must not appear in argv, where it would
        trip ARG_MAX (E2BIG) on macOS/Linux. Regression for a log-analysis
        CLI's report stage, which can render a prompt larger than 256 KB."""
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
    def test_extra_args_inserted_before_print(self, mock_which, mock_run):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(extra_args=["--allowedTools", "Bash"])
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        # --safe-mode leads (isolate defaults True); extra_args follow, before --print.
        assert cmd[1:5] == ["--safe-mode", "--allowedTools", "Bash", "--print"]

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
    switches to (billable, and possibly invalid) external API-key auth.

    Regression guard: a key in a consumer project's ``.env`` made every
    claude_code call fail auth while the Max session was perfectly
    healthy."""

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
    """``claude --print`` runs in the caller's working directory, so without
    isolation it auto-loads the surrounding project's CLAUDE.md, skills,
    hooks, and plugins. A weaker model then sometimes OBEYS them — a live
    vision call inside a repo was hijacked into emitting skill text where an
    image caption belonged. ``--safe-mode`` ("start with all customizations
    off"; auth, model, and explicit flags still apply) strips that ambient
    context. It is ON by default — a programmatic library call almost never
    wants the surrounding repo's instructions — with an ``isolate=False``
    opt-out for the rare consumer that deliberately runs claude inside a
    project to use that project's customizations."""

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
    def test_safe_mode_coexists_with_extra_args_and_model(
        self, mock_which, mock_run, monkeypatch
    ):
        """Canonical argv with everything set:
        ``[binary, --safe-mode, *extra_args, --model X, --print]``. ``--safe-mode``
        strips ambient customizations but explicit flags (--allowedTools,
        --model) still apply, so they coexist."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(extra_args=["--allowedTools", "Bash"], model="haiku")
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        assert cmd[1:7] == [
            "--safe-mode", "--allowedTools", "Bash", "--model", "haiku", "--print",
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

    def test_error_class_is_app_error_subclass(self):
        """Consumers catching pf_core.exceptions.AppError should also
        catch ClaudeCodeError."""
        assert issubclass(ClaudeCodeError, AppError)


# ---------------------------------------------------------------------------
# A3 — retry on transient failure
# ---------------------------------------------------------------------------


class TestRetry:
    """A3a: ClaudeCodeClient(retry=N) — auto-retry on non-zero exit and
    timeout. Cheap insurance against transient session blips
    (rate-limit windows, momentary auth refresh). Default ``retry=0``
    preserves the pre-A3 behavior of raising on the first failure.

    Missing binary and empty-messages are NOT retryable (deterministic
    config errors); only subprocess failures are."""

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_default_retry_is_zero_no_retry_on_failure(
        self, mock_which, mock_run, monkeypatch
    ):
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
    def test_retry_one_succeeds_on_second_attempt(
        self, mock_which, mock_run, monkeypatch
    ):
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
    def test_retry_one_exhausted_raises_after_two_attempts(
        self, mock_which, mock_run, monkeypatch
    ):
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
    def test_retry_two_makes_three_attempts(
        self, mock_which, mock_run, monkeypatch
    ):
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
    def test_retry_also_handles_timeout(
        self, mock_which, mock_run, monkeypatch
    ):
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
    def test_preflight_inherits_retry(
        self, mock_which, mock_run, monkeypatch
    ):
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
    """A2: ClaudeCodeClient.preflight() — fail-fast auth check before
    launching long batches. Issues a tiny ``claude --print "ok"`` and
    raises ClaudeCodeError with an actionable ``<binary> /login``
    remediation message on any failure (auth, missing binary, timeout).
    A consumer project originally caught this pattern: 1180 parallel
    calls burning ~10 minutes of wall-clock before "Not logged in ·
    Please run /login" became visible. With preflight, the same condition
    surfaces in single-digit seconds with a clear remediation."""

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
    def test_nonzero_exit_raises_with_login_remediation(
        self, mock_which, mock_run, monkeypatch
    ):
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
    def test_missing_binary_raises_under_preflight_wrapper(
        self, mock_which, monkeypatch
    ):
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
    def test_timeout_wraps_into_preflight_error(
        self, mock_which, mock_run, monkeypatch
    ):
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
        client = ClaudeCodeClient()
        with pytest.raises(ClaudeCodeError, match=r"preflight"):
            client.preflight()

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_error_carries_preflight_context_flag(
        self, mock_which, mock_run, monkeypatch
    ):
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
    """A1: ClaudeCodeClient honors ``model=`` (constructor or per-call), falls
    back to ``PF_CORE_CLAUDE_CODE_MODEL`` env var, and adds nothing when
    none is set (preserves pre-v0.22 behavior of letting the active session
    decide). Without the flag, ``claude --print`` runs against the user's
    interactive session model — fine for local Claude Max users but
    devastating for batch consumers (a batch pipeline ran into
    this) where calls silently land on Sonnet/Opus and chew through
    quota."""

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
    def test_chat_default_model_falls_through_to_instance(
        self, mock_which, mock_run, monkeypatch
    ):
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
    def test_per_call_model_with_no_instance_default(
        self, mock_which, mock_run, monkeypatch
    ):
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
    def test_openrouter_style_model_translated(
        self, mock_which, mock_run, monkeypatch
    ):
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
    def test_per_call_openrouter_style_translated(
        self, mock_which, mock_run, monkeypatch
    ):
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
    def test_multiple_slashes_takes_last_segment(
        self, mock_which, mock_run, monkeypatch
    ):
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
    def test_empty_after_translation_drops_flag(
        self, mock_which, mock_run, monkeypatch
    ):
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
        """Order: [binary, --safe-mode, *extra_args, --model X, --print, prompt].
        Putting --model in extra_args manually still works (caller's choice),
        but the per-instance/per-call model uses this canonical position so
        callers don't accidentally double up."""
        monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run("ok")
        client = ClaudeCodeClient(
            extra_args=["--allowedTools", "Bash"], model="haiku"
        )
        client.chat(messages=[{"role": "user", "content": "x"}])
        cmd = mock_run.call_args.args[0]
        # Expected: [binary, --safe-mode, --allowedTools, Bash, --model, haiku, --print, prompt]
        assert cmd[1:7] == [
            "--safe-mode", "--allowedTools", "Bash", "--model", "haiku", "--print",
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

    def test_subsequent_args_ignored(self):
        first = get_client(timeout=42)
        second = get_client(timeout=99)  # ignored
        assert second is first
        assert second.timeout == 42

    def test_default_timeout_when_none_passed(self):
        c = get_client()
        assert c.timeout == DEFAULT_TIMEOUT_SECONDS

    def test_different_models_get_different_singletons(self, monkeypatch):
        """Per-task model pinning needs distinct singletons. A document-
        pipeline consumer wants `get_client(model='sonnet')` for markdown
        analysis AND `get_client(model='haiku')` for vision in the same
        process."""
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

    def test_no_model_and_explicit_model_are_different_singletons(
        self, monkeypatch
    ):
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
