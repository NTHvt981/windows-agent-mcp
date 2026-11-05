from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

from .. import windowing
from ..error import mcp_error
from ..utils import (
    GUI_PROCESSES_ENV_VAR,
    get_download_root,
    gui_enabled,
    gui_process_allowed,
)

__all__: list[str] = ["capture_window"]

_RESTORE_SETTLE_SECONDS = 0.35


def _sanitize_stem(process: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", process.rsplit(".", 1)[0].lower()).strip("-")


def capture_window(window: str, restore: bool = False) -> str:
    """Capture a window's client area to a PNG file and return its path.

    The PNG path is returned, NOT the image itself. A client that can display
    images can open that path directly; this server's own read_file cannot,
    because it reads UTF-8 text only. The window's process must be listed in
    the WAMCP_GUI_PROCESSES allowlist, which the operator sets; nothing is
    capturable by default.

    Args:
        window: The target. A process name such as "conflict-converge.exe",
            its stem ("conflict-converge"), or a numeric hwnd from
            list_windows. Window TITLES are never matched.
        restore: Set true to restore a minimized window before capturing.
            A minimized window has no rendered frame, so it cannot be
            captured as-is.

    Returns:
        JSON with ok, path, width, height and elapsed_ms on success, or a
        structured error. Never embeds image data.

    Example:
        capture_window(window="conflict-converge.exe")
    """

    if not gui_enabled():
        return mcp_error(
            "GUI_DISABLED",
            "capture_window",
            "Desktop capture is not enabled on this server.",
            recovery=[
                "DO NOT retry: no request will succeed until it is enabled.",
                "Tell the user to set WAMCP_GUI=1 and restart the server if "
                "they want window capture.",
            ],
        )

    if not window or not window.strip():
        return mcp_error(
            "INVALID_ARGUMENT",
            "capture_window",
            "The window selector cannot be empty.",
            recovery=[
                "Pass the executable name from list_windows, e.g. "
                "'conflict-converge.exe'.",
                "Do not retry with an empty selector.",
            ],
        )

    try:
        info = windowing.find_window(window)
    except windowing.WindowError as exc:
        return mcp_error(
            exc.code,
            "capture_window",
            exc.message,
            recovery=exc.recovery,
        )

    if info is None:
        return mcp_error(
            "WINDOW_NOT_FOUND",
            "capture_window",
            f"No visible window matched {window!r}.",
            details={"selector": window},
            recovery=[
                "Call list_windows to see which windows exist and their process names.",
                "Titles are not used for matching; pass the process name.",
            ],
        )

    if not gui_process_allowed(info.process):
        return mcp_error(
            "PROCESS_NOT_ALLOWED",
            "capture_window",
            f"{info.process or 'This executable'} is not on the capture "
            "allowlist, so its contents may not be read.",
            details={"process": info.process},
            recovery=[
                "Capturing reads the contents of another application's window.",
                f"Add {info.process} to {GUI_PROCESSES_ENV_VAR} (comma- or "
                "semicolon-separated) and restart the server.",
                "Do not try other windows to work around the allowlist.",
            ],
        )

    if info.minimized and not restore:
        return mcp_error(
            "WINDOW_MINIMIZED",
            "capture_window",
            "The window is minimized and has no rendered frame to capture.",
            details={"process": info.process},
            recovery=[
                "Call again with restore=True, or ask the user to open the "
                "window; then retry once.",
            ],
        )

    if info.minimized and restore:
        try:
            windowing.restore_window(info)
        except windowing.WindowError as exc:
            return mcp_error(
                exc.code,
                "capture_window",
                exc.message,
                recovery=exc.recovery,
            )

        time.sleep(_RESTORE_SETTLE_SECONDS)

        refreshed = windowing.find_window(window)

        if refreshed is not None:
            info = refreshed

    started = time.perf_counter()

    try:
        png, width, height = windowing.capture_window_png(info)
    except windowing.WindowError as exc:
        return mcp_error(
            exc.code,
            "capture_window",
            exc.message,
            recovery=exc.recovery,
        )

    elapsed_ms = round((time.perf_counter() - started) * 1000)

    stem = _sanitize_stem(info.process) or "window"

    directory = get_download_root() / "captures"

    target = directory / (
        f"{stem}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:-3]}.png"
    )

    temporary = target.with_suffix(".part")

    try:
        directory.mkdir(parents=True, exist_ok=True)

        with temporary.open("wb") as stream:
            stream.write(png)

        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)

        return mcp_error(
            "WRITE_FAILED",
            "capture_window",
            f"Could not write the capture to disk: {exc}",
            path=str(target),
            recovery=[
                "Check the filesystem state before retrying.",
                "Do not repeatedly retry the identical capture.",
            ],
        )

    payload: dict[str, object] = {
        "ok": True,
        "path": str(target),
        "width": width,
        "height": height,
        "process": info.process,
        "title": info.title,
        "hwnd": info.hwnd,
        "window_rect": {
            "left": info.left,
            "top": info.top,
            "right": info.right,
            "bottom": info.bottom,
        },
        "client_offset": {
            "x": info.client_x - info.left,
            "y": info.client_y - info.top,
        },
        "bytes": len(png),
        "elapsed_ms": elapsed_ms,
        "foreground": info.foreground,
        "png": True,
    }

    return json.dumps(payload, indent=2)
