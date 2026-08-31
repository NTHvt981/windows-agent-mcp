"""Shared pytest fixtures and assertion helpers.

Tools in this project return either a JSON envelope ({"ok": ...}) or plain
text. The helpers below assert on the envelope shape so individual tests do
not repeat the json.loads dance.
"""

from __future__ import annotations

import json
import socket
from typing import Any
from urllib.parse import quote

import pytest

from windows_agent_mcp import hostgrants
from windows_agent_mcp.tools import fetch_web_page as page_module
from windows_agent_mcp.tools import web_search as search_module

# Padding that keeps a stubbed search response above the backend's
# minimum-plausible-page size, so a legitimate empty result set is not read
# as a truncated or blocked page.
_PAD = "<!-- " + "x" * 2000 + " -->"

# ============================================================
# HTTP stubs
# ============================================================

# The network tools reach the wire through utils.HTTP_OPENER, which each tool
# imports into its own namespace. Stubbing means replacing that attribute on
# the TOOL module, not on utils.


class FakeResponse:
    """Stand-in for the object urllib returns from opener.open().

    Only the surface the tools actually touch: a context manager, read(n),
    headers.get(), status and geturl(). Reads are served incrementally so the
    chunked download loop and its size cap behave as they would against a
    real socket.

    Two deliberate divergences from the real object, worth knowing:

    * `headers` is a plain dict, not an http.client.HTTPMessage. So it is
      CASE-SENSITIVE and has no get_content_charset(). Production code must
      therefore use the exact casing "Content-Type" and parse the charset
      itself, or it will silently read None here while working in the wild.
    * `status` defaults to 200. It exists because DuckDuckGo signals bot
      blocking with a 202, and urllib's HTTPErrorProcessor treats any 2xx as
      success -- so a blocked page arrives as a perfectly normal response and
      is only distinguishable by checking status explicitly.
    """

    def __init__(
        self,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        status: int = 200,
        url: str = "",
    ):
        self._body = body
        self._offset = 0
        self.headers: dict[str, str] = headers or {}
        self.status = status
        self.url = url

    def read(self, amount: int | None = None) -> bytes:
        if amount is None:
            chunk = self._body[self._offset :]
            self._offset = len(self._body)
            return chunk

        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


class FakeOpener:
    """Stand-in for utils.HTTP_OPENER.

    Records the requests it was given so tests can assert on headers and
    method without a live server.
    """

    def __init__(self, *responses: FakeResponse | Exception):
        self._responses = list(responses)
        self.requests: list[Any] = []
        self.timeouts: list[Any] = []

    def open(self, request: Any, timeout: Any = None) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)

        if not self._responses:
            raise AssertionError("FakeOpener called more times than expected")

        result = self._responses.pop(0)

        if isinstance(result, Exception):
            raise result

        return result


# ============================================================
# Environment isolation
# ============================================================

# Environment variables the server reads, scrubbed before each test.
#
# WAMCP_CONFIG_FILE and WAMCP_ALLOWED_HOSTS_FILE are deliberately NOT here.
# The fixtures below POINT those at tmp_path, and scrubbing them in one autouse
# fixture while setting them in another makes the result depend on fixture
# ordering -- which pytest decides from the dependency graph, not from the order
# they are written here. That silently un-isolated the config file: the suite
# read the developer's real one and every test that called main() inherited it.
_SERVER_ENV_VARS = (
    "WAMCP_TOOLS",
    "WAMCP_PROFILE",
    "WAMCP_WEB_RESEARCH",
    "WAMCP_SEARCH_BACKEND",
    "WAMCP_PROJECT_ROOTS",
    "WAMCP_EXTRA_DOC_HOSTS",
    "WAMCP_HOST_CONSENT",
    "WAMCP_HOST_GRANT_PERSIST",
)


@pytest.fixture(autouse=True)
def _isolate_server_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the server's environment variables before every test.

    Not tidiness -- correctness. pre-commit runs this suite on every commit
    and its comment promises the suite is hermetic. Without this, a developer
    who has WAMCP_WEB_RESEARCH exported (that is, anyone actually USING the
    research tools) would see a different tool count and could not commit.

    Tests that want a variable set do so explicitly with monkeypatch.setenv,
    which still wins because this runs first.
    """

    for name in _SERVER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_host_grants(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Guarantee no test sees a real granted-hosts file, or another test's.

    Scrubbing the environment is not enough. find_grants_file() falls back to
    `mcp-allowed-hosts.json` in the current directory and then in the
    repository root, so a developer who granted themselves a host would run a
    suite in which fetch_web_page silently permits it -- and the tests
    asserting that an off-list host is refused would fail on their machine
    only. Pointing at a path inside tmp_path that does not exist makes "no
    grants" the guaranteed starting state.

    Session grants are process state rather than environment, so they are
    cleared here too. A grant leaking forward would make a later test pass
    for the wrong reason, which is worse than failing.
    """

    monkeypatch.setenv(
        "WAMCP_ALLOWED_HOSTS_FILE",
        str(tmp_path / "no-such-grants.json"),
    )

    hostgrants.clear_session_grants()

    yield

    hostgrants.clear_session_grants()


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee no test reads the developer's real input/config.json.

    Scrubbing WAMCP_CONFIG_FILE is not enough: find_config_file() falls back to
    `input/config.json` under the current directory and then under the
    repository root, so a developer who had set a project root there would run
    a suite configured differently from everyone else's -- and the tests
    asserting default behaviour would fail on their machine only.

    Points at a path inside tmp_path instead. Nothing is created: a missing
    file is the guaranteed starting state, and a test that wants one writes it.
    """

    monkeypatch.setenv("WAMCP_CONFIG_FILE", str(tmp_path / "no-such-config.json"))


@pytest.fixture
def config_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A writable config path, wired into the environment.

    Returns the path rather than creating it, because "the file does not exist
    yet" is the case the server generates from.
    """

    path = tmp_path / "input" / "config.json"

    monkeypatch.setenv("WAMCP_CONFIG_FILE", str(path))

    return path


