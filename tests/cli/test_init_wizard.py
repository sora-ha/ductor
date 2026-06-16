"""Tests for onboarding wizard behavior."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.cli.init_wizard import (
    _ask_matrix_homeserver,
    _check_clis,
    _WizardConfig,
    _write_config,
    run_onboarding,
)
from ductor_bot.workspace.paths import DuctorPaths


def _make_paths(tmp_path: Path) -> DuctorPaths:
    fw = tmp_path / "framework"
    fw.mkdir(parents=True, exist_ok=True)
    return DuctorPaths(
        ductor_home=tmp_path / "home",
        home_defaults=fw / "workspace",
        framework_root=fw,
    )


def test_write_config_ignores_corrupt_existing_json(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text("{broken json", encoding="utf-8")

    with (
        patch("ductor_bot.cli.init_wizard.resolve_paths", return_value=paths),
        patch("ductor_bot.cli.init_wizard.init_workspace"),
    ):
        out = _write_config(
            _WizardConfig(
                transport="telegram",
                telegram_token="123456789:abcdefghijklmnopqrstuvwxyzABCDE",
                allowed_user_ids=[1234],
                user_timezone="UTC",
                docker_enabled=False,
            )
        )

    assert out == paths.config_path
    data = json.loads(paths.config_path.read_text(encoding="utf-8"))
    assert data["telegram_token"] == "123456789:abcdefghijklmnopqrstuvwxyzABCDE"
    assert data["allowed_user_ids"] == [1234]
    assert data["user_timezone"] == "UTC"
    assert data["gemini_api_key"] == "null"


def test_write_config_normalizes_existing_null_gemini_api_key(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text('{"gemini_api_key": null}', encoding="utf-8")

    with (
        patch("ductor_bot.cli.init_wizard.resolve_paths", return_value=paths),
        patch("ductor_bot.cli.init_wizard.init_workspace"),
    ):
        _write_config(
            _WizardConfig(
                transport="telegram",
                telegram_token="123456789:abcdefghijklmnopqrstuvwxyzABCDE",
                allowed_user_ids=[1234],
                user_timezone="UTC",
                docker_enabled=False,
            )
        )

    data = json.loads(paths.config_path.read_text(encoding="utf-8"))
    assert data["gemini_api_key"] == "null"


def test_write_config_sets_transports_for_matrix(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    # Simulate an untouched example config that still defaults to telegram transports.
    paths.config_path.write_text('{"transports": ["telegram"]}', encoding="utf-8")

    with (
        patch("ductor_bot.cli.init_wizard.resolve_paths", return_value=paths),
        patch("ductor_bot.cli.init_wizard.init_workspace"),
    ):
        _write_config(
            _WizardConfig(
                transport="matrix",
                matrix_homeserver="http://matrix.local:6167",
                matrix_user_id="@user1:matrix.local",
                matrix_password="password1",
                matrix_allowed_users=["@wingkit:matrix.local"],
                user_timezone="UTC",
                docker_enabled=False,
            )
        )

    data = json.loads(paths.config_path.read_text(encoding="utf-8"))
    assert data["transport"] == "matrix"
    assert data["transports"] == ["matrix"]
    assert data["matrix"]["homeserver"] == "http://matrix.local:6167"


def test_run_onboarding_returns_false_when_service_install_fails(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)

    with (
        patch("ductor_bot.cli.init_wizard._show_banner"),
        patch("ductor_bot.cli.init_wizard._check_clis"),
        patch("ductor_bot.cli.init_wizard._show_disclaimer"),
        patch("ductor_bot.cli.init_wizard._ask_transport", return_value="telegram"),
        patch("ductor_bot.cli.init_wizard._ask_telegram_token", return_value="token"),
        patch("ductor_bot.cli.init_wizard._ask_user_id", return_value=[1]),
        patch("ductor_bot.cli.init_wizard._ask_docker", return_value=False),
        patch("ductor_bot.cli.init_wizard._ask_timezone", return_value="UTC"),
        patch("ductor_bot.cli.init_wizard._write_config", return_value=paths.config_path),
        patch("ductor_bot.cli.init_wizard.resolve_paths", return_value=paths),
        patch("ductor_bot.cli.init_wizard._offer_service_install", return_value=True),
        patch("ductor_bot.infra.service.install_service", return_value=False),
    ):
        assert run_onboarding() is False


def test_run_onboarding_returns_true_when_service_install_succeeds(tmp_path: Path) -> None:
    paths = _make_paths(tmp_path)

    with (
        patch("ductor_bot.cli.init_wizard._show_banner"),
        patch("ductor_bot.cli.init_wizard._check_clis"),
        patch("ductor_bot.cli.init_wizard._show_disclaimer"),
        patch("ductor_bot.cli.init_wizard._ask_transport", return_value="telegram"),
        patch("ductor_bot.cli.init_wizard._ask_telegram_token", return_value="token"),
        patch("ductor_bot.cli.init_wizard._ask_user_id", return_value=[1]),
        patch("ductor_bot.cli.init_wizard._ask_docker", return_value=False),
        patch("ductor_bot.cli.init_wizard._ask_timezone", return_value="UTC"),
        patch("ductor_bot.cli.init_wizard._write_config", return_value=paths.config_path),
        patch("ductor_bot.cli.init_wizard.resolve_paths", return_value=paths),
        patch("ductor_bot.cli.init_wizard._offer_service_install", return_value=True),
        patch("ductor_bot.infra.service.install_service", return_value=True),
    ):
        assert run_onboarding() is True


# --- Regression tests for non-fatal CLI auth-check failures (#109 / P1-BUG-01) ---


def test_check_clis_survives_codex_exception() -> None:
    """An exception in one probe (codex) must not abort the wizard when
    another provider (claude) is authenticated."""

    def _boom() -> AuthResult:
        raise RuntimeError("boom")

    console = Console(record=True, width=120)
    with (
        patch(
            "ductor_bot.cli.init_wizard.check_claude_auth",
            return_value=AuthResult("claude", AuthStatus.AUTHENTICATED),
        ),
        patch("ductor_bot.cli.init_wizard.check_codex_auth", side_effect=_boom),
        patch(
            "ductor_bot.cli.init_wizard.check_gemini_auth",
            return_value=AuthResult("gemini", AuthStatus.NOT_FOUND),
        ),
        patch(
            "ductor_bot.cli.init_wizard.check_antigravity_auth",
            return_value=AuthResult("antigravity", AuthStatus.NOT_FOUND),
        ),
    ):
        # Must NOT raise SystemExit.
        _check_clis(console)

    output = console.export_text()
    lowered = output.lower()
    assert "codex" in lowered
    assert "boom" in lowered


def test_check_clis_aborts_when_all_fail_or_unauthenticated() -> None:
    """When no provider is authenticated, the wizard must still abort."""
    console = Console(record=True, width=120)
    with (
        patch(
            "ductor_bot.cli.init_wizard.check_claude_auth",
            return_value=AuthResult("claude", AuthStatus.NOT_FOUND),
        ),
        patch(
            "ductor_bot.cli.init_wizard.check_codex_auth",
            return_value=AuthResult("codex", AuthStatus.NOT_FOUND),
        ),
        patch(
            "ductor_bot.cli.init_wizard.check_gemini_auth",
            return_value=AuthResult("gemini", AuthStatus.NOT_FOUND),
        ),
        patch(
            "ductor_bot.cli.init_wizard.check_antigravity_auth",
            return_value=AuthResult("antigravity", AuthStatus.NOT_FOUND),
        ),
        pytest.raises(SystemExit),
    ):
        _check_clis(console)


def test_check_clis_continues_when_only_claude_authed() -> None:
    """When Claude is authenticated and codex/gemini are NOT_FOUND, wizard continues."""
    console = Console(record=True, width=120)
    with (
        patch(
            "ductor_bot.cli.init_wizard.check_claude_auth",
            return_value=AuthResult("claude", AuthStatus.AUTHENTICATED),
        ),
        patch(
            "ductor_bot.cli.init_wizard.check_codex_auth",
            return_value=AuthResult("codex", AuthStatus.NOT_FOUND),
        ),
        patch(
            "ductor_bot.cli.init_wizard.check_gemini_auth",
            return_value=AuthResult("gemini", AuthStatus.NOT_FOUND),
        ),
        patch(
            "ductor_bot.cli.init_wizard.check_antigravity_auth",
            return_value=AuthResult("antigravity", AuthStatus.NOT_FOUND),
        ),
    ):
        # Returns None; does not raise SystemExit.
        assert _check_clis(console) is None


# --- Matrix homeserver URL validation ---


def _mock_questionary(urls: list[str | None]) -> object:
    """Return a questionary.text mock that yields *urls* on successive .ask() calls."""

    class _MockQuestionary:
        def ask(self) -> str | None:
            return urls.pop(0)

    return _MockQuestionary()


@pytest.mark.parametrize(
    ("input_url", "expected_url"),
    [
        ("https://matrix.example.com", "https://matrix.example.com"),
        ("http://matrix.example.com", "http://matrix.example.com"),
        ("https://matrix.example.com/", "https://matrix.example.com"),
        ("http://matrix.example.com/", "http://matrix.example.com"),
    ],
)
def test_ask_matrix_homeserver_accepts_http_and_https(
    input_url: str, expected_url: str
) -> None:
    """Matrix homeserver URL must accept both HTTP and HTTPS schemes."""
    console = Console(record=True, width=120)
    with patch(
        "ductor_bot.cli.init_wizard.questionary.text",
        return_value=_mock_questionary([input_url]),
    ):
        assert _ask_matrix_homeserver(console) == expected_url


def test_ask_matrix_homeserver_rejects_invalid_then_accepts() -> None:
    """Invalid or scheme-less URLs are rejected; a valid URL is eventually returned."""
    console = Console(record=True, width=120)
    with patch(
        "ductor_bot.cli.init_wizard.questionary.text",
        return_value=_mock_questionary(
            ["matrix.example.com", "ftp://matrix.example.com", "http://localhost:8008"]
        ),
    ):
        assert _ask_matrix_homeserver(console) == "http://localhost:8008"
