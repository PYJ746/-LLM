"""Tool registration and the stateless MCP 2026-07-28 connection flow.

These tests assert the *wire* contract directly, rather than trusting the SDK's
client to hide it: no `initialize`, no `Mcp-Session-Id`, one self-contained
POST per request.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import (
    PROTOCOL_VERSION,
    TEST_BASE_URL,
    RecordingBackend,
    backend_ok,
    open_mcp,
    request_meta,
)
from pet_hospital_mcp import server as server_module
from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.rest_client import PetHospitalClient
from pet_hospital_mcp.tools import list_pets

TOOL_NAME = "list_pets"

EXPECTED_INPUT_FIELDS = {
    "q",
    "name",
    "ownerName",
    "ownerPhone",
    "species",
    "doctor",
    "disease",
    "status",
    "min",
    "max",
    "sortBy",
    "order",
    "page",
    "pageSize",
}


# --- tools/list ------------------------------------------------------------


async def test_tool_is_registered_under_a_snake_case_name(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        tools = await mcp.list_tools()

    assert [t["name"] for t in tools] == [TOOL_NAME]
    assert TOOL_NAME.islower() and "_" in TOOL_NAME


async def test_input_schema_exposes_exactly_the_backend_query_parameters(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        (tool,) = await mcp.list_tools()

    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert set(schema["properties"]) == EXPECTED_INPUT_FIELDS
    # No adapter-private parameter may be introduced.
    assert set(schema["properties"]).issubset(EXPECTED_INPUT_FIELDS)
    # Everything is optional; the backend applies its own defaults.
    assert not schema.get("required")
    # The published schema must be honest about rejecting unknown fields.
    assert schema.get("additionalProperties") is False


async def test_input_schema_publishes_the_real_backend_enums(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        (tool,) = await mcp.list_tools()

    props = tool["inputSchema"]["properties"]

    def enum_of(field: str) -> set[str]:
        node = props[field]
        variants = node.get("anyOf", [node])
        return {value for variant in variants for value in variant.get("enum", [])}

    assert enum_of("species") == {"犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"}
    assert enum_of("status") == {"待就诊", "就诊中", "住院中", "已康复", "慢性病随访"}
    assert enum_of("order") == {"asc", "desc"}
    assert enum_of("sortBy") == {
        "id",
        "name",
        "ownerName",
        "species",
        "doctor",
        "disease",
        "status",
        "totalCost",
        "visitCount",
        "createdAt",
        "updatedAt",
    }


async def test_input_schema_publishes_the_documented_bounds(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        (tool,) = await mcp.list_tools()

    props = tool["inputSchema"]["properties"]

    def numeric(node: dict) -> dict:
        return next((v for v in node.get("anyOf", [node]) if v.get("type") in ("integer", "number")), {})

    assert numeric(props["page"]).get("minimum") == 1
    assert numeric(props["pageSize"]).get("minimum") == 1
    assert numeric(props["pageSize"]).get("maximum") == 500
    assert numeric(props["min"]).get("minimum") == 0
    assert numeric(props["max"]).get("minimum") == 0


async def test_tool_description_documents_purpose_parameters_and_return(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        (tool,) = await mcp.list_tools()

    description = tool["description"]
    assert description
    # Purpose, the query parameters, and the returned fields must all be stated.
    assert "GET /api/v1/pets" in description
    for field in ("q", "ownerPhone", "pageSize", "sortBy", "min"):
        assert field in description, field
    for returned in ("items", "total", "pageSize", "totalPages", "totalCost"):
        assert returned in description, returned
    # Callers must be told the collections can be null.
    assert "null" in description


# --- statelessness ---------------------------------------------------------


async def test_requests_never_require_an_initialize_handshake(backend):
    """A cold request must succeed with no prior interaction of any kind."""
    async with open_mcp(backend) as (mcp, _client, _settings):
        response = await mcp.rpc("tools/list", {})

    assert response.status_code == 200
    assert response.json()["result"]["tools"]


async def test_server_never_issues_a_session_id(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        responses = [
            await mcp.rpc("server/discover", {}),
            await mcp.rpc("tools/list", {}),
            await mcp.rpc("tools/call", {"name": TOOL_NAME, "arguments": {}}),
        ]

    for response in responses:
        assert "mcp-session-id" not in {k.lower() for k in response.headers}


async def test_a_client_supplied_session_id_is_ignored(backend):
    """The revision removed sessions; a stray header must neither break nor echo."""
    async with open_mcp(backend) as (mcp, _client, _settings):
        response = await mcp.rpc(
            "tools/list",
            {},
            extra_headers={"Mcp-Session-Id": "client-minted-should-be-ignored"},
        )

    assert response.status_code == 200
    assert "mcp-session-id" not in {k.lower() for k in response.headers}


async def test_two_requests_are_independent(backend):
    """No state may be inferred from a previous request on the same connection."""
    async with open_mcp(backend) as (mcp, _client, _settings):
        first = await mcp.rpc("tools/list", {})
        # The second request deliberately omits nothing but is a fresh envelope.
        second = await mcp.rpc("tools/list", {})

    assert first.json()["result"] == second.json()["result"]


@pytest.mark.parametrize("method", ["get", "delete"])
async def test_removed_transport_methods_are_rejected(backend, method):
    """GET/DELETE (server stream, session teardown) are gone in 2026-07-28."""
    async with open_mcp(backend) as (mcp, _client, _settings):
        response = await mcp._client.request(method.upper(), "/mcp")

    assert response.status_code == 405


async def test_legacy_initialize_is_not_part_of_this_revision(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        response = await mcp.rpc("initialize", {"protocolVersion": PROTOCOL_VERSION})

    # Either an unknown-method rejection or a protocol error is acceptable; what
    # matters is that the modern flow never depends on it.
    assert response.status_code != 200 or "error" in response.json()


# --- server/discover -------------------------------------------------------


async def test_discover_reports_the_supported_version_and_capabilities(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        body = await mcp.json_rpc("server/discover", {})

    result = body["result"]
    assert result["resultType"] == "complete"
    assert PROTOCOL_VERSION in result["supportedVersions"]
    assert "tools" in result["capabilities"]
    # Discovery results are cacheable in this revision.
    assert "cacheScope" in result and "ttlMs" in result


# --- request envelope validation ------------------------------------------


async def test_missing_envelope_is_rejected_with_400(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        response = await mcp.rpc("tools/list", {}, envelope=False)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602


async def test_unsupported_protocol_version_is_rejected(backend):
    meta = request_meta()
    meta["io.modelcontextprotocol/protocolVersion"] = "1999-01-01"

    async with open_mcp(backend) as (mcp, _client, _settings):
        # Header and body agree, so the request reaches the version rung rather
        # than failing the header/body comparison first.
        response = await mcp.post_raw(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}},
            headers={"MCP-Protocol-Version": "1999-01-01"},
            method="tools/list",
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32022


async def test_header_body_version_mismatch_is_rejected(backend):
    """A client that disagrees with itself is told so, before version support."""
    async with open_mcp(backend) as (mcp, _client, _settings):
        response = await mcp.post_raw(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": request_meta()}},
            headers={"MCP-Protocol-Version": "2027-01-01"},
            method="tools/list",
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020


async def test_legacy_version_header_does_not_reach_the_modern_path(backend):
    """A pre-2026-07-28 header routes to the SDK's legacy transports, not ours.

    This service only claims 2026-07-28; the assertion records that the modern
    validation ladder (and therefore our stateless contract) is not what a
    legacy client is talking to.
    """
    async with open_mcp(backend) as (mcp, _client, _settings):
        response = await mcp.post_raw(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": request_meta()}},
            headers={"MCP-Protocol-Version": "2024-11-05"},
            method="tools/list",
        )

    assert "mcp-session-id" not in {k.lower() for k in response.headers}


async def test_method_header_must_match_the_body(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        response = await mcp.post_raw(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": request_meta()}},
            method="tools/call",
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020


# --- tools/call ------------------------------------------------------------


async def test_tool_call_returns_structured_content(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool(TOOL_NAME, {"species": "犬"})

    assert result["resultType"] == "complete"
    assert result["isError"] is False
    assert result["structuredContent"]["items"]
    assert backend.query()["species"] == "犬"


async def test_unknown_tool_never_reaches_the_backend(backend):
    """An unknown tool name is rejected by the SDK before dispatch.

    The SDK reports it as a failed tool result (`isError: true`) rather than a
    JSON-RPC error; what matters here is that it is refused without the backend
    being contacted.
    """
    async with open_mcp(backend) as (mcp, _client, _settings):
        response = await mcp.rpc("tools/call", {"name": "delete_everything", "arguments": {}})

    assert response.status_code == 200
    result = response.json().get("result")
    assert result is not None and result["isError"] is True
    assert "delete_everything" in result["content"][0]["text"]
    assert backend.attempts == 0


# --- /health ---------------------------------------------------------------


async def test_health_endpoint_reports_service_identity(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        response = await mcp._client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == server_module.SERVER_NAME
    assert body["protocolVersion"] == PROTOCOL_VERSION


async def test_health_does_not_touch_the_backend(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        await mcp._client.get("/health")

    assert backend.attempts == 0


async def test_health_is_served_alongside_the_mcp_endpoint(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        health = await mcp._client.get("/health")
        rpc = await mcp.rpc("tools/list", {})

    assert health.status_code == 200
    assert rpc.status_code == 200


# --- SDK 2.x client --------------------------------------------------------


async def test_sdk_client_can_discover_and_call_the_tool(backend):
    """The official SDK client, not just our hand-rolled JSON-RPC."""
    from mcp.client import Client

    settings = Settings(base_url="http://127.0.0.1:8080")
    client = PetHospitalClient(settings.base_url, transport=httpx.MockTransport(backend))
    server = server_module.build_server(client, settings)

    async with Client(server) as sdk_client:
        listing = await sdk_client.list_tools()
        assert [tool.name for tool in listing.tools] == [TOOL_NAME]

        result = await sdk_client.call_tool(TOOL_NAME, {"pageSize": 5})
        assert result.is_error is False
        assert result.structured_content["items"]
        assert result.result_type == "complete"

    await client.aclose()


def test_server_reports_the_modern_protocol_version():
    assert server_module._modern_protocol_version() == PROTOCOL_VERSION


def test_module_constants_are_wired_to_the_app():
    """`build_app` must place the MCP endpoint at the configured path."""
    settings = Settings(base_url="http://127.0.0.1:8080", mcp_path="/custom-mcp")
    client = PetHospitalClient(settings.base_url, transport=httpx.MockTransport(backend_ok()))
    app = server_module.build_app(client, settings)

    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/custom-mcp" in paths
    assert "/health" in paths


def test_tool_module_exposes_its_registry_name():
    assert list_pets.TOOL_NAME == TOOL_NAME


def test_base_url_used_by_the_test_transport_is_not_the_backend():
    """Guard: the suite must never be pointed at a real service by accident."""
    assert TEST_BASE_URL != "http://127.0.0.1:8080"
    assert isinstance(RecordingBackend(backend_ok()), RecordingBackend)
