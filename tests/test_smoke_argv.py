"""Smoke scripts must use flags the real parser accepts. Nothing is executed."""

from __future__ import annotations

import contextlib
import io
import shlex
import unittest
from pathlib import Path

from ssios.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
_STOP = {">", ">>", "<", "|", "&", "&&", "||", ";"}


def script_commands() -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []
    for path in sorted((ROOT / "scripts").glob("*.sh")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "ssios" not in stripped:
                continue
            try:
                tokens = shlex.split(stripped, posix=True)
            except ValueError as exc:
                raise AssertionError(f"{path.name}:{lineno} is not a parseable shell line: {exc}") from exc
            if "ssios" not in tokens:
                continue
            argv: list[str] = []
            for token in tokens[tokens.index("ssios") + 1 :]:
                if token in _STOP or token.startswith(">") or (len(token) > 1 and token[0].isdigit() and ">" in token):
                    break
                expanded = token.replace("$STATE", "/tmp/ssios-smoke-state").replace("${STATE}", "/tmp/ssios-smoke-state")
                if "$" in expanded:
                    raise AssertionError(f"{path.name}:{lineno} has an unexpanded variable: {token}")
                argv.append(expanded)
            found.append((f"{path.name}:{lineno}", argv))
    return found


class SmokeArgvTest(unittest.TestCase):
    def test_every_smoke_command_parses(self) -> None:
        commands = script_commands()
        names = {item[0].split(":")[0] for item in commands}
        self.assertIn("macos-vz-codex-smoke.sh", names)
        self.assertIn("macos-vm-smoke.sh", names)
        parser = build_parser()
        for label, argv in commands:
            with self.subTest(label=label, argv=argv):
                parsed = parser.parse_args(argv)
                self.assertTrue(parsed.command)

    def test_vm_prepare_rejects_profile(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                build_parser().parse_args(["vm-prepare", "--profile", "dev", "--state", "/tmp/ssios-smoke-state"])
        self.assertEqual(caught.exception.code, 2)
