#!/bin/bash
# deb after-remove hook (electron-builder "deb.afterRemove").
# Removes the /usr/bin symlink + alternatives entry we created on install.
# NOTE: must never contain dollar-brace macros (electron-builder substitution).
set -e

rm -f /usr/bin/phantom 2>/dev/null || true
update-alternatives --remove phantom /opt/Phantom/phantom 2>/dev/null || true

exit 0
