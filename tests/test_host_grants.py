"""Tests for operator-granted readable hosts.

The design claim being tested: an operator can allow fetch_web_page to read
one extra host, the grant takes effect without restarting the server, and the
model cannot grant one to itself.

The last part is the one worth being careful about. Every other check in
utils.py is decorative if a tool can write the file that says which hosts are
readable.
"""

from __future__ import annotations

import io
import json
import urllib.request
from http.client import HTTPMessage
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeResponse, assert_error_response

from windows_agent_mcp import hostgrants
from windows_agent_mcp.inspector_config import FORWARDED_ENV_VARS
from windows_agent_mcp.tools.edit_file import edit_file
from windows_agent_mcp.tools.fetch_web_page import clear_cache, read_web_page
from windows_agent_mcp.tools.write_file import write_file
from windows_agent_mcp.utils import (
    ALLOWED_DOC_HOSTS,
    PROTECTED_CONFIG_FILENAMES,
    DocRedirectHandler,
    RedirectNotAllowedError,
    host_grant_would_help,
    readable_web_hosts,
)


def write_grants(path: Path, *hosts: Any, version: int = 1) -> None:
    """Write a grants file. Entries may be strings or dicts, as the format is."""

    path.write_text(
        json.dumps({"version": version, "hosts": list(hosts)}),
        encoding="utf-8",
    )


# ============================================================
# Host normalisation
# ============================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "example.com"),
        ("Example.COM", "example.com"),
        ("  example.com  ", "example.com"),
        # A trailing dot is the absolute form of the same name, and
        # is_allowed_host() strips it before comparing, so an entry keeping it
        # would never match anything.
        ("example.com.", "example.com"),
        ("a.b.c.example.co.uk", "a.b.c.example.co.uk"),
    ],
)
def test_normalise_host_accepts_hostnames(raw: str, expected: str) -> None:
    assert hostgrants.normalise_host(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected_message"),
    [
        ("", "cannot be empty"),
        ("   ", "cannot be empty"),
        ("*.example.com", "wildcard"),
        ("exam?le.com", "wildcard"),
        ("https://example.com/page", "looks like a URL"),
        ("example.com/page", "looks like a URL"),
        ("user@example.com", "credentials"),
        ("example.com:8443", "port"),
        ("a.com b.com", "whitespace"),
    ],
)
def test_normalise_host_rejects_everything_else(
    raw: str, expected_message: str
) -> None:
    with pytest.raises(ValueError, match=expected_message):
        hostgrants.normalise_host(raw)


def test_a_pasted_url_is_rejected_with_the_hostname_to_use() -> None:
    """The likeliest operator mistake, so the error has to be actionable.

    Coercing it silently would be friendlier and wrong: in a file that decides
    what may be read, the operator must approve the thing they typed.
    """

    with pytest.raises(ValueError) as caught:
        hostgrants.normalise_host("https://www.redblobgames.com/programming/rts")

    assert "www.redblobgames.com" in str(caught.value)


def test_a_wildcard_is_refused_rather_than_narrowed() -> None:
    """A subdomain wildcard is an any-host grant for that zone.

    Not a style preference. Anything that lets strangers publish under a
    subdomain -- or one stale CNAME -- turns `*.example.com` into the
    permission research mode grants, while reading like the opposite.
    """

    with pytest.raises(ValueError, match="any-host grant"):
        hostgrants.normalise_host("*.githubusercontent.com")


# ============================================================
# The separated list form
# ============================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, set()),
        ("", set()),
        ("   ", set()),
        ("a.example.com", {"a.example.com"}),
        ("a.example.com,b.example.com", {"a.example.com", "b.example.com"}),
        # Semicolon matches WAMCP_PROJECT_ROOTS; cmd.exe splits batch
        # arguments on both, so a launcher flag can arrive either way.
        ("a.example.com;b.example.com", {"a.example.com", "b.example.com"}),
        ("a.example.com\nb.example.com", {"a.example.com", "b.example.com"}),
        ("A.Example.com,,b.example.com", {"a.example.com", "b.example.com"}),
    ],
)
def test_parse_host_list(raw: str | None, expected: set[str]) -> None:
    hosts, errors = hostgrants.parse_host_list(raw)

    assert hosts == expected
    assert errors == []


def test_one_bad_entry_does_not_discard_the_good_ones() -> None:
    """Failing the whole list over a typo would silently revoke working hosts."""

    hosts, errors = hostgrants.parse_host_list("good.example.com,*.bad.example.com")

    assert hosts == {"good.example.com"}
    assert len(errors) == 1
    assert "WAMCP_EXTRA_DOC_HOSTS" in errors[0]


# ============================================================
# The file format
# ============================================================


