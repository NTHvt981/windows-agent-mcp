"""Shared fixtures for the focused build-feedback suite.

Deliberately small, and not a revival of the suite removed in c777c0c. It
covers only what the new tests need, and every writable directory is
redirected into tmp_path so a test can never touch the developer's real
download root or a real project tree.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_server_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub the server's environment variables before every test.

    Not tidiness but correctness: a developer with WAMCP_PROJECT_ROOTS
    exported would otherwise run the suite against a different set of writable
    roots from everyone else's, and a confinement test could fail on their
    machine only.
    """

    for name in (
        "WAMCP_TOOLS",
        "WAMCP_PROFILE",
        "WAMCP_WEB_RESEARCH",
        "WAMCP_PROJECT_ROOTS",
        "WAMCP_WORKSPACE_FROM_CWD",
        "WAMCP_LAUNCH_CWD",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def writable_project(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A temporary project directory that build_project may run inside.

    Points WAMCP_DOWNLOAD_ROOT at tmp_path too, so get_allowed_working_
    directories() does not create or read the developer's real download
    directory while resolving the working directory.
    """

    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setenv("WAMCP_DOWNLOAD_ROOT", str(tmp_path / "downloads"))
    monkeypatch.setenv("WAMCP_PROJECT_ROOTS", str(project))

    return project.resolve()
