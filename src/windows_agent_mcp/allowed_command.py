"""PowerShell security policy for Windows Agent MCP Server.

What this policy now guarantees:

    * exactly ONE command runs per call -- no separators, pipelines,
      redirections or subexpressions, so the allowlist cannot be sidestepped
      by appending a second command;
    * that command's executable is on ALLOWED_COMMANDS;
    * no interpreter is handed code inline (`python -c`, `node -e`).

What it does NOT guarantee, and cannot:

    * an allowlisted interpreter given a FILE runs whatever that file
      contains -- `python build.py` is arbitrary code, by design, because
      running project scripts is the point of the tool;
    * `npx` and `pip` fetch and execute third-party packages;
    * an allowlisted build tool runs whatever its build files say.

So this is a meaningful boundary on *what program starts*, not a sandbox on
what that program then does. For untrusted input, isolate at the OS level.
"""

from __future__ import annotations

from pathlib import Path

# ============================================================
# PowerShell security policy
# ============================================================

# IMPORTANT:
#
# This is deliberately NOT:
#
#     "allow every PowerShell command"
#
# Instead we permit common development executables/cmdlets.
#
# This is defense-in-depth only.
#
# The real Bionic shell is still available to the agent.
# Therefore, this MCP PowerShell tool should be considered
# a convenience tool, not a hardened security boundary.

ALLOWED_COMMANDS: set[str] = {
    # Development tools
    "git",
    "git.exe",
    "cmake",
    "cmake.exe",
    "ninja",
    "ninja.exe",
    "premake5",
    "premake5.exe",
    "python",
    "python.exe",
    "py",
    "py.exe",
    "pip",
    "pip.exe",
    "node",
    "node.exe",
    "npm",
    "npm.cmd",
    "npx",
    "npx.cmd",
    "dotnet",
    "dotnet.exe",
    "cargo",
    "cargo.exe",
    "rustc",
    "rustc.exe",
    "clang",
    "clang.exe",
    "clang++",
    "clang++.exe",
    "gcc",
    "gcc.exe",
    "g++",
    "g++.exe",
    "cl",
    "cl.exe",
    "msbuild",
    "msbuild.exe",
    "ctest",
    "ctest.exe",
    # Shader compilers and SPIR-V tools.
    #
    # A renderer cannot be worked on without these: compiling a shader is the
    # tightest feedback loop in graphics code, and without them the model can
    # write GLSL/HLSL but never find out whether it compiles. glslc and
    # glslangValidator ship with the Vulkan SDK, dxc with both the Vulkan SDK
    # and the Windows SDK, fxc with the Windows SDK. run_powershell rebuilds
    # PATH from the registry, so an SDK installed after this process started
    # is still found.
    "glslc",
    "glslc.exe",
    "glslangValidator",
    "glslangValidator.exe",
    "dxc",
    "dxc.exe",
    "fxc",
    "fxc.exe",
    "spirv-val",
    "spirv-val.exe",
    "spirv-dis",
    "spirv-dis.exe",
    "spirv-opt",
    "spirv-opt.exe",
    "spirv-cross",
    "spirv-cross.exe",
    # Archive tools
    "tar",
    "tar.exe",
    "7z",
    "7z.exe",
    # Basic filesystem navigation
    "cd",
    # Basic filesystem inspection
    "Get-Location",
    "Get-ChildItem",
    "Get-Item",
    "Test-Path",
    "Get-Content",
    "Select-String",
    # Basic filesystem creation/manipulation
    "New-Item",
    "Copy-Item",
    "Move-Item",
    # Environment inspection
    "Get-Command",
    "Get-ComputerInfo",
    "Get-Process",
}

