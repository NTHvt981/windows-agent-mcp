"""Turn a fetched HTML page into text a small model can read."""

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

_CHROME_DROP = ("nav", "footer", "form")

_AMBIGUOUS_DROP = ("header", "aside")

_MAIN_SELECTORS = "main, [role=main], article"

# latin-1 never fails, so a wrong one mojibakes silently.
_UNRELIABLE_CHARSETS = frozenset({"iso-8859-1", "latin-1", "latin1", "us-ascii"})

_META_CHARSET_PATTERN = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-.:]+)""",
    re.IGNORECASE,
)

# U+202E reverses displayed URLs; raw forms corrupted this file before.
_INVISIBLE_CHARACTERS = (
    "\u200b\u200c\u200d\u200e\u200f"  # zero-width + LTR/RTL marks
    "\u2028\u2029"  # line + paragraph separators
    "\u202a\u202b\u202c\u202d\u202e"  # bidi embeddings / overrides
    "\u2066\u2067\u2068\u2069"  # bidi isolates
    "\ufeff"  # zero-width no-break space (BOM)
)


class PageText(NamedTuple):
    """Extracted page content."""

    title: str
    text: str
    links: list[str]


def strip_unsafe_characters(value: str) -> str:
    """Remove control and direction-manipulating characters."""

    cleaned: list[str] = []

    for char in value:
        if char in "\t\n":
            cleaned.append(char)
            continue

        if char in _INVISIBLE_CHARACTERS:
            continue

        if unicodedata.category(char) in {"Cc", "Cf"}:
            continue

        cleaned.append(char)

    return "".join(cleaned)


def _charset_from_meta(raw: bytes) -> str | None:
    """Find a <meta charset> declaration in the head of a document."""

    # Regex over bytes: parsing needs the encoding first.

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
    """Decode page bytes to text, degrading rather than failing."""

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

    # UTF-8 before latin-1: latin-1 never errors.
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
    """Strip each line and collapse runs of blank lines to one."""

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
    """Remove every element, tolerating ones already gone."""

    # decompose() kills descendants too; touching them raises.

    for element in elements:
        if not element.decomposed:
            element.decompose()


def _is_display_none(element: Tag) -> bool:

    style = element.get("style")

    if not isinstance(style, str):
        return False

    return "display:none" in style.replace(" ", "").lower()


def extract_page(html: str, *, base_url: str, max_links: int) -> PageText:
    """Extract title, prose and links from an HTML document."""

    soup = BeautifulSoup(html, "html.parser")

    # get_text() includes comments variably across bs4 4.x.
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    _decompose_all(soup.select(",".join(_ALWAYS_DROP)))
    _decompose_all(soup.select("[hidden], [aria-hidden=true]"))

    _decompose_all(
        element
        for element in soup.select("[style]")
        if not element.decomposed and _is_display_none(element)
    )

    title = ""

    if soup.title is not None and soup.title.string is not None:
        title = strip_unsafe_characters(str(soup.title.string)).strip()

    # Links first: nav links help on index pages.
    links = _collect_links(soup, base_url=base_url, max_links=max_links)

    main = soup.select_one(_MAIN_SELECTORS)

    if main is not None:
        root = main
        _decompose_all(root.select(",".join(_CHROME_DROP + _AMBIGUOUS_DROP)))
    else:
        root = soup.body if soup.body is not None else soup
        _decompose_all(root.select(",".join(_CHROME_DROP)))

    text = _collapse_blank_lines(strip_unsafe_characters(root.get_text("\n")))

    if not text:
        fallback = BeautifulSoup(html, "html.parser")

        for comment in fallback.find_all(string=lambda node: isinstance(node, Comment)):
            comment.extract()

        _decompose_all(fallback.select(",".join(_ALWAYS_DROP)))

        body = fallback.body if fallback.body is not None else fallback

        text = _collapse_blank_lines(strip_unsafe_characters(body.get_text("\n")))

    return PageText(title=title, text=text, links=links)


def _collect_links(soup: BeautifulSoup, *, base_url: str, max_links: int) -> list[str]:
    """Collect absolute https links, deduplicated and capped."""

    seen: set[str] = set()
    links: list[str] = []

    for anchor in soup.find_all("a"):
        href = anchor.get("href")

        if not isinstance(href, str) or not href.strip():
            continue

        absolute = urljoin(base_url, strip_unsafe_characters(href).strip())

        # https only: never hand the model javascript: or file: URLs.
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
    """Break any occurrence of a delimiter inside untrusted content."""

    # A page emitting the marker would read as the server's voice.

    for mark in marks:
        if mark and mark in text:
            text = text.replace(mark, mark[:3] + "[escaped]" + mark[3:])

    return text
