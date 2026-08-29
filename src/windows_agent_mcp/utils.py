"""Utility functions for Windows Agent MCP Server."""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.request
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, ClassVar
from urllib.parse import urlparse

from .hostgrants import DEFAULT_GRANTS_FILENAME, granted_hosts

# ============================================================
# Configuration with type hints
# ============================================================

SERVER_VERSION: str = "1.0.0"

# Maximum HTTP response returned by http_get().
MAX_HTTP_BYTES: int = 2 * 1024 * 1024  # 2 MB

# Maximum file that download_file() may download.
MAX_DOWNLOAD_BYTES: int = 500 * 1024 * 1024  # 500 MB

# Maximum length of a sanitized download filename. Well under the 255-char
# limit of a single NTFS path component, leaving room for the download root
# prefix and the ".part" suffix used while a download is in flight.
MAX_FILENAME_LENGTH: int = 180

# Maximum time to download.
HTTP_TIMEOUT_SECONDS: int = 20

POWERSHELL_TIMEOUT_SECONDS: int = 300

# Builds get their own, longer default: a cold C++ build of a game project
# routinely runs for minutes, and killing it at the shell default would make
# build_project useless for exactly the projects it exists for.
BUILD_TIMEOUT_SECONDS: int = 600

# Shader compiles are fast. A glslc invocation that has not finished in a
# minute is stuck, not busy.
SHADER_TIMEOUT_SECONDS: int = 60

# ============================================================
# Context budget
# ============================================================

# This server targets small local models (7B-9B class) with correspondingly
# small context windows. An unbounded read_file or list_directory can consume
# the entire window in a single tool call, so every tool that returns
# arbitrary-length content truncates and says so.

# Default number of lines read_file() returns in one call.
DEFAULT_READ_LINES: int = 2000

# Hard cap on bytes read_file() will decode, regardless of line count.
MAX_READ_BYTES: int = 256 * 1024  # 256 KB

# Maximum entries list_directory() will report.
MAX_DIRECTORY_ENTRIES: int = 1000

# Maximum directories list_empty_dirs() will report.
MAX_EMPTY_DIR_RESULTS: int = 1000

# Maximum search results web_search() will report.
MAX_SEARCH_RESULTS: int = 5

# Maximum characters of a single search result field (title, snippet). Result
# text is attacker-controlled, so it is capped as well as truncated.
MAX_SEARCH_FIELD_CHARS: int = 300

# Hard cap on raw HTML fetch_web_page() will download before extraction.
MAX_PAGE_BYTES: int = 2 * 1024 * 1024  # 2 MB

# Maximum links fetch_web_page() lists so the model can navigate onward.
MAX_PAGE_LINKS: int = 20

# Maximum bytes write_file() will accept in one call. Generous for source
# code, small enough that a runaway generation cannot fill a disk.
MAX_WRITE_BYTES: int = 2 * 1024 * 1024  # 2 MB

# Maximum matches search_files() will report.
MAX_SEARCH_MATCHES: int = 100

# Maximum bytes search_files() will scan in a single file. A generated
# single-line header or an accidentally committed blob would otherwise dominate
# the scan for no benefit.
MAX_GREP_FILE_BYTES: int = 1024 * 1024  # 1 MB

# Maximum files search_files() / find_files() will visit or report. Game trees
# are large; without a ceiling a mistyped glob walks the whole drive.
MAX_FILES_SCANNED: int = 20_000
MAX_FIND_RESULTS: int = 500

# Directories never worth searching: version control, build output and vendored
# dependencies. On a C++ game project the build tree is usually an order of
# magnitude larger than the source, so skipping it is the difference between a
# fast grep and one that reads gigabytes of object files.
#
# Matched case-insensitively against the directory NAME at any depth. "build"
# and "out" are deliberately included even though they occasionally hold
# checked-in files: a false skip is recoverable via read_file, whereas
# scanning Intermediate/ is not recoverable within a small context window.
SKIPPED_DIRECTORY_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        ".vs",
        ".vscode",
        ".idea",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "build",
        "builds",
        "out",
        "bin",
        "obj",
        "binaries",
        "intermediate",
        "deriveddatacache",
        "cmakefiles",
        "x64",
        "x86",
        "debug",
        "release",
        "relwithdebinfo",
        "minsizerel",
        "packages",
        "target",
        "dist",
        ".cache",
    }
)

# ============================================================
# Tool groups
# ============================================================

