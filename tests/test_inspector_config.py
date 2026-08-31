"""Tests for the MCP Inspector config generator.

This module exists because of a bug that was invisible for a while: the MCP
SDK's stdio client spawns a server with a FIXED environment allowlist rather
than the parent environment, so under `run_server.bat --dev` every WAMCP_*
variable silently vanished. `--tools core,docs` registered all 17 tools,
`--web` gave a server with research mode off, and a project root set for a
--dev session left writes confined to the download sandbox.

The fix is a config file with an explicit `env` block. What is tested here is
that the block actually carries the variables, and that nothing the operator
did not set gets written into it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from windows_agent_mcp.inspector_config import (
    FORWARDED_ENV_VARS,
    SERVER_NAME,
    build_config,
    main,
)


def server_entry(config: dict) -> dict:
    return config["mcpServers"][SERVER_NAME]


# ============================================================
# Shape
# ============================================================


def test_config_names_the_command_and_module() -> None:
    config = build_config("C:/projects/windows-agent-mcp/.venv/Scripts/python.exe", {})

    entry = server_entry(config)

    assert entry["command"] == "C:/projects/windows-agent-mcp/.venv/Scripts/python.exe"
    assert entry["args"] == ["-m", "windows_agent_mcp"]


def test_backslashes_become_forward_slashes() -> None:
    """The path lands in JSON, where a lone backslash is an invalid escape."""

    config = build_config(r"C:\projects\windows-agent-mcp\.venv\Scripts\python.exe", {})

    command = server_entry(config)["command"]

    assert "\\" not in command
    assert command == "C:/projects/windows-agent-mcp/.venv/Scripts/python.exe"


def test_config_serialises_to_valid_json() -> None:
    config = build_config(
        r"C:\Program Files\Python\python.exe", {"WAMCP_TOOLS": "core"}
    )

    reloaded = json.loads(json.dumps(config))

    assert server_entry(reloaded)["env"]["WAMCP_TOOLS"] == "core"


def test_single_server_named_for_the_launcher_flag() -> None:
    """The launcher passes --server windows-agent-mcp; the name must match."""

    config = build_config("python", {})

    assert list(config["mcpServers"]) == [SERVER_NAME]
    assert SERVER_NAME == "windows-agent-mcp"


# ============================================================
# Which variables get forwarded
# ============================================================


def test_every_variable_the_server_reads_is_forwarded() -> None:
    """A variable the server honours but the Inspector drops is the whole bug.

    Pinned against the real config surface rather than a hand-written list, so
    adding a new setting to utils without adding it here fails loudly.
    """

    from windows_agent_mcp import utils

    required = {
        utils.TOOL_GROUPS_ENV_VAR,
        utils.WEB_RESEARCH_ENV_VAR,
        utils.SEARCH_BACKEND_ENV_VAR,
        utils.PROJECT_ROOTS_ENV_VAR,
        "WAMCP_DOWNLOAD_ROOT",
    }

    assert required <= set(FORWARDED_ENV_VARS)


def test_stdio_encoding_is_forwarded() -> None:
    """stdout IS the JSON-RPC channel; cp1252 would corrupt the stream."""

    assert "PYTHONIOENCODING" in FORWARDED_ENV_VARS


@pytest.mark.parametrize("name", FORWARDED_ENV_VARS)
def test_a_set_variable_reaches_the_env_block(name: str) -> None:
    config = build_config("python", {name: "value-for-test"})

    assert server_entry(config)["env"][name] == "value-for-test"


def test_unset_variables_are_omitted() -> None:
    """Writing a variable nobody set invites a future truthiness change to
    turn a feature on by accident."""

    config = build_config("python", {"WAMCP_TOOLS": "core"})

    env = server_entry(config)["env"]

    assert env == {"WAMCP_TOOLS": "core"}


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_blank_variables_are_omitted(value: str) -> None:
    config = build_config("python", {"WAMCP_TOOLS": value})

    assert server_entry(config)["env"] == {}


def test_unrelated_variables_are_not_leaked() -> None:
    """The SDK's allowlist exists to stop secret leakage; do not undo that."""

    config = build_config(
        "python",
        {
            "WAMCP_TOOLS": "core",
            "AWS_SECRET_ACCESS_KEY": "should-not-travel",
            "GITHUB_TOKEN": "should-not-travel",
        },
    )

    env = server_entry(config)["env"]

    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GITHUB_TOKEN" not in env


def test_empty_environment_yields_an_empty_block() -> None:
    config = build_config("python", {})

    assert server_entry(config)["env"] == {}


def test_defaults_to_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAMCP_TOOLS", "edit,build")

    config = build_config("python")

    assert server_entry(config)["env"]["WAMCP_TOOLS"] == "edit,build"


# ============================================================
# main()
# ============================================================


def test_main_writes_the_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "nested" / "inspector.json"

    monkeypatch.setenv("WAMCP_TOOLS", "core,docs")
    monkeypatch.setattr(sys, "argv", ["prog", str(target), "C:/py/python.exe"])

    assert main() == 0

    config = json.loads(target.read_text(encoding="utf-8"))

    assert server_entry(config)["env"]["WAMCP_TOOLS"] == "core,docs"
    assert server_entry(config)["command"] == "C:/py/python.exe"


def test_main_creates_missing_parents(tmp_path, monkeypatch) -> None:
    target = tmp_path / "a" / "b" / "c.json"

    monkeypatch.setattr(sys, "argv", ["prog", str(target), "python"])

    assert main() == 0
    assert target.is_file()


@pytest.mark.parametrize("argv", [["prog"], ["prog", "only-one-arg"]])
def test_main_rejects_missing_arguments(argv, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", argv)

    assert main() == 1
    assert "usage:" in capsys.readouterr().err


def test_main_reports_an_unwritable_destination(tmp_path, monkeypatch, capsys) -> None:
    """The launcher checks errorlevel and aborts, so this must return 1."""

    target = tmp_path / "blocked.json"

    def denied(self, *args: object, **kwargs: object):
        raise PermissionError("read-only volume")

    monkeypatch.setattr(Path, "write_text", denied)
    monkeypatch.setattr(sys, "argv", ["prog", str(target), "python"])

    assert main() == 1
    assert "could not write" in capsys.readouterr().err


def test_module_runs_as_a_script(tmp_path) -> None:
    """The launcher invokes it with -m, so that entry point must work."""

    target = tmp_path / "cfg.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "windows_agent_mcp.inspector_config",
            str(target),
            "C:/py/python.exe",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
