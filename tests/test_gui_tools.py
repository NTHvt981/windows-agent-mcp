from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from windows_agent_mcp import windowing
from windows_agent_mcp.main import get_tools
from windows_agent_mcp.tools import (
    capture_window as capture_module,
)
from windows_agent_mcp.tools import (
    list_windows as list_module,
)
from windows_agent_mcp.utils import (
    VALID_TOOL_GROUPS,
    active_tool_groups,
    allowed_gui_processes,
    gui_process_allowed,
    parse_tool_groups,
)
from windows_agent_mcp.windowing import WindowInfo, encode_png

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def make_window(**overrides: object) -> WindowInfo:
    defaults: dict[str, object] = {
        "hwnd": 0x1234,
        "pid": 100,
        "process": "game.exe",
        "title": "Some window",
        "left": 10,
        "top": 20,
        "right": 410,
        "bottom": 220,
        "client_width": 400,
        "client_height": 200,
        "client_x": 10,
        "client_y": 20,
        "minimized": False,
        "foreground": False,
    }

    defaults.update(overrides)

    return WindowInfo(**defaults)  # pyright: ignore[reportArgumentType]


def decode_png(png: bytes) -> tuple[int, int, bytearray]:
    assert png[:8] == PNG_SIGNATURE

    offset = 8

    ihdr: bytes = b""

    scanlines: list[bytes] = []

    while offset < len(png):
        length = struct.unpack(">I", png[offset : offset + 4])[0]
        chunk = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + length]
        offset += 12 + length

        if chunk == b"IHDR":
            ihdr = data
        elif chunk == b"IDAT":
            scanlines.append(data)

    width, height = struct.unpack(">II", ihdr[:8])

    raw = zlib.decompress(b"".join(scanlines))

    stride = width * 3 + 1

    pixels = bytearray()

    for row in range(height):
        assert raw[row * stride] == 0  # filter None
        pixels += raw[row * stride + 1 : (row + 1) * stride]

    return width, height, pixels


def test_parse_tool_groups_all_excludes_gui() -> None:
    groups, error = parse_tool_groups("all")

    assert error is None

    assert "gui" not in groups
    assert groups == frozenset(VALID_TOOL_GROUPS) - {"gui"}


def test_active_tool_groups_gui_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAMCP_GUI", "1")
    monkeypatch.setenv("WAMCP_TOOLS", "core")

    groups, error = active_tool_groups()

    assert error is None
    assert "gui" in groups

    monkeypatch.delenv("WAMCP_GUI")
    monkeypatch.setenv("WAMCP_TOOLS", "gui")

    groups, error = active_tool_groups()

    assert "gui" not in groups
    assert error is not None
    assert "WAMCP_GUI" in error


def test_get_tools_group_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAMCP_GUI", "1")

    with_gui = {tool.__name__ for tool in get_tools(groups=frozenset({"core", "gui"}))}

    without_gui = {tool.__name__ for tool in get_tools(groups=frozenset({"core"}))}

    assert {"list_windows", "capture_window"} == with_gui - without_gui


def test_allowed_gui_processes_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WAMCP_GUI_PROCESSES", raising=False)

    assert allowed_gui_processes() == frozenset()
    assert gui_process_allowed("game.exe") is False

    monkeypatch.setenv(
        "WAMCP_GUI_PROCESSES",
        "Game.exe; other.exe, C:\\Tools\\third.exe",
    )

    assert allowed_gui_processes() == frozenset({"game.exe", "other.exe", "third.exe"})

    assert gui_process_allowed("GAME.EXE") is True
    assert gui_process_allowed("fourth.exe") is False


def test_list_windows_reports_capture_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAMCP_GUI", "1")
    monkeypatch.setenv("WAMCP_GUI_PROCESSES", "game.exe")

    monkeypatch.setattr(
        windowing,
        "list_visible_windows",
        lambda limit=100: [make_window()],
    )

    result = list_module.list_windows()

    assert '"capture_allowed": true' in result
    assert '"process": "game.exe"' in result


