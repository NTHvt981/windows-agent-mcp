"""PowerShell tool for Windows Agent MCP Server."""

from __future__ import annotations

import base64
import re
import subprocess

from ..allowed_command import (
    ALLOWED_COMMANDS,
    find_command_composition,
    find_inline_code_flag,
    is_command_allowed,
)
from ..error import mcp_error
from ..log import log
from ..process import get_windows_development_environment
from ..utils import POWERSHELL_TIMEOUT_SECONDS, resolve_working_directory

# PowerShell syntax patterns that we refuse completely.
#
# Every entry MUST be anchored with \b on BOTH sides of the literal.
# A pattern like r"rm\b" (leading boundary omitted) matches the tail of any
# word ending in "rm" -- it rejected "git commit -m 'confirm fix'",
# "cmake --target platform", "git log --grep=term" and similar ordinary
# development commands.
DANGEROUS_PATTERNS: list[str] = [
    r"\b(cmd\.exe|powershell\.exe)\s",  # Shell spawn
    r"\bStart-Sleep\b",  # Sleep/DoS
    r"\bInvoke-Expression\b",  # Dynamic execution
    r"\bNew-Process\b",  # Process creation
    r"\bRemove-Item\s+.*\.\d{4}\b",  # Delete files by number
    r"\bnet\s+-[dl]\s+",  # Network commands
    r"\bxargs\b.*\brmdir\b",
    r"\brmdir\b",
    r"\brm\s+-rf\b",
    r"\brm\b",
    r"\bremove-item\b",
    r"\bdel\b",
]


def looks_like_base64_payload(value: str) -> bool:
    """Return True only when a token strongly resembles a Base64 payload.

    Expects a SINGLE token, with its original case intact. Passing a whole
    command line here can never match, because the fullmatch below rejects
    any string containing whitespace; lowercasing first also corrupts the
    Base64 alphabet so the decode would fail. Use
    command_contains_base64_payload() to scan a full command.

    Args:
        value: Single token to check for base64 payload characteristics.

    Returns:
        True if the token appears to be a base64-encoded payload, False otherwise.
    """

    value = value.strip()

    if len(value) < 24:
        return False

    if len(value) % 4 not in (0,):
        return False

    if not re.fullmatch(
        r"[A-Za-z0-9+/]+={0,2}",
        value,
    ):
        return False

    try:
        decoded = base64.b64decode(
            value,
            validate=True,
        )
    except Exception:
        return False

    # Encoded payloads containing shell/script text are substantially more suspicious.
    suspicious_markers = (
        b"powershell",
        b"cmd.exe",
        b"invoke-",
        b"start-process",
        b"iex ",
        b"downloadstring",
    )

    # PowerShell's own -EncodedCommand expects UTF-16-LE, so decoded ASCII
    # text arrives NUL-interleaved ("p\x00o\x00w\x00..."). Stripping NULs
    # normalizes both UTF-8 and UTF-16-LE payloads onto the same markers.
    lowered = decoded.replace(b"\x00", b"").lower()

    return any(marker in lowered for marker in suspicious_markers)


# Maximal runs of the Base64 alphabet, plus any padding.
#
# Deliberately NOT a token split on separators: PowerShell glues payloads to
# a switch (-EncodedCommand=VALUE, -e:VALUE) or wraps them in quotes, and
# treating "=" as a separator would strip the padding and break the
# length-modulo-4 test in looks_like_base64_payload(). Scanning for runs
# finds the payload wherever it is embedded.
_BASE64_RUN_PATTERN = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


def command_contains_base64_payload(command: str) -> str | None:
    """Scan a command for an embedded encoded shell payload.

    Case is preserved, because Base64 is case-sensitive.

    Args:
        command: Full PowerShell command line, in its original case.

    Returns:
        The offending substring if one looks like an encoded payload, else None.
    """

    for candidate in _BASE64_RUN_PATTERN.findall(command):
        if looks_like_base64_payload(candidate):
            return candidate

    return None


def validate_powershell_command(command: str) -> None:
    """Validate a development PowerShell command.

    This is intentionally conservative. Checks run cheapest-first, and the
    composition check comes before the allowlist: the allowlist names a single
    executable, so it is only meaningful once the string is known to hold a
    single command.

    Args:
        command: PowerShell command to validate.

    Raises:
        ValueError: If the command is too long, composes more than one command,
                    matches a dangerous pattern, is not allowlisted, hands code
                    to an interpreter inline, or carries an encoded payload.
    """

    if len(command) > 4000:
        raise ValueError("Command is too long.")

    # Must be a single statement, or everything below inspects only the first
    # of several commands.
    composition = find_command_composition(command)

    if composition is not None:
        raise ValueError(
            f"Command blocked: it runs more than one command, or redirects "
            f"output ({composition}). Run one command per call. If the "
            f"character is meant as literal text, quote the argument "
            f"containing it."
        )

    # re.IGNORECASE already handles case, so the command is matched as-is
    # rather than pre-lowercased.
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            raise ValueError(
                f"Command blocked because it contains dangerous patterns: {pattern}"
            )

    if not is_command_allowed(command):
        raise ValueError(
            f"Command is not in the development allowlist: {sorted(ALLOWED_COMMANDS)}"
        )

    inline_code = find_inline_code_flag(command)

    if inline_code is not None:
        raise ValueError(
            f"Command blocked: {inline_code}. Allowlisting an interpreter does "
            f"not allow arbitrary inline code. Run a script file instead."
        )

    encoded = command_contains_base64_payload(command)

    if encoded is not None:
        raise ValueError(
            f"Command contains what looks like a base64-encoded "
            f"payload: {encoded[:32]}..."
        )


