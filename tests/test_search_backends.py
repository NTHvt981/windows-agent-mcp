"""Tests for the search backends.

The parser is a pure function, so most of this needs no stubs. The backend
takes an injected opener, so the network path needs no monkeypatching either.
"""

from __future__ import annotations

import urllib.error
from typing import Any
from urllib.parse import quote

import pytest
from conftest import FakeOpener, FakeResponse

from windows_agent_mcp.search_backends import (
    MIN_REQUEST_INTERVAL_SECONDS,
    DuckDuckGoBackend,
    SearchOutcome,
    get_backend,
    parse_duckduckgo,
)

# ============================================================
# Fixture builders
# ============================================================

# Padding so bodies clear the "too small to be a results page" floor.
_PAD = "<!-- " + "x" * 2000 + " -->"


def _wrapped(target: str) -> str:
    """The protocol-relative uddg wrapper DuckDuckGo actually emits."""

    return f"//duckduckgo.com/l/?uddg={quote(target, safe='')}&rut=abc123"


def _results_page(*targets: str) -> str:
    rows: list[str] = []

    for index, target in enumerate(targets, start=1):
        rows.append(
            f'<tr><td><a class="result-link" href="{target}">Result {index}</a></td></tr>'
            f'<tr><td class="result-snippet">Snippet for result {index}.</td></tr>'
        )

    return f"<html><body><table>{''.join(rows)}</table></body></html>{_PAD}"


_ANOMALY_PAGE = (
    "<html><body><p>Unfortunately, bots use DuckDuckGo too. "
    f"Please try again.</p></body></html>{_PAD}"
)

_EMPTY_PAGE = (
    f"<html><body><div>No results found for that query.</div></body></html>{_PAD}"
)

_UNKNOWN_LAYOUT_PAGE = (
    f"<html><body><div class='brand-new-markup'>stuff</div></body></html>{_PAD}"
)


# ============================================================
# parse_duckduckgo -- happy path
# ============================================================


def test_parses_results() -> None:
    html = _results_page(
        _wrapped("https://docs.python.org/3/library/os.html"),
        _wrapped("https://example.com/two"),
    )

    outcome = parse_duckduckgo(html, max_results=5)

    assert outcome.status == "results"
    assert len(outcome.results) == 2
    assert outcome.results[0].url == "https://docs.python.org/3/library/os.html"
    assert outcome.results[0].title == "Result 1"
    assert outcome.results[0].snippet == "Snippet for result 1."


def test_respects_max_results() -> None:
    html = _results_page(*[_wrapped(f"https://example.com/{n}") for n in range(20)])

    outcome = parse_duckduckgo(html, max_results=3)

    assert len(outcome.results) == 3


def test_html_layout_selectors_also_work() -> None:
    """The fallback selector set for the older html/ endpoint."""

    html = (
        "<html><body>"
        '<div class="result">'
        f'<a class="result__a" href="{_wrapped("https://example.com/p")}">Title</a>'
        '<a class="result__snippet">The snippet.</a>'
        "</div></body></html>" + _PAD
    )

    outcome = parse_duckduckgo(html, max_results=5)

    assert outcome.status == "results"
    assert outcome.results[0].url == "https://example.com/p"
    assert outcome.results[0].snippet == "The snippet."


# ============================================================
# uddg unwrapping -- the three shapes plus the %25 trap
# ============================================================


@pytest.mark.parametrize(
    "href",
    [
        # Protocol-relative wrapper (urlparse sees an empty scheme).
        "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage",
        # Root-relative wrapper.
        "/l/?uddg=https%3A%2F%2Fexample.com%2Fpage",
        # Already direct.
        "https://example.com/page",
    ],
)
def test_all_three_href_shapes_unwrap(href: str) -> None:
    outcome = parse_duckduckgo(_results_page(href), max_results=5)

    assert outcome.status == "results"
    assert outcome.results[0].url == "https://example.com/page"


def test_literal_percent_is_not_double_decoded() -> None:
    """parse_qs already decodes once; unquoting again corrupts the target.

    A second unquote would turn %2520 into a space, silently producing a
    wrong-but-plausible URL.
    """

    target = "https://example.com/a%20b"

    outcome = parse_duckduckgo(_results_page(_wrapped(target)), max_results=5)

    assert outcome.results[0].url == target


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "data:text/html,<script>x</script>",
        "file:///C:/Windows/win.ini",
        "//duckduckgo.com/l/?uddg=javascript%3Aalert(1)",
        "//duckduckgo.com/l/?uddg=",
        "",
    ],
)
def test_dangerous_or_empty_targets_are_dropped(href: str) -> None:
    """A search result must never hand the model a javascript: URL."""

    outcome = parse_duckduckgo(_results_page(href), max_results=5)

    assert outcome.results == ()


