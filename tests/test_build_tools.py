"""Tests for build_project's structured timeout and msbuild defaults.

run_process is stubbed: these assert how the tool turns an outcome into an
envelope and how it assembles argv, not whether a compiler happens to be
installed on the machine running the suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from windows_agent_mcp.process import ProcessResult
from windows_agent_mcp.tools import build_project as build_module
from windows_agent_mcp.tools.build_project import (
    _with_default_verbosity,
    build_project,
)


def test_timeout_returns_structured_status_with_pid(
    writable_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv, *, cwd, timeout_seconds, log_path=None):
        return ProcessResult(
            stdout="compiling a.cpp\n",
            stderr="",
            exit_code=-1,
            timed_out=True,
            combined="compiling a.cpp\n",
            pid=4242,
            elapsed_ms=1500,
        )

    monkeypatch.setattr(build_module, "run_process", fake_run)

    payload = json.loads(build_project("msbuild game.sln", str(writable_project)))

    error = payload["error"]

    assert payload["ok"] is False
    assert error["type"] == "BUILD_TIMED_OUT"
    assert error["status"] == "timeout"
    assert error["pid"] == 4242
    assert error["elapsed_ms"] == 1500
    assert error["killed"] is True
    assert "compiling a.cpp" in error["message"]


def test_msbuild_gains_quiet_defaults_when_absent() -> None:
    argv = _with_default_verbosity(["msbuild", "game.sln"])

    assert argv == ["msbuild", "game.sln", "/v:minimal", "/nologo"]


def test_msbuild_defaults_are_not_duplicated_when_present() -> None:
    argv = _with_default_verbosity(["msbuild", "game.sln", "/v:diag", "/nologo"])

    assert argv == ["msbuild", "game.sln", "/v:diag", "/nologo"]


def test_msbuild_defaults_are_not_duplicated_with_the_long_switch() -> None:
    argv = _with_default_verbosity(["msbuild", "game.sln", "/verbosity:quiet"])

    assert argv == ["msbuild", "game.sln", "/verbosity:quiet", "/nologo"]


def test_other_build_tools_are_left_alone() -> None:
    argv = ["cmake", "--build", "build"]

    assert _with_default_verbosity(list(argv)) == argv


def test_second_build_for_the_same_directory_is_refused_while_one_runs(
    writable_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    captured: dict[str, str] = {}

    def fake_run(argv, *, cwd, timeout_seconds, log_path=None):
        nonlocal calls
        calls += 1

        if calls == 1:
            # Re-enter while the outer build still holds the slot. This is the
            # race the guard exists for: two tools building one obj/.
            captured["inner"] = build_project(
                "cmake --build build", str(writable_project)
            )

        return ProcessResult(
            stdout="",
            stderr="",
            exit_code=0,
            timed_out=False,
            combined="",
        )

    monkeypatch.setattr(build_module, "run_process", fake_run)

    outer = build_project("cmake --build build", str(writable_project))

    inner = json.loads(captured["inner"])

    assert inner["ok"] is False
    assert inner["error"]["type"] == "BUILD_ALREADY_RUNNING"
    assert inner["error"]["running_command"] == "cmake --build build"
    assert "elapsed_ms" in inner["error"]
    assert "BUILD SUCCEEDED" in outer


def test_slot_is_released_after_a_timeout(
    writable_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv, *, cwd, timeout_seconds, log_path=None):
        return ProcessResult(
            stdout="",
            stderr="",
            exit_code=-1,
            timed_out=True,
            combined="",
            pid=99,
            elapsed_ms=5,
        )

    monkeypatch.setattr(build_module, "run_process", fake_run)

    first = json.loads(
        build_project("cmake --build build", str(writable_project), timeout_seconds=5)
    )
    second = json.loads(
        build_project("cmake --build build", str(writable_project), timeout_seconds=5)
    )

    assert first["error"]["type"] == "BUILD_TIMED_OUT"
    # A timeout ends the process, so the slot is free again: the second call
    # must see a timeout, not a refusal.
    assert second["error"]["type"] == "BUILD_TIMED_OUT"


def test_full_log_path_is_named_in_the_success_report(
    writable_project, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A truncated diagnostic summary must still point at the raw log."""

    retained = tmp_path / "build.log"

    def fake_run(argv, *, cwd, timeout_seconds, log_path=None):
        return ProcessResult(
            stdout="",
            stderr="",
            exit_code=0,
            timed_out=False,
            combined="",
            log_path=retained,
        )

    monkeypatch.setattr(build_module, "run_process", fake_run)

    report = build_project("cmake --build build", str(writable_project))

    assert "full log:" in report
    assert str(retained) in report


def test_full_log_path_is_carried_in_the_timeout_details(
    writable_project, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    retained = tmp_path / "build.log"

    def fake_run(argv, *, cwd, timeout_seconds, log_path=None):
        return ProcessResult(
            stdout="",
            stderr="",
            exit_code=-1,
            timed_out=True,
            combined="",
            pid=7,
            elapsed_ms=3,
            log_path=retained,
        )

    monkeypatch.setattr(build_module, "run_process", fake_run)

    payload = json.loads(
        build_project("cmake --build build", str(writable_project))
    )

    assert payload["error"]["full_log"] == str(retained)

