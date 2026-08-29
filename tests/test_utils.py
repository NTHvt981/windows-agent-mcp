"""Tests for utility functions."""

from __future__ import annotations

from pathlib import Path

from windows_agent_mcp.utils import (  # type: ignore[import-untyped]
    ALLOWED_NETWORK_HOSTS,
    default_download_root,
    get_download_root,
    is_allowed_host,
    is_private_or_special_ip,
)


def test_get_download_root_honours_env_override(tmp_path, monkeypatch):
    """BIONIC_DOWNLOAD_ROOT should decide the download root, and be created."""

    target = tmp_path / "custom"

    monkeypatch.setenv("BIONIC_DOWNLOAD_ROOT", str(target))

    root = get_download_root()

    assert root == target.resolve()
    assert root.is_dir()


def test_get_download_root_default_is_not_machine_specific(monkeypatch):
    """Without an override the default must be derived from the environment.

    Regression guard: the default used to be a hardcoded path under another
    user's profile, which raised PermissionError on every other machine.
    """

    monkeypatch.delenv("BIONIC_DOWNLOAD_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))

    root = default_download_root()

    assert root.name == "downloads"
    assert "windows-agent-mcp" in root.parts
    assert Path.home() in root.parents


def test_get_download_root_exists(tmp_path, monkeypatch):
    """Test that download root always exists after get_download_root() is called."""

    monkeypatch.setenv("BIONIC_DOWNLOAD_ROOT", str(tmp_path / "dl"))

    root = get_download_root()

    assert root.exists(), "Download root should exist"


def test_is_private_or_special_ip_loopback():
    """Test that loopback IPs are detected as special."""

    assert is_private_or_special_ip("127.0.0.1") is True
    assert is_private_or_special_ip("::1") is True


def test_is_private_or_special_ip_private():
    """Test that private network IPs are detected."""

    assert is_private_or_special_ip("192.168.1.1") is True
    assert is_private_or_special_ip("10.0.0.1") is True
    assert is_private_or_special_ip("172.16.0.1") is True


def test_is_allowed_host():
    """Test that allowed hosts are recognized."""

    assert is_allowed_host("github.com") is True
    assert is_allowed_host("api.github.com") is True
    assert is_allowed_host("GITHUB.COM") is True  # Case insensitive


def test_not_allowed_host():
    """Test that non-allowed hosts are rejected."""

    assert is_allowed_host("malicious.example.com") is False
    assert is_allowed_host("localhost") is False


def test_allowed_network_hosts_set():
    """Verify the allowlist contains expected entries."""

    assert "github.com" in ALLOWED_NETWORK_HOSTS
    assert "api.github.com" in ALLOWED_NETWORK_HOSTS
    assert "raw.githubusercontent.com" in ALLOWED_NETWORK_HOSTS
