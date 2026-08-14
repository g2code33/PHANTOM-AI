"""Entry point: `python -m phantom_ai.main` starts the Phantom app.

Works BOTH as a module (`python -m phantom_ai.main`, used in dev and by the
Electron dev fallback) AND as a frozen PyInstaller script (the packaged app).
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys

import uvicorn

# PyInstaller runs this file as a standalone script, so relative imports fail
# ("attempted relative import with no known parent package"). Import the
# package absolutely instead; when frozen, the package is bundled alongside.
if bool(getattr(sys, "frozen", False)):
    from phantom_ai.api.app import App
    from phantom_ai.api.server import create_app
else:
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


async def serve(app: App | None = None, port_file: str | None = None) -> None:
    app = app or build_app()
    await app.startup()
    if app.scheduler:
        await app.scheduler.start()
    port = free_port(PORT)
    if port_file:
        path = os.path.abspath(os.path.expanduser(port_file))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(port))
    config = uvicorn.Config(create_app(app), host=HOST, port=port, log_level="info")
    server = uvicorn.Server(config)
    print(f"\n  Phantom running at http://{HOST}:{port}")
    print(f"  DB: {app.db.path}")
    try:
        await server.serve()
    finally:
        await app.shutdown()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="PHANTOM + CODED backend")
    parser.add_argument("--port-file", default="",
                        help="write the bound port to this file once the server is up "
                             "(used by the Electron shell)")
    args = parser.parse_args()
    asyncio.run(serve(port_file=args.port_file or None))


if __name__ == "__main__":
    main()