# Programs build_project will drive.
#
# A strict subset of ALLOWED_COMMANDS, and narrower on purpose. build_project
# executes argv directly with no shell, so a PowerShell cmdlet such as
# Get-ChildItem cannot run at all -- and "cmdlet failed to start" is a far
# worse error message than "that is not a build command". Restricting the set
# up front turns a confusing exec failure into an actionable one.
BUILD_COMMANDS: set[str] = {
    "cmake",
    "cmake.exe",
    "ninja",
    "ninja.exe",
    "msbuild",
    "msbuild.exe",
    "ctest",
    "ctest.exe",
    "premake5",
    "premake5.exe",
    "dotnet",
    "dotnet.exe",
    "cargo",
    "cargo.exe",
    "clang",
    "clang.exe",
    "clang++",
    "clang++.exe",
    "gcc",
    "gcc.exe",
    "g++",
    "g++.exe",
    "cl",
    "cl.exe",
    "python",
    "python.exe",
    "py",
    "py.exe",
}

# Shader compilers, keyed by the executable compile_shader will invoke.
SHADER_COMPILERS: set[str] = {
    "glslc",
    "glslangValidator",
    "dxc",
    "fxc",
}


def extract_command_name(command: str) -> str:
    """Extract the first token/command name from a PowerShell command.

    This is intentionally simple because the command is already subject to the
    stricter token policy.

    Args:
        command: PowerShell command string.

    Returns:
        The first word/token of the command (stripped).

    Raises:
        ValueError: If command is empty or has unterminated quotes.

    Example:
        >>> extract_command_name("python --version")
        'python',

        >>> extract_command_name('"git" status')
        'git',
    """

    tokens = tokenize_command(command)

    if not tokens:
        raise ValueError("Command cannot be empty.")

    return tokens[0]


def is_command_allowed(command: str) -> bool:
    """Check whether the command's executable is on the allowlist.

    Only meaningful once find_command_composition() has confirmed the string
    holds a single command; otherwise this describes the first of several.

    Args:
        command: PowerShell command to check.

    Returns:
        True if the command name or its basename is in ALLOWED_COMMANDS.

    Example:
        >>> is_command_allowed("python --version")
        True

        >>> is_command_allowed("rm -rf /")
        False
    """

    command_name = extract_command_name(command)

    command_basename = Path(command_name).name

    return command_name in ALLOWED_COMMANDS or command_basename in ALLOWED_COMMANDS


# ============================================================
# Command composition
# ============================================================

# Characters that start a second command, capture another command's output, or
# write to the filesystem outside the allowlist's reach.
#
# These are only rejected OUTSIDE quotes. A blanket textual search would
# reject ordinary work -- `git commit -m "fix; cleanup"` and
# `pip install "requests>=2.0"` both carry one of these inside a quoted
# argument, where PowerShell treats it as literal text.
_COMPOSITION_OPERATORS: dict[str, str] = {
    ";": "statement separator ';'",
    "|": "pipeline '|'",
    "&": "call/background operator '&'",
    "\n": "newline (more than one statement)",
    "\r": "carriage return (more than one statement)",
    ">": "output redirection '>'",
    "<": "input redirection '<'",
    "`": "backtick escape",
}


def find_command_composition(command: str) -> str | None:
    """Find any construct that would run a second command.

    The allowlist inspects one executable name, so it only means anything if
    the string contains exactly one command. Without this check
    `git status; Stop-Computer` passed: the first token was `git`, and
    everything after the separator went unexamined.

    Quoting is honoured, because PowerShell honours it -- with one exception.
    `$(...)` interpolates *inside* double quotes, so it executes there and is
    rejected in double-quoted text as well as unquoted text. Single quotes are
    literal in PowerShell, so nothing inside them can run.

    Args:
        command: PowerShell command to inspect.

    Returns:
        A description of the first offending construct, or None if the command
        is a single statement.
    """

    in_single = False
    in_double = False

    index = 0
    length = len(command)

    while index < length:
        char = command[index]

        if in_single:
            # '' is an escaped literal quote; anything else is literal text.
            if char == "'":
                if command[index + 1 : index + 2] == "'":
                    index += 2
                    continue
                in_single = False
            index += 1
            continue

        if in_double:
            if char == "`":
                # Escapes the following character, including a quote.
                index += 2
                continue
            if char == '"':
                if command[index + 1 : index + 2] == '"':
                    index += 2
                    continue
                in_double = False
                index += 1
                continue
            if char == "$" and command[index + 1 : index + 2] == "(":
                return "subexpression '$(' inside a double-quoted string"
            index += 1
            continue

        # Unquoted.
        if char == "'":
            in_single = True
            index += 1
            continue

        if char == '"':
            in_double = True
            index += 1
            continue

        if char == "$" and command[index + 1 : index + 2] == "(":
            return "subexpression '$('"

        if char == "@" and command[index + 1 : index + 2] == "(":
            return "array subexpression '@('"

        if char in _COMPOSITION_OPERATORS:
            return _COMPOSITION_OPERATORS[char]

        index += 1

    if in_single or in_double:
        return "unterminated quoted string"

    return None