def test_plain_strings_are_a_valid_entry() -> None:
    grants, errors = hostgrants.parse_grants(
        {"version": 1, "hosts": ["a.example.com", "b.example.com"]}
    )

    assert errors == []
    assert set(grants) == {"a.example.com", "b.example.com"}
    assert all(grant.enable for grant in grants.values())


def test_objects_carry_enable_and_note() -> None:
    grants, errors = hostgrants.parse_grants(
        {
            "version": 1,
            "hosts": [{"host": "a.example.com", "enable": False, "note": "why"}],
        }
    )

    assert errors == []
    assert grants["a.example.com"] == hostgrants.HostGrant(
        host="a.example.com", enable=False, note="why"
    )


@pytest.mark.parametrize(
    ("data", "expected_message"),
    [
        ([], "must be a JSON object"),
        ("nope", "must be a JSON object"),
        ({"version": 2, "hosts": []}, "unsupported version"),
        ({"version": 1, "hosts": {}}, "must be a list"),
    ],
)
def test_a_malformed_file_yields_no_hosts(data: Any, expected_message: str) -> None:
    grants, errors = hostgrants.parse_grants(data)

    assert grants == {}
    assert expected_message in errors[0]


@pytest.mark.parametrize(
    ("entry", "expected_message"),
    [
        (42, "must be a hostname string or an object"),
        ({"host": 42}, "'host' must be a string"),
        ({"host": "a.example.com", "enable": "yes"}, "'enable' must be true or false"),
        ({"host": "a.example.com", "note": 42}, "'note' must be a string"),
        ({"host": "*.example.com"}, "wildcard"),
    ],
)
def test_a_bad_entry_is_reported_with_its_index(
    entry: Any, expected_message: str
) -> None:
    grants, errors = hostgrants.parse_grants({"version": 1, "hosts": [entry]})

    assert grants == {}
    assert errors[0].startswith("hosts[0]:")
    assert expected_message in errors[0]


def test_a_duplicate_host_is_reported_rather_than_silently_merged() -> None:
    """Two entries for one host means two different intentions in the file.

    Merging would pick one silently, and if they disagree about `enable` the
    operator's revocation could be the one discarded.
    """

    grants, errors = hostgrants.parse_grants(
        {
            "version": 1,
            "hosts": [
                {"host": "a.example.com", "enable": True},
                {"host": "a.example.com", "enable": False},
            ],
        }
    )

    assert set(grants) == {"a.example.com"}
    assert "duplicate host" in errors[0]


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """No grants is the normal state; warning about it would train indifference."""

    grants, errors = hostgrants.load_grants(tmp_path / "absent.json")

    assert grants == {}
    assert errors == []


def test_invalid_json_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    path.write_text("{not json", encoding="utf-8")

    grants, errors = hostgrants.load_grants(path)

    assert grants == {}
    assert "not valid JSON" in errors[0]


def test_an_unreadable_path_is_an_error(tmp_path: Path) -> None:
    """A directory where a file was expected. Reported, not raised."""

    grants, errors = hostgrants.load_grants(tmp_path)

    assert grants == {}
    assert "could not read" in errors[0]


# ============================================================
# Taking effect without a restart
# ============================================================


def test_a_grant_added_after_the_first_read_is_picked_up(grants_file: Path) -> None:
    """The entire point of the file: no restart.

    The first call is what makes this a real test -- it populates the cache, so
    a naive implementation that read the file once would pass everything else
    here and fail this.
    """

    assert hostgrants.granted_hosts() == (frozenset(), None)

    write_grants(grants_file, "late.example.com")

    hosts, error = hostgrants.granted_hosts()

    assert hosts == {"late.example.com"}
    assert error is None


def test_an_edit_to_an_existing_file_is_picked_up(grants_file: Path) -> None:
    write_grants(grants_file, "first.example.com")

    assert hostgrants.granted_hosts()[0] == {"first.example.com"}

    write_grants(grants_file, "first.example.com", "second.example.com")

    assert hostgrants.granted_hosts()[0] == {
        "first.example.com",
        "second.example.com",
    }


def test_two_same_length_versions_in_quick_succession(grants_file: Path) -> None:
    """The case that removed the cache this module used to have.

    The cache keyed parsed results on `(path, st_mtime_ns, st_size)`, which
    looks sound and is not: mtime resolution on NTFS here is about a
    millisecond, so two writes of equal length inside one tick share a
    signature. The stale entry then wins -- a revoked host still reading as
    granted, or a fresh grant still reading as refused.

    Written directly rather than through write_grants so nothing between the
    two versions can hide the collision.
    """

    write_grants(grants_file, "aaaa.example.com")

    assert hostgrants.granted_hosts()[0] == {"aaaa.example.com"}

    grants_file.write_text(
        json.dumps({"version": 1, "hosts": ["bbbb.example.com"]}),
        encoding="utf-8",
    )

    assert hostgrants.granted_hosts()[0] == {"bbbb.example.com"}


