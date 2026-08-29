"""Tests for named tool-group profiles.

Every test passes an explicit path or uses tmp_path. None may read a real
mcp-profiles.json from the repository: that file is machine-specific and
gitignored, so a test depending on it would pass or fail based on whether the
developer happened to run --init.

The property worth protecting hardest is load-time validation. A profile saying
`"tools": "core,cpp"` is the natural mistake -- `cpp` sounds like a group and is
not one -- and passed through as a bare environment variable it would merely
warn at startup and silently register the default 17 tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from windows_agent_mcp import main as main_module
from windows_agent_mcp.main import apply_selected_profile, get_tools
from windows_agent_mcp.profiles import (
    DEFAULT_PROFILES_FILENAME,
    PROFILES_FILE_ENV_VAR,
    SCHEMA_VERSION,
    Profile,
    apply_profile_to_environment,
    emit_client_config,
    emit_inspector_config,
    find_profiles_file,
    load_profiles,
    main,
    parse_profiles,
    resolve_profile,
    scaffold,
)
from windows_agent_mcp.tools.get_server_info import get_server_info
from windows_agent_mcp.utils import VALID_TOOL_GROUPS, parse_tool_groups


def document(**profiles: object) -> dict[str, object]:
    return {"version": SCHEMA_VERSION, "profiles": profiles}


@pytest.fixture
def profiles_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Write a profiles file and point the server at it."""

    def write(data: object) -> Path:
        path = tmp_path / DEFAULT_PROFILES_FILENAME
        text = data if isinstance(data, str) else json.dumps(data)
        path.write_text(text, encoding="utf-8")
        monkeypatch.setenv(PROFILES_FILE_ENV_VAR, str(path))
        return path

    return write


# ============================================================
# The scaffold must be valid
# ============================================================


def test_scaffold_parses_without_error() -> None:
    """--init writing an invalid file would be an unrecoverable first run."""

    profiles, errors = parse_profiles(scaffold())

    assert errors == []
    assert set(profiles) == {"full", "cpp", "review", "explore", "research"}


def test_scaffold_profiles_resolve_to_the_documented_counts() -> None:
    profiles, _errors = parse_profiles(scaffold())

    expected = {"explore": 7, "review": 12, "cpp": 14, "full": 17, "research": 9}

    for name, count in expected.items():
        groups, _error = parse_tool_groups(profiles[name].tools)
        assert len(get_tools(groups=groups)) == count, name


def test_research_is_scaffolded_disabled() -> None:
    """It widens the network posture, so it must be opt-in even here."""

    profiles, _errors = parse_profiles(scaffold())

    assert profiles["research"].enable is False
    assert profiles["cpp"].enable is True


def test_every_scaffolded_profile_is_described() -> None:
    """Nothing is committed, so the scaffold is the only documentation."""

    profiles, _errors = parse_profiles(scaffold())

    for name, profile in profiles.items():
        assert profile.description.strip(), name


# ============================================================
# parse_profiles
# ============================================================


def test_valid_profile_is_parsed() -> None:
    profiles, errors = parse_profiles(
        document(
            cpp={
                "enable": True,
                "description": "C++ work",
                "tools": "edit,build,docs",
                "env": {"BIONIC_PROJECT_ROOTS": "S:/game"},
            }
        )
    )

    assert errors == []
    assert profiles["cpp"] == Profile(
        name="cpp",
        enable=True,
        description="C++ work",
        tools="edit,build,docs",
        env={"BIONIC_PROJECT_ROOTS": "S:/game"},
    )


def test_enable_defaults_to_true() -> None:
    """A profile you bothered to write is presumably wanted."""

    profiles, errors = parse_profiles(document(cpp={"tools": "edit"}))

    assert errors == []
    assert profiles["cpp"].enable is True


def test_a_bad_group_name_is_caught_at_load() -> None:
    """The whole point: `cpp` is not a group, and the bare env var would not say so."""

    profiles, errors = parse_profiles(document(cpp={"tools": "core,cpp"}))

    assert profiles == {}
    assert len(errors) == 1
    assert "cpp" in errors[0]

    # The message must name the real groups or it is not actionable.
    for group in VALID_TOOL_GROUPS:
        assert group in errors[0]


