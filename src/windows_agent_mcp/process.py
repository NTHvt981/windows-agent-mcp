"""Subprocess execution shared by the tools that run external programs."""

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

MAX_PROCESS_OUTPUT_CHARS: int = 64 * 1024

MAX_PROCESS_OUTPUT_TAIL_CHARS: int = 16 * 1024

PROCESS_KILL_TIMEOUT_SECONDS: int = 10


def get_windows_development_environment() -> dict[str, str]:
    """Build a Windows environment with the current machine/user PATH."""

    # Processes inherit PATH at launch and miss later installs.

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

        combined = os.pathsep.join(
            p
            for p in (
                machine_path,
                user_path,
                env.get("PATH", ""),
            )
            if p
        )

        combined = os.path.expandvars(combined)

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
    """Outcome of a bounded subprocess run."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    combined: str
    pid: int | None = None
    elapsed_ms: int = 0
    log_path: Path | None = None


def _truncate(text: str, label: str) -> str:
    """Cap one stream, keeping both ends and saying what was dropped."""

    if len(text) <= MAX_PROCESS_OUTPUT_CHARS:
        return text

    omitted = len(text) - MAX_PROCESS_OUTPUT_CHARS - MAX_PROCESS_OUTPUT_TAIL_CHARS

    if omitted <= 0:
        return f"...[{label} truncated]...\n" + text[-MAX_PROCESS_OUTPUT_CHARS:]

    return (
        text[:MAX_PROCESS_OUTPUT_CHARS]
        + f"\n...[{label} truncated, {omitted} chars omitted]...\n"
        + text[-MAX_PROCESS_OUTPUT_TAIL_CHARS:]
    )


def _write_log(log_path: Path, text: str) -> Path | None:
    """Write the full output, returning the path or None on failure."""

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(text, encoding="utf-8", errors="replace")
        return log_path
    except OSError as exc:
        log.warning("could not write build log %s: %s", log_path, exc)
        return None


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill a build and everything it spawned."""

    # taskkill /T runs before process.kill(): grandchildren hold obj/ locks.

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
    log_path: Path | None = None,
) -> ProcessResult:
    """Run a program directly, capturing bounded output."""

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

        # Kill before draining: grandchildren hold the pipes open.
        _terminate_process_tree(process)

        try:
            stdout, stderr = process.communicate(
                timeout=PROCESS_KILL_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            # Bound the drain: a surviving grandchild can hold the pipe open.
            log.warning(
                "output pipe did not close after killing pid %s",
                process.pid,
            )
            stdout, stderr = "", ""

    elapsed_ms = int((time.monotonic() - started) * 1000)

    stdout = stdout or ""
    stderr = stderr or ""

    full_combined = stdout + "\n" + stderr

    exit_code = -1

    if not timed_out and process.returncode is not None:
        exit_code = process.returncode

    kept_log: Path | None = None

    if log_path is not None and (
        timed_out or len(full_combined) > MAX_PROCESS_OUTPUT_CHARS
    ):
        kept_log = _write_log(log_path, full_combined)

    return ProcessResult(
        stdout=_truncate(stdout, "stdout"),
        stderr=_truncate(stderr, "stderr"),
        exit_code=exit_code,
        timed_out=timed_out,
        combined=_truncate(full_combined, "output"),
        pid=process.pid,
        elapsed_ms=elapsed_ms,
        log_path=kept_log,
    )
