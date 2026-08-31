"""Tests for build_project and compile_shader.

Subprocesses are stubbed: these tests assert on the argv assembled and on how
output is turned into a report, not on whether cmake happens to be installed
on the machine running them. That keeps the suite hermetic, which pre-commit
depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import assert_error_response

from windows_agent_mcp.process import ProcessResult
from windows_agent_mcp.tools import build_project as build_module
from windows_agent_mcp.tools import compile_shader as shader_module
from windows_agent_mcp.tools.build_project import build_project
from windows_agent_mcp.tools.compile_shader import compile_shader


def result(
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    timed_out: bool = False,
) -> ProcessResult:
    return ProcessResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        timed_out=timed_out,
        combined=stdout + "\n" + stderr,
    )


@pytest.fixture
def build_runner(monkeypatch: pytest.MonkeyPatch):
    """Stub run_process for build_project, recording the argv it was given."""

    calls: list[dict[str, object]] = []

    def install(outcome: ProcessResult | Exception):
        def fake(argv, *, cwd, timeout_seconds):
            calls.append({"argv": argv, "cwd": cwd, "timeout": timeout_seconds})

            if isinstance(outcome, Exception):
                raise outcome

            return outcome

        monkeypatch.setattr(build_module, "run_process", fake)

        return calls

    return install


# ============================================================
# build_project: policy
# ============================================================


def test_build_rejects_composition(writable_project) -> None:
    """The allowlist means nothing if a second command can be appended."""

    payload = assert_error_response(
        build_project("cmake --build . ; Stop-Computer", str(writable_project)),
        "COMMAND_NOT_ALLOWED",
    )

    assert "more than one command" in payload["error"]["message"]


def test_build_rejects_a_non_allowlisted_program(writable_project) -> None:
    assert_error_response(
        build_project("notepad.exe", str(writable_project)),
        "COMMAND_NOT_ALLOWED",
    )


def test_build_rejects_an_allowlisted_but_non_build_command(
    writable_project,
) -> None:
    """Get-ChildItem is allowlisted, but cannot be exec'd without a shell."""

    payload = assert_error_response(
        build_project("Get-ChildItem", str(writable_project)),
        "NOT_A_BUILD_COMMAND",
    )

    recovery = " ".join(payload["error"]["recovery"])

    assert "run_powershell" in recovery
    assert "cmake" in recovery


def test_build_rejects_inline_code(writable_project) -> None:
    """python is a build driver, but not with -c."""

    assert_error_response(
        build_project('python -c "print(1)"', str(writable_project)),
        "COMMAND_NOT_ALLOWED",
    )


def test_build_rejects_an_empty_command() -> None:
    assert_error_response(build_project(""), "INVALID_COMMAND")


