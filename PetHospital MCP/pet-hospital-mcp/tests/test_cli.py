"""The console entry point.

`main()` must never silently start a server when it was handed an argument it
did not understand — that turns a typo into an unexpected listener.
"""

from __future__ import annotations

import pytest

from pet_hospital_mcp import __version__
from pet_hospital_mcp.__main__ import main


def test_version_flag_exits_without_serving(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_help_flag_exits_without_serving(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    # The environment variables are the whole configuration surface; say so.
    for name in ("PET_HOSPITAL_BASE_URL", "MCP_HOST", "MCP_PORT", "MCP_PATH"):
        assert name in out, name


@pytest.mark.parametrize("argv", [["--nope"], ["serve"], ["--port", "9000"]])
def test_unknown_arguments_are_rejected(argv):
    with pytest.raises(SystemExit) as exit_info:
        main(argv)

    assert exit_info.value.code != 0


def test_invalid_configuration_fails_before_binding(monkeypatch, capsys):
    monkeypatch.setenv("MCP_PORT", "not-a-number")

    assert main([]) == 2
    assert "MCP_PORT" in capsys.readouterr().err
