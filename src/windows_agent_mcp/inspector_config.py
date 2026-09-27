"""Generate an Inspector config so the Inspector passes env through."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

__all__: list[str] = [
    "FORWARDED_ENV_VARS",
    "SERVER_NAME",
    "build_config",
    "main",
]

SERVER_NAME = "windows-agent-mcp"

# stdout is the JSON-RPC channel; cp1252 would mangle it.
FORWARDED_ENV_VARS: tuple[str, ...] = (
    "WAMCP_TOOLS",
    "WAMCP_PROFILE",
    "WAMCP_CONFIG_FILE",
    "WAMCP_WEB_RESEARCH",
    "WAMCP_SEARCH_BACKEND",
    "WAMCP_PROJECT_ROOTS",
    "WAMCP_WORKSPACE_FROM_CWD",
    "WAMCP_LAUNCH_CWD",
    "WAMCP_DOWNLOAD_ROOT",
    "WAMCP_ALLOWED_HOSTS_FILE",
    "WAMCP_EXTRA_DOC_HOSTS",
    "WAMCP_HOST_CONSENT",
    "WAMCP_HOST_GRANT_PERSIST",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
)


def build_config(
    python: str,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build the Inspector config that launches this server."""

    source = os.environ if environment is None else environment

    forwarded = {
        name: source[name]
        for name in FORWARDED_ENV_VARS
        if source.get(name, "").strip()
    }

    return {
        "mcpServers": {
            SERVER_NAME: {
                # Forward slashes: backslashes break JSON.
                "command": python.replace("\\", "/"),
                "args": ["-m", "windows_agent_mcp"],
                "env": forwarded,
            }
        }
    }


def main() -> int:
    """Write the config file named by argv[1], launching argv[2]."""

    if len(sys.argv) < 3:
        print(
            "usage: python -m windows_agent_mcp.inspector_config "
            "<config-path> <python-executable>",
            file=sys.stderr,
        )
        return 1

    destination = Path(sys.argv[1])

    config = build_config(sys.argv[2])

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"could not write {destination}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
