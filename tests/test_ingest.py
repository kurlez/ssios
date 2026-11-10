"""Ingest allowlist and empty-topic summary behavior."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from support import ROOT, PlaneTest, request, wait_task

from ssios.errors import SsiosError
from ssios.ingest import collect_notes, notes_to_memory
from ssios.llm import is_template_boilerplate, validate_summary_quality


class IngestCollectTest(unittest.TestCase):
    def test_sample_notes_are_accepted(self) -> None:
        notes = collect_notes(ROOT / "examples" / "notes")
        names = {path for path, _text in notes}
        self.assertIn("local-agent-security.md", names)
        self.assertTrue(any("HARBOR-LOCK" in text for _path, text in notes))
        stored = notes_to_memory("local-summary", notes, None)
        paths = {item["path"] for item in stored}
        self.assertIn("topics/local-summary/local-agent-security.md", paths)
        self.assertIn("topics/local-summary/MEMORY.md", paths)
        self.assertTrue(all(item["path"].endswith(".md") for item in stored))

    def test_symlink_is_rejected(self) -> None:
        folder = ROOT / ".ssios-ingest-link"
        folder.mkdir(exist_ok=True)
        self.addCleanup(lambda: _rm(folder))
        link = folder / "note.md"
        link.symlink_to(ROOT / "examples" / "notes" / "local-agent-security.md")
        with self.assertRaises(SsiosError) as caught:
            collect_notes(folder)
        self.assertEqual(caught.exception.code, "symlink_rejected")

    def test_secret_is_rejected(self) -> None:
        folder = ROOT / ".ssios-ingest-secret"
        folder.mkdir(exist_ok=True)
        self.addCleanup(lambda: _rm(folder))
        (folder / "leak.md").write_text("api_key = sk-thisisnotarealkey1234567890\n", encoding="utf-8")
        with self.assertRaises(SsiosError) as caught:
            collect_notes(folder)
        self.assertEqual(caught.exception.code, "secret_rejected")

    def test_template_echo_is_low_quality(self) -> None:
        boilerplate = "I need to learn more about the person I'm working with before I can help."
        self.assertTrue(is_template_boilerplate("human.md", boilerplate))
        self.assertFalse(
            is_template_boilerplate(
                "human.md",
                boilerplate + "\n\nHARBOR-LOCK keeps approvals on this laptop and refuses a cloud fallback. " * 3,
            )
        )
        with self.assertRaises(SsiosError) as caught:
            validate_summary_quality(boilerplate + " The profile is still empty and waiting.")
        self.assertEqual(caught.exception.code, "model_output_low_quality")


class EmptyTopicTest(PlaneTest):
    def test_missing_topic_creates_no_approval(self) -> None:
        code, created, _headers = self.call(
            "POST",
            "/v1/tasks",
            token=self.human,
            body={
                "project_id": "demo-alpha",
                "topic": "missing-notes",
                "kind": "project-summary",
                "input_revision": "exp-1",
                "input_path": "notes.md",
            },
        )
        self.assertEqual(code, 202, created)
        task = wait_task(self.base, self.human, created["id"], {"failed", "awaiting_approval"})
        self.assertEqual(task["status"], "failed", task)
        self.assertEqual(task["error_code"], "no_content")
        self.assertEqual(task["approval_ids"], [])


def _rm(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    for child in path.iterdir():
        _rm(child)
    os.rmdir(path)
