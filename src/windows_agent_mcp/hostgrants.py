"""Hosts the operator has approved for fetch_web_page, without a restart."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple, cast
from urllib.parse import urlparse

from .log import log

__all__: list[str] = [
    "DEFAULT_GRANTS_FILENAME",
    "EXTRA_HOSTS_ENV_VAR",
    "GRANTS_FILE_ENV_VAR",
    "PERSIST_ENV_VAR",
    "SCHEMA_VERSION",
    "HostGrant",
    "clear_session_grants",
    "find_grants_file",
    "granted_hosts",
    "grant_for_session",
    "load_grants",
    "main",
    "normalise_host",
    "parse_grants",
    "parse_host_list",
    "persist_enabled",
    "persist_grant",
    "scaffold",
    "session_grants",
]

DEFAULT_GRANTS_FILENAME = "mcp-allowed-hosts.json"

GRANTS_FILE_ENV_VAR = "WAMCP_ALLOWED_HOSTS_FILE"

EXTRA_HOSTS_ENV_VAR = "WAMCP_EXTRA_DOC_HOSTS"

# Approvals may come from an agent, not a human.
PERSIST_ENV_VAR = "WAMCP_HOST_GRANT_PERSIST"

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})

SCHEMA_VERSION = 1

# cmd.exe pre-splits on commas and semicolons.
_HOST_SEPARATORS = ",;"

_WILDCARD_CHARACTERS = "*?"


class HostGrant(NamedTuple):
    """One approved host."""

    host: str
    enable: bool
    note: str


_session_grants: set[str] = set()


def find_grants_file() -> Path:
    """Locate the grants file."""

    configured = os.environ.get(GRANTS_FILE_ENV_VAR, "").strip()

    if configured:
        return Path(configured).expanduser()

    in_cwd = Path.cwd() / DEFAULT_GRANTS_FILENAME

    if in_cwd.is_file():
        return in_cwd

    repo_root = Path(__file__).resolve().parents[2]

    if (repo_root / "pyproject.toml").is_file():
        return repo_root / DEFAULT_GRANTS_FILENAME

    return in_cwd


def normalise_host(raw: str) -> str:
    """Turn one operator-written entry into a hostname, or explain why not."""

    # Strict: silent coercion would approve more than read.

    entry = raw.strip()

    if not entry:
        raise ValueError("host cannot be empty")

    if any(character in entry for character in _WILDCARD_CHARACTERS):
        raise ValueError(
            f"'{entry}' contains a wildcard. Wildcards are not supported: "
            f"a subdomain grant is an any-host grant for that domain. "
            f"List each hostname you actually need."
        )

    if "://" in entry or "/" in entry:
        parsed = urlparse(entry if "://" in entry else f"https://{entry}")
        suggestion = parsed.hostname or ""

        raise ValueError(
            f"'{entry}' looks like a URL, not a hostname. "
            + (f"Use '{suggestion}'." if suggestion else "Use the hostname only.")
        )

    if "@" in entry:
        raise ValueError(f"'{entry}' contains credentials. Use the hostname only.")

    # Only 443 is ever fetched; a port would be silently ignored.
    if ":" in entry:
        raise ValueError(
            f"'{entry}' contains a port. Only HTTPS on port 443 is fetched, "
            f"so name the host alone."
        )

    host = entry.lower().rstrip(".")

    if not host:
        raise ValueError(f"'{entry}' is not a hostname")

    if " " in host or "\t" in host:
        raise ValueError(f"'{entry}' contains whitespace. One host per entry.")

    return host


def parse_host_list(raw: str | None) -> tuple[frozenset[str], list[str]]:
    """Parse a separated host list, as used by WAMCP_EXTRA_DOC_HOSTS."""

    if not raw or not raw.strip():
        return frozenset(), []

    normalised = raw
    for separator in _HOST_SEPARATORS:
        normalised = normalised.replace(separator, "\n")

    hosts: set[str] = set()
    errors: list[str] = []

    for line in normalised.splitlines():
        if not line.strip():
            continue

        try:
            hosts.add(normalise_host(line))
        except ValueError as exc:
            errors.append(f"{EXTRA_HOSTS_ENV_VAR}: {exc}")

    return frozenset(hosts), errors


def parse_grants(data: Any) -> tuple[dict[str, HostGrant], list[str]]:
    """Turn parsed JSON into grants, collecting every problem found."""

    errors: list[str] = []

    if not isinstance(data, dict):
        return {}, ["grants file must be a JSON object"]

    version = data.get("version", SCHEMA_VERSION)

    if version != SCHEMA_VERSION:
        return {}, [
            f"unsupported version {version!r}, expected {SCHEMA_VERSION}. "
            f"This build cannot read that format."
        ]

    raw_hosts: Any = data.get("hosts", [])

    if not isinstance(raw_hosts, list):
        return {}, ["'hosts' must be a list"]

    entries: list[Any] = raw_hosts

    grants: dict[str, HostGrant] = {}

    for index, item in enumerate(entries):
        label = f"hosts[{index}]"

        if isinstance(item, str):
            entry: dict[str, Any] = {"host": item}
        elif isinstance(item, dict):
            entry = cast("dict[str, Any]", item)
        else:
            errors.append(f"{label}: must be a hostname string or an object")
            continue

        raw_host = entry.get("host")

        if not isinstance(raw_host, str):
            errors.append(f"{label}: 'host' must be a string")
            continue

        try:
            host = normalise_host(raw_host)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue

        enable = entry.get("enable", True)

        if not isinstance(enable, bool):
            errors.append(f"{label}: 'enable' must be true or false")
            continue

        note = entry.get("note", "")

        if not isinstance(note, str):
            errors.append(f"{label}: 'note' must be a string")
            continue

        if host in grants:
            errors.append(f"{label}: duplicate host '{host}'")
            continue

        grants[host] = HostGrant(host=host, enable=enable, note=note)

    return grants, errors


def load_grants(path: Path) -> tuple[dict[str, HostGrant], list[str]]:
    """Read and validate a grants file."""

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, []
    except OSError as exc:
        return {}, [f"could not read {path}: {exc}"]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, [f"{path} is not valid JSON: {exc}"]

    return parse_grants(data)


def _load_file_hosts(path: Path) -> tuple[frozenset[str], str | None]:
    """Load enabled hosts from the grants file. Read fresh every time."""

    # No cache: NTFS mtime resolution makes it unsound.

    grants, errors = load_grants(path)

    hosts = frozenset(grant.host for grant in grants.values() if grant.enable)

    error = "; ".join(errors) if errors else None

    if error is not None:
        log.warning("%s: %s", DEFAULT_GRANTS_FILENAME, error)

    return hosts, error


def session_grants() -> frozenset[str]:
    """Hosts approved for this process only."""

    return frozenset(_session_grants)


def grant_for_session(host: str) -> None:
    """Approve a host for this process."""

    _session_grants.add(host)


def clear_session_grants() -> None:

    _session_grants.clear()


def granted_hosts() -> tuple[frozenset[str], str | None]:
    """Every host the operator has approved, from all three sources."""

    file_hosts, file_error = _load_file_hosts(find_grants_file())

    env_hosts, env_errors = parse_host_list(os.environ.get(EXTRA_HOSTS_ENV_VAR))

    problems = [message for message in [file_error, *env_errors] if message]

    hosts = file_hosts | env_hosts | frozenset(_session_grants)

    return hosts, "; ".join(problems) if problems else None


def persist_enabled() -> bool:
    """Whether an approved host may be written back to the grants file."""

    return os.environ.get(PERSIST_ENV_VAR, "").strip().lower() in _TRUTHY_VALUES


def persist_grant(host: str, *, note: str = "") -> str | None:
    """Add a host to the grants file, creating the file if needed."""

    try:
        host = normalise_host(host)
    except ValueError as exc:
        return str(exc)

    path = find_grants_file()

    grants, errors = load_grants(path)

    if errors:
        # Refusing beats discarding entries the operator meant to keep.
        return f"{path} has problems, so it was not modified: {errors[0]}"

    if host in grants and grants[host].enable:
        return None

    existing: dict[str, Any] = {}

    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (
            OSError,
            json.JSONDecodeError,
        ):  # pragma: no cover - load_grants caught it
            loaded = {}

        if isinstance(loaded, dict):
            existing = loaded  # pyright: ignore[reportUnknownVariableType]

    grants[host] = HostGrant(host=host, enable=True, note=note)

    existing["version"] = SCHEMA_VERSION
    existing["hosts"] = [
        {"host": grant.host, "enable": grant.enable, "note": grant.note}
        for grant in sorted(grants.values())
    ]

    payload = json.dumps(existing, indent=2) + "\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        handle, temporary = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
    except OSError as exc:
        return f"could not write {path}: {exc}"

    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)

        os.replace(temporary, path)
    except OSError as exc:
        return f"could not write {path}: {exc}"
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise

    return None


def scaffold() -> dict[str, Any]:
    """Build a starter grants file."""

    # Empty: pre-approving grants unchosen access.

    return {
        "version": SCHEMA_VERSION,
        "comment": (
            "Hosts fetch_web_page may read, in addition to the built-in "
            "documentation hosts. Read-only: these hosts are never used by "
            "download_file. Exact hostnames, no wildcards. Takes effect on "
            "the next call -- no restart needed."
        ),
        "hosts": [
            {
                "host": "example.com",
                "enable": False,
                "note": "Template. Set enable to true, or replace it.",
            }
        ],
    }


def _print_list(grants: dict[str, HostGrant], path: Path) -> None:
    """Print the grants table."""

    print(f"grants file: {path}")

    if not path.is_file():
        print("(no file yet; create one with --init or --add HOST)")

    env_hosts, env_errors = parse_host_list(os.environ.get(EXTRA_HOSTS_ENV_VAR))

    if not grants and not env_hosts:
        print("no granted hosts")
    else:
        print()
        print(f"{'host':40} {'state':10} note")

        for grant in sorted(grants.values()):
            state = "enabled" if grant.enable else "DISABLED"
            print(f"{grant.host:40} {state:10} {grant.note}")

        for host in sorted(env_hosts):
            print(f"{host:40} {'env':10} from {EXTRA_HOSTS_ENV_VAR}")

    for message in env_errors:
        print(f"error: {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Operator CLI for the grants file."""

    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m windows_agent_mcp.hostgrants",
        description=(
            "Manage the hosts fetch_web_page may read, beyond the built-in "
            "documentation hosts."
        ),
    )
    parser.add_argument("--file", help="grants file to use")
    parser.add_argument(
        "--init",
        action="store_true",
        help="write a starter grants file",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --init, overwrite an existing file",
    )
    parser.add_argument("--add", metavar="HOST", help="approve one host")
    parser.add_argument(
        "--note",
        default="",
        help="with --add, record why",
    )
    parser.add_argument(
        "--remove",
        metavar="HOST",
        help="revoke one host by deleting its entry",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="show the granted hosts (the default action)",
    )

    args = parser.parse_args(argv)

    if args.file:
        os.environ[GRANTS_FILE_ENV_VAR] = args.file

    path = find_grants_file()

    if args.init:
        if path.exists() and not args.force:
            print(
                f"error: {path} already exists. Use --force to overwrite.",
                file=sys.stderr,
            )
            return 1

        try:
            path.write_text(json.dumps(scaffold(), indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write {path}: {exc}", file=sys.stderr)
            return 1

        print(f"wrote {path}")

    if args.add:
        error = persist_grant(args.add, note=args.note)

        if error is not None:
            print(f"error: {error}", file=sys.stderr)
            return 1

        print(f"granted {args.add}")

    if args.remove:
        error = _remove(path, args.remove)

        if error is not None:
            print(f"error: {error}", file=sys.stderr)
            return 1

        print(f"revoked {args.remove}")

    grants, errors = load_grants(path)

    for message in errors:
        print(f"error: {message}", file=sys.stderr)

    if args.list or not (args.init or args.add or args.remove):
        _print_list(grants, path)

    return 1 if errors else 0


def _remove(path: Path, host: str) -> str | None:
    """Delete one host's entry from the grants file."""

    try:
        host = normalise_host(host)
    except ValueError as exc:
        return str(exc)

    grants, errors = load_grants(path)

    if errors:
        return f"{path} has problems, so it was not modified: {errors[0]}"

    if host not in grants:
        return f"'{host}' is not in {path}"

    del grants[host]

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (
        OSError,
        json.JSONDecodeError,
    ) as exc:  # pragma: no cover - load_grants caught it
        return f"could not read {path}: {exc}"

    existing: dict[str, Any] = loaded if isinstance(loaded, dict) else {}

    existing["version"] = SCHEMA_VERSION
    existing["hosts"] = [
        {"host": grant.host, "enable": grant.enable, "note": grant.note}
        for grant in sorted(grants.values())
    ]

    try:
        path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return f"could not write {path}: {exc}"

    return None


if __name__ == "__main__":
    raise SystemExit(main())
