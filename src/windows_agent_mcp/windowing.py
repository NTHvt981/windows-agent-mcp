"""Win32 layer for the gui tool group: enumerate and capture windows.

Pure mechanics only: no MCP, no environment variables, no policy checks.
The tool layer owns the executable allowlist; this module would happily
capture any window it is handed.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import struct
import time
import zlib
from typing import Any, NamedTuple

__all__: list[str] = [
    "KEY_SCANCODES",
    "MODIFIER_SCANCODES",
    "MOUSE_BUTTONS",
    "WINDOWS_AVAILABLE",
    "WindowError",
    "WindowInfo",
    "capture_window_frame",
    "capture_window_png",
    "client_to_screen",
    "encode_png",
    "find_window",
    "focus_window",
    "foreground_hwnd",
    "frame_delta",
    "key_down",
    "key_up",
    "list_visible_windows",
    "mouse_button",
    "move_cursor",
    "restore_window",
]

WINDOWS_AVAILABLE: bool = hasattr(ctypes, "WinDLL")

PW_RENDERFULLCONTENT: int = 2
DIB_RGB_COLORS: int = 0
SW_RESTORE: int = 9
PROCESS_QUERY_LIMITED_INFORMATION: int = 0x1000
MAX_TITLE_CHARS: int = 120

INPUT_MOUSE: int = 0
INPUT_KEYBOARD: int = 1
KEYEVENTF_KEYUP: int = 2
KEYEVENTF_SCANCODE: int = 8
VK_MENU: int = 0x12
# Shell helper windows (taskbar strips, 1x1 owners) have no title and no
# pixels worth capturing; listing them only costs the model context.
MIN_UNTITLED_CLIENT: int = 100

# Pre-declared so the GUI functions type-check on a non-Windows host too:
# they are None exactly when WINDOWS_AVAILABLE is False, and every public
# entry point raises UNSUPPORTED_PLATFORM before touching them.
_user32: Any = None
_gdi32: Any = None
_kernel32: Any = None

if WINDOWS_AVAILABLE:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    try:
        # PER_MONITOR_AWARE_V2: without it GetWindowRect/GetClientRect return
        # DPI-scaled coordinates and the client crop misses. Setting a better
        # awareness fails once and for all if a context is already set, so
        # failure is simply inherited host behaviour, not an error.
        _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # pyright: ignore[reportAttributeAccessIssue]
    except (AttributeError, OSError):
        pass


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _BITMAPINFOHEADER(ctypes.Structure):
    # DWORD/WORD spellings: c_uint32/c_uint16 are the same sizes on Windows.
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", ctypes.c_uint32 * 3),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


# INPUT's union is 32 bytes on x64; an explicit pad keeps the struct the size
# Win32 expects on both 32- and 64-bit hosts.
class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("pad", ctypes.c_ubyte * 32),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _INPUTUNION)]


if WINDOWS_AVAILABLE:
    _user32.IsWindowVisible.argtypes = [wt.HWND]
    _user32.IsWindowVisible.restype = wt.BOOL
    _user32.IsIconic.argtypes = [wt.HWND]
    _user32.IsIconic.restype = wt.BOOL
    _user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(_RECT)]
    _user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(_RECT)]
    _user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(_POINT)]
    _user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    _user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetForegroundWindow.restype = wt.HWND
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetDC.restype = wt.HDC
    _user32.GetDC.argtypes = [wt.HWND]
    _user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    _user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    _user32.PrintWindow.restype = wt.BOOL
    _user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]

    _user32.SetForegroundWindow.restype = wt.BOOL
    _user32.SetForegroundWindow.argtypes = [wt.HWND]
    _user32.SetCursorPos.restype = wt.BOOL
    _user32.SetCursorPos.argtypes = [ctypes.c_long, ctypes.c_long]
    _user32.keybd_event.restype = None
    _user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, wt.LPVOID]
    _user32.SendInput.restype = wt.UINT
    _user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]

    _gdi32.CreateCompatibleDC.restype = wt.HDC
    _gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    _gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
    _gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    _gdi32.SelectObject.restype = wt.HGDIOBJ
    _gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    _gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    _gdi32.DeleteDC.argtypes = [wt.HDC]
    _gdi32.GetDIBits.argtypes = [
        wt.HDC,
        wt.HBITMAP,
        wt.UINT,
        wt.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFO),
        wt.UINT,
    ]

    _kernel32.OpenProcess.restype = wt.HANDLE
    _kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE,
        wt.DWORD,
        wt.LPWSTR,
        ctypes.POINTER(wt.DWORD),
    ]
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]


class WindowError(Exception):
    """A windowing failure the tool layer maps straight into an MCP error."""

    def __init__(
        self,
        code: str,
        message: str,
        recovery: list[str],
    ) -> None:
        super().__init__(message)

        self.code = code
        self.message = message
        self.recovery = recovery

    def __str__(self) -> str:
        return self.message


def _require_windows() -> None:
    if not WINDOWS_AVAILABLE:
        raise WindowError(
            "UNSUPPORTED_PLATFORM",
            "Window enumeration and capture need the Win32 API, which only "
            "exists on Windows.",
            [
                "Do not retry: no windowing call can succeed on this platform.",
                "Tell the user this server only provides gui tools on Windows.",
            ],
        )


class WindowInfo(NamedTuple):
    hwnd: int
    pid: int
    process: str
    title: str
    left: int
    top: int
    right: int
    bottom: int
    client_width: int
    client_height: int
    client_x: int
    client_y: int
    minimized: bool
    foreground: bool


def _process_basename(hwnd: int) -> str:
    """The executable's basename for a window, or "" when unavailable."""

    pid = wt.DWORD()

    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)

    if not handle:
        return ""

    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)

        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""

        return os.path.basename(buf.value)
    finally:
        _kernel32.CloseHandle(handle)


