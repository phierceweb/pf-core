"""Retry-backoff regression for pf_core.clients.claude_code.

The retry loop re-invoked ``claude --print`` with zero delay, so a
rate-limited or refreshing session got hammered inside the same window.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pf_core.clients.claude_code import ClaudeCodeClient, ClaudeCodeError, reset_client


@pytest.fixture(autouse=True)
def _reset():
    reset_client()
    yield
    reset_client()


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("pf_core.clients.claude_code.time.sleep") as sleep:
        yield sleep


def _ok_run(stdout: str = "ok") -> MagicMock:
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout
    m.stderr = ""
    return m


def _fail_run(returncode: int = 1) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = ""
    m.stderr = "transient"
    return m


class TestRetryBacksOff:
    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_sleeps_between_nonzero_exit_retries(self, mock_which, mock_run, _no_sleep):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _fail_run()
        client = ClaudeCodeClient(retry=2)
        with pytest.raises(ClaudeCodeError):
            client.chat(messages=[{"role": "user", "content": "x"}])
        delays = [c.args[0] for c in _no_sleep.call_args_list]
        assert len(delays) == 2
        assert all(d > 0 for d in delays)
        assert delays[1] > delays[0]

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_sleeps_between_timeout_retries(self, mock_which, mock_run, _no_sleep):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=5)
        client = ClaudeCodeClient(retry=2)
        with pytest.raises(ClaudeCodeError):
            client.chat(messages=[{"role": "user", "content": "x"}])
        delays = [c.args[0] for c in _no_sleep.call_args_list]
        assert len(delays) == 2
        assert all(d > 0 for d in delays)

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_no_sleep_after_last_attempt(self, mock_which, mock_run, _no_sleep):
        """The final failure raises — sleeping after it just delays the error."""
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.side_effect = [_fail_run(), _ok_run("recovered")]
        client = ClaudeCodeClient(retry=1)
        content, _ = client.chat(messages=[{"role": "user", "content": "x"}])
        assert content == "recovered"
        assert _no_sleep.call_count == 1

    @patch("pf_core.clients.claude_code.subprocess.run")
    @patch("pf_core.clients.claude_code.shutil.which")
    def test_no_sleep_on_success(self, mock_which, mock_run, _no_sleep):
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.return_value = _ok_run()
        client = ClaudeCodeClient(retry=2)
        client.chat(messages=[{"role": "user", "content": "x"}])
        _no_sleep.assert_not_called()
