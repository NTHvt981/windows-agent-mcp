"""Tests for tool groups.

Groups exist because tool definitions are re-serialized into the model's prompt
every turn, so they are a permanent tax on the context window rather than a
one-off cost. The properties worth protecting are:

* the default configuration is byte-identical to what existed before groups,
  so no existing install changes behaviour on upgrade;
* `core` is always present, because get_server_info lives in it and is what
  makes a missing tool diagnosable;
* the group NAMES in utils and the name -> tools MAPPING in main cannot drift;
* a typo neither silently changes the tool set nor kills the server.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import FakeResponse, _ddg_results, assert_error_response

from windows_agent_mcp import main as main_module
from windows_agent_mcp.main import (
    ALL_TOOLS,
    TOOL_GROUPS,
    TOOLS,
    get_tools,
    posture_warnings,
)
from windows_agent_mcp.tools.fetch_web_page import clear_cache, read_web_page
from windows_agent_mcp.tools.get_server_info import get_server_info
from windows_agent_mcp.tools.web_search import web_search
from windows_agent_mcp.untrusted import BEGIN_MARK
from windows_agent_mcp.utils import (
    ALWAYS_ON_TOOL_GROUPS,
    DEFAULT_TOOL_GROUPS,
    TOOL_GROUPS_ENV_VAR,
    VALID_TOOL_GROUPS,
    active_tool_groups,
    parse_tool_groups,
    web_research_enabled,
    web_search_enabled,
)


def names(tools) -> set[str]:
    return {tool.__name__ for tool in tools}


# ============================================================
# The two halves cannot drift
# ============================================================


def test_mapping_covers_exactly_the_valid_group_names() -> None:
    """Names live in utils, the mapping in main. This is the seam."""

    assert set(TOOL_GROUPS) == VALID_TOOL_GROUPS


def test_every_tool_belongs_to_a_group() -> None:
    """A tool in no group can never be registered."""

    assigned = {tool.__name__ for group in TOOL_GROUPS.values() for tool in group}

    assert assigned == names(ALL_TOOLS)


def test_only_web_search_is_in_two_groups() -> None:
    """One deliberate overlap, and nothing else.

    `research` is defined as everything `search` gives plus any-public-host
    fetching, so web_search appears in both. Any OTHER tool in two groups is a
    mistake: it would make the group a tool belongs to ambiguous, and turning
    one group off would not turn the tool off.

    Asserted as an exact set rather than a count so adding a second overlap has
    to be a decision someone writes down here.
    """

    assigned = [tool.__name__ for group in TOOL_GROUPS.values() for tool in group]

    duplicates = sorted({name for name in assigned if assigned.count(name) > 1})

    assert duplicates == ["web_search"], (
        f"unexpected tools in more than one group: {duplicates}"
    )

    # And the overlap is exactly the pair it is supposed to be.
    holders = sorted(
        group for group, tools in TOOL_GROUPS.items() if "web_search" in names(tools)
    )

    assert holders == ["research", "search"]


def test_default_groups_exclude_both_internet_groups() -> None:
    """Neither `search` nor `research` is on by default.

    `search` is excluded for the same reason as `research` rather than because
    it is equally dangerous: adding it to the default would silently change the
    posture of every existing install on upgrade, and sending a model-composed
    query to a third-party search engine is a decision the operator should make
    on purpose.
    """

    assert DEFAULT_TOOL_GROUPS == VALID_TOOL_GROUPS - {"research", "search"}


def test_core_is_the_always_on_group() -> None:
    assert ALWAYS_ON_TOOL_GROUPS == frozenset({"core"})


def test_get_server_info_is_in_core() -> None:
    """It reports which groups are active, so it must survive every profile.

    Without this, "why can I not see compile_shader" becomes unanswerable in
    exactly the configuration where it is most likely to be asked.
    """

    assert get_server_info in TOOL_GROUPS["core"]


def test_core_is_read_only() -> None:
    """core must never contain a tool that writes, executes or reaches out.

    That is what makes `WAMCP_TOOLS=core` a safe posture to hand to something
    untrusted, and it is a property that would erode silently as tools are
    added to the group for convenience.
    """

    forbidden = {
        "write_file",
        "edit_file",
        "run_powershell",
        "build_project",
        "compile_shader",
        "download_file",
        "fetch_https_response",
        "fetch_web_page",
        "get_github_latest_release",
        "web_search",
    }

    assert names(TOOL_GROUPS["core"]).isdisjoint(forbidden)


# ============================================================
# parse_tool_groups
# ============================================================


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, DEFAULT_TOOL_GROUPS),
        ("", DEFAULT_TOOL_GROUPS),
        ("   ", DEFAULT_TOOL_GROUPS),
        (",,,", DEFAULT_TOOL_GROUPS),
        ("all", VALID_TOOL_GROUPS),
        ("ALL", VALID_TOOL_GROUPS),
        # core is unioned in whether or not it was asked for.
        ("edit", frozenset({"core", "edit"})),
        ("core", frozenset({"core"})),
        ("edit,build,docs", frozenset({"core", "edit", "build", "docs"})),
        # Case and whitespace tolerant, duplicates collapse.
        ("  EDIT , build ,edit ", frozenset({"core", "edit", "build"})),
        ("research", frozenset({"core", "research"})),
    ],
)
def test_parse_tool_groups(value: str | None, expected: frozenset[str]) -> None:
    groups, error = parse_tool_groups(value)

    assert groups == expected
    assert error is None


def test_all_keyword_wins_over_other_entries() -> None:
    groups, error = parse_tool_groups("core,all")

    assert groups == VALID_TOOL_GROUPS
    assert error is None


def test_unknown_group_falls_back_and_reports() -> None:
    """A typo must not silently change the tool set, nor kill the server.

    Raising would abort startup, which an MCP client renders as an opaque
    connection failure. Falling back keeps the server usable while the mistake
    stays visible.
    """

    groups, error = parse_tool_groups("core,cpp")

    assert groups == DEFAULT_TOOL_GROUPS
    assert error is not None
    assert "cpp" in error
    # The message has to name the valid options, or the model cannot fix it.
    for valid in VALID_TOOL_GROUPS:
        assert valid in error


def test_unknown_group_error_lists_every_bad_name() -> None:
    _groups, error = parse_tool_groups("cpp,graphics,edit")

    assert error is not None
    assert "cpp" in error
    assert "graphics" in error


def test_parse_is_pure(monkeypatch: pytest.MonkeyPatch) -> None:
    """It takes the string, so the environment cannot influence it."""

    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "core")

    assert parse_tool_groups("all")[0] == VALID_TOOL_GROUPS


# ============================================================
# active_tool_groups: environment resolution
# ============================================================


def test_unset_environment_gives_the_default_set() -> None:
    groups, error = active_tool_groups()

    assert groups == DEFAULT_TOOL_GROUPS
    assert error is None


def test_environment_value_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "edit,build")

    assert active_tool_groups()[0] == frozenset({"core", "edit", "build"})


def test_web_research_env_var_implicitly_adds_the_research_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compatibility path: --web and every pre-groups config still work."""

    monkeypatch.setenv("WAMCP_WEB_RESEARCH", "1")

    groups, _error = active_tool_groups()

    assert "research" in groups
    assert groups == DEFAULT_TOOL_GROUPS | {"research"}


