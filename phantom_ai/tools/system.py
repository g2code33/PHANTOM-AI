"""System information tools (real readings from the local OS)."""

from __future__ import annotations

import asyncio
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


async def _hud_status(ctx: ToolContext) -> ToolResult:
    """Live HUD telemetry: per-core CPU, memory, disk/net I/O rates, battery,
    top process — the same real readings shown on the home screen. Lets
    Phantom/Coded report and regulate the machine (pair with process tools)."""
    import time

    # prime psutil so percentages are real deltas
    psutil.cpu_percent(interval=None)
    psutil.cpu_percent(percpu=True, interval=None)
    await asyncio.sleep(0.15)
    per_core = [round(x, 1) for x in psutil.cpu_percent(percpu=True, interval=None)]
    vm = psutil.virtual_memory()
    net = psutil.net_io_counters()
    disk = psutil.disk_io_counters()
    battery = None
    try:
        b = psutil.sensors_battery()
        if b is not None:
            battery = {"percent": round(b.percent, 1), "plugged": bool(b.power_plugged)}
    except Exception:  # noqa: BLE001
        battery = None
    # top process by one-shot cpu_percent
    top = None
    try:
        rows = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                rows.append((p.cpu_percent(interval=None) or 0, p.pid, p.name()))
            except Exception:  # noqa: BLE001
                continue
        if rows:
            rows.sort(reverse=True)
            top = {"pid": rows[0][1], "name": rows[0][2], "cpu": round(rows[0][0], 1)}
    except Exception:  # noqa: BLE001
        top = None
    info = {
        "cpu_percent_per_core": per_core,
        "cpu_total_percent": round(sum(per_core) / max(len(per_core), 1), 1),
        "cores": len(per_core),
        "memory_percent": round(vm.percent, 1),
        "memory_used_gb": round(vm.used / (1024 ** 3), 2),
        "memory_total_gb": round(vm.total / (1024 ** 3), 2),
        "net_bytes_sent": net.bytes_sent, "net_bytes_recv": net.bytes_recv,
        "disk_read_bytes": disk.read_bytes if disk else None,
        "disk_write_bytes": disk.write_bytes if disk else None,
        "battery": battery,
        "top_cpu_process": top,
        "ts": time.time(),
    }
    text = (
        f"CPU {info['cpu_total_percent']}% (cores: {info['cpu_percent_per_core']}) | "
        f"MEM {info['memory_percent']}% | top: {(top or {}).get('name') or '-'} "
        f"{(top or {}).get('cpu') or 0}% | battery: "
        f"{battery['percent'] if battery else 'n/a'}")
    return ToolResult.ok(text, data=info)


async def _hud_open(ctx: ToolContext, panel: str = "") -> ToolResult:
    """Open a HUD panel on the user's screen (cpu/memory/disk/net/weather/
    moon/system) — the same detail view as clicking the home gauge — and
    return the live data so you can explain it."""
    panel = str(panel or "").strip().lower()
    aliases = {
        "cpu": "cpu", "processor": "cpu", "processors": "cpu", "core": "cpu",
        "memory": "memory", "ram": "memory", "mem": "memory",
        "disk": "disk", "storage": "disk", "drive": "disk", "io": "disk",
        "net": "net", "network": "net", "internet": "net", "wifi": "net",
        "weather": "weather",
        "moon": "moon", "lunar": "moon",
        "system": "system", "battery": "system", "info": "system",
    }
    key = aliases.get(panel, "")
    if not key:
        return ToolResult.fail("Unknown HUD panel. Use: cpu, memory, disk, net, weather, moon, system")
    try:
        events = getattr(ctx, "events", None)
        if events is not None:
            await events.publish("hud.open", {"panel": key})
        hud = getattr(ctx, "hud", None)
        if hud is not None:
            snap = await hud.snapshot()
            return ToolResult.ok(f"Opened the {panel} HUD panel on screen.",
                                 data={"panel": key, "snapshot": snap})
        return ToolResult.ok(f"Opened the {panel} HUD panel on screen.", data={"panel": key})
    except Exception as exc:  # noqa: BLE001
        return ToolResult.fail(f"Could not open HUD panel: {exc}")


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
        name="hud_open", description="Open a HUD panel on the user's screen and read its live data. panel: cpu | memory | disk | net | weather | moon | system.",
        purpose="Show a live system panel to the user", category="system",
        parameters={"panel": {"type": "string", "required": True, "description": "cpu | memory | disk | net | weather | moon | system"}},
        handler=_hud_open, permission=PermissionLevel.READ_ONLY, timeout=10,
    ))
    registry.register(ToolSpec(
        name="hud_status", description="Live HUD telemetry: per-core CPU, memory, disk/net I/O, battery, top CPU process — same real readings as the home screen. Use to report or regulate the machine (pair with process tools).",
        purpose="Monitor the machine live", category="system",
        parameters={}, handler=_hud_status, permission=PermissionLevel.READ_ONLY, timeout=10,
    ))
    registry.register(ToolSpec(
        name="network_info", description="List network interfaces and their state/IPs.",
        purpose="Inspect networking", category="system",
        parameters={}, handler=_network_info, permission=PermissionLevel.READ_ONLY, timeout=10,
    ))
