"""Tests for how pf_core.clients.claude_code sends tools and the system prompt."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pf_core.clients import routing
from pf_core.clients._claude_code_wire import (
    APPEND_SYSTEM_PROMPT_FILE_FLAG,
    SYSTEM_PROMPT_FILE_FLAG,
    checked_tools,
    has_flag,
    split_messages,
    stdin_and_system_flag,
    system_prompt_file,
    tool_args,
)
from pf_core.clients.claude_code import (
    ClaudeCodeClient,
    ClaudeCodeError,
    get_client,
    new_client,
    reset_client,
)
from pf_core.exceptions import ConfigurationError

SYSTEM_AND_USER = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "summarize this"},
]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("PF_CORE_CLAUDE_CODE_MODEL", raising=False)
    reset_client()
    routing.clear_client_cache()
    yield
    reset_client()
    routing.clear_client_cache()


def _ok(result: str = "ok") -> MagicMock:
    m = MagicMock()
    m.returncode = 0
    m.stdout = json.dumps({"result": result, "is_error": False, "stop_reason": "end_turn"})
    m.stderr = ""
    return m


def _file_after(cmd: list[str], flag: str) -> str | None:
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


class _Recorder:
    """``subprocess.run`` stand-in recording each call's argv and system-prompt file."""

    def __init__(self, *results):
        self.results = list(results) or [_ok()]
        self.calls: list[dict] = []

    def __call__(self, cmd, **kwargs):
        path = _file_after(cmd, SYSTEM_PROMPT_FILE_FLAG) or _file_after(
            cmd, APPEND_SYSTEM_PROMPT_FILE_FLAG
        )
        entry = {"cmd": cmd, "input": kwargs.get("input"), "path": path, "system": None}
        if path:
            with open(path, encoding="utf-8") as fh:
                entry["system"] = fh.read()
            entry["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        self.calls.append(entry)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return result


def _chat(recorder: _Recorder, messages=SYSTEM_AND_USER, **client_kwargs):
    with (
        patch("pf_core.clients.claude_code.shutil.which", return_value="/usr/bin/claude"),
        patch("pf_core.clients.claude_code.subprocess.run", side_effect=recorder),
        patch("pf_core.clients.claude_code.time.sleep"),
    ):
        return ClaudeCodeClient(**client_kwargs).chat(messages=messages)


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


class TestSplitMessages:
    def test_system_and_body_apart(self):
        assert split_messages(SYSTEM_AND_USER) == ("be brief", "summarize this")

    def test_each_side_joined_in_order_with_blank_lines(self):
        messages = [
            {"role": "system", "content": "rule one"},
            {"role": "user", "content": "first"},
            {"role": "System", "content": "rule two"},
            {"role": "assistant", "content": "second"},
        ]
        assert split_messages(messages) == ("rule one\n\nrule two", "first\n\nsecond")

    def test_empty_content_skipped_and_none_tolerated(self):
        assert split_messages([{"role": "system", "content": ""}, {"role": "user"}]) == ("", "")
        assert split_messages(None) == ("", "")


class TestHasFlag:
    def test_space_and_equals_forms(self):
        assert has_flag(["--tools", "Read"], ("--tools",))
        assert has_flag(["--tools=Read"], ("--tools",))

    def test_a_longer_flag_sharing_the_prefix_is_not_a_match(self):
        assert not has_flag(["--tools-extra"], ("--tools",))
        assert not has_flag(["--system-prompt-file", "f"], ("--system-prompt",))


class TestToolArgs:
    def test_no_tools_removes_built_in_and_mcp_tools(self):
        assert tool_args([]) == ["--tools", "", "--strict-mcp-config"]

    def test_named_tools_are_one_comma_joined_value(self):
        assert tool_args(["Read", "Grep"]) == ["--tools", "Read,Grep"]


class TestCheckedTools:
    def test_returns_a_list(self):
        assert checked_tools(("Read",), []) == ["Read"]
        assert checked_tools(None, []) == []

    def test_string_is_refused(self):
        with pytest.raises(ConfigurationError, match="list of tool names"):
            checked_tools("Read", [])

    @pytest.mark.parametrize(
        "extra_args",
        [["--allowedTools", "Read"], ["--allowed-tools", "Read"], ["--allowedTools=Read"]],
    )
    def test_granting_tools_without_tools_is_refused(self, extra_args):
        with pytest.raises(ConfigurationError, match=r"tools=\[\.\.\.\]"):
            checked_tools((), extra_args)

    def test_grant_with_tools_is_fine(self):
        assert checked_tools(["Read"], ["--allowedTools", "Read"]) == ["Read"]

    def test_grant_with_consumer_tools_flag_is_fine(self):
        assert checked_tools((), ["--tools", "", "--allowedTools", "mcp__x__y"]) == []

    def test_tools_in_both_places_is_refused(self):
        with pytest.raises(ConfigurationError, match="not both"):
            checked_tools(["Read"], ["--tools", "Grep"])

    def test_mcp_names_are_refused(self):
        with pytest.raises(ConfigurationError, match="isolate=False"):
            checked_tools(["Read", "mcp__example__query"], [])


class TestStdinAndSystemFlag:
    def _route(self, extra_args=(), agent_prompt=False, messages=SYSTEM_AND_USER):
        system, body = split_messages(messages)
        return stdin_and_system_flag(
            messages, system, body, extra_args=list(extra_args), agent_prompt=agent_prompt
        )

    def test_default_replaces_the_agent_prompt(self):
        assert self._route() == ("summarize this", SYSTEM_PROMPT_FILE_FLAG)

    def test_default_replaces_it_even_without_system_messages(self):
        user_only = [{"role": "user", "content": "hi"}]
        assert self._route(messages=user_only) == ("hi", SYSTEM_PROMPT_FILE_FLAG)

    def test_agent_prompt_appends(self):
        assert self._route(agent_prompt=True) == (
            "summarize this",
            APPEND_SYSTEM_PROMPT_FILE_FLAG,
        )

    def test_agent_prompt_without_system_messages_sends_no_file(self):
        user_only = [{"role": "user", "content": "hi"}]
        assert self._route(agent_prompt=True, messages=user_only) == ("hi", None)

    @pytest.mark.parametrize("agent_prompt", [False, True])
    @pytest.mark.parametrize(
        "extra_args",
        [["--system-prompt", "x"], ["--system-prompt-file", "f"], ["--system-prompt=x"]],
    )
    def test_consumer_replace_flag_flattens_onto_stdin(self, extra_args, agent_prompt):
        assert self._route(extra_args=extra_args, agent_prompt=agent_prompt) == (
            "be brief\n\n---\n\nsummarize this",
            None,
        )

    @pytest.mark.parametrize(
        "extra_args", [["--append-system-prompt", "x"], ["--append-system-prompt-file", "f"]]
    )
    def test_consumer_append_flag_composes_with_the_replacement(self, extra_args):
        assert self._route(extra_args=extra_args) == ("summarize this", SYSTEM_PROMPT_FILE_FLAG)

    @pytest.mark.parametrize(
        "extra_args", [["--append-system-prompt", "x"], ["--append-system-prompt-file=f"]]
    )
    def test_consumer_append_flag_takes_the_one_append_slot(self, extra_args):
        assert self._route(extra_args=extra_args, agent_prompt=True) == (
            "be brief\n\n---\n\nsummarize this",
            None,
        )


class TestSystemPromptFile:
    def test_private_file_holding_the_text_removed_on_exit(self):
        with system_prompt_file("rules — ünïcode") as path:
            with open(path, encoding="utf-8") as fh:
                assert fh.read() == "rules — ünïcode"
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert not os.path.exists(path)

    def test_removed_when_the_block_raises(self):
        with pytest.raises(RuntimeError):
            with system_prompt_file("x") as path:
                raise RuntimeError("boom")
        assert not os.path.exists(path)

    def test_already_removed_file_is_not_an_error(self):
        with system_prompt_file("x") as path:
            os.unlink(path)


# ---------------------------------------------------------------------------
# ClaudeCodeClient
# ---------------------------------------------------------------------------


class TestCompletionDefaults:
    def test_argv(self):
        rec = _Recorder()
        _chat(rec)
        cmd = rec.calls[0]["cmd"]
        assert cmd[1:9] == [
            "--safe-mode",
            "--no-session-persistence",
            "--tools",
            "",
            "--strict-mcp-config",
            "--system-prompt-file",
            rec.calls[0]["path"],
            "--output-format",
        ]
        assert cmd[-2:] == ["json", "--print"]

    def test_system_in_a_private_file_body_on_stdin(self):
        rec = _Recorder()
        content, _ = _chat(rec)
        assert content == "ok"
        assert rec.calls[0]["system"] == "be brief"
        assert rec.calls[0]["mode"] == 0o600
        assert rec.calls[0]["input"] == "summarize this"

    def test_file_removed_after_the_call(self):
        rec = _Recorder()
        _chat(rec)
        assert not os.path.exists(rec.calls[0]["path"])

    def test_no_system_messages_sends_an_empty_system_prompt(self):
        rec = _Recorder()
        _chat(rec, messages=[{"role": "user", "content": "hi"}])
        assert rec.calls[0]["system"] == ""
        assert rec.calls[0]["input"] == "hi"

    def test_large_system_prompt_stays_out_of_argv(self):
        rec = _Recorder()
        big = "s" * 2_000_000
        _chat(rec, messages=[{"role": "system", "content": big}, {"role": "user", "content": "u"}])
        assert all(len(arg) < 1000 for arg in rec.calls[0]["cmd"])
        assert rec.calls[0]["system"] == big

    def test_attributes(self):
        client = ClaudeCodeClient()
        assert client.tools == []
        assert client.agent_prompt is False


class TestToolsOption:
    def test_named_tools_without_strict_mcp(self):
        rec = _Recorder()
        _chat(rec, tools=["Read", "Grep"])
        cmd = rec.calls[0]["cmd"]
        assert cmd[cmd.index("--tools") + 1] == "Read,Grep"
        assert "--strict-mcp-config" not in cmd

    @pytest.mark.parametrize("extra_args", [["--tools", "Read"], ["--tools=Read"]])
    def test_consumer_tools_flag_replaces_the_clients(self, extra_args):
        rec = _Recorder()
        _chat(rec, extra_args=extra_args)
        cmd = rec.calls[0]["cmd"]
        assert [a for a in cmd if a.startswith("--tools")] == [extra_args[0]]
        assert "" not in cmd
        assert "--strict-mcp-config" not in cmd

    def test_allowed_tools_alone_fails_at_construction(self):
        with pytest.raises(ConfigurationError, match="--allowedTools"):
            ClaudeCodeClient(extra_args=["--allowedTools", "Read,Grep"])
        with pytest.raises(ConfigurationError):
            get_client(extra_args=["--allowedTools", "Read"])
        with pytest.raises(ConfigurationError):
            new_client(extra_args=["--allowedTools", "Read"])


class TestAgentPrompt:
    def test_argv(self):
        rec = _Recorder()
        _chat(rec, agent_prompt=True, tools=["Read"], extra_args=["--allowedTools", "Read"])
        assert rec.calls[0]["cmd"][1:] == [
            "--safe-mode",
            "--no-session-persistence",
            "--tools",
            "Read",
            APPEND_SYSTEM_PROMPT_FILE_FLAG,
            rec.calls[0]["path"],
            "--allowedTools",
            "Read",
            "--output-format",
            "json",
            "--print",
        ]
        assert rec.calls[0]["system"] == "be brief"
        assert rec.calls[0]["input"] == "summarize this"

    def test_without_tools_still_no_tools(self):
        rec = _Recorder()
        _chat(rec, agent_prompt=True)
        cmd = rec.calls[0]["cmd"]
        assert cmd[cmd.index("--tools") : cmd.index("--tools") + 3] == [
            "--tools",
            "",
            "--strict-mcp-config",
        ]

    def test_without_system_messages_no_prompt_flag(self):
        rec = _Recorder()
        _chat(rec, messages=[{"role": "user", "content": "hi"}], agent_prompt=True)
        cmd = rec.calls[0]["cmd"]
        assert SYSTEM_PROMPT_FILE_FLAG not in cmd
        assert APPEND_SYSTEM_PROMPT_FILE_FLAG not in cmd

    def test_consumer_append_flag_takes_the_one_append_slot(self):
        rec = _Recorder()
        _chat(rec, agent_prompt=True, extra_args=["--append-system-prompt", "extra"])
        cmd = rec.calls[0]["cmd"]
        assert rec.calls[0]["path"] is None
        assert [a for a in cmd if "system-prompt" in a] == ["--append-system-prompt"]
        assert rec.calls[0]["input"] == "be brief\n\n---\n\nsummarize this"


class TestConsumerSystemPromptFlags:
    def test_append_flag_composes_with_the_replacement(self):
        rec = _Recorder()
        _chat(rec, extra_args=["--append-system-prompt", "extra"])
        cmd = rec.calls[0]["cmd"]
        assert [a for a in cmd if "system-prompt" in a] == [
            SYSTEM_PROMPT_FILE_FLAG,
            "--append-system-prompt",
        ]
        assert rec.calls[0]["system"] == "be brief"
        assert rec.calls[0]["input"] == "summarize this"

    @pytest.mark.parametrize("agent_prompt", [False, True])
    def test_replace_flag_owns_the_system_prompt(self, agent_prompt):
        rec = _Recorder()
        _chat(rec, agent_prompt=agent_prompt, extra_args=["--system-prompt", "mine"])
        cmd = rec.calls[0]["cmd"]
        assert rec.calls[0]["path"] is None
        assert [a for a in cmd if "system-prompt" in a] == ["--system-prompt"]
        assert rec.calls[0]["input"] == "be brief\n\n---\n\nsummarize this"


class TestFileLifecycle:
    def test_same_file_for_every_retry_then_removed(self):
        fail = MagicMock(returncode=1, stdout="", stderr="rate limited")
        rec = _Recorder(fail, _ok())
        _chat(rec, retry=1)
        assert len(rec.calls) == 2
        assert rec.calls[0]["path"] == rec.calls[1]["path"]
        assert rec.calls[1]["system"] == "be brief"
        assert not os.path.exists(rec.calls[0]["path"])

    def test_removed_after_a_failed_call(self):
        rec = _Recorder(MagicMock(returncode=2, stdout="", stderr="boom"))
        with pytest.raises(ClaudeCodeError):
            _chat(rec)
        assert not os.path.exists(rec.calls[0]["path"])

    def test_removed_after_a_timeout(self):
        rec = _Recorder(subprocess.TimeoutExpired(cmd="claude", timeout=1))
        with pytest.raises(ClaudeCodeError, match="timed out"):
            _chat(rec)
        assert not os.path.exists(rec.calls[0]["path"])

    def test_no_file_created_when_messages_are_rejected(self):
        with (
            patch("pf_core.clients.claude_code.shutil.which", return_value="/usr/bin/claude"),
            patch("pf_core.clients.claude_code.system_prompt_file") as spf,
            pytest.raises(ClaudeCodeError, match="no usable user content"),
        ):
            ClaudeCodeClient().chat(messages=[{"role": "system", "content": "only"}])
        spf.assert_not_called()


class TestConstruction:
    def test_get_client_shares_one_client_per_argument_set(self):
        client = get_client(model="haiku", tools=["Read"], agent_prompt=True)
        assert (client.tools, client.agent_prompt) == (["Read"], True)
        assert get_client(model="haiku", tools=("Read",), agent_prompt=True) is client

    def test_get_client_never_hands_one_callers_grant_to_another(self):
        granted = get_client(tools=["Bash"], extra_args=["--allowedTools", "Bash"], isolate=False)
        plain = get_client()
        assert plain is not granted
        assert (plain.tools, plain.extra_args, plain.isolate) == ([], [], True)
        assert routing.get_client_for_backend("claude_code") is plain
        assert get_client(model="haiku").tools == []

    def test_get_client_refuses_tools_given_as_a_string(self):
        with pytest.raises(ConfigurationError, match="list of tool names"):
            get_client(tools="Read")

    def test_new_client_passes_options(self):
        client = new_client(tools=("Read",), agent_prompt=True)
        assert (client.tools, client.agent_prompt) == (["Read"], True)

    def test_router_client_kwargs_reach_the_client(self):
        client = routing.get_client_for_backend(
            "claude_code", tools=["Read", "Grep"], agent_prompt=True
        )
        assert isinstance(client, ClaudeCodeClient)
        assert (client.tools, client.agent_prompt) == (["Read", "Grep"], True)
        assert (
            routing.get_client_for_backend("claude_code", tools=["Read", "Grep"], agent_prompt=True)
            is client
        )

    def test_preflight_runs_as_a_plain_completion(self):
        rec = _Recorder()
        with (
            patch("pf_core.clients.claude_code.shutil.which", return_value="/usr/bin/claude"),
            patch("pf_core.clients.claude_code.subprocess.run", side_effect=rec),
        ):
            ClaudeCodeClient().preflight()
        cmd = rec.calls[0]["cmd"]
        assert cmd[cmd.index("--tools") + 1] == ""
        assert rec.calls[0]["system"] == ""
