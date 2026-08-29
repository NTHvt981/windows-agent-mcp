"""Tests for the build-output parser.

Every sample here is in the real format the named tool emits. That matters
more than usual: the parser's entire value is turning a specific vendor's log
into structure, so a fixture invented for the parser's convenience would test
nothing. The MSVC and clang samples in particular are the shapes a C++ game
build actually produces, including the MSBuild project tag and the linker's
line-number-free errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from windows_agent_mcp.diagnostics import (
    MAX_ERRORS_SHOWN,
    MAX_MESSAGE_CHARS,
    format_report,
    parse_build_output,
)

# ============================================================
# MSVC
# ============================================================

MSVC_ERROR = (
    r"C:\game\src\renderer\device.cpp(88,12): error C2065: "
    r"'vkCreateDevice': undeclared identifier [C:\game\build\game.vcxproj]"
)

MSVC_WARNING = (
    r"C:\game\src\main.cpp(14): warning C4100: "
    r"'argc': unreferenced formal parameter [C:\game\build\game.vcxproj]"
)

MSVC_FATAL = (
    r"C:\game\src\pch.h(3,10): fatal error C1083: "
    r"Cannot open include file: 'vulkan/vulkan.h': No such file or directory"
)

LINKER_ERROR = (
    r"device.obj : error LNK2019: unresolved external symbol "
    r"vkCreateDevice referenced in function main"
)

LINKER_FATAL = "LINK : fatal error LNK1120: 1 unresolved externals"


def test_msvc_error_is_parsed_with_file_line_and_column() -> None:
    parsed = parse_build_output(MSVC_ERROR)

    assert len(parsed.errors) == 1

    error = parsed.errors[0]

    assert error.file.endswith("src/renderer/device.cpp")
    assert error.line == 88
    assert error.column == 12
    assert error.code == "C2065"
    assert "undeclared identifier" in error.message


def test_msbuild_project_tag_is_stripped() -> None:
    """The tag is identical on every line from a project, so it is pure noise."""

    parsed = parse_build_output(MSVC_ERROR)

    assert "vcxproj" not in parsed.errors[0].message


def test_msvc_warning_without_a_column() -> None:
    parsed = parse_build_output(MSVC_WARNING)

    assert parsed.warnings[0].line == 14
    assert parsed.warnings[0].column is None
    assert parsed.warnings[0].code == "C4100"


def test_fatal_error_is_an_error() -> None:
    parsed = parse_build_output(MSVC_FATAL)

    assert parsed.errors[0].severity == "error"
    assert parsed.errors[0].code == "C1083"


def test_linker_error_has_no_line_but_keeps_its_file() -> None:
    """LNK2019 is the error a small model handles worst, so it must survive."""

    parsed = parse_build_output(LINKER_ERROR)

    assert len(parsed.errors) == 1
    assert parsed.errors[0].code == "LNK2019"
    assert parsed.errors[0].file == "device.obj"
    assert parsed.errors[0].line is None


def test_linker_fatal_with_a_pseudo_file() -> None:
    parsed = parse_build_output(LINKER_FATAL)

    assert parsed.errors[0].code == "LNK1120"


def test_drive_letter_is_not_mistaken_for_a_separator() -> None:
    """A naive split on ":" turns "C:\\x\\a.obj" into the file "C"."""

    parsed = parse_build_output(
        r"C:\game\build\device.obj : error LNK2001: unresolved external symbol foo"
    )

    assert parsed.errors[0].file.endswith("device.obj")


# ============================================================
# clang / gcc
# ============================================================

CLANG_ERROR = (
    "/game/src/renderer/pipeline.cpp:120:9: error: "
    "use of undeclared identifier 'vkCmdDraw'"
)

CLANG_WITH_NOTES = """/game/src/mesh.cpp:44:5: error: no matching function for call to 'load'
/game/src/mesh.h:12:10: note: candidate function not viable: no known conversion
/game/src/mesh.h:18:10: note: candidate function not viable: requires 2 arguments
/game/src/mesh.h:24:10: note: candidate function not viable: third candidate
"""


def test_clang_error_is_parsed() -> None:
    parsed = parse_build_output(CLANG_ERROR)

    assert parsed.errors[0].line == 120
    assert parsed.errors[0].column == 9
    assert parsed.errors[0].code == ""
    assert "vkCmdDraw" in parsed.errors[0].message


def test_clang_windows_path_with_a_drive_letter() -> None:
    parsed = parse_build_output(
        r"C:\game\src\a.cpp:7:3: error: expected ';' after expression"
    )

    assert parsed.errors[0].line == 7
    assert parsed.errors[0].file.endswith("a.cpp")


def test_notes_attach_to_the_preceding_error_and_are_capped() -> None:
    """Overload notes are the useful part; fifty of them are not."""

    parsed = parse_build_output(CLANG_WITH_NOTES)

    assert len(parsed.errors) == 1
    assert len(parsed.errors[0].notes) == 2
    assert "candidate function not viable" in parsed.errors[0].notes[0]

    # A note must never be counted as an error.
    assert parsed.total_errors == 1


def test_a_note_with_no_preceding_error_is_dropped() -> None:
    parsed = parse_build_output("/game/x.h:1:1: note: orphaned note")

    assert parsed.errors == ()
    assert parsed.warnings == ()


# ============================================================
# Deduplication
# ============================================================


def test_identical_errors_collapse_with_a_count() -> None:
    """A bad header repeats once per translation unit; 40 copies is 1 problem."""

    parsed = parse_build_output("\n".join([MSVC_FATAL] * 40))

    assert len(parsed.errors) == 1
    assert parsed.errors[0].occurrences == 40
    assert parsed.total_errors == 40


def test_errors_differing_by_line_are_kept_apart() -> None:
    parsed = parse_build_output("/a.cpp:1:1: error: first\n/a.cpp:2:1: error: second\n")

    assert len(parsed.errors) == 2


def test_first_seen_order_is_preserved() -> None:
    parsed = parse_build_output(
        "/a.cpp:1:1: error: alpha\n/b.cpp:1:1: error: beta\n/a.cpp:1:1: error: alpha\n"
    )

    assert [error.message for error in parsed.errors] == ["alpha", "beta"]


# ============================================================
# Shader compilers
# ============================================================


def test_glslc_output_uses_the_clang_shape() -> None:
    parsed = parse_build_output(
        "shaders/blur.frag:12:5: error: 'outColor' : undeclared identifier"
    )

    assert parsed.errors[0].line == 12
    assert parsed.errors[0].file == "shaders/blur.frag"


def test_glslang_validator_output() -> None:
    parsed = parse_build_output(
        "ERROR: shaders/mesh.vert:9: '' :  syntax error, unexpected IDENTIFIER"
    )

    assert parsed.errors[0].line == 9
    assert parsed.errors[0].severity == "error"


def test_glslang_summary_line_is_not_a_diagnostic() -> None:
    """ "ERROR: 1 compilation errors." has no location and must not parse."""

    parsed = parse_build_output("ERROR: 1 compilation errors.  No code generated.")

    assert parsed.errors == ()
    assert parsed.unparsed


def test_fxc_output_uses_the_msvc_shape() -> None:
    parsed = parse_build_output(
        r"shaders\post.hlsl(31,14): error X3004: undeclared identifier 'gTime'"
    )

    assert parsed.errors[0].code == "X3004"
    assert parsed.errors[0].line == 31


# ============================================================
# CMake and ninja
# ============================================================

CMAKE_LOCATED = """CMake Error at CMakeLists.txt:12 (find_package):
  Could not find a package configuration file provided by "Vulkan" with any
  of the following names:

    VulkanConfig.cmake

