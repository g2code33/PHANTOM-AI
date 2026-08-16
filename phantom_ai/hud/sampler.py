"""Real system telemetry for the JARVIS/Stark HUD.

Every value comes from psutil (the same library the process tools and the
monitor engine already use) — nothing is simulated. Readings that cannot be
obtained on a given machine (e.g. battery on a desktop) report
`available: false` with a short reason instead of a fake number.

CPU is delta-based per core, disk/net I/O are byte-rate deltas between polls,
and the top process is the process with the highest *measured* CPU usage
between samples (not a guess).
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import psutil


class HudSampler:
    """Stateful sampler: keeps previous samples so deltas are real."""

    def __init__(self) -> None:
        # prime psutil so the FIRST reported CPU percentages are already real
        # (psutil's interval=None returns % since the previous call)
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(percpu=True, interval=None)
        self._last_io: Optional[tuple[float, Any, Any]] = None  # (ts, disk, net)
        self._proc_samples: dict[int, tuple[Any, float, str]] = {}  # pid -> (times, ts, name)

    async def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ts": time.time()}
        out["cpu"] = self._cpu()
        out["disk"] = self._disk()
        out["net"] = self._net()
        out["battery"] = self._battery()
        out["process"] = self._top_process()
        return out

    # ------------------------------------------------------------------
    def _cpu(self) -> dict[str, Any]:
        try:
            per_core = psutil.cpu_percent(percpu=True, interval=None)
            total = round(sum(per_core) / max(len(per_core), 1), 1)
            return {
                "per_core": [round(x, 1) for x in per_core],
                "total": total,
                "cores": len(per_core),
                "load_avg": [round(x, 2) for x in os.getloadavg()],
            }
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": str(exc)[:120]}

    def _disk(self) -> dict[str, Any]:
        try:
            now = time.time()
            io = psutil.disk_io_counters()
            if io is None:
                return {"available": False, "reason": "no disk I/O counters on this system"}
            prev_disk = None if self._last_io is None else self._last_io[1]
            prev_net = None if self._last_io is None else self._last_io[2]
            result: dict[str, Any] = {"read_bps": 0, "write_bps": 0}
            if prev_disk is not None:
                dt = max(now - self._last_io[0], 0.001)
                result = {
                    "read_bps": int(max(0, io.read_bytes - prev_disk.read_bytes) / dt),
                    "write_bps": int(max(0, io.write_bytes - prev_disk.write_bytes) / dt),
                }
            self._last_io = (now, io, prev_net)
            return result
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": str(exc)[:120]}

    def _net(self) -> dict[str, Any]:
        try:
            now = time.time()
            io = psutil.net_io_counters()
            prev_disk = None if self._last_io is None else self._last_io[1]
            prev_net = None if self._last_io is None else self._last_io[2]
            result: dict[str, Any] = {"up_bps": 0, "down_bps": 0}
            if prev_net is not None:
                dt = max(now - self._last_io[0], 0.001)
                result = {
                    "up_bps": int(max(0, io.bytes_sent - prev_net.bytes_sent) / dt),
                    "down_bps": int(max(0, io.bytes_recv - prev_net.bytes_recv) / dt),
                }
            self._last_io = (now, prev_disk, io)
            return result
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": str(exc)[:120]}

    def _battery(self) -> dict[str, Any]:
        try:
            b = psutil.sensors_battery()
            if b is None:
                return {"available": False, "reason": "no battery on this device"}
            return {
                "available": True,
                "percent": round(b.percent, 1),
                "plugged": bool(b.power_plugged),
                "seconds_left": b.secsleft if b.secsleft not in (
                    psutil.POWER_TIME_UNKNOWN, psutil.POWER_TIME_UNLIMITED) else None,
            }
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": str(exc)[:120]}

    def _top_process(self) -> dict[str, Any]:
        """Highest *measured* CPU process between polls (first poll: no data)."""
        try:
            now = time.time()
            candidates: list[tuple[float, int, str]] = []
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    ct = p.cpu_times()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
                prev = self._proc_samples.get(p.pid)
                try:
                    name = p.name()
                except Exception:  # noqa: BLE001
                    name = "?"
                self._proc_samples[p.pid] = (ct, now, name)
                if prev is not None:
                    dt = max(now - prev[1], 0.001)
                    dcpu = max(0.0, (ct.user + ct.system) - (prev[0].user + prev[0].system))
                    candidates.append((dcpu / dt * 100.0, p.pid, name))
            if len(self._proc_samples) > 512:
                live = {pid for _, pid, _ in candidates}
                self._proc_samples = {pid: v for pid, v in self._proc_samples.items()
                                      if pid in live}
            if not candidates:
                return {"name": "", "pid": 0, "cpu_percent": 0.0}
            pct, pid, name = max(candidates)
            return {"name": name, "pid": pid, "cpu_percent": round(pct, 1)}
        except Exception as exc:  # noqa: BLE001
            return {"name": "", "cpu_percent": 0.0, "unavailable": True,
                    "reason": str(exc)[:120]}
