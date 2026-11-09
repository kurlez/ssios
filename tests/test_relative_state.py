"""State paths must survive the git subprocess cwd, including relative demo invocations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from support import ROOT

from ssios.config import load_settings
from ssios.errors import SsiosError
from ssios.gitio import GitRunner
from ssios.service import ControlPlane

RELATIVE_NAMES = (".ssios-rel-unit", ".ssios-rel-demo", ".ssios-rel-expire", ".ssios-missing-repo", ".ssios-interrupted")


def _clean() -> None:
    for name in RELATIVE_NAMES:
        shutil.rmtree(ROOT / name, ignore_errors=True)
    shutil.rmtree(ROOT / "ssios path fixture", ignore_errors=True)


class RelativePathTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = Path.cwd()
        os.chdir(ROOT)
        _clean()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        os.chdir(self._previous)
        _clean()

    def test_git_rev_parse_after_cwd_change(self) -> None:
        runner = GitRunner(Path(".ssios-rel-unit") / "git-hooks", Path(".ssios-rel-unit") / "empty-gitconfig")
        repo = Path(".ssios-rel-unit") / "repo"
        runner.init_repo(repo)
        (ROOT / ".ssios-rel-unit" / "repo" / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        runner.commit_paths(repo, ["MEMORY.md"], "seed")
        os.chdir(Path("/tmp"))
        head = runner.head(repo)
        self.assertEqual(len(head), 40)
        self.assertTrue((ROOT / ".ssios-rel-unit" / "repo" / ".git").is_dir())

    def test_settings_keep_relative_state_after_process_cwd_changes(self) -> None:
        settings = load_settings(ROOT / "config" / "ssios.example.toml", state_dir=Path(".ssios-rel-unit"), port=0)
        self.assertTrue(settings.state_dir.is_absolute())
        os.chdir(Path("/tmp"))
        plane = ControlPlane(settings)
        plane.open()
        repo = settings.state_dir / "memory" / "demo-alpha" / "repo"
        self.assertEqual(len(plane.git.head(repo)), 40)

    def test_space_in_parent_and_relative_state(self) -> None:
        work = ROOT / "ssios path fixture"
        work.mkdir()
        os.chdir(work)
        settings = load_settings(ROOT / "config" / "ssios.example.toml", state_dir=Path(".ssios-demo"), port=0)
        os.chdir(ROOT)
        plane = ControlPlane(settings)
        plane.open()
        self.assertIn("ssios path fixture", str(settings.state_dir))
        self.assertTrue((work / ".ssios-demo" / "memory" / "demo-alpha" / "repo" / ".git").is_dir())
        os.chdir(Path("/tmp"))
        self.assertEqual(len(plane.memory.head("demo-alpha")), 40)

    def test_missing_canonical_repo_does_not_reset_approvals(self) -> None:
        state = ROOT / ".ssios-missing-repo"
        state.mkdir()
        approval_id = "appr-0123456789abcdef"
        payload = {
            "approvals": {
                approval_id: {
                    "approval_id": approval_id,
                    "action": "memory.commit",
                    "project_id": "demo-alpha",
                    "task_id": "task-0123456789abcdef",
                    "proposal_id": "prop-0123456789abcdef",
                    "payload_sha256": "a" * 64,
                    "expected_revision": "b" * 40,
                    "bound_hash": "c" * 64,
                    "nonce": "kept-nonce",
                    "created_at": 1,
                    "expires_at": 1,
                    "status": "expired",
                    "decision": None,
                }
            }
        }
        path = state / "approvals.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        before = path.read_bytes()
        settings = load_settings(ROOT / "config" / "ssios.example.toml", state_dir=Path(".ssios-missing-repo"), port=0)
        plane = ControlPlane(settings)
        with self.assertRaises(SsiosError) as caught:
            plane.open()
        self.assertEqual(caught.exception.code, "memory_repo_missing")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((state / "memory" / "demo-alpha" / "repo" / ".git").exists())

    def test_interrupted_setup_keeps_existing_memory_file(self) -> None:
        repo = ROOT / ".ssios-interrupted" / "memory" / "demo-alpha" / "repo"
        repo.mkdir(parents=True)
        original = "# Memory\n\nkept\n"
        (repo / "MEMORY.md").write_text(original, encoding="utf-8")
        settings = load_settings(ROOT / "config" / "ssios.example.toml", state_dir=Path(".ssios-interrupted"), port=0)
        plane = ControlPlane(settings)
        plane.open()
        self.assertEqual((repo / "MEMORY.md").read_text(encoding="utf-8"), original)
        self.assertTrue((repo / ".git").is_dir())
        self.assertEqual(len(plane.memory.head("demo-alpha")), 40)


class RelativeCliDemoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = Path.cwd()
        os.chdir(ROOT)
        for name in (".ssios-rel-demo", ".ssios-rel-expire"):
            shutil.rmtree(ROOT / name, ignore_errors=True)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        os.chdir(self._previous)
        for name in (".ssios-rel-demo", ".ssios-rel-expire"):
            shutil.rmtree(ROOT / name, ignore_errors=True)

    def _demo(self, state: str, cwd: Path, config: str = "config/ssios.example.toml") -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run(
            [sys.executable, "-m", "ssios", "demo", "--state", state, "--config", config],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    def test_documented_relative_demo_twice_from_repo_root(self) -> None:
        first = self._demo(".ssios-rel-demo", ROOT)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        second = self._demo(".ssios-rel-demo", ROOT)
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.assertNotIn("git_failed", second.stdout + second.stderr)
        self.assertTrue((ROOT / ".ssios-rel-demo" / "memory" / "demo-alpha" / "repo" / ".git").is_dir())

    def test_relative_expired_state_recovers_across_process_restart(self) -> None:
        created = self._demo(".ssios-rel-expire", ROOT)
        self.assertEqual(created.returncode, 0, created.stderr + created.stdout)
        path = ROOT / ".ssios-rel-expire" / "approvals.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        nonces = {key: value["nonce"] for key, value in stored["approvals"].items()}
        for record in stored["approvals"].values():
            record["expires_at"] = 1
            record["status"] = "pending"
        path.write_text(json.dumps(stored), encoding="utf-8")
        tasks = json.loads((ROOT / ".ssios-rel-expire" / "tasks.json").read_text(encoding="utf-8"))
        for task in tasks["tasks"].values():
            if task.get("kind") == "project-summary":
                task["status"] = "awaiting_approval"
        (ROOT / ".ssios-rel-expire" / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")
        nested = ROOT / "nested-cwd"
        nested.mkdir(exist_ok=True)
        try:
            recovered = self._demo(str(Path("..") / ".ssios-rel-expire"), nested, config="../config/ssios.example.toml")
        finally:
            shutil.rmtree(nested, ignore_errors=True)
        self.assertEqual(recovered.returncode, 0, recovered.stderr + recovered.stdout)
        self.assertNotIn("cannot change to", recovered.stdout + recovered.stderr)
        again = json.loads(path.read_text(encoding="utf-8"))
        for approval_id, nonce in nonces.items():
            self.assertEqual(again["approvals"][approval_id]["nonce"], nonce)
            self.assertEqual(again["approvals"][approval_id]["status"], "expired")
            self.assertEqual(again["approvals"][approval_id]["expires_at"], 1)
