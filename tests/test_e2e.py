"""End-to-end demo against the localhost API. The model provider is the labeled mock."""

from __future__ import annotations

import json
import stat
import unittest
from dataclasses import replace
from pathlib import Path

from support import ROOT, PlaneTest, request, wait_task

from ssios.cli import run_demo
from ssios.config import load_settings
from ssios.errors import SsiosError
from ssios.httpapi import ControlServer
from ssios.service import ControlPlane


class DemoApiTest(PlaneTest):
    def test_mock_vertical_slice_approval_and_restart(self) -> None:
        status, health, _headers = self.call("GET", "/v1/health", token=self.human)
        self.assertEqual(status, 200)
        self.assertEqual(health["llm_provider"], "mock")
        self.assertEqual(health["memory_mode"], "letta-memfs-compat")
        self.assertEqual(health["runtime_backend"], "dev-guard")

        status, created, _headers = self.call(
            "POST",
            "/v1/tasks",
            token=self.human,
            body={
                "project_id": "demo-alpha",
                "kind": "project-summary",
                "input_revision": "exp-1",
                "input_path": "notes.md",
            },
        )
        self.assertEqual(status, 202, created)
        task = wait_task(self.base, self.human, created["id"], {"awaiting_approval", "failed", "timeout"})
        self.assertEqual(task["status"], "awaiting_approval", task)
        self.assertEqual(task["tool_trace"], ["memory.core", "memory.read", "storage.read"])
        self.assertTrue(task["output_sha256"])
        self.assertNotIn("NORTHWIND-FIXTURE-NOTE", json.dumps(task))

        code, worker_decision, _headers = self.call(
            "POST",
            f"/v1/approvals/{task['approval_ids'][0]}/decide",
            token=self.worker,
            body={"decision": "approve"},
        )
        self.assertEqual(code, 403)
        self.assertEqual(worker_decision["error"], "worker_cannot_approve")

        for approval_id in task["approval_ids"]:
            code, approval, _headers = self.call("GET", f"/v1/approvals/{approval_id}", token=self.human)
            self.assertEqual(code, 200, approval)
            self.assertIn("NORTHWIND-FIXTURE-NOTE", approval["preview"])
            code, result, _headers = self.call(
                "POST",
                f"/v1/approvals/{approval_id}/decide",
                token=self.human,
                body={
                    "decision": "approve",
                    "action": approval["action"],
                    "payload_sha256": approval["payload_sha256"],
                    "expected_revision": approval["expected_revision"],
                    "bound_hash": approval["bound_hash"],
                },
            )
            self.assertEqual(code, 200, result)
            replay, replay_body, _headers = self.call(
                "POST",
                f"/v1/approvals/{approval_id}/decide",
                token=self.human,
                body={
                    "decision": "approve",
                    "action": approval["action"],
                    "payload_sha256": approval["payload_sha256"],
                    "expected_revision": approval["expected_revision"],
                    "bound_hash": approval["bound_hash"],
                },
            )
            self.assertEqual(replay, 409)
            self.assertEqual(replay_body["error"], "approval_replay_denied")

        finished = wait_task(self.base, self.human, created["id"], {"committed", "failed"})
        self.assertEqual(finished["status"], "committed", finished)

        self.server.stop()
        for thread in list(self.plane._threads.values()):
            thread.join(timeout=3)
        restarted = ControlPlane(self.settings)
        restarted.open()
        from ssios.httpapi import ControlServer

        server = ControlServer(restarted)
        self.addCleanup(server.stop)
        server.start()
        base = f"http://127.0.0.1:{restarted.bound_port}"
        human = restarted.auth.read_secret("human")
        code, core, _headers = request(base, "GET", "/v1/memory/core?project_id=demo-alpha", token=human)
        code2, topic, _headers = request(
            base,
            "GET",
            "/v1/memory/file?project_id=demo-alpha&path=topics/fixtures/last-summary.md",
            token=human,
        )
        code3, published, _headers = request(
            base,
            "GET",
            "/v1/storage/published?project_id=demo-alpha&revision=HEAD",
            token=human,
        )
        self.assertEqual(code, 200)
        self.assertEqual(code2, 200)
        self.assertEqual(code3, 200)
        core_blob = json.dumps(core)
        self.assertIn("CORE-PROJECT-FACT", core_blob)
        self.assertNotIn("THREE-BULLET-RULE", core_blob)
        self.assertNotIn("NORTHWIND-FIXTURE-NOTE", core_blob)
        self.assertEqual(topic["trust"], "approved")
        self.assertIn("NORTHWIND-FIXTURE-NOTE", topic["content"])
        self.assertIn("THREE-BULLET-RULE", published["content"])
        audit = (self.state / "audit" / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("model_approve_flag_ignored", audit)
        self.assertNotIn(self.human, audit)
        self.assertNotIn("NORTHWIND-FIXTURE-NOTE", audit)
        tasks = (self.state / "tasks.json").read_text(encoding="utf-8")
        self.assertNotIn(self.human, tasks)
        self.assertNotIn("NORTHWIND-FIXTURE-NOTE", tasks)
        mode = stat.S_IMODE((self.state / "secrets" / "human.token").stat().st_mode)
        self.assertEqual(mode & 0o077, 0)


class DemoCommandTest(unittest.TestCase):
    def _settings(self, tmp: str):
        return replace(
            load_settings(ROOT / "config" / "ssios.example.toml", state_dir=Path(tmp) / "state", port=0),
            llm_provider="mock",
        )

    def test_run_demo_command(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_demo(self._settings(tmp)), 0)

    def test_demo_rerun_keeps_existing_state(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            self.assertEqual(run_demo(settings), 0)
            memory = settings.state_dir / "memory" / "demo-alpha" / "repo"
            before = memory.stat().st_mtime
            self.assertEqual(run_demo(settings), 0)
            self.assertTrue(memory.exists())
            self.assertGreaterEqual(memory.stat().st_mtime, before)

    def test_demo_resumes_after_partial_approval(self) -> None:
        import tempfile

        from ssios.client import request
        from ssios.httpapi import ControlServer
        from ssios.service import ControlPlane
        from support import wait_task

        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            plane = ControlPlane(settings)
            plane.open()
            server = ControlServer(plane)
            server.start()
            base = f"http://127.0.0.1:{plane.bound_port}"
            human = plane.auth.read_secret("human")
            code, created, _headers = request(
                base,
                "POST",
                "/v1/tasks",
                token=human,
                body={
                    "project_id": "demo-alpha",
                    "kind": "project-summary",
                    "input_revision": "exp-1",
                    "input_path": "notes.md",
                },
            )
            self.assertEqual(code, 202, created)
            task = wait_task(base, human, created["id"], {"awaiting_approval", "failed"})
            self.assertEqual(task["status"], "awaiting_approval", task)
            storage = next(item for item in task["approvals"] if item["action"] == "storage.publish")
            code, result, _headers = request(
                base,
                "POST",
                f"/v1/approvals/{storage['id']}/decide",
                token=human,
                body={
                    "decision": "approve",
                    "action": storage["action"],
                    "payload_sha256": storage["payload_sha256"],
                    "expected_revision": storage["expected_revision"],
                    "bound_hash": storage["bound_hash"],
                },
            )
            self.assertEqual(code, 200, result)
            server.stop()
            for thread in plane._threads.values():
                thread.join(timeout=3)
            self.assertEqual(run_demo(settings), 0)


class PortConflictTest(unittest.TestCase):
    def test_busy_port_names_the_failure(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(
                load_settings(ROOT / "config" / "ssios.example.toml", state_dir=Path(tmp) / "state", port=0),
                llm_provider="mock",
            )
            plane = ControlPlane(settings)
            plane.open()
            server = ControlServer(plane)
            server.start()
            self.addCleanup(server.stop)
            second = ControlPlane(replace(settings, port=plane.bound_port))
            second.open()
            with self.assertRaises(SsiosError) as caught:
                ControlServer(second)
            self.assertEqual(caught.exception.code, "port_unavailable")
            self.assertIn(str(plane.bound_port), caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
