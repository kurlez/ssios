"""Codex guest configuration, the mock Responses proxy, and fail-closed startup."""

from __future__ import annotations

import ctypes
import json
import os
import socket
import struct
import unittest
from dataclasses import replace
from pathlib import Path

from support import PlaneTest, wait_task

from ssios.codex_guest import (
    RequestBudget,
    ResponsesProxy,
    _with_upstream,
    asset_for_machine,
    boundary_error_code,
    classify_guest_result,
    clean_tool_arguments,
    codex_config_text,
    codex_items_from_ollama,
    exec_commands_from_text,
    final_exception_line,
    guest_entry_source,
    items_from_ollama_chat,
    ollama_smoke_preflight,
    responses_to_chat,
    open_vsock,
    prepare_codex_binary,
    prepared_binary,
    render_codex_sse,
    selfcheck_failure,
    sockaddr_vm,
    sse_response,
    verify_sha256,
    workspace_diff,
)
from ssios.config import load_settings
from ssios.errors import SsiosError
from ssios.runtime import cloud_hypervisor_guest_command
from ssios.vmguest import redact_text


class PinAndConfigTest(unittest.TestCase):
    def test_pin_and_guest_config_stay_on_the_mock_provider(self) -> None:
        asset = asset_for_machine("x86_64")
        self.assertEqual(len(asset["archive_sha256"]), 64)
        self.assertEqual(len(asset["binary_sha256"]), 64)
        self.assertTrue(asset["url"].endswith("codex-x86_64-unknown-linux-musl.tar.gz"))
        text = codex_config_text(19090, "mock-summary")
        self.assertIn('model_provider = "ssios"', text)
        self.assertIn('wire_api = "responses"', text)
        self.assertIn("requires_openai_auth = false", text)
        self.assertIn("enabled = false", text)
        self.assertIn('web_search = "disabled"', text)
        self.assertIn('metrics_exporter = "none"', text)
        self.assertNotIn("[model_providers.ollama]", text)
        self.assertNotIn("--oss", text)
        entry = guest_entry_source(19090)
        self.assertIn("127.0.0.1", entry)
        self.assertIn("workspace-write", entry)
        self.assertNotIn("danger-full-access", entry)
        self.assertNotIn("yolo", entry)
        self.assertNotIn("ollama", entry)

    def test_checksum_mismatch_is_rejected(self) -> None:
        path = Path("/tmp/ssios-codex-checksum")
        path.write_bytes(b"not-the-binary")
        with self.assertRaises(SsiosError) as caught:
            verify_sha256(path, "0" * 64)
        self.assertEqual(caught.exception.code, "codex_checksum_mismatch")
        verify_sha256(path, __import__("hashlib").sha256(b"not-the-binary").hexdigest())

    def test_settings_default_to_the_summary_worker(self) -> None:
        loaded = load_settings(Path("config/ssios.example.toml"), state_dir=Path("/tmp/ssios-codex-settings"), port=0)
        self.assertEqual(loaded.runtime_agent, "summary")
        self.assertEqual(loaded.codex_model, "qwen3:14b")
        self.assertEqual(loaded.codex_num_ctx, 16384)
        self.assertEqual(loaded.codex_max_requests, 4)
        self.assertIn("qwen2.5-coder:7b", loaded.codex_allow_models)
        self.assertIn("muse-glimmer:30b-mlx", loaded.codex_allow_models)
        with self.assertRaises(SsiosError):
            load_settings_text('agent = "remote"\n', loaded.state_dir)

    def test_prepare_refuses_to_invent_a_binary(self) -> None:
        root = Path("/tmp/ssios-codex-missing")
        if root.exists():
            import shutil

            shutil.rmtree(root)
        with self.assertRaises(SsiosError) as caught:
            prepared_binary(root, "x86_64")
        self.assertEqual(caught.exception.code, "codex_not_prepared")


def load_settings_text(runtime_extra: str, state: Path):
    path = state / "bad.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[runtime]\nbackend = \"dev-guard\"\n" + runtime_extra, encoding="utf-8")
    return load_settings(path, state_dir=state, port=0)


