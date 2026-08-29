"""Named tool-group profiles, loaded from a JSON file.

Tool groups shipped as a raw environment variable
(`BIONIC_TOOLS=edit,build,docs`). That works, but the useful combinations have
to be remembered, there is nowhere to record *why* one exists, and switching
posture means retyping a comma list. A profile gives the combination a name, a
description and an `enable` flag, so one can be parked without being deleted.

Two things this module deliberately does rather than trusting the caller:

* **It validates `tools` through `utils.parse_tool_groups()`.** A profile
  saying `"core,cpp"` -- a natural guess, since `cpp` is not a group -- is
  reported at load with a message naming the six real groups. Passed straight
  through as an environment variable it would merely warn at startup and
  silently register the default 17 tools.
* **It emits an absolute interpreter path.** `"command": "windows-agent-mcp"`
  looks right but fails in a real client: the console script lives in
  `.venv/Scripts` and is not on a global PATH, so Claude Desktop cannot launch
  it.

Layering note. `utils` must not import this module (this module needs
`VALID_TOOL_GROUPS` from it), so the server does NOT consult profiles deep in
`active_tool_groups()`. Instead `main()` resolves the profile first and
populates `BIONIC_TOOLS` from it, after which every existing code path works
unchanged.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, NamedTuple

from .inspector_config import FORWARDED_ENV_VARS
from .inspector_config import build_config as build_inspector_config
from .utils import (
    PROFILE_ENV_VAR,
    PROFILES_FILENAME,
    TOOL_GROUPS_ENV_VAR,
    parse_tool_groups,
)

__all__: list[str] = [
    "DEFAULT_PROFILES_FILENAME",
    "PROFILES_FILE_ENV_VAR",
    "SCHEMA_VERSION",
    "Profile",
    "apply_profile_to_environment",
    "emit_client_config",
    "emit_inspector_config",
    "find_profiles_file",
    "load_profiles",
    "main",
    "parse_profiles",
    "resolve_profile",
    "scaffold",
]

# Sourced from utils rather than declared here, even though this module owns
# the format. utils needs the name for its write guard and cannot import this
# module (the dependency runs the other way), so one of the two has to be the
# definition and it cannot be this one.
DEFAULT_PROFILES_FILENAME = PROFILES_FILENAME

# Points at a profiles file elsewhere. Useful when a client sets a working
# directory that is not the repository.
PROFILES_FILE_ENV_VAR = "BIONIC_PROFILES_FILE"

# Bumped only for a breaking format change. An unrecognised version is an error
# rather than a best-effort read, so a future format cannot be misinterpreted
# as valid by an older build.
SCHEMA_VERSION = 1

# Keys a profile's `env` block may never set.
#
# BIONIC_TOOLS duplicates the `tools` field, and two sources for one setting is
# ambiguous rather than convenient. BIONIC_PROFILE would be circular: a profile
# selecting a profile.
_FORBIDDEN_ENV_KEYS = frozenset({TOOL_GROUPS_ENV_VAR, PROFILE_ENV_VAR})


class Profile(NamedTuple):
    """One named combination of tool groups.

    Attributes:
        name: Key from the profiles file.
        enable: False parks the profile: it is not emitted to client config,
            and selecting it by name is refused.
        description: Free text, shown by --list. The file is not committed, so
            this is where the reason for a profile lives.
        tools: A BIONIC_TOOLS string, already validated.
        env: Extra environment variables to apply.
    """

    name: str
    enable: bool
    description: str
    tools: str
    env: dict[str, str]


def find_profiles_file() -> Path:
    """Locate the profiles file.

    Order, most explicit first:

    1. BIONIC_PROFILES_FILE.
    2. `mcp-profiles.json` in the current directory. The launcher pushd's to
       the repository root, so this is the normal case.
    3. The repository root inferred from this file's location, but only for an
       editable install -- confirmed by pyproject.toml being there. In a
       site-packages install that path is meaningless, so it is not used.

    Returns:
        The path to use. May not exist; callers handle that.
    """

    configured = os.environ.get(PROFILES_FILE_ENV_VAR, "").strip()

    if configured:
        return Path(configured).expanduser()

    in_cwd = Path.cwd() / DEFAULT_PROFILES_FILENAME

    if in_cwd.is_file():
        return in_cwd

    # src/windows_agent_mcp/profiles.py -> repository root
    repo_root = Path(__file__).resolve().parents[2]

    if (repo_root / "pyproject.toml").is_file():
        return repo_root / DEFAULT_PROFILES_FILENAME

    return in_cwd


def _parse_env_block(
    name: str,
    raw: Any,
    errors: list[str],
) -> dict[str, str]:
    """Validate one profile's `env` block."""

    if raw is None:
        return {}

    if not isinstance(raw, dict):
        errors.append(f"profile '{name}': 'env' must be an object")
        return {}

    entries: dict[str, str] = {}

    for key, value in raw.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(key, str):
            errors.append(f"profile '{name}': env keys must be strings")
            continue

        if key in _FORBIDDEN_ENV_KEYS:
            errors.append(
                f"profile '{name}': env must not set {key}. "
                + (
                    "Use the 'tools' field instead."
                    if key == TOOL_GROUPS_ENV_VAR
                    else "A profile cannot select a profile."
                )
            )
            continue

        # Only BIONIC_* names are checked against the known list -- a caller may
        # legitimately want PYTHONPATH or similar, but a misspelled BIONIC_ name
        # would silently do nothing, which is the failure worth catching.
        if key.startswith("BIONIC_") and key not in FORWARDED_ENV_VARS:
            errors.append(
                f"profile '{name}': unknown setting {key}. "
                f"Known: {', '.join(sorted(FORWARDED_ENV_VARS))}"
            )
            continue

        if not isinstance(value, str):
            errors.append(f"profile '{name}': env value for {key} must be a string")
            continue

        entries[key] = value

    return entries


