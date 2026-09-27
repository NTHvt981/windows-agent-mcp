from __future__ import annotations

import time

import pytest

from windows_agent_mcp.buildlock import (
    BuildAlreadyRunning,
    build_slot,
    current_build,
)


def test_slot_is_visible_and_a_second_entry_is_refused(tmp_path) -> None:
    directory = tmp_path / "project"
    directory.mkdir()

    with build_slot(directory, "cmake --build build") as slot:
        assert current_build(directory) == slot

        with pytest.raises(BuildAlreadyRunning) as raised:
            with build_slot(directory, "ninja"):
                pytest.fail("a second build must not acquire the slot")

        assert raised.value.running.command == "cmake --build build"
        assert raised.value.elapsed_ms >= 0
        assert "already running" in str(raised.value)
        assert "cmake --build build" in str(raised.value)


def test_slot_is_released_after_normal_exit(tmp_path) -> None:
    directory = tmp_path / "project"
    directory.mkdir()

    with build_slot(directory, "cmake --build build"):
        pass

    assert current_build(directory) is None


def test_slot_is_released_when_the_body_raises(tmp_path) -> None:
    directory = tmp_path / "project"
    directory.mkdir()

    with pytest.raises(ValueError):
        with build_slot(directory, "cmake --build build"):
            raise ValueError("body failed")

    assert current_build(directory) is None


def test_different_directories_do_not_block_each_other(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    with build_slot(first, "cmake --build build"):
        with build_slot(second, "ninja") as slot:
            assert slot.command == "ninja"
            assert current_build(second) == slot


def test_elapsed_ms_grows_while_the_slot_is_held(tmp_path) -> None:
    directory = tmp_path / "project"
    directory.mkdir()

    with build_slot(directory, "cmake --build build"):
        time.sleep(0.02)

        with pytest.raises(BuildAlreadyRunning) as raised:
            with build_slot(directory, "ninja"):
                pass

        assert raised.value.elapsed_ms >= 20
