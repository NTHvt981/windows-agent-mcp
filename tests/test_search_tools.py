"""Tests for search_files, find_files and the shared walking layer.

The behaviour that matters most on a real C++ project is what is NOT searched.
A game tree's build output dwarfs its source, so a grep that descends into
Intermediate/ or x64/Debug/ returns object-file noise and burns the context
window. Those exclusions are asserted here directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import assert_error_response

from windows_agent_mcp.filescan import iter_files, looks_binary, matches_any_glob
from windows_agent_mcp.tools.find_files import find_files
from windows_agent_mcp.tools.search_files import search_files


@pytest.fixture
def tree(tmp_path) -> Path:
    """A miniature C++ project, including directories that must be skipped."""

    def write(relative: str, content: str) -> None:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    write("src/main.cpp", "#include <vulkan/vulkan.h>\nint main() { return 0; }\n")
    write(
        "src/renderer/swapchain.cpp",
        "void create() {\n    vkCreateSwapchainKHR(device, &info, nullptr, &sc);\n}\n",
    )
    write("src/renderer/swapchain.h", "#pragma once\nvoid create();\n")
    write("shaders/blur.frag", "#version 450\nvoid main() {}\n")
    write("shaders/mesh.vert", "#version 450\nvoid main() {}\n")
    write("CMakeLists.txt", "project(game)\n")

    # Everything below must be invisible to both tools.
    write("build/CMakeCache.txt", "vkCreateSwapchainKHR is mentioned here\n")
    write("build/x64/Debug/main.obj.txt", "vkCreateSwapchainKHR\n")
    write(".git/config", "vkCreateSwapchainKHR\n")
    write("node_modules/pkg/index.js", "vkCreateSwapchainKHR\n")
    write("Intermediate/gen.cpp", "vkCreateSwapchainKHR\n")

    (tmp_path / "src" / "blob.spv").write_bytes(b"\x03\x02#\x07\x00\x00vkCreate")

    return tmp_path


# ============================================================
# search_files
# ============================================================


def test_search_finds_a_match_with_path_and_line(tree) -> None:
    result = search_files("vkCreateSwapchainKHR", str(tree))

    assert "src/renderer/swapchain.cpp:2:" in result
    assert "vkCreateSwapchainKHR(device" in result


def test_search_skips_build_and_vcs_directories(tree) -> None:
    """The core context-economy property of the tool."""

    result = search_files("vkCreateSwapchainKHR", str(tree))

    for excluded in ("build/", ".git/", "node_modules/", "Intermediate/"):
        assert excluded not in result

    assert "1 matches in 1 files" in result


def test_search_skips_binary_files(tree) -> None:
    result = search_files("vkCreate", str(tree))

    assert "blob.spv" not in result
    assert "skipped 1 binary" in result


def test_search_honours_a_file_glob(tree) -> None:
    result = search_files("void", str(tree), "*.h")

    assert "swapchain.h" in result
    assert "swapchain.cpp" not in result


def test_search_accepts_comma_separated_globs(tree) -> None:
    result = search_files("void main", str(tree), "*.frag,*.vert")

    assert "blur.frag" in result
    assert "mesh.vert" in result


def test_search_glob_with_a_slash_anchors_to_the_path(tree) -> None:
    result = search_files("void", str(tree), "src/renderer/*.h")

    assert "swapchain.h" in result
    assert "main.cpp" not in result


def test_search_is_literal_by_default(tree) -> None:
    """Regex metacharacters must not be interpreted unless asked for."""

    result = search_files("main()", str(tree))

    assert "No matches" not in result


def test_search_regex_mode(tree) -> None:
    result = search_files(r"vkCreate\w+KHR", str(tree), regex=True)

    assert "swapchain.cpp" in result


def test_search_rejects_an_invalid_regex(tree) -> None:
    payload = assert_error_response(
        search_files("(unclosed", str(tree), regex=True),
        "INVALID_PATTERN",
    )

    assert "regex=false" in " ".join(payload["error"]["recovery"])


def test_search_ignore_case(tree) -> None:
    assert "No matches" in search_files("VKCREATESWAPCHAINKHR", str(tree))

    result = search_files("VKCREATESWAPCHAINKHR", str(tree), ignore_case=True)

    assert "swapchain.cpp" in result


def test_search_no_matches_explains_the_exclusions(tree) -> None:
    result = search_files("ThisAppearsNowhere", str(tree))

    assert "No matches." in result
    assert "version-control" in result


def test_search_truncates_and_says_so(tree) -> None:
    result = search_files("void", str(tree), max_results=1)

    assert "truncated at 1 results" in result
    assert "Narrow the search" in result


def test_truncation_forbids_reporting_the_list_as_complete(tree) -> None:
    """The notice alone was not enough.

    A model read "truncated at 100 results", tried to narrow, failed, and then
    answered from the truncated list anyway -- counting its lines to state a
    total that was wrong by a third. So the notice says what not to do.
    """

    result = search_files("void", str(tree), max_results=1)

    assert "INCOMPLETE" in result
    assert "do not count these lines" in result.lower()


def test_a_capped_max_results_is_reported(tmp_path) -> None:
    """Raising max_results past the ceiling is silently ignored, so say it.

    Measured: after a truncated search a model asked for 200, then 300, got the
    identical result each time, and never learned the parameter did nothing.
    An ignored argument that looks like it worked is a loop.
    """

    (tmp_path / "many.cpp").write_text("void f();\n" * 150, encoding="utf-8")

    result = search_files("void", str(tmp_path), max_results=500)

    assert "truncated at 100 results" in result
    assert "was capped at" in result
    assert "500" in result
    assert "will not return more" in result


def test_capping_is_silent_when_the_result_was_not_truncated(tree) -> None:
    """Nothing was lost, so the cap did not matter -- saying so is noise."""

    result = search_files("void", str(tree), max_results=500)

    assert "was capped at" not in result


def test_literal_search_with_regex_syntax_says_so(tree) -> None:
    r"""`foo\(` searched literally finds nothing, and the reason is not obvious.

    Measured: a model narrowed a truncated search to `return mcp_error\(`
    without regex=True, got "No matches", and was pointed at excluded build
    directories -- which was not the cause.
    """

    result = search_files(r"void frobnicate\(", str(tree))

    assert "No matches." in result
    assert "regex=True" in result

    # Ahead of the generic advice: when it applies it is almost always the
    # answer, and the generic note sends the reader somewhere else.
    assert result.index("regex=True") < result.index("version-control")


def test_regex_hint_is_silent_when_it_would_mislead(tree) -> None:
    """A bare "(" or "." is ordinary in a literal code search."""

    for pattern in ("frobnicate(", "cfg.value", "MISSING_CONSTANT"):
        result = search_files(pattern, str(tree))

        assert "No matches." in result
        assert "regex=True" not in result


def test_regex_hint_is_silent_when_regex_is_already_on(tree) -> None:
    result = search_files(r"void frobnicate\(", str(tree), regex=True)

    assert "No matches." in result
    assert "regex=True" not in result


def test_search_truncates_a_very_long_line(tmp_path) -> None:
    """One generated line must not consume the whole context window."""

    (tmp_path / "generated.h").write_text("x" * 5000 + "NEEDLE", encoding="utf-8")

    result = search_files("NEEDLE", str(tmp_path))

    assert "[line truncated]" in result
    assert len(result) < 2000


def test_search_survives_an_unreadable_file(tree, monkeypatch) -> None:
    """A locked .pdb mid-build must not abort the whole search."""

    real_open = Path.open

    def flaky(self, *args, **kwargs):
        if self.name == "main.cpp":
            raise PermissionError("locked")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky)

    result = search_files("vkCreateSwapchainKHR", str(tree))

    assert "swapchain.cpp" in result
    assert "1 unreadable" in result


def test_search_rejects_an_empty_pattern() -> None:
    assert_error_response(search_files(""), "INVALID_PATTERN")


def test_search_reports_a_missing_directory() -> None:
    assert_error_response(
        search_files("x", "/no/such/directory/anywhere"),
        "PATH_NOT_FOUND",
    )


def test_search_rejects_a_file_path(tree) -> None:
    payload = assert_error_response(
        search_files("x", str(tree / "CMakeLists.txt")),
        "PATH_IS_NOT_DIRECTORY",
    )

    assert "read_file" in " ".join(payload["error"]["recovery"])


def test_search_decodes_non_utf8_without_failing(tmp_path) -> None:
    """A latin-1 comment must not hide the rest of the file's matches."""

    (tmp_path / "legacy.cpp").write_bytes(b"// caf\xe9\nint NEEDLE = 1;\n")

    result = search_files("NEEDLE", str(tmp_path))

    assert "legacy.cpp:2:" in result