def test_missing_version_is_reported_but_tolerated() -> None:
    profiles, errors = parse_profiles({"profiles": {"cpp": {"tools": "edit"}}})

    assert "version" in errors[0]
    assert "cpp" in profiles


def test_unsupported_version_is_fatal() -> None:
    """An older build must not best-effort read a future format."""

    profiles, errors = parse_profiles({"version": 99, "profiles": {}})

    assert profiles == {}
    assert "99" in errors[0]
    assert str(SCHEMA_VERSION) in errors[0]


@pytest.mark.parametrize(
    "data",
    ["not an object", 42, ["a", "list"], None],
)
def test_non_object_document_is_rejected(data: object) -> None:
    profiles, errors = parse_profiles(data)

    assert profiles == {}
    assert errors


def test_missing_profiles_key() -> None:
    _profiles, errors = parse_profiles({"version": SCHEMA_VERSION})

    assert any("profiles" in error for error in errors)


@pytest.mark.parametrize("tools", [None, "", "   ", 42, ["edit"]])
def test_tools_must_be_a_non_empty_string(tools: object) -> None:
    profiles, errors = parse_profiles(document(cpp={"tools": tools}))

    assert profiles == {}
    assert "tools" in errors[0]


def test_enable_must_be_boolean() -> None:
    profiles, errors = parse_profiles(document(cpp={"tools": "edit", "enable": "yes"}))

    assert profiles == {}
    assert "enable" in errors[0]


def test_errors_accumulate_across_profiles() -> None:
    """One pass should report everything wrong, not just the first problem."""

    _profiles, errors = parse_profiles(
        document(
            a={"tools": "nonsense"},
            b={"tools": "alsonotagroup"},
        )
    )

    assert len(errors) == 2


def test_a_good_profile_survives_a_bad_sibling() -> None:
    profiles, errors = parse_profiles(
        document(good={"tools": "edit"}, bad={"tools": "nope"})
    )

    assert set(profiles) == {"good"}
    assert errors


# ============================================================
# The env block
# ============================================================


def test_env_may_not_set_bionic_tools() -> None:
    """Two sources for one setting is ambiguous, not convenient."""

    _profiles, errors = parse_profiles(
        document(cpp={"tools": "edit", "env": {"BIONIC_TOOLS": "core"}})
    )

    assert any("BIONIC_TOOLS" in error for error in errors)
    assert any("'tools' field" in error for error in errors)


def test_env_may_not_set_bionic_profile() -> None:
    """A profile selecting a profile is circular."""

    _profiles, errors = parse_profiles(
        document(cpp={"tools": "edit", "env": {"BIONIC_PROFILE": "other"}})
    )

    assert any("cannot select a profile" in error for error in errors)


def test_an_unknown_bionic_setting_is_rejected() -> None:
    """A misspelled BIONIC_ name would silently do nothing."""

    _profiles, errors = parse_profiles(
        document(cpp={"tools": "edit", "env": {"BIONIC_PROJECT_ROOT": "S:/x"}})
    )

    assert any("BIONIC_PROJECT_ROOT" in error for error in errors)


def test_a_non_bionic_variable_passes_through() -> None:
    """Someone may legitimately want PYTHONPATH; only our own names are policed."""

    profiles, errors = parse_profiles(
        document(cpp={"tools": "edit", "env": {"PYTHONPATH": "S:/libs"}})
    )

    assert errors == []
    assert profiles["cpp"].env == {"PYTHONPATH": "S:/libs"}


@pytest.mark.parametrize("env", ["a string", 42, ["a", "list"]])
def test_env_must_be_an_object(env: object) -> None:
    _profiles, errors = parse_profiles(document(cpp={"tools": "edit", "env": env}))

    assert any("'env' must be an object" in error for error in errors)


