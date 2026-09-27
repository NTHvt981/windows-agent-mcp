"""Pluggable web-search backends."""

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

# Spacing requests avoids anomaly pages.
MIN_REQUEST_INTERVAL_SECONDS: float = 2.0


class SearchResult(NamedTuple):
    """One search hit. Every field is attacker-controlled text."""

    title: str
    url: str
    snippet: str


class SearchOutcome(NamedTuple):
    """The result of a search attempt."""

    status: Literal["results", "empty", "blocked"]
    results: tuple[SearchResult, ...] = ()
    detail: str = ""


class SearchBackend(Protocol):
    """A search provider."""

    name: str
    allowed_hosts: frozenset[str]

    def search(self, query: str, max_results: int) -> SearchOutcome:
        """Run a query and return its outcome."""
        ...


# lite: a plain table, smaller and historically steadier.
_DDG_ENDPOINT = "https://lite.duckduckgo.com/lite/"

_DDG_HOSTS = frozenset({"lite.duckduckgo.com", "html.duckduckgo.com", "duckduckgo.com"})

# Markers meaning refused, not empty.
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

# Real result pages are never this small.
_MIN_PLAUSIBLE_BODY_BYTES = 1500


def _unwrap_ddg_url(href: str) -> str | None:
    """Recover the real target from a DuckDuckGo redirect wrapper."""

    href = strip_unsafe_characters(href).strip()

    if not href:
        return None

    # Relative forms need a scheme and host for urlparse.
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

        # parse_qs already decodes once; never unquote again.
        return values[0]

    if parsed.scheme in {"http", "https"}:
        return candidate

    return None


_AD_MARKERS = ("/y.js", "ad_domain=", "ad_provider=", "ad_type=")

_MAX_RESULT_URL_CHARS = 500


def _is_advertisement(url: str) -> bool:
    """Detect a sponsored result or tracking redirect."""

    if len(url) > _MAX_RESULT_URL_CHARS:
        return True

    lowered = url.lower()

    if any(marker in lowered for marker in _AD_MARKERS):
        return True

    return "duckduckgo.com/duckduckgo-help-pages" in lowered


def _clean_field(value: str) -> str:
    """Sanitise and cap one attacker-controlled result field."""

    # Newlines would break the numbered list.

    text = strip_unsafe_characters(value)
    text = " ".join(text.split())

    if len(text) > MAX_SEARCH_FIELD_CHARS:
        text = text[:MAX_SEARCH_FIELD_CHARS].rstrip() + "..."

    return text


def parse_duckduckgo(html: str, max_results: int) -> SearchOutcome:
    """Parse a DuckDuckGo results page."""

    lowered = html.lower()

    for marker in _BLOCKED_MARKERS:
        if marker in lowered:
            return SearchOutcome(
                status="blocked",
                detail=f"the response looks like a bot challenge ('{marker}')",
            )

    soup = BeautifulSoup(html, "html.parser")

    results: list[SearchResult] = []

    for title_selector, snippet_selector in (
        ("a.result-link", "td.result-snippet"),
        ("a.result__a", "a.result__snippet"),
        ("h2.result__title a", "div.result__snippet"),
    ):
        title_nodes = soup.select(title_selector)

        if not title_nodes:
            continue

        # Identity, not tag name: snippets are also <a>.
        title_ids = {id(node) for node in title_nodes}

        # Walk once: index pairing mismatches titles and snippets.
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

                # Shape only: fetch_web_page validates before retrieving.
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

            if results and not results[-1].snippet:
                results[-1] = results[-1]._replace(
                    snippet=_clean_field(node.get_text(" "))
                )

        break

    if results:
        return SearchOutcome(status="results", results=tuple(results))

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
    name = "duckduckgo"
    allowed_hosts = _DDG_HOSTS

    def __init__(
        self,
        opener: urllib.request.OpenerDirector | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._opener = opener if opener is not None else SEARCH_HTTP_OPENER
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
        """Run a query against DuckDuckGo."""

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

                # urllib treats 2xx as success, so 202 arrives normally.
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


BACKEND_NAMES: tuple[str, ...] = ("duckduckgo",)


def get_backend(name: str | None = None) -> SearchBackend:
    """Return the configured search backend."""

    requested = (name or os.environ.get(SEARCH_BACKEND_ENV_VAR, "")).strip().lower()

    if not requested or requested == "duckduckgo":
        return DuckDuckGoBackend()

    raise ValueError(
        f"Unknown search backend '{requested}'. "
        f"Set {SEARCH_BACKEND_ENV_VAR} to one of: {', '.join(BACKEND_NAMES)}."
    )


def log_search(query: str, outcome: SearchOutcome) -> None:

    log.info(
        "web_search q=%r status=%s hits=%d",
        query[:200],
        outcome.status,
        len(outcome.results),
    )