# ============================================================
# find_files
# ============================================================


def test_find_lists_matching_files(tree) -> None:
    result = find_files("*.frag,*.vert", str(tree))

    assert "shaders/blur.frag" in result
    assert "shaders/mesh.vert" in result
    assert "2 files" in result


def test_find_skips_excluded_directories(tree) -> None:
    result = find_files("*.txt", str(tree))

    assert "CMakeLists.txt" in result
    assert "build/" not in result
    assert "CMakeCache" not in result


def test_find_reports_nothing_found_with_a_hint(tree) -> None:
    result = find_files("*.zzz", str(tree))

    assert "No files matched." in result
    assert "excluded" in result


def test_find_truncates(tree) -> None:
    result = find_files("*", str(tree), max_results=2)

    assert "truncated at 2 results" in result


def test_find_rejects_an_empty_glob() -> None:
    payload = assert_error_response(find_files(""), "INVALID_PATTERN")

    assert "list_directory" in " ".join(payload["error"]["recovery"])


def test_find_rejects_a_whitespace_only_glob() -> None:
    assert_error_response(find_files("  ,  "), "INVALID_PATTERN")


def test_find_reports_a_missing_directory() -> None:
    assert_error_response(find_files("*.cpp", "/no/such/dir"), "PATH_NOT_FOUND")


