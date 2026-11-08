"""Test helpers. Inserts the source tree on sys.path."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from ssios.client import request  # noqa: E402
from ssios.config import load_settings  # noqa: E402
from ssios.httpapi import ControlServer  # noqa: E402
from ssios.service import ControlPlane  # noqa: E402


def wait_task(base: str, token: str, task_id: str, terminal: set[str], timeout: float = 8) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        status, last, _headers = request(base, "GET", f"/v1/tasks/{task_id}", token=token)
        if status == 200 and last.get("status") in terminal:
            return last
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {terminal}: {last}")


class PlaneTest(unittest.TestCase):
    settings_overrides: dict = {}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        loaded = load_settings(ROOT / "config" / "ssios.example.toml", state_dir=self.state, port=0)
        self.settings = replace(loaded, **self.settings_overrides)
        self.plane = ControlPlane(self.settings)
        self.plane.open()
        self.server = ControlServer(self.plane)
        self.server.start()
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.plane.bound_port}"
        self.human = self.plane.auth.read_secret("human")
        self.worker = self.plane.auth.read_secret("worker")

    def _stop(self) -> None:
        self.server.stop()
        for thread in list(self.plane._threads.values()):
            thread.join(timeout=3)

    def call(self, method: str, path: str, *, token: str | None = None, body: dict | None = None, headers: dict | None = None):
        return request(self.base, method, path, token=token, body=body, headers=headers)
