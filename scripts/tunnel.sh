#!/usr/bin/env bash
# Phantom — Cloudflare Tunnel helper (Jarvis companion from anywhere).
#
# Exposes your local Phantom backend (default http://127.0.0.1:8000) through
# a free Cloudflare quick tunnel: you get an https://<random>.trycloudflare.com
# URL. Your iPhone opens that URL in Safari -> Share -> Add to Home Screen and
# the PWA companion works from anywhere (home, campus, anywhere).
#
# Requirements:
#   - Phantom backend running locally (./scripts/run.sh or `phantom`)
#   - cloudflared installed:  https://developers.cloudflare.com/cloudflare-one/
#       Linux:  sudo apt install cloudflared   (or download the binary)
#       macOS:  brew install cloudflared
#       Windows: choco install cloudflared    (or download)
#
# Usage:
#   bash scripts/tunnel.sh            # auto-detects the backend port
#   PHAI_PORT=9000 bash scripts/tunnel.sh   # force a custom port
#
# Port auto-detect: tries the default dev port (8000), the packaged-app port
# (47611), then any live Phantom port file in /tmp/phai-port-*.txt — so it
# works whether you started `bash scripts/run.sh` or the desktop app.
#
# Security note: a quick tunnel is public (anyone with the URL can reach the
# backend). Set PHAI_ACCESS_TOKEN before starting the backend, and the phone
# will ask for that token — keep the URL + token private.

set -euo pipefail
cd "$(dirname "$0")/.."

# Auto-detect the backend port: explicit PHAI_PORT wins, then try the dev
# port (8000), the packaged-app port (47611), then any running Phantom
# port-file (Electron writes /tmp/phai-port-<pid>.txt).
if [ -n "${PHAI_PORT:-}" ]; then
  PORT="$PHAI_PORT"
else
  PORT=""
  for cand in 8000 47611; do
    if curl -fsS --max-time 2 "http://127.0.0.1:${cand}/api/status" >/dev/null 2>&1; then
      PORT="$cand"; break
    fi
  done
  if [ -z "$PORT" ]; then
    for f in /tmp/phai-port-*.txt; do
      [ -e "$f" ] || continue
      p="$(cat "$f" 2>/dev/null | tr -d '[:space:]')"
      if [ -n "$p" ] && curl -fsS --max-time 2 "http://127.0.0.1:${p}/api/status" >/dev/null 2>&1; then
        PORT="$p"; break
      fi
    done
  fi
  PORT="${PORT:-8000}"
fi
URL="http://127.0.0.1:${PORT}"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "❌ cloudflared not found."
  echo
  echo "   Debian/Ubuntu (the package is NOT in the default repos —"
  echo "   use Cloudflare's official repo or the direct .deb):"
  echo
  echo "     # option 1 — Cloudflare's apt repo:"
  echo "     sudo mkdir -p --mode=0755 /usr/share/keyrings"
  echo "     curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null"
  echo "     echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list"
  echo "     sudo apt-get update && sudo apt-get install -y cloudflared"
  echo
  echo "     # option 2 — direct .deb download:"
  echo "     curl -L -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb"
  echo "     sudo dpkg -i /tmp/cloudflared.deb"
  echo
  echo "     macOS: brew install cloudflared"
  echo "     Windows: choco install cloudflared"
  echo
  echo "   Then re-run: bash scripts/tunnel.sh"
  exit 1
fi

echo "────────────────────────────────────────────────────────────"
echo "  Phantom — Cloudflare Tunnel"
echo "  Local backend : ${URL}"
echo "  Checking backend is up…"
echo "────────────────────────────────────────────────────────────"

if ! curl -fsS --max-time 5 "${URL}/api/status" >/dev/null 2>&1; then
  echo "⚠️  Backend not responding at ${URL}."
  echo "   Start it first:  ./scripts/run.sh   (dev, port 8000)"
  echo "                   or launch the Phantom desktop app (port 47611)"
  echo "   Then re-run:     bash scripts/tunnel.sh"
  exit 1
fi
echo "  ✓ backend is up"

echo
echo "  Starting tunnel… (keep this terminal open)"
echo "  On your iPhone:"
echo "    1. Copy the https://…trycloudflare.com URL from below"
echo "    2. Open it in Safari, then  Share → Add to Home Screen"
echo "    3. If the backend uses an access token, enter it when asked"
echo "────────────────────────────────────────────────────────────"
exec cloudflared tunnel --url "${URL}"
