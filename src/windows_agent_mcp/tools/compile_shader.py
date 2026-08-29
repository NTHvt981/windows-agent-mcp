"""Compile a GLSL or HLSL shader and report structured diagnostics."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..diagnostics import format_report, parse_build_output
from ..error import mcp_error
from ..log import log
from ..process import get_windows_development_environment, run_process
from ..utils import (
    SHADER_TIMEOUT_SECONDS,
    resolve_working_directory,
    resolve_write_path,
)

__all__: list[str] = ["compile_shader"]

# Extension -> glslc shader stage. These are the conventional Vulkan SDK
# suffixes, and glslc infers the stage from them on its own; the mapping is
# kept so an unknown extension is a clear error rather than a confusing one
# from the compiler.
_GLSL_STAGES: dict[str, str] = {
    ".vert": "vertex",
    ".frag": "fragment",
    ".comp": "compute",
    ".geom": "geometry",
    ".tesc": "tesscontrol",
    ".tese": "tesseval",
    ".mesh": "mesh",
    ".task": "task",
    ".rgen": "rgen",
    ".rchit": "rchit",
    ".rahit": "rahit",
    ".rmiss": "rmiss",
    ".rint": "rint",
    ".rcall": "rcall",
}

# Stages accepted for the ambiguous ".glsl" extension.
_VALID_STAGES = frozenset(_GLSL_STAGES.values())

# dxc and fxc profiles: ps_6_6, vs_5_0, lib_6_3, cs_6_0.
_PROFILE_PATTERN = re.compile(r"^[A-Za-z]{2,4}_[0-9]+_[0-9]+$")

_HLSL_EXTENSIONS = frozenset({".hlsl", ".hlsli"})
_FX_EXTENSIONS = frozenset({".fx"})


def compile_shader(
    source: str,
    output: str = "",
    stage: str = "",
    profile: str = "",
    spirv: bool = False,
    working_directory: str | None = None,
) -> str:
    """Compile a shader and report errors with file and line.

    The compiler is chosen from the file extension:

    * .vert .frag .comp .geom .tesc .tese .mesh .task .rgen .rchit .rahit
      .rmiss .rint .rcall -> glslc (Vulkan SPIR-V). Nothing else is needed.
    * .glsl -> glslc, but you must pass `stage` (vertex, fragment, compute,
      geometry, tesscontrol, tesseval, mesh, task or a ray-tracing stage).
    * .hlsl -> dxc. You must pass `profile`, e.g. "ps_6_6" or "cs_6_0". Pass
      spirv=true to target Vulkan instead of DXIL.
    * .fx -> fxc, which also needs `profile`.

    glslangValidator is used automatically if glslc is not installed.

    The output file is written beside the source unless `output` says
    otherwise, and is subject to the same write confinement as write_file:
    the download root, or a directory in BIONIC_PROJECT_ROOTS.

    Args:
        source: Shader file to compile.
        output: Destination. Defaults to the source path plus .spv (SPIR-V),
            .dxil (dxc) or .cso (fxc).
        stage: Shader stage, required only for a bare .glsl file.
        profile: Target profile, required for .hlsl and .fx.
        spirv: For .hlsl, emit SPIR-V for Vulkan rather than DXIL.
        working_directory: Directory to compile in, and what relative paths
            are resolved against. Defaults to the download root.

    Returns:
        A plain-text report: the output path and size on success, or errors
        with file and line. A structured JSON error on failure to start.

    Example:
        >>> compile_shader("shaders/post/blur.frag", working_directory="C:/game")
        'COMPILE: glslc shaders/post/blur.frag  (exit code 0)\\n\\n...'
    """

    if not source or not source.strip():
        return mcp_error(
            "INVALID_PATH",
            "compile_shader",
            "source cannot be empty.",
            recovery=["Pass the path of the shader to compile."],
        )

    try:
        resolved_directory = resolve_working_directory(working_directory)
    except ValueError as exc:
        return mcp_error(
            "WORKING_DIRECTORY_NOT_ALLOWED",
            "compile_shader",
            str(exc),
            path=working_directory,
            recovery=[
                "DO NOT retry the identical working_directory.",
                "Ask the user to add the project directory to BIONIC_PROJECT_ROOTS.",
            ],
        )

    # Resolve relative to the working directory rather than the server's cwd,
    # so a path that works for build_project works here too.
    source_path = Path(source)

    if not source_path.is_absolute():
        source_path = resolved_directory / source_path

    try:
        source_path = source_path.resolve()
    except OSError as exc:
        return mcp_error(
            "INVALID_PATH",
            "compile_shader",
            f"Could not resolve source '{source}': {exc}",
            path=source,
            recovery=["Check the path."],
        )

    if not source_path.is_file():
        return mcp_error(
            "PATH_NOT_FOUND",
            "compile_shader",
            f"'{source_path}' does not exist or is not a file.",
            path=source,
            recovery=[
                "DO NOT retry the identical path.",
                "Use find_files with '*.vert,*.frag,*.hlsl' to see which "
                "shaders exist.",
            ],
        )

    suffix = source_path.suffix.lower()

    plan = _plan_compile(
        suffix=suffix,
        stage=stage,
        profile=profile,
        spirv=spirv,
    )

    if isinstance(plan, str):
        return plan

    compiler_names, default_extension = plan

    environment = get_windows_development_environment()

    executable = None
    chosen = ""

    for name in compiler_names:
        found = shutil.which(name, path=environment.get("PATH"))

        if found:
            executable = found
            chosen = name
            break

    if executable is None:
        return mcp_error(
            "SHADER_COMPILER_NOT_FOUND",
            "compile_shader",
            f"None of {', '.join(compiler_names)} was found on PATH.",
            path=source,
            recovery=[
                "DO NOT retry the identical call.",
                "glslc and glslangValidator ship with the Vulkan SDK; dxc "
                "ships with the Vulkan SDK and the Windows SDK; fxc ships "
                "with the Windows SDK.",
                "Use get_gpu_info to see which shader tools were detected.",
                "Ask the user to install the relevant SDK.",
            ],
        )

    destination = output.strip() or str(source_path) + default_extension

    try:
        output_path = resolve_write_path(destination)
    except ValueError as exc:
        return mcp_error(
            "WRITE_PATH_NOT_ALLOWED",
            "compile_shader",
            str(exc),
            path=destination,
            recovery=[
                "DO NOT retry the identical output path.",
                "Compiling writes a binary next to the source, so the "
                "shader's directory must be writable.",
                "Ask the user to add the project directory to BIONIC_PROJECT_ROOTS.",
            ],
        )

    argv = _build_argv(
        executable=executable,
        compiler=chosen,
        source=source_path,
        output=output_path,
        suffix=suffix,
        stage=stage,
        profile=profile,
        spirv=spirv,
    )

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)

        result = run_process(
            argv,
            cwd=resolved_directory,
            timeout_seconds=SHADER_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        log.exception("compile_shader could not start %s", chosen)

        return mcp_error(
            "SHADER_COMPILE_START_FAILED",
            "compile_shader",
            f"Could not run '{chosen}': {exc}",
            path=source,
            recovery=["Do not repeatedly retry the identical command."],
        )

    if result.timed_out:
        return mcp_error(
            "SHADER_COMPILE_TIMED_OUT",
            "compile_shader",
            (
                f"'{chosen}' did not finish within {SHADER_TIMEOUT_SECONDS} "
                f"seconds, which for a shader means it is stuck."
            ),
            path=source,
            recovery=[
                "Do not immediately retry.",
                "Check the shader for a pathological loop or an include cycle.",
            ],
        )

    parsed = parse_build_output(result.combined, base_directory=resolved_directory)

    try:
        display_source = str(source_path.relative_to(resolved_directory))
    except ValueError:
        display_source = str(source_path)

    header = f"COMPILE: {chosen} {display_source.replace(chr(92), '/')}"

    # Naming the artefact and its size is the confirmation that matters: a
    # zero exit code with no output file means the compiler did nothing.
    success_line = None

    if output_path.is_file():
        size = output_path.stat().st_size
        success_line = f"COMPILED to {output_path} ({size:,} bytes)"

    log.info(
        "compile_shader %s %s: exit=%d errors=%d",
        chosen,
        source_path,
        result.exit_code,
        parsed.total_errors,
    )

    return format_report(
        parsed,
        header=header,
        exit_code=result.exit_code,
        raw_output=result.combined,
        # "BUILD FAILED" for one bad shader reads as though the whole project
        # failed, which sends the model looking in the wrong place.
        subject="COMPILE",
        success_line=success_line,
    )


def _plan_compile(
    *,
    suffix: str,
    stage: str,
    profile: str,
    spirv: bool,
) -> tuple[list[str], str] | str:
    """Decide which compiler to use, or return an error envelope.

    Returns:
        (candidate executables in preference order, default output extension),
        or a structured error string when the request cannot be satisfied.
    """

    if suffix in _HLSL_EXTENSIONS or suffix in _FX_EXTENSIONS:
        if not profile:
            return mcp_error(
                "MISSING_PROFILE",
                "compile_shader",
                f"A '{suffix}' shader needs a target profile.",
                recovery=[
                    "Pass profile, e.g. 'ps_6_6' for a pixel shader, "
                    "'vs_6_6' for vertex, 'cs_6_0' for compute.",
                    "dxc profiles use shader model 6.x; fxc uses 5.x.",
                ],
            )

        if not _PROFILE_PATTERN.match(profile):
            return mcp_error(
                "INVALID_PROFILE",
                "compile_shader",
                f"'{profile}' is not a valid shader profile.",
                recovery=[
                    "DO NOT retry the identical profile.",
                    "Use the form <stage>_<major>_<minor>, e.g. 'ps_6_6'.",
                ],
            )

        if suffix in _FX_EXTENSIONS:
            return ["fxc"], ".cso"

        return ["dxc"], ".spv" if spirv else ".dxil"

    if suffix == ".glsl":
        if not stage:
            return mcp_error(
                "MISSING_STAGE",
                "compile_shader",
                "A bare '.glsl' file does not say which stage it is.",
                recovery=[
                    "Pass stage, e.g. 'vertex', 'fragment' or 'compute'.",
                    "Or rename the file with a stage extension such as "
                    "'.vert' or '.frag', which is the Vulkan convention.",
                ],
            )

    if stage and stage not in _VALID_STAGES:
        return mcp_error(
            "INVALID_STAGE",
            "compile_shader",
            f"'{stage}' is not a known shader stage.",
            recovery=[
                "DO NOT retry the identical stage.",
                f"Use one of: {', '.join(sorted(_VALID_STAGES))}.",
            ],
        )

    if suffix not in _GLSL_STAGES and suffix != ".glsl":
        return mcp_error(
            "UNKNOWN_SHADER_TYPE",
            "compile_shader",
            f"'{suffix}' is not a recognised shader extension.",
            recovery=[
                "DO NOT retry the identical file.",
                "GLSL: .vert .frag .comp .geom .tesc .tese (or .glsl with a "
                "stage). HLSL: .hlsl with a profile. Effects: .fx.",
            ],
        )

    # glslc first: better diagnostics and it understands #include. Fall back
    # to glslangValidator, which some Vulkan SDK installs ship without glslc.
    return ["glslc", "glslangValidator"], ".spv"


def _build_argv(
    *,
    executable: str,
    compiler: str,
    source: Path,
    output: Path,
    suffix: str,
    stage: str,
    profile: str,
    spirv: bool,
) -> list[str]:
    """Assemble the compiler command line.

    Built as argv and executed with no shell, so nothing here is reparsed and
    a path containing spaces needs no quoting.
    """

    if compiler == "glslc":
        argv = [executable, str(source), "-o", str(output)]

        if stage:
            argv.append(f"-fshader-stage={stage}")

        return argv

    if compiler == "glslangValidator":
        # -V means "produce SPIR-V for Vulkan". -S sets the stage, which
        # glslangValidator needs spelled with its own short names.
        argv = [executable, "-V", str(source), "-o", str(output)]

        if stage:
            argv.extend(["-S", _GLSLANG_STAGE_FLAGS.get(stage, stage)])

        return argv

    if compiler == "dxc":
        argv = [executable, "-T", profile, "-Fo", str(output), str(source)]

        if spirv:
            argv.append("-spirv")

        return argv

    # fxc uses slash-prefixed options.
    del suffix

    return [executable, "/T", profile, "/Fo", str(output), str(source)]


# glslangValidator's -S takes short stage names, unlike glslc's long ones.
_GLSLANG_STAGE_FLAGS: dict[str, str] = {
    "vertex": "vert",
    "fragment": "frag",
    "compute": "comp",
    "geometry": "geom",
    "tesscontrol": "tesc",
    "tesseval": "tese",
    "mesh": "mesh",
    "task": "task",
}
