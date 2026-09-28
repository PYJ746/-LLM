"""Runtime configuration for the Pet Hospital MCP service.

Every setting is read from the environment so the process can be pointed at a
different go backend or bound to a different address without code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8000
DEFAULT_MCP_PATH = "/mcp"

# MCP only ever listens on loopback unless the operator explicitly overrides it.
ENV_BASE_URL = "PET_HOSPITAL_BASE_URL"
ENV_MCP_HOST = "MCP_HOST"
ENV_MCP_PORT = "MCP_PORT"
ENV_MCP_PATH = "MCP_PATH"
ENV_LOG_LEVEL = "LOG_LEVEL"
ENV_HTTP_TIMEOUT = "HTTP_TIMEOUT_SECONDS"
ENV_HTTP_RETRIES = "HTTP_MAX_RETRIES"


class ConfigError(ValueError):
    """Raised when an environment variable cannot be interpreted."""


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved service settings."""

    base_url: str = DEFAULT_BASE_URL
    mcp_host: str = DEFAULT_MCP_HOST
    mcp_port: int = DEFAULT_MCP_PORT
    mcp_path: str = DEFAULT_MCP_PATH
    log_level: str = "INFO"
    http_timeout_seconds: float = 10.0
    http_max_retries: int = 2

    @property
    def pets_endpoint(self) -> str:
        """Absolute URL of the Go `GET /api/v1/pets` endpoint."""
        return f"{self.base_url.rstrip('/')}/api/v1/pets"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        """Build settings from the process environment (or a supplied mapping)."""
        if env is not None:
            # Temporarily shadow the process environment so callers (and tests)
            # can supply an explicit mapping without leaking state.
            previous = dict(os.environ)
            try:
                os.environ.clear()
                os.environ.update(env)
                return cls._read()
            finally:
                os.environ.clear()
                os.environ.update(previous)
        return cls._read()

    @classmethod
    def _read(cls) -> Settings:
        base_url = _env_str(ENV_BASE_URL, DEFAULT_BASE_URL).rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError(f"{ENV_BASE_URL} must be an http(s) URL, got {base_url!r}")

        mcp_path = _env_str(ENV_MCP_PATH, DEFAULT_MCP_PATH)
        if not mcp_path.startswith("/"):
            raise ConfigError(f"{ENV_MCP_PATH} must start with '/', got {mcp_path!r}")

        return cls(
            base_url=base_url,
            mcp_host=_env_str(ENV_MCP_HOST, DEFAULT_MCP_HOST),
            mcp_port=_env_int(ENV_MCP_PORT, DEFAULT_MCP_PORT, minimum=1, maximum=65535),
            mcp_path=mcp_path,
            log_level=_env_str(ENV_LOG_LEVEL, "INFO").upper(),
            http_timeout_seconds=_env_float(
                ENV_HTTP_TIMEOUT, 10.0, minimum=0.1, maximum=300.0
            ),
            http_max_retries=_env_int(ENV_HTTP_RETRIES, 2, minimum=0, maximum=10),
        )
