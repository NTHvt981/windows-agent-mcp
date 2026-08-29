"""Generate an MCP Inspector config so the Inspector passes our env through.

Why this exists at all: the MCP SDK's stdio client spawns a server with a
FIXED environment allowlist, not the parent environment. On Windows that list
is APPDATA, HOMEDRIVE, HOMEPATH, LOCALAPPDATA, PATH,
PROCESSOR_ARCHITECTURE, SYSTEMDRIVE, SYSTEMROOT, TEMP, USERNAME, USERPROFILE
and PROGRAMFILES (see DEFAULT_INHERITED_ENV_VARS in the SDK's client/stdio).
That is a deliberate measure so a client cannot leak its own secrets into every
server it launches.

The consequence for this project is severe and was invisible for a while:
running under the Inspector, `set BIONIC_TOOLS=core,docs` had NO effect,
because the variable never reached the server process. The same was true of
BIONIC_PROJECT_ROOTS and BIONIC_WEB_RESEARCH -- so `--dev --web` quietly gave
you a server with research mode off, and a project root set for a `--dev`
session quietly left writes confined to the download sandbox.

Inspector 2.x has no `-e KEY=VALUE` flag. The supported route is a config file
naming the command and an explicit `env` block, selected with
`--config <path> --server <name>`. Values there ARE passed to the spawned
process, because they were stated rather than inherited.

Building that JSON in batch would mean quoting a Windows path inside JSON
inside a `set` inside cmd. Doing it here instead keeps the launcher readable
and makes the mapping testable.
"""

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

# The name the launcher passes to --server.
SERVER_NAME = "windows-agent-mcp"

# Variables worth forwarding: everything this server reads, plus the two
# Python settings that matter over stdio.
#
# PYTHONIOENCODING is not cosmetic here. stdout IS the JSON-RPC channel, and on
# a Windows console Python would otherwise default to cp1252 and mangle any
# non-ASCII byte in the stream.
FORWARDED_ENV_VARS: tuple[str, ...] = (
    "BIONIC_TOOLS",
    # Without this, `--dev --profile cpp` would silently do nothing -- the same
    # invisible failure BIONIC_TOOLS had before this module existed.
    "BIONIC_PROFILE",
    "BIONIC_PROFILES_FILE",
    "BIONIC_WEB_RESEARCH",
    "BIONIC_SEARCH_BACKEND",
    "BIONIC_PROJECT_ROOTS",
    "BIONIC_DOWNLOAD_ROOT",
    # Host grants. Omitting these would reproduce the original bug in
    # miniature: an operator who granted a host would find it refused under
    # --dev, with nothing to indicate the grant had not been delivered.
    "BIONIC_ALLOWED_HOSTS_FILE",
    "BIONIC_EXTRA_DOC_HOSTS",
    "BIONIC_HOST_CONSENT",
    "BIONIC_HOST_GRANT_PERSIST",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
)


def build_config(
    python: str,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build the Inspector config that launches this server.

    Only variables that are actually set are included. An empty `env` block is
    fine, and is better than emitting empty strings -- `BIONIC_WEB_RESEARCH=""`
    is falsy today, but writing a variable the operator never set invites a
    future truthiness change to turn it on by accident.

    Args:
        python: Interpreter to launch, normally the venv's python.exe.
        environment: Source of variables. Defaults to os.environ.

    Returns:
        The config structure, ready to serialise.
    """

    source = os.environ if environment is None else environment

    forwarded = {
        name: source[name]
        for name in FORWARDED_ENV_VARS
        if source.get(name, "").strip()
    }

    return {
        "mcpServers": {
            SERVER_NAME: {
                # Forward slashes: this lands in JSON, where a Windows
                # backslash would need escaping and is a reliable source of
                # "unexpected token" failures.
                "command": python.replace("\\", "/"),
                "args": ["-m", "windows_agent_mcp"],
                "env": forwarded,
            }
        }
    }


def main() -> int:
    """Write the config file named by argv[1], launching argv[2].

    Returns:
        0 on success, 1 on a usage error or an unwritable destination.
    """

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
