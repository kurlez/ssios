"""Guest worker protocol and the boundary that refuses to call a host run isolated."""

from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path

from dataclasses import replace

from support import ROOT, PlaneTest, wait_task

from ssios.runtime import cloud_hypervisor_guest_command, krunvm_create_command, krunvm_echo_command
from ssios.vmguest import GuestOutcome, GuestSession, run_worker_subprocess, serve_model


class ProtocolTest(unittest.TestCase):
    def test_worker_round_trip_has_no_token(self) -> None:
        root = Path(self.id().replace(".", "-"))
        # Keep the fixture under /tmp so the test does not write into the repo.
        root = Path("/tmp") / root.name
        if root.exists():
            import shutil

            shutil.rmtree(root)
        (root / "input").mkdir(parents=True)
        (root / "scratch").mkdir()
        token = "human-token-must-not-appear"
        grant = {"system": "Write the summary as ordinary prose.", "user": "Summarize only these notes.\n\nHARBOR-LOCK and Northwind Lantern."}
        (root / "input" / "grant.json").write_text(json.dumps(grant), encoding="utf-8")
        (root / "scratch" / "worker.py").write_text(Path(ROOT / "src" / "ssios" / "guest_worker.py").read_text(encoding="utf-8"), encoding="utf-8")
        session = GuestSession(root=root, plain=False, grant=grant, task_id="task-0123456789abcdef", kind="project-summary", project_id="demo-alpha")

        from ssios.llm import ModelResponse

        class Plane:
            class llm:
                @staticmethod
                def complete(messages, *, kind, cancel_event, deadline):
                    assert token not in json.dumps(messages)
                    return ModelResponse(kind="final", text="HARBOR-LOCK stays local. Northwind Lantern kept its notes on the laptop.")

            @staticmethod
            def now():
                return "t"

            class audit:
                @staticmethod
                def write(**_kwargs):
                    return None

        thread = threading.Thread(target=serve_model, args=(Plane(), session, threading.Event(), __import__("time").monotonic() + 5), daemon=True)
        thread.start()
        code = run_worker_subprocess(session)
        thread.join(timeout=5)
        self.assertEqual(code, 0)
        output = json.loads((root / "scratch" / "output.json").read_text(encoding="utf-8"))
        self.assertIn("HARBOR-LOCK", output["text"])
        blob = b"".join(path.read_bytes() for path in root.rglob("*") if path.is_file())
        self.assertNotIn(token.encode(), blob)


class CommandTest(unittest.TestCase):
    def test_krunvm_and_cloud_hypervisor_commands_have_no_network(self) -> None:
        created = krunvm_create_command(
            "krunvm",
            name="ssiosabc",
            image="docker.io/library/python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7",
            input_dir=Path("/tmp/ssios-input"),
            scratch_dir=Path("/tmp/ssios-scratch"),
            cpus=1,
            memory_mb=256,
        )
        self.assertNotIn("--port", created)
        self.assertNotIn("--dns", created)
        self.assertNotIn("--net", created)
        self.assertNotIn("--readonly", created)
        echo = krunvm_echo_command("krunvm", "ssiosabc")
        self.assertEqual(echo[-1], "ok")
        guest = cloud_hypervisor_guest_command(
            binary="cloud-hypervisor",
            kernel="/tmp/bzImage",
            initramfs="/tmp/initrd",
            api_socket=Path("/tmp/api.sock"),
            cpus=1,
            memory_mb=256,
            filesystems=[("input", Path("/tmp/input.sock")), ("scratch", Path("/tmp/scratch.sock"))],
        )
        self.assertNotIn("--net", guest)
        self.assertIn("shared=on", " ".join(guest))
        self.assertIn("tag=input,socket=/tmp/input.sock", guest)


class ScriptedGuest:
    name = "macos-vm"

    def __init__(self, plane, isolated: bool) -> None:
        self.plane = plane
        self.isolated = isolated

    def can_launch(self) -> bool:
        return True

    def run(self, session, cancel, deadline) -> GuestOutcome:
        thread = threading.Thread(target=serve_model, args=(self.plane, session, cancel, deadline), daemon=True)
        thread.start()
        code = run_worker_subprocess(session)
        thread.join(timeout=5)
        payload = json.loads((session.root / "scratch" / "output.json").read_text(encoding="utf-8"))
        ran = code == 0 and bool(payload.get("text"))
        return GuestOutcome(payload.get("text", ""), bool(self.isolated and ran), self.name, 15, "scripted launcher")


class ScriptedPlaneTest(PlaneTest):
    def test_launcher_flag_is_what_the_task_records(self) -> None:
        self.plane.settings = replace(self.plane.settings, runtime_backend="macos-vm")
        self.plane.guest_runtime = ScriptedGuest(self.plane, isolated=True)
        code, created, _headers = self.call(
            "POST",
            "/v1/tasks",
            token=self.human,
            body={"project_id": "demo-alpha", "kind": "project-summary", "input_revision": "exp-1", "input_path": "notes.md"},
        )
        self.assertEqual(code, 202, created)
        task = wait_task(self.base, self.human, created["id"], {"awaiting_approval", "failed"})
        self.assertEqual(task["status"], "awaiting_approval", task)
        self.assertTrue(task["isolated"])
        self.assertEqual(task["boot_ms"], 15)
        self.assertEqual(task["runtime_backend"], "macos-vm")
        self.assertEqual(task["tool_trace"], ["guest.prompt", "host.model"])
        guest = self.state / "storage" / "tasks" / created["id"] / "guest"
        blob = b"".join(path.read_bytes() for path in guest.rglob("*") if path.is_file())
        self.assertNotIn(self.human.encode(), blob)
        from test_ui import _form

        _status, page, cookie = _form(self.base, "/ui/login", {"token": self.human})
        session = cookie.split(";", 1)[0]
        import urllib.request

        request = urllib.request.Request(self.base + f"/ui/tasks/{created['id']}", headers={"Cookie": session})
        with urllib.request.urlopen(request, timeout=10) as response:
            html = response.read().decode()
        self.assertIn("隔离：是", html)
        self.assertIn("15 毫秒", html)
        self.assertIn("macos-vm", html)

    def test_unisolated_launcher_does_not_approve_or_run_the_host_agent(self) -> None:
        self.plane.settings = replace(self.plane.settings, runtime_backend="macos-vm")
        self.plane.guest_runtime = ScriptedGuest(self.plane, isolated=False)

        def explode(*_args, **_kwargs):
            raise AssertionError("host agent ran")

        self.plane.runner.run = explode
        code, created, _headers = self.call(
            "POST",
            "/v1/tasks",
            token=self.human,
            body={"project_id": "demo-alpha", "kind": "project-summary", "input_revision": "exp-1", "input_path": "notes.md"},
        )
        self.assertEqual(code, 202, created)
        task = wait_task(self.base, self.human, created["id"], {"failed"})
        self.assertEqual(task["error_code"], "microvm_fail_closed")
        self.assertFalse(task["isolated"])
        self.assertEqual(task["approval_ids"], [])
