"""Tests for the entry point and the filesystem error handlers.

These are the last paths a normal run does not reach: main()'s registration
loop, and the OSError/PermissionError branches that only fire when the
filesystem misbehaves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import assert_error_response

from windows_agent_mcp import main as main_module
from windows_agent_mcp.main import ALL_TOOLS, TOOLS, main, tool_description
from windows_agent_mcp.tools import list_directory as list_dir_module
from windows_agent_mcp.tools import list_empty_dirs as empty_dirs_module
from windows_agent_mcp.tools import read_file as read_file_module
from windows_agent_mcp.tools.list_directory import list_directory
from windows_agent_mcp.tools.list_empty_dirs import list_empty_dirs
from windows_agent_mcp.tools.read_file import read_file

# ============================================================
# Entry point
# ============================================================


class FakeServer:
    """Records registrations; run() does nothing so no transport starts."""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.descriptions: dict[str, str] = {}
        self.ran = False

    def add_tool(self, fn: Any, description: str | None = None) -> None:
        self.registered.append(fn.__name__)
        self.descriptions[fn.__name__] = description or ""

    def run(self) -> None:
        self.ran = True


@pytest.mark.parametrize("research", [False, True])
def test_main_registers_the_configured_tools_then_runs(
    research: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() is the wiring the whole server depends on.

    run() is stubbed, so this exercises registration without opening stdio.
    Parametrized over both modes because registration is conditional -- the
    count is derived from the tuples rather than hardcoded.
    """

    if research:
        monkeypatch.setenv("BIONIC_WEB_RESEARCH", "1")

    expected = ALL_TOOLS if research else TOOLS

    server = FakeServer()

    monkeypatch.setattr(main_module, "mcp", server)

    main()

    assert server.registered == [tool.__name__ for tool in expected]
    assert server.ran