def test_a_disabled_entry_grants_nothing(grants_file: Path) -> None:
    write_grants(
        grants_file,
        {"host": "parked.example.com", "enable": False, "note": "revoked"},
        "live.example.com",
    )

    assert hostgrants.granted_hosts()[0] == {"live.example.com"}


def test_a_broken_file_fails_closed_and_says_why(grants_file: Path) -> None:
    """No hosts, and a message the operator can act on.

    Failing open would be the dangerous direction; failing closed silently
    would be the confusing one, because an approved host would look like a host
    that was never approved.
    """

    grants_file.write_text("{oops", encoding="utf-8")

    hosts, error = hostgrants.granted_hosts()

    assert hosts == frozenset()
    assert error is not None
    assert "not valid JSON" in error


def test_the_environment_variable_merges_with_the_file(
    grants_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_grants(grants_file, "file.example.com")

    monkeypatch.setenv("WAMCP_EXTRA_DOC_HOSTS", "env.example.com")

    assert hostgrants.granted_hosts()[0] == {
        "file.example.com",
        "env.example.com",
    }


def test_a_session_grant_merges_too_and_does_not_touch_the_file(
    grants_file: Path,
) -> None:
    hostgrants.grant_for_session("session.example.com")

    assert hostgrants.granted_hosts()[0] == {"session.example.com"}
    assert hostgrants.session_grants() == {"session.example.com"}
    assert not grants_file.exists()


# ============================================================
# Persisting a grant
# ============================================================


def test_persist_creates_the_file(grants_file: Path) -> None:
    assert hostgrants.persist_grant("new.example.com", note="asked for it") is None

    assert hostgrants.granted_hosts()[0] == {"new.example.com"}

    data = json.loads(grants_file.read_text(encoding="utf-8"))

    assert data["version"] == hostgrants.SCHEMA_VERSION
    assert data["hosts"] == [
        {"host": "new.example.com", "enable": True, "note": "asked for it"}
    ]


def test_persist_keeps_unrelated_content(grants_file: Path) -> None:
    """An operator's comment key survives being written through."""

    grants_file.write_text(
        json.dumps(
            {
                "version": 1,
                "comment": "hand-written, do not lose me",
                "hosts": ["old.example.com"],
            }
        ),
        encoding="utf-8",
    )

    assert hostgrants.persist_grant("new.example.com") is None

    data = json.loads(grants_file.read_text(encoding="utf-8"))

    assert data["comment"] == "hand-written, do not lose me"
    assert {entry["host"] for entry in data["hosts"]} == {
        "old.example.com",
        "new.example.com",
    }


def test_persist_is_idempotent(grants_file: Path) -> None:
    assert hostgrants.persist_grant("new.example.com", note="first") is None
    assert hostgrants.persist_grant("new.example.com", note="second") is None

    data = json.loads(grants_file.read_text(encoding="utf-8"))

    assert len(data["hosts"]) == 1
    # The original note survives: re-approving is not a reason to overwrite
    # what the operator wrote.
    assert data["hosts"][0]["note"] == "first"


def test_persist_refuses_to_rewrite_a_file_it_could_not_parse(
    grants_file: Path,
) -> None:
    """Rewriting it would discard entries the operator meant to keep."""

    grants_file.write_text("{broken", encoding="utf-8")

    error = hostgrants.persist_grant("new.example.com")

    assert error is not None
    assert "not modified" in error
    assert grants_file.read_text(encoding="utf-8") == "{broken"


def test_persist_rejects_a_bad_host_without_writing(grants_file: Path) -> None:
    error = hostgrants.persist_grant("*.example.com")

    assert error is not None
    assert "wildcard" in error
    assert not grants_file.exists()


def test_persist_leaves_no_temporary_file_behind(grants_file: Path) -> None:
    assert hostgrants.persist_grant("new.example.com") is None

    leftovers = [
        path.name for path in grants_file.parent.iterdir() if path.suffix == ".tmp"
    ]

    assert leftovers == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("no", False),
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
    ],
)
def test_persist_is_off_unless_the_operator_says_otherwise(
    value: str | None, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default off, because an accepted elicitation may have been auto-answered.

    The MCP spec lets an agentic client answer on the user's behalf. A
    session-scoped grant that was auto-approved dies with the process; a
    persisted one would not.
    """

    if value is not None:
        monkeypatch.setenv("WAMCP_HOST_GRANT_PERSIST", value)

    assert hostgrants.persist_enabled() is expected


# ============================================================
# The model cannot grant itself anything
# ============================================================


@pytest.mark.parametrize(
    "filename",
    [
        "mcp-allowed-hosts.json",
        "MCP-ALLOWED-HOSTS.JSON",
    ],
)
def test_write_file_refuses_the_trust_files(
    filename: str, writable_project: Path
) -> None:
    """Inside a writable root, and still refused.

    This is the load-bearing test of the whole feature. find_grants_file()
    prefers the current directory, so a model that could CREATE this file
    where none existed would grant itself every host -- and a path-equality
    check would not stop it, because the file does not exist yet.
    """

    payload = assert_error_response(
        write_file(str(writable_project / filename), "{}"),
        "PROTECTED_PATH",
    )

    assert "server configuration" in payload["error"]["message"]
    assert not (writable_project / filename).exists()


def test_edit_file_refuses_the_trust_files(writable_project: Path) -> None:
    """The same guard, through the other writing tool.

    Checked separately because a guard applied in one tool and not the other is
    a plausible mistake, and edit_file could otherwise modify a file the
    operator had created.
    """

    target = writable_project / "mcp-allowed-hosts.json"
    target.write_text('{"version": 1, "hosts": []}', encoding="utf-8")

    assert_error_response(
        edit_file(str(target), "[]", '["evil.example.com"]'),
        "PROTECTED_PATH",
    )

    assert "evil.example.com" not in target.read_text(encoding="utf-8")


def test_an_ordinary_json_file_is_still_writable(writable_project: Path) -> None:
    """Regression guard: the rule is by name, so it must not catch everything."""

    target = writable_project / "settings.json"

    write_file(str(target), "{}")

    assert target.exists()


def test_the_protected_names_match_the_modules_that_own_them() -> None:
    """utils holds the name; hostgrants holds the format.

    The constant lives in utils because the write guard cannot import
    hostgrants the other way round. That split is only safe while the names
    agree.
    """

    assert PROTECTED_CONFIG_FILENAMES == {hostgrants.DEFAULT_GRANTS_FILENAME}


# ============================================================
# What a grant actually unblocks
# ============================================================


def test_readable_hosts_is_the_documentation_list_plus_grants(
    grants_file: Path,
) -> None:
    write_grants(grants_file, "extra.example.com")

    hosts, error = readable_web_hosts()

    assert error is None
    assert ALLOWED_DOC_HOSTS <= hosts
    assert "extra.example.com" in hosts


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Already readable, so a grant is not what is missing.
        ("https://cmake.org/documentation/", None),
        # Refused for something a grant cannot fix.
        ("http://example.com/", None),
        ("https://example.com:8443/", None),
        ("https://user:pw@example.com/", None),
        ("not a url", None),
        # The only case worth asking about.
        ("https://unknown.example.com/page", "unknown.example.com"),
        ("https://UNKNOWN.example.com./page", "unknown.example.com"),
    ],
)
def test_host_grant_would_help_is_precise(url: str, expected: str | None) -> None:
    """Consent is only offered where consent is the answer.

    Offering to unblock a private address would be inviting the operator to
    approve an SSRF probe; offering to unblock an http:// URL would be offering
    a permission that changes nothing.
    """

    assert host_grant_would_help(url) == expected


def test_no_grant_is_needed_in_research_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAMCP_WEB_RESEARCH", "1")

    assert host_grant_would_help("https://unknown.example.com/") is None


def test_a_granted_host_no_longer_needs_a_grant(grants_file: Path) -> None:
    assert host_grant_would_help("https://granted.example.com/") == (
        "granted.example.com"
    )

    write_grants(grants_file, "granted.example.com")

    assert host_grant_would_help("https://granted.example.com/") is None


def test_the_fetcher_reads_a_granted_host(
    grants_file: Path, page_opener, resolves_public: None
) -> None:
    """End to end through read_web_page, with the wire stubbed."""

    clear_cache()
    write_grants(grants_file, "granted.example.com")

    page_opener(
        FakeResponse(
            b"<html><head><title>Granted</title></head>"
            b"<body><p>Hello</p></body></html>",
            {"Content-Type": "text/html"},
        )
    )

    result = read_web_page("https://granted.example.com/page")

    assert "Granted" in result
    assert "Hello" in result


def test_the_fetcher_still_refuses_an_ungranted_host(
    grants_file: Path, resolves_public: None
) -> None:
    clear_cache()
    write_grants(grants_file, "granted.example.com")

    assert_error_response(
        read_web_page("https://other.example.com/page"),
        "URL_NOT_ALLOWED",
    )


def test_a_broken_grants_file_is_reported_in_the_refusal(grants_file: Path) -> None:
    """Otherwise the operator has no reason to suspect their own file."""

    grants_file.write_text("{broken", encoding="utf-8")
    clear_cache()

    payload = assert_error_response(
        read_web_page("https://other.example.com/page"),
        "URL_NOT_ALLOWED",
    )

    recovery = " ".join(payload["error"]["recovery"])

    assert "granted-hosts file has a problem" in recovery


def test_redirects_into_a_granted_host_are_followed(grants_file: Path) -> None:
    """A grant that broke on the first redirect would be nearly useless.

    Documentation sites redirect constantly (locale prefixes, versioned paths),
    so the redirect handler has to consult the same set as the fetcher -- and
    it has to do so per hop, because the openers are built once at import and
    the grants file changes afterwards.
    """

    assert "granted.example.com" not in DocRedirectHandler.allowed_extra_hosts()

    write_grants(grants_file, "granted.example.com")

    assert "granted.example.com" in DocRedirectHandler.allowed_extra_hosts()
    assert ALLOWED_DOC_HOSTS <= DocRedirectHandler.allowed_extra_hosts()


# ============================================================
# Reaching the server at all
# ============================================================


def test_every_setting_the_server_reads_is_forwarded_to_the_inspector() -> None:
    """The invariant that would have caught the original --dev bug.

    The MCP stdio client spawns servers with a fixed environment allowlist, so
    a variable absent from FORWARDED_ENV_VARS silently does nothing under
    --dev. Every failure of that kind looks like a plausible default rather
    than a bug, which is why it survived so long the first time.
    """

    import re

    source_root = Path(__file__).resolve().parents[1] / "src"

    referenced: set[str] = set()

    for path in source_root.rglob("*.py"):
        referenced |= set(re.findall(r'"(WAMCP_[A-Z_]+)"', path.read_text("utf-8")))

    missing = sorted(referenced - set(FORWARDED_ENV_VARS))

    assert missing == [], (
        f"{missing} are read by the server but never forwarded, so they "
        f"silently do nothing under run_server.bat --dev. Add them to "
        f"FORWARDED_ENV_VARS in inspector_config.py."
    )


def test_a_profile_may_carry_a_host_grant() -> None:
    """The config file validates setting names, so the host ones must be known."""

    from windows_agent_mcp.config import parse_config

    config, errors = parse_config(
        {
            "version": 1,
            "profiles": {
                "docs": {
                    "tools": "core,docs",
                    "settings": {"WAMCP_EXTRA_DOC_HOSTS": "docs.example.com"},
                }
            },
        }
    )

    assert errors == []
    assert config.profiles["docs"].settings["WAMCP_EXTRA_DOC_HOSTS"] == (
        "docs.example.com"
    )


# ============================================================
# Finding the file
# ============================================================


def test_the_environment_variable_wins(tmp_path, monkeypatch) -> None:
    target = tmp_path / "elsewhere.json"

    monkeypatch.setenv("WAMCP_ALLOWED_HOSTS_FILE", str(target))

    assert hostgrants.find_grants_file() == target


def test_a_file_in_the_working_directory_is_used(tmp_path, monkeypatch) -> None:
    """The launcher pushd's to the repository root, so this is the normal case."""

    monkeypatch.delenv("WAMCP_ALLOWED_HOSTS_FILE", raising=False)
    monkeypatch.chdir(tmp_path)

    local = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME
    local.write_text('{"version": 1, "hosts": []}', encoding="utf-8")

    assert hostgrants.find_grants_file() == local


def test_the_repository_root_is_the_fallback(tmp_path, monkeypatch) -> None:
    """Only for an editable checkout, which pyproject.toml confirms.

    In a site-packages install the inferred path is meaningless, so it must not
    be used -- otherwise the server would look for its trust file somewhere
    inside the virtual environment.
    """

    monkeypatch.delenv("WAMCP_ALLOWED_HOSTS_FILE", raising=False)
    monkeypatch.chdir(tmp_path)

    found = hostgrants.find_grants_file()

    assert found.name == hostgrants.DEFAULT_GRANTS_FILENAME
    assert (found.parent / "pyproject.toml").is_file()


# ============================================================
# The operator CLI
# ============================================================


def test_init_writes_a_file_that_loads(tmp_path) -> None:
    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME

    assert hostgrants.main(["--file", str(target), "--init"]) == 0

    grants, errors = hostgrants.load_grants(target)

    assert errors == []
    # The template entry is disabled, so scaffolding grants nothing. A starter
    # file that pre-approved hosts would be granting access nobody chose.
    assert all(not grant.enable for grant in grants.values())


def test_init_refuses_to_clobber(tmp_path, capsys) -> None:
    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME
    target.write_text("keep me", encoding="utf-8")

    assert hostgrants.main(["--file", str(target), "--init"]) == 1
    assert "--force" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "keep me"


def test_init_force_overwrites(tmp_path) -> None:
    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME
    target.write_text("old", encoding="utf-8")

    assert hostgrants.main(["--file", str(target), "--init", "--force"]) == 0
    assert "hosts" in target.read_text(encoding="utf-8")


def test_init_reports_an_unwritable_path(tmp_path, monkeypatch, capsys) -> None:
    def denied(self, *args: object, **kwargs: object):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "write_text", denied)

    assert hostgrants.main(["--file", str(tmp_path / "x.json"), "--init"]) == 1
    assert "could not write" in capsys.readouterr().err


