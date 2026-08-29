"""List directory tool for Windows Agent MCP Server."""

from __future__ import annotations

from pathlib import Path

from ..error import mcp_error
from ..log import log
from ..utils import MAX_DIRECTORY_ENTRIES

__all__: list[str] = ["list_directory"]


def list_directory(path: str = ".") -> str:
    """List the contents of a directory.

    Returns a plain text listing, directories first then files, each
    alphabetically. Large directories are truncated rather than refused.
    This is a read-only operation.

    Args:
        path: Directory path to list. Defaults to the current directory.

    Returns:
        A plain text listing, or a structured JSON error on failure.

    Example:
        >>> list_directory("src")
        'DIRECTORY: src\\n  [dir]  tools\\n  [file] utils.py\\n\\n2 entries'
    """

    try:
        if not path or not path.strip():
            path = "."

        directory = Path(path)

        if not directory.exists():
            return mcp_error(
                "PATH_NOT_FOUND",
                "list_directory",
                "The requested directory does not exist.",
                path=path,
                recovery=[
                    "Do not retry the identical operation.",
                    "List the parent directory to see what exists.",
                ],
            )

        if not directory.is_dir():
            return mcp_error(
                "PATH_IS_NOT_DIRECTORY",
                "list_directory",
                "The requested path is not a directory.",
                path=path,
                recovery=[
                    "Use read_file if this path refers to a file.",
                ],
            )

        entries = sorted(
            directory.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )

        total = len(entries)

        shown = entries[:MAX_DIRECTORY_ENTRIES]

        lines = [f"DIRECTORY: {directory}"]

        for entry in shown:
            kind = "dir " if entry.is_dir() else "file"
            lines.append(f"  [{kind}] {entry.name}")

        if not shown:
            lines.append("  (empty)")

        if total > len(shown):
            lines.append("")
            lines.append(f"...[truncated: showed {len(shown)} of {total} entries]...")
        else:
            lines.append("")
            lines.append(f"{total} entries")

        return "\n".join(lines)

    except PermissionError:
        return mcp_error(
            "PERMISSION_DENIED",
            "list_directory",
            "Permission denied while listing the directory.",
            path=path,
            recovery=[
                "Do not repeatedly retry the same operation.",
                "Ask the user to resolve the permission problem if necessary.",
            ],
        )

    except OSError as exc:
        log.exception("list_directory failed: %s", path)

        return mcp_error(
            "DIRECTORY_READ_FAILED",
            "list_directory",
            str(exc),
            path=path,
            recovery=[
                "Inspect the parent directory.",
                "Do not repeatedly retry the same operation.",
            ],
        )
