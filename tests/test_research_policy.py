"""Tests for research-mode host policy and the opener split.

Research mode widens exactly one thing -- the host allowlist -- and nothing
else. These tests pin that boundary from both sides: what it newly permits,
and what it must still refuse.
"""

from __future__ import annotations

import socket
import urllib.request
from email.message import Message
from typing import Any

import pytest

from windows_agent_mcp.utils import (
    HTTP_OPENER,
    RESEARCH_HTTP_OPENER,
    SEARCH_HTTP_OPENER,
    NoRedirectHandler,
    ResearchRedirectHandler,
    SafeRedirectHandler,
    is_allowed_host,
    resolve_and_validate_host,
    validate_url,
    web_research_enabled,
)


def _resolve_to(*addresses: str):
    def getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    return getaddrinfo


# ============================================================
# web_research_enabled
# ============================================================


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "On", " 1 "])
def test_research_enabled_for_truthy_values(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAMCP_WEB_RESEARCH", value)

    assert web_research_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_research_disabled_for_everything_else(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAMCP_WEB_RESEARCH", value)

    assert web_research_enabled() is False


def test_research_disabled_when_unset() -> None:
    """The autouse env fixture removes it, so this is the default state."""

    assert web_research_enabled() is False


# ============================================================
# The default path must be byte-identical to before
# ============================================================

# Every URL from the pre-existing accept/reject table. The property under test
# is that adding the keyword arguments changed nothing for existing callers.
_EXISTING_CASES = [
    "https://github.com/user/repo",
    "https://api.github.com/repos/o/r/releases/latest",
    "https://objects.githubusercontent.com/some/asset.zip",
    "https://premake.github.io/download",
    "http://github.com/user/repo",
    "ftp://github.com/x",
    "file:///C:/Windows/win.ini",
    "https://user:password@github.com/x",
    "https://malicious.example.com/x",
    "https://localhost/x",
    "https://127.0.0.1/x",
    "https://github.com.evil.example/x",
    "https://github.com:8080/x",
    "https://github.com:443/x",
]


@pytest.mark.parametrize("url", _EXISTING_CASES)
def test_explicit_default_matches_implicit_default(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("140.82.121.4"))

    def outcome(**kwargs: Any) -> str:
        try:
            return "ok:" + validate_url(url, **kwargs)
        except ValueError as exc:
            return "err:" + str(exc)

    assert outcome() == outcome(allow_any_host=False)


@pytest.mark.parametrize("hostname", ["github.com", "api.github.com"])
def test_is_allowed_host_unchanged_without_extra(hostname: str) -> None:
    assert is_allowed_host(hostname) is True


# ============================================================
# allow_any_host widens the allowlist and NOTHING else
# ============================================================


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.python.org/3/library/urllib.html",
        "https://learn.microsoft.com/en-us/cpp/",
        "https://cmake.org/documentation/",
        "https://stackoverflow.com/questions/123",
    ],
)
def test_research_mode_accepts_any_public_host(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("140.82.121.4"))

    assert validate_url(url, allow_any_host=True) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.python.org/x",
        "https://anything.example/x",
    ],
)
def test_research_mode_is_refused_without_the_flag(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("140.82.121.4"))

    with pytest.raises(ValueError, match="not allowed"):
        validate_url(url)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        # Every non-allowlist check must still fire in research mode.
        ("http://docs.python.org/x", "Only HTTPS"),
        ("ftp://docs.python.org/x", "Only HTTPS"),
        ("https://user:pw@docs.python.org/x", "credentials"),
        ("https://docs.python.org:8080/x", "port 443"),
        ("https://docs.python.org:abc/x", "invalid port"),
        ("https:///no-host", "no hostname"),
    ],
)
def test_research_mode_still_enforces_everything_else(
    url: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("140.82.121.4"))

    with pytest.raises(ValueError, match=message):
        validate_url(url, allow_any_host=True)


