"""List empty directories tool for Windows Agent MCP Server."""

from __future__ import annotations

import os
from pathlib import Path

from ..error import mcp_error
from ..log import log
from ..utils import MAX_EMPTY_DIR_RESULTS

__all__: list[str] = ["list_empty_dirs"]


def list_empty_dirs(path: str) -> str:
    """Find empty directories beneath a path.

    A directory counts as empty when it contains no files and every
    subdirectory it contains is itself empty. So a tree of nothing but
    directories is reported from the top down, which is what makes the
    result usable for cleanup.

    Args:
        path: Root directory to search.

    Returns:
        One directory path per line, deepest first, or a structured JSON
        error on failure. Reports "no empty directories found" when there
        are none.

    Example:
        >>> list_empty_dirs("build")
        'build/obj\\nbuild/obj/debug'
    """

    if not path or not path.strip():
        return mcp_error(
            "INVALID_PATH",
            "list_empty_dirs",
            "Directory path cannot be empty.",
            recovery=[
                "Provide a valid directory path.",
                "Do not retry with an empty path.",
            ],
        )

    root = Path(path)

    if not root.exists():
        return mcp_error(
            "PATH_NOT_FOUND",
            "list_empty_dirs",
            "The requested directory does not exist.",
            path=path,
            recovery=[
                "Do not retry the identical operation.",
                "Use list_directory to inspect the parent directory.",
            ],
        )

    if not root.is_dir():
        return mcp_error(
            "PATH_IS_NOT_DIRECTORY",
            "list_empty_dirs",
            "The requested path is not a directory.",
            path=path,
            recovery=[
                "Provide a directory path, not a file path.",
            ],
        )

    try:
        empty: list[str] = []

        # topdown=False visits children before parents, so by the time a
        # directory is examined every child has already been classified.
        # That is what lets rule 2 (a parent holding only empty
        # directories) be evaluated in a single pass.
        empty_lookup: set[str] = set()

        for dirpath, dirnames, filenames in os.walk(path, topdown=False):
            if filenames:
                continue

            children = (os.path.join(dirpath, name) for name in dirnames)

            if all(child in empty_lookup for child in children):
                empty_lookup.add(dirpath)
                empty.append(dirpath)

    except OSError as exc:
        log.exception("list_empty_dirs failed: %s", path)

        return mcp_error(
            "DIRECTORY_WALK_FAILED",
            "list_empty_dirs",
            f"Filesystem error while walking '{path}': {exc}",
            path=path,
            recovery=[
                "Inspect the filesystem state before retrying.",
                "Do not repeatedly retry the identical operation.",
            ],
        )

    if not empty:
        return "no empty directories found"

    shown = empty[:MAX_EMPTY_DIR_RESULTS]

    body = "\n".join(shown)

    if len(empty) > len(shown):
        body += (
            f"\n\n...[truncated: showed {len(shown)} of {len(empty)} directories]..."
        )

    return body