# Tool definitions are re-serialized into the model's prompt on EVERY turn, so
# they are a permanent tax on the context window rather than a one-off cost.
# Measured on this server: 18 tools cost roughly 4,000 tokens over tools/list
# even after the description trim in main.tool_description() -- about 20% of a
# 16K window, or 40% of an 8K one, before the model reads a line of code.
#
# Groups let one server present only the tools a session actually needs. A
# session writing C++ has no use for download_file; a review session needs no
# write access at all.
#
# Deliberately NOT solved by running several MCP servers. The tools that any
# one purpose needs are the expensive ones, so splitting buys far less than it
# looks (~16-26% for a C++ workflow), and it would put resolve_write_path, the
# PowerShell allowlist, SSRF validation and the diagnostics parser into
# separate codebases that must not drift on security policy. Separate
# processes also do not break the exfiltration chain, because the model holds
# both tool lists in one context and can relay data between them itself.
TOOL_GROUPS_ENV_VAR: str = "BIONIC_TOOLS"

# Selects a named profile from mcp-profiles.json, which is a friendlier way to
# say the same thing as TOOL_GROUPS_ENV_VAR.
#
# Note what does NOT happen here: active_tool_groups() below knows nothing
# about profiles. It cannot -- profiles.py needs VALID_TOOL_GROUPS from this
# module, so importing it here would be a cycle. main() resolves the profile
# first and populates TOOL_GROUPS_ENV_VAR from it, after which every path below
# works unchanged. See profiles.apply_profile_to_environment.
PROFILE_ENV_VAR: str = "BIONIC_PROFILE"

# Group NAMES live here; the name -> tools mapping lives in main.TOOL_GROUPS,
# because that needs the tool imports and is a registration concern. utils
# cannot import from main (main imports utils), and fetch_web_page needs to ask
# whether research is on without knowing about main at all. A test asserts the
# two halves cannot drift.
VALID_TOOL_GROUPS: frozenset[str] = frozenset(
    {
        # Read-only local inspection. Always registered.
        "core",
        # write_file, edit_file.
        "edit",
        # run_powershell, build_project, compile_shader, get_gpu_info.
        "build",
        # fetch_web_page, scoped to ALLOWED_DOC_HOSTS.
        "docs",
        # download_file, fetch_https_response, get_github_latest_release.
        "net",
        # web_search alone: finding URLs, without widening which hosts may
        # be READ. Split from `research` because those are different
        # permissions that happened to share a switch.
        #
        # Search is a far weaker channel than any-host fetching. The
        # exfiltration risk in `research` is the arbitrary outbound GET: the
        # attacker reads their own server's logs. A search query reaches one
        # endpoint the attacker does not control, so it carries data nowhere
        # they can see it. What search DOES do is put attacker-chosen titles
        # and snippets in the model's context, which is why results are
        # wrapped as untrusted -- the same treatment fetched pages get.
        "search",
        # web_search, and widens fetch_web_page to any public host.
        "research",
    }
)

# Registered when TOOL_GROUPS_ENV_VAR is unset: everything except the two
# groups that reach out to the open internet.
#
# Chosen so an existing install sees exactly the tool set it saw before
# groups existed. `search` is excluded for the same reason as `research`
# rather than because it is equally dangerous: a tool that sends a
# model-composed query to a third-party search engine is something the
# operator should switch on deliberately, and adding it to the default would
# silently change the posture of every existing install on upgrade.
DEFAULT_TOOL_GROUPS: frozenset[str] = VALID_TOOL_GROUPS - {
    "research",
    "search",
}

# Always present regardless of configuration. get_server_info is in this group
# on purpose: it is the tool that reports which groups are active, so "why is
# my tool missing" has to stay answerable in every configuration.
ALWAYS_ON_TOOL_GROUPS: frozenset[str] = frozenset({"core"})

# Accepted in TOOL_GROUPS_ENV_VAR to mean "every group".
_ALL_GROUPS_KEYWORD = "all"

# ============================================================
# Web research mode
# ============================================================

# Research mode is the `research` group. Two things follow from enabling it:
#
#   1. web_search is registered. It cannot be scoped to trusted hosts the way
#      fetch_web_page can -- its whole purpose is discovering URLs nobody
#      vetted -- and it scrapes a search engine behind a spoofed browser
#      User-Agent.
#   2. fetch_web_page may reach any public HTTPS host, rather than only
#      ALLOWED_NETWORK_HOSTS plus ALLOWED_DOC_HOSTS.
#
# Point 2 is a real widening of the network posture. HTTPS-only, no
# credentials, port 443 only, and private/non-global IP rejection all still
# apply, and download_file / fetch_https_response keep the strict allowlist so
# nothing new can write to disk. But an arbitrary outbound GET combined with
# read_file is an exfiltration channel, and fetched pages are untrusted text
# going to a model that also has run_powershell.
#
# NOTE, and this is a deliberate operator-chosen tradeoff rather than an
# oversight: because `research` is an ordinary tool group, listing it in
# BIONIC_TOOLS widens the network posture on its own. A context-economy
# setting therefore also changes a security setting. That is one variable to
# reason about instead of two, which is what was asked for -- but it must be
# stated plainly wherever BIONIC_TOOLS is documented.
WEB_RESEARCH_ENV_VAR: str = "BIONIC_WEB_RESEARCH"

