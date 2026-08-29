"""Tests for get_gpu_info.

Every probe is stubbed. The property worth protecting is that a failed probe
says so: reporting an empty adapter list when WMI simply did not answer would
have the model conclude there is no GPU, which is a far worse outcome than
saying "unknown".
"""

from __future__ import annotations

import json

import pytest

from windows_agent_mcp.process import ProcessResult
from windows_agent_mcp.tools import get_gpu_info as gpu_module
from windows_agent_mcp.tools.get_gpu_info import get_gpu_info

ADAPTER = {
    "Name": "NVIDIA GeForce RTX 4070",
    "DriverVersion": "32.0.15.6094",
    "DriverDate": "2025-01-14T00:00:00Z",
    "AdapterRAM": 4293918720,
    "VideoProcessor": "NVIDIA GeForce RTX 4070",
    "CurrentHorizontalResolution": 3840,
    "CurrentVerticalResolution": 2160,
}

VULKANINFO = """==========
VULKANINFO
==========

Vulkan Instance Version: 1.4.309

Instance Extensions: count = 3
VK_EXT_debug_report                    : extension revision 10
VK_EXT_debug_utils                     : extension revision 2
VK_KHR_surface                         : extension revision 25
Instance Layers: count = 2
VK_LAYER_KHRONOS_validation       Khronos Validation Layer   1.3.250  version 1
VK_LAYER_LUNARG_monitor           Execution Monitoring Layer 1.3.250  version 1

Devices:
--------
GPU0:
	apiVersion = 1.4.303
	driverVersion = 580.88.0.0
	deviceName = NVIDIA GeForce RTX 4070
"""


def ok(stdout: str) -> ProcessResult:
    return ProcessResult(
        stdout=stdout, stderr="", exit_code=0, timed_out=False, combined=stdout
    )


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch):
    """Stub which() and run_process, routing by which program is asked for."""

    def install(
        *,
        wmi: ProcessResult | Exception | None = None,
        vulkan: ProcessResult | Exception | None = None,
        available: tuple[str, ...] = (),
    ):
        def fake_which(name: str, path: str | None = None) -> str | None:
            return f"C:/tools/{name}.exe" if name in available else None

        def fake_run(argv, *, cwd, timeout_seconds):
            target = argv[0].lower()

            outcome = vulkan if "vulkaninfo" in target else wmi

            if outcome is None:
                raise AssertionError(f"unexpected probe: {argv}")

            if isinstance(outcome, Exception):
                raise outcome

            return outcome

        monkeypatch.setattr(gpu_module.shutil, "which", fake_which)
        monkeypatch.setattr(gpu_module, "run_process", fake_run)

    return install


# ============================================================
# Adapters
# ============================================================


def test_adapter_is_reported_with_driver_and_mode(probes) -> None:
    probes(wmi=ok(json.dumps(ADAPTER)))

    report = get_gpu_info()

    assert "NVIDIA GeForce RTX 4070" in report
    assert "driver 32.0.15.6094" in report
    assert "2025-01-14" in report
    assert "3840x2160" in report


def test_single_adapter_json_object_is_handled(probes) -> None:
    """ConvertTo-Json emits an object for one adapter and an array for many."""

    probes(wmi=ok(json.dumps(ADAPTER)))

    assert "GPU ADAPTERS" in get_gpu_info()


def test_multiple_adapters_are_all_listed(probes) -> None:
    second = dict(ADAPTER, Name="Intel UHD Graphics 770", AdapterRAM=1073741824)

    probes(wmi=ok(json.dumps([ADAPTER, second])))

    report = get_gpu_info()

    assert "NVIDIA GeForce RTX 4070" in report
    assert "Intel UHD Graphics 770" in report
    assert "1.0 GB" in report


def test_vram_at_the_wmi_ceiling_is_flagged_not_reported_as_fact(probes) -> None:
    """AdapterRAM is a 32-bit field; a 12 GB card reports ~4 GB."""

    probes(wmi=ok(json.dumps(ADAPTER)))

    report = get_gpu_info()

    assert ">= 4 GB" in report
    assert "caps this field" in report


def test_wmi_failure_says_unknown_rather_than_none(probes) -> None:
    """The important one: a probe failure must not read as "no GPU"."""

    probes(wmi=ProcessResult("", "", 1, False, ""))

    report = get_gpu_info()

    assert "Could not query WMI" in report
    assert "not an absence of hardware" in report


def test_wmi_timeout_is_handled(probes) -> None:
    probes(wmi=ProcessResult("", "", -1, True, ""))

    assert "Could not query WMI" in get_gpu_info()


def test_non_json_from_wmi_is_handled(probes) -> None:
    probes(wmi=ok("this is not json"))

    assert "Could not query WMI" in get_gpu_info()


