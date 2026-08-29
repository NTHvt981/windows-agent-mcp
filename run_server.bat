@echo off
REM ============================================================
REM  Windows Agent MCP Server -- launcher
REM
REM    run_server.bat              start the server over stdio
REM    run_server.bat --dev        start it under the MCP Inspector
REM    run_server.bat --web        enable the web research tools
REM    run_server.bat --dev --web  both
REM
REM    run_server.bat --allow-host docs.example.com
REM                                let fetch_web_page read one extra host
REM
REM  Anchored to the script's own directory, so it works from anywhere.
REM  setlocal keeps the environment changes below out of your shell.
REM ============================================================

setlocal EnableExtensions
pushd "%~dp0"

set "DEV="
set "WEB="
set "TOOLS="
set "PROFILE="
set "ALLOW_HOSTS="

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--dev" (
    set "DEV=1"
    shift
    goto parse_args
)
if /I "%~1"=="--web" (
    set "WEB=1"
    shift
    goto parse_args
)
if /I "%~1"=="--tools" (
    set "TOOLS_SEEN=1"
    shift
    goto collect_tools
)
if /I "%~1"=="--allow-host" (
    set "HOSTS_SEEN=1"
    shift
    goto collect_hosts
)
if /I "%~1"=="--profile" (
    if "%~2"=="" (
        echo [error] --profile needs a name, e.g. --profile cpp
        echo.
        goto usage
    )
    set "PROFILE=%~2"
    shift
    shift
    goto parse_args
)
if /I "%~1"=="-h" goto usage
if /I "%~1"=="--help" goto usage
if "%~1"=="/?" goto usage
echo [error] Unknown option: %~1
echo.
goto usage

:collect_tools
if "%~1"=="" goto args_done
set "TOKEN=%~1"
if "%TOKEN:~0,1%"=="-" goto parse_args
if defined TOOLS (set "TOOLS=%TOOLS%,%TOKEN%") else (set "TOOLS=%TOKEN%")
shift
goto collect_tools

REM Same rejoin loop as :collect_tools, for the same reason: cmd.exe splits
REM a batch argument on commas and semicolons, so --allow-host a.com,b.com
REM arrives as two separate tokens and the second would look like a stray
REM option.
:collect_hosts
if "%~1"=="" goto args_done
set "HOST_TOKEN=%~1"
if "%HOST_TOKEN:~0,1%"=="-" goto parse_args
if defined ALLOW_HOSTS (set "ALLOW_HOSTS=%ALLOW_HOSTS%,%HOST_TOKEN%") else (set "ALLOW_HOSTS=%HOST_TOKEN%")
shift
goto collect_hosts

:args_done

if defined TOOLS_SEEN if not defined TOOLS goto no_tools_list
if defined HOSTS_SEEN if not defined ALLOW_HOSTS goto no_hosts_list

set "PYTHON=%CD%\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [error] No virtual environment found at:
    echo         %PYTHON%
    echo.
    echo         Create it with:  uv sync --extra dev
    goto fail
)

REM The package must be importable, or the server dies with a traceback that
REM does not obviously mean "you did not install it".
"%PYTHON%" -c "import windows_agent_mcp" >nul 2>&1
if errorlevel 1 (
    echo [error] The windows_agent_mcp package is not importable.
    echo         Install it in editable mode with:  uv sync --extra dev
    goto fail
)

REM stdout IS the MCP protocol channel, so its encoding is not cosmetic: on a
REM Windows console Python would otherwise default to cp1252 and mangle any
REM non-ASCII byte in the JSON-RPC stream. Unbuffered keeps responses from
REM sitting in a pipe buffer waiting for the client.
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

if defined WEB (
    set "BIONIC_WEB_RESEARCH=1"
    echo [info] Web research ENABLED -- web_search is registered, and
    echo        fetch_web_page may now reach any public HTTPS host instead of
    echo        documentation sites only. Fetched pages are untrusted input,
    echo        and this is an outbound channel. See README.md.
)

