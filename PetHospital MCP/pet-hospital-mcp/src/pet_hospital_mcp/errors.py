"""Unified structured error model for every MCP tool call.

Both invalid tool input and upstream failures are reported to the MCP client as
the same envelope, carried in a *successful* JSON-RPC response whose
`CallToolResult.isError` flag is set (the MCP 2026-07-28 / SDK 2.x mechanism for
marking a tool call as failed):

    {"error": {"code": "ERROR_CODE", "message": "...", "details": {}}}
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ErrorCode(str, Enum):
    """The error codes this service can report."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    BACKEND_TIMEOUT = "BACKEND_TIMEOUT"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    BACKEND_API_ERROR = "BACKEND_API_ERROR"
    BACKEND_INVALID_RESPONSE = "BACKEND_INVALID_RESPONSE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorDetail(BaseModel):
    """The `error` object of the unified envelope."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ToolErrorEnvelope(BaseModel):
    """Structured error output returned by every tool on failure."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail


class ToolCallError(Exception):
    """An expected failure that maps onto the unified error envelope.

    Anything raised that is *not* a `ToolCallError` is treated as a bug and
    reported as `INTERNAL_ERROR` without leaking the original traceback.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_envelope(self) -> ToolErrorEnvelope:
        return ToolErrorEnvelope(
            error=ErrorDetail(code=self.code, message=self.message, details=self.details)
        )


def validation_error_from(exc: ValidationError) -> ToolCallError:
    """Convert a Pydantic failure into the unified envelope.

    Only the field path, Pydantic's stable error type, and a one-line reason are
    carried over — never the exception, its repr, or a traceback. The error list
    is bounded so a hostile payload cannot inflate the response.
    """
    return ToolCallError(
        ErrorCode.VALIDATION_ERROR,
        "输入参数校验失败",
        {
            "errors": [
                {
                    "field": ".".join(str(part) for part in err.get("loc", ())) or "(body)",
                    "reason": err.get("msg", "invalid value"),
                    "type": err.get("type", "value_error"),
                }
                for err in exc.errors()[:20]
            ]
        },
    )


def error_result(err: ToolCallError) -> CallToolResult:
    """Build a failed `CallToolResult` carrying the unified error envelope.

    The envelope is emitted twice, per MCP convention: once as
    `structuredContent` for clients that understand it, and once as a JSON text
    block so it is visible to text-only clients.

    Returning the result (rather than raising) is deliberate: a raised exception
    would be rendered by the SDK as `str(exc)`, losing the structured envelope
    and risking the leak of exception internals.
    """
    envelope = err.to_envelope()
    payload = envelope.model_dump(mode="json")
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
        is_error=True,
    )


def internal_error_result(message: str = "内部错误，请稍后重试") -> CallToolResult:
    """Build a failed result for an unexpected exception, with no internals."""
    return error_result(ToolCallError(ErrorCode.INTERNAL_ERROR, message))


def success_result(payload: BaseModel) -> CallToolResult:
    """Build a successful `CallToolResult` from a validated output model."""
    data = payload.model_dump(mode="json")
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        structured_content=data,
        is_error=False,
    )
