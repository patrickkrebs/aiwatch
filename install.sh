#!/usr/bin/env bash
# Install aiwatch as a uv tool from the local source.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "Installing aiwatch from $DIR..."
uv tool install --editable "$DIR"
echo "Done. Run: aiwatch"
