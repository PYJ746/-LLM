"""Input validation for `list_pets`.

Every rejected input must come back as the unified `VALIDATION_ERROR` envelope,
marked as a failed tool call, and must never reach the Go backend.
"""

from __future__ import annotations

import pytest

from conftest import backend_ok, open_mcp, parse_error
from pet_hospital_mcp.tools import list_pets

VALID_INPUTS = [
    {},
    {"page": 1, "pageSize": 20},
    {"species": "犬"},
    {"status": "已康复"},
    {"sortBy": "totalCost", "order": "desc"},
    # A JSON number without a fractional part is still a valid cost bound.
    {"min": 100},
    {"min": 0, "max": 100.5},
    {"min": 50, "max": 50},
    {"page": 500, "pageSize": 500},
    {"q": "肠胃炎", "name": "旺财", "ownerName": "张三", "ownerPhone": "138", "doctor": "李医生", "disease": "炎"},
]

INVALID_INPUTS = [
    pytest.param({"page": 0}, "greater_than_equal", id="page-zero"),
    pytest.param({"page": -3}, "greater_than_equal", id="page-negative"),
    pytest.param({"page": 1.5}, "int_type", id="page-float"),
    pytest.param({"page": "2"}, "int_type", id="page-string"),
    pytest.param({"page": True}, "int_type", id="page-bool"),
    pytest.param({"pageSize": 0}, "greater_than_equal", id="pagesize-zero"),
    pytest.param({"pageSize": 501}, "less_than_equal", id="pagesize-too-big"),
    pytest.param({"pageSize": "20"}, "int_type", id="pagesize-string"),
    pytest.param({"min": -1}, "greater_than_equal", id="min-negative"),
    pytest.param({"max": -1}, "greater_than_equal", id="max-negative"),
    pytest.param({"min": 100, "max": 50}, "value_error", id="min-greater-than-max"),
    pytest.param({"species": "恐龙"}, "literal_error", id="species-unknown"),
    pytest.param({"status": "已出院"}, "literal_error", id="status-unknown"),
    pytest.param({"sortBy": "bogus"}, "literal_error", id="sortby-unknown"),
    pytest.param({"order": "bogus"}, "literal_error", id="order-unknown"),
    pytest.param({"order": "ASC"}, "literal_error", id="order-wrong-case"),
    pytest.param({"unsupported": 1}, "extra_forbidden", id="unknown-field"),
    pytest.param({"pageSize_": 5}, "extra_forbidden", id="misspelled-field"),
    pytest.param({"name": 123}, "string_type", id="name-wrong-type"),
    pytest.param({"species": ["犬"]}, "literal_error", id="species-array"),
]


async def test_valid_inputs_are_accepted(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        for payload in VALID_INPUTS:
            result = await mcp.call_tool("list_pets", payload)
            assert result.get("isError") is not True, f"{payload} should be accepted: {result}"
    assert backend.attempts == len(VALID_INPUTS)


@pytest.mark.parametrize(("payload", "expected_type"), INVALID_INPUTS)
async def test_invalid_inputs_are_rejected_with_unified_envelope(backend, payload, expected_type):
    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", payload)

    # Reported as a failed tool call, not a JSON-RPC protocol error, so the
    # model can see the failure and self-correct.
    assert result["isError"] is True
    assert result["resultType"] == "complete"

    error = parse_error(result)
    assert error["code"] == "VALIDATION_ERROR"
    assert isinstance(error["message"], str) and error["message"]
    assert error["details"]["errors"], "the envelope must name the offending field(s)"
    assert any(expected_type in entry["type"] for entry in error["details"]["errors"])

    # A rejected input must never be forwarded to the Go service.
    assert backend.attempts == 0


async def test_unknown_field_is_named_in_the_error(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        result = await mcp.call_tool("list_pets", {"pageSize": 5, "nope": 1})

    fields = {entry["field"] for entry in parse_error(result)["details"]["errors"]}
    assert "nope" in fields


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_nan_and_infinity_are_rejected(backend, literal):
    # `NaN`/`Infinity` are not legal JSON, but Python's decoder accepts them, so
    # a hostile or sloppy client can still put them on the wire.
    raw = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{'
        '"name":"list_pets",'
        f'"arguments":{{"min":{literal}}},'
        '"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28",'
        '"io.modelcontextprotocol/clientCapabilities":{}}}}'
    ).encode()

    async with open_mcp(backend) as (mcp, _client, _settings):
        response = await mcp.post_content(raw, method="tools/call", extra_headers={"Mcp-Name": "list_pets"})

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is True
    assert parse_error(result)["code"] == "VALIDATION_ERROR"
    assert backend.attempts == 0


async def test_validation_happens_before_the_backend_is_called(backend):
    async with open_mcp(backend) as (mcp, _client, _settings):
        await mcp.call_tool("list_pets", {"page": 0})
        assert backend.attempts == 0
        await mcp.call_tool("list_pets", {"page": 1})
        assert backend.attempts == 1


def test_input_model_forbids_unknown_and_non_finite():
    """The model itself — independent of the wire — is strict."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        list_pets.ListPetsInput.model_validate({"nope": 1})
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            list_pets.ListPetsInput.model_validate({"min": bad})
    with pytest.raises(ValidationError):
        list_pets.ListPetsInput.model_validate({"min": 10, "max": 5})


def test_query_params_drop_unset_optional_fields():
    params = list_pets.ListPetsInput.model_validate({"species": "犬"}).to_query_params()

    assert params == {"species": "犬", "page": 1, "pageSize": 20}


def test_backend_ok_helper_is_well_formed():
    """Guards the fixture itself: the suite would be meaningless otherwise."""
    assert backend_ok().status_code == 200