if not defined ALLOW_HOSTS goto hosts_done
set "BIONIC_EXTRA_DOC_HOSTS=%ALLOW_HOSTS%"
echo [info] Extra readable hosts: %ALLOW_HOSTS%
echo        fetch_web_page may read these in addition to the built-in
echo        documentation hosts. Reading only -- download_file is
echo        unaffected. This lasts for this run; for a lasting grant:
echo            .venv\Scripts\python.exe -m windows_agent_mcp.hostgrants --add HOST
echo.
:hosts_done

if not defined PROFILE goto profile_done
set "BIONIC_PROFILE=%PROFILE%"
echo [info] Profile: %PROFILE%
echo        Resolved from mcp-profiles.json. If the name is wrong or
echo        disabled the server still starts with default tools and
echo        get_server_info reports why.
echo.
:profile_done

if not defined TOOLS goto tools_done
set "BIONIC_TOOLS=%TOOLS%"
echo [info] Tool groups: %TOOLS%
echo        core is always included. get_server_info reports the
echo        active set and names any group it did not recognise.
REM Substring test via batch string substitution rather than piping to
REM find.exe: on a machine with Git for Windows on PATH, `find` resolves to
REM the Unix find and the check silently breaks. %VAR:text=% removes every
REM occurrence, so an unchanged value means the text was absent.
set "EDIT_CHECK=%TOOLS:edit=%"
set "ALL_CHECK=%TOOLS:all=%"
if not "%EDIT_CHECK%"=="%TOOLS%" goto tools_note_done
if not "%ALL_CHECK%"=="%TOOLS%" goto tools_note_done
echo        NOTE: 'edit' is not listed, so write_file and edit_file
echo              are NOT registered.
:tools_note_done
echo.
:tools_done

REM The single most common confusion: write_file/edit_file/build_project all
REM refuse to touch a project until the operator names it. Say so up front
REM rather than letting the model discover it as a permission error.
if not defined BIONIC_PROJECT_ROOTS (
    echo [info] BIONIC_PROJECT_ROOTS is not set, so writes and builds are
    echo        limited to the download sandbox. To work on a real project:
    echo            set BIONIC_PROJECT_ROOTS=C:\path\to\your\project
    echo.
) else (
    echo [info] Writable/buildable roots: %BIONIC_PROJECT_ROOTS%
    echo.
)

if defined DEV goto run_inspector

echo [info] Starting MCP server on stdio.
echo        There is no prompt: it waits for JSON-RPC on stdin, so it will
echo        look like it has hung. Use --dev for an interactive client.
echo.
"%PYTHON%" -m windows_agent_mcp
set "CODE=%ERRORLEVEL%"
goto done

:run_inspector

REM Find npx: PATH first, then the standard install locations.
REM
REM The fallback is not paranoia. Node can be installed AND listed in the
REM machine PATH while this process still cannot see it, because a process
REM inherits PATH at launch and never sees later changes. Observed here:
REM node.exe existed and the registry PATH named it, yet "where npx" found
REM nothing. Without the fallback the script tells you to install software
REM you already have.
set "NODE_DIR="
where npx >nul 2>&1
if not errorlevel 1 goto npx_ready

if exist "%ProgramFiles%\nodejs\npx.cmd" set "NODE_DIR=%ProgramFiles%\nodejs"
if not defined NODE_DIR if exist "%LOCALAPPDATA%\Programs\nodejs\npx.cmd" set "NODE_DIR=%LOCALAPPDATA%\Programs\nodejs"

if not defined NODE_DIR goto no_npx

REM Prepend rather than calling npx.cmd by full path: npx shells out to node,
REM and tooling underneath it expects both on PATH.
set "PATH=%NODE_DIR%;%PATH%"
echo [info] npx was not on PATH; using %NODE_DIR%
echo        Your shell's PATH predates the Node install -- reopen it to fix.

:npx_ready

REM The Inspector does NOT hand its own environment to the server it spawns.
REM The MCP SDK's stdio client uses a fixed allowlist (PATH, TEMP, APPDATA and
REM a few more) so a client cannot leak its secrets into every server it
REM launches -- which means BIONIC_TOOLS, BIONIC_PROJECT_ROOTS and
REM BIONIC_WEB_RESEARCH all silently vanished under --dev. Inspector 2.x has
REM no -e flag; a config file with an explicit env block is the supported
REM route, and values stated there ARE passed through.
REM
REM Generated by Python rather than assembled here: quoting a Windows path
REM inside JSON inside a cmd `set` is a reliable way to produce a broken file.
set "MCP_CFG=%TEMP%\windows-agent-mcp-inspector.json"
"%PYTHON%" -m windows_agent_mcp.inspector_config "%MCP_CFG%" "%PYTHON%"
if errorlevel 1 (
    echo [error] Could not write the Inspector config to:
    echo         %MCP_CFG%
    goto fail
)

