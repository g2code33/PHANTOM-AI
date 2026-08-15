#!/usr/bin/env bash
# Phantom — one-shot deploy of the Portable Phantom (Cloudflare Worker).
#
# Automates: login → create KV namespaces (if needed) → fill wrangler.toml →
# set secrets → deploy → print your worker URL. Free Cloudflare account.
#
# Usage:
#   bash cloud/deploy.sh                 # interactive (asks for secrets)
#   NVIDIA_API_KEY=... DEEPGRAM_API_KEY=... PHANTOM_CLOUD_TOKEN=... bash cloud/deploy.sh
#   CLOUD_TOKEN="" bash cloud/deploy.sh  # no auth token (not recommended)
#
# After deploy, paste the printed URL (+ token) into:
#   PC app → Settings → Portable Phantom (cloud) → Sync now
#   Phone app → connect screen → Portable URL/token

set -euo pipefail
cd "$(dirname "$0")"

WR="npx wrangler"
TOML="wrangler.toml"

echo "────────────────────────────────────────────────────────────"
echo " Phantom — Portable Worker deploy (Cloudflare)"
echo "────────────────────────────────────────────────────────────"

# 0) deps
if ! command -v npx >/dev/null 2>&1; then
  echo "❌ node/npx not found — install Node.js first."; exit 1
fi
if [ ! -d node_modules/wrangler ] && ! npx --no-install wrangler --version >/dev/null 2>&1; then
  echo " Installing wrangler…"
  npm install --no-save wrangler >/dev/null 2>&1 || npm i -g wrangler
fi

# 1) login
echo " Step 1 — Cloudflare login (browser opens; free account is fine)…"
$WR whoami >/dev/null 2>&1 || $WR login

# 2) KV namespaces (create only those still using the placeholder)
echo " Step 2 — KV namespaces…"
create_kv() {
  local binding="$1"
  local id
  id=$(grep -A2 "binding = \"$binding\"" "$TOML" | grep "id = " | head -1 | sed 's/.*id = "\(.*\)".*/\1/')
  if [ "$id" = "REPLACE_WITH_KV_NAMESPACE_ID" ] || [ "$id" = "REPLACE_WITH_KV_PROFILE_ID" ] || [ "$id" = "REPLACE_WITH_KV_REMINDERS_ID" ]; then
    echo "   creating $binding…"
    id=$($WR kv namespace create "$binding" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])" || \
         $WR kv namespace create "$binding" 2>&1 | grep -oE '"id":\s*"[a-f0-9]+"' | head -1 | sed 's/.*"\([a-f0-9]*\)".*/\1/')
    python3 - "$TOML" "$binding" "$id" <<'PY'
import sys
p, b, i = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(p).read()
s = s.replace('id = "REPLACE_WITH_KV_NAMESPACE_ID"', 'id = "' + i + '"', 1) if b == "PHANTOM_MEMORY" else s
s = s.replace('id = "REPLACE_WITH_KV_PROFILE_ID"', 'id = "' + i + '"', 1) if b == "PHANTOM_PROFILE" else s
s = s.replace('id = "REPLACE_WITH_KV_REMINDERS_ID"', 'id = "' + i + '"', 1) if b == "PHANTOM_REMINDERS" else s
open(p, "w").write(s)
PY
    echo "   ✓ $binding = $id"
  else
    echo "   $binding already set ($id)"
  fi
}
create_kv PHANTOM_MEMORY
create_kv PHANTOM_PROFILE
create_kv PHANTOM_REMINDERS

# 3) secrets (env vars win; else prompt)
echo " Step 3 — secrets…"
set_secret() {
  local name="$1"
  local val="${!name:-}"
  if [ -z "$val" ]; then
    read -r -s -p "   ${name}: " val; echo
  fi
  if [ -n "$val" ]; then
    echo "$val" | $WR secret put "$name" >/dev/null 2>&1 && echo "   ✓ ${name} set"
  else
    echo "   ⚠ ${name} empty — skipped"
  fi
}
set_secret NVIDIA_API_KEY
set_secret DEEPGRAM_API_KEY
if [ "${CLOUD_TOKEN_SET:-}" != "skip" ]; then
  set_secret PHANTOM_CLOUD_TOKEN
fi

# 4) deploy
echo " Step 4 — deploying…"
DEPLOY_OUT=$($WR deploy 2>&1 | tee /tmp/phantom-deploy.log)
echo "$DEPLOY_OUT"

# 5) print the URL (wrangler prints https://<name>.<sub>.workers.dev)
echo "────────────────────────────────────────────────────────────"
URL=$(grep -oE 'https://[a-z0-9-]+\.workers\.dev' /tmp/phantom-deploy.log | head -1 || true)
if [ -z "$URL" ]; then
  NAME=$(grep -E '^name' "$TOML" | sed 's/name = "\(.*\)"/\1/')
  URL="https://${NAME}.<your-subdomain>.workers.dev"
fi
rm -f /tmp/phantom-deploy.log
echo " ✅ Deployed."
echo
echo " Portable Phantom URL (paste into the app):"
echo "   ${URL}"
echo
echo " Phone: connect screen → Portable URL + token"
echo " PC   : Settings → Portable Phantom → URL + token → Sync now"
echo "────────────────────────────────────────────────────────────"
