"""Read file tool for Windows Agent MCP Server."""

from __future__ import annotations

from pathlib import Path

from ..error import mcp_error
from ..log import log
from ..utils import DEFAULT_READ_LINES, MAX_READ_BYTES

__all__: list[str] = ["read_file"]


def read_file(
    path: str,
    start_line: int = 1,
    max_lines: int = DEFAULT_READ_LINES,
) -> str:
    """Read a UTF-8 text file.

    Returns the file's text directly, NOT wrapped in JSON. A JSON envelope
    would escape every newline in the content onto a single line, which is
    hard to read and expensive in context.

    Long files are truncated rather than refused, and the truncation is
    reported with the exact call needed to continue. This is a read-only
    operation.

    Args:
        path: Path to the file to read.
        start_line: 1-based line number to start from. Values below 1 are
                    treated as 1.
        max_lines: Maximum number of lines to return. Defaults to 2000.

    Returns:
        The requested lines as plain text, with a trailing notice if the
        content was truncated. On failure, a structured JSON error with
        recovery instructions.

    Example:
        >>> read_file("README.md", start_line=1, max_lines=5)
        '# Title\\n\\nFirst paragraph...'
    """

    if not path or not path.strip():
        return mcp_error(
            "INVALID_PATH",
            "read_file",
            "File path cannot be empty.",
            recovery=[
                "Provide a valid file path.",
                "Do not retry with an empty path.",
            ],
        )

    try:
        requested = Path(path)

        if not requested.exists():
            # Report the deepest ancestor that DOES exist, so the caller can
            # orient itself instead of guessing again.
            missing = requested

            while missing.parent != missing and not missing.exists():
                missing = missing.parent

            return mcp_error(
                "PATH_NOT_FOUND",
                "read_file",
                (
                    "The requested file does not exist. "
                    f"Deepest existing ancestor: {missing}"
                ),
                path=path,
                recovery=[
                    "DO NOT retry the identical read_file operation.",
                    "Use list_directory to see what actually exists at the "
                    "ancestor path named above.",
                    "Prefer paths returned by list_directory over paths you "
                    "inferred or guessed.",
                ],
            )

        if not requested.is_file():
            return mcp_error(
                "PATH_IS_NOT_FILE",
                "read_file",
                "The requested path exists but is not a regular file.",
                path=path,
                recovery=[
                    "Do not retry read_file on this path.",
                    "Use list_directory to inspect the path.",
                ],
            )

        start_line = max(1, int(start_line))
        max_lines = max(1, int(max_lines))

        # Cap the bytes decoded so a huge file cannot exhaust the context
        # window. Read one extra byte to detect that more remains.
        with requested.open("rb") as handle:
            raw = handle.read(MAX_READ_BYTES + 1)

        byte_truncated = len(raw) > MAX_READ_BYTES

        if byte_truncated:
            raw = raw[:MAX_READ_BYTES]

            # Cutting at a fixed byte offset can split a multi-byte
            # character. Drop up to 3 trailing bytes to land on a boundary,
            # so a valid UTF-8 file is never reported as bad encoding purely
            # because of where the cap fell.
            for trim in range(4):
                try:
                    raw[: len(raw) - trim].decode("utf-8")
                except UnicodeDecodeError:
                    continue
                raw = raw[: len(raw) - trim]
                break

        text = raw.decode("utf-8")

        lines = text.splitlines()

        total_available = len(lines)

        if start_line > total_available and total_available > 0:
            return mcp_error(
                "START_LINE_OUT_OF_RANGE",
                "read_file",
                (
                    f"start_line {start_line} is past the end of the "
                    f"readable content ({total_available} lines)."
                ),
                path=path,
                recovery=[
                    "Do not retry with the same start_line.",
                    f"Use a start_line between 1 and {total_available}.",
                ],
            )

        selected = lines[start_line - 1 : start_line - 1 + max_lines]

        last_line = start_line - 1 + len(selected)

        body = "\n".join(selected)

        line_truncated = last_line < total_available

        if not (line_truncated or byte_truncated):
            return body

        notice = [
            "",
            f"...[truncated: showed lines {start_line}-{last_line}]...",
        ]

        if line_truncated:
            notice.append(
                f"Continue with: read_file(path={path!r}, start_line={last_line + 1})"
            )

        if byte_truncated:
            notice.append(
                f"The file is larger than the {MAX_READ_BYTES:,} byte read "
                f"limit; content beyond that is not reachable with this tool."
            )

        return body + "\n" + "\n".join(notice)

    except PermissionError:
        log.warning("read_file permission denied: %s", path)

        return mcp_error(
            "PERMISSION_DENIED",
            "read_file",
            f"Permission denied while reading '{path}'.",
            path=path,
            recovery=[
                "Do not repeatedly retry the same operation.",
                "Check the file permissions.",
                "Ask the user to resolve the permission problem if necessary.",
            ],
        )

    except UnicodeDecodeError:
        return mcp_error(
            "INVALID_TEXT_ENCODING",
            "read_file",
            "The file could not be decoded as UTF-8.",
            path=path,
            recovery=[
                "Do not assume the file is UTF-8 text.",
                "This tool reads text only; it cannot read binary files.",
            ],
        )

    except OSError as exc:
        log.exception("read_file failed: %s", path)

        return mcp_error(
            "READ_FAILED",
            "read_file",
            f"Filesystem error while reading '{path}': {exc}",
            path=path,
            recovery=[
                "Inspect the filesystem state before retrying.",
                "Do not repeatedly retry the identical operation.",
            ],
        )
