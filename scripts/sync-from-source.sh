#!/usr/bin/env bash
# Vendor piano-guard source from the upstream repo into this skill.
# Idempotent. Run after upstream changes; follow with install.sh if pyproject.toml or uv.lock changed.
set -euo pipefail

SRC="${PIANO_GUARD_SRC:-$HOME/ghq/github.com/g960059/davinci-automation}"
DST="$(cd "$(dirname "$0")/.." && pwd)"

if [[ ! -d "$SRC/src/piano_guard" ]]; then
  echo "error: piano-guard source not found at $SRC/src/piano_guard" >&2
  echo "set PIANO_GUARD_SRC to the davinci-automation repo root, or clone it to the default path." >&2
  exit 1
fi

mkdir -p "$DST/src/piano_guard"
rsync -a --delete \
  --exclude '__pycache__' --exclude '.venv' --exclude '*.egg-info' \
  "$SRC/src/piano_guard/" "$DST/src/piano_guard/"
cp "$SRC/pyproject.toml" "$DST/pyproject.toml"
if [[ -f "$SRC/uv.lock" ]]; then
  cp "$SRC/uv.lock" "$DST/uv.lock"
fi

echo "synced from $SRC"
