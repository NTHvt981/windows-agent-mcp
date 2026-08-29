"""Tests for the download_file I/O body.

The HTTP layer is stubbed at the tool's HTTP_OPENER, so these exercise the
real streaming loop, the size caps, the atomic .part rename and the error
paths without a network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeOpener, FakeResponse, assert_error_response

from windows_agent_mcp.tools import download_file as module
from windows_agent_mcp.tools.download_file import download_file
from windows_agent_mcp.utils import MAX_DOWNLOAD_BYTES

URL = "https://github.com/premake/premake-core/archive/v5.zip"


@pytest.fixture
def opener(monkeypatch: pytest.MonkeyPatch):
    """Install a FakeOpener and hand it back for assertions."""

    def install(*responses: FakeResponse | Exception) -> FakeOpener:
        fake = FakeOpener(*responses)
        monkeypatch.setattr(module, "HTTP_OPENER", fake)
        return fake

    return install


# ============================================================
# Success path
# ============================================================


def test_download_writes_the_file(
    opener, isolated_download_root: Path, resolves_public: None
) -> None:
    opener(FakeResponse(b"zip-bytes-here"))

    result = download_file(URL)

    destination = isolated_download_root / "v5.zip"

    assert destination.read_bytes() == b"zip-bytes-here"
    assert "DOWNLOAD SUCCESS" in result
    assert str(destination) in result
    assert "14 bytes" in result


def test_download_leaves_no_part_file(
    opener, isolated_download_root: Path, resolves_public: None
) -> None:
    """The .part file is renamed onto the destination, not left behind."""

    opener(FakeResponse(b"payload"))

    download_file(URL)

    assert list(isolated_download_root.iterdir()) == [isolated_download_root / "v5.zip"]


def test_download_streams_multiple_chunks(
    opener, isolated_download_root: Path, resolves_public: None
) -> None:
    """The loop reads 1 MB at a time, so a larger body needs several passes."""

    body = b"x" * (1024 * 1024 * 2 + 512)

    opener(FakeResponse(body))

    result = download_file(URL)

    assert (isolated_download_root / "v5.zip").read_bytes() == body
    assert f"{len(body):,} bytes" in result


def test_download_uses_the_url_filename(
    opener, isolated_download_root: Path, resolves_public: None
) -> None:
    opener(FakeResponse(b"x"))

    download_file("https://github.com/o/r/releases/premake-5.0.tar.gz")

    assert (isolated_download_root / "premake-5.0.tar.gz").is_file()


def test_download_falls_back_to_download_bin(
    opener, isolated_download_root: Path, resolves_public: None
) -> None:
    """A URL with no filename in its path still needs somewhere to land."""

    opener(FakeResponse(b"x"))

    download_file("https://github.com/")

    assert (isolated_download_root / "download.bin").is_file()


def test_download_honours_an_explicit_filename(
    opener, isolated_download_root: Path, resolves_public: None
) -> None:
    opener(FakeResponse(b"x"))

    download_file(URL, filename="renamed.zip")

    assert (isolated_download_root / "renamed.zip").is_file()


def test_download_sanitizes_an_explicit_filename(
    opener, isolated_download_root: Path, resolves_public: None
) -> None:
    """A traversal attempt in the filename collapses into the root."""

    opener(FakeResponse(b"x"))

    download_file(URL, filename="../../escape.zip")

    assert (isolated_download_root / "escape.zip").is_file()
    assert not (isolated_download_root.parent / "escape.zip").exists()


def test_download_sends_a_user_agent(
    opener, isolated_download_root: Path, resolves_public: None
) -> None:
    fake = opener(FakeResponse(b"x"))

    download_file(URL)

    request = fake.requests[0]

    assert request.get_method() == "GET"
    assert "Bionic" in request.get_header("User-agent", "")


# ============================================================
# Size caps
# ============================================================


def test_declared_size_over_the_limit_is_refused_before_reading(
    opener, isolated_download_root: Path, resolves_public: None
) -> None:
    opener(
        FakeResponse(
            b"never-read",
            headers={"Content-Length": str(MAX_DOWNLOAD_BYTES + 1)},
        )
    )

    assert_error_response(download_file(URL), "DOWNLOAD_TOO_LARGE")

    # Nothing was written, not even a partial file.
    assert list(isolated_download_root.iterdir()) == []


def test_streamed_size_over_the_limit_aborts_and_cleans_up(
    opener,
    isolated_download_root: Path,
    resolves_public: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lying or absent Content-Length must not defeat the cap."""

    monkeypatch.setattr(module, "MAX_DOWNLOAD_BYTES", 1024)

    opener(FakeResponse(b"y" * 4096))

    payload = assert_error_response(download_file(URL), "DOWNLOAD_TOO_LARGE")

    assert "partial file was removed" in payload["error"]["message"]
    assert list(isolated_download_root.iterdir()) == []


def test_unparseable_content_length_is_ignored(
    opener, isolated_download_root: Path, resolves_public: None
) -> None:
    """A malformed header must not abort an otherwise fine download."""

    opener(FakeResponse(b"body", headers={"Content-Length": "not-a-number"}))

    result = download_file(URL)

    assert "DOWNLOAD SUCCESS" in result
    assert (isolated_download_root / "v5.zip").read_bytes() == b"body"


def test_size_exactly_at_the_limit_is_allowed(
    opener,
    isolated_download_root: Path,
    resolves_public: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "MAX_DOWNLOAD_BYTES", 64)

    opener(FakeResponse(b"z" * 64, headers={"Content-Length": "64"}))

    assert "DOWNLOAD SUCCESS" in download_file(URL)


# ============================================================
# Existing files
# ============================================================


def test_existing_destination_is_never_overwritten(
    opener, isolated_download_root: Path, resolves_public: None
) -> None:
    isolated_download_root.mkdir(parents=True, exist_ok=True)
    destination = isolated_download_root / "v5.zip"
    destination.write_bytes(b"original")

    # No response queued: the tool must refuse before opening a connection.
    opener()

    assert_error_response(download_file(URL), "DESTINATION_EXISTS")

    assert destination.read_bytes() == b"original"


def test_stale_part_file_is_replaced(
    opener, isolated_download_root: Path, resolves_public: None
) -> None:
    """A leftover .part from a crashed run must not corrupt the next one."""

    isolated_download_root.mkdir(parents=True, exist_ok=True)
    (isolated_download_root / "v5.zip.part").write_bytes(b"stale-garbage")

    opener(FakeResponse(b"fresh"))

    download_file(URL)

    assert (isolated_download_root / "v5.zip").read_bytes() == b"fresh"
    assert not (isolated_download_root / "v5.zip.part").exists()


# ============================================================
# Failures
# ============================================================


def test_network_error_is_reported_structurally(
    opener, isolated_download_root: Path, resolves_public: None
) -> None:
    opener(OSError("connection reset"))

    payload = assert_error_response(download_file(URL), "DOWNLOAD_FAILED")

    assert "connection reset" in payload["error"]["message"]


def test_refused_url_never_reaches_the_network(opener) -> None:
    """validate_url runs first, so no response is needed."""

    opener()

    assert_error_response(download_file("http://github.com/x"), "DOWNLOAD_NOT_ALLOWED")


def test_refused_host_never_reaches_the_network(opener) -> None:
    opener()

    assert_error_response(
        download_file("https://malicious.example.com/x"), "DOWNLOAD_NOT_ALLOWED"
    )