def test_add_grants_a_host(tmp_path, capsys) -> None:
    """The one command the refusal message tells the user to run."""

    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME

    code = hostgrants.main(
        ["--file", str(target), "--add", "docs.example.com", "--note", "why"]
    )

    assert code == 0
    assert "granted docs.example.com" in capsys.readouterr().out

    grants, errors = hostgrants.load_grants(target)

    assert errors == []
    assert grants["docs.example.com"].note == "why"


def test_add_rejects_a_pasted_url(tmp_path, capsys) -> None:
    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME

    code = hostgrants.main(
        ["--file", str(target), "--add", "https://docs.example.com/page"]
    )

    assert code == 1

    error = capsys.readouterr().err

    assert "looks like a URL" in error
    assert "docs.example.com" in error
    assert not target.exists()


def test_remove_revokes_a_host(tmp_path, capsys) -> None:
    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME

    hostgrants.main(["--file", str(target), "--add", "a.example.com"])
    hostgrants.main(["--file", str(target), "--add", "b.example.com"])
    capsys.readouterr()

    assert hostgrants.main(["--file", str(target), "--remove", "a.example.com"]) == 0
    assert "revoked a.example.com" in capsys.readouterr().out

    grants, _errors = hostgrants.load_grants(target)

    assert set(grants) == {"b.example.com"}


