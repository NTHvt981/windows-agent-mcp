"""Tests for input/config.json.

The design claim being tested: every setting lives in one generated,
self-documenting file; an environment variable still overrides it; and nothing
about the file can stop the server starting.

Ports the cases that mattered from the profiles file this replaced --
precedence, disabled-versus-not-found, degradation, and absolute-path emission
-- because those were each written for a specific failure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from windows_agent_mcp import config as config_module
from windows_agent_mcp.config import (
    SCHEMA_VERSION,
    SETTINGS,
    Config,
    Profile,
    apply_to_environment,
    emit_client_config,
    emit_inspector_config,
    find_config_file,
    load_config,
    parse_config,
    resolve_profile,
    scaffold,
    write_scaffold,
)
from windows_agent_mcp.inspector_config import FORWARDED_ENV_VARS


def document(**overrides: Any) -> dict[str, Any]:
    """A minimal valid config document."""

    return {"version": SCHEMA_VERSION, **overrides}


def settings_of(**values: str) -> dict[str, Any]:
    """A settings block in the generated (object) form."""

    return {name: {"value": value} for name, value in values.items()}


# ============================================================
# The generated file
# ============================================================


def test_the_scaffold_parses_cleanly() -> None:
    """The file the server writes must be one the server can read."""

    config, errors = parse_config(scaffold())

    assert errors == []
    assert config.settings == {}, "a generated file sets nothing; it shows defaults"
    assert "cpp" in config.profiles


def test_every_setting_appears_in_the_generated_file() -> None:
    """Discoverability is the whole point of generating it.

    A setting missing from the scaffold is one nobody can find without reading
    the source, which is the situation this file exists to end.
    """

    written = scaffold()["settings"]

    assert set(written) == set(SETTINGS)

    for name, entry in written.items():
        assert entry["description"].strip(), f"{name} has no description"
        assert entry["default"].strip(), f"{name} does not say what its default is"
        assert entry["value"] == "", f"{name} ships with a value already set"


def test_every_setting_is_a_variable_the_server_forwards() -> None:
    """A setting the Inspector does not forward silently does nothing under --dev.

    That failure survived a long time once, every symptom looking like a
    plausible default, which is why it is pinned in two places.
    """

    missing = sorted(set(SETTINGS) - set(FORWARDED_ENV_VARS))

    assert missing == [], (
        f"{missing} are configurable but not forwarded; add them to "
        f"FORWARDED_ENV_VARS in inspector_config.py"
    )


def test_write_scaffold_creates_the_directory(tmp_path: Path) -> None:
    target = tmp_path / "input" / "config.json"

    assert write_scaffold(target) is None
    assert target.is_file()

    config, errors = load_config(target)

    assert errors == []
    assert config.profiles


def test_write_scaffold_leaves_no_temporary_file(tmp_path: Path) -> None:
    """An interrupted first run must not leave a truncated config behind."""

    target = tmp_path / "config.json"

    write_scaffold(target)

    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_write_scaffold_reports_a_failure(tmp_path: Path, monkeypatch) -> None:
    def denied(*args: object, **kwargs: object):
        raise PermissionError("read-only")

    monkeypatch.setattr(config_module.tempfile, "mkstemp", denied)

    error = write_scaffold(tmp_path / "config.json")

    assert error is not None
    assert "could not create" in error


# ============================================================
# Precedence: the environment wins
# ============================================================


def test_settings_are_applied_to_the_environment() -> None:
    environ: dict[str, str] = {}

    skipped = apply_to_environment({"WAMCP_TOOLS": "core,docs"}, environ)

    assert environ == {"WAMCP_TOOLS": "core,docs"}
    assert skipped == []


def test_an_already_set_variable_is_never_overwritten() -> None:
    """ "More local wins": a client's env block beats the file.

    For some MCP clients an `env` block is the only way to configure a server
    at all, so the file must not silently override it.
    """

    environ = {"WAMCP_TOOLS": "core"}

    skipped = apply_to_environment({"WAMCP_TOOLS": "core,docs"}, environ)

    assert environ == {"WAMCP_TOOLS": "core"}
    assert skipped == ["WAMCP_TOOLS"]


def test_an_override_is_reported_not_swallowed() -> None:
    """A silently ignored setting is the confusion this file exists to remove."""

    environ = {"WAMCP_TOOLS": "core", "WAMCP_PROJECT_ROOTS": "C:/x"}

    skipped = apply_to_environment(
        {"WAMCP_TOOLS": "all", "WAMCP_PROJECT_ROOTS": "C:/y"}, environ
    )

    assert sorted(skipped) == ["WAMCP_PROJECT_ROOTS", "WAMCP_TOOLS"]


def test_a_whitespace_only_variable_does_not_count_as_set() -> None:
    environ = {"WAMCP_TOOLS": "   "}

    apply_to_environment({"WAMCP_TOOLS": "core,docs"}, environ)

    assert environ["WAMCP_TOOLS"] == "core,docs"


# ============================================================
# Parsing
# ============================================================


def test_an_empty_value_means_unset() -> None:
    """So a caller never has to distinguish "" from absent."""

    config, errors = parse_config(
        document(settings=settings_of(WAMCP_TOOLS="", WAMCP_PROJECT_ROOTS="C:/g"))
    )

    assert errors == []
    assert config.settings == {"WAMCP_PROJECT_ROOTS": "C:/g"}


def test_a_plain_string_setting_is_accepted() -> None:
    """Somebody editing by hand will write the short form; accept it."""

    config, errors = parse_config(document(settings={"WAMCP_TOOLS": "core,docs"}))

    assert errors == []
    assert config.settings == {"WAMCP_TOOLS": "core,docs"}


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ([], "must contain a JSON object"),
        ("nope", "must contain a JSON object"),
        ({"version": 99}, "unsupported 'version'"),
        ({}, "missing 'version'"),
    ],
)
def test_a_malformed_document_is_reported(data: Any, expected: str) -> None:
    _config, errors = parse_config(data)

    assert any(expected in error for error in errors)


def test_an_unknown_setting_names_the_valid_ones() -> None:
    """A typo must be fixable from the message alone."""

    _config, errors = parse_config(document(settings={"WAMCP_TOLS": "core"}))

    assert len(errors) == 1
    assert "WAMCP_TOLS" in errors[0]

    for known in SETTINGS:
        assert known in errors[0]


def test_errors_accumulate_rather_than_stopping_at_the_first() -> None:
    """One pass should show everything wrong with the file."""

    _config, errors = parse_config(
        document(
            settings={"WAMCP_NOPE": "x", "WAMCP_TOOLS": 42},
            profiles={"bad": {"tools": "cpp"}},
        )
    )

    assert len(errors) >= 3


def test_a_bad_group_name_is_caught_at_load() -> None:
    """The validation a bare environment variable cannot do.

    `WAMCP_TOOLS=core,cpp` only warns at startup and silently registers the
    default set. Here the profile is named, the mistake is named, and the valid
    groups are listed.
    """

    _config, errors = parse_config(document(profiles={"cpp": {"tools": "core,cpp"}}))

    assert len(errors) == 1
    assert "cpp" in errors[0]
    assert "core" in errors[0]


def test_a_profile_may_not_set_the_tools_variable() -> None:
    """Two sources for one setting is ambiguous rather than convenient."""

    _config, errors = parse_config(
        document(
            profiles={
                "x": {"tools": "edit", "settings": {"WAMCP_TOOLS": "core"}},
            }
        )
    )

    assert len(errors) == 1
    assert "use the 'tools' field" in errors[0]


def test_a_profile_carries_its_own_settings() -> None:
    config, errors = parse_config(
        document(
            profiles={
                "cpp": {
                    "tools": "edit,build",
                    "settings": {"WAMCP_PROJECT_ROOTS": "C:/game"},
                }
            }
        )
    )

    assert errors == []
    assert config.profiles["cpp"].settings == {"WAMCP_PROJECT_ROOTS": "C:/game"}


def test_active_profile_is_read() -> None:
    config, errors = parse_config(
        document(active_profile="cpp", profiles={"cpp": {"tools": "edit"}})
    )

    assert errors == []
    assert config.active_profile == "cpp"


# ============================================================
# Loading a file
# ============================================================


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """The caller generates one; a warning here would be noise."""

    config, errors = load_config(tmp_path / "absent.json")

    assert errors == []
    assert config.settings == {}


def test_invalid_json_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ broken", encoding="utf-8")

    _config, errors = load_config(path)

    assert "not valid JSON" in errors[0]


def test_an_unreadable_path_is_reported(tmp_path: Path) -> None:
    _config, errors = load_config(tmp_path)

    assert "could not read" in errors[0]


# ============================================================
# Profiles
# ============================================================


def test_disabled_and_missing_are_reported_differently() -> None:
    """They need different fixes: a flag to flip, or a name to correct."""

    profiles = {
        "on": Profile("on", True, "", "core", {}),
        "off": Profile("off", False, "", "core", {}),
    }

    _profile, missing = resolve_profile("nope", profiles)
    _profile, disabled = resolve_profile("off", profiles)

    assert missing is not None and "no profile named" in missing
    assert disabled is not None and "is disabled" in disabled


def test_only_enabled_profiles_are_offered_as_alternatives() -> None:
    profiles = {
        "on": Profile("on", True, "", "core", {}),
        "off": Profile("off", False, "", "core", {}),
    }

    _profile, error = resolve_profile("typo", profiles)

    assert error is not None
    assert "on" in error
    assert "off" not in error.replace("no profile", "")


# ============================================================
# Emitting client configuration
# ============================================================


def config_with(**profiles: Profile) -> Config:
    return Config(
        settings={"WAMCP_PROJECT_ROOTS": "C:/g"},
        active_profile="",
        profiles=dict(profiles),
    )


def test_emitted_command_is_absolute() -> None:
    """A bare "windows-agent-mcp" is not on a global PATH and fails in a client.

    This was a real failure, not a hypothetical one.
    """

    config = config_with(cpp=Profile("cpp", True, "", "edit,build", {}))

    emitted = emit_client_config(config, "C:/projects/mcp/.venv/Scripts/python.exe")

    command = emitted["mcpServers"]["cpp"]["command"]

    assert command.startswith("C:/projects/mcp/")
    assert "\\" not in command


def test_emitted_entries_carry_the_base_settings() -> None:
    """So the client config keeps working if the file moves."""

    config = config_with(
        cpp=Profile("cpp", True, "", "edit", {"WAMCP_HOST_CONSENT": "0"})
    )

    entry = emit_client_config(config, "C:/py/python.exe")["mcpServers"]["cpp"]

    assert entry["env"]["WAMCP_PROJECT_ROOTS"] == "C:/g"
    assert entry["env"]["WAMCP_TOOLS"] == "edit"
    assert entry["env"]["WAMCP_HOST_CONSENT"] == "0"


def test_disabled_profiles_are_not_emitted() -> None:
    config = config_with(
        on=Profile("on", True, "", "core", {}),
        off=Profile("off", False, "", "core", {}),
    )

    servers = emit_client_config(config, "C:/py/python.exe")["mcpServers"]

    assert set(servers) == {"on"}


def test_inspector_config_delegates_its_shape() -> None:
    """So the two cannot drift, and the reason that module exists stays in one place."""

    config = config_with(cpp=Profile("cpp", True, "", "edit", {}))

    emitted = emit_inspector_config(config, config.profiles["cpp"], "C:/py/python.exe")

    assert "mcpServers" in emitted
    assert emitted["mcpServers"]["windows-agent-mcp"]["env"]["WAMCP_TOOLS"] == "edit"


# ============================================================
# Finding the file
# ============================================================


def test_the_environment_variable_wins(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "elsewhere.json"

    monkeypatch.setenv("WAMCP_CONFIG_FILE", str(target))

    assert find_config_file() == target


def test_input_config_json_under_the_working_directory_is_used(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("WAMCP_CONFIG_FILE", raising=False)
    monkeypatch.chdir(tmp_path)

    local = tmp_path / "input" / "config.json"
    local.parent.mkdir()
    local.write_text('{"version": 1}', encoding="utf-8")

    assert find_config_file() == local


def test_the_repository_root_is_the_fallback(tmp_path: Path, monkeypatch) -> None:
    """Only for an editable checkout, which pyproject.toml confirms."""

    monkeypatch.delenv("WAMCP_CONFIG_FILE", raising=False)
    monkeypatch.chdir(tmp_path)

    found = find_config_file()

    assert found.name == "config.json"
    assert found.parent.name == "input"
    assert (found.parent.parent / "pyproject.toml").is_file()


# ============================================================
# The CLI
# ============================================================


def test_init_writes_a_file_that_loads(tmp_path: Path) -> None:
    target = tmp_path / "config.json"

    assert config_module.main(["--file", str(target), "--init"]) == 0

    _config, errors = load_config(target)

    assert errors == []


def test_init_refuses_to_clobber(tmp_path: Path, capsys) -> None:
    target = tmp_path / "config.json"
    target.write_text("keep me", encoding="utf-8")

    assert config_module.main(["--file", str(target), "--init"]) == 1
    assert "--force" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "keep me"


def test_list_shows_every_setting(tmp_path: Path, capsys) -> None:
    target = tmp_path / "config.json"
    write_scaffold(target)

    assert config_module.main(["--file", str(target), "--list"]) == 0

    out = capsys.readouterr().out

    for name in SETTINGS:
        assert name in out


def test_list_marks_the_active_profile(tmp_path: Path, capsys) -> None:
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(document(active_profile="cpp", profiles={"cpp": {"tools": "edit"}})),
        encoding="utf-8",
    )

    config_module.main(["--file", str(target)])

    assert "<- active" in capsys.readouterr().out


def test_emit_client_prints_json(tmp_path: Path, capsys) -> None:
    target = tmp_path / "config.json"
    write_scaffold(target)
    capsys.readouterr()

    assert config_module.main(["--file", str(target), "--emit", "client"]) == 0

    emitted = json.loads(capsys.readouterr().out)

    assert "cpp" in emitted["mcpServers"]


def test_emit_inspector_requires_a_profile(tmp_path: Path, capsys) -> None:
    target = tmp_path / "config.json"
    write_scaffold(target)
    capsys.readouterr()

    assert config_module.main(["--file", str(target), "--emit", "inspector"]) == 1
    assert "requires --profile" in capsys.readouterr().err


def test_emit_inspector_rejects_a_disabled_profile(tmp_path: Path, capsys) -> None:
    target = tmp_path / "config.json"
    write_scaffold(target)
    capsys.readouterr()

    code = config_module.main(
        ["--file", str(target), "--emit", "inspector", "--profile", "research"]
    )

    assert code == 1
    assert "disabled" in capsys.readouterr().err


def test_the_cli_exits_nonzero_on_a_broken_file(tmp_path: Path, capsys) -> None:
    target = tmp_path / "config.json"
    target.write_text("{ broken", encoding="utf-8")

    assert config_module.main(["--file", str(target)]) == 1
    assert "not valid JSON" in capsys.readouterr().err