def parse_profiles(data: Any) -> tuple[dict[str, Profile], list[str]]:
    """Turn parsed JSON into profiles, collecting every problem found.

    Pure: takes already-decoded JSON so the whole table can be tested without
    touching the filesystem. Errors accumulate rather than raising on the first
    one, so a caller sees everything wrong with the file in one pass.

    Args:
        data: Decoded contents of the profiles file.

    Returns:
        (profiles, errors). Profiles that failed validation are omitted.
    """

    errors: list[str] = []

    if not isinstance(data, dict):
        return {}, ["profiles file must contain a JSON object"]

    document: dict[str, Any] = data

    version = document.get("version")

    if version is None:
        errors.append(f"missing 'version'. This build understands {SCHEMA_VERSION}")
    elif version != SCHEMA_VERSION:
        return {}, [
            f"unsupported 'version': {version!r}. "
            f"This build understands {SCHEMA_VERSION}"
        ]

    raw_profiles = document.get("profiles")

    if raw_profiles is None:
        errors.append("missing 'profiles' object")
        return {}, errors

    if not isinstance(raw_profiles, dict):
        return {}, [*errors, "'profiles' must be an object"]

    profiles: dict[str, Profile] = {}

    for name, raw in raw_profiles.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(name, str) or not name.strip():
            errors.append("profile names must be non-empty strings")
            continue

        if not isinstance(raw, dict):
            errors.append(f"profile '{name}' must be an object")
            continue

        entry: dict[str, Any] = raw

        tools = entry.get("tools")

        if not isinstance(tools, str) or not tools.strip():
            errors.append(
                f"profile '{name}': 'tools' must be a non-empty string, "
                f'e.g. "edit,build,docs"'
            )
            continue

        # The load-time validation that the plain environment variable cannot
        # do. parse_tool_groups already handles 'all', case, whitespace,
        # duplicates and the implicit 'core'.
        _groups, group_error = parse_tool_groups(tools)

        if group_error is not None:
            errors.append(f"profile '{name}': {group_error}")
            continue

        enable = entry.get("enable", True)

        if not isinstance(enable, bool):
            errors.append(f"profile '{name}': 'enable' must be true or false")
            continue

        description = entry.get("description", "")

        if not isinstance(description, str):
            errors.append(f"profile '{name}': 'description' must be a string")
            continue

        env = _parse_env_block(name, entry.get("env"), errors)

        profiles[name] = Profile(
            name=name,
            enable=enable,
            description=description,
            tools=tools,
            env=env,
        )

    return profiles, errors