def test_build_refuses_a_directory_outside_every_root(
    tmp_path, isolated_download_root
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    assert_error_response(
        build_project("cmake --build .", str(outside)),
        "WORKING_DIRECTORY_NOT_ALLOWED",
    )


# ============================================================
# build_project: execution
# ============================================================


def test_build_runs_argv_without_a_shell(writable_project, build_runner) -> None:
    calls = build_runner(result(stdout="[1/1] Linking game\n"))

    build_project("cmake --build build --config Debug", str(writable_project))

    assert calls[0]["argv"] == ["cmake", "--build", "build", "--config", "Debug"]
    assert calls[0]["cwd"] == writable_project


def test_build_preserves_quoted_arguments(writable_project, build_runner) -> None:
    calls = build_runner(result())

    build_project('cmake --build "my build dir"', str(writable_project))

    assert calls[0]["argv"] == ["cmake", "--build", "my build dir"]


def test_build_reports_success(writable_project, build_runner) -> None:
    build_runner(result(stdout="[2/2] Linking CXX executable game\n"))

    report = build_project("ninja", str(writable_project))

    assert "BUILD SUCCEEDED" in report
    assert "exit code 0" in report


def test_build_reports_structured_errors(writable_project, build_runner) -> None:
    log = (
        r"C:\game\src\a.cpp(10,5): error C2065: 'x': undeclared identifier"
        "\n"
        r"C:\game\src\a.cpp(10,5): error C2065: 'x': undeclared identifier"
        "\n"
    )

    build_runner(result(stdout=log, exit_code=1))

    report = build_project("msbuild game.sln", str(writable_project))

    assert "BUILD FAILED" in report
    assert "C2065" in report
    assert "(x2)" in report


def test_build_shows_raw_tail_when_nothing_parses(
    writable_project, build_runner
) -> None:
    build_runner(result(stdout="something went sideways\n", exit_code=9))

    report = build_project("ninja", str(writable_project))

    assert "no recognised compiler diagnostics" in report
    assert "something went sideways" in report


def test_build_reports_a_missing_tool(writable_project, build_runner) -> None:
    build_runner(FileNotFoundError("cmake"))

    payload = assert_error_response(
        build_project("cmake --version", str(writable_project)),
        "BUILD_TOOL_NOT_FOUND",
    )

    assert "Get-Command" in " ".join(payload["error"]["recovery"])


def test_build_reports_a_start_failure(writable_project, build_runner) -> None:
    build_runner(OSError("exec format error"))

    assert_error_response(
        build_project("ninja", str(writable_project)),
        "BUILD_START_FAILED",
    )


def test_build_timeout_includes_partial_output(writable_project, build_runner) -> None:
    """The tail names the file the compiler was stuck on."""

    build_runner(result(stdout="compiling shader_permutations.cpp\n", timed_out=True))

    payload = assert_error_response(
        build_project("ninja", str(writable_project)),
        "BUILD_TIMED_OUT",
    )

    assert "shader_permutations.cpp" in payload["error"]["message"]
    assert "timeout_seconds" in " ".join(payload["error"]["recovery"])


def test_build_clamps_the_timeout(writable_project, build_runner) -> None:
    calls = build_runner(result())

    build_project("ninja", str(writable_project), timeout_seconds=99_999)

    assert calls[0]["timeout"] == 1800


def test_build_defaults_to_the_download_root(
    isolated_download_root, build_runner
) -> None:
    calls = build_runner(result())

    build_project("cmake --version")

    assert calls[0]["cwd"] == isolated_download_root


# ============================================================
# compile_shader: compiler selection
# ============================================================


@pytest.fixture
def shader_runner(monkeypatch: pytest.MonkeyPatch):
    """Stub which() and run_process for compile_shader."""

    calls: list[list[str]] = []

    def install(
        outcome: ProcessResult | Exception,
        *,
        available: tuple[str, ...] = ("glslc", "dxc", "fxc", "glslangValidator"),
    ):
        def fake_which(name: str, path: str | None = None) -> str | None:
            if name in available:
                return rf"C:\VulkanSDK\bin\{name}.exe"
            return None

        def fake_run(argv, *, cwd, timeout_seconds):
            calls.append(argv)

            if isinstance(outcome, Exception):
                raise outcome

            return outcome

        monkeypatch.setattr(shader_module.shutil, "which", fake_which)
        monkeypatch.setattr(shader_module, "run_process", fake_run)

        return calls

    return install


def shader(project: Path, name: str) -> str:
    target = project / "shaders" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#version 450\nvoid main() {}\n", encoding="utf-8")
    return str(target)


def test_glsl_extension_selects_glslc(writable_project, shader_runner) -> None:
    calls = shader_runner(result())

    source = shader(writable_project, "blur.frag")

    report = compile_shader(source, working_directory=str(writable_project))

    assert Path(calls[0][0]).stem == "glslc"
    assert calls[0][1] == source
    assert "-o" in calls[0]
    assert "COMPILE: glslc" in report


def test_glslc_output_defaults_to_spv_beside_the_source(
    writable_project, shader_runner
) -> None:
    calls = shader_runner(result())

    source = shader(writable_project, "blur.frag")

    compile_shader(source, working_directory=str(writable_project))

    assert calls[0][calls[0].index("-o") + 1] == source + ".spv"


def test_glslangvalidator_is_used_when_glslc_is_absent(
    writable_project, shader_runner
) -> None:
    calls = shader_runner(result(), available=("glslangValidator",))

    compile_shader(
        shader(writable_project, "mesh.vert"),
        working_directory=str(writable_project),
    )

    assert Path(calls[0][0]).stem == "glslangValidator"
    assert "-V" in calls[0]


def test_glslangvalidator_uses_short_stage_names(
    writable_project, shader_runner
) -> None:
    """-S takes "frag", not glslc's "fragment"."""

    calls = shader_runner(result(), available=("glslangValidator",))

    source = writable_project / "generic.glsl"
    source.write_text("void main() {}\n", encoding="utf-8")

    compile_shader(
        str(source),
        stage="fragment",
        working_directory=str(writable_project),
    )

    assert calls[0][calls[0].index("-S") + 1] == "frag"


def test_bare_glsl_requires_a_stage(writable_project, shader_runner) -> None:
    shader_runner(result())

    source = writable_project / "generic.glsl"
    source.write_text("void main() {}\n", encoding="utf-8")

    payload = assert_error_response(
        compile_shader(str(source), working_directory=str(writable_project)),
        "MISSING_STAGE",
    )

    assert ".vert" in " ".join(payload["error"]["recovery"])


def test_bare_glsl_with_a_stage_compiles(writable_project, shader_runner) -> None:
    calls = shader_runner(result())

    source = writable_project / "generic.glsl"
    source.write_text("void main() {}\n", encoding="utf-8")

    compile_shader(
        str(source),
        stage="compute",
        working_directory=str(writable_project),
    )

    assert "-fshader-stage=compute" in calls[0]


def test_invalid_stage_is_rejected(writable_project, shader_runner) -> None:
    shader_runner(result())

    assert_error_response(
        compile_shader(
            shader(writable_project, "a.frag"),
            stage="pixels",
            working_directory=str(writable_project),
        ),
        "INVALID_STAGE",
    )


def test_hlsl_requires_a_profile(writable_project, shader_runner) -> None:
    shader_runner(result())

    source = writable_project / "post.hlsl"
    source.write_text("float4 main() { return 0; }\n", encoding="utf-8")

    payload = assert_error_response(
        compile_shader(str(source), working_directory=str(writable_project)),
        "MISSING_PROFILE",
    )

    assert "ps_6_6" in " ".join(payload["error"]["recovery"])


def test_hlsl_profile_shape_is_validated(writable_project, shader_runner) -> None:
    shader_runner(result())

    source = writable_project / "post.hlsl"
    source.write_text("x\n", encoding="utf-8")

    assert_error_response(
        compile_shader(
            str(source),
            profile="pixel-shader",
            working_directory=str(writable_project),
        ),
        "INVALID_PROFILE",
    )


def test_hlsl_targets_dxil_by_default(writable_project, shader_runner) -> None:
    calls = shader_runner(result())

    source = writable_project / "post.hlsl"
    source.write_text("x\n", encoding="utf-8")

    compile_shader(
        str(source),
        profile="ps_6_6",
        working_directory=str(writable_project),
    )

    assert Path(calls[0][0]).stem == "dxc"
    assert calls[0][calls[0].index("-T") + 1] == "ps_6_6"
    assert calls[0][calls[0].index("-Fo") + 1].endswith(".dxil")
    assert "-spirv" not in calls[0]


def test_hlsl_can_target_spirv(writable_project, shader_runner) -> None:
    calls = shader_runner(result())

    source = writable_project / "post.hlsl"
    source.write_text("x\n", encoding="utf-8")

    compile_shader(
        str(source),
        profile="ps_6_6",
        spirv=True,
        working_directory=str(writable_project),
    )

    assert "-spirv" in calls[0]
    assert calls[0][calls[0].index("-Fo") + 1].endswith(".spv")


def test_fx_uses_fxc_with_slash_options(writable_project, shader_runner) -> None:
    calls = shader_runner(result())

    source = writable_project / "legacy.fx"
    source.write_text("x\n", encoding="utf-8")

    compile_shader(
        str(source),
        profile="ps_5_0",
        working_directory=str(writable_project),
    )

    assert Path(calls[0][0]).stem == "fxc"
    assert "/T" in calls[0]
    assert calls[0][calls[0].index("/Fo") + 1].endswith(".cso")


def test_unknown_extension_is_rejected(writable_project, shader_runner) -> None:
    shader_runner(result())

    source = writable_project / "notes.txt"
    source.write_text("x\n", encoding="utf-8")

    assert_error_response(
        compile_shader(str(source), working_directory=str(writable_project)),
        "UNKNOWN_SHADER_TYPE",
    )


# ============================================================
# compile_shader: outcomes
# ============================================================


def test_missing_compiler_is_reported_with_sdk_guidance(
    writable_project, shader_runner
) -> None:
    shader_runner(result(), available=())

    payload = assert_error_response(
        compile_shader(
            shader(writable_project, "a.frag"),
            working_directory=str(writable_project),
        ),
        "SHADER_COMPILER_NOT_FOUND",
    )

    recovery = " ".join(payload["error"]["recovery"])

    assert "Vulkan SDK" in recovery
    assert "get_gpu_info" in recovery


def test_shader_errors_are_parsed(writable_project, shader_runner) -> None:
    source = shader(writable_project, "blur.frag")

    shader_runner(
        result(
            stderr=f"{source}:12:5: error: 'outColor' : undeclared identifier\n",
            exit_code=1,
        )
    )

    report = compile_shader(source, working_directory=str(writable_project))

    # "COMPILE FAILED", not "BUILD FAILED": one bad shader is not a failed
    # project, and saying so sends the model looking in the wrong place.
    assert "COMPILE FAILED" in report
    assert "BUILD FAILED" not in report
    assert "undeclared identifier" in report
    assert ":12:5" in report


def test_successful_compile_reports_the_output_size(
    writable_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patched via monkeypatch, not by assigning to the module.

    A bare module assignment here would survive the test and silently stub
    run_process for everything that ran afterwards.
    """

    source = shader(writable_project, "blur.frag")

    def fake_which(name: str, path: str | None = None) -> str | None:
        return "C:/VulkanSDK/bin/glslc.exe" if name == "glslc" else None

    def fake_run(argv, *, cwd, timeout_seconds):
        # A real compiler writes the artefact; the report reads its size.
        Path(source + ".spv").write_bytes(b"\x03\x02#\x07" * 8)
        return result()

    monkeypatch.setattr(shader_module.shutil, "which", fake_which)
    monkeypatch.setattr(shader_module, "run_process", fake_run)

    report = compile_shader(source, working_directory=str(writable_project))

    assert "COMPILED to" in report
    assert "32 bytes" in report
    assert "SUCCEEDED" not in report


def test_missing_source_is_reported(writable_project, shader_runner) -> None:
    shader_runner(result())

    payload = assert_error_response(
        compile_shader(
            str(writable_project / "nope.frag"),
            working_directory=str(writable_project),
        ),
        "PATH_NOT_FOUND",
    )

    assert "find_files" in " ".join(payload["error"]["recovery"])


def test_empty_source_is_rejected() -> None:
    assert_error_response(compile_shader(""), "INVALID_PATH")


def test_output_outside_a_writable_root_is_refused(
    writable_project, shader_runner, tmp_path
) -> None:
    shader_runner(result())

    payload = assert_error_response(
        compile_shader(
            shader(writable_project, "a.frag"),
            output=str(tmp_path / "elsewhere.spv"),
            working_directory=str(writable_project),
        ),
        "WRITE_PATH_NOT_ALLOWED",
    )

    assert "WAMCP_PROJECT_ROOTS" in " ".join(payload["error"]["recovery"])


def test_shader_timeout_is_reported(writable_project, shader_runner) -> None:
    shader_runner(result(timed_out=True))

    assert_error_response(
        compile_shader(
            shader(writable_project, "a.frag"),
            working_directory=str(writable_project),
        ),
        "SHADER_COMPILE_TIMED_OUT",
    )


def test_shader_start_failure_is_reported(writable_project, shader_runner) -> None:
    shader_runner(OSError("denied"))

    assert_error_response(
        compile_shader(
            shader(writable_project, "a.frag"),
            working_directory=str(writable_project),
        ),
        "SHADER_COMPILE_START_FAILED",
    )


def test_relative_source_resolves_against_the_working_directory(
    writable_project, shader_runner
) -> None:
    calls = shader_runner(result())

    shader(writable_project, "blur.frag")

    compile_shader("shaders/blur.frag", working_directory=str(writable_project))

    assert calls[0][1] == str(writable_project / "shaders" / "blur.frag")


def test_shader_refuses_a_working_directory_outside_every_root(
    tmp_path, isolated_download_root
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    assert_error_response(
        compile_shader("a.frag", working_directory=str(outside)),
        "WORKING_DIRECTORY_NOT_ALLOWED",
    )
