"""Parse compiler, linker and build-system output into structured diagnostics."""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

__all__: list[str] = [
    "Diagnostic",
    "ParsedOutput",
    "format_report",
    "parse_build_output",
    "parse_rebuild_kind",
]

MAX_ERRORS_SHOWN = 20
MAX_WARNINGS_SHOWN = 10

MAX_NOTES_PER_ERROR = 2

MAX_RAW_TAIL_LINES = 25

# Applied at render time, so the dedup key stays full.
MAX_MESSAGE_CHARS = 500

_VENDORED_PATH_PARTS = frozenset(
    {
        "deps",
        "external",
        "externals",
        "third_party",
        "thirdparty",
        "3rdparty",
        "vendor",
        "vendored",
        "_deps",
        "subprojects",
    }
)

_KNOWN_BENIGN_CODES = frozenset({"C4996", "LNK4099"})

# Present exactly when ninja compiles or links.
_NINJA_PROGRESS_PATTERN = re.compile(r"\[\d+/\d+\]")

# Matching only turns "" into incremental, never up-to-date.
_SOURCE_FILE_PATTERN = re.compile(r"\S+\.(?:c|cc|cpp|cxx|m|mm)$", re.IGNORECASE)


class Diagnostic(NamedTuple):
    """One compiler, linker or build-system message."""

    # occurrences, not count: NamedTuple inherits tuple.count().

    file: str
    line: int | None
    column: int | None
    severity: str
    code: str
    message: str
    notes: tuple[str, ...] = ()
    occurrences: int = 1


class ParsedOutput(NamedTuple):
    """Everything extracted from one build log."""

    errors: tuple[Diagnostic, ...]
    warnings: tuple[Diagnostic, ...]
    total_errors: int
    total_warnings: int
    unparsed: tuple[str, ...]


_MSVC_PATTERN = re.compile(
    r"^\s*(?P<file>(?:[A-Za-z]:)?[^(]+?)"
    r"\((?P<line>\d+)(?:,(?P<column>\d+))?\)"
    r"\s*:\s*(?P<severity>fatal error|error|warning|note|message)"
    r"\s+(?P<code>[A-Za-z]+\d+)\s*:\s*(?P<message>.*)$"
)

_MSVC_NO_LINE_PATTERN = re.compile(
    r"^\s*(?P<file>(?:[A-Za-z]:)?[^:]*?)"
    r"\s*:\s*(?P<severity>fatal error|error|warning)"
    r"\s+(?P<code>[A-Za-z]+\d+)\s*:\s*(?P<message>.*)$"
)

_CLANG_PATTERN = re.compile(
    r"^\s*(?P<file>.+?):(?P<line>\d+)(?::(?P<column>\d+))?"
    r":\s*(?P<severity>fatal error|error|warning|note):\s*(?P<message>.*)$"
)

_GLSLANG_PATTERN = re.compile(
    r"^\s*(?P<severity>ERROR|WARNING):\s*(?P<file>.+?):(?P<line>\d+):\s*(?P<message>.*)$"
)

_CMAKE_LOCATED_PATTERN = re.compile(
    r"^CMake (?P<severity>Error|Warning)(?: \(dev\))? at "
    r"(?P<file>.+?):(?P<line>\d+)\s*(?:\((?P<code>\w+)\))?\s*:\s*$"
)

_CMAKE_BARE_PATTERN = re.compile(
    r"^CMake (?P<severity>Error|Warning)(?: \(dev\))?\s*:\s*(?P<message>.*)$"
)

_NINJA_PATTERN = re.compile(r"^ninja:\s*(?P<message>.*(?:error|stopped).*)$")

_BARE_PATTERN = re.compile(
    r"^\s*(?P<severity>fatal error|error|warning):\s*(?P<message>.+)$"
)

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
    """Shorten an absolute path against the build directory."""

    if not file:
        return ""

    text = file.strip().strip('"')

    if base is not None:
        try:
            candidate = Path(text)

            if candidate.is_absolute():
                text = str(candidate.relative_to(base))
        except (ValueError, OSError):
            # A wrong relative path is worse than a long correct one.
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
    """Try every pattern against one line, most specific first."""

    # Permissive patterns would swallow precise matches.

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
    """Extract deduplicated diagnostics from a build log."""

    seen: dict[tuple[str, int | None, int | None, str, str, str], int] = {}
    order: list[Diagnostic] = []
    unparsed: list[str] = []

    total_errors = 0
    total_warnings = 0

    last_real = -1

    pending_cmake: Diagnostic | None = None
    cmake_message: list[str] = []

    def flush_cmake() -> None:

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
            # Only a non-blank line at column zero ends the block.
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


