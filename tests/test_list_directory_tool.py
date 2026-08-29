"""Tests for the list_directory tool.

On success list_directory returns a plain text listing. Only errors are JSON.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import assert_error_response

from windows_agent_mcp.tools.list_directory import list_directory
from windows_agent_mcp.utils import MAX_DIRECTORY_ENTRIES


def test_list_directory_lists_files_and_dirs(tmp_path: Path) -> None:
    (tmp_path / "file1.txt").write_text("content1", encoding="utf-8")
    (tmp_path / "file2.py").write_text("print('hello')", encoding="utf-8")
    (tmp_path / "subdir").mkdir()

    result = list_directory(str(tmp_path))

    assert f"DIRECTORY: {tmp_path}" in result
    assert "[dir ] subdir" in result
    assert "[file] file1.txt" in result
    assert "[file] file2.py" in result
    assert "3 entries" in result


def test_list_directory_sorts_directories_first(tmp_path: Path) -> None:
    (tmp_path / "aaa.txt").write_text("x", encoding="utf-8")
    (tmp_path / "zzz_dir").mkdir()

    result = list_directory(str(tmp_path))

    assert result.index("zzz_dir") < result.index("aaa.txt")


def test_list_directory_empty(tmp_path: Path) -> None:
    result = list_directory(str(tmp_path))

    assert "(empty)" in result
    assert "0 entries" in result


def test_list_directory_current() -> None:
    assert "DIRECTORY:" in list_directory(".")


@pytest.mark.parametrize("path", ["", "   "])
def test_list_directory_empty_path_defaults_to_cwd(path: str) -> None:
    """An empty path falls back to "." rather than erroring."""

    assert "DIRECTORY:" in list_directory(path)


def test_list_directory_truncates_large_directories(tmp_path: Path) -> None:
    total = MAX_DIRECTORY_ENTRIES + 25

    for index in range(total):
        (tmp_path / f"file{index:05d}.txt").write_text("x", encoding="utf-8")

    result = list_directory(str(tmp_path))

    assert f"showed {MAX_DIRECTORY_ENTRIES} of {total} entries" in result
    assert result.count("[file]") == MAX_DIRECTORY_ENTRIES


def test_list_directory_nonexistent(tmp_path: Path) -> None:
    assert_error_response(list_directory(str(tmp_path / "nope")), "PATH_NOT_FOUND")


def test_list_directory_on_a_file(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")

    assert_error_response(list_directory(str(target)), "PATH_IS_NOT_DIRECTORY")
