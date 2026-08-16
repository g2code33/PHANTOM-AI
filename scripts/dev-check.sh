#!/usr/bin/env bash
# PHANTOM — pull the latest arena work and test it in dev mode BEFORE pushing
# to build. Run this from your local clone:
#
#   bash scripts/dev-check.sh            # pull + test + launch dev server
#   bash scripts/dev-check.sh --no-run   # pull + test only (no server)
#
# What it does:
#   1. commits your local cloud/worker/wrangler.toml (real KV ids) so the
#      merge never conflicts (the known pattern)
#   2. fetches + merges origin/arena/01a00049-phantom-ai
#   3. creates/updates the .venv with the minimal test deps
#   4. runs the full backend suite + the JS/worker checks (expect 201 passed)
#   5. (optional) launches the dev server on http://localhost:8000
#
# When everything is green, build with:  bash scripts/activate-ci.sh
set -euo pipefail
cd "$(dirname "$0")/.."

BRANCH="arena/01a00049-phantom-ai"
RUN_DEV=1
if [ "${1:-}" = "--no-run" ]; then RUN_DEV=0; fi

echo "▶ 1/5  Check working tree (auto-commit local KV ids)"
# Only wrangler.toml is auto-committed (real Cloudflare KV ids); anything else
# dirty would make the merge refuse — abort with a hint instead of clobbering.
if [ -n "$(git status --porcelain | grep -v 'cloud/worker/wrangler.toml')" ]; then
  echo "   ⚠ you have uncommitted changes other than wrangler.toml:" >&2
  git status --porcelain | grep -v 'cloud/worker/wrangler.toml' | sed 's/^/     /' >&2
  echo "   Commit or stash them, then re-run." >&2
  exit 1
fi
if git status --porcelain -- cloud/worker/wrangler.toml | grep -q .; then
  git add cloud/worker/wrangler.toml
  git commit -q -m "chore: local KV ids"
  echo "   ✓ committed cloud/worker/wrangler.toml"
else
  echo "   ✓ working tree clean"
fi

echo "▶ 2/5  Pull ${BRANCH}"
git fetch origin "$BRANCH"
git merge FETCH_HEAD --no-edit
echo "   ✓ merged origin/${BRANCH} into $(git rev-parse --abbrev-ref HEAD)"

echo "▶ 3/5  Python env (venv + minimal test deps)"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[dev]" numpy
echo "   ✓ deps ready"

echo "▶ 4/5  Tests (expect: 201 passed)"
.venv/bin/python -m pytest -q
node scripts/test-hud.mjs
node cloud/tests/test_worker.mjs
echo "   ✓ all checks green"

if [ "$RUN_DEV" = "1" ]; then
  echo "▶ 5/5  Dev server → http://localhost:8000   (Ctrl+C to stop)"
  echo "       (if port 8000 is busy:  PHAI_PORT=8010 bash scripts/run.sh)"
  exec bash scripts/run.sh
else
  echo "✅ all checks passed — safe to build:  bash scripts/activate-ci.sh"
fi
