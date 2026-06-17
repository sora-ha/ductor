"""Reasonix CLI wrapper for ductor.

Reasonix runs in a single-shot, non-interactive mode:

    reasonix run <prompt>

It emits plain text (MCP handshake progress lines, the assistant answer, and a
final usage footer). This wrapper strips the noise and returns a clean result.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator
from pathlib import Path
from shutil import which

from ductor_bot.cli.base import BaseCLI, CLIConfig
from ductor_bot.cli.executor import SubprocessSpec, run_oneshot_subprocess
from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent, StreamEvent
from ductor_bot.cli.timeout_controller import TimeoutController
from ductor_bot.cli.types import CLIResponse

logger = logging.getLogger(__name__)

# Node/nvm path supplied by the user. Used as a fallback when ``reasonix`` is
# not on PATH.
DEFAULT_REASONIX_BINARY = (
    Path.home() / ".nvm" / "versions" / "node" / "v24.16.0" / "bin" / "reasonix"
)

# Lines that are part of Reasonix's MCP/tool handshake progress output.
_PROGRESS_PREFIXES = ("⌘ MCP", "↻", "✓", "✗", "→", "←")
# Footer line starts with an em-dash and contains turn/usage statistics.
_FOOTER_RE = re.compile(r"^— turns:.*$", re.MULTILINE)


class ReasonixCLI(BaseCLI):
    """Provider wrapper for the Reasonix CLI."""

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli = self._find_cli()
        logger.info(
            "Reasonix CLI wrapper: cli=%s cwd=%s model=%s",
            self._cli,
            self._working_dir,
            config.model,
        )

    @staticmethod
    def _find_cli() -> str:
        """Locate the Reasonix binary.

        Prefers ``reasonix`` on PATH, then the known nvm installation path.
        """
        path = which("reasonix")
        if path:
            return path
        if DEFAULT_REASONIX_BINARY.is_file():
            return str(DEFAULT_REASONIX_BINARY)
        raise FileNotFoundError(
            f"reasonix CLI not found. Expected 'reasonix' on PATH or at {DEFAULT_REASONIX_BINARY}"
        )

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> CLIResponse:
        cmd = self._build_command(
            prompt,
            resume_session=resume_session,
            continue_session=continue_session,
        )
        return await run_oneshot_subprocess(
            config=self._config,
            spec=SubprocessSpec(
                exec_cmd=cmd,
                use_cwd=str(self._working_dir),
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                timeout_controller=timeout_controller,
            ),
            parse_output=self._parse_response,
            provider_label="Reasonix",
        )

    async def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
        timeout_controller: TimeoutController | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        response = await self.send(
            prompt,
            resume_session=resume_session,
            continue_session=continue_session,
            timeout_seconds=timeout_seconds,
            timeout_controller=timeout_controller,
        )
        if response.result:
            yield AssistantTextDelta(type="assistant", text=response.result)
        yield ResultEvent(type="result", **response.model_dump())

    def _build_command(
        self,
        prompt: str,
        *,
        resume_session: str | None = None,
        continue_session: bool = False,
    ) -> list[str]:
        cmd: list[str] = [self._cli, "run"]

        # Extra CLI parameters come first so user-supplied values can be
        # overridden by ductor-managed flags if necessary.
        if self._config.cli_parameters:
            cmd.extend(self._config.cli_parameters)

        model = self._config.model or "deepseek-v4-flash"
        if model:
            cmd += ["--model", model]

        if self._config.system_prompt:
            cmd += ["--system", self._config.system_prompt]

        if self._config.reasoning_effort and self._config.reasoning_effort != "medium":
            cmd += ["--effort", self._config.reasoning_effort]

        if self._config.max_budget_usd:
            cmd += ["--budget", str(self._config.max_budget_usd)]

        if resume_session:
            cmd += ["--transcript", resume_session]
        elif continue_session:
            cmd.append("--continue")

        cmd.append(prompt)
        return cmd

    def _parse_response(
        self, stdout: bytes, stderr: bytes, returncode: int | None
    ) -> CLIResponse:
        raw = stdout.decode(errors="replace")
        cleaned = _clean_reasonix_output(raw)

        is_error = returncode != 0
        stderr_text = stderr.decode(errors="replace").strip()

        # Try to extract the reported cost from the footer so the orchestrator
        # can track spend across providers.
        total_cost = _extract_cost(raw)

        result_text = (
            cleaned
            if not is_error
            else (stderr_text or cleaned or f"reasonix exited with code {returncode}")
        )
        return CLIResponse(
            result=result_text,
            is_error=is_error,
            returncode=returncode,
            stderr=stderr_text,
            total_cost_usd=total_cost,
        )


def _clean_reasonix_output(raw: str) -> str:
    """Remove Reasonix progress lines and the usage footer.

    Reasonix output looks like:

        ⌘ MCP · filesystem      ↻ handshake…   initialise → tools/list
        ...
        The actual assistant answer.

        — turns:1 cache:99.9% cost:$0.000046 save-vs-claude:99.9%

    We keep only the assistant answer.
    """
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_PROGRESS_PREFIXES):
            continue
        lines.append(line)

    # Remove the trailing footer line if present.
    text = "\n".join(lines)
    text = _FOOTER_RE.sub("", text)
    return text.strip()


def _extract_cost(raw: str) -> float | None:
    """Parse ``cost:$0.000046`` from the Reasonix footer."""
    match = re.search(r"cost:\s*\$([0-9]+(?:\.[0-9]+)?)", raw)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None
