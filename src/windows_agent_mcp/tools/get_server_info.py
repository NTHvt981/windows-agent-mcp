"""Get server info tool for Windows Agent MCP Server."""

from __future__ import annotations

import json
import os

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
    MAX_DOWNLOAD_BYTES,
    MAX_HTTP_BYTES,
    PROFILE_ENV_VAR,
    PROJECT_ROOTS_ENV_VAR,
    SEARCH_BACKEND_ENV_VAR,
    SERVER_VERSION,
    TOOL_GROUPS_ENV_VAR,
    VALID_TOOL_GROUPS,
    WEB_RESEARCH_ENV_VAR,
    active_tool_groups,
    get_allowed_working_directories,
    get_download_root,
    web_research_enabled,
    web_search_enabled,
)

__all__: list[str] = ["get_server_info"]


def _consent_summary() -> str:
    """One field describing whether, and how, the operator can be asked.

    A sentence rather than three booleans: the model's only useful decision
    here is whether to say "I can ask you" or "please run this command", and
    that is one bit plus the reason.
    """

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
    """Return information about this MCP server.

    Returns server metadata including version, transport protocol, and
    configuration -- including whether the web research tools are available,
    which is the only way to tell a disabled tool from a missing one.

    Args:
        None

    Returns:
        JSON string containing server information.

    Example:
        >>> get_server_info()
        '{"name": "Windows Agent MCP", "version": "1.0.0", ...}',
    """

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

    groups, groups_error = active_tool_groups()

    # Imported lazily: main imports every tool module, so importing it at module
    # scope here would be a cycle (main -> get_server_info -> main). config
    # imports main, so it has the same constraint.
    from ..config import find_config_file, load_config, resolve_profile
    from ..main import TOOL_GROUPS, get_tools

    # Kept in get_tools() order, not sorted: that order is the workflow
    # ordering declared in TOOLS, and it steers which tool a small model
    # reaches for first.
    registered = get_tools(groups=groups)

    config_file = find_config_file()

    # Recomputed rather than cached from startup: no module state to go stale,
    # and the cost is one small file read on a call that already shells out to
    # nothing.
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
            # The first thing to check when a tool is missing: it may simply
            # not be in an active group. Reported before anything else because
            # "why can I not see compile_shader" has to be answerable in one
            # call, without the operator reading any documentation.
            "tool_groups_env_var": TOOL_GROUPS_ENV_VAR,
            "active_tool_groups": sorted(groups),
            "available_tool_groups": sorted(VALID_TOOL_GROUPS),
            "registered_tool_count": len(registered),
            # The names, not just the count. Without them a model asked
            # which tools it has answers from whatever its CLIENT also
            # offers: measured, a 9B model attributed its client's shell,
            # git and file-writing tools to this server's read-only core
            # group, and invented a membership for docs.
            "registered_tools": [tool.__name__ for tool in registered],
            # Every group's membership, inactive ones included, so that
            # "why can I not see compile_shader" is answered by reading
            # rather than by guessing from the group name.
            "tools_by_group": {
                group: [tool.__name__ for tool in TOOL_GROUPS[group]]
                for group in sorted(TOOL_GROUPS)
            },
            "tool_groups_error": groups_error,
            # The config file is where every setting now lives, so a problem
            # with it is the likeliest cause of "my tool is missing" or "my
            # project root is not set" -- and it has to be visible here rather
            # than only in the startup log.
            "config_file": str(config_file),
            "config_error": config_error,
            "active_profile": active_profile or None,
            "download_root": str(get_download_root()),
            "network_policy": network_policy,
            "max_download_mb": MAX_DOWNLOAD_BYTES // (1024 * 1024),
            "max_http_mb": MAX_HTTP_BYTES // (1024 * 1024),
            # The single most common cause of a refused write or build: the
            # operator never set WAMCP_PROJECT_ROOTS, so the only writable
            # place is the download sandbox. Reporting it turns "permission
            # denied" into something the model can explain to the user.
            "writable_roots": [str(root) for root in writable],
            "project_roots_env_var": PROJECT_ROOTS_ENV_VAR,
            "project_roots_configured": len(writable) > 1,
            "web_research": research,
            "web_research_env_var": WEB_RESEARCH_ENV_VAR,
            "documentation_hosts": sorted(ALLOWED_DOC_HOSTS),
            # The answer to "the user said I could read that site, why can I
            # still not read it": either the grant is not here, or the file has
            # an error. Both are visible in one call.
            #
            # Kept to a handful of keys on purpose. Everything here is
            # re-serialised into the model's context whenever it asks, so a
            # field that merely restates a constant earns nothing -- the
            # remaining env var names are in README.md.
            "granted_hosts": sorted(granted),
            "granted_hosts_file": str(find_grants_file()),
            "granted_hosts_env_var": EXTRA_HOSTS_ENV_VAR,
            "granted_hosts_error": grants_error,
            # Session grants vanish on restart, so a host that worked ten
            # minutes ago and does not now is not a mystery.
            "session_granted_hosts": sorted(session_grants()),
            "host_consent": _consent_summary(),
            # Reported whenever search is available, which is the `search`
            # group as well as `research` -- gating this on research alone
            # would report None on a server where web_search works.
            "web_search": search_available,
            "search_backend": (
                os.environ.get(SEARCH_BACKEND_ENV_VAR, "duckduckgo")
                if search_available
                else None
            ),
        },
        indent=2,
    )