class ProxyTest(unittest.TestCase):
    def test_budget_and_redaction(self) -> None:
        budget = RequestBudget(1, 32)
        self.assertEqual(budget.allow(4), "ok")
        self.assertEqual(budget.allow(4), "too_many_requests")
        huge = RequestBudget(2, 8)
        self.assertEqual(huge.allow(9), "request_too_large")
        redacted = redact_text(
            'Bearer ya29.secret123 refresh_token":"1//refresh-token" "client_secret":"super-secret"',
            [],
        )
        self.assertNotIn("ya29.", redacted)
        self.assertNotIn("1//refresh", redacted)
        self.assertNotIn("super-secret", redacted)
        self.assertIn("[redacted]", redacted)

    def test_unix_proxy_returns_the_sse_subset_and_then_rejects(self) -> None:
        root = Path("/tmp/ssios-codex-proxy")
        if root.exists():
            import shutil

            shutil.rmtree(root)
        root.mkdir()
        proxy = ResponsesProxy(root / "vsock.sock", "Synthetic project summary\n- NORTHWIND-FIXTURE-NOTE stays local.", "mock-summary", RequestBudget(1, 2000), root / "proxy.log")
        proxy.start()
        try:
            first = _post(proxy.listen_path, {"model": "mock-summary", "stream": True, "input": "notes"})
            self.assertIn(b"event: response.output_text.delta", first)
            self.assertIn(b"event: response.completed", first)
            self.assertIn(b"NORTHWIND-FIXTURE-NOTE", first)
            second = _post(proxy.listen_path, {"model": "mock-summary", "stream": True, "input": "again"})
            self.assertIn(b"429", second)
            self.assertTrue(proxy.rejected)
            self.assertEqual(proxy.served, 1)
            log = (root / "proxy.log").read_text(encoding="utf-8")
            self.assertIn('"decision": "ok"', log)
            self.assertEqual((root / "proxy.log").stat().st_mode & 0o777, 0o600)
        finally:
            proxy.close()
        body = sse_response("hello from the mock proxy, long enough")
        self.assertIn(b"response.created", body)
        before = {"notes.md": b"old"}
        folder = root / "workspace"
        folder.mkdir()
        (folder / "notes.md").write_bytes(b"new")
        self.assertIn("changed notes.md", workspace_diff(before, folder))


