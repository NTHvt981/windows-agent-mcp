"""Tests for the remaining reachable branches.

Mostly escape handling inside the quote-aware scanners, which matters because
a backtick can hide a closing quote and so hide a separator behind it.
"""

from __future__ import annotations

import socket
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from windows_agent_mcp.allowed_command import (
    find_command_composition,
    tokenize_command,
)
from windows_agent_mcp.tools.run_powershell import looks_like_base64_payload
from windows_agent_mcp.utils import (
    SafeRedirectHandler,
    default_download_root,
    get_download_root,
    resolve_and_validate_host,
)

# ============================================================
# Escape handling in the quote-aware scanners
# ============================================================


@pytest.mark.parametrize(
    "command",
    [
        # A backtick inside double quotes escapes the next character. If the
        # scanner did not skip it, a backtick-escaped quote would end the
        # string early and hide whatever followed.
        'git commit -m "line`nbreak"',
        'git commit -m "a`"b"',
        # A doubled quote is an escaped literal quote, not the end of the
        # string, so the scanner must stay inside it.
        'git commit -m "say ""hi"""',
    ],
)
def test_escapes_inside_double_quotes_stay_quoted(command: str) -> None:
    assert find_command_composition(command) is None


def test_backtick_escaped_quote_cannot_smuggle_a_separator() -> None:
    """The payload stays inside the quoted string, so nothing is smuggled.

    If the escape were mishandled the scanner would think the string ended at
    the escaped quote and treat the rest as unquoted -- or, worse, miss the
    separator entirely.
    """

    assert find_command_composition('git commit -m "a`"b"') is None

    # An unescaped quote really does end the string, exposing the separator.
    assert find_command_composition('git commit -m "a"; whoami') is not None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # The backtick is consumed and the escaped character kept.
        ('git -m "a`nb"', ["git", "-m", "anb"]),
        ('git -m "a`"b"', ["git", "-m", 'a"b']),
        ("git -m 'a''b'", ["git", "-m", "a'b"]),
    ],
)
def test_tokenizer_handles_escapes(command: str, expected: list[str]) -> None:
    assert tokenize_command(command) == expected


# ============================================================
# Base64 detector guards
# ============================================================


def test_correct_length_but_wrong_alphabet_is_rejected() -> None:
    """Reachable only by direct call: the run scanner never yields these."""

    assert looks_like_base64_payload("!" * 24) is False
    assert looks_like_base64_payload("@" * 28) is False


def test_padding_in_the_middle_is_rejected() -> None:
    assert looks_like_base64_payload("AAAA=AAAAAAAAAAAAAAAAAAA") is False


# ============================================================
# Download root defaults
# ============================================================


def test_default_root_falls_back_without_localappdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOCALAPPDATA is absent off Windows, and the default must still work."""

    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    root = default_download_root()

    assert root == Path.home() / ".local" / "share" / "windows-agent-mcp" / "downloads"


def test_get_download_root_uses_the_default_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WAMCP_DOWNLOAD_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    root = get_download_root()

    assert root == (tmp_path / "windows-agent-mcp" / "downloads").resolve()
    assert root.is_dir()


# ============================================================
# DNS edge case
# ============================================================


def test_host_resolving_to_nothing_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """getaddrinfo can succeed yet return no usable addresses."""

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])

    with pytest.raises(ValueError, match="No IP addresses found"):
        resolve_and_validate_host("github.com")


# ============================================================
# Redirects that ARE allowed get followed
# ============================================================


def test_allowed_redirect_is_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation must not block a legitimate redirect.

    The refusal cases are covered in test_network_validation.py; this is the
    other half -- an allowlisted target is handed to urllib as normal.
    """

    def getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.121.4", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    handler = SafeRedirectHandler()

    request = urllib.request.Request("https://github.com/o/r/releases/latest")

    redirected = handler.redirect_request(
        request,
        None,  # type: ignore[arg-type]
        302,
        "Found",
        Message(),  # type: ignore[arg-type]
        "https://objects.githubusercontent.com/asset.zip",
    )

    assert redirected is not None
    assert redirected.full_url == "https://objects.githubusercontent.com/asset.zip"
