"""Package the project for copying to another machine.

    python package.py            dist/windows-agent-mcp-<version>.zip
    python package.py --list     dry run: show what would be included
    python package.py --dest D   copy into a directory instead of zipping

Stdlib only, so this also runs inside a copy that has not been bootstrapped.

Selection is an ALLOWLIST, not an exclusion list. For "copy to another
machine" the bad outcome is shipping an absolute path or a secret, so anything
not named below is left out, and a machine-specific file added to the project
later is excluded by default rather than silently travelling. The cost is that
an allowlist fails by omission -- hence --list, and hence the extract-and-test
step documented in docs/HOW_TO_USE.md.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

PROJECT_NAME = "windows-agent-mcp"

# Trees copied whole, minus the cache pruning below.
INCLUDED_DIRS: tuple[str, ...] = ("src", "tests")

# Individual files. Everything needed to install, run, test and understand the
# project on a machine that has never seen it.
INCLUDED_FILES: tuple[str, ...] = (
    "pyproject.toml",
    # No requirements.txt: pyproject.toml is the canonical dependency
    # declaration and uv.lock the resolved one. A third hand-synced copy
    # only had to drift once to be wrong.
    "uv.lock",
    "README.md",
    # The step-by-step guide for a copied project. Forgetting this one would
    # ship the archive without the file that explains the archive; a test
    # asserts every document is listed here.
    "docs/HOW_TO_USE.md",
    "docs/TEST_PROMPTS.md",
    "docs/CHANGELOG.md",
    "docs/CONTRIBUTING.md",
    "LICENSE",
    ".env.example",
    ".editorconfig",
    ".gitignore",
    ".gitattributes",
    ".pre-commit-config.yaml",
    "run_server.bat",
    "bootstrap.py",
    "package.py",
)

# What the allowlist leaves out, and why -- stated because "why is my file
# missing from the zip" should be answerable from this file alone:
#
#   .venv               absolute paths are baked into its scripts, it is large,
#                       and it is specific to one Python build. bootstrap.py
#                       recreates it on the target.
#   input/              config.json holds WAMCP_PROJECT_ROOTS paths that mean
#                       nothing elsewhere. The target generates its own on the
#                       first run.
#   mcp-allowed-hosts.json
#                       web hosts trusted on ONE machine. A trust decision
#                       should be made again there, not inherited.
#   *_claude.md         local working notes and design rationale. Not
#                       published, and nothing tracked may depend on them.
#   dist/               output of this script.

# Cache directories pruned inside the included trees.
#
# Deliberately NOT filescan.SKIPPED_DIRECTORY_NAMES: that is a search-oriented
# list which also skips bin, obj, dist, debug, release and x64. Correct for a
# grep, wrong for a source archive -- it would one day silently drop a
# legitimate src/.../bin/. A narrow list here says what it means.
PRUNED_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    }
)

# Files skipped wherever they appear.
PRUNED_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo", ".pyd", ".spv", ".dxil", ".cso")

PRUNED_NAMES: frozenset[str] = frozenset({".coverage", ".env"})


def read_version(pyproject: Path) -> str:
    """Read the project version, for the archive name.

    tomllib when available, regex otherwise: requires-python is >=3.10 and
    tomllib only arrives in 3.11, and this script must work on the older floor.

    Args:
        pyproject: Path to pyproject.toml.

    Returns:
        The version, or "unknown" if it could not be read.
    """

    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return "unknown"

    try:
        import tomllib

        version = tomllib.loads(text).get("project", {}).get("version")

        if isinstance(version, str) and version:
            return version
    except ModuleNotFoundError:
        pass
    except (ValueError, TypeError):
        return "unknown"

    match = re.search(r"^version\s*=\s*[\"']([^\"']+)[\"']", text, re.MULTILINE)

    return match.group(1) if match else "unknown"


def _is_pruned(path: Path) -> bool:
    """Whether one file is excluded by the cache/artefact rules."""

    if path.name in PRUNED_NAMES:
        return True

    if path.suffix in PRUNED_SUFFIXES:
        return True

    return path.name.endswith(".egg-info")


def collect_files(root: Path) -> list[Path]:
    """Select the files to ship, as paths relative to `root`.

    Args:
        root: Repository root.

    Returns:
        Relative paths, sorted, with directories walked and caches pruned.
        Missing allowlisted entries are simply absent rather than an error --
        an optional file such as docs/CONTRIBUTING.md should not stop a
        package.
    """

    selected: list[Path] = []

    for name in INCLUDED_FILES:
        candidate = root / name

        if candidate.is_file() and not _is_pruned(candidate):
            selected.append(Path(name))

    for name in INCLUDED_DIRS:
        tree = root / name

        if not tree.is_dir():
            continue

        for path in tree.rglob("*"):
            if not path.is_file():
                continue

            relative = path.relative_to(root)

            # Prune if any component is a cache directory, so an entire
            # __pycache__ subtree drops out rather than file by file.
            if PRUNED_DIRS.intersection(relative.parts):
                continue

            if any(part.endswith(".egg-info") for part in relative.parts):
                continue

            if _is_pruned(path):
                continue

            selected.append(relative)

    return sorted(selected, key=lambda item: item.as_posix())


def _size(paths: list[Path], root: Path) -> int:
    total = 0

    for relative in paths:
        try:
            total += (root / relative).stat().st_size
        except OSError:
            continue

    return total


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0

    return f"{size:.1f} GB"  # pragma: no cover - unreachable, loop returns


def write_archive(root: Path, files: list[Path], destination: Path) -> tuple[int, str]:
    """Write the zip and verify it.

    Everything sits under a single PROJECT_NAME/ prefix so extracting does not
    splatter files into the current directory.

    Args:
        root: Repository root.
        files: Relative paths from collect_files.
        destination: Archive path to write.

    Returns:
        (entry count, error message). The message is "" on success.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(
            destination, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for relative in files:
                archive.write(root / relative, f"{PROJECT_NAME}/{relative.as_posix()}")
    except OSError as exc:
        return 0, f"could not write {destination}: {exc}"

    # Reopen and check. Cheap, and catches a truncated or partial write that
    # the loop above would not notice.
    try:
        with zipfile.ZipFile(destination) as archive:
            broken = archive.testzip()

            if broken is not None:
                return 0, f"archive is corrupt at {broken}"

            written = len(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        return 0, f"could not verify {destination}: {exc}"

    if written != len(files):
        return written, (
            f"archive holds {written} entries but {len(files)} were selected"
        )

    return written, ""


def copy_tree(root: Path, files: list[Path], destination: Path) -> tuple[int, str]:
    """Copy the same selection into a directory.

    Args:
        root: Repository root.
        files: Relative paths from collect_files.
        destination: Directory to copy into. Created if absent.

    Returns:
        (file count, error message). The message is "" on success.
    """

    target_root = destination / PROJECT_NAME

    try:
        for relative in files:
            target = target_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / relative, target)
    except OSError as exc:
        return 0, f"could not copy into {target_root}: {exc}"

    return len(files), ""


def main(argv: list[str] | None = None) -> int:
    """Package the project.

    Returns:
        0 on success, 1 on failure.
    """

    parser = argparse.ArgumentParser(
        prog="python package.py",
        description="Package the project for copying to another machine.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="dry run: print every file that would be included",
    )
    parser.add_argument(
        "--dest",
        help="copy into this directory instead of writing a zip",
    )
    parser.add_argument(
        "--output",
        help="archive path (default: dist/<name>-<version>.zip)",
    )

    options = parser.parse_args(argv)

    files = collect_files(ROOT)

    if not files:
        print("error: nothing to package -- is this the project root?", file=sys.stderr)
        return 1

    version = read_version(ROOT / "pyproject.toml")

    total = _size(files, ROOT)

    if options.list:
        for relative in files:
            print(f"  {relative.as_posix()}")

        print()
        print(f"  {len(files)} files, {_human(total)} uncompressed")
        print()
        print(
            "  Excluded by the allowlist: .venv, input/, "
            "mcp-allowed-hosts.json, .vscode, dist"
        )
        return 0

    if options.dest:
        destination = Path(options.dest).expanduser()

        count, error = copy_tree(ROOT, files, destination)

        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1

        print(f"copied {count} files to {destination / PROJECT_NAME}")
        print()
        print("On the target machine:")
        print(f"    cd {PROJECT_NAME}")
        print("    python bootstrap.py")
        return 0

    if options.output:
        archive_path = Path(options.output).expanduser()
    else:
        archive_path = ROOT / "dist" / f"{PROJECT_NAME}-{version}.zip"

    count, error = write_archive(ROOT, files, archive_path)

    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"wrote {archive_path}")
    print(
        f"  {count} files, {_human(total)} uncompressed, "
        f"{_human(archive_path.stat().st_size)} compressed"
    )
    print("  verified: archive reopened and checked")
    print()
    print("On the target machine:")
    print(f"    unzip {archive_path.name}")
    print(f"    cd {PROJECT_NAME}")
    print("    python bootstrap.py")
    print()
    # Named individually rather than as "local config", because each one is a
    # thing the operator has to redo and would otherwise wonder about. The
    # grants file in particular: a trust decision should be made again on the
    # new machine, not inherited from a zip.
    print(
        "Not included (recreated there): .venv, input/config.json, "
        "mcp-allowed-hosts.json"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