def test_remove_reports_a_host_that_was_never_granted(tmp_path, capsys) -> None:
    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME
    target.write_text('{"version": 1, "hosts": []}', encoding="utf-8")

    assert hostgrants.main(["--file", str(target), "--remove", "gone.example.com"]) == 1
    assert "is not in" in capsys.readouterr().err


def test_remove_rejects_a_bad_hostname(tmp_path, capsys) -> None:
    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME
    target.write_text('{"version": 1, "hosts": []}', encoding="utf-8")

    assert hostgrants.main(["--file", str(target), "--remove", "*.example.com"]) == 1
    assert "wildcard" in capsys.readouterr().err


def test_remove_refuses_to_rewrite_a_broken_file(tmp_path, capsys) -> None:
    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME
    target.write_text("{broken", encoding="utf-8")

    assert hostgrants.main(["--file", str(target), "--remove", "a.example.com"]) == 1
    assert target.read_text(encoding="utf-8") == "{broken"


def test_list_is_the_default_action(tmp_path, capsys) -> None:
    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME
    write_grants(
        target,
        {"host": "live.example.com", "enable": True, "note": "in use"},
        {"host": "parked.example.com", "enable": False, "note": "revoked"},
    )

    assert hostgrants.main(["--file", str(target)]) == 0

    out = capsys.readouterr().out

    assert "live.example.com" in out
    assert "enabled" in out
    assert "parked.example.com" in out
    assert "DISABLED" in out
    assert "in use" in out


