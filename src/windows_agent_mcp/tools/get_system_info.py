from __future__ import annotations

import json
import os
import platform

__all__: list[str] = ["get_system_info"]

# Windows 11 still reports 10.0.x; the build number separates them.
_WINDOWS_11_MIN_BUILD = 22000


def _windows_build_number(version: str) -> int | None:
    """Trailing build number of a `10.0.26100`-style version string."""

    try:
        return int(version.rsplit(".", 1)[-1])
    except (AttributeError, ValueError):
        return None


def _os_display_name() -> str:
    """OS and release joined into one answer, such as `Windows 11`."""

    system = platform.system()
    release = platform.release()

    if system == "Windows" and release == "10":
        # platform.release() misreports Windows 11 before Python 3.12.
        build = _windows_build_number(platform.version())

        if build is not None and build >= _WINDOWS_11_MIN_BUILD:
            release = "11"

    if system and release:
        return f"{system} {release}"

    return system or release or "unknown"


def get_system_info() -> str:
    """Return information about the system this server is running on."""

    return json.dumps(
        {
            "os": _os_display_name(),
            "os_build": platform.version(),
            "machine": platform.machine(),
            "architecture": platform.architecture(),
            "python_version": platform.python_version(),
            "username": os.environ.get("USERNAME", "unknown"),
            "home": os.path.expanduser("~"),
            "cwd": os.getcwd(),
        },
        indent=2,
    )
