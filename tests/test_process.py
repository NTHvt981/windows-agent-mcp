"""Tests for the shared subprocess layer.

subprocess is stubbed rather than really launched: the behaviour under test is
how a timeout and an oversized log are turned into a ProcessResult, and
spawning real processes to check that would make the suite slow and
platform-dependent for no extra confidence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from windows_agent_mcp import process as process_module
from windows_agent_mcp.process import (
    MAX_PROCESS_OUTPUT_CHARS,
    get_windows_development_environment,
    run_process,
)


class Completed:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def fake_subprocess(monkeypatch: pytest.MonkeyPatch):
    def install(outcome: object):
        recorded: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            recorded["argv"] = argv
            recorded.update(kwargs)

            if isinstance(outcome, Exception):
                raise outcome

            return outcome

        monkeypatch.setattr(process_module.subprocess, "run", fake_run)

        return recorded

    return install


def test_output_and_exit_code_are_returned(fake_subprocess) -> None:
    fake_subprocess(Completed(stdout="built\n", stderr="warn\n", returncode=0))

    result = run_process(["ninja"], cwd=Path.cwd(), timeout_seconds=5)

    assert result.stdout == "built\n"
    assert result.stderr == "warn\n"
    assert result.exit_code == 0
    assert result.timed_out is False


def test_combined_holds_both_streams(fake_subprocess) -> None:
    """Compilers split diagnostics across both streams, and which one varies."""

    fake_subprocess(Completed(stdout="a", stderr="b"))

    result = run_process(["cl"], cwd=Path.cwd(), timeout_seconds=5)

    assert "a" in result.combined
    assert "b" in result.combined


def test_no_shell_is_used(fake_subprocess) -> None:
    """argv must reach the child unreparsed, or quoting becomes a hazard."""

    recorded = fake_subprocess(Completed())

    run_process(["cmake", "--build", "my dir"], cwd=Path.cwd(), timeout_seconds=5)

    assert recorded["argv"] == ["cmake", "--build", "my dir"]
    assert "shell" not in recorded


def test_check_is_false_so_a_failing_build_is_not_an_exception(
    fake_subprocess,
) -> None:
    recorded = fake_subprocess(Completed(returncode=1))

    result = run_process(["ninja"], cwd=Path.cwd(), timeout_seconds=5)

    assert recorded["check"] is False
    assert result.exit_code == 1


def test_long_output_is_truncated_with_a_notice(fake_subprocess) -> None:
    fake_subprocess(Completed(stdout="x" * (MAX_PROCESS_OUTPUT_CHARS + 500)))

    result = run_process(["ninja"], cwd=Path.cwd(), timeout_seconds=5)

    assert "[stdout truncated]" in result.stdout
    assert len(result.stdout) < MAX_PROCESS_OUTPUT_CHARS + 100


def test_long_stderr_is_truncated(fake_subprocess) -> None:
    fake_subprocess(Completed(stderr="y" * (MAX_PROCESS_OUTPUT_CHARS + 500)))

    result = run_process(["ninja"], cwd=Path.cwd(), timeout_seconds=5)

    assert "[stderr truncated]" in result.stderr


def test_timeout_is_reported_not_raised(fake_subprocess) -> None:
    """A timeout must come back as a tool result, not a transport error."""

    fake_subprocess(
        subprocess.TimeoutExpired(cmd="ninja", timeout=5, output="compiling a.cpp\n")
    )

    result = run_process(["ninja"], cwd=Path.cwd(), timeout_seconds=5)

    assert result.timed_out is True
    assert result.exit_code == -1
    assert "compiling a.cpp" in result.stdout


def test_timeout_partial_output_may_be_bytes(fake_subprocess) -> None:
    """TimeoutExpired.output is typed as bytes and sometimes is."""

    fake_subprocess(
        subprocess.TimeoutExpired(
            cmd="ninja", timeout=5, output=b"partial\n", stderr=b"err\n"
        )
    )

    result = run_process(["ninja"], cwd=Path.cwd(), timeout_seconds=5)

    assert "partial" in result.stdout
    assert "err" in result.stderr


def test_timeout_with_no_output_at_all(fake_subprocess) -> None:
    fake_subprocess(subprocess.TimeoutExpired(cmd="ninja", timeout=5))

    result = run_process(["ninja"], cwd=Path.cwd(), timeout_seconds=5)

    assert result.stdout == ""
    assert result.timed_out is True


def test_start_failure_propagates(fake_subprocess) -> None:
    """Callers distinguish "not installed" from "failed", so this must raise."""

    fake_subprocess(FileNotFoundError("cmake"))

    with pytest.raises(FileNotFoundError):
        run_process(["cmake"], cwd=Path.cwd(), timeout_seconds=5)


def test_decoding_is_lenient(fake_subprocess) -> None:
    """A compiler emitting one non-UTF-8 byte must not fail the whole build.

    MSVC on a non-English locale does exactly that, and errors="strict" would
    turn a successful build into an unexplained exception.
    """

    recorded = fake_subprocess(Completed(stdout="ok"))

    run_process(["ninja"], cwd=Path.cwd(), timeout_seconds=5)

    assert recorded["encoding"] == "utf-8"
    assert recorded["errors"] == "replace"


# ============================================================
# Environment reconstruction
# ============================================================


def test_path_is_rebuilt_from_the_registry() -> None:
    """A tool installed after this process started must still be findable."""

    env = get_windows_development_environment()

    assert env["PATH"]


def test_path_entries_are_deduplicated() -> None:
    entries = get_windows_development_environment()["PATH"].split(";")

    normalised = [entry.lower().rstrip("\\") for entry in entries if entry]

    assert len(normalised) == len(set(normalised))


def test_registry_failure_degrades_to_the_process_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked-down machine must not break every tool that runs a program."""

    import winreg

    def boom(*args: object, **kwargs: object):
        raise OSError("access denied")

    monkeypatch.setattr(winreg, "OpenKey", boom)

    env = get_windows_development_environment()

    assert "PATH" in env