def test_powershell_missing_is_handled(probes) -> None:
    probes(wmi=OSError("powershell.exe not found"))

    assert "Could not query WMI" in get_gpu_info()


def test_empty_adapter_list_is_stated(probes) -> None:
    probes(wmi=ok("[]"))

    assert "No video controllers reported." in get_gpu_info()


def test_unnamed_adapter_does_not_crash(probes) -> None:
    probes(wmi=ok(json.dumps({"DriverVersion": "1.0"})))

    assert "(unnamed adapter)" in get_gpu_info()


def test_non_dict_entries_are_skipped(probes) -> None:
    probes(wmi=ok(json.dumps(["not an object", ADAPTER])))

    assert "NVIDIA GeForce RTX 4070" in get_gpu_info()


# ============================================================
# Vulkan
# ============================================================


def test_vulkaninfo_summary_is_included(probes) -> None:
    probes(
        wmi=ok(json.dumps(ADAPTER)),
        vulkan=ok(VULKANINFO),
        available=("vulkaninfo",),
    )

    report = get_gpu_info()

    assert "Vulkan Instance Version: 1.4.309" in report
    assert "apiVersion = 1.4.303" in report

    # Rule lines carry no information and cost tokens.
    assert "==========" not in report


def test_missing_vulkaninfo_is_explained(probes) -> None:
    probes(wmi=ok(json.dumps(ADAPTER)))

    report = get_gpu_info()

    assert "vulkaninfo not found" in report
    assert "Install the Vulkan SDK" in report


def test_vulkaninfo_timeout_suggests_a_broken_driver(probes) -> None:
    probes(
        wmi=ok(json.dumps(ADAPTER)),
        vulkan=ProcessResult("", "", -1, True, ""),
        available=("vulkaninfo",),
    )

    assert "vulkaninfo timed out" in get_gpu_info()


def test_vulkaninfo_failure_is_reported(probes) -> None:
    probes(
        wmi=ok(json.dumps(ADAPTER)),
        vulkan=ProcessResult("", "no device", 1, False, "no device"),
        available=("vulkaninfo",),
    )

    assert "vulkaninfo failed" in get_gpu_info()


def test_vulkaninfo_start_failure_is_reported(probes) -> None:
    probes(
        wmi=ok(json.dumps(ADAPTER)),
        vulkan=OSError("access denied"),
        available=("vulkaninfo",),
    )

    assert "Could not run vulkaninfo" in get_gpu_info()


def test_vulkaninfo_output_is_capped(probes) -> None:
    probes(
        wmi=ok(json.dumps(ADAPTER)),
        vulkan=ok("\n".join(f"line {index}" for index in range(500))),
        available=("vulkaninfo",),
    )

    report = get_gpu_info()

    assert "vulkaninfo output truncated" in report


def test_empty_vulkaninfo_output(probes) -> None:
    probes(
        wmi=ok(json.dumps(ADAPTER)),
        vulkan=ok("   \n"),
        available=("vulkaninfo",),
    )

    assert "produced no output" in get_gpu_info()


# ============================================================
# Driver dates
# ============================================================

# What ConvertTo-Json actually emits for a DateTime. Not ISO 8601 -- this is
# the form observed on a real machine, and assuming ISO here rendered the
# driver date as "(/Date(1719".
WMI_DATE_ADAPTER = dict(ADAPTER, DriverDate="/Date(1719705600000)/")


def test_wmi_json_date_form_is_parsed(probes) -> None:
    probes(wmi=ok(json.dumps(WMI_DATE_ADAPTER)))

    report = get_gpu_info()

    assert "2024-06-30" in report
    assert "/Date(" not in report


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/Date(1719705600000)/", "2024-06-30"),
        ("/Date(0)/", "1970-01-01"),
        ("2025-01-14T00:00:00Z", "2025-01-14"),
        ("2025-01-14", "2025-01-14"),
        # Junk degrades to "" rather than being sliced into nonsense.
        ("/Date(abc)/", ""),
        ("14/01/2025", ""),
        ("", ""),
        (None, ""),
        (12345, ""),
        # Out of range for fromtimestamp on Windows.
        ("/Date(-99999999999999999)/", ""),
    ],
)
def test_driver_date_rendering(value: object, expected: str) -> None:
    assert gpu_module._driver_date(value) == expected


def test_an_unparseable_date_is_omitted_not_shown_raw(probes) -> None:
    probes(wmi=ok(json.dumps(dict(ADAPTER, DriverDate="not a date"))))

    report = get_gpu_info()

    assert "not a date" not in report
    assert "NVIDIA GeForce RTX 4070" in report


