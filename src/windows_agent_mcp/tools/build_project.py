"""Run a build and report structured diagnostics instead of a raw log."""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from ..allowed_command import BUILD_COMMANDS, tokenize_command
from ..buildlock import BuildAlreadyRunning, build_slot
from ..diagnostics import format_report, parse_build_output, parse_rebuild_kind
from ..error import mcp_error
from ..log import log
from ..process import run_process
from ..utils import (
    BUILD_TIMEOUT_SECONDS,
    get_download_root,
    resolve_working_directory,
)
from .run_powershell import validate_powershell_command

__all__: list[str] = ["build_project"]

# Longest slice of raw output quoted inside an error envelope. Enough to see
# what the tool said, short enough not to blow the context on a failure the
# model cannot act on anyway.
_MAX_ERROR_EXCERPT = 1500


def build_project(
    command: str,
    working_directory: str | None = None,
    timeout_seconds: int = BUILD_TIMEOUT_SECONDS,
) -> str:
    """Run a build command and return only the diagnostics that matter.

    Prefer this over run_powershell for anything that compiles. run_powershell
    returns the raw log, which on a C++ project means hundreds of warnings and
    the same header error repeated once per translation unit. This parses that
    output and returns unique errors first, each with its file, line and code,
    with repeat counts instead of repetition.

    Understands MSVC (C####, LNK####), clang and gcc, CMake configure errors,
    and ninja. If nothing parses but the build failed, the tail of the raw
    output is shown rather than a misleading "no errors".

    Only build drivers may be run: cmake, ninja, msbuild, ctest, premake5,
    dotnet, cargo, clang, gcc, cl, python. Exactly one command per call, with
    the same restrictions as run_powershell -- no pipelines, redirection or
    separators outside quotes.

    Args:
        command: Build command, e.g. "cmake --build build --config Debug".
        working_directory: Directory to build in. Must be inside the download
            root or a root listed in WAMCP_PROJECT_ROOTS.
        timeout_seconds: Wall-clock limit, 1 to 1800. Defaults to 600, since
            a cold C++ build takes far longer than a shell command.

    Returns:
        A plain-text diagnostic report, or a structured JSON error.

    Example:
        >>> build_project("cmake --build build", "C:/game")
        'BUILD: cmake --build build  (exit code 0)\\n\\nBUILD SUCCEEDED'
    """

    if not command or not command.strip():
        return mcp_error(
            "INVALID_COMMAND",
            "build_project",
            "command cannot be empty.",
            recovery=["Pass a build command such as 'cmake --build build'."],
        )

    # Reuse run_powershell's policy wholesale rather than reimplementing a
    # weaker version: composition, dangerous patterns, the allowlist, inline
    # code and encoded payloads are all checked identically.
    try:
        validate_powershell_command(command)
    except ValueError as exc:
        return mcp_error(
            "COMMAND_NOT_ALLOWED",
            "build_project",
            str(exc),
            recovery=[
                "DO NOT retry the identical command.",
                "Run exactly one build command per call: no ';', '|', '&', "
                "'>' or '$()' outside quotes.",
                "Configure and build in two separate calls.",
            ],
        )

    # tokenize_command raises only on an empty command or an unterminated
    # quote, and validate_powershell_command above already rejects both --
    # empty via the check at the top of this function, unterminated quotes via
    # find_command_composition. So this handler is currently unreachable and
    # is kept only so a future relaxation of that policy cannot turn a
    # ValueError into a transport-level failure.
    try:
        argv = tokenize_command(command)
    except ValueError as exc:  # pragma: no cover - unreachable, see above
        return mcp_error(
            "INVALID_COMMAND",
            "build_project",
            str(exc),
            recovery=["Check the quoting in the command."],
        )

    program = Path(argv[0]).name.lower()

    if program not in BUILD_COMMANDS:
        return mcp_error(
            "NOT_A_BUILD_COMMAND",
            "build_project",
            (
                f"'{argv[0]}' is not a build driver. build_project runs "
                f"programs directly, with no shell, so PowerShell cmdlets "
                f"cannot run here."
            ),
            recovery=[
                "DO NOT retry the identical command.",
                f"Use one of: {', '.join(sorted(BUILD_COMMANDS))}.",
                "Use run_powershell for anything that is not a build.",
            ],
        )

    argv = _with_default_verbosity(argv)

    try:
        resolved_directory = resolve_working_directory(working_directory)
    except ValueError as exc:
        return mcp_error(
            "WORKING_DIRECTORY_NOT_ALLOWED",
            "build_project",
            str(exc),
            path=working_directory,
            recovery=[
                "DO NOT retry the identical working_directory.",
                "Ask the user to add the project directory to WAMCP_PROJECT_ROOTS.",
            ],
        )

    timeout_seconds = max(1, min(int(timeout_seconds), 1800))

    # Best-effort: a None here means retention is unavailable (download root
    # unwritable), which must not stop the build. run_process treats None as
    # "do not retain".
    log_path = _build_log_path(resolved_directory)

    try:
        with build_slot(resolved_directory, command):
            result = run_process(
                argv,
                cwd=resolved_directory,
                timeout_seconds=timeout_seconds,
                log_path=log_path,
            )
    except BuildAlreadyRunning as exc:
        return mcp_error(
            "BUILD_ALREADY_RUNNING",
            "build_project",
            str(exc),
            details={
                "running_command": exc.running.command,
                "elapsed_ms": exc.elapsed_ms,
            },
            recovery=[
                "DO NOT start a second build: concurrent msbuild on the same "
                "obj/ and lib/ causes link-lock errors (LNK1163/LNK1104) that "
                "look like source bugs.",
                "Wait for the running build to finish, then call again.",
                "If it is wedged, ask the user to stop it.",
            ],
        )
    except FileNotFoundError:
        return mcp_error(
            "BUILD_TOOL_NOT_FOUND",
            "build_project",
            f"'{argv[0]}' was not found on PATH.",
            recovery=[
                "DO NOT retry the identical command.",
                "Use run_powershell with 'Get-Command <tool>' to check "
                "whether it is installed.",
                "Ask the user to install it or add it to PATH.",
            ],
        )
    except OSError as exc:
        log.exception("build_project could not start %s", argv[0])

        return mcp_error(
            "BUILD_START_FAILED",
            "build_project",
            f"Could not start '{argv[0]}': {exc}",
            recovery=["Do not repeatedly retry the identical command."],
        )

    if result.timed_out:
        details = {
            "status": "timeout",
            "pid": result.pid,
            "elapsed_ms": result.elapsed_ms,
            "timeout_seconds": timeout_seconds,
            "killed": True,
        }

        message = (
            f"'{command}' did not finish within {timeout_seconds} seconds. "
            f"The build process tree was terminated and it is safe to "
            f"retry.\nOutput so far (tail):\n"
            f"{result.combined[-_MAX_ERROR_EXCERPT:]}"
        )

        # Name the retained log in the prose as well as the details, because a
        # caller that only prints the message would otherwise never learn the
        # full output is on disk.
        if result.log_path is not None:
            details["full_log"] = str(result.log_path)
            message = f"{message}\nFull log: {result.log_path}"

        return mcp_error(
            "BUILD_TIMED_OUT",
            "build_project",
            message,
            details=details,
            recovery=[
                "The build process tree was terminated, so no orphaned "
                "msbuild/cl/link is holding obj/ or lib/ locks. A retry is safe.",
                "A cold build of a large project can legitimately exceed "
                "this; raise timeout_seconds (max 1800).",
                "Building a single target is much faster than a full build.",
            ],
        )

    parsed = parse_build_output(result.combined, base_directory=resolved_directory)

    log.info(
        "build_project ran %r in %s: exit=%d errors=%d warnings=%d",
        command,
        resolved_directory,
        result.exit_code,
        parsed.total_errors,
        parsed.total_warnings,
    )

    return format_report(
        parsed,
        header=f"BUILD: {command}",
        exit_code=result.exit_code,
        raw_output=result.combined,
        elapsed_ms=result.elapsed_ms,
        rebuild=parse_rebuild_kind(result.combined),
        full_log=result.log_path,
    )


