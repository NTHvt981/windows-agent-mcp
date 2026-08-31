"""Integration tests: tool registration and the shared return contract.

The return contract this server commits to is:

    success  ->  plain text (or JSON *data* for the info tools)
    failure  ->  the mcp_error envelope: {"ok": false, "error": {...}}

Small models key off response shape, so the contract is enforced here rather
than left to each tool's own tests.
"""

from __future__ import annotations

import json

import pytest

from windows_agent_mcp.main import ALL_TOOLS, TOOLS, WEB_TOOLS, get_tools
from windows_agent_mcp.tools import get_system_info as system_info_module
from windows_agent_mcp.tools.build_project import build_project
from windows_agent_mcp.tools.compile_shader import compile_shader
from windows_agent_mcp.tools.download_file import download_file
from windows_agent_mcp.tools.edit_file import edit_file
from windows_agent_mcp.tools.fetch_https_response import fetch_https_response
from windows_agent_mcp.tools.fetch_web_page import read_web_page
from windows_agent_mcp.tools.find_files import find_files
from windows_agent_mcp.tools.get_github_latest_release import (
    get_github_latest_release,
)
from windows_agent_mcp.tools.get_server_info import get_server_info
from windows_agent_mcp.tools.get_system_info import get_system_info
from windows_agent_mcp.tools.list_directory import list_directory
from windows_agent_mcp.tools.list_empty_dirs import list_empty_dirs
from windows_agent_mcp.tools.read_file import read_file
from windows_agent_mcp.tools.run_powershell import run_powershell
from windows_agent_mcp.tools.search_files import search_files
from windows_agent_mcp.tools.web_search import web_search
from windows_agent_mcp.tools.write_file import write_file
from windows_agent_mcp.utils import get_download_root

# ============================================================
# Registration
# ============================================================

EXPECTED_TOOLS = {
    "build_project",
    "compile_shader",
    "download_file",
    "edit_file",
    "fetch_https_response",
    # Registered by default: scoped to the documentation hosts unless research
    # mode widens it. Moving it out of WEB_TOOLS was deliberate.
    "fetch_web_page",
    "find_files",
    "get_github_latest_release",
    "get_gpu_info",
    "get_server_info",
    "get_system_info",
    "list_directory",
    "list_empty_dirs",
    "read_file",
    "run_powershell",
    "search_files",
    "write_file",
}

# Registered only in research mode, but still subject to every contract check
# below -- otherwise conditional registration would quietly exempt them.
EXPECTED_WEB_TOOLS = {"web_search"}


def test_every_expected_tool_is_registered() -> None:
    """Guards the class of bug where a tool is documented but never wired up.

    list_empty_dirs shipped unregistered while the README advertised it.
    """

    assert {tool.__name__ for tool in TOOLS} == EXPECTED_TOOLS


def test_every_expected_web_tool_exists() -> None:
    assert {tool.__name__ for tool in WEB_TOOLS} == EXPECTED_WEB_TOOLS


def test_all_tools_is_the_union() -> None:
    assert set(ALL_TOOLS) == set(TOOLS) | set(WEB_TOOLS)
    assert len(ALL_TOOLS) == len(TOOLS) + len(WEB_TOOLS)


def test_web_tools_are_not_registered_by_default() -> None:
    """Least privilege: the capability should not exist unless asked for."""

    assert get_tools(web_enabled=False) == TOOLS

    assert EXPECTED_WEB_TOOLS.isdisjoint(
        {tool.__name__ for tool in get_tools(web_enabled=False)}
    )


def test_web_tools_are_registered_in_research_mode() -> None:
    names = {tool.__name__ for tool in get_tools(web_enabled=True)}

    assert names == EXPECTED_TOOLS | EXPECTED_WEB_TOOLS


