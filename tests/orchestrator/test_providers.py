"""Tests for ProviderManager: model resolution, directives, auth, and defaults."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ductor_bot.config import (
    DEFAULT_CURSOR_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_KIMI_MODEL,
    DEFAULT_REASONIX_MODEL,
    AgentConfig,
    reset_cursor_models,
    reset_gemini_models,
    reset_kimi_models,
    reset_reasonix_models,
    set_gemini_models,
    set_reasonix_models,
)
from ductor_bot.orchestrator.providers import ProviderManager


@pytest.fixture(autouse=True)
def _reset_gemini():
    reset_gemini_models()
    yield
    reset_gemini_models()


@pytest.fixture(autouse=True)
def _reset_kimi():
    reset_kimi_models()
    yield
    reset_kimi_models()


@pytest.fixture(autouse=True)
def _reset_cursor():
    reset_cursor_models()
    yield
    reset_cursor_models()


@pytest.fixture(autouse=True)
def _reset_reasonix():
    reset_reasonix_models()
    yield
    reset_reasonix_models()


def _pm(
    *,
    model: str = "sonnet",
    provider: str = "claude",
    codex_cache_fn: object | None = None,
) -> ProviderManager:
    cfg = AgentConfig(model=model, provider=provider)
    return ProviderManager(cfg, codex_cache_fn=codex_cache_fn)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# resolve_runtime_target
# ---------------------------------------------------------------------------


class TestResolveRuntimeTarget:
    def test_default_model(self) -> None:
        pm = _pm(model="opus")
        model, provider = pm.resolve_runtime_target()
        assert model == "opus"
        assert provider == "claude"

    def test_explicit_model(self) -> None:
        pm = _pm()
        model, provider = pm.resolve_runtime_target("haiku")
        assert model == "haiku"
        assert provider == "claude"

    def test_auto_resolves_to_configured_claude_default(self) -> None:
        pm = _pm()
        model, provider = pm.resolve_runtime_target("auto")
        assert model == "sonnet"
        assert provider == "claude"

    def test_kimi_model(self) -> None:
        pm = _pm()
        model, provider = pm.resolve_runtime_target("kimi-code/kimi-for-coding")
        assert model == "kimi-code/kimi-for-coding"
        assert provider == "kimi"

    def test_cursor_model(self) -> None:
        pm = _pm()
        model, provider = pm.resolve_runtime_target("composer-2.5-fast")
        assert model == "composer-2.5-fast"
        assert provider == "cursor"

    def test_codex_model(self) -> None:
        pm = _pm()
        model, provider = pm.resolve_runtime_target("o3-mini")
        assert model == "o3-mini"
        assert provider == "codex"

    def test_none_falls_back_to_config(self) -> None:
        pm = _pm(model="haiku")
        model, provider = pm.resolve_runtime_target(None)
        assert model == "haiku"
        assert provider == "claude"

    def test_auto_respects_configured_cursor_provider(self) -> None:
        pm = _pm(model="auto", provider="cursor")
        model, provider = pm.resolve_runtime_target()
        assert model == "auto"
        assert provider == "cursor"

    def test_auto_resolves_to_configured_kimi_default(self) -> None:
        pm = _pm(model="auto", provider="kimi")
        model, provider = pm.resolve_runtime_target()
        assert model == "kimi-code/kimi-for-coding"
        assert provider == "kimi"

    def test_reasonix_model(self) -> None:
        pm = _pm()
        model, provider = pm.resolve_runtime_target("deepseek-v4-flash")
        assert model == "deepseek-v4-flash"
        assert provider == "reasonix"

    def test_auto_resolves_to_configured_reasonix_default(self) -> None:
        pm = _pm(model="auto", provider="reasonix")
        model, provider = pm.resolve_runtime_target()
        assert model == DEFAULT_REASONIX_MODEL
        assert provider == "reasonix"


# ---------------------------------------------------------------------------
# resolve_session_directive
# ---------------------------------------------------------------------------


class TestResolveSessionDirective:
    def test_provider_name_claude(self) -> None:
        pm = _pm(model="opus", provider="claude")
        result = pm.resolve_session_directive("claude")
        assert result is not None
        assert result[0] == "claude"
        assert result[1] == "opus"  # default_model_for_provider returns config.model

    def test_provider_name_gemini(self) -> None:
        pm = _pm()
        result = pm.resolve_session_directive("gemini")
        assert result is not None
        assert result[0] == "gemini"
        assert result[1] == DEFAULT_GEMINI_MODEL

    def test_provider_name_codex(self) -> None:
        pm = _pm()
        result = pm.resolve_session_directive("codex")
        assert result is not None
        assert result[0] == "codex"

    def test_provider_name_kimi(self) -> None:
        pm = _pm()
        result = pm.resolve_session_directive("kimi")
        assert result is not None
        assert result[0] == "kimi"
        assert result[1] == DEFAULT_KIMI_MODEL

    def test_provider_name_cursor(self) -> None:
        pm = _pm()
        result = pm.resolve_session_directive("cursor")
        assert result is not None
        assert result[0] == "cursor"
        assert result[1] == DEFAULT_CURSOR_MODEL

    def test_provider_name_reasonix(self) -> None:
        pm = _pm()
        result = pm.resolve_session_directive("reasonix")
        assert result is not None
        assert result[0] == "reasonix"
        assert result[1] == DEFAULT_REASONIX_MODEL

    def test_known_model(self) -> None:
        pm = _pm()
        result = pm.resolve_session_directive("opus")
        assert result is not None
        assert result == ("claude", "opus")

    def test_known_gemini_alias(self) -> None:
        pm = _pm()
        result = pm.resolve_session_directive("auto")
        assert result is not None
        assert result == ("gemini", "auto")

    def test_unknown_returns_none(self) -> None:
        pm = _pm()
        assert pm.resolve_session_directive("unknown-model-xyz") is None


# ---------------------------------------------------------------------------
# is_known_model
# ---------------------------------------------------------------------------


class TestIsKnownModel:
    def test_claude_models(self) -> None:
        pm = _pm()
        for name in ("haiku", "sonnet", "opus"):
            assert pm.is_known_model(name) is True

    def test_gemini_aliases(self) -> None:
        pm = _pm()
        assert pm.is_known_model("auto") is True
        assert pm.is_known_model("flash") is True

    def test_gemini_runtime_models(self) -> None:
        set_gemini_models(frozenset({"gemini-2.5-pro"}))
        pm = _pm()
        assert pm.is_known_model("gemini-2.5-pro") is True

    def test_kimi_auto_alias(self) -> None:
        pm = _pm()
        assert pm.is_known_model("kimi-auto") is True

    def test_reasonix_default_model(self) -> None:
        pm = _pm()
        assert pm.is_known_model("deepseek-v4-flash") is True

    def test_reasonix_runtime_models(self) -> None:
        set_reasonix_models(frozenset({"deepseek-reasoner"}))
        pm = _pm()
        assert pm.is_known_model("deepseek-reasoner") is True

    def test_unknown_model(self) -> None:
        pm = _pm()
        assert pm.is_known_model("nonexistent-model") is False

    def test_codex_via_cache(self) -> None:
        cache = MagicMock()
        cache.validate_model.return_value = True
        pm = _pm(codex_cache_fn=lambda: cache)
        assert pm.is_known_model("o3-mini") is True
        cache.validate_model.assert_called_once_with("o3-mini")

    def test_codex_cache_none(self) -> None:
        pm = _pm(codex_cache_fn=lambda: None)
        assert pm.is_known_model("o3-mini") is False


# ---------------------------------------------------------------------------
# default_model_for_provider
# ---------------------------------------------------------------------------


class TestDefaultModelForProvider:
    def test_claude_default(self) -> None:
        pm = _pm(model="opus", provider="claude")
        assert pm.default_model_for_provider("claude") == "opus"

    def test_claude_when_not_active_provider(self) -> None:
        pm = _pm(model="o3-mini", provider="codex")
        assert pm.default_model_for_provider("claude") == "sonnet"

    def test_codex_with_cache_default(self) -> None:
        model = MagicMock()
        model.is_default = True
        model.id = "o4-mini"
        cache = MagicMock()
        cache.models = [model]
        pm = _pm(codex_cache_fn=lambda: cache)
        assert pm.default_model_for_provider("codex") == "o4-mini"

    def test_codex_no_cache(self) -> None:
        pm = _pm()
        assert pm.default_model_for_provider("codex") == ""

    def test_gemini(self) -> None:
        pm = _pm()
        assert pm.default_model_for_provider("gemini") == DEFAULT_GEMINI_MODEL

    def test_kimi(self) -> None:
        pm = _pm()
        assert pm.default_model_for_provider("kimi") == DEFAULT_KIMI_MODEL

    def test_cursor(self) -> None:
        pm = _pm()
        assert pm.default_model_for_provider("cursor") == DEFAULT_CURSOR_MODEL

    def test_reasonix(self) -> None:
        pm = _pm()
        assert pm.default_model_for_provider("reasonix") == DEFAULT_REASONIX_MODEL

    def test_unknown_provider(self) -> None:
        pm = _pm()
        assert pm.default_model_for_provider("unknown") == ""


# ---------------------------------------------------------------------------
# apply_auth_results
# ---------------------------------------------------------------------------


class TestApplyAuthResults:
    def test_updates_available_providers(self) -> None:
        pm = _pm()
        cli_service = MagicMock()

        auth_status = MagicMock()
        auth_status.AUTHENTICATED = "auth"
        auth_status.INSTALLED = "inst"

        result_claude = MagicMock()
        result_claude.status = "auth"
        result_claude.is_authenticated = True

        result_codex = MagicMock()
        result_codex.status = "inst"
        result_codex.is_authenticated = False

        pm.apply_auth_results(
            {"claude": result_claude, "codex": result_codex},
            auth_status_enum=auth_status,
            cli_service=cli_service,
        )
        assert pm.available_providers == frozenset({"claude"})
        cli_service.update_available_providers.assert_called_once_with(frozenset({"claude"}))

    def test_all_authenticated(self) -> None:
        pm = _pm()
        cli_service = MagicMock()

        auth_status = MagicMock()
        auth_status.AUTHENTICATED = "auth"
        auth_status.INSTALLED = "inst"

        results = {}
        for name in ("claude", "codex", "gemini"):
            r = MagicMock()
            r.status = "auth"
            r.is_authenticated = True
            results[name] = r

        pm.apply_auth_results(
            results,
            auth_status_enum=auth_status,
            cli_service=cli_service,
        )
        assert pm.available_providers == frozenset({"claude", "codex", "gemini"})


# ---------------------------------------------------------------------------
# active_provider_name
# ---------------------------------------------------------------------------


class TestActiveProviderName:
    def test_claude(self) -> None:
        pm = _pm(model="sonnet", provider="claude")
        assert pm.active_provider_name == "Claude Code"

    def test_gemini(self) -> None:
        pm = _pm(model="auto", provider="gemini")
        assert pm.active_provider_name == "Gemini"

    def test_codex(self) -> None:
        pm = _pm(model="o3-mini", provider="codex")
        assert pm.active_provider_name == "Codex"

    def test_kimi(self) -> None:
        pm = _pm(model="kimi-code/kimi-for-coding", provider="kimi")
        assert pm.active_provider_name == "Kimi"

    def test_cursor(self) -> None:
        pm = _pm(model="auto", provider="cursor")
        assert pm.active_provider_name == "Cursor"

    def test_reasonix(self) -> None:
        pm = _pm(model="deepseek-v4-flash", provider="reasonix")
        assert pm.active_provider_name == "Reasonix"


# ---------------------------------------------------------------------------
# on_gemini_models_refresh
# ---------------------------------------------------------------------------


class TestOnGeminiModelsRefresh:
    def test_updates_known_model_ids(self) -> None:
        pm = _pm()
        assert not pm.is_known_model("gemini-2.5-pro")

        pm.on_gemini_models_refresh(("gemini-2.5-pro", "gemini-2.5-flash"))
        assert pm.is_known_model("gemini-2.5-pro")
        assert pm.is_known_model("gemini-2.5-flash")

    def test_invalidates_gemini_api_key_mode(self) -> None:
        pm = _pm()
        pm._gemini_api_key_mode = True
        pm.on_gemini_models_refresh(("gemini-2.5-pro",))
        assert pm._gemini_api_key_mode is None


# ---------------------------------------------------------------------------
# on_kimi_models_refresh
# ---------------------------------------------------------------------------


class TestOnKimiModelsRefresh:
    def test_updates_known_model_ids(self) -> None:
        pm = _pm()
        assert not pm.is_known_model("kimi-k2-0905-preview")

        pm.on_kimi_models_refresh(("kimi-k2-0905-preview",))
        assert pm.is_known_model("kimi-k2-0905-preview")


# ---------------------------------------------------------------------------
# on_cursor_models_refresh
# ---------------------------------------------------------------------------


class TestOnCursorModelsRefresh:
    def test_updates_known_model_ids(self) -> None:
        pm = _pm()
        assert not pm.is_known_model("composer-2.5-fast")

        pm.on_cursor_models_refresh(("composer-2.5-fast",))
        assert pm.is_known_model("composer-2.5-fast")


# ---------------------------------------------------------------------------
# on_reasonix_models_refresh
# ---------------------------------------------------------------------------


class TestOnReasonixModelsRefresh:
    def test_updates_known_model_ids(self) -> None:
        pm = _pm()
        assert not pm.is_known_model("deepseek-reasoner")

        pm.on_reasonix_models_refresh(("deepseek-reasoner",))
        assert pm.is_known_model("deepseek-reasoner")
