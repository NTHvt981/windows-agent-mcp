"""Tests for the list_empty_dirs tool.

A directory is empty when it holds no files AND every subdirectory it holds
is itself empty. Results are newline-separated text; errors are JSON.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import assert_error_response

from windows_agent_mcp.tools.list_empty_dirs import list_empty_dirs


def _reported(result: str) -> set[str]:
    return set(result.splitlines())


def test_single_empty_folder(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()

    assert str(tmp_path / "empty") in _reported(list_empty_dirs(str(tmp_path)))


def test_folder_with_file_is_not_empty(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "file").write_text("test", encoding="utf-8")

    assert list_empty_dirs(str(tmp_path)) == "no empty directories found"


def test_nested_empty_folders(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    (nested / "child1").mkdir(parents=True)
    (nested / "child2").mkdir()
    (nested / "child2" / "file").write_text("test", encoding="utf-8")

    reported = _reported(list_empty_dirs(str(tmp_path)))

    # child1 is empty; child2 holds a file, so neither it nor its ancestors
    # qualify.
    assert reported == {str(nested / "child1")}


def test_root_itself_when_empty(tmp_path: Path) -> None:
    assert str(tmp_path) in _reported(list_empty_dirs(str(tmp_path)))


def test_parent_of_only_empty_dirs_is_empty(tmp_path: Path) -> None:
    """Rule 2: a directory holding nothing but empty directories is empty."""

    parent = tmp_path / "parent"
    (parent / "child_empty").mkdir(parents=True)

    reported = _reported(list_empty_dirs(str(tmp_path)))

    assert str(parent) in reported
    assert str(parent / "child_empty") in reported
    # The search root qualifies too, since parent is empty.
    assert str(tmp_path) in reported


def test_deep_chain_of_empty_dirs_reported_entirely(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)

    reported = _reported(list_empty_dirs(str(tmp_path)))

    for expected in (
        tmp_path / "a",
        tmp_path / "a" / "b",
        tmp_path / "a" / "b" / "c",
        deep,
    ):
        assert str(expected) in reported


def test_file_deep_in_tree_disqualifies_all_ancestors(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "keep.txt").write_text("x", encoding="utf-8")

    assert list_empty_dirs(str(tmp_path)) == "no empty directories found"


def test_children_reported_before_parents(tmp_path: Path) -> None:
    """Deepest-first ordering makes the output safe to delete in sequence."""

    (tmp_path / "a" / "b").mkdir(parents=True)

    lines = list_empty_dirs(str(tmp_path)).splitlines()

    assert lines.index(str(tmp_path / "a" / "b")) < lines.index(str(tmp_path / "a"))


# ============================================================
# Errors
# ============================================================


@pytest.mark.parametrize("path", ["", "   "])
def test_empty_path_is_rejected(path: str) -> None:
    assert_error_response(list_empty_dirs(path), "INVALID_PATH")


def test_nonexistent_path(tmp_path: Path) -> None:
    assert_error_response(list_empty_dirs(str(tmp_path / "nope")), "PATH_NOT_FOUND")


def test_file_path_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")

    assert_error_response(list_empty_dirs(str(target)), "PATH_IS_NOT_DIRECTORY")
