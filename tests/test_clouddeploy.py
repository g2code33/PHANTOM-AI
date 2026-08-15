"""Jarvis P12 — redeploy cloud from the app: runs cloud/deploy.sh as a tracked
task, streams logs as WS events, passes secrets via env only."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from phantom_ai.clouddeploy.service import CloudDeploy


async def test_deploy_runs_script_and_logs(app):
    instance, _state, _wd = app
    # create a fake deploy.sh that echoes env keys and exits 0
    tmp = Path(tempfile.mkdtemp(dir=os.path.expanduser("~")))
    script = tmp / "cloud" / "deploy.sh"
    script.parent.mkdir(parents=True)
    script.write_text(
        "#!/bin/bash\n"
        'echo "deploy-start"\n'
        'echo "NVIDIA set: ${NVIDIA_API_KEY:+yes}"\n'
        'echo "TOKEN set: ${PHANTOM_CLOUD_TOKEN:+yes}"\n'
        'echo "deploy-done"\n'
    )
    script.chmod(0o755)

    sub = await instance.events.subscribe("deploy-test")
    service = CloudDeploy(instance.secrets, instance.tasks, instance.events,
                          instance.audit, repo_root=str(tmp))
    task = await service.deploy(nvidia_key="nvkey1", cloud_token="tok1")
    assert task["status"] in ("queued", "running")

    # wait for completion
    for _ in range(100):
        t = await instance.tasks.get(task["id"])
        if t and t["status"] == "completed":
            break
        await asyncio.sleep(0.05)
    t = await instance.tasks.get(task["id"])
    assert t["status"] == "completed"
    logs = t["logs"]
    assert any("deploy-start" in l for l in logs)
    assert any("deploy-done" in l for l in logs)
    # secrets passed via env, not in logs
    joined = "\n".join(logs)
    assert "nvkey1" not in joined and "tok1" not in joined
    assert "NVIDIA set: yes" in joined and "TOKEN set: yes" in joined

    # WS events streamed
    seen = []
    try:
        while True:
            ev = await asyncio.wait_for(sub.get(), timeout=0.3)
            if ev["event"] == "cloud.deploy_log":
                seen.append(ev["data"]["line"])
    except asyncio.TimeoutError:
        pass
    assert any("deploy-done" in l for l in seen)

    await instance.events.unsubscribe("deploy-test")


async def test_deploy_missing_script_raises(app):
    instance, _state, _wd = app
    service = CloudDeploy(instance.secrets, instance.tasks, instance.events,
                          instance.audit, repo_root="/nonexistent")
    with pytest.raises(RuntimeError):
        await service.deploy()


async def test_deploy_api_endpoint(app):
    import httpx
    from phantom_ai.api.server import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(instance_app(app))),
        base_url="http://test") as client:
        r = await client.post("/api/cloud/deploy", json={})
        # no script in the sandbox repo → either 400 or 200 with a task that
        # fails; accept 400 (missing script) since deploy.sh exists here
        assert r.status_code in (200, 400)


def instance_app(app):
    return app[0]
