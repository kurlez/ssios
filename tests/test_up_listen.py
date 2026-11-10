"""ssios up must show its URL even when stdout is a file, not a terminal."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from support import ROOT


class UpListenTest(unittest.TestCase):
    def test_redirected_up_prints_the_url_and_writes_the_port_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            log_path = Path(tmp) / "up.log"
            env = os.environ.copy()
            env.pop("PYTHONUNBUFFERED", None)
            env["PYTHONPATH"] = str(ROOT / "src")
            log = log_path.open("w", encoding="utf-8")
            self.addCleanup(log.close)
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "ssios",
                    "up",
                    "--profile",
                    "dev",
                    "--port",
                    "0",
                    "--state",
                    str(state),
                    "--config",
                    str(ROOT / "config" / "ssios.example.toml"),
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

            def stop() -> None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()

            self.addCleanup(stop)
            deadline = time.time() + 10
            text = ""
            while time.time() < deadline:
                log.flush()
                text = log_path.read_text(encoding="utf-8", errors="replace")
                if "http://127.0.0.1:" in text and (state / "listen.url").is_file():
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertIn("http://127.0.0.1:", text, text)
            url = (state / "listen.url").read_text(encoding="utf-8").strip()
            self.assertTrue(url.startswith("http://127.0.0.1:"), url)
            self.assertIn(url, text)
            with urllib.request.urlopen(url + "/ui/login", timeout=5) as response:
                self.assertEqual(response.status, 200)
            notes = state / "storage" / "exports" / "demo-alpha" / "exp-1" / "notes.md"
            self.assertTrue(notes.is_file(), "fresh state is missing the smoke task input")
            self.assertIn("NORTHWIND-FIXTURE-NOTE", notes.read_text(encoding="utf-8"))
