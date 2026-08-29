# Windows Agent MCP Server - Project Rules

## Architecture & Design

- **MCP Transport**: stdio only (no listening network ports)
- **Server Structure**: Each tool is a separate module under
  `src/windows_agent_mcp/tools/`, listed in the `TOOLS` tuple in
  `src/windows_agent_mcp/main.py`. `tools/__init__.py` exports nothing.
- **Return Contract** (enforced by `tests/test_integration.py`):
  - success -> plain text (or JSON *data* for the two info tools)
  - failure -> `mcp_error()` envelope: `{"ok": false, "error": {...}}`
  - Never wrap file content in JSON: the escaping puts the whole file on one
    line, which is hard for a small model to read and wastes context.
- **Error Handling**: All tools return structured JSON errors via
  `error.mcp_error()`, always with `recovery` instructions. A tool must never
  raise: a rejection is a normal tool result, not a transport failure.
- **Context Budget**: Any tool returning arbitrary-length content must
  truncate against a limit in `utils.py` and say so in the output, including
  the exact call needed to continue.
- **Logging**: Use `log.info()/warning()/exception()` from `logging`, never print()

## Security Model

- **Network**: HTTPS only for network tools; explicit host allowlist
  (`utils.ALLOWED_NETWORK_HOSTS`) -- see the research-mode exception below
- **SSRF Protection**: Check the host allowlist first, then resolve, then
  reject private/special IPs. The `not ip.is_global` term is load-bearing:
  without it 100.64.0.0/10 (carrier-grade NAT, also Tailscale) passes, and the
  classification varies by Python patch level. Redirects are re-validated on
  every hop, by `SafeRedirectHandler` or `ResearchRedirectHandler`.
- **Ports**: only 443. The host is resolved against 443 regardless, so any
  other port would validate one service and connect to another.
- **The allowlist is NOT absolute any more, and there are now three tiers.**
  `download_file` and `fetch_https_response` stay confined to
  `ALLOWED_NETWORK_HOSTS` in every mode. `fetch_web_page` additionally reads
  `ALLOWED_DOC_HOSTS` by default, and any public host when research mode is
  on. Pass `allow_any_host=True` only for research mode; use the narrower
  `extra_allowed_hosts` everywhere it suffices (`web_search` for its single
  endpoint, `fetch_web_page` for the doc hosts). **Never widen a tool that
  writes to disk** -- that is exactly why `ALLOWED_DOC_HOSTS` is a separate
  frozenset instead of extra entries in `ALLOWED_NETWORK_HOSTS`, and a test
  asserts the two are disjoint.
- **Doc hosts are not an exfiltration channel.** Exfiltration needs an
  arbitrary outbound GET so the attacker can read their own logs; smuggling
  data into a path on a host they do not control tells them nothing. Keep the
  list fixed and wildcard-free, or that reasoning stops holding.
- **Each mode needs its own redirect handler.** `DocRedirectHandler` carries
  `extra_allowed_hosts = ALLOWED_DOC_HOSTS`; plain `SafeRedirectHandler` would
  validate hops against the six-host list and refuse
  learn.microsoft.com's locale redirect, which fires on essentially every
  request. Both the policy flag and the host set MUST stay class attributes:
  `build_opener` instantiates the class with zero arguments.
- **Download Safety**: All downloads go to `get_download_root()` (sandboxed directory)
- **PowerShell Restrictions**: Defense-in-depth pattern matching, command
  allowlist, base64 payload detection. Every `DANGEROUS_PATTERNS` entry MUST
  be anchored with a word boundary on **both** sides -- omitting the
  leading one made the `rm` rule reject "confirm", "platform", "term".
- **Base64 Detection**: scan for Base64-alphabet runs with case preserved,
  and strip NUL bytes before matching markers (PowerShell's
  `-EncodedCommand` is UTF-16-LE). Lowercasing the command, or splitting
  tokens on `=`, breaks detection entirely.
