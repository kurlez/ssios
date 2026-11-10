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
# Manifest list for python:3.12-alpine, read from registry-1.docker.io on 2026-11-09.
PYTHON_GUEST_IMAGE = "docker.io/library/python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7"


@dataclass(frozen=True)
class Share:
    tag: str
    path: Path
    readonly: bool


def krunvm_create_command(
    binary: str,
    *,
    name: str,
    image: str,
    input_dir: Path,
    scratch_dir: Path,
    cpus: int,
    memory_mb: int,
) -> list[str]:
    """krunvm has no read-only volume flag. The directories are per-task copies."""
    for value in (name, image, str(input_dir), str(scratch_dir)):
        _reject_option_text(value)
    for value in (str(input_dir), str(scratch_dir)):
        if ":" in value or " " in value:
            raise SsiosError("runtime_invalid", "krunvm path rejected", 400)
    if cpus < 1 or memory_mb < 128:
        raise SsiosError("runtime_invalid", "microvm size rejected", 400)
    return [
        binary,
        "create",
        image,
        "--name",
        name,
        "--cpus",
        str(cpus),
        "--mem",
        str(memory_mb),
        "-v",
        f"{input_dir}:/input",
        "-v",
        f"{scratch_dir}:/scratch",
        "-w",
        "/scratch",
    ]


def krunvm_start_command(binary: str, name: str) -> list[str]:
    _reject_option_text(name)
    return [binary, "start", name, "/usr/local/bin/python3", "--", "/scratch/worker.py"]


def krunvm_echo_command(binary: str, name: str) -> list[str]:
    _reject_option_text(name)
    return [binary, "start", name, "/bin/echo", "--", "ok"]


def krunvm_delete_command(binary: str, name: str) -> list[str]:
    _reject_option_text(name)
    return [binary, "delete", name]


def krunvm_changevm_command(
    binary: str,
    name: str,
    *,
    input_dir: Path,
    scratch_dir: Path,
    cpus: int,
    memory_mb: int,
) -> list[str]:
    """Replace the cached VM's mounts. Do not pass --remove-volumes: 0.2.7 ignores -v when that flag is set."""
    for value in (name, str(input_dir), str(scratch_dir)):
        _reject_option_text(value)
    for value in (str(input_dir), str(scratch_dir)):
        if ":" in value:
            raise SsiosError("runtime_invalid", "krunvm path rejected", 400)
    return [
        binary,
        "changevm",
        name,
        "--cpus",
        str(cpus),
        "--mem",
        str(memory_mb),
        "-v",
        f"{input_dir}:/input",
        "-v",
        f"{scratch_dir}:/scratch",
        "-w",
        "/scratch",
    ]


def krunvm_list_command(binary: str) -> list[str]:
    return [binary, "list"]


def krunvm_prepare_command(binary: str, name: str, image: str) -> list[str]:
    """Warm the buildah cache. Same image string the task will create, no extra mounts."""
    _reject_option_text(name)
    _reject_option_text(image)
    return [binary, "create", image, "--name", name]


def virtiofsd_share_command(binary: str, socket_path: Path, share: Path) -> list[str]:
    """This virtiofsd build has no read-only flag. The caller passes a fresh copy."""
    _reject_option_text(str(socket_path))
    _reject_option_text(str(share))
    return [
        binary,
        "--socket-path",
        str(socket_path),
        "--shared-dir",
        str(share),
        "--sandbox",
        "none",
        "--cache",
        "never",
    ]


