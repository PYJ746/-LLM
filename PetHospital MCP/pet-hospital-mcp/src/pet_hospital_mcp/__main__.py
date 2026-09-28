"""Console entry point: `python -m pet_hospital_mcp`.

Starts the stateless Streamable HTTP MCP server with uvicorn. Configuration is
read from the environment (see `config.Settings`).
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import ConfigError, Settings
from .errors import ToolCallError
from .logging_config import configure_logging, get_logger
from .server import serve

DESCRIPTION = """\
Stateless MCP server (protocol 2026-07-28) exposing the Go Pet Hospital REST API.

All configuration comes from the environment:
  PET_HOSPITAL_BASE_URL  Go REST API base URL   (default http://127.0.0.1:8080)
  MCP_HOST               bind address           (default 127.0.0.1)
  MCP_PORT               bind port              (default 8000)
  MCP_PATH               MCP endpoint path      (default /mcp)
  LOG_LEVEL              log level              (default INFO)
  HTTP_TIMEOUT_SECONDS   upstream timeout       (default 10)
  HTTP_MAX_RETRIES       upstream retry budget  (default 2)
"""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pet-hospital-mcp",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # Unknown arguments are an error rather than being silently ignored: a typo
    # in what looks like a flag should not quietly start a server.
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _parse_args(argv)

    logger = get_logger(__name__)

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        # Configuration is read before logging is configured; keep this on stderr
        # in a plain form so the operator sees the real reason.
        configure_logging("INFO")
        logger.error("invalid configuration: %s", exc)
        return 2

    configure_logging(settings.log_level)

    try:
        import anyio

        anyio.run(serve, settings)
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        return 0
    except ToolCallError as exc:  # pragma: no cover - defensive
        logger.error("startup failed: %s", exc.message)
        return 1
    except Exception:  # pragma: no cover - defensive
        logger.exception("fatal error")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
