#!/bin/sh
# Apple Silicon preparation for a Codex guest. This script does not claim success.
# krunvm 0.2.7 cannot turn the guest NIC off, and SSIOS refuses agent=codex on macos-vm
# for that reason. The hard-isolation evidence is the Linux Cloud Hypervisor run.
set -eu

echo "macos-codex-smoke: do not record isolated=true from krunvm. The NIC stays up."
uname_s=$(uname -s)
uname_m=$(uname -m)
if [ "$uname_s" != "Darwin" ] || [ "$uname_m" != "arm64" ]; then
  echo "this host is ${uname_s} ${uname_m}. No aarch64 guest was started."
  exit 2
fi

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
STATE=${SSIOS_CODEX_STATE:-/tmp/ssios-codex-mac}
mkdir -p "$STATE"
PYTHONPATH="$ROOT/src" python3 - "$STATE" <<'PY'
import sys
from pathlib import Path
from ssios.codex_guest import prepare_codex_binary
report = prepare_codex_binary(Path(sys.argv[1]), "aarch64")
print(report["detail"])
print(report["sha256"])
PY

echo "Pinned Codex binary is on the host. The guest must not download it."
echo "Ollama stays on 127.0.0.1. Suggested pulls, if they are not already present:"
echo "  ollama pull qwen3:14b"
echo "  # deepseek-r1:7b was weak at tool calls on Ollama 0.40.15 /v1/responses."
if command -v ollama >/dev/null 2>&1; then
  ollama list || true
else
  echo "ollama is not on PATH."
fi
if command -v krunvm >/dev/null 2>&1; then
  krunvm --version || true
  echo "krunvm is present. SSIOS still does not start the Codex guest here."
else
  echo "krunvm is not on PATH."
fi
echo "Private config sketch, not written by this script:"
echo "  [llm] provider = \"ollama\" and ollama_base_url = \"http://127.0.0.1:11434\""
echo "  [runtime] backend = \"macos-vm\" and agent = \"codex\""
echo "  [agent.codex] model = \"qwen3:14b\""
echo "A later krunvm boot would still have a network. Do not treat it as the Cloud Hypervisor result."
exit 2
