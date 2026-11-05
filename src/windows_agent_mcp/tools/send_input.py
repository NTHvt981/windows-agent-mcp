from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any, cast

from .. import windowing
from ..error import mcp_error
from ..utils import (
    GUI_INPUT_ENV_VAR,
    GUI_PROCESSES_ENV_VAR,
    get_download_root,
    gui_enabled,
    gui_input_enabled,
    gui_process_allowed,
)

__all__: list[str] = ["send_input"]

MIN_HOLD_MS = 10
MAX_HOLD_MS = 1000
DEFAULT_HOLD_MS = 50

MIN_DRAG_MS = 50
MAX_DRAG_MS = 3000
DEFAULT_DRAG_MS = 250

DEFAULT_SETTLE_MS = 350
MAX_SETTLE_MS = 5000

MOUSE_AIM_SETTLE_SECONDS = 0.02
CLICK_GAP_SECONDS = 0.06
DRAG_START_SETTLE_SECONDS = 0.08
DRAG_STEP_SECONDS = 0.03
DRAG_STEPS = 8

MAX_ACTIONS = 20

_ACCEPTED_SHAPES = (
    '{"type": "click", "x": int, "y": int, "button": "left"|"right"|"middle" '
    '(default "left"), "count": 1|2|3 (default 1), "hold_ms": int (default '
    "50, 10..1000)}; "
    '{"type": "drag", "from": [x, y], "to": [x, y], "button": "left"|"right" '
    '(default "left"), "duration_ms": int (default 250, 50..3000)}; '
    '{"type": "key", "key": "<name>", "modifiers": ["ctrl"|"shift"|"alt"] '
    '(optional), "hold_ms": int (default 50, 10..1000)}'
)


class _ActionError(ValueError):
    """One invalid action, carrying its index for the tool error."""

    def __init__(self, index: int, problem: str) -> None:
        super().__init__(problem)

        self.index = index
        self.problem = problem


def _int_field(action: dict[str, Any], index: int, field: str) -> int:
    value = action.get(field)

    if isinstance(value, bool) or not isinstance(value, int):
        raise _ActionError(index, f"{field!r} must be an integer")

    return value


