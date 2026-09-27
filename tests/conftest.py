from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_server_env(monkeypatch: pytest.MonkeyPatch) -> None:

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

    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setenv("WAMCP_DOWNLOAD_ROOT", str(tmp_path / "downloads"))
    monkeypatch.setenv("WAMCP_PROJECT_ROOTS", str(project))

    return project.resolve()
