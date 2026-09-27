"""One build at a time per working directory.

A second msbuild against the same obj/ and lib/ does not fail cleanly: it
produces LNK1163/LNK1104 lock contention that reads exactly like a source
error, and the model then edits correct code. So a build in flight is
recorded, and a second build for the same directory is refused rather than
queued -- queueing would make an MCP call block for minutes with no
feedback, which is its own failure.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

__all__ = ["BuildAlreadyRunning", "RunningBuild", "build_slot", "current_build"]


class RunningBuild(NamedTuple):
    """A build that currently holds its directory's single slot.

    Args:
        command: The caller's command string, kept verbatim so a refusal can
            name what the user asked for rather than an internal argv.
        started: ``time.monotonic()`` when the slot was taken. Monotonic, not
            wall-clock, so elapsed time is immune to a clock adjustment.
    """

    command: str
    started: float


class BuildAlreadyRunning(Exception):
    """Raised when a build slot for a directory is already held.

    Carries the running build and the elapsed milliseconds so the caller can
    report what is in flight without a second lookup.
    """

    def __init__(self, running: RunningBuild, working_directory: Path) -> None:
        self.running = running
        self.working_directory = working_directory
        self.elapsed_ms = int((time.monotonic() - running.started) * 1000)

        super().__init__(
            f"A build is already running in {working_directory}: "
            f"'{running.command}' ({self.elapsed_ms / 1000:.0f}s elapsed)."
        )


_lock = threading.Lock()
_running: dict[str, RunningBuild] = {}


def _key(working_directory: Path) -> str:
    # The key is the resolved working directory, not the solution path: parsing
    # which solution a command names is tool-specific and brittle, and the
    # shared resource that collides is the directory's obj/ + libs. Two
    # unrelated solutions in one directory therefore false-positive -- the safe
    # direction, since a refused build is cheaper than a corrupt one.
    return os.path.normcase(os.path.normpath(str(working_directory)))


@contextmanager
def build_slot(
    working_directory: Path, command: str
) -> Generator[RunningBuild, None, None]:
    """Reserve the single slot for a directory or raise BuildAlreadyRunning.

    The slot is released on every exit path -- normal return or an exception
    from the body.
    """

    key = _key(working_directory)

    with _lock:
        existing = _running.get(key)

        if existing is not None:
            raise BuildAlreadyRunning(existing, working_directory)

        slot = RunningBuild(command=command, started=time.monotonic())
        _running[key] = slot

    try:
        yield slot
    finally:
        with _lock:
            _running.pop(key, None)


def current_build(working_directory: Path) -> RunningBuild | None:
    """Return the build holding the directory's slot, or None if free."""

    with _lock:
        return _running.get(_key(working_directory))
