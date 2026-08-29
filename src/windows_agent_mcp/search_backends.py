"""Pluggable web-search backends.

Only DuckDuckGo ships today. The Protocol exists so Brave or a self-hosted
SearXNG can be added without touching the tool, selected by
BIONIC_SEARCH_BACKEND.

The design problem here is not fetching results -- it is telling three
outcomes apart:

    results       -> a list
    no results    -> the query genuinely matched nothing
    blocked       -> we were rate-limited, or the layout changed

Conflating the last two is the failure that matters. A small model told "no
results" will rewrite its query and try again forever; told "blocked" it can
wait or ask the user. So a parse that finds nothing is never reported as
"no results" unless the page explicitly says so.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Literal, NamedTuple, Protocol

from bs4 import BeautifulSoup

from .html_text import decode_html, strip_unsafe_characters
from .log import log
from .utils import (
    BROWSER_USER_AGENT,
    HTTP_TIMEOUT_SECONDS,
    MAX_HTTP_BYTES,
    MAX_SEARCH_FIELD_CHARS,
    SEARCH_BACKEND_ENV_VAR,
    SEARCH_HTTP_OPENER,
    validate_url,
    validate_url_shape,
)

__all__: list[str] = [
    "BACKEND_NAMES",
    "DuckDuckGoBackend",
    "SearchBackend",
    "SearchOutcome",
    "SearchResult",
    "get_backend",
    "parse_duckduckgo",
]

# Minimum gap between search requests. A model told to research will loop, and
# DuckDuckGo answers a burst with anomaly pages -- which then presents as
# exactly the blocked/no-results ambiguity this module exists to avoid.
MIN_REQUEST_INTERVAL_SECONDS: float = 2.0


class SearchResult(NamedTuple):
    """One search hit. Every field is attacker-controlled text."""

    title: str
    url: str
    snippet: str


class SearchOutcome(NamedTuple):
    """The result of a search attempt.

    Attributes:
        status: "results", "empty" (query matched nothing) or "blocked"
            (rate-limited, challenged, or the layout no longer parses).
        results: Hits, when status is "results".
        detail: Human-readable explanation, when status is "blocked".
    """

    status: Literal["results", "empty", "blocked"]
    # Immutable default: a NamedTuple default is shared by every instance.
    results: tuple[SearchResult, ...] = ()
    detail: str = ""


class SearchBackend(Protocol):
    """A search provider."""

    name: str
    allowed_hosts: frozenset[str]

    def search(self, query: str, max_results: int) -> SearchOutcome:
        """Run a query and return its outcome."""
        ...


# ============================================================
# DuckDuckGo
# ============================================================

# The "lite" endpoint in preference to "html": a plain table rather than
# nested divs, several times smaller (which matters for both the byte cap and
# a small context window), and historically the more stable of the two.
_DDG_ENDPOINT = "https://lite.duckduckgo.com/lite/"

_DDG_HOSTS = frozenset({"lite.duckduckgo.com", "html.duckduckgo.com", "duckduckgo.com"})

# Markers that mean "we were refused", not "nothing matched".
_BLOCKED_MARKERS = (
    "anomaly",
    "bots use duckduckgo",
    "unfortunately, bots",
    "captcha",
    "challenge",
    "/t/blocked",
)

_EMPTY_MARKERS = (
    "no results found",
    "no results for",
    "not match any documents",
)

# A real result page is never this small; a challenge or error page often is.
_MIN_PLAUSIBLE_BODY_BYTES = 1500


def _unwrap_ddg_url(href: str) -> str | None:
    """Recover the real target from a DuckDuckGo redirect wrapper.

    Hrefs arrive in three shapes, and all three occur on live pages:

        //duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fp&rut=...
        /l/?uddg=https%3A%2F%2Fexample.com%2Fp
        https://example.com/p          (already direct)

    Two traps: the wrapper is protocol-relative, so urlparse reports an empty
    scheme; and parse_qs ALREADY percent-decodes once, so calling unquote on
    its output corrupts any target containing a literal %25 into a
    wrong-but-plausible URL.

    Args:
        href: Raw href from the results page.

    Returns:
        An absolute URL, or None if nothing usable could be recovered.
    """

    href = strip_unsafe_characters(href).strip()

    if not href:
        return None

    # Give the protocol-relative and root-relative forms a scheme and host so
    # urlparse can see the query string.
    if href.startswith("//"):
        candidate = "https:" + href
    elif href.startswith("/"):
        candidate = "https://duckduckgo.com" + href
    else:
        candidate = href

    parsed = urllib.parse.urlparse(candidate)

    if "uddg" in parsed.query:
        values = urllib.parse.parse_qs(parsed.query).get("uddg")

        if not values or not values[0]:
            return None

        # Already decoded exactly once by parse_qs. Do not unquote again.
        return values[0]

    if parsed.scheme in {"http", "https"}:
        return candidate

    return None


# Query parameters and paths that mark a sponsored result. Observed live: an
# ad arrives as https://duckduckgo.com/y.js?ad_domain=...&ad_provider=... with
# a click-tracking URL running to ~2000 characters, which on its own would
# consume a meaningful slice of a 7B model's context.
_AD_MARKERS = ("/y.js", "ad_domain=", "ad_provider=", "ad_type=")

# Longer than any URL worth showing. Truncating a URL would make it unusable,
# so an over-long one is dropped instead -- in practice they are trackers.
_MAX_RESULT_URL_CHARS = 500


def _is_advertisement(url: str) -> bool:
    """Detect a sponsored result or tracking redirect.

    Ads are not useful answers and their URLs are enormous, so they are
    dropped rather than shown. This also removes the "more info" link that
    accompanies an ad block, which was previously surfaced as a result in its
    own right.

    Args:
        url: Unwrapped result URL.

    Returns:
        True if the URL looks like an ad or tracker.
    """

    if len(url) > _MAX_RESULT_URL_CHARS:
        return True

    lowered = url.lower()

    if any(marker in lowered for marker in _AD_MARKERS):
        return True

    # DuckDuckGo's own help pages are never the answer to a technical query,
    # and the "more info" ad link points at them.
    return "duckduckgo.com/duckduckgo-help-pages" in lowered


def _clean_field(value: str) -> str:
    """Sanitise and cap one attacker-controlled result field.

    Newlines are collapsed because the tool renders results as a numbered
    list; a snippet containing a newline would break the shape the model is
    reading.
    """

    text = strip_unsafe_characters(value)
    text = " ".join(text.split())

    if len(text) > MAX_SEARCH_FIELD_CHARS:
        text = text[:MAX_SEARCH_FIELD_CHARS].rstrip() + "..."

    return text


def parse_duckduckgo(html: str, max_results: int) -> SearchOutcome:
    """Parse a DuckDuckGo results page.

    Pure function so it can be tested against saved captures.

    Args:
        html: Decoded page HTML.
        max_results: Maximum hits to return.

    Returns:
        The outcome. "blocked" whenever the page cannot be trusted to be a
        genuine result page -- never "empty" on a mere parse failure.
    """

    lowered = html.lower()

    for marker in _BLOCKED_MARKERS:
        if marker in lowered:
            return SearchOutcome(
                status="blocked",
                detail=f"the response looks like a bot challenge ('{marker}')",
            )

    soup = BeautifulSoup(html, "html.parser")

    results: list[SearchResult] = []

    # The lite layout is a table of rows; the html layout uses div.result.
    # Selectors are data so a layout change is a one-line edit.
    for title_selector, snippet_selector in (
        ("a.result-link", "td.result-snippet"),
        ("a.result__a", "a.result__snippet"),
        ("h2.result__title a", "div.result__snippet"),
    ):
        title_nodes = soup.select(title_selector)

        if not title_nodes:
            continue

        # Identity membership, not tag name: in the html/ layout the snippet
        # selector is "a.result__snippet" -- also an <a> -- so testing
        # node.name would classify every snippet as a title.
        title_ids = {id(node) for node in title_nodes}

        # Titles and snippets are paired by walking the document ONCE, in
        # order, rather than by zipping two flat lists by index.
        #
        # Index pairing looks equivalent and is not: a sponsored block
        # contributes two anchors (the ad plus its "more info" link) but only
        # one snippet, which shifts every later pair by one. The observed
        # result was a Stack Overflow snippet printed under a DuckDuckGo help
        # page title -- mismatched data, which is worse than missing data.
        #
        # A comma selector returns nodes in document order, so a snippet is
        # attached to the anchor it actually followed.
        for node in soup.select(f"{title_selector}, {snippet_selector}"):
            if len(results) >= max_results:
                break

            if id(node) in title_ids:
                href = node.get("href")

                if not isinstance(href, str):
                    continue

                target = _unwrap_ddg_url(href)

                if target is None or _is_advertisement(target):
                    continue

                # Shape only, deliberately: a result must never be shown to
                # the model as a javascript:, file: or credential-bearing URL.
                # But resolving each hit would mean a DNS lookup per result
                # and would stop this being a pure function -- and nothing is
                # fetched here. fetch_web_page runs the full check, DNS
                # included, before it retrieves anything.
                try:
                    validate_url_shape(target)
                except ValueError:
                    continue

                results.append(
                    SearchResult(
                        title=_clean_field(node.get_text(" ")) or target,
                        url=target,
                        snippet="",
                    )
                )
                continue

            # A snippet belongs to the most recent anchor, and only if that
            # anchor does not already have one.
            if results and not results[-1].snippet:
                results[-1] = results[-1]._replace(
                    snippet=_clean_field(node.get_text(" "))
                )

        break

    if results:
        return SearchOutcome(status="results", results=tuple(results))

    # Nothing parsed. Only claim "no results" if the page says so itself.
    for marker in _EMPTY_MARKERS:
        if marker in lowered:
            return SearchOutcome(status="empty")

    return SearchOutcome(
        status="blocked",
        detail=(
            "the response contained no recognisable results and no "
            "no-results message, so the page layout has probably changed"
        ),
    )


class DuckDuckGoBackend:
    """Search via DuckDuckGo's lite HTML endpoint.

    The opener is injected rather than imported so tests can pass a fake
    directly, instead of monkeypatching a module attribute.
    """

    name = "duckduckgo"
    allowed_hosts = _DDG_HOSTS

    def __init__(
        self,
        opener: urllib.request.OpenerDirector | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._opener = opener if opener is not None else SEARCH_HTTP_OPENER
        # Clock and sleep are injectable so tests exercise the rate limiter
        # without actually waiting.
        self._clock: Callable[[], float] = clock or time.monotonic
        self._sleep: Callable[[float], None] = sleep or time.sleep
        self._last_request: float = 0.0

    def _respect_rate_limit(self) -> None:
        now = self._clock()

        elapsed = now - self._last_request

        if self._last_request and elapsed < MIN_REQUEST_INTERVAL_SECONDS:
            self._sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)

        self._last_request = self._clock()

    def search(self, query: str, max_results: int) -> SearchOutcome:
        """Run a query against DuckDuckGo.

        Args:
            query: Search terms.
            max_results: Maximum hits to return.

        Returns:
            The outcome. Network and protocol failures come back as "blocked"
            with a detail string rather than raising.
        """

        url = _DDG_ENDPOINT + "?" + urllib.parse.urlencode({"q": query})

        validate_url(url, extra_allowed_hosts=self.allowed_hosts)

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": BROWSER_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            method="GET",
        )

        self._respect_rate_limit()

        try:
            with self._opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", 200)

                # 202 is DuckDuckGo's anomaly response. It matters because
                # urllib treats every 2xx as success, so a blocked page
                # arrives as a perfectly normal response object and would
                # otherwise parse to zero results and be reported as "no
                # results found".
                if status == 202:
                    return SearchOutcome(
                        status="blocked",
                        detail="DuckDuckGo returned 202 (rate limited)",
                    )

                raw = response.read(MAX_HTTP_BYTES + 1)

        except urllib.error.HTTPError as exc:
            return SearchOutcome(
                status="blocked",
                detail=f"HTTP {exc.code} from the search endpoint",
            )
        except OSError as exc:
            return SearchOutcome(
                status="blocked",
                detail=f"could not reach the search endpoint: {exc}",
            )

        if len(raw) > MAX_HTTP_BYTES:
            return SearchOutcome(
                status="blocked",
                detail="the search response exceeded the size limit",
            )

        if len(raw) < _MIN_PLAUSIBLE_BODY_BYTES:
            return SearchOutcome(
                status="blocked",
                detail=(
                    f"the response was only {len(raw)} bytes, too small to be "
                    f"a results page"
                ),
            )

        return parse_duckduckgo(decode_html(raw), max_results)


# ============================================================
# Selection
# ============================================================

BACKEND_NAMES: tuple[str, ...] = ("duckduckgo",)


def get_backend(name: str | None = None) -> SearchBackend:
    """Return the configured search backend.

    Args:
        name: Backend name. Defaults to BIONIC_SEARCH_BACKEND, then
            "duckduckgo".

    Returns:
        The backend.

    Raises:
        ValueError: If the name is not recognised. Deliberately not a silent
            fallback -- a typo in the environment variable would otherwise be
            invisible forever, with the operator believing they had switched
            provider.
    """

    requested = (name or os.environ.get(SEARCH_BACKEND_ENV_VAR, "")).strip().lower()

    if not requested or requested == "duckduckgo":
        return DuckDuckGoBackend()

    raise ValueError(
        f"Unknown search backend '{requested}'. "
        f"Set {SEARCH_BACKEND_ENV_VAR} to one of: {', '.join(BACKEND_NAMES)}."
    )


def log_search(query: str, outcome: SearchOutcome) -> None:
    """Record a search on stderr for the operator's audit trail."""

    log.info(
        "web_search q=%r status=%s hits=%d",
        query[:200],
        outcome.status,
        len(outcome.results),
    )
