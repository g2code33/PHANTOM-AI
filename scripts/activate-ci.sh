#!/usr/bin/env bash
# Activate the release-on-push GitHub Actions workflow.
#
# WHY THIS SCRIPT EXISTS
# The Arena bot token is a GitHub App WITHOUT the "Workflows" permission, so it
# cannot push .github/workflows/ (GitHub rejects it). That's why the workflow
# lives in the repo as docs/workflow-build-desktop.yml and .github/ is absent.
# Your own terminal token CAN push it — run this script from your local clone
# to activate CI: builds (.exe/.deb/.AppImage/.apk) + auto GitHub Release on
# every push to main.
#
# Usage:  bash scripts/activate-ci.sh
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="docs/workflow-build-desktop.yml"
DST=".github/workflows/build-desktop.yml"

if [ ! -f "$SRC" ]; then
  echo "error: $SRC not found — are you in the repo root?" >&2
  exit 1
fi

# Make sure we're on the latest main before touching anything.
git fetch origin main 2>/dev/null || true
git checkout main 2>/dev/null || git checkout -b main origin/main 2>/dev/null || true
if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then
  echo "error: please switch to your main branch first (git checkout main)" >&2
  exit 1
fi
if [ -n "$(git log HEAD..origin/main --oneline)" ]; then
  echo "your local main is behind origin/main — pulling…"
  git pull origin main
fi

mkdir -p "$(dirname "$DST")"
cp "$SRC" "$DST"
git add "$DST"

if git diff --cached --quiet; then
  echo "✓ workflow already committed — nothing to change"
else
  git commit -m "ci: activate build-desktop workflow"
  echo "✓ committed the workflow"
fi

echo "pushing to origin/main…"
git push origin main

echo
echo "DONE — GitHub Actions is now active."
echo "The push above (or your next push to main) will:"
echo "  • build PhantomCoded-Setup-<ver>.exe, .deb, .AppImage, phantom-coded-<ver>.apk"
echo "  • auto-create a GitHub Release (v<version from package.json>) with all assets"
echo "  • generate latest.yml / latest-linux.yml for in-app self-updates"
echo "Watch it at: https://github.com/g2code33/PHANTOM-AI/actions"
echo "Check the release at: https://github.com/g2code33/PHANTOM-AI/releases"
