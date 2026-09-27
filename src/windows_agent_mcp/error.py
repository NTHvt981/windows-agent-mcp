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
    details: dict[str, Any] | None = None,
) -> str:
    """
    Return a structured error for the AI agent.

    The response deliberately contains explicit recovery instructions
    so the model does not blindly repeat a failed operation.

    Args:
        error_type: Machine-readable error code, e.g. "BUILD_TIMED_OUT".
        tool: Tool that raised the error.
        message: Human-readable description of what went wrong.
        path: Related filesystem path, when there is one.
        recovery: Steps the model should take instead of retrying blindly.
        details: Extra machine-readable fields merged into the error object.
            A timeout uses this to carry the pid, elapsed_ms and status so a
            caller can act on the failure without parsing the prose.
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

    # Merged after the fixed fields so an error can carry as many
    # machine-readable fields as the caller needs without changing the
    # envelope shape; callers use key names of their own.
    if details:
        error.update(details)

    return json.dumps(
        {
            "ok": False,
            "error": error,
        },
        indent=2,
    )
