"""The scripts/ssios wrapper starts and stops only its own UI process."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ssios"


class WrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.env = os.environ.copy()
        self.env["SSIOS_STATE"] = str(self.state)
        self.env["SSIOS_CONFIG"] = str(ROOT / "config" / "ssios.example.toml")
        self.env.pop("SSIOS_PYTHON", None)
        self.addCleanup(self._stop)
        self.addCleanup(self.tmp.cleanup)

    def _run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), *args],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            check=check,
            timeout=40,
        )

    def _stop(self) -> None:
        subprocess.run([str(SCRIPT), "stop"], cwd=ROOT, env=self.env, text=True, capture_output=True, timeout=20)

    def test_script_does_not_name_disk_tools(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("hdiutil", text)
        self.assertNotIn("diskutil", text)
        self.assertIn(".venv/", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_refuses_legacy_state_directories(self) -> None:
        for name in (".ssios", ".ssios-demo", ".ssios-memfs"):
            env = self.env.copy()
            env["SSIOS_STATE"] = str(Path(self.tmp.name) / name)
            result = subprocess.run(
                [str(SCRIPT), "status"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertNotEqual(result.returncode, 0, name)
            self.assertIn("Refusing", result.stderr)

    def test_config_copy_does_not_overwrite(self) -> None:
        config = self.state / "ollama.toml"
        self.env["SSIOS_CONFIG"] = str(config)
        first = self._run("_ensure-config")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("Wrote", first.stdout)
        self.assertIn("Do not put an OAuth client secret", first.stdout)
        config.write_text("keep-this\n", encoding="utf-8")
        second = self._run("_ensure-config")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Left existing config", second.stdout)
        self.assertEqual(config.read_text(encoding="utf-8"), "keep-this\n")
        config.write_text("", encoding="utf-8")
        third = self._run("_ensure-config")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertIn("Wrote", third.stdout)
        self.assertIn("backend", config.read_text(encoding="utf-8"))

    def test_stop_does_not_kill_an_unrelated_pid(self) -> None:
        sleep = subprocess.Popen(["sleep", "120"])

        def _stop_sleep() -> None:
            if sleep.poll() is None:
                sleep.kill()
                sleep.wait(timeout=5)

        self.addCleanup(_stop_sleep)
        run = self.state / "run"
        run.mkdir()
        (run / "ui.pid").write_text(str(sleep.pid), encoding="utf-8")
        result = self._run("stop")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not the SSIOS UI", result.stderr)
        self.assertIsNone(sleep.poll())

    def test_start_status_stop_and_passthrough(self) -> None:
        started = self._run("start", "--port", "0")
        self.assertEqual(started.returncode, 0, started.stderr + started.stdout)
        self.assertIn("http://127.0.0.1:", started.stdout)
        self.assertNotIn("client_secret", started.stdout)
        self.assertNotIn("refresh_token", started.stdout)
        status = self._run("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("running pid", status.stdout)
        diagnose = self._run("vm-diagnose")
        self.assertIn("backend", diagnose.stdout)
        self.assertNotIn("client_secret", diagnose.stdout)
        stopped = self._run("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn("stopped", stopped.stdout)
        again = self._run("status")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("stopped", again.stdout)


if __name__ == "__main__":
    unittest.main()
