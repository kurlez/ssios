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
    tools: object = None
    cancel: object = None


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
        outcome = _boot(plane, request, root, backend, getattr(request, "tools", None))
        checks = read_checks(root)
        on_status({"phase": "isolation", "backend": backend, "checks": checks})
        if not _checks_pass(checks) or not outcome.isolated:
            code = outcome.error_code or "microvm_fail_closed"
            detail = (outcome.detail or "the guest did not pass the isolation checks").strip()
            raise SsiosError(code, detail[:500], 503)
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
    from ssios.macos_vz import state_path_report

    path_report = state_path_report(Path(settings.state_dir))
    report["state_path"] = path_report
    if not path_report["ok"]:
        report["detail"] = path_report["detail"]
        return report
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


def _boot(plane, request: ChatGuestRequest, root: Path, backend: str, tool_handler=None):
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
    from ssios.calendar_intent import clock_block

    grant = {
        "system": "You are the SSIOS guest. Stay inside the workspace. Never invent a date, a weekday, or a time.\n\n" + clock_block(plane, {"id": request.account_id}),
        "user": request.prompt,
    }
    session = _prepare_codex(plane, task, token, grant, binary)
    cancel = threading.Event()
    deadline = time.monotonic() + float(plane.settings.task_timeout_seconds) + float(plane.settings.guest_prepare_timeout_seconds)
    if backend == "macos-vz":
        from ssios.macos_vz import boot_vfkit, prepared_initramfs, prepared_kernel
        from ssios.runtime import MacOSVZRuntime

        runtime = MacOSVZRuntime(plane.settings)
        runtime.plane = plane
        upstream = _upstream(plane)
        return boot_vfkit(
            runtime,
            session,
            "",
            cancel,
            deadline,
            prepared_kernel(plane.settings.state_dir),
            prepared_initramfs(plane.settings.state_dir),
            upstream,
            tool_handler,
        )
    from ssios.codex_guest import _boot_codex
    from ssios.linux_guest import build_codex_initramfs
    from ssios.runtime import CloudHypervisorRuntime

    runtime = CloudHypervisorRuntime(plane.settings)
    runtime.plane = plane
    initramfs = plane.settings.state_dir / "storage" / "codex-initrd.img"
    if not initramfs.is_file():
        build_codex_initramfs(initramfs)
    return _boot_codex(runtime, session, "", cancel, deadline, initramfs, _upstream(plane), tool_handler)


def vm_diagnose(settings) -> dict:
    """Redacted guest summary. Paths and presence only, plus one exception line. No tokens."""
    import urllib.error
    import urllib.request

    state = Path(settings.state_dir)
    kernel = state / "cache" / "macos-vz" / "Image-arm64"
    initramfs = state / "cache" / "macos-vz" / "codex-initrd.img"
    codex = _codex_binary(state)
    vfkit = _which(getattr(settings, "vfkit_bin", "") or "vfkit")
    report = chat_guest_report(settings)
    ollama = _ollama_probe(getattr(settings, "ollama_base_url", ""), urllib.request, urllib.error)
    latest = _latest_guest_error(state)
    return {
        "ok": bool(report.get("ok")) and ollama.get("reachable") is not False,
        "backend": settings.runtime_backend,
        "agent": settings.runtime_agent,
        "memory_mb": settings.memory_mb,
        "task_timeout_seconds": settings.task_timeout_seconds,
        "ollama_model": settings.ollama_model,
        "codex_model": settings.codex_model,
        "guest_model": settings.codex_model,
        "guest_num_ctx": int(getattr(settings, "codex_num_ctx", 16384) or 16384),
        "ollama": ollama,
        "files": {
            "vfkit": {"present": bool(vfkit), "path": vfkit or ""},
            "kernel": _file_fact(kernel),
            "initramfs": _file_fact(initramfs),
            "codex": _file_fact(codex) if codex else {"present": False, "path": "", "bytes": 0},
        },
        "prerequisites": {"ok": report.get("ok"), "detail": _redact(str(report.get("detail") or ""))},
        "last_vm_task": latest,
        "logs": {
            "ui": "The ssios ui process prints to the terminal that started it. listen.url in the state directory has the address only.",
            "tasks": "accounts/<id>/tasks.json holds the last error_code and error_detail for that account.",
            "guest": "accounts/<id>/workspace/tasks/<task-id>/guest/serial.log is the vfkit console. proxy.log is beside it. This report does not print those files.",
        },
    }


