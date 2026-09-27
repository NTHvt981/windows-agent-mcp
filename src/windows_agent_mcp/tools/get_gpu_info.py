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

_WMI_TIMEOUT_SECONDS = 20
_VULKANINFO_TIMEOUT_SECONDS = 20

_MAX_VULKAN_LINES = 60

# "/Date(1719705600000)/" -- ConvertTo-Json's DateTime form.
_WMI_DATE_PATTERN = re.compile(r"^/Date\((-?\d+)\)/$")

_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}")

_VULKAN_DEVICES_MARKER = "Devices:"

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
    """Report the GPU adapters, driver versions and graphics toolchain."""

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
    """Run a PowerShell expression that emits JSON, returning parsed data."""

    # Fixed literals only, so the run_powershell allowlist does not apply.

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
    """Render a WMI driver date as YYYY-MM-DD, or "" if it cannot be read."""

    # ConvertTo-Json emits /Date(ms)/, not ISO 8601.

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

    # ConvertTo-Json emits an object for one adapter, an array for several.
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
            # AdapterRAM is 32-bit, so >= 4 GB reports as ~4095 MB.
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
    """Keep the instance version and the device list, drop the long lists."""

    lines = [line.rstrip() for line in output.splitlines()]

    def useful(line: str) -> bool:
        """Keep any non-blank line that is not just a rule."""

        # Subset, not superset: the latter drops deviceName lines.

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
