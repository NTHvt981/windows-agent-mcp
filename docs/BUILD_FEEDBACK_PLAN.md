# build_project feedback — implementation plan

Source: `windows-agent-mcp-feedback.md` (an AI coding agent's report after using
this server on a large C++/MSVC project).

This document is the plan only. No behaviour has been changed yet.

## Decisions taken

Three choices in the feedback had more than one reasonable answer. Locked:

1. **Timeout (Finding 1):** kill the whole process tree on timeout and return a
   structured result carrying the PID, elapsed time and captured output. No
   orphaned `msbuild`/`cl`/`link`, so an immediate retry is safe.
2. **Concurrency (Finding 3):** refuse a second build for the same resolved
   working directory with a clear error. No queue, no automatic lock cleanup.
3. **Deliverable:** this plan. Implementation follows separately.

The feedback's own priority ordering is kept: Finding 1 is high, Findings 2 and
3 medium, Finding 4 low.

## Why the current code does what the feedback observed

| Observation | Cause in the code |
|---|---|
| Timeout leaves a build running | `process.run_process` uses `subprocess.run` (`src/windows_agent_mcp/process.py:163`). On `TimeoutExpired`, `subprocess.run` calls `process.kill()`, which on Windows terminates only the **direct** child. `msbuild`'s `cl.exe`/`link.exe` grandchildren keep running and keep writing `obj/`/libs. `run_process` never captures a PID, so it cannot kill the tree. |
| Timeout returns no diagnostics | `process.py:174-186` returns `timed_out=True` and *does* keep pre-timeout output, but `_truncate` keeps the **head** (`process.py:128-134`), so for a long build the useful tail is discarded. `build_project` then quotes `result.combined[-1500:]` of an already head-truncated string (`tools/build_project.py:164-179`). |
| Success output has no triage info | `format_report` (`diagnostics.py:509`) is never given elapsed time, and the success branch (`diagnostics.py:574-580`) prints only `BUILD SUCCEEDED`. |
| No lock guard | `build_project` has no notion of an in-flight build. Nothing records one. |
| `/v:minimal` passed by hand | The tool echoes the command through unchanged; no default verbosity is applied. |

## Scope by slice

Each slice ends at a compiling, testable state. They are independent enough to
land separately, but slices 1 and 2 share the `format_report`/`ProcessResult`
edit surface, so implementing them together avoids two touches of the same
signatures.

| # | Finding | Files | Risk |
|---|---|---|---|
| 1 | Timeout: kill tree, structured result, keep tail | `process.py`, `error.py`, `tools/build_project.py` | medium — subprocess rewrite |
| 2 | Success triage: elapsed, counts, histogram, rebuild | `diagnostics.py`, `tools/build_project.py`, `tools/compile_shader.py` (call site only) | low |
| 3 | Concurrency guard | new `buildlock.py`, `tools/build_project.py` | low |
| 4a | Default MSBuild verbosity | `tools/build_project.py` | low |
| 4b | Full log retention (optional) | `process.py`, `tools/build_project.py` | medium — defer unless wanted |
| — | Docs + changelog | `README.md`, `docs/CHANGELOG.md` | low |
| — | Tests | `tests/` (recover from git, see below) | medium |

---

## Slice 1 — Timeout: kill the tree, return a structured result

### 1.1 `process.py`: capture PID + elapsed, kill the tree, keep the tail

Add constants beside `MAX_PROCESS_OUTPUT_CHARS` (`process.py:27`):

```python
# Budget for the tail kept when a stream exceeds MAX_PROCESS_OUTPUT_CHARS.
# A build's last lines are the part that says why it failed or stopped, and
# for a timed-out build the tail is the whole point -- so the cap keeps both
# ends and drops the middle rather than keeping only the head.
MAX_PROCESS_OUTPUT_TAIL_CHARS: int = 16 * 1024

# Bound on the taskkill call itself, so a wedged kill cannot hang the tool.
PROCESS_KILL_TIMEOUT_SECONDS: int = 10
```

