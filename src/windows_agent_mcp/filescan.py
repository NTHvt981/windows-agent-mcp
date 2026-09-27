"""Directory walking shared by search_files and find_files."""

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
    """Split a comma-separated glob list into individual patterns."""

    return [part.strip() for part in file_glob.split(",") if part.strip()]


def matches_any_glob(relative: Path, patterns: list[str]) -> bool:
    """Test a relative path against a list of globs."""

    if not patterns:
        return True

    # Forward slashes, so Windows callers' patterns match.
    posix = relative.as_posix()

    for pattern in patterns:
        if "/" in pattern:
            if fnmatch(posix, pattern):
                return True
        elif fnmatch(relative.name, pattern):
            return True

    return False


def looks_binary(raw: bytes) -> bool:
    """Detect a binary file cheaply."""

    return b"\x00" in raw[:8192]


def iter_files(
    root: Path,
    *,
    patterns: list[str],
    max_scanned: int = MAX_FILES_SCANNED,
) -> Iterator[tuple[Path, Path]]:
    """Walk `root`, yielding (absolute, relative) for each matching file."""

    # Pruned before descending; symlinked directories are not followed.

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
