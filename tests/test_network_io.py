"""Tests for the fetch_https_response and get_github_latest_release bodies.

HTTP is stubbed at each tool's HTTP_OPENER, so the response handling, size
caps, decoding and error paths run for real without a network.
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeOpener, FakeResponse, assert_error_response

from windows_agent_mcp.tools import fetch_https_response as fetch_module
from windows_agent_mcp.tools import get_github_latest_release as github_module
from windows_agent_mcp.tools.fetch_https_response import fetch_https_response
from windows_agent_mcp.tools.get_github_latest_release import (
    get_github_latest_release,
)
from windows_agent_mcp.utils import HTTP_TIMEOUT_SECONDS, MAX_HTTP_BYTES

URL = "https://raw.githubusercontent.com/premake/premake-core/master/README.md"


@pytest.fixture
def fetch_opener(monkeypatch: pytest.MonkeyPatch):
    def install(*responses: FakeResponse | Exception) -> FakeOpener:
        fake = FakeOpener(*responses)
        monkeypatch.setattr(fetch_module, "HTTP_OPENER", fake)
        return fake

    return install


@pytest.fixture
def github_opener(monkeypatch: pytest.MonkeyPatch):
    def install(*responses: FakeResponse | Exception) -> FakeOpener:
        fake = FakeOpener(*responses)
        monkeypatch.setattr(github_module, "HTTP_OPENER", fake)
        return fake

    return install


# ============================================================
# fetch_https_response
# ============================================================


def test_fetch_returns_the_body(fetch_opener, resolves_public: None) -> None:
    fetch_opener(FakeResponse(b"# Premake\n\nA build system."))

    assert fetch_https_response(URL) == "# Premake\n\nA build system."


def test_fetch_decodes_utf8(fetch_opener, resolves_public: None) -> None:
    fetch_opener(FakeResponse("café → 日本語".encode()))

    assert fetch_https_response(URL) == "café → 日本語"


def test_fetch_replaces_undecodable_bytes(fetch_opener, resolves_public: None) -> None:
    """Malformed bytes must not raise; the tool decodes with errors=replace."""

    fetch_opener(FakeResponse(b"ok \xff\xfe bad"))

    result = fetch_https_response(URL)

    assert result.startswith("ok ")
    assert "�" in result


def test_fetch_sends_the_expected_request(fetch_opener, resolves_public: None) -> None:
    fake = fetch_opener(FakeResponse(b"x"))

    fetch_https_response(URL)

    request = fake.requests[0]

    assert request.get_method() == "GET"
    assert "Local-Agent-MCP" in request.get_header("User-agent", "")
    assert fake.timeouts[0] == HTTP_TIMEOUT_SECONDS


def test_fetch_refuses_a_declared_oversize_response(
    fetch_opener, resolves_public: None
) -> None:
    fetch_opener(
        FakeResponse(b"x", headers={"Content-Length": str(MAX_HTTP_BYTES + 1)})
    )

    assert_error_response(fetch_https_response(URL), "RESPONSE_TOO_LARGE")


def test_fetch_refuses_an_oversize_body(
    fetch_opener, resolves_public: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing or lying Content-Length must not defeat the cap."""

    monkeypatch.setattr(fetch_module, "MAX_HTTP_BYTES", 32)

    fetch_opener(FakeResponse(b"y" * 4096))

    assert_error_response(fetch_https_response(URL), "RESPONSE_TOO_LARGE")


def test_fetch_allows_a_body_exactly_at_the_limit(
    fetch_opener, resolves_public: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch_module, "MAX_HTTP_BYTES", 8)

    fetch_opener(FakeResponse(b"z" * 8))

    assert fetch_https_response(URL) == "z" * 8


def test_fetch_ignores_an_unparseable_content_length(
    fetch_opener, resolves_public: None
) -> None:
    fetch_opener(FakeResponse(b"body", headers={"Content-Length": "huge"}))

    assert fetch_https_response(URL) == "body"


def test_fetch_reports_a_network_error(fetch_opener, resolves_public: None) -> None:
    fetch_opener(OSError("connection reset"))

    payload = assert_error_response(fetch_https_response(URL), "HTTP_REQUEST_FAILED")

    assert "connection reset" in payload["error"]["message"]


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/x",
        "https://malicious.example.com/x",
        "https://user:pass@github.com/x",
    ],
)
def test_fetch_refuses_a_disallowed_url(fetch_opener, url: str) -> None:
    """No response is queued: validate_url must reject before connecting."""

    fetch_opener()

    assert_error_response(fetch_https_response(url), "URL_NOT_ALLOWED")


# ============================================================
# get_github_latest_release
# ============================================================


