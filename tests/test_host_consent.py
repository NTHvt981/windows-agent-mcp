"""Tests for asking the operator mid-call whether a host may be read.

The design claim being tested: where the client supports MCP elicitation,
fetch_web_page can turn a refusal into a question, and every way that question
can fail degrades to the same structured refusal the model already knows how to
report.

Nothing here touches a live MCP session. The context is a double exposing only
the two things consent.py uses -- `session.check_client_capability` and
`elicit` -- which is also a check that it uses no more than that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from conftest import FakeResponse, assert_error_response
from mcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)

from windows_agent_mcp import consent, hostgrants
from windows_agent_mcp.consent import (
    ConsentOutcome,
    _AllowHost,
    _AllowHostPersistently,
    _prompt,
    request_host_grant,
)
from windows_agent_mcp.tools.fetch_web_page import clear_cache, fetch_web_page

HOST = "granted.example.com"
URL = f"https://{HOST}/article"


def page() -> FakeResponse:
    """A fresh response every time.

    A module-level constant would be a trap: FakeResponse tracks a read offset,
    so a shared instance is empty for every test after the first -- and the
    symptom is a page that fetches successfully with no content, which looks
    like an extraction bug.
    """

    return FakeResponse(
        b"<html><head><title>Allowed</title></head>"
        b"<body><p>Body text</p></body></html>",
        {"Content-Type": "text/html"},
    )


class FakeSession:
    """Only check_client_capability, which is all consent.py may use."""

    def __init__(self, *, supports: bool = True):
        self.supports = supports
        self.asked: list[Any] = []

    def check_client_capability(self, capability: Any) -> bool:
        self.asked.append(capability)
        return self.supports


class FakeContext:
    """A stand-in for mcp.server.mcpserver.Context.

    Records every elicitation so a test can assert on the message the human
    would have seen, and on how many times it was asked -- a consent loop that
    re-asked per attempt would be a serious annoyance and is easy to write.
    """

    def __init__(
        self,
        *results: Any,
        supports: bool = True,
        delay: float = 0.0,
    ):
        self.session = FakeSession(supports=supports)
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []
        self.delay = delay

    async def elicit(self, message: str, schema: Any) -> Any:
        self.calls.append({"message": message, "schema": schema})

        if self.delay:
            await anyio.sleep(self.delay)

        if not self._results:
            raise AssertionError("elicit called more times than expected")

        result = self._results.pop(0)

        if isinstance(result, Exception):
            raise result

        return result


def accepted(allow: bool = True) -> AcceptedElicitation[_AllowHost]:
    return AcceptedElicitation(data=_AllowHost(allow=allow))


def accepted_decision(decision: str) -> AcceptedElicitation[_AllowHostPersistently]:
    return AcceptedElicitation(
        data=_AllowHostPersistently(decision=decision)  # type: ignore[arg-type]
    )


def run(coroutine: Any) -> Any:
    """Drive one coroutine to completion.

    anyio.run rather than a pytest async plugin: the suite has no async plugin
    and adding a dependency for six tests is not worth it.
    """

    return anyio.run(lambda: coroutine)


# ============================================================
# When nothing should be asked
# ============================================================


def test_no_context_means_no_question(page_opener, resolves_public: None) -> None:
    """A direct call, or a client that passes no context.

    The tool still works; it just cannot ask. This is also the path every
    non-elicitation client takes.
    """

    clear_cache()
    page_opener()

    result = run(fetch_web_page(URL))

    assert_error_response(result, "URL_NOT_ALLOWED")


def test_a_client_without_the_capability_is_not_asked(
    page_opener, resolves_public: None
) -> None:
    """Checked locally, so an unsupporting client costs no round trip."""

    clear_cache()
    page_opener()

    ctx = FakeContext(supports=False)

    assert_error_response(run(fetch_web_page(URL, ctx=ctx)), "URL_NOT_ALLOWED")
    assert ctx.calls == []
    assert ctx.session.asked, "capability should have been checked"


def test_the_operator_can_switch_asking_off(
    page_opener, resolves_public: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt the model can trigger is a prompt an injected page can trigger.

    Turning it off must leave the file mechanism working, which is why this is
    a separate switch from the grants file rather than a mode.
    """

    monkeypatch.setenv("WAMCP_HOST_CONSENT", "0")

    clear_cache()
    page_opener()

    ctx = FakeContext(accepted())

    assert_error_response(run(fetch_web_page(URL, ctx=ctx)), "URL_NOT_ALLOWED")
    assert ctx.calls == []
    # Not even the capability check: the decision is ours, not the client's.
    assert ctx.session.asked == []


def test_a_successful_fetch_asks_nothing(page_opener, resolves_public: None) -> None:
    clear_cache()
    page_opener(page())

    ctx = FakeContext()

    result = run(fetch_web_page("https://cmake.org/documentation/", ctx=ctx))

    assert "Body text" in result
    assert ctx.calls == []