def test_list_says_so_when_there_is_no_file(tmp_path, capsys) -> None:
    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME

    assert hostgrants.main(["--file", str(target), "--list"]) == 0

    out = capsys.readouterr().out

    assert "no file yet" in out
    assert "no granted hosts" in out


def test_list_shows_hosts_from_the_environment(tmp_path, capsys, monkeypatch) -> None:
    """Otherwise a host granted through a client env block looks unaccounted for."""

    monkeypatch.setenv("WAMCP_EXTRA_DOC_HOSTS", "env.example.com")

    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME

    assert hostgrants.main(["--file", str(target), "--list"]) == 0

    out = capsys.readouterr().out

    assert "env.example.com" in out
    assert "WAMCP_EXTRA_DOC_HOSTS" in out


def test_list_reports_a_bad_environment_entry(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("WAMCP_EXTRA_DOC_HOSTS", "*.example.com")

    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME
    target.write_text('{"version": 1, "hosts": []}', encoding="utf-8")

    assert hostgrants.main(["--file", str(target), "--list"]) == 0
    assert "wildcard" in capsys.readouterr().err


def test_the_cli_exits_nonzero_on_a_broken_file(tmp_path, capsys) -> None:
    """So a script wiring this up notices."""

    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME
    target.write_text('{"version": 99, "hosts": []}', encoding="utf-8")

    assert hostgrants.main(["--file", str(target)]) == 1
    assert "unsupported version" in capsys.readouterr().err


def test_the_scaffold_is_valid_input_to_the_parser() -> None:
    grants, errors = hostgrants.parse_grants(hostgrants.scaffold())

    assert errors == []
    assert grants


# ============================================================
# Failure paths that must not raise
# ============================================================


def test_a_bare_dot_is_not_a_hostname() -> None:
    """The root label alone. Survives every earlier check and means nothing."""

    with pytest.raises(ValueError, match="is not a hostname"):
        hostgrants.normalise_host(".")


def test_an_unstattable_path_is_reported_not_raised(grants_file, monkeypatch) -> None:
    """A grants file the process is not permitted to look at.

    Realistic on Windows, where a file can exist with an ACL that denies the
    server. Any exception escaping here would surface as a fetch that crashed
    rather than one that was refused.

    Note what is NOT this case: a path that simply does not exist. That is
    reported as FileNotFoundError, and "no grants" is the correct, silent
    answer to it.
    """

    def denied(self, *args: object, **kwargs: object):
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "read_text", denied)

    hosts, error = hostgrants.granted_hosts()

    assert hosts == frozenset()
    assert error is not None
    assert "could not read" in error


