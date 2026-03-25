"""Unit tests for the configuration system."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_forge.config import (
    ForgeConfig,
    _collect_env_overrides,
    _deep_merge,
    _flatten_cli_overrides,
    _load_toml,
    load_config,
)

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    import pytest


# ---------------------------------------------------------------------------
# Deep Merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    """Tests for the _deep_merge helper."""

    def test_flat_override(self) -> None:
        base = {"a": 1, "b": 2}
        override = {"b": 99}
        assert _deep_merge(base, override) == {"a": 1, "b": 99}

    def test_nested_merge(self) -> None:
        base = {"x": {"a": 1, "b": 2}, "y": 10}
        override = {"x": {"b": 99, "c": 3}}
        result = _deep_merge(base, override)
        assert result == {"x": {"a": 1, "b": 99, "c": 3}, "y": 10}

    def test_override_replaces_non_dict(self) -> None:
        base = {"x": "string"}
        override = {"x": {"nested": True}}
        assert _deep_merge(base, override) == {"x": {"nested": True}}

    def test_no_mutation(self) -> None:
        base = {"a": {"b": 1}}
        override = {"a": {"b": 2}}
        _deep_merge(base, override)
        assert base == {"a": {"b": 1}}  # original unchanged


# ---------------------------------------------------------------------------
# TOML Loading
# ---------------------------------------------------------------------------


class TestTOMLLoading:
    """Tests for _load_toml."""

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        assert _load_toml(tmp_path / "nonexistent.toml") == {}

    def test_valid_toml(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "test.toml"
        toml_file.write_text("[agent]\nmax_iterations = 42\n")
        result = _load_toml(toml_file)
        assert result == {"agent": {"max_iterations": 42}}

    def test_malformed_toml(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "bad.toml"
        toml_file.write_text("this is not valid toml [[[")
        assert _load_toml(toml_file) == {}


# ---------------------------------------------------------------------------
# Environment Variable Overrides
# ---------------------------------------------------------------------------


class TestEnvOverrides:
    """Tests for _collect_env_overrides."""

    def test_agent_max_iterations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_FORGE_AGENT_MAX_ITERATIONS", "10")
        result = _collect_env_overrides()
        assert result == {"agent": {"max_iterations": 10}}

    def test_sandbox_memory_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_FORGE_SANDBOX_MEMORY_LIMIT", "1g")
        result = _collect_env_overrides()
        assert result == {"sandbox": {"memory_limit": "1g"}}

    def test_bool_coercion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_FORGE_SANDBOX_NETWORK_ENABLED", "true")
        result = _collect_env_overrides()
        assert result == {"sandbox": {"network_enabled": True}}

    def test_float_coercion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_FORGE_AGENT_TEMPERATURE", "0.7")
        result = _collect_env_overrides()
        assert result == {"agent": {"temperature": 0.7}}

    def test_unknown_section_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_FORGE_UNKNOWN_FIELD", "value")
        result = _collect_env_overrides()
        assert result == {}

    def test_unknown_field_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_FORGE_AGENT_NONEXISTENT_FIELD", "value")
        result = _collect_env_overrides()
        assert result == {}

    def test_queue_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_FORGE_QUEUE_BACKEND", "redis")
        result = _collect_env_overrides()
        assert result == {"queue": {"backend": "redis"}}

    def test_service_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_FORGE_SERVICE_PORT", "8123")
        result = _collect_env_overrides()
        assert result == {"service": {"port": 8123}}

    def test_service_auth_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_FORGE_SERVICE_AUTH_ENABLED", "true")
        result = _collect_env_overrides()
        assert result == {"service": {"auth_enabled": True}}


# ---------------------------------------------------------------------------
# CLI Override Mapping
# ---------------------------------------------------------------------------


class TestCLIOverrides:
    """Tests for _flatten_cli_overrides."""

    def test_dotted_keys(self) -> None:
        overrides = {"agent.max_iterations": 5, "sandbox.memory_limit": "2g"}
        result = _flatten_cli_overrides(overrides)
        assert result == {
            "agent": {"max_iterations": 5},
            "sandbox": {"memory_limit": "2g"},
        }

    def test_none_values_skipped(self) -> None:
        overrides: dict[str, Any] = {"agent.max_iterations": None, "agent.temperature": 0.5}
        result = _flatten_cli_overrides(overrides)
        assert result == {"agent": {"temperature": 0.5}}

    def test_bare_key_goes_to_agent(self) -> None:
        overrides = {"max_iterations": 10}
        result = _flatten_cli_overrides(overrides)
        assert result == {"agent": {"max_iterations": 10}}


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    """Test that built-in defaults are correct per spec."""

    def test_default_config(self, tmp_path: Path) -> None:
        # Load with no files and no env vars
        cfg = load_config(
            project_path=tmp_path / "nonexistent.toml",
            user_path=tmp_path / "also-nonexistent.toml",
        )
        assert isinstance(cfg, ForgeConfig)
        assert cfg.agent.max_iterations == 25
        assert cfg.agent.max_tokens_per_run == 200_000
        assert cfg.agent.default_provider == "gemini"
        assert cfg.agent.default_model == "gemini-3.1-flash-lite-preview"
        assert cfg.agent.temperature == 0.0
        assert cfg.sandbox.image == "agent-forge-sandbox:latest"
        assert cfg.sandbox.memory_limit == "512m"
        assert cfg.sandbox.timeout_seconds == 300
        assert cfg.sandbox.network_enabled is False
        assert cfg.queue.backend == "memory"
        assert cfg.queue.max_concurrent_runs == 4
        assert cfg.logging.level == "INFO"
        assert cfg.logging.format == "text"
        assert cfg.service.host == "127.0.0.1"
        assert cfg.service.port == 8000
        assert cfg.service.auth_enabled is False
        assert cfg.service.api_key_header == "X-Agent-Forge-API-Key"
        assert cfg.service.allow_local_path_sources is False
        assert cfg.service.max_source_size_bytes == 50_000_000

    def test_default_providers(self, tmp_path: Path) -> None:
        cfg = load_config(
            project_path=tmp_path / "x.toml",
            user_path=tmp_path / "y.toml",
        )
        assert "gemini" in cfg.providers
        assert cfg.providers["gemini"].api_key_env == "GEMINI_API_KEY"
        assert cfg.providers["openai"].default_model == "gpt-4o"
        assert cfg.providers["anthropic"].api_key_env == "ANTHROPIC_API_KEY"


# ---------------------------------------------------------------------------
# Full Precedence Integration
# ---------------------------------------------------------------------------


class TestPrecedence:
    """Test the full 5-layer precedence chain."""

    def test_project_overrides_defaults(self, tmp_path: Path) -> None:
        project_toml = tmp_path / "agent-forge.toml"
        project_toml.write_text("[agent]\nmax_iterations = 50\n")

        cfg = load_config(
            project_path=project_toml,
            user_path=tmp_path / "no-user.toml",
        )
        assert cfg.agent.max_iterations == 50
        # Other defaults still intact
        assert cfg.agent.temperature == 0.0

    def test_user_config_applies(self, tmp_path: Path) -> None:
        user_toml = tmp_path / "config.toml"
        user_toml.write_text('[sandbox]\nmemory_limit = "2g"\n')

        cfg = load_config(
            project_path=tmp_path / "no-project.toml",
            user_path=user_toml,
        )
        assert cfg.sandbox.memory_limit == "2g"

    def test_project_overrides_user(self, tmp_path: Path) -> None:
        user_toml = tmp_path / "config.toml"
        user_toml.write_text("[agent]\nmax_iterations = 10\n")

        project_toml = tmp_path / "agent-forge.toml"
        project_toml.write_text("[agent]\nmax_iterations = 30\n")

        cfg = load_config(project_path=project_toml, user_path=user_toml)
        assert cfg.agent.max_iterations == 30  # project wins

    def test_env_overrides_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_toml = tmp_path / "agent-forge.toml"
        project_toml.write_text("[agent]\nmax_iterations = 50\n")

        monkeypatch.setenv("AGENT_FORGE_AGENT_MAX_ITERATIONS", "99")

        cfg = load_config(
            project_path=project_toml,
            user_path=tmp_path / "no-user.toml",
        )
        assert cfg.agent.max_iterations == 99  # env wins

    def test_cli_overrides_everything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # User config
        user_toml = tmp_path / "config.toml"
        user_toml.write_text("[agent]\nmax_iterations = 10\n")

        # Project config
        project_toml = tmp_path / "agent-forge.toml"
        project_toml.write_text("[agent]\nmax_iterations = 50\n")

        # Env var
        monkeypatch.setenv("AGENT_FORGE_AGENT_MAX_ITERATIONS", "99")

        # CLI flag
        cfg = load_config(
            cli_overrides={"agent.max_iterations": 3},
            project_path=project_toml,
            user_path=user_toml,
        )
        assert cfg.agent.max_iterations == 3  # CLI wins over all

    def test_service_cli_overrides(self, tmp_path: Path) -> None:
        cfg = load_config(
            cli_overrides={"service.port": 9000},
            project_path=tmp_path / "x.toml",
            user_path=tmp_path / "y.toml",
        )
        assert cfg.service.port == 9000

    def test_partial_overrides_preserve_other_fields(self, tmp_path: Path) -> None:
        project_toml = tmp_path / "agent-forge.toml"
        project_toml.write_text("[agent]\ntemperature = 0.8\n")

        cfg = load_config(
            cli_overrides={"agent.max_iterations": 5},
            project_path=project_toml,
            user_path=tmp_path / "no-user.toml",
        )
        assert cfg.agent.max_iterations == 5  # CLI
        assert cfg.agent.temperature == 0.8  # project TOML
        assert cfg.agent.default_provider == "gemini"  # default
