"""Async wrapper around the Antigravity CLI (agy)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.antigravity_events import parse_antigravity_json
from ductor_bot.cli.base import BaseCLI, CLIConfig
from ductor_bot.cli.executor import build_subprocess_env
from ductor_bot.cli.process_registry import ProcessRegistry, TrackedProcess
from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent, StreamEvent
from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import ANTIGRAVITY_MODELS
from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.process_tree import force_kill_process_tree

if TYPE_CHECKING:
    from ductor_bot.cli.timeout_controller import TimeoutController

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 300.0  # 5 minutes, matches agy --print-timeout default


class AntigravityCLI(BaseCLI):
    """Async wrapper around the Antigravity CLI (agy).

    agy has no headless streaming protocol: ``--print`` returns the whole
    answer in one shot and ``--prompt-interactive`` is a bubbletea TUI that
    requires a real ``/dev/tty``, which a subprocess does not have. Both
    :meth:`send` and :meth:`send_streaming` therefore drive the same
    ``--print`` command; streaming just re-emits the one-shot answer as a
    single text delta plus a final result event.

    agy flags reference:
      --print / -p <prompt>   Non-interactive single-shot
      --continue / -c         Continue most recent conversation
      --conversation <id>     Resume a specific conversation
      --dangerously-skip-permissions  Auto-approve all tools
      --print-timeout <dur>   Timeout for --print mode (default 5m)
      --model <id>            Select a model (see ``agy models``)
      --add-dir <dir>         Add workspace directory
      --log-file <path>       Override log file
    """

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = "agy"
        logger.info("AntigravityCLI: cwd=%s model=%s", self._working_dir, config.model)

    def _build_command(
        self,
        *,
        resume_session: str | None = None,
        continue_session: bool = False,
    ) -> list[str]:
        """Build the ``agy --print`` command list (prompt appended by caller)."""
        cmd = [self._cli, "--print"]

        if self._config.model and self._config.model not in ANTIGRAVITY_MODELS:
            cmd += ["--model", self._config.model]

        # Session resume / continue
        if resume_session:
            cmd += ["--conversation", resume_session]
        elif continue_session:
            cmd += ["--continue"]

        # Auto-approve when bypass mode is set
        if self._config.permission_mode == "bypassPermissions":
            cmd += ["--dangerously-skip-permissions"]

        cmd.extend(self._config.cli_parameters)
        return cmd

    def _host_command(self, cmd: list[str]) -> tuple[list[str], str]:
        """Return a host-execution command.

        Antigravity is a host CLI. The standard Docker sandbox image does not
        include ``agy`` or its user auth state, so running it through
        ``docker exec`` produces an OCI "agy not found" error.
        """
        if self._config.docker_container:
            logger.info("Antigravity runs on host; ignoring Docker container for agy")
        return cmd, str(self._working_dir)

    # -- Process tracking -----------------------------------------------------

    def _track_process(
        self,
        process: asyncio.subprocess.Process,
    ) -> tuple[ProcessRegistry | None, TrackedProcess | None]:
        """Register a subprocess in ProcessRegistry if tracking is enabled."""
        reg = self._config.process_registry
        tracked = (
            reg.register(
                self._config.chat_id,
                process,
                self._config.process_label,
                topic_id=self._config.topic_id,
            )
            if reg
            else None
        )
        return reg, tracked

    @staticmethod
    def _untrack_process(reg: ProcessRegistry | None, tracked: TrackedProcess | None) -> None:
        """Unregister a previously tracked subprocess."""
        if tracked is not None and reg is not None:
            reg.unregister(tracked)

    # -- Non-streaming --------------------------------------------------------

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> CLIResponse:
        """Send a prompt via ``agy --print`` and return the full response."""
        effective_timeout = timeout_seconds or _DEFAULT_TIMEOUT
        cmd = self._build_command(
            resume_session=resume_session,
            continue_session=continue_session,
        )
        # --print takes prompt as a positional argument
        cmd.append(prompt)

        cmd, cwd = self._host_command(cmd)
        safe_cmd = _safe_command_for_logging(cmd)
        logger.debug("Antigravity send: %s", safe_cmd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=build_subprocess_env(self._config),
            creationflags=_CREATION_FLAGS,
        )

        reg, tracked = self._track_process(proc)

        try:
            timed_out = False
            try:
                communicate_coro = proc.communicate()
                if timeout_controller is not None:
                    stdout_bytes, stderr_bytes = await timeout_controller.run_with_timeout(
                        communicate_coro,
                    )
                else:
                    async with asyncio.timeout(effective_timeout):
                        stdout_bytes, stderr_bytes = await communicate_coro
            except TimeoutError:
                timed_out = True
                logger.warning("Antigravity send timed out")
                force_kill_process_tree(proc.pid)
                stdout_bytes, stderr_bytes = await proc.communicate()
                return CLIResponse(
                    result="Timeout",
                    is_error=True,
                    timed_out=True,
                    returncode=proc.returncode,
                    stderr=stderr_bytes.decode(errors="replace")[:2000] if stderr_bytes else "",
                )
        finally:
            self._untrack_process(reg, tracked)
            if not timed_out and proc.returncode is None:
                force_kill_process_tree(proc.pid)

        stdout = stdout_bytes.decode(errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""

        result_text = parse_antigravity_json(stdout)
        is_error = proc.returncode not in (None, 0)

        return CLIResponse(
            result=result_text,
            is_error=is_error,
            returncode=proc.returncode,
            stderr=stderr,
        )

    # -- Streaming ------------------------------------------------------------

    async def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream a response from agy.

        agy exposes no incremental stream, so this runs the one-shot
        ``--print`` path and emits the answer as a single text delta followed
        by the final result event. This keeps the streaming contract intact
        for the orchestrator while matching what the CLI can actually do.
        """
        response = await self.send(
            prompt,
            resume_session=resume_session,
            continue_session=continue_session,
            timeout_seconds=timeout_seconds,
            timeout_controller=timeout_controller,
        )

        if response.result:
            yield AssistantTextDelta(type="assistant", text=response.result)

        yield ResultEvent(
            type="result",
            result=response.result,
            is_error=response.is_error,
            returncode=response.returncode,
            session_id=response.session_id,
        )


# -- Module-level helpers -----------------------------------------------------


def _safe_command_for_logging(cmd: list[str]) -> list[str]:
    """Return a command safe for debug logs."""
    safe = [part if len(part) <= 80 else part[:80] + "..." for part in cmd]
    if "--print" in cmd and safe:
        safe[-1] = "<prompt>"
    return safe
