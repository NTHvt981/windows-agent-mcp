"""Parse compiler, linker and build-system output into structured diagnostics.

Why this module exists: `run_powershell("cmake --build .")` already works, but
it returns up to 64 KB of raw log. A C++ game project routinely emits hundreds
of warnings, a single template error can run fifty lines, and MSVC repeats a
bad header's error once per translation unit. For a 7B-9B model that log IS
the context window, and the three lines that matter are buried in it.

So the tools here run the build and return only what a caller can act on:
unique errors first, deduplicated with a repeat count, capped, warnings after,
and -- crucially -- a tail of raw output whenever nothing could be parsed, so
an unrecognised failure is never reported as success.

Pure functions: text in, diagnostics out. No subprocess, no filesystem.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

__all__: list[str] = [
    "Diagnostic",
    "ParsedOutput",
    "format_report",
    "parse_build_output",
]

# Maximum diagnostics reproduced in a report. Past this the model has plenty
# to work on, and a 400-error build is one root cause anyway.
MAX_ERRORS_SHOWN = 20
MAX_WARNINGS_SHOWN = 10

# Notes shown per error. clang attaches "candidate function not viable" notes
# to overload failures, which are the most useful thing in the log -- but a
# failed template instantiation can attach fifty, which is pure noise.
MAX_NOTES_PER_ERROR = 2

# Raw lines shown when nothing parsed. Build failures announce themselves at
# the END of the log, so this is a tail, not a head.
MAX_RAW_TAIL_LINES = 25

# Display cap on one message. Applied at render time, not at parse time, so
# the deduplication key stays the full text and two errors differing only past
# the cap do not merge.
#
# Not defensive: a real MSVC template error prints the fully expanded type,
# which runs to thousands of characters, and a CMake find_package failure with
# its candidate filenames comfortably exceeds 500. Twenty of those is the
# entire context window of the model this server targets.
MAX_MESSAGE_CHARS = 500


class Diagnostic(NamedTuple):
    """One compiler, linker or build-system message.

    Attributes:
        file: Source path as reported, normalised to forward slashes, or ""
            for messages with no location (many linker and CMake errors).
        line: 1-based line, or None when the tool did not report one.
        column: 1-based column, or None.
        severity: "error", "warning" or "note".
        code: Tool-specific code such as "C2065", "LNK2019" or "X3004".
            Empty for clang and CMake, which do not emit codes.
        message: The message text, with MSVC's trailing project tag removed.
        notes: Related "note:" lines, already capped.
        occurrences: How many times this exact diagnostic appeared.
            Named 'occurrences' rather than 'count' because NamedTuple
            inherits tuple.count(), and shadowing it is a type error.
    """

    file: str
    line: int | None
    column: int | None
    severity: str
    code: str
    message: str
    notes: tuple[str, ...] = ()
    occurrences: int = 1


class ParsedOutput(NamedTuple):
    """Everything extracted from one build log.

    Attributes:
        errors: Unique errors, first-seen order.
        warnings: Unique warnings, first-seen order.
        total_errors: Errors including duplicates.
        total_warnings: Warnings including duplicates.
        unparsed: Lines no pattern matched, for the raw-tail fallback.
    """

    errors: tuple[Diagnostic, ...]
    warnings: tuple[Diagnostic, ...]
    total_errors: int
    total_warnings: int
    unparsed: tuple[str, ...]


# MSVC's cl.exe, fxc.exe and MSBuild all use file(line,col): sev CODE: msg.
# The optional column is real -- older MSVC and some tools emit file(line).
_MSVC_PATTERN = re.compile(
    r"^\s*(?P<file>(?:[A-Za-z]:)?[^(]+?)"
    r"\((?P<line>\d+)(?:,(?P<column>\d+))?\)"
    r"\s*:\s*(?P<severity>fatal error|error|warning|note|message)"
    r"\s+(?P<code>[A-Za-z]+\d+)\s*:\s*(?P<message>.*)$"
)

# Linker messages carry no line number: "main.obj : error LNK2019: ...".
# The file group tolerates a leading drive letter so "C:\x\main.obj" is not
# split at the drive colon.
_MSVC_NO_LINE_PATTERN = re.compile(
    r"^\s*(?P<file>(?:[A-Za-z]:)?[^:]*?)"
    r"\s*:\s*(?P<severity>fatal error|error|warning)"
    r"\s+(?P<code>[A-Za-z]+\d+)\s*:\s*(?P<message>.*)$"
)

# clang, gcc, dxc and glslc: file:line:col: sev: msg. The non-greedy file
# group backtracks correctly over a Windows drive letter, because "\" after
# "C:" is not a digit run.
_CLANG_PATTERN = re.compile(
    r"^\s*(?P<file>.+?):(?P<line>\d+)(?::(?P<column>\d+))?"
    r":\s*(?P<severity>fatal error|error|warning|note):\s*(?P<message>.*)$"
)

# glslangValidator: "ERROR: shader.frag:5: '' : syntax error"
_GLSLANG_PATTERN = re.compile(
    r"^\s*(?P<severity>ERROR|WARNING):\s*(?P<file>.+?):(?P<line>\d+):\s*(?P<message>.*)$"
)

# CMake: "CMake Error at CMakeLists.txt:12 (find_package):" with the message
# on the following indented lines.
_CMAKE_LOCATED_PATTERN = re.compile(
    r"^CMake (?P<severity>Error|Warning)(?: \(dev\))? at "
    r"(?P<file>.+?):(?P<line>\d+)\s*(?:\((?P<code>\w+)\))?\s*:\s*$"
)

_CMAKE_BARE_PATTERN = re.compile(
    r"^CMake (?P<severity>Error|Warning)(?: \(dev\))?\s*:\s*(?P<message>.*)$"
)

# ninja's own failures, and a bare "error: msg" from a tool that reported no
# file (dxc does this for command-line problems).
_NINJA_PATTERN = re.compile(r"^ninja:\s*(?P<message>.*(?:error|stopped).*)$")

_BARE_PATTERN = re.compile(
    r"^\s*(?P<severity>fatal error|error|warning):\s*(?P<message>.+)$"
)

# MSBuild appends the owning project to every line:
#   "... undeclared identifier [C:\proj\game.vcxproj]"
# It is identical on every line from that project, so it is pure noise.
_PROJECT_TAG_PATTERN = re.compile(r"\s*\[[^\]]*\.(?:vcx|cs|fs|vb)proj\]\s*$")


def _normalise_severity(raw: str) -> str:
    """Collapse tool spellings onto "error" / "warning" / "note"."""

    lowered = raw.strip().lower()

    if lowered in {"fatal error", "error"}:
        return "error"

    if lowered == "warning":
        return "warning"

    return "note"


def _relative_to(file: str, base: Path | None) -> str:
    """Shorten an absolute path against the build directory.

    Compilers report absolute paths; "src/renderer/device.cpp" costs a
    fraction of the tokens of the same path under a deep checkout, and is
    what the caller needs to pass to read_file anyway.
    """

    if not file:
        return ""

    text = file.strip().strip('"')

    if base is not None:
        try:
            candidate = Path(text)

            if candidate.is_absolute():
                text = str(candidate.relative_to(base))
        except (ValueError, OSError):
            # Different drive, or not under base. Keep the absolute path:
            # a wrong relative path is worse than a long correct one.
            pass

    return text.replace("\\", "/")


def _to_int(value: str | None) -> int | None:
    """Parse an optional numeric group."""

    if value is None:
        return None

    try:
        return int(value)
    except ValueError:  # pragma: no cover - regex guarantees digits
        return None


def _match_line(line: str, base: Path | None) -> Diagnostic | None:
    """Try every pattern against one line, most specific first.

    Order matters. _MSVC_NO_LINE_PATTERN and _BARE_PATTERN are permissive
    enough to swallow lines the earlier patterns parse properly, so they come
    last.
    """

    stripped = _PROJECT_TAG_PATTERN.sub("", line.rstrip())

    if not stripped.strip():
        return None

    match = _MSVC_PATTERN.match(stripped)

    if match is not None:
        return Diagnostic(
            file=_relative_to(match.group("file"), base),
            line=_to_int(match.group("line")),
            column=_to_int(match.group("column")),
            severity=_normalise_severity(match.group("severity")),
            code=match.group("code"),
            message=match.group("message").strip(),
        )

    match = _CLANG_PATTERN.match(stripped)

    if match is not None:
        return Diagnostic(
            file=_relative_to(match.group("file"), base),
            line=_to_int(match.group("line")),
            column=_to_int(match.group("column")),
            severity=_normalise_severity(match.group("severity")),
            code="",
            message=match.group("message").strip(),
        )

    match = _GLSLANG_PATTERN.match(stripped)

    if match is not None:
        return Diagnostic(
            file=_relative_to(match.group("file"), base),
            line=_to_int(match.group("line")),
            column=None,
            severity=_normalise_severity(match.group("severity")),
            code="",
            message=match.group("message").strip(),
        )

    match = _CMAKE_BARE_PATTERN.match(stripped)

    if match is not None:
        return Diagnostic(
            file="",
            line=None,
            column=None,
            severity=_normalise_severity(match.group("severity")),
            code="CMake",
            message=match.group("message").strip(),
        )

    match = _NINJA_PATTERN.match(stripped)

    if match is not None:
        return Diagnostic(
            file="",
            line=None,
            column=None,
            severity="error",
            code="ninja",
            message=match.group("message").strip(),
        )

    match = _MSVC_NO_LINE_PATTERN.match(stripped)

    if match is not None:
        return Diagnostic(
            file=_relative_to(match.group("file"), base),
            line=None,
            column=None,
            severity=_normalise_severity(match.group("severity")),
            code=match.group("code"),
            message=match.group("message").strip(),
        )

    match = _BARE_PATTERN.match(stripped)

    if match is not None:
        return Diagnostic(
            file="",
            line=None,
            column=None,
            severity=_normalise_severity(match.group("severity")),
            code="",
            message=match.group("message").strip(),
        )

    return None


def parse_build_output(
    text: str, *, base_directory: Path | None = None
) -> ParsedOutput:
    """Extract deduplicated diagnostics from a build log.

    Deduplication is the point, not a nicety. A broken header included by
    forty translation units produces forty identical errors, which would fill
    the report with one problem restated. Identical diagnostics collapse to
    one entry carrying a count.

    Args:
        text: Combined stdout and stderr from the build.
        base_directory: Build directory, used to shorten absolute paths.

    Returns:
        The parsed diagnostics, plus the lines nothing matched.
    """

    # Key -> index into `order`, so first-seen ordering survives dedup.
    seen: dict[tuple[str, int | None, int | None, str, str, str], int] = {}
    order: list[Diagnostic] = []
    unparsed: list[str] = []

    total_errors = 0
    total_warnings = 0

    # Index of the last error/warning, so a following "note:" attaches to it.
    last_real = -1

    pending_cmake: Diagnostic | None = None
    cmake_message: list[str] = []

    def flush_cmake() -> None:
        """Emit a CMake diagnostic once its indented message is collected."""

        nonlocal pending_cmake, total_errors, total_warnings, last_real

        if pending_cmake is None:
            return

        finished = pending_cmake._replace(
            message=" ".join(part.strip() for part in cmake_message if part.strip())
            or "(no message)"
        )

        pending_cmake = None
        cmake_message.clear()

        key = (
            finished.file,
            finished.line,
            finished.column,
            finished.severity,
            finished.code,
            finished.message,
        )

        if finished.severity == "error":
            total_errors += 1
        else:
            total_warnings += 1

        if key in seen:
            index = seen[key]
            order[index] = order[index]._replace(
                occurrences=order[index].occurrences + 1
            )
            last_real = index
            return

        seen[key] = len(order)
        last_real = len(order)
        order.append(finished)

    for line in text.splitlines():
        located = _CMAKE_LOCATED_PATTERN.match(line.rstrip())

        if located is not None:
            flush_cmake()

            pending_cmake = Diagnostic(
                file=_relative_to(located.group("file"), base_directory),
                line=_to_int(located.group("line")),
                column=None,
                severity=_normalise_severity(located.group("severity")),
                code=located.group("code") or "CMake",
                message="",
            )

            continue

        if pending_cmake is not None:
            # CMake's message is the indented block that follows. Blank lines
            # do NOT end it: a find_package failure separates its prose from
            # the list of candidate config filenames with an empty line, and
            # treating that as the terminator drops the filenames -- which are
            # the actionable half of the message. Only a non-blank line at
            # column zero ends the block.
            if not line.strip() or line.startswith(("  ", "\t")):
                cmake_message.append(line)
                continue

            flush_cmake()

        diagnostic = _match_line(line, base_directory)

        if diagnostic is None:
            if line.strip():
                unparsed.append(line.rstrip())
            continue

        if diagnostic.severity == "note":
            if last_real >= 0:
                existing = order[last_real]

                if len(existing.notes) < MAX_NOTES_PER_ERROR:
                    label = diagnostic.message

                    if diagnostic.file:
                        location = diagnostic.file

                        if diagnostic.line is not None:
                            location = f"{location}:{diagnostic.line}"

                        label = f"{location}: {label}"

                    order[last_real] = existing._replace(
                        notes=existing.notes + (label,)
                    )
            continue

        key = (
            diagnostic.file,
            diagnostic.line,
            diagnostic.column,
            diagnostic.severity,
            diagnostic.code,
            diagnostic.message,
        )

        if diagnostic.severity == "error":
            total_errors += 1
        else:
            total_warnings += 1

        if key in seen:
            index = seen[key]
            order[index] = order[index]._replace(
                occurrences=order[index].occurrences + 1
            )
            last_real = index
            continue

        seen[key] = len(order)
        last_real = len(order)
        order.append(diagnostic)

    flush_cmake()

    return ParsedOutput(
        errors=tuple(item for item in order if item.severity == "error"),
        warnings=tuple(item for item in order if item.severity == "warning"),
        total_errors=total_errors,
        total_warnings=total_warnings,
        unparsed=tuple(unparsed),
    )


def _format_one(diagnostic: Diagnostic) -> list[str]:
    """Render one diagnostic as report lines."""

    location = diagnostic.file or "(no file)"

    if diagnostic.line is not None:
        location = f"{location}:{diagnostic.line}"

        if diagnostic.column is not None:
            location = f"{location}:{diagnostic.column}"

    parts = [location]

    if diagnostic.code:
        parts.append(diagnostic.code)

    message = diagnostic.message

    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS] + " ...[message truncated]"

    parts.append(message)

    head = "  " + "  ".join(parts)

    if diagnostic.occurrences > 1:
        head = f"{head}  (x{diagnostic.occurrences})"

    return [head] + [f"      note: {note}" for note in diagnostic.notes]


def format_report(
    parsed: ParsedOutput,
    *,
    header: str,
    exit_code: int,
    raw_output: str,
    subject: str = "BUILD",
    success_line: str | None = None,
) -> str:
    """Render a parsed build log as the tool's plain-text result.

    Args:
        parsed: Output of parse_build_output.
        header: First line, naming what ran.
        exit_code: Process exit status.
        raw_output: Combined output, for the fallback tail.
        subject: Word used in the verdict lines. compile_shader passes
            "COMPILE" so a shader failure does not report "BUILD FAILED",
            which reads as though the whole project failed.
        success_line: Replaces the default "<subject> SUCCEEDED" verdict.
            Used to name the artefact that was produced.

    Returns:
        The report. Never empty, and never claims success on a non-zero exit
        it could not explain.
    """

    lines = [f"{header}  (exit code {exit_code})", ""]

    if parsed.errors:
        shown = parsed.errors[:MAX_ERRORS_SHOWN]

        lines.append(
            f"ERRORS ({len(parsed.errors)} unique, {parsed.total_errors} total):"
        )

        for diagnostic in shown:
            lines.extend(_format_one(diagnostic))

        if len(parsed.errors) > MAX_ERRORS_SHOWN:
            lines.append(
                f"  ...[{len(parsed.errors) - MAX_ERRORS_SHOWN} more unique "
                f"errors not shown]..."
            )

        lines.append("")

    if parsed.warnings:
        shown_warnings = parsed.warnings[:MAX_WARNINGS_SHOWN]

        lines.append(
            f"WARNINGS ({len(parsed.warnings)} unique, {parsed.total_warnings} total):"
        )

        for diagnostic in shown_warnings:
            lines.extend(_format_one(diagnostic))

        if len(parsed.warnings) > MAX_WARNINGS_SHOWN:
            lines.append(
                f"  ...[{len(parsed.warnings) - MAX_WARNINGS_SHOWN} more "
                f"unique warnings not shown]..."
            )

        lines.append("")

    if exit_code == 0 and not parsed.errors:
        lines.append(success_line or f"{subject} SUCCEEDED")

        if parsed.warnings:
            lines.append("Warnings above are not fatal.")

        return "\n".join(lines)

    if not parsed.errors:
        # The critical case: the build failed and no pattern matched. Saying
        # "no errors" here would be a lie the model acts on, so show the tail
        # of the real output instead.
        tail = [item for item in raw_output.splitlines() if item.strip()]
        tail = tail[-MAX_RAW_TAIL_LINES:]

        lines.append(
            f"{subject} FAILED, but no recognised compiler diagnostics were found."
        )
        lines.append("Raw output (last lines):")
        lines.extend(f"  {item}" for item in tail)
        lines.append("")
        lines.append(
            "This may be a build-system or configuration failure rather than "
            "a compile error."
        )

        return "\n".join(lines)

    lines.append(f"{subject} FAILED")

    return "\n".join(lines)
