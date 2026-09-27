import json
from typing import Any


def mcp_error(
    error_type: str,
    tool: str,
    message: str,
    *,
    path: str | None = None,
    recovery: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> str:
    """Return a structured error for the AI agent."""

    error: dict[str, Any] = {
        "type": error_type,
        "tool": tool,
        "message": message,
    }

    if path is not None:
        error["path"] = path

    if recovery:
        error["recovery"] = recovery

    if details:
        error.update(details)

    return json.dumps(
        {
            "ok": False,
            "error": error,
        },
        indent=2,
    )