def run_powershell(
    command: str,
    timeout_seconds: int = POWERSHELL_TIMEOUT_SECONDS,
    working_directory: str | None = None,
) -> str:
    """Run a restricted Windows PowerShell development command.

    Examples:

        Get-Command premake5

        Get-ChildItem

        premake5 --version

        cmake --version

        ninja --version

        git status

        Get-Content C:\\project\\README.md

    Exactly ONE command runs per call. Separators, pipelines, redirections and
    subexpressions (`;` `|` `&` `>` `$()`) are rejected outside quotes, so the
    allowlist cannot be sidestepped by appending a second command. Inside
    quotes they are ordinary text, so `git commit -m "fix; cleanup"` is fine.

    IMPORTANT:
        This is NOT a security sandbox. It constrains WHICH PROGRAM starts,
        not what that program then does: an allowlisted interpreter given a
        script file (`python build.py`) runs whatever that file contains, and
        `npx`/`pip` fetch and execute third-party packages. For untrusted
        input, isolate at the OS level.

    Args:
        command: PowerShell command to execute. Must be in the allowlist.
        timeout_seconds: Execution timeout in seconds (1-600). Defaults to 300.
        working_directory: Directory to run in. Must be inside the download
            root or a root listed in BIONIC_PROJECT_ROOTS. Defaults to the
            download root. Each call is a separate process, so `cd` does not
            persist between calls -- pass this instead.

    Returns:
        Formatted output string with stdout, stderr, and exit code, or a
        structured JSON error. This tool never raises: a rejected command
        comes back as a COMMAND_NOT_ALLOWED error so the caller sees a
        normal tool result rather than a transport-level failure.

    Example:
        >>> run_powershell("python --version")
        '--- STDOUT ---\\nPython 3.12.0\\n--- EXIT CODE: 0 ---',
    """

    try:
        validate_powershell_command(command)
    except ValueError as exc:
        return mcp_error(
            "COMMAND_NOT_ALLOWED",
            "run_powershell",
            str(exc),
            recovery=[
                "DO NOT retry the identical command.",
                "Run exactly one allowlisted executable per call: no ';', "
                "'|', '&', '>' or '$()' outside quotes.",
                "If such a character is literal text, quote the argument "
                "containing it.",
                "Interpreters cannot be given code inline (no 'python -c'); "
                "run a script file instead.",
                "If the command genuinely needs a tool that is not "
                "allowlisted, ask the user to run it in their own shell.",
            ],
        )

    try:
        resolved_directory = resolve_working_directory(working_directory)
    except ValueError as exc:
        return mcp_error(
            "WORKING_DIRECTORY_NOT_ALLOWED",
            "run_powershell",
            str(exc),
            path=working_directory,
            recovery=[
                "DO NOT retry the identical working_directory.",
                "Omit working_directory to run in the default download root.",
                "Ask the user to add the directory to BIONIC_PROJECT_ROOTS "
                "if the command needs to run there.",
            ],
        )

    try:
        timeout_seconds = max(
            1,
            min(int(timeout_seconds), 600),
        )

        pc_env = get_windows_development_environment()

        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                # No -ExecutionPolicy: it governs script files, and this
                # only ever runs -Command. The previous value
                # ("NoProfile") was not a valid policy name at all.
                "-Command",
                command,
            ],
            cwd=resolved_directory,
            env=pc_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        # Keep giant build logs from flooding context.
        max_output = 64 * 1024

        if len(stdout) > max_output:
            stdout = stdout[:max_output] + "\n...[stdout truncated]..."

        if len(stderr) > max_output:
            stderr = stderr[:max_output] + "\n...[stderr truncated]..."

        response: list[str] = []

        if stdout:
            response.append("--- STDOUT ---\n" + stdout)

        if stderr:
            response.append("--- STDERR ---\n" + stderr)

        response.append(f"--- EXIT CODE: {result.returncode} ---")

        return "\n\n".join(response)

    except subprocess.TimeoutExpired:
        return mcp_error(
            "COMMAND_TIMEOUT",
            "run_powershell",
            f"Command exceeded {timeout_seconds} seconds and was killed.",
            recovery=[
                "Do not retry with the same timeout.",
                "Narrow the command so it does less work, or pass a larger "
                "timeout_seconds (maximum 600).",
            ],
        )

    except OSError as exc:
        log.exception("run_powershell failed to launch")

        return mcp_error(
            "POWERSHELL_LAUNCH_FAILED",
            "run_powershell",
            f"Could not run powershell.exe: {type(exc).__name__}: {exc}",
            recovery=[
                "Do not repeatedly retry the identical operation.",
                "Verify that powershell.exe is present on PATH.",
            ],
        )
