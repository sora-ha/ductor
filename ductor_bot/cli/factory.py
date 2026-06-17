"""CLI backend factory -- returns the right provider based on config."""

from __future__ import annotations

import logging

from ductor_bot.cli.base import BaseCLI, CLIConfig

logger = logging.getLogger(__name__)


def create_cli(config: CLIConfig) -> BaseCLI:  # noqa: PLR0911
    """Create a CLI backend instance based on ``config.provider``."""
    logger.debug("CLI factory creating provider=%s", config.provider)
    if config.provider == "gemini":
        from ductor_bot.cli.gemini_provider import GeminiCLI

        return GeminiCLI(config)

    if config.provider == "codex":
        from ductor_bot.cli.codex_provider import CodexCLI

        return CodexCLI(config)

    if config.provider == "antigravity":
        from ductor_bot.cli.antigravity_provider import AntigravityCLI

        return AntigravityCLI(config)

    if config.provider == "kimi":
        from ductor_bot.cli.kimi_provider import KimiCLI

        return KimiCLI(config)

    if config.provider == "cursor":
        from ductor_bot.cli.cursor_provider import CursorCLI

        return CursorCLI(config)

    if config.provider == "reasonix":
        from ductor_bot.cli.reasonix_provider import ReasonixCLI

        return ReasonixCLI(config)

    from ductor_bot.cli.claude_provider import ClaudeCodeCLI

    return ClaudeCodeCLI(config)
