"""Tests for command-composition rejection and tokenisation.

The allowlist names a single executable, so it only means anything if the
command string holds exactly one command. Before this check,
`git status; Stop-Computer` passed validation: the first token was `git`, and
everything after the separator went unexamined.

The hard part is not rejecting composition -- it is rejecting it without
rejecting ordinary work. `git commit -m "fix; cleanup"` and
`pip install "requests>=2.0"` both carry a composition character inside a
quoted argument, where PowerShell treats it as literal text. A blanket
textual search would reject both, which is the same class of false positive
as the old missing-word-boundary bug in DANGEROUS_PATTERNS.
"""

from __future__ import annotations

import pytest

from windows_agent_mcp.allowed_command import (
    find_command_composition,
    find_inline_code_flag,
    tokenize_command,
)
from windows_agent_mcp.tools.run_powershell import validate_powershell_command

# ============================================================
# Composition is rejected
# ============================================================


@pytest.mark.parametrize(
    "command",
    [
        # Statement separator: the original bypass.
        "git status; Stop-Computer -Force",
        "cmake --version; Set-Content C:/x.ps1 payload",
        # Pipelines hand output to an unchecked command.
        "git --version | Out-File C:/Windows/Temp/a.txt",
        # Chain operators.
        "git status && Stop-Computer",
        "git status || Stop-Computer",
        # Call / background operator.
        "git & Stop-Computer",
        # Redirection writes anywhere the process can reach.
        "git --version > C:/Windows/Temp/a.txt",
        "git --version >> out.txt",
        "git status < input.txt",
        # Subexpressions execute before the outer command runs.
        "git $(Invoke-WebRequest https://example.invalid)",
        "git @(Get-Content payload.txt)",
        # A newline is simply another statement.
        "cmake --version\nStop-Computer",
        "cmake --version\rStop-Computer",
        # Backtick escaping is an obfuscation route.
        "git `nStop-Computer",
    ],
)
def test_validate_rejects_composition(command: str) -> None:
    with pytest.raises(ValueError, match="more than one command"):
        validate_powershell_command(command)


@pytest.mark.parametrize(
    "command",
    [
        'git "$(Invoke-WebRequest https://example.invalid)"',
        'git commit -m "$(whoami)"',
        'cmake -DX="$(whoami)"',
    ],
)
def test_subexpression_inside_double_quotes_is_rejected(command: str) -> None:
    """$() interpolates inside double quotes, so it executes there too.

    This is the one place where honouring quotes would be wrong.
    """

    with pytest.raises(ValueError, match="more than one command"):
        validate_powershell_command(command)


# ============================================================
# ...but not inside quotes, where it is literal text
# ============================================================


@pytest.mark.parametrize(
    "command",
    [
        # Single quotes are fully literal in PowerShell.
        "git commit -m 'fix; cleanup'",
        "git commit -m 'a | b'",
        "git commit -m '$(whoami)'",
        "git commit -m 'redirect > here'",
        # Double quotes make these literal too (except $(), covered above).
        'git commit -m "fix; cleanup"',
        'git commit -m "handles a|b and c>d"',
        'git log --grep="feat|fix"',
        'python -m pip install "requests>=2.0"',
        'msbuild "/p:Configuration=Release;Platform=x64" App.sln',
        # An apostrophe inside a double-quoted string is just text.
        'git commit -m "it\'s fine"',
        # A doubled quote is an escaped literal quote.
        "git commit -m 'it''s fine'",
    ],
)
def test_composition_characters_allowed_inside_quotes(command: str) -> None:
    validate_powershell_command(command)


def test_unquoted_separator_is_rejected_even_if_meant_as_text() -> None:
    """PowerShell would split this too, so rejecting it is correct.

    `msbuild /p:A=1;B=2` runs msbuild with `/p:A=1` and then tries to run
    `B=2`, so the argument has to be quoted either way. The error says so.
    """

    with pytest.raises(ValueError, match="quote the argument"):
        validate_powershell_command("msbuild /p:Configuration=Release;Platform=x64")


@pytest.mark.parametrize(
    "command", ['git commit -m "unterminated', "git commit -m 'unterminated"]
)
def test_unterminated_quotes_are_rejected(command: str) -> None:
    with pytest.raises(ValueError):
        validate_powershell_command(command)


# ============================================================
# find_command_composition reports what it found
# ============================================================


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", None),
        ('git commit -m "a; b"', None),
        ("git status; whoami", "statement separator ';'"),
        ("git | whoami", "pipeline '|'"),
        ("git & whoami", "call/background operator '&'"),
        ("git > out.txt", "output redirection '>'"),
        ("git < in.txt", "input redirection '<'"),
        ("git $(whoami)", "subexpression '$('"),
        ("git @(whoami)", "array subexpression '@('"),
        ('git "$(whoami)"', "subexpression '$(' inside a double-quoted string"),
        ("git `x", "backtick escape"),
        ('git "unterminated', "unterminated quoted string"),
    ],
)
def test_find_command_composition(command: str, expected: str | None) -> None:
    assert find_command_composition(command) == expected