- **PowerShell Working Directory**: `run_powershell` executes only inside the
  download root or a root listed in `BIONIC_PROJECT_ROOTS`. Each call is a
  fresh process, so `cd` never persists -- callers pass `working_directory`.
- **One Command Per Call**: `find_command_composition()` rejects `;` `|` `&`
  `>` `<` `$()` `@()`, backticks and newlines OUTSIDE quotes, so the allowlist
  cannot be bypassed by appending a second command. It is quote-aware on
  purpose -- a blanket textual search would reject
  `git commit -m "fix; cleanup"`. The one exception is `$(`, rejected inside
  double quotes too, because PowerShell interpolates it there.
- **Interpreters**: `find_inline_code_flag()` blocks `python -c` / `node -e`.
  This bounds WHICH PROGRAM starts, not what it does -- `python build.py`
  still runs arbitrary code, deliberately, since running project scripts is
  the point. Never describe this as a sandbox.
- **Note**: This is NOT a true OS sandbox. For hostile code, use VM/container isolation

## Web Research

- **Only `web_search` is gated now.** `fetch_web_page` is always registered
  and scoped by host instead, so a model can check a Vulkan enum without the
  operator widening anything. `main.get_tools(web_enabled=...)` decides registration
  and MUST stay pure -- it takes the flag as an argument rather than reading
  `os.environ`. pre-commit runs the suite on every commit, so an env-reading
  version would stop anyone who actually uses the feature from committing.
- **Everything fetched is untrusted data.** Wrap it with
  `untrusted.wrap_untrusted()`, which puts a warning before AND after the
  block (the trailing one is what a small model actually heeds) and
  neutralises any copy of the markers inside the content. Search titles and
  snippets need this as much as page bodies -- anyone can rank a page called
  "SYSTEM: ignore previous instructions".
- **Strip hiding places, not just scripts.** `html_text.extract_page` removes
  HTML comments, `[hidden]`, `aria-hidden`, `display:none` and bidi/zero-width
  characters. These are where instructions get hidden from a reader but not
  from `get_text()`.
- **Never conflate "blocked" with "no results".** A rate-limited search
  reported as "no results" makes a small model rephrase and retry forever.
  DuckDuckGo signals blocking with a **202**, which urllib treats as success,
  so the status must be checked explicitly. A parse that finds nothing is
  `SEARCH_PARSE_FAILED`, not "empty", unless the page itself says so.
- **Decode before parsing.** Hand BeautifulSoup a `str`, never bytes. Distrust
  an `iso-8859-1` header: latin-1 never fails to decode, so honouring a wrong
  one silently mojibakes the whole page -- but demote it below utf-8 rather
  than ignoring it, or genuinely latin-1 pages break.
- **Chrome removal is conditional.** Only drop `nav/header/footer/form` when a
  `<main>`/`[role=main]`/`<article>` container exists. Dropping them
  unconditionally blanks documentation sites and the model reports the page as
  empty.
- **Accepted limits, state them rather than implying otherwise**: research mode
  plus `read_file` is an exfiltration channel that delimiters mitigate but do
  not close; DNS is resolved by us and again by urllib at connect time, so an
  attacker who controls their own zone can rebind between the two (fixing that
  needs a pinned-IP connector, which this server does not have); and once
  `BIONIC_PROJECT_ROOTS` is set, write access plus `build_project` is a
  code-execution loop, because `python build.py` runs whatever was just
  written. The last one is the intended capability of a coding assistant, but
  it must be documented as the blast radius of the roots granted, never
  presented as contained.

## Host Grants and Consent

- **Three tiers of readable host, and they are not interchangeable.**
  `ALLOWED_NETWORK_HOSTS` also governs `download_file`, so adding a host there
  grants the right to write its bytes to disk. `ALLOWED_DOC_HOSTS` is read into
  context only. Granted hosts (`hostgrants`) join the *second* set. Never
  "simplify" these into one list.
