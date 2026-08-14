"""PyInstaller runtime hook for the Phantom backend.

When frozen, sys._MEIPASS is the extraction dir. phantom_ai/config.py computes
ROOT_DIR from __file__ (inside the bundle) and expects ui/ next to it — but the
spec puts ui/ at the bundle ROOT (_MEIPASS/ui), not under phantom_ai/. This hook
injects ROOT_DIR into the config module before it computes UI_DIR.

Also makes the bundled package importable (frozen one-dir layout puts the
package in sys.path automatically; this is a safety net for onefile-style runs).
"""

import os
import sys

_meipass = getattr(sys, "_MEIPASS", None)
if _meipass:
    root = os.path.abspath(_meipass)
    os.environ.setdefault("PHAI_BUNDLE_ROOT", root)
    # ensure the bundle root is importable (contains phantom_ai/)
    if root not in sys.path:
        sys.path.insert(0, root)

    # monkeypatch ROOT_DIR on the config module if it was already imported
    try:
        import phantom_ai.config as _cfg

        _cfg.ROOT_DIR = type("_P", (), {"__fspath__": lambda self: root})()
        # simpler: set directly as string-compatible Path
        from pathlib import Path as _Path

        _cfg.ROOT_DIR = _Path(root)
        # re-derive UI_DIR so the backend finds the bundled web UI
        _cfg.UI_DIR = _Path(root) / "ui"
    except Exception:
        pass
