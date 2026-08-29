# How to use this server

A step-by-step guide: start at the top, finish with a working server.

For reference material — every tool's parameters, the security model, the full
environment-variable table — see [README.md](../README.md).

---

## 1. What this is, and what you need

This is an **MCP server**: a small program that gives an AI coding assistant a
fixed set of tools for working on a Windows machine. It can read and search
files, write and edit them, run builds, compile shaders, and look things up in
reference documentation — all through a restricted interface rather than an open
shell.

It is built for **small local models** (roughly 7B–9B), so every tool truncates
its output, explains its errors, and tells the model what to do next.

You need:

| | |
|---|---|
| **Windows** | The server runs PowerShell and reads Windows-specific things like GPU adapters |
| **Python 3.10 or newer** | Check with `python --version` |
| **Node.js** *(optional)* | Only for the browser-based Inspector UI (`--dev`) |
| **An MCP client** | Whatever runs your AI model and speaks MCP |

Everything else is installed for you in step 3.

---

## 2. Copy it to another machine

Run this on the machine that already has the project:

```bash
python package.py
```

You get `dist/windows-agent-mcp-<version>.zip`. The script reopens and verifies
the archive after writing it, so a truncated or corrupt zip is reported rather
than handed to you.

Check what it will include before you send anything:

```bash
python package.py --list
```

Or copy into a folder instead of zipping — useful for a network share or USB
stick:

```bash
python package.py --dest D:\transfer
```

### What is left out, and why

Three things are **deliberately excluded** and recreated on the target machine:

| Excluded | Why |
|---|---|
| `.venv\` | The virtual environment has absolute paths baked into its scripts, so it simply does not work in a different folder or on a different machine. Step 3 recreates it |
| `mcp-profiles.json` | It contains paths to *your* projects, which mean nothing on another machine. Step 5 recreates it |
| `mcp-allowed-hosts.json` | The web hosts you allowed on *this* machine. A trust decision should be made again, not inherited. Step 6 recreates it |

Also skipped: editor settings, previous archives, and all caches
(`__pycache__`, `.pytest_cache`, `.ruff_cache`, `.coverage`, compiled shaders).

> **Adding a file of your own?** `package.py` ships only what is named in its
> `INCLUDED_DIRS` and `INCLUDED_FILES` lists. Anything else is left out on
> purpose — that way a file holding local paths or credentials never travels by
> accident. The trade-off is that **a new file you *do* want shipped must be
> added to `INCLUDED_FILES`**. Use `--list` to confirm before sending.

Then, on the target machine: unzip it, open a terminal in the unzipped folder,
and continue with step 3.

---

## 3. Install

```bash
python bootstrap.py
```

That is the whole install. It creates a virtual environment in `.venv`,
installs the server and its dependencies, and then checks its own work by
importing the package and counting the tools it registers:

```
Windows Agent MCP -- bootstrap

  python 3.13.7......................... ok (>= 3.10)
  uv.................................... found at C:\Users\you\.local\bin\uv.EXE
  dev dependencies...................... included

  ... install output ...

  verify................................ 17 tools registered

  Done. Next steps:
    run_server.bat --dev
```

If [uv](https://github.com/astral-sh/uv) is installed it uses that (it is much
faster and installs exact locked versions). Otherwise it falls back to Python's
built-in `venv` plus `pip`. Either way the result is the same, so you do not
need to install anything first.

Useful variations:

```bash
python bootstrap.py --check     # report what is present; install nothing
python bootstrap.py --no-dev    # skip test/lint tools; runtime only
python bootstrap.py --force     # delete an existing .venv and start over
```

Re-running it is safe.

<details>
<summary>Or install by hand</summary>

```bash
# With uv. Note that plain `uv sync` does NOT install the test and lint
# tools -- the "dev" extra is separate.
uv sync --extra dev

# Or with pip
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

</details>

> **There is no `setup.py`, on purpose.** In Python, that filename is reserved
> for build tooling — putting an installer script there can break
> `pip install .`. `pyproject.toml` describes the package, and
> `bootstrap.py` installs it.

---

## 4. Point it at your project

**Do this before you try to write or build anything.**

By default the server can only write inside a private download sandbox. Point
it at your real project with `BIONIC_PROJECT_ROOTS`:

```bash
set BIONIC_PROJECT_ROOTS=C:\path\to\your\project
```

Several projects, separated by semicolons:

```bash
set BIONIC_PROJECT_ROOTS=C:\path\to\game;D:\path\to\engine
```

If you skip this, everything still starts — but `write_file`, `edit_file`,
`build_project` and `compile_shader` will refuse with
`WRITE_PATH_NOT_ALLOWED`, which looks like a bug and is actually this setting.
`run_server.bat` prints a reminder at startup when it is unset.

