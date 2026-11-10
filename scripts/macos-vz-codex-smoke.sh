#!/bin/sh
# One Codex task in a vfkit guest with no virtio-net device.
# This script does not read or delete .ssios, .ssios-demo, or .ssios-memfs.
# It does not create disk images. On failure the temporary state is kept.
# Linux cannot boot Virtualization.framework, so this host only prints that fact.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$ROOT"
uname_s=$(uname -s)
uname_m=$(uname -m)
if [ "$uname_s" != "Darwin" ] || [ "$uname_m" != "arm64" ]; then
  echo "this host is ${uname_s} ${uname_m}. vfkit was not started. Do not record a Mac result from this machine." >&2
  exit 2
fi
if ! command -v vfkit >/dev/null 2>&1; then
  echo "vfkit is not installed. Run: brew install vfkit" >&2
  exit 2
fi
vfkit --version || true
if ! command -v ollama >/dev/null 2>&1; then
  echo "ollama is not on PATH." >&2
  exit 2
fi
echo "If qwen2.5-coder:7b is not listed, run: ollama pull qwen2.5-coder:7b" >&2
ollama list || true
PYTHONPATH=src python3 - << 'PY'
import json
import sys
from ssios.codex_guest import ollama_smoke_preflight
report = ollama_smoke_preflight()
print(json.dumps(report, ensure_ascii=False), flush=True)
if not report["ok"]:
    sys.stderr.write(report["detail"] + "\n")
    raise SystemExit(1)
PY
STATE=$(mktemp -d "${TMPDIR:-/tmp}/ssios-vz-codex.XXXXXX")
echo "临时状态目录：$STATE" >&2
cleanup() {
  if [ -n "${PID:-}" ]; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  if [ "${SSIOS_SMOKE_CLEAN:-}" = "1" ]; then
    rm -rf "$STATE"
  else
    echo "临时状态保留在 $STATE" >&2
  fi
}
trap cleanup EXIT
cp config/ssios.example.toml "$STATE/smoke.toml"
python3 - "$STATE/smoke.toml" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = text.replace('backend = "dev-guard"', 'backend = "macos-vz"', 1)
text = text.replace('agent = "summary"', 'agent = "codex"', 1)
text = text.replace('provider = "mock"', 'provider = "ollama"', 1)
text = text.replace("memory_mb = 512", "memory_mb = 2048", 1)
text = text.replace("task_timeout_seconds = 30", "task_timeout_seconds = 240", 1)
required = (
    'backend = "macos-vz"',
    'agent = "codex"',
    'provider = "ollama"',
    "memory_mb = 2048",
)
missing = [item for item in required if item not in text]
if missing:
    raise SystemExit("smoke config was not updated: " + ", ".join(missing))
path.write_text(text, encoding="utf-8")
PY
echo "准备钉住的 aarch64 内核和 initramfs。客户机启动时不再下载。" >&2
# vm-prepare reads profile from the config file. It does not accept --profile.
PYTHONPATH=src python3 -m ssios vm-prepare --state "$STATE" --config "$STATE/smoke.toml"
PYTHONPATH=src PYTHONUNBUFFERED=1 python3 -u -m ssios up --profile dev --state "$STATE" --config "$STATE/smoke.toml" --port 0 >"$STATE/up.log" 2>&1 &
PID=$!
python3 - "$STATE" <<'PY'
import json, pathlib, sys, time, urllib.request
state = pathlib.Path(sys.argv[1])
listen = state / "listen.url"
deadline = time.time() + 30
url = ""
while time.time() < deadline:
    if listen.is_file():
        candidate = listen.read_text(encoding="utf-8").strip()
        if candidate.startswith("http://127.0.0.1:"):
            try:
                urllib.request.urlopen(candidate + "/ui/login", timeout=1).read()
                url = candidate
                break
            except Exception:
                pass
    time.sleep(0.1)
else:
    log = (state / "up.log").read_text(encoding="utf-8", errors="replace") if (state / "up.log").exists() else ""
    sys.stderr.write(log)
    raise SystemExit("ssios up did not start")
token = (state / "secrets" / "human.token").read_text(encoding="utf-8").strip()
body = json.dumps({
    "project_id": "demo-alpha",
    "kind": "project-summary",
    "input_revision": "exp-1",
    "input_path": "notes.md",
}).encode()
request = urllib.request.Request(
    url + "/v1/tasks",
    data=body,
    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    created = json.load(response)
task_id = created["id"]
deadline = time.time() + 900
task = {}
while time.time() < deadline:
    req = urllib.request.Request(url + "/v1/tasks/" + task_id, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=30) as response:
        task = json.load(response)
    if task["status"] in {"awaiting_approval", "failed", "committed"}:
        break
    time.sleep(1)
else:
    raise SystemExit("task did not finish")
guest = state / "storage" / "tasks" / task_id / "guest"
net = {}
boundary = {}
argv = ""
net_path = guest / "scratch" / "netcheck.json"
boundary_path = guest / "scratch" / "boundary.json"
argv_path = guest / "hypervisor.argv"
if net_path.is_file():
    net = json.loads(net_path.read_text(encoding="utf-8"))
if boundary_path.is_file():
    boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
if argv_path.is_file():
    argv = argv_path.read_text(encoding="utf-8")
serial_log = guest / "serial.log"
report = {
    "status": task.get("status"),
    "isolated": task.get("isolated"),
    "boot_ms": task.get("boot_ms"),
    "error_code": task.get("error_code"),
    "error_detail": task.get("error_detail"),
    "nic": net.get("interfaces"),
    "outbound": net.get("outbound"),
    "marker": boundary.get("marker"),
    "outside_write": boundary.get("outside"),
    "sandbox_denied": boundary.get("denied"),
    "argv_has_virtio_net": "virtio-net" in argv,
    "serial_log": str(serial_log),
    "diagnostic_path": task.get("diagnostic_path") or "",
}
print(json.dumps(report, ensure_ascii=False, indent=2))
if task.get("status") != "awaiting_approval" or not task.get("isolated"):
    sys.stderr.write("serial_log " + str(serial_log) + "\n")
    if task.get("diagnostic_path"):
        sys.stderr.write("diagnostic " + str(task.get("diagnostic_path")) + "\n")
    raise SystemExit("codex guest did not reach an isolated approval")
if net.get("interfaces") != ["lo"] or net.get("outbound") is not False:
    raise SystemExit("guest network check failed")
if boundary.get("outside") is not False or "virtio-net" in argv:
    raise SystemExit("outside write or NIC check failed")
PY
echo "这次结果来自这台 Mac。在 Linux 上的代理没有执行过 vfkit。" >&2