def test_env_values_must_be_strings() -> None:
    _profiles, errors = parse_profiles(
        document(cpp={"tools": "edit", "env": {"BIONIC_PROJECT_ROOTS": 42}})
    )

    assert any("must be a string" in error for error in errors)


def test_missing_env_block_is_fine() -> None:
    profiles, errors = parse_profiles(document(cpp={"tools": "edit"}))

    assert errors == []
    assert profiles["cpp"].env == {}


# ============================================================
# load_profiles
# ============================================================


def test_missing_file_reports_the_init_command(tmp_path) -> None:
    _profiles, errors = load_profiles(tmp_path / "nope.json")

    assert "--init" in errors[0]


def test_malformed_json_is_reported_not_raised(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{ this is not json", encoding="utf-8")

    profiles, errors = load_profiles(path)

    assert profiles == {}
    assert "not valid JSON" in errors[0]


def test_unreadable_file_is_reported(tmp_path, monkeypatch) -> None:
    path = tmp_path / "locked.json"
    path.write_text("{}", encoding="utf-8")

    def denied(self, *args: object, **kwargs: object):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "read_text", denied)

    _profiles, errors = load_profiles(path)

    assert "could not read" in errors[0]


def test_load_round_trips_the_scaffold(tmp_path) -> None:
    path = tmp_path / DEFAULT_PROFILES_FILENAME
    path.write_text(json.dumps(scaffold()), encoding="utf-8")

    profiles, errors = load_profiles(path)

    assert errors == []
    assert "cpp" in profiles


# ============================================================
# find_profiles_file
# ============================================================


def test_env_var_wins(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(PROFILES_FILE_ENV_VAR, str(tmp_path / "custom.json"))

    assert find_profiles_file() == tmp_path / "custom.json"


def test_falls_back_to_the_current_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / DEFAULT_PROFILES_FILENAME).write_text("{}", encoding="utf-8")

    assert find_profiles_file() == tmp_path / DEFAULT_PROFILES_FILENAME


def test_falls_back_to_the_repository_root(tmp_path, monkeypatch) -> None:
    """Covers an editable install whose client set a different cwd."""

    monkeypatch.chdir(tmp_path)

    found = find_profiles_file()

    assert found.name == DEFAULT_PROFILES_FILENAME
    assert (found.parent / "pyproject.toml").is_file()


# ============================================================
# resolve_profile
# ============================================================


def test_unknown_name_lists_the_enabled_profiles() -> None:
    profiles, _errors = parse_profiles(
        document(cpp={"tools": "edit"}, off={"tools": "core", "enable": False})
    )

    profile, error = resolve_profile("nonesuch", profiles)

    assert profile is None
    assert error is not None
    assert "cpp" in error
    # A disabled profile is not something you can select, so do not offer it.
    assert "off" not in error


def test_disabled_says_disabled_not_missing() -> None:
    """Different problems, different fixes: a flag to flip vs a name to correct."""

    profiles, _errors = parse_profiles(
        document(research={"tools": "core,research", "enable": False})
    )

    profile, error = resolve_profile("research", profiles)

    assert profile is None
    assert error is not None
    assert "disabled" in error
    assert "enable" in error


def test_enabled_profile_resolves() -> None:
    profiles, _errors = parse_profiles(document(cpp={"tools": "edit,build"}))

    profile, error = resolve_profile("cpp", profiles)

    assert error is None
    assert profile is not None
    assert profile.tools == "edit,build"


def test_no_enabled_profiles_at_all() -> None:
    profiles, _errors = parse_profiles(document(a={"tools": "core", "enable": False}))

    _profile, error = resolve_profile("b", profiles)

    assert error is not None
    assert "none enabled" in error


# ============================================================
# apply_profile_to_environment
# ============================================================


def test_apply_sets_tools_and_env() -> None:
    profile = Profile("cpp", True, "", "edit,build", {"BIONIC_PROJECT_ROOTS": "S:/g"})

    environ: dict[str, str] = {}

    assert apply_profile_to_environment(profile, environ) == []
    assert environ == {
        "BIONIC_TOOLS": "edit,build",
        "BIONIC_PROJECT_ROOTS": "S:/g",
    }


def test_apply_never_overwrites_an_existing_value() -> None:
    """ "More local wins": an explicit variable beats the file."""

    profile = Profile("cpp", True, "", "edit,build", {})

    environ = {"BIONIC_TOOLS": "core"}

    skipped = apply_profile_to_environment(profile, environ)

    assert skipped == ["BIONIC_TOOLS"]
    assert environ["BIONIC_TOOLS"] == "core"


def test_apply_treats_a_blank_value_as_unset() -> None:
    profile = Profile("cpp", True, "", "edit", {})

    environ = {"BIONIC_TOOLS": "   "}

    assert apply_profile_to_environment(profile, environ) == []
    assert environ["BIONIC_TOOLS"] == "edit"


def test_apply_reports_every_skipped_variable() -> None:
    profile = Profile("cpp", True, "", "edit", {"BIONIC_PROJECT_ROOTS": "S:/g"})

    environ = {"BIONIC_TOOLS": "core", "BIONIC_PROJECT_ROOTS": "C:/other"}

    assert set(apply_profile_to_environment(profile, environ)) == {
        "BIONIC_TOOLS",
        "BIONIC_PROJECT_ROOTS",
    }


# ============================================================
# Emission
# ============================================================


def test_client_config_omits_disabled_profiles() -> None:
    profiles, _errors = parse_profiles(
        document(
            cpp={"tools": "edit,build"},
            research={"tools": "core,research", "enable": False},
        )
    )

    config = emit_client_config(profiles, "C:/py/Scripts/python.exe")

    assert set(config["mcpServers"]) == {"cpp"}


def test_client_config_keys_are_the_profile_names() -> None:
    profiles, _errors = parse_profiles(document(cpp={"tools": "edit"}))

    config = emit_client_config(profiles, "C:/py/Scripts/python.exe")

    assert list(config["mcpServers"]) == ["cpp"]
    assert config["mcpServers"]["cpp"]["env"]["BIONIC_TOOLS"] == "edit"


def test_client_config_carries_profile_env() -> None:
    profiles, _errors = parse_profiles(
        document(cpp={"tools": "edit", "env": {"BIONIC_PROJECT_ROOTS": "S:/g"}})
    )

    entry = emit_client_config(profiles, "C:/py/python.exe")["mcpServers"]["cpp"]

    assert entry["env"]["BIONIC_PROJECT_ROOTS"] == "S:/g"


def test_client_config_command_is_absolute(tmp_path) -> None:
    """A bare "windows-agent-mcp" is not on a global PATH and fails in a client."""

    profiles, _errors = parse_profiles(document(cpp={"tools": "edit"}))

    interpreter = tmp_path / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)

    entry = emit_client_config(profiles, str(interpreter))["mcpServers"]["cpp"]

    assert Path(entry["command"]).is_absolute()
    assert entry["args"] == ["-m", "windows_agent_mcp"]


