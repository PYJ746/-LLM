"""Structural guarantees about what this service is — and is not.

These read the source and the installed distribution rather than exercising
behaviour, so a regression that reintroduces the 1.x FastMCP line or session
machinery fails loudly even if every runtime test still passes.
"""

from __future__ import annotations

import ast
import importlib.metadata
import pathlib

import pytest

import pet_hospital_mcp

SRC = pathlib.Path(pet_hospital_mcp.__file__).parent
SOURCE_FILES = sorted(SRC.rglob("*.py"))

EXPECTED_SDK_VERSION = "2.0.0"
PROTOCOL_VERSION = "2026-07-28"

#: Identifiers that belong to the removed/forbidden world. Matched against AST
#: *code* (names, attributes, imports), so prose in comments and docstrings —
#: which legitimately discusses what this service does not do — is ignored.
FORBIDDEN_IDENTIFIERS = {
    "FastMCP",
    "fastmcp",
    "max_sessions",
    "session_store",
    "SessionStore",
    "EventStore",
    "event_store",
    "event_stream",
    "resumability",
}


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_the_package_has_modules():
    assert SOURCE_FILES, "no source files were discovered"


def test_no_module_imports_fastmcp():
    offenders: list[str] = []
    for path in SOURCE_FILES:
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Import):
                offenders += [f"{path.name}: import {a.name}" for a in node.names if "fastmcp" in a.name]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "fastmcp" in module:
                    offenders.append(f"{path.name}: from {module} import ...")
    assert offenders == []


def test_fastmcp_is_not_even_importable_in_this_sdk():
    """The SDK 2.x line removed it; this documents which world we are in."""
    with pytest.raises(ModuleNotFoundError):
        __import__("mcp.server.fastmcp")


def test_the_1x_entry_points_are_absent():
    import mcp.server

    assert not hasattr(mcp.server, "FastMCP")
    assert hasattr(mcp.server, "MCPServer")


def test_no_source_file_references_forbidden_session_machinery():
    offenders: list[str] = []
    for path in SOURCE_FILES:
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_IDENTIFIERS:
                offenders.append(f"{path.name}:{node.lineno} name {node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_IDENTIFIERS:
                offenders.append(f"{path.name}:{node.lineno} attr {node.attr}")
            elif isinstance(node, ast.alias) and node.name in FORBIDDEN_IDENTIFIERS:
                offenders.append(f"{path.name}: alias {node.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FORBIDDEN_IDENTIFIERS:
                offenders.append(f"{path.name}: def {node.name}")
    assert offenders == []


def test_no_module_handles_the_legacy_initialize_handshake():
    """`initialize` may be discussed, but never implemented."""
    for path in SOURCE_FILES:
        for node in ast.walk(_parse(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert "initialize" not in node.name.lower(), f"{path.name}: {node.name}"
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                # A method name in a dispatch table would appear as a plain string.
                assert node.value != "initialize", f"{path.name}:{node.lineno}"


def test_the_service_uses_the_sdk_2x_server_class():
    from mcp.server.mcpserver import MCPServer

    from pet_hospital_mcp import server as server_module

    assert server_module.MCPServer is MCPServer


def test_installed_sdk_version_is_pinned_as_declared():
    assert importlib.metadata.version("mcp") == EXPECTED_SDK_VERSION


def test_pyproject_pins_the_sdk_and_requires_a_modern_python():
    pyproject = (SRC.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert f"mcp=={EXPECTED_SDK_VERSION}" in pyproject
    assert 'requires-python = ">=3.11"' in pyproject


def test_the_modern_protocol_version_is_the_one_we_claim():
    from mcp_types.version import LATEST_MODERN_VERSION

    from pet_hospital_mcp import server as server_module

    assert LATEST_MODERN_VERSION == PROTOCOL_VERSION
    assert server_module._modern_protocol_version() == PROTOCOL_VERSION


def test_streamable_http_app_is_built_stateless():
    """`stateless_http=True` — the pre-handshake transports are never used."""
    source = (SRC / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    keywords = {
        kw.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg in {"stateless_http", "json_response", "streamable_http_path"}
    }
    assert {"stateless_http", "json_response", "streamable_http_path"} <= keywords


def test_no_sse_or_session_code_is_present_in_our_source():
    text = "\n".join(p.read_text(encoding="utf-8") for p in SOURCE_FILES)
    for token in ("sse_app(", "run_sse_async(", "streamable_http_manager", "Last-Event-ID"):
        assert token not in text, token


def test_only_one_tool_module_exists():
    """Stage one ships exactly one tool; stage two adds modules, not branches."""
    modules = sorted(p.stem for p in (SRC / "tools").glob("*.py") if p.stem != "__init__")
    assert modules == ["list_pets"]
