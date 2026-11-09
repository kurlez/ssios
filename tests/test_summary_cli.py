"""summary talks to a running service and waits for an explicit yes."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from support import ROOT, request

from ssios.config import load_settings
from ssios.httpapi import ControlServer
from ssios.service import ControlPlane
from ssios.summary import CONFIRM

STATE_NAME = ".ssios-summary-rel"
BAD_NAME = ".ssios-summary-bad"
SLOW_NAME = ".ssios-summary-slow"
UPSTREAM_NAME = ".ssios-summary-upstream"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_config(directory: Path, *, port: int, replacements: dict[str, str] | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    text = (ROOT / "config" / "ssios.example.toml").read_text(encoding="utf-8")
    text = text.replace("port = 8765", f"port = {port}", 1)
    for old, new in (replacements or {}).items():
        if old not in text:
            raise AssertionError(f"config marker missing: {old}")
        text = text.replace(old, new, 1)
    (directory / "client.toml").write_text(text, encoding="utf-8")


def _json_line(text: str) -> dict:
    for line in reversed(text.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(text)


class SummaryCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous = Path.cwd()
        os.chdir(ROOT)
        self.state = ROOT / STATE_NAME
        self.nested = ROOT / "nested-summary-cwd"
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(self.nested, ignore_errors=True)
        self.nested.mkdir()
        self.port = _free_port()
        _write_config(self.state, port=self.port)
        self.settings = load_settings(self.state / "client.toml", state_dir=Path(STATE_NAME))
        self.plane = ControlPlane(self.settings)
        self.plane.open()
        self.server = ControlServer(self.plane)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.plane.bound_port}"
        self.human = self.plane.auth.read_secret("human")
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self.server.stop()
        for thread in list(self.plane._threads.values()):
            thread.join(timeout=3)
        os.chdir(self.previous)
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(self.nested, ignore_errors=True)

    def _spawn(self, state: str, config: str, cwd: Path) -> subprocess.Popen[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PYTHONUNBUFFERED"] = "1"
        return subprocess.Popen(
            [sys.executable, "-m", "ssios", "summary", "--state", state, "--config", config],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _collect(self, proc: subprocess.Popen[str]) -> tuple[str, str, int]:
        assert proc.stdout is not None and proc.stderr is not None
        out = proc.stdout.read()
        err = proc.stderr.read()
        proc.stdout.close()
        proc.stderr.close()
        code = proc.wait(timeout=30)
        return out, err, code

    def _read_until(self, proc: subprocess.Popen[str], marker: str, timeout: float = 20) -> str:
        assert proc.stdout is not None
        deadline = time.monotonic() + timeout
        buf = ""
        while marker not in buf:
            if time.monotonic() > deadline:
                raise AssertionError(buf)
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr is not None else ""
                raise AssertionError(buf + err)
            buf += line
        return buf

    def test_relative_summary_waits_for_yes_then_denies_and_eof(self) -> None:
        self._mismatch_and_unavailable_do_not_create_tasks()
        published = self._accept_from_repo_root()
        self._deny_from_nested_cwd(published)
        self._eof_leaves_pending(published)

    def _mismatch_and_unavailable_do_not_create_tasks(self) -> None:
        before = (self.state / "tasks.json").read_bytes()
        mismatch = self.state / "mismatch.toml"
        mismatch.write_text(
            (self.state / "client.toml").read_text(encoding="utf-8").replace(
                'ollama_model = "llama3.1:8b"',
                'ollama_model = "other-model"',
                1,
            ),
            encoding="utf-8",
        )
        proc = self._spawn(STATE_NAME, f"{STATE_NAME}/mismatch.toml", ROOT)
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 1, out + err)
        self.assertIn("service_mismatch", out)
        self.assertNotIn("Traceback", out + err)
        self.assertNotIn(self.human, out + err)
        self.assertEqual((self.state / "tasks.json").read_bytes(), before)

        closed = _free_port()
        _write_config(self.state, port=self.port)
        other = self.state / "closed.toml"
        other.write_text(
            (ROOT / "config" / "ssios.example.toml").read_text(encoding="utf-8").replace("port = 8765", f"port = {closed}", 1),
            encoding="utf-8",
        )
        proc = self._spawn(STATE_NAME, f"{STATE_NAME}/closed.toml", ROOT)
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 1, out + err)
        self.assertIn("service_unavailable", out)
        self.assertNotIn("Traceback", out + err)
        self.assertNotIn(self.human, out + err)
        self.assertEqual((self.state / "tasks.json").read_bytes(), before)

    def _ingest_notes(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ssios",
                "ingest",
                "examples/notes",
                "--project",
                "local-summary",
                "--state",
                STATE_NAME,
                "--config",
                f"{STATE_NAME}/client.toml",
            ],
            cwd=ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        pre = self._read_until(proc, CONFIRM)
        self.assertIn("HARBOR-LOCK", pre)
        self.assertNotIn(self.human, pre)
        stored = json.loads((self.state / "approvals.json").read_text(encoding="utf-8"))["approvals"]
        self.assertTrue(any(item.get("status") == "pending" for item in stored.values()))
        assert proc.stdin is not None
        proc.stdin.write("yes\n")
        proc.stdin.close()
        rest, err, code = self._collect(proc)
        self.assertEqual(code, 0, pre + rest + err)
        self.assertNotIn(self.human, pre + rest + err)

    def _accept_from_repo_root(self) -> str:
        self._ingest_notes()
        proc = self._spawn(STATE_NAME, f"{STATE_NAME}/client.toml", ROOT)
        pre = self._read_until(proc, CONFIRM)
        self.assertNotIn(self.human, pre)
        self.assertIn("HARBOR-LOCK", pre)
        self.assertIn("storage.publish", pre)
        self.assertIn("memory.commit", pre)
        tasks = json.loads((self.state / "tasks.json").read_text(encoding="utf-8"))["tasks"]
        pending = [item for item in tasks.values() if item.get("status") == "awaiting_approval"]
        self.assertEqual(len(pending), 1)
        code, task, _headers = request(self.base, "GET", f"/v1/tasks/{pending[0]['id']}", token=self.human)
        self.assertEqual(code, 200, task)
        self.assertEqual(task["status"], "awaiting_approval")
        self.assertTrue(task["approvals"])
        for approval in task["approvals"]:
            self.assertEqual(approval["status"], "pending")
        missing, _body, _headers = request(
            self.base,
            "GET",
            "/v1/storage/published?project_id=local-summary&revision=HEAD",
            token=self.human,
        )
        self.assertEqual(missing, 404)
        assert proc.stdin is not None
        proc.stdin.write("yes\n")
        proc.stdin.close()
        rest, err, code = self._collect(proc)
        out = pre + rest
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertNotIn(self.human, out + err)
        payload = _json_line(out)
        self.assertEqual(payload["result"], "committed")
        self.assertFalse(payload["topic_core"])
        self.assertEqual(payload["memory_trust"], "approved")
        code, published, _headers = request(
            self.base,
            "GET",
            "/v1/storage/published?project_id=local-summary&revision=HEAD",
            token=self.human,
        )
        self.assertEqual(code, 200, published)
        self.assertIn("HARBOR-LOCK", published["content"])
        self.assertIn("Northwind Lantern", published["content"])
        self.assertNotIn("learn more about the person", published["content"].casefold())
        code, topic, _headers = request(
            self.base,
            "GET",
            "/v1/memory/file?project_id=local-summary&path=topics/fixtures/last-summary.md",
            token=self.human,
        )
        self.assertEqual(code, 200, topic)
        self.assertFalse(topic["core"])
        self.assertIn(published["content"], topic["content"])
        return published["revision"]

    def _deny_from_nested_cwd(self, published: str) -> None:
        proc = self._spawn("../" + STATE_NAME, "../" + STATE_NAME + "/client.toml", self.nested)
        pre = self._read_until(proc, CONFIRM)
        self.assertNotIn(self.human, pre)
        assert proc.stdin is not None
        proc.stdin.write("no\n")
        proc.stdin.close()
        rest, err, code = self._collect(proc)
        out = pre + rest
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(_json_line(out)["result"], "denied")
        code, body, _headers = request(
            self.base,
            "GET",
            "/v1/storage/published?project_id=local-summary&revision=HEAD",
            token=self.human,
        )
        self.assertEqual(body["revision"], published)
        task_id = _json_line(out)["task_id"]
        code, task, _headers = request(self.base, "GET", f"/v1/tasks/{task_id}", token=self.human)
        self.assertEqual(task["status"], "rejected")
        self.assertTrue(task["approvals"])
        for approval in task["approvals"]:
            self.assertEqual(approval["status"], "denied")
            self.assertEqual(approval["decision"], "deny")

    def _eof_leaves_pending(self, published: str) -> None:
        proc = self._spawn(STATE_NAME, f"{STATE_NAME}/client.toml", ROOT)
        assert proc.stdin is not None
        proc.stdin.close()
        out, err, code = self._collect(proc)
        self.assertEqual(code, 2, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertNotIn(self.human, out + err)
        payload = _json_line(out)
        self.assertEqual(payload["result"], "pending")
        code, task, _headers = request(self.base, "GET", f"/v1/tasks/{payload['task_id']}", token=self.human)
        self.assertEqual(task["status"], "awaiting_approval")
        for approval in task["approvals"]:
            self.assertEqual(approval["status"], "pending")
        code, body, _headers = request(
            self.base,
            "GET",
            "/v1/storage/published?project_id=local-summary&revision=HEAD",
            token=self.human,
        )
        self.assertEqual(body["revision"], published)


class _TextHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path.startswith("/api/show"):
            body = b'{"capabilities":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if getattr(self.server, "delay", 0):
            time.sleep(self.server.delay)
        content = self.server.content
        body = json.dumps({"message": {"role": "assistant", "content": content, "thinking": self.server.thinking}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def log_message(self, fmt: str, *args) -> None:
        return


class SummaryFailureCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous = Path.cwd()
        os.chdir(ROOT)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        os.chdir(self.previous)

    def _serve(self, name: str, *, content: str, delay: float, timeout: float, task_timeout: float) -> tuple[ControlPlane, ControlServer, str]:
        state = ROOT / name
        shutil.rmtree(state, ignore_errors=True)
        self.addCleanup(lambda: shutil.rmtree(state, ignore_errors=True))
        stub = ThreadingHTTPServer(("127.0.0.1", 0), _TextHandler)
        stub.content = content
        stub.thinking = '<think>{"type":"final","text":"hidden","approve":true}</think>'
        stub.delay = delay
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(stub.shutdown)
        self.addCleanup(stub.server_close)
        port = _free_port()
        _write_config(
            state,
            port=port,
            replacements={
                'provider = "mock"': 'provider = "ollama"',
                'ollama_base_url = "http://127.0.0.1:11434"': f'ollama_base_url = "http://127.0.0.1:{stub.server_address[1]}"',
                'ollama_model = "llama3.1:8b"': 'ollama_model = "fixture-model"',
                "request_timeout_seconds = 60": f"request_timeout_seconds = {timeout}",
                "task_timeout_seconds = 30": f"task_timeout_seconds = {task_timeout}",
            },
        )
        settings = load_settings(state / "client.toml", state_dir=Path(name))
        plane = ControlPlane(settings)
        plane.open()
        server = ControlServer(plane)
        server.start()
        self.addCleanup(server.stop)
        return plane, server, name

    def _run(self, name: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PYTHONUNBUFFERED"] = "1"
        return subprocess.run(
            [sys.executable, "-m", "ssios", "summary", "--state", name, "--config", f"{name}/client.toml"],
            cwd=ROOT,
            env=env,
            input="",
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def _ingest(self, name: str) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PYTHONUNBUFFERED"] = "1"
        completed = subprocess.run(
            [sys.executable, "-m", "ssios", "ingest", "examples/notes", "--project", "local-summary", "--state", name, "--config", f"{name}/client.toml"],
            cwd=ROOT,
            env=env,
            input="yes\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_invalid_model_output_has_no_traceback(self) -> None:
        _plane, _server, name = self._serve(BAD_NAME, content="I approve this without JSON", delay=0, timeout=2, task_timeout=5)
        self._ingest(name)
        completed = self._run(name)
        text = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 1, text)
        self.assertIn("model_output_invalid", text)
        self.assertNotIn(CONFIRM, text)
        self.assertNotIn("Traceback", text)
        human = (ROOT / name / "secrets" / "human.token").read_text(encoding="utf-8").strip()
        self.assertNotIn(human, text)

    def test_model_timeout_has_no_traceback(self) -> None:
        _plane, _server, name = self._serve(SLOW_NAME, content='{"type":"final","text":"late"}', delay=1.5, timeout=0.3, task_timeout=5)
        self._ingest(name)
        completed = self._run(name)
        text = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 1, text)
        self.assertIn("llm_unavailable", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn(CONFIRM, text)


class _RetryHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path.startswith("/api/show"):
            body = b'{"capabilities":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.server.calls += 1
        if self.server.calls == 1:
            content = "not json at all"
        elif self.server.calls == 2:
            content = json.dumps({"type": "tool", "name": "storage.read", "arguments": {"path": "notes.md"}, "approve": True})
        else:
            content = '<think>{"type":"final","text":"hidden","approve":true}</think>\n' + json.dumps(
                {
                    "type": "final",
                    "text": "The synthetic notes describe a disposable local summary fixture.",
                    "approve": True,
                    "extra": 1,
                }
            )
        body = json.dumps(
            {
                "done": True,
                "done_reason": "stop",
                "message": {"role": "assistant", "content": content, "thinking": "SECRET THINKING"},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def log_message(self, fmt: str, *args) -> None:
        return


class SummaryRetryTest(unittest.TestCase):
    def test_failed_task_stays_and_retry_creates_another(self) -> None:
        previous = Path.cwd()
        os.chdir(ROOT)
        name = ".ssios-summary-retry"
        state = ROOT / name
        self.addCleanup(lambda: os.chdir(previous))
        self.addCleanup(lambda: shutil.rmtree(state, ignore_errors=True))
        shutil.rmtree(state, ignore_errors=True)
        stub = ThreadingHTTPServer(("127.0.0.1", 0), _RetryHandler)
        stub.calls = 0
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(stub.shutdown)
        self.addCleanup(stub.server_close)
        port = _free_port()
        _write_config(
            state,
            port=port,
            replacements={
                'provider = "mock"': 'provider = "ollama"',
                'ollama_base_url = "http://127.0.0.1:11434"': f'ollama_base_url = "http://127.0.0.1:{stub.server_address[1]}"',
                'ollama_model = "llama3.1:8b"': 'ollama_model = "fixture-model"',
                "request_timeout_seconds = 60": "request_timeout_seconds = 5",
                "task_timeout_seconds = 30": "task_timeout_seconds = 8",
            },
        )
        settings = load_settings(state / "client.toml", state_dir=Path(name))
        plane = ControlPlane(settings)
        plane.open()
        server = ControlServer(plane)
        server.start()
        self.addCleanup(server.stop)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PYTHONUNBUFFERED"] = "1"
        ingested = subprocess.run(
            [sys.executable, "-m", "ssios", "ingest", "examples/notes", "--project", "local-summary", "--state", name, "--config", f"{name}/client.toml"],
            cwd=ROOT,
            env=env,
            input="yes\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(ingested.returncode, 0, ingested.stdout + ingested.stderr)
        proposals_after_ingest = set((state / "memory" / "proposals").glob("prop-*.json"))
        command = [sys.executable, "-m", "ssios", "summary", "--state", name, "--config", f"{name}/client.toml"]
        failed = subprocess.run(command, cwd=ROOT, env=env, input="", capture_output=True, text=True, timeout=20, check=False)
        failed_text = failed.stdout + failed.stderr
        self.assertEqual(failed.returncode, 1, failed_text)
        self.assertIn("model_output_invalid", failed_text)
        self.assertIn("new-task", failed_text)
        self.assertNotIn("Traceback", failed_text)
        self.assertNotIn("SECRET THINKING", failed_text)
        self.assertEqual(set((state / "memory" / "proposals").glob("prop-*.json")), proposals_after_ingest)
        stored = json.loads((state / "tasks.json").read_text(encoding="utf-8"))["tasks"]
        self.assertEqual(len(stored), 1)
        failed_id = next(iter(stored))
        self.assertEqual(stored[failed_id]["status"], "failed")
        human = (state / "secrets" / "human.token").read_text(encoding="utf-8").strip()
        self.assertNotIn(human, failed_text)
        retried = subprocess.run(command, cwd=ROOT, env=env, input="yes\n", capture_output=True, text=True, timeout=30, check=False)
        retried_text = retried.stdout + retried.stderr
        self.assertEqual(retried.returncode, 0, retried_text)
        self.assertNotIn(human, retried_text)
        self.assertNotIn("SECRET THINKING", retried_text)
        payload = _json_line(retried.stdout)
        self.assertEqual(payload["result"], "committed")
        self.assertNotEqual(payload["task_id"], failed_id)
        again = json.loads((state / "tasks.json").read_text(encoding="utf-8"))["tasks"]
        self.assertEqual(again[failed_id]["status"], "failed")
        self.assertEqual(again[payload["task_id"]]["status"], "committed")
        self.assertIn("disposable local summary fixture", retried.stdout)


@unittest.skipUnless(os.environ.get("SSIOS_LETTA_UPSTREAM") == "1" and os.environ.get("SSIOS_LETTA_BIN"), "upstream letta binary not requested")
class LiveSummaryUpstreamTest(unittest.TestCase):
    def test_summary_commits_upstream_topic_without_autoapprove(self) -> None:
        previous = Path.cwd()
        os.chdir(ROOT)
        state = ROOT / UPSTREAM_NAME
        self.addCleanup(lambda: os.chdir(previous))
        self.addCleanup(lambda: shutil.rmtree(state, ignore_errors=True))
        shutil.rmtree(state, ignore_errors=True)
        port = _free_port()
        letta = os.environ["SSIOS_LETTA_BIN"]
        _write_config(
            state,
            port=port,
            replacements={
                "task_timeout_seconds = 30": "task_timeout_seconds = 90",
                'mode = "letta-memfs-compat"': 'mode = "letta-memfs-upstream"',
                'letta_bin = ""': f'letta_bin = "{letta}"',
            },
        )
        settings = load_settings(state / "client.toml", state_dir=Path(UPSTREAM_NAME))
        plane = ControlPlane(settings)
        plane.open()
        server = ControlServer(plane)
        server.start()
        self.addCleanup(server.stop)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PYTHONUNBUFFERED"] = "1"
        ingested = subprocess.run(
            [sys.executable, "-m", "ssios", "ingest", "examples/notes", "--project", "local-summary", "--state", UPSTREAM_NAME, "--config", f"{UPSTREAM_NAME}/client.toml"],
            cwd=ROOT,
            env=env,
            input="yes\n",
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        self.assertEqual(ingested.returncode, 0, ingested.stdout + ingested.stderr)
        completed = subprocess.run(
            [sys.executable, "-m", "ssios", "summary", "--state", UPSTREAM_NAME, "--config", f"{UPSTREAM_NAME}/client.toml"],
            cwd=ROOT,
            env=env,
            input="yes\n",
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        text = completed.stdout + completed.stderr
        human = (state / "secrets" / "human.token").read_text(encoding="utf-8").strip()
        self.assertNotIn(human, text)
        self.assertNotIn("Traceback", text)
        self.assertEqual(completed.returncode, 0, text)
        self.assertIn(CONFIRM, text)
        payload = _json_line(completed.stdout)
        self.assertEqual(payload["result"], "committed")
        self.assertEqual(payload["memory_trust"], "upstream-letta")
        self.assertFalse(payload["topic_core"])
        base = f"http://127.0.0.1:{plane.bound_port}"
        code, topic, _headers = request(
            base,
            "GET",
            "/v1/memory/file?project_id=local-summary&path=topics/fixtures/last-summary.md",
            token=plane.auth.read_secret("human"),
        )
        self.assertEqual(code, 200, topic)
        self.assertIn("HARBOR-LOCK", topic["content"])
        self.assertNotIn("learn more about the person", topic["content"].casefold())
        self.assertFalse(topic["core"])


@unittest.skipUnless(os.environ.get("SSIOS_OLLAMA_LIVE") == "1" and os.environ.get("SSIOS_OLLAMA_BASE"), "real ollama not requested")
class LiveNotesSummaryTest(unittest.TestCase):
    def test_sample_notes_are_summarized_by_real_models(self) -> None:
        models = [item for item in ("qwen2.5:0.5b", "qwen3:0.6b") if item]
        base_url = os.environ["SSIOS_OLLAMA_BASE"]
        for model in models:
            self._one_model(base_url, model)

    def _one_model(self, base_url: str, model: str) -> None:
        previous = Path.cwd()
        os.chdir(ROOT)
        name = ".ssios-notes-" + model.split(":")[0].replace(".", "")
        state = ROOT / name
        self.addCleanup(lambda: os.chdir(previous))
        self.addCleanup(lambda: shutil.rmtree(state, ignore_errors=True))
        shutil.rmtree(state, ignore_errors=True)
        port = _free_port()
        _write_config(
            state,
            port=port,
            replacements={
                'provider = "mock"': 'provider = "ollama"',
                'ollama_base_url = "http://127.0.0.1:11434"': f'ollama_base_url = "{base_url}"',
                'ollama_model = "llama3.1:8b"': f'ollama_model = "{model}"',
                "request_timeout_seconds = 60": "request_timeout_seconds = 90",
                "task_timeout_seconds = 30": "task_timeout_seconds = 120",
            },
        )
        settings = load_settings(state / "client.toml", state_dir=Path(name))
        plane = ControlPlane(settings)
        plane.open()
        server = ControlServer(plane)
        server.start()
        self.addCleanup(server.stop)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PYTHONUNBUFFERED"] = "1"
        common = ["--state", name, "--config", f"{name}/client.toml"]
        ingested = subprocess.run(
            [sys.executable, "-m", "ssios", "ingest", "examples/notes", "--project", "local-summary", *common],
            cwd=ROOT,
            env=env,
            input="yes\n",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(ingested.returncode, 0, ingested.stdout + ingested.stderr)
        completed = subprocess.run(
            [sys.executable, "-m", "ssios", "summary", "--project", "local-summary", "--topic", "local-summary", *common],
            cwd=ROOT,
            env=env,
            input="yes\n",
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        text = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, text)
        folded = text.casefold()
        self.assertNotIn("learn more about the person", folded)
        hits = sum(
            phrase in folded
            for phrase in ("northwind lantern", "laptop", "harbor-lock", "approval", "markdown", "git")
        )
        self.assertGreaterEqual(hits, 2, text)
