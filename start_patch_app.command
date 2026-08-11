#!/bin/sh
cd "$(dirname "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
  echo "Run: uv sync --extra dev"
  exit 1
fi
PYTHONPATH="$(pwd)/src" exec .venv/bin/python -m rep_patch