# Selects the search backend. See search_backends.get_backend().
SEARCH_BACKEND_ENV_VAR: str = "BIONIC_SEARCH_BACKEND"

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


def parse_tool_groups(value: str | None) -> tuple[frozenset[str], str | None]:
    """Parse a BIONIC_TOOLS value into a set of group names.

    Pure: takes the raw string rather than reading the environment, so the test
    suite can cover the whole table without touching os.environ.

    An unrecognised name falls back to the default set AND returns an error,
    rather than raising. Raising would abort startup, and an MCP client renders
    that as an opaque connection failure with no hint of the cause. Falling
    back keeps the server usable while the mistake stays visible in stderr and
    in get_server_info, so the model can tell the user what is wrong. What is
    NOT acceptable is a typo silently yielding a different tool set, and
    reporting prevents that without a dead server.

    Args:
        value: Raw environment value, or None.

    Returns:
        (groups, error). `groups` always contains ALWAYS_ON_TOOL_GROUPS.
        `error` is None on success, else a message naming the bad value and
        listing the valid group names.
    """

    if value is None or not value.strip():
        return DEFAULT_TOOL_GROUPS, None

    requested = [part.strip().lower() for part in value.split(",")]
    requested = [part for part in requested if part]

    # A value holding separators but no actual names (",,,") is a mistake, and
    # indistinguishable in intent from an empty one -- so it degrades the same
    # way rather than silently yielding a core-only server.
    if not requested:
        return DEFAULT_TOOL_GROUPS, None

    if _ALL_GROUPS_KEYWORD in requested:
        return VALID_TOOL_GROUPS, None

    unknown = sorted({part for part in requested if part not in VALID_TOOL_GROUPS})

    if unknown:
        return DEFAULT_TOOL_GROUPS, (
            f"{TOOL_GROUPS_ENV_VAR} contains unknown group(s): "
            f"{', '.join(unknown)}. Valid groups: "
            f"{', '.join(sorted(VALID_TOOL_GROUPS))}, or '{_ALL_GROUPS_KEYWORD}'. "
            f"Falling back to the default set: "
            f"{', '.join(sorted(DEFAULT_TOOL_GROUPS))}."
        )

    return frozenset(requested) | ALWAYS_ON_TOOL_GROUPS, None


def active_tool_groups() -> tuple[frozenset[str], str | None]:
    """Resolve the groups to register from the environment.

    BIONIC_WEB_RESEARCH is honoured as an alias that implicitly adds the
    `research` group. That keeps every existing setup working -- the launcher's
    --web flag, .env.example, and any MCP client config written before groups
    existed -- and it avoids the confusing half-state where the network posture
    widens but web_search is absent.

    Returns:
        (groups, error) as described on parse_tool_groups.
    """

    groups, error = parse_tool_groups(os.environ.get(TOOL_GROUPS_ENV_VAR))

    if os.environ.get(WEB_RESEARCH_ENV_VAR, "").strip().lower() in _TRUTHY_VALUES:
        groups = groups | {"research"}

    return groups, error


def web_search_enabled() -> bool:
    """Return True when web_search is available.

    True for the `search` group and for `research`, which keeps `research`
    a strict superset -- an operator who had research mode on before this
    group existed loses nothing.

    Separate from web_research_enabled() on purpose: that one answers "may
    fetch_web_page read any host", which is a different and much larger
    permission. Conflating them is what made "find URLs but still gate
    reading" inexpressible.

    Returns:
        True if the `search` or `research` group is active.
    """

    return not active_tool_groups()[0].isdisjoint({"search", "research"})


def web_research_enabled() -> bool:
    """Return True when research mode is on, by either mechanism.

    Routed through the group set rather than reading WEB_RESEARCH_ENV_VAR
    directly, so `BIONIC_TOOLS=...,research` and `BIONIC_WEB_RESEARCH=1` mean
    the same thing to fetch_web_page and web_search without either tool
    knowing how the group set was assembled.

    Returns:
        True if the `research` group is active.
    """

    return "research" in active_tool_groups()[0]


