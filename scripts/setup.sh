#!/usr/bin/env bash
set -euo pipefail

if ! command -v python >/dev/null 2>&1; then
  echo "Error: python is required but was not found on PATH." >&2
  exit 1
fi

python -m pip install --upgrade pip
python -m pip install --no-build-isolation -e ".[dev]"

echo "Setup complete. Next steps:"
echo "  pisco-api init"
echo "  pisco-api run --reload"