Extend `ProcessResult` (`process.py:108`) with two defaulted fields, so existing
construction sites and `NamedTuple` unpacking keep working:

```python
    pid: int | None = None
    elapsed_ms: int = 0
```

Change `_truncate` (`process.py:128`) to keep head **and** tail:

```python
def _truncate(text: str, label: str) -> str:
    if len(text) <= MAX_PROCESS_OUTPUT_CHARS:
        return text

    omitted = len(text) - MAX_PROCESS_OUTPUT_CHARS - MAX_PROCESS_OUTPUT_TAIL_CHARS

    if omitted <= 0:
        # Only just over the limit: keep the tail, which is the actionable end.
        return f"...[{label} truncated]...\n" + text[-MAX_PROCESS_OUTPUT_CHARS:]

    return (
        text[:MAX_PROCESS_OUTPUT_CHARS]
        + f"\n...[{label} truncated, {omitted} chars omitted]...\n"
        + text[-MAX_PROCESS_OUTPUT_TAIL_CHARS:]
    )
```

Rewrite `run_process` (`process.py:137`) on `subprocess.Popen` so the PID exists
and the kill can be explicit. Mirror CPython's `subprocess.run` timeout handling
(kill, then one final `communicate()` to drain output):

```python
    started = time.monotonic()
    process = subprocess.Popen(
        argv, cwd=cwd, env=get_windows_development_environment(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )

    timed_out = False

    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()

    elapsed_ms = int((time.monotonic() - started) * 1000)
    # ... existing decode/truncate, plus pid=process.pid, elapsed_ms=elapsed_ms
```

New `_terminate_process_tree`:

```python
def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill a build and everything it spawned.

    On Windows process.kill() == TerminateProcess on the direct child only.
    msbuild's cl.exe and link.exe grandchildren survive that and keep writing
    obj/ and .lib files, which is what produced the LNK1163/LNK1104 lock
    errors a retry then misread as a source bug. taskkill /T walks the parent
    tree, so it must run BEFORE the direct child is killed.
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            timeout=PROCESS_KILL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        log.warning("taskkill failed for pid %s: %s", process.pid, exc)

    try:
        process.kill()
    except OSError:  # already gone
        pass
```

Notes:
- `taskkill` is a Windows system binary; this call is internal to the server and
  is **not** routed through `allowed_command` (it is the server acting on its own
  child, not a tool command).
- `run_powershell` (`tools/run_powershell.py:356`) and `compile_shader`
  (`tools/compile_shader.py`) use `run_process`/inline `subprocess.run`
  respectively. `compile_shader` inherits the fix. `run_powershell` is out of
  scope here; add a follow-up ticket to give it the same tree-kill, since a
  timed-out `msbuild` through `run_powershell` orphans identically.

### 1.2 `error.py`: allow structured details

Add an optional `details` mapping merged into the error object, so the timeout
can carry machine-readable fields rather than only prose:

```python
def mcp_error(..., recovery: list[str] | None = None,
              details: dict[str, Any] | None = None) -> str:
    ...
    if details:
        error.update(details)
```

Backward compatible: every existing call site omits it.

### 1.3 `tools/build_project.py`: structured timeout

Time the `run_process` call (or use `result.elapsed_ms`) and replace the timeout
branch (`tools/build_project.py:164`) with:

```python
    if result.timed_out:
        return mcp_error(
            "BUILD_TIMED_OUT",
            "build_project",
            f"'{command}' did not finish within {timeout_seconds} seconds. "
            f"The build process tree was terminated; it is safe to retry.\n"
            f"Output so far (tail):\n{result.combined[-_MAX_ERROR_EXCERPT:]}",
            details={
                "status": "timeout",
                "pid": result.pid,
                "elapsed_ms": result.elapsed_ms,
                "timeout_seconds": timeout_seconds,
                "killed": True,
            },
            recovery=[
                "The build process tree was terminated, so no orphaned "
                "msbuild/cl/link is holding obj/ or lib/ locks. A retry is safe.",
                "A cold build of a large project can legitimately exceed this; "
                "raise timeout_seconds (max 1800).",
                "Building a single target is much faster than a full build.",
            ],
        )
```

