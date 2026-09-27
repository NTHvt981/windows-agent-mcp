"""One build at a time per working directory."""

# A second build on one obj/ tree fails like a source error.

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
    """A build that currently holds its directory's single slot."""

    command: str
    started: float


class BuildAlreadyRunning(Exception):
    """Raised when a build slot for a directory is already held."""

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
    # Keyed by directory: two solutions in one dir false-positive safe.
    return os.path.normcase(os.path.normpath(str(working_directory)))


@contextmanager
def build_slot(
    working_directory: Path, command: str
) -> Generator[RunningBuild, None, None]:
    """Reserve the single slot for a directory or raise BuildAlreadyRunning."""

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