def _post(path: Path, payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    request = (
        f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode("ascii") + body
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(path))
    client.sendall(request)
    data = b""
    while True:
        chunk = client.recv(65536)
        if not chunk:
            break
        data += chunk
    client.close()
    return data


class TranslateTest(unittest.TestCase):
    def test_null_fields_are_removed_and_json_text_becomes_a_command(self) -> None:
        cleaned = clean_tool_arguments('{"cmd":"printf IN > marker.txt","tty":null,"justification":"please","sandbox_permissions":"use_default"}')
        self.assertEqual(json.loads(cleaned), {"cmd": "printf IN > marker.txt"})
        text = '```json\n{"name":"exec_command","arguments":{"cmd":"printf IN > marker.txt"}}\n```'
        self.assertEqual(exec_commands_from_text(text), ["printf IN > marker.txt"])
        raw = (
            'event: response.output_item.done\n'
            'data: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":"{\\"name\\": \\"exec_command\\", \\"arguments\\": {\\"cmd\\": \\"printf IN > marker.txt\\"}}"}]}}\n\n'
            'event: response.completed\n'
            'data: {"type":"response.completed","response":{"usage":{"output_tokens":12}}}\n\n'
        ).encode()
        items, tokens = codex_items_from_ollama(raw)
        self.assertEqual(tokens, 12)
        self.assertEqual(items[0]["name"], "exec_command")
        rendered = render_codex_sse(items)
        self.assertIn(b"response.function_call_arguments.done", rendered)
        self.assertNotIn(b"tty", rendered)

    def test_proxy_strips_auth_and_does_not_log_the_prompt(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["auth"] = self.headers.get("Authorization")
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                body = (
                    'event: response.output_text.done\ndata: {"type":"response.output_text.done","text":"The workspace copy kept the marker. Outside write and network were refused."}\n\n'
                    'event: response.output_item.done\ndata: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":"The workspace copy kept the marker. Outside write and network were refused."}]}}\n\n'
                    'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"output_tokens":16}}}\n\n'
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = Path("/tmp/ssios-codex-forward")
        if root.exists():
            import shutil

            shutil.rmtree(root)
        root.mkdir()
        port = server.server_address[1]
        proxy = ResponsesProxy(
            root / "vsock.sock",
            "",
            "qwen2.5-coder:7b",
            RequestBudget(2, 4000),
            root / "proxy.log",
            upstream=f"http://127.0.0.1:{port}/v1/responses",
            max_output_tokens=64,
        )
        proxy.start()
        try:
            payload = {"model": "qwen2.5-coder:7b", "stream": True, "input": "SECRET-PROMPT-BODY"}
            body = json.dumps(payload).encode()
            request = (
                f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer ya29.secret-token\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode() + body
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(proxy.listen_path))
            client.sendall(request)
            data = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
            client.close()
            self.assertIsNone(seen["auth"])
            self.assertIn(b"Outside write and network were refused", data)
            log = (root / "proxy.log").read_text(encoding="utf-8")
            self.assertNotIn("SECRET-PROMPT-BODY", log)
            self.assertIn('"sha256"', log)
            self.assertNotIn("ya29.", log)
        finally:
            proxy.close()
            server.shutdown()


class CommandTest(unittest.TestCase):
    def test_vsock_command_has_no_nic(self) -> None:
        argv = cloud_hypervisor_guest_command(
            binary="cloud-hypervisor",
            kernel="/tmp/bzImage",
            initramfs="/tmp/initrd",
            api_socket=Path("/tmp/api.sock"),
            cpus=1,
            memory_mb=1536,
            filesystems=[("tools", Path("/tmp/tools.sock"))],
            vsock=Path("/tmp/vsock.sock"),
        )
        self.assertIn("--vsock", argv)
        self.assertIn("cid=3,socket=/tmp/vsock.sock", argv)
        self.assertNotIn("--net", argv)
        self.assertNotIn("yolo", " ".join(argv))


class FailClosedTest(PlaneTest):
    def test_macos_codex_does_not_run_on_the_host(self) -> None:
        self.plane.settings = replace(self.plane.settings, runtime_backend="macos-vm", runtime_agent="codex", memory_mb=2048)

        class Stub:
            name = "macos-vm"

            def can_launch(self) -> bool:
                return True

        self.plane.guest_runtime = Stub()

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
        self.assertIn("macos-vm", task.get("error_detail") or "")
        self.assertEqual(task["approval_ids"], [])

    def test_missing_binary_does_not_download_or_run_the_host_agent(self) -> None:
        self.plane.settings = replace(
            self.plane.settings,
            runtime_backend="cloud-hypervisor",
            runtime_agent="codex",
            memory_mb=2048,
        )

        class Stub:
            name = "cloud-hypervisor"

            def can_launch(self) -> bool:
                return True

        self.plane.guest_runtime = Stub()

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
        self.assertIn("vm-prepare", task.get("error_detail") or "")
        self.assertFalse(any((self.state / "cache").rglob("*.tar.gz")))

    def test_internal_exception_writes_a_diagnostic(self) -> None:
        self.plane.settings = replace(self.plane.settings, runtime_backend="macos-vm")

        class Boom:
            name = "macos-vm"

            def can_launch(self) -> bool:
                return True

            def run(self, session, cancel, deadline):
                del cancel, deadline
                (session.root / "serial.log").write_text("console: booted\n" + self_token + "\n", encoding="utf-8")
                raise RuntimeError("AF_UNIX path too long " + self_token)

        self_token = self.human
        self.plane.guest_runtime = Boom()
        code, created, _headers = self.call(
            "POST",
            "/v1/tasks",
            token=self.human,
            body={"project_id": "demo-alpha", "kind": "project-summary", "input_revision": "exp-1", "input_path": "notes.md"},
        )
        self.assertEqual(code, 202, created)
        task = wait_task(self.base, self.human, created["id"], {"failed"})
        self.assertEqual(task["error_code"], "internal")
        self.assertIn("RuntimeError", task.get("error_detail") or "")
        self.assertNotIn(self.human, task.get("error_detail") or "")
        diagnostic = Path(task["diagnostic_path"])
        text = diagnostic.read_text(encoding="utf-8")
        self.assertNotIn(self.human, text)
        self.assertIn("console: booted", text)
        self.assertIn("serial.log", text)
        self.assertIn("AF_UNIX path too long", text)
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)


class VsockRelayTest(unittest.TestCase):
    def test_relay_uses_family_40_when_af_vsock_is_absent(self) -> None:
        source = guest_entry_source(19090)
        self.assertNotIn("socket.AF_VSOCK", source)
        self.assertIn("family = 40", source)
        self.assertEqual(len(sockaddr_vm(19090, 2)), 16)
        self.assertEqual(sockaddr_vm(19090, 2)[:12], struct.pack("<HHII", 40, 0, 19090, 2))
        left, right = socket.socketpair()
        self.addCleanup(left.close)

        class Libc:
            def socket(self, family, typ, proto):
                self.family = family
                return right.detach()

            def connect(self, fd, addr, length):
                self.packed = addr.raw[:length]
                return 0

            def setsockopt(self, fd, level, option, buf, length):
                self.timeouts.append((option, bytes(buf.raw[:length])))
                return 0

        libc = Libc()
        libc.timeouts = []
        remote = open_vsock(2, 19090, timeout=1, libc=libc)
        self.addCleanup(remote.close)
        self.assertEqual(libc.family, 40)
        self.assertEqual(libc.packed, sockaddr_vm(19090, 2))
        self.assertTrue(libc.timeouts)
        self.assertEqual(libc.timeouts[-1][1], struct.pack("@qq", 0, 0))
        self.assertIn("os.read", guest_entry_source(19090))
        remote.sendall(b"ok")
        self.assertEqual(left.recv(2), b"ok")

    def test_selfcheck_and_exception_line(self) -> None:
        code, detail = selfcheck_failure({"ok": False, "error_code": "guest_virtiofs_unavailable", "detail": "virtio-fs not mounted: /input"})
        self.assertEqual(code, "guest_virtiofs_unavailable")
        self.assertIn("/input", detail)
        self.assertIsNone(selfcheck_failure({"ok": True, "detail": "vsock connected"}))
        text = (
            'File "/scratch/entry.py", line 28, in handle\n'
            "    remote = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)\n"
            "AttributeError: module 'socket' has no attribute 'AF_VSOCK'\n"
        )
        self.assertEqual(final_exception_line(text), "AttributeError: module 'socket' has no attribute 'AF_VSOCK'")
        source = guest_entry_source(19090)
        self.assertIn('"result": "guest_exit"', source)
        self.assertIn('sandbox_mode="workspace-write"', source)
        self.assertIn("printf '%s\\\\n' SSIOS-IN > /workspace/marker.txt", source)
        self.assertIn("printf '%s\\\\n' SSIOS-OUT > /opt/ssios-outside", source)
        self.assertIn("def sandbox_probe", source)
        self.assertLess(source.index('"-a",\n                "never",\n                "exec"'), source.index("denied = sandbox_probe()"))
        good = {
            "text": "The workspace copy kept the marker. Outside write and network were refused.",
            "exit_code": 1,
        }
        net = {"interfaces": ["lo"], "outbound": False}
        boundary = {"marker": True, "outside": False, "denied": True}
        ran, detail = classify_guest_result(good, net, 1, False, boundary, vmm_exit=0)
        self.assertTrue(ran, detail)
        ran, detail = classify_guest_result(good, net, 1, False, boundary, vmm_exit=1)
        self.assertTrue(ran, detail)
        empty = {"text": "", "exit_code": 1}
        ran, detail = classify_guest_result(empty, net, 1, False, boundary, 0)
        self.assertFalse(ran)
        self.assertIn("last_message_empty", detail)
        self.assertIn("codex_exit=1", detail)
        ran, detail = classify_guest_result(good, net, 1, False, {"outside": True}, 0)
        self.assertFalse(ran)
        self.assertIn("outside_write", detail)
        self.assertTrue(detail.startswith("codex guest failed:"))
        ran, detail = classify_guest_result(good, net, 1, False, {"marker": False, "outside": False, "denied": True}, 0)
        self.assertFalse(ran)
        self.assertIn("probe_marker_missing", detail)
        self.assertEqual(boundary_error_code({"marker": False, "outside": False, "denied": True}), "probe_marker_missing")
        ran, detail = classify_guest_result(good, net, 1, False, {"marker": True, "outside": False, "denied": False}, 0)
        self.assertFalse(ran)
        self.assertIn("probe_sandbox_not_denied", detail)
        self.assertEqual(boundary_error_code({"marker": True, "outside": False, "denied": False}), "probe_sandbox_not_denied")
        self.assertIsNone(boundary_error_code(boundary))

    def test_empty_upstream_is_not_reported_as_the_request_cap(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or "0")
                self.rfile.read(length)
                body = b"event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"usage\":{\"output_tokens\":0}}}\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        root = Path("/tmp/ssios-upstream-empty")
        if root.exists():
            import shutil
            shutil.rmtree(root)
        root.mkdir()
        os.chmod(root, 0o700)
        proxy = ResponsesProxy(
            root / "vsock.sock",
            "unused",
            "qwen2.5-coder:7b",
            RequestBudget(4, 200000),
            root / "proxy.log",
            upstream=f"http://127.0.0.1:{server.server_address[1]}/v1/responses",
            allow_models=("qwen2.5-coder:7b",),
        )
        proxy.start()
        self.addCleanup(proxy.close)
        for _ in range(2):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(proxy.listen_path))
            payload = json.dumps({"model": "qwen2.5-coder:7b", "stream": True, "input": "hi"}).encode()
            sock.sendall(
                b"POST /v1/responses HTTP/1.1\r\nHost: h\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(payload)).encode()
                + b"\r\n\r\n"
                + payload
            )
            sock.recv(200)
            sock.close()
        self.assertEqual(proxy.served, 0)
        self.assertEqual(proxy.upstream_failure["error"], "upstream_empty")
        self.assertGreater(proxy.upstream_failure["request_bytes"], 0)
        self.assertGreater(proxy.upstream_failure["response_bytes"], 0)
        log = (root / "proxy.log").read_text(encoding="utf-8")
        self.assertIn("upstream_empty", log)
        self.assertIn("upstream_status", log)
        self.assertIn("latency_ms", log)
        self.assertNotIn('"input"', log)
        detail, code = _with_upstream(proxy, "codex guest failed: proxy_rejected", None)
        self.assertEqual(code, "upstream_empty")
        self.assertIn("status=200", detail)
        self.assertNotIn("too_many_requests", detail.split(";")[0])

    def test_chat_translation_sets_num_ctx_and_a_4xx_is_fatal(self) -> None:
        chat = responses_to_chat(
            {
                "model": "qwen2.5-coder:7b",
                "instructions": "system",
                "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": "list files"}]},
                    {"type": "function_call", "name": "exec_command", "arguments": "{\"cmd\":\"ls\"}"},
                    {"type": "function_call_output", "call_id": "call_0", "output": "notes.md"},
                ],
                "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
                "tool_choice": "required",
                "stream": True,
            },
            16384,
            1024,
        )
        self.assertEqual(chat["options"]["num_ctx"], 16384)
        self.assertFalse(chat["stream"])
        self.assertEqual(chat["messages"][0]["role"], "system")
        self.assertEqual(chat["messages"][-1]["role"], "tool")
        self.assertEqual(chat["tools"][0]["function"]["name"], "exec_command")
        rendered, tokens = items_from_ollama_chat(
            b'{"message":{"role":"assistant","content":"","tool_calls":[{"function":{"name":"exec_command","arguments":{"cmd":"pwd"}}}]},"done":true,"eval_count":4}\n'
        )
        self.assertEqual(rendered[0]["name"], "exec_command")
        self.assertIn("pwd", rendered[0]["arguments"])
        self.assertEqual(tokens, 4)

        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or "0")
                seen["body"] = json.loads(self.rfile.read(length))
                body = b'{"error":{"message":"request (4098 tokens) exceeds the available context size (4096 tokens)"}}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        root = Path("/tmp/ssios-upstream-4xx")
        if root.exists():
            import shutil

            shutil.rmtree(root)
        root.mkdir()
        os.chmod(root, 0o700)
        proxy = ResponsesProxy(
            root / "vsock.sock",
            "unused",
            "qwen2.5-coder:7b",
            RequestBudget(4, 200000),
            root / "proxy.log",
            upstream=f"http://127.0.0.1:{server.server_address[1]}/api/chat",
            allow_models=("qwen2.5-coder:7b",),
            num_ctx=16384,
        )
        proxy.start()
        self.addCleanup(proxy.close)

        def once() -> None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(proxy.listen_path))
            payload = json.dumps({"model": "qwen2.5-coder:7b", "stream": True, "input": "SECRET-PROMPT"}).encode()
            sock.sendall(
                b"POST /v1/responses HTTP/1.1\r\nHost: h\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(payload)).encode()
                + b"\r\n\r\n"
                + payload
            )
            sock.recv(400)
            sock.close()

        once()
        once()
        self.assertEqual(proxy.budget.requests, 1)
        self.assertTrue(proxy.upstream_failure["fatal"])
        self.assertEqual(proxy.upstream_failure["status"], 400)
        self.assertEqual(seen["body"]["options"]["num_ctx"], 16384)
        self.assertIn("SECRET-PROMPT", json.dumps(seen["body"]["messages"]))
        log = (root / "proxy.log").read_text(encoding="utf-8")
        self.assertNotIn("SECRET-PROMPT", log)
        detail, code = _with_upstream(proxy, "codex guest failed: last_message_empty", None)
        self.assertEqual(code, "upstream_error")
        self.assertEqual(detail.count("status="), 1)
        self.assertIn("status=400", detail)
        self.assertNotIn("status=None", detail)
        self.assertNotIn("too_many_requests", detail)

    def test_preflight_checks_the_responses_stream(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            context_length = 16384

            def do_GET(self):
                if self.path == "/api/tags":
                    body = json.dumps({"models": [{"name": "qwen2.5-coder:7b"}]}).encode()
                elif self.path == "/api/ps":
                    body = json.dumps({"models": [{"name": "qwen2.5-coder:7b", "context_length": self.context_length}]}).encode()
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or "0")
                raw = json.loads(self.rfile.read(length))
                if self.path != "/api/chat" or raw.get("options", {}).get("num_ctx") != 16384:
                    self.send_error(404)
                    return
                body = json.dumps({"message": {"content": "ok"}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        report = ollama_smoke_preflight(base, model="qwen2.5-coder:7b", timeout=5, num_ctx=16384)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["context_length"], 16384)
        missing = ollama_smoke_preflight(base, model="missing:1b", timeout=5, num_ctx=16384)
        self.assertFalse(missing["ok"])
        self.assertIn("ollama pull", missing["detail"])
        Handler.context_length = 4096
        short = ollama_smoke_preflight(base, model="qwen2.5-coder:7b", timeout=5, num_ctx=16384)
        self.assertFalse(short["ok"])
        self.assertIn("4096", short["detail"])
        self.assertIn("OLLAMA_CONTEXT_LENGTH=16384", short["detail"])
        self.assertIn("ollama create", short["detail"])


class PrepareSkip(unittest.TestCase):
    def test_prepare_is_not_called_from_unit_tests(self) -> None:
        self.assertTrue(callable(prepare_codex_binary))
