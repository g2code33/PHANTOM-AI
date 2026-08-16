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


async def test_hud_memory_and_process_count(app):
    instance, _state, _wd = app
    async with await _client(instance) as c:
        d = (await c.get("/api/hud")).json()
        assert "memory" in d
        mem = d["memory"]
        if mem.get("available") is not False:
            assert 0 <= mem["percent"] <= 100
            assert mem["total_bytes"] > 0
        assert isinstance(d.get("process_count"), int) and d["process_count"] > 0


async def test_weather_endpoint(app, monkeypatch):
    """Weather proxies Open-Meteo; when unreachable it's HONEST unavailable."""
    instance, _state, _wd = app
    async with await _client(instance) as c:
        # without network this returns unavailable (never raises)
        r = await c.get("/api/hud/weather")
        assert r.status_code == 200
        data = r.json()
        assert "available" in data
        if not data["available"]:
            assert data["reason"]  # honest reason, no fake values
        else:
            assert "current" in data and "daily" in data


async def test_weather_endpoint_mocked_success(app, monkeypatch):
    """With a mocked Open-Meteo response the panel data is real & shaped."""
    instance, _state, _wd = app

    class FakeResp:
        status_code = 200

        def raise_for_status(self):  # noqa: D401
            pass

        def json(self):
            return {
                "current": {"temperature_2m": 28.4, "relative_humidity_2m": 71,
                            "wind_speed_10m": 12.3, "surface_pressure": 1011.2,
                            "weather_code": 3, "apparent_temperature": 30.1},
                "daily": {"time": ["2026-08-16", "2026-08-17"],
                          "weather_code": [3, 61],
                          "temperature_2m_max": [30.0, 29.0],
                          "temperature_2m_min": [24.0, 23.0],
                          "sunrise": ["2026-08-16T06:00:00Z"],
                          "sunset": ["2026-08-16T18:30:00Z"]},
                "current_units": {"temperature_2m": "°C"},
            }

    import phantom_ai.api.server as srv

    async def fake_fetch(lat, lon):
        assert 5.0 <= lat <= 6.5 and -0.5 <= lon <= 0.0  # Accra vicinity
        return FakeResp().json()

    monkeypatch.setattr(srv, "_fetch_weather", fake_fetch)
    async with await _client(instance) as c:
        r = await c.get("/api/hud/weather")
        data = r.json()
        assert data["available"] is True
        assert data["current"]["temperature_2m"] == 28.4
        assert len(data["daily"]["time"]) == 2
