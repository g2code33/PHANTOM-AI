#!/usr/bin/env bash
# PHANTOM + CODED — setup & run
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "[setup] creating virtualenv…"
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e ".[gui,dev]"
fi

echo "[run] starting PHANTOM + CODED on ${PHAI_HOST:-0.0.0.0}:${PHAI_PORT:-8000}"
exec .venv/bin/python -m phantom_ai.main