def test_every_version_is_seen_however_fast_they_are_written(
    grants_file: Path,
) -> None:
    """Twelve rewrites in a tight loop, every one observed.

    This is the test that found the cache bug, so it stays. Several of these
    writes land inside a single filesystem clock tick with identical file
    length, which is exactly the collision the old stat-signature cache could
    not distinguish -- it failed here intermittently, which is the worst way for
    a trust-file bug to present.
    """

    for index in range(12):
        grants_file.write_text(
            json.dumps({"version": 1, "hosts": [f"host{index}.example.com"]}),
            encoding="utf-8",
        )

        assert hostgrants.granted_hosts()[0] == {f"host{index}.example.com"}


def test_persist_reports_a_failed_temporary_file(grants_file, monkeypatch) -> None:
    def denied(*args: object, **kwargs: object):
        raise PermissionError("no space")

    monkeypatch.setattr(hostgrants.tempfile, "mkstemp", denied)

    error = hostgrants.persist_grant("new.example.com")

    assert error is not None
    assert "could not write" in error


def test_persist_reports_a_failed_replace(grants_file, monkeypatch) -> None:
    """The rename is the atomic step, so its failure is the one that matters."""

    def denied(*args: object, **kwargs: object):
        raise OSError("cross-device link")

    monkeypatch.setattr(hostgrants.os, "replace", denied)

    error = hostgrants.persist_grant("new.example.com")

    assert error is not None
    assert "could not write" in error


def test_persist_cleans_up_after_an_interrupt(grants_file, monkeypatch) -> None:
    """Ctrl+C mid-write must not leave a .tmp beside the trust file.

    KeyboardInterrupt is not an OSError, so it needs the BaseException handler
    rather than the one above.
    """

    def interrupted(*args: object, **kwargs: object):
        raise KeyboardInterrupt

    monkeypatch.setattr(hostgrants.os, "replace", interrupted)

    with pytest.raises(KeyboardInterrupt):
        hostgrants.persist_grant("new.example.com")

    leftovers = [path for path in grants_file.parent.iterdir() if ".tmp" in path.name]

    assert leftovers == []


def test_remove_reports_a_failed_write(tmp_path, monkeypatch, capsys) -> None:
    target = tmp_path / hostgrants.DEFAULT_GRANTS_FILENAME
    write_grants(target, "a.example.com")

    def denied(self, *args: object, **kwargs: object):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "write_text", denied)

    assert hostgrants.main(["--file", str(target), "--remove", "a.example.com"]) == 1
    assert "could not write" in capsys.readouterr().err