def _window_title(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)

    if length <= 0:
        return ""

    buf = ctypes.create_unicode_buffer(min(length + 1, MAX_TITLE_CHARS + 1))

    _user32.GetWindowTextW(hwnd, buf, len(buf))

    # Control characters are not displayable and can confuse a small model.
    cleaned = "".join(c for c in buf.value if c.isprintable())

    return cleaned[:MAX_TITLE_CHARS]


def _describe_window(hwnd: int) -> WindowInfo | None:
    rect = _RECT()

    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None

    left, top = rect.left, rect.top
    right, bottom = rect.right, rect.bottom

    if right - left <= 0 or bottom - top <= 0:
        return None

    client = _RECT()

    if not _user32.GetClientRect(hwnd, ctypes.byref(client)):
        return None

    client_width = client.right - client.left
    client_height = client.bottom - client.top

    title = _window_title(hwnd)

    if not title and (
        client_width < MIN_UNTITLED_CLIENT or client_height < MIN_UNTITLED_CLIENT
    ):
        return None

    origin = _POINT()

    if not _user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None

    pid = wt.DWORD()

    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    return WindowInfo(
        hwnd=int(hwnd),
        pid=int(pid.value),
        process=_process_basename(hwnd),
        title=title,
        left=left,
        top=top,
        right=right,
        bottom=bottom,
        client_width=client_width,
        client_height=client_height,
        client_x=origin.x,
        client_y=origin.y,
        minimized=bool(_user32.IsIconic(hwnd)),
        foreground=int(_user32.GetForegroundWindow()) == int(hwnd),
    )


def list_visible_windows(limit: int = 100) -> list[WindowInfo]:
    """Visible top-level windows, in z-order, capped at ``limit``.

    Title-less windows smaller than 100 px in either direction are skipped:
    they are shell helpers, not applications.
    """

    _require_windows()

    if limit <= 0:
        return []

    found: list[WindowInfo] = []

    def visit(hwnd: int, _param: int) -> bool:
        if _user32.IsWindowVisible(hwnd):
            info = _describe_window(hwnd)

            if info is not None:
                found.append(info)
                return len(found) < limit

        return True

    proc_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    _user32.EnumWindows(proc_type(visit), 0)

    return found


