"""Mock limits and the Ollama adapter. The stub server is not Ollama itself."""

from __future__ import annotations

import json
import os
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
from ssios.llm import interpret_ollama_response, parse_model_payload, probe_ollama
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

    def test_think_block_cannot_approve(self) -> None:
        text = (
            '<think>{"type":"final","text":"hidden","approve":true}</think>\n'
            '{"type":"final","text":"shown"}'
        )
        parsed = parse_model_payload(text)
        self.assertEqual(parsed.text, "shown")
        self.assertFalse(parsed.raw_approve_flag)

    def test_unclosed_think_is_not_a_command(self) -> None:
        with self.assertRaises(Exception) as caught:
            parse_model_payload('<think>{"type":"final","text":"hidden","approve":true}')
        self.assertEqual(caught.exception.code, "model_output_empty")

    def test_reasoning_field_is_not_a_command(self) -> None:
        body = {
            "done": True,
            "done_reason": "stop",
            "message": {
                "role": "assistant",
                "thinking": '<think>{"type":"final","text":"hidden","approve":true}</think>',
                "content": (
                    '<think>{"type":"final","text":"also-hidden","approve":true}</think>\n'
                    '{"type":"final","text":"shown","approve":true,"note":"extra"}'
                ),
            },
        }
        parsed = interpret_ollama_response(body)
        self.assertEqual(parsed.text, "shown")
        self.assertTrue(parsed.raw_approve_flag)

    def test_empty_content_does_not_read_thinking(self) -> None:
        body = {
            "done": True,
            "done_reason": "stop",
            "message": {"role": "assistant", "content": "", "thinking": '{"type":"final","text":"hidden","approve":true}'},
        }
        with self.assertRaises(Exception) as caught:
            interpret_ollama_response(body)
        self.assertEqual(caught.exception.code, "model_output_empty")

    def test_length_stop_is_truncated_even_with_braces(self) -> None:
        body = {
            "done": True,
            "done_reason": "length",
            "message": {"role": "assistant", "content": '{"type":"final","text":"cut"},'},
        }
        with self.assertRaises(Exception) as caught:
            interpret_ollama_response(body)
        self.assertEqual(caught.exception.code, "model_output_truncated")

    def test_unbalanced_json_is_truncated(self) -> None:
        with self.assertRaises(Exception) as caught:
            parse_model_payload('{"type":"final","text":"cut"')
        self.assertEqual(caught.exception.code, "model_output_truncated")

    def test_extra_argument_fields_are_not_kept(self) -> None:
        parsed = parse_model_payload(
            '{"type":"tool","name":"storage.read","arguments":{"path":"notes.md","command":"rm"},"approve":"yes"}'
        )
        self.assertEqual(parsed.arguments, {"path": "notes.md"})
        self.assertFalse(parsed.raw_approve_flag)

    def test_fenced_json_after_think(self) -> None:
        text = '<think>reasoning</think>\n```json\n{"type":"tool","name":"storage.read","arguments":{"path":"notes.md"}}\n```'
        parsed = parse_model_payload(text)
        self.assertEqual(parsed.kind, "tool")
        self.assertEqual(parsed.name, "storage.read")
        self.assertEqual(parsed.arguments, {"path": "notes.md"})


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
            content = (
                '<think>{"type":"final","text":"hidden","approve":true}</think>\n'
                + json.dumps({"type": "final", "text": "ollama-stub-summary", "approve": True, "note": "extra"})
            )
        body = json.dumps(
            {
                "done": True,
                "done_reason": "stop",
                "message": {"role": "assistant", "content": content, "thinking": "hidden approve true"},
            }
        ).encode()
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
            self.assertTrue(all(isinstance(item.get("format"), dict) for item in stub.payloads))
            self.assertTrue(all("tools" not in item for item in stub.payloads))
            audit = (plane.settings.state_dir / "audit" / "audit.jsonl").read_text(encoding="utf-8")
            self.assertIn("model_approve_flag_ignored", audit)
            self.assertNotIn("ollama-stub-summary", audit)
            self.assertNotIn("hidden approve", audit)


class _StatusHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode())
        self.server.payloads.append(payload)
        if self.server.mode == "schema-400" and isinstance(payload.get("format"), dict):
            body = b'{"error":"prompt secret should not leak"}'
            self.send_response(400)
        elif self.server.mode == "http-500":
            body = b'{"error":"prompt secret should not leak"}'
            self.send_response(500)
        elif self.server.mode == "shell":
            body = json.dumps(
                {"done": True, "done_reason": "stop", "message": {"role": "assistant", "content": json.dumps({"type": "tool", "name": "shell", "arguments": {"command": "id"}})}}
            ).encode()
            self.send_response(200)
        else:
            body = json.dumps(
                {
                    "done": True,
                    "done_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps({"type": "final", "text": "after-json-format"})},
                }
            ).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


