"""How `list_pets` talks to the Go service, and how upstream failures surface.

The Go service is the only business backend. Every transport and protocol
failure it can produce must arrive at the MCP client as the unified envelope,
never as an `httpx` or `pydantic` detail.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import (
    PETS_PATH,
    RecordingBackend,
    backend_error,
    backend_ok,
    open_mcp,
    parse_error,
    pet,
    pets_data,
)

ALL_FILTERS = {
    "q": "肠胃炎",
    "name": "旺财",
    "ownerName": "张三",
    "ownerPhone": "13800001111",
    "species": "犬",
    "doctor": "李医生",
    "disease": "肠胃炎",
    "status": "已康复",
    "min": 10,
    "max": 5000,
    "sortBy": "totalCost",
    "order": "desc",
    "page": 3,
    "pageSize": 50,
}


async def test_every_filter_is_forwarded_to_the_backend(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", ALL_FILTERS)

    assert result["isError"] is False
    assert backend.path() == PETS_PATH

    query = backend.query()
    # Every one of the 14 parameters must survive the trip, and no adapter-private
    # parameter may be added.
    assert query.keys() == ALL_FILTERS.keys()
    numeric = {"min", "max", "page", "pageSize"}
    for key, expected in ALL_FILTERS.items():
        if key in numeric:
            # `min`/`max` are floating-point fields, so `10` travels as `10.0`.
            # Both forms are equivalent to the Go service's `ParseFloat`.
            assert float(query[key]) == float(expected), key
        else:
            assert query[key] == str(expected), key


async def test_unset_optional_filters_are_not_sent(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        await mcp.call_tool("list_pets", {"species": "猫"})

    # Only the supplied filter, plus the two always-present paging defaults.
    assert backend.query() == {"species": "猫", "page": "1", "pageSize": "20"}


async def test_success_payload_mirrors_the_go_data_object(backend):
    data = pets_data(
        items=[pet(id="PET-000007", name="小黑", records=None, charges=None)],
        total=1,
        page=1,
        page_size=20,
        total_pages=1,
        total_cost=320.5,
    )
    backend = RecordingBackend(backend_ok(data))

    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    structured = result["structuredContent"]
    for key in ("items", "total", "page", "pageSize", "totalPages", "totalCost"):
        assert key in structured, f"`{key}` must be part of the success output"
    assert structured["total"] == 1
    assert structured["totalPages"] == 1
    assert structured["totalCost"] == 320.5
    assert structured["items"][0]["id"] == "PET-000007"


async def test_null_records_and_charges_are_normalised_to_empty_lists(backend):
    """The Go service may serialise both collections as `null` or as `[]`."""
    data = pets_data(items=[pet(records=None, charges=None)])
    backend = RecordingBackend(backend_ok(data))

    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    item = result["structuredContent"]["items"][0]
    assert item["records"] == []
    assert item["charges"] == []


async def test_record_and_charge_arrays_are_preserved(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    item = result["structuredContent"]["items"][0]
    assert item["records"][0]["diagnosis"] == "肠胃炎"
    assert item["records"][0]["prescription"] == ["益生菌"]
    assert item["charges"][0]["item"] == "血常规"


@pytest.mark.parametrize("status", [400, 404, 422, 500, 501])
async def test_stable_error_statuses_map_to_backend_api_error(backend, status):
    backend = RecordingBackend(backend_error(status, message="记录不存在"))

    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    assert result["isError"] is True
    error = parse_error(result)
    assert error["code"] == "BACKEND_API_ERROR"
    assert "记录不存在" in error["message"]
    assert error["details"]["status"] == status
    # A definitive answer from the backend must not be retried.
    assert backend.attempts == 1


async def test_non_json_error_body_still_maps_cleanly(backend):
    backend = RecordingBackend(httpx.Response(500, text="<html>boom</html>"))

    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    error = parse_error(result)
    assert error["code"] == "BACKEND_API_ERROR"
    assert error["details"]["status"] == 500
    assert "<html>" not in error["message"]


async def test_retryable_status_is_retried_then_reported(backend):
    backend = RecordingBackend(backend_error(503, message="服务暂时不可用"))

    async with open_mcp(backend, max_retries=2) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    error = parse_error(result)
    assert error["code"] == "BACKEND_API_ERROR"
    assert backend.attempts == 3, "2 retries on top of the initial attempt"


async def test_transient_failure_is_retried_and_then_succeeds(backend):
    backend = RecordingBackend(backend_error(503), backend_ok())

    async with open_mcp(backend, max_retries=2) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    assert result["isError"] is False
    assert backend.attempts == 2


async def test_timeout_maps_to_backend_timeout(backend):
    def explode(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    backend = RecordingBackend(explode)

    async with open_mcp(backend, max_retries=1) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    error = parse_error(result)
    assert error["code"] == "BACKEND_TIMEOUT"
    assert backend.attempts == 2
    assert "timed out" not in error["message"], "no transport detail may leak"


@pytest.mark.parametrize(
    "exception",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadError("connection reset"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
async def test_connection_failures_map_to_backend_unavailable(backend, exception):
    def explode(_request: httpx.Request) -> httpx.Response:
        raise exception

    backend = RecordingBackend(explode)

    async with open_mcp(backend, max_retries=0) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    error = parse_error(result)
    assert error["code"] == "BACKEND_UNAVAILABLE"
    assert backend.attempts == 1
    assert "refused" not in error["message"] and "reset" not in error["message"]


async def test_connect_timeout_is_a_timeout_not_unavailable(backend):
    def explode(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out")

    backend = RecordingBackend(explode)

    async with open_mcp(backend, max_retries=0) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    # `ConnectTimeout` is both a transport and a timeout error; the timeout
    # classification is the more actionable of the two.
    assert parse_error(result)["code"] == "BACKEND_TIMEOUT"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json at all", id="garbage"),
        pytest.param(b"", id="empty"),
        pytest.param(b"[1,2,3]", id="json-array-not-object"),
        pytest.param(b'"just a string"', id="json-string"),
    ],
)
async def test_unparseable_or_non_object_body_is_reported(backend, body):
    backend = RecordingBackend(httpx.Response(200, content=body, headers={"content-type": "application/json"}))

    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    assert result["isError"] is True
    assert parse_error(result)["code"] == "BACKEND_INVALID_RESPONSE"


async def test_envelope_code_other_than_200_is_an_api_error(backend):
    backend = RecordingBackend(httpx.Response(200, json={"code": 500, "message": "内部错误"}))

    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    error = parse_error(result)
    assert error["code"] == "BACKEND_API_ERROR"
    assert "内部错误" in error["message"]


async def test_missing_data_object_is_an_invalid_response(backend):
    backend = RecordingBackend(httpx.Response(200, json={"code": 200, "message": "ok"}))

    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    assert parse_error(result)["code"] == "BACKEND_INVALID_RESPONSE"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.pop("items"), id="items-missing"),
        pytest.param(lambda d: d.pop("totalCost"), id="totalcost-missing"),
        pytest.param(lambda d: d.update(total="not-a-number"), id="total-wrong-type"),
        pytest.param(lambda d: d.update(items="not-a-list"), id="items-wrong-type"),
        pytest.param(lambda d: d.update(items=[{"name": "no id"}]), id="item-missing-id"),
        pytest.param(lambda d: d.update(totalPages=None), id="totalpages-null"),
    ],
)
async def test_data_violating_the_model_is_an_invalid_response(backend, mutate):
    data = pets_data()
    mutate(data)
    backend = RecordingBackend(httpx.Response(200, json={"code": 200, "message": "ok", "data": data}))

    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    assert result["isError"] is True
    error = parse_error(result)
    assert error["code"] == "BACKEND_INVALID_RESPONSE"
    # Field-level detail is allowed; a Python repr or traceback is not.
    assert "Traceback" not in str(error)
    assert "pydantic" not in str(error).lower()


async def test_empty_result_set_is_a_success(backend):
    data = pets_data(items=[], total=0, page=1, page_size=20, total_pages=0, total_cost=0.0)
    backend = RecordingBackend(backend_ok(data))

    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    assert result["isError"] is False
    assert result["structuredContent"]["items"] == []
    assert result["structuredContent"]["total"] == 0
    assert result["structuredContent"]["totalPages"] == 0


async def test_upstream_detail_never_leaks_into_the_error(backend):
    """A hostile backend must not be able to inject its own error vocabulary."""
    backend = RecordingBackend(
        httpx.Response(
            500,
            json={"code": 500, "message": "sql: connection to 10.0.0.5 refused", "trace": "Traceback (most recent call last)"},
        )
    )

    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {})

    error = parse_error(result)
    # The upstream message is passed through as *data*, never as our own
    # message, and the exception/stack internals are dropped entirely.
    assert error["code"] == "BACKEND_API_ERROR"
    assert "trace" not in error["details"]
