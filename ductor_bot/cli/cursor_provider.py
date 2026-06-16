"""Async wrapper around the Cursor CLI (`cursor agent`)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from functools import partial
from pathlib import Path
from shutil import which
from typing import Any

from ductor_bot.cli.base import BaseCLI, CLIConfig, docker_wrap
from ductor_bot.cli.executor import (
    SubprocessResult,
    SubprocessSpec,
    run_oneshot_subprocess,
    run_streaming_subprocess,
)
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
    SystemInitEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from ductor_bot.cli.types import CLIResponse

logger = logging.getLogger(__name__)


class _CursorStreamState:
    """Mutable accumulator for Cursor streaming output."""

    __slots__ = ("accumulated_text", "session_id", "saw_delta")

    def __init__(self, session_id: str | None) -> None:
        self.accumulated_text: list[str] = []
        self.session_id = session_id
        # True once we have seen a partial-output assistant delta.
        self.saw_delta: bool = False

    def track(self, event: StreamEvent) -> None:
        """Update state from one stream event."""
        if isinstance(event, AssistantTextDelta) and event.text:
            self.accumulated_text.append(event.text)
        if isinstance(event, ResultEvent) and event.session_id:
            self.session_id = event.session_id


class CursorCLI(BaseCLI):
    """Async wrapper around the Cursor CLI."""

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = "cursor" if config.docker_container else self._find_cli()
        logger.info("Cursor CLI wrapper: cwd=%s model=%s", self._working_dir, config.model)

    @staticmethod
    def _find_cli() -> str:
        path = which("cursor")
        if not path:
            msg = "cursor CLI not found on PATH. Install from https://cursor.com/"
            raise FileNotFoundError(msg)
        return path

    def _compose_prompt(self, prompt: str) -> str:
        """Inject system context into user prompt."""
        cfg = self._config
        parts: list[str] = []
        if cfg.system_prompt:
            parts.append(cfg.system_prompt)
        parts.append(prompt)
        if cfg.append_system_prompt:
            parts.append(cfg.append_system_prompt)
        return "\n\n".join(parts)

    def _build_command(
        self,
        prompt: str,
        resume_session: str | None,
        continue_session: bool,
    ) -> tuple[list[str], str | None]:
        """Build Cursor agent command and effective session id."""
        cfg = self._config
        effective_session_id = resume_session
        cmd = [
            self._cli,
            "agent",
            "--print",
            "--output-format",
            "stream-json",
            "--stream-partial-output",
            "--trust",
        ]
        if cfg.model:
            cmd += ["--model", cfg.model]
        if effective_session_id:
            cmd += ["--resume", effective_session_id]
        elif continue_session:
            cmd.append("--continue")
        if cfg.permission_mode == "bypassPermissions":
            cmd.append("--force")
        if cfg.cli_parameters:
            cmd.extend(cfg.cli_parameters)

        cmd.append("--")
        cmd.append(self._compose_prompt(prompt))
        return cmd, effective_session_id

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: Any | None = None,
    ) -> CLIResponse:
        """Send a prompt and return the final result."""
        cmd, effective_session_id = self._build_command(
            prompt,
            resume_session=resume_session,
            continue_session=continue_session,
        )
        exec_cmd, use_cwd = docker_wrap(cmd, self._config)
        _log_cmd(exec_cmd)
        return await run_oneshot_subprocess(
            config=self._config,
            spec=SubprocessSpec(exec_cmd, use_cwd, prompt, timeout_seconds, timeout_controller),
            parse_output=partial(_parse_response, fallback_session_id=effective_session_id),
            provider_label="Cursor",
        )

    async def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: Any | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Send a prompt and yield streaming events as they arrive."""
        cmd, effective_session_id = self._build_command(
            prompt,
            resume_session=resume_session,
            continue_session=continue_session,
        )
        exec_cmd, use_cwd = docker_wrap(cmd, self._config)
        _log_cmd(exec_cmd, streaming=True)

        state = _CursorStreamState(session_id=effective_session_id)

        async def line_handler(line: str) -> AsyncGenerator[StreamEvent, None]:
            for event in parse_cursor_stream_line(line, state):
                state.track(event)
                yield event

        async def post_handler(result: SubprocessResult) -> AsyncGenerator[StreamEvent, None]:
            yield _cursor_final_result(result, state.accumulated_text, state.session_id)

        async for event in run_streaming_subprocess(
            config=self._config,
            spec=SubprocessSpec(exec_cmd, use_cwd, prompt, timeout_seconds, timeout_controller),
            line_handler=line_handler,
            provider_label="Cursor",
            post_handler=post_handler,
        ):
            yield event


def parse_cursor_stream_line(line: str, state: _CursorStreamState) -> list[StreamEvent]:
    """Parse one Cursor stream-json line into normalized stream events."""
    stripped = line.strip()
    if not stripped:
        return []
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.debug("Cursor unparseable line: %.200s", stripped)
        return []
    if not isinstance(data, dict):
        return []

    event_type = data.get("type")

    if event_type == "system":
        subtype = data.get("subtype")
        if subtype == "init":
            return [
                SystemInitEvent(
                    type="system",
                    subtype="init",
                    session_id=data.get("session_id"),
                )
            ]
        return []

    if event_type == "thinking":
        text = data.get("text")
        if isinstance(text, str) and text:
            return [ThinkingEvent(type="assistant", text=text)]
        return []

    if event_type == "assistant":
        return _parse_cursor_assistant_event(data, state)

    if event_type == "result":
        return [
            ResultEvent(
                type="result",
                subtype=data.get("subtype"),
                session_id=data.get("session_id"),
                result=data.get("result", ""),
                is_error=bool(data.get("is_error", False)),
                returncode=data.get("returncode"),
                duration_ms=data.get("duration_ms"),
                duration_api_ms=data.get("duration_api_ms"),
                total_cost_usd=data.get("total_cost_usd"),
                usage=data.get("usage", {}),
                model_usage=data.get("modelUsage", {}),
                num_turns=data.get("num_turns"),
            )
        ]

    return []