This satisfies the feedback's "status = timeout, PID, elapsed, all diagnostics
captured so far" in one structured envelope. `BUILD_TIMED_OUT` stays in the
README error list; the payload gains fields.

---

## Slice 2 — Success result triage

### 2.1 `diagnostics.py`: accept elapsed, count, rebuild kind, warning histogram

Extend `format_report` (`diagnostics.py:509`) with keyword-only, defaulted
parameters so the `compile_shader` call site (`tools/compile_shader.py:281`) is
unaffected:

```python
def format_report(
    parsed, *, header, exit_code, raw_output,
    subject: str = "BUILD",
    success_line: str | None = None,
    elapsed_ms: int | None = None,
    rebuild: str = "",
) -> str:
```

- Header becomes `f"{header}  (exit code {exit_code}, {elapsed_ms/1000:.1f}s)"`
  when `elapsed_ms is not None`.
- Before any failure verdict, if `rebuild` is non-empty, append
  `f"rebuild: {rebuild}"` to the header line or as its own line.
- Success branch (`diagnostics.py:574`) becomes, e.g.:

  ```python
  if exit_code == 0 and not parsed.errors:
      verdict = success_line or f"{subject} SUCCEEDED"
      lines.append(
          f"{verdict} ({parsed.total_errors} errors, "
          f"{parsed.total_warnings} warnings)"
      )
      if parsed.warnings:
          lines.append("Warnings above are not fatal.")
      return "\n".join(lines)
  ```

- **Warning histogram.** New pure helper, one line so it does not undo the
  context savings:

  ```python
  def _warning_histogram(warnings: tuple[Diagnostic, ...]) -> str:
      # "C4996 x7 (deps/raygui) | LNK4099 x2"
  ```

  Group by `code` (fall back to `message` when empty), sum `occurrences`, and
  append the most common file's top path component in parentheses when one
  exists. Emit only when there is at least one warning. This is the feedback's
  `C4996 x7 (deps/raygui)` request.

- **Pre-existing-warnings hint.** New pure helper applied only to warnings:

  ```python
  _VENDORED_PATH_PARTS = frozenset(
      {"deps", "external", "externals", "third_party", "thirdparty",
       "3rdparty", "vendor", "vendored", "_deps", "subprojects"}
  )

  _KNOWN_BENIGN_CODES = frozenset({"C4996", "LNK4099"})

  def _pre_existing(diagnostic: Diagnostic) -> bool:
      # C4996 (deprecation) and LNK4099 (missing PDB) are the classic
      # third-party noise in a C++ game; a vendored path is the other signal.
  ```

  Tag the rendered warning line with `  [likely pre-existing]`. Deliberately a
  *hint*, worded as such, because the server cannot know a project's vendored
  dirs for certain.

- **Rebuild kind.** New pure `parse_rebuild_kind(raw_output: str) -> str`
  returning `"up-to-date" | "incremental" | "full" | ""`. Markers only, no
  timing heuristics:
  - up-to-date: `ninja: no work to do`, `Everything is up to date`,
    `Build succeeded` accompanied by no compile/link lines (see below).
  - full: `Rebuild All`, `Performing full rebuild`, `"Cleaning"` followed by a
    compile, `-t:Rebuild`.
  - incremental: at least one compile/link line (`-> ...obj`, `Linking`, `[N/M]`)
    with neither marker above.
  - `""` (omit) whenever unsure. The plan's rule is **never guess**: a wrong
    "up-to-date" is exactly the kind of claim the model acts on.

### 2.2 `tools/build_project.py`: measure and pass through

Wrap the `run_process` call with `time.monotonic()` or reuse
`result.elapsed_ms` from Slice 1, and pass to `format_report`:

