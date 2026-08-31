"""Entry point for the Windows Agent MCP Server.

Registers the appropriate tools with the MCP server and starts the stdio
transport.
"""

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

# Always registered. Keep this list in sync with README.md.
#
# Grouped by what the model is trying to do, because the order a tool list is
# presented in measurably affects which tool a small model reaches for.
TOOLS: tuple[Callable[..., Any], ...] = (
    # Read and navigate
    read_file,
    list_directory,
    list_empty_dirs,
    find_files,
    search_files,
    # Write
    write_file,
    edit_file,
    # Build and compile
    build_project,
    compile_shader,
    run_powershell,
    # Machine and server facts
    get_system_info,
    get_gpu_info,
    get_server_info,
    # Network, allowlisted hosts only
    download_file,
    fetch_https_response,
    get_github_latest_release,
    # Documentation hosts by default, any public host in research mode
    fetch_web_page,
)

# Registered only when research mode is on.
#
# web_search is the one tool that cannot be scoped to a fixed set of trusted
# hosts: its whole purpose is to discover URLs nobody vetted, and it reaches a
# search engine by scraping HTML behind a spoofed browser User-Agent. Both are
# operator decisions.
#
# fetch_web_page deliberately does NOT live here any more. It is scoped to
# documentation hosts by default (ALLOWED_DOC_HOSTS), which lets a model check
# a Vulkan enum or a D3D12 resource state without anyone widening the network
# posture -- and reading learn.microsoft.com is not an exfiltration channel,
# because the attacker cannot read Microsoft's logs. Research mode still
# widens it to any public host.
WEB_TOOLS: tuple[Callable[..., Any], ...] = (web_search,)

# Every tool that exists, regardless of configuration. Used by the contract
# tests: registration is conditional, so iterating TOOLS alone would silently
# exempt the web tools from the docstring and MCP-schema checks.
ALL_TOOLS: tuple[Callable[..., Any], ...] = TOOLS + WEB_TOOLS

# Which group each tool belongs to. The group NAMES live in
# utils.VALID_TOOL_GROUPS; this is the mapping, kept here because it needs the
# tool imports. Every tool must appear in exactly one group -- tests enforce
# both halves and that the two sets of names agree.
#
# The boundaries follow trust, not topic:
#
#   core   read-only local inspection. Cannot change anything, cannot reach the
#          network. Always registered.
#   edit   the ability to modify source. The dangerous half of "coding".
#   build  the ability to start a program. Separate from `edit` on purpose:
#          "review and build, but do not touch my files" is a real posture,
#          and it is only expressible if these are distinct.
#   docs   reading reference documentation. Network, but read-only into
#          context, and only from ALLOWED_DOC_HOSTS.
#   net    fetching dependencies from ALLOWED_NETWORK_HOSTS. Writes to the
#          download sandbox, which is why it is not merged with `docs`.
#   search discovering URLs nobody vetted. Does NOT widen which hosts may
#          be read, so a found URL still needs `docs` plus a host the
#          operator allowed. That combination is the useful one for a small
#          model: it can find a page, then ask for it, instead of inventing
#          plausible URLs from memory -- which is what it does when it has
#          a page reader and no way to search.
#   research  everything `search` gives, plus any-public-host fetching.
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
    # Both map to the same tool: `search` is the capability, `research` is
    # that capability plus a wider fetch posture. get_tools() unions group
    # tools into a set, so listing web_search twice registers it once.
    "search": (web_search,),
    "research": (web_search,),
}


def get_tools(
    *,
    groups: frozenset[str] | None = None,
    web_enabled: bool | None = None,
) -> tuple[Callable[..., Any], ...]:
    """Return the tools to register for a given set of groups.

    Pure by design: takes the resolved groups as an argument rather than
    reading the environment. That matters because pre-commit runs the test
    suite on every commit -- if this read os.environ directly, anyone with
    WAMCP_TOOLS or WAMCP_WEB_RESEARCH exported (that is, anyone actually
    using the feature) would see a different tool count and be unable to
    commit.

    Args:
        groups: Group names to register. None means DEFAULT_TOOL_GROUPS.
        web_enabled: Deprecated compatibility shim, predating groups. When
            given, it adds or removes the `research` group from the resolved
            set. Prefer passing `groups`.

    Returns:
        The matching tools, in the order they appear in TOOLS + WEB_TOOLS.
    """

    resolved = DEFAULT_TOOL_GROUPS if groups is None else frozenset(groups)

    resolved = resolved | ALWAYS_ON_TOOL_GROUPS

    if web_enabled is not None:
        resolved = resolved | {"research"} if web_enabled else resolved - {"research"}

    wanted = {tool for name in resolved for tool in TOOL_GROUPS.get(name, ())}

    # Filter the canonical ordering rather than concatenating group tuples, so
    # presentation order is defined in exactly one place. It is not cosmetic:
    # the order a tool list is presented in affects which tool a small model
    # reaches for, which is why TOOLS is grouped by workflow.
    return tuple(tool for tool in ALL_TOOLS if tool in wanted)


# Google-style docstring sections that the JSON input schema already conveys.
# The schema states every parameter's name, type and default more precisely
# than prose can, so sending both spends context to say the same thing twice.
_SCHEMA_SECTIONS = ("Args:", "Returns:", "Raises:", "Example:", "Examples:")


