#!/bin/bash
# deb after-install hook (electron-builder "deb.afterInstall").
#
# Runs as root on every install/upgrade and guarantees three things that
# electron-builder's default deb gets wrong:
#
# 1. SUID sandbox:  /opt/Phantom/chrome-sandbox must be root:root mode 4755 or
#    Chromium aborts ("setuid_sandbox_host.cc ... mode 4755"). The generated
#    deb never sets this bit. (The app also falls back to --no-sandbox in
#    electron/main.ts, but fixing it here is the proper solution.)
# 2. PATH:          /usr/bin/phantom -> /opt/Phantom/phantom so `phantom`
#    works from a terminal (electron-builder's alternatives entry is flaky).
# 3. Launcher:      /usr/share/applications/phantom.desktop must Exec the
#    absolute binary path so clicking the app icon actually opens Phantom.
set -e

APP_DIR="/opt/Phantom"
BIN="${APP_DIR}/phantom"
SANDBOX="${APP_DIR}/chrome-sandbox"
SYMLINK="/usr/bin/phantom"
DESKTOP="/usr/share/applications/phantom.desktop"

# ---- 1. sandbox ----
if [ -f "$SANDBOX" ]; then
  chown root:root "$SANDBOX" 2>/dev/null || true
  chmod 4755 "$SANDBOX" 2>/dev/null || true
fi

# ---- 2. PATH symlink ----
if [ -f "$BIN" ]; then
  ln -sf "$BIN" "$SYMLINK" 2>/dev/null || true
fi

# ---- 3. desktop launcher Exec absolute ----
if [ -f "$DESKTOP" ]; then
  sed -i "s|^Exec=.*|Exec=${BIN}|" "$DESKTOP" 2>/dev/null || true
  sed -i "s|^TryExec=.*|TryExec=${BIN}|" "$DESKTOP" 2>/dev/null || true
fi

# refresh the desktop database if available
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database /usr/share/applications 2>/dev/null || true

exit 0
