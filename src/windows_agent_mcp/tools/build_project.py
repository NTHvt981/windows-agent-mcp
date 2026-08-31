"""Run a build and report structured diagnostics instead of a raw log."""

from __future__ import annotations

from pathlib import Path

from ..allowed_command import BUILD_COMMANDS, tokenize_command
from ..diagnostics import format_report, parse_build_output
from ..error import mcp_error
from ..log import log
from ..process import run_process
from ..utils import BUILD_TIMEOUT_SECONDS, resolve_working_directory
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

    try:
        result = run_process(
            argv,
            cwd=resolved_directory,
            timeout_seconds=timeout_seconds,
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
        return mcp_error(
            "BUILD_TIMED_OUT",
            "build_project",
            (
                f"'{command}' did not finish within {timeout_seconds} "
                f"seconds. Output so far:\n"
                f"{result.combined[-_MAX_ERROR_EXCERPT:]}"
            ),
            recovery=[
                "Do not immediately retry the identical command.",
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
    )
