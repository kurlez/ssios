#!/bin/sh
# Full summary inside krunvm, against a temporary state directory.
# This script does not read or delete .ssios, .ssios-demo, or .ssios-memfs.
# It does not create disk images. Create a case-sensitive volume yourself if
# krunvm/buildah asks for one; see docs/macos-vm.md.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$ROOT"
if ! command -v krunvm >/dev/null 2>&1; then
  echo "krunvm is not installed. See docs/macos-vm.md." >&2
  exit 2
fi
krunvm --version
STATE=$(mktemp -d "${TMPDIR:-/tmp}/ssios-vm-smoke.XXXXXX")
cleanup() {
  if [ -n "${PID:-}" ]; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  rm -rf "$STATE"
}
trap cleanup EXIT
cp config/ssios.example.toml "$STATE/smoke.toml"
python3 - "$STATE/smoke.toml" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = text.replace('backend = "dev-guard"', 'backend = "macos-vm"', 1)
path.write_text(text, encoding="utf-8")
PY
PYTHONPATH=src PYTHONUNBUFFERED=1 python3 -u -m ssios up --profile dev --state "$STATE" --config "$STATE/smoke.toml" --port 0 >"$STATE/up.log" 2>&1 &
PID=$!
python3 - "$STATE" <<'PY'
import json, pathlib, sys, time, urllib.request
state = pathlib.Path(sys.argv[1])
listen = state / "listen.url"
deadline = time.time() + 20
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
request = urllib.request.Request(url + "/v1/tasks", data=body, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(request, timeout=30) as response:
    created = json.load(response)
task_id = created["id"]
deadline = time.time() + 180
while time.time() < deadline:
    req = urllib.request.Request(url + "/v1/tasks/" + task_id, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=10) as response:
        task = json.load(response)
    if task["status"] in {"awaiting_approval", "failed", "committed"}:
        break
    time.sleep(0.5)
else:
    raise SystemExit("task did not finish")
print(json.dumps({
    "status": task["status"],
    "runtime_backend": task.get("runtime_backend"),
    "isolated": task.get("isolated"),
    "boot_ms": task.get("boot_ms"),
    "error_code": task.get("error_code"),
}, ensure_ascii=False))
if not task.get("isolated"):
    raise SystemExit("guest did not run")
PY
