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

SERVER_VERSION: str = "1.0.0"

MAX_HTTP_BYTES: int = 2 * 1024 * 1024  # 2 MB

MAX_DOWNLOAD_BYTES: int = 500 * 1024 * 1024  # 500 MB

# Well under the NTFS 255-char component limit, leaving room for the .part suffix.
MAX_FILENAME_LENGTH: int = 180

HTTP_TIMEOUT_SECONDS: int = 20

POWERSHELL_TIMEOUT_SECONDS: int = 300

BUILD_TIMEOUT_SECONDS: int = 600

SHADER_TIMEOUT_SECONDS: int = 60

DEFAULT_READ_LINES: int = 2000

MAX_READ_BYTES: int = 256 * 1024  # 256 KB

MAX_DIRECTORY_ENTRIES: int = 1000

MAX_EMPTY_DIR_RESULTS: int = 1000

MAX_SEARCH_RESULTS: int = 5

# Search result text is attacker-controlled.
MAX_SEARCH_FIELD_CHARS: int = 300

MAX_PAGE_BYTES: int = 2 * 1024 * 1024  # 2 MB

MAX_PAGE_LINKS: int = 20

MAX_WRITE_BYTES: int = 2 * 1024 * 1024  # 2 MB

MAX_SEARCH_MATCHES: int = 100

MAX_GREP_FILE_BYTES: int = 1024 * 1024  # 1 MB

MAX_FILES_SCANNED: int = 20_000
MAX_FIND_RESULTS: int = 500

# build/out stay skipped: a false skip recovers via read_file, a full scan does not.
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

TOOL_GROUPS_ENV_VAR: str = "WAMCP_TOOLS"

# active_tool_groups ignores profiles: importing profiles here would cycle.
PROFILE_ENV_VAR: str = "WAMCP_PROFILE"

# Mapping lives in main; utils cannot import main.
VALID_TOOL_GROUPS: frozenset[str] = frozenset(
    {
        "core",
        "edit",
        "build",
        "docs",
        "net",
        "search",
        "research",
    }
)

DEFAULT_TOOL_GROUPS: frozenset[str] = VALID_TOOL_GROUPS - {
    "research",
    "search",
}

ALWAYS_ON_TOOL_GROUPS: frozenset[str] = frozenset({"core"})

_ALL_GROUPS_KEYWORD = "all"

WEB_RESEARCH_ENV_VAR: str = "WAMCP_WEB_RESEARCH"

SEARCH_BACKEND_ENV_VAR: str = "WAMCP_SEARCH_BACKEND"

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


def parse_tool_groups(value: str | None) -> tuple[frozenset[str], str | None]:
    """Parse a WAMCP_TOOLS value into group names."""

    if value is None or not value.strip():
        return DEFAULT_TOOL_GROUPS, None

    requested = [part.strip().lower() for part in value.split(",")]
    requested = [part for part in requested if part]

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
    """Resolve the groups to register from the environment."""

    groups, error = parse_tool_groups(os.environ.get(TOOL_GROUPS_ENV_VAR))

    if os.environ.get(WEB_RESEARCH_ENV_VAR, "").strip().lower() in _TRUTHY_VALUES:
        groups = groups | {"research"}

    return groups, error


def web_search_enabled() -> bool:
    """Return True when web_search is available."""

    return not active_tool_groups()[0].isdisjoint({"search", "research"})


def web_research_enabled() -> bool:
    """Return True when research mode is on."""

    return "research" in active_tool_groups()[0]


# DuckDuckGo rejects non-browser User-Agents with a 202 page.
BROWSER_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def default_download_root() -> Path:
    """Return a per-machine default download directory."""

    local_app_data = os.environ.get("LOCALAPPDATA")

    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path.home() / ".local" / "share"

    return base / "windows-agent-mcp" / "downloads"


def get_download_root() -> Path:
    """Return the configured download directory, creating it if needed."""

    configured = os.environ.get("WAMCP_DOWNLOAD_ROOT")

    if configured:
        root = Path(configured)
    else:
        root = default_download_root()

    root = root.expanduser().resolve()

    root.mkdir(parents=True, exist_ok=True)

    return root


PROJECT_ROOTS_ENV_VAR: str = "WAMCP_PROJECT_ROOTS"

WORKSPACE_FROM_CWD_ENV_VAR: str = "WAMCP_WORKSPACE_FROM_CWD"