# DuckDuckGo's HTML endpoints reject non-browser User-Agents: the urllib
# default and an honest "windows-agent-mcp/1.0" both get a 202 anomaly page
# instead of results.
#
# This is deliberately evading a bot filter, which is a policy choice rather
# than a technical detail -- it is called out in README.md rather than left
# buried here. Kept as a named constant so it is one line to change.
BROWSER_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ============================================================
# Safe download directory
# ============================================================


def default_download_root() -> Path:
    """Return a per-machine default download directory.

    Deliberately not a hardcoded absolute path: a path baked in from one
    developer's machine cannot be created on anyone else's (mkdir under
    another user's profile raises PermissionError), which took down every
    tool that touches the download root.

    Prefers %LOCALAPPDATA% on Windows, falling back to the platform-neutral
    ~/.local/share when it is unset.

    Returns:
        Path to the default download root. Not created by this function.
    """

    local_app_data = os.environ.get("LOCALAPPDATA")

    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path.home() / ".local" / "share"

    return base / "windows-agent-mcp" / "downloads"


def get_download_root() -> Path:
    """Return the configured download directory, creating it if needed.

    Optional environment variable: BIONIC_DOWNLOAD_ROOT

    If absent, falls back to default_download_root(), which resolves to
    %LOCALAPPDATA%\\windows-agent-mcp\\downloads on Windows.

    Returns:
        Path to the download root directory, ensuring it exists.

    Raises:
        OSError: If the directory cannot be created (e.g. BIONIC_DOWNLOAD_ROOT
                 points somewhere unwritable).
    """

    configured = os.environ.get("BIONIC_DOWNLOAD_ROOT")

    if configured:
        root = Path(configured)
    else:
        root = default_download_root()

    root = root.expanduser().resolve()

    root.mkdir(parents=True, exist_ok=True)

    return root


# ============================================================
# Working directories for run_powershell
# ============================================================

# run_powershell() used to force cwd to the download root unconditionally.
# Because each call is a fresh process, the allowlisted `cd` never persisted,
# so the model could not run git/cmake against an actual source tree -- the
# thing the tool exists for.
#
# Callers may now pass a working_directory, but only inside a root the
# OPERATOR has approved. Configure with a path-separator-delimited list:
#
#   BIONIC_PROJECT_ROOTS=C:/work/game;C:/dev/engine
#
# When unset, the download root remains the only permitted location, so the
# default behaviour is unchanged and widening it is opt-in.
PROJECT_ROOTS_ENV_VAR: str = "BIONIC_PROJECT_ROOTS"

# Filename of the tool-group profiles file.
#
# Defined here rather than in profiles.py, which is the module that owns the
# format, purely because of import direction: profiles imports this module, so
# the write guard below cannot import the constant from there.
PROFILES_FILENAME: str = "mcp-profiles.json"

# Files the model may never write, by NAME, anywhere on disk.
#
# These two files are configuration the server trusts: the grants file says
# which hosts may be read, and the profiles file says which tools load and
# which directories are writable. A model that can write either one can widen
# its own permissions, which makes every other check in this module decorative.
#
# Matching on filename rather than resolved path is deliberate and is the whole
# point. Both files are looked up with a search order that PREFERS the current
# directory, so the dangerous move is not editing the file we are reading --
# it is creating one where none existed, in a directory that gets searched
# first. A path-equality check cannot see that file, because it does not exist
# yet. A name check refuses it before it can.
#
# The cost of the broader rule is nil: no real project contains a file by
# either name that a coding assistant needs to edit, and an operator editing
# their own config does not go through this function.
PROTECTED_CONFIG_FILENAMES: frozenset[str] = frozenset(
    {
        PROFILES_FILENAME,
        DEFAULT_GRANTS_FILENAME,
    }
)


class ProtectedPathError(ValueError):
    """A write was refused because the path names server configuration.

    A ValueError subclass so existing `except ValueError` handlers keep
    working, but a distinct type so the writing tools can say something
    different. The generic refusal tells the model to ask for the directory to
    be added to BIONIC_PROJECT_ROOTS, which for this case is advice that cannot
    possibly work -- and sends the user off to change a setting for no reason.
    """


def get_allowed_working_directories() -> list[Path]:
    """Return the roots that run_powershell may execute inside.

    Always includes the download root. Additional roots come from
    BIONIC_PROJECT_ROOTS, separated by os.pathsep (";" on Windows).

    Non-existent configured roots are skipped rather than raising, so one
    stale entry cannot disable the tool entirely.

    Returns:
        Resolved, existing directories, download root first, no duplicates.
    """

    roots: list[Path] = [get_download_root()]

    configured = os.environ.get(PROJECT_ROOTS_ENV_VAR, "")

    for entry in configured.split(os.pathsep):
        entry = entry.strip().strip('"')

        if not entry:
            continue

        candidate = Path(entry).expanduser()

        try:
            resolved = candidate.resolve()
        except OSError:
            continue

        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)

    return roots