def test_parsing_does_not_resolve_dns() -> None:
    """The parser must stay pure, and cheap.

    Resolving every hit would mean a DNS lookup per result, would make this
    function impure, and would make the tests non-hermetic. A result is
    checked for SHAPE here; the address check happens in fetch_web_page,
    before anything is actually retrieved.
    """

    import socket

    original = socket.getaddrinfo

    def explode(*args: Any, **kwargs: Any):
        raise AssertionError("the parser resolved DNS")

    socket.getaddrinfo = explode  # type: ignore[assignment]

    try:
        outcome = parse_duckduckgo(
            _results_page(_wrapped("https://example.com/page")), max_results=5
        )
    finally:
        socket.getaddrinfo = original  # type: ignore[assignment]

    assert outcome.status == "results"


def test_a_private_host_result_is_shown_but_not_fetchable() -> None:
    """Shape is fine, so it survives parsing -- the fetch is what refuses it.

    Covered end to end by test_web_tools.test_fetch_refuses_a_private_address.
    """

    outcome = parse_duckduckgo(
        _results_page(_wrapped("https://internal.example/admin")), max_results=5
    )

    assert outcome.results[0].url == "https://internal.example/admin"


# ============================================================
# Ads: dropped, and they must not shift the pairing
# ============================================================

# The real shape of a sponsored block, observed live: TWO anchors (the ad and
# its "more info" link) but only ONE snippet. Index-based pairing shifted
# every later result by one, printing a Stack Overflow snippet under a
# DuckDuckGo help-page title.
_AD_BLOCK = (
    "<tr><td>"
    '<a class="result-link" href="//duckduckgo.com/y.js?ad_domain=udemy.com'
    "&ad_provider=bingv7aa&ad_type=txad&click_metadata="
    + ("z" * 1200)
    + '">100 Projects In 100 Days</a>'
    "</td></tr>"
    '<tr><td class="result-snippet">Learn python like a pro. Sign up now!</td></tr>'
    "<tr><td>"
    '<a class="result-link" href="//duckduckgo.com/duckduckgo-help-pages/'
    'company/ads-by-microsoft/">more info</a>'
    "</td></tr>"
)


def _page_with_ad(*targets: str) -> str:
    rows = "".join(
        f'<tr><td><a class="result-link" href="{t}">Real {n}</a></td></tr>'
        f'<tr><td class="result-snippet">Snippet {n}.</td></tr>'
        for n, t in enumerate(targets, start=1)
    )

    return f"<html><body><table>{_AD_BLOCK}{rows}</table></body></html>{_PAD}"


def test_ads_are_dropped() -> None:
    outcome = parse_duckduckgo(
        _page_with_ad(_wrapped("https://example.com/real")), max_results=5
    )

    urls = [r.url for r in outcome.results]

    assert urls == ["https://example.com/real"]
    assert not any("y.js" in u or "ad_domain" in u for u in urls)


def test_the_ad_more_info_link_is_dropped() -> None:
    """It is not a result, and it used to be surfaced as one."""

    outcome = parse_duckduckgo(
        _page_with_ad(_wrapped("https://example.com/real")), max_results=5
    )

    assert not any("duckduckgo-help-pages" in r.url for r in outcome.results)
    assert not any(r.title == "more info" for r in outcome.results)


def test_an_ad_block_does_not_shift_snippet_pairing() -> None:
    """The regression that mattered: mismatched data, not missing data.

    The ad block supplies two anchors and one snippet, so index pairing put
    result 1's snippet under result 2's title.
    """

    outcome = parse_duckduckgo(
        _page_with_ad(
            _wrapped("https://example.com/one"),
            _wrapped("https://example.com/two"),
        ),
        max_results=5,
    )

    assert [(r.title, r.snippet) for r in outcome.results] == [
        ("Real 1", "Snippet 1."),
        ("Real 2", "Snippet 2."),
    ]


def test_absurdly_long_urls_are_dropped() -> None:
    """Truncating a URL makes it unusable, so an over-long one is skipped.

    A live ad URL ran to ~2000 characters, which alone is a meaningful slice
    of a small model's context.
    """

    long_url = "https://example.com/?q=" + "y" * 900

    outcome = parse_duckduckgo(_results_page(long_url), max_results=5)

    assert outcome.results == ()


