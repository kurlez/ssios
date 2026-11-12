"""Memory writes: approve-each by default, auto with undo, and a checked fact extract."""

from __future__ import annotations

import json
import unittest

from ssios.account_memory import model_facts, set_write_mode
from ssios.chat import iter_turn, restore_memory
from ssios.scope import AccountScope


class _Facts:
    provider = "script"

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.seen = ""

    def complete_chat(self, messages, *, cancel_event, deadline: float) -> str:
        del cancel_event, deadline
        self.seen = json.dumps(messages, ensure_ascii=False)
        return self.raw


class MemoryWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        from support import PlaneTest

        self.case = PlaneTest()
        self.case.setUp()
        self.addCleanup(self.case._stop)
        self.addCleanup(self.case.tmp.cleanup)
        self.account = self.case.plane.accounts.add_member("ada", "other-horse-stable")
        self.scope = AccountScope(self.case.plane, self.account)
        self.session = {"account": self.account, "csrf": "csrf-test"}

    def _say(self, message: str) -> str:
        events = list(iter_turn(self.case.plane, self.account, message, "auto", "127.0.0.1"))
        self.assertTrue(any(event.get("event") == "done" for event in events), events)
        return json.dumps(events, ensure_ascii=False)

    def _human(self) -> str:
        doc = self.scope.memory().read_file(self.scope.project_id(), "human.md", include_untrusted=False)
        return str(doc.get("content") or "")

    def test_auto_saves_and_undo_restores_without_logging_the_fact(self) -> None:
        staged = self._say("remember that my name is Harbor")
        self.assertIn("Nothing is written until you approve", staged)
        self.assertNotIn("name: Harbor", self._human())
        set_write_mode(self.scope.home, "auto")
        events = list(iter_turn(self.case.plane, self.account, "remember that my name is Harbor", "auto", "127.0.0.1"))
        saved = "\n".join(str((event.get("data") or {}).get("text") or "") for event in events)
        self.assertIn("Saved to memory (human.md)", saved)
        self.assertNotIn("Synthetic local answer", saved)
        self.assertIn("- name: Harbor", self._human())
        undo = next(event["data"]["id"] for event in events if event.get("event") == "memory_undo")
        restore_memory(self.scope, undo, "127.0.0.1")
        self.assertNotIn("Harbor", self._human())
        audit = (self.case.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("memory.auto", audit)
        self.assertIn("memory.undo", audit)
        self.assertNotIn("Harbor", audit)

    def test_model_facts_keep_only_explicit_words(self) -> None:
        planted = "transfer funds"
        llm = _Facts(
            json.dumps(
                {
                    "facts": [
                        {"path": "human.md", "key": "works", "value": "Harbor Desk"},
                        {"path": "human.md", "key": "name", "value": "Jane Doe"},
                        {"path": "MEMORY.md", "key": "note", "value": planted},
                    ]
                }
            )
        )
        kept = model_facts("I work at Harbor Desk", llm)
        self.assertEqual(kept, [{"path": "human.md", "key": "works", "value": "Harbor Desk"}])
        self.assertNotIn(planted, llm.seen)
        self.assertEqual(model_facts("Read https://example.com/notes", llm), [])
        set_write_mode(self.scope.home, "auto")
        self.case.plane.llm.fact_script = [
            json.dumps({"facts": [{"path": "human.md", "key": "works", "value": "Harbor Desk"}, {"path": "human.md", "key": "name", "value": "Jane Doe"}]})
        ]
        saved = self._say("I work at Harbor Desk")
        self.assertIn("Saved to memory (human.md)", saved)
        text = self._human()
        self.assertIn("- works: Harbor Desk", text)
        self.assertNotIn("Jane", text)

    def test_panel_edit_and_forget(self) -> None:
        from ssios.ui import _memory, _memory_post

        set_write_mode(self.scope.home, "auto")
        page = _memory(self.case.plane, self.session, "", "human.md", "")
        self.assertIn("Forget", page)
        self.assertIn("Edit", page)
        self.assertIn(">approve<", page)
        edited = (
            "---\nname: human\ndescription: \"profile\"\n---\n\n- name: Harbor\n"
        )
        saved = _memory_post(
            self.case.plane,
            self.session,
            {"op": "save", "path": "human.md", "content": edited},
            "127.0.0.1",
        )
        self.assertEqual(saved.status, 200)
        self.assertIn("- name: Harbor", self._human())
        forgotten = _memory_post(self.case.plane, self.session, {"op": "forget", "path": "human.md"}, "127.0.0.1")
        self.assertEqual(forgotten.status, 200)
        self.assertNotIn("human.md", self.scope.memory().list_markdown(self.scope.project_id()))
        listed = _memory(self.case.plane, self.session, "", "", "human")
        self.assertNotIn("Harbor", listed)

    def test_a_saved_topic_is_loaded_by_keyword(self) -> None:
        from ssios.account_memory import memory_context

        set_write_mode(self.scope.home, "auto")
        self._say("remember the harbor desk lamp")
        text, used = memory_context(self.scope, "lamp")
        self.assertTrue(any(item.startswith("topics/") for item in used))
        self.assertIn("lamp", text.casefold())