@pytest.fixture
def grants_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A writable granted-hosts file path, wired into the environment.

    Returns the path rather than creating it: "the file does not exist yet" is
    a case worth testing, and a fixture that pre-created it would hide it.
    """

    path = tmp_path / "mcp-allowed-hosts.json"

    monkeypatch.setenv("WAMCP_ALLOWED_HOSTS_FILE", str(path))

    return path


@pytest.fixture
def page_opener(monkeypatch: pytest.MonkeyPatch):
    """Stub both openers fetch_web_page can pick.

    Which one it uses depends on research mode -- RESEARCH_HTTP_OPENER when
    on, DOC_HTTP_OPENER when off -- so patching only one would let a test
    reach the real network the moment that choice changed.
    """

    def install(*responses: FakeResponse | Exception) -> FakeOpener:
        fake = FakeOpener(*responses)
        monkeypatch.setattr(page_module, "RESEARCH_HTTP_OPENER", fake)
        monkeypatch.setattr(page_module, "DOC_HTTP_OPENER", fake)
        return fake

    return install


@pytest.fixture
def search_opener(monkeypatch: pytest.MonkeyPatch):
    """Stub the opener the DuckDuckGo backend picks up by default."""

    def install(*responses: FakeResponse | Exception) -> FakeOpener:
        fake = FakeOpener(*responses)
        monkeypatch.setattr(search_module, "get_backend", lambda: _backend_with(fake))
        return fake

    return install


def _backend_with(opener: FakeOpener):
    from windows_agent_mcp.search_backends import DuckDuckGoBackend

    return DuckDuckGoBackend(
        opener=opener,  # type: ignore[arg-type]
        clock=lambda: 0.0,
        sleep=lambda _: None,
    )


def _ddg_results(*targets: str) -> bytes:
    rows = "".join(
        f'<tr><td><a class="result-link" '
        f'href="//duckduckgo.com/l/?uddg={quote(target, safe="")}">Title {n}</a>'
        f"</td></tr>"
        f'<tr><td class="result-snippet">Snippet {n}.</td></tr>'
        for n, target in enumerate(targets, start=1)
    )

    return f"<html><body><table>{rows}</table></body></html>{_PAD}".encode()


@pytest.fixture
def resolves_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make validate_url's DNS check succeed without touching the network."""

    def getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.121.4", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def isolated_download_root(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point WAMCP_DOWNLOAD_ROOT at a temporary directory.

    Without this, tests that touch the download root either write into the
    developer's real download directory or leave stray directories in the
    repository.
    """

    root = tmp_path / "downloads"

    monkeypatch.setenv("WAMCP_DOWNLOAD_ROOT", str(root))

    return root.resolve()


@pytest.fixture
def writable_project(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A temporary project directory that write_file and edit_file may write to.

    Points BOTH roots at tmp_path: WAMCP_DOWNLOAD_ROOT so
    get_allowed_working_directories() does not create (or write into) the
    developer's real download directory, and WAMCP_PROJECT_ROOTS so the
    project itself is writable. Every write test needs both, because the
    download root is always in the allowed list.
    """

    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setenv("WAMCP_DOWNLOAD_ROOT", str(tmp_path / "downloads"))
    monkeypatch.setenv("WAMCP_PROJECT_ROOTS", str(project))

    return project.resolve()


def assert_success_response(result: str) -> dict[str, Any]:
    """Assert a tool returned a successful JSON envelope.

    Args:
        result: Raw string returned by a tool.

    Returns:
        The parsed envelope.

    Raises:
        AssertionError: If the payload is not valid JSON or ok is not True.
    """

    payload = json.loads(result)

    assert payload["ok"] is True, f"Expected success, got: {payload}"

    return payload


def assert_error_response(result: str, expected_type: str) -> dict[str, Any]:
    """Assert a tool returned a structured error of a specific type.

    Args:
        result: Raw string returned by a tool.
        expected_type: Value expected at error.type, e.g. "PATH_NOT_FOUND".

    Returns:
        The parsed envelope.

    Raises:
        AssertionError: If the payload is not an error of the expected type,
                        or is missing the documented error fields.
    """

    payload = json.loads(result)

    assert payload["ok"] is False, f"Expected an error, got: {payload}"

    error = payload["error"]

    for field in ("type", "tool", "message"):
        assert field in error, f"Error envelope missing '{field}': {error}"

    assert error["type"] == expected_type, (
        f"Expected {expected_type}, got {error['type']}"
    )

    return payload
