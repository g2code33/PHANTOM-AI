"""Entry point: `python -m phantom_ai.main` starts the PHANTOM + CODED app."""

from __future__ import annotations

import asyncio
import os
import socket

import uvicorn

from .api.app import App
from .api.server import create_app

HOST = os.environ.get("PHAI_HOST", "0.0.0.0")
PORT = int(os.environ.get("PHAI_PORT", os.environ.get("PORT", "8000")))


def free_port(preferred: int) -> int:
    if preferred and _port_free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def build_app() -> App:
    return App()


async def serve(app: App | None = None) -> None:
    app = app or build_app()
    await app.startup()
    if app.scheduler:
        await app.scheduler.start()
    port = free_port(PORT)
    config = uvicorn.Config(create_app(app), host=HOST, port=port, log_level="info")
    server = uvicorn.Server(config)
    print(f"\n  PHANTOM + CODED running at http://{HOST}:{port}")
    print(f"  DB: {app.db.path}")
    try:
        await server.serve()
    finally:
        await app.shutdown()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
