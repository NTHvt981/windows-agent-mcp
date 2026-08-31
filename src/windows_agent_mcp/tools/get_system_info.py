"""Get system info tool for Windows Agent MCP Server."""

from __future__ import annotations

import json
import os
import platform

__all__: list[str] = ["get_system_info"]

# Windows 11 starts here. Microsoft never bumped the NT major version, so every
# Windows 11 build still reports itself as 10.0.x and the build number is the
# only thing separating the two.
_WINDOWS_11_MIN_BUILD = 22000


def _windows_build_number(version: str) -> int | None:
    """Trailing build number of a `10.0.26100`-style version string."""

    try:
        return int(version.rsplit(".", 1)[-1])
    except (AttributeError, ValueError):
        return None


def _os_display_name() -> str:
    """OS and release joined into one answer, such as `Windows 11`.

    Two separate faults make this worth doing here rather than emitting the raw
    fields and leaving the caller to assemble them.

    `platform.release()` is WRONG on Windows 11 before Python 3.12: it returns
    "10". Measured on 3.10.20 and 3.11.15 against build 26100, correct from
    3.12.13. `requires-python` is >=3.10, so the server can run on an
    interpreter that misreports the OS, and the build number is the only way to
    tell. The correction is guarded on release == "10" so it cannot fire on a
    version that was already right.

    Separately, a model asked for the OS *version* reaches for
    `platform.version()`, which reads "10.0.26100" on Windows 11 -- it takes the
    leading 10 and answers "Windows 10", contradicting the release field beside
    it. Observed on a 9B model. Joining the halves here leaves nothing to
    assemble wrongly, and `os_build` no longer looks like the field to quote.

    Known limit: Windows Server 2025 is build 26100, so on Python 3.10 or 3.11 --
    where `release()` returns "10" there too -- it would be named Windows 11.
    That is not a regression; it is already reported as Windows 10 today.
    """

    system = platform.system()
    release = platform.release()

    if system == "Windows" and release == "10":
        build = _windows_build_number(platform.version())

        if build is not None and build >= _WINDOWS_11_MIN_BUILD:
            release = "11"

    if system and release:
        return f"{system} {release}"

    return system or release or "unknown"


def get_system_info() -> str:
    """Return information about the system this server is running on.

    Args:
        None

    Returns:
        JSON string containing system information including OS, architecture, and paths.

    Example:
        >>> get_system_info()
        '{"os": "Windows 11", "os_build": "10.0.26100", "machine": "AMD64", ...}',
    """

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