> **What you are granting.** These directories become writable *and* runnable:
> the server may edit files there and start build programs there, and a build
> script can do anything a program can. That is what a coding assistant needs
> in order to be useful — but point it at your project folder, not at a whole
> drive.

Note that **nothing reads a `.env` file automatically.** Set these as real
environment variables in whatever launches the server — usually the `env` block
of your MCP client's configuration (step 8). `.env.example` documents every
available variable.

---

## 5. Choose which tools load

Each tool costs space in the model's limited context, every single turn. Loading
only what a session needs leaves more room for the actual work — which matters a
lot for small models.

Tools come in six **groups**:

| Group | What it gives the model |
|---|---|
| `core` | Read files, list directories, search. **Always loaded** |
| `edit` | Create and modify files |
| `build` | Run builds, compile shaders, run allowlisted commands |
| `docs` | Read reference documentation (Vulkan spec, Microsoft Learn, cppreference) |
| `net` | Download dependencies from approved hosts |
| `search` | Web search — lets it *find* pages (it still needs permission to read them) |
| `research` | Web search **and** reading any site, with no per-site approval |

A **profile** is a named combination. Create the profiles file:

```bash
.venv\Scripts\python.exe -m windows_agent_mcp.profiles --init
```

If it already exists, that refuses rather than overwriting your edits — add
`--force` if you really want to start over.

See what it defines:

```bash
.venv\Scripts\python.exe -m windows_agent_mcp.profiles --list
```

```
profile     state       tools  groups
cpp         enabled        14  build,core,docs,edit
explore     enabled         7  core
full        enabled        17  build,core,docs,edit,net
research    DISABLED        9  core,docs,research
review      enabled        12  build,core,docs
```

Edit `mcp-profiles.json` to suit you: change which groups a profile loads, set
`"enable": false` to park one without deleting it, or add a per-profile
`BIONIC_PROJECT_ROOTS` so a profile carries its own project path.

