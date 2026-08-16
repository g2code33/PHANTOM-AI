# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Phantom backend.
#
# Produces a single self-contained executable (dist-backend/phantom-backend,
# or phantom-backend.exe on Windows) that the Electron shell spawns instead of
# relying on a system Python. The web UI (ui/) is bundled as data so the
# backend can serve it from anywhere.
#
# Build:  pyinstaller --noconfirm --distpath dist-backend \
#                     --workpath build/pyinstaller-work build/backend.spec

import os

# PyInstaller resolves paths inside the spec relative to the CURRENT WORKING
# DIRECTORY (where pyinstaller is invoked), NOT the spec file's folder — the
# runtime_hooks path proved that in CI (FileNotFoundError:
# '.../PHANTOM-AI/backend-runtime-hook.py'). Build every path as absolute from
# SPECPATH (the spec's own directory) so the build works from any CWD.
spec_dir = os.path.abspath(SPECPATH)        # .../build
repo_root = os.path.dirname(spec_dir)       # repo root
entry = os.path.join(repo_root, "phantom_ai", "main.py")
ui_dir = os.path.join(repo_root, "ui")
runtime_hook = os.path.join(spec_dir, "backend-runtime-hook.py")

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

# Collect the whole phantom_ai package explicitly so frozen absolute imports
# (from phantom_ai.api.app import App) always resolve regardless of what the
# modulegraph happens to follow from main.py.
phantom_pkg_hidden = [
    "phantom_ai",
    "phantom_ai.config",
    "phantom_ai.main",
    "phantom_ai.api.app",
    "phantom_ai.api.server",
    "phantom_ai.agents.core",
    "phantom_ai.agents.delegation",
    "phantom_ai.agents.identities",
    "phantom_ai.brains.definitions",
    "phantom_ai.brains.health",
    "phantom_ai.brains.registry",
    "phantom_ai.brains.specialists",
    "phantom_ai.core.events",
    "phantom_ai.core.killswitch",
    "phantom_ai.evolution.analyst",
    "phantom_ai.evolution.snapshots",
    "phantom_ai.graph.engine",
    "phantom_ai.health.manager",
    "phantom_ai.health.redflags",
    "phantom_ai.health.vault",
    "phantom_ai.heartbeat.scheduler",
    "phantom_ai.loops.engine",
    "phantom_ai.memory.retriever",
    "phantom_ai.permissions.confirm",
    "phantom_ai.permissions.levels",
    "phantom_ai.permissions.policy",
    "phantom_ai.permissions.validation",
    "phantom_ai.providers",
    "phantom_ai.providers.base",
    "phantom_ai.providers.mock",
    "phantom_ai.providers.nvidia",
    "phantom_ai.providers.router",
    "phantom_ai.storage.conversations",
    "phantom_ai.storage.db",
    "phantom_ai.storage.evolution",
    "phantom_ai.storage.ops",
    "phantom_ai.tasks.manager",
    "phantom_ai.tools",
    "phantom_ai.tools.base",
    "phantom_ai.tools.browser",
    "phantom_ai.tools.clipboard",
    "phantom_ai.tools.delegation_tools",
    "phantom_ai.tools.evolution_tools",
    "phantom_ai.tools.files",
    "phantom_ai.tools.gui",
    "phantom_ai.tools.health_tools",
    "phantom_ai.tools.memory_tools",
    "phantom_ai.tools.notify",
    "phantom_ai.tools.processes",
    "phantom_ai.tools.system",
    "phantom_ai.tools.terminal",
    "phantom_ai.tools.web",
    "phantom_ai.voice.stt",
    "phantom_ai.voice.tts",
]

a = Analysis(
    [entry],
    pathex=[repo_root],
    binaries=[],
    datas=[(ui_dir, "ui")],
    hiddenimports=uvicorn_hidden + phantom_pkg_hidden + [
        "aiosqlite", "cryptography", "jsonschema", "psutil",
        "numpy", "resemblyzer", "multipart", "multipart.multipart",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[runtime_hook],
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
