#!/bin/sh
# Run this on the Mac. It does not boot a guest and does not delete state.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$ROOT"
echo "krunvm: $(command -v krunvm || echo missing)"
if command -v krunvm >/dev/null 2>&1; then
  krunvm --version || true
fi
PYTHONPATH=src python3 -m ssios vm-doctor --state .ssios-memfs --config .ssios/memfs.toml
echo "vm-doctor does not start a microVM. Isolation is true only when status reports isolated=true."
