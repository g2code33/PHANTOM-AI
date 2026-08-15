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
# Resolve the script's own directory so this works from ANY cwd:
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

wr() { ( cd "$WORKER_DIR" && npx wrangler "$@" ); }
# the worker config lives in cloud/worker/
WORKER_DIR="$SCRIPT_DIR/worker"
TOML="$WORKER_DIR/wrangler.toml"

echo "────────────────────────────────────────────────────────────"
echo " Phantom — Portable Worker deploy (Cloudflare)"
echo "────────────────────────────────────────────────────────────"

# 0) deps
( cd "$WORKER_DIR" && [ -d node_modules ] || npm install --no-save >/dev/null 2>&1 ) || true
if ! command -v npx >/dev/null 2>&1; then
  echo "❌ node/npx not found — install Node.js first."; exit 1
fi
if [ ! -d "$WORKER_DIR/node_modules/wrangler" ] && ! (cd "$WORKER_DIR" && npx --no-install wrangler --version >/dev/null 2>&1); then
  echo " Installing wrangler…"
  (cd "$WORKER_DIR" && npm install --no-save wrangler) >/dev/null 2>&1 || npm i -g wrangler
fi

# 1) login (with a clear error if auth can't be established)
echo " Step 1 — Cloudflare login (browser opens; free account is fine)…"
if ! wr whoami >/dev/null 2>&1; then
  wr login || { echo "❌ Could not log in to Cloudflare. Run 'bash cloud/deploy.sh' on your own machine (browser login) or set CLOUDFLARE_API_TOKEN." >&2; exit 1; }
  wr whoami >/dev/null 2>&1 || { echo "❌ Login did not complete." >&2; exit 1; }
fi
echo " ✓ logged in"

# 2) KV namespaces (create only those still using the placeholder)
echo " Step 2 — KV namespaces…"
create_kv() {
  local binding="$1"
  local placeholder
  case "$binding" in
    PHANTOM_MEMORY)   placeholder="REPLACE_WITH_KV_NAMESPACE_ID" ;;
    PHANTOM_PROFILE)  placeholder="REPLACE_WITH_KV_PROFILE_ID" ;;
    PHANTOM_REMINDERS) placeholder="REPLACE_WITH_KV_REMINDERS_ID" ;;
    *) echo "   ? unknown binding $binding"; return ;;
  esac
  # pull the id from the toml block for this binding (capture between quotes)
  local id
  id=$(awk -v b="\"$binding\"" '$0 ~ "binding = " b {f=1; next} f && /id = / {match($0, /"[a-f0-9]+"/); print substr($0, RSTART+1, RLENGTH-2); exit}' "$TOML")
  if [ -z "$id" ] || [ "$id" = "$placeholder" ] || [ "$id" = "REPLACE_WITH_KV" ]; then
    echo "   creating $binding…"
    local out
    out=$(wr kv namespace create "$binding" 2>&1 || true)
    id=$(echo "$out" | python3 -c "import sys,json;
try: print(json.load(sys.stdin)['id'])
except Exception: print('')" 2>/dev/null || true)
    if [ -z "$id" ]; then
      id=$(echo "$out" | grep -oE '[a-f0-9]{32}' | head -1 || true)
    fi
    if [ -z "$id" ]; then
      echo "   ❌ could not create $binding (wrangler said: $out)" >&2
      exit 1
    fi
    # replace the placeholder with the real id
    python3 - "$TOML" "$placeholder" "$id" <<'PY'
import sys
p, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(p).read()
s = s.replace('id = "' + old + '"', 'id = "' + new + '"', 1)
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
    echo "$val" | wr secret put "$name" >/dev/null 2>&1 && echo "   ✓ ${name} set"
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
DEPLOY_OUT=$(wr deploy 2>&1 | tee /tmp/phantom-deploy.log)
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