def cloud_hypervisor_guest_command(
    *,
    binary: str,
    kernel: str,
    initramfs: str,
    api_socket: Path,
    cpus: int,
    memory_mb: int,
    filesystems: list[tuple[str, Path]],
    serial: Path | None = None,
    vsock: Path | None = None,
) -> list[str]:
    if cpus < 1 or memory_mb < 128:
        raise SsiosError("runtime_invalid", "microvm size rejected", 400)
    for value in (binary, kernel, initramfs, str(api_socket)):
        _reject_option_text(value)
    argv = [
        binary,
        "--kernel",
        kernel,
        "--initramfs",
        initramfs,
        "--cmdline",
        "console=ttyS0 reboot=k panic=1 rdinit=/init",
        "--cpus",
        f"boot={cpus}",
        "--memory",
        f"size={memory_mb}M,shared=on",
        "--console",
        "off",
        "--api-socket",
        str(api_socket),
    ]
    if serial is None:
        argv.extend(["--serial", "off"])
    else:
        _reject_option_text(str(serial))
        argv.extend(["--serial", f"file={serial}"])
    for tag, socket in filesystems:
        _reject_option_text(tag)
        _reject_option_text(str(socket))
        argv.extend(["--fs", f"tag={tag},socket={socket}"])
    if vsock is not None:
        _reject_option_text(str(vsock))
        argv.extend(["--vsock", f"cid=3,socket={vsock}"])
    if "--net" in argv:
        raise SsiosError("network_denied", "guest network is denied", 403)
    return argv


class MacOSVMRuntime:
    """krunvm on Hypervisor.framework. preflight does not boot; vm-doctor and tasks do."""

    name = "macos-vm"
    tool = "krunvm"
    pinned_note = "brew tap slp/krun && brew install krunvm. This repository does not vendor krunvm."

    def __init__(self, settings) -> None:
        self.settings = settings
        self.plane = None

    def binary(self) -> str | None:
        configured = getattr(self.settings, "krunvm_bin", "") or self.tool
        return shutil.which(configured)

    def can_launch(self) -> bool:
        return self.binary() is not None

    def preflight(self) -> dict:
        binary = self.binary()
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
            "guest_image": getattr(self.settings, "guest_image", PYTHON_GUEST_IMAGE),
            "volume_readonly": False,
            "note": self.pinned_note,
            "detail": "preflight does not boot. ssios vm-doctor starts a guest when krunvm is installed.",
        }

    def command(self) -> list[str]:
        return [self.binary() or self.tool, "list"]

    def launch(self, host_workload=None) -> None:
        del host_workload
        raise SsiosError(
            "microvm_fail_closed",
            "macos-vm did not start a guest; the task was not run on the host",
            503,
        )

    def run(self, session, cancel, deadline):
        from ssios.vmguest import run_krunvm

        return run_krunvm(self, session, cancel, deadline)

    def doctor(self) -> dict:
        from ssios.vmguest import doctor_krunvm

        return doctor_krunvm(self)


class MacOSVZRuntime:
    """vfkit on Virtualization.framework. Used only for the Codex guest, with no NIC."""

    name = "macos-vz"

    def __init__(self, settings) -> None:
        self.settings = settings
        self.plane = None

    def binary(self) -> str | None:
        configured = getattr(self.settings, "vfkit_bin", "") or "vfkit"
        return shutil.which(configured)

    def requirements(self) -> dict:
        from ssios.errors import SsiosError
        from ssios.macos_vz import prepared_initramfs, prepared_kernel

        if not self.binary():
            return {"ok": False, "detail": "vfkit is not installed"}
        try:
            prepared_kernel(self.settings.state_dir)
            prepared_initramfs(self.settings.state_dir)
        except SsiosError as exc:
            return {"ok": False, "detail": exc.detail}
        return {"ok": True, "detail": "vfkit and the pinned aarch64 guest are present"}

    def can_launch(self) -> bool:
        return self.requirements()["ok"]

    def preflight(self) -> dict:
        return {
            "ok": False,
            "backend": self.name,
            "microvm": True,
            "isolated": False,
            "host_shell_fallback": False,
            "network": "deny",
            "binary": self.binary(),
            "detail": "preflight does not boot. Virtualization.framework is macOS only.",
        }

    def doctor(self) -> dict:
        present = self.binary()
        return {
            "backend": self.name,
            "booted": False,
            "isolated": False,
            "boot_ms": None,
            "network": "deny",
            "vfkit": present,
            "detail": "vfkit is on PATH" if present else "vfkit is not installed. brew install vfkit. This check does not boot a guest.",
        }

    def run(self, session, cancel, deadline):
        del session, cancel, deadline
        raise SsiosError(
            "microvm_fail_closed",
            "macos-vz only runs the codex agent. The summary worker stays on macos-vm or cloud-hypervisor",
            503,
        )