def load_profiles(path: Path) -> tuple[dict[str, Profile], list[str]]:
    """Read and validate a profiles file.

    A missing file, an unreadable one and malformed JSON all come back as
    errors rather than exceptions, because the server calls this at startup and
    must not die over a config typo.

    Args:
        path: File to read.

    Returns:
        (profiles, errors).
    """

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, [
            f"no profiles file at {path}. "
            f"Create one with: python -m windows_agent_mcp.profiles --init"
        ]
    except OSError as exc:
        return {}, [f"could not read {path}: {exc}"]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, [f"{path} is not valid JSON: {exc}"]

    return parse_profiles(data)


def resolve_profile(
    name: str,
    profiles: dict[str, Profile],
) -> tuple[Profile | None, str | None]:
    """Look up a profile by name.

    "Disabled" and "not found" are reported distinctly because they need
    different fixes -- one is a flag to flip, the other a name to correct.

    Args:
        name: Profile name requested.
        profiles: Loaded profiles.

    Returns:
        (profile, error). Exactly one is non-None.
    """

    available = sorted(
        profile_name for profile_name, profile in profiles.items() if profile.enable
    )

    profile = profiles.get(name)

    if profile is None:
        return None, (
            f"no profile named '{name}'. "
            f"Available: {', '.join(available) if available else '(none enabled)'}"
        )

    if not profile.enable:
        return None, (
            f"profile '{name}' is disabled. Set \"enable\": true to use it. "
            f"Currently enabled: "
            f"{', '.join(available) if available else '(none)'}"
        )

    return profile, None


def apply_profile_to_environment(
    profile: Profile,
    environ: dict[str, str] | None = None,
) -> list[str]:
    """Apply a profile's settings, never overwriting what is already set.

    "More local wins": an explicitly set variable beats the file, so a profile
    named in a client config can be overridden for a single run without editing
    it. Overrides are returned rather than swallowed -- a silently ignored
    profile is exactly the confusion this feature exists to remove.

    Args:
        profile: Profile to apply.
        environ: Mapping to mutate. Defaults to os.environ.

    Returns:
        Names of variables left untouched because they were already set.
    """

    target = os.environ if environ is None else environ

    skipped: list[str] = []

    for key, value in ((TOOL_GROUPS_ENV_VAR, profile.tools), *profile.env.items()):
        if target.get(key, "").strip():
            skipped.append(key)
            continue

        target[key] = value

    return skipped


def scaffold() -> dict[str, Any]:
    """Return the content written by --init.

    Every profile carries a description because the file is not committed --
    there is no checked-in template to read, so the scaffold has to document
    itself.
    """

    return {
        "version": SCHEMA_VERSION,
        "profiles": {
            "full": {
                "enable": True,
                "description": "Everything except research. The default.",
                "tools": "edit,build,docs,net",
            },
            "cpp": {
                "enable": True,
                "description": "C++ / Vulkan development. 14 tools.",
                "tools": "edit,build,docs",
                # C: rather than any other drive letter: this placeholder is
                # written into the operator's file, and a letter that happens
                # to exist only on the authoring machine reads like a real
                # path rather than something to replace.
                "env": {"BIONIC_PROJECT_ROOTS": "C:/path/to/your/project"},
            },
            "review": {
                "enable": True,
                "description": ("Read and build, but NO write access. 12 tools."),
                "tools": "build,docs",
            },
            "explore": {
                "enable": True,
                "description": (
                    "Read-only: cannot write, execute or reach the network. 7 tools."
                ),
                "tools": "core",
            },
            "research": {
                "enable": False,
                "description": (
                    "Adds web_search AND lets fetch_web_page reach any public "
                    "host. Disabled by default because it widens the network "
                    "posture -- read the Web research section of README.md "
                    "before enabling."
                ),
                "tools": "core,docs,research",
            },
        },
    }


def _launch_command(python: str) -> tuple[str, list[str]]:
    """Decide how a client should start the server.

    Prefers the console script beside the interpreter, because that is one
    process rather than two. Both forms use an ABSOLUTE path: a bare
    "windows-agent-mcp" is not on a global PATH and fails in a real client.
    """

    interpreter = Path(python)

    script = interpreter.parent / "windows-agent-mcp.exe"

    if script.is_file():
        return str(script).replace("\\", "/"), []

    return str(interpreter).replace("\\", "/"), ["-m", "windows_agent_mcp"]