def find_window(selector: str) -> WindowInfo | None:
    """Resolve a process name, executable stem, or hwnd to one window.

    Titles are never matched: many applications share a generic title.
    """

    _require_windows()

    selector = selector.strip()

    if not selector:
        return None

    try:
        hwnd = int(selector, 0)
    except ValueError:
        hwnd = None

    if hwnd is not None:
        for info in list_visible_windows():
            if info.hwnd == hwnd:
                return info

        return None

    wanted = os.path.basename(selector).lower()
    stem = os.path.splitext(wanted)[0]

    matches = [
        info
        for info in list_visible_windows()
        if info.process and info.process.lower() in {wanted, stem}
    ]

    if not matches:
        return None

    foreground = [info for info in matches if info.foreground]

    if foreground:
        return foreground[0]

    return max(matches, key=lambda info: info.client_width * info.client_height)


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Minimal RGB PNG encoder: zlib and struct only."""

    expected = width * height * 3

    if len(rgb) != expected:
        raise ValueError(
            f"rgb is {len(rgb)} bytes; expected {expected} for a {width}x{height} RGB image."
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    stride = width * 3

    scanlines = bytearray()

    for row in range(height):
        scanlines.append(0)  # filter type 0: None
        scanlines += rgb[row * stride : (row + 1) * stride]

    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", ihdr),
            _png_chunk(b"IDAT", zlib.compress(bytes(scanlines))),
            _png_chunk(b"IEND", b""),
        ]
    )


def _bgra_to_rgb_and_crop(
    raw: bytes,
    width: int,
    x: int,
    y: int,
    crop_w: int,
    crop_h: int,
) -> bytes:
    """Convert BGRA to RGB with C-speed slicing, then clip to the client area.

    Extended-slice assignment first (whole buffer, no per-pixel Python
    loop), row surgery second: both run in C and keep capture cheap.
    """

    rgb = bytearray(len(raw) // 4 * 3)

    rgb[0::3] = raw[2::4]
    rgb[1::3] = raw[1::4]
    rgb[2::3] = raw[0::4]

    stride = width * 3

    rows = [
        rgb[(y + row) * stride + x * 3 : (y + row) * stride + (x + crop_w) * 3]
        for row in range(crop_h)
    ]

    return b"".join(rows)


def capture_window_frame(info: WindowInfo) -> tuple[bytes, int, int]:
    """Capture a window's client area; returns ``(rgb, width, height)``.

    ``PrintWindow`` with ``PW_RENDERFULLCONTENT`` renders windows that are
    fully occluded by other windows, so a capture never needs to disturb
    the desktop.
    """
    _require_windows()
    _raise_if_unsupported_frame(info)
    rgb, width, height = _capture_client_pixels(info)
    return rgb, width, height


def capture_window_png(info: WindowInfo) -> tuple[bytes, int, int]:
    """Capture a window's client area; returns ``(png, width, height)``."""

    rgb, width, height = capture_window_frame(info)

    return encode_png(width, height, rgb), width, height


def _raise_if_unsupported_frame(info: WindowInfo) -> None:
    if info.minimized:
        raise WindowError(
            "WINDOW_MINIMIZED",
            "The window is minimized and has no rendered frame to capture.",
            [
                "Call capture_window again with restore=True.",
                "A minimized window has no rendered frame.",
            ],
        )

    if info.client_width <= 0 or info.client_height <= 0:
        raise WindowError(
            "CAPTURE_FAILED",
            "The window reports no client area to capture.",
            [
                "Do not retry the identical capture.",
                "Check with list_windows whether the window is still visible.",
            ],
        )


def _capture_client_pixels(info: WindowInfo) -> tuple[bytes, int, int]:
    width = info.right - info.left
    height = info.bottom - info.top

    screen = _user32.GetDC(None)
    memory = _gdi32.CreateCompatibleDC(screen)
    bitmap = _gdi32.CreateCompatibleBitmap(screen, width, height)
    previous = _gdi32.SelectObject(memory, bitmap)

    try:
        ok = bool(_user32.PrintWindow(info.hwnd, memory, PW_RENDERFULLCONTENT))
        attempted = "PW_RENDERFULLCONTENT(2)"

        if not ok:
            attempted = "PrintWindow(0)"
            ok = bool(_user32.PrintWindow(info.hwnd, memory, 0))

        if not ok:
            raise WindowError(
                "CAPTURE_FAILED",
                "PrintWindow refused to render this window.",
                [
                    "Do not repeatedly retry the identical capture.",
                    "Some hardware-accelerated windows refuse PrintWindow; "
                    f"both {attempted} and the plain call already failed.",
                    "Tell the user this application cannot be captured.",
                ],
            )

        image_info = _BITMAPINFO()
        image_info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        image_info.bmiHeader.biWidth = width
        image_info.bmiHeader.biHeight = -height  # negative: top-down
        image_info.bmiHeader.biPlanes = 1
        image_info.bmiHeader.biBitCount = 32
        image_info.bmiHeader.biCompression = 0

        buffer = ctypes.create_string_buffer(width * height * 4)

        got = _gdi32.GetDIBits(
            memory,
            bitmap,
            0,
            height,
            buffer,
            ctypes.byref(image_info),
            DIB_RGB_COLORS,
        )

        if got != height:
            raise WindowError(
                "CAPTURE_FAILED",
                f"GetDIBits returned {got} of {height} scanlines.",
                [
                    "Do not retry the identical capture.",
                    "Check with list_windows whether the window is still there.",
                ],
            )

        raw = buffer.raw
    finally:
        _gdi32.SelectObject(memory, previous)
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(memory)
        _user32.ReleaseDC(None, screen)

    offset_x = info.client_x - info.left
    offset_y = info.client_y - info.top

    # Clamp: some windows report a client wider than their own frame.
    offset_x = max(0, min(offset_x, width - 1))
    offset_y = max(0, min(offset_y, height - 1))

    crop_w = min(info.client_width, width - offset_x)
    crop_h = min(info.client_height, height - offset_y)

    if crop_w <= 0 or crop_h <= 0:
        raise WindowError(
            "CAPTURE_FAILED",
            "The client area lies outside the captured bitmap.",
            ["Do not retry the identical capture."],
        )

    rgb = _bgra_to_rgb_and_crop(raw, width, offset_x, offset_y, crop_w, crop_h)

    return rgb, crop_w, crop_h


