"""Fetch HTTPS response tool for Windows Agent MCP Server."""

from __future__ import annotations

import urllib.request

from ..error import mcp_error
from ..log import log
from ..utils import HTTP_OPENER, HTTP_TIMEOUT_SECONDS, MAX_HTTP_BYTES, validate_url

__all__: list[str] = ["fetch_https_response"]


def fetch_https_response(
    url: str,
) -> str:
    """Fetch text from an approved HTTPS URL.

    Allowed domains are restricted to trusted development sources such as GitHub and Premake.

    Maximum response size: 2 MB.

    Useful for:
        - release metadata
        - documentation
        - GitHub API responses
        - text configuration files

    Args:
        url: HTTPS URL to fetch. Must be in an allowlisted domain.

    Returns:
        UTF-8 decoded string content from the response, or error message on failure.

    Raises:
        ValueError: If URL is invalid, not HTTPS, or hostname not allowlisted.
        Exception: For network errors or other failures.

    Example:
        >>> fetch_https_response("https://api.github.com/repos/example/repo/releases/latest")
        '{"name": "v1.0", ...}',  # JSON response decoded as string
    """

    try:
        validate_url(url)
    except ValueError as exc:
        # Only URL policy failures land here. Keeping this separate from the
        # request body matters: ValueError is raised by plenty of ordinary
        # code (json.JSONDecodeError subclasses it), and mislabelling that as
        # a refused URL tells the caller to stop rather than retry.
        return mcp_error(
            "URL_NOT_ALLOWED",
            "fetch_https_response",
            str(exc),
            path=url,
            recovery=[
                "DO NOT retry the identical URL.",
                "Only HTTPS URLs on the host allowlist are permitted.",
                "Ask the user to add the host to ALLOWED_NETWORK_HOSTS if it "
                "is genuinely needed.",
            ],
        )

    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Local-Agent-MCP/1.0",
                "Accept": "application/json,text/plain,*/*",
            },
            method="GET",
        )

        with HTTP_OPENER.open(
            request,
            timeout=HTTP_TIMEOUT_SECONDS,
        ) as response:
            content_length = response.headers.get("Content-Length")

            if content_length:
                try:
                    declared_size = int(content_length)

                    if declared_size > MAX_HTTP_BYTES:
                        return mcp_error(
                            "RESPONSE_TOO_LARGE",
                            "fetch_https_response",
                            f"The server declared a {declared_size:,} byte "
                            f"response, over the {MAX_HTTP_BYTES:,} byte limit.",
                            path=url,
                            recovery=[
                                "Do not retry the identical request.",
                                "Fetch a smaller resource, or use "
                                "download_file to save it to disk instead.",
                            ],
                        )

                except ValueError:
                    pass

            data = response.read(MAX_HTTP_BYTES + 1)

            if len(data) > MAX_HTTP_BYTES:
                return mcp_error(
                    "RESPONSE_TOO_LARGE",
                    "fetch_https_response",
                    f"The response exceeded the {MAX_HTTP_BYTES:,} byte limit "
                    f"while being read.",
                    path=url,
                    recovery=[
                        "Do not retry the identical request.",
                        "Fetch a smaller resource, or use download_file to "
                        "save it to disk instead.",
                    ],
                )

        return data.decode(
            "utf-8",
            errors="replace",
        )

    except Exception as exc:
        log.exception("fetch_https_response failed")

        return mcp_error(
            "HTTP_REQUEST_FAILED",
            "fetch_https_response",
            f"{type(exc).__name__}: {exc}",
            path=url,
            recovery=[
                "Do not repeatedly retry the identical request.",
                "Verify the URL is correct and the host is reachable.",
            ],
        )
