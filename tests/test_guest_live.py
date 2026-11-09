"""Real Cloud Hypervisor boot. Skipped unless SSIOS_VM_LIVE=1 and the binaries exist."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ssios.config import load_settings
from ssios.linux_guest import build_worker_initramfs
from ssios.runtime import build_runtime
from ssios.service import ControlPlane

KERNEL = Path("/tmp/ssios-vm/bzImage")
HYPERVISOR = Path("/tmp/ssios-vm/cloud-hypervisor")
VIRTIOFSD = Path("/usr/libexec/virtiofsd")
INITRAMFS = Path("/tmp/ssios-vm/worker.img")


def _ready() -> bool:
    return (
        os.environ.get("SSIOS_VM_LIVE") == "1"
        and KERNEL.is_file()
        and HYPERVISOR.is_file()
        and VIRTIOFSD.is_file()
        and os.access("/dev/kvm", os.R_OK | os.W_OK)
    )


@unittest.skipUnless(_ready(), "real cloud hypervisor boot not requested")
class LiveCloudHypervisorTest(unittest.TestCase):
    def test_echo_and_summary_run_inside_the_guest(self) -> None:
        if not INITRAMFS.is_file():
            build_worker_initramfs(INITRAMFS)
        with tempfile.TemporaryDirectory() as tmp:
            loaded = load_settings(Path("config/ssios.example.toml"), state_dir=Path(tmp), port=0)
            settings = replace(
                loaded,
                runtime_backend="cloud-hypervisor",
                cloud_hypervisor_bin=str(HYPERVISOR),
                virtiofsd_bin=str(VIRTIOFSD),
                kernel_path=str(KERNEL),
                initramfs_path=str(INITRAMFS),
                task_timeout_seconds=60,
            )
            plane = ControlPlane(settings)
            plane.open()
            doctor = build_runtime(settings).doctor()
            self.assertTrue(doctor["booted"], doctor)
            self.assertTrue(doctor["isolated"], doctor)
            self.assertGreater(doctor["boot_ms"], 0)
            human = plane.auth.read_secret("human")
            created = plane.create_task(
                human,
                {
                    "project_id": "demo-alpha",
                    "kind": "project-summary",
                    "input_revision": "exp-1",
                    "input_path": "notes.md",
                },
            )
            plane._threads[created["id"]].join(timeout=60)
            task = plane.tasks[created["id"]]
            self.assertEqual(task["status"], "awaiting_approval", task)
            self.assertTrue(task["isolated"], task)
            self.assertGreater(task["boot_ms"], 0)
            self.assertEqual(task["runtime_backend"], "cloud-hypervisor")
            guest = Path(tmp) / "storage" / "tasks" / created["id"] / "guest"
            blob = b"".join(path.read_bytes() for path in guest.rglob("*") if path.is_file() and path.suffix != ".log")
            self.assertNotIn(human.encode(), blob)
            print(f"LIVE echo_ms={doctor['boot_ms']} task_ms={task['boot_ms']}")