def test_the_alias_applies_even_to_a_narrow_group_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avoids the half-state where the posture widens but web_search is gone."""

    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "core")
    monkeypatch.setenv("WAMCP_WEB_RESEARCH", "1")

    assert active_tool_groups()[0] == frozenset({"core", "research"})


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_alias_truthy_values(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAMCP_WEB_RESEARCH", value)

    assert "research" in active_tool_groups()[0]


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
def test_alias_non_truthy_values(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAMCP_WEB_RESEARCH", value)

    assert "research" not in active_tool_groups()[0]


def test_group_error_survives_the_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "bogus")
    monkeypatch.setenv("WAMCP_WEB_RESEARCH", "1")

    groups, error = active_tool_groups()

    assert error is not None
    assert "research" in groups


# ============================================================
# web_research_enabled routes through the group set
# ============================================================


def test_research_enabled_via_the_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user's chosen coupling: the group alone widens the posture."""

    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "core,research")

    assert web_research_enabled() is True


def test_research_enabled_via_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAMCP_WEB_RESEARCH", "1")

    assert web_research_enabled() is True


def test_research_disabled_by_default() -> None:
    assert web_research_enabled() is False


def test_excluding_research_from_an_explicit_list_disables_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "core,edit,build,docs,net")

    assert web_research_enabled() is False


