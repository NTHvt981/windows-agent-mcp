"""Tests for the run_powershell execution body.

subprocess.run is stubbed, so the argv construction, output formatting,
truncation and failure paths are exercised without launching PowerShell.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import assert_error_response

from windows_agent_mcp.tools.run_powershell import (
    get_windows_development_environment,
    run_powershell,
)


class FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Stub subprocess.run and record the call it received."""

    monkeypatch.setenv("BIONIC_DOWNLOAD_ROOT", str(tmp_path / "dl"))

    calls: list[dict[str, Any]] = []

    def install(result: FakeCompleted | Exception) -> list[dict[str, Any]]:
        def fake(args: Any, **kwargs: Any):
            calls.append({"args": args, **kwargs})

            if isinstance(result, Exception):
                raise result

            return result

        monkeypatch.setattr(subprocess, "run", fake)
        return calls

    return install


# ============================================================
# Output formatting
# ============================================================


def test_stdout_only(fake_run) -> None:
    fake_run(FakeCompleted(stdout="Python 3.13.7\n"))

    result = run_powershell("python --version")

    assert result == "--- STDOUT ---\nPython 3.13.7\n\n\n--- EXIT CODE: 0 ---"


def test_stderr_only(fake_run) -> None:
    fake_run(FakeCompleted(stderr="fatal: not a repository\n", returncode=128))

    result = run_powershell("git status")

    assert "--- STDERR ---" in result
    assert "not a repository" in result
    assert "--- EXIT CODE: 128 ---" in result
    assert "--- STDOUT ---" not in result


def test_both_streams(fake_run) -> None:
    fake_run(FakeCompleted(stdout="out", stderr="err", returncode=1))

    result = run_powershell("cmake --build .")

    assert result.index("--- STDOUT ---") < result.index("--- STDERR ---")
    assert "--- EXIT CODE: 1 ---" in result


def test_no_output_still_reports_the_exit_code(fake_run) -> None:
    """A silent success must not come back as an empty string."""

    fake_run(FakeCompleted())

    assert run_powershell("git status") == "--- EXIT CODE: 0 ---"


def test_none_streams_are_tolerated(fake_run) -> None:
    """subprocess can hand back None rather than an empty string."""

    completed = FakeCompleted()
    completed.stdout = None  # type: ignore[assignment]
    completed.stderr = None  # type: ignore[assignment]

    fake_run(completed)

    assert run_powershell("git status") == "--- EXIT CODE: 0 ---"


def test_nonzero_exit_is_not_an_error_envelope(fake_run) -> None:
    """A failing build is a result, not a tool failure."""

    fake_run(FakeCompleted(stderr="error C2065", returncode=2))

    result = run_powershell("msbuild App.sln")

    assert "--- EXIT CODE: 2 ---" in result
    assert '"ok": false' not in result


# ============================================================
# Truncation
# ============================================================


def test_long_stdout_is_truncated(fake_run) -> None:
    fake_run(FakeCompleted(stdout="x" * (70 * 1024)))

    result = run_powershell("cmake --build .")

    assert "...[stdout truncated]..." in result
    assert len(result) < 70 * 1024


def test_long_stderr_is_truncated(fake_run) -> None:
    fake_run(FakeCompleted(stderr="y" * (70 * 1024), returncode=1))

    result = run_powershell("cmake --build .")

    assert "...[stderr truncated]..." in result


def test_output_just_under_the_cap_is_untouched(fake_run) -> None:
    body = "z" * (64 * 1024)

    fake_run(FakeCompleted(stdout=body))

    result = run_powershell("cmake --build .")

    assert "truncated" not in result
    assert body in result


# ============================================================
# How PowerShell is invoked
# ============================================================


def test_argv_is_non_interactive_and_profile_free(fake_run) -> None:
    calls = fake_run(FakeCompleted())

    run_powershell("git status")

    args = calls[0]["args"]

    assert args[0] == "powershell.exe"
    assert "-NoLogo" in args
    assert "-NoProfile" in args
    assert "-NonInteractive" in args
    assert args[-2:] == ["-Command", "git status"]


def test_no_execution_policy_argument(fake_run) -> None:
    """It governs script files only, and the previous value was not valid."""

    calls = fake_run(FakeCompleted())

    run_powershell("git status")

    assert "-ExecutionPolicy" not in calls[0]["args"]


def test_runs_in_the_download_root_by_default(fake_run, tmp_path: Path) -> None:
    calls = fake_run(FakeCompleted())

    run_powershell("git status")

    assert calls[0]["cwd"] == (tmp_path / "dl").resolve()


