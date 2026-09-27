from __future__ import annotations

import re
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from ..error import mcp_error
from ..log import log
from ..utils import (
    HTTP_OPENER,
    MAX_DOWNLOAD_BYTES,
    MAX_FILENAME_LENGTH,
    get_download_root,
    validate_url,
)

__all__: list[str] = ["download_file"]


# Opening these resolves to a device, wherever they appear.
RESERVED_DEVICE_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')

_PLAUSIBLE_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,10}\Z")


def _truncate_preserving_extension(filename: str, limit: int) -> str:
    """Shorten a filename to `limit` characters, keeping its extension."""

    if len(filename) <= limit:
        return filename

    stem = filename
    suffixes: list[str] = []

    for _ in range(2):
        match = _PLAUSIBLE_SUFFIX.search(stem)

        if match is None:
            break

        suffixes.insert(0, match.group())
        stem = stem[: match.start()]

    extension = "".join(suffixes)

    if not extension or len(extension) >= limit:
        return filename[:limit]

    return stem[: limit - len(extension)] + extension


def sanitize_filename(filename: str) -> str:
    """Convert a caller-supplied filename into a safe, simple filename."""

    filename = filename.strip()

    if not filename:
        raise ValueError("Filename cannot be empty.")

    # Both separators are treated the same.
    filename = filename.replace("\\", "/").split("/")[-1]

    filename = _INVALID_FILENAME_CHARS.sub("_", filename)

    # Windows discards trailing dots and spaces.
    filename = filename.rstrip(". ")

    # . and .. reduce to empty, so no separate traversal check.
    if not filename:
        raise ValueError(
            "Filename consists only of dots and spaces, leaving nothing usable."
        )

    # A device name stays reserved with an extension.
    stem = filename.split(".", 1)[0]

    if stem.upper() in RESERVED_DEVICE_NAMES:
        filename = "_" + filename

    return _truncate_preserving_extension(filename, MAX_FILENAME_LENGTH)


def safe_download_path(filename: str) -> Path:
    """Create a path strictly inside the download directory."""

    root = get_download_root()

    safe_name = sanitize_filename(filename)

    destination = (root / safe_name).resolve()

    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise ValueError("Destination escapes download directory.") from exc

    return destination


def download_file(
    url: str,
    filename: str | None = None,
) -> str:
    """Download a file from an approved HTTPS domain."""

    # ValueError here is policy or filename only, kept separate from the request.
    try:
        validate_url(url)

        parsed = urlparse(url)

        if filename:
            safe_name = sanitize_filename(filename)
        else:
            candidate = Path(parsed.path).name
            if not candidate:
                candidate = "download.bin"
            safe_name = sanitize_filename(candidate)

        destination = safe_download_path(safe_name)
    except ValueError as exc:
        return mcp_error(
            "DOWNLOAD_NOT_ALLOWED",
            "download_file",
            str(exc),
            path=url,
            recovery=[
                "DO NOT retry the identical request.",
                "Only HTTPS URLs on the host allowlist are permitted, and "
                "files are always written inside the download root.",
            ],
        )

    try:
        temporary = destination.with_suffix(destination.suffix + ".part")

        if destination.exists():
            return mcp_error(
                "DESTINATION_EXISTS",
                "download_file",
                f"A file already exists at {destination} and will not be overwritten.",
                path=str(destination),
                recovery=[
                    "Do not retry the identical download.",
                    "The file is already present -- use it as it is.",
                    "Pass a different filename if a fresh copy is required.",
                ],
            )

        temporary.unlink(missing_ok=True)

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "windows-agent-mcp/1.0",
            },
            method="GET",
        )

        total = 0

        with HTTP_OPENER.open(
            request,
            timeout=60,
        ) as response:
            content_length = response.headers.get("Content-Length")

            if content_length:
                try:
                    declared_size = int(content_length)

                    if declared_size > MAX_DOWNLOAD_BYTES:
                        return mcp_error(
                            "DOWNLOAD_TOO_LARGE",
                            "download_file",
                            f"The remote file is {declared_size:,} bytes, "
                            f"over the {MAX_DOWNLOAD_BYTES:,} byte limit.",
                            path=url,
                            recovery=[
                                "Do not retry the identical download.",
                                "Ask the user to fetch this file manually.",
                            ],
                        )

                except ValueError:
                    pass

            with open(temporary, "wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)

                    if not chunk:
                        break

                    total += len(chunk)

                    if total > MAX_DOWNLOAD_BYTES:
                        output.close()

                        temporary.unlink(missing_ok=True)

                        return mcp_error(
                            "DOWNLOAD_TOO_LARGE",
                            "download_file",
                            f"The download exceeded the "
                            f"{MAX_DOWNLOAD_BYTES:,} byte limit and was "
                            f"aborted. The partial file was removed.",
                            path=url,
                            recovery=[
                                "Do not retry the identical download.",
                                "Ask the user to fetch this file manually.",
                            ],
                        )

                    output.write(chunk)

        temporary.replace(destination)

        log.info(
            "Downloaded %s -> %s (%d bytes)",
            url,
            destination,
            total,
        )

        return (
            f"DOWNLOAD SUCCESS\nURL: {url}\nFile: {destination}\nSize: {total:,} bytes"
        )

    except Exception as exc:
        log.exception("download_file failed")

        return mcp_error(
            "DOWNLOAD_FAILED",
            "download_file",
            f"{type(exc).__name__}: {exc}",
            path=url,
            recovery=[
                "Do not repeatedly retry the identical download.",
                "Verify the URL is correct and the host is reachable.",
            ],
        )