def _parse_cursor_assistant_event(
    data: dict[str, Any], state: _CursorStreamState
) -> list[StreamEvent]:
    """Extract text/tool events from a Cursor assistant message."""
    message = data.get("message") or {}
    if not isinstance(message, dict):
        return []

    events: list[StreamEvent] = []
    has_timestamp = "timestamp_ms" in data

    # Partial-output deltas carry timestamp_ms; the final full message does not.
    # Emit deltas as they arrive, but skip the redundant full-message event once
    # we have already streamed partial deltas.
    if has_timestamp:
        state.saw_delta = True
    elif state.saw_delta:
        return events

    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text:
                    events.append(AssistantTextDelta(type="assistant", text=text))
    elif isinstance(content, str) and content:
        events.append(AssistantTextDelta(type="assistant", text=content))

    for tool_call in _iter_cursor_tool_calls(message):
        events.append(
            ToolUseEvent(
                type="assistant",
                tool_name=tool_call.get("name", ""),
                tool_id=tool_call.get("id"),
                parameters=tool_call.get("arguments"),
            )
        )

    return events


def _iter_cursor_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract tool call objects from a Cursor assistant message."""
    raw = message.get("tool_calls")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        if isinstance(fn, dict):
            out.append(
                {
                    "id": str(item.get("id", "")),
                    "name": str(fn.get("name", "")),
                    "arguments": _parse_cursor_tool_arguments(fn.get("arguments")),
                }
            )
    return out


def _parse_cursor_tool_arguments(value: object) -> dict[str, Any] | None:
    """Normalize tool arguments: parse JSON string into dict if needed."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            logger.debug("Cursor unparseable tool arguments: %.200s", value)
            return None
        if isinstance(parsed, dict):
            return parsed
        return None
    return None


def _parse_response(
    stdout: bytes,
    stderr: bytes,
    returncode: int | None,
    *,
    fallback_session_id: str | None,
) -> CLIResponse:
    """Parse Cursor subprocess output into a CLIResponse."""
    stderr_text = stderr.decode(errors="replace")[:2000] if stderr else ""
    raw = stdout.decode(errors="replace").strip()

    if not raw:
        return CLIResponse(
            session_id=fallback_session_id,
            result=stderr_text[:500] if stderr_text else "",
            is_error=True,
            returncode=returncode,
            stderr=stderr_text,
        )

    text_parts: list[str] = []
    discovered_session_id = fallback_session_id
    usage: dict[str, Any] = {}
    duration_ms: float | None = None
    duration_api_ms: float | None = None
    total_cost_usd: float | None = None

    for line in raw.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue

        if isinstance(data.get("session_id"), str):
            discovered_session_id = data["session_id"]

        event_type = data.get("type")
        if event_type == "assistant":
            message = data.get("message") or {}
            if isinstance(message, dict):
                text = _extract_cursor_text(message.get("content"))
                if text:
                    text_parts.append(text)
        elif event_type == "result":
            result_text = data.get("result", "")
            if isinstance(result_text, str) and result_text:
                text_parts = [result_text]
            usage = data.get("usage", {}) or {}
            duration_ms = data.get("duration_ms")
            duration_api_ms = data.get("duration_api_ms")
            total_cost_usd = data.get("total_cost_usd")

    result_text = "".join(text_parts).strip() or raw[:2000]
    if returncode and returncode != 0 and stderr_text:
        result_text = stderr_text[:500]

    return CLIResponse(
        session_id=discovered_session_id,
        result=result_text,
        is_error=bool(returncode and returncode != 0),
        returncode=returncode,
        stderr=stderr_text,
        usage=usage,
        duration_ms=duration_ms,
        duration_api_ms=duration_api_ms,
        total_cost_usd=total_cost_usd,
    )


def _extract_cursor_text(value: object) -> str:
    """Extract readable text from a Cursor message content payload."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                chunks.append(item["text"])
        return "".join(chunks)
    if isinstance(value, dict):
        for key in ("text", "content", "message", "result"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return ""


def _cursor_final_result(
    result: SubprocessResult,
    accumulated_text: list[str],
    session_id: str | None,
) -> ResultEvent:
    """Build final stream ResultEvent for Cursor."""
    stderr_text = result.stderr_bytes.decode(errors="replace")[:2000] if result.stderr_bytes else ""
    if result.process.returncode != 0:
        detail = stderr_text or "\n".join(accumulated_text) or "(no output)"
        return ResultEvent(
            type="result",
            session_id=session_id,
            result=detail[:500],
            is_error=True,
            returncode=result.process.returncode,
        )
    return ResultEvent(
        type="result",
        session_id=session_id,
        result="".join(accumulated_text),
        is_error=False,
        returncode=result.process.returncode,
    )


def _log_cmd(cmd: list[str], *, streaming: bool = False) -> None:
    """Log Cursor command with truncated long values."""
    safe_cmd = [(c[:80] + "...") if len(c) > 80 else c for c in cmd]
    prefix = "Cursor stream cmd" if streaming else "Cursor cmd"
    logger.info("%s: %s", prefix, " ".join(safe_cmd))