def emit_client_config(
    profiles: dict[str, Profile],
    python: str,
) -> dict[str, Any]:
    """Build MCP client config with one entry per ENABLED profile.

    Keyed by the profile name verbatim, so the client's server list reads
    "cpp", "review" rather than a prefixed variant. Rename a profile to rename
    the entry.

    Args:
        profiles: Loaded profiles.
        python: Interpreter to launch.

    Returns:
        A {"mcpServers": {...}} structure.
    """

    command, args = _launch_command(python)

    servers: dict[str, Any] = {}

    for name, profile in sorted(profiles.items()):
        if not profile.enable:
            continue

        entry: dict[str, Any] = {
            "command": command,
            "env": {TOOL_GROUPS_ENV_VAR: profile.tools, **profile.env},
        }

        if args:
            entry["args"] = args

        servers[name] = entry

    return {"mcpServers": servers}


def emit_inspector_config(profile: Profile, python: str) -> dict[str, Any]:
    """Build an Inspector config for one profile.

    Delegates the shape to inspector_config.build_config so the two cannot
    drift, and so the reason that file exists at all -- the Inspector not
    inheriting the environment -- stays documented in one place.
    """

    environment = {TOOL_GROUPS_ENV_VAR: profile.tools, **profile.env}

    return build_inspector_config(python, environment)


def _print_list(profiles: dict[str, Profile], path: Path) -> None:
    """Render --list, including the resolved tool count per profile."""

    # Imported here rather than at module scope: main imports every tool
    # module, and profiles is imported by main, so a top-level import is a
    # cycle.
    from .main import get_tools

    print(f"profiles file: {path}")
    print()

    if not profiles:
        print("no profiles defined")
        return

    print(f"{'profile':<12}{'state':<11}{'tools':>6}  groups")
    print("-" * 62)

    for name, profile in sorted(profiles.items()):
        groups, _error = parse_tool_groups(profile.tools)
        count = len(get_tools(groups=groups))
        state = "enabled" if profile.enable else "DISABLED"

        print(f"{name:<12}{state:<11}{count:>6}  {','.join(sorted(groups))}")

        if profile.description:
            print(f"              {profile.description}")


def _report(errors: list[str]) -> None:
    for error in errors:
        print(f"error: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Command line for inspecting and emitting profiles.

    Unlike the server, this exits non-zero on a bad file. It is interactive, so
    failing loudly is right; the server instead degrades to the default tool
    set, because an MCP client renders a startup crash as an opaque connection
    failure.

    Returns:
        0 on success, 1 on any error.
    """

    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m windows_agent_mcp.profiles",
        description="Manage named tool-group profiles.",
    )
    parser.add_argument(
        "--file",
        help=f"profiles file (default: {PROFILES_FILE_ENV_VAR}, else ./{DEFAULT_PROFILES_FILENAME})",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="write a starter profiles file",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --init, overwrite an existing file",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="show profiles, their state and tool counts",
    )
    parser.add_argument(
        "--emit",
        choices=("client", "inspector"),
        help="print MCP client config, or an Inspector config for one profile",
    )
    parser.add_argument(
        "--profile",
        help="profile name, required by --emit inspector",
    )

    options = parser.parse_args(argv)

    path = Path(options.file).expanduser() if options.file else find_profiles_file()

    if options.init:
        if path.exists() and not options.force:
            print(
                f"error: {path} already exists. Pass --force to overwrite.",
                file=sys.stderr,
            )
            return 1

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(scaffold(), indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write {path}: {exc}", file=sys.stderr)
            return 1

        print(f"wrote {path}")
        print("This file is gitignored: it holds machine-specific paths.")
        return 0

    if not (options.list or options.emit):
        parser.print_help()
        return 1

    profiles, errors = load_profiles(path)

    if errors:
        _report(errors)
        return 1

    if options.list:
        _print_list(profiles, path)
        return 0

    if options.emit == "client":
        print(json.dumps(emit_client_config(profiles, sys.executable), indent=2))
        return 0

    if not options.profile:
        print("error: --emit inspector requires --profile NAME", file=sys.stderr)
        return 1

    profile, error = resolve_profile(options.profile, profiles)

    if profile is None:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(json.dumps(emit_inspector_config(profile, sys.executable), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
