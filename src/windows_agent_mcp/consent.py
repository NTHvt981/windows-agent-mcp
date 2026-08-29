"""Ask the operator, mid-tool-call, whether a host may be read.

MCP's elicitation feature lets a server pause a tool call and put a question to
the human, then continue with the answer. That is exactly the missing step in
the web-fetch story: the denial message could always say "ask the user", but
the model had no channel to ask through and the user had no answer to give
short of restarting the server with research mode on.

Kept in its own module because it is the only part of the tool layer coupled to
the server SDK. `fetch_web_page` imports one function from here; everything
else about fetching stays testable without a live MCP session.

Two limits worth knowing before relying on this:

* **Most clients do not implement elicitation yet.** The capability is checked
  first and an absent one is not an error -- the tool falls back to the
  structured denial, which now names the exact command that grants the host.
  The file mechanism in `hostgrants` is the path that works everywhere; this is
  the nicer path where it is available.
* **An accepted elicitation is not proof a human saw it.** The spec explicitly
  allows an agentic client to answer on the user's behalf. That is why an
  approval here grants for the current process only, unless the operator has
  set BIONIC_HOST_GRANT_PERSIST=1 to say their client really does ask a person.
"""

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

# Set to 0 to stop the server ever prompting, leaving the grants file as the
# only way to approve a host.
#
# A real posture rather than a hypothetical one: a prompt the model can trigger
# is a prompt an injected page can trigger, and an operator who does not want
# to be asked repeatedly -- or does not want a habit of clicking "allow" --
# should be able to turn the question off without losing the file mechanism.
CONSENT_ENV_VAR: str = "BIONIC_HOST_CONSENT"

_FALSY_VALUES = frozenset({"0", "false", "no", "off"})

# How long to wait for an answer before giving up and returning the denial.
#
# There has to be a limit. Without one, a client that displays the prompt and
# is then left alone -- or one that acknowledges elicitation support and never
# answers -- hangs the tool call indefinitely, and from the model's side that
# is indistinguishable from a server that has died mid-conversation.
CONSENT_TIMEOUT_SECONDS: float = 120.0


class ConsentOutcome(NamedTuple):
    """What came back from asking.

    Attributes:
        granted: Whether the host may now be read.
        persist: Whether the operator asked for the grant to be remembered.
        detail: Short reason, for the log and for the denial message. Written
            for a human reading stderr, not for the model.
    """

    granted: bool
    persist: bool
    detail: str


class _AllowHost(BaseModel):
    """Schema for the session-only question."""

    allow: bool = Field(
        description="Allow this server to read pages from this host?",
    )


class _AllowHostPersistently(BaseModel):
    """Schema for the question when persisting is enabled."""

    decision: Literal["no", "session", "always"] = Field(
        description=(
            "no = refuse; session = allow until this server restarts; "
            "always = allow and remember it in mcp-allowed-hosts.json"
        ),
    )


def client_supports_elicitation(ctx: Context) -> bool:
    """Whether the connected client declared the elicitation capability.

    Checked before asking so an unsupporting client costs a local lookup rather
    than a round trip that fails.

    Args:
        ctx: The tool call's context.

    Returns:
        True if the client can present an elicitation.
    """

    try:
        return ctx.session.check_client_capability(
            ClientCapabilities(elicitation=ElicitationCapability())
        )
    except Exception as exc:  # pragma: no cover - defensive
        # A context without a live session (a direct call, a test double)
        # reports "no" rather than exploding: consent is an enhancement, and
        # failing to ask must never fail the fetch.
        log.debug("could not read client capabilities: %s", exc)
        return False


def _prompt(host: str, url: str, *, persistable: bool) -> str:
    """Compose the text the human reads.

    Says what is being granted, what it is not, and why they might be seeing
    this without having asked for anything -- the last part matters, because a
    request to read an unexpected host is one of the few visible symptoms of a
    prompt injection.
    """

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
    """Trim a URL for display. It is model-supplied text, so it may be long."""

    return url if len(url) <= limit else f"{url[:limit]}..."


async def request_host_grant(ctx: Context, host: str, url: str) -> ConsentOutcome:
    """Ask the operator whether `host` may be read.

    Never raises. Every failure mode -- no capability, declined, cancelled,
    timed out, transport error -- comes back as a not-granted outcome, because
    the caller's fallback (return the denial the model already understands) is
    correct for all of them.

    Args:
        ctx: The tool call's context, which carries the client session.
        host: Hostname being requested, already normalised.
        url: The full URL, for display only.

    Returns:
        The outcome. `granted` False means carry on with the denial.
    """

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
        # Covers a client that advertised the capability and then failed on it,
        # and any transport-level problem. The fetch still returns a useful
        # denial, so this is a warning rather than an error.
        log.warning("could not ask about %s: %s", host, exc)
        return ConsentOutcome(False, False, f"could not ask: {exc}")

    return outcome


async def _ask_session(ctx: Context, message: str) -> ConsentOutcome:
    """Ask the yes/no question."""

    result = await ctx.elicit(message=message, schema=_AllowHost)

    if result.action != "accept":
        return ConsentOutcome(False, False, result.action)

    if not result.data.allow:
        return ConsentOutcome(False, False, "declined")

    return ConsentOutcome(True, False, "session")


async def _ask_persistent(ctx: Context, message: str) -> ConsentOutcome:
    """Ask the no/session/always question."""

    result = await ctx.elicit(message=message, schema=_AllowHostPersistently)

    if result.action != "accept":
        return ConsentOutcome(False, False, result.action)

    decision = result.data.decision

    if decision == "no":
        return ConsentOutcome(False, False, "declined")

    return ConsentOutcome(True, decision == "always", decision)


def consent_available() -> bool:
    """Whether this server is willing to ask at all.

    Separate from client support: this is the operator's choice, that is the
    client's capability. Read from the environment on every call rather than
    cached, so it can be set per profile.
    """

    return os.environ.get(CONSENT_ENV_VAR, "1").strip().lower() not in _FALSY_VALUES
