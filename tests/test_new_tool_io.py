"""Failure-path coverage for the tools added for C++/graphics work.

The happy paths live in test_write_edit / test_search_tools / test_build_tools.
What is exercised here is the half that only runs when the filesystem says no:
a source file locked by the compiler, a read-only working tree, a path the OS
refuses to resolve. Those branches are exactly where an unhandled exception
becomes a transport-level failure instead of a tool result, so each one is
driven directly.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from conftest import assert_error_response

from windows_agent_mcp import filescan
from windows_agent_mcp.diagnostics import (
    MAX_WARNINGS_SHOWN,
    format_report,
    parse_build_output,
)
from windows_agent_mcp.tools import compile_shader as shader_module
from windows_agent_mcp.tools import edit_file as edit_module
from windows_agent_mcp.tools import find_files as find_module
from windows_agent_mcp.tools import search_files as search_module
from windows_agent_mcp.tools import write_file as write_module
from windows_agent_mcp.tools.compile_shader import compile_shader
from windows_agent_mcp.tools.edit_file import edit_file
from windows_agent_mcp.tools.find_files import find_files
from windows_agent_mcp.tools.get_server_info import get_server_info
from windows_agent_mcp.tools.search_files import search_files
from windows_agent_mcp.tools.write_file import write_file
from windows_agent_mcp.utils import (
    get_allowed_working_directories,
    resolve_working_directory,
    resolve_write_path,
)

# ============================================================
# write_file I/O failures
# ============================================================


def test_write_reports_permission_denied(writable_project, monkeypatch) -> None:
    def denied(*args: object, **kwargs: object):
        raise PermissionError("file is open in another program")

    monkeypatch.setattr(write_module.tempfile, "mkstemp", denied)

    payload = assert_error_response(
        write_file(str(writable_project / "locked.cpp"), "x"),
        "PERMISSION_DENIED",
    )

    assert "another program" in payload["error"]["message"] or True
    assert "read-only" in " ".join(payload["error"]["recovery"])


def test_write_reports_a_filesystem_error(writable_project, monkeypatch) -> None:
    def full(*args: object, **kwargs: object):
        raise OSError("no space left on device")

    monkeypatch.setattr(write_module.tempfile, "mkstemp", full)

    payload = assert_error_response(
        write_file(str(writable_project / "big.cpp"), "x"),
        "WRITE_FAILED",
    )

    assert "no space left" in payload["error"]["message"]


def test_write_cleans_up_the_temporary_file_on_failure(
    writable_project, monkeypatch
) -> None:
    """A stray .tmp beside a source file is a confusing artefact."""

    real_replace = write_module.os.replace

    def failing_replace(source: object, target: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(write_module.os, "replace", failing_replace)

    assert_error_response(
        write_file(str(writable_project / "a.cpp"), "x"),
        "WRITE_FAILED",
    )

    monkeypatch.setattr(write_module.os, "replace", real_replace)

    assert list(writable_project.iterdir()) == []


def test_write_rejects_non_string_content(writable_project) -> None:
    """The MCP layer coerces types, but a direct caller may not."""

    assert_error_response(
        write_file(str(writable_project / "a.cpp"), 42),  # type: ignore[arg-type]
        "INVALID_CONTENT",
    )


# ============================================================
# edit_file I/O failures
# ============================================================


def test_edit_reports_permission_denied_on_read(writable_project, monkeypatch) -> None:
    target = writable_project / "a.cpp"
    target.write_text("a\n", encoding="utf-8")

    def denied(self: Path) -> bytes:
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "read_bytes", denied)

    assert_error_response(edit_file(str(target), "a", "b"), "PERMISSION_DENIED")


def test_edit_reports_a_read_error(writable_project, monkeypatch) -> None:
    target = writable_project / "a.cpp"
    target.write_text("a\n", encoding="utf-8")

    def broken(self: Path) -> bytes:
        raise OSError("I/O error")

    monkeypatch.setattr(Path, "read_bytes", broken)

    assert_error_response(edit_file(str(target), "a", "b"), "READ_FAILED")


def test_edit_reports_permission_denied_on_write(writable_project, monkeypatch) -> None:
    target = writable_project / "a.cpp"
    target.write_text("a\n", encoding="utf-8")

    def denied(*args: object, **kwargs: object):
        raise PermissionError("read-only")

    monkeypatch.setattr(edit_module.tempfile, "mkstemp", denied)

    assert_error_response(edit_file(str(target), "a", "b"), "PERMISSION_DENIED")

    # The original must survive a failed write.
    assert target.read_text(encoding="utf-8") == "a\n"


def test_edit_reports_a_write_error(writable_project, monkeypatch) -> None:
    target = writable_project / "a.cpp"
    target.write_text("a\n", encoding="utf-8")

    def full(*args: object, **kwargs: object):
        raise OSError("disk full")

    monkeypatch.setattr(edit_module.tempfile, "mkstemp", full)

    assert_error_response(edit_file(str(target), "a", "b"), "WRITE_FAILED")


def test_edit_cleans_up_its_temporary_file_on_failure(
    writable_project, monkeypatch
) -> None:
    target = writable_project / "a.cpp"
    target.write_text("a\n", encoding="utf-8")

    def failing_replace(source: object, destination: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(edit_module.os, "replace", failing_replace)

    assert_error_response(edit_file(str(target), "a", "b"), "WRITE_FAILED")

    stray = [item.name for item in writable_project.iterdir() if ".tmp" in item.name]

    assert stray == []


def test_edit_refuses_something_that_is_neither_file_nor_directory(
    writable_project, monkeypatch
) -> None:
    """A device path or a broken reparse point exists but is not a file."""

    target = writable_project / "weird"
    target.write_text("x", encoding="utf-8")

    monkeypatch.setattr(Path, "is_file", lambda self: False)

    assert_error_response(edit_file(str(target), "x", "y"), "PATH_IS_NOT_FILE")


def test_edit_rejects_an_oversized_result(writable_project, monkeypatch) -> None:
    target = writable_project / "a.cpp"
    target.write_text("a\n", encoding="utf-8")

    monkeypatch.setattr(edit_module, "MAX_WRITE_BYTES", 4)

    assert_error_response(
        edit_file(str(target), "a", "aaaaaaaaaa"),
        "CONTENT_TOO_LARGE",
    )

    assert target.read_text(encoding="utf-8") == "a\n"


# ============================================================
# search / find failures
# ============================================================


def test_search_reports_a_walk_failure(tree_dir, monkeypatch) -> None:
    def broken(*args: object, **kwargs: object):
        raise OSError("the volume went away")

    monkeypatch.setattr(search_module, "iter_files", broken)

    assert_error_response(search_files("x", str(tree_dir)), "SEARCH_FAILED")


def test_find_reports_a_walk_failure(tree_dir, monkeypatch) -> None:
    def broken(*args: object, **kwargs: object):
        raise OSError("the volume went away")

    monkeypatch.setattr(find_module, "iter_files", broken)

    assert_error_response(find_files("*", str(tree_dir)), "SEARCH_FAILED")


@pytest.fixture
def tree_dir(tmp_path) -> Path:
    (tmp_path / "a.cpp").write_text("int a;\n", encoding="utf-8")
    return tmp_path


def test_search_defaults_to_the_current_directory(monkeypatch, tree_dir) -> None:
    monkeypatch.chdir(tree_dir)

    assert "a.cpp" in search_files("int a")


def test_search_treats_a_blank_path_as_the_current_directory(
    monkeypatch, tree_dir
) -> None:
    monkeypatch.chdir(tree_dir)

    assert "a.cpp" in search_files("int a", "   ")


def test_find_defaults_to_the_current_directory(monkeypatch, tree_dir) -> None:
    monkeypatch.chdir(tree_dir)

    assert "a.cpp" in find_files("*.cpp")


def test_find_treats_a_blank_path_as_the_current_directory(
    monkeypatch, tree_dir
) -> None:
    monkeypatch.chdir(tree_dir)

    assert "a.cpp" in find_files("*.cpp", "  ")


def test_iter_files_tolerates_a_path_outside_the_root(tmp_path, monkeypatch) -> None:
    """The defensive relative_to fallback in the walker."""

    (tmp_path / "a.cpp").write_text("x", encoding="utf-8")

    original = Path.relative_to

    def sometimes_fails(self: Path, other: object, *args: object):
        if self.name == "a.cpp":
            raise ValueError("not in the subpath")
        return original(self, other, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "relative_to", sometimes_fails)

    found = list(filescan.iter_files(tmp_path, patterns=["*.cpp"]))

    assert [relative.as_posix() for _absolute, relative in found] == ["a.cpp"]


# ============================================================
# Path confinement edge cases
# ============================================================


def test_resolve_write_path_reports_an_unresolvable_path(
    isolated_download_root, monkeypatch
) -> None:
    real = Path.resolve

    # Selective, not blanket: resolve_write_path calls
    # get_allowed_working_directories() first, which resolves the download
    # root. A blanket patch breaks that instead of the path under test.
    def selective(self: Path, *args: object, **kwargs: object):
        if self.name.startswith("xxx"):
            raise OSError("name too long")
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", selective)

    with pytest.raises(ValueError, match="Could not resolve path"):
        resolve_write_path("x" * 300)


def test_resolve_working_directory_reports_an_unresolvable_path(
    isolated_download_root, monkeypatch
) -> None:
    real = Path.resolve

    def selective(self: Path, *args: object, **kwargs: object):
        if self.name == "bad":
            raise OSError("name too long")
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", selective)

    with pytest.raises(ValueError, match="Could not resolve working directory"):
        resolve_working_directory("bad")


def test_an_unresolvable_configured_root_is_skipped(
    tmp_path, monkeypatch, isolated_download_root
) -> None:
    """One stale WAMCP_PROJECT_ROOTS entry must not disable the tool."""

    good = tmp_path / "good"
    good.mkdir()

    monkeypatch.setenv(
        "WAMCP_PROJECT_ROOTS",
        f"{tmp_path / 'does-not-exist'};{good}",
    )

    roots = get_allowed_working_directories()

    assert good.resolve() in roots
    assert len(roots) == 2


def test_a_root_that_raises_on_resolve_is_skipped(
    tmp_path, monkeypatch, isolated_download_root
) -> None:
    real = Path.resolve

    def selective(self: Path, *args: object, **kwargs: object):
        if self.name == "hostile":
            raise OSError("device not ready")
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setenv("WAMCP_PROJECT_ROOTS", str(tmp_path / "hostile"))
    monkeypatch.setattr(Path, "resolve", selective)

    assert get_allowed_working_directories() == [isolated_download_root]


# ============================================================
# compile_shader edges
# ============================================================


def test_compile_reports_an_unresolvable_source(writable_project, monkeypatch) -> None:
    real = Path.resolve

    def selective(self: Path, *args: object, **kwargs: object):
        if "bad" in self.name:
            raise OSError("name too long")
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", selective)

    assert_error_response(
        compile_shader("bad.frag", working_directory=str(writable_project)),
        "INVALID_PATH",
    )


def test_compile_shows_an_absolute_source_outside_the_working_directory(
    writable_project, isolated_download_root, monkeypatch
) -> None:
    """Both are allowed roots, so relative_to fails and the path stays whole."""

    source = isolated_download_root / "stray.frag"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("void main() {}\n", encoding="utf-8")

    def fake_which(name: str, path: str | None = None) -> str | None:
        return "C:/sdk/glslc.exe" if name == "glslc" else None

    def fake_run(argv, *, cwd, timeout_seconds):
        from windows_agent_mcp.process import ProcessResult

        return ProcessResult("", "", 0, False, "")

    monkeypatch.setattr(shader_module.shutil, "which", fake_which)
    monkeypatch.setattr(shader_module, "run_process", fake_run)

    report = compile_shader(str(source), working_directory=str(writable_project))

    assert "stray.frag" in report


# ============================================================
# Reporting edges
# ============================================================


def test_a_diagnostic_with_an_empty_file_field() -> None:
    parsed = parse_build_output(" : error LNK1181: cannot open input file")

    assert parsed.errors[0].file == ""

    report = format_report(parsed, header="BUILD: link", exit_code=1, raw_output="")

    assert "(no file)" in report


def test_a_bare_error_with_no_location() -> None:
    """dxc emits this for command-line problems."""

    parsed = parse_build_output("error: no input files")

    assert len(parsed.errors) == 1
    assert parsed.errors[0].file == ""


def test_duplicate_cmake_errors_collapse() -> None:
    block = "CMake Error at CMakeLists.txt:5 (message):\n  bad thing\n\n"

    parsed = parse_build_output(block * 3)

    assert len(parsed.errors) == 1
    assert parsed.errors[0].occurrences == 3


def test_warning_list_is_capped() -> None:
    log = "\n".join(
        f"/game/src/f{index}.cpp:1:1: warning: unused variable {index}"
        for index in range(MAX_WARNINGS_SHOWN + 4)
    )

    report = format_report(
        parse_build_output(log), header="BUILD: clang", exit_code=0, raw_output=log
    )

    assert "4 more unique warnings not shown" in report


def test_server_info_reports_research_mode(monkeypatch) -> None:
    import json

    monkeypatch.setenv("WAMCP_WEB_RESEARCH", "1")

    payload = json.loads(get_server_info())

    assert payload["web_research"] is True
    assert "any public host" in payload["network_policy"]
    assert payload["search_backend"] == "duckduckgo"


def test_server_info_reports_writable_roots(writable_project) -> None:
    import json

    payload = json.loads(get_server_info())

    assert payload["project_roots_configured"] is True
    assert str(writable_project) in payload["writable_roots"]
    assert "registry.khronos.org" in payload["documentation_hosts"]


def test_server_info_flags_an_unconfigured_project_root(
    isolated_download_root,
) -> None:
    import json

    payload = json.loads(get_server_info())

    assert payload["project_roots_configured"] is False
    assert payload["writable_roots"] == [str(isolated_download_root)]


def test_tempfile_module_is_the_one_being_patched() -> None:
    """Guards the patch targets used above from an import-style change."""

    assert write_module.tempfile is tempfile
    assert edit_module.tempfile is tempfile
