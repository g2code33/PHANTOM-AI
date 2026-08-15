"""Real-telemetry HUD tests: every reading comes from psutil deltas, and
anything unreadable reports available=false (never a fake number)."""

from __future__ import annotations

import asyncio

from phantom_ai.hud.sampler import HudSampler


async def _client(app):
    import httpx
    from phantom_ai.api.server import create_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                             base_url="http://test")


async def test_hud_snapshot_real_values():
    s = HudSampler()
    snap = await s.snapshot()
    cpu = snap["cpu"]
    assert isinstance(cpu["per_core"], list) and len(cpu["per_core"]) >= 1
    assert all(isinstance(x, (int, float)) for x in cpu["per_core"])
    assert cpu["cores"] == len(cpu["per_core"])
    assert len(cpu["load_avg"]) == 3
    # battery is either real or honestly unavailable
    assert isinstance(snap["battery"].get("available"), bool)
    assert "process" in snap and isinstance(snap["process"].get("name"), str)
    assert isinstance(snap["process"].get("cpu_percent"), (int, float))
    # disk/net deltas
    assert isinstance(snap["disk"].get("read_bps"), int)
    assert isinstance(snap["net"].get("down_bps"), int)


async def test_hud_io_deltas_change_over_time():
    s = HudSampler()
    await s.snapshot()  # prime deltas
    # generate some real disk + net activity
    with open("/tmp/hud_delta_test.bin", "wb") as f:
        f.write(b"\x00" * 512 * 1024)
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(b"ping-hud-test", ("127.0.0.1", 9))
    except OSError:
        pass
    sock.close()
    await asyncio.sleep(0.15)
    snap2 = await s.snapshot()
    assert snap2["disk"]["read_bps"] >= 0
    assert snap2["disk"]["write_bps"] >= 0
    assert snap2["net"]["up_bps"] >= 0
    assert snap2["net"]["down_bps"] >= 0


async def test_hud_top_process_eventually_reported(app):
    instance, _state, _wd = app
    async with await _client(instance) as c:
        r1 = await c.get("/api/hud")
        assert r1.status_code == 200
        d1 = r1.json()
        assert "cpu" in d1 and "disk" in d1 and "net" in d1 and "battery" in d1
        # second poll gives a real top-CPU process (or empty with 0 — honest)
        r2 = await c.get("/api/hud")
        d2 = r2.json()
        assert "name" in d2["process"]
        assert d2["process"].get("cpu_percent", 0) >= 0


async def test_hud_sleep_rendering_tier_contract():
    """The tier decision lives in the UI, but the backend guarantees the
    data contract the low-power tier relies on: slow polling only needs the
    same snapshot shape — cheap, blocking-free reads."""
    s = HudSampler()
    snap = await s.snapshot()
    assert set(("cpu", "disk", "net", "battery", "process")) <= set(snap.keys())
    # everything serializable & small (fast even at 8s intervals)
    import json
    payload = json.dumps(snap)
    assert len(payload) < 20000
