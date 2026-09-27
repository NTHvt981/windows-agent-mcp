"""PowerShell security policy: bounds which program starts, not what it does."""

from __future__ import annotations

from pathlib import Path

ALLOWED_COMMANDS: set[str] = {
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
    "tar",
    "tar.exe",
    "7z",
    "7z.exe",
    "cd",
    "Get-Location",
    "Get-ChildItem",
    "Get-Item",
    "Test-Path",
    "Get-Content",
    "Select-String",
    "New-Item",
    "Copy-Item",
    "Move-Item",
    "Get-Command",
    "Get-ComputerInfo",
    "Get-Process",
}

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

SHADER_COMPILERS: set[str] = {
    "glslc",
    "glslangValidator",
    "dxc",
    "fxc",
}


def extract_command_name(command: str) -> str:
    """Extract the first token/command name from a PowerShell command."""

    tokens = tokenize_command(command)

    if not tokens:
        raise ValueError("Command cannot be empty.")

    return tokens[0]


def is_command_allowed(command: str) -> bool:
    """Check whether the command's executable is on the allowlist."""

    # Only meaningful after composition confirms a single command.

    command_name = extract_command_name(command)

    command_basename = Path(command_name).name

    return command_name in ALLOWED_COMMANDS or command_basename in ALLOWED_COMMANDS


# Rejected outside quotes only; quoted text is literal.
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
    """Find any construct that would run a second command."""

    # $(...) executes inside double quotes, so it is rejected there too.

    in_single = False
    in_double = False

    index = 0
    length = len(command)

    while index < length:
        char = command[index]

        if in_single:
            if char == "'":
                if command[index + 1 : index + 2] == "'":
                    index += 2
                    continue
                in_single = False
            index += 1
            continue

        if in_double:
            if char == "`":
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


def tokenize_command(command: str) -> list[str]:
    """Split a command into tokens, honouring PowerShell quoting."""

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


INLINE_CODE_FLAGS: dict[str, frozenset[str]] = {
    "python": frozenset({"-c", "--command"}),
    "py": frozenset({"-c", "--command"}),
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
}


def find_inline_code_flag(command: str) -> str | None:
    """Find an interpreter switch that executes code supplied inline."""

    tokens = tokenize_command(command)

    executable = Path(tokens[0]).name.lower()

    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if executable.endswith(suffix):
            executable = executable[: -len(suffix)]
            break

    flags = INLINE_CODE_FLAGS.get(executable)

    if not flags:
        return None

    for token in tokens[1:]:
        candidate = token.split("=", 1)[0]

        if candidate in flags:
            return f"{executable} {candidate} (runs code supplied inline)"

        if token.startswith("-") and not token.startswith("--"):
            for flag in flags:
                if len(flag) == 2 and token.startswith(flag) and len(token) > 2:
                    return f"{executable} {flag} (runs code supplied inline)"

    return None


FILE_WRITE_CMDLETS: frozenset[str] = frozenset({"new-item", "copy-item", "move-item"})

_VALUELESS_SWITCHES: frozenset[str] = frozenset({"force", "recurse", "passthru", "whatif", "confirm"})


def find_cmdlet_destinations(command: str) -> list[str] | None:
    """Destination paths a file-creation cmdlet would write, if any."""

    try:
        tokens = tokenize_command(command)
    except ValueError:
        return None

    if not tokens or tokens[0].lower() not in FILE_WRITE_CMDLETS:
        return None

    is_new_item = tokens[0].lower() == "new-item"

    path_param: str | None = None
    name_param: str | None = None
    destination_param: str | None = None
    positionals: list[str] = []

    index = 1

    while index < len(tokens):
        token = tokens[index]

        if token.startswith("-") and len(token) > 1:
            name, separator, inline = token[1:].partition(":")

            name = name.lower()

            if separator and inline:
                value: str | None = inline
            elif name in _VALUELESS_SWITCHES:
                if name == "whatif":
                    return None
                index += 1
                continue
            elif index + 1 < len(tokens):
                value = tokens[index + 1]
                index += 1
            else:
                value = None

            if value is not None:
                if name in ("path", "literalpath"):
                    path_param = value
                elif name == "destination":
                    destination_param = value
                elif name == "name":
                    name_param = value
        else:
            positionals.append(token)

        index += 1

    if is_new_item:
        base = path_param if path_param is not None else (positionals[0] if positionals else None)

        if base is None:
            return []

        if name_param is not None:
            return [str(Path(base) / name_param)]

        return [base]

    if destination_param is not None:
        return [destination_param]

    # A lone positional prompts and dies; only a second positional is a target.
    if len(positionals) >= 2:
        return [positionals[-1]]

    return []
