"""Security regressions for auth, grants, approvals, paths, and snapshots."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from urllib.parse import quote

from support import ROOT, PlaneTest

from ssios.config import load_settings
from ssios.runtime import snapshot_refuse
from ssios.service import ControlPlane


class AuthAndGrantTest(PlaneTest):
    def test_unauthenticated_and_bad_token_are_rejected(self) -> None:
        status, body, _headers = self.call("GET", "/v1/health")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        status, body, _headers = self.call("GET", "/v1/status", token="not-a-real-token")
        self.assertEqual(status, 401)
        status, body, _headers = self.call("POST", "/v1/tasks", body={"project_id": "demo-alpha"})
        self.assertEqual(status, 401)

    def test_worker_cannot_approve_or_create_tasks(self) -> None:
        status, body, _headers = self.call(
            "POST",
            "/v1/tasks",
            token=self.worker,
            body={
                "project_id": "demo-alpha",
                "kind": "project-summary",
                "input_revision": "exp-1",
                "input_path": "notes.md",
            },
        )
        self.assertEqual(status, 403)
        status, body, _headers = self.call(
            "POST",
            "/v1/approvals/appr-does-not-exist/decide",
            token=self.worker,
            body={"decision": "approve"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "worker_cannot_approve")

    def test_cross_project_and_ungranted_input_are_denied(self) -> None:
        token = self.plane.issue_task_token("demo-alpha", paths=("notes.md",), revision="exp-1")
        status, body, _headers = self.call("GET", "/v1/memory/core?project_id=demo-beta", token=token)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "cross_project_denied")
        self.assertNotIn("BETA-ONLY-MEMORY-PHRASE", json.dumps(body))
        status, body, _headers = self.call(
            "GET",
            "/v1/storage/input?project_id=demo-beta&revision=exp-1&path=notes.md",
            token=token,
        )
        self.assertEqual(status, 403)
        self.assertNotIn("BETA-ONLY-EXPORT-PHRASE", json.dumps(body))
        status, body, _headers = self.call(
            "GET",
            "/v1/storage/input?project_id=demo-alpha&revision=exp-1&path=extra.md",
            token=token,
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "path_not_granted")
        self.assertNotIn("EXTRA-NOT-GRANTED", json.dumps(body))
        status, body, _headers = self.call(
            "GET",
            "/v1/storage/input?project_id=demo-alpha&revision=exp-1&path=notes.md",
            token=token,
        )
        self.assertEqual(status, 200)
        self.assertIn("NORTHWIND-FIXTURE-NOTE", body["content"])
        status, body, _headers = self.call("GET", "/v1/memory/core?project_id=demo-alpha", token=self.worker)
        self.assertEqual(status, 403)

    def test_human_can_read_ungranted_export_but_not_vault_paths(self) -> None:
        status, body, _headers = self.call(
            "GET",
            "/v1/storage/input?project_id=demo-alpha&revision=exp-1&path=extra.md",
            token=self.human,
        )
        self.assertEqual(status, 200)
        self.assertIn("EXTRA-NOT-GRANTED", body["content"])
        for path in ("../../vault/canary.txt", "/etc/passwd", "notes.md/../../vault/canary.txt"):
            status, body, _headers = self.call(
                "GET",
                "/v1/storage/input?project_id=demo-alpha&revision=exp-1&path=" + quote(path, safe=""),
                token=self.human,
            )
            self.assertIn(status, (400, 404), body)
            self.assertNotIn("SYNTHETIC-VAULT-CANARY", json.dumps(body))
        status, body, _headers = self.call(
            "GET",
            "/v1/memory/file?project_id=demo-alpha&path=" + quote("../../vault/canary.txt", safe=""),
            token=self.human,
        )
        self.assertEqual(status, 400)
        self.assertNotIn("SYNTHETIC-VAULT-CANARY", json.dumps(body))

    def test_cors_and_host_are_rejected(self) -> None:
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.plane.bound_port, timeout=5)
        connection.request(
            "GET",
            "/v1/health",
            headers={"Origin": "https://evil.example", "Authorization": f"Bearer {self.human}"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        self.assertNotIn("SYNTHETIC-VAULT-CANARY", response.read().decode())
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", self.plane.bound_port, timeout=5)
        connection.request("GET", "/v1/health", headers={"Host": "evil.example", "Authorization": f"Bearer {self.human}"})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.read().decode())["error"], "bad_host")
        connection.close()

    def test_token_permissions_and_refuse_world_readable_secret(self) -> None:
        secret = self.state / "secrets" / "human.token"
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode) & 0o077, 0)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode) & 0o077, 0)
        stored = (self.state / "auth" / "tokens.json").read_text(encoding="utf-8")
        self.assertNotIn(self.human, stored)
        os.chmod(secret, 0o644)
        other = ControlPlane(self.settings)
        with self.assertRaises(Exception) as caught:
            other.open()
        self.assertEqual(getattr(caught.exception, "code", ""), "insecure_permissions")

    def test_snapshot_never_copies_vault_or_clean_disk(self) -> None:
        destination = self.state / "not-created"
        with self.assertRaises(Exception) as caught:
            snapshot_refuse(self.state, [self.state / "vault" / "canary.txt"], destination)
        self.assertEqual(caught.exception.code, "snapshot_forbidden")
        self.assertFalse(destination.exists())
        task_dir = self.state / "storage" / "tasks" / "task-aaaaaaaaaaaaaaaa"
        task_dir.mkdir(parents=True)
        with self.assertRaises(Exception) as caught:
            snapshot_refuse(self.state, [task_dir], destination)
        self.assertEqual(caught.exception.code, "snapshot_not_implemented")
        self.assertFalse(destination.exists())
        with self.assertRaises(Exception) as caught:
            snapshot_refuse(self.state, [PathHome()], destination)
        self.assertEqual(caught.exception.code, "snapshot_forbidden")


def PathHome():
    from pathlib import Path

    return Path.home() / "ssios-should-not-snapshot"


class ApprovalEdgeTest(PlaneTest):
    def test_tamper_does_not_consume_and_expiry_blocks(self) -> None:
        code, core, _headers = self.call("GET", "/v1/memory/core?project_id=demo-alpha", token=self.human)
        self.assertEqual(code, 200)
        approval = self._propose(core["revision"], "topics/fixtures/tamper.md", "TAMPER-MARKER")
        bad, body, _headers = self.call(
            "POST",
            f"/v1/approvals/{approval['id']}/decide",
            token=self.human,
            body={
                "decision": "approve",
                "action": approval["action"],
                "payload_sha256": "0" * 64,
                "expected_revision": approval["expected_revision"],
                "bound_hash": approval["bound_hash"],
            },
        )
        self.assertEqual(bad, 409)
        self.assertEqual(body["error"], "approval_binding_mismatch")
        ok, result, _headers = self.call(
            "POST",
            f"/v1/approvals/{approval['id']}/decide",
            token=self.human,
            body=self._approve_body(approval),
        )
        self.assertEqual(ok, 200, result)
        late = self._propose(result["applied_revision"], "topics/fixtures/late.md", "LATE-MARKER")
        self.plane.advance_clock(self.settings.approval_ttl_seconds + 1)
        expired, body, _headers = self.call(
            "POST",
            f"/v1/approvals/{late['id']}/decide",
            token=self.human,
            body=self._approve_body(late),
        )
        self.assertEqual(expired, 409)
        self.assertEqual(body["error"], "approval_expired")
        read_code, read_body, _headers = self.call(
            "GET",
            "/v1/memory/file?project_id=demo-alpha&path=topics/fixtures/late.md",
            token=self.human,
        )
        self.assertEqual(read_code, 404)
        self.assertNotIn("LATE-MARKER", json.dumps(read_body))

    def _propose(self, revision: str, path: str, marker: str) -> dict:
        content = f'---\nname: note\ndescription: "synthetic"\n---\n\n{marker}\n'
        code, body, _headers = self.call(
            "POST",
            "/v1/memory/propose",
            token=self.human,
            body={
                "project_id": "demo-alpha",
                "expected_revision": revision,
                "files": [{"path": path, "content": content}],
            },
        )
        self.assertEqual(code, 200, body)
        return body["approval"]

    @staticmethod
    def _approve_body(approval: dict) -> dict:
        return {
            "decision": "approve",
            "action": approval["action"],
            "payload_sha256": approval["payload_sha256"],
            "expected_revision": approval["expected_revision"],
            "bound_hash": approval["bound_hash"],
        }


class ProductionStartupTest(PlaneTest):
    def test_production_cli_fails_closed_without_starting_work(self) -> None:
        import subprocess
        import sys

        state = self.state.parent / "production-state"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "ssios",
                "--state",
                str(state),
                "--config",
                str(ROOT / "config" / "ssios.example.toml"),
                "up",
                "--profile",
                "production",
                "--port",
                "0",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn('"host_shell_fallback": false', completed.stdout)
        self.assertIn('"execution_implemented": false', completed.stdout)
        self.assertFalse((state / "secrets" / "human.token").exists())

    def test_cloud_backend_does_not_run_host_task(self) -> None:
        loaded = load_settings(ROOT / "config" / "ssios.example.toml", state_dir=self.state, port=0)
        settings = replace(loaded, runtime_backend="cloud-hypervisor", profile="dev")
        plane = ControlPlane(settings)
        plane.auth = self.plane.auth
        plane.tasks = self.plane.tasks
        with self.assertRaises(Exception) as caught:
            plane.create_task(
                self.human,
                {
                    "project_id": "demo-alpha",
                    "kind": "project-summary",
                    "input_revision": "exp-1",
                    "input_path": "notes.md",
                },
            )
        self.assertEqual(caught.exception.code, "microvm_fail_closed")
        self.assertEqual(plane.tasks, {})