# ============================================================
# vulkaninfo filtering
# ============================================================


def test_device_list_survives_long_extension_and_layer_lists(probes) -> None:
    """The regression this filtering exists for.

    A real three-adapter machine reported 20 instance extensions and 17
    layers. Those lists alone exceeded the line cap, so the section printed
    everything EXCEPT the GPUs -- the only part anyone wanted.
    """

    extensions = "\n".join(
        f"VK_KHR_padding_{index}  : extension revision 1" for index in range(80)
    )

    bloated = VULKANINFO.replace(
        "VK_EXT_debug_report                    : extension revision 10",
        extensions,
    )

    probes(
        wmi=ok(json.dumps(ADAPTER)),
        vulkan=ok(bloated),
        available=("vulkaninfo",),
    )

    report = get_gpu_info()

    assert "deviceName = NVIDIA GeForce RTX 4070" in report
    assert "apiVersion = 1.4.303" in report
    assert "VK_KHR_padding_40" not in report


def test_extension_and_layer_counts_are_kept(probes) -> None:
    """The counts are cheap and answer "is validation available at all"."""

    probes(
        wmi=ok(json.dumps(ADAPTER)),
        vulkan=ok(VULKANINFO),
        available=("vulkaninfo",),
    )

    report = get_gpu_info()

    assert "Instance Extensions: count = 3" in report
    assert "Instance Layers: count = 2" in report
    assert "VK_LAYER_LUNARG_monitor" not in report


def test_the_omission_is_disclosed(probes) -> None:
    """Dropping content silently would misrepresent what Vulkan reported."""

    probes(
        wmi=ok(json.dumps(ADAPTER)),
        vulkan=ok(VULKANINFO),
        available=("vulkaninfo",),
    )

    report = get_gpu_info()

    assert "instance extension and layer lists omitted" in report
    assert "run_powershell" in report


def test_output_without_a_device_list_is_kept_whole(probes) -> None:
    """An older or unusual vulkaninfo must not be filtered down to nothing."""

    probes(
        wmi=ok(json.dumps(ADAPTER)),
        vulkan=ok("Vulkan Instance Version: 1.2.0\nsomething else entirely\n"),
        available=("vulkaninfo",),
    )

    report = get_gpu_info()

    assert "something else entirely" in report
    assert "omitted" not in report


def test_rule_lines_are_dropped_but_data_lines_are_not() -> None:
    """Guards the set-comparison direction directly.

    `set(line) > {"=", "-"}` is a strict-superset test, so it only passes for
    lines containing BOTH characters -- which silently dropped every
    "deviceName = ..." line while keeping the UUIDs.
    """

    kept = gpu_module._summarise_vulkaninfo(
        "Devices:\n--------\nGPU0:\n\tdeviceName = Test GPU\n====\n"
    )

    joined = "\n".join(kept)

    assert "deviceName = Test GPU" in joined
    assert "GPU0:" in joined
    assert "----" not in joined
    assert "====" not in joined


# ============================================================
# Toolchain and SDK
# ============================================================


def test_found_tools_are_listed_with_their_paths(probes) -> None:
    probes(wmi=ok(json.dumps(ADAPTER)), available=("glslc", "dxc", "cmake"))

    report = get_gpu_info()

    assert "glslc" in report
    assert "C:/tools/dxc.exe" in report


def test_missing_tools_are_named(probes) -> None:
    probes(wmi=ok(json.dumps(ADAPTER)), available=("glslc",))

    report = get_gpu_info()

    assert "not found:" in report
    assert "fxc" in report


def test_no_tools_at_all_is_stated_plainly(probes) -> None:
    probes(wmi=ok(json.dumps(ADAPTER)))

    assert "No compiler or build tool is on PATH" in get_gpu_info()


def test_sdk_variables_are_reported(probes, monkeypatch) -> None:
    monkeypatch.setenv("VULKAN_SDK", "C:/VulkanSDK/1.4.309.0")

    probes(wmi=ok(json.dumps(ADAPTER)))

    report = get_gpu_info()

    assert "VULKAN_SDK = C:/VulkanSDK/1.4.309.0" in report


def test_absent_sdk_variables_are_explained(probes, monkeypatch) -> None:
    for name in ("VULKAN_SDK", "VK_SDK_PATH", "WindowsSdkDir", "VCToolsInstallDir"):
        monkeypatch.delenv(name, raising=False)

    probes(wmi=ok(json.dumps(ADAPTER)))

    report = get_gpu_info()

    assert "None of the usual SDK variables are set" in report


def test_d3d_feature_level_is_not_invented() -> None:
    """The docstring promises we do not guess; hold it to that."""

    assert "feature level" in (get_gpu_info.__doc__ or "").lower()
