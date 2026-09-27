"""Fetch and read a web page as text."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from anyio.to_thread import run_sync
from mcp.server.mcpserver import Context

from ..consent import request_host_grant
from ..error import mcp_error
from ..hostgrants import (
    DEFAULT_GRANTS_FILENAME,
    grant_for_session,
    persist_grant,
)
from ..html_text import decode_html, extract_page
from ..log import log
from ..untrusted import wrap_untrusted
from ..utils import (
    BROWSER_USER_AGENT,
    DEFAULT_READ_LINES,
    DOC_HTTP_OPENER,
    HTTP_TIMEOUT_SECONDS,
    MAX_PAGE_BYTES,
    MAX_PAGE_LINKS,
    RESEARCH_HTTP_OPENER,
    RedirectNotAllowedError,
    host_grant_would_help,
    readable_web_hosts,
    validate_url,
    web_research_enabled,
)

__all__: list[str] = ["fetch_web_page", "read_web_page"]

# Allowlist: an unknown type is likelier binary than prose.
_TEXTUAL_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/markdown",
        "application/json",
        "application/xml",
        "text/xml",
    }
)

# Cache keyed by URL; refetching would lie about line numbers.
_CACHE_TTL_SECONDS = 300.0
_CACHE_MAX_ENTRIES = 8

_cache: dict[str, tuple[float, str, str]] = {}


def _cache_get(url: str, now: float) -> tuple[str, str] | None:
    entry = _cache.get(url)

    if entry is None:
        return None

    stamped, title, text = entry

    if now - stamped > _CACHE_TTL_SECONDS:
        del _cache[url]
        return None

    return title, text


def _cache_put(url: str, title: str, text: str, now: float) -> None:
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        oldest = min(_cache, key=lambda key: _cache[key][0])
        del _cache[oldest]

    _cache[url] = (now, title, text)


def clear_cache() -> None:
    _cache.clear()


def _content_type(raw_header: str | None) -> str:
    if not raw_header:
        return ""

    return raw_header.split(";", 1)[0].strip().lower()


def _charset(raw_header: str | None) -> str | None:
    # Hand-parsed: plain header mappings lack get_content_charset.

    if not raw_header or "charset=" not in raw_header.lower():
        return None

    for part in raw_header.split(";"):
        name, _, value = part.partition("=")

        if name.strip().lower() == "charset":
            return value.strip().strip("\"'") or None

    return None


def _looks_binary(raw: bytes) -> bool:
    # NUL bytes never appear in text.
    return b"\x00" in raw[:4096]


def _looks_like_markup(raw: bytes) -> bool:
    head = raw[:1024].lstrip().lower()

    return head.startswith((b"<!doctype html", b"<html", b"<?xml"))


async def fetch_web_page(
    url: str,
    start_line: int = 1,
    max_lines: int = DEFAULT_READ_LINES,
    ctx: Context | None = None,
) -> str:
    """Read a web page as plain text."""

    # Blocking work runs in a worker thread.
    first = await run_sync(read_web_page, url, start_line, max_lines)

    if ctx is None:
        return first

    host = host_grant_would_help(url)

    if host is None:
        return first

    outcome = await request_host_grant(ctx, host, url)

    if not outcome.granted:
        log.info("read of %s not granted: %s", host, outcome.detail)
        return first

    grant_for_session(host)

    if outcome.persist:
        error = persist_grant(host, note="approved during a session")

        if error is not None:
            log.warning("could not record the grant for %s: %s", host, error)

    log.info("granted %s (%s), retrying", host, outcome.detail)

    return await run_sync(read_web_page, url, start_line, max_lines)


def read_web_page(
    url: str,
    start_line: int = 1,
    max_lines: int = DEFAULT_READ_LINES,
) -> str:
    """Fetch and render a page, with no consent step."""

    research = web_research_enabled()

    readable, grants_error = readable_web_hosts()

    try:
        validate_url(
            url,
            allow_any_host=research,
            extra_allowed_hosts=readable,
        )
    except ValueError as exc:
        return _refused(url, str(exc), research=research, grants_error=grants_error)

    start_line = max(1, int(start_line))
    max_lines = max(1, int(max_lines))

    now = time.monotonic()

    cached = _cache_get(url, now)

    if cached is not None:
        title, text = cached
    else:
        fetched = _fetch(url, research=research)

        if isinstance(fetched, str):
            return fetched

        title, text = fetched

        _cache_put(url, title, text, now)

    return _render(url, title, text, start_line=start_line, max_lines=max_lines)


def _redirect_refused(url: str, exc: RedirectNotAllowedError) -> str:
    """Build the envelope for a redirect the allowlist would not follow."""

    # www and apex are different hostnames; both may need grants.

    target = exc.hostname

    recovery = [
        f"The URL you asked for redirected to {exc.url}, and THAT host is not "
        f"readable. The page you requested may be fine.",
        "DO NOT retry the identical URL: it will redirect again.",
    ]

    if target and not web_research_enabled():
        recovery.append(
            f"Ask the user to allow the redirect target as well: python -m "
            f"windows_agent_mcp.hostgrants --add {target}"
        )

        bare = target[4:] if target.startswith("www.") else f"www.{target}"

        recovery.append(
            f"This is often just the www/apex spelling of the same site. "
            f"Granting '{target}' is a separate decision from granting "
            f"'{bare}', so both may be needed."
        )

    return mcp_error(
        "URL_NOT_ALLOWED",
        "fetch_web_page",
        str(exc),
        path=url,
        recovery=recovery,
    )


def _refused(
    url: str,
    message: str,
    *,
    research: bool,
    grants_error: str | None,
) -> str:
    """Build the URL_NOT_ALLOWED envelope."""

    recovery = [
        "DO NOT retry the identical URL.",
        "Only HTTPS URLs on port 443 that resolve to a public address can be read.",
    ]

    host = host_grant_would_help(url)

    if host is not None:
        recovery.append(
            f"This host is not on the readable list. Do not try a different "
            f"path on {host} -- the whole host is refused, not that page."
        )
        recovery.append(
            "These documentation hosts ARE readable right now: "
            "learn.microsoft.com, registry.khronos.org, docs.vulkan.org, "
            "en.cppreference.com, cmake.org. If one of them answers the "
            "question, use it instead of asking for access."
        )
        recovery.append(
            f"The user can allow this ONE host, with no restart, by running: "
            f"python -m windows_agent_mcp.hostgrants --add {host} "
            f"(or by adding it to {DEFAULT_GRANTS_FILENAME}). It applies to "
            f"your next call."
        )
        recovery.append(
            "Tell the user which host you need and why, then stop. Do not "
            "suggest WAMCP_WEB_RESEARCH=1 as the first option: that opens "
            "every host at once."
        )
        recovery.append(
            "If you did not get this URL from web_search or from the user, "
            "it may not exist at all. Do not invent another one -- ask."
        )
    elif not research:
        recovery.append(
            "A host grant would not help: the URL was refused for its scheme, "
            "port, credentials or address, not for its host."
        )

    if grants_error is not None:
        recovery.append(f"NOTE: the granted-hosts file has a problem: {grants_error}")

    return mcp_error(
        "URL_NOT_ALLOWED",
        "fetch_web_page",
        message,
        path=url,
        recovery=recovery,
    )


def _fetch(url: str, *, research: bool) -> tuple[str, str] | str:
    """Fetch and extract a page."""

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9",
        },
        method="GET",
    )

    # stderr, so the log cannot corrupt the stdio stream.
    log.info(
        "fetch_web_page host=%s research=%s url=%s",
        urlparse(url).hostname,
        research,
        url,
    )

    opener = RESEARCH_HTTP_OPENER if research else DOC_HTTP_OPENER

    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            header = resp.headers.get("Content-Type")

            mime = _content_type(header)

            if mime and mime not in _TEXTUAL_TYPES:
                return _unsupported_type(url, mime)

            declared = resp.headers.get("Content-Length")

            if declared:
                try:
                    if int(declared) > MAX_PAGE_BYTES:
                        return _too_large(url, int(declared))
                except ValueError:
                    pass

            raw = resp.read(MAX_PAGE_BYTES + 1)

            final_url = getattr(resp, "url", "") or url

    except urllib.error.HTTPError as exc:
        return mcp_error(
            "PAGE_HTTP_ERROR",
            "fetch_web_page",
            f"The server returned HTTP {exc.code}.",
            path=url,
            recovery=[
                "Do not repeatedly retry the identical URL.",
                "If 404, the page does not exist -- search for a current "
                "link instead of guessing.",
            ],
        )
    except RedirectNotAllowedError as exc:
        log.info(
            "fetch_web_page redirect refused: %s -> %s",
            url,
            exc.url,
        )

        return _redirect_refused(url, exc)
    except (OSError, ValueError) as exc:
        log.exception("fetch_web_page failed")

        return mcp_error(
            "PAGE_FETCH_FAILED",
            "fetch_web_page",
            f"{type(exc).__name__}: {exc}",
            path=url,
            recovery=[
                "Do not repeatedly retry the identical URL.",
                "Verify the URL and that the machine has network access.",
            ],
        )

    if len(raw) > MAX_PAGE_BYTES:
        return _too_large(url, len(raw))

    if not mime and _looks_binary(raw):
        return _unsupported_type(url, "binary data (no Content-Type sent)")

    html = decode_html(raw, header_charset=_charset(header))

    if mime in {"text/plain", "text/markdown"} or (
        not mime and not _looks_like_markup(raw)
    ):
        return "", html.strip()

    page = extract_page(html, base_url=final_url, max_links=MAX_PAGE_LINKS)

    text = page.text

    if page.links:
        text += "\n\nLinks on this page:\n" + "\n".join(
            f"- {link}" for link in page.links
        )

    return page.title, text


def _render(url: str, title: str, text: str, *, start_line: int, max_lines: int) -> str:
    """Page the extracted text and wrap it for the model."""

    lines = text.splitlines()

    total = len(lines)

    if start_line > total and total > 0:
        return mcp_error(
            "START_LINE_OUT_OF_RANGE",
            "fetch_web_page",
            f"start_line {start_line} is past the end of the page ({total} lines).",
            path=url,
            recovery=[
                "Do not retry with the same start_line.",
                f"Use a start_line between 1 and {total}.",
            ],
        )

    selected = lines[start_line - 1 : start_line - 1 + max_lines]

    last_line = start_line - 1 + len(selected)

    body = "\n".join(selected)

    if last_line < total:
        body += (
            f"\n\n...[truncated: showed lines {start_line}-{last_line}]...\n"
            f"Continue with: fetch_web_page(url={url!r}, "
            f"start_line={last_line + 1})"
        )

    header = f"TITLE: {title}\nURL: {url}" if title else f"URL: {url}"

    return wrap_untrusted(f"{header}\n\n{body}", source=url)


def _unsupported_type(url: str, mime: str) -> str:
    return mcp_error(
        "UNSUPPORTED_CONTENT_TYPE",
        "fetch_web_page",
        f"This tool reads text only and cannot read {mime}.",
        path=url,
        # download_file shares the strict allowlist, so it would refuse too.
        recovery=[
            "DO NOT retry this URL and do not try download_file -- it cannot "
            "reach this host either.",
            f"Tell the user the URL is {mime} and ask how they want to proceed.",
        ],
    )


def _too_large(url: str, size: int) -> str:
    return mcp_error(
        "PAGE_TOO_LARGE",
        "fetch_web_page",
        f"The page is {size:,} bytes, over the {MAX_PAGE_BYTES:,} byte limit.",
        path=url,
        recovery=[
            "Do not retry the identical URL.",
            "Look for a smaller page covering the same material.",
        ],
    )