```python
    return format_report(
        parsed,
        header=f"BUILD: {command}",
        exit_code=result.exit_code,
        raw_output=result.combined,
        elapsed_ms=result.elapsed_ms,
        rebuild=parse_rebuild_kind(result.combined),
    )
```

`compile_shader` may pass `elapsed_ms=result.elapsed_ms` too, or keep the default
(no change required).

---

## Slice 3 — Concurrency guard

New module `src/windows_agent_mcp/buildlock.py`:

```python
"""One build at a time per working directory.

A second msbuild against the same obj/ and lib/ does not fail cleanly: it
produces LNK1163/LNK1104 lock contention that reads exactly like a source
error, and the model then edits correct code. So a build in flight is
recorded, and a second build for the same directory is refused rather than
queued -- queueing would make an MCP call block for minutes with no
feedback, which is its own failure.
"""

class BuildAlreadyRunning(Exception):
    def __init__(self, command: str, pid: int | None, elapsed_ms: int): ...

@contextmanager
def build_slot(working_directory: Path, command: str) -> Iterator[RunningBuild]:
    """Reserve the slot or raise BuildAlreadyRunning."""
```

Implementation details:
- Module-level `_lock = threading.Lock()` and `_running: dict[str, RunningBuild]`,
  keyed by `os.path.normcase(os.path.normpath(str(working_directory)))`.
- The key is the **resolved working directory**, not the solution path: parsing
  which solution a command names is tool-specific and brittle, and the failure
  mode (shared `obj/`+`libs`) is per-directory. Document that two unrelated
  solutions in one directory will false-positive -- the safe direction.
- `build_project` registers *after* `resolve_working_directory` succeeds and
  *before* `run_process`, wrapped in `try/finally` so every exit path
  (exception, timeout, success) releases the slot.
- On `BuildAlreadyRunning`, return:

  ```python
  mcp_error(
      "BUILD_ALREADY_RUNNING",
      "build_project",
      f"A build is already running in {resolved_directory}: "
      f"'{running.command}' (pid {running.pid}, "
      f"{running.elapsed_ms/1000:.0f}s elapsed).",
      details={...},
      recovery=[
          "DO NOT start a second build: concurrent msbuild on the same "
          "obj/ and lib/ causes link-lock errors that look like source bugs.",
          "Wait for the running build to finish, then call again.",
          "If it is wedged, ask the user to stop it.",
      ],
  )
  ```

- Known limitation to state in the README: only builds started by this server
  process are detected. A build the user started in their own terminal is not.

---

## Slice 4a — Default MSBuild verbosity

In `tools/build_project.py`, after `argv = tokenize_command(command)`
(`tools/build_project.py:93`) and the `BUILD_COMMANDS` check, add:

```python
    argv = _with_default_verbosity(argv)


def _with_default_verbosity(argv: list[str]) -> list[str]:
    """Add /v:minimal /nologo to msbuild when the caller did not ask.

    The feedback passed /v:minimal on every call by hand. It changes console
    noise only, not what builds, and the parser throws the banners away
    anyway -- so the savings are in the raw tail quoted on failure, which is
    easier to read without per-project banners. Only msbuild is touched:
    cmake/ninja have different flags and are already quiet enough.
    """
    if Path(argv[0]).name.lower() not in {"msbuild", "msbuild.exe"}:
        return argv

    lowered = [token.lower() for token in argv[1:]]
    has_verbosity = any(
        token.startswith(("/v:", "/verbosity:")) for token in lowered
    )
    has_nologo = "/nologo" in lowered

    result = list(argv)
    if not has_verbosity:
        result.append("/v:minimal")
    if not has_nologo:
        result.append("/nologo")
    return result
```

The echoed header must show the executed command, not the caller's, so the
report and any error name the flags actually used. Keep `command` for the error
message text only if it does not cause confusion; otherwise build the header
from `argv`.

---

## Slice 4b — Full log retention (optional, recommend deferring)

The feedback asks that a truncated summary still leave the full log retrievable.
Today `run_process` holds the head (and, after Slice 1, the tail) in memory and
writes nothing.

