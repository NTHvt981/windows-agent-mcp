"""Ask the operator, mid-tool-call, whether a host may be read."""

from __future__ import annotations

import os
from typing import Literal, NamedTuple

import anyio
from mcp.server.mcpserver import Context
from mcp_types import ClientCapabilities, ElicitationCapability
from pydantic import BaseModel, Field

from .hostgrants import persist_enabled
from .log import log

__all__: list[str] = [
    "CONSENT_ENV_VAR",
    "CONSENT_TIMEOUT_SECONDS",
    "ConsentOutcome",
    "client_supports_elicitation",
    "consent_available",
    "request_host_grant",
]

# A prompt the model triggers is one an injected page triggers.
CONSENT_ENV_VAR: str = "WAMCP_HOST_CONSENT"

_FALSY_VALUES = frozenset({"0", "false", "no", "off"})

CONSENT_TIMEOUT_SECONDS: float = 120.0


class ConsentOutcome(NamedTuple):
    """What came back from asking."""

    granted: bool
    persist: bool
    detail: str


class _AllowHost(BaseModel):
    allow: bool = Field(
        description="Allow this server to read pages from this host?",
    )


class _AllowHostPersistently(BaseModel):
    decision: Literal["no", "session", "always"] = Field(
        description=(
            "no = refuse; session = allow until this server restarts; "
            "always = allow and remember it in mcp-allowed-hosts.json"
        ),
    )


def client_supports_elicitation(ctx: Context) -> bool:
    """Whether the connected client declared the elicitation capability."""

    try:
        return ctx.session.check_client_capability(
            ClientCapabilities(elicitation=ElicitationCapability())
        )
    except Exception as exc:  # pragma: no cover - defensive
        # No session means no, never an error.
        log.debug("could not read client capabilities: %s", exc)
        return False


def _prompt(host: str, url: str, *, persistable: bool) -> str:
    """Compose the text the human reads."""

    scope = (
        "Choose 'session' to allow until the server restarts, or 'always' to "
        "remember it."
        if persistable
        else "Allowing lasts until this server restarts."
    )

    return (
        f"The assistant wants to read a web page from {host}.\n\n"
        f"URL: {_shorten(url)}\n\n"
        f"This grants reading pages from {host} into the conversation. It does "
        f"not allow downloading files, and it does not affect any other host. "
        f"{scope}\n\n"
        f"If you did not ask for anything involving {host}, say no: a request "
        f"for an unexpected host can mean a page the assistant already read "
        f"told it to fetch one."
    )


def _shorten(url: str, limit: int = 200) -> str:
    """Trim a URL for display."""

    return url if len(url) <= limit else f"{url[:limit]}..."


async def request_host_grant(ctx: Context, host: str, url: str) -> ConsentOutcome:
    """Ask the operator whether `host` may be read."""

    if not consent_available():
        return ConsentOutcome(False, False, f"{CONSENT_ENV_VAR} is off")

    if not client_supports_elicitation(ctx):
        return ConsentOutcome(False, False, "client does not support elicitation")

    persistable = persist_enabled()

    message = _prompt(host, url, persistable=persistable)

    try:
        with anyio.fail_after(CONSENT_TIMEOUT_SECONDS):
            outcome = (
                await _ask_persistent(ctx, message)
                if persistable
                else await _ask_session(ctx, message)
            )
    except TimeoutError:
        log.warning(
            "no answer within %.0fs to the request to read %s",
            CONSENT_TIMEOUT_SECONDS,
            host,
        )
        return ConsentOutcome(False, False, "no answer in time")
    except Exception as exc:
        log.warning("could not ask about %s: %s", host, exc)
        return ConsentOutcome(False, False, f"could not ask: {exc}")

    return outcome


async def _ask_session(ctx: Context, message: str) -> ConsentOutcome:

    result = await ctx.elicit(message=message, schema=_AllowHost)

    if result.action != "accept":
        return ConsentOutcome(False, False, result.action)

    if not result.data.allow:
        return ConsentOutcome(False, False, "declined")

    return ConsentOutcome(True, False, "session")


async def _ask_persistent(ctx: Context, message: str) -> ConsentOutcome:

    result = await ctx.elicit(message=message, schema=_AllowHostPersistently)

    if result.action != "accept":
        return ConsentOutcome(False, False, result.action)

    decision = result.data.decision

    if decision == "no":
        return ConsentOutcome(False, False, "declined")

    return ConsentOutcome(True, decision == "always", decision)


def consent_available() -> bool:
    """Whether this server is willing to ask at all."""

    # Read every call, so profiles can set it.

    return os.environ.get(CONSENT_ENV_VAR, "1").strip().lower() not in _FALSY_VALUES
