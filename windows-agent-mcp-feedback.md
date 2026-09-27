# windows-agent-mcp feedback (from an AI coding agent, 2026-09-26)

## Context

- Tool user: an automated coding agent (opencode/DeepSeek) and its subagents.
- Project: a C++20 + MSBuild/VS2022 game (raylib), x64, ~200 source files plus a
  heavy static-link step.
- MCP tools used: `build_project` (primary), plus occasional `run_powershell`.
  `find_files`/`search_files`/`read_file` overlap the agent's own tools and were
  rarely used.
- Commands used most: `msbuild prj/ConflictConverge.sln /p:Configuration=Debug
  /p:Platform=x64 /v:minimal` and the Release variant, plus a separately built
  ASan test project.

## TL;DR

1. **`build_project` timeouts leave the build running orphaned** and return no
   diagnostics. This is the one change that would measurably help. On a big C++
   link this happens often enough to be disruptive.
2. Success output ("BUILD SUCCEEDED") is correct but too terse to trust on its
   own: no elapsed time, no warning count, no incremental-vs-full signal.
3. No guard against launching a second `msbuild` while an orphaned first one is
   still writing the same `obj/`+`libs`, which produces link-lock/flaky failures
   (LNK1163/LNK1104) that the caller misreads as a code error.
4. `find_files`/`search_files` skipping build output trees is genuinely useful
   for this kind of repo; the unique-error parser is the right idea — just make
   sure the timeout path also returns the tail.

## Finding 1 (high): timeout orphans the build and drops diagnostics

Observed: two `build_project` calls returned a timeout while `msbuild` continued
running in the background. The caller received no compiler diagnostics, and the
build later completed successfully (a subsequent fresh test executable run
confirmed it). A retry then risks racing the still-running first invocation.

Why it matters:
- The caller cannot distinguish "build failed", "build is slow", and "build
  succeeded after I stopped listening".
- Orphaned `msbuild`/`cl`/`link` hold PDB/lib locks. This project's own docs
  already instruct agents to `Stop-Process -Name msbuild,cl,link -Force` after
  timed-out MSBuild runs — a workaround the tool should not require.
- The most valuable output (first compile/link errors) is exactly what is lost.

Suggested behavior:
- On timeout, return a structured result: `status = "timeout"`, the child PID if
  available, elapsed time, and **all diagnostics captured so far** (at minimum
  the tail).
- Provide a follow-up mode to *wait/attach* to the running build (or to poll it)
  so a retry does not start a competing build against the same outputs.
- Alternatively (or additionally), on timeout terminate the child process tree.
  If termination is off by policy, say so explicitly in the message.
- A `timeout_seconds` passed by the caller should be honored for the whole
  invocation; the agent observed that explicitly raising it (600–900 s) made the
  same build return `exit code 0` reliably.

## Finding 2 (medium): success result lacks triage information

`BUILD SUCCEEDED` (exit 0) is accurate but tells the caller nothing about:
- elapsed time (was it incremental or a 10-minute first link?),
- warning count and origin (this repo has known third-party C4996 in
  `deps/raygui` and LNK4099 PDB warnings — they should be labelled
  "pre-existing" so agents do not chase them),
- whether the compiler actually did work (`up-to-date` vs `recompiled N`).

Suggested additions to the result:
- `elapsed_ms`, `warnings`, `errors`, and `rebuild = up-to-date | incremental |
  full` when derivable.
- Group warnings/errors as "new in this invocation" vs "also seen in a previous
  run" if caching is possible; otherwise just surface the counts and a short
  per-code histogram (e.g. `C4996 x7 (deps/raygui)`).

## Finding 3 (medium): no concurrency/lock guard

If a previous invocation was orphaned (Finding 1) or the user has a build running
elsewhere, `build_project` happily starts another `msbuild` on the same solution.
Symptoms look like source errors but are lock contention.

Suggested behavior:
- Detect an existing `msbuild`/`link` for the same solution/working directory and
  either refuse with a clear message or queue behind it.
- Optionally expose a `--force`/`clean-locks` action that stops the known build
  processes before starting (mirroring this project's documented manual fix).

## Finding 4 (low): verbosity / log handling

- `/v:minimal` was passed manually on every call. Defaulting to minimal (or
  stripping per-TU banner noise) would make the parsed output smaller without
  losing errors.
- Confirm large logs are still fully retrievable when the summary is truncated
  (the tool description says they are; keep that, and return an offset/path so
  the caller can page the full log).

## What already works well

- The **unique-errors-with-repeat-counts** parser is exactly right for C++
  (per-TU repetition of the same header error is the norm). Keep it, and make it
  also emit the raw tail when nothing parses but the exit code is non-zero.
- Restricting to one build-driver command with no pipelines/separators is a good
  safety/portability constraint for PowerShell on Windows.
- Explicit `working_directory` behaved predictably.
- `find_files`/`search_files` ignoring `build/`, `obj/`, `bin/`, `x64/`,
  `Debug/`, `Release/` is a real quality-of-life win on game repos.
- Splitting `build_project` (diagnostics) from `run_powershell` (everything else)
  is the right separation.

## Concrete minimal requests

1. Timeout result must include status + partial diagnostics; add a way to
   wait/attach or to cancel cleanly. (Finding 1)
2. Add `elapsed_ms` / `warnings` / `errors` (and a pre-existing-warnings hint) to
   the success result. (Finding 2)
3. Refuse or serialize concurrent builds on the same solution. (Finding 3)
4. Default to minimal verbosity. (Finding 4)

## Appendix: representative invocations

```
msbuild prj/ConflictConverge.sln /p:Configuration=Debug /p:Platform=x64 /v:minimal
msbuild prj/ConflictConverge.sln /p:Configuration=Release /p:Platform=x64 /v:minimal
# first ASan build: 15+ min (documented in the project), the case most likely
# to hit the timeout path
```

Note: none of this blocked the work; it only cost retries and manual
lock-cleanup. The feedback is offered to make the tool's timeouts and success
results self-explanatory.
