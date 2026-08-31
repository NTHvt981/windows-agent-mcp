# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-08-28

First public release.

An MCP server giving a local AI model a restricted, well-described set of tools
for software work on Windows — built specifically for small models (7B–9B),
where context is the scarce resource and a wrong tool choice is expensive.

### Tools

Seventeen tools in six groups, so a session registers only what it needs:

- **core** — `read_file`, `list_directory`, `list_empty_dirs`, `find_files`,
  `search_files`, `get_system_info`, `get_server_info`. Read-only: cannot
  write, execute or reach the network. Always registered.
- **edit** — `write_file`, `edit_file`.
- **build** — `run_powershell`, `build_project`, `compile_shader`,
  `get_gpu_info`.
- **docs** — `fetch_web_page`, scoped to reference documentation hosts.
- **net** — `download_file`, `fetch_https_response`,
  `get_github_latest_release`.
- **search** / **research** — `web_search`, and in `research` any-public-host
  fetching. Neither is on by default.

### Design

- **Every tool truncates its output** against an explicit limit and says so,
  including the exact call needed to continue. An unbounded read can consume a
  small model's entire context in one call.
- **Every error is a structured envelope with recovery instructions.** A model
  told "permission denied" retries; one told "DO NOT retry, ask the user to set
  `WAMCP_PROJECT_ROOTS`" reports the problem and stops.
- **`build_project` parses compiler output rather than returning it.** MSVC,
  clang, CMake, ninja and the shader compilers; identical diagnostics collapse
  to one entry with a count. A raw build log is the context window for a small
  model.
- **Tool groups and named profiles** keep the tool list short. The definitions
  are re-serialised into the prompt every turn, so they are a permanent tax
  rather than a one-off cost.

### Configuration

- **All settings live in `input/config.json`**, generated on first run with the
  complete table: every setting, its default, and what it does. The options are
  discoverable by opening the file rather than by reading documentation.
- **An environment variable still overrides the file** for that run, so a
  client's `env` block — the only configuration channel some MCP clients have —
  keeps working. Overrides are reported, never silent.
- Named profiles live in the same file, so there is one configuration file
  rather than two. `python -m windows_agent_mcp.config --list` shows both, and
  `--emit client` writes a ready-to-paste MCP client configuration with the
  absolute paths a client actually needs.
- A missing file is generated; a malformed one, an unknown setting or an
  unknown profile leaves the server running on defaults with the reason in
  `get_server_info`. Nothing about the file can stop the server starting.

### Security

- Writes are confined to a download sandbox plus any directory in
  `WAMCP_PROJECT_ROOTS`; reads are deliberately unconfined. Paths resolve
  before the containment check, so `..` and symlinked parents cannot escape it.
- The server's own configuration files cannot be written by a tool, refused by
  filename anywhere on disk.
- PowerShell is pattern-checked and allowlisted, one command per call, with
  base64 payload detection. This bounds *which program starts*, not what it
  then does — it is not a sandbox, and the README says so plainly.
- Network access is HTTPS-only on port 443, with per-hop redirect validation
  and private-address rejection. Readable hosts are tiered: a documentation
  list, an operator-granted list, and the download allowlist, kept separate
  because only one of them writes bytes to disk.
- Web hosts can be granted one at a time, effective immediately with no
  restart. Where the client supports MCP elicitation the model can ask
  directly; approvals last until restart unless the operator opts in to
  persisting them.
- Everything fetched from the web is wrapped in untrusted-content markers, and
  the extractor strips the places instructions hide — comments, `hidden`,
  `aria-hidden`, `display:none`, and bidi/zero-width characters.

### Known limits

Stated rather than implied, because they decide how much to trust it:

- **This is not a sandbox.** For genuinely untrusted input, use VM or container
  isolation.
- **Write access plus command execution is a code-execution loop.** The project
  roots you grant are the blast radius.
- **DNS is resolved twice** — once by the validator, once by the connection —
  so an attacker controlling their own zone can rebind between them. Closing
  that needs a pinned-IP connector, which this server does not have.
