"""Host side of a guest summary.

The guest builds one prompt from the granted notes and writes it to the scratch
directory. This process calls the configured local model and writes the answer
back. The guest copies that answer to output.json. The guest directory never
receives the human token, and the guest is not given a network endpoint.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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


@dataclass
class GuestSession:
    root: Path
    plain: bool
    grant: dict
    task_id: str
    kind: str
    project_id: str


def execute_guest(plane, task: dict, token: str, cancel, deadline: float) -> dict:
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
        raise SsiosError("microvm_fail_closed", outcome.detail or "the guest did not run", 503)
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
    binary = runtime.binary()
    if not binary:
        return GuestOutcome("", False, runtime.name, None, "krunvm is not installed")
    name = _vm_name(session.task_id)
    image = runtime.settings.guest_image
    started = time.monotonic()
    thread = _proxy_thread(runtime, session, cancel, deadline)
    create = krunvm_create_command(
        binary,
        name=name,
        image=image,
        input_dir=session.root / "input",
        scratch_dir=session.root / "scratch",
        cpus=runtime.settings.cpus,
        memory_mb=runtime.settings.memory_mb,
    )
    try:
        created = _run(create, deadline)
        if created.returncode != 0:
            return GuestOutcome("", False, runtime.name, _ms(started), "krunvm create failed")
        started_guest = _run(krunvm_start_command(binary, name), deadline)
        output = _read_json(session.root / "scratch" / "output.json")
        ran = started_guest.returncode == 0 and isinstance(output, dict) and isinstance(output.get("text"), str)
        return GuestOutcome(
            output.get("text", "") if ran and isinstance(output, dict) else "",
            ran,
            runtime.name,
            _ms(started),
            "guest wrote output.json" if ran else "krunvm did not produce guest output",
        )
    finally:
        thread.join(timeout=1)
        _run(krunvm_delete_command(binary, name), time.monotonic() + 30)


def doctor_krunvm(runtime) -> dict:
    binary = runtime.binary()
    base = {"backend": runtime.name, "isolated": False, "booted": False, "boot_ms": None, "host_shell_fallback": False}
    if not binary:
        return {**base, "detail": "krunvm is not installed"}
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
        time.monotonic() + 180,
    )
    try:
        if created.returncode != 0:
            detail = created.stderr.decode("utf-8", "replace")[-240:]
            return {**base, "detail": detail or "krunvm create failed"}
        echoed = _run(krunvm_echo_command(binary, name), time.monotonic() + 60)
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
        _run(krunvm_delete_command(binary, name), time.monotonic() + 30)


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
        completed = _run(command, deadline)
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
    completed = _run(command, time.monotonic() + 30)
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


def _run(command: list[str], deadline: float) -> subprocess.CompletedProcess[bytes]:
    timeout = max(1.0, deadline - time.monotonic())
    try:
        return subprocess.run(command, check=False, capture_output=True, timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, b"", b"timeout")


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
