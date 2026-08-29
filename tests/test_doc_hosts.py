"""Tests for the documentation-host grant.

The design claim being tested: fetch_web_page can read reference
documentation without research mode, and that grant is strictly narrower than
research mode in two specific ways --

    1. it does not let anything WRITE those bytes to disk (download_file and
       fetch_https_response keep the six-host network allowlist), and
    2. it does not let a redirect escape to an arbitrary host.

Both are asserted directly, because both are the kind of thing that silently
regresses when someone "simplifies" the opener selection.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from conftest import assert_error_response

from windows_agent_mcp.utils import (
    ALLOWED_DOC_HOSTS,
    ALLOWED_NETWORK_HOSTS,
    DocRedirectHandler,
    ResearchRedirectHandler,
    SafeRedirectHandler,
    is_allowed_host,
    validate_url,
)

DOC_URL = "https://registry.khronos.org/vulkan/specs/latest/man/html/VkFormat.html"
MS_URL = "https://learn.microsoft.com/en-us/windows/win32/direct3d12/"


@pytest.fixture
def resolves_private(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


# ============================================================
# The two sets are separate on purpose
# ============================================================


def test_doc_hosts_are_not_in_the_network_allowlist() -> None:
    """If they overlapped, download_file would gain write access to them."""

    assert ALLOWED_DOC_HOSTS.isdisjoint(ALLOWED_NETWORK_HOSTS)


def test_doc_hosts_are_not_downloadable() -> None:
    """The narrower grant must stay narrow: read into context, never to disk."""

    from windows_agent_mcp.tools.download_file import download_file

    assert_error_response(
        download_file(f"{MS_URL}spec.pdf"),
        "DOWNLOAD_NOT_ALLOWED",
    )


def test_doc_hosts_are_not_fetchable_as_raw_responses() -> None:
    from windows_agent_mcp.tools.fetch_https_response import fetch_https_response

    assert_error_response(fetch_https_response(MS_URL), "URL_NOT_ALLOWED")


def test_no_wildcards_in_the_doc_list() -> None:
    """A wildcard would turn a fixed grant into an open one."""

    for host in ALLOWED_DOC_HOSTS:
        assert "*" not in host
        assert host == host.lower()


# ============================================================
# is_allowed_host / validate_url
# ============================================================


def test_extra_hosts_are_accepted_without_widening_the_default() -> None:
    assert is_allowed_host("registry.khronos.org", extra=ALLOWED_DOC_HOSTS)
    assert not is_allowed_host("registry.khronos.org")


def test_network_allowlist_still_works_when_extras_are_passed() -> None:
    assert is_allowed_host("github.com", extra=ALLOWED_DOC_HOSTS)


def test_doc_url_validates_with_the_extra_grant(resolves_public: None) -> None:
    validate_url(DOC_URL, extra_allowed_hosts=ALLOWED_DOC_HOSTS)


def test_off_list_url_is_refused_even_with_the_extra_grant(
    resolves_public: None,
) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        validate_url("https://example.com/", extra_allowed_hosts=ALLOWED_DOC_HOSTS)


def test_research_mode_is_a_superset(resolves_public: None) -> None:
    """A URL that works by default must never stop working in research mode."""

    validate_url(DOC_URL, allow_any_host=True, extra_allowed_hosts=ALLOWED_DOC_HOSTS)


def test_a_doc_host_resolving_privately_is_still_refused(
    resolves_private: None,
) -> None:
    """The allowlist is not a bypass for SSRF checks."""

    with pytest.raises(ValueError, match="Blocked network address"):
        validate_url(DOC_URL, extra_allowed_hosts=ALLOWED_DOC_HOSTS)


@pytest.mark.parametrize(
    "url",
    [
        "http://learn.microsoft.com/",
        "https://learn.microsoft.com:8443/",
        "https://user:pass@learn.microsoft.com/",
    ],
)
def test_shape_rules_still_apply_to_doc_hosts(url: str) -> None:
    with pytest.raises(ValueError):
        validate_url(url, extra_allowed_hosts=ALLOWED_DOC_HOSTS)


# ============================================================
# Redirect handlers
# ============================================================


def test_doc_handler_carries_the_doc_hosts() -> None:
    assert DocRedirectHandler.extra_allowed_hosts == ALLOWED_DOC_HOSTS
    assert DocRedirectHandler.allow_any_host is False


def test_base_handler_grants_nothing_extra() -> None:
    assert SafeRedirectHandler.extra_allowed_hosts == frozenset()
    assert SafeRedirectHandler.allow_any_host is False


def test_research_handler_allows_any_host() -> None:
    assert ResearchRedirectHandler.allow_any_host is True


def redirect(handler_class: type[SafeRedirectHandler], target: str) -> None:
    """Drive redirect_request the way urllib does, with a real Request."""

    import urllib.request
    from http.client import HTTPMessage

    handler = handler_class()

    handler.redirect_request(
        urllib.request.Request("https://learn.microsoft.com/"),
        None,  # type: ignore[arg-type]
        302,
        "Found",
        HTTPMessage(),
        target,
    )


def test_doc_handler_follows_a_locale_redirect(resolves_public: None) -> None:
    """learn.microsoft.com does this on essentially every request."""

    redirect(DocRedirectHandler, "https://learn.microsoft.com/en-us/windows/")


def test_doc_handler_follows_a_hop_to_another_doc_host(
    resolves_public: None,
) -> None:
    redirect(DocRedirectHandler, DOC_URL)


def test_doc_handler_refuses_a_hop_off_the_list(resolves_public: None) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        redirect(DocRedirectHandler, "https://evil.example/")


def test_doc_handler_refuses_a_hop_to_a_private_address(
    resolves_private: None,
) -> None:
    with pytest.raises(ValueError):
        redirect(DocRedirectHandler, "https://learn.microsoft.com/internal")


def test_research_handler_refuses_a_private_hop(resolves_private: None) -> None:
    """allow_any_host skips the allowlist only, never the SSRF check."""

    with pytest.raises(ValueError, match="Blocked network address"):
        redirect(ResearchRedirectHandler, "https://anything.example/")


def test_handlers_take_no_constructor_arguments() -> None:
    """build_opener instantiates the CLASS with zero args.

    An __init__ parameter here would raise TypeError inside build_opener, so
    the policy has to stay a class attribute. This test is the tripwire for
    anyone "cleaning that up".
    """

    for handler_class in (
        SafeRedirectHandler,
        DocRedirectHandler,
        ResearchRedirectHandler,
    ):
        handler_class()
