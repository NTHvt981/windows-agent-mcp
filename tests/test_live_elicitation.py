"""End-to-end consent through a real MCP client session.

Every other consent test drives `fetch_web_page` directly with a hand-written
context double. That covers the branches but proves nothing about the protocol:
a Context that never reaches the wire cannot show that the SDK injects it, that
capability negotiation works, that the elicitation schema survives
serialisation, or that an approval arrives as a shape we actually accept.

So this module wires a genuine `ClientSession` to a genuine `MCPServer` over
in-memory streams and calls the tool the way a client does -- by name, with a
JSON argument dict. What runs here is the real request/response path; only the
socket is replaced.

The HTTP layer is still stubbed. This is about the consent protocol, not about
reaching any website.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest
from conftest import FakeOpener, FakeResponse
from mcp.client.session import ClientSession
from mcp.server import MCPServer
from mcp.shared.memory import create_client_server_memory_streams
from mcp_types import ElicitRequestParams, ElicitResult

from windows_agent_mcp import hostgrants
from windows_agent_mcp.main import get_tools, tool_description
from windows_agent_mcp.tools import fetch_web_page as page_module
from windows_agent_mcp.tools.fetch_web_page import clear_cache

HOST = "granted.example.com"
URL = f"https://{HOST}/article"

BODY = b"<html><head><title>Allowed</title></head><body><p>Body text</p></body></html>"


def build_server() -> MCPServer:
    """A server carrying the real tool set, registered the way main() does."""

    server = MCPServer("Windows Agent MCP")

    for tool in get_tools():
        server.add_tool(tool, description=tool_description(tool))

    return server


def text_of(result: Any) -> str:
    """Flatten a CallToolResult into the text the model would see."""

    return "\n".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


async def call_tool(
    elicitation_callback: Any,
    arguments: dict[str, Any] | None = None,
) -> tuple[str, list[ElicitRequestParams]]:
    """Run one fetch_web_page call through a live client/server pair.

    Args:
        elicitation_callback: Passed to ClientSession. Passing one is also what
            makes the client DECLARE the elicitation capability, so `None` here
            is a faithful stand-in for the many clients that do not implement
            it.
        arguments: Tool arguments. Defaults to the ungranted URL.

    Returns:
        (tool output text, the elicitation params the client received).
    """

    seen: list[ElicitRequestParams] = []

    wrapped = None

    if elicitation_callback is not None:

        async def wrapped(context: Any, params: ElicitRequestParams) -> Any:  # noqa: F811
            seen.append(params)
            return await elicitation_callback(context, params)

    server = build_server()

    output = ""

    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async with anyio.create_task_group() as group:

            async def run_server() -> None:
                await server._lowlevel_server.run(
                    server_read,
                    server_write,
                    server._lowlevel_server.create_initialization_options(),
                )

            group.start_soon(run_server)

            async with ClientSession(
                client_read,
                client_write,
                elicitation_callback=wrapped,
            ) as session:
                await session.initialize()

                result = await session.call_tool(
                    "fetch_web_page",
                    arguments or {"url": URL},
                )

                output = text_of(result)

            group.cancel_scope.cancel()

    return output, seen


async def approve(_context: Any, _params: ElicitRequestParams) -> ElicitResult:
    return ElicitResult(action="accept", content={"allow": True})


async def refuse(_context: Any, _params: ElicitRequestParams) -> ElicitResult:
    return ElicitResult(action="accept", content={"allow": False})


async def decline(_context: Any, _params: ElicitRequestParams) -> ElicitResult:
    return ElicitResult(action="decline")


@pytest.fixture(autouse=True)
def stub_http(monkeypatch: pytest.MonkeyPatch, resolves_public: None):
    """Serve one page, without a network.

    Installed on the tool module for both openers, matching how the rest of the
    suite stubs this: which opener the tool picks depends on research mode.
    """

    clear_cache()

    fake = FakeOpener(FakeResponse(BODY, {"Content-Type": "text/html"}))

    monkeypatch.setattr(page_module, "RESEARCH_HTTP_OPENER", fake)
    monkeypatch.setattr(page_module, "DOC_HTTP_OPENER", fake)

    return fake


def test_the_tool_is_callable_over_the_protocol() -> None:
    """The baseline: a granted host, fetched by name through a real session.

    If the async signature or the injected context parameter were wrong, this
    is what would fail -- and it would fail for every client, not just ones
    that support elicitation.
    """

    hostgrants.grant_for_session(HOST)

    output, asked = anyio.run(call_tool, None)

    assert "Body text" in output
    assert asked == []


def test_an_approval_over_the_wire_unblocks_the_fetch() -> None:
    """The whole feature, through the real protocol.

    A refused host, a genuine elicitation/create round trip, an accept carrying
    a JSON object, and the page comes back in the same tool call.
    """

    output, asked = anyio.run(call_tool, approve)

    assert "Body text" in output
    assert len(asked) == 1
    assert hostgrants.session_grants() == {HOST}


def test_the_question_reaching_the_client_is_the_one_we_wrote() -> None:
    """The message and schema survive serialisation intact.

    Worth asserting here rather than only against the double: the schema is
    rendered by the SDK and validated against the spec's
    PrimitiveSchemaDefinition on the way out, so a field type the spec rejects
    would fail here and nowhere else.
    """

    _output, asked = anyio.run(call_tool, approve)

    params = asked[0]

    assert HOST in params.message
    assert "does not allow downloading files" in params.message
    assert "told it to fetch one" in params.message

    schema = params.requested_schema

    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"allow"}
    assert schema["properties"]["allow"]["type"] == "boolean"


def test_saying_no_over_the_wire_leaves_the_refusal_in_place() -> None:
    output, asked = anyio.run(call_tool, refuse)

    payload = json.loads(output)

    assert payload["ok"] is False
    assert payload["error"]["type"] == "URL_NOT_ALLOWED"
    assert len(asked) == 1
    assert hostgrants.session_grants() == frozenset()


def test_declining_over_the_wire_leaves_the_refusal_in_place() -> None:
    output, _asked = anyio.run(call_tool, decline)

    assert json.loads(output)["error"]["type"] == "URL_NOT_ALLOWED"
    assert hostgrants.session_grants() == frozenset()


def test_a_client_that_cannot_elicit_gets_the_refusal_and_the_command() -> None:
    """The path most real clients take today.

    Capability negotiation is genuine here: no elicitation_callback means the
    client does not declare the capability, so the server must not attempt the
    round trip -- and the refusal has to carry the command a human can run.
    """

    output, asked = anyio.run(call_tool, None)

    payload = json.loads(output)

    assert payload["error"]["type"] == "URL_NOT_ALLOWED"
    assert asked == []

    recovery = " ".join(payload["error"]["recovery"])

    assert f"hostgrants --add {HOST}" in recovery


def test_a_grants_file_host_needs_no_question(grants_file) -> None:
    """The file mechanism, end to end, with a client that COULD have been asked."""

    grants_file.write_text(
        json.dumps({"version": 1, "hosts": [HOST]}),
        encoding="utf-8",
    )

    output, asked = anyio.run(call_tool, approve)

    assert "Body text" in output
    assert asked == []


def test_the_context_parameter_is_absent_from_the_advertised_schema() -> None:
    """What a client actually receives in tools/list.

    The earlier check read the schema off the server object; this one reads it
    from the listing that crosses the wire, which is what a model is shown.
    """

    async def listing() -> dict[str, Any]:
        server = build_server()

        async with create_client_server_memory_streams() as (
            client_streams,
            server_streams,
        ):
            client_read, client_write = client_streams
            server_read, server_write = server_streams

            async with anyio.create_task_group() as group:

                async def run_server() -> None:
                    await server._lowlevel_server.run(
                        server_read,
                        server_write,
                        server._lowlevel_server.create_initialization_options(),
                    )

                group.start_soon(run_server)

                async with ClientSession(client_read, client_write) as session:
                    await session.initialize()

                    tools = await session.list_tools()

                group.cancel_scope.cancel()

        for tool in tools.tools:
            if tool.name == "fetch_web_page":
                return dict(tool.input_schema)

        raise AssertionError("fetch_web_page was not advertised")

    schema = anyio.run(listing)

    assert set(schema["properties"]) == {"url", "start_line", "max_lines"}
    assert "ctx" not in schema["properties"]
