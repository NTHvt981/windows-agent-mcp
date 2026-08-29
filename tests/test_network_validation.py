"""Tests for network validation utilities.

DNS is stubbed throughout: these assert the policy, not the state of the
internet, so the suite is deterministic and runs offline.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from windows_agent_mcp.utils import (
    SafeRedirectHandler,
    is_allowed_host,
    is_private_or_special_ip,
    resolve_and_validate_host,
    validate_url,
)


def _fake_getaddrinfo(*addresses: str):
    """Build a getaddrinfo stub that resolves any host to `addresses`."""

    def getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    return getaddrinfo


@pytest.fixture
def resolves_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every DNS lookup return a routable public address."""

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("140.82.121.4"))


# ============================================================
# is_private_or_special_ip
# ============================================================


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",  # cloud instance metadata
        "0.0.0.0",
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        # Carrier-grade NAT (RFC 6598), also the Tailscale range. Reports
        # is_private=False AND is_reserved=False on Python 3.13, so only the
        # is_global term catches it.
        "100.64.0.1",
        "100.127.255.254",
        "::ffff:100.64.0.1",
        "192.0.0.1",  # IETF protocol assignments
        "198.18.0.1",  # benchmarking
        "2002:7f00:1::1",  # 6to4 wrapping 127.0.0.1
        "2001::1",  # Teredo
        # IPv4-mapped IPv6 must not be a way around the IPv4 checks.
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "::ffff:10.0.0.1",
        # Anything unparseable is treated as unsafe.
        "not-an-ip",
        "0177.0.0.1",
        "",
    ],
)
def test_is_private_or_special_ip_blocks(address: str) -> None:
    assert is_private_or_special_ip(address) is True


@pytest.mark.parametrize("address", ["140.82.121.4", "8.8.8.8", "2606:50c0:8000::153"])
def test_is_private_or_special_ip_allows_public(address: str) -> None:
    assert is_private_or_special_ip(address) is False


# ============================================================
# is_allowed_host
# ============================================================


@pytest.mark.parametrize(
    "hostname",
    [
        "github.com",
        "api.github.com",
        "GITHUB.COM",
        "github.com.",
        "raw.githubusercontent.com",
        "premake.github.io",
    ],
)
def test_is_allowed_host_accepts(hostname: str) -> None:
    assert is_allowed_host(hostname) is True


@pytest.mark.parametrize(
    "hostname",
    [
        "localhost",
        "malicious.example.com",
        # Lookalikes must not pass: matching is exact, not suffix-based.
        "github.com.evil.example",
        "evilgithub.com",
        "notapi.github.com",
        "",
    ],
)
def test_is_allowed_host_rejects(hostname: str) -> None:
    assert is_allowed_host(hostname) is False


# ============================================================
# resolve_and_validate_host
# ============================================================


def test_resolve_and_validate_host_accepts_public(resolves_public: None) -> None:
    resolve_and_validate_host("github.com")


def test_resolve_and_validate_host_rejects_unlisted_host(
    resolves_public: None,
) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        resolve_and_validate_host("malicious.example.com")


def test_resolve_and_validate_host_rejects_empty() -> None:
    with pytest.raises(ValueError, match="no hostname"):
        resolve_and_validate_host("")


def test_resolve_and_validate_host_blocks_dns_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An allowlisted host resolving to a private address must be refused."""

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))

    with pytest.raises(ValueError, match="Blocked network address"):
        resolve_and_validate_host("github.com")


def test_resolve_and_validate_host_blocks_mixed_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad address among several is enough to refuse the host."""

    monkeypatch.setattr(
        socket, "getaddrinfo", _fake_getaddrinfo("140.82.121.4", "169.254.169.254")
    )

    with pytest.raises(ValueError, match="Blocked network address"):
        resolve_and_validate_host("github.com")


def test_resolve_and_validate_host_reports_resolution_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)

    with pytest.raises(ValueError, match="Could not resolve"):
        resolve_and_validate_host("github.com")


# ============================================================
# validate_url
# ============================================================


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/user/repo",
        "https://api.github.com/repos/premake/premake-core/releases/latest",
        "https://objects.githubusercontent.com/some/asset.zip",
        "https://premake.github.io/download",
    ],
)
def test_validate_url_accepts(url: str, resolves_public: None) -> None:
    assert validate_url(url) == url


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://github.com/user/repo", "Only HTTPS"),
        ("ftp://github.com/x", "Only HTTPS"),
        ("file:///C:/Windows/win.ini", "Only HTTPS"),
        ("https://user:password@github.com/x", "credentials"),
        ("https://malicious.example.com/x", "not allowed"),
        ("https://localhost/x", "not allowed"),
        ("https://127.0.0.1/x", "not allowed"),
        # Suffix lookalike must not slip through.
        ("https://github.com.evil.example/x", "not allowed"),
        # Only 443 is permitted: the host is resolved against 443 regardless,
        # so any other port validates one service and connects to another.
        ("https://github.com:8080/x", "port 443"),
        ("https://github.com:80/x", "port 443"),
        ("https://github.com:0/x", "port 443"),
        # urlparse raises on these, so the policy message must win over its.
        ("https://github.com:99999/x", "invalid port"),
        ("https://github.com:abc/x", "invalid port"),
    ],
)
def test_validate_url_rejects(url: str, message: str, resolves_public: None) -> None:
    with pytest.raises(ValueError, match=message):
        validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/x",
        # An explicit :443 is the default, so it is allowed.
        "https://github.com:443/x",
        # An empty port component parses to None.
        "https://github.com:/x",
    ],
)
def test_validate_url_accepts_default_port(url: str, resolves_public: None) -> None:
    assert validate_url(url) == url


def test_validate_url_rejects_overlong(resolves_public: None) -> None:
    with pytest.raises(ValueError, match="too long"):
        validate_url("https://" + "a" * 4100 + ".com")


def test_validate_url_rejects_missing_hostname(resolves_public: None) -> None:
    with pytest.raises(ValueError, match="no hostname"):
        validate_url("https:///path/only")


# ============================================================
# SafeRedirectHandler
# ============================================================


def test_redirect_to_disallowed_host_is_refused(resolves_public: None) -> None:
    """A redirect off the allowlist must be rejected before it is followed."""

    handler = SafeRedirectHandler()

    with pytest.raises(ValueError, match="not allowed"):
        handler.redirect_request(
            None, None, 302, "Found", {}, "https://malicious.example.com/x"
        )


def test_redirect_downgrade_to_http_is_refused(resolves_public: None) -> None:
    handler = SafeRedirectHandler()

    with pytest.raises(ValueError, match="Only HTTPS"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://github.com/x")


def test_redirect_to_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))

    handler = SafeRedirectHandler()

    with pytest.raises(ValueError, match="Blocked network address"):
        handler.redirect_request(None, None, 302, "Found", {}, "https://github.com/x")
