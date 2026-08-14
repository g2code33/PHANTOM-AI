"""System information tools (real readings from the local OS)."""

from __future__ import annotations

import os
import platform
import shutil
import socket

import psutil

from .base import PermissionLevel, ToolContext, ToolResult, ToolSpec


async def _system_info(ctx: ToolContext) -> ToolResult:
    info = {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu": platform.processor() or platform.machine(),
        "cpu_cores": psutil.cpu_count(logical=True),
        "memory_total_gb": round(psutil.virtual_memory().total / (1024 ** 3), 2),
        "disk_root_gb": round(shutil.disk_usage("/").total / (1024 ** 3), 2),
        "user": os.environ.get("USER") or os.environ.get("USERNAME", "?"),
        "uptime_s": int(psutil.boot_time() and __import__("time").time() - psutil.boot_time()),
    }
    return ToolResult.ok(
        "\n".join(f"{k}: {v}" for k, v in info.items()), data=info)


async def _system_resources(ctx: ToolContext) -> ToolResult:
    vm = psutil.virtual_memory()
    info = {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "memory_percent": vm.percent,
        "memory_used_gb": round(vm.used / (1024 ** 3), 2),
        "memory_total_gb": round(vm.total / (1024 ** 3), 2),
        "load_avg": [round(x, 2) for x in os.getloadavg()],
        "disk_percent": psutil.disk_usage("/").percent,
    }
    return ToolResult.ok(
        "CPU: {cpu_percent}% | MEM: {memory_percent}% | DISK: {disk_percent}%".format(**info),
        data=info)


async def _network_info(ctx: ToolContext) -> ToolResult:
    ifaddrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    out = []
    for iface, addrs in ifaddrs.items():
        stat = stats.get(iface)
        up = "UP" if stat and stat.isup else "DOWN"
        ips = [a.address for a in addrs if a.family == socket.AF_INET]
        out.append({"interface": iface, "state": up, "ipv4": ips})
    return ToolResult.ok(
        "\n".join(f"{i['interface']:12} {i['state']:6} {','.join(i['ipv4']) or '-'}" for i in out),
        data={"interfaces": out})


def register_system_tools(registry) -> None:
    registry.register(ToolSpec(
        name="system_info", description="Get OS, hardware, disk, and user information for this computer.",
        purpose="Read system information", category="system",
        parameters={}, handler=_system_info, permission=PermissionLevel.READ_ONLY, timeout=10,
    ))
    registry.register(ToolSpec(
        name="system_resources", description="Get live CPU, memory, load and disk usage.",
        purpose="Monitor resources", category="system",
        parameters={}, handler=_system_resources, permission=PermissionLevel.READ_ONLY, timeout=10,
    ))
    registry.register(ToolSpec(
        name="network_info", description="List network interfaces and their state/IPs.",
        purpose="Inspect networking", category="system",
        parameters={}, handler=_network_info, permission=PermissionLevel.READ_ONLY, timeout=10,
    ))
