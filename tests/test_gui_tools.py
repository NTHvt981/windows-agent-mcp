from __future__ import annotations

import json
import struct
import time
import zlib
from pathlib import Path
from typing import Any

import pytest

from windows_agent_mcp import windowing
from windows_agent_mcp.main import get_tools
from windows_agent_mcp.tools import (
    capture_window as capture_module,
)
from windows_agent_mcp.tools import (
    list_windows as list_module,
)
from windows_agent_mcp.tools import (
    send_input as send_module,
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

    assert {
        "list_windows",
        "capture_window",
        "send_input",
    } == with_gui - without_gui


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


# ---- send_input: everything below runs against seams; no real input, no
# ---- real focus, no real process is ever touched.


def solid_frame(
    width: int, height: int, rgb: bytes = b"\xff\x00\x00"
) -> tuple[bytes, int, int]:
    return rgb * (width * height), width, height


def edited_frame(
    frame: tuple[bytes, int, int], pixel_index: int
) -> tuple[bytes, int, int]:
    rgb, width, height = frame

    buffer = bytearray(rgb)

    offset = pixel_index * 3

    buffer[offset] = (buffer[offset] + 40) % 256
    buffer[offset + 1] = (buffer[offset + 1] + 40) % 256
    buffer[offset + 2] = (buffer[offset + 2] + 40) % 256

    return bytes(buffer), width, height


def install_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    info: WindowInfo,
    frames: list[tuple[bytes, int, int]],
    *,
    foreground_sequence: list[int] | None = None,
    focus_result: tuple[bool, str] | None = None,
) -> dict[str, list[Any]]:
    """Replace every windowing seam send_input touches; return the record."""

    monkeypatch.setenv("WAMCP_GUI", "1")
    monkeypatch.setenv("WAMCP_GUI_INPUT", "1")
    monkeypatch.setenv("WAMCP_GUI_PROCESSES", info.process)
    monkeypatch.setenv("WAMCP_DOWNLOAD_ROOT", str(tmp_path))

    record: dict[str, list[Any]] = {
        "focus": [],
        "client_to_screen": [],
        "moves": [],
        "buttons": [],
        "keys": [],
        "sleeps": [],
    }

    state: dict[str, Any] = {
        "frames": list(frames),
        "foreground": list(foreground_sequence or [info.hwnd] * 64),
    }

    monkeypatch.setattr(windowing, "find_window", lambda selector: info)
    monkeypatch.setattr(
        windowing,
        "focus_window",
        lambda target: (
            record["focus"].append(target) or (focus_result or (True, "already"))
        ),
    )
    monkeypatch.setattr(
        windowing,
        "foreground_hwnd",
        lambda: state["foreground"].pop(0) if state["foreground"] else 0,
    )
    monkeypatch.setattr(
        windowing,
        "capture_window_frame",
        lambda target: state["frames"].pop(0),
    )
    monkeypatch.setattr(
        windowing,
        "client_to_screen",
        lambda target, x, y: (
            record["client_to_screen"].append((x, y)) or (x + 342, y + 127)
        ),
    )
    monkeypatch.setattr(
        windowing, "move_cursor", lambda x, y: record["moves"].append((x, y))
    )
    monkeypatch.setattr(
        windowing, "mouse_button", lambda b, d: record["buttons"].append((b, d))
    )
    monkeypatch.setattr(
        windowing, "key_down", lambda s: record["keys"].append(("down", s))
    )
    monkeypatch.setattr(windowing, "key_up", lambda s: record["keys"].append(("up", s)))

    # Patch the tool module's binding to time -- never the global module.
    class _FakeTime:
        def sleep(self, seconds: float) -> None:
            record["sleeps"].append(seconds)

        perf_counter = staticmethod(time.perf_counter)

    monkeypatch.setattr(send_module, "time", _FakeTime())

    return record


