"""Download file tool for Windows Agent MCP Server."""

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


# Windows device names. Opening any of these resolves to a device rather than
# a file, wherever it appears in the tree.
#
# The full set matters: COM5-COM9 and LPT4-LPT9 were previously missing, and
# CONIN$/CONOUT$ are reserved too.
RESERVED_DEVICE_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)

# Characters Windows forbids in a filename, plus the C0 control range.
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')

# A suffix worth preserving when a long name has to be shortened. Deliberately
# narrow so an absurd trailing ".aaaa...." is not mistaken for an extension.
_PLAUSIBLE_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,10}\Z")


def _truncate_preserving_extension(filename: str, limit: int) -> str:
    """Shorten a filename to `limit` characters, keeping its extension.

    Truncating from the end destroys the extension, which on Windows decides
    how the file opens -- a shortened "premake.tar.gz" must not become
    "premakexxxx" with no type at all.

    Up to two trailing suffixes are preserved, so compound extensions such as
    ".tar.gz" survive.

    Args:
        filename: Name to shorten. Assumed already sanitized.
        limit: Maximum length of the result.

    Returns:
        A name of at most `limit` characters, ending in the original
        extension whenever one can be identified and still fits.
    """

    if len(filename) <= limit:
        return filename

    stem = filename
    suffixes: list[str] = []

    # Collect at most two plausible suffixes, innermost last.
    for _ in range(2):
        match = _PLAUSIBLE_SUFFIX.search(stem)

        if match is None:
            break

        suffixes.insert(0, match.group())
        stem = stem[: match.start()]

    extension = "".join(suffixes)

    # A stem of at least one character must remain, otherwise the extension is
    # not worth keeping and a hard cut is the only option.
    if not extension or len(extension) >= limit:
        return filename[:limit]

    return stem[: limit - len(extension)] + extension


def sanitize_filename(filename: str) -> str:
    """Convert a caller-supplied filename into a safe, simple filename.

    Path components, traversal, Windows-invalid characters, device names and
    trailing dots are all handled. The result is a bare filename that is safe
    to join onto the download root.

    Args:
        filename: Original filename to sanitize.

    Returns:
        A safe filename of at most MAX_FILENAME_LENGTH characters.

    Raises:
        ValueError: If nothing usable remains after sanitizing.

    Example:
        >>> sanitize_filename("./../malicious.txt")
        'malicious.txt'
        >>> sanitize_filename("NUL.txt")
        '_NUL.txt'
    """

    filename = filename.strip()

    if not filename:
        raise ValueError("Filename cannot be empty.")

    # Strip path components. Both separators are treated the same, so neither
    # "../x" nor "..\\x" can escape.
    filename = filename.replace("\\", "/").split("/")[-1]

    filename = _INVALID_FILENAME_CHARS.sub("_", filename)

    # Windows silently discards trailing dots and spaces, so a name ending in
    # them would not match the file actually created. Strip them before any
    # further checks: "NUL." must be recognised as the NUL device.
    filename = filename.rstrip(". ")

    # This also disposes of "." and "..", which reduce to the empty string --
    # so no separate traversal check is needed here.
    if not filename:
        raise ValueError(
            "Filename consists only of dots and spaces, leaving nothing usable."
        )

    # A device name is reserved even when it carries an extension: NUL.txt
    # still opens the NUL device. The portion before the first dot decides.
    stem = filename.split(".", 1)[0]

    if stem.upper() in RESERVED_DEVICE_NAMES:
        filename = "_" + filename

    return _truncate_preserving_extension(filename, MAX_FILENAME_LENGTH)


def safe_download_path(filename: str) -> Path:
    """Create a path strictly inside the download directory.

    Args:
        filename: Filename to use for the downloaded file.

    Returns:
        Path object guaranteed to be within the sandboxed download directory.

    Raises:
        ValueError: If destination would escape the download directory.

    Example:
        >>> safe_download_path("example.tar.gz")
        PosixPath('/sandbox/downloads/example.tar.gz'),
    """

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
    """Download a file from an approved HTTPS domain.

    Files are ALWAYS written inside the download root (BIONIC_DOWNLOAD_ROOT,
    or %LOCALAPPDATA%\\windows-agent-mcp\\downloads by default). The caller
    cannot choose an arbitrary filesystem destination, and an existing file is
    never overwritten.

    Maximum download size: 500 MB.

    Args:
        url: HTTPS URL to download from. Must be on the host allowlist.
        filename: Optional custom filename. Defaults to the name in the URL.
            Path components are stripped, so this cannot escape the root.

    Returns:
        A success message with the final path and size, or a structured JSON
        error. This tool never raises.

    Example:
        >>> download_file("https://example.com/file.txt")
        'DOWNLOAD SUCCESS\\nURL: https://example.com/file.txt\\nFile: .../file.txt\\nSize: 123 bytes'
    """

    # Refusals -- URL policy and filename sanitisation -- are separated from
    # the request body below. ValueError is raised by plenty of ordinary code,
    # and a shared handler would mislabel such a failure as a policy refusal,
    # telling the caller to stop rather than retry.
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
        # Use temporary file for atomic writes
        temporary = destination.with_suffix(destination.suffix + ".part")

        # Never overwrite a real existing file.
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

        # Remove stale partial file.
        temporary.unlink(missing_ok=True)

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Bionic-Windows-Agent-MCP/1.0",
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