echo [info] Starting the MCP Inspector. It will open a browser tab.
echo        Press Ctrl+C here to stop both.
echo.

REM "call" matters: npx is a .cmd, and invoking it directly would hand over
REM control and never return here, skipping the cleanup below.
call npx @modelcontextprotocol/inspector --config "%MCP_CFG%" --server windows-agent-mcp
set "CODE=%ERRORLEVEL%"
goto done

:no_hosts_list
echo [error] --allow-host needs a hostname, for example:
echo            run_server.bat --allow-host docs.example.com
echo            run_server.bat --allow-host a.example.com,b.example.com
echo.
echo         A hostname only: no scheme, no path, no wildcard.
goto fail

:no_tools_list
echo [error] --tools needs a group list, for example:
echo            run_server.bat --tools core,docs
echo            run_server.bat --tools edit,build,docs
echo.
goto usage

:no_npx
echo [error] npx was not found, so the Inspector cannot start.
echo.
echo         Looked on PATH, and in:
echo           %ProgramFiles%\nodejs
echo           %LOCALAPPDATA%\Programs\nodejs
echo.
echo         Install Node.js, or run without --dev.
goto fail

:usage
echo Usage: run_server.bat [--dev] [--web] [--tools GROUPS]
echo                       [--profile NAME] [--allow-host HOSTS]
echo.
echo   --dev    Run under the MCP Inspector (needs Node.js/npx). Gives you a
echo            browser UI to list and call tools by hand.
echo   --web    Set BIONIC_WEB_RESEARCH=1, which registers web_search and
echo            lets fetch_web_page reach ANY public host. Read the
echo            "Web research" section of README.md before using this.
echo            Usually you want less: --tools core,docs,search registers
echo            web_search WITHOUT widening which hosts may be read, and
echo            --allow-host below opens just the sites you need.
echo   --tools  Comma-separated tool groups to register. Fewer tools means
echo            more context left for the model to work in. Groups:
echo              core      always on: read, list, find, search, info
echo              edit      write_file, edit_file
echo              build     run_powershell, build_project, compile_shader,
echo                        get_gpu_info
echo              docs      fetch_web_page (documentation hosts)
echo              net       download_file, fetch_https_response,
echo                        get_github_latest_release
echo              search    web_search. Lets the model FIND urls without
echo                        widening which hosts it may read. Pair it
echo                        with docs; alone it finds pages nothing can
echo                        open, and startup will say so.
echo              research  web_search, and any-public-host fetching
echo              all       every group
echo            Omit to register everything except search and research.
echo            Examples: --tools edit,build,docs
echo                      --tools core,docs,search
echo   --profile  Named profile from mcp-profiles.json, which is a
echo            friendlier way to say --tools. Create the file with:
echo              .venv\Scripts\python.exe -m windows_agent_mcp.profiles --init
echo            then list what it defines with --list. An explicit
echo            --tools wins over a profile.
echo   --allow-host  Extra hostnames fetch_web_page may READ, on top of
echo            the built-in documentation hosts. Comma-separated,
echo            hostnames only -- no scheme, path or wildcard. Reading
echo            only: download_file is not affected.
echo            Applies to this run. To grant a host permanently, or
echo            while a server is already running (it takes effect on
echo            the next call, no restart):
echo              .venv\Scripts\python.exe -m windows_agent_mcp.hostgrants --add HOST
echo            Prefer this over --web when you need one extra site.
echo   -h       Show this help.
echo.
echo With no options the server runs on stdio, which is what an MCP client
echo expects. Run the tests instead with:
echo     .venv\Scripts\python.exe -m pytest tests -q
set "CODE=1"
goto done

:fail
set "CODE=1"

:done
popd
endlocal & exit /b %CODE%