def test_a_site_packages_install_does_not_invent_a_repository_path(
    tmp_path, monkeypatch
) -> None:
    """Without pyproject.toml the inferred root is meaningless.

    Falling back to it would send the server looking for its trust file inside
    the virtual environment, where nobody would think to put one.
    """

    monkeypatch.delenv("WAMCP_ALLOWED_HOSTS_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "is_file", lambda self: False)

    assert hostgrants.find_grants_file() == tmp_path / (
        hostgrants.DEFAULT_GRANTS_FILENAME
    )


# ============================================================
# A redirect the allowlist will not follow
# ============================================================
#
# Found by driving the real server against a real site, not by any unit test
# here: www.gamedev.net was granted, it redirected to its apex domain
# gamedev.net, and that hop was refused -- correctly, since those are different
# hostnames. The refusal surfaced as PAGE_FETCH_FAILED, whose recovery text
# told the operator to check their network connection about a request their own
# allowlist had blocked.


def refused_hop() -> RedirectNotAllowedError:
    """The policy error the redirect handler raises, for FakeOpener to raise."""

    return RedirectNotAllowedError(
        "https://elsewhere.example.com/x",
        "Domain 'elsewhere.example.com' is not allowed.",
    )


def test_a_refused_redirect_is_a_refusal_not_a_network_failure(
    grants_file: Path, page_opener, resolves_public: None
) -> None:
    """The error type decides how the model reacts.

    PAGE_FETCH_FAILED reads as "try again later"; URL_NOT_ALLOWED reads as "ask
    someone". Only one of those is true here.
    """

    clear_cache()
    write_grants(grants_file, "granted.example.com")

    page_opener(refused_hop())

    payload = assert_error_response(
        read_web_page("https://granted.example.com/x"),
        "URL_NOT_ALLOWED",
    )

    recovery = " ".join(payload["error"]["recovery"])

    assert "network access" not in recovery


def test_a_refused_redirect_names_the_target_not_the_request(
    grants_file: Path, page_opener, resolves_public: None
) -> None:
    """The host needing a grant is the one the site redirected TO.

    Naming the requested URL would send the operator to grant a host that is
    already granted, which is the most confusing possible outcome.
    """

    clear_cache()
    write_grants(grants_file, "granted.example.com")

    page_opener(refused_hop())

    payload = assert_error_response(
        read_web_page("https://granted.example.com/x"),
        "URL_NOT_ALLOWED",
    )

    recovery = " ".join(payload["error"]["recovery"])

    assert "hostgrants --add elsewhere.example.com" in recovery
    assert "hostgrants --add granted.example.com" not in recovery
    assert "redirected to" in recovery


def test_a_refused_redirect_explains_the_www_apex_split(
    grants_file: Path, page_opener, resolves_public: None
) -> None:
    """The case that actually happens, and reads like a bug until explained.

    ALLOWED_DOC_HOSTS lists both `khronos.org` and `www.khronos.org` for this
    exact reason. Widening a grant to cover both spellings automatically would
    be the same silent broadening this module refuses for wildcards, so the
    error explains the split instead.
    """

    clear_cache()
    write_grants(grants_file, "www.example.com")

    page_opener(
        RedirectNotAllowedError(
            "https://example.com/",
            "Domain 'example.com' is not allowed.",
        )
    )

    payload = assert_error_response(
        read_web_page("https://www.example.com/"),
        "URL_NOT_ALLOWED",
    )

    recovery = " ".join(payload["error"]["recovery"])

    assert "hostgrants --add example.com" in recovery
    # Names the other spelling, so the operator can see both are needed.
    assert "'www.example.com'" in recovery


def test_a_refused_redirect_does_not_advertise_grants_in_research_mode(
    page_opener, resolves_public: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In research mode a grant is meaningless: no host is allowlisted.

    A hop refused there failed the scheme, port or address checks, so telling
    the operator to grant the host would be advice that cannot work.
    """

    monkeypatch.setenv("WAMCP_WEB_RESEARCH", "1")

    clear_cache()

    page_opener(
        RedirectNotAllowedError(
            "https://10.0.0.1/",
            "Blocked network address for '10.0.0.1': 10.0.0.1",
        )
    )

    payload = assert_error_response(
        read_web_page("https://anything.example.com/"),
        "URL_NOT_ALLOWED",
    )

    recovery = " ".join(payload["error"]["recovery"])

    assert "hostgrants --add" not in recovery
    assert "redirected to" in recovery


def test_the_redirect_handler_raises_the_typed_error(grants_file: Path) -> None:
    """The handler is where the distinction has to be made.

    urllib lets the exception out of opener.open() unchanged, so the type is
    the only thing carrying "policy, not network" to the caller.
    """

    handler = DocRedirectHandler()

    with pytest.raises(RedirectNotAllowedError) as caught:
        handler.redirect_request(
            urllib.request.Request("https://cmake.org/"),
            io.BytesIO(b""),
            301,
            "Moved",
            HTTPMessage(),
            "https://not-granted.example.com/",
        )

    assert caught.value.hostname == "not-granted.example.com"
    assert caught.value.url == "https://not-granted.example.com/"
    # Still a ValueError, so every existing `except ValueError` keeps working.
    assert isinstance(caught.value, ValueError)


def test_a_redirect_into_a_granted_host_is_followed(
    grants_file: Path, resolves_public: None
) -> None:
    """The other half: a grant must actually permit the hop.

    The earlier test asserted the handler's host SET contained the grant; this
    one drives redirect_request, which is what urllib calls.
    """

    write_grants(grants_file, "granted.example.com")

    handler = DocRedirectHandler()
    handler.parent = None  # type: ignore[assignment]

    request = handler.redirect_request(
        urllib.request.Request("https://cmake.org/"),
        io.BytesIO(b""),
        301,
        "Moved",
        HTTPMessage(),
        "https://granted.example.com/page",
    )

    assert request is not None
    assert request.full_url == "https://granted.example.com/page"


def test_the_network_allowlist_is_readable_too(resolves_public: None) -> None:
    """github.com is fetchable without any grant, and that is intended.

    Worth pinning because readable_web_hosts() does NOT contain it -- the base
    ALLOWED_NETWORK_HOSTS check inside is_allowed_host() is what admits it. That
    gap misled a reader once, so the behaviour and the reason are recorded here.
    """

    assert "github.com" not in readable_web_hosts()[0]
    assert host_grant_would_help("https://github.com/owner/repo") is None