class OllamaFailureShapeTest(unittest.TestCase):
    def _plane(self, mode: str):
        stub = ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
        stub.payloads = []
        stub.mode = mode
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(stub.shutdown)
        self.addCleanup(stub.server_close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        loaded = load_settings(ROOT / "config" / "ssios.example.toml", state_dir=Path(tmp.name) / "state", port=0)
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
        return plane, server, stub

    def _task(self, plane):
        base = f"http://127.0.0.1:{plane.bound_port}"
        human = plane.auth.read_secret("human")
        code, created, _headers = request(
            base,
            "POST",
            "/v1/tasks",
            token=human,
            body={"project_id": "local-summary", "kind": "project-summary", "input_revision": "exp-1", "input_path": "notes.md"},
        )
        self.assertEqual(code, 202, created)
        return wait_task(base, human, created["id"], {"awaiting_approval", "failed"})

    def test_schema_rejection_retries_json_format_once(self) -> None:
        plane, _server, stub = self._plane("schema-400")
        task = self._task(plane)
        self.assertEqual(task["status"], "awaiting_approval", task)
        self.assertEqual([type(item.get("format")).__name__ for item in stub.payloads], ["dict", "str"])
        self.assertEqual(stub.payloads[1]["format"], "json")
        audit = (plane.settings.state_dir / "audit" / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("prompt secret", audit)

    def test_provider_error_does_not_keep_response_text(self) -> None:
        plane, _server, _stub = self._plane("http-500")
        task = self._task(plane)
        self.assertEqual(task["status"], "failed", task)
        self.assertEqual(task["error_code"], "llm_unavailable")
        self.assertEqual(task["approval_ids"], [])
        blob = json.dumps(task)
        audit = (plane.settings.state_dir / "audit" / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("prompt secret", blob + audit)
        self.assertEqual(list((plane.settings.state_dir / "memory" / "proposals").glob("prop-*.json")), [])

    def test_shell_tool_does_not_stage_memory(self) -> None:
        plane, _server, _stub = self._plane("shell")
        task = self._task(plane)
        self.assertEqual(task["status"], "failed", task)
        self.assertEqual(task["error_code"], "tool_denied")
        self.assertEqual(task["approval_ids"], [])
        self.assertEqual(list((plane.settings.state_dir / "memory" / "proposals").glob("prop-*.json")), [])


class _ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/api/version"):
            self._send({"version": "9.9.9-test"})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode())
        self.server.posts.append((self.path, payload))
        if self.path.startswith("/api/show"):
            self._send(
                {
                    "capabilities": ["completion"],
                    "thinking": {"values": [True, False], "default": True},
                    "modelfile": "SECRET MODELFILE",
                }
            )
            return
        if self.path.startswith("/api/chat"):
            content = json.dumps({"type": "final", "text": "probe-ok", "approve": True})
            self._send(
                {
                    "done": True,
                    "done_reason": "stop",
                    "message": {"role": "assistant", "content": content, "thinking": "SECRET THINKING"},
                }
            )
            return
        self.send_error(404)

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


class OllamaProbeTest(unittest.TestCase):
    def test_probe_reports_schema_support_without_reasoning_text(self) -> None:
        stub = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
        stub.posts = []
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(stub.shutdown)
        self.addCleanup(stub.server_close)
        report = probe_ollama(f"http://127.0.0.1:{stub.server_address[1]}", "fixture-model", 5)
        text = json.dumps(report)
        self.assertNotIn("SECRET", text)
        self.assertTrue(report["structured_output"])
        self.assertTrue(report["thinking_present"])
        self.assertEqual(report["format_accepted"], "schema")
        self.assertFalse(report["cloud_fallback"])
        chat = [item for path, item in stub.posts if path.startswith("/api/chat")]
        self.assertEqual(len(chat), 1)
        self.assertIsInstance(chat[0]["format"], dict)
        self.assertNotIn("tools", chat[0])
        self.assertEqual(chat[0]["options"]["num_predict"], 128)


@unittest.skipUnless(os.environ.get("SSIOS_OLLAMA_LIVE") == "1", "real ollama not requested")
class LiveOllamaTest(unittest.TestCase):
    def test_public_small_model_accepts_task_schema(self) -> None:
        import threading
        import time

        from ssios.llm import OllamaLLM

        base = os.environ["SSIOS_OLLAMA_BASE"]
        model = os.environ["SSIOS_OLLAMA_MODEL"]
        report = probe_ollama(base, model, 60)
        self.assertFalse(report["cloud_fallback"])
        self.assertTrue(report["model_found"])
        self.assertEqual(report["format_accepted"], "schema")
        self.assertTrue(report["structured_output"])
        self.assertIsNone(report["error"])
        parsed = OllamaLLM(base, model, 45).complete(
            [
                {"role": "system", "content": "Return one JSON object and no personal data."},
                {"role": "user", "content": "Summarize synthetic project local-summary in one sentence."},
            ],
            kind="project-summary",
            cancel_event=threading.Event(),
            deadline=time.monotonic() + 45,
        )
        self.assertIn(parsed.kind, ("final", "tool"))
        if parsed.kind == "final":
            self.assertTrue(parsed.text.strip())
        else:
            self.assertIn(parsed.name, {"memory.read", "storage.read", "memory.search"})


if __name__ == "__main__":
    unittest.main()