def _pre_existing(diagnostic: Diagnostic) -> bool:
    """Whether a warning probably predates the caller's current edit."""

    if diagnostic.code in _KNOWN_BENIGN_CODES:
        return True

    if not diagnostic.file:
        return False

    parts = diagnostic.file.lower().replace("\\", "/").split("/")

    return any(part in _VENDORED_PATH_PARTS for part in parts)


def _warning_histogram(warnings: tuple[Diagnostic, ...]) -> str:
    """Summarise warnings as one line, e.g. "C4996 x7 (deps) | LNK4099 x2"."""

    if not warnings:
        return ""

    counts: dict[str, int] = {}
    directories: dict[str, dict[str, int]] = {}
    order: list[str] = []

    for diagnostic in warnings:
        key = diagnostic.code or diagnostic.message

        if key not in counts:
            counts[key] = 0
            directories[key] = {}
            order.append(key)

        counts[key] += diagnostic.occurrences

        if diagnostic.file:
            top = diagnostic.file.lower().replace("\\", "/").split("/")[0]

            if top:
                directories[key][top] = (
                    directories[key].get(top, 0) + diagnostic.occurrences
                )

    parts: list[str] = []

    # sorted() is stable, so ties keep first-seen order.
    for key in sorted(order, key=lambda item: counts[item], reverse=True):
        label = f"{key} x{counts[key]}"

        if directories[key]:
            top = max(directories[key], key=lambda item: directories[key][item])
            label = f"{label} ({top})"

        parts.append(label)

    return " | ".join(parts)


def parse_rebuild_kind(raw_output: str) -> str:
    """Infer what a build actually did: up-to-date, incremental or full."""

    # Fail closed: an unrecognised log returns "", never a guess.

    lowered = raw_output.lower()

    has_activity = any(
        _has_compile_activity(line) for line in raw_output.splitlines()
    )

    if "ninja: no work to do" in lowered or "everything is up to date" in lowered:
        return "up-to-date"

    if any(
        marker in lowered
        for marker in (
            "rebuild all",
            "performing full rebuild",
            "recompiling",
            "-t:rebuild",
        )
    ):
        return "full"

    if "build succeeded" in lowered and not has_activity:
        return "up-to-date"

    if has_activity:
        return "incremental"

    return ""


def _has_compile_activity(line: str) -> bool:
    """Whether one log line is a compiler or linker doing work."""

    # The arrow appears only when the link actually ran.
    if "-> " in line:
        return True

    if "Linking" in line:
        return True

    if _NINJA_PROGRESS_PATTERN.search(line) is not None:
        return True

    return _SOURCE_FILE_PATTERN.match(line.strip()) is not None


def _format_one(diagnostic: Diagnostic, *, is_warning: bool = False) -> list[str]:
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

    if is_warning and _pre_existing(diagnostic):
        head = f"{head}  [likely pre-existing]"

    return [head] + [f"      note: {note}" for note in diagnostic.notes]


def format_report(
    parsed: ParsedOutput,
    *,
    header: str,
    exit_code: int,
    raw_output: str,
    subject: str = "BUILD",
    success_line: str | None = None,
    elapsed_ms: int | None = None,
    rebuild: str = "",
    full_log: Path | None = None,
) -> str:
    """Render a parsed build log as the tool's plain-text result."""

    if elapsed_ms is None:
        lines = [f"{header}  (exit code {exit_code})"]
    else:
        lines = [f"{header}  (exit code {exit_code}, {elapsed_ms / 1000:.1f}s)"]

    if rebuild:
        lines.append(f"rebuild: {rebuild}")

    if full_log is not None:
        lines.append(f"full log: {full_log}")

    lines.append("")

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
            lines.extend(_format_one(diagnostic, is_warning=True))

        if len(parsed.warnings) > MAX_WARNINGS_SHOWN:
            lines.append(
                f"  ...[{len(parsed.warnings) - MAX_WARNINGS_SHOWN} more "
                f"unique warnings not shown]..."
            )

        histogram = _warning_histogram(parsed.warnings)

        if histogram:
            lines.append(f"WARNING SUMMARY: {histogram}")

        lines.append("")

    if exit_code == 0 and not parsed.errors:
        verdict = success_line or f"{subject} SUCCEEDED"

        lines.append(
            f"{verdict} ({parsed.total_errors} errors, "
            f"{parsed.total_warnings} warnings)"
        )

        if parsed.warnings:
            lines.append("Warnings above are not fatal.")

        return "\n".join(lines)

    if not parsed.errors:
        # Fail closed: never report "no errors" on a failed build.
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