def test_send_input_requires_gui_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WAMCP_GUI", "1")
    monkeypatch.delenv("WAMCP_GUI_INPUT", raising=False)
    monkeypatch.setenv("WAMCP_GUI_PROCESSES", "game.exe")

    info = make_window()

    monkeypatch.setattr(windowing, "find_window", lambda selector: info)

    def fail_focus(_target: object) -> tuple[bool, str]:
        raise AssertionError("focus must not be taken when input is disabled")

    monkeypatch.setattr(windowing, "focus_window", fail_focus)

    result = send_module.send_input("game.exe", [{"type": "click", "x": 1, "y": 1}])

    assert "GUI_INPUT_DISABLED" in result
    assert '"ok": false' in result


def test_send_input_requires_allowlisted_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WAMCP_GUI", "1")
    monkeypatch.setenv("WAMCP_GUI_INPUT", "1")
    monkeypatch.delenv("WAMCP_GUI_PROCESSES", raising=False)

    info = make_window()

    monkeypatch.setattr(windowing, "find_window", lambda selector: info)

    def fail_focus(_target: object) -> tuple[bool, str]:
        raise AssertionError("focus must not be taken for an unlisted process")

    monkeypatch.setattr(windowing, "focus_window", fail_focus)

    result = send_module.send_input("secret.exe", [{"type": "click", "x": 1, "y": 1}])

    assert "PROCESS_NOT_ALLOWED" in result


@pytest.mark.parametrize(
    "action",
    [
        [{"type": "explode"}],
        [{"type": "click", "y": 5}],
        [{"type": "click", "x": 1000, "y": 5}],
        [{"type": "click", "x": 5, "y": None}],
        [{"type": "key", "key": "cancel-quit", "modifiers": ["ala"]}],
    ],
)
def test_send_input_rejects_bad_action_shapes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: list[dict[str, object]],
) -> None:
    monkeypatch.setenv("WAMCP_GUI", "1")
    monkeypatch.setenv("WAMCP_GUI_INPUT", "1")
    monkeypatch.setenv("WAMCP_GUI_PROCESSES", "game.exe")

    info = make_window()

    record = install_seams(
        tmp_path,
        monkeypatch,
        info,
        frames=[],  # capture must never run
    )

    result = send_module.send_input("game.exe", action)

    assert "INVALID_ARGUMENT" in result
    assert '"action_index": 0' in result
    assert record["focus"] == []
    assert record["moves"] == []
    assert record["buttons"] == []
    assert record["keys"] == []


def test_send_input_focus_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    info = make_window()

    record = install_seams(
        tmp_path,
        monkeypatch,
        info,
        frames=[],
        focus_result=(False, "refused"),
    )

    result = send_module.send_input("game.exe", [{"type": "key", "key": "escape"}])

    assert "FOCUS_REFUSED" in result
    assert record["keys"] == []
    assert record["buttons"] == []
    assert record["moves"] == []


def test_send_input_click_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    info = make_window()

    before = solid_frame(info.client_width, info.client_height)
    after = edited_frame(before, 2)

    record = install_seams(
        tmp_path,
        monkeypatch,
        info,
        frames=[before, after],
    )

    result = send_module.send_input(
        "game.exe",
        [{"type": "click", "x": 7, "y": 9, "button": "right", "count": 2}],
    )

    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["delta"] > 0
    assert payload["focus"] == "already"
    assert payload["actions"] == 1

    # Window sizes from the seams, not from any real window.
    assert payload["window"]["client_width"] == info.client_width

    # client-relative goes in, screen comes out.
    assert record["client_to_screen"] == [(7, 9), (7, 9)]

    assert len(record["moves"]) == 2
    assert record["buttons"] == [
        ("right", True),
        ("right", False),
        ("right", True),
        ("right", False),
    ]

    before_path = Path(payload["before"])
    after_path = Path(payload["after"])

    assert before_path.parent == tmp_path / "captures"
    assert before_path.name.startswith("game-before-")
    assert after_path.name.startswith("game-after-")

    assert before_path.read_bytes().startswith(PNG_SIGNATURE)
    assert after_path.read_bytes().startswith(PNG_SIGNATURE)
    assert not list(before_path.parent.glob("*.part"))


