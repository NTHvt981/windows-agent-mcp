"""Tests for write_file and edit_file.

The security property under test is confinement: with BIONIC_PROJECT_ROOTS
unset, the only writable place is the download sandbox, so a default install
cannot modify source anywhere. The correctness property is line endings --
this is a Windows-targeted server and silently converting a CRLF file to LF
would show up as a whole-file diff in the user's repository.
"""

from __future__ import annotations

import pytest
from conftest import assert_error_response

from windows_agent_mcp.tools.edit_file import edit_file
from windows_agent_mcp.tools.write_file import write_file

# ============================================================
# Confinement
# ============================================================


def test_write_refuses_outside_every_root(tmp_path, isolated_download_root) -> None:
    """With no project root configured, an arbitrary path must be refused."""

    target = tmp_path / "outside" / "evil.cpp"

    payload = assert_error_response(
        write_file(str(target), "int main() {}"),
        "WRITE_PATH_NOT_ALLOWED",
    )

    assert "BIONIC_PROJECT_ROOTS" in " ".join(payload["error"]["recovery"])
    assert not target.exists()


def test_write_allows_the_download_root(isolated_download_root) -> None:
    """The download sandbox is writable by default, and is the only place."""

    target = isolated_download_root / "notes.txt"

    result = write_file(str(target), "hello")

    assert "WROTE:" in result
    assert target.read_text(encoding="utf-8") == "hello"


def test_write_allows_a_configured_project_root(writable_project) -> None:
    target = writable_project / "src" / "main.cpp"

    result = write_file(str(target), "int main() { return 0; }\n")

    assert "new file" in result
    assert target.read_text(encoding="utf-8") == "int main() { return 0; }\n"


def test_write_creates_missing_parents(writable_project) -> None:
    target = writable_project / "src" / "renderer" / "vk" / "device.h"

    write_file(str(target), "#pragma once\n")

    assert target.is_file()


def test_write_refuses_a_parent_escape(writable_project) -> None:
    """ ".." must be resolved before the containment test, not after."""

    escape = str(writable_project / ".." / "escaped.txt")

    assert_error_response(write_file(escape, "x"), "WRITE_PATH_NOT_ALLOWED")

    assert not (writable_project.parent / "escaped.txt").exists()


def test_write_refuses_an_existing_directory(writable_project) -> None:
    (writable_project / "src").mkdir()

    assert_error_response(
        write_file(str(writable_project / "src"), "x"),
        "WRITE_PATH_NOT_ALLOWED",
    )


@pytest.mark.parametrize("path", ["", "   "])
def test_write_refuses_an_empty_path(path: str) -> None:
    """Checked before the roots are resolved, so this touches no filesystem."""

    assert_error_response(write_file(path, "x"), "WRITE_PATH_NOT_ALLOWED")


# ============================================================
# Overwrite policy
# ============================================================


def test_write_refuses_to_clobber_by_default(writable_project) -> None:
    target = writable_project / "main.cpp"
    target.write_text("original", encoding="utf-8")

    payload = assert_error_response(
        write_file(str(target), "replacement"),
        "FILE_EXISTS",
    )

    assert "edit_file" in " ".join(payload["error"]["recovery"])

    # The whole point: the original survives.
    assert target.read_text(encoding="utf-8") == "original"


def test_write_overwrites_when_asked(writable_project) -> None:
    target = writable_project / "main.cpp"
    target.write_text("original", encoding="utf-8")

    result = write_file(str(target), "replacement", overwrite=True)

    assert "overwritten" in result
    assert target.read_text(encoding="utf-8") == "replacement"


def test_write_rejects_content_over_the_cap(writable_project, monkeypatch) -> None:
    monkeypatch.setattr(
        "windows_agent_mcp.tools.write_file.MAX_WRITE_BYTES",
        16,
    )

    target = writable_project / "big.cpp"

    assert_error_response(
        write_file(str(target), "x" * 64),
        "CONTENT_TOO_LARGE",
    )

    assert not target.exists()


def test_write_leaves_no_temporary_file_behind(writable_project) -> None:
    """The atomic-rename implementation must not litter .tmp files."""

    write_file(str(writable_project / "a.cpp"), "int a;\n")

    stray = [item.name for item in writable_project.iterdir() if ".tmp" in item.name]

    assert stray == []


def test_write_does_not_translate_newlines(writable_project) -> None:
    """Text mode on Windows would turn every \\n into \\r\\n behind our back."""

    target = writable_project / "lf.cpp"

    write_file(str(target), "a\nb\n")

    assert target.read_bytes() == b"a\nb\n"


def test_write_handles_non_ascii(writable_project) -> None:
    target = writable_project / "utf8.txt"

    write_file(str(target), "é中文\n")

    assert target.read_text(encoding="utf-8") == "é中文\n"


# ============================================================
# edit_file basics
# ============================================================


def test_edit_replaces_a_unique_string(writable_project) -> None:
    target = writable_project / "main.cpp"
    target.write_text("int a = 1;\nint b = 2;\n", encoding="utf-8")

    result = edit_file(str(target), "int b = 2;", "int b = 3;")

    assert "1 replacement at line 2" in result
    assert target.read_text(encoding="utf-8") == "int a = 1;\nint b = 3;\n"


def test_edit_refuses_a_non_unique_string(writable_project) -> None:
    target = writable_project / "main.cpp"
    target.write_text("x;\nx;\n", encoding="utf-8")

    payload = assert_error_response(
        edit_file(str(target), "x;", "y;"),
        "EDIT_STRING_NOT_UNIQUE",
    )

    assert "2 times" in payload["error"]["message"]

    # Nothing changed: refusing beats guessing.
    assert target.read_text(encoding="utf-8") == "x;\nx;\n"