def test_client_config_prefers_the_console_script(tmp_path) -> None:
    """One process rather than two, when the script is installed."""

    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "windows-agent-mcp.exe").write_bytes(b"stub")
    interpreter = scripts / "python.exe"

    profiles, _errors = parse_profiles(document(cpp={"tools": "edit"}))

    entry = emit_client_config(profiles, str(interpreter))["mcpServers"]["cpp"]

    assert entry["command"].endswith("windows-agent-mcp.exe")
    assert "args" not in entry


def test_client_config_uses_forward_slashes() -> None:
    profiles, _errors = parse_profiles(document(cpp={"tools": "edit"}))

    config = emit_client_config(profiles, r"C:\py\python.exe")

    assert "\\" not in config["mcpServers"]["cpp"]["command"]


def test_client_config_is_valid_json() -> None:
    profiles, _errors = parse_profiles(scaffold())

    reloaded = json.loads(json.dumps(emit_client_config(profiles, "python")))

    assert "research" not in reloaded["mcpServers"]


def test_inspector_config_delegates_to_the_shared_builder() -> None:
    """The two shapes must not drift; only one of them documents the reason."""

    from windows_agent_mcp.inspector_config import SERVER_NAME

    profile = Profile("cpp", True, "", "edit,build", {"BIONIC_PROJECT_ROOTS": "S:/g"})

    config = emit_inspector_config(profile, "C:/py/python.exe")

    entry = config["mcpServers"][SERVER_NAME]

    assert entry["env"]["BIONIC_TOOLS"] == "edit,build"
    assert entry["env"]["BIONIC_PROJECT_ROOTS"] == "S:/g"


