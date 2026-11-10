"""Chat guest launcher.

The production path uses the backend and agent in the config. macos-vz and
cloud-hypervisor are the no-NIC Codex backends. dev-guard and the summary
agent are refused before any boot status. If the guest cannot start, the
task fails closed and the host agent is not run. The guest root is always
inside that account's workspace.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ssios.errors import SsiosError


@dataclass
class ChatGuestRequest:
    account_id: str
    account_home: Path
    task_id: str
    prompt: str


class ProductionChatGuest:
    def run(self, plane, request: ChatGuestRequest, on_status) -> dict:
        report = chat_guest_report(plane.settings)
        if not report.get("ok"):
            raise SsiosError(
                "microvm_fail_closed",
                report.get("detail") or "microVM backend cannot start a guest; the task was not run on the host",
                503,
            )
        backend = str(plane.settings.runtime_backend)
        on_status({"phase": "booting", "backend": backend})
        root = guest_root(request.account_home, request.task_id)
        on_status({"phase": "running", "backend": backend})
        outcome = _boot(plane, request, root, backend)
        checks = read_checks(root)
        on_status({"phase": "isolation", "backend": backend, "checks": checks})
        if not _checks_pass(checks) or not outcome.isolated:
            raise SsiosError("microvm_fail_closed", "the guest did not pass the isolation checks", 503)
        text = (outcome.text or "").strip()
        if not text:
            raise SsiosError("model_output_invalid", "guest returned no result", 502)
        return {
            "text": text,
            "isolated": True,
            "boot_ms": outcome.boot_ms,
            "backend": outcome.backend or backend,
            "checks": checks,
        }


def guest_root(account_home: Path, task_id: str) -> Path:
    if not task_id or "/" in task_id or ".." in task_id:
        raise SsiosError("path_rejected", "task id rejected", 400)
    home = account_home.resolve()
    root = (home / "workspace" / "tasks" / task_id / "guest").resolve()
    if home not in root.parents:
        raise SsiosError("path_rejected", "guest root is outside the account workspace", 403)
    return root


def vm_prepare_command(settings) -> str:
    """The command that prepares the guest for this config. It does not accept --profile."""
    import shlex

    state = shlex.quote(str(settings.state_dir))
    config = getattr(settings, "config_path", None)
    if config:
        return f"PYTHONPATH=src python3 -m ssios vm-prepare --state {state} --config {shlex.quote(str(config))}"
    return f"PYTHONPATH=src python3 -m ssios vm-prepare --state {state}"


def unsuitable_guest_detail(settings) -> str:
    backend = settings.runtime_backend
    agent = settings.runtime_agent
    return (
        f'This config has [runtime] backend = "{backend}" and agent = "{agent}". '
        'A chat guest needs backend = "macos-vz" and agent = "codex" on macOS, '
        'or backend = "cloud-hypervisor" and agent = "codex" on Linux. '
        "Also set memory_mb = 2048 and [limits] task_timeout_seconds = 240. "
        "[llm] ollama_model answers chat and reads calendar requests. "
        "[agent.codex] model is only the model inside the guest. The two names are independent. "
        "vm-prepare does not accept --profile. After saving the config, run: "
        + vm_prepare_command(settings)
    )


def chat_guest_report(settings) -> dict:
    """Prerequisites for the configured backend and agent. Does not boot a guest."""
    backend = settings.runtime_backend
    agent = settings.runtime_agent
    report = {
        "ok": False,
        "backend": backend,
        "agent": agent,
        "ollama_model": settings.ollama_model,
        "codex_model": settings.codex_model,
        "codex_num_ctx": settings.codex_num_ctx,
        "models_independent": True,
        "memory_mb": settings.memory_mb,
        "task_timeout_seconds": settings.task_timeout_seconds,
        "vm_prepare": vm_prepare_command(settings),
        "detail": "",
    }
    if backend not in ("macos-vz", "cloud-hypervisor") or agent != "codex":
        report["detail"] = unsuitable_guest_detail(settings)
        return report
    if int(settings.memory_mb) < 1536:
        report["detail"] = (
            f'This config has memory_mb = {settings.memory_mb}. '
            'Set [runtime] memory_mb = 2048. The guest needs at least 1536 MB.'
        )
        return report
    try:
        from ssios.codex_guest import prepared_binary

        prepared_binary(settings.state_dir)
    except SsiosError as exc:
        report["detail"] = (
            "The pinned Codex binary is missing for this state directory. "
            f'This config has backend = "{backend}" and agent = "codex", so this command prepares it: '
            + vm_prepare_command(settings)
        )
        if exc.code != "codex_not_prepared":
            report["detail"] = exc.detail + " Run: " + vm_prepare_command(settings)
        return report
    ready = readiness(type("Plane", (), {"settings": settings})(), backend)
    if not ready.get("ok"):
        report["detail"] = str(ready.get("detail") or "the guest is not prepared") + " Run: " + vm_prepare_command(settings)
        return report
    report["ok"] = True
    report["detail"] = "chat guest prerequisites are present"
    return report


def readiness(plane, backend: str) -> dict:
    from dataclasses import replace

    from ssios.runtime import build_runtime

    settings = replace(plane.settings, runtime_backend=backend, runtime_agent="codex")
    runtime = build_runtime(settings)
    try:
        from ssios.codex_guest import prepared_binary

        prepared_binary(plane.settings.state_dir)
    except SsiosError as exc:
        return {"ok": False, "detail": exc.detail}
    if backend == "macos-vz":
        return runtime.requirements()
    from ssios.codex_guest import _hypervisor_ready

    return _hypervisor_ready(runtime)


def read_checks(root: Path) -> dict:
    checks = {
        "nic": [],
        "outbound": None,
        "marker": False,
        "sandbox_denied": False,
        "outside_write": False,
    }
    net_path = root / "scratch" / "netcheck.json"
    if net_path.is_file() and not net_path.is_symlink():
        try:
            data = json.loads(net_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            names = data.get("interfaces")
            checks["nic"] = [str(item) for item in names] if isinstance(names, list) else []
            if "outbound" in data:
                checks["outbound"] = bool(data.get("outbound"))
    marker = root / "workspace" / "marker.txt"
    if marker.is_file() and not marker.is_symlink():
        checks["marker"] = b"SSIOS-IN" in marker.read_bytes()
    err = root / "scratch" / "probe.err"
    if err.is_file() and not err.is_symlink():
        text = err.read_text(encoding="utf-8", errors="replace")
        checks["sandbox_denied"] = any(
            phrase in text for phrase in ("Operation not permitted", "Read-only file system", "Permission denied")
        )
    outside = root / "workspace" / "ssios-outside"
    checks["outside_write"] = outside.exists()
    return checks


def _checks_pass(checks: dict) -> bool:
    return (
        checks.get("nic") == ["lo"]
        and checks.get("outbound") is False
        and checks.get("marker") is True
        and checks.get("sandbox_denied") is True
        and checks.get("outside_write") is False
    )


def _boot(plane, request: ChatGuestRequest, root: Path, backend: str):
    from ssios.codex_guest import prepared_binary
    from ssios.vmguest import _prepare_codex

    binary = prepared_binary(plane.settings.state_dir)
    task = {
        "id": request.task_id,
        "kind": "chat",
        "project_id": "chat",
        "guest_root": str(root),
    }
    import secrets

    token = "guest-" + secrets.token_hex(8)
    grant = {"system": "You are the SSIOS guest. Stay inside the workspace.", "user": request.prompt}
    session = _prepare_codex(plane, task, token, grant, binary)
    cancel = threading.Event()
    deadline = time.monotonic() + float(plane.settings.task_timeout_seconds) + float(plane.settings.guest_prepare_timeout_seconds)
    if backend == "macos-vz":
        from ssios.macos_vz import boot_vfkit, prepared_initramfs, prepared_kernel
        from ssios.runtime import MacOSVZRuntime

        runtime = MacOSVZRuntime(plane.settings)
        runtime.plane = plane
        upstream = _upstream(plane)
        return boot_vfkit(runtime, session, "", cancel, deadline, prepared_kernel(plane.settings.state_dir), prepared_initramfs(plane.settings.state_dir), upstream)
    from ssios.codex_guest import _boot_codex
    from ssios.linux_guest import build_codex_initramfs
    from ssios.runtime import CloudHypervisorRuntime

    runtime = CloudHypervisorRuntime(plane.settings)
    runtime.plane = plane
    initramfs = plane.settings.state_dir / "storage" / "codex-initrd.img"
    if not initramfs.is_file():
        build_codex_initramfs(initramfs)
    return _boot_codex(runtime, session, "", cancel, deadline, initramfs, _upstream(plane))


def _upstream(plane) -> str | None:
    if getattr(plane.llm, "provider", "") != "ollama":
        return None
    return plane.settings.ollama_base_url.rstrip("/") + "/api/chat"
