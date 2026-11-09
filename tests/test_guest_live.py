"""Real Cloud Hypervisor boot. Skipped unless SSIOS_VM_LIVE=1 and the binaries exist."""

from __future__ import annotations

import json
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

    def test_codex_exec_reaches_the_mock_proxy_without_a_nic(self) -> None:
        from ssios.codex_guest import prepare_codex_binary
        import shutil

        tmp = "/tmp/ssios-codex-live"
        if Path(tmp).exists():
            shutil.rmtree(tmp)
        Path(tmp).mkdir()
        try:
            prepare = prepare_codex_binary(Path(tmp), timeout=180)
            self.assertTrue(prepare["ok"], prepare)
            loaded = load_settings(Path("config/ssios.example.toml"), state_dir=Path(tmp), port=0)
            settings = replace(
                loaded,
                runtime_backend="cloud-hypervisor",
                runtime_agent="codex",
                cloud_hypervisor_bin=str(HYPERVISOR),
                virtiofsd_bin=str(VIRTIOFSD),
                kernel_path=str(KERNEL),
                memory_mb=2048,
                task_timeout_seconds=180,
            )
            # The binary was prepared in this directory before settings resolved it.
            # prepare_codex_binary wrote into Path(tmp), and state_dir is that same path.
            plane = ControlPlane(settings)
            plane.open()
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
            plane._threads[created["id"]].join(timeout=240)
            task = plane.tasks[created["id"]]
            self.assertEqual(task["status"], "awaiting_approval", task)
            self.assertTrue(task["isolated"], task)
            self.assertGreater(task["boot_ms"], 0)
            self.assertEqual(task["runtime_backend"], "cloud-hypervisor")
            self.assertEqual(task["tool_trace"], ["guest.codex", "host.mock-responses"])
            guest = Path(tmp) / "storage" / "tasks" / created["id"] / "guest"
            argv = (guest / "hypervisor.argv").read_text(encoding="utf-8")
            self.assertNotIn("--net", argv)
            self.assertIn("--vsock", argv)
            net = json.loads((guest / "scratch" / "netcheck.json").read_text(encoding="utf-8"))
            self.assertEqual(net["interfaces"], ["lo"])
            self.assertFalse(net["outbound"])
            self.assertIn('"decision": "ok"', (guest / "proxy.log").read_text(encoding="utf-8"))
            blob = b"".join(path.read_bytes() for path in guest.rglob("*") if path.is_file() and path.name != "codex")
            self.assertNotIn(human.encode(), blob)
            self.assertTrue((guest / "patch.diff").is_file())
            print(f"LIVE codex_ms={task['boot_ms']} net={net}")
        except Exception:
            print("LIVE codex state kept at", tmp)
            raise
