"""Expired approvals stay expired. Recovery mints a new binding and does not approve it."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from support import ROOT, request, wait_task

from ssios.cli import _existing_demo_task, run_demo
from ssios.config import load_settings
from ssios.errors import SsiosError
from ssios.httpapi import ControlServer
from ssios.service import ControlPlane


def _settings(tmp: str):
    return replace(
        load_settings(ROOT / "config" / "ssios.example.toml", state_dir=Path(tmp) / "state", port=0),
        llm_provider="mock",
    )


def _approve(base: str, token: str, approval: dict):
    return request(
        base,
        "POST",
        f"/v1/approvals/{approval['id']}/decide",
        token=token,
        body={
            "decision": "approve",
            "action": approval["action"],
            "payload_sha256": approval["payload_sha256"],
            "expected_revision": approval["expected_revision"],
            "bound_hash": approval["bound_hash"],
        },
    )


class ExpiryRecoveryTest(unittest.TestCase):
    def test_one_applied_one_expired_reissue_keeps_old_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp)
            plane = ControlPlane(settings)
            plane.open()
            server = ControlServer(plane)
            server.start()
            self.addCleanup(server.stop)
            base = f"http://127.0.0.1:{plane.bound_port}"
            human = plane.auth.read_secret("human")
            worker = plane.auth.read_secret("worker")
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
            storage = next(item for item in task["approvals"] if item["action"] == "storage.publish")
            memory = next(item for item in task["approvals"] if item["action"] == "memory.commit")
            code, result, _headers = _approve(base, human, storage)
            self.assertEqual(code, 200, result)
            published = plane.storage.published_head("demo-alpha")
            self.assertNotEqual(published, "none")
            before = json.loads((settings.state_dir / "approvals.json").read_text(encoding="utf-8"))
            old = before["approvals"][memory["id"]]
            audit_before = (settings.state_dir / "audit" / "audit.jsonl").read_bytes()
            plane.advance_clock(settings.approval_ttl_seconds + 30)
            denied, denied_body, _headers = request(base, "POST", f"/v1/tasks/{task['id']}/reissue", token=worker, body={})
            self.assertEqual(denied, 403)
            code, recovered, _headers = request(base, "POST", f"/v1/tasks/{task['id']}/reissue", token=human, body={})
            self.assertEqual(code, 200, recovered)
            self.assertEqual(recovered["status"], "awaiting_approval")
            self.assertEqual(recovered["issued"], [item["id"] for item in recovered["approvals"] if item["id"] not in task["approval_ids"]])
            self.assertEqual(len(recovered["issued"]), 1)
            self.assertEqual(plane.storage.published_head("demo-alpha"), published)
            after = json.loads((settings.state_dir / "approvals.json").read_text(encoding="utf-8"))
            kept = after["approvals"][memory["id"]]
            self.assertEqual(kept["status"], "expired")
            self.assertEqual(kept["nonce"], old["nonce"])
            self.assertEqual(kept["bound_hash"], old["bound_hash"])
            self.assertEqual(kept["expires_at"], old["expires_at"])
            self.assertEqual(kept["payload_sha256"], old["payload_sha256"])
            audit_after = (settings.state_dir / "audit" / "audit.jsonl").read_bytes()
            self.assertTrue(audit_after.startswith(audit_before))
            self.assertIn(b"approval.reissue", audit_after[len(audit_before) :])
            again, again_body, _headers = request(base, "POST", f"/v1/tasks/{task['id']}/reissue", token=human, body={})
            self.assertEqual(again, 200, again_body)
            self.assertEqual(again_body["issued"], [])
            expired, expired_body, _headers = _approve(base, human, memory)
            self.assertEqual(expired, 409)
            self.assertEqual(expired_body["error"], "approval_expired")
            new_id = recovered["issued"][0]
            new_approval = next(item for item in recovered["approvals"] if item["id"] == new_id)
            self.assertNotEqual(new_approval["bound_hash"], memory["bound_hash"])
            self.assertNotEqual(new_approval["id"], memory["id"])
            bad, bad_body, _headers = _approve(base, human, {**new_approval, "payload_sha256": "0" * 64})
            self.assertEqual(bad, 409)
            self.assertEqual(bad_body["error"], "approval_binding_mismatch")
            ok, applied, _headers = _approve(base, human, new_approval)
            self.assertEqual(ok, 200, applied)
            replay, replay_body, _headers = _approve(base, human, new_approval)
            self.assertEqual(replay, 409)
            self.assertEqual(replay_body["error"], "approval_replay_denied")
            server.stop()
            for thread in plane._threads.values():
                thread.join(timeout=3)
            restarted = ControlPlane(settings)
            restarted.open()
            server2 = ControlServer(restarted)
            server2.start()
            self.addCleanup(server2.stop)
            base2 = f"http://127.0.0.1:{restarted.bound_port}"
            human2 = restarted.auth.read_secret("human")
            still, still_body, _headers = _approve(base2, human2, memory)
            self.assertEqual(still, 409)
            self.assertEqual(still_body["error"], "approval_expired")
            stored = json.loads((settings.state_dir / "approvals.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["approvals"][memory["id"]]["nonce"], old["nonce"])

    def test_cas_conflict_on_replacement_does_not_revive_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp)
            plane = ControlPlane(settings)
            plane.open()
            server = ControlServer(plane)
            server.start()
            self.addCleanup(server.stop)
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
            task = wait_task(base, human, created["id"], {"awaiting_approval", "failed"})
            plane.advance_clock(settings.approval_ttl_seconds + 5)
            code, recovered, _headers = request(base, "POST", f"/v1/tasks/{task['id']}/reissue", token=human, body={})
            self.assertEqual(code, 200, recovered)
            self.assertEqual(recovered["status"], "awaiting_approval")
            core_code, core, _headers = request(base, "GET", "/v1/memory/core?project_id=demo-alpha", token=human)
            self.assertEqual(core_code, 200, core)
            extra, extra_body, _headers = request(
                base,
                "POST",
                "/v1/memory/propose",
                token=human,
                body={
                    "project_id": "demo-alpha",
                    "expected_revision": core["revision"],
                    "files": [
                        {
                            "path": "topics/fixtures/cas-note.md",
                            "content": '---\nname: cas-note\ndescription: "synthetic"\n---\n\nCAS-MARKER\n',
                        }
                    ],
                },
            )
            self.assertEqual(extra, 200, extra_body)
            moved, moved_body, _headers = _approve(base, human, extra_body["approval"])
            self.assertEqual(moved, 200, moved_body)
            memory = next(item for item in recovered["approvals"] if item["id"] in recovered["issued"] and item["action"] == "memory.commit")
            conflict, conflict_body, _headers = _approve(base, human, memory)
            self.assertEqual(conflict, 409)
            self.assertEqual(conflict_body["error"], "cas_conflict")
            for original in task["approvals"]:
                rejected, rejected_body, _headers = _approve(base, human, original)
                self.assertEqual(rejected, 409)
                self.assertEqual(rejected_body["error"], "approval_expired")

    def test_closed_tasks_are_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plane = ControlPlane(_settings(tmp))
            plane.open()
            human = plane.auth.read_secret("human")
            for status in ("rejected", "cancelled", "failed", "timeout", "committed"):
                task_id = "task-" + (status.encode().hex() + "0" * 16)[:16]
                plane.tasks[task_id] = {
                    "id": task_id,
                    "project_id": "demo-alpha",
                    "kind": "project-summary",
                    "status": status,
                    "approval_ids": [],
                    "tool_trace": [],
                    "error_code": status,
                    "published_revision": None,
                    "memory_revision": None,
                    "output_sha256": None,
                    "created_at": 0,
                    "input_revision": "exp-1",
                    "input_path": "notes.md",
                }
                with self.assertRaises(SsiosError) as caught:
                    plane.reissue_expired(human, task_id)
                self.assertEqual(caught.exception.code, "task_closed")
            plane.tasks = {
                task_id: task
                for task_id, task in plane.tasks.items()
                if task.get("status") != "committed"
            }
            self.assertIsNone(_existing_demo_task(plane))


class PersistedExpiryDemoTest(unittest.TestCase):
    def test_demo_replaces_persisted_expired_approvals_without_deleting_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp)
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
            task = wait_task(base, human, created["id"], {"awaiting_approval", "failed"})
            old_ids = list(task["approval_ids"])
            server.stop()
            for thread in plane._threads.values():
                thread.join(timeout=3)
            path = settings.state_dir / "approvals.json"
            stored = json.loads(path.read_text(encoding="utf-8"))
            nonces = {approval_id: stored["approvals"][approval_id]["nonce"] for approval_id in old_ids}
            for approval_id in old_ids:
                stored["approvals"][approval_id]["expires_at"] = 1
            path.write_text(json.dumps(stored), encoding="utf-8")
            self.assertEqual(run_demo(settings), 0)
            self.assertTrue((settings.state_dir / "memory" / "demo-alpha" / "repo" / ".git").exists())
            reloaded = json.loads(path.read_text(encoding="utf-8"))
            for approval_id, nonce in nonces.items():
                record = reloaded["approvals"][approval_id]
                self.assertEqual(record["status"], "expired")
                self.assertEqual(record["nonce"], nonce)
                self.assertEqual(record["expires_at"], 1)
            check = ControlPlane(settings)
            check.open()
            server = ControlServer(check)
            server.start()
            self.addCleanup(server.stop)
            base = f"http://127.0.0.1:{check.bound_port}"
            human = check.auth.read_secret("human")
            for approval_id in old_ids:
                code, body, _headers = request(
                    base,
                    "POST",
                    f"/v1/approvals/{approval_id}/decide",
                    token=human,
                    body={"decision": "approve", "action": "storage.publish", "payload_sha256": "0" * 64, "expected_revision": "none", "bound_hash": "0" * 64},
                )
                self.assertEqual(code, 409)
                self.assertEqual(body["error"], "approval_expired")
