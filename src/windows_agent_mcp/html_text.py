"""Turn a fetched HTML page into text a small model can read.

Pure functions: bytes/str in, text out. No network, so every rule here is
unit-testable on its own.

Two themes run through this module:

* **Context economy.** A 7B-9B model has little room. Boilerplate that a
  human skims costs the same tokens as the answer, so navigation chrome is
  dropped where it can be identified safely.
* **This is untrusted input.** A page can contain text aimed at the model
  rather than the reader -- in comments, in hidden elements, in a bidi
  override that visually reverses a URL. Those are removed here, before the
  text ever reaches the caller.
"""

from __future__ import annotations

import codecs
import re
import unicodedata
from collections.abc import Iterable
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, Tag

__all__: list[str] = [
    "PageText",
    "decode_html",
    "extract_page",
    "neutralise_delimiters",
    "strip_unsafe_characters",
]

# Elements that are never prose. Removed unconditionally.
_ALWAYS_DROP = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "object",
    "embed",
)

# Chrome the page itself labels as non-content. <nav> and <footer> are
# defined by HTML as navigation and footer material, so dropping them is safe
# even with no main container. Worth doing: a real page led with twelve lines
# of navigation before its first sentence, which is pure cost in a small
# context window.
_CHROME_DROP = ("nav", "footer", "form")

# Ambiguous chrome, removed ONLY when a main-content container exists.
# Documentation sites routinely put real prose in <aside>, and <header> can
# hold an article's own heading, so neither can be dropped on sight.
_AMBIGUOUS_DROP = ("header", "aside")

# Containers that indicate the page marks its own main content.
_MAIN_SELECTORS = "main, [role=main], article"

# Charsets servers name when they do not actually know. latin-1 in particular
# never fails to decode, so honouring a wrong one silently mojibakes every
# non-ASCII character with no error anywhere.
_UNRELIABLE_CHARSETS = frozenset({"iso-8859-1", "latin-1", "latin1", "us-ascii"})

