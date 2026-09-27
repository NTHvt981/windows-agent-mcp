from __future__ import annotations

from ..error import mcp_error
from ..log import log
from ..search_backends import get_backend, log_search
from ..untrusted import research_disabled_error, wrap_untrusted
from ..utils import MAX_SEARCH_RESULTS, web_search_enabled

__all__: list[str] = ["web_search"]

MAX_QUERY_CHARS = 400


def web_search(query: str, max_results: int = MAX_SEARCH_RESULTS) -> str:
    """Search the web and return titles, URLs and snippets."""

    if not web_search_enabled():
        return research_disabled_error("web_search")

    query = query.strip()

    if not query:
        return mcp_error(
            "INVALID_QUERY",
            "web_search",
            "The search query cannot be empty.",
            recovery=[
                "Do not retry with an empty query.",
                "Provide the terms you want to search for.",
            ],
        )

    if len(query) > MAX_QUERY_CHARS:
        return mcp_error(
            "INVALID_QUERY",
            "web_search",
            f"The query is longer than {MAX_QUERY_CHARS} characters.",
            recovery=[
                "Do not retry the same query.",
                "Search for a few keywords rather than a whole passage.",
            ],
        )

    max_results = max(1, min(int(max_results), 10))

    try:
        backend = get_backend()
    except ValueError as exc:
        return mcp_error(
            "SEARCH_BACKEND_INVALID",
            "web_search",
            str(exc),
            recovery=[
                "DO NOT retry: this is a server misconfiguration, not a bad query.",
                "Tell the user the WAMCP_SEARCH_BACKEND value is not valid.",
            ],
        )

    try:
        outcome = backend.search(query, max_results)
    except ValueError as exc:
        return mcp_error(
            "SEARCH_URL_NOT_ALLOWED",
            "web_search",
            str(exc),
            recovery=[
                "DO NOT retry: the search endpoint itself was refused.",
                "Tell the user the search backend is misconfigured.",
            ],
        )
    except Exception as exc:
        log.exception("web_search failed")

        return mcp_error(
            "SEARCH_FAILED",
            "web_search",
            f"{type(exc).__name__}: {exc}",
            recovery=[
                "Do not repeatedly retry the identical query.",
                "Check that the machine has network access.",
            ],
        )

    log_search(query, outcome)

    if outcome.status == "blocked":
        return mcp_error(
            "SEARCH_BLOCKED",
            "web_search",
            f"The search provider did not return results: {outcome.detail}.",
            recovery=[
                "DO NOT retry immediately and DO NOT rephrase the query -- "
                "the query was not the problem.",
                "Wait and try once more, or tell the user search is being "
                "rate limited and ask how to proceed.",
            ],
        )

    if outcome.status == "empty":
        return f"No results for {query!r}. Try different or broader terms."

    lines: list[str] = []

    for index, result in enumerate(outcome.results, start=1):
        lines.append(f"{index}. {result.title}")
        lines.append(f"   {result.url}")

        if result.snippet:
            lines.append(f"   {result.snippet}")

        lines.append("")

    return wrap_untrusted("\n".join(lines).rstrip(), source=f"web search for {query!r}")
