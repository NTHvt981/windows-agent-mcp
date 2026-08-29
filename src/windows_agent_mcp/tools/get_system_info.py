"""Get system info tool for Windows Agent MCP Server."""

from __future__ import annotations

import json
import os
import platform

__all__: list[str] = ["get_system_info"]


def get_system_info() -> str:
    """Return information about the system this server is running on.

    Args:
        None

    Returns:
        JSON string containing system information including OS, architecture, and paths.

    Example:
        >>> get_system_info()
        '{"os": "Windows", "architecture": "AMD64", "python_version": "3.12.0", ...}',
    """

    return json.dumps(
        {
            "os": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "architecture": platform.architecture(),
            "python_version": platform.python_version(),
            "username": os.environ.get("USERNAME", "unknown"),
            "home": os.path.expanduser("~"),
            "cwd": os.getcwd(),
        },
        indent=2,
    )
