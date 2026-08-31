"""The server's configuration file: `input/config.json`.

Every setting this server reads is an environment variable, which is the only
channel some MCP clients offer. That works, but it means the available settings
are invisible: you have to already know which variables exist before you can
set one, and there is nowhere to look them up.

This module puts them in a file instead. It is generated on first run with the
complete table -- every setting, its default and what it does -- so the options
are discoverable by opening it. Named profiles live here too, so there is one
configuration file rather than two.

Two things that are load-bearing rather than incidental:

* **An environment variable still wins.** The file supplies values;
  anything explicitly set in the environment overrides it for that run. That
  keeps a client's `env` block working -- for some clients it is the only way
  to configure a server at all -- and it means a single run can be overridden
  without editing the file. Overrides are reported, never silent.
* **A broken file degrades, it does not stop the server.** Missing, malformed,
  unknown setting, bad profile name: all of them log, fall back to defaults and
  surface the reason in `get_server_info`. An MCP client renders a dead server
  as an opaque connection failure, so refusing to start is the worst available
  outcome.

Layering note. `utils` must not import this module (this module needs
`parse_tool_groups` from it), so the server does NOT consult the config deep in
`active_tool_groups()`. Instead `main.apply_configuration()` reads it first and
populates the environment, after which every existing path works unchanged.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

from .inspector_config import build_config as build_inspector_config
from .utils import (
    TOOL_GROUPS_ENV_VAR,
    parse_tool_groups,
)

__all__: list[str] = [
    "CONFIG_FILE_ENV_VAR",
    "DEFAULT_CONFIG_DIR",
    "DEFAULT_CONFIG_FILENAME",
    "SCHEMA_VERSION",
    "SETTINGS",
    "Config",
    "Profile",
    "apply_to_environment",
    "emit_client_config",
    "emit_inspector_config",
    "find_config_file",
    "load_config",
    "main",
    "parse_config",
    "resolve_profile",
    "scaffold",
    "write_scaffold",
]

# The directory is part of the name on purpose: `input/` says "things you give
# the server", which is what distinguishes it from everything else at the root.
DEFAULT_CONFIG_DIR = "input"
DEFAULT_CONFIG_FILENAME = "config.json"

# Points at a config file elsewhere. Deliberately NOT one of the settings in
# the file: a setting naming the file it lives in cannot be read before the
# file is found.
CONFIG_FILE_ENV_VAR = "WAMCP_CONFIG_FILE"

# Bumped only for a breaking format change. An unrecognised version is an error
# rather than a best-effort read, so a future format cannot be misinterpreted
# as valid by an older build.
SCHEMA_VERSION = 1


class Setting(NamedTuple):
    """One configurable value, as it appears in the generated file.

    Attributes:
        default: What happens when the value is empty. Prose, not a value --
            "the download sandbox only" is more useful than "".
        description: What it does, written for someone who has never seen the
            server. This is the entire reason the file is generated rather than
            left for the operator to invent.
    """

    default: str
    description: str


# Every setting, in the order they appear in the generated file: the ones you
# are most likely to need first.
#
# This table is the single source for the scaffold AND for validating an
# unknown key, so a setting cannot exist in one and not the other.
#
# WAMCP_PROFILE and WAMCP_CONFIG_FILE are absent deliberately. The first is
# expressed as `active_profile` below; the second cannot be read from the file
# it identifies.
SETTINGS: dict[str, Setting] = {
    "WAMCP_PROJECT_ROOTS": Setting(
        default="the download sandbox only",
        description=(
            "Directories the model may WRITE to and RUN builds in, separated "
            "by ';'. This is the consent switch for touching your project: "
            "leave it empty and the server cannot modify source code anywhere."
        ),
    ),
    "WAMCP_TOOLS": Setting(
        default="edit,build,docs,net (everything except search and research)",
        description=(
            "Tool groups to register: core, edit, build, docs, net, search, "
            "research -- or 'all'. core is always included. Fewer tools leaves "
            "more context for the model's actual work."
        ),
    ),
    "WAMCP_DOWNLOAD_ROOT": Setting(
        default="%LOCALAPPDATA%\\windows-agent-mcp\\downloads",
        description="Directory downloads are written to. Always writable.",
    ),
    "WAMCP_EXTRA_DOC_HOSTS": Setting(
        default="the built-in documentation hosts only",
        description=(
            "Extra hostnames fetch_web_page may READ, ',' or ';' separated. "
            "Read-only: download_file is unaffected. Exact hostnames, no "
            "wildcards."
        ),
    ),
    "WAMCP_ALLOWED_HOSTS_FILE": Setting(
        default="./mcp-allowed-hosts.json",
        description=(
            "Where granted hosts are stored. Managed with: python -m "
            "windows_agent_mcp.hostgrants --add HOST"
        ),
    ),
    "WAMCP_HOST_CONSENT": Setting(
        default="1 (the server may ask you to approve a host)",
        description=(
            "Set to 0 to stop the server ever prompting. The grants file "
            "remains the way to allow a host."
        ),
    ),
    "WAMCP_HOST_GRANT_PERSIST": Setting(
        default="unset (an approval lasts until the server restarts)",
        description=(
            "Set to 1 to let an approval be written back to the grants file. "
            "Off by default because the MCP spec permits a client to answer an "
            "elicitation itself, so an approval is not proof a human saw it."
        ),
    ),
    "WAMCP_WEB_RESEARCH": Setting(
        default="unset (documentation and granted hosts only)",
        description=(
            "Set to 1 to register web_search AND let fetch_web_page reach any "
            "public host. Read the Web research section of README.md first; "
            "usually you want the 'search' tool group plus a host grant."
        ),
    ),
    "WAMCP_SEARCH_BACKEND": Setting(
        default="duckduckgo",
        description=(
            "Search provider. Only 'duckduckgo' is implemented; an "
            "unrecognised value is an error rather than a silent fallback."
        ),
    ),
}


class Profile(NamedTuple):
    """One named combination of tool groups.

    Attributes:
        name: Key from the profiles section.
        enable: False parks the profile: it is not emitted to client config,
            and selecting it by name is refused. Kept rather than deleted so
            the reason it existed is not lost with it.
        description: Free text, shown by --list.
        tools: A WAMCP_TOOLS string, already validated.
        settings: Extra settings to apply when this profile is active.
    """

    name: str
    enable: bool
    description: str
    tools: str
    settings: dict[str, str]


class Config(NamedTuple):
    """The whole file, parsed.

    Attributes:
        settings: Setting name -> value, empty values already dropped.
        active_profile: Profile to apply, or "" for none.
        profiles: Named profiles by name.
    """

    settings: dict[str, str]
    active_profile: str
    profiles: dict[str, Profile]


EMPTY_CONFIG = Config(settings={}, active_profile="", profiles={})


def find_config_file() -> Path:
    """Locate the config file.

    Order, most explicit first:

    1. WAMCP_CONFIG_FILE.
    2. `input/config.json` under the current directory. The launcher pushd's
       to the repository root, so this is the normal case.
    3. The repository root inferred from this file's location, but only for an
       editable install -- confirmed by pyproject.toml being there. In a
       site-packages install that path is meaningless.

    Returns:
        The path to use. May not exist; callers handle that.
    """

    configured = os.environ.get(CONFIG_FILE_ENV_VAR, "").strip()

    if configured:
        return Path(configured).expanduser()

    in_cwd = Path.cwd() / DEFAULT_CONFIG_DIR / DEFAULT_CONFIG_FILENAME

    if in_cwd.is_file():
        return in_cwd

    # src/windows_agent_mcp/config.py -> repository root
    repo_root = Path(__file__).resolve().parents[2]

    if (repo_root / "pyproject.toml").is_file():
        return repo_root / DEFAULT_CONFIG_DIR / DEFAULT_CONFIG_FILENAME

    return in_cwd


def _parse_settings_block(
    label: str,
    raw: Any,
    errors: list[str],
) -> dict[str, str]:
    """Validate a settings block, from the top level or from a profile.

    Accepts both the documented form -- an object carrying `value` alongside
    the generated `default` and `description` -- and a plain string, because an
    operator editing the file by hand will reasonably write one.

    An empty value means "not set" and is dropped here, so callers never have
    to distinguish "" from absent.
    """

    if raw is None:
        return {}

    if not isinstance(raw, dict):
        errors.append(f"{label}: 'settings' must be an object")
        return {}

    entries: dict[str, str] = {}

    for key, item in raw.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(key, str):
            errors.append(f"{label}: setting names must be strings")
            continue

        if key == TOOL_GROUPS_ENV_VAR and label != "settings":
            errors.append(
                f"{label}: use the 'tools' field rather than "
                f"{TOOL_GROUPS_ENV_VAR} in a profile's settings"
            )
            continue

        if key not in SETTINGS:
            errors.append(
                f"{label}: unknown setting {key}. Known: {', '.join(sorted(SETTINGS))}"
            )
            continue

        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            block: dict[str, Any] = item
            raw_value = block.get("value", "")

            if not isinstance(raw_value, str):
                errors.append(f"{label}: '{key}' value must be a string")
                continue

            value = raw_value
        else:
            errors.append(
                f"{label}: '{key}' must be a string, or an object with a 'value' field"
            )
            continue

        if value.strip():
            entries[key] = value

    return entries


def _parse_profiles_block(raw: Any, errors: list[str]) -> dict[str, Profile]:
    """Validate the profiles section."""

    if raw is None:
        return {}

    if not isinstance(raw, dict):
        errors.append("'profiles' must be an object")
        return {}

    profiles: dict[str, Profile] = {}

    for name, entry in raw.items():  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(name, str) or not name.strip():
            errors.append("profile names must be non-empty strings")
            continue

        if not isinstance(entry, dict):
            errors.append(f"profile '{name}' must be an object")
            continue

        body: dict[str, Any] = entry

        tools = body.get("tools")

        if not isinstance(tools, str) or not tools.strip():
            errors.append(
                f"profile '{name}': 'tools' must be a non-empty string, "
                f'e.g. "edit,build,docs"'
            )
            continue

        # The load-time validation a bare environment variable cannot do: a
        # wrong group name is named here, with the valid ones listed, rather
        # than silently yielding the default set at startup.
        _groups, group_error = parse_tool_groups(tools)

        if group_error is not None:
            errors.append(f"profile '{name}': {group_error}")
            continue

        enable = body.get("enable", True)

        if not isinstance(enable, bool):
            errors.append(f"profile '{name}': 'enable' must be true or false")
            continue

        description = body.get("description", "")

        if not isinstance(description, str):
            errors.append(f"profile '{name}': 'description' must be a string")
            continue

        profiles[name] = Profile(
            name=name,
            enable=enable,
            description=description,
            tools=tools,
            settings=_parse_settings_block(
                f"profile '{name}'", body.get("settings"), errors
            ),
        )

    return profiles


def parse_config(data: Any) -> tuple[Config, list[str]]:
    """Turn decoded JSON into a Config, collecting every problem found.

    Pure: takes already-decoded JSON so the whole table is testable without
    touching the filesystem. Errors accumulate rather than raising on the first
    one, so a caller sees everything wrong with the file in one pass.

    Args:
        data: Decoded contents of the config file.

    Returns:
        (config, errors). Entries that failed validation are omitted; the rest
        still load.
    """

    errors: list[str] = []

    if not isinstance(data, dict):
        return EMPTY_CONFIG, ["config file must contain a JSON object"]

    document: dict[str, Any] = data

    version = document.get("version")

    if version is None:
        errors.append(f"missing 'version'. This build understands {SCHEMA_VERSION}")
    elif version != SCHEMA_VERSION:
        return EMPTY_CONFIG, [
            f"unsupported 'version': {version!r}. "
            f"This build understands {SCHEMA_VERSION}"
        ]

    active = document.get("active_profile", "")

    if not isinstance(active, str):
        errors.append("'active_profile' must be a string")
        active = ""

    return (
        Config(
            settings=_parse_settings_block(
                "settings", document.get("settings"), errors
            ),
            active_profile=active.strip(),
            profiles=_parse_profiles_block(document.get("profiles"), errors),
        ),
        errors,
    )


def load_config(path: Path) -> tuple[Config, list[str]]:
    """Read and validate the config file.

    A missing file is NOT an error: the caller generates one. Every other
    problem comes back as a message rather than an exception, because the
    server calls this at startup and must not die over a typo.

    Args:
        path: File to read.

    Returns:
        (config, errors).
    """

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EMPTY_CONFIG, []
    except OSError as exc:
        return EMPTY_CONFIG, [f"could not read {path}: {exc}"]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return EMPTY_CONFIG, [f"{path} is not valid JSON: {exc}"]

    return parse_config(data)


def scaffold() -> dict[str, Any]:
    """Build the file written on first run.

    Every setting appears with its default and description, because the file is
    not committed -- there is no checked-in template to read, so the generated
    copy has to document itself. That is the whole point of generating it: the
    settings become discoverable by opening the file.

    Returns:
        JSON-serialisable contents.
    """

    return {
        "version": SCHEMA_VERSION,
        "comment": (
            "Settings for windows-agent-mcp. Fill in 'value' to change one; an "
            "empty value means the default shown beside it. An environment "
            "variable of the same name overrides anything here, so a client's "
            "env block still wins."
        ),
        "settings": {
            name: {
                "value": "",
                "default": setting.default,
                "description": setting.description,
            }
            for name, setting in SETTINGS.items()
        },
        "active_profile": "",
        "profiles": {
            "cpp": {
                "enable": True,
                "description": "C++ / Vulkan development.",
                "tools": "edit,build,docs",
                # C: rather than any other drive letter: this placeholder is
                # written into the operator's own file, and a letter that
                # exists only on the authoring machine reads like a real path
                # rather than something to replace.
                "settings": {"WAMCP_PROJECT_ROOTS": "C:/path/to/your/project"},
            },
            "review": {
                "enable": True,
                "description": "Read and build, but NO write access.",
                "tools": "build,docs",
            },
            "explore": {
                "enable": True,
                "description": (
                    "Read-only: cannot write, execute or reach the network."
                ),
                "tools": "core",
            },
            "research": {
                "enable": False,
                "description": (
                    "Adds web_search AND lets fetch_web_page reach any public "
                    "host. Disabled by default because it widens the network "
                    "posture -- read the Web research section of README.md."
                ),
                "tools": "core,docs,research",
            },
        },
    }


def write_scaffold(path: Path) -> str | None:
    """Write the generated config file, creating its directory.

    Atomic: content goes to a temporary file in the same directory and is
    renamed into place, so an interrupted first run cannot leave a truncated
    config that the next run then reports as malformed.

    Args:
        path: Where to write.

    Returns:
        None on success, or a message explaining why nothing was written.
    """

    payload = json.dumps(scaffold(), indent=2) + "\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        handle, temporary = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
    except OSError as exc:
        return f"could not create {path}: {exc}"

    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)

        os.replace(temporary, path)
    except OSError as exc:
        return f"could not write {path}: {exc}"
    except BaseException:
        # Includes KeyboardInterrupt: a stray .tmp beside the config is
        # confusing at exactly the wrong moment.
        Path(temporary).unlink(missing_ok=True)
        raise

    return None


def apply_to_environment(
    settings: dict[str, str],
    environ: dict[str, str] | None = None,
) -> list[str]:
    """Apply settings, never overwriting what is already set.

    "More local wins": an explicitly set variable beats the file, so a client's
    env block or a one-off `set` overrides the config without editing it.
    Overrides are returned rather than swallowed -- a silently ignored setting
    is exactly the confusion this file exists to remove.

    Args:
        settings: Setting name -> value.
        environ: Mapping to mutate. Defaults to os.environ.

    Returns:
        Names left untouched because they were already set.
    """

    target = os.environ if environ is None else environ

    skipped: list[str] = []

    for key, value in settings.items():
        if target.get(key, "").strip():
            skipped.append(key)
            continue

        target[key] = value

    return skipped


def resolve_profile(
    name: str,
    profiles: dict[str, Profile],
) -> tuple[Profile | None, str | None]:
    """Look up a profile by name.

    "Disabled" and "not found" are reported distinctly because they need
    different fixes -- one is a flag to flip, the other a name to correct --
    and only ENABLED profiles are offered as alternatives.

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
            f"Currently enabled: {', '.join(available) if available else '(none)'}"
        )

    return profile, None


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


