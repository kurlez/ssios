"""Codex guest configuration, the mock Responses proxy, and fail-closed startup."""

from __future__ import annotations

import json
import socket
import unittest
from dataclasses import replace
from pathlib import Path

from support import PlaneTest, wait_task

from ssios.codex_guest import (
    RequestBudget,
    ResponsesProxy,
    asset_for_machine,
    codex_config_text,
    guest_entry_source,
    prepare_codex_binary,
    prepared_binary,
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
        self.assertEqual(loaded.codex_model, "mock-summary")
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


class PrepareSkip(unittest.TestCase):
    def test_prepare_is_not_called_from_unit_tests(self) -> None:
        self.assertTrue(callable(prepare_codex_binary))
