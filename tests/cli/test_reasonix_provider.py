"""Tests for the Reasonix CLI wrapper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.cli.base import CLIConfig, CLIResponse
from ductor_bot.cli.reasonix_provider import ReasonixCLI, _clean_reasonix_output, _extract_cost
from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent


@pytest.fixture
def reasonix_config(tmp_path: Path) -> CLIConfig:
    return CLIConfig(
        provider="reasonix",
        working_dir=str(tmp_path),
        model="deepseek-v4-flash",
        system_prompt="You are a test assistant.",
        reasoning_effort="high",
        max_budget_usd=1.0,
        cli_parameters=["--no-config"],
    )


@pytest.fixture
def reasonix_cli(reasonix_config: CLIConfig) -> ReasonixCLI:
    with patch.object(ReasonixCLI, "_find_cli", return_value="/usr/bin/reasonix"):
        return ReasonixCLI(reasonix_config)


def test_find_cli_prefers_path() -> None:
    with patch("ductor_bot.cli.reasonix_provider.which", return_value="/path/reasonix"):
        assert ReasonixCLI._find_cli() == "/path/reasonix"


def test_find_cli_fallback_to_known_nvm_path(tmp_path: Path) -> None:
    fallback = tmp_path / "reasonix"
    fallback.write_text("#!/bin/sh\n")
    fallback.chmod(0o755)
    from ductor_bot.cli import reasonix_provider

    with (
        patch("ductor_bot.cli.reasonix_provider.which", return_value=None),
        patch.object(reasonix_provider, "DEFAULT_REASONIX_BINARY", fallback),
    ):
        assert ReasonixCLI._find_cli() == str(fallback)


def test_find_cli_raises_when_missing() -> None:
    from ductor_bot.cli import reasonix_provider

    nonexistent = Path("/nonexistent/reasonix")
    with (
        patch("ductor_bot.cli.reasonix_provider.which", return_value=None),
        patch.object(reasonix_provider, "DEFAULT_REASONIX_BINARY", nonexistent),
        pytest.raises(FileNotFoundError),
    ):
        ReasonixCLI._find_cli()


def test_build_command(reasonix_cli: ReasonixCLI) -> None:
    cmd = reasonix_cli._build_command("ping")
    assert cmd[0] == "/usr/bin/reasonix"
    assert cmd[1] == "run"
    assert "--no-config" in cmd
    assert "--model" in cmd
    assert "deepseek-v4-flash" in cmd
    assert "--system" in cmd
    assert "You are a test assistant." in cmd
    assert "--effort" in cmd
    assert "high" in cmd
    assert "--budget" in cmd
    assert "1.0" in cmd
    assert cmd[-1] == "ping"


def test_build_command_no_default_effort(tmp_path: Path) -> None:
    config = CLIConfig(
        provider="reasonix",
        working_dir=str(tmp_path),
        model="deepseek-v4-flash",
        reasoning_effort="medium",
    )
    with patch.object(ReasonixCLI, "_find_cli", return_value="/usr/bin/reasonix"):
        cli = ReasonixCLI(config)
    cmd = cli._build_command("ping")
    assert "--effort" not in cmd


def test_build_command_resume_session(tmp_path: Path) -> None:
    config = CLIConfig(provider="reasonix", working_dir=str(tmp_path))
    with patch.object(ReasonixCLI, "_find_cli", return_value="/usr/bin/reasonix"):
        cli = ReasonixCLI(config)
    cmd = cli._build_command("hi", resume_session="/tmp/transcript.json")
    assert "--transcript" in cmd
    assert "/tmp/transcript.json" in cmd


def test_build_command_continue_session(tmp_path: Path) -> None:
    config = CLIConfig(provider="reasonix", working_dir=str(tmp_path))
    with patch.object(ReasonixCLI, "_find_cli", return_value="/usr/bin/reasonix"):
        cli = ReasonixCLI(config)
    cmd = cli._build_command("hi", continue_session=True)
    assert "--continue" in cmd


def test_parse_response_success(reasonix_cli: ReasonixCLI) -> None:
    raw = (
        "⌘ MCP · filesystem      ↻ handshake…   initialise → tools/list\n"
        "pong 🏓\n"
        "\n"
        "— turns:1 cache:99.9% cost:$0.000046 save-vs-claude:99.9%\n"
    )
    response = reasonix_cli._parse_response(raw.encode(), b"", 0)
    assert response.result == "pong 🏓"
    assert response.total_cost_usd == 0.000046
    assert response.is_error is False


def test_parse_response_error(reasonix_cli: ReasonixCLI) -> None:
    response = reasonix_cli._parse_response(b"", b"connection refused", 1)
    assert response.returncode == 1
    assert response.is_error is True
    assert response.result == "connection refused"


def test_clean_reasonix_output_strips_progress_and_footer() -> None:
    raw = (
        "⌘ MCP · filesystem      ↻ handshake…\n"
        "✓ connected\n"
        "The answer is 42.\n"
        "— turns:1 cache:99.9% cost:$0.001\n"
    )
    assert _clean_reasonix_output(raw) == "The answer is 42."


def test_extract_cost() -> None:
    assert _extract_cost("cost: $1.23") == 1.23
    assert _extract_cost("cost:$0.000046") == 0.000046
    assert _extract_cost("no cost here") is None


@pytest.mark.asyncio
async def test_send_uses_run_oneshot_subprocess(reasonix_cli: ReasonixCLI) -> None:
    response = CLIResponse(result="pong 🏓", returncode=0)
    with patch(
        "ductor_bot.cli.reasonix_provider.run_oneshot_subprocess",
        new=AsyncMock(return_value=response),
    ) as mock_run:
        result = await reasonix_cli.send("ping")
    assert result is response
    mock_run.assert_awaited_once()
    call_kwargs = mock_run.await_args.kwargs
    assert call_kwargs["config"] is reasonix_cli._config
    spec = call_kwargs["spec"]
    assert spec.use_cwd == str(reasonix_cli._working_dir)
    assert spec.prompt == "ping"
    assert spec.exec_cmd[0] == "/usr/bin/reasonix"
    assert spec.exec_cmd[-1] == "ping"
    assert call_kwargs["provider_label"] == "Reasonix"


@pytest.mark.asyncio
async def test_send_streaming_emits_delta_and_result(reasonix_cli: ReasonixCLI) -> None:
    response = CLIResponse(result="pong 🏓", returncode=0)
    with patch.object(reasonix_cli, "send", new=AsyncMock(return_value=response)):
        events = [event async for event in reasonix_cli.send_streaming("ping")]
    assert len(events) == 2
    assert isinstance(events[0], AssistantTextDelta)
    assert events[0].text == "pong 🏓"
    assert isinstance(events[1], ResultEvent)
    assert events[1].result == "pong 🏓"
