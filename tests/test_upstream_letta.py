"""Letta Code local MemFS. The live test is opt-in and uses the pinned CLI.

The fixture binary test checks command shape only. It is not a live integration.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from support import ROOT, load_settings

from ssios.errors import SsiosError
from ssios.httpapi import ControlServer
from ssios.letta_upstream import PINNED_VERSION, validate_letta_argv
from ssios.service import ControlPlane


FIXTURE = """#!/usr/bin/env python3
import sys
if sys.argv[1:] == ["--version"]:
    print("0.34.45 (Letta Code)")
    raise SystemExit(0)
print("refused", file=sys.stderr)
raise SystemExit(9)
"""


class ArgvPolicyTest(unittest.TestCase):
    def test_prompt_and_yolo_are_rejected(self) -> None:
        for argv in (["letta", "-p", "hi"], ["letta", "--yolo"], ["letta", "--permission-mode", "unrestricted"]):
            with self.assertRaises(SsiosError) as caught:
                validate_letta_argv(argv)
            self.assertEqual(caught.exception.code, "upstream_rejected")

    def test_documented_subcommands_are_allowed(self) -> None:
        validate_letta_argv(["/usr/bin/letta", "--backend", "local", "memory", "tokens", "--agent", "agent-local-1", "--format", "json"])


class NoFallbackTest(unittest.TestCase):
    def test_missing_cli_does_not_seed_compat_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            settings = replace(
                load_settings(ROOT / "config" / "ssios.example.toml", state_dir=state, port=0),
                memory_mode="letta-memfs-upstream",
                letta_bin=str(Path(tmp) / "missing-letta"),
            )
            plane = ControlPlane(settings)
            with self.assertRaises(SsiosError) as caught:
                plane.open()
            self.assertEqual(caught.exception.code, "upstream_unavailable")
            self.assertFalse((state / "memory" / "demo-alpha" / "repo").exists())


class LiveUpstreamTest(unittest.TestCase):
    def test_real_letta_local_memfs_roundtrip(self) -> None:
        if os.environ.get("SSIOS_LETTA_UPSTREAM") != "1":
            self.skipTest("set SSIOS_LETTA_UPSTREAM=1 and SSIOS_LETTA_BIN to run the pinned Letta CLI")
        binary = os.environ.get("SSIOS_LETTA_BIN", "")
        self.assertTrue(binary and Path(binary).is_file(), "SSIOS_LETTA_BIN must point at the pinned letta executable")
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            settings = replace(
                load_settings(ROOT / "config" / "ssios.example.toml", state_dir=state, port=0),
                memory_mode="letta-memfs-upstream",
                letta_bin=binary,
            )
            plane = ControlPlane(settings)
            plane.open()
            server = ControlServer(plane)
            server.start()
            self.addCleanup(server.stop)
            from ssios.client import request

            base = f"http://127.0.0.1:{plane.bound_port}"
            human = plane.auth.read_secret("human")
            worker = plane.auth.read_secret("worker")
            code, core, _headers = request(base, "GET", "/v1/memory/core?project_id=demo-alpha", token=human)
            self.assertEqual(code, 200, core)
            self.assertEqual(core["mode"], "letta-memfs-upstream")
            self.assertEqual(core["source"], "letta-memory-tokens+export")
            self.assertEqual({item["path"] for item in core["files"]}, {"MEMORY.md", "persona.md", "human.md"})
            self.assertNotIn("CORE-PROJECT-FACT", json.dumps(core))
            self.assertIn(PINNED_VERSION, json.dumps(plane.upstream.preflight()))
            proposal_body = {
                "project_id": "demo-alpha",
                "expected_revision": core["revision"],
                "files": [
                    {
                        "path": "topics/fixtures/overview.md",
                        "content": "---\nname: overview\ndescription: \"synthetic upstream topic\"\n---\n\nUPSTREAM-TOPIC-MARKER\n",
                    },
                    {"path": "topics/fixtures/MEMORY.md", "content": "# Fixtures\n\n- overview.md\n"},
                ],
            }
            code, proposal, _headers = request(base, "POST", "/v1/memory/propose", token=human, body=proposal_body)
            self.assertEqual(code, 200, proposal)
            approval = proposal["approval"]
            hidden, hidden_body, _headers = request(
                base,
                "GET",
                "/v1/memory/file?project_id=demo-alpha&path=topics/fixtures/overview.md",
                token=human,
            )
            self.assertEqual(hidden, 404)
            self.assertNotIn("UPSTREAM-TOPIC-MARKER", json.dumps(hidden_body))
            denied, denied_body, _headers = request(
                base,
                "POST",
                f"/v1/approvals/{approval['id']}/decide",
                token=worker,
                body={"decision": "approve"},
            )
            self.assertEqual(denied, 403)
            self.assertEqual(denied_body["error"], "worker_cannot_approve")
            approved, result, _headers = request(
                base,
                "POST",
                f"/v1/approvals/{approval['id']}/decide",
                token=human,
                body={
                    "decision": "approve",
                    "action": approval["action"],
                    "payload_sha256": approval["payload_sha256"],
                    "expected_revision": approval["expected_revision"],
                    "bound_hash": approval["bound_hash"],
                },
            )
            self.assertEqual(approved, 200, result)
            code, topic, _headers = request(
                base,
                "GET",
                "/v1/memory/file?project_id=demo-alpha&path=topics/fixtures/overview.md",
                token=human,
            )
            self.assertEqual(code, 200, topic)
            self.assertFalse(topic["core"])
            self.assertIn("UPSTREAM-TOPIC-MARKER", topic["content"])
            self.assertEqual(topic["source"], "letta-memory-export")
            code, core_after, _headers = request(base, "GET", "/v1/memory/core?project_id=demo-alpha", token=human)
            self.assertEqual({item["path"] for item in core_after["files"]}, {"MEMORY.md", "persona.md", "human.md"})
            self.assertIn("topics/fixtures/MEMORY.md", core_after["topics"])
            self.assertNotIn("UPSTREAM-TOPIC-MARKER", json.dumps(core_after))
            self.assertNotEqual(core_after["revision"], core["revision"])
            audit = (state / "audit" / "audit.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("UPSTREAM-TOPIC-MARKER", audit)
            self.assertNotIn(human, audit)
            self.assertFalse((state / "memory" / "demo-alpha" / "repo").exists())
            mode = stat.S_IMODE((state / "secrets" / "human.token").stat().st_mode)
            self.assertEqual(mode & 0o077, 0)


class FixtureCliTest(unittest.TestCase):
    def test_version_stub_is_not_the_upstream_package(self) -> None:
        self.assertNotIn("@letta-ai/letta-code", FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "letta"
            binary.write_text(FIXTURE, encoding="utf-8")
            binary.chmod(0o755)
            import subprocess

            completed = subprocess.run([str(binary), "--version"], check=False, capture_output=True, text=True)
            self.assertIn("0.34.45", completed.stdout)
            refused = subprocess.run([str(binary), "--yolo"], check=False, capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)


if __name__ == "__main__":
    unittest.main()