# Captured before run_server.bat pushd's to its own directory.
LAUNCH_CWD_ENV_VAR: str = "WAMCP_LAUNCH_CWD"

# Defined here: profiles imports this module.
# Matched by name: path checks miss a file created where none existed.
PROTECTED_CONFIG_FILENAMES: frozenset[str] = frozenset(
    {
        DEFAULT_GRANTS_FILENAME,
    }
)

# Two-component suffix leaves ordinary config.json files alone.
PROTECTED_PATH_SUFFIX: tuple[str, ...] = ("input", "config.json")


class ProtectedPathError(ValueError):
    """A write was refused because the path names server configuration."""


def _unsafe_workspace_reason(path: Path) -> str | None:
    """Why `path` must not be auto-adopted as a writable workspace, else None."""

    if path == path.parent:
        return "it is a filesystem or drive root"

    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None

    if home is not None and home.is_relative_to(path):
        return "it is the home directory or an ancestor of it"

    for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)"):
        raw = os.environ.get(var)

        if not raw:
            continue

        try:
            system_dir = Path(raw).expanduser().resolve()
        except OSError:
            continue

        if system_dir.is_relative_to(path):
            return f"it is or contains a system directory ({system_dir})"

    return None


def workspace_from_cwd() -> tuple[Path | None, str | None]:
    """The current directory as an automatic writable root, if enabled and safe."""

    enabled = (
        os.environ.get(WORKSPACE_FROM_CWD_ENV_VAR, "").strip().lower() in _TRUTHY_VALUES
    )

    if not enabled:
        return None, None

    # Path.cwd() is the install dir after run_server.bat pushd's; prefer the launcher value.
    launch_cwd = os.environ.get(LAUNCH_CWD_ENV_VAR, "").strip()

    try:
        if launch_cwd:
            cwd = Path(launch_cwd).expanduser().resolve()
        else:
            cwd = Path.cwd().resolve()
    except OSError as exc:
        return None, f"could not resolve the workspace directory: {exc}"

    reason = _unsafe_workspace_reason(cwd)

    if reason is not None:
        return None, (
            f"refused {cwd}: {reason}. Set {PROJECT_ROOTS_ENV_VAR} explicitly."
        )

    return cwd, None


def get_allowed_working_directories() -> list[Path]:
    """Return the roots that run_powershell may execute inside."""

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

    workspace, _ = workspace_from_cwd()

    if workspace is not None and workspace.is_dir() and workspace not in roots:
        roots.append(workspace)

    return roots


def resolve_working_directory(requested: str | None) -> Path:
    """Validate a requested working directory against the allowed roots."""

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


def resolve_write_path(requested: str) -> Path:
    """Validate a path the caller wants to WRITE, against the allowed roots."""

    if not requested or not requested.strip():
        raise ValueError("Path cannot be empty.")

    allowed = get_allowed_working_directories()

    candidate = Path(requested).expanduser()

    try:
        # resolve() follows a symlinked parent here, before the containment check.
        resolved = candidate.resolve()
    except OSError as exc:
        raise ValueError(f"Could not resolve path '{requested}': {exc}") from exc

    if resolved.is_dir():
        raise ValueError(f"Path is an existing directory, not a file: {resolved}")

    # resolve() expands Windows 8.3 short names back to the real one.
    for name in {candidate.name.lower(), resolved.name.lower()}:
        if name in {protected.lower() for protected in PROTECTED_CONFIG_FILENAMES}:
            raise ProtectedPathError(
                f"'{name}' is server configuration and cannot be written by a "
                f"tool. It decides which hosts may be read, so only the "
                f"operator may change it. Ask the user to run: "
                f"python -m windows_agent_mcp.hostgrants --add HOST"
            )

    for parts in (
        {p.lower() for p in candidate.parts[-2:]},
        {p.lower() for p in resolved.parts[-2:]},
    ):
        if parts == {part.lower() for part in PROTECTED_PATH_SUFFIX}:
            raise ProtectedPathError(
                f"'{'/'.join(PROTECTED_PATH_SUFFIX)}' is the server's own "
                f"settings file and cannot be written by a tool. It decides "
                f"which directories are writable and which tools load, so only "
                f"the operator may change it. Ask the user to edit it."
            )

    for root in allowed:
        if root in resolved.parents:
            return resolved

    raise ValueError(
        f"Path '{resolved}' is outside every writable root. "
        f"Writable roots: {[str(r) for r in allowed]}. "
        f"Set {PROJECT_ROOTS_ENV_VAR} to permit writing elsewhere."
    )


