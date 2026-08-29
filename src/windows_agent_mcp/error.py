import json
from typing import Any

# ============================================================
# Error handler
# ============================================================


def mcp_error(
    error_type: str,
    tool: str,
    message: str,
    *,
    path: str | None = None,
    recovery: list[str] | None = None,
) -> str:
    """
    Return a structured error for the AI agent.

    The response deliberately contains explicit recovery instructions
    so the model does not blindly repeat a failed operation.
    """

    error: dict[str, Any] = {
        "type": error_type,
        "tool": tool,
        "message": message,
    }

    if path is not None:
        error["path"] = path

    if recovery:
        error["recovery"] = recovery

    return json.dumps(
        {
            "ok": False,
            "error": error,
        },
        indent=2,
    )