# ============================================================
# Server integration
# ============================================================


def test_profile_selects_the_tool_set(profiles_file, monkeypatch) -> None:
    profiles_file(document(cpp={"tools": "edit,build,docs"}))
    monkeypatch.setenv("BIONIC_PROFILE", "cpp")

    assert apply_selected_profile() is None

    from windows_agent_mcp.utils import active_tool_groups

    groups, _error = active_tool_groups()

    assert len(get_tools(groups=groups)) == 14


def test_explicit_tools_override_a_profile(profiles_file, monkeypatch) -> None:
    profiles_file(document(cpp={"tools": "edit,build,docs"}))
    monkeypatch.setenv("BIONIC_PROFILE", "cpp")
    monkeypatch.setenv("BIONIC_TOOLS", "core")

    message = apply_selected_profile()

    assert message is not None
    assert "already set" in message
    assert "BIONIC_TOOLS" in message


def test_no_profile_requested_is_a_no_op() -> None:
    assert apply_selected_profile() is None


def test_a_disabled_profile_degrades_with_a_message(profiles_file, monkeypatch) -> None:
    profiles_file(document(research={"tools": "core,research", "enable": False}))
    monkeypatch.setenv("BIONIC_PROFILE", "research")

    message = apply_selected_profile()

    assert message is not None
    assert "disabled" in message

    from windows_agent_mcp.utils import active_tool_groups

    # Degraded to the default set rather than dying.
    assert len(get_tools(groups=active_tool_groups()[0])) == 17


def test_an_unknown_profile_degrades_with_a_message(profiles_file, monkeypatch) -> None:
    profiles_file(document(cpp={"tools": "edit"}))
    monkeypatch.setenv("BIONIC_PROFILE", "nonesuch")

    message = apply_selected_profile()

    assert message is not None
    assert "nonesuch" in message


def test_a_malformed_file_degrades_with_a_message(profiles_file, monkeypatch) -> None:
    profiles_file("{ broken")
    monkeypatch.setenv("BIONIC_PROFILE", "cpp")

    message = apply_selected_profile()

    assert message is not None
    assert "not valid JSON" in message


def test_profile_env_reaches_the_server(profiles_file, monkeypatch, tmp_path) -> None:
    project = tmp_path / "game"
    project.mkdir()

    profiles_file(
        document(cpp={"tools": "edit", "env": {"BIONIC_PROJECT_ROOTS": str(project)}})
    )
    monkeypatch.setenv("BIONIC_PROFILE", "cpp")
    monkeypatch.setenv("BIONIC_DOWNLOAD_ROOT", str(tmp_path / "dl"))

    apply_selected_profile()

    from windows_agent_mcp.utils import get_allowed_working_directories

    assert project.resolve() in get_allowed_working_directories()


class RecordingServer:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.ran = False

    def add_tool(self, fn, description: str | None = None) -> None:
        self.registered.append(fn.__name__)

    def run(self) -> None:
        self.ran = True


def test_main_honours_a_profile(profiles_file, monkeypatch) -> None:
    profiles_file(document(explore={"tools": "core"}))
    monkeypatch.setenv("BIONIC_PROFILE", "explore")

    server = RecordingServer()
    monkeypatch.setattr(main_module, "mcp", server)

    main_module.main()

    assert len(server.registered) == 7
    assert "write_file" not in server.registered