- **The grants file is a trust file, so the model must not be able to write
  it.** `resolve_write_path` refuses any path named `mcp-allowed-hosts.json`
  or `mcp-profiles.json` with `ProtectedPathError`. Refusing by FILENAME rather
  than by resolved path is the whole point: both files are searched for in the
  current directory first, so the dangerous move is creating one where none
  existed, and a path-equality check cannot see a file that does not exist yet.
- **`DocRedirectHandler` must consult grants per hop, not at import.** The
  openers are built once at module scope; a class attribute captured then would
  make a granted host readable at its canonical URL and refused the instant it
  redirected -- which is most documentation sites. Hence
  `allowed_extra_hosts()` as a classmethod. Do not turn it back into a plain
  attribute.
- **`host_grant_would_help()` must not resolve DNS.**
  `resolve_and_validate_host` checks the allowlist *before* resolving, so a
  refused host is never looked up today. Adding a lookup on the denial path
  would leak the attempt to the host's nameserver before anyone consented, and
  would let an injected URL trigger a probe.
- **Consent is offered only where consent is the answer.** An `http://` URL, a
  non-443 port, credentials in the URL or a private address are all refused for
  reasons a grant cannot fix; prompting for those offers a permission that
  changes nothing, and for the private-address case it invites the operator to
  approve an SSRF probe.
- **An accepted elicitation is not proof a human saw it.** The MCP spec permits
  an agentic client to answer on the user's behalf, which is why an approval
  grants for the process only unless `BIONIC_HOST_GRANT_PERSIST=1`. Do not make
  persistence the default.
- **Elicitation must never be required.** Most clients do not implement it. Every
  failure path -- no capability, declined, cancelled, timed out, transport error
  -- returns the same structured refusal, and that refusal names the CLI command
  a user can run instead. `consent.request_host_grant` never raises.
- **A timeout is mandatory.** Without `anyio.fail_after` a client that displays
  the prompt and is then left alone hangs the tool call forever, which from the
  model's side is indistinguishable from a dead server.
- **The refusal text is a feature, not boilerplate.** It has to say that the
  whole host is refused (or a small model tries another path on it), name the
  documentation hosts that DO work (so a spec lookup can answer the question
  without bothering anyone), name the one command that grants the host, and warn
  that a URL the model invented may not exist at all. That last line came from a
  real failure: a model produced a plausible-looking URL from memory, and the
  grant it asked for would only have yielded a 404.
- **Fail closed, and say so.** A malformed grants file grants nothing, which
  otherwise looks exactly like a host that was never granted -- so the parse
  error is surfaced in the refusal's `recovery` and in `get_server_info`.
- **Do not reintroduce a cache in `_load_file_hosts`.** The obvious key --
  `(path, st_mtime_ns, st_size)` -- is unsound on Windows: NTFS mtime
  resolution here is about a millisecond, so two same-length writes inside one
  tick are indistinguishable and the stale host set wins. For this file that
  means a revoked host still granted. A correct key would have to read the file,
  which is the thing a cache was avoiding, and the read is free next to the DNS
  lookup and TLS handshake of the fetch it serves.
- **A refused redirect is a policy refusal, not a network failure.** It raises
  `RedirectNotAllowedError` (a ValueError subclass, so existing handlers still
  work) and is reported as `URL_NOT_ALLOWED` naming the redirect TARGET. Naming
  the requested URL instead would tell the operator to grant a host that is
  already granted. The commonest case is www/apex: those are different
  hostnames, and the error explains that rather than the code silently covering
  both.
- **A new `BIONIC_*` variable must be added to `FORWARDED_ENV_VARS`.** The MCP
  stdio client spawns servers with a fixed environment allowlist, so anything
  missing from that tuple silently does nothing under `run_server.bat --dev`.
  A test enforces this by scanning `src/` for `BIONIC_*` literals; it exists
  because that failure once survived a long time, every symptom looking like a
  plausible default.

- **`search` and `research` are two permissions, not one.**
  `web_search_enabled()` answers "may run web_search" (either group);
  `web_research_enabled()` answers "may fetch_web_page read any host"
  (`research` only). Never collapse them: conflating them is what made "find
  URLs but still gate reading" inexpressible, and it is why a model with a page
  reader and no search invents URLs from memory.