def test_research_mode_adds_exactly_one_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only web_search is gated now.

    fetch_web_page is registered unconditionally and scoped to the
    documentation hosts instead, so research mode adds one tool, not two.
    """

    monkeypatch.setenv("BIONIC_WEB_RESEARCH", "1")

    server = FakeServer()

    monkeypatch.setattr(main_module, "mcp", server)

    main()

    assert len(server.registered) == len(TOOLS) + 1


@pytest.mark.parametrize("research", [False, True])
def test_main_registers_each_tool_once(
    research: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if research:
        monkeypatch.setenv("BIONIC_WEB_RESEARCH", "1")

    server = FakeServer()

    monkeypatch.setattr(main_module, "mcp", server)

    main()

    assert len(set(server.registered)) == len(server.registered)


# ============================================================
# read_file error handlers
# ============================================================


def test_read_file_permission_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "locked.txt"
    target.write_text("secret", encoding="utf-8")

    def deny(*args: Any, **kwargs: Any):
        raise PermissionError("access denied")

    monkeypatch.setattr(read_file_module.Path, "open", deny)

    assert_error_response(read_file(str(target)), "PERMISSION_DENIED")


def test_read_file_os_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "flaky.txt"
    target.write_text("data", encoding="utf-8")

    def fail(*args: Any, **kwargs: Any):
        raise OSError("device not ready")

    monkeypatch.setattr(read_file_module.Path, "open", fail)

    payload = assert_error_response(read_file(str(target)), "READ_FAILED")

    assert "device not ready" in payload["error"]["message"]


# ============================================================
# list_directory error handlers
# ============================================================


def test_list_directory_permission_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*args: Any, **kwargs: Any):
        raise PermissionError("access denied")

    monkeypatch.setattr(list_dir_module.Path, "iterdir", deny)

    assert_error_response(list_directory(str(tmp_path)), "PERMISSION_DENIED")


def test_list_directory_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any):
        raise OSError("directory handle invalid")

    monkeypatch.setattr(list_dir_module.Path, "iterdir", fail)

    payload = assert_error_response(
        list_directory(str(tmp_path)), "DIRECTORY_READ_FAILED"
    )

    assert "directory handle invalid" in payload["error"]["message"]


# ============================================================
# list_empty_dirs: truncation and errors
# ============================================================


def test_list_empty_dirs_truncates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(empty_dirs_module, "MAX_EMPTY_DIR_RESULTS", 5)

    for index in range(12):
        (tmp_path / f"empty{index:03d}").mkdir()

    result = list_empty_dirs(str(tmp_path))

    assert "showed 5 of" in result
    # The truncation notice must not be mistaken for another path.
    assert len(result.splitlines()) == 5 + 2


def test_list_empty_dirs_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any):
        raise OSError("walk failed")

    monkeypatch.setattr(empty_dirs_module.os, "walk", fail)

    payload = assert_error_response(
        list_empty_dirs(str(tmp_path)), "DIRECTORY_WALK_FAILED"
    )

    assert "walk failed" in payload["error"]["message"]


# ============================================================
# list_directory truncation
# ============================================================


def test_list_directory_truncation_notice_counts_correctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(list_dir_module, "MAX_DIRECTORY_ENTRIES", 3)

    for index in range(10):
        (tmp_path / f"file{index}.txt").write_text("x", encoding="utf-8")

    result = list_directory(str(tmp_path))

    assert "showed 3 of 10 entries" in result
    assert result.count("[file]") == 3


# ============================================================
# Advertised descriptions
# ============================================================


def test_tool_description_drops_the_schema_sections() -> None:
    """Args/Returns/Example duplicate the JSON schema the client receives."""

    from windows_agent_mcp.tools.read_file import read_file

    advertised = tool_description(read_file)

    for section in ("Args:", "Returns:", "Example:"):
        assert section not in advertised


def test_tool_description_keeps_the_steering_prose() -> None:
    """The lines that make a small model pick the RIGHT tool must survive.

    Trimming these would trade context away for exactly the mistakes the
    context was preventing, so each one is pinned.
    """

    from windows_agent_mcp.tools.compile_shader import compile_shader
    from windows_agent_mcp.tools.edit_file import edit_file
    from windows_agent_mcp.tools.search_files import search_files

    assert "Preferred over write_file" in tool_description(edit_file)
    assert "must match EXACTLY" in tool_description(edit_file)
    assert "BIONIC_PROJECT_ROOTS" in tool_description(edit_file)

    assert "where is X used" in tool_description(search_files)

    # The compiler-selection table is what stops a .hlsl call arriving with
    # no profile.
    assert ".hlsl" in tool_description(compile_shader)


def test_every_tool_advertises_something() -> None:
    for tool in ALL_TOOLS:
        advertised = tool_description(tool)

        assert advertised.strip(), f"{tool.__name__} would advertise nothing"


def test_tool_description_falls_back_to_the_full_docstring() -> None:
    """A docstring opening straight into Args: must not advertise nothing."""

    def odd() -> None:
        """Args:
        thing: something
        """

    assert "thing: something" in tool_description(odd)


def test_tool_description_handles_a_missing_docstring() -> None:
    def bare() -> None:
        pass

    assert tool_description(bare) == ""


def test_tool_description_is_shorter_than_the_full_docstring() -> None:
    """The whole point. Asserted in aggregate so one tool cannot mask it."""

    import inspect

    full = sum(len(inspect.getdoc(tool) or "") for tool in ALL_TOOLS)
    advertised = sum(len(tool_description(tool)) for tool in ALL_TOOLS)

    assert advertised < full / 2, (
        f"expected the trim to at least halve description text; "
        f"got {advertised} of {full}"
    )


def test_main_registers_the_trimmed_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trim has to reach add_tool, or it saves nothing on the wire."""

    server = FakeServer()

    monkeypatch.setattr(main_module, "mcp", server)

    main()

    assert "Args:" not in server.descriptions["read_file"]
    assert server.descriptions["read_file"].startswith("Read a UTF-8 text file.")


def test_function_docstrings_are_not_mutated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration must not shorten the real objects.

    Mutating __doc__ would be the easy implementation and would leak across
    the process -- any later reader, including these tests, would see a
    truncated docstring depending on whether main() had run first.
    """

    from windows_agent_mcp.tools.read_file import read_file

    monkeypatch.setattr(main_module, "mcp", FakeServer())

    main()

    assert "Args:" in (read_file.__doc__ or "")


def test_main_logs_a_posture_warning_and_still_starts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A useless-but-legal group combination is reported, not rejected.

    Refusing to start would be worse: an MCP client renders a dead server as an
    opaque connection failure, and the combination does work, it just will not
    do what the operator expects.
    """

    monkeypatch.setenv("BIONIC_TOOLS", "core,search")

    server = FakeServer()
    monkeypatch.setattr(main_module, "mcp", server)

    with caplog.at_level("WARNING"):
        main()

    assert "web_search" in server.registered
    assert "fetch_web_page" not in server.registered
    assert server.ran

    assert any("'docs' is not" in record.message for record in caplog.records)


def test_main_is_silent_on_the_default_posture(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No warning on the configuration everyone gets, or warnings stop working."""

    server = FakeServer()
    monkeypatch.setattr(main_module, "mcp", server)

    with caplog.at_level("WARNING"):
        main()

    assert caplog.records == []
    assert server.ran
