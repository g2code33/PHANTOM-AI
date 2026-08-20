#!/usr/bin/env bash
# Phantom — one-shot deploy of the Portable Phantom (Cloudflare Worker).
#
# Automates: login → create KV namespaces (if needed) → fill a GITIGNORED
# local config (wrangler.local.toml) → set secrets → deploy → print URL.
#
# IMPORTANT: the repo's wrangler.toml keeps its KV placeholders. Real KV ids
# are written to cloud/worker/wrangler.local.toml (gitignored), and wrangler
# uses it via --config. So merging the repo NEVER conflicts with your deploy
# ids, and redeploys keep working after merges.
#
# Usage:
#   bash cloud/deploy.sh                 # interactive (asks for secrets)
#   NONINTERACTIVE=1 NVIDIA_API_KEY=... DEEPGRAM_API_KEY=... PHANTOM_CLOUD_TOKEN=... bash cloud/deploy.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

WORKER_DIR="$SCRIPT_DIR/worker"
TOML="$WORKER_DIR/wrangler.toml"
LOCAL_CFG="$WORKER_DIR/wrangler.local.toml"

# wrangler wrapper: use the local override when present
wr() {
  local cfg=""
  if [ -f "$LOCAL_CFG" ]; then
    cfg="--config $LOCAL_CFG"
  fi
  ( cd "$WORKER_DIR" && npx wrangler $cfg "$@" )
}

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

# 1) login
echo " Step 1 — Cloudflare login check…"
if ! wr whoami >/dev/null 2>&1; then
  if [ "${NONINTERACTIVE:-}" = "1" ]; then
    echo "❌ Not logged in to Cloudflare. Open the app on a machine where you've run 'wrangler login', or set CLOUDFLARE_API_TOKEN." >&2
    exit 1
  fi
  wr login || { echo "❌ Could not log in to Cloudflare." >&2; exit 1; }
  wr whoami >/dev/null 2>&1 || { echo "❌ Login did not complete." >&2; exit 1; }
fi
echo " ✓ logged in"

# seed the local override from the repo toml (first run)
if [ ! -f "$LOCAL_CFG" ]; then
  cp "$TOML" "$LOCAL_CFG"
fi

# 2) KV namespaces
echo " Step 2 — KV namespaces…"
placeholder_of() {
  case "$1" in
    PHANTOM_MEMORY)   echo "REPLACE_WITH_KV_NAMESPACE_ID" ;;
    PHANTOM_PROFILE)  echo "REPLACE_WITH_KV_PROFILE_ID" ;;
    PHANTOM_REMINDERS) echo "REPLACE_WITH_KV_REMINDERS_ID" ;;
    PHANTOM_KEYS)     echo "REPLACE_WITH_KV_KEYS_ID" ;;
    *) echo "" ;;
  esac
}
# true if the given KV namespace id exists on the CURRENT Cloudflare account
kv_exists_on_account() {
  local id="$1"
  wr kv namespace list 2>/dev/null | grep -q "\"$id\""
}

set_kv_id() {
  local binding="$1"
  local placeholder
  placeholder=$(placeholder_of "$binding")
  [ -n "$placeholder" ] || return 0
  # read id from the local override
  local id
  id=$(awk -v b="\"$binding\"" '$0 ~ "binding = " b {f=1; next} f && /id = / {match($0, /"[a-f0-9]+"/); print substr($0, RSTART+1, RLENGTH-2); exit}' "$LOCAL_CFG")
  if [ -z "$id" ] || [ "$id" = "$placeholder" ]; then
    echo "   creating $binding…"
    local out
    out=$(wr kv namespace create "$binding" 2>&1 || true)
    id=$(echo "$out" | python3 -c "import sys,json
try: print(json.load(sys.stdin)['id'])
except Exception: print('')" 2>/dev/null || true)
    if [ -z "$id" ]; then
      id=$(echo "$out" | grep -oE '[a-f0-9]{32}' | head -1 || true)
    fi
    if [ -z "$id" ]; then
      echo "   ❌ could not create $binding (wrangler said: $out)" >&2
      exit 1
    fi
    write_kv_id "$binding" "$placeholder" "$id"
    echo "   ✓ $binding = $id"
  elif ! kv_exists_on_account "$id"; then
    # the saved id belongs to a DIFFERENT Cloudflare account (or was deleted).
    # Recreate it on the current account and update the local override so the
    # deploy never 500s against a stale namespace. (Self-healing: no need to
    # delete wrangler.local.toml manually when switching accounts.)
    echo "   $binding id ($id) not found on this Cloudflare account — recreating…"
    local out
    out=$(wr kv namespace create "$binding" 2>&1 || true)
    local new_id
    new_id=$(echo "$out" | python3 -c "import sys,json
try: print(json.load(sys.stdin)['id'])
except Exception: print('')" 2>/dev/null || true)
    if [ -z "$new_id" ]; then
      new_id=$(echo "$out" | grep -oE '[a-f0-9]{32}' | head -1 || true)
    fi
    if [ -z "$new_id" ]; then
      echo "   ❌ could not recreate $binding (wrangler said: $out)" >&2
      exit 1
    fi
    write_kv_id "$binding" "$id" "$new_id"
    echo "   ✓ $binding recreated = $new_id"
  else
    echo "   $binding already set ($id)"
  fi
}

# write a kv id into the gitignored local override (replacing old, or appending)
write_kv_id() {
  local binding="$1" old="$2" new="$3"
  python3 - "$LOCAL_CFG" "$old" "$new" <<'PY'
import sys
p, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(p).read()
if ('id = "' + old + '"') in s:
    s = s.replace('id = "' + old + '"', 'id = "' + new + '"', 1)
else:
    s += '\n[[kv_namespaces]]\nbinding = "X"\nid = "' + new + '"\n'
open(p, "w").write(s)
PY
}
set_kv_id PHANTOM_MEMORY
set_kv_id PHANTOM_PROFILE
set_kv_id PHANTOM_REMINDERS
set_kv_id PHANTOM_KEYS

# 3) secrets (env wins; prompt interactively unless NONINTERACTIVE)
echo " Step 3 — secrets…"
set_secret() {
  local name="$1"
  local val="${!name:-}"
  # already set on Cloudflare? keep it — no re-prompt, no re-upload
  if [ -z "$val" ]; then
    if wr secret list 2>/dev/null | grep -q ""$name""; then
      echo "   ${name} already set — keeping"
      return
    fi
  fi
  if [ -z "$val" ] && [ "${NONINTERACTIVE:-}" != "1" ]; then
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
set_secret PHANTOM_CLOUD_TOKEN

# 4) deploy
# embed the mobile UI into the Worker so the workers.dev link IS the companion
echo " Step 3.5 — embedding mobile UI…"
node build-embed.mjs || { echo "   ❌ could not build mobile embed"; exit 1; }

echo " Step 4 — deploying…"
DEPLOY_OUT=$(wr deploy 2>&1 | tee /tmp/phantom-deploy.log)
echo "$DEPLOY_OUT"

# 5) print the URL
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