- **`web_search` is the one tool deliberately in two groups.** `research` is
  defined as everything `search` gives plus any-host fetching, so it appears in
  both and `get_tools()` unions to a set. A test asserts this is the ONLY
  overlap -- any other duplicate would make "turn that group off" fail to turn
  the tool off.
- **`search` stays out of `DEFAULT_TOOL_GROUPS`.** Not because it is as
  dangerous as `research`, but because adding it would silently change the
  posture of every existing install on upgrade.
- **`posture_warnings()` warns about broken combinations, never the default.**
  `search` without `docs` yields URLs nothing can open, so it is reported.
  `docs` without `search` is the DEFAULT and a legitimate choice, so it must
  stay silent -- a warning on the default configuration teaches operators to
  ignore warnings, which costs more than the note is worth. That kind of note
  belongs in the documentation instead.

## Tool Groups

- **Names in `utils`, mapping in `main`, and a test guarding the seam.**
  `utils.VALID_TOOL_GROUPS` holds the group names; `main.TOOL_GROUPS` holds
  name -> tools. They are split because `utils` cannot import `main` (main
  imports utils) and `fetch_web_page` must ask whether research is on without
  knowing `main` exists. `test_tool_groups.py` asserts the two sets agree and
  that every tool is in exactly one group -- a tool in no group can never be
  registered, and one in two is ambiguous.
- **`core` is always registered, and `get_server_info` must stay in it.** It is
  the tool that reports which groups are active, so removing it makes "why can
  I not see compile_shader" unanswerable in exactly the configuration where it
  is most likely to be asked. A test pins it.
- **`core` must stay read-only.** No tool in it may write, execute or reach the
  network. That is what makes `BIONIC_TOOLS=core` a posture safe to hand to
  something untrusted, and it would erode silently as tools get added to the
  group for convenience. A test lists the forbidden names explicitly.
- **Group boundaries follow trust, not topic.** `edit` and `build` are separate
  so "build and review this, but do not touch my files" is expressible. `docs`
  and `net` are separate because `docs` only reads into context while `net`
  writes bytes to disk -- the same distinction as `ALLOWED_DOC_HOSTS` versus
  `ALLOWED_NETWORK_HOSTS`.
- **`get_tools()` must stay pure.** It takes the resolved groups; `main()` reads
  the environment. pre-commit runs the suite, so an env-reading `get_tools`
  would stop anyone actually using `BIONIC_TOOLS` from committing. The same
  reasoning already applied to `web_enabled`, which survives only as a
  deprecated compatibility shim.
- **Order is derived, never re-declared.** `get_tools` filters `ALL_TOOLS`
  rather than concatenating group tuples, so the workflow ordering in `TOOLS`
  stays the single source of presentation order. That order affects which tool
  a small model reaches for, so it is not cosmetic.
- **A bad group name degrades, it does not abort.** `parse_tool_groups` returns
  `(default set, error message)`. Raising would kill startup, which an MCP
  client renders as an opaque connection failure. Log to stderr, register the
  default set, and surface the message in `get_server_info` so the model can
  explain it. What is unacceptable is a typo silently yielding a different tool
  set, and reporting prevents that without a dead server.
- **`BIONIC_WEB_RESEARCH` is an alias that adds the `research` group.** Keep it.
  It is what makes every pre-groups config, and the launcher's `--web` flag,
  continue to work, and it avoids the half-state where the network posture
  widens but `web_search` is missing.
- **Accepted coupling, stated rather than implied**: because `research` is an
  ordinary group, listing it in `BIONIC_TOOLS` widens the network posture on
  its own. A context-economy setting therefore also changes a security setting.
  That was an explicit operator choice for one variable instead of two; every
  place `BIONIC_TOOLS` is documented must say so.

