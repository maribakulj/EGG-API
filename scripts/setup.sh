#!/usr/bin/env bash
set -euo pipefail

# Install runtime + dev dependencies required for local test execution.
python -m pip install --no-build-isolation -e '.[dev]'