def test_send_input_identical_frames_reports_zero_delta(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    info = make_window()

    frame = solid_frame(info.client_width, info.client_height)

    record = install_seams(tmp_path, monkeypatch, info, frames=[frame, frame])

    result = send_module.send_input("game.exe", [{"type": "click", "x": 1, "y": 1}])

    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["delta"] == 0.0
    assert record["buttons"] == [("left", True), ("left", False)]


def test_send_input_drag_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    info = make_window()

    frame = solid_frame(info.client_width, info.client_height)

    record = install_seams(tmp_path, monkeypatch, info, frames=[frame, frame])

    result = send_module.send_input(
        "game.exe",
        [{"type": "drag", "from": [1, 1], "to": [50, 40]}],
    )

    payload = json.loads(result)

    assert payload["ok"] is True

    assert len(record["moves"]) >= 8
    assert record["buttons"] == [("left", True), ("left", False)]

    # The first move aims at the drag start; the later ones progress.
    assert record["moves"][0] == (1 + 342, 1 + 127)

    xs = [x for x, _y in record["moves"][1:]]

    assert xs == sorted(xs)


def test_send_input_key_with_modifier_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    info = make_window()

    frame = solid_frame(info.client_width, info.client_height)

    record = install_seams(tmp_path, monkeypatch, info, frames=[frame, frame])

    result = send_module.send_input(
        "game.exe",
        [{"type": "key", "key": "a", "modifiers": ["ctrl"]}],
    )

    payload = json.loads(result)

    assert payload["ok"] is True

    # ctrl down strictly before the key down; ctrl up strictly after the key up.
    assert record["keys"] == [
        ("down", windowing.MODIFIER_SCANCODES["ctrl"]),
        ("down", windowing.KEY_SCANCODES["a"]),
        ("up", windowing.KEY_SCANCODES["a"]),
        ("up", windowing.MODIFIER_SCANCODES["ctrl"]),
    ]


def test_send_input_unknown_key_name_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    info = make_window()

    record = install_seams(tmp_path, monkeypatch, info, frames=[])

    result = send_module.send_input("game.exe", [{"type": "key", "key": "cancel"}])

    assert "INVALID_ARGUMENT" in result
    assert record["keys"] == []


def test_send_input_focus_lost_mid_sequence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    info = make_window()

    frame = solid_frame(info.client_width, info.client_height)

    # First check after action 1 sees the target; after action 2 it does not.
    record = install_seams(
        tmp_path,
        monkeypatch,
        info,
        frames=[frame, frame],
        foreground_sequence=[info.hwnd, 0x9999],
    )

    result = send_module.send_input(
        "game.exe",
        [
            {"type": "click", "x": 1, "y": 1},
            {"type": "click", "x": 2, "y": 2},
        ],
    )

    assert "FOCUS_LOST" in result
    assert '"completed": 1' in result
    assert record["buttons"] == [("left", True), ("left", False)]
    assert len(record["client_to_screen"]) == 1


def test_frame_delta() -> None:
    frame = solid_frame(8, 4)

    assert windowing.frame_delta(frame, frame) == 0.0
    assert windowing.frame_delta(frame, edited_frame(frame, 2)) > 0.0
    assert windowing.frame_delta(frame, solid_frame(8, 5)) == 1.0


def test_send_input_settle_clamped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    info = make_window()

    frame = solid_frame(info.client_width, info.client_height)

    record = install_seams(
        tmp_path,
        monkeypatch,
        info,
        frames=[frame, frame, frame, frame],
    )

    send_module.send_input(
        "game.exe", [{"type": "click", "x": 1, "y": 1}], settle_ms=-10
    )

    send_module.send_input(
        "game.exe",
        [{"type": "click", "x": 1, "y": 1}],
        settle_ms=99999,
    )

    assert 0.0 in record["sleeps"]
    assert 5.0 in record["sleeps"]
