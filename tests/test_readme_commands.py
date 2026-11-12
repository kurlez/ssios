"""Every ssios command named in the README exists in the wrapper or the CLI."""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SCRIPT = ROOT / "scripts" / "ssios"

_FENCE = re.compile(r"```(?:bash|sh)\n(.*?)```", re.DOTALL)
_SSIOS = re.compile(r"(?:^\s*)(?:\./scripts/ssios|python3 -m ssios)\s+([a-z][a-z0-9-]*)", re.MULTILINE)
_MAKE = re.compile(r"(?:^\s*)make\s+([A-Za-z0-9_-]+)", re.MULTILINE)


def _fences(text: str) -> str:
    return "\n".join(_FENCE.findall(text))


class ReadmeCommandTest(unittest.TestCase):
    def test_documented_commands_exist(self) -> None:
        text = README.read_text(encoding="utf-8")
        fences = _fences(text)
        commands = set(_SSIOS.findall(fences))
        makes = set(_MAKE.findall(fences))
        self.assertIn("start", commands)
        self.assertIn("diagnose", commands)
        wrapper = subprocess.run([str(SCRIPT), "--help"], cwd=ROOT, text=True, capture_output=True, check=True)
        wrapper_names = set(re.findall(r"^  ([a-z][a-z0-9-]*)", wrapper.stdout, re.MULTILINE))
        env = dict(**{k: v for k, v in __import__("os").environ.items()})
        env["PYTHONPATH"] = str(ROOT / "src")
        cli = subprocess.run(
            [sys.executable, "-m", "ssios", "--help"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        brace = re.search(r"\{([a-z0-9,-]+)\}", cli.stdout)
        self.assertIsNotNone(brace)
        cli_names = set(brace.group(1).split(","))
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        missing = []
        for name in sorted(commands):
            if name not in wrapper_names and name not in cli_names:
                missing.append(name)
        for name in sorted(makes):
            if not re.search(rf"^{re.escape(name)}:", makefile, re.MULTILINE):
                missing.append("make " + name)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
