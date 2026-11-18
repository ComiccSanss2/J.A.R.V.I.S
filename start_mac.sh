#!/usr/bin/env bash
# J.A.R.V.I.S. OS - quick start on macOS / Linux
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "[JARVIS] Creating the virtual environment..."
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  echo "[JARVIS] Created .env: add ANTHROPIC_API_KEY and restart."
  open -e .env 2>/dev/null || true
fi
exec python run.py "$@"