def test_get_tools_does_not_read_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Purity keeps the suite independent of the developer's shell.

    If this read os.environ, anyone with WAMCP_WEB_RESEARCH exported could
    not commit, because pre-commit runs these tests.
    """

    monkeypatch.setenv("WAMCP_WEB_RESEARCH", "1")

    assert get_tools(web_enabled=False) == TOOLS


def test_tools_are_callable_and_unique() -> None:
    assert all(callable(tool) for tool in ALL_TOOLS)
    assert len({tool.__name__ for tool in ALL_TOOLS}) == len(ALL_TOOLS)


def test_tools_register_with_the_mcp_server() -> None:
    """Every tool's signature must be acceptable to the MCP schema builder.

    Iterates ALL_TOOLS, not TOOLS: this is the only check that the new web
    tools' signatures are MCP-serializable, and conditional registration
    would otherwise skip them entirely.
    """

    from mcp.server import MCPServer

    server = MCPServer("contract-test")

    for tool in ALL_TOOLS:
        server.add_tool(tool)


def test_every_tool_has_a_docstring() -> None:
    """The docstring is what the model sees as the tool description."""

    missing = [tool.__name__ for tool in ALL_TOOLS if not (tool.__doc__ or "").strip()]

    assert missing == []


# ============================================================
# Error contract
# ============================================================

# (label, callable, expected error type). Each case fails on input validation
# alone, so none of these touch the network or the filesystem.
ERROR_CASES = [
    ("read_file empty path", lambda: read_file(""), "INVALID_PATH"),
    (
        "list_directory missing",
        lambda: list_directory("/no/such/directory/anywhere"),
        "PATH_NOT_FOUND",
    ),
    ("list_empty_dirs empty path", lambda: list_empty_dirs(""), "INVALID_PATH"),
    (
        "run_powershell blocked",
        lambda: run_powershell("Stop-Computer -Force"),
        "COMMAND_NOT_ALLOWED",
    ),
    (
        "github bad repo",
        lambda: get_github_latest_release("not-a-repo"),
        "INVALID_REPOSITORY",
    ),
    (
        "fetch non-https",
        lambda: fetch_https_response("http://github.com/x"),
        "URL_NOT_ALLOWED",
    ),
    (
        "download non-https",
        lambda: download_file("http://github.com/x"),
        "DOWNLOAD_NOT_ALLOWED",
    ),
    # Research mode is off (the autouse env fixture guarantees it), so these
    # reach an error without touching the network.
    (
        "web_search disabled",
        lambda: web_search("anything"),
        "WEB_RESEARCH_DISABLED",
    ),
    # fetch_web_page IS registered by default, so its hermetic error case is
    # an off-list host rather than a disabled tool. example.com is neither a
    # documentation host nor on the network allowlist, and the allowlist check
    # runs before DNS, so this touches nothing.
    (
        "fetch_web_page off-list host",
        lambda: read_web_page("https://example.com/"),
        "URL_NOT_ALLOWED",
    ),
    # The tools added for C++/graphics work. Each fails on argument validation
    # before touching the filesystem, PATH or a subprocess.
    ("write_file empty path", lambda: write_file("", "x"), "WRITE_PATH_NOT_ALLOWED"),
    (
        "edit_file empty path",
        lambda: edit_file("", "a", "b"),
        "WRITE_PATH_NOT_ALLOWED",
    ),
    ("search_files empty pattern", lambda: search_files(""), "INVALID_PATTERN"),
    ("find_files empty glob", lambda: find_files(""), "INVALID_PATTERN"),
    ("build_project empty command", lambda: build_project(""), "INVALID_COMMAND"),
    ("compile_shader empty source", lambda: compile_shader(""), "INVALID_PATH"),
]


@pytest.mark.parametrize(
    ("call", "expected_type"),
    [(case[1], case[2]) for case in ERROR_CASES],
    ids=[case[0] for case in ERROR_CASES],
)
def test_errors_use_the_shared_envelope(call, expected_type: str) -> None:
    result = call()

    assert isinstance(result, str), "tools must return strings"

    payload = json.loads(result)

    assert payload["ok"] is False
    assert payload["error"]["type"] == expected_type
    assert payload["error"]["tool"]
    assert payload["error"]["message"]

    # Recovery guidance is the whole point of the envelope for a small model:
    # without it the model retries the same failing call.
    assert payload["error"]["recovery"], "every error must carry recovery steps"


@pytest.mark.parametrize(
    ("call", "expected_type"),
    [(case[1], case[2]) for case in ERROR_CASES],
    ids=[case[0] for case in ERROR_CASES],
)
def test_no_tool_raises_on_bad_input(call, expected_type: str) -> None:
    """Rejections must be tool results, not transport-level exceptions."""

    call()


# ============================================================
# Success contract
# ============================================================


def test_read_file_success_is_not_a_json_envelope(tmp_path) -> None:
    target = tmp_path / "plain.txt"
    target.write_text("just text", encoding="utf-8")

    assert read_file(str(target)) == "just text"


def test_list_directory_success_is_not_a_json_envelope(tmp_path) -> None:
    result = list_directory(str(tmp_path))

    with pytest.raises(json.JSONDecodeError):
        json.loads(result)


# ============================================================
# Info tools
# ============================================================


def test_server_info(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WAMCP_DOWNLOAD_ROOT", str(tmp_path / "dl"))

    data = json.loads(get_server_info())

    assert data["name"] == "Windows Agent MCP"
    assert data["transport"] == "stdio"
    assert data["version"]
    assert data["download_root"] == str((tmp_path / "dl").resolve())


def test_system_info() -> None:
    data = json.loads(get_system_info())

    for field in ("os", "os_build", "machine", "python_version"):
        assert field in data


def test_system_info_has_no_bare_version_field() -> None:
    """`version` and `release` are gone, and must not come back.

    A field called `version` sitting next to a question about the OS version is
    what produced the failure this join exists to prevent: a model quoted
    `10.0.26200` and answered "Windows 10". The name is the trap, so the guard
    is on the name.
    """

    data = json.loads(get_system_info())

    assert "version" not in data
    assert "release" not in data


@pytest.mark.parametrize(
    ("release", "version", "expected"),
    [
        # platform.release() lies on Windows 11 before Python 3.12: it says
        # "10". Measured on 3.10.20 and 3.11.15. The build number is the only
        # way to tell, so it has to win.
        ("10", "10.0.26100", "Windows 11"),
        ("10", "10.0.22000", "Windows 11"),
        # Genuine Windows 10 is below the threshold and must stay put.
        ("10", "10.0.19045", "Windows 10"),
        # Already correct on 3.12+; the correction must not fire.
        ("11", "10.0.26100", "Windows 11"),
    ],
)
def test_windows_release_corrected_from_build(
    monkeypatch, release: str, version: str, expected: str
) -> None:
    monkeypatch.setattr(system_info_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(system_info_module.platform, "release", lambda: release)
    monkeypatch.setattr(system_info_module.platform, "version", lambda: version)

    assert json.loads(get_system_info())["os"] == expected


def test_os_display_name_survives_missing_parts(monkeypatch) -> None:
    """Never emit a bare "Windows " with the release missing."""

    monkeypatch.setattr(system_info_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(system_info_module.platform, "release", lambda: "")
    monkeypatch.setattr(system_info_module.platform, "version", lambda: "")

    assert json.loads(get_system_info())["os"] == "Windows"

    monkeypatch.setattr(system_info_module.platform, "system", lambda: "")

    assert json.loads(get_system_info())["os"] == "unknown"


def test_unparseable_build_leaves_release_alone(monkeypatch) -> None:
    monkeypatch.setattr(system_info_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(system_info_module.platform, "release", lambda: "10")
    monkeypatch.setattr(system_info_module.platform, "version", lambda: "not.a.build")

    assert json.loads(get_system_info())["os"] == "Windows 10"


def test_download_directory_created_from_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WAMCP_DOWNLOAD_ROOT", str(tmp_path / "dl"))

    root = get_download_root()

    assert root.is_dir()
    assert root == (tmp_path / "dl").resolve()