def test_edit_replace_all_changes_every_occurrence(writable_project) -> None:
    target = writable_project / "main.cpp"
    target.write_text("x;\nx;\nx;\n", encoding="utf-8")

    result = edit_file(str(target), "x;", "y;", replace_all=True)

    assert "3 replacements" in result
    assert target.read_text(encoding="utf-8") == "y;\ny;\ny;\n"


def test_edit_reports_a_missing_string_with_guidance(writable_project) -> None:
    target = writable_project / "main.cpp"
    target.write_text("int a = 1;\n", encoding="utf-8")

    payload = assert_error_response(
        edit_file(str(target), "int zzz = 9;", "int a = 2;"),
        "EDIT_STRING_NOT_FOUND",
    )

    recovery = " ".join(payload["error"]["recovery"])

    assert "read_file" in recovery
    assert "DO NOT retry" in recovery


def test_edit_can_delete_text(writable_project) -> None:
    target = writable_project / "main.cpp"
    target.write_text("keep\ndrop\n", encoding="utf-8")

    edit_file(str(target), "drop\n", "")

    assert target.read_text(encoding="utf-8") == "keep\n"


def test_edit_rejects_a_no_op(writable_project) -> None:
    """A model that loops on an identical edit never makes progress."""

    target = writable_project / "main.cpp"
    target.write_text("a\n", encoding="utf-8")

    assert_error_response(edit_file(str(target), "a", "a"), "INVALID_EDIT")


def test_edit_rejects_an_empty_old_string(writable_project) -> None:
    target = writable_project / "main.cpp"
    target.write_text("a\n", encoding="utf-8")

    assert_error_response(edit_file(str(target), "", "b"), "INVALID_EDIT")


def test_edit_requires_the_file_to_exist(writable_project) -> None:
    payload = assert_error_response(
        edit_file(str(writable_project / "missing.cpp"), "a", "b"),
        "PATH_NOT_FOUND",
    )

    assert "write_file" in " ".join(payload["error"]["recovery"])


def test_edit_refuses_a_directory(writable_project) -> None:
    (writable_project / "src").mkdir()

    assert_error_response(
        edit_file(str(writable_project / "src"), "a", "b"),
        "WRITE_PATH_NOT_ALLOWED",
    )


def test_edit_refuses_outside_every_root(tmp_path, isolated_download_root) -> None:
    outside = tmp_path / "outside.cpp"
    outside.write_text("secret\n", encoding="utf-8")

    assert_error_response(
        edit_file(str(outside), "secret", "leaked"),
        "WRITE_PATH_NOT_ALLOWED",
    )

    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_edit_rejects_binary(writable_project) -> None:
    target = writable_project / "blob.spv"
    target.write_bytes(b"\x03\x02#\x07\x00\xff\xfe")

    assert_error_response(
        edit_file(str(target), "a", "b"),
        "INVALID_TEXT_ENCODING",
    )


# ============================================================
# Line endings
# ============================================================


def test_edit_preserves_crlf(writable_project) -> None:
    """The regression this exists for: a CRLF file must stay CRLF."""

    target = writable_project / "crlf.cpp"
    target.write_bytes(b"int a = 1;\r\nint b = 2;\r\n")

    result = edit_file(str(target), "int b = 2;", "int b = 3;")

    assert "CRLF preserved" in result
    assert target.read_bytes() == b"int a = 1;\r\nint b = 3;\r\n"


def test_edit_matches_lf_strings_against_a_crlf_file(writable_project) -> None:
    """A multi-line LF old_string must still match inside a CRLF file.

    Without newline normalisation this is the most common possible failure:
    the model passes "\\n", the file has "\\r\\n", nothing matches, and the
    error message gives no hint why.
    """

    target = writable_project / "crlf.cpp"
    target.write_bytes(b"void f()\r\n{\r\n    old();\r\n}\r\n")

    result = edit_file(str(target), "{\n    old();\n}", "{\n    new();\n}")

    assert "EDITED" in result
    assert target.read_bytes() == b"void f()\r\n{\r\n    new();\r\n}\r\n"


def test_edit_preserves_lf(writable_project) -> None:
    target = writable_project / "lf.cpp"
    target.write_bytes(b"a\nb\n")

    result = edit_file(str(target), "b", "c")

    assert "LF preserved" in result
    assert target.read_bytes() == b"a\nc\n"


def test_edit_inserting_crlf_strings_into_an_lf_file(writable_project) -> None:
    """The reverse direction: CRLF input must not contaminate an LF file."""

    target = writable_project / "lf.cpp"
    target.write_bytes(b"a\nb\n")

    edit_file(str(target), "b", "b\r\nc")

    assert target.read_bytes() == b"a\nb\nc\n"


def test_edit_of_a_mixed_file_follows_the_majority(writable_project) -> None:
    target = writable_project / "mixed.cpp"
    target.write_bytes(b"a\r\nb\r\nc\nTARGET\r\n")

    edit_file(str(target), "TARGET", "DONE")

    assert target.read_bytes() == b"a\r\nb\r\nc\r\nDONE\r\n"


def test_edit_reports_the_first_line_of_a_multi_replacement(
    writable_project,
) -> None:
    target = writable_project / "main.cpp"
    target.write_text("a\nb\nx\nd\nx\n", encoding="utf-8")

    result = edit_file(str(target), "x", "y", replace_all=True)

    assert "first at line 3" in result