def test_main_logs_a_bad_profile(profiles_file, monkeypatch, caplog) -> None:
    profiles_file(document(cpp={"tools": "edit"}))
    monkeypatch.setenv("BIONIC_PROFILE", "typo")
    monkeypatch.setattr(main_module, "mcp", RecordingServer())

    with caplog.at_level("WARNING"):
        main_module.main()

    assert any("typo" in record.message for record in caplog.records)


def test_server_info_reports_the_profile(profiles_file, monkeypatch) -> None:
    profiles_file(document(cpp={"tools": "edit,build,docs"}))
    monkeypatch.setenv("BIONIC_PROFILE", "cpp")

    payload = json.loads(get_server_info())

    assert payload["profile"] == "cpp"
    assert payload["profile_error"] is None


def test_server_info_reports_a_profile_error(profiles_file, monkeypatch) -> None:
    """This is how the model learns to tell the user what is wrong."""

    profiles_file(document(cpp={"tools": "edit"}))
    monkeypatch.setenv("BIONIC_PROFILE", "nonesuch")

    payload = json.loads(get_server_info())

    assert payload["profile"] == "nonesuch"
    assert payload["profile_error"] is not None
    assert "nonesuch" in payload["profile_error"]


def test_server_info_without_a_profile() -> None:
    payload = json.loads(get_server_info())

    assert payload["profile"] is None
    assert payload["profile_error"] is None
    assert payload["profiles_file"]


# ============================================================
# CLI
# ============================================================


def test_init_writes_a_valid_file(tmp_path) -> None:
    target = tmp_path / DEFAULT_PROFILES_FILENAME

    assert main(["--file", str(target), "--init"]) == 0

    profiles, errors = load_profiles(target)

    assert errors == []
    assert "cpp" in profiles


def test_init_refuses_to_clobber(tmp_path, capsys) -> None:
    target = tmp_path / DEFAULT_PROFILES_FILENAME
    target.write_text("keep me", encoding="utf-8")

    assert main(["--file", str(target), "--init"]) == 1
    assert "--force" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "keep me"


def test_init_force_overwrites(tmp_path) -> None:
    target = tmp_path / DEFAULT_PROFILES_FILENAME
    target.write_text("old", encoding="utf-8")

    assert main(["--file", str(target), "--init", "--force"]) == 0
    assert "profiles" in target.read_text(encoding="utf-8")


def test_init_reports_an_unwritable_path(tmp_path, monkeypatch, capsys) -> None:
    def denied(self, *args: object, **kwargs: object):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "write_text", denied)

    assert main(["--file", str(tmp_path / "x.json"), "--init"]) == 1
    assert "could not write" in capsys.readouterr().err


def test_list_shows_state_and_counts(tmp_path, capsys) -> None:
    target = tmp_path / DEFAULT_PROFILES_FILENAME
    main(["--file", str(target), "--init"])

    assert main(["--file", str(target), "--list"]) == 0

    out = capsys.readouterr().out

    assert "cpp" in out
    assert "DISABLED" in out
    assert "14" in out


def test_list_on_an_empty_profiles_object(tmp_path, capsys) -> None:
    target = tmp_path / DEFAULT_PROFILES_FILENAME
    target.write_text(json.dumps(document()), encoding="utf-8")

    assert main(["--file", str(target), "--list"]) == 0
    assert "no profiles defined" in capsys.readouterr().out


def test_emit_client_prints_json(tmp_path, capsys) -> None:
    target = tmp_path / DEFAULT_PROFILES_FILENAME
    main(["--file", str(target), "--init"])

    # Drain the --init output, or it prefixes the JSON we are about to parse.
    capsys.readouterr()

    assert main(["--file", str(target), "--emit", "client"]) == 0

    config = json.loads(capsys.readouterr().out)

    assert "research" not in config["mcpServers"]
    assert "cpp" in config["mcpServers"]