def _codex_binary(state: Path) -> Path | None:
    root = state / "cache" / "codex"
    if not root.is_dir():
        return None
    found = sorted(path for path in root.glob("*/*") if path.is_file() and not path.is_symlink() and path.suffix != ".tar.gz")
    return found[0] if found else None


def _which(name: str) -> str:
    import shutil

    return shutil.which(name) or ""


def _file_fact(path: Path) -> dict:
    present = path.is_file() and not path.is_symlink()
    return {"present": present, "path": str(path), "bytes": path.stat().st_size if present else 0}


def _ollama_probe(base: str, request_mod, error_mod) -> dict:
    url = str(base or "").rstrip("/")
    if not url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        return {"reachable": False, "detail": "ollama URL is not a loopback address, so it was not contacted"}
    try:
        with request_mod.urlopen(url + "/api/tags", timeout=3) as response:
            raw = response.read(65536)
    except error_mod.HTTPError as exc:
        exc.read()
        return {"reachable": False, "detail": f"ollama http {exc.code}"}
    except (error_mod.URLError, TimeoutError, OSError):
        return {"reachable": False, "detail": "ollama is unreachable"}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"reachable": True, "detail": "ollama answered, but the model list was not readable"}
    names = []
    if isinstance(body, dict):
        for item in body.get("models") or []:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"])[:80])
    return {"reachable": True, "models": names[:20], "detail": "ollama answered /api/tags"}


def _latest_guest_error(state: Path) -> dict:
    newest: tuple[float, Path] | None = None
    if state.is_dir():
        for path in state.glob("accounts/*/workspace/tasks/*/guest"):
            if not path.is_dir():
                continue
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
            if newest is None or stamp > newest[0]:
                newest = (stamp, path)
    task_errors = _task_errors(state)
    if newest is None and not task_errors:
        return {"found": False}
    guest = newest[1] if newest else None
    line = ""
    if guest is not None:
        chunks = []
        for relative in ("serial.log", "scratch/codex.err"):
            file = guest / relative
            if file.is_file() and not file.is_symlink() and file.stat().st_size <= 200000:
                chunks.append(file.read_text(encoding="utf-8", errors="replace")[-4000:])
        for row in chunks:
            for candidate in reversed(row.splitlines()):
                stripped = candidate.strip()
                if stripped and ("Error" in stripped or "Exception" in stripped or stripped.startswith("error")):
                    line = stripped
                    break
            if line:
                break
    chosen = task_errors[0] if task_errors else {}
    return {
        "found": True,
        "task_id": guest.parent.name if guest is not None else chosen.get("task_id", ""),
        "error_code": chosen.get("error_code") or "",
        "error_detail": _redact(str(chosen.get("error_detail") or "")),
        "exception_line": _redact(line),
        "serial_log": str(guest / "serial.log") if guest is not None else "",
        "proxy_log": str(guest / "proxy.log") if guest is not None else "",
    }


def _task_errors(state: Path) -> list[dict]:
    rows = []
    if not state.is_dir():
        return rows
    for path in state.glob("accounts/*/tasks.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        tasks = data.get("tasks") if isinstance(data, dict) else {}
        if not isinstance(tasks, dict):
            continue
        for task_id, task in tasks.items():
            if not isinstance(task, dict):
                continue
            if task.get("status") == "failed" or task.get("error_code") or task.get("error_detail"):
                rows.append(
                    {
                        "task_id": str(task_id),
                        "error_code": str(task.get("error_code") or ""),
                        "error_detail": str(task.get("error_detail") or ""),
                    }
                )
    return rows


def _redact(text: str) -> str:
    import re

    cleaned = re.sub(r"(?i)bearer\s+\S+", "bearer [redacted]", text)
    cleaned = re.sub(r"(?i)\bguest-[0-9a-f]{8,}\b", "guest-[redacted]", cleaned)
    cleaned = re.sub(r"\b[0-9a-f]{24,}\b", "[redacted]", cleaned)
    return cleaned[:240]


def _upstream(plane) -> str | None:
    if getattr(plane.llm, "provider", "") != "ollama":
        return None
    return plane.settings.ollama_base_url.rstrip("/") + "/api/chat"
