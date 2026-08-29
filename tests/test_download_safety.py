"""Tests for download filename sanitisation and path containment.

These guard the second security-critical surface: nothing a model supplies as
a filename may write outside the download root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from windows_agent_mcp.tools.download_file import (
    RESERVED_DEVICE_NAMES,
    safe_download_path,
    sanitize_filename,
)
from windows_agent_mcp.utils import MAX_FILENAME_LENGTH


@pytest.fixture
def download_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the download root at an isolated temporary directory."""

    root = tmp_path / "downloads"

    monkeypatch.setenv("BIONIC_DOWNLOAD_ROOT", str(root))

    return root.resolve()


# ============================================================
# sanitize_filename -- path components are stripped
# ============================================================


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("premake-5.0.tar.gz", "premake-5.0.tar.gz"),
        ("../../evil.txt", "evil.txt"),
        ("..\\..\\evil.txt", "evil.txt"),
        ("C:\\Windows\\System32\\evil.dll", "evil.dll"),
        ("/etc/passwd", "passwd"),
        ("./nested/../file.bin", "file.bin"),
        # Windows-invalid characters are replaced, not dropped.
        ('a<b>c:d"e|f?g*h.txt', "a_b_c_d_e_f_g_h.txt"),
        # Surrounding whitespace is trimmed.
        ("  spaced.bin  ", "spaced.bin"),
    ],
)
def test_sanitize_filename(supplied: str, expected: str) -> None:
    assert sanitize_filename(supplied) == expected


@pytest.mark.parametrize("supplied", ["", "   ", ".", ".."])
def test_sanitize_filename_rejects(supplied: str) -> None:
    with pytest.raises(ValueError):
        sanitize_filename(supplied)


@pytest.mark.parametrize("reserved", sorted(RESERVED_DEVICE_NAMES))
def test_reserved_device_names_are_escaped(reserved: str) -> None:
    """Reserved device names are prefixed so they cannot open a device.

    Covers the whole set, including COM5-COM9 and LPT4-LPT9, which were
    previously missing, and CONIN$/CONOUT$.
    """

    assert sanitize_filename(reserved) == "_" + reserved
    assert sanitize_filename(reserved.lower()) == "_" + reserved.lower()


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        # A device name is still a device when it carries an extension.
        ("NUL.txt", "_NUL.txt"),
        ("CON.log", "_CON.log"),
        ("AUX.bin", "_AUX.bin"),
        ("COM9.dat", "_COM9.dat"),
        ("LPT4.tmp", "_LPT4.tmp"),
        ("nul.txt", "_nul.txt"),
        ("PRN.tar.gz", "_PRN.tar.gz"),
        # Trailing dots are stripped first, so this is still the NUL device.
        ("NUL.", "_NUL"),
        ("NUL.txt.", "_NUL.txt"),
    ],
)
def test_reserved_names_with_extension_are_escaped(
    supplied: str, expected: str
) -> None:
    assert sanitize_filename(supplied) == expected


@pytest.mark.parametrize(
    "supplied",
    [
        # Merely starting with a device name is fine; the match is exact.
        "NULL.txt",
        "CONFIG.txt",
        "COMMIT.log",
        "AUXILIARY.bin",
        "LPTX.txt",
        "PRNT.dat",
        "COM0",
        "COM10",
        "LPT0",
    ],
)
def test_non_reserved_lookalikes_are_untouched(supplied: str) -> None:
    """Over-escaping would silently rename ordinary files."""

    assert sanitize_filename(supplied) == supplied


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        # Windows silently discards trailing dots and spaces, so a name
        # ending in them would not match the file actually created.
        ("evil.txt.", "evil.txt"),
        ("evil.txt...", "evil.txt"),
        ("evil.txt ", "evil.txt"),
        ("evil.txt . . ", "evil.txt"),
        ("archive.tar.gz.", "archive.tar.gz"),
    ],
)
def test_trailing_dots_and_spaces_are_stripped(supplied: str, expected: str) -> None:
    assert sanitize_filename(supplied) == expected


@pytest.mark.parametrize("supplied", ["...", ". . .", "  .  ", ".", "..", " .. "])
def test_names_of_only_dots_and_spaces_are_rejected(supplied: str) -> None:
    """Nothing usable remains, so this must not silently become something."""

    with pytest.raises(ValueError):
        sanitize_filename(supplied)


# ============================================================
# Length truncation
# ============================================================


@pytest.mark.parametrize(
    ("supplied", "expected_suffix"),
    [
        # Truncating from the end used to destroy the extension, which on
        # Windows decides how the file opens.
        ("x" * 400 + ".bin", ".bin"),
        ("x" * 400 + ".tar.gz", ".tar.gz"),
        ("x" * 400 + ".zip", ".zip"),
        ("premake-" + "5" * 400 + ".tar.bz2", ".tar.bz2"),
    ],
)
def test_truncation_preserves_extension(supplied: str, expected_suffix: str) -> None:
    result = sanitize_filename(supplied)

    assert len(result) == MAX_FILENAME_LENGTH
    assert result.endswith(expected_suffix)


@pytest.mark.parametrize(
    "supplied",
    [
        "x" * 400,  # no extension at all
        "x" * 400 + "." + "y" * 50,  # trailing run too long to be one
        "." * 5 + "x" * 400,  # leading dots, no real extension
    ],
)
def test_truncation_without_a_usable_extension(supplied: str) -> None:
    """With no plausible extension, a hard cut at the limit is correct."""

    assert len(sanitize_filename(supplied)) == MAX_FILENAME_LENGTH


def test_short_names_are_not_truncated() -> None:
    name = "premake-5.0.0-beta2-windows.tar.gz"

    assert sanitize_filename(name) == name


def test_truncated_name_still_fits_the_part_suffix(tmp_path, monkeypatch) -> None:
    """The limit must leave room for the ".part" file used during download."""

    monkeypatch.setenv("BIONIC_DOWNLOAD_ROOT", str(tmp_path))

    destination = safe_download_path("x" * 400 + ".tar.gz")
    temporary = destination.with_suffix(destination.suffix + ".part")

    assert len(temporary.name) < 255


# ============================================================
# safe_download_path -- containment
# ============================================================


def test_safe_download_path_stays_in_root(download_root: Path) -> None:
    destination = safe_download_path("premake.tar.gz")

    assert destination.parent == download_root
    assert destination.name == "premake.tar.gz"


@pytest.mark.parametrize(
    "supplied",
    [
        "../../escape.bin",
        "..\\..\\escape.bin",
        "C:\\Windows\\System32\\escape.dll",
        "/etc/escape",
        "nested/../../escape.bin",
    ],
)
def test_safe_download_path_never_escapes_root(
    supplied: str, download_root: Path
) -> None:
    """Traversal attempts collapse to a bare name inside the root."""

    destination = safe_download_path(supplied)

    # The authoritative check: the resolved path is under the root.
    assert destination.resolve().is_relative_to(download_root)
    assert destination.parent == download_root


def test_safe_download_path_creates_root_on_demand(download_root: Path) -> None:
    assert not download_root.exists()

    safe_download_path("file.bin")

    assert download_root.is_dir()
