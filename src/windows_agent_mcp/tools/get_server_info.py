from __future__ import annotations

import json
import os
from pathlib import Path

from ..consent import CONSENT_ENV_VAR, consent_available
from ..hostgrants import (
    EXTRA_HOSTS_ENV_VAR,
    PERSIST_ENV_VAR,
    find_grants_file,
    granted_hosts,
    persist_enabled,
    session_grants,
)
from ..utils import (
    ALLOWED_DOC_HOSTS,
    LAUNCH_CWD_ENV_VAR,
    MAX_DOWNLOAD_BYTES,
    MAX_HTTP_BYTES,
    PROFILE_ENV_VAR,
    PROJECT_ROOTS_ENV_VAR,
    SEARCH_BACKEND_ENV_VAR,
    SERVER_VERSION,
    TOOL_GROUPS_ENV_VAR,
    VALID_TOOL_GROUPS,
    WEB_RESEARCH_ENV_VAR,
    WORKSPACE_FROM_CWD_ENV_VAR,
    active_tool_groups,
    get_allowed_working_directories,
    get_download_root,
    web_research_enabled,
    web_search_enabled,
    workspace_from_cwd,
)

__all__: list[str] = ["get_server_info"]


def _consent_summary() -> str:
    """Whether, and how, the operator can be asked."""

    if not consent_available():
        return f"off ({CONSENT_ENV_VAR}=0); grants file only"

    if persist_enabled():
        return (
            "may ask the user (client must support MCP elicitation); "
            f"an approval can be remembered ({PERSIST_ENV_VAR}=1)"
        )

    return (
        "may ask the user (client must support MCP elicitation); "
        "an approval lasts until restart"
    )


def get_server_info() -> str:
    """Return information about this MCP server."""

    research = web_research_enabled()

    granted, grants_error = granted_hosts()

    search_available = web_search_enabled()

    if research:
        network_policy = (
            "HTTPS + SSRF checks; strict host allowlist for download_file and "
            "fetch_https_response; any public host for fetch_web_page"
        )
    else:
        network_policy = (
            "HTTPS + SSRF checks; strict host allowlist for download_file and "
            "fetch_https_response; fetch_web_page reads the documentation "
            "hosts, any granted host, and that same download allowlist"
        )

    writable = get_allowed_working_directories()

    server_cwd = str(Path.cwd())
    launch_cwd = os.environ.get(LAUNCH_CWD_ENV_VAR, "") or None
    adopted_workspace, workspace_note = workspace_from_cwd()

    groups, groups_error = active_tool_groups()

    # Local import avoids a cycle through main.
    from ..config import find_config_file, load_config, resolve_profile
    from ..main import TOOL_GROUPS, get_tools

    # get_tools() order steers which tool the model reaches for.
    registered = get_tools(groups=groups)

    config_file = find_config_file()

    config, config_errors = load_config(config_file)

    config_error: str | None = config_errors[0] if config_errors else None

    active_profile = (
        os.environ.get(PROFILE_ENV_VAR, "").strip() or config.active_profile
    )

    if active_profile and config_error is None:
        _profile, profile_error = resolve_profile(active_profile, config.profiles)

        if profile_error is not None:
            config_error = profile_error

    return json.dumps(
        {
            "name": "Windows Agent MCP",
            "version": SERVER_VERSION,
            "transport": "stdio",
            "tool_groups_env_var": TOOL_GROUPS_ENV_VAR,
            "active_tool_groups": sorted(groups),
            "available_tool_groups": sorted(VALID_TOOL_GROUPS),
            "registered_tool_count": len(registered),
            "registered_tools": [tool.__name__ for tool in registered],
            "tools_by_group": {
                group: [tool.__name__ for tool in TOOL_GROUPS[group]]
                for group in sorted(TOOL_GROUPS)
            },
            "tool_groups_error": groups_error,
            "config_file": str(config_file),
            "config_error": config_error,
            "active_profile": active_profile or None,
            "download_root": str(get_download_root()),
            "network_policy": network_policy,
            "max_download_mb": MAX_DOWNLOAD_BYTES // (1024 * 1024),
            "max_http_mb": MAX_HTTP_BYTES // (1024 * 1024),
            "writable_roots": [str(root) for root in writable],
            "project_roots_env_var": PROJECT_ROOTS_ENV_VAR,
            "project_roots_configured": bool(
                os.environ.get(PROJECT_ROOTS_ENV_VAR, "").strip()
            ),
            "server_cwd": server_cwd,
            "launch_cwd": launch_cwd,
            "workspace_from_cwd_env_var": WORKSPACE_FROM_CWD_ENV_VAR,
            "adopted_workspace": (
                str(adopted_workspace) if adopted_workspace is not None else None
            ),
            "workspace_note": workspace_note,
            "web_research": research,
            "web_research_env_var": WEB_RESEARCH_ENV_VAR,
            "documentation_hosts": sorted(ALLOWED_DOC_HOSTS),
            "granted_hosts": sorted(granted),
            "granted_hosts_file": str(find_grants_file()),
            "granted_hosts_env_var": EXTRA_HOSTS_ENV_VAR,
            "granted_hosts_error": grants_error,
            "session_granted_hosts": sorted(session_grants()),
            "host_consent": _consent_summary(),
            "web_search": search_available,
            "search_backend": (
                os.environ.get(SEARCH_BACKEND_ENV_VAR, "duckduckgo")
                if search_available
                else None
            ),
        },
        indent=2,
    )