RELEASE = {
    "tag_name": "v5.0.0-beta2",
    "name": "Premake 5.0 beta 2",
    "published_at": "2024-01-01T00:00:00Z",
    "html_url": "https://github.com/premake/premake-core/releases/v5",
    "assets": [
        {
            "name": "premake-5.0.0-beta2-windows.zip",
            "size": 1234,
            "browser_download_url": "https://github.com/a.zip",
        },
        {
            "name": "premake-5.0.0-beta2-linux.tar.gz",
            "size": 5678,
            "browser_download_url": "https://github.com/b.tar.gz",
        },
    ],
}


def test_github_returns_release_metadata(github_opener, resolves_public: None) -> None:
    github_opener(FakeResponse(json.dumps(RELEASE).encode()))

    data = json.loads(get_github_latest_release("premake/premake-core"))

    assert data["repository"] == "premake/premake-core"
    assert data["tag"] == "v5.0.0-beta2"
    assert data["name"] == "Premake 5.0 beta 2"
    assert data["html_url"].endswith("/releases/v5")


def test_github_maps_assets(github_opener, resolves_public: None) -> None:
    github_opener(FakeResponse(json.dumps(RELEASE).encode()))

    data = json.loads(get_github_latest_release("premake/premake-core"))

    assert data["assets"] == [
        {
            "name": "premake-5.0.0-beta2-windows.zip",
            "size": 1234,
            "download_url": "https://github.com/a.zip",
        },
        {
            "name": "premake-5.0.0-beta2-linux.tar.gz",
            "size": 5678,
            "download_url": "https://github.com/b.tar.gz",
        },
    ]


def test_github_handles_a_release_with_no_assets(
    github_opener, resolves_public: None
) -> None:
    github_opener(FakeResponse(json.dumps({"tag_name": "v1"}).encode()))

    data = json.loads(get_github_latest_release("o/r"))

    assert data["assets"] == []
    assert data["tag"] == "v1"
    # Absent fields come back as null rather than being omitted.
    assert data["name"] is None
    assert data["published_at"] is None


def test_github_tolerates_assets_missing_fields(
    github_opener, resolves_public: None
) -> None:
    github_opener(FakeResponse(json.dumps({"assets": [{}]}).encode()))

    data = json.loads(get_github_latest_release("o/r"))

    assert data["assets"] == [{"name": None, "size": None, "download_url": None}]


def test_github_requests_the_api_json_media_type(
    github_opener, resolves_public: None
) -> None:
    fake = github_opener(FakeResponse(b"{}"))

    get_github_latest_release("o/r")

    request = fake.requests[0]

    assert request.full_url == "https://api.github.com/repos/o/r/releases/latest"
    assert "vnd.github" in request.get_header("Accept", "")


def test_github_reports_malformed_json(github_opener, resolves_public: None) -> None:
    github_opener(FakeResponse(b"<html>502 Bad Gateway</html>"))

    assert_error_response(get_github_latest_release("o/r"), "GITHUB_REQUEST_FAILED")


def test_github_reports_a_network_error(github_opener, resolves_public: None) -> None:
    github_opener(OSError("404 not found"))

    payload = assert_error_response(
        get_github_latest_release("o/r"), "GITHUB_REQUEST_FAILED"
    )

    assert "404 not found" in payload["error"]["message"]


@pytest.mark.parametrize(
    "repository",
    [
        "not-a-repo",
        "too/many/slashes",
        "https://github.com/o/r",
        "o/r extra",
        "",
        "o/",
        "/r",
    ],
)
def test_github_rejects_a_malformed_repository(github_opener, repository: str) -> None:
    """No response queued: the pattern check must reject before connecting."""

    github_opener()

    assert_error_response(get_github_latest_release(repository), "INVALID_REPOSITORY")


@pytest.mark.parametrize("repository", ["..", "../..", "o/..", "../r"])
def test_github_rejects_traversal_in_the_repository(
    github_opener, repository: str
) -> None:
    """Dot segments would alter the API path that gets requested."""

    github_opener()

    assert_error_response(get_github_latest_release(repository), "INVALID_REPOSITORY")


def test_github_reports_a_delisted_api_host(
    github_opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The README invites editing ALLOWED_NETWORK_HOSTS; removing the API host
    must produce a clear explanation rather than an opaque network error."""

    from windows_agent_mcp import utils

    monkeypatch.setattr(
        utils, "ALLOWED_NETWORK_HOSTS", utils.ALLOWED_NETWORK_HOSTS - {"api.github.com"}
    )

    github_opener()

    payload = assert_error_response(
        get_github_latest_release("premake/premake-core"), "URL_NOT_ALLOWED"
    )

    assert "api.github.com" in payload["error"]["message"]
