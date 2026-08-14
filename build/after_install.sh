#!/bin/bash
# deb after-install hook (electron-builder "deb.afterInstall").
#
# Fixes the classic Electron-on-Linux SUID sandbox failure:
#   FATAL:setuid_sandbox_host.cc] The SUID sandbox helper binary was found,
#   but is not configured correctly ... /opt/PhantomCoded/chrome-sandbox is
#   owned by root and has mode 4755.
#
# electron-builder's generated deb does not set the setuid bit, so Chromium
# aborts. This hook (which dpkg runs as root) sets the correct ownership and
# mode so the sandbox works — and if it ever cannot, the app itself falls
# back to --no-sandbox (see electron/main.ts) so it still opens.
set -e

APP_DIR="/opt/PhantomCoded"
SANDBOX="${APP_DIR}/chrome-sandbox"

if [ -f "$SANDBOX" ]; then
  chown root:root "$SANDBOX" 2>/dev/null || true
  chmod 4755 "$SANDBOX" 2>/dev/null || true
fi

# Belt-and-braces: if the app directory is elsewhere (custom install), find it.
for candidate in "${APP_DIR}/chrome-sandbox" "/opt/phantom-coded/chrome-sandbox"; do
  if [ -f "$candidate" ]; then
    chown root:root "$candidate" 2>/dev/null || true
    chmod 4755 "$candidate" 2>/dev/null || true
  fi
done

exit 0