def test_a_result_without_a_snippet_does_not_steal_the_next_one() -> None:
    """Structural pairing must not borrow forward."""

    html = (
        "<html><body><table>"
        f'<tr><td><a class="result-link" href="{_wrapped("https://example.com/a")}">'
        "A</a></td></tr>"
        f'<tr><td><a class="result-link" href="{_wrapped("https://example.com/b")}">'
        "B</a></td></tr>"
        '<tr><td class="result-snippet">Belongs to B.</td></tr>'
        "</table></body></html>" + _PAD
    )

    outcome = parse_duckduckgo(html, max_results=5)

    assert [(r.title, r.snippet) for r in outcome.results] == [
        ("A", ""),
        ("B", "Belongs to B."),
    ]


# ============================================================
# The three outcomes must stay distinguishable
# ============================================================


def test_anomaly_page_is_blocked_not_empty() -> None:
    """The distinction this module exists for.

    Told "no results" a small model rewrites its query forever; told
    "blocked" it can wait or ask the user.
    """

    outcome = parse_duckduckgo(_ANOMALY_PAGE, max_results=5)

    assert outcome.status == "blocked"
    assert "bots use duckduckgo" in outcome.detail


def test_explicit_no_results_is_empty() -> None:
    outcome = parse_duckduckgo(_EMPTY_PAGE, max_results=5)

    assert outcome.status == "empty"


def test_unrecognised_layout_is_blocked_not_empty() -> None:
    """A parse failure must never masquerade as a genuine zero-result query."""

    outcome = parse_duckduckgo(_UNKNOWN_LAYOUT_PAGE, max_results=5)

    assert outcome.status == "blocked"
    assert "layout" in outcome.detail


@pytest.mark.parametrize("marker", ["anomaly", "captcha", "challenge", "/t/blocked"])
def test_all_block_markers_are_detected(marker: str) -> None:
    html = f"<html><body>{marker}</body></html>{_PAD}"

    assert parse_duckduckgo(html, max_results=5).status == "blocked"


# ============================================================
# Field sanitisation
# ============================================================


def test_result_fields_are_sanitised_and_capped() -> None:
    html = (
        "<html><body><table><tr><td>"
        f'<a class="result-link" href="{_wrapped("https://example.com/p")}">'
        "Bad\u202etitle</a></td></tr>"
        f'<tr><td class="result-snippet">{"y" * 900}</td></tr>'
        "</table></body></html>" + _PAD
    )

    result = parse_duckduckgo(html, max_results=5).results[0]

    assert "\u202e" not in result.title
    assert len(result.snippet) < 400
    assert result.snippet.endswith("...")


def test_snippet_newlines_are_collapsed() -> None:
    """A newline would break the numbered list the model is reading."""

    html = (
        "<html><body><table><tr><td>"
        f'<a class="result-link" href="{_wrapped("https://example.com/p")}">T</a>'
        "</td></tr>"
        '<tr><td class="result-snippet">line one\nline two\n\nline three</td></tr>'
        "</table></body></html>" + _PAD
    )

    assert "\n" not in parse_duckduckgo(html, max_results=5).results[0].snippet


def test_missing_snippet_is_empty_not_an_error() -> None:
    html = (
        "<html><body><table><tr><td>"
        f'<a class="result-link" href="{_wrapped("https://example.com/p")}">T</a>'
        "</td></tr></table></body></html>" + _PAD
    )

    assert parse_duckduckgo(html, max_results=5).results[0].snippet == ""


# ============================================================
# The network path (injected opener, no monkeypatching)
# ============================================================


def _backend(*responses: FakeResponse | Exception) -> DuckDuckGoBackend:
    return DuckDuckGoBackend(
        opener=FakeOpener(*responses),  # type: ignore[arg-type]
        clock=lambda: 0.0,
        sleep=lambda _: None,
    )


def test_search_end_to_end(resolves_public: None) -> None:
    body = _results_page(_wrapped("https://example.com/p")).encode()

    outcome = _backend(FakeResponse(body)).search("premake tutorial", max_results=5)

    assert outcome.status == "results"
    assert outcome.results[0].url == "https://example.com/p"


def test_search_sends_a_browser_user_agent(resolves_public: None) -> None:
    """DuckDuckGo refuses the urllib default and an honest UA alike."""

    opener = FakeOpener(FakeResponse(_results_page().encode()))

    DuckDuckGoBackend(
        opener=opener,  # type: ignore[arg-type]
        clock=lambda: 0.0,
        sleep=lambda _: None,
    ).search("x", max_results=5)

    request = opener.requests[0]

    assert "Mozilla/5.0" in request.get_header("User-agent", "")
    assert "text/html" in request.get_header("Accept", "")


def test_search_encodes_the_query(resolves_public: None) -> None:
    opener = FakeOpener(FakeResponse(_results_page().encode()))

    DuckDuckGoBackend(
        opener=opener,  # type: ignore[arg-type]
        clock=lambda: 0.0,
        sleep=lambda _: None,
    ).search("c++ & cmake", max_results=5)

    assert "q=c%2B%2B+%26+cmake" in opener.requests[0].full_url


