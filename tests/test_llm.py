"""Mock limits and the Ollama adapter. The stub server is not Ollama itself."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from support import ROOT, PlaneTest, request, wait_task

from ssios.config import load_settings
from ssios.httpapi import ControlServer
from ssios.llm import parse_model_payload
from ssios.service import ControlPlane


class LimitTest(PlaneTest):
    settings_overrides = {"task_timeout_seconds": 0.4}

    def test_mock_timeout_creates_no_approval(self) -> None:
        code, created, _headers = self.call(
            "POST",
            "/v1/tasks",
            token=self.human,
            body={
                "project_id": "demo-alpha",
                "kind": "slow",
                "input_revision": "exp-1",
                "input_path": "notes.md",
            },
        )
        self.assertEqual(code, 202, created)
        task = wait_task(self.base, self.human, created["id"], {"timeout", "failed", "cancelled"}, timeout=4)
        self.assertEqual(task["status"], "timeout", task)
        self.assertEqual(task["approval_ids"], [])


class CancelTest(PlaneTest):
    settings_overrides = {"task_timeout_seconds": 10}

    def test_human_cancel_stops_mock_task(self) -> None:
        code, created, _headers = self.call(
            "POST",
            "/v1/tasks",
            token=self.human,
            body={
                "project_id": "demo-alpha",
                "kind": "slow",
                "input_revision": "exp-1",
                "input_path": "notes.md",
            },
        )
        self.assertEqual(code, 202, created)
        time.sleep(0.15)
        code, body, _headers = self.call("POST", f"/v1/tasks/{created['id']}/cancel", token=self.worker)
        self.assertEqual(code, 403)
        code, body, _headers = self.call("POST", f"/v1/tasks/{created['id']}/cancel", token=self.human)
        self.assertEqual(code, 200, body)
        task = wait_task(self.base, self.human, created["id"], {"cancelled", "failed", "timeout"}, timeout=4)
        self.assertEqual(task["status"], "cancelled", task)
        self.assertEqual(task["approval_ids"], [])


class OutputLimitTest(PlaneTest):
    settings_overrides = {"max_output_bytes": 16}

    def test_output_limit_fails_closed(self) -> None:
        code, created, _headers = self.call(
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
        task = wait_task(self.base, self.human, created["id"], {"failed", "awaiting_approval"})
        self.assertEqual(task["status"], "failed", task)
        self.assertEqual(task["error_code"], "output_limit")
        self.assertEqual(task["approval_ids"], [])


class ParserTest(unittest.TestCase):
    def test_approve_field_is_only_a_flag(self) -> None:
        parsed = parse_model_payload('{"type":"final","text":"hello","approve":true}')
        self.assertEqual(parsed.kind, "final")
        self.assertEqual(parsed.text, "hello")
        self.assertTrue(parsed.raw_approve_flag)

    def test_invalid_model_json_is_rejected(self) -> None:
        with self.assertRaises(Exception) as caught:
            parse_model_payload("please run rm -rf /")
        self.assertEqual(caught.exception.code, "model_output_invalid")


class OllamaUnavailableTest(PlaneTest):
    settings_overrides = {
        "llm_provider": "ollama",
        "ollama_base_url": "http://127.0.0.1:9",
        "llm_timeout_seconds": 1,
    }

    def test_unreachable_ollama_fails_the_task(self) -> None:
        code, created, _headers = self.call(
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
        self.assertEqual(code, 202, created)
        task = wait_task(self.base, self.human, created["id"], {"failed", "awaiting_approval"}, timeout=4)
        self.assertEqual(task["status"], "failed", task)
        self.assertEqual(task["error_code"], "llm_unavailable")
        self.assertEqual(task["approval_ids"], [])


class _StubHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        self.server.payloads.append(json.loads(raw.decode()))
        step = len(self.server.payloads)
        if step == 1:
            content = json.dumps({"type": "tool", "name": "memory.read", "arguments": {"path": "topics/fixtures/overview.md"}})
        elif step == 2:
            content = json.dumps({"type": "tool", "name": "storage.read", "arguments": {"path": "notes.md"}})
        else:
            content = json.dumps({"type": "final", "text": "ollama-stub-summary", "approve": True})
        body = json.dumps({"message": {"role": "assistant", "content": content}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


class OllamaStubTest(unittest.TestCase):
    def test_adapter_talks_to_loopback_stub_and_does_not_self_approve(self) -> None:
        stub = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        stub.payloads = []
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(stub.shutdown)
        self.addCleanup(stub.server_close)
        with tempfile.TemporaryDirectory() as tmp:
            loaded = load_settings(ROOT / "config" / "ssios.example.toml", state_dir=Path(tmp) / "state", port=0)
            settings = replace(
                loaded,
                llm_provider="ollama",
                ollama_base_url=f"http://127.0.0.1:{stub.server_address[1]}",
                ollama_model="fixture-model",
                llm_timeout_seconds=5,
            )
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
            self.assertEqual(code, 202, created)
            task = wait_task(base, human, created["id"], {"awaiting_approval", "failed"})
            self.assertEqual(task["status"], "awaiting_approval", task)
            self.assertEqual(task["tool_trace"], ["memory.core", "memory.read", "storage.read"])
            self.assertTrue(stub.payloads)
            self.assertEqual(stub.payloads[0]["model"], "fixture-model")
            self.assertTrue(all(item["stream"] is False for item in stub.payloads))
            audit = (plane.settings.state_dir / "audit" / "audit.jsonl").read_text(encoding="utf-8")
            self.assertIn("model_approve_flag_ignored", audit)
            self.assertNotIn("ollama-stub-summary", audit)


if __name__ == "__main__":
    unittest.main()