def _build_log_path(working_directory: Path) -> Path | None:
    """Choose where a build's full output is retained, or None if unavailable.

    The file lives under the download root rather than the project tree, so a
    build never leaves an artefact the caller's repository would track. The
    name carries the directory's hash so a log can be attributed to a project
    at a glance, and a nanosecond stamp so two builds in the same second do
    not clobber each other's log.
    """

    try:
        root = get_download_root()
    except OSError as exc:
        # Retention is a convenience; a build must still run without it.
        log.warning("build log retention unavailable: %s", exc)
        return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha1(
        os.path.normcase(str(working_directory)).encode("utf-8")
    ).hexdigest()[:8]
    unique = time.time_ns()

    return root / "build-logs" / f"{stamp}-{digest}-{unique}.log"


def _with_default_verbosity(argv: list[str]) -> list[str]:
    """Add /v:minimal /nologo to msbuild when the caller did not ask.

    It changes console noise only, not what builds, and the parser throws the
    banners away anyway -- so the saving is in the raw tail quoted on failure,
    which is easier to read without a per-project banner. Only msbuild is
    touched: cmake/ninja have different flags and are already quiet enough.
    """

    if Path(argv[0]).name.lower() not in {"msbuild", "msbuild.exe"}:
        return argv

    lowered = [token.lower() for token in argv[1:]]

    has_verbosity = any(
        token.startswith(("/v:", "/verbosity:")) for token in lowered
    )
    has_nologo = "/nologo" in lowered

    result = list(argv)

    if not has_verbosity:
        result.append("/v:minimal")

    if not has_nologo:
        result.append("/nologo")

    return result
