"""Modify part of an existing text file by exact string replacement."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..error import mcp_error
from ..log import log
from ..utils import (
    MAX_WRITE_BYTES,
    PROJECT_ROOTS_ENV_VAR,
    ProtectedPathError,
    resolve_write_path,
)

__all__: list[str] = ["edit_file"]


def _dominant_newline(text: str) -> str:
    """Return the line ending the file mostly uses.

    Ties and empty files go to LF. A file with no newline at all also reports
    LF, which is harmless: there is nothing to convert back.
    """

    crlf = text.count("\r\n")

    # Every \r\n contains an \n, so bare LF count is the difference.
    lf = text.count("\n") - crlf

    return "\r\n" if crlf > lf else "\n"


def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Replace an exact string in an existing text file.

    Preferred over write_file for changing existing code: only the named
    region moves, so the rest of the file cannot be lost to a truncated
    generation.

    `old_string` must match EXACTLY, including indentation, and must be
    unique in the file. If it appears more than once the edit is refused
    rather than guessed at -- include a surrounding line or two to
    disambiguate, or pass replace_all.

    Line endings are handled for you: the file's own convention is detected
    and preserved, and CRLF/LF differences between your strings and the file
    are ignored when matching. So a CRLF file can be edited with plain "\\n"
    strings and stays CRLF.

    Edits are confined to the download root plus any directory listed in
    BIONIC_PROJECT_ROOTS.

    Args:
        path: File to edit. Must already exist.
        old_string: Exact text to find.
        new_string: Text to replace it with. May be empty to delete.
        replace_all: Replace every occurrence instead of requiring exactly
            one. Defaults to false.

    Returns:
        A confirmation naming the file and what changed, or a structured JSON
        error with recovery instructions.

    Example:
        >>> edit_file("src/main.cpp", "VK_FORMAT_B8G8R8A8_UNORM",
        ...           "VK_FORMAT_B8G8R8A8_SRGB")
        'EDITED: C:\\\\game\\\\src\\\\main.cpp (1 replacement at line 88)'
    """

    try:
        target = resolve_write_path(path)
    except ProtectedPathError as exc:
        return mcp_error(
            "PROTECTED_PATH",
            "edit_file",
            str(exc),
            path=path,
            recovery=[
                "DO NOT retry, and do not try another directory: the "
                "refusal is by filename, not by location.",
                "Only the operator may change this file. Say what you "
                "needed it for and let the user decide.",
                "To have a web host allowed, the user runs: python -m "
                "windows_agent_mcp.hostgrants --add HOST",
            ],
        )
    except ValueError as exc:
        return mcp_error(
            "WRITE_PATH_NOT_ALLOWED",
            "edit_file",
            str(exc),
            path=path,
            recovery=[
                "DO NOT retry the identical path.",
                "Edit inside a directory the operator has approved.",
                f"Ask the user to add the project directory to "
                f"{PROJECT_ROOTS_ENV_VAR} if it is missing.",
            ],
        )

    if not old_string:
        return mcp_error(
            "INVALID_EDIT",
            "edit_file",
            "old_string cannot be empty.",
            path=str(target),
            recovery=[
                "Provide the exact existing text to replace.",
                "Use write_file to create a new file.",
            ],
        )

    if old_string == new_string:
        return mcp_error(
            "INVALID_EDIT",
            "edit_file",
            "old_string and new_string are identical, so this edit would do nothing.",
            path=str(target),
            recovery=[
                "DO NOT retry this call.",
                "Check what you intended to change and pass a different new_string.",
            ],
        )

    if not target.exists():
        return mcp_error(
            "PATH_NOT_FOUND",
            "edit_file",
            f"'{target}' does not exist.",
            path=str(target),
            recovery=[
                "DO NOT retry the identical operation.",
                "Use list_directory to see what exists.",
                "Use write_file to create the file first.",
            ],
        )

    if not target.is_file():
        return mcp_error(
            "PATH_IS_NOT_FILE",
            "edit_file",
            f"'{target}' is not a regular file.",
            path=str(target),
            recovery=["Do not retry edit_file on this path."],
        )

    try:
        raw = target.read_bytes()
    except PermissionError:
        log.warning("edit_file permission denied reading: %s", target)

        return mcp_error(
            "PERMISSION_DENIED",
            "edit_file",
            f"Permission denied while reading '{target}'.",
            path=str(target),
            recovery=[
                "Do not repeatedly retry the same operation.",
                "Ask the user to resolve the permission problem.",
            ],
        )
    except OSError as exc:
        log.exception("edit_file failed to read: %s", target)

        return mcp_error(
            "READ_FAILED",
            "edit_file",
            f"Filesystem error while reading '{target}': {exc}",
            path=str(target),
            recovery=["Do not repeatedly retry the identical operation."],
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return mcp_error(
            "INVALID_TEXT_ENCODING",
            "edit_file",
            f"'{target}' is not valid UTF-8 text.",
            path=str(target),
            recovery=[
                "This tool edits UTF-8 text only; it cannot edit binary files.",
                "Do not retry.",
            ],
        )

    newline = _dominant_newline(text)

    # Match in LF space so the caller never has to know or guess the file's
    # convention. Without this, editing a CRLF file with an LF old_string
    # fails to match for reasons invisible in the tool output -- and the model
    # then rewrites the whole file to work around it.
    normalised = text.replace("\r\n", "\n")
    wanted = old_string.replace("\r\n", "\n")
    replacement = new_string.replace("\r\n", "\n")

    occurrences = normalised.count(wanted)

    if occurrences == 0:
        return mcp_error(
            "EDIT_STRING_NOT_FOUND",
            "edit_file",
            f"old_string was not found in '{target}'.",
            path=str(target),
            recovery=[
                "DO NOT retry with the same old_string.",
                "Use read_file on this path and copy the target text "
                "verbatim, including indentation.",
                "Leading and trailing whitespace is significant; line endings are not.",
            ],
        )

    if occurrences > 1 and not replace_all:
        return mcp_error(
            "EDIT_STRING_NOT_UNIQUE",
            "edit_file",
            (
                f"old_string appears {occurrences} times in '{target}'. "
                f"Refusing to guess which one you meant."
            ),
            path=str(target),
            recovery=[
                "DO NOT retry the identical call.",
                "Extend old_string with the surrounding lines until it is unique.",
                "Pass replace_all=true only if every occurrence should change.",
            ],
        )

    if replace_all:
        updated = normalised.replace(wanted, replacement)
        changed = occurrences
    else:
        updated = normalised.replace(wanted, replacement, 1)
        changed = 1

    # 1-based line of the first change, computed before restoring CRLF so the
    # count is not thrown off by the two-character ending.
    first_line = normalised[: normalised.index(wanted)].count("\n") + 1

    if newline != "\n":
        updated = updated.replace("\n", newline)

    encoded = updated.encode("utf-8")

    if len(encoded) > MAX_WRITE_BYTES:
        return mcp_error(
            "CONTENT_TOO_LARGE",
            "edit_file",
            (
                f"The edited file would be {len(encoded):,} bytes, over the "
                f"{MAX_WRITE_BYTES:,} byte limit."
            ),
            path=str(target),
            recovery=["Do not retry. Make a smaller edit."],
        )

    try:
        handle, temporary = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )

        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(encoded)

            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    except PermissionError:
        log.warning("edit_file permission denied writing: %s", target)

        return mcp_error(
            "PERMISSION_DENIED",
            "edit_file",
            f"Permission denied while writing '{target}'.",
            path=str(target),
            recovery=[
                "Do not repeatedly retry the same operation.",
                "The file may be read-only or open in another program.",
            ],
        )

    except OSError as exc:
        log.exception("edit_file failed to write: %s", target)

        return mcp_error(
            "WRITE_FAILED",
            "edit_file",
            f"Filesystem error while writing '{target}': {exc}",
            path=str(target),
            recovery=["Do not repeatedly retry the identical operation."],
        )

    log.info("edit_file made %d replacement(s) in %s", changed, target)

    ending = "CRLF" if newline == "\r\n" else "LF"

    if changed == 1:
        detail = f"1 replacement at line {first_line}"
    else:
        detail = f"{changed} replacements, first at line {first_line}"

    return f"EDITED: {target} ({detail}, {ending} preserved)"