def resolve_working_directory(requested: str | None) -> Path:
    """Validate a requested working directory against the allowed roots.

    Args:
        requested: Directory supplied by the caller, or None for the default.

    Returns:
        The resolved directory to execute in.

    Raises:
        ValueError: If the path does not exist, is not a directory, or falls
                    outside every allowed root.
    """

    allowed = get_allowed_working_directories()

    if requested is None or not requested.strip():
        return allowed[0]

    candidate = Path(requested).expanduser()

    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ValueError(
            f"Could not resolve working directory '{requested}': {exc}"
        ) from exc

    if not resolved.exists():
        raise ValueError(f"Working directory does not exist: {resolved}")

    if not resolved.is_dir():
        raise ValueError(f"Working directory is not a directory: {resolved}")

    for root in allowed:
        if resolved == root or root in resolved.parents:
            return resolved

    raise ValueError(
        f"Working directory '{resolved}' is outside every allowed root. "
        f"Allowed roots: {[str(r) for r in allowed]}. "
        f"Set {PROJECT_ROOTS_ENV_VAR} to permit additional locations."
    )


# ============================================================
# Write confinement
# ============================================================


def resolve_write_path(requested: str) -> Path:
    """Validate a path the caller wants to WRITE, against the allowed roots.

    Writing reuses the same consent mechanism as run_powershell rather than
    introducing a second switch, and that is a deliberate security decision.
    With BIONIC_PROJECT_ROOTS unset the only writable location is the download
    root -- a sandbox the operator has already accepted -- so a default
    install cannot modify source code anywhere. Granting write access to a
    real project is the same single action that grants the right to run cmake
    inside it, which is the same trust decision in practice.

    Note the asymmetry with read_file, which is deliberately unconfined: a
    bad read costs context, a bad write costs work.

    Args:
        requested: Destination path supplied by the caller.

    Returns:
        The resolved, confined path. Not created, and its parent is not
        created either -- that is the caller's decision to make explicit.

    Raises:
        ValueError: If the path is empty, unresolvable, points at an existing
                    directory, names a protected configuration file, or falls
                    outside every allowed root.
    """

    if not requested or not requested.strip():
        raise ValueError("Path cannot be empty.")

    allowed = get_allowed_working_directories()

    candidate = Path(requested).expanduser()

    try:
        # resolve() with a non-existent leaf still normalises the existing
        # prefix, which is what makes the containment test below sound: a
        # symlinked parent pointing out of the root is followed HERE, before
        # the check, rather than silently at open() time.
        resolved = candidate.resolve()
    except OSError as exc:
        raise ValueError(f"Could not resolve path '{requested}': {exc}") from exc

    if resolved.is_dir():
        raise ValueError(f"Path is an existing directory, not a file: {resolved}")

    # Checked on the resolved name as well as the requested one: on Windows a
    # path can reach a file through its 8.3 short name, and resolve() expands
    # that back to the real one.
    for name in {candidate.name.lower(), resolved.name.lower()}:
        if name in {protected.lower() for protected in PROTECTED_CONFIG_FILENAMES}:
            raise ProtectedPathError(
                f"'{name}' is server configuration and cannot be written by a "
                f"tool. It decides which hosts may be read and which "
                f"directories are writable, so only the operator may change "
                f"it. Ask the user to edit it, or to run: "
                f"python -m windows_agent_mcp.hostgrants --add HOST"
            )

    for root in allowed:
        if root in resolved.parents:
            return resolved

    raise ValueError(
        f"Path '{resolved}' is outside every writable root. "
        f"Writable roots: {[str(r) for r in allowed]}. "
        f"Set {PROJECT_ROOTS_ENV_VAR} to permit writing elsewhere."
    )


# ============================================================
# SSRF protection
# ============================================================


def is_private_or_special_ip(address: str) -> bool:
    """Reject IPs that should never be accessed by this MCP.

    Blocks:
        loopback
        private networks
        link-local
        multicast
        unspecified
        reserved
        anything not globally routable -- notably carrier-grade NAT
        (100.64.0.0/10, which is also the Tailscale range), 6to4 and
        Teredo relays, and benchmarking ranges

    The final `not ip.is_global` term is load-bearing, not belt-and-braces.
    Without it 100.64.0.0/10 passes: on Python 3.13 a CGNAT address reports
    is_private=False AND is_reserved=False, so every other term above is
    False too. Worse, that classification changed during the 3.12 series, so
    relying on is_private alone made the SSRF posture depend on the
    interpreter's patch level. is_global settles it in one term for both IPv4
    and IPv4-mapped IPv6.

    Args:
        address: IP address string to check.

    Returns:
        True if the IP is private/special/non-global, False otherwise.
        Unparseable input returns True -- this fails closed.
    """

    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True

    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or not ip.is_global
    )


