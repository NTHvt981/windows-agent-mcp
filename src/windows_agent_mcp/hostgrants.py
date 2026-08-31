"""Hosts the operator has approved for fetch_web_page, without a restart.

The problem this solves. `fetch_web_page` reads from a fixed list of reference
documentation hosts (`utils.ALLOWED_DOC_HOSTS`), and everything else needs
research mode -- which is all-or-nothing and requires restarting the server.
So when a model hit a legitimate but unlisted host, the denial told it to "ask
the user", and the user's only available answer was "set
WAMCP_WEB_RESEARCH=1 and restart", i.e. open the network posture completely.
There was no way to say "yes, that one host".

This module is that way. Hosts named here are added to ALLOWED_DOC_HOSTS for
every fetch, and the file is re-read on every fetch -- not cached -- so a
grant takes effect on the model's *next call* with no restart.

Three properties that are load-bearing rather than incidental:

* **The model cannot grant itself a host.** `utils.resolve_write_path` refuses
  to write any file named `mcp-allowed-hosts.json`, anywhere. That is
  deliberately broader than "the file we are currently reading": the search
  order below prefers the current directory, so a model able to *create* a
  grants file where none existed would grant itself the internet. Refusing by
  filename closes that, including for files that do not exist yet.
* **A grant is read-only.** Granted hosts join ALLOWED_DOC_HOSTS (fetched into
  context), never ALLOWED_NETWORK_HOSTS (bytes written to disk). Approving a
  host to read is a much smaller decision than approving it to deliver files.
* **No wildcards.** `*.example.com` reads like a narrow grant and is not: one
  forgotten subdomain CNAME, or any host that lets strangers publish under a
  subdomain, turns it into an any-host grant for that zone. Entries are exact
  hostnames.

What a grant does cost. An approved host can be fetched with an arbitrary
path, so if that host is attacker-controlled it reopens the exfiltration
channel research mode opens -- for that host only. "I trust this site" is the
decision being made, not "just this once".
"""

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

# Points at a grants file elsewhere, for a client whose working directory is
# not the repository. Mirrors WAMCP_PROFILES_FILE.
GRANTS_FILE_ENV_VAR = "WAMCP_ALLOWED_HOSTS_FILE"

# Hosts as a plain list, for the case where a file is awkward: some MCP clients
# expose an `env` block and nothing else, and a profile can carry a per-profile
# value. Merged with the file rather than overriding it.
EXTRA_HOSTS_ENV_VAR = "WAMCP_EXTRA_DOC_HOSTS"

# Opt-in for writing an approved host back to the grants file.
#
# Off by default, and that is a security decision rather than caution. The MCP
# spec permits a client to answer an elicitation itself -- "If the client is an
# agent, it might decide how to handle the elicitation" -- so an approval is
# not proof a human saw it. A session-scoped grant that an agent auto-approved
# dies with the process; a persisted one would be permanent. Persisting
# therefore requires the operator to say, once, that their client really does
# put the question to a person.
PERSIST_ENV_VAR = "WAMCP_HOST_GRANT_PERSIST"

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})

# Bumped only for a breaking format change. An unrecognised version is an error
# rather than a best-effort read.
SCHEMA_VERSION = 1

# Separators accepted in WAMCP_EXTRA_DOC_HOSTS. Comma is the natural one;
# semicolon matches WAMCP_PROJECT_ROOTS, and whitespace costs nothing to
# accept. cmd.exe splits batch arguments on commas AND semicolons, so a
# launcher flag will arrive pre-split -- accepting both means the rejoined
# value parses either way.
_HOST_SEPARATORS = ",;"

# Characters that make a "hostname" something else. Checked explicitly so the
# error can name the problem rather than saying "invalid".
_WILDCARD_CHARACTERS = "*?"


class HostGrant(NamedTuple):
    """One approved host.

    Attributes:
        host: Exact lowercase hostname.
        enable: False parks the grant: it stays in the file, with its note, but
            is not applied. The point of a file over an environment variable is
            that a revoked host can be revoked without losing the record of
            why it was ever added.
        note: Free text. This is where "needed for the RTS pathfinding
            articles" lives.
    """

    host: str
    enable: bool
    note: str


