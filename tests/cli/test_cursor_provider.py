"""Tests for CursorCLI provider: command building and parsing."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.cursor_provider import (
    CursorCLI,
    _CursorStreamState,
    _cursor_final_result,
    _extract_cursor_text,
    _parse_response,
    parse_cursor_stream_line,
)
from ductor_bot.cli.executor import SubprocessResult
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    SystemInitEvent,
    ThinkingEvent,
    ToolUseEvent,
)


def _make_cli(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> CursorCLI:
    monkeypatch.setattr("ductor_bot.cli.cursor_provider.which", lambda _: "/usr/bin/cursor")
    cfg = CLIConfig(
        provider="cursor",
        working_dir=overrides.pop("working_dir", "."),
        model=overrides.pop("model", "auto"),
        system_prompt=overrides.pop("system_prompt", None),
        append_system_prompt=overrides.pop("append_system_prompt", None),
        chat_id=overrides.pop("chat_id", 123),
        topic_id=overrides.pop("topic_id", 9),
        cli_parameters=overrides.pop("cli_parameters", []),
        permission_mode=overrides.pop("permission_mode", "bypassPermissions"),
    )
    return CursorCLI(cfg)


def test_build_command_has_stream_json(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_cli(monkeypatch)
    cmd, sid = cli._build_command("hello", resume_session=None, continue_session=False)
    assert cmd[0] == "/usr/bin/cursor"
    assert cmd[1] == "agent"
    assert "--print" in cmd
    assert "--output-format" in cmd
    fmt_idx = cmd.index("--output-format")
    assert cmd[fmt_idx + 1] == "stream-json"
    assert "--stream-partial-output" in cmd
    assert "--trust" in cmd
    assert "--model" in cmd
    assert sid is None
    assert "--resume" not in cmd


def test_build_command_continue_without_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_cli(monkeypatch)
    cmd, sid = cli._build_command("hello", resume_session=None, continue_session=True)
    assert "--continue" in cmd
    assert "--resume" not in cmd
    assert sid is None


def test_build_command_uses_given_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_cli(monkeypatch)
    cmd, sid = cli._build_command("hello", resume_session="abc-123", continue_session=False)
    assert sid == "abc-123"
    idx = cmd.index("--resume")
    assert cmd[idx + 1] == "abc-123"


def test_build_command_adds_force_in_bypass_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_cli(monkeypatch, permission_mode="bypassPermissions")
    cmd, _sid = cli._build_command("hello", resume_session=None, continue_session=False)
    assert "--force" in cmd


def test_build_command_no_force_when_restricted(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_cli(monkeypatch, permission_mode="confirm")
    cmd, _sid = cli._build_command("hello", resume_session=None, continue_session=False)
    assert "--force" not in cmd


def test_compose_prompt_includes_system_context(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _make_cli(
        monkeypatch,
        system_prompt="SYSTEM",
        append_system_prompt="TAIL",
    )
    composed = cli._compose_prompt("USER")
    assert composed == "SYSTEM\n\nUSER\n\nTAIL"


def test_parse_cursor_stream_line_system_init() -> None:
    state = _CursorStreamState(None)
    line = json.dumps({"type": "system", "subtype": "init", "session_id": "sid-1"})
    events = parse_cursor_stream_line(line, state)
    assert len(events) == 1
    assert isinstance(events[0], SystemInitEvent)
    assert events[0].session_id == "sid-1"


def test_parse_cursor_stream_line_thinking() -> None:
    state = _CursorStreamState(None)
    line = json.dumps({"type": "thinking", "subtype": "delta", "text": "thinking..."})
    events = parse_cursor_stream_line(line, state)
    assert len(events) == 1
    assert isinstance(events[0], ThinkingEvent)
    assert events[0].text == "thinking..."


def test_parse_cursor_stream_line_assistant_text_and_tool() -> None:
    state = _CursorStreamState(None)
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
                "tool_calls": [
                    {
                        "id": "t1",
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": "/tmp/x"},
                        },
                    }
                ],
            },
        }
    )
    events = parse_cursor_stream_line(line, state)
    assert any(isinstance(e, AssistantTextDelta) and e.text == "hello" for e in events)
    assert any(
        isinstance(e, ToolUseEvent) and e.tool_name == "read_file" and e.tool_id == "t1"
        for e in events
    )


def test_parse_cursor_stream_line_assistant_tool_with_string_arguments() -> None:
    state = _CursorStreamState(None)
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [],
                "tool_calls": [
                    {
                        "id": "t2",
                        "function": {
                            "name": "Bash",
                            "arguments": '{"command": "echo hi", "timeout": 60}',
                        },
                    }
                ],
            },
        }
    )
    events = parse_cursor_stream_line(line, state)
    tool_events = [e for e in events if isinstance(e, ToolUseEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "Bash"
    assert tool_events[0].parameters == {"command": "echo hi", "timeout": 60}


def test_parse_cursor_stream_line_bad_json() -> None:
    state = _CursorStreamState(None)
    assert parse_cursor_stream_line("not-json", state) == []


def test_extract_cursor_text_variants() -> None:
    assert _extract_cursor_text("abc") == "abc"
    assert _extract_cursor_text([{"type": "text", "text": "a"}, {"content": "b"}]) == "ab"
    assert _extract_cursor_text({"text": "x"}) == "x"
    assert _extract_cursor_text(123) == ""


def test_parse_response_collects_result_event() -> None:
    stdout = (
        b'{"type":"system","subtype":"init","session_id":"sid-1"}\n'
        b'{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Hello "}]}}\n'
        b'{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"world"}]}}\n'
        b'{"type":"result","subtype":"success","result":"Hello world","session_id":"sid-1","usage":{"inputTokens":10}}\n'
    )
    resp = _parse_response(stdout, b"", 0, fallback_session_id="fallback")
    assert resp.result == "Hello world"
    assert resp.session_id == "sid-1"
    assert resp.is_error is False
    assert resp.usage.get("inputTokens") == 10


def test_parse_response_empty_stdout_is_error() -> None:
    resp = _parse_response(b"", b"stderr", 1, fallback_session_id="sid-x")
    assert resp.is_error is True
    assert resp.session_id == "sid-x"
    assert "stderr" in resp.result


def test_cursor_final_result_success() -> None:
    class _Proc:
        returncode = 0

    result = _cursor_final_result(
        SubprocessResult(process=_Proc(), stderr_bytes=b""),
        ["abc", "def"],
        "sid-1",
    )
    assert isinstance(result, ResultEvent)
    assert result.is_error is False
    assert result.result == "abcdef"
    assert result.session_id == "sid-1"


def test_cursor_final_result_error() -> None:
    class _Proc:
        returncode = 2

    result = _cursor_final_result(
        SubprocessResult(process=_Proc(), stderr_bytes=b"boom"),
        [],
        "sid-2",
    )
    assert result.is_error is True
    assert result.returncode == 2
    assert "boom" in result.result