def emit_client_config(config: Config, python: str) -> dict[str, Any]:
    """Build MCP client config with one entry per ENABLED profile.

    Keyed by the profile name verbatim, so the client's server list reads
    "cpp", "review" rather than a prefixed variant.

    The base settings are included in every entry: a client launches the server
    with a fixed environment, so anything only in the file would be picked up
    anyway -- but emitting it means the client config is self-describing and
    keeps working if the file moves.

    Args:
        config: Loaded configuration.
        python: Interpreter to launch.

    Returns:
        A {"mcpServers": {...}} structure.
    """

    command, args = _launch_command(python)

    servers: dict[str, Any] = {}

    for name, profile in sorted(config.profiles.items()):
        if not profile.enable:
            continue

        entry: dict[str, Any] = {
            "command": command,
            "env": {
                **config.settings,
                TOOL_GROUPS_ENV_VAR: profile.tools,
                **profile.settings,
            },
        }

        if args:
            entry["args"] = args

        servers[name] = entry

    return {"mcpServers": servers}


def emit_inspector_config(
    config: Config,
    profile: Profile,
    python: str,
) -> dict[str, Any]:
    """Build an Inspector config for one profile.

    Delegates the shape to inspector_config.build_config so the two cannot
    drift, and so the reason that module exists at all -- the Inspector not
    inheriting the environment -- stays documented in one place.
    """

    environment = {
        **config.settings,
        TOOL_GROUPS_ENV_VAR: profile.tools,
        **profile.settings,
    }

    return build_inspector_config(python, environment)


