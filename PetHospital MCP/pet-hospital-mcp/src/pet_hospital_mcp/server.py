"""MCPServer assembly: one stateless Streamable HTTP app exposing `list_pets`.

Targets MCP protocol revision 2026-07-28 via the SDK 2.x `MCPServer`. There is
deliberately no `initialize` handshake, no `Mcp-Session-Id`, no session store
and no SSE resumption: every request is a self-contained POST.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse, PlainTextResponse

from . import __version__
from .config import Settings
from .errors import ErrorCode, ToolCallError, error_result
from .logging_config import get_logger
from .rest_client import PetHospitalClient
from .tools import list_pets
from .validation import StrictToolInputMiddleware

if TYPE_CHECKING:
    from starlette.applications import Starlette

logger = get_logger(__name__)

SERVER_NAME = "pet-hospital-mcp"
HEALTH_PATH = "/health"

SERVER_INSTRUCTIONS = """\
本服务把本地 Go 宠物医院 REST API 暴露给 AI Agent。
当前仅提供 list_pets 一个工具，用于查询宠物档案列表（过滤 / 排序 / 分页）。
宠物病历与收费明细包含在 list_pets 返回的 items[].records / items[].charges 中。
"""


def build_server(client: PetHospitalClient, settings: Settings) -> MCPServer:
    """Create the `MCPServer` with its tools, middleware and health route."""
    mcp: MCPServer = MCPServer(
        name=SERVER_NAME,
        title="Pet Hospital MCP",
        description="宠物医院档案查询 MCP 服务（Go REST API 的 MCP 封装）",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
        middleware=[StrictToolInputMiddleware()],
    )

    list_pets.register(mcp, client)
    _forbid_unknown_properties(mcp, list_pets.TOOL_NAME)

    @mcp.custom_route(HEALTH_PATH, methods=["GET"], name="health")
    async def health(_request: Any) -> JSONResponse:
        """Liveness probe for the MCP service itself (does not touch the backend)."""
        return JSONResponse(
            {
                "status": "ok",
                "service": SERVER_NAME,
                "version": __version__,
                "protocolVersion": _modern_protocol_version(),
                "upstream": settings.base_url,
            }
        )

    return mcp


def _forbid_unknown_properties(mcp: MCPServer, tool_name: str) -> None:
    """Advertise `additionalProperties: false` for a tool's input schema.

    The SDK derives the schema from the function signature and omits the keyword,
    which understates our contract: the strict-validation middleware genuinely
    rejects unknown fields. Declaring it keeps the published schema honest, and
    is also what the modern transport's `Mcp-Param-*` header validation reads.
    """
    try:
        tool = mcp._tool_manager.get_tool(tool_name)  # noqa: SLF001
    except AttributeError:  # pragma: no cover - defensive across SDK revisions
        logger.warning("could not access tool manager to tighten input schema")
        return
    if tool is None:  # pragma: no cover - defensive
        return
    schema = tool.parameters
    if isinstance(schema, dict) and schema.get("type") == "object":
        schema["additionalProperties"] = False


def _modern_protocol_version() -> str:
    """The modern protocol revision this service speaks."""
    from mcp_types.version import LATEST_MODERN_VERSION

    return LATEST_MODERN_VERSION


class PostOnlyMcpEndpoint:
    """Reject `GET`/`DELETE` on the MCP endpoint with `405`.

    MCP 2026-07-28 removed both: the standalone server-to-client `GET` stream and
    the `DELETE` session teardown. The SDK still serves them for older protocol
    revisions, and a bodyless `GET` carries no version header for it to route on,
    so it opens a legacy SSE stream and waits — a hung connection rather than the
    `405` the revision mandates.

    This guard is the whole of our involvement with the legacy transports: it
    refuses them, and every other request falls through to the SDK untouched.
    Lifespan and routing are unaffected because it is installed as ordinary
    Starlette middleware.
    """

    def __init__(self, app: Any, *, path: str) -> None:
        self._app = app
        self._path = path

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope["type"] == "http"
            and scope.get("path") == self._path
            and scope.get("method") in ("GET", "DELETE")
        ):
            response = PlainTextResponse(
                "Method Not Allowed: this endpoint accepts POST only",
                status_code=405,
                headers={"Allow": "POST"},
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def app_from_server(mcp: MCPServer, settings: Settings) -> Starlette:
    """Assemble the ASGI app around an already-built `MCPServer`."""
    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        stateless_http=True,
        json_response=True,
        host=settings.mcp_host,
    )
    app.add_middleware(PostOnlyMcpEndpoint, path=settings.mcp_path)
    return app


def build_app(client: PetHospitalClient, settings: Settings) -> Starlette:
    """Build the stateless Streamable HTTP ASGI app."""
    return app_from_server(build_server(client, settings), settings)


async def serve(settings: Settings | None = None) -> None:
    """Run the service until interrupted."""
    from .logging_config import configure_logging

    settings = settings or Settings.from_env()

    client = PetHospitalClient(
        settings.base_url,
        timeout_seconds=settings.http_timeout_seconds,
        max_retries=settings.http_max_retries,
    )
    mcp = build_server(client, settings)

    # Must run *after* `build_server`: constructing an `MCPServer` calls the
    # SDK's own `logging.basicConfig`, which is a no-op only once the root
    # logger already has a handler. Installing ours first would leave the SDK
    # free to install its plain-text handler instead of our JSON formatter.
    configure_logging(settings.log_level)

    logger.info(
        "starting pet-hospital-mcp",
        extra={
            "event": {
                "mcp_host": settings.mcp_host,
                "mcp_port": settings.mcp_port,
                "mcp_path": settings.mcp_path,
                "upstream": settings.base_url,
                "protocol_version": _modern_protocol_version(),
            }
        },
    )
    try:
        # Serve the *same* app object `build_app` produces, rather than letting
        # the SDK assemble its own: otherwise the running service would differ
        # from the one under test (no `PostOnlyMcpEndpoint`, no `/health`).
        # uvicorn drives the Starlette lifespan, which is what starts the
        # SDK's session manager.
        import uvicorn

        config = uvicorn.Config(
            app_from_server(mcp, settings),
            host=settings.mcp_host,
            port=settings.mcp_port,
            log_level=settings.log_level.lower(),
            # `log_config=None` leaves uvicorn's loggers propagating to root, so
            # access/error lines go through our JSON formatter instead of
            # uvicorn's plain-text default. Without it the process emits two
            # incompatible log formats.
            log_config=None,
        )
        await uvicorn.Server(config).serve()
    finally:
        await client.aclose()


__all__ = [
    "HEALTH_PATH",
    "SERVER_NAME",
    "PostOnlyMcpEndpoint",
    "app_from_server",
    "build_app",
    "build_server",
    "serve",
    "ErrorCode",
    "ToolCallError",
    "error_result",
]
