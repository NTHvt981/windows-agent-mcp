"""GitHub latest release tool for Windows Agent MCP Server."""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

from ..error import mcp_error
from ..log import log
from ..utils import HTTP_OPENER, HTTP_TIMEOUT_SECONDS, MAX_HTTP_BYTES, validate_url

__all__: list[str] = ["get_github_latest_release"]

# owner/repository. Dots are legal in GitHub names (a repo may even be called
# ".github"), so they cannot simply be banned -- but a segment made ENTIRELY
# of dots is a path traversal that would change the API URL requested, so each
# segment must contain at least one non-dot character.
_SEGMENT = r"(?=[^/]*[A-Za-z0-9_-])[A-Za-z0-9_.-]+"

GITHUB_REPO_PATTERN: re.Pattern[str] = re.compile(f"^{_SEGMENT}/{_SEGMENT}$")


def get_github_latest_release(
    repository: str,
) -> str:
    """Return the latest GitHub release metadata.

    Example: premake/premake-core

    This uses the GitHub API through the approved api.github.com domain.

    Args:
        repository: GitHub repository in format "owner/repository".

    Returns:
        JSON string containing release metadata, or a structured JSON error.
        This tool never raises.

    Example:
        >>> get_github_latest_release("premake/premake")
        '{"repository": "premake/premake", "tag": "v1.0.0", ...}',
    """

    if not GITHUB_REPO_PATTERN.fullmatch(repository):
        return mcp_error(
            "INVALID_REPOSITORY",
            "get_github_latest_release",
            f"'{repository}' is not of the form 'owner/repository'.",
            recovery=[
                "Do not retry with the same value.",
                "Pass exactly one slash, e.g. 'premake/premake-core'.",
                "Do not pass a full URL.",
            ],
        )

    url = f"https://api.github.com/repos/{repository}/releases/latest"

    try:
        validate_url(url)
    except ValueError as exc:
        return mcp_error(
            "URL_NOT_ALLOWED",
            "get_github_latest_release",
            str(exc),
            recovery=[
                "DO NOT retry the identical repository value.",
                "api.github.com must be on the host allowlist for this tool to work.",
            ],
        )

    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Bionic-Windows-Agent-MCP/1.0",
                "Accept": "application/vnd.github+json",
            },
            method="GET",
        )

        with HTTP_OPENER.open(
            request,
            timeout=HTTP_TIMEOUT_SECONDS,
        ) as response:
            data = response.read(MAX_HTTP_BYTES)

            release = json.loads(
                data.decode(
                    "utf-8",
                    errors="replace",
                )
            )

            assets: list[dict[str, Any]] = []

            for asset in release.get("assets", []):
                assets.append(
                    {
                        "name": asset.get("name"),
                        "size": asset.get("size"),
                        "download_url": asset.get("browser_download_url"),
                    }
                )

            result = {
                "repository": repository,
                "tag": release.get("tag_name"),
                "name": release.get("name"),
                "published_at": release.get("published_at"),
                "html_url": release.get("html_url"),
                "assets": assets,
            }

            return json.dumps(result, indent=2)

    # No `except ValueError` here on purpose: json.JSONDecodeError subclasses
    # it, so catching ValueError would report a malformed API response as a
    # refused URL, telling the caller to give up instead of retrying.
    except Exception as exc:
        log.exception("get_github_latest_release failed")

        return mcp_error(
            "GITHUB_REQUEST_FAILED",
            "get_github_latest_release",
            f"{type(exc).__name__}: {exc}",
            recovery=[
                "Do not repeatedly retry the identical request.",
                "Verify the repository exists and has at least one release; "
                "repositories with only tags return 404 here.",
            ],
        )
