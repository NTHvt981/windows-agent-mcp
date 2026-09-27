from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

from ..allowed_command import (
    ALLOWED_COMMANDS,
    find_cmdlet_destinations,
    find_command_composition,
    find_inline_code_flag,
    is_command_allowed,
)
from ..error import mcp_error
from ..log import log
from ..process import get_windows_development_environment
from ..utils import (
    POWERSHELL_TIMEOUT_SECONDS,
    PROJECT_ROOTS_ENV_VAR,
    ProtectedPathError,
    resolve_working_directory,
    resolve_write_path,
)

# Every entry is \b-anchored on both sides.
DANGEROUS_PATTERNS: list[str] = [
    r"\b(cmd\.exe|powershell\.exe)\s",
    r"\bStart-Sleep\b",
    r"\bInvoke-Expression\b",
    r"\bNew-Process\b",
    r"\bRemove-Item\s+.*\.\d{4}\b",
    r"\bnet\s+-[dl]\s+",
    r"\bxargs\b.*\brmdir\b",
    r"\brmdir\b",
    r"\brm\s+-rf\b",
    r"\brm\b",
    r"\bremove-item\b",
    r"\bdel\b",
]


def looks_like_base64_payload(value: str) -> bool:
    """Return True only when a token strongly resembles a Base64 payload."""

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

    suspicious_markers = (
        b"powershell",
        b"cmd.exe",
        b"invoke-",
        b"start-process",
        b"iex ",
        b"downloadstring",
    )

    # PowerShell -EncodedCommand arrives NUL-interleaved (UTF-16-LE).
    lowered = decoded.replace(b"\x00", b"").lower()

    return any(marker in lowered for marker in suspicious_markers)


# Scan runs: splitting on separators would strip padding.
_BASE64_RUN_PATTERN = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


def command_contains_base64_payload(command: str) -> str | None:
    """Scan a command for an embedded encoded shell payload."""

    for candidate in _BASE64_RUN_PATTERN.findall(command):
        if looks_like_base64_payload(candidate):
            return candidate

    return None


def validate_powershell_command(command: str) -> None:
    """Validate a development PowerShell command."""

    if len(command) > 4000:
        raise ValueError("Command is too long.")

    # Single statement only, or checks below inspect just the first.
    composition = find_command_composition(command)

    if composition is not None:
        raise ValueError(
            f"Command blocked: it runs more than one command, or redirects "
            f"output ({composition}). Run one command per call. If the "
            f"character is meant as literal text, quote the argument "
            f"containing it."
        )

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
    """Run a restricted Windows PowerShell development command."""

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
                "Ask the user to add the directory to WAMCP_PROJECT_ROOTS "
                "if the command needs to run there.",
            ],
        )

    # Destinations escape cwd confinement; resolve them as writes.
    destinations = find_cmdlet_destinations(command)

    if destinations is not None and not destinations:
        return mcp_error(
            "WRITE_PATH_NOT_ALLOWED",
            "run_powershell",
            "File-creation cmdlet without an identifiable destination. "
            "Name the file to create, copy, or move explicitly.",
            recovery=[
                "DO NOT retry the identical command.",
                "Pass -Path (New-Item) or -Destination (Copy/Move-Item), "
                "or give the destination positionally.",
            ],
        )

    for destination in destinations or []:
        candidate = Path(destination)

        if not candidate.is_absolute():
            candidate = resolved_directory / candidate

        try:
            resolve_write_path(str(candidate))
        except ProtectedPathError as exc:
            return mcp_error(
                "PROTECTED_PATH",
                "run_powershell",
                str(exc),
                path=destination,
                recovery=[
                    "DO NOT retry, and do not try another directory: the "
                    "refusal is by filename, not by location.",
                    "Only the operator may change this file. Say what you "
                    "needed it for and let the user decide.",
                ],
            )
        except ValueError as exc:
            return mcp_error(
                "WRITE_PATH_NOT_ALLOWED",
                "run_powershell",
                str(exc),
                path=destination,
                recovery=[
                    "DO NOT retry the identical destination.",
                    "Create, copy, or move inside a directory the operator "
                    "has approved.",
                    f"Ask the user to add the project directory to "
                    f"{PROJECT_ROOTS_ENV_VAR} if it is missing.",
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
                # No -ExecutionPolicy: it governs script files, not -Command.
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
