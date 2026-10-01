#!/bin/sh
# Build the distributable zip with Electrum's own packaging script (no __pycache__ inside).
set -e
HERE=$(cd "$(dirname "$0")/.." && pwd)
ELECTRUM=${ELECTRUM:-"$HERE/../electrum"}
PY=${PY:-"$ELECTRUM/.venv/bin/python"}
find "$HERE/lowtide" -name __pycache__ -type d -prune -exec rm -rf {} +
mkdir -p "$HERE/dist" && cd "$HERE/dist"
"$PY" "$ELECTRUM/contrib/make_plugin" "$HERE/lowtide" | tail -1