-- Configuring incomplete, errors occurred!
"""


def test_cmake_located_error_collects_its_indented_message() -> None:
    parsed = parse_build_output(CMAKE_LOCATED)

    assert len(parsed.errors) == 1

    error = parsed.errors[0]

    assert error.file == "CMakeLists.txt"
    assert error.line == 12
    assert error.code == "find_package"
    assert "Could not find a package configuration file" in error.message
    assert "VulkanConfig.cmake" in error.message


def test_cmake_bare_error() -> None:
    parsed = parse_build_output("CMake Error: Unknown argument --nonsense")

    assert parsed.errors[0].code == "CMake"
    assert "Unknown argument" in parsed.errors[0].message


def test_cmake_dev_warning() -> None:
    parsed = parse_build_output(
        "CMake Warning (dev) at CMakeLists.txt:3 (set):\n  Oops\n"
    )

    assert parsed.warnings[0].severity == "warning"


def test_cmake_block_ends_at_an_unindented_line() -> None:
    parsed = parse_build_output(CMAKE_LOCATED)

    assert "Configuring incomplete" not in parsed.errors[0].message


def test_ninja_failure_is_reported() -> None:
    parsed = parse_build_output("ninja: build stopped: subcommand failed.")

    assert parsed.errors[0].code == "ninja"


# ============================================================
# Report formatting
# ============================================================


def test_success_report_says_so() -> None:
    report = format_report(
        parse_build_output("[1/1] Linking target game\n"),
        header="BUILD: ninja",
        exit_code=0,
        raw_output="[1/1] Linking target game\n",
    )

    assert "BUILD SUCCEEDED" in report


def test_success_with_warnings_marks_them_non_fatal() -> None:
    report = format_report(
        parse_build_output(MSVC_WARNING),
        header="BUILD: msbuild",
        exit_code=0,
        raw_output=MSVC_WARNING,
    )

    assert "BUILD SUCCEEDED" in report
    assert "not fatal" in report
    assert "C4100" in report


def test_failure_report_lists_errors_before_warnings() -> None:
    report = format_report(
        parse_build_output(MSVC_WARNING + "\n" + MSVC_ERROR),
        header="BUILD: msbuild",
        exit_code=1,
        raw_output="",
    )

    assert report.index("ERRORS") < report.index("WARNINGS")
    assert "BUILD FAILED" in report


def test_repeat_count_is_shown() -> None:
    report = format_report(
        parse_build_output("\n".join([MSVC_FATAL] * 7)),
        header="BUILD: msbuild",
        exit_code=1,
        raw_output="",
    )

    assert "(x7)" in report
    assert "1 unique, 7 total" in report


def test_error_list_is_capped() -> None:
    log = "\n".join(
        f"/game/src/f{index}.cpp:1:1: error: problem {index}"
        for index in range(MAX_ERRORS_SHOWN + 15)
    )

    report = format_report(
        parse_build_output(log),
        header="BUILD: ninja",
        exit_code=1,
        raw_output=log,
    )

    assert "15 more unique errors not shown" in report


def test_unrecognised_failure_shows_the_raw_tail() -> None:
    """The critical case: never report "no errors" for a failed build."""

    raw = "\n".join(f"line {index}" for index in range(60))

    report = format_report(
        parse_build_output(raw),
        header="BUILD: ninja",
        exit_code=1,
        raw_output=raw,
    )

    assert "no recognised compiler diagnostics" in report
    assert "line 59" in report
    # It is a tail, because a build announces its failure at the end.
    assert "line 0" not in report


def test_a_very_long_message_is_truncated_for_display() -> None:
    """A real MSVC template error prints the fully expanded type."""

    long_message = "std::vector<std::pair<int, std::string>> " * 40

    parsed = parse_build_output(f"/a.cpp:1:1: error: {long_message}")

    report = format_report(parsed, header="BUILD: clang", exit_code=1, raw_output="")

    assert "[message truncated]" in report
    assert len(report) < 1200

    # Truncation is display-only: the parsed message keeps its full text so
    # two errors differing past the cap still deduplicate separately.
    assert len(parsed.errors[0].message) > MAX_MESSAGE_CHARS


def test_messages_differing_past_the_cap_stay_distinct() -> None:
    prefix = "x" * (MAX_MESSAGE_CHARS + 10)

    parsed = parse_build_output(
        f"/a.cpp:1:1: error: {prefix}alpha\n/a.cpp:1:1: error: {prefix}beta\n"
    )

    assert len(parsed.errors) == 2


def test_notes_are_rendered_under_their_error() -> None:
    report = format_report(
        parse_build_output(CLANG_WITH_NOTES),
        header="BUILD: clang",
        exit_code=1,
        raw_output=CLANG_WITH_NOTES,
    )

    assert "note: " in report


# ============================================================
# Path shortening
# ============================================================


def test_absolute_paths_are_shortened_against_the_build_directory() -> None:
    base = Path(r"C:\game")

    parsed = parse_build_output(MSVC_ERROR, base_directory=base)

    assert parsed.errors[0].file == "src/renderer/device.cpp"


def test_a_path_outside_the_base_is_left_absolute() -> None:
    """A wrong relative path is worse than a long correct one."""

    parsed = parse_build_output(
        r"C:\vulkan-sdk\include\vulkan.h(10,1): error C2059: syntax error",
        base_directory=Path(r"C:\game"),
    )

    assert parsed.errors[0].file.startswith("C:/vulkan-sdk")


@pytest.mark.parametrize("text", ["", "\n", "   \n\t\n"])
def test_empty_output_parses_to_nothing(text: str) -> None:
    parsed = parse_build_output(text)

    assert parsed.errors == ()
    assert parsed.warnings == ()
    assert parsed.total_errors == 0