# ============================================================
# Tokenisation
# ============================================================


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", ["git", "status"]),
        ("  git   status  ", ["git", "status"]),
        (
            '"C:/Program Files/Git/git.exe" status',
            ["C:/Program Files/Git/git.exe", "status"],
        ),
        ("'C:/tools/premake5.exe' --version", ["C:/tools/premake5.exe", "--version"]),
        ('git commit -m "two words"', ["git", "commit", "-m", "two words"]),
        ("git commit -m 'it''s'", ["git", "commit", "-m", "it's"]),
        ('git commit -m "say ""hi"""', ["git", "commit", "-m", 'say "hi"']),
        # Quoting can begin part-way through a token.
        ('cmake -DFLAGS="-Wall -Wextra"', ["cmake", "-DFLAGS=-Wall -Wextra"]),
        # An empty quoted argument is a real, distinct token.
        ('git commit -m ""', ["git", "commit", "-m", ""]),
    ],
)
def test_tokenize_command(command: str, expected: list[str]) -> None:
    assert tokenize_command(command) == expected


@pytest.mark.parametrize("command", ["", "   ", '"open', "'open"])
def test_tokenize_command_rejects(command: str) -> None:
    with pytest.raises(ValueError):
        tokenize_command(command)


# ============================================================
# Interpreters may not be handed code inline
# ============================================================


@pytest.mark.parametrize(
    "command",
    [
        'python -c "import os"',
        "python -cprint(1)",
        'python.exe -c "x"',
        'py -c "x"',
        'node -e "x"',
        'node --eval "x"',
        'node -p "x"',
        'node --print "x"',
    ],
)
def test_validate_rejects_inline_code(command: str) -> None:
    with pytest.raises(ValueError, match="inline"):
        validate_powershell_command(command)


@pytest.mark.parametrize(
    "command",
    [
        # -m runs an installed module: how pip and pytest are invoked.
        "python -m pip install requests",
        "python -m pytest tests -q",
        "python -V",
        "python --version",
        # A script file is still allowed -- see the module docstring in
        # allowed_command.py for why this is not a contradiction.
        "python build.py --release",
        "node build.js",
        "node --version",
        # The flag belongs to another tool, not an interpreter.
        "cmake -E echo hi",
        "git commit -c HEAD",
        "7z a -p archive.7z dir",
    ],
)
def test_inline_code_check_does_not_overreach(command: str) -> None:
    validate_powershell_command(command)


@pytest.mark.parametrize(
    ("command", "is_clean"),
    [
        ("git -c core.editor=vim commit", True),
        ("cmake -e something", True),
        ("python -m pip list", True),
        ('python -c "x"', False),
    ],
)
def test_find_inline_code_flag_targets_interpreters_only(
    command: str, is_clean: bool
) -> None:
    assert (find_inline_code_flag(command) is None) is is_clean


# ============================================================
# The whole point: ordinary work still runs
# ============================================================


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git clone --depth 1 https://github.com/premake/premake-core",
        "git checkout 9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f00",
        'git log --format="%h %s"',
        "cmake --build . --config RelWithDebInfo --target install",
        'cmake -DCMAKE_CXX_FLAGS="-Wall -Wextra" -S . -B build',
        'cmake -G "Visual Studio 17 2022" -A x64 -S . -B build',
        'msbuild "MySolution.sln" /p:Configuration=Release',
        "python -m pip install --upgrade pip",
        "npm install --save-dev typescript",
        "npx tsc --noEmit",
        "premake5 --file=premake5.lua vs2022",
        "ninja -C build -j 16",
        "tar -xzf premake-5.0.0-beta2-windows.tar.gz",
        "7z a -mx=9 archive.7z build",
        "dotnet build --configuration Release --no-restore",
        "cargo build --release --target x86_64-pc-windows-msvc",
        "Get-ChildItem -Recurse -Filter *.cpp",
        'Select-String -Path "src/*.cpp" -Pattern TODO',
        'Get-Content "C:/project/README.md"',
        'Test-Path "C:/Program Files/Git/bin/git.exe"',
        "Get-Command premake5",
        '"C:/Program Files/Git/bin/git.exe" status',
        "'C:/tools/premake5.exe' --version",
        "clang++ -std=c++20 -O2 main.cpp -o app.exe",
        "cl /std:c++20 /EHsc main.cpp",
        "gcc -Wall -Wextra -o out main.c",
    ],
)
def test_realistic_development_commands_are_not_blocked(command: str) -> None:
    """Regression guard: tightening the policy must not break the tool."""

    validate_powershell_command(command)