# ============================================================
# Tokenisation
# ============================================================


def tokenize_command(command: str) -> list[str]:
    """Split a command into tokens, honouring PowerShell quoting.

    Quotes are removed from the tokens they delimit, so a quoted executable
    path arrives as a plain path.

    Args:
        command: PowerShell command to split.

    Returns:
        The command's tokens, in order.

    Raises:
        ValueError: If the command is empty or a quoted string is unterminated.

    Example:
        >>> tokenize_command('"C:/Program Files/Git/git.exe" status')
        ['C:/Program Files/Git/git.exe', 'status']
    """

    if not command.strip():
        raise ValueError("Command cannot be empty.")

    tokens: list[str] = []
    current: list[str] = []
    has_token = False

    in_single = False
    in_double = False

    index = 0
    length = len(command)

    while index < length:
        char = command[index]

        if in_single:
            if char == "'":
                if command[index + 1 : index + 2] == "'":
                    current.append("'")
                    index += 2
                    continue
                in_single = False
            else:
                current.append(char)
            index += 1
            continue

        if in_double:
            if char == "`" and index + 1 < length:
                current.append(command[index + 1])
                index += 2
                continue
            if char == '"':
                if command[index + 1 : index + 2] == '"':
                    current.append('"')
                    index += 2
                    continue
                in_double = False
            else:
                current.append(char)
            index += 1
            continue

        if char == "'":
            in_single = True
            has_token = True
            index += 1
            continue

        if char == '"':
            in_double = True
            has_token = True
            index += 1
            continue

        if char.isspace():
            if has_token:
                tokens.append("".join(current))
                current.clear()
                has_token = False
            index += 1
            continue

        current.append(char)
        has_token = True
        index += 1

    if in_single or in_double:
        raise ValueError("Unterminated quoted command.")

    if has_token:
        tokens.append("".join(current))

    if not tokens:
        raise ValueError("Command cannot be empty.")

    return tokens


# ============================================================
# Interpreters
# ============================================================

# Allowlisting an interpreter allows whatever it is told to run. Blocking the
# inline-code switches removes the direct route, so the allowlist governs
# which *program* runs rather than being bypassed outright in one argument.
#
# This does NOT contain an interpreter given a script file: `python build.py`
# still runs whatever build.py contains. See the module docstring.
INLINE_CODE_FLAGS: dict[str, frozenset[str]] = {
    "python": frozenset({"-c", "--command"}),
    "py": frozenset({"-c", "--command"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
}


def find_inline_code_flag(command: str) -> str | None:
    """Find an interpreter switch that executes code supplied inline.

    Args:
        command: PowerShell command to inspect.

    Returns:
        A description of the offending switch, or None.

    Raises:
        ValueError: If the command cannot be tokenised.
    """

    tokens = tokenize_command(command)

    executable = Path(tokens[0]).name.lower()

    # Drop a Windows executable suffix so "python.exe" matches "python".
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if executable.endswith(suffix):
            executable = executable[: -len(suffix)]
            break

    flags = INLINE_CODE_FLAGS.get(executable)

    if not flags:
        return None

    for token in tokens[1:]:
        # Match "-c" and the glued "-c<code>" form alike.
        candidate = token.split("=", 1)[0]

        if candidate in flags:
            return f"{executable} {candidate} (runs code supplied inline)"

        if token.startswith("-") and not token.startswith("--"):
            # Short switches may be glued to their value: -cprint(1)
            for flag in flags:
                if len(flag) == 2 and token.startswith(flag) and len(token) > 2:
                    return f"{executable} {flag} (runs code supplied inline)"

    return None
