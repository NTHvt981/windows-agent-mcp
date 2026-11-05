from __future__ import annotations

import json

from .. import windowing
from ..error import mcp_error
from ..utils import (
    GUI_PROCESSES_ENV_VAR,
    allowed_gui_processes,
    gui_enabled,
    gui_process_allowed,
)

__all__: list[str] = ["list_windows"]

MAX_WINDOWS: int = 100


def list_windows() -> str:
    """List visible top-level windows and whether each may be captured.

    Returns JSON. Windows are matched for capture by process name or hwnd --
    titles are not used, because many applications share a generic title. A
    false ``capture_allowed`` is not an error state, it says capture_window
    will refuse that window with ``PROCESS_NOT_ALLOWED`` until you ask the
    user to add the process to the WAMCP_GUI_PROCESSES allowlist.
    """

    if not gui_enabled():
        return mcp_error(
            "GUI_DISABLED",
            "list_windows",
            "Desktop capture is not enabled on this server.",
            recovery=[
                "DO NOT retry: no request will succeed until it is enabled.",
                "Tell the user to set WAMCP_GUI=1 and restart the server if "
                "they want window capture.",
            ],
        )

    try:
        windows = windowing.list_visible_windows(limit=MAX_WINDOWS)
    except windowing.WindowError as exc:
        return mcp_error(
            exc.code,
            "list_windows",
            exc.message,
            recovery=exc.recovery,
        )

    entries: list[dict[str, object]] = []

    for info in windows:
        entries.append(
            {
                "hwnd": info.hwnd,
                "pid": info.pid,
                "process": info.process,
                "title": info.title,
                "rect": {
                    "left": info.left,
                    "top": info.top,
                    "width": info.right - info.left,
                    "height": info.bottom - info.top,
                },
                "client": {
                    "width": info.client_width,
                    "height": info.client_height,
                },
                "minimized": info.minimized,
                "foreground": info.foreground,
                "capture_allowed": gui_process_allowed(info.process),
            }
        )

    payload: dict[str, object] = {
        "ok": True,
        "windows": entries,
        "capture_allowed_processes": sorted(allowed_gui_processes()),
        "policy": (
            "capture_window requires the window's executable to be named in "
            f"{GUI_PROCESSES_ENV_VAR}; nothing is capturable by default"
        ),
    }

    if len(entries) >= MAX_WINDOWS:
        payload["truncated"] = True

    return json.dumps(payload, indent=2)
