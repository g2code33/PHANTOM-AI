"""Shared fixtures: mock NVIDIA server + full application instance."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

import pytest
import uvicorn

from mock_nvidia import MockState, make_mock_app


@pytest.fixture
async def mock_server():
    state = MockState()
    config = uvicorn.Config(make_mock_app(state), host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started, "mock server failed to start"
    port = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}/v1"  # mirrors the real NVIDIA NIM layout
    yield state, base_url
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=10)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        task.cancel()


@pytest.fixture
def workdir():
    """A temp dir under the real home so path-safety rules allow it."""
    d = tempfile.mkdtemp(prefix=".pcai-test-", dir=os.path.expanduser("~"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
async def app(mock_server, workdir):
    state, base_url = mock_server
    os.environ["PHANTOM_NVIDIA_API_KEY"] = "nvapi-test-phantom-key-0000000000"
    os.environ["CODED_NVIDIA_API_KEY"] = "nvapi-test-coded-key-0000000000"
    os.environ["EVOLUTION_NVIDIA_API_KEY"] = "nvapi-test-evolution-key-0000000000"
    os.environ["HEALTH_NVIDIA_API_KEY"] = "nvapi-test-health-key-0000000000"
    os.environ["PHANTOM_NVIDIA_BASE_URL"] = base_url
    os.environ["CODED_NVIDIA_BASE_URL"] = base_url
    os.environ["EVOLUTION_NVIDIA_BASE_URL"] = base_url
    os.environ["HEALTH_NVIDIA_BASE_URL"] = base_url

    from phantom_ai.api.app import App

    db_path = os.path.join(workdir, "test.db")
    data_dir = os.path.join(workdir, "data")
    instance = App(db_path=db_path, data_dir=data_dir, workspace_root=workdir)
    await instance.startup()
    yield instance, state, workdir
    await instance.shutdown()
    for var in ("PHANTOM_NVIDIA_API_KEY", "CODED_NVIDIA_API_KEY",
                "EVOLUTION_NVIDIA_API_KEY", "HEALTH_NVIDIA_API_KEY",
                "PHANTOM_NVIDIA_BASE_URL", "CODED_NVIDIA_BASE_URL",
                "EVOLUTION_NVIDIA_BASE_URL", "HEALTH_NVIDIA_BASE_URL"):
        os.environ.pop(var, None)


class _CtxWrap:
    """Wraps a ToolContext and exposes registry_get(name)."""

    def __init__(self, ctx):
        object.__setattr__(self, "_ctx", ctx)

    def __getattr__(self, item):
        return getattr(self._ctx, item)

    def registry_get(self, name):
        return self._ctx.registry.get(name)


@pytest.fixture
async def tool_ctx(app):
    """A ready-to-use ToolContext bound to the test app."""
    from phantom_ai.tools.base import ToolContext

    instance, _state, workdir = app

    def factory(agent="phantom"):
        return _CtxWrap(ToolContext(
            agent_id=agent, conversation_id="test-conv", session_id="test-session",
            data_dir=os.path.join(workdir, "data"), workspace_root=workdir,
            db=instance.db, registry=instance.registry, settings=instance.settings,
            permission_manager=instance.permissions, audit=instance.audit,
            events=instance.events, memory_store=instance.memories,
            conversation_store=instance.conversations,
            delegation_manager=instance.delegations, secrets=instance.secrets,
            confirmations=instance.confirmations, notifications=instance.notifications,
            brain_registry=instance.brains, graph=instance.graph,
            proposals=instance.proposals, snapshots=instance.snapshots,
            loop_engine=instance.loops, verifier=instance.verifier,
            analyst=instance.analyst, agent_ids=("phantom", "coded", "evolution"),
            health=instance.health, profiles=instance.profiles,
            briefing=instance.briefing, monitor=instance.monitor,
        ))

    yield factory