- **The MCP Inspector does not inherit the environment.** The SDK's
  `StdioClientTransport` spawns servers with `getDefaultEnvironment()`, a fixed
  allowlist (`PATH`, `TEMP`, `APPDATA`, ... on Windows) so a client cannot leak
  its secrets into every server it launches. Anything this server reads must
  therefore be declared in the generated Inspector config
  (`inspector_config.FORWARDED_ENV_VARS`), or `--dev` silently runs with
  defaults and nothing errors. **When you add a new environment variable, add
  it there too** -- a test pins the list against the constants in `utils`, so
  forgetting fails loudly rather than shipping.

## Profiles

- **Profiles live one layer above groups, never inside them.** `profiles.py`
  needs `VALID_TOOL_GROUPS` from `utils`, so `utils` must not import it. That
  rules out teaching `active_tool_groups()` about profiles. Instead
  `main.apply_selected_profile()` reads `BIONIC_PROFILE`, resolves it FIRST and
  populates `BIONIC_TOOLS` from it; every path downstream then works unchanged.
  Do not "simplify" this by moving profile lookup into utils -- it is a cycle.
- **`BIONIC_PROFILE` and `BIONIC_PROFILES_FILE` must stay in
  `inspector_config.FORWARDED_ENV_VARS`.** Otherwise `--dev --profile` silently
  does nothing, which is the exact invisible failure `BIONIC_TOOLS` had before
  that module existed. Any new environment variable needs the same treatment.
- **Validate `tools` with `utils.parse_tool_groups()`.** That is the entire
  advantage of a profile over the raw variable: `"core,cpp"` is caught at load
  with the six real group names listed, where the bare variable only warns at
  startup and silently registers the default set. Never hand-roll the parsing.
- **`enable: false` means parked, and "disabled" is not "not found".** They need
  different fixes -- a flag to flip versus a name to correct -- so
  `resolve_profile` reports them distinctly, and only ever offers ENABLED
  profiles as alternatives.
- **Precedence is "more local wins", and overrides are reported.** An explicit
  environment variable beats the file, and `apply_profile_to_environment`
  returns what it skipped so `main()` can log it. A silently ignored profile is
  exactly the confusion profiles exist to remove.
- **Emit absolute command paths.** A bare `"windows-agent-mcp"` looks right and
  fails in a real client: the console script is in `.venv/Scripts`, not on a
  global PATH.
- **A bad profiles file must not kill the server.** Missing, malformed, unknown
  name, disabled name -- all degrade to the default tool set with the reason in
  the log and in `get_server_info`. The CLI is the opposite: it is interactive,
  so it exits non-zero.
- **Nothing about profiles is committed.** `mcp-profiles.json` is gitignored, so
  `scaffold()` is the only documentation of the format that a fresh checkout
  has. Every scaffolded profile must carry a `description`, and a test enforces
  it.
- **No test may read the real `mcp-profiles.json` from the repository.** It is
  machine-specific and may not exist, so a test depending on it would pass or
  fail based on whether someone had run `--init`. Pass an explicit path or use
  `tmp_path`.

## Context Economy

- **Tool definitions are re-serialized into the prompt every turn.** They are a
  permanent tax on the context window, not a one-off cost. Measured here: 18
  tools cost ~6,500 tokens before the description trim and ~4,000 after, which
  is 40% of an 8K window before the model reads any code.
- **`main.tool_description()` advertises only the leading prose** of each
  docstring, dropping `Args:` / `Returns:` / `Example:` because the JSON schema
  states the same things more precisely. Keep every line that steers tool
  CHOICE -- those exist because a 7B-9B model picks the wrong tool without
  them, and cutting them trades context for exactly the errors the context was
  preventing. Tests pin the important ones.
- **Never trim by mutating `fn.__doc__`.** Use `add_tool(fn, description=...)`.
  Mutation leaks across the process, so any later reader -- the test suite
  included -- sees a truncated docstring depending on whether `main()` ran
  first. A test asserts the function objects are untouched.
