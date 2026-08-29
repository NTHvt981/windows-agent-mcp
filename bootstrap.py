"""Create the virtual environment and install everything the server needs.

Run this first on a fresh machine:

    python bootstrap.py

STDLIB ONLY, and that is a hard constraint rather than a style preference: this
script runs *before* anything is installed, so a single third-party import
would make it unable to do the one job it exists for. A test parses this file's
imports and checks every one against sys.stdlib_module_names.

Deliberately NOT named setup.py. That name is reserved: setuptools' PEP 517
backend picks up a setup.py if one exists, so a plain installer script under
that name risks breaking `pip install .`. pyproject.toml stays the single build
definition.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

VENV_DIR = ROOT / ".venv"

# Windows layout. The launcher hardcodes the same path, so the two agree.
VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"

DEV_EXTRA = "dev"


def read_python_floor(pyproject: Path) -> tuple[int, int] | None:
    """Read requires-python from pyproject.toml.

    Read rather than hardcoded so this script and the package metadata cannot
    drift. tomllib is used when available and a regex otherwise, because
    requires-python is ">=3.10" while tomllib only arrives in 3.11 -- the
    script has to be able to reject an interpreter too old to parse the file
    that says it is too old.

    Args:
        pyproject: Path to pyproject.toml.

    Returns:
        (major, minor), or None if it could not be determined.
    """

    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None

    specifier: str | None = None

    try:
        import tomllib

        specifier = tomllib.loads(text).get("project", {}).get("requires-python")
    except ModuleNotFoundError:
        match = re.search(
            r"^requires-python\s*=\s*[\"']([^\"']+)[\"']",
            text,
            re.MULTILINE,
        )
        specifier = match.group(1) if match else None
    except (ValueError, TypeError):
        return None

    if not specifier:
        return None

    version = re.search(r"(\d+)\.(\d+)", specifier)

    if version is None:
        return None

    return int(version.group(1)), int(version.group(2))


def plan_commands(
    *,
    root: Path,
    python: str,
    uv: str | None,
    dev: bool,
    venv_exists: bool,
) -> list[list[str]]:
    """Return the commands an install would run, in order.

    Pure: takes the environment as arguments and touches nothing, so the
    decision logic is testable without performing an install. Same split as
    inspector_config.build_config and profiles.parse_profiles.

    Args:
        root: Repository root.
        python: Interpreter to build the venv with, when uv is absent.
        uv: Path to uv, or None.
        dev: Install the dev extra as well.
        venv_exists: Whether .venv is already present.

    Returns:
        Commands for subprocess, in execution order.
    """

    if uv is not None:
        # uv sync creates .venv itself and honours uv.lock, so it is both
        # faster and more reproducible than the pip path. --extra adds the
        # optional dependency group; plain sync installs runtime only.
        command = [uv, "sync"]

        if dev:
            command += ["--extra", DEV_EXTRA]

        return [command]

    commands: list[list[str]] = []

    if not venv_exists:
        commands.append([python, "-m", "venv", str(root / ".venv")])

    venv_python = str(root / ".venv" / "Scripts" / "python.exe")

    # Upgrading pip first is not ceremony: the wheel for a project with a
    # pyproject-only build needs a pip recent enough to use PEP 517, and the
    # pip bundled with an older python is not always.
    commands.append([venv_python, "-m", "pip", "install", "--upgrade", "pip"])

    target = f".[{DEV_EXTRA}]" if dev else "."

    commands.append([venv_python, "-m", "pip", "install", "-e", target])

    return commands


def _step(label: str, detail: str) -> None:
    """Print one aligned progress line."""

    print(f"  {label:.<38} {detail}")


def _run(command: list[str], root: Path) -> bool:
    """Run one command, streaming its output. Returns True on success."""

    print()
    print(f"  $ {' '.join(command)}")
    print()

    try:
        completed = subprocess.run(command, cwd=root, check=False)
    except OSError as exc:
        print(f"\n  could not start {command[0]}: {exc}", file=sys.stderr)
        return False

    return completed.returncode == 0


def verify(venv_python: Path) -> tuple[bool, str]:
    """Confirm the install actually produced a working server.

    An install that reports success but leaves the package unimportable is the
    failure worth catching, and it is the same check run_server.bat performs
    before starting the server.

    Args:
        venv_python: Interpreter inside the virtual environment.

    Returns:
        (ok, detail) where detail is a tool count or an error message.
    """

    if not venv_python.is_file():
        return False, f"no interpreter at {venv_python}"

    probe = "from windows_agent_mcp.main import get_tools;print(len(get_tools()))"

    try:
        completed = subprocess.run(
            [str(venv_python), "-c", probe],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        return False, detail[-1] if detail else "import failed"

    return True, f"{completed.stdout.strip()} tools registered"


def main(argv: list[str] | None = None) -> int:
    """Install the server into .venv and verify it.

    Returns:
        0 on success, 1 on any failure.
    """

    parser = argparse.ArgumentParser(
        prog="python bootstrap.py",
        description="Create .venv and install the server and its dependencies.",
    )
    parser.add_argument(
        "--no-dev",
        action="store_true",
        help="skip the dev extra (pytest, ruff, pyright, pre-commit)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what is present and working, install nothing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="delete an existing .venv first",
    )

    options = parser.parse_args(argv)

    dev = not options.no_dev

    print()
    print("Windows Agent MCP -- bootstrap")
    print()

    running = ".".join(str(part) for part in sys.version_info[:3])

    floor = read_python_floor(ROOT / "pyproject.toml")

    if floor is not None and sys.version_info[:2] < floor:
        _step(f"python {running}", f"TOO OLD, need >= {floor[0]}.{floor[1]}")
        print()
        print("  Install a newer Python and re-run this script.", file=sys.stderr)
        return 1

    wanted = f" (>= {floor[0]}.{floor[1]})" if floor else ""

    _step(f"python {running}", f"ok{wanted}")

    uv = shutil.which("uv")

    _step("uv", f"found at {uv}" if uv else "not found, will use venv + pip")

    if options.check:
        _step(".venv", "present" if VENV_PYTHON.is_file() else "MISSING")

        ok, detail = verify(VENV_PYTHON)

        _step("package import", detail if ok else f"FAILED: {detail}")

        print()

        if not ok:
            print("  Run without --check to install.")
            return 1

        print("  Ready. Next:  run_server.bat --dev")
        print()
        return 0

    if options.force and VENV_DIR.exists():
        try:
            shutil.rmtree(VENV_DIR)
        except OSError as exc:
            print(f"\n  could not remove {VENV_DIR}: {exc}", file=sys.stderr)
            return 1

        _step(".venv", "removed (--force)")

    commands = plan_commands(
        root=ROOT,
        python=sys.executable,
        uv=uv,
        dev=dev,
        venv_exists=VENV_PYTHON.is_file(),
    )

    _step("dev dependencies", "included" if dev else "skipped (--no-dev)")

    for command in commands:
        if not _run(command, ROOT):
            print()
            print(f"  FAILED: {' '.join(command)}", file=sys.stderr)
            print("  Fix the error above and re-run this script.", file=sys.stderr)
            return 1

    print()

    ok, detail = verify(VENV_PYTHON)

    _step("verify", detail if ok else f"FAILED: {detail}")

    print()

    if not ok:
        print(
            "  The install reported success but the package is not importable.",
            file=sys.stderr,
        )
        return 1

    print("  Done. Next steps:")
    print()
    print("    1. Point it at your project, or writes and builds stay")
    print("       confined to the download sandbox:")
    print("         set BIONIC_PROJECT_ROOTS=C:\\path\\to\\your\\project")
    print()
    print("    2. Start it:")
    print("         run_server.bat --dev      with the MCP Inspector UI")
    print("         run_server.bat            plain stdio, for a client")
    print()
    print("    3. Check the install:")
    print("         .venv\\Scripts\\python.exe -m pytest tests -q")
    print()
    print("  Full guide: docs/HOW_TO_USE.md")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
