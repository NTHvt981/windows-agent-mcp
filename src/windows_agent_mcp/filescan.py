"""Directory walking shared by search_files and find_files.

Pure filesystem traversal with the pruning and caps both tools need. Kept
separate from the tools so the pruning rules -- the part most likely to be
wrong on a given project layout -- are unit-testable without going through a
tool's formatting and error handling.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from fnmatch import fnmatch
from pathlib import Path

from .utils import MAX_FILES_SCANNED, SKIPPED_DIRECTORY_NAMES

__all__: list[str] = [
    "iter_files",
    "looks_binary",
    "matches_any_glob",
    "split_globs",
]


def split_globs(file_glob: str) -> list[str]:
    """Split a comma-separated glob list into individual patterns.

    Comma separation exists because C++ sources come in pairs: a caller
    almost always wants "*.cpp,*.h,*.hpp" rather than three separate
    searches, and three searches is three times the context.

    Args:
        file_glob: One or more patterns, comma separated. Empty means "all".

    Returns:
        The non-empty patterns, whitespace stripped.
    """

    return [part.strip() for part in file_glob.split(",") if part.strip()]


def matches_any_glob(relative: Path, patterns: list[str]) -> bool:
    """Test a relative path against a list of globs.

    A pattern containing a slash is matched against the whole relative path;
    one without is matched against the file name alone. So "*.h" finds headers
    at any depth, while "include/*.h" restricts them to that subtree.

    Note that this is fnmatch, not pathlib.match: "*" spans directory
    separators. "src/*.cpp" therefore matches "src/deep/main.cpp" as well as
    "src/main.cpp" -- a slash anchors the PREFIX, it does not limit depth.
    That is the more useful reading for a caller who means "cpp files under
    src", and it makes "**" unnecessary.

    Args:
        relative: Path relative to the search root.
        patterns: Patterns from split_globs. Empty list matches everything.

    Returns:
        True if any pattern matches, or if there are no patterns.
    """

    if not patterns:
        return True

    # Compare with forward slashes so a caller can write "src/*.cpp" on
    # Windows without the pattern silently never matching.
    posix = relative.as_posix()

    for pattern in patterns:
        if "/" in pattern:
            if fnmatch(posix, pattern):
                return True
        elif fnmatch(relative.name, pattern):
            return True

    return False


def looks_binary(raw: bytes) -> bool:
    """Detect a binary file cheaply.

    A NUL byte in the first few KB is the single most reliable cheap signal:
    it appears in .obj, .lib, .pdb, .spv and every image format, and
    essentially never in source. Checking a prefix rather than the whole
    buffer keeps this O(1) on a large file.

    Args:
        raw: Leading bytes of the file.

    Returns:
        True if the content looks binary.
    """

    return b"\x00" in raw[:8192]


def iter_files(
    root: Path,
    *,
    patterns: list[str],
    max_scanned: int = MAX_FILES_SCANNED,
) -> Iterator[tuple[Path, Path]]:
    """Walk `root`, yielding (absolute, relative) for each matching file.

    Directories named in SKIPPED_DIRECTORY_NAMES are pruned, and pruned before
    descending -- which is the whole point. On a C++ game tree the build
    output dwarfs the source, so pruning after the fact would still pay to
    enumerate every object file.

    Symlinked directories are not followed: a link pointing at a parent turns
    the walk into an infinite loop, and one pointing outside the tree makes
    the reported relative paths meaningless.

    Args:
        root: Directory to walk.
        patterns: Globs from split_globs; empty matches everything.
        max_scanned: Stop after visiting this many candidate files.

    Yields:
        (absolute path, path relative to root) for each match, in directory
        order. Stops yielding once max_scanned files have been visited.
    """

    visited = 0

    for current, directories, filenames in os.walk(root, followlinks=False):
        # Mutating the list in place is how os.walk prunes.
        directories[:] = sorted(
            name for name in directories if name.lower() not in SKIPPED_DIRECTORY_NAMES
        )

        current_path = Path(current)

        for name in sorted(filenames):
            visited += 1

            if visited > max_scanned:
                return

            absolute = current_path / name

            try:
                relative = absolute.relative_to(root)
            except ValueError:  # pragma: no cover - defensive
                relative = Path(name)

            if matches_any_glob(relative, patterns):
                yield absolute, relative
