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
#   bash scripts/tunnel.sh            # tunnel to 127.0.0.1:8000
#   PHAI_PORT=9000 bash scripts/tunnel.sh   # tunnel to a custom port
#
# Security note: a quick tunnel is public (anyone with the URL can reach the
# backend). Set PHAI_ACCESS_TOKEN before starting the backend, and the phone
# will ask for that token — keep the URL + token private.

set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PHAI_PORT:-8000}"
URL="http://127.0.0.1:${PORT}"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "❌ cloudflared not found."
  echo "   Install it first:"
  echo "     Linux:   sudo apt install cloudflared   (or download from cloudflare.com)"
  echo "     macOS:   brew install cloudflared"
  echo "     Windows: choco install cloudflared"
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
  echo "   Start it first:  ./scripts/run.sh   (or launch the Phantom app)"
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
