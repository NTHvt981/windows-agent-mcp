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
import zlib
from typing import Any, NamedTuple

__all__: list[str] = [
    "WINDOWS_AVAILABLE",
    "WindowError",
    "WindowInfo",
    "capture_window_png",
    "encode_png",
    "find_window",
    "list_visible_windows",
    "restore_window",
]

WINDOWS_AVAILABLE: bool = hasattr(ctypes, "WinDLL")

PW_RENDERFULLCONTENT: int = 2
DIB_RGB_COLORS: int = 0
SW_RESTORE: int = 9
PROCESS_QUERY_LIMITED_INFORMATION: int = 0x1000
MAX_TITLE_CHARS: int = 120
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


def capture_window_png(info: WindowInfo) -> tuple[bytes, int, int]:
    """Capture a window's client area; returns ``(png, width, height)``.

    ``PrintWindow`` with ``PW_RENDERFULLCONTENT`` renders windows that are
    fully occluded by other windows, so a capture never needs to disturb
    the desktop.
    """

    _require_windows()

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

    return encode_png(crop_w, crop_h, rgb), crop_w, crop_h


def restore_window(info: WindowInfo) -> None:
    """Restore a minimized window so it has a frame to capture."""

    _require_windows()

    _user32.ShowWindow(info.hwnd, SW_RESTORE)
