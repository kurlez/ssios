"""Runtime preflight and command builder. These are not microVM execution tests."""

from __future__ import annotations

import os
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from support import ROOT, PlaneTest, wait_task

from ssios.config import load_settings
from ssios.runtime import (
    CloudHypervisorRuntime,
    assert_share_allowed,
    cloud_hypervisor_command,
    plan_shares,
    virtiofsd_command,
)
from ssios.service import ControlPlane


class PreflightTest(PlaneTest):
    def test_missing_kvm_and_binaries_fail_closed_without_processes(self) -> None:
        loaded = load_settings(ROOT / "config" / "ssios.example.toml", state_dir=self.state, port=0)
        settings = replace(loaded, kvm_path=str(self.state / "missing-kvm"))
        runtime = CloudHypervisorRuntime(settings)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("Popen")):
            with mock.patch("subprocess.run", side_effect=AssertionError("run")):
                report = runtime.preflight()
                with self.assertRaises(Exception) as caught:
                    runtime.launch(host_workload=lambda: (_ for _ in ()).throw(AssertionError("host")))
        self.assertFalse(report["ok"])
        self.assertFalse(report["execution_implemented"])
        self.assertFalse(report["host_shell_fallback"])
        self.assertFalse(report["checks"]["kvm"]["ok"])
        self.assertIn("absent", report["checks"]["kvm"]["detail"])
        self.assertEqual(caught.exception.code, "microvm_fail_closed")

    def test_real_device_report_stays_fail_closed(self) -> None:
        code, report, _headers = self.call("GET", "/v1/runtime/preflight", token=self.human)
        self.assertEqual(code, 200)
        cloud = report["cloud_hypervisor"]
        self.assertFalse(cloud["ok"])
        self.assertFalse(cloud["host_shell_fallback"])
        self.assertEqual(cloud["network"], "deny")
        self.assertFalse(cloud["checks"]["guest_workload"]["ok"])
        self.assertFalse(report["dev_guard"]["microvm"])
        kvm = Path("/dev/kvm")
        usable = kvm.exists() and os.access(kvm, os.R_OK | os.W_OK)
        self.assertEqual(cloud["checks"]["kvm"]["ok"], usable)


class CommandBuilderTest(PlaneTest):
    def test_shares_are_least_privilege_and_network_is_absent(self) -> None:
        shares, disk = plan_shares(self.state, "task-0123456789abcdef", "demo-alpha", "exp-1")
        self.assertEqual([share.tag for share in shares], ["input", "staging"])
        self.assertTrue(shares[0].readonly)
        self.assertFalse(shares[1].readonly)
        self.assertNotEqual(plan_shares(self.state, "task-fedcba9876543210", "demo-alpha", "exp-1")[1], disk)
        with self.assertRaises(Exception) as caught:
            assert_share_allowed(self.state, self.state / "memory" / "demo-alpha" / "repo")
        self.assertEqual(caught.exception.code, "share_forbidden")
        with self.assertRaises(Exception):
            assert_share_allowed(self.state, self.state / "vault")
        with self.assertRaises(Exception):
            assert_share_allowed(self.state, Path.home())
        sockets = [self.state / "runtime" / "input.sock", self.state / "runtime" / "staging.sock"]
        input_cmd = virtiofsd_command("virtiofsd", sockets[0], shares[0])
        stage_cmd = virtiofsd_command("virtiofsd", sockets[1], shares[1])
        self.assertIn("--readonly", input_cmd)
        self.assertNotIn("--readonly", stage_cmd)
        self.assertIn("--sandbox", input_cmd)
        self.assertIn("namespace", input_cmd)
        argv = cloud_hypervisor_command(
            binary="cloud-hypervisor",
            api_socket=self.state / "runtime" / "api.sock",
            cpus=1,
            memory_mb=512,
            kernel="/var/lib/ssios/vmlinux",
            disk=disk,
            filesystems=list(zip(shares, sockets, strict=True)),
            network=False,
        )
        self.assertNotIn("--net", argv)
        joined = " ".join(argv)
        self.assertNotIn(str(self.state / "memory"), joined)
        self.assertNotIn(str(self.state / "vault"), joined)
        self.assertNotIn(str(Path.home()), joined)
        with self.assertRaises(Exception) as caught:
            cloud_hypervisor_command(
                binary="cloud-hypervisor",
                api_socket=self.state / "api.sock",
                cpus=1,
                memory_mb=512,
                kernel="/var/lib/ssios/vmlinux",
                disk=disk,
                filesystems=[],
                network=True,
            )
        self.assertEqual(caught.exception.code, "network_denied")

    def test_shell_tool_does_not_spawn_a_host_shell(self) -> None:
        commands = []
        real_popen = subprocess.Popen

        def wrapped_popen(args, *pos, **kwargs):
            commands.append(list(args) if isinstance(args, (list, tuple)) else [args])
            if kwargs.get("shell"):
                raise AssertionError("shell=True")
            return real_popen(args, *pos, **kwargs)

        with mock.patch("subprocess.Popen", side_effect=wrapped_popen):
            code, created, _headers = self.call(
                "POST",
                "/v1/tasks",
                token=self.human,
                body={
                    "project_id": "demo-alpha",
                    "kind": "shell-attempt",
                    "input_revision": "exp-1",
                    "input_path": "notes.md",
                },
            )
            self.assertEqual(code, 202, created)
            task = wait_task(self.base, self.human, created["id"], {"failed", "awaiting_approval", "committed"})
        self.assertEqual(task["status"], "failed", task)
        self.assertEqual(task["error_code"], "tool_denied")
        self.assertEqual(task["approval_ids"], [])
        self.assertTrue(commands)
        for command in commands:
            executable = Path(str(command[0])).name
            self.assertNotIn(executable, {"sh", "bash", "dash", "id", "sudo"})
            self.assertNotIn("id", command[1:])


class MacOSRuntimeTest(unittest.TestCase):
    def test_macos_vm_refuses_to_run_on_the_host(self) -> None:
        import tempfile

        from ssios.errors import SsiosError

        with tempfile.TemporaryDirectory() as tmp:
            loaded = load_settings(ROOT / "config" / "ssios.example.toml", state_dir=Path(tmp) / "state", port=0)
            # A Mac that has krunvm installed would boot a guest here. Name a binary that is not on PATH.
            plane = ControlPlane(replace(loaded, runtime_backend="macos-vm", krunvm_bin="ssios-test-no-krunvm"))
            plane.open()
            status = plane.status(plane.auth.read_secret("human"))
            self.assertEqual(status["runtime"]["active_backend"], "macos-vm")
            self.assertFalse(status["runtime"]["isolated"])
            with self.assertRaises(SsiosError) as caught:
                plane.create_task(
                    plane.auth.read_secret("human"),
                    {
                        "project_id": "demo-alpha",
                        "kind": "project-summary",
                        "input_revision": "exp-1",
                        "input_path": "notes.md",
                    },
                )
            self.assertEqual(caught.exception.code, "microvm_fail_closed")
            self.assertEqual(plane.tasks, {})


class SourceGuardTest(unittest.TestCase):
    def test_python_sources_do_not_shell_out(self) -> None:
        banned = ("shell=True", "os.system(", "subprocess.getoutput", "subprocess.getstatusoutput")
        for path in (ROOT / "src").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in banned:
                self.assertNotIn(token, text, f"{path} contains {token}")