# ============================================================
# Network allowlist
# ============================================================

ALLOWED_NETWORK_HOSTS: set[str] = {
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "premake.github.io",
}

# Reference documentation, readable by fetch_web_page WITHOUT research mode.
#
# Kept separate from ALLOWED_NETWORK_HOSTS on purpose. That set also governs
# download_file and fetch_https_response, so adding a host there grants the
# right to write its bytes to disk. These hosts are only ever *read into
# context*, which is a much smaller grant.
#
# This does not reopen the exfiltration channel that research mode does.
# Exfiltration needs an arbitrary outbound GET so the attacker can read their
# own server's logs; smuggling data into a path on a host the attacker does
# not control tells them nothing. Hence: fixed list, no wildcards.
#
# Graphics work is the reason this exists. A model writing Vulkan or D3D12
# code guesses enum and structure names constantly, and the fix is one spec
# lookup -- which should not require opening the network posture.
ALLOWED_DOC_HOSTS: frozenset[str] = frozenset(
    {
        # Vulkan / OpenGL / SPIR-V
        "registry.khronos.org",
        "docs.vulkan.org",
        "www.khronos.org",
        "khronos.org",
        "vulkan.lunarg.com",
        # Direct3D, HLSL, Win32, MSVC
        "learn.microsoft.com",
        # C++ language and standard library
        "en.cppreference.com",
        "www.cppreference.com",
        "isocpp.org",
        # Build systems already represented in ALLOWED_COMMANDS
        "cmake.org",
        "ninja-build.org",
        # Vendor graphics documentation
        "gpuopen.com",
        "developer.nvidia.com",
    }
)


def readable_web_hosts() -> tuple[frozenset[str], str | None]:
    """The documentation hosts plus whatever the operator has granted.

    NOT the complete set of hosts fetch_web_page can read, and the difference
    has misled a reader before. This is what callers pass as
    `extra_allowed_hosts`; is_allowed_host() then checks ALLOWED_NETWORK_HOSTS
    as well, so github.com and friends are readable too without appearing here.
    The full readable set is this plus ALLOWED_NETWORK_HOSTS.

    One function so the fetcher, the redirect handler and get_server_info
    cannot disagree about the granted part -- a disagreement between the first
    two would look like a site that loads and then fails on its own redirect.

    Returns:
        (hosts, error) where error describes a malformed grants file, or None.
    """

    granted, error = granted_hosts()

    return ALLOWED_DOC_HOSTS | granted, error


def host_grant_would_help(url: str) -> str | None:
    """Return the hostname if the ONLY thing wrong with a URL is the allowlist.

    Used to decide whether asking the operator for consent is even meaningful.
    Offering to unblock a URL that is refused for being HTTP, or for naming a
    port, or for resolving to a private address, would be offering a permission
    that changes nothing -- and for the private-address case it would be
    inviting the operator to approve an SSRF probe.

    Deliberately does NOT resolve DNS. resolve_and_validate_host() checks the
    allowlist *before* it resolves, so today a refused host is never looked up;
    a lookup here would leak the attempt to the host's authoritative nameserver
    before anyone consented to it. The consequence is that consent can be
    granted for a host that then fails the private-address check -- rare, and
    it fails safely with an accurate message.

    Args:
        url: The URL that was refused.

    Returns:
        The hostname a grant would unblock, or None if a grant is not the
        answer.
    """

    try:
        hostname = validate_url_shape(url)
    except ValueError:
        return None

    if web_research_enabled():
        # Nothing is host-blocked in research mode, so a denial came from
        # somewhere else entirely.
        return None

    readable, _error = readable_web_hosts()

    if is_allowed_host(hostname, extra=readable):
        return None

    return hostname.lower().rstrip(".")


def is_allowed_host(
    hostname: str,
    *,
    extra: frozenset[str] = frozenset(),
) -> bool:
    """Check whether hostname is explicitly allowed.

    Args:
        hostname: Hostname to check (will be lowercased).
        extra: Additional hostnames permitted for this call only. Used by
            web_search to reach its one search endpoint without widening the
            policy for everything else.

    Returns:
        True if hostname is in the allowlist or in `extra`, False otherwise.
    """

    hostname = hostname.lower().rstrip(".")

    return hostname in ALLOWED_NETWORK_HOSTS or hostname in extra


