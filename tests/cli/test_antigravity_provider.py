"""Tests for Antigravity CLI provider integration."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from ductor_bot.cli.antigravity_events import parse_antigravity_json
from ductor_bot.cli.antigravity_provider import AntigravityCLI
from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent
from ductor_bot.config import ModelRegistry


def test_antigravity_batch_json_extracts_common_content_keys() -> None:
    assert parse_antigravity_json('{"result":"ok"}') == "ok"
    assert parse_antigravity_json("plain") == "plain"


def test_antigravity_command_uses_print_and_conversation() -> None:
    cli = AntigravityCLI(CLIConfig(provider="antigravity", model="antigravity-default"))

    cmd = cli._build_command(resume_session="conv-1")

    assert cmd[:2] == ["agy", "--print"]
    assert "--model" not in cmd
    assert "--conversation" in cmd
    assert "conv-1" in cmd


def test_antigravity_command_includes_selected_model() -> None:
    cli = AntigravityCLI(CLIConfig(provider="antigravity", model="claude-sonnet-4-5"))

    cmd = cli._build_command()

    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-5"


def test_antigravity_command_continue_and_bypass() -> None:
    cli = AntigravityCLI(CLIConfig(provider="antigravity", permission_mode="bypassPermissions"))

    cmd = cli._build_command(continue_session=True)

    assert "--continue" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--prompt-interactive" not in cmd


def test_antigravity_command_includes_cli_parameters() -> None:
    cli = AntigravityCLI(
        CLIConfig(
            provider="antigravity",
            cli_parameters=["--log-file", "agy.log"],
        )
    )

    cmd = cli._build_command()

    assert cmd[-2:] == ["--log-file", "agy.log"]


def test_antigravity_ignores_docker_container() -> None:
    cli = AntigravityCLI(
        CLIConfig(
            provider="antigravity",
            model="antigravity-default",
            docker_container="ductor-sandbox",
            working_dir=".",
        )
    )

    cmd, cwd = cli._host_command(["agy", "--print", "hello"])

    assert cmd[:2] == ["agy", "--print"]
    assert "docker" not in cmd
    assert cwd


def test_antigravity_model_prefix_routes_to_provider() -> None:
    assert ModelRegistry().provider_for("antigravity-default") == "antigravity"


def _make_oneshot_process(stdout: bytes = b"hello world") -> AsyncMock:
    proc = AsyncMock(spec=asyncio.subprocess.Process)
    proc.returncode = 0
    proc.pid = 12345
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


def _make_cli(**overrides: Any) -> AntigravityCLI:
    return AntigravityCLI(
        CLIConfig(
            provider="antigravity",
            model="antigravity-default",
            working_dir=".",
            **overrides,
        )
    )


class TestStreaming:
    """Streaming delegates to the one-shot --print path (agy has no stream)."""

    async def test_send_streaming_emits_text_then_result(self) -> None:
        cli = _make_cli()
        proc = _make_oneshot_process(b"the answer")

        with patch(
            "ductor_bot.cli.antigravity_provider.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            events = [event async for event in cli.send_streaming("hi")]

        assert [type(event) for event in events] == [AssistantTextDelta, ResultEvent]
        assert events[0].text == "the answer"
        assert events[1].result == "the answer"
        assert events[1].is_error is False

    async def test_send_streaming_skips_empty_text_delta(self) -> None:
        cli = _make_cli()
        proc = _make_oneshot_process(b"")

        with patch(
            "ductor_bot.cli.antigravity_provider.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            events = [event async for event in cli.send_streaming("hi")]

        assert [type(event) for event in events] == [ResultEvent]
        assert events[0].result == ""


class TestAgentEnvInjection:
    """agy subprocesses must receive the DUCTOR_* agent identification env."""

    async def test_send_injects_agent_env(self) -> None:
        cli = _make_cli(chat_id=77, transport="tg")
        proc = _make_oneshot_process()

        with patch(
            "ductor_bot.cli.antigravity_provider.asyncio.create_subprocess_exec",
            return_value=proc,
        ) as spawn:
            await cli.send("hello")

        env = spawn.call_args.kwargs["env"]
        assert env["DUCTOR_AGENT_NAME"] == "main"
        assert env["DUCTOR_CHAT_ID"] == "77"
        assert env["DUCTOR_TRANSPORT"] == "tg"
        assert "DUCTOR_HOME" in env
        assert "DUCTOR_SHARED_MEMORY_PATH" in env

    async def test_send_streaming_injects_agent_env(self) -> None:
        cli = _make_cli(chat_id=77)
        proc = _make_oneshot_process()

        with patch(
            "ductor_bot.cli.antigravity_provider.asyncio.create_subprocess_exec",
            return_value=proc,
        ) as spawn:
            async for _event in cli.send_streaming("hello"):
                pass

        env = spawn.call_args.kwargs["env"]
        assert env["DUCTOR_AGENT_NAME"] == "main"
        assert env["DUCTOR_CHAT_ID"] == "77"
        assert "DUCTOR_HOME" in env