# Hosts approved for this process only, via an elicitation the operator
# answered. Not written anywhere: see PERSIST_ENV_VAR.
_session_grants: set[str] = set()


def find_grants_file() -> Path:
    """Locate the grants file.

    Order, most explicit first:

    1. WAMCP_ALLOWED_HOSTS_FILE.
    2. `mcp-allowed-hosts.json` in the current directory. The launcher pushd's
       to the repository root, so this is the normal case.
    3. The repository root inferred from this file's location, but only for an
       editable install -- confirmed by pyproject.toml being there. In a
       site-packages install that path is meaningless.

    Returns:
        The path to use. May not exist; callers handle that.
    """

    configured = os.environ.get(GRANTS_FILE_ENV_VAR, "").strip()

    if configured:
        return Path(configured).expanduser()

    in_cwd = Path.cwd() / DEFAULT_GRANTS_FILENAME

    if in_cwd.is_file():
        return in_cwd

    # src/windows_agent_mcp/hostgrants.py -> repository root
    repo_root = Path(__file__).resolve().parents[2]

    if (repo_root / "pyproject.toml").is_file():
        return repo_root / DEFAULT_GRANTS_FILENAME

    return in_cwd


def normalise_host(raw: str) -> str:
    """Turn one operator-written entry into a hostname, or explain why not.

    Strict on purpose. The tempting alternative is to accept a pasted URL and
    quietly use its host, but silent coercion in a trust file is how an
    operator ends up approving something other than what they read. Instead the
    error names the hostname they probably meant, so the fix is a copy-paste
    rather than a puzzle.

    Args:
        raw: One entry, as written.

    Returns:
        The lowercase hostname.

    Raises:
        ValueError: If the entry is not a bare hostname.
    """

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

    # A hostname has no port: the fetcher only ever connects on 443, so a port
    # here would be silently ignored rather than honoured.
    if ":" in entry:
        raise ValueError(
            f"'{entry}' contains a port. Only HTTPS on port 443 is fetched, "
            f"so name the host alone."
        )

    host = entry.lower().rstrip(".")

    if not host:
        raise ValueError(f"'{entry}' is not a hostname")

    # Deliberately loose beyond this point: an allowlist entry that matches
    # nothing is inert, so the checks above (which catch entries that mean
    # something *broader* than they look) are the ones that matter.
    if " " in host or "\t" in host:
        raise ValueError(f"'{entry}' contains whitespace. One host per entry.")

    return host


def parse_host_list(raw: str | None) -> tuple[frozenset[str], list[str]]:
    """Parse a separated host list, as used by WAMCP_EXTRA_DOC_HOSTS.

    Args:
        raw: Separated list, or None.

    Returns:
        (hosts, errors). Bad entries are dropped and reported; good ones in the
        same list still apply, because failing an entire list over one typo
        would silently revoke hosts the operator already relies on.
    """

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
    """Turn parsed JSON into grants, collecting every problem found.

    Pure: takes already-decoded JSON, so the whole table is testable without
    touching the filesystem.

    Accepts two entry shapes, because both are things an operator will
    reasonably write::

        {"version": 1, "hosts": ["docs.example.com"]}

        {"version": 1, "hosts": [
            {"host": "docs.example.com", "enable": true, "note": "why"}
        ]}

    Args:
        data: Decoded contents of the grants file.

    Returns:
        (grants keyed by host, errors). Entries that failed validation are
        omitted; the rest still load.
    """

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
    """Read and validate a grants file.

    A missing file is NOT an error: no grants is the normal, default state, and
    a warning on every fetch would train the operator to ignore warnings.

    Args:
        path: File to read.

    Returns:
        (grants, errors).
    """

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
    """Load enabled hosts from the grants file. Read fresh every time.

    There is deliberately NO cache here, and the first attempt at one is worth
    recording because it looked obviously correct: key the parsed result on
    `(path, st_mtime_ns, st_size)` and reuse it while those are unchanged.

    That is unsound on Windows. Despite the nanosecond field, mtime resolution
    on NTFS here is about a millisecond, so two writes of the same length
    within one tick produce an identical signature -- and the cache then serves
    the OLD host set. For this file that means a revoked host still reading as
    granted, or a fresh grant still reading as refused. A cache that can be
    wrong about a trust decision is not worth having.

    The saving it bought was negligible anyway: this runs once per fetch plus
    once per redirect hop, against an operation that already does a DNS lookup
    and a TLS handshake. Reading and parsing a file of a few hundred bytes does
    not register next to that.

    Args:
        path: Grants file to read.

    Returns:
        (enabled hosts, error) -- fail closed, with the reason.
    """

    grants, errors = load_grants(path)

    hosts = frozenset(grant.host for grant in grants.values() if grant.enable)

    error = "; ".join(errors) if errors else None

    if error is not None:
        # Logged as well as returned: the model sees the returned message only
        # when it calls get_server_info, and a malformed grants file otherwise
        # looks exactly like a host that was never granted.
        log.warning("%s: %s", DEFAULT_GRANTS_FILENAME, error)

    return hosts, error