_META_CHARSET_PATTERN = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-.:]+)""",
    re.IGNORECASE,
)

# Zero-width and direction-control characters. Invisible to a reader, fully
# visible to the model -- U+202E can make a displayed URL read backwards.
_INVISIBLE_CHARACTERS = "​‌‍‎‏  ‪‫‬‭‮⁦⁧⁨⁩﻿"


class PageText(NamedTuple):
    """Extracted page content.

    Attributes:
        title: The document title, or "" when absent.
        text: Extracted prose, blank-line separated, safe characters only.
        links: Absolute, deduplicated outbound links.
    """

    title: str
    text: str
    links: list[str]


def strip_unsafe_characters(value: str) -> str:
    """Remove control and direction-manipulating characters.

    Applied to page text, titles, snippets and URLs alike -- a bidi override
    in a URL is exactly as misleading as one in prose.

    Args:
        value: Text to clean.

    Returns:
        The text with C0/C1 controls (except tab and newline) and zero-width
        or bidi characters removed.
    """

    cleaned: list[str] = []

    for char in value:
        if char in "\t\n":
            cleaned.append(char)
            continue

        if char in _INVISIBLE_CHARACTERS:
            continue

        # Cc = control, Cf = format. Both are invisible to a reader.
        if unicodedata.category(char) in {"Cc", "Cf"}:
            continue

        cleaned.append(char)

    return "".join(cleaned)


def _charset_from_meta(raw: bytes) -> str | None:
    """Find a <meta charset> declaration in the head of a document.

    Deliberately a regex over bytes: parsing the HTML would require already
    knowing the encoding this function exists to discover.
    """

    match = _META_CHARSET_PATTERN.search(raw[:2048])

    if match is None:
        return None

    return match.group(1).decode("ascii", errors="replace")


def _usable_codec(name: str | None) -> str | None:
    """Return `name` if Python can actually decode with it, else None."""

    if not name:
        return None

    try:
        codecs.lookup(name)
    except (LookupError, TypeError):
        return None

    return name


def decode_html(raw: bytes, *, header_charset: str | None = None) -> str:
    """Decode page bytes to text, degrading rather than failing.

    Precedence, in order:

    1. A UTF-8/UTF-16 byte-order mark. Authoritative, and stripped -- left in
       place it becomes a stray \\ufeff at the start of the extracted text.
    2. The HTTP header charset, UNLESS it names one of the historically
       unreliable defaults (see _UNRELIABLE_CHARSETS).
    3. A <meta charset> declaration.
    4. UTF-8 strict, then cp1252, then UTF-8 with replacement.

    Args:
        raw: Raw response body.
        header_charset: charset from the Content-Type header, if any.

    Returns:
        Decoded text. Never raises: a wrong or unknown charset degrades to
        replacement characters instead of failing the fetch.
    """

    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if raw.startswith(bom):
            return raw[len(bom) :].decode(encoding, errors="replace")

    candidates: list[str] = []

    header = _usable_codec(header_charset)

    header_is_reliable = bool(
        header and header.strip().lower() not in _UNRELIABLE_CHARSETS
    )

    if header and header_is_reliable:
        candidates.append(header)

    meta = _usable_codec(_charset_from_meta(raw))

    if meta:
        candidates.append(meta)

    # UTF-8 is tried BEFORE an unreliable header, and the order matters more
    # than it looks: latin-1 decodes any byte sequence without error, so
    # putting it first would mean utf-8 is never attempted and every UTF-8
    # page served with an "iso-8859-1" header comes back mojibaked. An
    # unreliable header is only a last resort, after utf-8 has genuinely
    # failed.
    candidates.append("utf-8")

    if header and not header_is_reliable:
        candidates.append(header)

    candidates.append("cp1252")

    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue

    return raw.decode("utf-8", errors="replace")


def _collapse_blank_lines(text: str) -> str:
    """Strip each line and collapse runs of blank lines to one.

    Paragraph structure is preserved on purpose. Collapsing everything to a
    single line would make fetch_web_page's start_line/max_lines paging
    meaningless.
    """

    lines = [line.strip() for line in text.splitlines()]

    out: list[str] = []

    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1]:
            out.append("")

    while out and not out[-1]:
        out.pop()

    return "\n".join(out)


def _decompose_all(elements: Iterable[Tag]) -> None:
    """Remove every element, tolerating ones already gone.

    decompose() destroys a tag AND its descendants, so a descendant still
    pending in the same selection list is already dead by the time the loop
    reaches it -- and touching its attributes then raises AttributeError,
    because decompose() sets attrs to None. Real pages hit this constantly
    (a styled element inside a hidden one), so the guard is required, not
    defensive.

    Args:
        elements: Elements to remove, possibly overlapping.
    """

    for element in elements:
        if not element.decomposed:
            element.decompose()


def _is_display_none(element: Tag) -> bool:
    """Return True when an inline style hides the element."""

    style = element.get("style")

    if not isinstance(style, str):
        return False

    return "display:none" in style.replace(" ", "").lower()


def extract_page(html: str, *, base_url: str, max_links: int) -> PageText:
    """Extract title, prose and links from an HTML document.

    Args:
        html: Decoded HTML. Always a str -- bs4's own encoding detection is
            weak without charset-normalizer, so decode_html() handles that.
        base_url: URL the document came from, for resolving relative links.
        max_links: Maximum links to return.

    Returns:
        The extracted PageText.
    """

    soup = BeautifulSoup(html, "html.parser")

    # Comments are a favourite place to hide instructions: invisible to a
    # reader, and whether get_text() includes them has changed across bs4
    # 4.x, so remove them explicitly rather than relying on the default.
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    _decompose_all(soup.select(",".join(_ALWAYS_DROP)))

    # Same reasoning as comments: hidden from the reader, not from get_text().
    _decompose_all(soup.select("[hidden], [aria-hidden=true]"))

    _decompose_all(
        element
        for element in soup.select("[style]")
        if not element.decomposed and _is_display_none(element)
    )

    title = ""

    if soup.title is not None and soup.title.string is not None:
        title = strip_unsafe_characters(str(soup.title.string)).strip()

    # Links are collected BEFORE chrome is dropped: navigation links are
    # often the useful ones on an index page, even though the nav text is not.
    links = _collect_links(soup, base_url=base_url, max_links=max_links)

    main = soup.select_one(_MAIN_SELECTORS)

    if main is not None:
        # The page identifies its own content, so everything outside it is
        # noise -- including the otherwise-ambiguous <aside> and <header>.
        root = main
        _decompose_all(root.select(",".join(_CHROME_DROP + _AMBIGUOUS_DROP)))
    else:
        root = soup.body if soup.body is not None else soup
        _decompose_all(root.select(",".join(_CHROME_DROP)))

    text = _collapse_blank_lines(strip_unsafe_characters(root.get_text("\n")))

    # Never hand back an empty page when there was content to show. A layout
    # that puts everything inside <nav>, or an <article> holding only a
    # header, would otherwise have the model report the page as blank. Falling
    # back to the never-prose set alone keeps context economy the default
    # while making a blank result impossible.
    if not text:
        fallback = BeautifulSoup(html, "html.parser")

        for comment in fallback.find_all(string=lambda node: isinstance(node, Comment)):
            comment.extract()

        _decompose_all(fallback.select(",".join(_ALWAYS_DROP)))

        body = fallback.body if fallback.body is not None else fallback

        text = _collapse_blank_lines(strip_unsafe_characters(body.get_text("\n")))

    return PageText(title=title, text=text, links=links)


def _collect_links(soup: BeautifulSoup, *, base_url: str, max_links: int) -> list[str]:
    """Collect absolute https links, deduplicated and capped.

    Links are gathered so the model can navigate onward, but they are
    reported as a separate section rather than inline -- inline they are both
    noise and a place to hide a misleading label.
    """

    seen: set[str] = set()
    links: list[str] = []

    for anchor in soup.find_all("a"):
        href = anchor.get("href")

        if not isinstance(href, str) or not href.strip():
            continue

        absolute = urljoin(base_url, strip_unsafe_characters(href).strip())

        # Only https survives: a result must never hand the model a
        # javascript:, data: or file: URL.
        if urlparse(absolute).scheme != "https":
            continue

        if absolute in seen:
            continue

        seen.add(absolute)
        links.append(absolute)

        if len(links) >= max_links:
            break

    return links


def neutralise_delimiters(text: str, *marks: str) -> str:
    """Break any occurrence of a delimiter inside untrusted content.

    Without this a page can simply emit the closing marker and continue in
    what looks like the server's own voice. A zero-width space would be
    invisible but strip_unsafe_characters removes those, so a visible
    interruption is used instead.

    Args:
        text: Untrusted text about to be wrapped.
        marks: Delimiter strings to neutralise.

    Returns:
        The text with each delimiter broken up.
    """

    for mark in marks:
        if mark and mark in text:
            text = text.replace(mark, mark[:3] + "[escaped]" + mark[3:])

    return text
