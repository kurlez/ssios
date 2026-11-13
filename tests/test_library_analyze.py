"""On-demand analysis stores a note and does not write core memory until approval."""

from __future__ import annotations

from ssios.errors import SsiosError
from ssios.library import ensure_personal, get_item, import_bytes
from ssios.library_analyze import LocalLibraryModel, ScriptedModel, analyze_item, stage_core_memory
from ssios.scope import AccountScope
from support import PlaneTest
from test_library import _png


class AnalyzeTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.library_id = ensure_personal(self.plane, self.account["id"])["id"]

    def test_a_scripted_model_saves_a_note_and_core_memory_stays_pending(self) -> None:
        saved = import_bytes(
            self.plane, self.account["id"], self.library_id, _png(9), name="tile.png", source="picker", peer_ip="127.0.0.1"
        )
        phrase = "Harbor tile 4419"
        analyzed = analyze_item(
            self.plane,
            self.account["id"],
            self.library_id,
            saved["id"],
            "127.0.0.1",
            model=ScriptedModel(phrase, ["harbor"], "HARBOR"),
        )
        self.assertEqual(analyzed["description"], phrase)
        self.assertIn("harbor", analyzed["tags"])
        self.assertEqual(analyzed["ocr"], "HARBOR")
        self.assertEqual(analyzed["analysis_model"], "scripted")
        scope = AccountScope(self.plane, self.account)
        note = scope.memory().read_file(scope.project_id(), f"topics/library/{saved['id']}.md", include_untrusted=False)
        self.assertIn("Added by: ada", note["content"])
        self.assertIn(phrase, note["content"])
        self.assertIn(saved["id"], note["content"])
        human = scope.memory().read_file(scope.project_id(), "human.md", include_untrusted=False)
        self.assertNotIn(phrase, human["content"])
        pending = stage_core_memory(self.plane, self.account["id"], self.library_id, saved["id"], "127.0.0.1")
        self.assertEqual(pending["status"], "pending")
        human = scope.memory().read_file(scope.project_id(), "human.md", include_untrusted=False)
        self.assertNotIn(phrase, human["content"])
        self.assertTrue(any(item.get("status") == "pending" and item.get("action") == "memory.commit" for item in scope.list_pending()))
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("library.analyze", audit)
        self.assertIn("library.memory", audit)
        self.assertNotIn(phrase, audit)
        self.assertNotIn("HARBOR", audit)
        with self.assertRaises(SsiosError) as remote:
            LocalLibraryModel("https://example.com")
        self.assertEqual(remote.exception.code, "library_model")
        with self.assertRaises(SsiosError) as down:
            LocalLibraryModel("http://127.0.0.1:9", timeout=1).describe(group="document", name="tile.png", text="tile", image=b"")
        self.assertEqual(down.exception.code, "library_model")
        stored = get_item(self.plane, self.account["id"], self.library_id, saved["id"])
        self.assertEqual(stored["description"], phrase)
