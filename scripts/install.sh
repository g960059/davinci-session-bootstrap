#!/usr/bin/env bash
# Build the skill's .venv from the vendored piano-guard source.
# Run once after sync-from-source.sh, then again whenever pyproject.toml or uv.lock changes.
# The .venv is a disposable cache; re-run after Python/macOS upgrades if it stops working.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

if [[ ! -f pyproject.toml ]]; then
  echo "error: pyproject.toml not found in $DIR. Run scripts/sync-from-source.sh first." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv not installed. Install via 'brew install uv' or https://docs.astral.sh/uv/." >&2
  exit 1
fi

uv venv --python 3.11 .venv

if [[ -f uv.lock ]]; then
  uv sync --frozen
else
  uv pip install -e .
fi

if [[ ! -x .venv/bin/piano-guard ]]; then
  echo "error: install completed but .venv/bin/piano-guard missing." >&2
  exit 1
fi

echo "installed; sanity check:"
.venv/bin/piano-guard --help | head -5