def test_fetch_web_page_posture_follows_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool must not need to know how the group set was assembled.

    DNS is stubbed rather than left to the real resolver. getaddrinfo is called
    even for a literal IP, so without this the test reaches the network and
    fails under the hermeticity guard -- and hermeticity is a hard requirement
    here, since pre-commit runs the suite.
    """

    import socket

    from windows_agent_mcp.tools.fetch_web_page import read_web_page

    def resolves_private(host: str, port: int, *args: Any, **kwargs: Any):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolves_private)
    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "core,research")

    # example.com is off both allowlists, so getting as far as the DNS check
    # proves the any-host path was taken rather than the doc-host path.
    result = read_web_page("https://example.com/")

    payload = json.loads(result)

    assert payload["error"]["type"] == "URL_NOT_ALLOWED"

    # Refused for the private address, NOT for being off the allowlist. That
    # distinction is the whole assertion: research mode skips the allowlist and
    # nothing else.
    assert "Blocked network address" in payload["error"]["message"]
    assert "not allowed" not in payload["error"]["message"]


def test_fetch_web_page_stays_doc_scoped_without_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction: no research group means the allowlist still bites.

    Refused before DNS, which is what keeps this hermetic with no stubbing.
    """

    from windows_agent_mcp.tools.fetch_web_page import read_web_page

    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "core,docs")

    payload = json.loads(read_web_page("https://example.com/"))

    assert payload["error"]["type"] == "URL_NOT_ALLOWED"
    assert "not allowed" in payload["error"]["message"]


# ============================================================
# get_tools composition
# ============================================================


def test_default_matches_the_pre_groups_tool_set() -> None:
    """The upgrade-safety property: nobody's tool list changes on upgrade."""

    assert get_tools() == TOOLS
    assert get_tools(groups=None) == TOOLS


@pytest.mark.parametrize(
    ("groups", "expected_count"),
    [
        (frozenset({"core"}), 7),
        (frozenset({"core", "docs"}), 8),
        (frozenset({"build", "docs"}), 12),
        (frozenset({"edit", "build", "docs"}), 14),
        (DEFAULT_TOOL_GROUPS, 17),
        (VALID_TOOL_GROUPS, 18),
    ],
)
def test_group_composition_counts(groups: frozenset[str], expected_count: int) -> None:
    assert len(get_tools(groups=groups)) == expected_count


def test_core_is_added_even_when_not_requested() -> None:
    tools = names(get_tools(groups=frozenset({"edit"})))

    assert "read_file" in tools
    assert "get_server_info" in tools
    assert tools == names(TOOL_GROUPS["core"]) | names(TOOL_GROUPS["edit"])


def test_empty_group_set_still_yields_core() -> None:
    """A server with zero tools is useless, so this floor is deliberate."""

    assert names(get_tools(groups=frozenset())) == names(TOOL_GROUPS["core"])


def test_unknown_group_names_are_ignored_by_get_tools() -> None:
    """get_tools is not the validator; parse_tool_groups is."""

    assert get_tools(groups=frozenset({"core", "nonsense"})) == TOOL_GROUPS["core"]


def test_review_profile_has_no_write_access() -> None:
    """The posture the edit/build split exists to make expressible."""

    tools = names(get_tools(groups=frozenset({"build", "docs"})))

    assert "build_project" in tools
    assert "compile_shader" in tools
    assert "write_file" not in tools
    assert "edit_file" not in tools


def test_ordering_follows_the_canonical_tool_order() -> None:
    """Presentation order affects which tool a small model reaches for.

    Derived by filtering ALL_TOOLS rather than concatenating group tuples, so
    the workflow grouping in TOOLS stays the single source of order.
    """

    selected = get_tools(groups=frozenset({"core", "edit", "research"}))

    canonical = [tool for tool in ALL_TOOLS if tool in set(selected)]

    assert list(selected) == canonical


def test_no_duplicates_when_groups_overlap_in_request() -> None:
    tools = get_tools(groups=frozenset({"core", "edit"}))

    assert len(tools) == len(set(tools))


# ============================================================
# get_tools stays pure
# ============================================================