def _clamped_int(
    action: dict[str, Any],
    index: int,
    field: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = action.get(field, default)

    if isinstance(value, bool) or not isinstance(value, int):
        raise _ActionError(index, f"{field!r} must be an integer")

    return max(minimum, min(value, maximum))


def _validate_click(action: dict[str, Any], index: int) -> dict[str, Any]:
    button = action.get("button", "left")

    if button not in windowing.MOUSE_BUTTONS:
        raise _ActionError(
            index,
            f"button must be one of {', '.join(sorted(windowing.MOUSE_BUTTONS))}",
        )

    count = action.get("count", 1)

    if isinstance(count, bool) or not isinstance(count, int) or count not in (1, 2, 3):
        raise _ActionError(index, "count must be 1, 2 or 3")

    return {
        "type": "click",
        "x": _int_field(action, index, "x"),
        "y": _int_field(action, index, "y"),
        "button": button,
        "count": count,
        "hold_ms": _clamped_int(
            action,
            index,
            "hold_ms",
            DEFAULT_HOLD_MS,
            MIN_HOLD_MS,
            MAX_HOLD_MS,
        ),
    }


def _validate_drag(action: dict[str, Any], index: int) -> dict[str, Any]:
    points: dict[str, list[object]] = {}

    for field in ("from", "to"):
        raw_point = action.get(field)

        point: list[object] | None = raw_point if isinstance(raw_point, list) else None

        if (
            point is None
            or len(point) != 2
            or any(isinstance(c, bool) or not isinstance(c, int) for c in point)
        ):
            raise _ActionError(
                index,
                f"{field!r} must be a [x, y] list of client-relative pixels",
            )

        points[field] = point

    button = action.get("button", "left")

    if button not in ("left", "right"):
        raise _ActionError(index, "drag button must be 'left' or 'right'")

    return {
        "type": "drag",
        "from": points["from"],
        "to": points["to"],
        "button": button,
        "duration_ms": _clamped_int(
            action,
            index,
            "duration_ms",
            DEFAULT_DRAG_MS,
            MIN_DRAG_MS,
            MAX_DRAG_MS,
        ),
    }


def _validate_key(action: dict[str, Any], index: int) -> dict[str, Any]:
    name = action.get("key")

    if not isinstance(name, str) or name.lower() not in windowing.KEY_SCANCODES:
        raise _ActionError(
            index,
            f"key must be one of {', '.join(sorted(windowing.KEY_SCANCODES))}",
        )

    raw_modifiers = action.get("modifiers", [])

    if not isinstance(raw_modifiers, list) or any(
        not isinstance(m, str) or m not in windowing.MODIFIER_SCANCODES
        for m in raw_modifiers
    ):
        raise _ActionError(
            index,
            "modifiers must be a list drawn from ctrl, shift, alt",
        )

    modifiers: list[str] = cast(list[str], raw_modifiers)

    return {
        "type": "key",
        "key": name.lower(),
        "modifiers": modifiers,
        "hold_ms": _clamped_int(
            action,
            index,
            "hold_ms",
            DEFAULT_HOLD_MS,
            MIN_HOLD_MS,
            MAX_HOLD_MS,
        ),
    }


def _points_of(parsed: dict[str, Any]) -> list[tuple[int, int]]:
    if parsed["type"] == "click":
        return [(parsed["x"], parsed["y"])]

    if parsed["type"] == "key":
        return []

    return [
        (parsed["from"][0], parsed["from"][1]),
        (parsed["to"][0], parsed["to"][1]),
    ]


def _validate_actions(
    actions: object, client_width: int, client_height: int
) -> list[dict[str, Any]]:
    """Validate EVERY action before anything is focused or injected."""

    validators = {
        "click": _validate_click,
        "drag": _validate_drag,
        "key": _validate_key,
    }

    actions_list: list[object] | None = actions if isinstance(actions, list) else None

    if actions_list is None:
        raise _ActionError(0, "actions must be a list of action objects")

    if not actions_list:
        raise _ActionError(0, "no actions")

    if len(actions_list) > MAX_ACTIONS:
        raise _ActionError(0, f"at most {MAX_ACTIONS} actions per call")

    validated: list[dict[str, Any]] = []

    for index, item in enumerate(actions_list):
        if not isinstance(item, dict) or item.get("type") not in validators:
            raise _ActionError(
                index,
                "an action must be an object of one of the accepted shapes: "
                + _ACCEPTED_SHAPES,
            )

        item_dict: dict[str, Any] = item

        parsed = validators[item_dict["type"]](item_dict, index)

        for x, y in _points_of(parsed):
            if not 0 <= x < client_width or not 0 <= y < client_height:
                raise _ActionError(
                    index,
                    f"coordinates must be client-relative and INSIDE the "
                    f"client area: x in [0, {client_width}), y in "
                    f"[0, {client_height}]); got ({x}, {y})",
                )

        validated.append(parsed)

    return validated


def _sanitize_stem(process: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", process.rsplit(".", 1)[0].lower()).strip("-")


def _save_capture(frame: tuple[bytes, int, int], stem: str, phase: str) -> Any:
    """Write one frame as `<stem>-<phase>-<timestamp>.png`; return the path."""

    rgb, width, height = frame

    directory = get_download_root() / "captures"

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]

    target = directory / f"{stem}-{phase}-{timestamp}.png"

    temporary = target.with_suffix(".part")

    try:
        directory.mkdir(parents=True, exist_ok=True)

        with temporary.open("wb") as stream:
            stream.write(windowing.encode_png(width, height, rgb))

        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise OSError(f"Could not write the capture to disk: {exc}") from exc

    return str(target)


def _run_click(parsed: dict[str, Any], info: windowing.WindowInfo) -> None:
    for _ in range(parsed["count"]):
        x, y = windowing.client_to_screen(info, parsed["x"], parsed["y"])

        windowing.move_cursor(x, y)

        time.sleep(MOUSE_AIM_SETTLE_SECONDS)

        windowing.mouse_button(parsed["button"], True)

        time.sleep(parsed["hold_ms"] / 1000)

        windowing.mouse_button(parsed["button"], False)

        time.sleep(CLICK_GAP_SECONDS)


def _run_drag(parsed: dict[str, Any], info: windowing.WindowInfo) -> None:
    start_x, start_y = windowing.client_to_screen(info, *parsed["from"])
    end_x, end_y = windowing.client_to_screen(info, *parsed["to"])

    windowing.move_cursor(start_x, start_y)

    time.sleep(DRAG_START_SETTLE_SECONDS)

    windowing.mouse_button(parsed["button"], True)

    for step in range(1, DRAG_STEPS + 1):
        fraction = step / DRAG_STEPS

        x = round(start_x + (end_x - start_x) * fraction)
        y = round(start_y + (end_y - start_y) * fraction)

        time.sleep(DRAG_STEP_SECONDS)

        windowing.move_cursor(x, y)

    windowing.mouse_button(parsed["button"], False)


def _run_key(parsed: dict[str, Any]) -> None:
    scan = windowing.KEY_SCANCODES[parsed["key"]]

    modifier_scans = [windowing.MODIFIER_SCANCODES[m] for m in parsed["modifiers"]]

    for modifier_scan in modifier_scans:
        windowing.key_down(modifier_scan)

    windowing.key_down(scan)

    time.sleep(parsed["hold_ms"] / 1000)

    windowing.key_up(scan)

    for modifier_scan in reversed(modifier_scans):
        windowing.key_up(modifier_scan)


def send_input(
    window: str, actions: list[dict[str, object]], settle_ms: int = DEFAULT_SETTLE_MS
) -> str:
    """Inject mouse and keyboard events into ONE allowlisted window.

    Focus is STOLEN from whatever the user is currently looking at, the real
    mouse cursor is moved, and every event goes to the FOREGROUND window
    only. SendInput returning 1 is not evidence the window heard anything:
    the window is captured before and after, and the returned raw pixel
    delta is the only evidence -- weigh it, never trust a single call. Escape
    can open a quit confirmation on a title screen; P often opens a modal
    pause menu; no key here is ever a generic "cancel". Coordinates must be
    CLIENT-relative pixels, never screen coordinates.

    Args:
        window: The target. A process name such as "conflict-converge.exe",
            its stem ("conflict-converge"), or a numeric hwnd from
            list_windows. Window TITLES are never matched.
        actions: Action dicts run in order. Accepted shapes:
            {"type": "click", "x": int, "y": int, "button": "left" (default)
            |"right"|"middle", "count": 1 (default)|2|3, "hold_ms": int
            (default 50, 10..1000)}
            {"type": "drag", "from": [x, y], "to": [x, y], "button":
            "left" (default)|"right", "duration_ms": int (default 250,
            50..3000)}
            {"type": "key", "key": "<name>", "modifiers": ["ctrl"|"shift"|
            "alt"] (optional), "hold_ms": int (default 50, 10..1000)}
            Key names: escape, 0-9, a-z, enter, backspace, tab, space,
            home, up, pageup, left, right, end, down, pagedown, insert,
            delete, f1..f12.
        settle_ms: Wait between the last action and the "after" capture,
            clamped to 0..5000.

    Returns:
        JSON with ok, window {process, title, hwnd, client_width,
        client_height}, actions (completed count), focus ("already"|"alt"),
        before, after (PNG paths), delta (sampled pixel ratio), delta_note,
        settle_ms, elapsed_ms, foreground_after. NO "changed" boolean:
        scene animation contributes, so compare deltas, do not threshold.

    Example:
        send_input(window="conflict-converge.exe", actions=[
            {"type": "click", "x": 600, "y": 337},
            {"type": "key", "key": "escape"},
        ])
    """

    if not gui_enabled():
        return mcp_error(
            "GUI_DISABLED",
            "send_input",
            "Desktop capture is not enabled on this server.",
            recovery=[
                "DO NOT retry: no request will succeed until it is enabled.",
                "Tell the user to set WAMCP_GUI=1 and restart the server if "
                "they want the gui tools at all.",
            ],
        )

    if not gui_input_enabled():
        return mcp_error(
            "GUI_INPUT_DISABLED",
            "send_input",
            "Input injection is not enabled: WAMCP_GUI_INPUT=1 was not set.",
            recovery=[
                "DO NOT retry until the user changes the environment.",
                f"Set {GUI_INPUT_ENV_VAR}=1 (it requires WAMCP_GUI=1) and "
                "restart the server to enable input.",
                "capture_window alone stays read-only: capture and input "
                "are separate grants on purpose.",
            ],
        )

    if not window or not window.strip():
        return mcp_error(
            "INVALID_ARGUMENT",
            "send_input",
            "The window selector cannot be empty.",
            recovery=[
                "Pass the executable name from list_windows, e.g. "
                "'conflict-converge.exe'.",
            ],
        )

    if not actions:
        return mcp_error(
            "INVALID_ARGUMENT",
            "send_input",
            "no actions: pass at least one action, e.g. "
            '{"type": "click", "x": 100, "y": 100}.',
            details={"problem": "no actions"},
            recovery=[
                f"One or more actions of the accepted shapes: {_ACCEPTED_SHAPES}",
            ],
        )

    try:
        info = windowing.find_window(window)
    except windowing.WindowError as exc:
        return mcp_error(exc.code, "send_input", exc.message, recovery=exc.recovery)

    if info is None:
        return mcp_error(
            "WINDOW_NOT_FOUND",
            "send_input",
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
            "send_input",
            f"{info.process or 'This executable'} is not on the capture "
            "allowlist, so it may not be focused, clicked or typed into.",
            details={"process": info.process},
            recovery=[
                f"Add {info.process} to {GUI_PROCESSES_ENV_VAR} (comma- or "
                "semicolon-separated) and restart the server; only name "
                "windows you accept being clicked.",
                "Do not try other windows to work around the allowlist.",
            ],
        )

    if info.minimized:
        return mcp_error(
            "WINDOW_MINIMIZED",
            "send_input",
            "The window is minimized and has no frame to aim at.",
            details={"process": info.process},
            recovery=[
                "Restore it (capture_window with restore=True restores it "
                "as a side effect of capturing) and retry once.",
            ],
        )

    try:
        validated = _validate_actions(actions, info.client_width, info.client_height)
    except _ActionError as exc:
        return mcp_error(
            "INVALID_ARGUMENT",
            "send_input",
            f"action {exc.index}: {exc.problem}",
            details={"action_index": exc.index, "problem": exc.problem},
            recovery=[
                "Accepted action shapes (all validated BEFORE any focus is "
                f"taken): {_ACCEPTED_SHAPES}",
                "Coordinates are CLIENT-relative: x in [0, client_width), y "
                "in [0, client_height), from list_windows' client size.",
            ],
        )

    try:
        focused, focus_kind = windowing.focus_window(info)
    except windowing.WindowError as exc:
        return mcp_error(exc.code, "send_input", exc.message, recovery=exc.recovery)

    if not focused:
        return mcp_error(
            "FOCUS_REFUSED",
            "send_input",
            "The target window could not take the foreground, so nothing was injected.",
            recovery=[
                "Another window refused to give up the foreground.",
                "Ask the user to click the target window once, then retry; "
                "do not retry in a loop.",
            ],
        )

    refreshed = windowing.find_window(window)

    if refreshed is None:
        return mcp_error(
            "WINDOW_NOT_FOUND",
            "send_input",
            "The window disappeared once it was focused.",
            recovery=["Call list_windows to check what is visible now."],
        )

    info = refreshed

    settled = max(0, min(settle_ms, MAX_SETTLE_MS))

    started = time.perf_counter()

    stem = _sanitize_stem(info.process) or "window"

    try:
        before = windowing.capture_window_frame(info)
    except windowing.WindowError as exc:
        return mcp_error(exc.code, "send_input", exc.message, recovery=exc.recovery)

    completed = 0

    try:
        for parsed in validated:
            # Re-check the foreground around every action: once focus is
            # lost, inject nothing more into whatever the user switched to.
            if windowing.foreground_hwnd() != info.hwnd:
                return mcp_error(
                    "FOCUS_LOST",
                    "send_input",
                    "Focus was lost mid-sequence; stopped immediately so no "
                    "event lands in whatever the user switched to.",
                    details={"completed": completed},
                    recovery=[
                        "Decide from the before/after captures what already "
                        "landed, then ask the user to re-focus the window "
                        "before sending more input.",
                        "Do not continue the remaining actions; they were NOT sent.",
                    ],
                )

            if parsed["type"] == "click":
                _run_click(parsed, info)
            elif parsed["type"] == "drag":
                _run_drag(parsed, info)
            else:
                _run_key(parsed)

            completed += 1
    except windowing.WindowError as exc:
        return mcp_error(exc.code, "send_input", exc.message, recovery=exc.recovery)

    time.sleep(settled / 1000)

    try:
        after = windowing.capture_window_frame(info)
    except windowing.WindowError as exc:
        return mcp_error(exc.code, "send_input", exc.message, recovery=exc.recovery)

    try:
        before_path = _save_capture(before, stem, "before")
        after_path = _save_capture(after, stem, "after")
    except OSError as exc:
        return mcp_error(
            "WRITE_FAILED",
            "send_input",
            str(exc),
            recovery=[
                "Check the filesystem state before retrying.",
                "Do not repeatedly retry the identical injection.",
            ],
        )

    delta = windowing.frame_delta(before, after)

    payload: dict[str, object] = {
        "ok": True,
        "window": {
            "process": info.process,
            "title": info.title,
            "hwnd": info.hwnd,
            "client_width": info.client_width,
            "client_height": info.client_height,
        },
        "actions": completed,
        "focus": focus_kind,
        "before": before_path,
        "after": after_path,
        "delta": delta,
        "delta_note": (
            "sampled pixel ratio; scene animation contributes — compare, "
            "do not threshold"
        ),
        "settle_ms": settled,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "foreground_after": windowing.foreground_hwnd() == info.hwnd,
    }

    return json.dumps(payload, indent=2)
