"""JSON logging, and recursive redaction of personal data.

Sensitive values must be masked wherever they appear — including inside the
`records`/`charges` arrays of a returned pet — under every spelling of the key.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from conftest import RecordingBackend, backend_ok, open_mcp, pet, pets_data
from pet_hospital_mcp.logging_config import (
    REDACTED,
    JsonFormatter,
    is_sensitive_key,
    log_tool_call,
    normalize_key,
    redact,
)

SENSITIVE_SPELLINGS = [
    "ownerPhone",
    "owner_phone",
    "OWNER_PHONE",
    "owner-phone",
    "ownerphone",
    "ownerAddr",
    "owner_addr",
    "OWNER_ADDR",
    "chipNo",
    "chip_no",
    "CHIPNO",
    "chip-no",
]


@pytest.mark.parametrize("key", SENSITIVE_SPELLINGS)
def test_sensitive_keys_are_recognised_in_every_spelling(key):
    assert is_sensitive_key(key)


@pytest.mark.parametrize("key", ["name", "ownerName", "doctor", "species", "ownerPhones", "min", "q"])
def test_non_sensitive_keys_are_left_alone(key):
    assert not is_sensitive_key(key)


def test_normalize_key_folds_case_and_punctuation():
    assert normalize_key("Owner_Phone") == "ownerphone"
    assert normalize_key("chip-no") == "chipno"


def test_redaction_is_recursive_through_nested_structures():
    payload = {
        "ownerPhone": "13800001111",
        "ownerName": "张三",
        "items": [
            {
                "chipNo": "CHIP-1",
                "ownerAddr": "北京市朝阳区 1 号",
                "records": [{"owner_phone": "13900002222", "diagnosis": "肠胃炎"}],
            }
        ],
    }

    cleaned = redact(payload)
    dumped = json.dumps(cleaned, ensure_ascii=False)

    assert cleaned["ownerPhone"] == REDACTED
    assert cleaned["ownerName"] == "张三", "only sensitive keys are masked"
    assert cleaned["items"][0]["chipNo"] == REDACTED
    assert cleaned["items"][0]["ownerAddr"] == REDACTED
    assert cleaned["items"][0]["records"][0]["owner_phone"] == REDACTED
    assert cleaned["items"][0]["records"][0]["diagnosis"] == "肠胃炎"
    for secret in ("13800001111", "13900002222", "CHIP-1", "北京市朝阳区 1 号"):
        assert secret not in dumped


def test_redaction_survives_pathological_nesting():
    """A deeply nested or cyclic structure must not hang a log statement."""
    deep: dict = {"ownerPhone": "13800001111"}
    node = deep
    for _ in range(200):
        node["child"] = {}
        node = node["child"]
    redact(deep)  # must terminate

    cyclic: dict = {"ownerPhone": "13800001111"}
    cyclic["self"] = cyclic
    redact(cyclic)  # must terminate


def test_formatter_emits_a_single_json_line():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="pet_hospital_mcp.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="tool_call",
        args=(),
        exc_info=None,
    )
    record.event = {
        "tool_name": "list_pets",
        "params": {"ownerPhone": "13800001111", "species": "犬"},
        "status": "ok",
        "duration_ms": 12.5,
    }

    line = formatter.format(record)
    assert "\n" not in line

    payload = json.loads(line)
    assert payload["timestamp"]
    assert payload["level"] == "INFO"
    assert payload["tool_name"] == "list_pets"
    assert payload["params"] == {"ownerPhone": REDACTED, "species": "犬"}
    assert payload["status"] == "ok"
    assert payload["duration_ms"] == 12.5
    assert "13800001111" not in line


def test_exception_logging_never_includes_a_traceback():
    formatter = JsonFormatter()
    try:
        raise RuntimeError("secret internal detail")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            name="pet_hospital_mcp.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="unexpected failure",
            args=(),
            exc_info=sys.exc_info(),
        )

    line = formatter.format(record)
    assert "Traceback" not in line
    assert "secret internal detail" not in line
    assert json.loads(line)["exception_type"] == "RuntimeError"


def test_log_tool_call_always_carries_the_required_fields():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("pet_hospital_mcp.logging_test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        log_tool_call(
            logger,
            tool_name="list_pets",
            params={"ownerPhone": "13800001111"},
            status="ok",
            duration_ms=3.25,
        )
    finally:
        logger.removeHandler(handler)

    payload = json.loads(stream.getvalue().strip())
    for field in ("timestamp", "tool_name", "params", "status", "duration_ms"):
        assert field in payload, field
    assert payload["tool_name"] == "list_pets"
    assert payload["status"] == "ok"
    assert payload["duration_ms"] == 3.25
    assert payload["params"]["ownerPhone"] == REDACTED


@pytest.mark.parametrize("logger_name", ["httpx", "httpcore"])
def test_http_client_loggers_are_quieted(logger_name):
    """Regression guard: httpx logs the full request URL, query string included.

    That URL carries `ownerPhone`, which our own redaction never sees because it
    is not one of our records. Keep the logger above INFO so it stays out.
    """
    from pet_hospital_mcp.logging_config import configure_logging

    configure_logging("INFO")
    assert logging.getLogger(logger_name).level >= logging.WARNING


async def test_a_real_tool_call_logs_redacted_params():
    """End-to-end: no personal data reaches the log, from any logger.

    Captures on the *root* logger, so records emitted by `httpx` and the SDK are
    inspected too — not just our own.
    """
    data = pets_data(items=[pet(ownerPhone="13800001111", chipNo="CHIP-X", ownerAddr="北京市 1 号")])
    backend = RecordingBackend(backend_ok(data))

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        async with open_mcp(backend) as (mcp, _client, _settings):
            await mcp.call_tool("list_pets", {"ownerPhone": "13800001111", "species": "犬"})
    finally:
        root.removeHandler(handler)

    logged = stream.getvalue()
    for secret in ("13800001111", "CHIP-X", "北京市 1 号"):
        assert secret not in logged, f"{secret} leaked into the log"

    records = [
        json.loads(line)
        for line in logged.splitlines()
        if line.startswith("{") and json.loads(line).get("message") == "tool_call"
    ]
    assert records, "the tool call must be logged"
    assert records[-1]["status"] == "ok"
    assert records[-1]["tool_name"] == "list_pets"
    assert records[-1]["params"]["ownerPhone"] == REDACTED