def is_private_or_special_ip(address: str) -> bool:
    """Reject IPs that must never be reached."""

    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True

    # is_global covers CGNAT where is_private changed across 3.12/3.13.
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or not ip.is_global
    )


ALLOWED_NETWORK_HOSTS: set[str] = {
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "premake.github.io",
}

ALLOWED_DOC_HOSTS: frozenset[str] = frozenset(
    {
        "registry.khronos.org",
        "docs.vulkan.org",
        "www.khronos.org",
        "khronos.org",
        "vulkan.lunarg.com",
        "learn.microsoft.com",
        "en.cppreference.com",
        "www.cppreference.com",
        "isocpp.org",
        "cmake.org",
        "ninja-build.org",
        "gpuopen.com",
        "developer.nvidia.com",
    }
)


def readable_web_hosts() -> tuple[frozenset[str], str | None]:
    """Return documentation hosts plus operator grants."""

    granted, error = granted_hosts()

    return ALLOWED_DOC_HOSTS | granted, error


def host_grant_would_help(url: str) -> str | None:
    """Return the hostname if only the allowlist blocks the URL."""

    # No DNS lookup: allowlist runs before resolve.
    try:
        hostname = validate_url_shape(url)
    except ValueError:
        return None

    if web_research_enabled():
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
    """Check whether hostname is explicitly allowed."""

    hostname = hostname.lower().rstrip(".")

    return hostname in ALLOWED_NETWORK_HOSTS or hostname in extra


def resolve_and_validate_host(
    hostname: str,
    *,
    allow_any_host: bool = False,
    extra_allowed_hosts: frozenset[str] = frozenset(),
) -> None:
    """Resolve hostname and reject private/special addresses."""

    # DNS resolves again at connect, so a hostile resolver can race the two.

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


def validate_url(
    url: str,
    *,
    allow_any_host: bool = False,
    extra_allowed_hosts: frozenset[str] = frozenset(),
) -> str:
    """Validate a URL before any network operation."""

    hostname = validate_url_shape(url)

    resolve_and_validate_host(
        hostname,
        allow_any_host=allow_any_host,
        extra_allowed_hosts=extra_allowed_hosts,
    )

    return url


def validate_url_shape(url: str) -> str:
    """Check everything about a URL that does not require DNS."""

    if len(url) > 4096:
        raise ValueError("URL is too long.")

    parsed = urlparse(url)

    if parsed.scheme.lower() != "https":
        raise ValueError("Only HTTPS URLs are allowed.")

    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed.")

    # Validated against port 443; urlparse reports ":0" as 0, not None.
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


class RedirectNotAllowedError(ValueError):
    """A redirect hop was refused by policy rather than by the network."""

    def __init__(self, url: str, reason: str):
        super().__init__(f"Redirect to {url} was refused: {reason}")

        self.url = url
        self.reason = reason
        self.hostname = urlparse(url).hostname or ""


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect."""

    # build_opener instantiates handler classes with zero arguments.
    allow_any_host: ClassVar[bool] = False

    extra_allowed_hosts: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def allowed_extra_hosts(cls) -> frozenset[str]:
        """Hosts permitted for a redirect hop, evaluated per hop."""

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
        try:
            validate_url(
                newurl,
                allow_any_host=self.allow_any_host,
                extra_allowed_hosts=self.allowed_extra_hosts(),
            )
        except ValueError as exc:
            raise RedirectNotAllowedError(newurl, str(exc)) from exc

        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ResearchRedirectHandler(SafeRedirectHandler):
    allow_any_host: ClassVar[bool] = True


class DocRedirectHandler(SafeRedirectHandler):
    allow_any_host: ClassVar[bool] = False
    extra_allowed_hosts: ClassVar[frozenset[str]] = ALLOWED_DOC_HOSTS

    @classmethod
    def allowed_extra_hosts(cls) -> frozenset[str]:
        return cls.extra_allowed_hosts | granted_hosts()[0]


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
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

RESEARCH_HTTP_OPENER = urllib.request.build_opener(ResearchRedirectHandler)

DOC_HTTP_OPENER = urllib.request.build_opener(DocRedirectHandler)

SEARCH_HTTP_OPENER = urllib.request.build_opener(NoRedirectHandler)
