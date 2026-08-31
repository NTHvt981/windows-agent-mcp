"""Tests for the operator scripts, bootstrap.py and package.py.

Both live at the repository root rather than inside the package, because
bootstrap.py has to work *before* the package is installed. That means they
cannot be imported normally, so they are loaded by path.

Two properties carry most of the weight:

* **bootstrap.py must import only the standard library.** It runs before
  anything is installed, so one third-party import makes it unable to do its
  job -- and the failure would appear as a confusing ImportError on a fresh
  machine, at the exact moment the user has least context.
* **package.py selects by allowlist.** An allowlist fails by omission, so the
  tests check both directions: that everything needed is present, and that
  machine-specific files are absent.

No test writes into the repository; everything goes through tmp_path.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import types
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> types.ModuleType:
    """Import a root-level script by path.

    Bytecode writing is suppressed for the duration. Without it, importing by
    path drops a `__pycache__/` into the repository root -- gitignored, but the
    suite promises not to write into the repository, and "gitignored" is not
    the same as "left no trace".
    """

    path = REPO_ROOT / name

    spec = importlib.util.spec_from_file_location(f"_script_{path.stem}", path)

    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)

    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True

    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous

    return module


bootstrap = load_script("bootstrap.py")
packager = load_script("package.py")


def top_level_imports(path: Path) -> set[str]:
    """Every module name imported anywhere in a file, top-level package only."""

    tree = ast.parse(path.read_text(encoding="utf-8"))

    names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])

    return names


# ============================================================
# The stdlib-only invariant
# ============================================================


@pytest.mark.parametrize("script", ["bootstrap.py", "package.py"])
def test_scripts_import_only_the_standard_library(script: str) -> None:
    """The invariant that lets bootstrap.py run on an un-installed checkout.

    Also applied to package.py, which must work inside a copy that has not been
    bootstrapped yet.
    """

    imported = top_level_imports(REPO_ROOT / script)

    # __future__ is not in stdlib_module_names but is always available.
    outside = sorted(imported - sys.stdlib_module_names - {"__future__"})

    assert outside == [], f"{script} imports non-stdlib modules: {outside}"


def test_scripts_do_not_import_the_package() -> None:
    """Importing windows_agent_mcp would defeat the purpose entirely."""

    for script in ("bootstrap.py", "package.py"):
        assert "windows_agent_mcp" not in top_level_imports(REPO_ROOT / script)


# ============================================================
# bootstrap: the python version floor
# ============================================================


def test_version_floor_is_read_from_pyproject() -> None:
    """Read rather than hardcoded, so the two cannot drift."""

    assert bootstrap.read_python_floor(REPO_ROOT / "pyproject.toml") == (3, 10)


def test_version_floor_regex_fallback(tmp_path) -> None:
    """tomllib arrives in 3.11 but requires-python is >=3.10.

    The script has to reject an interpreter too old to parse the file that says
    it is too old, so the regex path is not dead code.
    """

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nrequires-python = ">=3.12"\n', encoding="utf-8")

    real_import = builtins_import = __import__

    def no_tomllib(name: str, *args: object, **kwargs: object):
        if name == "tomllib":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    import builtins

    original = builtins.__import__
    builtins.__import__ = no_tomllib  # type: ignore[assignment]

    try:
        assert bootstrap.read_python_floor(pyproject) == (3, 12)
    finally:
        builtins.__import__ = original

    assert builtins_import is real_import


def test_missing_pyproject_yields_no_floor(tmp_path) -> None:
    assert bootstrap.read_python_floor(tmp_path / "absent.toml") is None


def test_pyproject_without_requires_python(tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n', encoding="utf-8")

    assert bootstrap.read_python_floor(pyproject) is None


# ============================================================
# bootstrap: plan_commands
# ============================================================


def plan(**overrides: object) -> list[list[str]]:
    arguments: dict[str, object] = {
        "root": Path("C:/project"),
        "python": "C:/py/python.exe",
        "uv": None,
        "dev": True,
        "venv_exists": False,
    }
    arguments.update(overrides)

    return bootstrap.plan_commands(**arguments)  # type: ignore[arg-type]


def test_uv_path_is_a_single_sync() -> None:
    """uv sync creates .venv itself and honours uv.lock."""

    commands = plan(uv="C:/uv/uv.exe")

    assert commands == [["C:/uv/uv.exe", "sync", "--extra", "dev"]]


def test_uv_path_without_dev() -> None:
    assert plan(uv="C:/uv/uv.exe", dev=False) == [["C:/uv/uv.exe", "sync"]]


def test_uv_path_ignores_an_existing_venv() -> None:
    """uv sync is idempotent, so there is nothing to branch on."""

    assert plan(uv="uv", venv_exists=True) == plan(uv="uv", venv_exists=False)


def test_pip_fallback_creates_the_venv_first() -> None:
    commands = plan()

    assert commands[0][:3] == ["C:/py/python.exe", "-m", "venv"]
    assert "venv" in commands[0][2]


def test_pip_fallback_skips_venv_creation_when_present() -> None:
    commands = plan(venv_exists=True)

    assert not any("venv" == part for command in commands for part in command[2:3])
    assert all(command[1:3] != ["-m", "venv"] for command in commands)


def test_pip_fallback_upgrades_pip_before_installing() -> None:
    """A pyproject-only build needs a pip recent enough for PEP 517."""

    commands = plan()

    upgrade = next(i for i, c in enumerate(commands) if "--upgrade" in c)
    install = next(i for i, c in enumerate(commands) if "-e" in c)

    assert upgrade < install


def test_pip_fallback_installs_editable_with_the_dev_extra() -> None:
    assert plan()[-1][-2:] == ["-e", ".[dev]"]


def test_pip_fallback_without_dev() -> None:
    assert plan(dev=False)[-1][-2:] == ["-e", "."]


def test_pip_fallback_uses_the_venv_interpreter_not_the_host() -> None:
    """Installing with the host interpreter would pollute the system Python."""

    commands = plan()

    for command in commands[1:]:
        assert ".venv" in command[0]


# ============================================================
# bootstrap: verify
# ============================================================


def test_verify_reports_a_missing_interpreter(tmp_path) -> None:
    ok, detail = bootstrap.verify(tmp_path / "python.exe")

    assert ok is False
    assert "no interpreter" in detail


@pytest.mark.skipif(
    not (REPO_ROOT / ".venv" / "Scripts" / "python.exe").is_file(),
    reason="no .venv: a fresh copy has not been bootstrapped yet",
)
def test_verify_counts_tools_with_the_real_venv() -> None:
    """The same check run_server.bat performs before starting the server.

    Skipped rather than failed when .venv is absent. That case is not
    hypothetical: it is exactly what an unzipped copy looks like before
    `python bootstrap.py` runs, and a suite that fails on a fresh checkout is a
    bad first impression of a working project. Found by extracting a real
    archive and running the tests from it.
    """

    ok, detail = bootstrap.verify(REPO_ROOT / ".venv" / "Scripts" / "python.exe")

    assert ok is True
    assert "tools registered" in detail


# ============================================================
# package: selection
# ============================================================


@pytest.fixture
def fake_repo(tmp_path) -> Path:
    """A miniature project, including everything that must NOT be shipped."""

    def write(relative: str, content: str = "x") -> None:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    write("pyproject.toml", '[project]\nversion = "9.9.9"\n')
    write("uv.lock")
    write("README.md")
    write("run_server.bat")
    write("bootstrap.py")
    write("package.py")
    write(".env.example")
    write(".gitignore")
    write("src/windows_agent_mcp/main.py")
    write("src/windows_agent_mcp/tools/read_file.py")
    write("tests/conftest.py")
    write("tests/test_thing.py")

    # None of the following may travel.
    write(".venv/Scripts/python.exe")
    write(".venv/Lib/site-packages/mcp/__init__.py")
    write("input/config.json", '{"version": 1}')
    write("mcp-allowed-hosts.json", '{"version": 1, "hosts": []}')
    write(".env", "SECRET=1")
    write(".coverage")
    write(".vscode/settings.json")
    write("dist/old.zip")
    write("src/windows_agent_mcp/__pycache__/main.cpython-313.pyc")
    write("tests/.pytest_cache/CACHEDIR.TAG")
    write("src/windows_agent_mcp.egg-info/PKG-INFO")
    write("src/shaders/blur.frag.spv")
    write("src/stray.pyc")

    return tmp_path


def selected(root: Path) -> set[str]:
    return {path.as_posix() for path in packager.collect_files(root)}


def test_allowlisted_files_are_included(fake_repo) -> None:
    names = selected(fake_repo)

    for expected in (
        "pyproject.toml",
        "uv.lock",
        "run_server.bat",
        "bootstrap.py",
        "package.py",
        ".env.example",
        "src/windows_agent_mcp/main.py",
        "tests/conftest.py",
    ):
        assert expected in names, expected


def test_the_venv_is_never_shipped(fake_repo) -> None:
    """Absolute paths are baked into its scripts, so it is useless elsewhere."""

    assert not any(name.startswith(".venv") for name in selected(fake_repo))


def test_machine_specific_files_are_never_shipped(fake_repo) -> None:
    """Local trust and path configuration must not travel in the archive.

    input/config.json holds project paths that mean nothing elsewhere, and
    mcp-allowed-hosts.json holds web hosts the operator chose to trust on ONE
    machine. Shipping either would hand a new machine decisions nobody made
    there -- and for the grants file that decision is which sites the model may
    read.
    """

    names = selected(fake_repo)

    assert "input/config.json" not in names
    assert "mcp-allowed-hosts.json" not in names
    assert ".env" not in names
    assert ".coverage" not in names


def test_caches_and_artefacts_are_pruned(fake_repo) -> None:
    names = selected(fake_repo)

    assert not any("__pycache__" in name for name in names)
    assert not any(".pytest_cache" in name for name in names)
    assert not any("egg-info" in name for name in names)
    assert not any(name.endswith(".pyc") for name in names)
    assert not any(name.endswith(".spv") for name in names)


def test_unlisted_top_level_files_are_omitted(fake_repo) -> None:
    """The allowlist bias: a new file must be added deliberately to ship."""

    (fake_repo / "notes-to-self.md").write_text("private", encoding="utf-8")
    (fake_repo / "credentials.json").write_text("{}", encoding="utf-8")

    names = selected(fake_repo)

    assert "notes-to-self.md" not in names
    assert "credentials.json" not in names


def test_unlisted_directories_are_omitted(fake_repo) -> None:
    (fake_repo / "scratch").mkdir()
    (fake_repo / "scratch" / "junk.txt").write_text("x", encoding="utf-8")

    assert not any(name.startswith("scratch") for name in selected(fake_repo))


def test_a_missing_optional_file_is_not_an_error(fake_repo) -> None:
    """CONTRIBUTING.md is absent from the fake repo and must not raise."""

    assert "docs/CONTRIBUTING.md" not in selected(fake_repo)
    assert selected(fake_repo)


def test_selection_is_sorted_and_unique(fake_repo) -> None:
    files = packager.collect_files(fake_repo)

    posix = [path.as_posix() for path in files]

    assert posix == sorted(posix)
    assert len(posix) == len(set(posix))


def test_empty_directory_yields_nothing(tmp_path) -> None:
    assert packager.collect_files(tmp_path) == []


def test_the_real_repository_ships_the_package_and_tests() -> None:
    """Read-only sanity check against the actual project."""

    names = selected(REPO_ROOT)

    assert "src/windows_agent_mcp/main.py" in names
    assert "src/windows_agent_mcp/config.py" in names
    assert "src/windows_agent_mcp/hostgrants.py" in names
    assert "src/windows_agent_mcp/consent.py" in names
    assert "tests/conftest.py" in names
    assert "pyproject.toml" in names

    assert "input/config.json" not in names
    assert "mcp-allowed-hosts.json" not in names
    assert not any(name.startswith(".venv") for name in names)


# ============================================================
# package: version
# ============================================================


def test_version_is_read_from_pyproject(fake_repo) -> None:
    assert packager.read_version(fake_repo / "pyproject.toml") == "9.9.9"


def test_version_of_the_real_project() -> None:
    assert packager.read_version(REPO_ROOT / "pyproject.toml") == "1.0.0"


def test_unknown_version_when_absent(tmp_path) -> None:
    assert packager.read_version(tmp_path / "nope.toml") == "unknown"


def test_unknown_version_when_unparseable(tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'x'\n", encoding="utf-8")

    assert packager.read_version(pyproject) == "unknown"


# ============================================================
# package: archive
# ============================================================


def test_archive_has_a_single_top_level_prefix(fake_repo, tmp_path) -> None:
    """Extracting must not splatter files into the current directory."""

    destination = tmp_path / "out" / "pkg.zip"

    count, error = packager.write_archive(
        fake_repo, packager.collect_files(fake_repo), destination
    )

    assert error == ""
    assert count > 0

    with zipfile.ZipFile(destination) as archive:
        prefixes = {name.split("/")[0] for name in archive.namelist()}

    assert prefixes == {packager.PROJECT_NAME}


def test_archive_is_verified_after_writing(fake_repo, tmp_path) -> None:
    destination = tmp_path / "pkg.zip"

    files = packager.collect_files(fake_repo)

    count, error = packager.write_archive(fake_repo, files, destination)

    assert error == ""
    assert count == len(files)

    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None


def test_archive_creates_missing_parents(fake_repo, tmp_path) -> None:
    destination = tmp_path / "a" / "b" / "pkg.zip"

    _count, error = packager.write_archive(
        fake_repo, packager.collect_files(fake_repo), destination
    )

    assert error == ""
    assert destination.is_file()


def test_archive_reports_an_unwritable_destination(
    fake_repo, tmp_path, monkeypatch
) -> None:
    def denied(*args: object, **kwargs: object):
        raise PermissionError("read-only volume")

    monkeypatch.setattr(packager.zipfile, "ZipFile", denied)

    _count, error = packager.write_archive(
        fake_repo, packager.collect_files(fake_repo), tmp_path / "pkg.zip"
    )

    assert "could not write" in error


def test_archive_content_round_trips(fake_repo, tmp_path) -> None:
    destination = tmp_path / "pkg.zip"

    packager.write_archive(fake_repo, packager.collect_files(fake_repo), destination)

    with zipfile.ZipFile(destination) as archive:
        body = archive.read(f"{packager.PROJECT_NAME}/pyproject.toml").decode()

    assert "9.9.9" in body


# ============================================================
# package: directory copy
# ============================================================


def test_dest_copy_reproduces_the_selection(fake_repo, tmp_path) -> None:
    destination = tmp_path / "share"

    count, error = packager.copy_tree(
        fake_repo, packager.collect_files(fake_repo), destination
    )

    assert error == ""

    copied = destination / packager.PROJECT_NAME

    assert (copied / "pyproject.toml").is_file()
    assert (copied / "src" / "windows_agent_mcp" / "main.py").is_file()
    assert (copied / "tests" / "conftest.py").is_file()
    assert count > 0


def test_dest_copy_omits_machine_specific_files(fake_repo, tmp_path) -> None:
    destination = tmp_path / "share"

    packager.copy_tree(fake_repo, packager.collect_files(fake_repo), destination)

    copied = destination / packager.PROJECT_NAME

    assert not (copied / "input" / "config.json").exists()
    assert not (copied / "mcp-allowed-hosts.json").exists()
    assert not (copied / ".venv").exists()


def test_dest_copy_reports_a_failure(fake_repo, tmp_path, monkeypatch) -> None:
    def denied(*args: object, **kwargs: object):
        raise PermissionError("read-only")

    monkeypatch.setattr(packager.shutil, "copy2", denied)

    _count, error = packager.copy_tree(
        fake_repo, packager.collect_files(fake_repo), tmp_path / "share"
    )

    assert "could not copy" in error


# ============================================================
# package: CLI
# ============================================================


@pytest.fixture
def rooted_at(monkeypatch):
    """Point package.py's module-level ROOT at a fake repo.

    main() reads ROOT, so without this the CLI tests would package the real
    repository and write into it.
    """

    def install(root: Path) -> None:
        monkeypatch.setattr(packager, "ROOT", root)

    return install


def test_cli_list_prints_the_selection(fake_repo, rooted_at, capsys) -> None:
    rooted_at(fake_repo)

    assert packager.main(["--list"]) == 0

    out = capsys.readouterr().out

    # The listed files are the indented lines before the blank-line summary.
    listed = {
        line.strip()
        for line in out.splitlines()
        if line.startswith("  ") and " " not in line.strip()
    }

    assert "pyproject.toml" in listed
    assert "input/config.json" not in listed
    assert "files," in out

    # The footer names what was excluded, which is the point of a dry run --
    # an allowlist fails by omission, so the omissions have to be visible.
    assert "Excluded by the allowlist" in out
    assert "input" in out


def test_cli_writes_a_versioned_archive(fake_repo, rooted_at, capsys) -> None:
    rooted_at(fake_repo)

    assert packager.main([]) == 0

    archive = fake_repo / "dist" / f"{packager.PROJECT_NAME}-9.9.9.zip"

    assert archive.is_file()
    assert "verified" in capsys.readouterr().out


def test_cli_honours_output(fake_repo, rooted_at, tmp_path) -> None:
    rooted_at(fake_repo)

    target = tmp_path / "custom.zip"

    assert packager.main(["--output", str(target)]) == 0
    assert target.is_file()


def test_cli_honours_dest(fake_repo, rooted_at, tmp_path, capsys) -> None:
    rooted_at(fake_repo)

    destination = tmp_path / "share"

    assert packager.main(["--dest", str(destination)]) == 0
    assert (destination / packager.PROJECT_NAME / "pyproject.toml").is_file()
    assert "bootstrap.py" in capsys.readouterr().out


def test_cli_refuses_an_empty_directory(tmp_path, rooted_at, capsys) -> None:
    """Running it from the wrong place should say so, not write an empty zip."""

    rooted_at(tmp_path)

    assert packager.main([]) == 1
    assert "nothing to package" in capsys.readouterr().err


def test_cli_reports_an_archive_failure(
    fake_repo, rooted_at, monkeypatch, capsys
) -> None:
    rooted_at(fake_repo)

    monkeypatch.setattr(packager, "write_archive", lambda *a, **k: (0, "disk on fire"))

    assert packager.main([]) == 1
    assert "disk on fire" in capsys.readouterr().err


def test_cli_reports_a_copy_failure(
    fake_repo, rooted_at, tmp_path, monkeypatch, capsys
) -> None:
    rooted_at(fake_repo)

    monkeypatch.setattr(packager, "copy_tree", lambda *a, **k: (0, "no space"))

    assert packager.main(["--dest", str(tmp_path / "x")]) == 1
    assert "no space" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (5 * 1024 * 1024, "5.0 MB")],
)
def test_human_sizes(size: int, expected: str) -> None:
    assert packager._human(size) == expected


# ============================================================
# Documentation must actually ship
# ============================================================

# Documents deliberately NOT packaged. Empty today; an entry here is a
# statement that a file is intentionally local, which is the only acceptable
# reason for a document to be missing from the archive.
NOT_PACKAGED: frozenset[str] = frozenset()


def test_every_document_is_packaged() -> None:
    """A doc that does not travel with the copy it explains is worse than none.

    The allowlist in package.py is fail-safe by design: anything not named is
    left out. That is right for files holding local paths, and wrong for
    documentation -- and the other allowlist tests use a fabricated tree, so
    they cannot notice a real file being forgotten. This test looks at the
    actual repository.

    Covers `docs/` as well as the root. When the docs moved into `docs/`, a
    root-only glob would have kept passing while silently guarding nothing --
    which is exactly the failure this test exists to prevent.
    """

    present = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in [*REPO_ROOT.glob("*.md"), *REPO_ROOT.glob("docs/*.md")]
        # `*_claude.md` is local context: gitignored, and deliberately absent
        # from the archive. test_local_context_files_are_never_packaged covers
        # that side, so exempting them here is not a hole.
        if not path.name.endswith("_claude.md")
    }

    packaged = set(packager.INCLUDED_FILES)

    missing = sorted(present - packaged - NOT_PACKAGED)

    assert missing == [], (
        f"docs missing from package.py INCLUDED_FILES: {missing}. "
        f"Add them there, or to NOT_PACKAGED if they are deliberately local."
    )


def test_the_docs_directory_is_where_the_docs_are() -> None:
    """Guides live in docs/; only README.md sits at the top."""

    at_root = sorted(path.name for path in REPO_ROOT.glob("*.md"))

    assert at_root == ["README.md"], (
        f"unexpected markdown at the repository root: {at_root}. "
        f"Everything except README.md belongs in docs/."
    )

    assert (REPO_ROOT / "docs" / "HOW_TO_USE.md").is_file()


def test_the_repository_is_self_contained() -> None:
    """Everything the project claims about itself must be inside it.

    This is published on its own, so a file one directory up does not exist for
    anyone who clones it. pyproject.toml declares MIT, and a licence claim with
    no licence file is worse than no claim.
    """

    assert (REPO_ROOT / "LICENSE").is_file(), (
        "pyproject.toml declares a licence but no LICENSE file is present"
    )
    assert (REPO_ROOT / ".gitignore").is_file(), (
        "no .gitignore: .venv, dist and input/config.json would be committed"
    )


def test_nothing_tracked_references_local_context_files() -> None:
    """`*_claude.md` files are gitignored, so a clone will not have them.

    They hold development narrative and design notes kept alongside the code
    but not published. A tracked file linking to one is a dead link for
    everybody else -- and the failure is invisible here, where the files exist.

    Checked by name rather than by asking git, so this works in an extracted
    archive and in a checkout that is not a repository yet.
    """

    tracked = [
        path
        for path in [*REPO_ROOT.glob("*.md"), *REPO_ROOT.glob("docs/*.md")]
        if not path.name.endswith("_claude.md")
    ]

    offenders: list[str] = []

    for path in tracked:
        text = path.read_text(encoding="utf-8")

        for line_no, line in enumerate(text.splitlines(), 1):
            if "_claude.md" in line:
                relative = path.relative_to(REPO_ROOT).as_posix()
                offenders.append(f"{relative}:{line_no}: {line.strip()[:70]}")

    assert offenders == [], (
        "published files reference gitignored local-context files:\n  "
        + "\n  ".join(offenders)
    )


def test_claude_md_is_never_committed_or_packaged() -> None:
    """CLAUDE.md holds design rules for an AI assistant, and is not published.

    Two separate guards, because the obvious one has a hole: the `*_claude.md`
    pattern in .gitignore does NOT match `CLAUDE.md` -- there is no underscore
    -- so the file has to be named explicitly there as well.

    The reasoning it holds is not lost by excluding it. It lives in README.md
    under "Design notes", and in comments beside the code it explains, where
    everyone reading the repository can find it.
    """

    assert not (REPO_ROOT / "CLAUDE.md").exists(), (
        "CLAUDE.md is present in the repository; it is local context and "
        "belongs above this directory"
    )

    assert "CLAUDE.md" not in packager.INCLUDED_FILES, (
        "CLAUDE.md is in the archive allowlist"
    )

    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "CLAUDE.md" in [line.strip() for line in ignored], (
        "CLAUDE.md is not named in .gitignore, and `*_claude.md` does not "
        "match it -- a copy placed here would be committed"
    )


def test_local_context_files_are_never_packaged() -> None:
    """The archive must be what the repository is, not more.

    package.py selects by allowlist, so this holds by construction today. The
    test exists so that adding a `*_claude.md` to INCLUDED_FILES has to be a
    deliberate argument rather than an oversight.
    """

    packaged = [name for name in packager.INCLUDED_FILES if name.endswith("_claude.md")]

    assert packaged == [], f"local-context files in the archive allowlist: {packaged}"

    selected = [
        path.as_posix()
        for path in packager.collect_files(REPO_ROOT)
        if path.name.endswith("_claude.md")
    ]

    assert selected == [], f"local-context files selected for the archive: {selected}"


def test_not_packaged_entries_are_real_files() -> None:
    """A stale exemption would silently hide a genuinely missing document."""

    for name in NOT_PACKAGED:
        assert (REPO_ROOT / name).is_file(), (
            f"NOT_PACKAGED names a missing file: {name}"
        )


def test_the_usage_guide_is_packaged() -> None:
    """Named explicitly: it is the one file a copied project cannot lack."""

    assert "docs/HOW_TO_USE.md" in packager.INCLUDED_FILES