def test_research_mode_overlong_url_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("140.82.121.4"))

    with pytest.raises(ValueError, match="too long"):
        validate_url("https://" + "a" * 4100 + ".com", allow_any_host=True)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        # The CGNAT range research mode makes newly reachable.
        "100.64.0.1",
        "::ffff:127.0.0.1",
    ],
)
def test_research_mode_still_blocks_private_addresses(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is the check that matters most once any host is reachable."""

    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to(address))

    with pytest.raises(ValueError, match="Blocked network address"):
        validate_url("https://anything.example/x", allow_any_host=True)


def test_research_mode_blocks_mixed_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", _resolve_to("140.82.121.4", "100.64.0.1")
    )

    with pytest.raises(ValueError, match="Blocked network address"):
        validate_url("https://anything.example/x", allow_any_host=True)


# ============================================================
# extra_allowed_hosts is the narrow knob search uses
# ============================================================


def test_extra_allowed_hosts_permits_just_that_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("140.82.121.4"))

    extra = frozenset({"lite.duckduckgo.com"})

    url = "https://lite.duckduckgo.com/lite/"

    assert validate_url(url, extra_allowed_hosts=extra) == url

    # Anything else remains refused -- this is narrower than allow_any_host.
    with pytest.raises(ValueError, match="not allowed"):
        validate_url("https://example.com/x", extra_allowed_hosts=extra)


def test_extra_allowed_hosts_still_blocks_private_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("127.0.0.1"))

    with pytest.raises(ValueError, match="Blocked network address"):
        resolve_and_validate_host(
            "lite.duckduckgo.com",
            extra_allowed_hosts=frozenset({"lite.duckduckgo.com"}),
        )


def test_is_allowed_host_extra_is_keyword_only() -> None:
    assert is_allowed_host("x.example", extra=frozenset({"x.example"})) is True
    assert is_allowed_host("x.example") is False


# ============================================================
# The opener / redirect-handler split
# ============================================================


def test_the_three_openers_are_distinct() -> None:
    assert HTTP_OPENER is not RESEARCH_HTTP_OPENER
    assert HTTP_OPENER is not SEARCH_HTTP_OPENER
    assert RESEARCH_HTTP_OPENER is not SEARCH_HTTP_OPENER


def test_handler_policy_defaults() -> None:
    assert SafeRedirectHandler.allow_any_host is False
    assert ResearchRedirectHandler.allow_any_host is True


def _redirect(handler: SafeRedirectHandler, newurl: str):
    return handler.redirect_request(
        urllib.request.Request("https://github.com/start"),
        None,  # type: ignore[arg-type]
        302,
        "Found",
        Message(),  # type: ignore[arg-type]
        newurl,
    )


def test_research_handler_follows_an_off_allowlist_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The class attribute is inert unless redirect_request threads it.

    Nothing else in the suite would catch that, which is why this test exists.
    """

    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("140.82.121.4"))

    redirected = _redirect(ResearchRedirectHandler(), "https://example.com/x")

    assert redirected is not None
    assert redirected.full_url == "https://example.com/x"


def test_strict_handler_refuses_the_same_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("140.82.121.4"))

    with pytest.raises(ValueError, match="not allowed"):
        _redirect(SafeRedirectHandler(), "https://example.com/x")


@pytest.mark.parametrize(
    ("newurl", "message"),
    [
        ("http://example.com/x", "Only HTTPS"),
        ("https://example.com:8080/x", "port 443"),
        ("https://user:pw@example.com/x", "credentials"),
    ],
)
def test_research_handler_still_enforces_the_rest(
    newurl: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("140.82.121.4"))

    with pytest.raises(ValueError, match=message):
        _redirect(ResearchRedirectHandler(), newurl)


def test_research_handler_refuses_a_redirect_to_a_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("100.64.0.1"))

    with pytest.raises(ValueError, match="Blocked network address"):
        _redirect(ResearchRedirectHandler(), "https://example.com/x")


def test_no_redirect_handler_refuses_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returning None makes urllib raise HTTPError rather than follow.

    Search needs this: HTTPRedirectHandler drops the request body on a
    301/302/303, turning a POST into a GET with no query, which would parse
    to zero results and be reported as "no results" instead of a redirect.
    """

    monkeypatch.setattr(socket, "getaddrinfo", _resolve_to("140.82.121.4"))

    handler = NoRedirectHandler()

    assert _redirect(handler, "https://example.com/x") is None  # type: ignore[arg-type]


def test_build_opener_accepts_the_handler_classes() -> None:
    """Regression guard for the class-attribute design.

    build_opener instantiates a handler class with ZERO arguments, so this
    breaks the moment someone converts allow_any_host into an __init__ arg.
    """

    for handler in (SafeRedirectHandler, ResearchRedirectHandler, NoRedirectHandler):
        assert urllib.request.build_opener(handler) is not None