def test_get_tools_ignores_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Purity keeps the suite independent of the developer's shell.

    pre-commit runs these tests, so an env-reading get_tools would stop anyone
    who actually uses WAMCP_TOOLS from committing.
    """

    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "core")
    monkeypatch.setenv("WAMCP_WEB_RESEARCH", "1")

    assert get_tools() == TOOLS


# ============================================================
# Backward-compatible web_enabled shim
# ============================================================


def test_web_enabled_false_matches_the_default_set() -> None:
    assert get_tools(web_enabled=False) == TOOLS


def test_web_enabled_true_adds_research() -> None:
    assert set(get_tools(web_enabled=True)) == set(ALL_TOOLS)


def test_web_enabled_overrides_the_group_set() -> None:
    """Explicit beats implicit, in both directions."""

    with_research = get_tools(groups=frozenset({"core", "research"}), web_enabled=False)
    without = get_tools(groups=frozenset({"core"}), web_enabled=True)

    assert "web_search" not in names(with_research)
    assert "web_search" in names(without)


# ============================================================
# get_server_info reporting
# ============================================================


def test_server_info_reports_the_active_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "edit,build")

    payload = json.loads(get_server_info())

    assert payload["active_tool_groups"] == ["build", "core", "edit"]
    assert payload["registered_tool_count"] == 13
    assert payload["tool_groups_error"] is None
    assert payload["tool_groups_env_var"] == TOOL_GROUPS_ENV_VAR
    assert set(payload["available_tool_groups"]) == VALID_TOOL_GROUPS


def test_server_info_reports_a_bad_group_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is how the model learns to tell the user about the typo."""

    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "cpp")

    payload = json.loads(get_server_info())

    assert payload["tool_groups_error"] is not None
    assert "cpp" in payload["tool_groups_error"]
    assert payload["registered_tool_count"] == 17


def test_server_info_default_reporting() -> None:
    payload = json.loads(get_server_info())

    assert payload["registered_tool_count"] == 17
    assert "research" not in payload["active_tool_groups"]


def test_server_info_names_the_registered_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count alone is not an answer to "which tools do you have".

    Asked that question with only a count available, a 9B model filled the gap
    from its CLIENT's tool list and attributed shell, git and file-writing
    tools to this server's read-only core group.
    """

    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "build")

    payload = json.loads(get_server_info())

    names = payload["registered_tools"]

    assert len(names) == payload["registered_tool_count"]
    assert "compile_shader" in names
    assert "write_file" not in names

    # Registration order, not alphabetical: TOOLS declares a workflow ordering
    # and it steers which tool a small model reaches for first.
    assert names == [tool.__name__ for tool in get_tools(groups={"core", "build"})]


def test_server_info_maps_every_group_to_its_tools() -> None:
    """Including inactive groups -- that is what makes a missing tool findable."""

    payload = json.loads(get_server_info())

    mapping = payload["tools_by_group"]

    assert set(mapping) == VALID_TOOL_GROUPS

    for group, tools in TOOL_GROUPS.items():
        assert mapping[group] == [tool.__name__ for tool in tools]


def test_server_info_shows_an_inactive_tool_and_its_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "why can I not see compile_shader" case, answered in one call."""

    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "core")

    payload = json.loads(get_server_info())

    assert "compile_shader" not in payload["registered_tools"]
    assert "compile_shader" in payload["tools_by_group"]["build"]
    assert "build" not in payload["active_tool_groups"]


# ============================================================
# main() honours the environment
# ============================================================


class RecordingServer:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.ran = False

    def add_tool(self, fn, description: str | None = None) -> None:
        self.registered.append(fn.__name__)

    def run(self) -> None:
        self.ran = True


@pytest.mark.parametrize(
    ("value", "expected_count"),
    [
        (None, 17),
        ("core", 7),
        ("edit,build,docs", 14),
        ("all", 18),
        ("core,bogus", 17),
    ],
)
def test_main_registers_the_configured_groups(
    value: str | None,
    expected_count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if value is not None:
        monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, value)

    server = RecordingServer()

    monkeypatch.setattr(main_module, "mcp", server)

    main_module.main()

    assert len(server.registered) == expected_count
    assert server.ran is True