If wanted, the correct shape is to tee the child's output to a file rather than
pipe-only, then read the bounded window back for parsing:

- Add `log_path: Path | None = None` to `run_process`.
- Open the file and pass `stdout=handle, stderr=subprocess.STDOUT` to `Popen`,
  then read head/tail from the file for the `ProcessResult`.
- `build_project` chooses
  `<get_download_root()>/build-logs/<utcstamp>-<dirhash>.log` whenever output
  exceeds the cap or the build times out, and adds `full_log: <path>` to the
  report/error. `read_file` can then page it.
- This avoids pipe-buffer deadlock and keeps one source of truth for the raw
  text.

Defer unless requested: it is the lowest-value item, adds file lifecycle
questions (cleanup, collisions), and the head+tail capture already carries the
actionable content.

---

## Tests

The suite was removed in commit `c777c0c` ("Remove some stuffs"), but
`pyproject.toml` still points pytest at `tests/`, and good prior tests exist in
the parent commit. Recover a starting point rather than writing from scratch:

```powershell
git show c777c0c^:tests/test_process.py      > tests/test_process.py
git show c777c0c^:tests/test_build_tools.py  > tests/test_build_tools.py
git show c777c0c^:tests/test_diagnostics.py  > tests/test_diagnostics.py
git show c777c0c^:tests/conftest.py          > tests/conftest.py
```

Then prune to what still applies and add cases for the new behaviour:

- `test_process.py`
  - over-cap output keeps both the first line and the last line, with an
    omission marker between them;
  - a timeout sets `timed_out`, populates `pid` and `elapsed_ms`, and calls
    `taskkill` with `/F /T /PID <pid>` (stub `subprocess.run`/`Popen`, as the
    recovered tests already stub `subprocess`).
- `test_diagnostics.py`
  - `format_report` includes elapsed and counts on success;
  - histogram renders `CODE xN (dir)`;
  - a `deps/raygui` `C4996` warning is tagged pre-existing;
  - `parse_rebuild_kind` returns `""` for an unrecognised log (the
    never-guess rule) and the right kind for ninja/cmake markers.
- `test_build_tools.py`
  - a second `build_project` for the same directory returns
    `BUILD_ALREADY_RUNNING`;
  - the slot is released after a timeout and after an exception;
  - `msbuild` gains `/v:minimal`/`/nologo` when absent, and neither is
    duplicated when already present.

## Docs and changelog

- `README.md` `build_project` section (`README.md:476`): document the timeout
  payload (`status`, `pid`, `elapsed_ms`, tree terminated), the new
  `BUILD_ALREADY_RUNNING` error, default msbuild verbosity, and the success
  line's elapsed/counts. Add `BUILD_ALREADY_RUNNING` to the error list.
- `docs/CHANGELOG.md` `[Unreleased]`: one `### Added`/`### Changed` block per
  slice, in the existing explanatory style (state the failure each change
  closes, as the current entries do).

## Verification

Run from the repo root:

```powershell
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m pyright src
```

Manual confirmation of the headline fix (Finding 1):

1. Start a deliberately long `build_project` (e.g. a full C++ rebuild) with a
   small `timeout_seconds`.
2. Confirm the result is JSON with `"status": "timeout"` and a `pid`.
3. In another shell, `Get-Process msbuild,cl,link -ErrorAction SilentlyContinue`
   returns nothing — the tree is gone.
4. Re-run immediately with a larger timeout; it must build, not fail with
   LNK1163/LNK1104.

## Non-goals

- Detecting builds started outside this server process.
- Queueing builds (refused instead, by decision).
- Cross-run/on-disk diagnostic caching to label "seen in a previous run". It
  goes stale the moment a file is edited, and a wrong "pre-existing" tag is
  worse than none; the histogram plus vendored-path hint covers the reported
  case (`deps/raygui` `C4996`) without that risk.
- Changing `run_powershell`'s timeout handling (follow-up ticket only).