- **A new tool must repay its permanent cost with the context it saves.**
  `search_files` earns its ~570 tokens the first time it replaces twenty
  `read_file` calls. Prefer folding capability into an existing tool over
  adding one: `get_gpu_info` answers adapters, Vulkan, toolchain and SDK paths
  in a single call rather than as four tools.

## Writing Files

- **Writes are confined, reads are not.** `write_file`, `edit_file` and
  `compile_shader`'s output all go through `utils.resolve_write_path()`, which
  permits only the download root and `BIONIC_PROJECT_ROOTS`. `read_file` is
  deliberately unconfined and stays that way: a bad read costs context, a bad
  write costs work. Do not "fix" the asymmetry in either direction.
- **Resolve before checking containment.** `resolve_write_path` calls
  `Path.resolve()` first, so `..` and a symlinked parent are followed BEFORE
  the root test rather than at `open()` time. Reordering these reintroduces a
  traversal escape.
- **One consent switch, not two.** Write access reuses
  `BIONIC_PROJECT_ROOTS` rather than adding its own variable. Granting the
  right to run cmake in a tree and to edit files in it is the same trust
  decision, and a second switch would only produce a state where the model can
  build but not fix what it broke.
- **Write atomically.** Content goes to a temporary file in the same directory
  (`os.replace` is only atomic within one filesystem) and is renamed into
  place. The cleanup handler catches `BaseException`, not `Exception`, so a
  Ctrl+C does not leave a `.tmp` beside a source file.
- **Never let Python translate newlines.** Write bytes, not text mode: on
  Windows text mode turns every `\n` into `\r\n` silently. `edit_file` detects
  the file's dominant ending, matches in LF space so a caller's `\n` string
  finds a CRLF file, and restores the original ending. Without that
  normalisation the most common possible failure is an edit that cannot match
  for reasons invisible in the output -- and the model then rewrites the whole
  file to work around it.
- **Refuse ambiguity rather than guessing.** A non-unique `old_string` is an
  error naming the count, not a first-match replacement. A no-op edit
  (`old_string == new_string`) is also an error: a model looping on one never
  makes progress.
- **`write_file` refuses to clobber** unless `overwrite=True`, and its error
  names `edit_file`. Rewriting a whole file to change three lines is how a
  small model loses the rest of it.

## Searching

- **Prune before descending.** `filescan.iter_files` mutates `os.walk`'s
  directory list in place to skip `SKIPPED_DIRECTORY_NAMES`. On a C++ game
  tree the build output dwarfs the source, so filtering afterwards would still
  pay to enumerate every object file.
- **Report truncation, never swallow it.** A search that silently stops early
  tells the model "no matches" when the truth is "stopped looking", and the
  model then trusts a wrong answer. Same for skipped binary and unreadable
  files: count them and say so.
- **One unreadable file must not abort a search.** A `.pdb` locked mid-build,
  a path over `MAX_PATH`, a device node -- catch, count, continue.
- **Decode leniently.** `errors="replace"`, not strict: one latin-1 comment in
  an otherwise UTF-8 codebase is common, and refusing the file over one byte
  hides real matches.
- **Globs are `fnmatch`, not `pathlib.match`.** `*` spans directory
  separators, so `src/*.cpp` also matches `src/renderer/vk/device.cpp`. That is
  documented as intended behaviour; do not "fix" it into anchored matching.

## Build and Shader Tools

- **Parse output, do not dump it.** `run_powershell` returns up to 64 KB of raw
  log. `build_project` exists because that log IS the context window for a 7B
  model: a C++ project emits hundreds of warnings, one template error runs
  fifty lines, and MSVC repeats a bad header's error once per translation unit.
  Identical diagnostics collapse to one entry with a count.
- **Never report "no errors" for a failed build.** If `exit_code != 0` and
  nothing parsed, `format_report` shows the tail of the raw output and says the
  diagnostics were unrecognised. A clean-looking report on a failed build is a
  lie the model acts on.
