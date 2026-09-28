"""Tool registry.

Adding a tool in a later stage means:

1. adding a module here that defines its input model and a `register(mcp, client)`
   function, reusing `rest_client`, `errors` and `logging_config`;
2. calling `register_input_model(<tool name>, <input model>)` from it.

The registry is what lets the strict-validation middleware reject unknown fields
for every registered tool without the middleware knowing anything about tools.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

_INPUT_MODELS: dict[str, type[BaseModel]] = {}


def register_input_model(tool_name: str, model: type[BaseModel]) -> None:
    """Associate a tool with the strict model used to validate its arguments."""
    _INPUT_MODELS[tool_name] = model


def get_input_model(tool_name: Any) -> type[BaseModel] | None:
    """Look up the strict input model for a tool name (None if unregistered)."""
    if not isinstance(tool_name, str):
        return None
    return _INPUT_MODELS.get(tool_name)


__all__ = ["get_input_model", "register_input_model"]