def test_list_windows_refuses_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WAMCP_GUI", raising=False)
    monkeypatch.delenv("WAMCP_TOOLS", raising=False)

    result = list_module.list_windows()

    assert "GUI_DISABLED" in result
    assert '"ok": false' in result


def test_encode_png_roundtrip() -> None:
    png = encode_png(2, 2, b"\xff\x00\x00" * 4)

    assert png.startswith(PNG_SIGNATURE)

    width, height, pixels = decode_png(png)

    assert (width, height) == (2, 2)
    assert list(pixels) == [255, 0, 0] * 4


def test_capture_window_writes_png(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAMCP_GUI", "1")
    monkeypatch.setenv("WAMCP_GUI_PROCESSES", "game.exe")
    monkeypatch.setenv("WAMCP_DOWNLOAD_ROOT", str(tmp_path))

    info = make_window()

    calls: list[str] = []

    monkeypatch.setattr(
        windowing,
        "find_window",
        lambda selector: calls.append(selector) or info,
    )

    monkeypatch.setattr(
        windowing,
        "capture_window_png",
        lambda target: (encode_png(2, 2, b"\xff\x00\x00" * 4), 2, 2),
    )

    result = capture_module.capture_window("game.exe")

    assert '"ok": true' in result
    assert '"png": true' in result

    import json

    payload = json.loads(result)

    path = Path(payload["path"])

    assert path.parent == tmp_path / "captures"

    assert path.read_bytes().startswith(PNG_SIGNATURE)

    assert not list(path.parent.glob("*.part"))

    assert calls == ["game.exe"]


def test_capture_window_refuses_unlisted_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAMCP_GUI", "1")
    monkeypatch.delenv("WAMCP_GUI_PROCESSES", raising=False)

    def fail_capture(_target: object) -> tuple[bytes, int, int]:
        raise AssertionError("capture must not run for an unlisted process")

    monkeypatch.setattr(windowing, "find_window", lambda _s: make_window())

    monkeypatch.setattr(windowing, "capture_window_png", fail_capture)

    result = capture_module.capture_window("secret.exe")

    assert "PROCESS_NOT_ALLOWED" in result
    assert '"capture must not run' not in result


def test_capture_window_minimized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAMCP_GUI", "1")
    monkeypatch.setenv("WAMCP_GUI_PROCESSES", "game.exe")
    monkeypatch.setenv("WAMCP_DOWNLOAD_ROOT", str(tmp_path))

    opened: list[object] = []

    monkeypatch.setattr(
        windowing,
        "find_window",
        lambda selector: (
            make_window(minimized=False) if opened else make_window(minimized=True)
        ),
    )

    def fake_restore(target: object) -> None:
        opened.append(target)

    monkeypatch.setattr(windowing, "restore_window", fake_restore)

    # The settle wait exists for a real window; a test needs no wall clock.
    monkeypatch.setattr(capture_module, "_RESTORE_SETTLE_SECONDS", 0.0)

    without_restore = capture_module.capture_window("game.exe")

    assert "WINDOW_MINIMIZED" in without_restore
    assert not opened

    def fake_capture(_target: object) -> tuple[bytes, int, int]:
        return encode_png(2, 2, b"\xff\x00\x00" * 4), 2, 2

    monkeypatch.setattr(windowing, "capture_window_png", fake_capture)

    with_restore = capture_module.capture_window("game.exe", restore=True)

    assert '"ok": true' in with_restore
    assert opened


def test_capture_window_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAMCP_GUI", "1")

    monkeypatch.setattr(windowing, "WINDOWS_AVAILABLE", False)

    with pytest.raises(windowing.WindowError) as raised:
        windowing.list_visible_windows()

    assert raised.value.code == "UNSUPPORTED_PLATFORM"


def test_capture_window_empty_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAMCP_GUI", "1")

    result = capture_module.capture_window("   ")

    assert "INVALID_ARGUMENT" in result
