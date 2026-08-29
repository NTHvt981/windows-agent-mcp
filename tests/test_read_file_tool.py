"""Tests for the read_file tool.

On success read_file returns the file's text directly. Only errors are JSON.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import assert_error_response

from windows_agent_mcp.tools.read_file import read_file
from windows_agent_mcp.utils import MAX_READ_BYTES


def test_read_file_returns_raw_text(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("Hello, World!", encoding="utf-8")

    assert read_file(str(target)) == "Hello, World!"


def test_read_file_preserves_real_newlines(tmp_path: Path) -> None:
    """Content must arrive as actual newlines, not JSON-escaped \\n."""

    target = tmp_path / "multi.txt"
    target.write_text("line one\nline two\nline three\n", encoding="utf-8")

    result = read_file(str(target))

    assert result == "line one\nline two\nline three"
    assert "\\n" not in result


def test_read_file_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "empty.txt"
    target.write_text("", encoding="utf-8")

    assert read_file(str(target)) == ""


def test_read_file_unicode_content(tmp_path: Path) -> None:
    target = tmp_path / "unicode.txt"
    target.write_text("café → 日本語", encoding="utf-8")

    assert read_file(str(target)) == "café → 日本語"


# ============================================================
# Line ranges
# ============================================================


@pytest.fixture
def numbered_file(tmp_path: Path) -> Path:
    target = tmp_path / "numbered.txt"
    target.write_text("\n".join(f"line {n}" for n in range(1, 101)), encoding="utf-8")
    return target


def test_read_file_start_line(numbered_file: Path) -> None:
    result = read_file(str(numbered_file), start_line=50, max_lines=3)

    assert result.startswith("line 50\nline 51\nline 52")


def test_read_file_max_lines_truncates_and_says_so(numbered_file: Path) -> None:
    result = read_file(str(numbered_file), max_lines=10)

    assert result.startswith("line 1\n")
    assert "line 10" in result
    assert "line 11" not in result.split("...[truncated")[0]
    assert "truncated: showed lines 1-10" in result
    # The notice must tell the caller exactly how to continue.
    assert "start_line=11" in result


def test_read_file_final_chunk_has_no_truncation_notice(
    numbered_file: Path,
) -> None:
    result = read_file(str(numbered_file), start_line=91, max_lines=50)

    assert result == "\n".join(f"line {n}" for n in range(91, 101))
    assert "truncated" not in result


@pytest.mark.parametrize(("start_line", "max_lines"), [(0, 5), (-10, 5), (1, 0)])
def test_read_file_clamps_nonsense_ranges(
    numbered_file: Path, start_line: int, max_lines: int
) -> None:
    """Out-of-range numbers are clamped, not rejected."""

    result = read_file(str(numbered_file), start_line=start_line, max_lines=max_lines)

    assert result.startswith("line 1")


def test_read_file_start_line_past_end(numbered_file: Path) -> None:
    assert_error_response(
        read_file(str(numbered_file), start_line=500),
        "START_LINE_OUT_OF_RANGE",
    )


# ============================================================
# Byte cap
# ============================================================


def test_read_file_caps_huge_file(tmp_path: Path) -> None:
    target = tmp_path / "huge.txt"
    line = "x" * 99 + "\n"
    target.write_text(line * ((MAX_READ_BYTES // 100) + 5000), encoding="utf-8")

    result = read_file(str(target), max_lines=1_000_000)

    assert len(result.encode("utf-8")) < MAX_READ_BYTES + 4096
    assert f"{MAX_READ_BYTES:,} byte read limit" in result


def test_byte_cap_does_not_corrupt_multibyte_characters(tmp_path: Path) -> None:
    """The cap must land on a character boundary, not mid-codepoint.

    A fixed byte offset can split a multi-byte character; if that were not
    handled, a perfectly valid UTF-8 file would be reported as bad encoding.
    """

    target = tmp_path / "multibyte.txt"
    # 3 bytes per char in UTF-8, so the cap cannot fall on a clean boundary.
    target.write_text("日" * (MAX_READ_BYTES // 2), encoding="utf-8")

    result = read_file(str(target), max_lines=1_000_000)

    assert "INVALID_TEXT_ENCODING" not in result
    assert result.startswith("日")


# ============================================================
# Errors
# ============================================================


def test_read_file_nonexistent(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "file.txt"

    payload = assert_error_response(read_file(str(missing)), "PATH_NOT_FOUND")

    assert "recovery" in payload["error"]
    assert str(tmp_path) in payload["error"]["message"]


@pytest.mark.parametrize("path", ["", "   "])
def test_read_file_empty_path(path: str) -> None:
    assert_error_response(read_file(path), "INVALID_PATH")


def test_read_file_on_a_directory(tmp_path: Path) -> None:
    assert_error_response(read_file(str(tmp_path)), "PATH_IS_NOT_FILE")


def test_read_file_rejects_non_utf8(tmp_path: Path) -> None:
    target = tmp_path / "binary.bin"
    target.write_bytes(b"\xff\xfe\x00\x01\x80\x81")

    assert_error_response(read_file(str(target)), "INVALID_TEXT_ENCODING")
