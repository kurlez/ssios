"""Disposable Letta export copies can be listed and removed without touching git history."""

from __future__ import annotations

import stat
import unittest
from pathlib import Path

from ssios.gc import collect, leftover_exports


class GcTest(unittest.TestCase):
    def test_dry_run_lists_snapshots_and_apply_keeps_memory(self) -> None:
        root = Path(self.id().replace(".", "_"))
        state = Path("/tmp") / root.name
        export = state / "upstream-letta" / "exports" / "agent-local-01234567-89ab-cdef-0123-456789abcdef" / "ab12"
        memory = state / "upstream-letta" / "backend" / "memfs" / "agent-local-01234567-89ab-cdef-0123-456789abcdef" / "memory"
        note = export / "topics" / "fixtures" / "last-summary.md"
        kept = memory / "topics" / "fixtures" / "last-summary.md"
        note.parent.mkdir(parents=True)
        kept.parent.mkdir(parents=True)
        note.write_text("copy\n", encoding="utf-8")
        kept.write_text("history\n", encoding="utf-8")
        self.addCleanup(lambda: __import__("shutil").rmtree(state, ignore_errors=True))
        preview = collect(state, apply=False)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["count"], 1)
        self.assertTrue(note.exists())
        self.assertEqual(kept.read_text(encoding="utf-8"), "history\n")
        applied = collect(state, apply=True)
        self.assertEqual(applied["removed"], 1)
        self.assertFalse(note.exists())
        self.assertEqual(kept.read_text(encoding="utf-8"), "history\n")
        self.assertEqual(leftover_exports(state), [])
        self.assertEqual(stat.S_IMODE(kept.stat().st_mode) & 0o777, kept.stat().st_mode & 0o777)
