"""Codex CLI inside the guest, talking to a host mock Responses proxy.

Checked against codex-cli 0.167.0 on 2026-11-09. That binary POSTs
``/v1/responses`` with ``stream: true`` and ``Accept: text/event-stream``.
It does not send ``Authorization`` when ``requires_openai_auth`` is false.
A single turn completes when the SSE stream contains, in order:

- ``response.created`` with ``response.id``
- ``response.output_item.added`` for one ``message`` item
- ``response.output_text.delta``
- ``response.output_item.done`` with ``content[].type == output_text``
- ``response.completed`` with ``status == completed`` and ``usage.input_tokens``
  plus ``usage.output_tokens``

No WebSocket and no second request were required for a plain final message.
This proxy implements that subset only. It does not call Ollama or any other host.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

from ssios.errors import SsiosError

PIN_PATH = Path(__file__).resolve().parents[2] / "third_party" / "codex.pin.json"
VSOCK_PORT = 19090
MIN_MEMORY_MB = 1536
_MACHINE = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def load_pin() -> dict:
    try:
        pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SsiosError("codex_pin_invalid", "codex pin file cannot be read", 500) from exc
    if pin.get("tag") != "rust-v0.167.0" or not isinstance(pin.get("assets"), dict):
        raise SsiosError("codex_pin_invalid", "codex pin is not rust-v0.167.0", 500)
    return pin


def asset_for_machine(machine: str | None = None) -> dict:
    name = _MACHINE.get(machine or platform.machine())
    pin = load_pin()
    asset = pin["assets"].get(name) if name else None
    if not isinstance(asset, dict):
        raise SsiosError("codex_unsupported_arch", "no pinned Codex binary for this machine", 500)
    for key in ("archive", "archive_sha256", "binary", "binary_sha256", "url"):
        if not isinstance(asset.get(key), str) or not asset[key]:
            raise SsiosError("codex_pin_invalid", "codex pin is missing a hash", 500)
    return asset


def verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise SsiosError("codex_checksum_mismatch", "codex file cannot be read", 500) from exc
    if digest.hexdigest() != expected:
        raise SsiosError("codex_checksum_mismatch", "codex checksum does not match the pin", 500)


def codex_cache_dir(state_dir: Path) -> Path:
    return state_dir / "cache" / "codex" / "rust-v0.167.0"


def prepared_binary(state_dir: Path, machine: str | None = None) -> Path:
    """Return the already checked binary. Never downloads."""
    asset = asset_for_machine(machine)
    path = codex_cache_dir(state_dir) / asset["binary"]
    if not path.is_file() or path.is_symlink():
        raise SsiosError(
            "codex_not_prepared",
            "pinned Codex binary is missing; run ssios vm-prepare on the host",
            503,
        )
    verify_sha256(path, asset["binary_sha256"])
    return path


def prepare_codex_binary(state_dir: Path, machine: str | None = None, timeout: float = 120) -> dict:
    """Download on the host, verify both hashes, and store the binary. Not used by the guest."""
    asset = asset_for_machine(machine)
    cache = codex_cache_dir(state_dir)
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / asset["archive"]
    binary = cache / asset["binary"]
    try:
        if not archive.is_file():
            _download(asset["url"], archive, timeout)
        verify_sha256(archive, asset["archive_sha256"])
        if binary.is_symlink() or not binary.is_file():
            _extract_member(archive, asset["binary"], binary)
        verify_sha256(binary, asset["binary_sha256"])
    except SsiosError:
        if archive.is_file():
            try:
                verify_sha256(archive, asset["archive_sha256"])
            except SsiosError:
                archive.unlink(missing_ok=True)
        raise
    binary.chmod(0o755)
    return {
        "ok": True,
        "version": "0.167.0",
        "path": str(binary),
        "sha256": asset["binary_sha256"],
        "detail": "pinned Codex binary is cached on the host",
    }


def codex_config_text(port: int, model: str) -> str:
    if model == "ollama" or "/" in model or "\n" in model or '"' in model:
        raise SsiosError("config_invalid", "codex model name is rejected", 500)
    if not 1 <= port <= 65535:
        raise SsiosError("config_invalid", "codex port is rejected", 500)
    return (
        'model_provider = "ssios"\n'
        f'model = "{model}"\n'
        "check_for_update_on_startup = false\n"
        'web_search = "disabled"\n'
        "\n"
        "[analytics]\n"
        "enabled = false\n"
        "\n"
        "[feedback]\n"
        "enabled = false\n"
        "\n"
        "[otel]\n"
        'metrics_exporter = "none"\n'
        "\n"
        "[model_providers.ssios]\n"
        'name = "SSIOS host proxy"\n'
        f'base_url = "http://127.0.0.1:{port}/v1"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = false\n"
    )


def guest_entry_source(port: int) -> str:
    if port != VSOCK_PORT:
        raise SsiosError("config_invalid", "codex guest port is fixed for this prototype", 500)
    return _ENTRY


class RequestBudget:
    def __init__(self, max_requests: int, max_bytes: int) -> None:
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.requests = 0

    def allow(self, size: int) -> str:
        self.requests += 1
        if self.requests > self.max_requests:
            return "too_many_requests"
        if size > self.max_bytes:
            return "request_too_large"
        return "ok"


def sse_response(text: str) -> bytes:
    message = {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }
    events = [
        {"type": "response.created", "response": {"id": "resp_mock", "object": "response", "status": "in_progress", "output": []}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "msg_mock", "type": "message", "role": "assistant", "status": "in_progress", "content": []},
        },
        {"type": "response.output_text.delta", "item_id": "msg_mock", "output_index": 0, "content_index": 0, "delta": text},
        {"type": "response.output_item.done", "output_index": 0, "item": message},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_mock",
                "object": "response",
                "status": "completed",
                "output": [message],
                "usage": {"input_tokens": 1, "output_tokens": max(1, len(text.split())), "total_tokens": 1 + max(1, len(text.split()))},
            },
        },
    ]
    payload = "".join(f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events)
    body = payload.encode("utf-8")
    header = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/event-stream\r\n"
        "Cache-Control: no-cache\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return header.encode("ascii") + body


def error_response(status: int, reason: str) -> bytes:
    body = json.dumps({"error": reason}).encode("utf-8")
    header = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return header.encode("ascii") + body


class ResponsesProxy:
    """Host side of the vsock. Cloud Hypervisor connects to ``socket``_``port``."""

    def __init__(self, socket_path: Path, text: str, model: str, budget: RequestBudget, log_path: Path) -> None:
        self.socket_path = socket_path
        self.listen_path = Path(str(socket_path) + f"_{VSOCK_PORT}")
        self.text = text
        self.model = model
        self.budget = budget
        self.log_path = log_path
        self.stop = threading.Event()
        self.served = 0
        self.rejected = False
        self._thread: threading.Thread | None = None
        self._server: socket.socket | None = None

    def start(self) -> None:
        if self.listen_path.exists():
            self.listen_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.listen_path))
        os.chmod(self.listen_path, 0o600)
        server.listen(4)
        server.settimeout(0.2)
        self._server = server
        self._thread = threading.Thread(target=self._loop, name="codex-proxy", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.stop.set()
        if self._server is not None:
            self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.listen_path.unlink(missing_ok=True)

    def _loop(self) -> None:
        assert self._server is not None
        while not self.stop.is_set():
            try:
                conn, _addr = self._server.accept()
            except (socket.timeout, OSError):
                continue
            try:
                self._handle(conn)
            finally:
                conn.close()

    def _handle(self, conn: socket.socket) -> None:
        from ssios.vmguest import redact_text

        try:
            request_line, _headers, body = _read_http(conn, self.budget.max_bytes)
        except ValueError as exc:
            self.rejected = True
            self._log(redact_text(str(exc), []), "rejected")
            conn.sendall(error_response(400, "bad_request"))
            return
        decision = self.budget.allow(len(body))
        model = ""
        try:
            payload = json.loads(body.decode("utf-8"))
            model = str(payload.get("model") or "")
        except (UnicodeError, json.JSONDecodeError):
            payload = None
        if decision != "ok":
            self.rejected = True
            self._log(model, decision)
            conn.sendall(error_response(429, decision))
            return
        if not isinstance(payload, dict) or model != self.model or payload.get("stream") is not True:
            self.rejected = True
            self._log(model, "model_rejected")
            conn.sendall(error_response(400, "model_rejected"))
            return
        if "POST" not in request_line or not request_line.split(" ", 1)[0] == "POST":
            self.rejected = True
            self._log(model, "method_rejected")
            conn.sendall(error_response(405, "method_rejected"))
            return
        self.served += 1
        self._log(model, "ok")
        conn.sendall(sse_response(self.text))

    def _log(self, model: str, decision: str) -> None:
        from ssios.vmguest import redact_text

        line = json.dumps(
            {"requests": self.budget.requests, "model": model, "decision": decision},
            ensure_ascii=False,
        )
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(redact_text(line, []) + "\n")
        os.chmod(self.log_path, 0o600)


def workspace_diff(before: dict[str, bytes], root: Path) -> str:
    current: dict[str, bytes] = {}
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                current[str(path.relative_to(root))] = path.read_bytes()
    lines = []
    for name in sorted(set(before) | set(current)):
        old = before.get(name)
        new = current.get(name)
        if old == new:
            continue
        if new is None:
            lines.append(f"removed {name}")
        elif old is None:
            lines.append(f"added {name}")
        else:
            lines.append(f"changed {name}")
    return ("\n".join(lines) + "\n") if lines else ""


def execute_codex(plane, task: dict, token: str, cancel, deadline: float) -> dict:
    from ssios.vmguest import _grant, _prepare_codex, _runtime, _stamp

    runtime = _runtime(plane)
    if runtime.name != "cloud-hypervisor":
        _stamp(plane, task["id"], runtime.name, False, None)
        raise SsiosError(
            "microvm_fail_closed",
            "codex M0 runs only in Cloud Hypervisor without a NIC; macos-vm cannot disable networking and is not claimed",
            503,
        )
    if plane.llm.provider != "mock":
        raise SsiosError("microvm_fail_closed", "codex M0 proxy is the in-process mock and does not call another model", 503)
    if plane.settings.memory_mb < MIN_MEMORY_MB:
        raise SsiosError("microvm_fail_closed", "codex guest needs at least 1536 MB", 503)
    try:
        binary = prepared_binary(plane.settings.state_dir)
    except SsiosError as exc:
        _stamp(plane, task["id"], runtime.name, False, None)
        raise SsiosError("microvm_fail_closed", exc.detail, 503) from exc
    report = _hypervisor_ready(runtime)
    if not report["ok"]:
        _stamp(plane, task["id"], runtime.name, False, None)
        raise SsiosError("microvm_fail_closed", report["detail"], 503)
    grant, _plain = _grant(plane, task, token, deadline)
    messages = [{"role": "system", "content": grant["system"]}, {"role": "user", "content": grant["user"]}]
    response = plane.llm.complete(messages, kind=task["kind"], cancel_event=cancel, deadline=deadline)
    if response.raw_approve_flag:
        plane.audit.write(
            ts=plane.now(),
            actor_role="task",
            actor_id=task["id"],
            action="model_approve_flag_ignored",
            result="ignored",
            task_id=task["id"],
            project_id=task["project_id"],
        )
    if response.kind != "final" or not response.text:
        raise SsiosError("microvm_fail_closed", "mock proxy has no final text", 503)
    from ssios.linux_guest import build_codex_initramfs

    initramfs = plane.settings.state_dir / "storage" / "codex-initrd.img"
    if not initramfs.is_file():
        build_codex_initramfs(initramfs)
    session = _prepare_codex(plane, task, token, grant, binary)
    outcome = _boot_codex(runtime, session, response.text, cancel, deadline, initramfs)
    _stamp(plane, task["id"], outcome.backend, outcome.isolated, outcome.boot_ms)
    if not outcome.isolated:
        failed = SsiosError("microvm_fail_closed", outcome.detail or "the codex guest did not run", 503)
        failed.diagnostic = {"classification": "microvm", "detail": outcome.detail, "commands": outcome.commands or []}
        raise failed
    from ssios.llm import validate_summary_quality

    try:
        validate_summary_quality(outcome.text)
    except SsiosError as exc:
        exc.diagnostic = {"classification": "low_quality", "content": outcome.text, "thinking": ""}
        raise
    return {
        "trace": ["guest.codex", "host.mock-responses"],
        "final_text": outcome.text,
        "runtime_backend": outcome.backend,
        "isolated": True,
        "boot_ms": outcome.boot_ms,
    }


def _hypervisor_ready(runtime) -> dict:
    checks = (
        runtime._kvm(),
        runtime._binary(runtime.settings.cloud_hypervisor_bin),
        runtime._binary(runtime.settings.virtiofsd_bin),
        runtime._regular_file("kernel", runtime.settings.kernel_path),
    )
    missing = [item["detail"] for item in checks if not item["ok"]]
    return {"ok": not missing, "detail": "; ".join(missing) if missing else "codex guest can start"}


def _boot_codex(runtime, session, text: str, cancel, deadline: float, initramfs: Path):
    from ssios.runtime import cloud_hypervisor_guest_command, virtiofsd_share_command
    from ssios.vmguest import GuestOutcome, _ms, _read_json, _run, _wait_sockets

    started = time.monotonic()
    budget = RequestBudget(runtime.settings.codex_max_requests, runtime.settings.codex_max_request_bytes)
    proxy = ResponsesProxy(
        session.root / "vsock.sock",
        text,
        runtime.settings.codex_model,
        budget,
        session.root / "proxy.log",
    )
    before = _snapshot(session.root / "workspace")
    sockets = []
    daemons = []
    records: list[dict] = []
    try:
        proxy.start()
        shares = (
            ("input", session.root / "input"),
            ("scratch", session.root / "scratch"),
            ("workspace", session.root / "workspace"),
            ("tools", session.root / "tools"),
        )
        for tag, path in shares:
            sock = session.root / f"{tag}.sock"
            sockets.append(sock)
            proc = __import__("subprocess").Popen(
                virtiofsd_share_command(runtime.settings.virtiofsd_bin, sock, path),
                stdout=__import__("subprocess").DEVNULL,
                stderr=__import__("subprocess").DEVNULL,
            )
            daemons.append(proc)
        if not _wait_sockets(sockets, deadline):
            return GuestOutcome("", False, runtime.name, _ms(started), "virtiofsd did not open a socket", records)
        serial = session.root / "serial.log"
        command = cloud_hypervisor_guest_command(
            binary=runtime._located(runtime.settings.cloud_hypervisor_bin) or runtime.settings.cloud_hypervisor_bin,
            kernel=runtime.settings.kernel_path,
            initramfs=str(initramfs),
            api_socket=session.root / "api.sock",
            cpus=runtime.settings.cpus,
            memory_mb=runtime.settings.memory_mb,
            filesystems=[(tag, session.root / f"{tag}.sock") for tag, _path in shares],
            serial=serial,
            vsock=session.root / "vsock.sock",
        )
        if "--net" in command or "danger-full-access" in command or "--yolo" in " ".join(command):
            raise SsiosError("network_denied", "guest network is denied", 403)
        (session.root / "hypervisor.argv").write_text("\n".join(command) + "\n", encoding="utf-8")
        completed = _run(command, max(1.0, deadline - time.monotonic()))
        from ssios.vmguest import _command_record

        records.append(_command_record(completed))
        output = _read_json(session.root / "scratch" / "output.json")
        net = _read_json(session.root / "scratch" / "netcheck.json")
        (session.root / "patch.diff").write_text(workspace_diff(before, session.root / "workspace"), encoding="utf-8")
        text_out = output.get("text", "") if isinstance(output, dict) else ""
        net_ok = isinstance(net, dict) and net.get("interfaces") == ["lo"] and net.get("outbound") is False
        ran = (
            completed.returncode == 0
            and isinstance(output, dict)
            and output.get("exit_code") == 0
            and isinstance(text_out, str)
            and bool(text_out.strip())
            and net_ok
            and proxy.served >= 1
            and not proxy.rejected
        )
        detail = "guest wrote output.json" if ran else _failure_detail(session, completed, net, proxy)
        return GuestOutcome(text_out if ran else "", ran, runtime.name, _ms(started), detail, records)
    finally:
        proxy.close()
        for proc in daemons:
            proc.terminate()
        for proc in daemons:
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()


def _failure_detail(session, completed, net, proxy) -> str:
    from ssios.vmguest import _command_detail, redact_text

    parts = [_command_detail(completed, "codex guest failed")]
    err = session.root / "scratch" / "codex.err"
    if err.is_file():
        parts.append(err.read_text(encoding="utf-8", errors="replace")[-300:])
    serial = session.root / "serial.log"
    if serial.is_file():
        parts.append(serial.read_text(encoding="utf-8", errors="replace")[-200:])
    parts.append(f"proxy_served={proxy.served} rejected={proxy.rejected} net={net}")
    return redact_text(" ".join(parts), [])[:500]


def _snapshot(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    if not root.is_dir():
        return found
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            found[str(path.relative_to(root))] = path.read_bytes()
    return found


def _download(url: str, destination: Path, timeout: float) -> None:
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response, temporary.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SsiosError("codex_not_prepared", "could not download the pinned Codex archive", 503) from exc
    os.replace(temporary, destination)


def _extract_member(archive: Path, member: str, destination: Path) -> None:
    if "/" in member or member.startswith("."):
        raise SsiosError("codex_pin_invalid", "codex archive member is rejected", 500)
    try:
        with tarfile.open(archive) as tar:
            info = tar.getmember(member)
            if not info.isfile() or info.name != member:
                raise SsiosError("codex_checksum_mismatch", "codex archive member is missing", 500)
            source = tar.extractfile(info)
            if source is None:
                raise SsiosError("codex_checksum_mismatch", "codex archive member is missing", 500)
            temporary = destination.with_suffix(".partial")
            with temporary.open("wb") as handle:
                shutil.copyfileobj(source, handle)
    except (tarfile.TarError, OSError, KeyError) as exc:
        raise SsiosError("codex_checksum_mismatch", "codex archive cannot be unpacked", 500) from exc
    os.replace(temporary, destination)


def _read_http(conn: socket.socket, max_bytes: int) -> tuple[str, dict, bytes]:
    conn.settimeout(5)
    buffer = b""
    limit = max_bytes + 8192
    while b"\r\n\r\n" not in buffer:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buffer += chunk
        if len(buffer) > limit:
            raise ValueError("request_too_large")
    head, _, rest = buffer.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1", "replace").split("\r\n")
    if not lines or " " not in lines[0]:
        raise ValueError("bad_request")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError as exc:
        raise ValueError("bad_request") from exc
    if length < 0 or length > max_bytes:
        raise ValueError("request_too_large")
    while len(rest) < length:
        chunk = conn.recv(length - len(rest))
        if not chunk:
            break
        rest += chunk
    if len(rest) < length:
        raise ValueError("bad_request")
    return lines[0], headers, rest[:length]


_ENTRY = r"""import json
import os
import socket
import subprocess
import threading
import time

