from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from windows_agent_mcp import process as process_module
from windows_agent_mcp.process import run_process


class FakeProcess:

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        pid: int = 4242,
    ) -> None:
        self.pid = pid
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.kill_called = False

    def communicate(self, timeout: float | None = None):
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.kill_called = True


class TimeoutProcess(FakeProcess):

    def __init__(self, stdout: str = "partial\n", pid: int = 4242) -> None:
        super().__init__(stdout=stdout, returncode=0, pid=pid)
        self.communicate_calls = 0

    def communicate(self, timeout: float | None = None):
        self.communicate_calls += 1

        if self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(cmd="msbuild", timeout=timeout)

        self.returncode = -1
        return self._stdout, self._stderr


def test_over_cap_output_keeps_both_ends_with_an_omission_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = "FIRST LINE\n" + ("x" * (200 * 1024)) + "\nLAST LINE"

    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(stdout=payload),
    )

    result = run_process(["ninja"], cwd=Path.cwd(), timeout_seconds=5)

    assert "FIRST LINE" in result.combined
    assert "LAST LINE" in result.combined
    assert "chars omitted" in result.combined
    assert len(result.combined) < len(payload)


def test_timeout_kills_the_tree_and_reports_pid_and_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_process = TimeoutProcess(pid=4242, stdout="compiling a.cpp\n")
    taskkill_calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(
        process_module.subprocess, "Popen", lambda *args, **kwargs: fake_process
    )

    def fake_run(argv, **kwargs):
        taskkill_calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(process_module.subprocess, "run", fake_run)

    ticks = iter([10.0, 12.5])
    monkeypatch.setattr(process_module.time, "monotonic", lambda: next(ticks, 12.5))

    result = run_process(["msbuild"], cwd=Path.cwd(), timeout_seconds=5)

    assert result.timed_out is True
    assert result.exit_code == -1
    assert result.pid == 4242
    assert result.elapsed_ms == 2500
    assert taskkill_calls[0][0] == ["taskkill", "/F", "/T", "/PID", "4242"]
    assert fake_process.kill_called is True
    assert "compiling a.cpp" in result.combined


def test_over_cap_output_writes_the_full_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = "FIRST LINE\n" + ("x" * (200 * 1024)) + "\nLAST LINE"
    log_path = tmp_path / "build.log"

    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(stdout=payload),
    )

    result = run_process(
        ["ninja"], cwd=Path.cwd(), timeout_seconds=5, log_path=log_path
    )

    assert result.log_path == log_path

    text = log_path.read_text(encoding="utf-8")

    assert "FIRST LINE" in text
    assert "LAST LINE" in text
    assert "truncated" not in text
    assert len(text) > len(result.combined)


def test_small_successful_output_leaves_no_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_path = tmp_path / "build.log"

    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(stdout="ok\n"),
    )

    result = run_process(
        ["ninja"], cwd=Path.cwd(), timeout_seconds=5, log_path=log_path
    )

    assert result.log_path is None
    assert not log_path.exists()


def test_timeout_writes_the_log_even_when_output_is_small(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_process = TimeoutProcess(pid=4242, stdout="compiling a.cpp\n")
    log_path = tmp_path / "build.log"

    monkeypatch.setattr(
        process_module.subprocess, "Popen", lambda *args, **kwargs: fake_process
    )

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(process_module.subprocess, "run", fake_run)

    result = run_process(
        ["msbuild"], cwd=Path.cwd(), timeout_seconds=5, log_path=log_path
    )

    assert result.timed_out is True
    assert result.log_path == log_path
    assert log_path.read_text(encoding="utf-8").strip() == "compiling a.cpp"