def tool_description(tool: Callable[..., Any]) -> str:
    """Return the part of a tool's docstring worth advertising to the model.

    Measured on this server: the 18 tool definitions cost roughly 6,500 tokens
    over tools/list, and two thirds of that is descriptions rather than
    schemas. Around half the description text is the Args/Returns/Example
    blocks -- which duplicate the JSON schema the client already receives.
    Dropping them returns roughly 2,400 tokens, which on an 8K-context local
    model is a quarter of the entire window.

    What is deliberately KEPT is every line that steers tool choice: "Preferred
    over write_file for changing existing code", the uniqueness rule, the
    compiler-selection table. Those exist because a 7B-9B model picks the wrong
    tool without them, and cutting them would trade context away for exactly
    the errors the context was preventing.

    The full docstring stays in the source for whoever reads the code; only
    what goes over the wire is trimmed, and the function object is left
    untouched so nothing else in the process sees a shortened docstring.

    Args:
        tool: The tool function.

    Returns:
        The leading prose, or the whole docstring if there is no prose before
        the first section header.
    """

    doc = inspect.getdoc(tool) or ""

    kept: list[str] = []

    for line in doc.split("\n"):
        if line.strip() in _SCHEMA_SECTIONS:
            break

        kept.append(line)

    trimmed = "\n".join(kept).strip()

    # A docstring that opens straight into "Args:" would otherwise advertise
    # nothing at all, which is worse than advertising too much.
    return trimmed or doc.strip()


def apply_configuration() -> tuple[list[str], list[str]]:
    """Load input/config.json and populate the environment from it.

    Done here, before anything reads a setting, rather than inside
    `active_tool_groups()`: config.py needs `parse_tool_groups` from utils, so
    utils cannot import it. Resolving at startup and writing the result into the
    environment means every existing path -- active_tool_groups,
    web_research_enabled, get_download_root, every tool -- keeps working with no
    new coupling.

    Generates the file when it is absent. That is the point of it: a fresh
    install gets a complete, documented table of every setting rather than
    having to discover the variable names from documentation.

    Returns:
        (notes, problems). Notes are news -- "a config file was generated" --
        and belong at info. Problems are conflicts and errors, and belong at
        warning. Keeping them apart matters: a warning on the ordinary path is
        how an operator learns to ignore warnings.
    """

    # Imported lazily: config imports get_tools from this module for its
    # --list output, so a module-scope import here would be a cycle.
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
            # Not fatal: the server runs on defaults perfectly well, so a
            # read-only checkout must not be a startup failure.
            problems.append(f"could not generate a config file: {error}")
        else:
            notes.append(f"generated {path} -- it lists every setting with its default")

    config, errors = load_config(path)

    problems.extend(f"{path.name}: {error}" for error in errors)

    skipped = apply_to_environment(config.settings)

    if skipped:
        # A conflict, not noise: this only fires when the operator has set a
        # value in the file AND in the environment. A silently ignored setting
        # is the confusion the file exists to remove.
        problems.append(
            f"{', '.join(sorted(skipped))} set in the environment, "
            f"which overrides {path.name}"
        )

    # An explicitly requested profile beats the file's own choice, same rule.
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
    """Report group combinations that will not do what the operator expects.

    Not validation -- every combination here is legal and starts the server.
    These are the ones where the resulting behaviour is silently useless, and
    the symptom appears as odd model behaviour rather than as an error.

    Pure, so the table is testable without starting anything.

    Args:
        groups: The resolved group set.

    Returns:
        Zero or more messages, for stderr.
    """

    messages: list[str] = []

    if "search" in groups and "docs" not in groups:
        # The trap this group was added to close, reintroduced from the other
        # side: with search but no fetch_web_page the model gets a list of URLs
        # and no way to open any of them. It then does what it did before --
        # answers from memory, or reports a link it never read.
        messages.append(
            "the 'search' group is active but 'docs' is not, so web_search can "
            "find URLs that nothing can read. Add 'docs' to make results "
            "usable."
        )

    # Deliberately NOT warned about: `docs` without `search`. That is the
    # DEFAULT posture and a legitimate one -- reading the Vulkan spec without
    # granting a search tool is the whole reason `docs` exists. Warning on the
    # default configuration would train the operator to ignore warnings, which
    # costs more than this note is worth. It lives in the troubleshooting table
    # in docs/HOW_TO_USE.md instead, where someone asking "why does it invent
    # will actually look.

    return messages


def main() -> None:
    """Register the configured tools and run the MCP server over stdio."""

    # A bad config degrades to defaults rather than killing startup: an MCP
    # client renders a dead server as an opaque connection failure, which is
    # far harder to diagnose than a warning on stderr.
    notes, problems = apply_configuration()

    for note in notes:
        log.info("%s", note)

    for problem in problems:
        log.warning("%s", problem)

    groups, error = active_tool_groups()

    if error is not None:
        # stderr, never stdout: stdout is the JSON-RPC wire. The server still
        # starts, with the default tool set, so a typo is visible rather than
        # fatal -- get_server_info reports the same message.
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