def session_grants() -> frozenset[str]:
    """Hosts approved for this process only."""

    return frozenset(_session_grants)


def grant_for_session(host: str) -> None:
    """Approve a host for this process.

    Args:
        host: Hostname, already normalised.
    """

    _session_grants.add(host)


def clear_session_grants() -> None:
    """Forget every session grant. Used by tests, and by nothing else."""

    _session_grants.clear()


def granted_hosts() -> tuple[frozenset[str], str | None]:
    """Every host the operator has approved, from all three sources.

    Sources are merged rather than ranked: a host is granted if any of the
    grants file, WAMCP_EXTRA_DOC_HOSTS, or an elicitation this session says
    so. There is no "deny" entry to conflict with -- `"enable": false` removes
    a host from the file's contribution, it does not veto the others -- so
    merging cannot produce a surprising result.

    Fails closed: a grants file that cannot be read or parsed contributes no
    hosts, and the reason comes back so get_server_info can show it.

    Returns:
        (hosts, error) where error is a human-readable summary or None.
    """

    file_hosts, file_error = _load_file_hosts(find_grants_file())

    env_hosts, env_errors = parse_host_list(os.environ.get(EXTRA_HOSTS_ENV_VAR))

    problems = [message for message in [file_error, *env_errors] if message]

    hosts = file_hosts | env_hosts | frozenset(_session_grants)

    return hosts, "; ".join(problems) if problems else None


def persist_enabled() -> bool:
    """Whether an approved host may be written back to the grants file."""

    return os.environ.get(PERSIST_ENV_VAR, "").strip().lower() in _TRUTHY_VALUES


def persist_grant(host: str, *, note: str = "") -> str | None:
    """Add a host to the grants file, creating the file if needed.

    Written atomically via a temporary file in the same directory plus
    os.replace, so an interrupted write cannot leave a truncated trust file --
    which would fail closed, but would also silently revoke every host the
    operator had approved.

    Existing content is preserved: unknown top-level keys are kept, and an
    entry already present is left alone rather than rewritten, so a note the
    operator wrote by hand survives.

    Args:
        host: Hostname to add. Normalised here, so a caller may pass raw input.
        note: Optional reason to record alongside it.

    Returns:
        None on success, or a message explaining why nothing was written.
    """

    try:
        host = normalise_host(host)
    except ValueError as exc:
        return str(exc)

    path = find_grants_file()

    grants, errors = load_grants(path)

    if errors:
        # Refusing rather than overwriting: rewriting a file we could not
        # understand would discard entries the operator meant to keep.
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
        # Includes KeyboardInterrupt: a stray .tmp beside a trust file is
        # confusing at exactly the wrong moment.
        Path(temporary).unlink(missing_ok=True)
        raise

    return None


def scaffold() -> dict[str, Any]:
    """Build a starter grants file.

    Empty of real hosts on purpose. A scaffold that pre-approved a few
    plausible sites would be granting access the operator never chose, and the
    hosts worth having by default are already in ALLOWED_DOC_HOSTS.

    Returns:
        JSON-serialisable contents.
    """

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
    """Operator CLI for the grants file.

    This is the answer to "the model asked, I said yes, now what": one command,
    effective on the model's next call, no restart and no editor.

    Args:
        argv: Arguments, defaulting to sys.argv[1:].

    Returns:
        Process exit code.
    """

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
    """Delete one host's entry from the grants file.

    Returns:
        None on success, or a message explaining why nothing changed.
    """

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