def resolve_and_validate_host(
    hostname: str,
    *,
    allow_any_host: bool = False,
    extra_allowed_hosts: frozenset[str] = frozenset(),
) -> None:
    """Resolve hostname and reject private/special addresses.

    This provides defense against DNS-based SSRF.

    NOTE:
        This is still not a perfect network sandbox. DNS is resolved here and
        again by urllib at connect time, so a hostile resolver can race the
        two. With the six-host allowlist that requires controlling the
        resolver; with allow_any_host the attacker owns the authoritative zone
        for their own domain and can simply serve a public address to this
        lookup and a private one to the connect lookup. Closing that needs a
        pinned-IP connector, which this server does not implement. For a true
        security boundary use OS/network isolation.

    Args:
        hostname: Hostname to resolve and validate.
        allow_any_host: Skip the allowlist check ONLY. Every other check --
            resolution and private/non-global IP rejection -- still runs.
            Reserved for research mode.
        extra_allowed_hosts: Additional hostnames permitted for this call.

    Raises:
        ValueError: If hostname is not allowed, cannot be resolved,
                    or resolves to private/special IPs.
    """

    if not hostname:
        raise ValueError("URL has no hostname.")

    if not allow_any_host and not is_allowed_host(hostname, extra=extra_allowed_hosts):
        raise ValueError(f"Domain '{hostname}' is not allowed.")

    try:
        results = socket.getaddrinfo(
            hostname,
            443,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve '{hostname}': {exc}") from exc

    addresses: set[str] = {str(result[4][0]) for result in results}

    if not addresses:
        raise ValueError(f"No IP addresses found for '{hostname}'.")

    for address in addresses:
        if is_private_or_special_ip(address):
            raise ValueError(f"Blocked network address for '{hostname}': {address}")


# ============================================================
# URL validation
# ============================================================


def validate_url(
    url: str,
    *,
    allow_any_host: bool = False,
    extra_allowed_hosts: frozenset[str] = frozenset(),
) -> str:
    """Validate a URL before any network operation.

    Args:
        url: URL string to validate.
        allow_any_host: Skip the host allowlist ONLY. Length, HTTPS-only,
            credential, port and private-IP checks all still apply. Used by
            fetch_web_page in research mode.
        extra_allowed_hosts: Additional hostnames permitted for this call.
            Used by web_search for its single search endpoint -- narrower and
            therefore preferable to allow_any_host where it suffices.

    Returns:
        The validated URL (same as input if valid).

    Raises:
        ValueError: If the URL is too long (>4096 chars), not HTTPS, contains
                    credentials, names a port other than 443, or has a
                    disallowed/unresolvable hostname.
    """

    hostname = validate_url_shape(url)

    resolve_and_validate_host(
        hostname,
        allow_any_host=allow_any_host,
        extra_allowed_hosts=extra_allowed_hosts,
    )

    return url


def validate_url_shape(url: str) -> str:
    """Check everything about a URL that does not require DNS.

    Split out from validate_url so a caller that is only *displaying* a URL,
    rather than fetching it, can reject the dangerous shapes without paying
    for a DNS lookup. web_search uses this on every result: resolving each
    one would mean a lookup per hit, and would stop the result parser from
    being a pure function. The full check still runs in fetch_web_page before
    anything is actually retrieved.

    Args:
        url: URL string to check.

    Returns:
        The hostname, so the caller can pass it to resolve_and_validate_host.

    Raises:
        ValueError: If the URL is too long, not HTTPS, carries credentials,
                    names a port other than 443, or has no hostname.
    """

    if len(url) > 4096:
        raise ValueError("URL is too long.")

    parsed = urlparse(url)

    if parsed.scheme.lower() != "https":
        raise ValueError("Only HTTPS URLs are allowed.")

    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed.")

    # Only the default HTTPS port is permitted.
    #
    # resolve_and_validate_host() always resolves against port 443, so a URL
    # naming a different port was validated for one service and then connected
    # to another. urlparse raises on a malformed port and reports 0 (not None)
    # for ":0", so both are handled here rather than compared naively.
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"URL has an invalid port: {exc}") from exc

    if port is not None and port != 443:
        raise ValueError(f"Only the default HTTPS port 443 is allowed, not {port}.")

    hostname = parsed.hostname

    if not hostname:
        raise ValueError("URL has no hostname.")

    return hostname


# ============================================================
# Safe redirect handler
# ============================================================


