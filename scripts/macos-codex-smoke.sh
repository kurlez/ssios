#!/bin/sh
# Prepare the pinned aarch64 Codex binary on a Mac. This script does not boot a
# guest and does not claim that krunvm isolated the run. krunvm 0.2.7 cannot
# disable the guest network from its command line.
set -eu

echo "macos-codex-smoke: M0 evidence is the Linux Cloud Hypervisor run, not this script."
uname_s=$(uname -s)
uname_m=$(uname -m)
if [ "$uname_s" != "Darwin" ] || [ "$uname_m" != "arm64" ]; then
  echo "this host is ${uname_s} ${uname_m}. The aarch64 guest was not started."
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
echo "Checksum verification finished on the host. No guest was started."
echo "krunvm 0.2.7 has no switch that removes the NIC. Do not record isolated=true from a Mac boot."
echo "Next manual step, still not a claimed success: keep [runtime] agent = \"codex\" and backend = \"macos-vm\" only for experiments."
exit 2
