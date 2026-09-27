"""Tests for the success-triage additions to the diagnostics renderer."""

from __future__ import annotations

from windows_agent_mcp.diagnostics import (
    Diagnostic,
    _warning_histogram,
    format_report,
    parse_build_output,
    parse_rebuild_kind,
)


def warning(
    file: str,
    code: str,
    *,
    occurrences: int = 1,
    message: str = "deprecated",
) -> Diagnostic:
    """Build a warning with only the fields these tests care about."""

    return Diagnostic(
        file=file,
        line=1,
        column=None,
        severity="warning",
        code=code,
        message=message,
        occurrences=occurrences,
    )


def test_success_report_carries_elapsed_counts_and_rebuild() -> None:
    report = format_report(
        parse_build_output(""),
        header="BUILD: ninja",
        exit_code=0,
        raw_output="",
        elapsed_ms=2500,
        rebuild="incremental",
    )

    assert "(exit code 0, 2.5s)" in report
    assert "BUILD SUCCEEDED (0 errors, 0 warnings)" in report
    assert "rebuild: incremental" in report


def test_histogram_groups_by_code_with_the_top_directory() -> None:
    histogram = _warning_histogram(
        (
            warning("deps/raygui/raygui.h", "C4996", occurrences=5),
            warning("deps/raygui/raygui.h", "C4996", occurrences=2),
            warning("src/renderer/device.cpp", "C4100"),
        )
    )

    assert histogram == "C4996 x7 (deps) | C4100 x1 (src)"


def test_vendored_warning_is_tagged_likely_preexisting() -> None:
    report = format_report(
        parse_build_output(
            "deps/raygui/raygui.h(42): warning C4996: deprecated"
        ),
        header="BUILD: msbuild",
        exit_code=0,
        raw_output="",
    )

    assert "[likely pre-existing]" in report


def test_errors_are_never_tagged_likely_preexisting() -> None:
    """Vendored or benign-coded, an error is still something to look at."""

    report = format_report(
        parse_build_output("deps/raygui/raygui.h(42): error C4996: deprecated"),
        header="BUILD: msbuild",
        exit_code=1,
        raw_output="",
    )

    assert "[likely pre-existing]" not in report


def test_rebuild_kind_is_empty_when_unsure() -> None:
    """A wrong "up-to-date" is a claim the model acts on, so no guessing."""

    assert parse_rebuild_kind("some unrelated output\n") == ""


def test_rebuild_kind_detects_ninja_no_work() -> None:
    assert parse_rebuild_kind("ninja: no work to do.") == "up-to-date"


def test_rebuild_kind_detects_incremental_compile() -> None:
    assert parse_rebuild_kind("[1/3] Building CXX object foo.obj") == "incremental"


def test_rebuild_kind_detects_msbuild_link_as_incremental() -> None:
    """The arrow appears on the link line, which is real work, not a no-op."""

    log = "  MyApp.vcxproj -> C:\\out\\MyApp.exe\nBuild succeeded."

    assert parse_rebuild_kind(log) == "incremental"


def test_rebuild_kind_detects_msbuild_compile_as_incremental() -> None:
    """At /v:minimal a compiled file is a bare path with no arrow."""

    assert parse_rebuild_kind("  renderer.cpp\nBuild succeeded.") == "incremental"


def test_rebuild_kind_reads_a_bare_msbuild_success_as_up_to_date() -> None:
    log = "Build succeeded.\n    0 Warning(s)\n    0 Error(s)"

    assert parse_rebuild_kind(log) == "up-to-date"


def test_rebuild_kind_detects_full_rebuild() -> None:
    assert parse_rebuild_kind("Rebuild All started...") == "full"