class RedirectNotAllowedError(ValueError):
    """A redirect hop was refused by policy rather than by the network.

    Carries the target so the caller can name it. A refusal the operator can
    act on has to say *which* host was refused, and that host is not the one
    the model asked for -- it is wherever the site sent the request next.
    """

    def __init__(self, url: str, reason: str):
        super().__init__(f"Redirect to {url} was refused: {reason}")

        self.url = url
        self.reason = reason
        self.hostname = urlparse(url).hostname or ""


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect.

    urllib normally follows redirects automatically.
    We don't want:

        github.com
            ↓
        malicious.example
            ↓
        localhost

    The policy is a CLASS ATTRIBUTE rather than a constructor argument, and
    that is not a style choice. build_opener() instantiates a handler class
    with zero arguments (`if isinstance(h, type): h = h()`), so an __init__
    parameter would raise TypeError for anyone passing the class. Do not
    "clean this up" into an __init__ argument.

    Raises:
        ValueError: If the redirect target fails validation.
    """

    allow_any_host: ClassVar[bool] = False

    # Same class-attribute constraint as allow_any_host, for the same reason.
    extra_allowed_hosts: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def allowed_extra_hosts(cls) -> frozenset[str]:
        """Hosts permitted for a redirect hop, evaluated per hop.

        A method rather than the class attribute directly so a subclass can
        return something that changes while the process runs. The openers below
        are built once at import; a grants file edited afterwards has to affect
        redirects too, or a granted host would be readable at its canonical URL
        and refused the moment it redirected -- which is most documentation
        sites.
        """

        return cls.extra_allowed_hosts

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Validate the redirect target before following it."""

        try:
            validate_url(
                newurl,
                allow_any_host=self.allow_any_host,
                extra_allowed_hosts=self.allowed_extra_hosts(),
            )
        except ValueError as exc:
            # Re-raised as a distinct type so the caller can tell "policy
            # refused a hop" from "the network failed".
            #
            # Not cosmetic. The commonest instance of this is a granted host
            # redirecting to its own apex domain -- www.example.com to
            # example.com, which is a different hostname and genuinely not
            # granted. Reported as a generic fetch failure, that told the
            # operator to check their network connection, about a request that
            # was refused by their own allowlist.
            raise RedirectNotAllowedError(newurl, str(exc)) from exc

        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ResearchRedirectHandler(SafeRedirectHandler):
    """Revalidate redirects in research mode, without the host allowlist.

    A research fetch that redirects off the six allowlisted hosts is normal --
    that is the whole point -- but every hop must still be HTTPS, port 443,
    credential-free and resolve to a globally routable address.
    """

    allow_any_host: ClassVar[bool] = True


class DocRedirectHandler(SafeRedirectHandler):
    """Follow redirects within the documentation hosts, and nowhere else.

    Required rather than optional: documentation sites redirect constantly.
    learn.microsoft.com rewrites a bare path to a locale-prefixed one
    (/windows/... -> /en-us/windows/...) on essentially every request, and
    docs.vulkan.org redirects to a versioned path. Reusing the plain
    SafeRedirectHandler would validate those hops against the six-host network
    allowlist and refuse them, so doc reading would fail on the first hop for
    reasons that look nothing like the cause.
    """

    allow_any_host: ClassVar[bool] = False
    extra_allowed_hosts: ClassVar[frozenset[str]] = ALLOWED_DOC_HOSTS

    @classmethod
    def allowed_extra_hosts(cls) -> frozenset[str]:
        """The documentation hosts plus whatever the operator has granted."""

        return cls.extra_allowed_hosts | granted_hosts()[0]


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects at all.

    Used for the search request, for two reasons:

    * HTTPRedirectHandler rebuilds a 301/302/303 WITHOUT the original `data`,
      silently converting a POST into a GET and dropping the query. The
      search endpoint would then return a generic page, parse to zero
      results, and the tool would tell the model "no results" when it had
      actually been redirected.
    * A search endpoint that redirects is a signal worth surfacing, not
      something to chase.

    Returning None from redirect_request makes urllib raise HTTPError, which
    the backend reports as a blocked/unexpected response.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


HTTP_OPENER = urllib.request.build_opener(SafeRedirectHandler)

# Research mode: any public host, every other check intact.
RESEARCH_HTTP_OPENER = urllib.request.build_opener(ResearchRedirectHandler)

# Default mode: the network allowlist plus the documentation hosts.
DOC_HTTP_OPENER = urllib.request.build_opener(DocRedirectHandler)

# Search: one known endpoint, no redirects.
SEARCH_HTTP_OPENER = urllib.request.build_opener(NoRedirectHandler)
