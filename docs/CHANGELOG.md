# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `WAMCP_LAUNCH_CWD` — the launcher records the directory it was started in.
  `run_server.bat` `pushd`'s to the server's own directory (to find its
  `.venv`), so by the time Python runs, `Path.cwd()` is the *install* directory,
  not the project the client launched from. `get_server_info`'s `server_cwd`
  confirmed this: under opencode it read `…\windows-agent-mcp`, not the open
  project — which made `WAMCP_WORKSPACE_FROM_CWD` adopt the wrong directory and
  refuse writes to the actual project.

  The launcher now captures `%CD%` **before** the `pushd` into `WAMCP_LAUNCH_CWD`,
  and `workspace_from_cwd()` prefers it over `Path.cwd()`. `get_server_info`
  reports it as `launch_cwd` so the captured directory is verifiable in one call.
  The safety guard still applies — a launch directory that resolves to a drive
  root, home, or system directory is refused.

- `WAMCP_WORKSPACE_FROM_CWD` — opt-in automatic workspace. When set to `1`, the
  server also treats its own current directory as a writable root, so a launcher
  that starts the server in the active project (opencode) needs no per-project
  path. It **fails closed**: a cwd that resolves to a drive root, the home
  directory (or an ancestor of it), or a system directory is refused, because
  the dangerous failure of automatic detection is not a broken tool but a
  silently wider write boundary. Off by default, so existing installs are
  unchanged. It flows through the single `get_allowed_working_directories()`, so
  writes and command execution widen together and reads stay unconfined — no
  parallel path-security layer.

  `get_server_info` now reports `server_cwd` (what directory the server was
  launched in), `adopted_workspace` (the cwd if it was accepted, else null), and
  `workspace_note` (why it was refused). `server_cwd` answers, without a debug
  print, the one question a cwd-based workspace depends on: does the launcher
  start the server in the project directory?

- `search_files` now says when a literal search was handed regex syntax. A
  pattern containing `\(`, `\d` or `.*` that finds nothing reports the likely
  cause and names `regex=True`, ahead of the generic "build directories are
  excluded" note — which was the advice given before, and was wrong. A bare
  `(` or `.` is deliberately not flagged: searching literally for `mcp_error(`
  is both common and correct.

- `search_files` reports a capped `max_results`. Values above the ceiling were
  silently clamped, which looks exactly like a search that really does have
  that many matches.

  These two came from one measured session. A model hit a truncated search,
  correctly decided to narrow, and narrowed to `return mcp_error\(` without
  setting `regex=True`. The escape was matched literally, the search reported
  "No matches", and the advice pointed at excluded build directories. It then
  raised `max_results` to 200 and to 300 — both silently clamped to 100, giving
  the identical result each time — before answering from the original truncated
  list as though it were complete, understating the total by a third.

  Every step it took was reasonable. Both dead ends are now named.

- `get_server_info` now reports `registered_tools` (the live tool names, in
  registration order) and `tools_by_group` (every group's membership, inactive
  groups included).

  It previously reported only `registered_tool_count`, so "which tools do you
  have" was not answerable from it. A 9B model asked that question called the
  tool, read the count, and then filled in the names from its *client's* tool
  list — attributing shell, git and file-writing tools to this server's
  read-only `core` group, and inventing a membership for `docs`. The group
  names it gave were correct; everything about their contents was not.

  `tools_by_group` also makes "why can I not see `compile_shader`" answerable
  by reading: the name appears under `build`, and `build` is absent from
  `active_tool_groups`.

- `build_project` timeout results are now structured. A timeout returns JSON
  carrying `status: "timeout"`, `pid`, `elapsed_ms`, `timeout_seconds` and
  `killed: true`, plus the tail of the output captured so far.

  Two calls in the feedback returned a bare timeout while `msbuild` kept
  running: the caller could not tell "build failed" from "build is slow" from
  "build succeeded after I stopped listening", and the output it lost was the
  first compile error. An explicitly larger `timeout_seconds` made the same
  build return exit 0.

- `build_project` refuses a second build for a working directory that already
  has one running, with `BUILD_ALREADY_RUNNING` naming the running command.
  Concurrent `msbuild` on the same `obj/` and `libs` does not fail cleanly — it
  produces `LNK1163`/`LNK1104` lock contention that reads like a source error,
  and the model then edits correct code. Refused rather than queued, because
  queueing an MCP call for minutes with no feedback is its own failure. Only
  builds this server process started are detected.

- `build_project` retains the full, untruncated output to
  `<download_root>/build-logs/<utc>-<dirhash>-<ns>.log` when the summary would
  be truncated or the build timed out, and reports the path as `full_log` (and
  `Full log:` in the timeout prose). The truncated summary is then a pointer
  rather than a dead end. Small successful builds write no file.

- Warning triage in the build report: a per-code `WARNING SUMMARY` histogram
  (`C4996 x7 (deps)`), a `[likely pre-existing]` tag for warnings from vendored
  paths (`deps/`, `third_party/`, …) or known-benign codes (`C4996`, `LNK4099`),
  and a `rebuild:` line (`up-to-date`/`incremental`/`full`) when the log makes
  it unambiguous. The vendored/benign hint is what keeps an agent from chasing
  the third-party `C4996` noise the feedback reported in `deps/raygui`.

### Changed

- `build_project` appends `/v:minimal /nologo` to `msbuild` commands that set
  no verbosity of their own. The feedback passed `/v:minimal` by hand on every
  call; it changes console noise only, not what builds.

- Build reports now state elapsed time and counts. The header carries
  `(exit code 0, 12.3s)` and the verdict reads
  `BUILD SUCCEEDED (0 errors, 7 warnings)`, so a two-second incremental build is
  distinguishable from a ten-minute cold link without guessing.

- Oversized process output now keeps both ends — the first lines and the last —
  with an omission marker between them, instead of the head only. A build's
  actionable content sits at both ends, and for a timeout the tail is the whole
  point.

- A truncated `search_files` result now states that the list is **incomplete**
  and that its lines must not be counted to produce a total. The previous
  notice reported the truncation accurately and was read, understood, and then
  not acted on; the count it produced was wrong by a third.

### Fixed

- `get_system_info` reported the wrong Windows version. It returned `release`
  and `version` as separate fields; asked for the OS version, a 9B model quoted
  `version` — `"10.0.26200"` — read the leading `10.` and answered "Windows 10"
  on a Windows 11 machine, contradicting the `release` field beside it.

  `os` is now the name and release already joined (`"Windows 11"`), and the NT
  version is `os_build`. There is no longer a field called `version` for a
  model to reach for when the question uses that word.

  The value is also corrected against the build number, because
  `platform.release()` genuinely returns `"10"` on Windows 11 under Python 3.10
  and 3.11 (fixed upstream in 3.12; `requires-python` is `>=3.10`). Without
  that, joining the fields would have produced a confident `"Windows 10"` on
  those interpreters — worse than the inconsistency it replaced.

  Found by driving a live model, not by the suite: the tool call was correct and
  only the answer was wrong, so nothing here was failing.

- A timed-out `build_project` left the build running. On Windows
  `subprocess.run` calls `process.kill()`, which terminates only the direct
  child; `msbuild`'s `cl.exe`/`link.exe` grandchildren survived and kept writing
  `obj/` and `libs`, so a retry raced them and failed with lock errors presented
  as source bugs. The process tree is now killed via `taskkill /F /T` before the
  output drain, which is what makes the documented manual fix —
  `Stop-Process -Name msbuild,cl,link -Force` — unnecessary.

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
