"""Recursive content search across a source tree."""

from __future__ import annotations

import re
from pathlib import Path

from ..error import mcp_error
from ..filescan import iter_files, looks_binary, split_globs
from ..log import log
from ..utils import MAX_GREP_FILE_BYTES, MAX_SEARCH_MATCHES

__all__: list[str] = ["search_files"]

# Longest matching line reproduced in the output. A generated header or a
# minified shader can be one line of 200 KB, which would consume the whole
# context window for a single hit.
_MAX_LINE_CHARS = 300

# Regex syntax strong enough to imply the caller MEANT a regex. Deliberately
# narrow: a bare "(" or "." is excluded, because searching literally for
# "mcp_error(" or "self.name" is both common and correct, and warning about
# those would train the reader to ignore the note. A backslash escape or a
# ".*" quantifier is different -- it almost never appears in the source text
# someone is looking for.
_REGEX_INTENT = re.compile(
    r"""
      \\[dDsSwWbBAZ().\[\]{}+*?^$|]   # an escape: \( \. \d \s ...
    | \.[*+]                          # .* or .+
    | \[\^                            # a negated character class
    """,
    re.VERBOSE,
)


def _regex_intent_hint(pattern: str, regex: bool) -> str | None:
    r"""Warn when a literal search was handed something that looks like a regex.

    Measured failure: a model narrowed a truncated search to
    `return mcp_error\(` without setting regex=True, so the escape was matched
    literally and the search reported "No matches." It then retried variants of
    the same broken pattern and finally answered from the earlier truncated
    result as though it were complete.

    The generic "if you expected matches" note pointed at excluded build
    directories and a narrow file_glob -- both wrong, and both plausible enough
    to act on. Naming the real cause is the difference between one more call
    and a wrong answer.
    """

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
    """Search file contents recursively and report matching lines.

    This is the tool for "where is X used" and "what creates the swapchain".
    Prefer it over reading files one by one: it is the cheapest way to locate
    code in a large tree.

    Build output and version-control directories are skipped automatically
    (.git, build, out, bin, obj, x64, Debug, Release, Intermediate,
    node_modules and similar), as are binary files. On a game project the
    build tree is far larger than the source, so this is what keeps the search
    fast and the results readable.

    Args:
        pattern: Text to find. A literal substring unless regex is true.
        path: Directory to search. Defaults to the current directory.
        file_glob: Restrict to matching files, comma separated, e.g.
            "*.cpp,*.h,*.hpp". A pattern without a slash matches the file name
            at any depth. One with a slash matches the relative path and
            anchors the prefix only -- "src/*.cpp" also matches
            "src/renderer/vk/device.cpp", because "*" spans directories.
            Empty searches every text file.
        ignore_case: Case-insensitive matching. Defaults to false.
        regex: Treat pattern as a Python regular expression. Defaults to
            false, which is usually what you want for identifiers.
        max_results: Maximum matching lines to report. Defaults to 100.

    Returns:
        Plain text: one "relative/path:line: content" per match, then a
        summary. A structured JSON error on failure.

    Example:
        >>> search_files("vkCreateSwapchainKHR", "src", "*.cpp")
        "SEARCH: 'vkCreateSwapchainKHR' in src (*.cpp)\\n\\n..."
    """

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
        # re.escape rather than str.find so ignore_case works identically for
        # literal and regex searches.
        matcher = re.compile(re.escape(pattern), flags)

    # Remembered so the cap can be reported. Raising max_results past the
    # ceiling used to be silently ignored, which is indistinguishable from the
    # search simply having that many matches: a model asked for 200, then 300,
    # got the identical truncated result each time, and had no way to learn
    # that the parameter it was adjusting did nothing.
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
                # A locked .pdb mid-build, a path over MAX_PATH, a device
                # file. One unreadable file must not abort the whole search.
                unreadable += 1
                continue

            if looks_binary(raw):
                skipped_binary += 1
                continue

            # errors="replace" rather than strict: a single latin-1 comment in
            # an otherwise UTF-8 codebase is common, and refusing to search
            # the file over one byte would hide real matches.
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

        # Before the generic advice: when it applies it is almost always the
        # answer, and the generic note sends the reader somewhere else.
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
        # Spelled out because the observed failure was not a missing notice --
        # the model read this one, tried to narrow, and then answered from the
        # truncated list anyway, counting these lines to state a total that was
        # wrong by a third.
        body.append(
            "This list is INCOMPLETE. Do not report it as every match, and do "
            "not count these lines to give a total."
        )
        if requested_results > MAX_SEARCH_MATCHES:
            # Said plainly, because the obvious next move is to ask for more
            # and that move does nothing.
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
