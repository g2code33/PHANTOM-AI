#!/usr/bin/env bash
# Phantom — run the named Cloudflare tunnel (after tunnel-setup.sh).
# Usage: bash scripts/tunnel-run.sh   (optionally TUNNEL_NAME=phantom)
set -euo pipefail
cd "$(dirname "$0")/.."
NAME="${TUNNEL_NAME:-phantom}"
echo "Starting Cloudflare tunnel '${NAME}' → local backend…"
exec cloudflared tunnel run "$NAME"
