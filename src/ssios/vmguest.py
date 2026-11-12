"""Host side of a guest summary.

The guest builds one prompt from the granted notes and writes it to the scratch
directory. This process calls the configured local model and writes the answer
back. The guest copies that answer to output.json. The guest directory never
receives the human token, and the guest is not given a network endpoint.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ssios.agent import PLAIN_PROMPT, TOPIC_PROMPT, cap_note_text, readable_material
from ssios.errors import FlowInterrupt, SsiosError
from ssios.llm import is_template_boilerplate, validate_summary_quality
from ssios.runtime import (
    cloud_hypervisor_guest_command,
    krunvm_create_command,
    krunvm_delete_command,
    krunvm_echo_command,
    krunvm_prepare_command,
    krunvm_start_command,
    virtiofsd_share_command,
)

WORKER = Path(__file__).with_name("guest_worker.py")


@dataclass
class GuestOutcome:
    text: str
    isolated: bool
    backend: str
    boot_ms: int | None
    detail: str
    commands: list | None = None
    error_code: str | None = None


@dataclass
class GuestSession:
    root: Path
    plain: bool
    grant: dict
    task_id: str
    kind: str
    project_id: str
    mode: str = "summary"


def execute_guest(plane, task: dict, token: str, cancel, deadline: float) -> dict:
    if getattr(plane.settings, "runtime_agent", "summary") == "codex":
        from ssios.codex_guest import execute_codex

        return execute_codex(plane, task, token, cancel, deadline)
    if task.get("kind") != "project-summary":
        raise SsiosError("microvm_fail_closed", "this task kind is not run inside the guest", 503)
    runtime = _runtime(plane)
    runtime.plane = plane
    if not runtime.can_launch():
        _stamp(plane, task["id"], runtime.name, False, None)
        raise SsiosError("microvm_fail_closed", "microVM backend cannot start a guest; the task was not run on the host", 503)
    session = _prepare(plane, task, token, deadline)
    outcome = runtime.run(session, cancel, deadline)
    _stamp(plane, task["id"], outcome.backend, outcome.isolated, outcome.boot_ms)
    if not outcome.isolated:
        failed = SsiosError("microvm_fail_closed", outcome.detail or "the guest did not run", 503)
        failed.diagnostic = {
            "classification": "microvm",
            "detail": outcome.detail,
            "commands": outcome.commands or [],
        }
        raise failed
    if not outcome.text:
        raise SsiosError("model_output_invalid", "guest returned no summary", 502)
    try:
        validate_summary_quality(outcome.text)
    except SsiosError as exc:
        exc.diagnostic = {"classification": "low_quality", "content": outcome.text, "thinking": ""}
        raise
    return {
        "trace": ["guest.prompt", "host.model"],
        "final_text": outcome.text,
        "runtime_backend": outcome.backend,
        "isolated": True,
        "boot_ms": outcome.boot_ms,
    }


def serve_model(plane, session: GuestSession, cancel, deadline: float) -> None:
    request_path = session.root / "scratch" / "request.json"
    response_path = session.root / "scratch" / "response.json"
    while time.monotonic() < deadline and not cancel.is_set():
        payload = _read_json(request_path)
        if isinstance(payload, dict):
            _answer(plane, session, payload, response_path, cancel, deadline)
            return
        time.sleep(0.05)


def run_krunvm(runtime, session: GuestSession, cancel, deadline: float) -> GuestOutcome:
    del deadline
    binary = runtime.binary()
    if not binary:
        return GuestOutcome("", False, runtime.name, None, "krunvm is not installed")
    name = _vm_name(session.task_id)
    image = runtime.settings.guest_image
    started = time.monotonic()
    prepare_timeout = float(getattr(runtime.settings, "guest_prepare_timeout_seconds", 600))
    run_timeout = float(getattr(runtime.settings, "task_timeout_seconds", 30))
    thread = _proxy_thread(runtime, session, cancel, time.monotonic() + prepare_timeout + run_timeout)
    records: list[dict] = []
    outcome = GuestOutcome("", False, runtime.name, None, "krunvm did not produce guest output", records)
    try:
        created = _run(
            krunvm_create_command(
                binary,
                name=name,
                image=image,
                input_dir=session.root / "input",
                scratch_dir=session.root / "scratch",
                cpus=runtime.settings.cpus,
                memory_mb=runtime.settings.memory_mb,
            ),
            prepare_timeout,
        )
        records.append(_command_record(created))
        if created.returncode != 0:
            outcome = GuestOutcome("", False, runtime.name, _ms(started), _command_detail(created, "krunvm create failed"), records)
            return outcome
        started_guest = _run(krunvm_start_command(binary, name), run_timeout)
        records.append(_command_record(started_guest))
        output = _read_json(session.root / "scratch" / "output.json")
        ran = started_guest.returncode == 0 and isinstance(output, dict) and isinstance(output.get("text"), str)
        outcome = GuestOutcome(
            output.get("text", "") if ran and isinstance(output, dict) else "",
            ran,
            runtime.name,
            _ms(started),
            "guest wrote output.json" if ran else _command_detail(started_guest, "krunvm did not produce guest output"),
            records,
        )
        return outcome
    finally:
        thread.join(timeout=1)
        deleted = _run(krunvm_delete_command(binary, name), 30)
        records.append(_command_record(deleted))
        outcome.commands = records


def doctor_krunvm(runtime) -> dict:
    binary = runtime.binary()
    volume = str(getattr(runtime.settings, "macos_vm_volume", "/Volumes/krunvm"))
    checks = {
        "krunvm": _tool_version(binary, ["--version"]) if binary else {"ok": False, "detail": "krunvm is not installed"},
        "buildah": _tool_version(shutil.which("buildah"), ["--version"]),
        "volume": volume_report(volume),
    }
    checks["volume"]["blocking"] = False
    base = {
        "backend": runtime.name,
        "isolated": False,
        "booted": False,
        "boot_ms": None,
        "host_shell_fallback": False,
        "checks": checks,
        "detail": "volume check does not block krunvm when the tool already runs",
    }
    if not binary:
        base["detail"] = "krunvm is not installed"
        return base
    name = "ssiosdoc" + os.urandom(3).hex()
    image = runtime.settings.guest_image
    for folder in ("doctor-input", "doctor-scratch"):
        (runtime.settings.state_dir / "storage" / folder).mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    created = _run(
        krunvm_create_command(
            binary,
            name=name,
            image=image,
            input_dir=runtime.settings.state_dir / "storage" / "doctor-input",
            scratch_dir=runtime.settings.state_dir / "storage" / "doctor-scratch",
            cpus=1,
            memory_mb=256,
        ),
        float(getattr(runtime.settings, "guest_prepare_timeout_seconds", 600)),
    )
    try:
        if created.returncode != 0:
            detail = _command_detail(created, "krunvm create failed")
            base["commands"] = [_command_record(created)]
            return {**base, "detail": detail}
        echoed = _run(krunvm_echo_command(binary, name), 60)
        text = echoed.stdout.decode("utf-8", "replace")
        booted = echoed.returncode == 0 and "ok" in text.split()
        return {
            **base,
            "booted": booted,
            "isolated": booted,
            "boot_ms": _ms(started),
            "detail": "guest printed ok" if booted else "guest did not print ok",
        }
    finally:
        _run(krunvm_delete_command(binary, name), 30)


def run_cloud_hypervisor(runtime, session: GuestSession, cancel, deadline: float) -> GuestOutcome:
    report = runtime.worker_requirements()
    if not report["ok"]:
        return GuestOutcome("", False, runtime.name, None, report["detail"])
    started = time.monotonic()
    thread = _proxy_thread(runtime, session, cancel, deadline)
    sockets = []
    daemons = []
    try:
        for tag, path in (("input", session.root / "input"), ("scratch", session.root / "scratch")):
            sock = session.root / f"{tag}.sock"
            sockets.append((tag, sock))
            proc = subprocess.Popen(
                virtiofsd_share_command(runtime.settings.virtiofsd_bin, sock, path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            daemons.append(proc)
        if not _wait_sockets([sock for _tag, sock in sockets], deadline):
            return GuestOutcome("", False, runtime.name, _ms(started), "virtiofsd did not open a socket")
        command = cloud_hypervisor_guest_command(
            binary=runtime._located(runtime.settings.cloud_hypervisor_bin) or runtime.settings.cloud_hypervisor_bin,
            kernel=runtime.settings.kernel_path,
            initramfs=runtime.settings.initramfs_path,
            api_socket=session.root / "api.sock",
            cpus=runtime.settings.cpus,
            memory_mb=runtime.settings.memory_mb,
            filesystems=sockets,
            serial=session.root / "serial.log",
        )
        completed = _run(command, max(1.0, deadline - time.monotonic()))
        output = _read_json(session.root / "scratch" / "output.json")
        ran = completed.returncode == 0 and isinstance(output, dict) and isinstance(output.get("text"), str)
        return GuestOutcome(
            output.get("text", "") if ran and isinstance(output, dict) else "",
            ran,
            runtime.name,
            _ms(started),
            "guest wrote output.json" if ran else "cloud-hypervisor did not produce guest output",
        )
    finally:
        thread.join(timeout=1)
        for proc in daemons:
            proc.terminate()
        for proc in daemons:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def doctor_cloud_hypervisor(runtime) -> dict:
    base = {"backend": runtime.name, "isolated": False, "booted": False, "boot_ms": None, "host_shell_fallback": False, "network": "deny"}
    ready = runtime.echo_requirements()
    if not ready["ok"]:
        return {**base, "detail": ready["detail"]}
    started = time.monotonic()
    serial = runtime.settings.state_dir / "storage" / "doctor-serial.log"
    serial.parent.mkdir(parents=True, exist_ok=True)
    command = cloud_hypervisor_guest_command(
        binary=ready["binary"],
        kernel=runtime.settings.kernel_path,
        initramfs=ready["initramfs"],
        api_socket=runtime.settings.state_dir / "storage" / "doctor-api.sock",
        cpus=1,
        memory_mb=256,
        filesystems=[],
        serial=serial,
    )
    completed = _run(command, 30)
    text = serial.read_text(encoding="utf-8", errors="replace") if serial.is_file() else ""
    booted = completed.returncode == 0 and any(line.strip() == "ok" for line in text.splitlines())
    return {
        **base,
        "booted": booted,
        "isolated": booted,
        "boot_ms": _ms(started),
        "detail": "guest printed ok" if booted else "guest did not print ok",
    }


def _prepare(plane, task: dict, token: str, deadline: float) -> GuestSession:
    grant, plain = _grant(plane, task, token, deadline)
    root = plane.settings.state_dir / "storage" / "tasks" / task["id"] / "guest"
    input_dir = root / "input"
    scratch = root / "scratch"
    if root.exists():
        shutil.rmtree(root)
    input_dir.mkdir(parents=True, mode=0o755)
    scratch.mkdir(mode=0o755)
    (input_dir / "grant.json").write_text(json.dumps(grant, ensure_ascii=False), encoding="utf-8")
    os.chmod(input_dir / "grant.json", 0o644)
    shutil.copyfile(WORKER, scratch / "worker.py")
    leaked = token.encode("utf-8")
    for path in root.rglob("*"):
        if path.is_file() and leaked in path.read_bytes():
            raise SsiosError("guest_rejected", "guest directory contained the task token", 500)
    return GuestSession(root=root, plain=plain, grant=grant, task_id=task["id"], kind=task["kind"], project_id=task["project_id"])


def _grant(plane, task: dict, token: str, deadline: float) -> tuple[dict, bool]:
    topic = task.get("topic")
    if topic:
        notes = plane.topic_notes(task["project_id"], str(topic))
        if not notes:
            raise SsiosError("no_content", "no real notes to summarize", 422)
        material = readable_material({"files": notes})
        plain = bool(getattr(plane.llm, "wants_plain_summary", None) and plane.llm.wants_plain_summary(deadline))
        system = PLAIN_PROMPT if plain else TOPIC_PROMPT
    else:
        files = []
        paths = plane.upstream.list_markdown(task["project_id"]) if plane._uses_upstream() else plane.memory.list_markdown(task["project_id"])
        for path in paths:
            if path.endswith("/MEMORY.md") or path == "MEMORY.md":
                continue
            try:
                doc = plane.read_memory(token, task["project_id"], path)
            except SsiosError:
                continue
            content = str(doc.get("content") or "")
            if is_template_boilerplate(path, content):
                continue
            files.append({"path": path, "content": content})
        granted = plane.read_input(token, task["project_id"], task["input_revision"], task["input_path"])
        files.append({"path": task["input_path"], "content": str(granted.get("content") or "")})
        material = readable_material({"files": files})
        plain = False
        system = PLAIN_PROMPT
    user = "Summarize only these notes.\n\n" + cap_note_text(material, plane.settings.max_note_chars)
    return {"system": system, "user": user}, plain


def _answer(plane, session: GuestSession, payload: dict, response_path: Path, cancel, deadline: float) -> None:
    system = payload.get("system")
    user = payload.get("user")
    if system != session.grant.get("system") or user != session.grant.get("user"):
        _write_json(response_path, {"error": "prompt_rejected"})
        return
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        if session.plain:
            text = plane.llm.complete_prose(messages, kind=session.kind, cancel_event=cancel, deadline=deadline)
        else:
            response = plane.llm.complete(messages, kind=session.kind, cancel_event=cancel, deadline=deadline)
            if response.raw_approve_flag:
                plane.audit.write(
                    ts=plane.now(),
                    actor_role="task",
                    actor_id=session.task_id,
                    action="model_approve_flag_ignored",
                    result="ignored",
                    task_id=session.task_id,
                    project_id=session.project_id,
                )
            if response.kind != "final" or not response.text:
                _write_json(response_path, {"error": "model_output_invalid"})
                return
            text = response.text
    except (SsiosError, FlowInterrupt) as exc:
        _write_json(response_path, {"error": getattr(exc, "code", "model_failed")})
        return
    _write_json(response_path, {"text": text})


def _proxy_thread(runtime, session: GuestSession, cancel, deadline: float) -> threading.Thread:
    plane = runtime.plane

    def watch() -> None:
        try:
            serve_model(plane, session, cancel, deadline)
        except Exception:
            _write_json(session.root / "scratch" / "response.json", {"error": "model_failed"})

    thread = threading.Thread(target=watch, name=f"proxy-{session.task_id}", daemon=True)
    thread.start()
    return thread


def _runtime(plane):
    custom = getattr(plane, "guest_runtime", None)
    if custom is not None:
        return custom
    from ssios.runtime import build_runtime

    return build_runtime(plane.settings)


def _stamp(plane, task_id: str, backend: str, isolated: bool, boot_ms: int | None) -> None:
    with plane.lock:
        task = plane.tasks.get(task_id)
        if task is None:
            return
        task["runtime_backend"] = backend
        task["isolated"] = bool(isolated)
        task["boot_ms"] = boot_ms
        plane._persist_tasks()


def _vm_name(task_id: str) -> str:
    hex_part = task_id.removeprefix("task-")
    return "ssios" + "".join(char for char in hex_part if char in "0123456789abcdef")[:12]


def prepare_guest_image(runtime) -> dict:
    """Pull the configured OCI image. This is separate from the task timeout."""
    binary = runtime.binary() if hasattr(runtime, "binary") else None
    if runtime.name != "macos-vm":
        return {"ok": False, "backend": runtime.name, "detail": "vm-prepare pulls the krunvm image. This backend does not use it."}
    if not binary:
        return {"ok": False, "backend": runtime.name, "detail": "krunvm is not installed"}
    name = "ssios-prepare"
    image = runtime.settings.guest_image
    timeout = float(getattr(runtime.settings, "guest_prepare_timeout_seconds", 600))
    created = _run(krunvm_prepare_command(binary, name, image), timeout)
    removed = _run(krunvm_delete_command(binary, name), 30)
    commands = [_command_record(created), _command_record(removed)]
    ok = created.returncode == 0 or b"already exists" in (created.stdout or b"") + (created.stderr or b"")
    return {
        "ok": ok,
        "backend": runtime.name,
        "image": image,
        "boot_ms": None,
        "detail": "image is cached" if ok else _command_detail(created, "krunvm create failed"),
        "commands": commands,
    }


def volume_report(path: str) -> dict:
    """Read-only check. Never creates or attaches a disk."""
    if not path.startswith("/"):
        return {"ok": False, "mounted": False, "case_sensitive": False, "blocking": False, "detail": "macos_vm_volume must be absolute"}
    if not Path(path).is_dir():
        return {
            "ok": False,
            "mounted": False,
            "case_sensitive": False,
            "blocking": False,
            "path": path,
            "detail": f"{path} is not mounted. Attach the case-sensitive sparse image yourself. ssios does not run hdiutil.",
        }
    if sys.platform != "darwin" or shutil.which("diskutil") is None:
        return {
            "ok": None,
            "mounted": True,
            "case_sensitive": None,
            "blocking": False,
            "path": path,
            "detail": "diskutil info is only used on macOS",
        }
    completed = subprocess.run(["diskutil", "info", path], check=False, capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
    text = ((completed.stdout or b"") + b"\n" + (completed.stderr or b"")).decode("utf-8", "replace")
    parsed = interpret_diskutil(text, completed.returncode)
    parsed["path"] = path
    parsed["blocking"] = False
    return parsed


def interpret_diskutil(text: str, returncode: int) -> dict:
    if returncode != 0:
        return {"ok": False, "mounted": False, "case_sensitive": False, "detail": "diskutil info failed"}
    mounted = any(line.strip().startswith("Mounted:") and "Yes" in line for line in text.splitlines())
    case_sensitive = "case-sensitive" in text.casefold()
    if not mounted:
        detail = "diskutil info does not show the volume as mounted"
    elif not case_sensitive:
        detail = "diskutil info does not show Case-sensitive APFS"
    else:
        detail = "mounted and case-sensitive"
    return {"ok": mounted and case_sensitive, "mounted": mounted, "case_sensitive": case_sensitive, "detail": detail}


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=max(1.0, timeout),
            stdin=subprocess.DEVNULL,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or b""
        return subprocess.CompletedProcess(command, 124, exc.stdout or b"", stderr + b"\ntimeout")


def _command_record(completed: subprocess.CompletedProcess[bytes]) -> dict:
    return {
        "argv": [str(part) for part in completed.args],
        "exit_code": completed.returncode,
        "stdout_tail": _tail(completed.stdout),
        "stderr_tail": _tail(completed.stderr),
    }


def _command_detail(completed: subprocess.CompletedProcess[bytes], message: str) -> str:
    tail = _tail(completed.stderr) or _tail(completed.stdout)
    text = message if not tail else f"{message}: {tail}"
    return text[:500]


def _tail(raw: bytes | None) -> str:
    if not raw:
        return ""
    return raw.decode("utf-8", "replace")[-400:]


def _tool_version(binary: str | None, args: list[str]) -> dict:
    if not binary:
        return {"ok": False, "detail": "not installed"}
    completed = _run([binary, *args], 10)
    text = _tail(completed.stdout) or _tail(completed.stderr)
    return {"ok": completed.returncode == 0, "path": binary, "version": text.strip(), "detail": text.strip() or "no version output"}


def redact_text(text: str, secrets: list[str]) -> str:
    cleaned = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            cleaned = cleaned.replace(secret, "[redacted]")
    cleaned = re.sub(r"(?i)(bearer\s+|token[\"']?\s*[:=]\s*)\S+", r"\1[redacted]", cleaned)
    cleaned = re.sub(r"ya29\.[0-9A-Za-z._\-]+", "[redacted]", cleaned)
    cleaned = re.sub(r"1//[0-9A-Za-z._\-]+", "[redacted]", cleaned)
    cleaned = re.sub(
        r"(?i)(\"(?:refresh_token|client_secret|access_token)\"\s*:\s*\")[^\"]+",
        r"\1[redacted]",
        cleaned,
    )
    return cleaned


def _prepare_codex(plane, task: dict, token: str, grant: dict, binary: Path) -> GuestSession:
    raw_root = task.get("guest_root")
    if raw_root:
        root = Path(str(raw_root)).resolve()
        allowed = (plane.settings.state_dir / "accounts").resolve()
        if allowed not in root.parents:
            raise SsiosError("path_rejected", "guest root is outside the account workspace", 403)
    else:
        root = plane.settings.state_dir / "storage" / "tasks" / task["id"] / "guest"
    if root.exists():
        shutil.rmtree(root)
    input_dir = root / "input"
    scratch = root / "scratch"
    workspace = root / "workspace"
    tools = root / "tools"
    home = scratch / "codex-home"
    for path in (input_dir, scratch, workspace, tools, home):
        path.mkdir(parents=True, mode=0o755)
    (input_dir / "grant.json").write_text(json.dumps(grant, ensure_ascii=False), encoding="utf-8")
    (input_dir / "prompt.txt").write_text(grant["user"], encoding="utf-8")
    (workspace / "notes.md").write_text(grant["user"], encoding="utf-8")
    # Codex's bubblewrap setup mounts these paths even for a throwaway workspace.
    for name in (".git", ".agents", ".codex", ".aws"):
        (workspace / name).mkdir()
    # The Codex sandbox runs the command as a user that is not the directory owner.
    workspace.chmod(0o777)
    from ssios.codex_guest import VSOCK_PORT, codex_config_text, guest_entry_source

    (home / "config.toml").write_text(codex_config_text(VSOCK_PORT, plane.settings.codex_model), encoding="utf-8")
    (scratch / "entry.py").write_text(guest_entry_source(VSOCK_PORT), encoding="utf-8")
    from ssios.codex_guest import mcp_bridge_source

    (tools / "ssios_mcp.py").write_text(mcp_bridge_source(VSOCK_PORT), encoding="utf-8")
    destination = tools / "codex"
    shutil.copy2(binary, destination)
    destination.chmod(0o755)
    leaked = token.encode("utf-8")
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and leaked in path.read_bytes():
            raise SsiosError("guest_rejected", "guest directory contained the task token", 500)
    return GuestSession(
        root=root,
        plain=False,
        grant=grant,
        task_id=task["id"],
        kind=task["kind"],
        project_id=task["project_id"],
        mode="codex",
    )


def _ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _wait_sockets(paths: list[Path], deadline: float) -> bool:
    while time.monotonic() < deadline:
        if all(path.exists() for path in paths):
            return True
        time.sleep(0.05)
    return False


def _read_json(path: Path) -> dict | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def run_worker_subprocess(session: GuestSession) -> int:
    """Run the worker on the host. This is not a microVM and must not be marked isolated."""
    env = os.environ.copy()
    env["SSIOS_INPUT"] = str(session.root / "input" / "grant.json")
    env["SSIOS_SCRATCH"] = str(session.root / "scratch")
    env["SSIOS_WAIT_SECONDS"] = "10"
    return _run_worker_env(env)


def _run_worker_env(env: dict) -> int:
    completed = subprocess.run(
        [os.environ.get("PYTHON", "python3"), str(WORKER)],
        check=False,
        capture_output=True,
        env=env,
        timeout=20,
        shell=False,
    )
    return completed.returncode
