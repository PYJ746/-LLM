"""Environment-driven configuration."""

from __future__ import annotations

import pytest

from pet_hospital_mcp.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORT,
    ConfigError,
    Settings,
)


def test_defaults_match_the_documented_values():
    settings = Settings.from_env({})

    assert settings.base_url == DEFAULT_BASE_URL == "http://127.0.0.1:8080"
    assert settings.mcp_host == DEFAULT_MCP_HOST == "127.0.0.1"
    assert settings.mcp_port == DEFAULT_MCP_PORT == 8000
    assert settings.pets_endpoint == "http://127.0.0.1:8080/api/v1/pets"


def test_mcp_binds_to_loopback_unless_told_otherwise():
    assert Settings.from_env({}).mcp_host == "127.0.0.1"


def test_backend_url_host_port_and_path_are_configurable():
    settings = Settings.from_env(
        {
            "PET_HOSPITAL_BASE_URL": "http://10.0.0.9:9000/",
            "MCP_HOST": "0.0.0.0",
            "MCP_PORT": "9100",
            "MCP_PATH": "/rpc",
        }
    )

    assert settings.base_url == "http://10.0.0.9:9000", "trailing slash is normalised away"
    assert settings.mcp_host == "0.0.0.0"
    assert settings.mcp_port == 9100
    assert settings.mcp_path == "/rpc"
    assert settings.pets_endpoint == "http://10.0.0.9:9000/api/v1/pets"


def test_blank_values_fall_back_to_defaults():
    settings = Settings.from_env({"MCP_HOST": "   ", "PET_HOSPITAL_BASE_URL": ""})

    assert settings.mcp_host == DEFAULT_MCP_HOST
    assert settings.base_url == DEFAULT_BASE_URL


@pytest.mark.parametrize("value", ["0", "-1", "65536", "not-a-number", "80.5"])
def test_invalid_port_is_rejected(value):
    with pytest.raises(ConfigError):
        Settings.from_env({"MCP_PORT": value})


@pytest.mark.parametrize("value", ["ftp://host", "127.0.0.1:8080", "javascript:alert(1)"])
def test_non_http_base_url_is_rejected(value):
    with pytest.raises(ConfigError):
        Settings.from_env({"PET_HOSPITAL_BASE_URL": value})


@pytest.mark.parametrize("value", ["mcp", "rpc/mcp", "mcp/"])
def test_mcp_path_must_be_absolute(value):
    with pytest.raises(ConfigError):
        Settings.from_env({"MCP_PATH": value})


def test_blank_mcp_path_falls_back_to_the_default():
    assert Settings.from_env({"MCP_PATH": ""}).mcp_path == "/mcp"


def test_timeout_and_retry_bounds_are_enforced():
    with pytest.raises(ConfigError):
        Settings.from_env({"HTTP_TIMEOUT_SECONDS": "0"})
    with pytest.raises(ConfigError):
        Settings.from_env({"HTTP_MAX_RETRIES": "-1"})

    settings = Settings.from_env({"HTTP_TIMEOUT_SECONDS": "2.5", "HTTP_MAX_RETRIES": "0"})
    assert settings.http_timeout_seconds == 2.5
    assert settings.http_max_retries == 0


def test_from_env_does_not_leak_process_environment(monkeypatch):
    monkeypatch.setenv("MCP_PORT", "1234")

    Settings.from_env({"MCP_PORT": "4321"})

    import os

    assert os.environ["MCP_PORT"] == "1234"
