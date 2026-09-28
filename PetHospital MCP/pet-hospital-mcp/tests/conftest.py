"""Shared test fixtures.

Every test drives the real ASGI app over an in-process HTTP transport, with the
Go backend replaced by `httpx.MockTransport`. No test ever opens a socket, so
the suite can never reach a real Pet Hospital service.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from pet_hospital_mcp import server as server_module
from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.logging_config import configure_logging
from pet_hospital_mcp.rest_client import PetHospitalClient

# The protocol revision this service speaks. Asserted throughout the suite.
PROTOCOL_VERSION = "2026-07-28"

#: Host used for the in-process transport. The port matters: the SDK's loopback
#: DNS-rebinding guard matches allowed hosts as `127.0.0.1:*`, so a bare host
#: without a port is rejected with 421.
TEST_BASE_URL = "http://127.0.0.1:8000"

BACKEND_BASE_URL = "http://127.0.0.1:8080"
PETS_PATH = "/api/v1/pets"

Handler = Callable[[httpx.Request], httpx.Response]


def request_meta() -> dict[str, Any]:
    """The per-request envelope every 2026-07-28 request must carry.

    Capabilities are declared per request rather than once at initialization —
    that is what makes the transport stateless.
    """
    return {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "pytest", "version": "0"},
    }


def pets_data(
    *,
    items: list[dict[str, Any]] | None = None,
    total: int | None = None,
    page: int = 1,
    page_size: int = 20,
    total_pages: int | None = None,
    total_cost: float = 0.0,
) -> dict[str, Any]:
    """Build a Go-shaped `data` object for `GET /api/v1/pets`."""
    if items is None:
        items = [pet()]
    return {
        "items": items,
        "total": len(items) if total is None else total,
        "page": page,
        "pageSize": page_size,
        "totalPages": (1 if items else 0) if total_pages is None else total_pages,
        "totalCost": total_cost,
    }


def pet(**overrides: Any) -> dict[str, Any]:
    """A single pet record as the Go service serialises it."""
    base: dict[str, Any] = {
        "id": "PET-000001",
        "name": "旺财",
        "species": "犬",
        "breed": "柯基",
        "gender": "公",
        "ageMonths": 24,
        "color": "黄白",
        "chipNo": "CHIP-000001",
        "ownerName": "张三",
        "ownerPhone": "13800001111",
        "ownerAddr": "北京市朝阳区示例路 1 号",
        "doctor": "李医生",
        "disease": "肠胃炎",
        "status": "已康复",
        "allergy": "无",
        "records": [
            {
                "id": "MR-1",
                "visitDate": "2024-01-01",
                "doctor": "李医生",
                "diagnosis": "肠胃炎",
                "prescription": ["益生菌"],
                "weightKg": 12.5,
                "charge": 320.5,
                "createdAt": "2024-01-01T10:00:00Z",
            }
        ],
        "charges": [
            {"id": "CH-1", "item": "血常规", "category": "检查", "amount": 320.5, "doctor": "李医生", "date": "2024-01-01"}
        ],
        "totalCost": 320.5,
        "visitCount": 1,
        "createdAt": "2026-01-01T00:00:00+08:00",
        "updatedAt": "2026-01-01T00:00:00+08:00",
    }
    base.update(overrides)
    return base


def backend_ok(data: dict[str, Any] | None = None) -> httpx.Response:
    """A well-formed Go success envelope."""
    return httpx.Response(200, json={"code": 200, "message": "ok", "data": data if data is not None else pets_data()})


def backend_error(status: int, *, code: int | None = None, message: str = "出错了") -> httpx.Response:
    """A Go error envelope (no `data` key, matching the real service)."""
    return httpx.Response(status, json={"code": code if code is not None else status, "message": message})


class RecordingBackend:
    """A `MockTransport` handler that records every request it receives."""

    def __init__(self, *responses: httpx.Response | Handler) -> None:
        self._responses = list(responses) or [backend_ok()]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        response = self._responses[index]
        if callable(response):
            return response(request)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def attempts(self) -> int:
        return len(self.requests)

    def query(self, index: int = 0) -> dict[str, str]:
        return dict(httpx.URL(str(self.requests[index].url)).params)

    def path(self, index: int = 0) -> str:
        return httpx.URL(str(self.requests[index].url)).path


class McpHttpClient:
    """A minimal raw JSON-RPC client for the MCP endpoint.

    Deliberately hand-rolled rather than using the SDK's `Client`: it proves the
    wire contract (headers, envelope, absence of a session id) instead of hiding
    it behind the SDK.
    """

    def __init__(self, client: httpx.AsyncClient, path: str = "/mcp") -> None:
        self._client = client
        self._path = path
        self._next_id = 0

    async def post_raw(
        self,
        body: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        method: str | None = None,
    ) -> httpx.Response:
        request_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if method is not None:
            request_headers["Mcp-Method"] = method
        request_headers.update(headers or {})
        return await self._client.post(self._path, json=body, headers=request_headers)

    async def post_content(
        self,
        raw: bytes,
        *,
        method: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """POST a pre-serialised body.

        Needed for payloads `json.dumps` refuses to emit (`NaN`, `Infinity`),
        which a hostile client can still put on the wire.
        """
        request_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if method is not None:
            request_headers["Mcp-Method"] = method
        request_headers.update(extra_headers or {})
        return await self._client.post(self._path, content=raw, headers=request_headers)

    async def rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        envelope: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        self._next_id += 1
        payload = dict(params or {})
        if envelope:
            payload["_meta"] = request_meta()
        headers = dict(extra_headers or {})
        # `Mcp-Name` must echo the named body param for name-bearing methods.
        if isinstance(payload.get("name"), str):
            headers.setdefault("Mcp-Name", payload["name"])
        body = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": payload}
        return await self.post_raw(body, headers=headers, method=method)

    async def json_rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.rpc(method, params)
        assert response.status_code == 200, response.text
        return response.json()

    async def list_tools(self) -> list[dict[str, Any]]:
        body = await self.json_rpc("tools/list", {})
        return body["result"]["tools"]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        body = await self.json_rpc("tools/call", {"name": name, "arguments": arguments})
        return body["result"]


@contextlib.asynccontextmanager
async def open_mcp(
    backend: RecordingBackend | Handler,
    *,
    mcp_path: str = "/mcp",
    settings: Settings | None = None,
    **client_kwargs: Any,
) -> AsyncIterator[tuple[McpHttpClient, PetHospitalClient, Settings]]:
    """Stand up the MCP ASGI app against a mock backend and yield a client."""
    resolved = settings or Settings(base_url=BACKEND_BASE_URL)
    client = PetHospitalClient(
        resolved.base_url,
        # Retry tests would otherwise sleep through real backoff.
        backoff_seconds=0.0,
        transport=httpx.MockTransport(backend),
        **client_kwargs,
    )
    app = server_module.build_app(client, resolved)
    # `httpx.ASGITransport` does not run lifespan; the SDK's session manager
    # starts there, so drive it explicitly.
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=TEST_BASE_URL) as http:
            yield McpHttpClient(http, mcp_path), client, resolved
    await client.aclose()


@pytest.fixture(autouse=True)
def _quiet_logging() -> None:
    """Keep our JSON logging configured so log assertions are deterministic."""
    configure_logging("INFO")


@pytest.fixture
def backend() -> RecordingBackend:
    return RecordingBackend(backend_ok())


def parse_error(result: dict[str, Any]) -> dict[str, Any]:
    """Extract the unified error envelope from a tool result."""
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "error" in structured:
        return structured["error"]
    return json.loads(result["content"][0]["text"])["error"]
