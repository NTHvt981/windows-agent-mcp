"""Subprocess execution shared by the tools that run external programs.

run_powershell, build_project and compile_shader all need the same two
things: a Windows environment whose PATH reflects reality, and a bounded
capture of a child process's output. Both live here so the three tools cannot
drift apart on encoding, timeout or truncation behaviour.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

from .log import log

__all__: list[str] = [
    "MAX_PROCESS_OUTPUT_CHARS",
    "ProcessResult",
    "get_windows_development_environment",
    "run_process",
]

# Per-stream cap on captured output. Build logs are unbounded; this keeps one
# noisy compile from filling the context window.
MAX_PROCESS_OUTPUT_CHARS: int = 64 * 1024


def get_windows_development_environment() -> dict[str, str]:
    """Build a Windows environment using the current process environment plus the current machine/user PATH.

    This matters because a process inherits PATH at launch and never sees
    later changes: a Vulkan SDK or compiler installed after the server
    started would otherwise be invisible to every tool that looks for it.

    Returns:
        Environment dictionary with merged and normalized PATH.

    Raises:
        Exception: If unable to reconstruct Windows environment (logged but not raised).
    """

    env = os.environ.copy()

    try:
        import winreg

        machine_path = ""
        user_path = ""

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as key:
            machine_path, _ = winreg.QueryValueEx(key, "Path")

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
        ) as key:
            user_path, _ = winreg.QueryValueEx(key, "Path")

        # Windows normally combines these paths for processes.
        combined = os.pathsep.join(
            p
            for p in (
                machine_path,
                user_path,
                env.get("PATH", ""),
            )
            if p
        )

        # Expand variables such as:
        # %SystemRoot%
        # %ProgramFiles%
        # %USERPROFILE%
        combined = os.path.expandvars(combined)

        # Normalize entries and remove duplicates.
        normalized: list[str] = []
        seen: set[str] = set()

        for entry in combined.split(os.pathsep):
            entry = entry.strip().strip('"')

            if not entry:
                continue

            key = os.path.normcase(os.path.normpath(entry))

            if key not in seen:
                seen.add(key)
                normalized.append(entry)

        env["PATH"] = os.pathsep.join(normalized)

    except Exception as exc:
        log.warning(
            "Could not reconstruct Windows PATH: %s",
            exc,
        )

    return env


class ProcessResult(NamedTuple):
    """Outcome of a bounded subprocess run.

    Attributes:
        stdout: Captured stdout, truncated to MAX_PROCESS_OUTPUT_CHARS.
        stderr: Captured stderr, truncated the same way.
        exit_code: Process exit status. -1 when it timed out.
        timed_out: True if the timeout fired.
        combined: stdout and stderr joined, which is what the diagnostic
            parsers want -- compilers split messages across both streams and
            which one they use varies by tool and by platform.
    """

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    combined: str


def _truncate(text: str, label: str) -> str:
    """Cap one stream, saying so rather than silently cutting."""

    if len(text) <= MAX_PROCESS_OUTPUT_CHARS:
        return text

    return text[:MAX_PROCESS_OUTPUT_CHARS] + f"\n...[{label} truncated]..."


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
) -> ProcessResult:
    """Run a program directly, capturing bounded output.

    No shell is involved: argv is passed through, so quoting and metacharacters
    are not reinterpreted by a command processor. Callers that accept a
    command string must validate it before splitting it into argv.

    Args:
        argv: Program and arguments.
        cwd: Working directory, already validated by the caller.
        timeout_seconds: Wall-clock limit.

    Returns:
        The captured result. A timeout is reported, not raised, so the caller
        can return a normal tool result.

    Raises:
        OSError: If the program cannot be started at all.
    """

    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=get_windows_development_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        # Output produced before the timeout is the useful part -- it names
        # the file the compiler was stuck on.
        stdout = _decode_partial(expired.stdout)
        stderr = _decode_partial(expired.stderr)

        return ProcessResult(
            stdout=_truncate(stdout, "stdout"),
            stderr=_truncate(stderr, "stderr"),
            exit_code=-1,
            timed_out=True,
            combined=_truncate(stdout + "\n" + stderr, "output"),
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    return ProcessResult(
        stdout=_truncate(stdout, "stdout"),
        stderr=_truncate(stderr, "stderr"),
        exit_code=completed.returncode,
        timed_out=False,
        combined=_truncate(stdout + "\n" + stderr, "output"),
    )


def _decode_partial(value: str | bytes | None) -> str:
    """Normalise TimeoutExpired's output, which may be bytes or str.

    subprocess.run(text=True) usually gives str here, but the attribute is
    typed as bytes and is bytes in some paths, so both are handled rather
    than assumed.
    """

    if value is None:
        return ""

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    return value
