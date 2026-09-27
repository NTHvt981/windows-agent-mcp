"""Create a new text file, atomically and inside an approved root."""

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

__all__: list[str] = ["write_file"]


def write_file(path: str, content: str, overwrite: bool = False) -> str:
    """Create text file, or replace one entirely."""

    try:
        target = resolve_write_path(path)
    except ProtectedPathError as exc:
        return mcp_error(
            "PROTECTED_PATH",
            "write_file",
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
            "write_file",
            str(exc),
            path=path,
            recovery=[
                "DO NOT retry the identical path.",
                "Write inside a directory the operator has approved.",
                f"Ask the user to add the project directory to "
                f"{PROJECT_ROOTS_ENV_VAR} if it is missing.",
            ],
        )

    if not isinstance(content, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        return mcp_error(
            "INVALID_CONTENT",
            "write_file",
            "content must be a string.",
            path=str(target),
            recovery=["Pass the file body as a string."],
        )

    encoded = content.encode("utf-8")

    if len(encoded) > MAX_WRITE_BYTES:
        return mcp_error(
            "CONTENT_TOO_LARGE",
            "write_file",
            (
                f"content is {len(encoded):,} bytes, over the "
                f"{MAX_WRITE_BYTES:,} byte limit."
            ),
            path=str(target),
            recovery=[
                "DO NOT retry with the same content.",
                "Split the file, or generate it with a script instead.",
            ],
        )

    existed = target.exists()

    if existed and not overwrite:
        return mcp_error(
            "FILE_EXISTS",
            "write_file",
            f"'{target}' already exists and overwrite is false.",
            path=str(target),
            recovery=[
                "DO NOT retry the identical call.",
                "Use edit_file to change part of an existing file. That is "
                "almost always what you want.",
                "Pass overwrite=true ONLY if replacing the entire file is "
                "genuinely intended.",
            ],
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)

        # Same directory keeps os.replace atomic within one filesystem.
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
        log.warning("write_file permission denied: %s", target)

        return mcp_error(
            "PERMISSION_DENIED",
            "write_file",
            f"Permission denied while writing '{target}'.",
            path=str(target),
            recovery=[
                "Do not repeatedly retry the same operation.",
                "The file may be read-only or open in another program.",
                "Ask the user to resolve the permission problem.",
            ],
        )

    except OSError as exc:
        log.exception("write_file failed: %s", target)

        return mcp_error(
            "WRITE_FAILED",
            "write_file",
            f"Filesystem error while writing '{target}': {exc}",
            path=str(target),
            recovery=[
                "Inspect the filesystem state before retrying.",
                "Do not repeatedly retry the identical operation.",
            ],
        )

    log.info("write_file wrote %d bytes to %s", len(encoded), target)

    disposition = "overwritten" if existed else "new file"

    return f"WROTE: {target} ({len(encoded):,} bytes, {disposition})"