def test_202_is_blocked(resolves_public: None) -> None:
    """The subtle one.

    urllib treats every 2xx as success, so DuckDuckGo's 202 anomaly response
    arrives as a normal response object. Without an explicit status check it
    would parse to zero results and be reported as "no results".
    """

    outcome = _backend(FakeResponse(_results_page().encode(), status=202)).search(
        "x", max_results=5
    )

    assert outcome.status == "blocked"
    assert "202" in outcome.detail


@pytest.mark.parametrize("code", [403, 429, 500])
def test_http_errors_are_blocked(code: int, resolves_public: None) -> None:
    error = urllib.error.HTTPError(
        "https://lite.duckduckgo.com/lite/", code, "boom", {}, None
    )

    outcome = _backend(error).search("x", max_results=5)

    assert outcome.status == "blocked"
    assert str(code) in outcome.detail


def test_network_failure_is_blocked(resolves_public: None) -> None:
    outcome = _backend(OSError("connection reset")).search("x", max_results=5)

    assert outcome.status == "blocked"
    assert "connection reset" in outcome.detail


def test_tiny_body_is_blocked(resolves_public: None) -> None:
    """A challenge or error page is often far smaller than a results page."""

    outcome = _backend(FakeResponse(b"<html>nope</html>")).search("x", max_results=5)

    assert outcome.status == "blocked"
    assert "too small" in outcome.detail


def test_oversize_body_is_blocked(
    resolves_public: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from windows_agent_mcp import search_backends

    monkeypatch.setattr(search_backends, "MAX_HTTP_BYTES", 64)

    outcome = _backend(FakeResponse(b"z" * 4096)).search("x", max_results=5)

    assert outcome.status == "blocked"
    assert "size limit" in outcome.detail


def test_search_refuses_a_url_off_its_own_allowlist(
    monkeypatch: pytest.MonkeyPatch, resolves_public: None
) -> None:
    """The endpoint is reached via extra_allowed_hosts, not allow_any_host.

    So if the endpoint constant were ever changed to another host, the URL
    check would refuse it rather than silently widening the policy.
    """

    from windows_agent_mcp import search_backends

    monkeypatch.setattr(search_backends, "_DDG_ENDPOINT", "https://evil.example/search")

    with pytest.raises(ValueError, match="not allowed"):
        _backend(FakeResponse(b"x")).search("x", max_results=5)


# ============================================================
# Rate limiting
# ============================================================


def test_rate_limiter_waits_between_requests(resolves_public: None) -> None:
    slept: list[float] = []
    now = [100.0]

    backend = DuckDuckGoBackend(
        opener=FakeOpener(  # type: ignore[arg-type]
            FakeResponse(_results_page().encode()),
            FakeResponse(_results_page().encode()),
        ),
        clock=lambda: now[0],
        sleep=slept.append,
    )

    backend.search("first", max_results=5)

    # Second call immediately afterwards, on the same clock reading.
    backend.search("second", max_results=5)

    assert slept and slept[0] == pytest.approx(MIN_REQUEST_INTERVAL_SECONDS)


def test_first_request_does_not_wait(resolves_public: None) -> None:
    slept: list[float] = []

    DuckDuckGoBackend(
        opener=FakeOpener(FakeResponse(_results_page().encode())),  # type: ignore[arg-type]
        clock=lambda: 100.0,
        sleep=slept.append,
    ).search("x", max_results=5)

    assert slept == []


# ============================================================
# Backend selection
# ============================================================


def test_default_backend_is_duckduckgo() -> None:
    assert get_backend().name == "duckduckgo"


def test_env_var_selects_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAMCP_SEARCH_BACKEND", "DuckDuckGo")

    assert get_backend().name == "duckduckgo"


@pytest.mark.parametrize("name", ["brave", "google", "ddg", "typo"])
def test_unknown_backend_raises_rather_than_falling_back(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent fallback would make an env-var typo invisible forever.

    The operator would believe they had switched provider while still using
    DuckDuckGo.
    """

    monkeypatch.setenv("WAMCP_SEARCH_BACKEND", name)

    with pytest.raises(ValueError, match="Unknown search backend"):
        get_backend()


def test_unknown_backend_error_lists_valid_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAMCP_SEARCH_BACKEND", "nope")

    with pytest.raises(ValueError, match="duckduckgo"):
        get_backend()


def test_outcome_defaults_are_immutable() -> None:
    """A NamedTuple default is shared, so it must not be a list."""

    assert SearchOutcome(status="empty").results == ()