def _print_list(config: Config, path: Path) -> None:
    """Print the settings and profiles tables."""

    print(f"config file: {path}")

    if not path.is_file():
        print("(no file yet; it is generated the first time the server starts)")

    print()
    print("settings")
    print("-" * 70)

    for name, setting in SETTINGS.items():
        value = config.settings.get(name, "")
        shown = value if value else f"(default: {setting.default})"
        print(f"  {name:28} {shown}")

    if not config.profiles:
        print()
        print("no profiles defined")
        return

    # Imported lazily: main imports every tool module, and profiles are only
    # counted for this listing.
    from .main import get_tools

    print()
    print("profiles")
    print("-" * 70)

    for name, profile in sorted(config.profiles.items()):
        groups, _error = parse_tool_groups(profile.tools)
        state = "enabled" if profile.enable else "DISABLED"
        active = "  <- active" if name == config.active_profile else ""

        print(
            f"  {name:12} {state:10} {len(get_tools(groups=groups)):3} tools  "
            f"{','.join(sorted(groups))}{active}"
        )

        if profile.description:
            print(f"               {profile.description}")


def _report(errors: list[str]) -> None:
    for error in errors:
        print(f"error: {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Operator CLI for the config file.

    Args:
        argv: Arguments, defaulting to sys.argv[1:].

    Returns:
        Process exit code.
    """

    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m windows_agent_mcp.config",
        description="Inspect and generate the server's configuration file.",
    )
    parser.add_argument("--file", help="config file to use")
    parser.add_argument(
        "--init",
        action="store_true",
        help="write the config file now, rather than waiting for the first run",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --init, overwrite an existing file",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="show the settings and profiles (the default action)",
    )
    parser.add_argument(
        "--emit",
        choices=["client", "inspector"],
        help="print configuration for an MCP client, or for the Inspector",
    )
    parser.add_argument(
        "--profile",
        help="with --emit inspector, the profile to emit",
    )

    args = parser.parse_args(argv)

    if args.file:
        os.environ[CONFIG_FILE_ENV_VAR] = args.file

    path = find_config_file()

    if args.init:
        if path.exists() and not args.force:
            print(
                f"error: {path} already exists. Use --force to overwrite.",
                file=sys.stderr,
            )
            return 1

        error = write_scaffold(path)

        if error is not None:
            print(f"error: {error}", file=sys.stderr)
            return 1

        print(f"wrote {path}")

    config, errors = load_config(path)

    _report(errors)

    if args.emit:
        if errors:
            return 1

        python = sys.executable

        if args.emit == "client":
            print(json.dumps(emit_client_config(config, python), indent=2))
            return 0

        if not args.profile:
            print("error: --emit inspector requires --profile", file=sys.stderr)
            return 1

        profile, error = resolve_profile(args.profile, config.profiles)

        if profile is None:
            print(f"error: {error}", file=sys.stderr)
            return 1

        print(json.dumps(emit_inspector_config(config, profile, python), indent=2))
        return 0

    if args.list or not args.init:
        _print_list(config, path)

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
