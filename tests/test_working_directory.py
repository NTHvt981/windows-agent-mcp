"""Tests for run_powershell working-directory resolution.

run_powershell used to force cwd to the download root, and because each call
is a fresh process the allowlisted `cd` never persisted -- so the model could
not run git or cmake against a real source tree. Callers may now pass a
working_directory, but only inside a root the operator approved.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from windows_agent_mcp.utils import (
    PROJECT_ROOTS_ENV_VAR,
    get_allowed_working_directories,
    resolve_working_directory,
)


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up a download root plus two approved project roots."""

    download = tmp_path / "downloads"
    project_a = tmp_path / "project_a"
    project_b = tmp_path / "project_b"
    outside = tmp_path / "outside"

    for directory in (project_a, project_b, outside):
        directory.mkdir()

    (project_a / "src").mkdir()

    monkeypatch.setenv("BIONIC_DOWNLOAD_ROOT", str(download))
    monkeypatch.setenv(
        PROJECT_ROOTS_ENV_VAR,
        os.pathsep.join([str(project_a), str(project_b)]),
    )

    return {
        "download": download.resolve(),
        "project_a": project_a.resolve(),
        "project_b": project_b.resolve(),
        "outside": outside.resolve(),
    }


# ============================================================
# get_allowed_working_directories
# ============================================================


def test_download_root_is_always_allowed(roots) -> None:
    allowed = get_allowed_working_directories()

    assert allowed[0] == roots["download"]


def test_configured_project_roots_are_included(roots) -> None:
    allowed = get_allowed_working_directories()

    assert roots["project_a"] in allowed
    assert roots["project_b"] in allowed
    assert roots["outside"] not in allowed


def test_default_is_download_root_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Widening beyond the download root must be opt-in."""

    monkeypatch.setenv("BIONIC_DOWNLOAD_ROOT", str(tmp_path / "dl"))
    monkeypatch.delenv(PROJECT_ROOTS_ENV_VAR, raising=False)

    assert get_allowed_working_directories() == [(tmp_path / "dl").resolve()]


def test_stale_configured_root_is_skipped_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad entry must not disable the tool entirely."""

    good = tmp_path / "good"
    good.mkdir()

    monkeypatch.setenv("BIONIC_DOWNLOAD_ROOT", str(tmp_path / "dl"))
    monkeypatch.setenv(
        PROJECT_ROOTS_ENV_VAR,
        os.pathsep.join([str(tmp_path / "does_not_exist"), str(good)]),
    )

    allowed = get_allowed_working_directories()

    assert good.resolve() in allowed
    assert (tmp_path / "does_not_exist").resolve() not in allowed


def test_a_file_configured_as_a_root_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")

    monkeypatch.setenv("BIONIC_DOWNLOAD_ROOT", str(tmp_path / "dl"))
    monkeypatch.setenv(PROJECT_ROOTS_ENV_VAR, str(target))

    assert target.resolve() not in get_allowed_working_directories()


# ============================================================
# resolve_working_directory
# ============================================================


@pytest.mark.parametrize("requested", [None, "", "   "])
def test_default_resolves_to_download_root(roots, requested) -> None:
    assert resolve_working_directory(requested) == roots["download"]


def test_approved_root_is_accepted(roots) -> None:
    assert resolve_working_directory(str(roots["project_a"])) == roots["project_a"]


def test_subdirectory_of_approved_root_is_accepted(roots) -> None:
    target = roots["project_a"] / "src"

    assert resolve_working_directory(str(target)) == target


def test_directory_outside_every_root_is_refused(roots) -> None:
    with pytest.raises(ValueError, match="outside every allowed root"):
        resolve_working_directory(str(roots["outside"]))


def test_traversal_out_of_an_approved_root_is_refused(roots) -> None:
    """Escaping via .. must be caught after resolution, not before."""

    escape = str(roots["project_a"] / ".." / "outside")

    with pytest.raises(ValueError, match="outside every allowed root"):
        resolve_working_directory(escape)


def test_nonexistent_directory_is_refused(roots) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        resolve_working_directory(str(roots["project_a"] / "nope"))


def test_file_is_refused(roots) -> None:
    target = roots["project_a"] / "file.txt"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        resolve_working_directory(str(target))


def test_error_names_the_env_var_to_fix_it(roots) -> None:
    """The message must tell the operator how to widen the allowlist."""

    with pytest.raises(ValueError, match=PROJECT_ROOTS_ENV_VAR):
        resolve_working_directory(str(roots["outside"]))


# ============================================================
# run_powershell integration
# ============================================================


def test_run_powershell_rejects_disallowed_directory(roots) -> None:
    """The rejection is a structured tool result, not an exception."""

    from windows_agent_mcp.tools.run_powershell import run_powershell

    result = run_powershell("git --version", working_directory=str(roots["outside"]))

    payload = json.loads(result)

    assert payload["ok"] is False
    assert payload["error"]["type"] == "WORKING_DIRECTORY_NOT_ALLOWED"
    assert payload["error"]["recovery"]


def test_run_powershell_validates_command_before_directory(roots) -> None:
    """A blocked command is reported as such even with a bad directory."""

    from windows_agent_mcp.tools.run_powershell import run_powershell

    result = run_powershell(
        "Stop-Computer -Force", working_directory=str(roots["outside"])
    )

    assert json.loads(result)["error"]["type"] == "COMMAND_NOT_ALLOWED"