def test_runs_in_an_approved_working_directory(
    fake_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setenv("BIONIC_PROJECT_ROOTS", str(project))

    calls = fake_run(FakeCompleted())

    run_powershell("git status", working_directory=str(project))

    assert calls[0]["cwd"] == project.resolve()


def test_output_is_decoded_with_replacement(fake_run) -> None:
    """Build tools emit non-UTF-8 bytes; that must not raise."""

    calls = fake_run(FakeCompleted())

    run_powershell("git status")

    assert calls[0]["encoding"] == "utf-8"
    assert calls[0]["errors"] == "replace"
    assert calls[0]["capture_output"] is True


def test_environment_is_passed_with_a_path(fake_run) -> None:
    calls = fake_run(FakeCompleted())

    run_powershell("git status")

    assert "PATH" in calls[0]["env"]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (300, 300),
        (0, 1),
        (-5, 1),
        (5000, 600),
        (600, 600),
    ],
)
def test_timeout_is_clamped(fake_run, requested: int, expected: int) -> None:
    calls = fake_run(FakeCompleted())

    run_powershell("git status", timeout_seconds=requested)

    assert calls[0]["timeout"] == expected


# ============================================================
# Failures
# ============================================================


def test_timeout_returns_a_structured_error(fake_run) -> None:
    fake_run(subprocess.TimeoutExpired(cmd="powershell.exe", timeout=30))

    payload = assert_error_response(
        run_powershell("cmake --build .", timeout_seconds=30),
        "COMMAND_TIMEOUT",
    )

    assert "30 seconds" in payload["error"]["message"]
    assert payload["error"]["recovery"]


def test_missing_powershell_returns_a_structured_error(fake_run) -> None:
    fake_run(FileNotFoundError("powershell.exe not found"))

    payload = assert_error_response(
        run_powershell("git status"), "POWERSHELL_LAUNCH_FAILED"
    )

    assert "powershell.exe" in payload["error"]["message"]


def test_permission_error_on_launch(fake_run) -> None:
    fake_run(PermissionError("access denied"))

    assert_error_response(run_powershell("git status"), "POWERSHELL_LAUNCH_FAILED")


# ============================================================
# Environment reconstruction
# ============================================================


def test_development_environment_includes_a_path() -> None:
    env = get_windows_development_environment()

    assert "PATH" in env
    assert env["PATH"]


def test_development_environment_deduplicates_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The machine and user PATH overlap; duplicates waste lookup time."""

    import winreg

    class FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    values = {
        "machine": r"C:\Windows;C:\Windows\System32;C:\Tools",
        "user": r"C:\Tools;C:\Users\dev\bin",
    }

    def open_key(hive: Any, path: Any):
        return FakeKey()

    calls = iter(["machine", "user"])

    def query(key: Any, name: str):
        return values[next(calls)], winreg.REG_EXPAND_SZ

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "QueryValueEx", query)
    monkeypatch.setenv("PATH", r"C:\Windows")

    entries = get_windows_development_environment()["PATH"].split(";")

    normalized = [e.lower().rstrip("\\") for e in entries if e]

    assert len(normalized) == len(set(normalized)), "PATH contains duplicates"
    assert r"c:\tools" in normalized
    assert r"c:\users\dev\bin" in normalized


def test_development_environment_survives_a_registry_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry read failure must degrade, not break every command."""

    import winreg

    def boom(*args: Any, **kwargs: Any):
        raise OSError("registry unavailable")

    monkeypatch.setattr(winreg, "OpenKey", boom)
    monkeypatch.setenv("PATH", r"C:\Windows")

    env = get_windows_development_environment()

    assert env["PATH"] == r"C:\Windows"


def test_development_environment_expands_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry PATH entries commonly contain %SystemRoot% and friends."""

    import winreg

    class FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    monkeypatch.setenv("MCP_TEST_ROOT", r"C:\Expanded")
    monkeypatch.setattr(winreg, "OpenKey", lambda *a: FakeKey())
    monkeypatch.setattr(
        winreg,
        "QueryValueEx",
        lambda key, name: (r"%MCP_TEST_ROOT%\bin", winreg.REG_EXPAND_SZ),
    )

    env = get_windows_development_environment()

    assert r"C:\Expanded\bin" in env["PATH"]
    assert "%MCP_TEST_ROOT%" not in env["PATH"]
