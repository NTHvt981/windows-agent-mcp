"""Tests for build_project's structured timeout and msbuild defaults.

run_process is stubbed: these assert how the tool turns an outcome into an
envelope and how it assembles argv, not whether a compiler happens to be
installed on the machine running the suite.
"""

from __future__ import annotations

import json

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
    def fake_run(argv, *, cwd, timeout_seconds):
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