def test_find_rejects_a_file_path(tree) -> None:
    assert_error_response(
        find_files("*", str(tree / "CMakeLists.txt")),
        "PATH_IS_NOT_DIRECTORY",
    )


# ============================================================
# filescan internals
# ============================================================


@pytest.mark.parametrize(
    ("relative", "patterns", "expected"),
    [
        ("src/main.cpp", ["*.cpp"], True),
        ("src/main.cpp", ["*.h"], False),
        ("src/main.cpp", ["src/*.cpp"], True),
        # fnmatch's "*" spans separators, so a slash pattern anchors the
        # prefix without limiting depth. Asserted rather than left implicit,
        # because it differs from pathlib.match and from shell globbing.
        ("src/deep/main.cpp", ["src/*.cpp"], True),
        ("other/main.cpp", ["src/*.cpp"], False),
        ("src/main.cpp", [], True),
        ("a.frag", ["*.vert", "*.frag"], True),
    ],
)
def test_matches_any_glob(relative: str, patterns: list[str], expected: bool) -> None:
    assert matches_any_glob(Path(relative), patterns) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"plain text", False),
        (b"\x03\x02#\x07\x00", True),
        (b"", False),
        # A NUL past the sniff window is not detected, which is the documented
        # tradeoff: the check is a prefix scan so it stays O(1).
        (b"a" * 9000 + b"\x00", False),
    ],
)
def test_looks_binary(raw: bytes, expected: bool) -> None:
    assert looks_binary(raw) is expected


def test_iter_files_stops_at_max_scanned(tree) -> None:
    """The runaway-walk backstop."""

    found = list(iter_files(tree, patterns=[], max_scanned=3))

    assert len(found) <= 3


def test_iter_files_yields_relative_paths(tree) -> None:
    found = dict(iter_files(tree, patterns=["*.frag"]))

    assert [relative.as_posix() for relative in found.values()] == ["shaders/blur.frag"]
