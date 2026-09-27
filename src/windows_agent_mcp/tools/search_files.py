"""Recursive content search across a source tree."""

from __future__ import annotations

import re
from pathlib import Path

from ..error import mcp_error
from ..filescan import iter_files, looks_binary, split_globs
from ..log import log
from ..utils import MAX_GREP_FILE_BYTES, MAX_SEARCH_MATCHES

__all__: list[str] = ["search_files"]

_MAX_LINE_CHARS = 300

_REGEX_INTENT = re.compile(
    r"""
      \\[dDsSwWbBAZ().\[\]{}+*?^$|]   # an escape: \( \. \d \s ...
    | \.[*+]                          # .* or .+
    | \[\^                            # a negated character class
    """,
    re.VERBOSE,
)


def _regex_intent_hint(pattern: str, regex: bool) -> str | None:
    r"""Warn when a literal search was handed something that looks like a regex."""

    if regex or not _REGEX_INTENT.search(pattern):
        return None

    return (
        "The pattern contains regex syntax but was searched literally. "
        "Pass regex=True to treat it as a regular expression."
    )


def search_files(
    pattern: str,
    path: str = ".",
    file_glob: str = "",
    ignore_case: bool = False,
    regex: bool = False,
    max_results: int = MAX_SEARCH_MATCHES,
) -> str:
    """Search file contents recursively and report matching lines."""

    if not pattern:
        return mcp_error(
            "INVALID_PATTERN",
            "search_files",
            "pattern cannot be empty.",
            recovery=[
                "Provide the text to search for.",
                "Use find_files to list files by name instead.",
            ],
        )

    if not path or not path.strip():
        path = "."

    root = Path(path)

    if not root.exists():
        return mcp_error(
            "PATH_NOT_FOUND",
            "search_files",
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
            "search_files",
            f"'{path}' is not a directory.",
            path=path,
            recovery=[
                "Pass a directory to search.",
                "Use read_file to read a single file.",
            ],
        )

    flags = re.IGNORECASE if ignore_case else 0

    if regex:
        try:
            matcher = re.compile(pattern, flags)
        except re.error as exc:
            return mcp_error(
                "INVALID_PATTERN",
                "search_files",
                f"pattern is not a valid regular expression: {exc}",
                recovery=[
                    "DO NOT retry the identical pattern.",
                    "Pass regex=false to search for the text literally.",
                ],
            )
    else:
        # re.escape keeps ignore_case identical for literal and regex.
        matcher = re.compile(re.escape(pattern), flags)

    requested_results = int(max_results)

    max_results = max(1, min(requested_results, MAX_SEARCH_MATCHES))

    patterns = split_globs(file_glob)

    lines: list[str] = []
    files_with_matches = 0
    total_matches = 0
    skipped_binary = 0
    unreadable = 0
    truncated = False

    try:
        for absolute, relative in iter_files(root, patterns=patterns):
            try:
                with absolute.open("rb") as handle:
                    raw = handle.read(MAX_GREP_FILE_BYTES)
            except (OSError, ValueError):
                # One unreadable file must not abort the whole search.
                unreadable += 1
                continue

            if looks_binary(raw):
                skipped_binary += 1
                continue

            # errors=replace: one latin-1 byte must not hide a file's matches.
            text = raw.decode("utf-8", errors="replace")

            matched_here = False

            for number, line in enumerate(text.splitlines(), start=1):
                if not matcher.search(line):
                    continue

                matched_here = True
                total_matches += 1

                if len(lines) >= max_results:
                    truncated = True
                    break

                shown = line.strip()

                if len(shown) > _MAX_LINE_CHARS:
                    shown = shown[:_MAX_LINE_CHARS] + " ...[line truncated]"

                lines.append(f"{relative.as_posix()}:{number}: {shown}")

            if matched_here:
                files_with_matches += 1

            if truncated:
                break

    except OSError as exc:
        log.exception("search_files failed under %s", root)

        return mcp_error(
            "SEARCH_FAILED",
            "search_files",
            f"Filesystem error while searching '{path}': {exc}",
            path=path,
            recovery=["Do not repeatedly retry the identical operation."],
        )

    scope = f" ({file_glob})" if patterns else ""

    header = f"SEARCH: {pattern!r} in {root.as_posix()}{scope}"

    if not lines:
        detail = [header, "", "No matches."]

        if skipped_binary or unreadable:
            detail.append(
                f"(skipped {skipped_binary} binary and {unreadable} unreadable files)"
            )

        detail.append("")

        hint = _regex_intent_hint(pattern, regex)

        if hint is not None:
            detail.append(hint)

        detail.append(
            "If you expected matches: build and version-control "
            "directories are excluded, and file_glob may be too narrow."
        )

        return "\n".join(detail)

    body = [header, "", *lines, ""]

    if truncated:
        body.append(
            f"...[truncated at {max_results} results; there are at least "
            f"{total_matches}]..."
        )
        body.append(
            "This list is INCOMPLETE. Do not report it as every match, and do "
            "not count these lines to give a total."
        )
        if requested_results > MAX_SEARCH_MATCHES:
            body.append(
                f"(max_results={requested_results} was capped at "
                f"{MAX_SEARCH_MATCHES}; asking for more will not return more.)"
            )

        body.append("Narrow the search with file_glob or a longer pattern instead.")
    else:
        body.append(f"{total_matches} matches in {files_with_matches} files")

    if skipped_binary or unreadable:
        body.append(f"(skipped {skipped_binary} binary, {unreadable} unreadable)")

    return "\n".join(body)