def restore_window(info: WindowInfo) -> None:
    """Restore a minimized window so it has a frame to capture."""

    _require_windows()

    _user32.ShowWindow(info.hwnd, SW_RESTORE)


# Set-2 make codes. Deliberately a short allowlist of names, never VK codes
# from the caller: the caller names a letter, this table owns the hardware.
KEY_SCANCODES: dict[str, int] = {
    "escape": 0x01,
    "1": 0x02,
    "2": 0x03,
    "3": 0x04,
    "4": 0x05,
    "5": 0x06,
    "6": 0x07,
    "7": 0x08,
    "8": 0x09,
    "9": 0x0A,
    "0": 0x0B,
    "backspace": 0x0E,
    "tab": 0x0F,
    "q": 0x10,
    "w": 0x11,
    "e": 0x12,
    "r": 0x13,
    "t": 0x14,
    "y": 0x15,
    "u": 0x16,
    "i": 0x17,
    "o": 0x18,
    "p": 0x19,
    "a": 0x1E,
    "s": 0x1F,
    "d": 0x20,
    "f": 0x21,
    "g": 0x22,
    "h": 0x23,
    "j": 0x24,
    "k": 0x25,
    "l": 0x26,
    "z": 0x2C,
    "x": 0x2D,
    "c": 0x2E,
    "v": 0x2F,
    "b": 0x30,
    "n": 0x31,
    "m": 0x32,
    "enter": 0x1C,
    "space": 0x39,
    "home": 0x47,
    "up": 0x48,
    "pageup": 0x49,
    "left": 0x4B,
    "right": 0x4D,
    "end": 0x4F,
    "down": 0x50,
    "pagedown": 0x51,
    "insert": 0x52,
    "delete": 0x53,
    "f1": 0x3B,
    "f2": 0x3C,
    "f3": 0x3D,
    "f4": 0x3E,
    "f5": 0x3F,
    "f6": 0x40,
    "f7": 0x41,
    "f8": 0x42,
    "f9": 0x43,
    "f10": 0x44,
    "f11": 0x57,
    "f12": 0x58,
}

MODIFIER_SCANCODES: dict[str, int] = {
    "ctrl": 0x1D,
    "shift": 0x2A,
    "alt": 0x38,
}

# SendInput MOUSEINPUT dwFlags pairs: (button-down flag, button-up flag).
MOUSE_BUTTONS: dict[str, tuple[int, int]] = {
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}


def _send_input(event: _INPUT) -> None:
    if _user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT)) == 0:
        raise WindowError(
            "INPUT_FAILED",
            f"SendInput refused the event and reported {ctypes.get_last_error()}.",
            [
                "Do not retry blindly; a system-level input block (UAC secure "
                "desktop, a fullscreen elevated app) refuses any injection.",
                "Do not retry repeatedly: this is a real Win32 error, not a "
                "delivery doubt.",
            ],
        )