def test_a_refusal_a_grant_cannot_fix_asks_nothing(
    page_opener, resolves_public: None
) -> None:
    """An http:// URL is refused for its scheme.

    Asking here would offer the operator a permission that changes nothing --
    and for a private address it would be inviting them to approve an SSRF
    probe.
    """

    clear_cache()
    page_opener()

    ctx = FakeContext()

    assert_error_response(
        run(fetch_web_page("http://granted.example.com/x", ctx=ctx)),
        "URL_NOT_ALLOWED",
    )
    assert ctx.calls == []


def test_research_mode_needs_no_consent(
    page_opener, resolves_public: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAMCP_WEB_RESEARCH", "1")

    clear_cache()
    page_opener(page())

    ctx = FakeContext()

    assert "Body text" in run(fetch_web_page(URL, ctx=ctx))
    assert ctx.calls == []


# ============================================================
# Answering yes
# ============================================================


def test_an_approval_grants_the_host_and_retries(
    page_opener, resolves_public: None
) -> None:
    """The whole point: one question, then the page.

    Exactly one response is queued, and FakeOpener raises if opened twice. So
    this also proves the first attempt never reached the wire: the host check
    happens before any network use, which is what keeps the refusal cheap.
    """

    clear_cache()
    page_opener(page())

    ctx = FakeContext(accepted())

    result = run(fetch_web_page(URL, ctx=ctx))

    assert "Allowed" in result
    assert "Body text" in result
    assert len(ctx.calls) == 1, "the operator must be asked exactly once"
    assert hostgrants.session_grants() == {HOST}


def test_an_approval_is_not_written_to_disk_by_default(
    page_opener, resolves_public: None, grants_file: Path
) -> None:
    """The spec lets an agentic client answer for the user.

    So an approval is not proof a human saw the prompt, and the default grant
    has to be one that dies with the process.
    """

    clear_cache()
    page_opener(page())

    run(fetch_web_page(URL, ctx=FakeContext(accepted())))

    assert hostgrants.session_grants() == {HOST}
    assert not grants_file.exists()


def test_a_second_read_of_a_granted_host_asks_again_only_if_forgotten(
    page_opener, resolves_public: None
) -> None:
    """Once granted for the session, later reads go straight through."""

    clear_cache()
    page_opener(page(), page())

    ctx = FakeContext(accepted())

    run(fetch_web_page(URL, ctx=ctx))
    clear_cache()
    run(fetch_web_page(f"https://{HOST}/other", ctx=ctx))

    assert len(ctx.calls) == 1


# ============================================================
# Answering no, or not at all
# ============================================================


@pytest.mark.parametrize(
    "result",
    [
        DeclinedElicitation(),
        CancelledElicitation(),
        RuntimeError("transport went away"),
    ],
    ids=["declined", "cancelled", "raised"],
)
def test_every_refusal_path_returns_the_denial(
    result: Any, page_opener, resolves_public: None
) -> None:
    """Declined, cancelled and broken all mean the same thing to the model."""

    clear_cache()
    page_opener()

    ctx = FakeContext(result)

    assert_error_response(run(fetch_web_page(URL, ctx=ctx)), "URL_NOT_ALLOWED")
    assert hostgrants.session_grants() == frozenset()


def test_saying_no_explicitly_is_not_an_approval(
    page_opener, resolves_public: None
) -> None:
    """`accept` with allow=false is a real answer, and it is "no".

    Worth its own test: treating any accepted elicitation as consent is the
    obvious bug here, and it would turn every prompt into a rubber stamp.
    """

    clear_cache()
    page_opener()

    ctx = FakeContext(accepted(allow=False))

    assert_error_response(run(fetch_web_page(URL, ctx=ctx)), "URL_NOT_ALLOWED")
    assert hostgrants.session_grants() == frozenset()


def test_an_unanswered_question_times_out(
    monkeypatch: pytest.MonkeyPatch, page_opener, resolves_public: None
) -> None:
    """A client that displays the prompt and is then left alone.

    Without the timeout the tool call never returns, and from the model's side
    that is indistinguishable from a dead server.
    """

    monkeypatch.setattr(consent, "CONSENT_TIMEOUT_SECONDS", 0.01)

    clear_cache()
    page_opener()

    ctx = FakeContext(accepted(), delay=5.0)

    assert_error_response(run(fetch_web_page(URL, ctx=ctx)), "URL_NOT_ALLOWED")
    assert hostgrants.session_grants() == frozenset()


# ============================================================
# Remembering an approval
# ============================================================


def test_always_writes_the_grant_to_the_file(
    page_opener, resolves_public: None, grants_file: Path, monkeypatch
) -> None:
    monkeypatch.setenv("WAMCP_HOST_GRANT_PERSIST", "1")

    clear_cache()
    page_opener(page())

    ctx = FakeContext(accepted_decision("always"))

    assert "Body text" in run(fetch_web_page(URL, ctx=ctx))

    data = json.loads(grants_file.read_text(encoding="utf-8"))

    assert [entry["host"] for entry in data["hosts"]] == [HOST]


def test_session_does_not_write_the_grant_to_the_file(
    page_opener, resolves_public: None, grants_file: Path, monkeypatch
) -> None:
    monkeypatch.setenv("WAMCP_HOST_GRANT_PERSIST", "1")

    clear_cache()
    page_opener(page())

    ctx = FakeContext(accepted_decision("session"))

    assert "Body text" in run(fetch_web_page(URL, ctx=ctx))

    assert hostgrants.session_grants() == {HOST}
    assert not grants_file.exists()


def test_no_in_the_three_way_question_is_still_no(
    page_opener, resolves_public: None, monkeypatch
) -> None:
    monkeypatch.setenv("WAMCP_HOST_GRANT_PERSIST", "1")

    clear_cache()
    page_opener()

    ctx = FakeContext(accepted_decision("no"))

    assert_error_response(run(fetch_web_page(URL, ctx=ctx)), "URL_NOT_ALLOWED")
    assert hostgrants.session_grants() == frozenset()


def test_a_failed_write_does_not_lose_the_session_grant(
    page_opener, resolves_public: None, grants_file: Path, monkeypatch
) -> None:
    """Remembering failed; reading was still approved.

    Refusing the fetch because the file could not be updated would punish the
    user for a filesystem problem they did not cause.
    """

    monkeypatch.setenv("WAMCP_HOST_GRANT_PERSIST", "1")

    grants_file.write_text("{not json", encoding="utf-8")

    clear_cache()
    page_opener(page())

    ctx = FakeContext(accepted_decision("always"))

    assert "Body text" in run(fetch_web_page(URL, ctx=ctx))
    assert hostgrants.session_grants() == {HOST}


def test_the_question_matches_the_persistence_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offering "always" while persistence is off would be a lie."""

    ctx = FakeContext(accepted())
    run(request_host_grant(ctx, HOST, URL))
    assert ctx.calls[0]["schema"] is _AllowHost

    monkeypatch.setenv("WAMCP_HOST_GRANT_PERSIST", "1")

    ctx = FakeContext(accepted_decision("session"))
    run(request_host_grant(ctx, HOST, URL))
    assert ctx.calls[0]["schema"] is _AllowHostPersistently


# ============================================================
# What the human is shown
# ============================================================


def test_the_prompt_names_the_host_and_warns_about_injection() -> None:
    """The text is the whole security value of the feature.

    A prompt that says only "allow?" trains the operator to click yes. It has
    to say what is being granted, what it is not, and that an unexpected host
    can mean a page the model already read told it to fetch one.
    """

    message = _prompt(HOST, URL, persistable=False)

    assert HOST in message
    assert URL in message
    assert "does not allow downloading files" in message
    assert "told it to fetch one" in message
    assert "until this server restarts" in message


def test_the_prompt_offers_always_only_when_it_is_available() -> None:
    assert "always" in _prompt(HOST, URL, persistable=True)
    assert "always" not in _prompt(HOST, URL, persistable=False)


def test_a_very_long_url_is_trimmed_for_display() -> None:
    """The URL is model-supplied. It should not be able to fill the dialog."""

    message = _prompt(HOST, f"https://{HOST}/" + "x" * 5000, persistable=False)

    assert "..." in message
    assert len(message) < 1000


def test_outcomes_report_why_they_failed() -> None:
    """The detail is for the operator reading stderr, not for the model."""

    outcome = run(request_host_grant(FakeContext(supports=False), HOST, URL))

    assert outcome == ConsentOutcome(
        False, False, "client does not support elicitation"
    )


# ============================================================
# Registration
# ============================================================


def test_the_context_parameter_is_not_part_of_the_tool_schema() -> None:
    """`ctx` is injected by the SDK, so a model must never see it as an argument.

    If it leaked into the schema a small model would try to fill it in, and the
    call would fail validation for reasons nothing in the error would explain.
    """

    from mcp.server import MCPServer

    from windows_agent_mcp.main import get_tools, tool_description

    server = MCPServer("test")

    for tool in get_tools():
        server.add_tool(tool, description=tool_description(tool))

    async def read_schema() -> dict[str, Any]:
        for tool in await server.list_tools():
            if tool.name == "fetch_web_page":
                return dict(tool.input_schema)

        raise AssertionError("fetch_web_page was not registered")

    schema = anyio.run(read_schema)

    assert set(schema["properties"]) == {"url", "start_line", "max_lines"}
    assert schema["required"] == ["url"]


def test_a_cancelled_three_way_question_is_not_an_approval(
    page_opener, resolves_public: None, monkeypatch
) -> None:
    """Cancelling the persistable prompt has its own branch to get wrong."""

    monkeypatch.setenv("WAMCP_HOST_GRANT_PERSIST", "1")

    clear_cache()
    page_opener()

    ctx = FakeContext(CancelledElicitation())

    assert_error_response(run(fetch_web_page(URL, ctx=ctx)), "URL_NOT_ALLOWED")
    assert hostgrants.session_grants() == frozenset()