def test_main_logs_a_bad_group_name(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """Reported to stderr, never stdout -- stdout is the JSON-RPC wire."""

    monkeypatch.setenv(TOOL_GROUPS_ENV_VAR, "cpp")
    monkeypatch.setattr(main_module, "mcp", RecordingServer())

    with caplog.at_level("WARNING"):
        main_module.main()

    assert any("cpp" in record.message for record in caplog.records)


def test_main_still_honours_the_web_research_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAMCP_WEB_RESEARCH", "1")

    server = RecordingServer()

    monkeypatch.setattr(main_module, "mcp", server)

    main_module.main()

    assert "web_search" in server.registered
    assert len(server.registered) == 18


# ============================================================
# The `search` group
# ============================================================
#
# Exists because WAMCP_WEB_RESEARCH gated two unrelated permissions with one
# switch: "may run web_search" and "may read any public host". The posture that
# actually suits a small model -- find URLs, but still ask before reading an
# unvetted host -- was therefore inexpressible, and a model with a page reader
# and no search answers from memory instead, inventing plausible URLs.


def test_search_registers_web_search() -> None:
    assert "web_search" in names(get_tools(groups=frozenset({"core", "search"})))


def test_search_does_not_widen_which_hosts_may_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of splitting the group, and the thing to never regress.

    If `search` widened fetch_web_page, it would just be `research` under
    another name and the split would buy nothing.
    """

    monkeypatch.setenv("WAMCP_TOOLS", "core,docs,search")

    assert web_search_enabled() is True
    assert web_research_enabled() is False


def test_research_still_enables_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """`research` is a strict superset, so an existing config loses nothing."""

    monkeypatch.setenv("WAMCP_TOOLS", "core,research")

    assert web_search_enabled() is True
    assert web_research_enabled() is True


def test_the_legacy_variable_still_enables_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WAMCP_WEB_RESEARCH=1 predates groups and must keep working."""

    monkeypatch.setenv("WAMCP_WEB_RESEARCH", "1")

    assert web_search_enabled() is True
    assert web_research_enabled() is True


def test_neither_group_means_no_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAMCP_TOOLS", "core,docs,edit,build,net")

    assert web_search_enabled() is False
    assert web_research_enabled() is False


def test_search_is_off_by_default() -> None:
    """Upgrading must not hand an existing install a new outbound tool."""

    assert "search" not in DEFAULT_TOOL_GROUPS
    assert "web_search" not in names(get_tools())


def test_web_search_refuses_to_run_without_either_group() -> None:
    """Called directly, with no group active."""

    assert_error_response(web_search("anything"), "WEB_RESEARCH_DISABLED")


def test_web_search_runs_under_the_search_group(
    search_opener, monkeypatch: pytest.MonkeyPatch, resolves_public: None
) -> None:
    """Search works with `search` alone -- no research mode anywhere.

    The stubbed backend means this asserts the gate, not the network.
    """

    monkeypatch.setenv("WAMCP_TOOLS", "core,docs,search")

    search_opener(FakeResponse(_ddg_results("https://found.example.com/a")))

    result = web_search("2d rts pathfinding")

    assert "https://found.example.com/a" in result
    assert BEGIN_MARK in result


def test_a_found_url_is_still_not_readable(
    monkeypatch: pytest.MonkeyPatch, page_opener, resolves_public: None
) -> None:
    """Search finds it; fetch still refuses it. This is the designed flow.

    The model is expected to surface the host to the user, who grants it. That
    loop is the reason this group is separate from `research`.
    """

    monkeypatch.setenv("WAMCP_TOOLS", "core,docs,search")

    clear_cache()
    page_opener()

    payload = assert_error_response(
        read_web_page("https://found.example.com/a"),
        "URL_NOT_ALLOWED",
    )

    recovery = " ".join(payload["error"]["recovery"])

    assert "hostgrants --add found.example.com" in recovery


def test_search_without_docs_is_reported_at_startup() -> None:
    """URLs the model cannot open. Legal, useless, and silent without this."""

    messages = posture_warnings(frozenset({"core", "search"}))

    assert len(messages) == 1
    assert "'docs' is not" in messages[0]


def test_the_useful_combination_warns_about_nothing() -> None:
    for groups in [
        frozenset({"core", "docs", "search"}),
        frozenset({"core", "docs", "research"}),
        frozenset({"core", "docs"}),
        DEFAULT_TOOL_GROUPS,
    ]:
        assert posture_warnings(groups) == [], groups


def test_the_default_posture_warns_about_nothing() -> None:
    """A warning on the default configuration trains people to ignore warnings.

    `docs` without `search` is the default AND a legitimate deliberate choice
    (reading the Vulkan spec without granting a search tool), so it must stay
    silent. The note about invented URLs lives in the documentation instead.
    """

    assert posture_warnings(DEFAULT_TOOL_GROUPS) == []


def test_get_server_info_reports_search_separately_from_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise a server where web_search works reports no backend at all."""

    monkeypatch.setenv("WAMCP_TOOLS", "core,docs,search")

    payload = json.loads(get_server_info())

    assert payload["web_search"] is True
    assert payload["web_research"] is False
    assert payload["search_backend"] == "duckduckgo"
    assert "search" in payload["active_tool_groups"]
    assert "search" in payload["available_tool_groups"]

    # The fetch posture must still read as the narrow one.
    assert "any public host" not in payload["network_policy"]
