#!/usr/bin/env bash
set -euo pipefail

on_error() {
  echo
  echo "Setup failed while installing dependencies." >&2
  echo "If this environment is locked down, use one of the documented fallbacks:" >&2
  echo "- internal mirror via PIP_INDEX_URL" >&2
  echo "- prebuilt wheelhouse with --no-index --find-links" >&2
  echo "See README -> 'Locked-down/offline install strategy'." >&2
}
trap on_error ERR

python -m pip install --no-build-isolation -e '.[dev]'

echo "Setup completed."