PORT = 19090
CID = 2


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    try:
        dst.shutdown(socket.SHUT_WR)
    except Exception:
        pass


def handle(client):
    remote = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    remote.settimeout(10)
    remote.connect((CID, PORT))
    remote.settimeout(None)
    left = threading.Thread(target=pipe, args=(client, remote), daemon=True)
    right = threading.Thread(target=pipe, args=(remote, client), daemon=True)
    left.start()
    right.start()
    left.join()
    right.join()
    client.close()
    remote.close()


def relay():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", PORT))
    server.listen(8)
    ready = open("/scratch/relay.ready", "w", encoding="utf-8")
    ready.write("ok\n")
    ready.close()
    while True:
        client, _addr = server.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


def netcheck():
    names = sorted(os.listdir("/sys/class/net"))
    outbound = False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(("1.1.1.1", 443))
        outbound = True
    except Exception:
        outbound = False
    finally:
        probe.close()
    payload = {"interfaces": names, "outbound": outbound}
    with open("/scratch/netcheck.json", "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))
    return names == ["lo"] and not outbound


def main():
    try:
        threading.Thread(target=relay, daemon=True).start()
    except Exception as exc:
        with open("/scratch/codex.err", "w", encoding="utf-8") as handle:
            handle.write(repr(exc))
    for _ in range(50):
        if os.path.exists("/scratch/relay.ready"):
            break
        time.sleep(0.05)
    if not os.path.exists("/scratch/relay.ready"):
        with open("/scratch/output.json", "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"text": "", "exit_code": 1, "net_ok": False}))
        return
    net_ok = netcheck()
    env = os.environ.copy()
    env["CODEX_HOME"] = "/scratch/codex-home"
    env["HOME"] = "/scratch"
    env["PATH"] = "/tools:/bin"
    with open("/input/prompt.txt", "rb") as stdin, open("/scratch/events.jsonl", "wb") as stdout, open("/scratch/codex.err", "wb") as stderr:
        completed = subprocess.run(
            [
                "/tools/codex",
                "-a",
                "never",
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "-C",
                "/workspace",
                "-o",
                "/scratch/last-message.md",
                "-",
            ],
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            check=False,
        )
    text = ""
    try:
        with open("/scratch/last-message.md", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        text = ""
    with open("/scratch/output.json", "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"text": text, "exit_code": completed.returncode, "net_ok": net_ok}))


if __name__ == "__main__":
    main()
"""
