#!/usr/bin/env bash
# Phantom — Cloudflare NAMED tunnel setup (stable https URL for the iPhone PWA).
#
# Quick tunnels (scripts/tunnel.sh) give a random https://...trycloudflare.com
# that changes each run. This sets up a named tunnel with a STABLE URL so your
# iPhone PWA never needs re-adding:
#
#   Option A (no domain needed):  https://phantom-<you>.trycloudflare.com
#                                 (Cloudflare gives you a stable *.trycloudflare
#                                  hostname when you add a public hostname)
#   Option B (you own a domain):  https://phantom.yourdomain.com
#
# REQUIREMENT: you MUST be logged in to your Cloudflare account (free tier is
# fine). This script runs `cloudflared tunnel login` for you.
#
# Usage:
#   bash scripts/tunnel-setup.sh            # interactive: picks tunnel name
#   TUNNEL_NAME=phantom bash scripts/tunnel-setup.sh
#   DOMAIN=phantom.yourdomain.com bash scripts/tunnel-setup.sh   # option B

set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "❌ cloudflared not installed. Install it first:"
  echo "   Linux: sudo apt install cloudflared   | macOS: brew install cloudflared   | Win: choco install cloudflared"
  exit 1
fi

# 1) login (opens browser; free Cloudflare account is enough)
echo "────────────────────────────────────────────────────────────"
echo " Step 1 — Log in to Cloudflare (free account is fine)."
echo " A browser will open. Choose your account/domain, then return here."
echo "────────────────────────────────────────────────────────────"
cloudflared tunnel login
echo " ✓ logged in"

TUNNEL_NAME="${TUNNEL_NAME:-phantom}"
CRED=$(cloudflared tunnel list 2>/dev/null | awk -v n="$TUNNEL_NAME" '$2==n {print $1}' | head -1 || true)
if [ -z "$CRED" ]; then
  echo " Creating tunnel '${TUNNEL_NAME}'…"
  cloudflared tunnel create "$TUNNEL_NAME" >/dev/null
  echo " ✓ tunnel created"
fi
TUNNEL_ID=$(cloudflared tunnel list | awk -v n="$TUNNEL_NAME" '$2==n {print $1}' | head -1)
echo " Tunnel ID: ${TUNNEL_ID}"

# 2) pick the hostname
if [ -n "${DOMAIN:-}" ]; then
  HOSTNAME="$DOMAIN"
else
  # no custom domain -> use a stable *.trycloudflare.com hostname
  HOSTNAME="phantom-$(echo "$TUNNEL_ID" | cut -c1-8).trycloudflare.com"
fi
echo " Hostname  : ${HOSTNAME}"
echo
echo " Step 2 — add the public hostname. Run the CNAME line below"
echo " (or do it in the Cloudflare dashboard → Zero Trust → Networks → Tunnels):"
echo
echo "   cloudflared tunnel route dns \"$TUNNEL_NAME\" \"$HOSTNAME\""
echo
read -r -p " Add the DNS route now? [Y/n] " yn
case "${yn:-Y}" in
  [Yy]* ) cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME" || true ;;
  * ) echo " Skipped. Add the CNAME later in the dashboard." ;;
esac

# 3) write the config so `cloudflared tunnel run phantom` just works
CONF_DIR="$HOME/.cloudflared"
mkdir -p "$CONF_DIR"
CONF="$CONF_DIR/phantom.yml"
cat > "$CONF" <<EOF
tunnel: ${TUNNEL_NAME}
credentials-file: ${CONF_DIR}/${TUNNEL_ID}.json
ingress:
  - hostname: ${HOSTNAME}
    service: http://127.0.0.1:${PHAI_PORT:-8000}
  - service: http_status:404
EOF
echo " ✓ wrote config: ${CONF}"
echo
echo " Run the tunnel (keep this terminal open):"
echo "   cloudflared tunnel run \"$TUNNEL_NAME\""
echo
echo " On your iPhone:"
echo "   open  https://${HOSTNAME}  in Safari → Share → Add to Home Screen"
echo " (add 'PHAI_PORT=9000' to the run line if your backend is on 9000)"
