"""Subprocess execution shared by the tools that run external programs.

run_powershell, build_project and compile_shader all need the same two
things: a Windows environment whose PATH reflects reality, and a bounded
capture of a child process's output. Both live here so the three tools cannot
drift apart on encoding, timeout or truncation behaviour.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from .log import log

__all__: list[str] = [
    "MAX_PROCESS_OUTPUT_CHARS",
    "MAX_PROCESS_OUTPUT_TAIL_CHARS",
    "PROCESS_KILL_TIMEOUT_SECONDS",
    "ProcessResult",
    "get_windows_development_environment",
    "run_process",
]

# Per-stream cap on captured output. Build logs are unbounded; this keeps one
# noisy compile from filling the context window.
MAX_PROCESS_OUTPUT_CHARS: int = 64 * 1024

# Budget for the tail kept when a stream exceeds MAX_PROCESS_OUTPUT_CHARS.
# A build's last lines are the part that says why it failed or stopped, and
# for a timed-out build the tail is the whole point -- so the cap keeps both
# ends and drops the middle rather than keeping only the head.
MAX_PROCESS_OUTPUT_TAIL_CHARS: int = 16 * 1024

# Bound on the taskkill call itself, so a wedged kill cannot hang the tool.
PROCESS_KILL_TIMEOUT_SECONDS: int = 10


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
        pid: Child process ID, or None when the process never started. Kept
            so a timed-out build can be reported back with the identity of
            what was killed.
        elapsed_ms: Wall-clock duration of the run in milliseconds, measured
            around communicate(). Reported so a caller can tell a build that
            finished in two seconds from one that ran for ten minutes.
    """

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    combined: str
    pid: int | None = None
    elapsed_ms: int = 0


def _truncate(text: str, label: str) -> str:
    """Cap one stream, keeping both ends and saying what was dropped.

    A build's actionable content is split across the two ends: the first lines
    name the command and the last lines say why it stopped. Dropping only the
    middle keeps both, so neither a head-only nor a tail-only capture can hide
    the reason a build failed.
    """

    if len(text) <= MAX_PROCESS_OUTPUT_CHARS:
        return text

    omitted = len(text) - MAX_PROCESS_OUTPUT_CHARS - MAX_PROCESS_OUTPUT_TAIL_CHARS

    if omitted <= 0:
        # Only just over the limit: keep the tail, which is the actionable end.
        return f"...[{label} truncated]...\n" + text[-MAX_PROCESS_OUTPUT_CHARS:]

    return (
        text[:MAX_PROCESS_OUTPUT_CHARS]
        + f"\n...[{label} truncated, {omitted} chars omitted]...\n"
        + text[-MAX_PROCESS_OUTPUT_TAIL_CHARS:]
    )


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill a build and everything it spawned.

    On Windows process.kill() == TerminateProcess on the direct child only.
    msbuild's cl.exe and link.exe grandchildren survive that and keep writing
    obj/ and .lib files, which is what produced the LNK1163/LNK1104 lock
    errors a retry then misread as a source bug. taskkill /T walks the parent
    tree, so it must run BEFORE the direct child is killed.
    """

    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            timeout=PROCESS_KILL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("taskkill failed for pid %s: %s", process.pid, exc)

    try:
        process.kill()
    except OSError:  # already gone
        pass


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

    started = time.monotonic()

    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=get_windows_development_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    timed_out = False

    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True

        # Kill the tree before draining: the final communicate() blocks until
        # the pipes close, and the grandchildren holding them open are exactly
        # what taskkill is for.
        _terminate_process_tree(process)

        try:
            stdout, stderr = process.communicate(
                timeout=PROCESS_KILL_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            # A grandchild that survived the kill -- taskkill failed, or it
            # detached -- can hold the pipe open indefinitely. Bound the drain
            # rather than hang the tool on a build that is already killed.
            log.warning(
                "output pipe did not close after killing pid %s",
                process.pid,
            )
            stdout, stderr = "", ""

    elapsed_ms = int((time.monotonic() - started) * 1000)

    # communicate() returns None on a stream that was not captured, and the
    # caller's parsers assume strings, so the default is applied here.
    stdout = stdout or ""
    stderr = stderr or ""

    # A timed-out process has no meaningful exit status, so it is reported as
    # -1 rather than whatever TerminateProcess happened to leave behind.
    exit_code = -1

    if not timed_out and process.returncode is not None:
        exit_code = process.returncode

    return ProcessResult(
        stdout=_truncate(stdout, "stdout"),
        stderr=_truncate(stderr, "stderr"),
        exit_code=exit_code,
        timed_out=timed_out,
        combined=_truncate(stdout + "\n" + stderr, "output"),
        pid=process.pid,
        elapsed_ms=elapsed_ms,
    )