`research` ships disabled because it lets the server fetch *any* website. Read
the *Web research* section of [README.md](../README.md#web-research) before
enabling it.

Leave profiles alone entirely if you like — with no profile selected you get
every group except `research`.

See [README.md](../README.md#tool-groups) for what each group costs and the full
tool listing.


---

## 6. Let it read the site you need

Your model will eventually try to read a web page and be refused:

```
"type": "URL_NOT_ALLOWED",
"message": "Domain 'www.example.com' is not allowed."
```

That is not a bug. By default the server reads reference documentation only
(the Vulkan spec, Microsoft Learn, cppreference, CMake and a few more), because
letting an AI fetch arbitrary pages is how a malicious page gets to put
instructions in front of it.

**To allow one site, run one command.** No restart — it applies to the model's
very next call:

```bash
.venv\Scripts\python.exe -m windows_agent_mcp.hostgrants --add www.example.com --note "why I needed it"
```

To see and undo what you have allowed:

```bash
.venv\Scripts\python.exe -m windows_agent_mcp.hostgrants --list
.venv\Scripts\python.exe -m windows_agent_mcp.hostgrants --remove www.example.com
```

Three things worth knowing before you type yes:

- **You are allowing a whole site, not one page.** `www.example.com` means any
  page on it, from now on.
- **Reading only.** A granted site cannot download files to your disk.
- **A hostname, not a URL.** `www.example.com`, never
  `https://www.example.com/some/page` — the command will tell you the hostname
  to use if you paste a URL by mistake.

Some AI clients can ask you directly instead: a dialog appears saying *"The
assistant wants to read a web page from www.example.com"*, and answering yes
lets the call carry on. This needs a client that supports MCP *elicitation*; if
yours does not, you will simply get the message above, which names the command
to run. Approvals given that way last until you restart the server.

> **A refusal that surprises you is worth reading twice.** If the model asks for
> a site you never mentioned, the request may have come from a page it already
> read rather than from you. Say no.

### When you want the whole web instead

If you are doing open-ended research and approving sites one at a time is not
practical, there is a switch for that — but read the *Web research* section of
[README.md](../README.md#web-research) first, because it also turns on web search
and lets the server fetch any public site.

```bash
run_server.bat --web
```

There is a middle option too: `run_server.bat --allow-host a.com,b.com` allows
named sites for one run without touching the grants file.

### If you want it to search, but not to read whatever it likes

This is usually the right setting. Add the `search` group:

```bash
run_server.bat --tools core,docs,search
```

Now the model can *find* pages, but reading one still needs the site to be
allowed — so it finds a real link, gets refused, and tells you which site it
needs. You allow that one, and it reads the page.

Without a search tool, asking the model to "search for X" makes it guess URLs
from memory, and guessed URLs are usually wrong. Worth knowing, because it looks
like the model making things up rather than a missing tool.

Use `search` together with `docs`. On its own it finds pages that nothing can
open, and the server warns you at startup if you do that.

---

## 7. Run it

```bash
run_server.bat
```

This is the normal way to start it: the server talks the MCP protocol over its
input and output, which is what an AI client expects. **There is no prompt and
no output — it looks frozen. That is correct.** It is waiting for its client.

To try the tools by hand, use the Inspector UI instead:

```bash
run_server.bat --dev
```

That opens a browser page where you can see every registered tool, fill in its
arguments and run it. It needs Node.js. The page shows a list of servers —
click the one named `windows-agent-mcp` to connect.

Combine with the options from steps 4, 5 and 6:

```bash
run_server.bat --dev --profile cpp        # load a named profile
run_server.bat --tools core,docs          # or name groups directly
run_server.bat --dev --web                # enable web search
run_server.bat -h                         # list every option
```

`--tools` wins over `--profile` if you pass both.

---

## 8. Connect your AI client

An MCP client needs to know how to start the server. Generate that
configuration rather than writing it by hand:

```bash
.venv\Scripts\python.exe -m windows_agent_mcp.profiles --emit client
```

It prints one entry per enabled profile, with full absolute paths that will
actually work:

```json
{
  "mcpServers": {
    "cpp": {
      "command": "C:/path/to/mcp-server/.venv/Scripts/windows-agent-mcp.exe",
      "env": {
        "BIONIC_TOOLS": "edit,build,docs",
        "BIONIC_PROJECT_ROOTS": "C:/path/to/your/project"
      }
    },
    "explore": {
      "command": "C:/path/to/mcp-server/.venv/Scripts/windows-agent-mcp.exe",
      "env": {
        "BIONIC_TOOLS": "core"
      }
    }
  }
}
```

Paste it into your client's MCP configuration file. You now have one togglable
entry per profile, so you can switch between "full access" and "read-only" from
the client's own UI.

Two things to know:

- **Use the generated absolute path.** A bare `windows-agent-mcp` will not work
  — the program lives inside `.venv` and is not on the system PATH.
- **Set variables in the `env` block, not your shell.** Your client starts the
  server itself, and it does not pass your shell's environment through.

Restart the client after editing its configuration.

---

## 9. Check it works, and what to do when it does not

The fastest check: ask the model to call **`get_server_info`**. It returns the
active tool groups, the tool count, which directories are writable, and any
configuration error it found. That one call answers most questions below.

```bash
python bootstrap.py --check      # is the install healthy?
.venv\Scripts\python.exe -m pytest -q     # run the full test suite
```

### Common problems

| What you see | What it means | What to do |
|---|---|---|
| `No virtual environment found` | Not installed yet | Run `python bootstrap.py` |
| Server starts, then nothing happens | Correct — it is waiting for a client | Use `run_server.bat --dev` to click tools by hand |
| A tool is missing from the list | Its group is not loaded | Call `get_server_info` and read `active_tool_groups` |
| `WRITE_PATH_NOT_ALLOWED` | `BIONIC_PROJECT_ROOTS` is not set, or the path is outside it | See step 4, then restart the server |
| `URL_NOT_ALLOWED` | The site is not on the readable list | See step 6. Allow the one host, do not enable all of them |
| `URL_NOT_ALLOWED` mentioning a *redirect* | The site redirected to another hostname, often `www.` vs no `www.` | Allow the host named in the message too |
| It invents web links that do not exist | It has no search tool, so it is recalling URLs from training | Add the `search` group — see step 6 |
| `PROTECTED_PATH` | The model tried to write the server's own config | Working as intended. Only you may edit those files |
| `npx was not found` | Your terminal's PATH predates the Node.js install | Close and reopen the terminal |
| Your profile seems ignored | An explicit `BIONIC_TOOLS` overrides a profile | Call `get_server_info` and read `profile_error` |
| A profile name is rejected | Wrong name, or `"enable": false` | `profiles --list` shows both |
| Tools missing when launched from a client | The variables are not in the client's `env` block | See step 8 |
| `COMMAND_NOT_ALLOWED` from `run_powershell` | Only one allowlisted program per call — no `;`, `|` or `>` | Split it into separate calls |

### Still stuck?

Every tool returns errors as structured JSON with a `recovery` field listing
what to try next — read that first; it is written for exactly this situation.

The server logs to **stderr**, never to stdout (stdout carries the protocol).
Under `--dev`, the Inspector shows that log in its console pane.
