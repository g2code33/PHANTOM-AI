# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the PHANTOM + CODED backend.
#
# Produces a single self-contained executable (dist-backend/phantom-backend,
# or phantom-backend.exe on Windows) that the Electron shell spawns instead of
# relying on a system Python. The web UI (ui/) is bundled as data so the
# backend can serve it from anywhere.
#
# Build:  pyinstaller --noconfirm --distpath dist-backend \
#                     --workpath build/pyinstaller-work build/backend.spec

import os

# uvicorn selects its loop/http/ws implementations at runtime via importlib,
# which PyInstaller cannot see statically — declare them explicitly.
uvicorn_hidden = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.middleware.wsgi",
    "uvicorn.middleware.proxy_headers",
    "uvicorn.middleware.message_logger",
    "uvicorn.middleware.debug",
]

a = Analysis(
    ["../phantom_ai/main.py"],
    pathex=[".."],
    binaries=[],
    datas=[("../ui", "ui")],
    hiddenimports=uvicorn_hidden + ["aiosqlite", "cryptography", "jsonschema", "psutil"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # GUI/browser automation is an optional, lazily-imported capability;
        # keeping it out of the bundle avoids heavy/compiled deps. The tools
        # degrade honestly at runtime if the user installs them separately.
        "pyautogui", "mss", "playwright", "PIL", "pygetwindow",
        # uvicorn[standard] accelerators: fall back to pure-python asyncio/h11
        "uvloop", "httptools", "watchfiles",
        # dev/test only
        "pytest", "setuptools",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="phantom-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="phantom-backend",
)