def test_emit_inspector_requires_a_profile(tmp_path, capsys) -> None:
    target = tmp_path / DEFAULT_PROFILES_FILENAME
    main(["--file", str(target), "--init"])

    assert main(["--file", str(target), "--emit", "inspector"]) == 1
    assert "requires --profile" in capsys.readouterr().err


def test_emit_inspector_prints_one_profile(tmp_path, capsys) -> None:
    target = tmp_path / DEFAULT_PROFILES_FILENAME
    main(["--file", str(target), "--init"])

    capsys.readouterr()

    assert main(["--file", str(target), "--emit", "inspector", "--profile", "cpp"]) == 0

    config = json.loads(capsys.readouterr().out)

    entry = next(iter(config["mcpServers"].values()))

    assert entry["env"]["BIONIC_TOOLS"] == "edit,build,docs"


def test_emit_inspector_rejects_a_disabled_profile(tmp_path, capsys) -> None:
    target = tmp_path / DEFAULT_PROFILES_FILENAME
    main(["--file", str(target), "--init"])

    code = main(["--file", str(target), "--emit", "inspector", "--profile", "research"])

    assert code == 1
    assert "disabled" in capsys.readouterr().err


def test_cli_exits_non_zero_on_a_bad_file(tmp_path, capsys) -> None:
    """Unlike the server, the CLI is interactive, so it fails loudly."""

    target = tmp_path / DEFAULT_PROFILES_FILENAME
    target.write_text("{ broken", encoding="utf-8")

    assert main(["--file", str(target), "--list"]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_cli_with_no_action_prints_help(capsys) -> None:
    assert main([]) == 1
    assert "usage" in capsys.readouterr().out.lower()


# ============================================================
# Remaining validation branches
# ============================================================


def test_profiles_must_be_an_object() -> None:
    _profiles, errors = parse_profiles({"version": SCHEMA_VERSION, "profiles": []})

    assert any("'profiles' must be an object" in error for error in errors)


@pytest.mark.parametrize("name", ["", "   "])
def test_profile_names_must_be_non_empty(name: str) -> None:
    profiles, errors = parse_profiles(
        {"version": SCHEMA_VERSION, "profiles": {name: {"tools": "edit"}}}
    )

    assert profiles == {}
    assert any("non-empty" in error for error in errors)


@pytest.mark.parametrize("raw", ["a string", 42, ["edit"]])
def test_a_profile_must_be_an_object(raw: object) -> None:
    profiles, errors = parse_profiles(document(cpp=raw))

    assert profiles == {}
    assert any("must be an object" in error for error in errors)


def test_description_must_be_a_string() -> None:
    profiles, errors = parse_profiles(
        document(cpp={"tools": "edit", "description": 42})
    )

    assert profiles == {}
    assert any("'description' must be a string" in error for error in errors)


def test_non_string_env_keys_are_rejected() -> None:
    """Unreachable through JSON, which has string keys -- but parse_profiles
    takes any mapping, and an in-process caller could pass one."""

    _profiles, errors = parse_profiles(
        document(cpp={"tools": "edit", "env": {42: "value"}})
    )

    assert any("env keys must be strings" in error for error in errors)


def test_find_falls_back_to_cwd_outside_a_repository(tmp_path, monkeypatch) -> None:
    """A site-packages install has no repository root to fall back to."""

    monkeypatch.chdir(tmp_path)

    # Make the repo-root probe fail, leaving only the cwd fallback.
    real_is_file = Path.is_file

    def no_pyproject(self: Path) -> bool:
        if self.name == "pyproject.toml":
            return False
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", no_pyproject)

    assert find_profiles_file() == tmp_path / DEFAULT_PROFILES_FILENAME


def test_server_info_reports_a_malformed_profiles_file(
    profiles_file, monkeypatch
) -> None:
    """The load-error branch, distinct from a resolve error."""

    profiles_file("{ not json")
    monkeypatch.setenv("BIONIC_PROFILE", "cpp")

    payload = json.loads(get_server_info())

    assert payload["profile_error"] is not None
    assert "not valid JSON" in payload["profile_error"]