- **Show the tail, not the head.** Builds announce failure at the end.
- **`build_project` reuses `validate_powershell_command()` wholesale** rather
  than reimplementing a weaker check, then narrows to `BUILD_COMMANDS`. It
  executes argv with **no shell**, so a PowerShell cmdlet cannot run -- which
  is why the narrower set exists: "that is not a build command" beats a
  confusing exec failure.
- **Diagnostic patterns are order-sensitive.** `_MSVC_NO_LINE_PATTERN` and
  `_BARE_PATTERN` are permissive enough to swallow lines the earlier patterns
  parse properly, so they must stay last. The file group tolerates a leading
  drive letter, or `C:\x\main.obj` splits at the drive colon.
- **CMake message blocks contain blank lines.** A `find_package` failure
  separates its prose from the candidate filenames with an empty line, so only
  a non-blank line at column zero ends the block. Treating a blank line as the
  terminator drops the actionable half of the message.
- **Notes are capped, not dropped.** clang's "candidate function not viable"
  notes are the most useful thing in an overload error, but a failed template
  instantiation attaches fifty.
- **Shader compilers are found via the reconstructed PATH.** Use
  `shutil.which(name, path=env["PATH"])` with `get_windows_development_environment()`,
  not bare `which`: a Vulkan SDK installed after the server started is
  otherwise invisible.

## Documentation

- **A new top-level document must be added to `package.py`'s
  `INCLUDED_FILES`, or it will not travel.** The allowlist is fail-safe by
  design, which is right for files holding local paths and wrong for
  documentation. A guard test asserts every top-level `*.md` is either packaged
  or in the explicit `NOT_PACKAGED` set, so forgetting fails loudly rather than
  shipping a copy that cannot explain itself.
- **Four documents, four audiences. Keep them apart.**
  `docs/HOW_TO_USE.md` is the task-oriented guide: steps a person follows
  in order. `README.md` is the reference: per-tool parameters, the security
  model, the configuration surface. `docs/CONTRIBUTING.md` is for someone
  changing the code: style, adding a tool, writing hermetic tests.
  `CLAUDE.md` (this file) is the design rules and the reasoning behind the
  non-obvious ones. Do not duplicate a section across two of them -- link
  instead. The README once carried its own copy of most of CONTRIBUTING, and
  the copy was the one that had gone stale.
- **Only `README.md` and `CLAUDE.md` belong at the repository root.**
  Everything else lives in `docs/`. CLAUDE.md is the exception on purpose:
  an AI coding tool discovers it at the project root, so moving it into
  `docs/` would leave the design rules unread -- which is the whole reason
  the file was renamed from AI_INSTRUCTIONS.md.
- **Never hand-maintain a list that the filesystem already knows.** A written
  project tree in README.md was missing two modules within a week of their
  being added. The same applies to per-directory file inventories and tool
  counts: describe the shape, name the authoritative source (`ls`, or
  `get_server_info`), and let the reader look.
- **Never put a machine-specific absolute path in shipped documentation.**
  `README.md`, `docs/HOW_TO_USE.md`, `.env.example` and anything
  `profiles --init`
  writes are read on other people's machines. Use relative paths
  (`.venv\Scripts\python.exe`) or obvious placeholders
  (`C:\path\to\your\project`). Prefer `C:` for placeholders -- a drive letter
  that exists only on the authoring machine reads like a real path rather than
  something to replace.
- **Do not restate tool counts per section.** "17 tools" is correct today and
  wrong the moment a group changes. State it once and name `get_server_info` as
  the authority.
- **Every command in a guide must have been run as written.** Commands
  documented but never executed are how a guide rots; walk the sections and
  check each one, including the failure paths a reader is likely to hit (such
  as `profiles --init` when the file already exists).

## Operator Scripts

- **`bootstrap.py` must import only the standard library.** It runs before
  anything is installed, so one third-party import makes it unable to do the
  one job it exists for, and the failure appears as a confusing ImportError on
  a fresh machine. A test AST-parses the file and checks every import against
  `sys.stdlib_module_names`. The same rule is applied to `package.py`, which
  must work inside a copy that has not been bootstrapped yet.