def move_cursor(x: int, y: int) -> None:
    """Move the cursor to the screen position: SetCursorPos lands exactly."""

    _require_windows()

    if not _user32.SetCursorPos(x, y):
        raise WindowError(
            "INPUT_FAILED",
            "SetCursorPos failed to move the mouse cursor.",
            ["Do not retry repeatedly; check the coordinates are on screen."],
        )


def mouse_button(button: str, down: bool) -> None:
    """Press or release a mouse button via SendInput at the current position."""

    _require_windows()

    event = _INPUT()
    event.type = INPUT_MOUSE
    event.union.mi.dwFlags = MOUSE_BUTTONS[button][0 if down else 1]

    _send_input(event)


def key_down(scan: int) -> None:
    """Press one key by Set-2 scancode via SendInput."""

    _require_windows()

    event = _INPUT()
    event.type = INPUT_KEYBOARD
    event.union.ki.wScan = scan
    event.union.ki.dwFlags = KEYEVENTF_SCANCODE

    _send_input(event)


def key_up(scan: int) -> None:
    """Release one key by Set-2 scancode via SendInput."""

    _require_windows()

    event = _INPUT()
    event.type = INPUT_KEYBOARD
    event.union.ki.wScan = scan
    event.union.ki.dwFlags = KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP

    _send_input(event)


def client_to_screen(info: WindowInfo, x: int, y: int) -> tuple[int, int]:
    """Map client-relative coordinates to screen coordinates right now.

    Called at injection time so a window that moved after ``find_window``
    still lands the click where the caller meant, not where it used to be.
    """

    _require_windows()

    point = _POINT(x, y)

    if not _user32.ClientToScreen(info.hwnd, ctypes.byref(point)):
        raise WindowError(
            "CAPTURE_FAILED",
            "ClientToScreen failed to map the client point to screen space.",
            [
                "Re-resolve the window with list_windows; the window may be closing.",
            ],
        )

    return int(point.x), int(point.y)


def foreground_hwnd() -> int:
    """The hwnd of the foreground window, or 0 when there is none."""

    _require_windows()

    return int(_user32.GetForegroundWindow() or 0)


_FOCUS_SETTLE_SECONDS = 0.35


def focus_window(info: WindowInfo) -> tuple[bool, str]:
    """Take the foreground; never raises on refusal.

    ``SetForegroundWindow`` alone is refused when another application owns
    the foreground (measured from a detached, windowless process): a brief
    Alt key press and release first unlocks the foreground lock, and only a
    follow-up verification decides the result -- a TRUE return is not proof.
    """

    _require_windows()

    if foreground_hwnd() == info.hwnd:
        return True, "already"

    _user32.keybd_event(VK_MENU, 0, 0, None)
    _user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)

    _user32.SetForegroundWindow(info.hwnd)

    time.sleep(_FOCUS_SETTLE_SECONDS)

    if foreground_hwnd() == info.hwnd:
        return True, "alt"

    return False, "refused"


# Sampling step for frame_delta: every second pixel in x (3 channels * 2) and
# every second row. A full Python per-pixel loop is minutes-slow; a quarter
# sample stays well under the budget at 1200x675.
_DELTA_CHANNEL_THRESHOLD = 8


def frame_delta(
    before: tuple[bytes, int, int],
    after: tuple[bytes, int, int],
) -> float:
    """Fraction of SAMPLED pixels that changed, not an exact count.

    Every second pixel in x and every second row is compared; a pixel counts
    as changed when any channel differs by more than 8. Sizes differing means
    different screenshots altogether, so 1.0. Identical frames are 0.0.
    """

    rgb_before, width, height = before
    if (width, height) != (after[1], after[2]):
        return 1.0

    rgb_after = after[0]

    if rgb_before == rgb_after:
        return 0.0

    stride = width * 3
    x_step = 6

    sampled = 0
    changed = 0

    for y in range(0, height, 2):
        row_start = y * stride
        row_before = rgb_before[row_start : row_start + stride]
        row_after = rgb_after[row_start : row_start + stride]

        for offset in range(0, stride - 2, x_step):
            sampled += 1

            if (
                abs(row_before[offset] - row_after[offset]) > _DELTA_CHANNEL_THRESHOLD
                or abs(row_before[offset + 1] - row_after[offset + 1])
                > _DELTA_CHANNEL_THRESHOLD
                or abs(row_before[offset + 2] - row_after[offset + 2])
                > _DELTA_CHANNEL_THRESHOLD
            ):
                changed += 1

    if sampled == 0:
        return 0.0

    return round(changed / sampled, 4)
