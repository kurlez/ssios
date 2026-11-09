"""Task runtime.

dev-guard runs the trusted agent loop in-process. It is a development control,
not a microVM. The Cloud Hypervisor adapter builds a real argv and fails closed.
It never executes the task on the host.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ssios.errors import SsiosError

EXECUTION_IMPLEMENTED = False


@dataclass(frozen=True)
class Share:
    tag: str
    path: Path
    readonly: bool


class MacOSVMRuntime:
    """Preflight and argv for krunvm. This build does not boot a guest."""

    name = "macos-vm"
    tool = "krunvm"
    pinned_note = "Install with Homebrew and check the version before trusting it: brew install krunvm. This repository does not vendor krunvm."

    def __init__(self, settings) -> None:
        self.settings = settings

    def preflight(self) -> dict:
        binary = shutil.which(self.tool)
        return {
            "ok": False,
            "backend": self.name,
            "microvm": True,
            "execution_implemented": False,
            "isolated": False,
            "host_shell_fallback": False,
            "tool": self.tool,
            "binary": binary,
            "binary_found": binary is not None,
            "framework": "Hypervisor.framework via libkrun",
            "note": self.pinned_note,
            "detail": "macOS guest execution is not performed by this process. Run ssios vm-doctor on the Mac.",
        }

    def command(self) -> list[str]:
        binary = shutil.which(self.tool) or self.tool
        return [binary, "list"]

    def launch(self, host_workload=None) -> None:
        del host_workload
        raise SsiosError(
            "microvm_fail_closed",
            "macos-vm did not start a guest; the task was not run on the host",
            503,
        )


def build_runtime(settings):
    if settings.runtime_backend == "dev-guard":
        return DevGuardRuntime()
    if settings.runtime_backend == "macos-vm":
        return MacOSVMRuntime(settings)
    if settings.runtime_backend == "cloud-hypervisor":
        return CloudHypervisorRuntime(settings)
    raise SsiosError("config_invalid", "runtime backend is not recognized", 500)


class DevGuardRuntime:
    name = "dev-guard"

    def describe(self) -> dict:
        return {
            "ok": True,
            "backend": self.name,
            "microvm": False,
            "isolation": "process-jail",
            "host_shell_fallback": False,
            "note": "Development control only. This is not a virtual machine boundary.",
        }


class CloudHypervisorRuntime:
    name = "cloud-hypervisor"

    def __init__(self, settings) -> None:
        self.settings = settings

    def preflight(self) -> dict:
        checks = {
            "kvm": self._kvm(),
            "cloud_hypervisor": self._binary(self.settings.cloud_hypervisor_bin),
            "virtiofsd": self._binary(self.settings.virtiofsd_bin),
            "kernel": self._regular_file("kernel", self.settings.kernel_path),
            "root_image": self._regular_file("root image", self.settings.root_image_path),
            "guest_workload": {
                "ok": False,
                "detail": "guest agent protocol is not implemented in this prototype",
            },
        }
        requirement_names = ("kvm", "cloud_hypervisor", "virtiofsd", "kernel", "root_image")
        requirements_met = all(checks[name]["ok"] for name in requirement_names)
        return {
            "ok": False,
            "backend": self.name,
            "microvm": True,
            "execution_implemented": EXECUTION_IMPLEMENTED,
            "requirements_met": requirements_met and EXECUTION_IMPLEMENTED,
            "requirements_present": requirements_met,
            "host_shell_fallback": False,
            "network": "deny",
            "checks": checks,
            "arch_note": "Cloud Hypervisor needs /dev/kvm. macOS hosts do not provide it. Nested KVM inside an Apple Silicon VM is not assumed.",
        }

    def launch(self, host_workload=None) -> None:
        report = self.preflight()
        del host_workload
        del report
        raise SsiosError(
            "microvm_fail_closed",
            "cloud hypervisor task execution is fail-closed; the host workload was not started",
            503,
        )

    def _kvm(self) -> dict:
        path = Path(self.settings.kvm_path)
        if not path.exists():
            return {"ok": False, "path": str(path), "detail": "kvm device is absent"}
        if not os.access(path, os.R_OK | os.W_OK):
            return {"ok": False, "path": str(path), "detail": "kvm device is not openable"}
        return {"ok": True, "path": str(path), "detail": "present"}

    def _binary(self, name: str) -> dict:
        if not name or any(char in name for char in "\n\r"):
            return {"ok": False, "detail": "not configured"}
        candidate = Path(name)
        if candidate.is_absolute():
            found = candidate.is_file() and os.access(candidate, os.X_OK) and not candidate.is_symlink()
            location = str(candidate) if found else None
        else:
            location = shutil.which(name)
            found = location is not None
        return {"ok": found, "location": location, "detail": "found" if found else "not found"}

    @staticmethod
    def _regular_file(label: str, configured: str) -> dict:
        if not configured:
            return {"ok": False, "detail": f"{label} is not configured"}
        path = Path(configured)
        if path.is_symlink() or not path.is_file():
            return {"ok": False, "detail": f"{label} is missing"}
        return {"ok": True, "detail": "present", "path": str(path.resolve())}


def plan_shares(state_dir: Path, task_id: str, project_id: str, revision: str) -> tuple[list[Share], Path]:
    export = (state_dir / "storage" / "exports" / project_id / revision).resolve()
    staging = (state_dir / "storage" / "tasks" / task_id / "staged").resolve()
    disk = (state_dir / "storage" / "tasks" / task_id / "disk.raw").resolve()
    assert_share_allowed(state_dir, export)
    assert_share_allowed(state_dir, staging)
    assert_share_allowed(state_dir, disk)
    return [Share("input", export, True), Share("staging", staging, False)], disk


def assert_share_allowed(state_dir: Path, path: Path) -> None:
    resolved = path.resolve(strict=False)
    home = Path.home().resolve()
    state = state_dir.resolve()
    if resolved == home or home in resolved.parents or state not in resolved.parents:
        raise SsiosError("share_forbidden", "share path is outside project storage", 403)
    relative = resolved.relative_to(state)
    if not relative.parts or relative.parts[0] in {"vault", "secrets", "auth", "memory", "git-hooks"}:
        raise SsiosError("share_forbidden", "share path is forbidden", 403)
    if relative.parts[0] != "storage":
        raise SsiosError("share_forbidden", "share path is outside project storage", 403)
    if any(part in {"..", ".git"} or part.startswith("-") for part in relative.parts):
        raise SsiosError("share_forbidden", "share path is forbidden", 403)


def virtiofsd_command(binary: str, socket_path: Path, share: Share) -> list[str]:
    _reject_option_text(share.tag)
    _reject_option_text(str(socket_path))
    _reject_option_text(str(share.path))
    argv = [
        binary,
        "--socket-path",
        str(socket_path),
        "--shared-dir",
        str(share.path),
        "--cache",
        "never",
        "--sandbox",
        "namespace",
    ]
    if share.readonly:
        argv.append("--readonly")
    return argv


def cloud_hypervisor_command(
    *,
    binary: str,
    api_socket: Path,
    cpus: int,
    memory_mb: int,
    kernel: str,
    disk: Path,
    filesystems: list[tuple[Share, Path]],
    network: bool = False,
) -> list[str]:
    if network:
        raise SsiosError("network_denied", "guest network is denied", 403)
    if cpus < 1 or memory_mb < 128:
        raise SsiosError("runtime_invalid", "microvm size rejected", 400)
    _reject_option_text(kernel)
    _reject_option_text(str(disk))
    _reject_option_text(str(api_socket))
    argv = [
        binary,
        "--api-socket",
        str(api_socket),
        "--cpus",
        f"boot={cpus}",
        "--memory",
        f"size={memory_mb}M,shared=on",
        "--kernel",
        kernel,
        "--disk",
        f"path={disk},readonly=off",
        "--serial",
        "off",
        "--console",
        "off",
    ]
    for share, socket in filesystems:
        _reject_option_text(share.tag)
        _reject_option_text(str(socket))
        argv.extend(["--fs", f"tag={share.tag},socket={socket}"])
    if "--net" in argv:
        raise SsiosError("network_denied", "guest network is denied", 403)
    return argv


def snapshot_refuse(state_dir: Path, paths: list[Path], destination: Path) -> None:
    del destination
    for raw in paths:
        resolved = raw.resolve(strict=False)
        if _snapshot_forbidden(state_dir, resolved):
            raise SsiosError("snapshot_forbidden", "snapshot path is forbidden", 403)
    raise SsiosError("snapshot_not_implemented", "single-use clean pool is not implemented", 501)


def _snapshot_forbidden(state_dir: Path, resolved: Path) -> bool:
    home = Path.home().resolve()
    state = state_dir.resolve()
    if resolved == home or home in resolved.parents:
        return True
    if "token" in resolved.name:
        return True
    if state != resolved and state not in resolved.parents:
        return True
    if state == resolved:
        return True
    relative = resolved.relative_to(state)
    if not relative.parts:
        return True
    return relative.parts[0] in {"vault", "secrets", "auth", "memory", "git-hooks"}


def _reject_option_text(value: str) -> None:
    if any(char in value for char in ",\n\r\"'\\"):
        raise SsiosError("runtime_invalid", "runtime path rejected", 400)