def build_runtime(settings):
    if settings.runtime_backend == "dev-guard":
        return DevGuardRuntime()
    if settings.runtime_backend == "macos-vm":
        return MacOSVMRuntime(settings)
    if settings.runtime_backend == "macos-vz":
        return MacOSVZRuntime(settings)
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

    def doctor(self) -> dict:
        return {
            "backend": self.name,
            "booted": False,
            "isolated": False,
            "microvm": False,
            "boot_ms": None,
            "detail": "dev-guard is not a microVM",
        }

    def can_launch(self) -> bool:
        return False


class CloudHypervisorRuntime:
    name = "cloud-hypervisor"

    def __init__(self, settings) -> None:
        self.settings = settings
        self.plane = None

    def can_launch(self) -> bool:
        if getattr(self.settings, "runtime_agent", "summary") == "codex":
            checks = (
                self._kvm(),
                self._binary(self.settings.cloud_hypervisor_bin),
                self._binary(self.settings.virtiofsd_bin),
                self._regular_file("kernel", self.settings.kernel_path),
            )
            return all(item["ok"] for item in checks)
        return self.worker_requirements()["ok"]

    def worker_requirements(self) -> dict:
        checks = (
            self._kvm(),
            self._binary(self.settings.cloud_hypervisor_bin),
            self._binary(self.settings.virtiofsd_bin),
            self._regular_file("kernel", self.settings.kernel_path),
            self._regular_file("initramfs", getattr(self.settings, "initramfs_path", "")),
        )
        missing = [item["detail"] for item in checks if not item["ok"]]
        return {"ok": not missing, "detail": "; ".join(missing) if missing else "worker guest is available"}

    def echo_requirements(self) -> dict:
        kvm = self._kvm()
        binary = self._binary(self.settings.cloud_hypervisor_bin)
        kernel = self._regular_file("kernel", self.settings.kernel_path)
        if not (kvm["ok"] and binary["ok"] and kernel["ok"]):
            return {"ok": False, "detail": "kvm, cloud-hypervisor, or kernel is missing", "binary": None, "initramfs": ""}
        initramfs = getattr(self.settings, "echo_initramfs_path", "") or ""
        if not initramfs:
            from ssios.linux_guest import build_echo_initramfs

            target = self.settings.state_dir / "storage" / "echo-initrd.img"
            try:
                build_echo_initramfs(target)
            except SsiosError as exc:
                return {"ok": False, "detail": exc.detail, "binary": binary.get("location"), "initramfs": ""}
            initramfs = str(target)
        return {"ok": True, "detail": "echo guest is available", "binary": binary.get("location"), "initramfs": initramfs}

    def run(self, session, cancel, deadline):
        from ssios.vmguest import run_cloud_hypervisor

        return run_cloud_hypervisor(self, session, cancel, deadline)

    def doctor(self) -> dict:
        from ssios.vmguest import doctor_cloud_hypervisor

        return doctor_cloud_hypervisor(self)

    def _located(self, name: str) -> str | None:
        found = self._binary(name)
        return found.get("location") if found.get("ok") else None

    def preflight(self) -> dict:
        checks = {
            "kvm": self._kvm(),
            "cloud_hypervisor": self._binary(self.settings.cloud_hypervisor_bin),
            "virtiofsd": self._binary(self.settings.virtiofsd_bin),
            "kernel": self._regular_file("kernel", self.settings.kernel_path),
            "root_image": self._regular_file("root image", self.settings.root_image_path),
            "guest_workload": {
                "ok": self.worker_requirements()["ok"],
                "detail": self.worker_requirements()["detail"],
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
