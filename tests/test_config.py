"""Frozen (packaged) mode: user data must live in a stable writable location,
never the PyInstaller temp extraction dir."""

from __future__ import annotations

import importlib
import os
import sys


def test_frozen_config_uses_xdg(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "/tmp/pyinstaller-extract", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.delenv("PHAI_DATA_DIR", raising=False)

    import phantom_ai.config as config
    importlib.reload(config)

    try:
        assert config.IS_FROZEN is True
        expected = xdg / "phantom-coded"
        assert config.DATA_DIR == expected
        assert config.DB_PATH.parent == expected
        assert "pyinstaller-extract" not in str(config.DATA_DIR)
    finally:
        importlib.reload(config)  # restore


def test_frozen_config_prefers_phai_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    data_dir = tmp_path / "userdata" / "data"
    monkeypatch.setenv("PHAI_DATA_DIR", str(data_dir))

    import phantom_ai.config as config
    importlib.reload(config)

    try:
        assert config.DATA_DIR == data_dir
    finally:
        importlib.reload(config)
