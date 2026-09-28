"""Strict argument validation, applied before the SDK's own arg model.

The SDK builds its tool-argument model from the Python signature and does not
forbid extra properties, so an unknown field would be silently dropped before
the tool body ever sees it. Unknown fields must be *rejected*, so this
middleware runs ahead of params validation (the SDK documents that
`ServerMiddleware` sees raw, unvalidated `ctx.params`) and validates the raw
arguments against the tool's registered strict model.

It also means a validation failure is reported as our unified envelope rather
than the SDK's default `str(PydanticError)` rendering, which would leak
Pydantic internals.
"""

from __future__ import annotations

from typing import Any

from mcp.server.context import ServerRequestContext
from pydantic import ValidationError

from .errors import ToolCallError, error_result, validation_error_from
from .logging_config import get_logger
from .tools import get_input_model

logger = get_logger(__name__)


class StrictToolInputMiddleware:
    """Validate `tools/call` arguments against each tool's strict input model."""

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: Any) -> Any:
        if ctx.method != "tools/call":
            return await call_next(ctx)

        params = ctx.params
        if not isinstance(params, dict):
            return await call_next(ctx)

        model = get_input_model(params.get("name"))
        if model is None:
            # Unregistered tool: let the SDK own the outcome (unknown-tool error,
            # or a tool that legitimately takes free-form arguments).
            return await call_next(ctx)

        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return error_result(_validation_error_from_type())

        try:
            model.model_validate(arguments)
        except ValidationError as exc:
            log_validation_failure(params.get("name"), exc)
            return error_result(validation_error_from(exc))

        return await call_next(ctx)


def _validation_error_from_type() -> ToolCallError:
    from .errors import ErrorCode

    return ToolCallError(
        ErrorCode.VALIDATION_ERROR,
        "输入参数校验失败",
        {"errors": [{"field": "arguments", "reason": "必须是一个 JSON 对象", "type": "type_error"}]},
    )


def log_validation_failure(tool_name: Any, exc: ValidationError) -> None:
    """Log the failure shape only — never the offending values."""
    logger.info(
        "tool_validation_failed",
        extra={
            "event": {
                "tool_name": tool_name if isinstance(tool_name, str) else str(tool_name),
                "status": "VALIDATION_ERROR",
                "invalid_fields": [
                    ".".join(str(part) for part in err.get("loc", ())) or "(body)"
                    for err in exc.errors()[:20]
                ],
            }
        },
    )


__all__ = ["StrictToolInputMiddleware"]
