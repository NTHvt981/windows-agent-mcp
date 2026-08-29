"""Find files by name pattern across a source tree."""

from __future__ import annotations

from pathlib import Path

from ..error import mcp_error
from ..filescan import iter_files, split_globs
from ..log import log
from ..utils import MAX_FIND_RESULTS

__all__: list[str] = ["find_files"]


def find_files(
    file_glob: str,
    path: str = ".",
    max_results: int = MAX_FIND_RESULTS,
) -> str:
    """List files matching a name pattern, recursively.

    Use this to answer "what exists" before reading anything: which shaders
    the project has, where the CMakeLists files are, whether a header is
    present at all. list_directory shows one level; this searches the tree.

    Build output and version-control directories are skipped automatically
    (.git, build, out, bin, obj, x64, Debug, Release, Intermediate,
    node_modules and similar).

    Args:
        file_glob: Name pattern, comma separated for several, e.g.
            "*.vert,*.frag,*.hlsl". A pattern without a slash matches the file
            name at any depth. One with a slash matches the relative path and
            anchors the prefix only -- "shaders/*.glsl" also matches
            "shaders/post/blur.glsl", because "*" spans directories.
        path: Directory to search. Defaults to the current directory.
        max_results: Maximum paths to report. Defaults to 500.

    Returns:
        Plain text: one relative path per line, then a count. A structured
        JSON error on failure.

    Example:
        >>> find_files("*.vert,*.frag", "shaders")
        'FIND: *.vert,*.frag in shaders\\n\\nshaders/post/blur.frag\\n...'
    """

    patterns = split_globs(file_glob)

    if not patterns:
        return mcp_error(
            "INVALID_PATTERN",
            "find_files",
            "file_glob cannot be empty.",
            recovery=[
                "Provide a name pattern such as '*.cpp' or '*.vert,*.frag'.",
                "Use list_directory to list one directory without a pattern.",
            ],
        )

    if not path or not path.strip():
        path = "."

    root = Path(path)

    if not root.exists():
        return mcp_error(
            "PATH_NOT_FOUND",
            "find_files",
            f"'{path}' does not exist.",
            path=path,
            recovery=[
                "DO NOT retry the identical path.",
                "Use list_directory to see what exists.",
            ],
        )

    if not root.is_dir():
        return mcp_error(
            "PATH_IS_NOT_DIRECTORY",
            "find_files",
            f"'{path}' is not a directory.",
            path=path,
            recovery=["Pass a directory to search."],
        )

    max_results = max(1, min(int(max_results), MAX_FIND_RESULTS))

    found: list[str] = []
    truncated = False

    try:
        for _absolute, relative in iter_files(root, patterns=patterns):
            if len(found) >= max_results:
                truncated = True
                break

            found.append(relative.as_posix())

    except OSError as exc:
        log.exception("find_files failed under %s", root)

        return mcp_error(
            "SEARCH_FAILED",
            "find_files",
            f"Filesystem error while searching '{path}': {exc}",
            path=path,
            recovery=["Do not repeatedly retry the identical operation."],
        )

    header = f"FIND: {file_glob} in {root.as_posix()}"

    if not found:
        return "\n".join(
            [
                header,
                "",
                "No files matched.",
                "",
                "Build and version-control directories are excluded. Check "
                "the pattern, or search a parent directory.",
            ]
        )

    body = [header, "", *found, ""]

    if truncated:
        body.append(f"...[truncated at {max_results} results]...")
        body.append("Narrow the pattern or search a subdirectory.")
    else:
        body.append(f"{len(found)} files")

    return "\n".join(body)
