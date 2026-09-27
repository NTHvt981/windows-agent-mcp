"""Register tools and run the MCP server over stdio."""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable
from typing import Any

from .log import log
from .server import mcp
from .tools.build_project import build_project
from .tools.compile_shader import compile_shader
from .tools.download_file import download_file
from .tools.edit_file import edit_file
from .tools.fetch_https_response import fetch_https_response
from .tools.fetch_web_page import fetch_web_page
from .tools.find_files import find_files
from .tools.get_github_latest_release import get_github_latest_release
from .tools.get_gpu_info import get_gpu_info
from .tools.get_server_info import get_server_info
from .tools.get_system_info import get_system_info
from .tools.list_directory import list_directory
from .tools.list_empty_dirs import list_empty_dirs
from .tools.read_file import read_file
from .tools.run_powershell import run_powershell
from .tools.search_files import search_files
from .tools.web_search import web_search
from .tools.write_file import write_file
from .utils import (
    ALWAYS_ON_TOOL_GROUPS,
    DEFAULT_TOOL_GROUPS,
    PROFILE_ENV_VAR,
    TOOL_GROUPS_ENV_VAR,
    active_tool_groups,
)

__all__: list[str] = [
    "ALL_TOOLS",
    "apply_configuration",
    "TOOL_GROUPS",
    "TOOLS",
    "WEB_TOOLS",
    "get_tools",
    "main",
    "tool_description",
]

# Order steers which tool a small model reaches for.
TOOLS: tuple[Callable[..., Any], ...] = (
    read_file,
    list_directory,
    list_empty_dirs,
    find_files,
    search_files,
    write_file,
    edit_file,
    build_project,
    compile_shader,
    run_powershell,
    get_system_info,
    get_gpu_info,
    get_server_info,
    download_file,
    fetch_https_response,
    get_github_latest_release,
    fetch_web_page,
)

WEB_TOOLS: tuple[Callable[..., Any], ...] = (web_search,)

ALL_TOOLS: tuple[Callable[..., Any], ...] = TOOLS + WEB_TOOLS

TOOL_GROUPS: dict[str, tuple[Callable[..., Any], ...]] = {
    "core": (
        read_file,
        list_directory,
        list_empty_dirs,
        find_files,
        search_files,
        get_system_info,
        get_server_info,
    ),
    "edit": (
        write_file,
        edit_file,
    ),
    "build": (
        run_powershell,
        build_project,
        compile_shader,
        get_gpu_info,
    ),
    "docs": (fetch_web_page,),
    "net": (
        download_file,
        fetch_https_response,
        get_github_latest_release,
    ),
    "search": (web_search,),
    "research": (web_search,),
}


def get_tools(
    *,
    groups: frozenset[str] | None = None,
    web_enabled: bool | None = None,
) -> tuple[Callable[..., Any], ...]:
    """Return the tools to register for a given set of groups."""

    resolved = DEFAULT_TOOL_GROUPS if groups is None else frozenset(groups)

    resolved = resolved | ALWAYS_ON_TOOL_GROUPS

    if web_enabled is not None:
        resolved = resolved | {"research"} if web_enabled else resolved - {"research"}

    wanted = {tool for name in resolved for tool in TOOL_GROUPS.get(name, ())}

    # One place defines presentation order; it steers model choice.
    return tuple(tool for tool in ALL_TOOLS if tool in wanted)


# The JSON schema already conveys these sections.
_SCHEMA_SECTIONS = ("Args:", "Returns:", "Raises:", "Example:", "Examples:")


def tool_description(tool: Callable[..., Any]) -> str:
    """Return the part of a tool's docstring worth advertising to the model."""

    doc = inspect.getdoc(tool) or ""

    kept: list[str] = []

    for line in doc.split("\n"):
        if line.strip() in _SCHEMA_SECTIONS:
            break

        kept.append(line)

    trimmed = "\n".join(kept).strip()

    return trimmed or doc.strip()


def apply_configuration() -> tuple[list[str], list[str]]:
    """Load input/config.json and populate the environment from it."""

    # Local import avoids a cycle through config.
    from .config import (
        apply_to_environment,
        find_config_file,
        load_config,
        resolve_profile,
        write_scaffold,
    )

    notes: list[str] = []
    problems: list[str] = []

    path = find_config_file()

    if not path.is_file():
        error = write_scaffold(path)

        if error is not None:
            problems.append(f"could not generate a config file: {error}")
        else:
            notes.append(f"generated {path} -- it lists every setting with its default")

    config, errors = load_config(path)

    problems.extend(f"{path.name}: {error}" for error in errors)

    skipped = apply_to_environment(config.settings)

    if skipped:
        problems.append(
            f"{', '.join(sorted(skipped))} set in the environment, "
            f"which overrides {path.name}"
        )

    requested = os.environ.get(PROFILE_ENV_VAR, "").strip() or config.active_profile

    if not requested:
        return notes, problems

    profile, error = resolve_profile(requested, config.profiles)

    if profile is None:
        problems.append(f"profile: {error}")
        return notes, problems

    notes.append(f"profile: {requested}")

    profile_skipped = apply_to_environment(
        {TOOL_GROUPS_ENV_VAR: profile.tools, **profile.settings}
    )

    if profile_skipped:
        problems.append(
            f"profile '{requested}' applied, but "
            f"{', '.join(sorted(profile_skipped))} was already set and takes "
            f"precedence"
        )

    return notes, problems


def posture_warnings(groups: frozenset[str]) -> list[str]:
    """Report group combinations that will not do what the operator expects."""

    messages: list[str] = []

    if "search" in groups and "docs" not in groups:
        messages.append(
            "the 'search' group is active but 'docs' is not, so web_search can "
            "find URLs that nothing can read. Add 'docs' to make results "
            "usable."
        )

    return messages


def main() -> None:
    """Register the configured tools and run the MCP server over stdio."""

    # Degrade, never die: a dead server is an opaque client failure.
    notes, problems = apply_configuration()

    for note in notes:
        log.info("%s", note)

    for problem in problems:
        log.warning("%s", problem)

    groups, error = active_tool_groups()

    if error is not None:
        log.warning("%s", error)

    log.info(
        "registering tool groups: %s",
        ", ".join(sorted(groups)),
    )

    for message in posture_warnings(groups):
        log.warning("%s", message)

    for tool in get_tools(groups=groups):
        mcp.add_tool(tool, description=tool_description(tool))

    mcp.run()


if __name__ == "__main__":
    main()