- **Never add a `setup.py`.** The name is reserved: setuptools' PEP 517 backend
  picks one up if it exists, so an installer script under that name risks
  breaking `pip install .`. `pyproject.toml` is the single build definition.
  If a packaging shim is ever genuinely needed, it must be the real
  `from setuptools import setup; setup()` form and nothing else.
- **Both scripts split a pure planner from a thin runner**, the same shape as
  `inspector_config.build_config` and `profiles.parse_profiles`.
  `bootstrap.plan_commands()` returns the commands it *would* run, so the
  decision logic is testable without performing an install;
  `package.collect_files()` returns the selection without writing anything.
- **Read the Python floor from `requires-python`, never hardcode it**, so the
  script and the package metadata cannot drift. Keep the `tomllib` /
  regex fallback: `requires-python` is `>=3.10` and `tomllib` arrives in 3.11,
  so the script must be able to reject an interpreter too old to parse the file
  that says it is too old.
- **`package.py` selects by ALLOWLIST.** Anything not in `INCLUDED_DIRS` or
  `INCLUDED_FILES` does not ship. That bias is deliberate: for copying to
  another machine the bad outcome is shipping an absolute path or a secret, so
  a new machine-specific file is excluded by default. **The consequence is that
  a new file you want shipped must be added to the list** -- and because an
  allowlist fails by omission, `--list` and the extract-and-test check below
  are how that is caught.
- **Do not reuse `filescan.SKIPPED_DIRECTORY_NAMES` in `package.py`.** It is
  search-oriented and also skips `bin`, `obj`, `dist`, `debug`, `release` and
  `x64` -- correct for a grep, wrong for a source archive, where it would one
  day silently drop a legitimate `src/.../bin/`. `PRUNED_DIRS` is narrow on
  purpose.
- **Verify an archive by extracting it and running the suite from inside**, not
  by inspecting the file list. `pyproject.toml` sets `pythonpath = ["src"]`, so
  pytest can only resolve the package if that config file itself reached the
  archive -- a missing file fails the run instead of passing quietly. That
  check is what caught a test which assumed a bootstrapped `.venv` and so
  failed in a fresh copy.
- **A test must never fail on a fresh, un-bootstrapped checkout.** Anything
  that needs `.venv` has to `skipif` on its absence: an unzipped copy is the
  first thing a new user runs the suite in, and a red suite there reads as a
  broken project.

## Tool Design Patterns

1. **Validation First**: Validate inputs before filesystem/network operations
2. **Explicit Error Types**: Use structured errors with recovery instructions
3. **Truncate Large Outputs**: Cap stdout/stderr at 64KB to avoid context flooding
4. **Path Safety**: Never allow arbitrary filesystem destinations
5. **Atomic Operations**: Use temporary files for downloads, then rename

## Testing

- Tests live in `tests/` directory
- Run with pytest using the `.venv` environment (`uv sync --extra dev` first)
- The suite must not touch the network: stub `socket.getaddrinfo`, and pass a
  `FakeOpener` (search backends take an injected opener; tools have their
  module attribute patched)
- Use `tmp_path`/`monkeypatch`, never write into the repository
- Record a known bug as an `xfail(strict=True)` test asserting the DESIRED
  behaviour, so the suite fails once it is fixed and the marker is removed

## Configuration

- `pyproject.toml`: dependencies, packaging, pytest/ruff/pyright settings
- Environment: `BIONIC_DOWNLOAD_ROOT` (download directory),
  `BIONIC_PROJECT_ROOTS` (extra run_powershell roots), `BIONIC_WEB_RESEARCH`
  (registers the web tools and widens their host policy),
  `BIONIC_SEARCH_BACKEND` (search provider). Nothing else is read from the
  environment; all limits are constants in `utils.py`.
- Every variable the server reads must be listed in `_SERVER_ENV_VARS` in
  `tests/conftest.py`, which scrubs them before each test. Otherwise the suite
  behaves differently depending on the developer's shell and pre-commit
  blocks them.
