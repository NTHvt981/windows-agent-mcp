"""Report GPU adapters and the graphics toolchain available on this machine."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..error import mcp_error
from ..log import log
from ..process import get_windows_development_environment, run_process

__all__: list[str] = ["get_gpu_info"]

# Probes are separate processes and each one can hang on a broken driver, so
# every call is individually bounded. Short: this is an orientation tool, and
# a model waiting thirty seconds for it has already lost.
_WMI_TIMEOUT_SECONDS = 20
_VULKANINFO_TIMEOUT_SECONDS = 20

# Cap on reproduced vulkaninfo lines, applied after the filtering below.
_MAX_VULKAN_LINES = 60

# "/Date(1719705600000)/" -- ConvertTo-Json's DateTime form.
_WMI_DATE_PATTERN = re.compile(r"^/Date\((-?\d+)\)/$")

_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}")

# vulkaninfo --summary leads with the instance extension and layer lists, which
# on a real machine run to forty-odd lines of "VK_KHR_x : extension revision 1"
# before reaching the device list. The device list is the part that answers the
# question -- deviceName, apiVersion, driverVersion -- so the lists are dropped
# and their counts kept. Observed first-hand: a three-adapter machine with 20
# extensions and 17 layers pushed the devices past the line cap entirely, so
# the section reported everything except the GPUs.
_VULKAN_DEVICES_MARKER = "Devices:"

# Executables worth knowing about, in the order they matter for graphics work.
_TOOLCHAIN = (
    "glslc",
    "glslangValidator",
    "dxc",
    "fxc",
    "spirv-val",
    "spirv-dis",
    "cmake",
    "ninja",
    "cl",
    "clang",
    "clang++",
    "msbuild",
)

# Environment variables that locate the SDKs these tools come from.
_SDK_VARIABLES = (
    "VULKAN_SDK",
    "VK_SDK_PATH",
    "WindowsSdkDir",
    "WindowsSDKVersion",
    "WindowsSdkVerBinPath",
    "VCToolsInstallDir",
    "VCINSTALLDIR",
    "DXSDK_DIR",
)


def get_gpu_info() -> str:
    """Report the GPU adapters, driver versions and graphics toolchain.

    Answers the questions a renderer needs settled before generating code:
    which adapter is present, which driver, whether the Vulkan SDK and the
    shader compilers are installed, and which Vulkan API version the driver
    reports. Without this a model guesses at extension and feature support,
    and code that compiles then fails at device creation.

    Reads the adapter list through WMI and, when vulkaninfo is installed,
    Vulkan's own view of the device. Both probes are individually
    time-limited, so a broken driver degrades one section rather than failing
    the call.

    Note: Direct3D feature levels are NOT reported. Obtaining them requires
    creating a D3D12 device, which this server does not do -- so a feature
    level here would be a guess. Query it from the application instead.

    Args:
        None

    Returns:
        A plain-text report in sections. Sections that could not be probed say
        so explicitly rather than being omitted, so absence of information is
        never mistaken for absence of hardware.

    Example:
        >>> get_gpu_info()
        'GPU ADAPTERS\\n  NVIDIA GeForce RTX 4070  driver 32.0.15.6094\\n...'
    """

    try:
        environment = get_windows_development_environment()

        sections = [
            _adapter_section(),
            _vulkan_section(environment),
            _toolchain_section(environment),
            _sdk_section(),
        ]

        return "\n\n".join(section for section in sections if section)

    except Exception as exc:  # pragma: no cover - defensive
        log.exception("get_gpu_info failed")

        return mcp_error(
            "GPU_INFO_FAILED",
            "get_gpu_info",
            f"Could not gather GPU information: {exc}",
            recovery=[
                "Do not repeatedly retry.",
                "Use get_system_info for basic machine details instead.",
            ],
        )


def _powershell_json(command: str, *, timeout_seconds: int) -> Any | None:
    """Run a PowerShell expression that emits JSON, returning parsed data.

    This bypasses the run_powershell allowlist deliberately: the command is a
    fixed literal in this file, not caller input, so there is nothing for the
    allowlist to protect against. Passing it through the allowlist would just
    mean adding Get-CimInstance to a set that governs model-supplied strings.
    """

    try:
        result = run_process(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            cwd=Path.cwd(),
            timeout_seconds=timeout_seconds,
        )
    except OSError:
        log.warning("get_gpu_info could not run powershell.exe")
        return None

    if result.timed_out or result.exit_code != 0 or not result.stdout.strip():
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("get_gpu_info got non-JSON from WMI probe")
        return None


def _driver_date(value: object) -> str:
    """Render a WMI driver date as YYYY-MM-DD, or "" if it cannot be read.

    ConvertTo-Json does NOT emit ISO 8601 for a DateTime. It emits Microsoft's
    JSON date form, "/Date(1719705600000)/", where the number is milliseconds
    since the Unix epoch. Slicing the first ten characters of that -- which is
    what an ISO assumption produces -- yields "/Date(171", which is how this
    was first shipped.
    """

    if not isinstance(value, str) or not value:
        return ""

    match = _WMI_DATE_PATTERN.match(value.strip())

    if match is not None:
        try:
            stamp = int(match.group(1)) / 1000
            return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return ""

    # Some PowerShell versions and -AsUTC do emit ISO 8601.
    if _ISO_DATE_PATTERN.match(value):
        return value[:10]

    return ""


def _adapter_section() -> str:
    """List video controllers as WMI sees them."""

    data = _powershell_json(
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,DriverVersion,DriverDate,AdapterRAM,"
        "VideoProcessor,CurrentHorizontalResolution,"
        "CurrentVerticalResolution | ConvertTo-Json -Depth 3",
        timeout_seconds=_WMI_TIMEOUT_SECONDS,
    )

    if data is None:
        return (
            "GPU ADAPTERS\n"
            "  Could not query WMI. The adapter list is unknown -- this is a "
            "probe failure, not an absence of hardware."
        )

    # ConvertTo-Json emits a bare object for a single adapter and an array for
    # several, so a one-GPU machine would otherwise crash the iteration.
    adapters = data if isinstance(data, list) else [data]

    lines = ["GPU ADAPTERS"]

    for adapter in adapters:
        if not isinstance(adapter, dict):
            continue

        entry: dict[str, Any] = adapter

        name = entry.get("Name") or "(unnamed adapter)"
        parts = [f"  {name}"]

        driver = entry.get("DriverVersion")

        if driver:
            parts.append(f"driver {driver}")

        date = _driver_date(entry.get("DriverDate"))

        if date:
            parts.append(f"({date})")

        lines.append("  ".join(parts))

        processor = entry.get("VideoProcessor")

        if processor and processor != name:
            lines.append(f"      chip: {processor}")

        width = entry.get("CurrentHorizontalResolution")
        height = entry.get("CurrentVerticalResolution")

        if width and height:
            lines.append(f"      current mode: {width}x{height}")

        ram = entry.get("AdapterRAM")

        if isinstance(ram, int) and ram > 0:
            # WMI's AdapterRAM is a 32-bit field, so anything at or above 4 GB
            # reports as ~4095 MB. Saying so is more useful than printing a
            # number the model would reason from.
            gigabytes = ram / (1024**3)

            if gigabytes >= 3.9:
                lines.append(
                    "      reported VRAM: >= 4 GB "
                    "(WMI caps this field at 4 GB; the real figure is higher)"
                )
            else:
                lines.append(f"      reported VRAM: {gigabytes:.1f} GB")

    if len(lines) == 1:
        lines.append("  No video controllers reported.")

    return "\n".join(lines)


def _vulkan_section(environment: dict[str, str]) -> str:
    """Report Vulkan's own view of the devices, if vulkaninfo is installed."""

    executable = shutil.which("vulkaninfo", path=environment.get("PATH"))

    if executable is None:
        return (
            "VULKAN\n"
            "  vulkaninfo not found, so the driver's Vulkan API version and "
            "device list are unknown.\n"
            "  Install the Vulkan SDK to make this section available."
        )

    try:
        result = run_process(
            [executable, "--summary"],
            cwd=Path.cwd(),
            timeout_seconds=_VULKANINFO_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        return f"VULKAN\n  Could not run vulkaninfo: {exc}"

    if result.timed_out:
        return (
            "VULKAN\n"
            "  vulkaninfo timed out, which usually means a broken or "
            "partially installed driver."
        )

    if result.exit_code != 0 and not result.stdout.strip():
        return (
            f"VULKAN\n"
            f"  vulkaninfo failed (exit {result.exit_code}). "
            f"No Vulkan-capable device may be present."
        )

    interesting = _summarise_vulkaninfo(result.stdout)

    if not interesting:
        return "VULKAN\n  vulkaninfo produced no output."

    return "VULKAN (vulkaninfo --summary)\n" + "\n".join(interesting)


def _summarise_vulkaninfo(output: str) -> list[str]:
    """Keep the instance version and the device list, drop the long lists.

    Args:
        output: Raw vulkaninfo --summary stdout.

    Returns:
        Indented report lines, capped. Empty when there was nothing to show.
    """

    lines = [line.rstrip() for line in output.splitlines()]

    def useful(line: str) -> bool:
        """Keep any non-blank line that is not just a "====" / "----" rule.

        The comparison direction matters: `set(stripped) > {"=", "-"}` is a
        strict-superset test, which requires the line to contain BOTH those
        characters, and so silently dropped every "deviceName = ..." line.
        """

        stripped = line.strip()

        return bool(stripped) and not set(stripped) <= {"=", "-"}

    devices_at = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().startswith(_VULKAN_DEVICES_MARKER)
        ),
        None,
    )

    if devices_at is None:
        # No device list to protect, so keep everything and let the cap apply.
        kept = [line.strip() for line in lines if useful(line)]
        omitted = False
    else:
        head = [
            line.strip()
            for line in lines[:devices_at]
            if useful(line)
            and (
                line.strip().startswith("Vulkan Instance Version") or "count =" in line
            )
        ]

        body = [line.strip() for line in lines[devices_at:] if useful(line)]

        kept = head + body
        omitted = True

    report = [f"  {line}" for line in kept[:_MAX_VULKAN_LINES]]

    if len(kept) > _MAX_VULKAN_LINES:
        report.append("  ...[vulkaninfo output truncated]...")

    if omitted and report:
        report.append(
            "  (instance extension and layer lists omitted; run "
            "'vulkaninfo' via run_powershell for the full dump)"
        )

    return report


def _toolchain_section(environment: dict[str, str]) -> str:
    """Report which graphics and build executables are on PATH."""

    lines = ["GRAPHICS TOOLCHAIN"]

    missing: list[str] = []

    for name in _TOOLCHAIN:
        found = shutil.which(name, path=environment.get("PATH"))

        if found:
            lines.append(f"  {name:<18} {found}")
        else:
            missing.append(name)

    if len(lines) == 1:
        lines.append("  Nothing found. No compiler or build tool is on PATH.")

    if missing:
        lines.append(f"  not found: {', '.join(missing)}")

    return "\n".join(lines)


def _sdk_section() -> str:
    """Report SDK locations from the environment."""

    present = [
        f"  {name} = {os.environ[name]}"
        for name in _SDK_VARIABLES
        if os.environ.get(name)
    ]

    if not present:
        return (
            "SDK ENVIRONMENT\n"
            "  None of the usual SDK variables are set (VULKAN_SDK, "
            "WindowsSdkDir, VCToolsInstallDir).\n"
            "  Tools may still work if they are on PATH; a developer command "
            "prompt sets these."
        )

    return "SDK ENVIRONMENT\n" + "\n".join(present)
