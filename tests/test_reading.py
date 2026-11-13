"""Notes follow the vault's folders, and only what the account has read is used as memory."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ssios.account_memory import memory_context
from ssios.errors import SsiosError
from ssios.reading import depth_of, mark, mark_note, note_depth, remind_to_read
from ssios.reminders import ReminderStore
from ssios.scope import AccountScope
from ssios.vault import account_vault, list_notes, read_asset, read_note, resolve_link, set_vault
from support import PlaneTest
from test_library import _png
from test_ui import _form, _get


class VaultTreeTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.plane.accounts.add_admin("owner", "correct-horse-battery")
        self.account = next(item for item in self.plane.accounts.public_list() if item["username"] == "owner")
        self.scope = AccountScope(self.plane, self.account)
        self.root = account_vault(self.plane.settings, self.account["id"])
        (self.root / "Clippings" / "assets").mkdir(parents=True)
        (self.root / "Projects" / "Harbor").mkdir(parents=True)
        (self.root / ".obsidian").mkdir()
        (self.root / ".trash").mkdir()
        (self.root / "Clippings" / "pier-story.md").write_text('---\ntitle: "Pier story"\nsource: "https://example.com/pier"\ntags: [clipping, harbor]\n---\n\nThe cedar pier reopened in spring.\n', encoding="utf-8")
        (self.root / "Projects" / "Harbor" / "Plan.md").write_text("# Plan\n\nSee [[Budget]] and [[pier-story|the story]] and ![[dock.png]].\n\n#harbor #planning\n", encoding="utf-8")
        (self.root / "Projects" / "Harbor" / "Budget.md").write_text("Lantern budget is 4419 dollars.\n", encoding="utf-8")
        (self.root / "Projects" / "Harbor" / "dock.png").write_bytes(_png(3))
        (self.root / ".obsidian" / "app.md").write_text("config", encoding="utf-8")
        (self.root / ".trash" / "old.md").write_text("deleted note about cedar", encoding="utf-8")
        (self.root / "link.md").symlink_to(self.root / "Projects" / "Harbor" / "Budget.md")

    def test_notes_follow_the_vault_folders_links_and_tags(self) -> None:
        notes = {item["path"]: item for item in list_notes(self.plane.settings, self.account["id"])}
        self.assertEqual(set(notes), {"Clippings/pier-story.md", "Projects/Harbor/Plan.md", "Projects/Harbor/Budget.md"})
        self.assertEqual(notes["Projects/Harbor/Plan.md"]["folder"], "Projects/Harbor")
        self.assertEqual(notes["Projects/Harbor/Plan.md"]["tags"], ["harbor", "planning"])
        self.assertEqual(notes["Clippings/pier-story.md"]["tags"], ["clipping", "harbor"])
        settings, account = self.plane.settings, self.account["id"]
        self.assertEqual(resolve_link(settings, account, "Projects/Harbor/Plan.md", "Budget"), "Projects/Harbor/Budget.md")
        self.assertEqual(resolve_link(settings, account, "Projects/Harbor/Plan.md", "pier-story|the story"), "Clippings/pier-story.md")
        self.assertEqual(resolve_link(settings, account, "Clippings/pier-story.md", "dock.png"), "Projects/Harbor/dock.png")
        self.assertEqual(resolve_link(settings, account, "Projects/Harbor/Plan.md", "../../../etc/passwd"), "")
        self.assertEqual(read_asset(settings, account, "Projects/Harbor/dock.png")[1], "image/png")
        for bad in (".obsidian/app.md", ".trash/old.md", "../x.md", "Projects/../.obsidian/app.md", "link.md", "Projects/Harbor/dock.png"):
            with self.assertRaises(SsiosError):
                read_note(settings, account, bad)
        status, _page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": "correct-horse-battery"})
        cookie = cookie.split(";", 1)[0]
        status, page = _get(self.base, "/ui/vault", cookie)
        self.assertIn("Projects/Harbor", page)
        self.assertIn("Clippings", page)
        self.assertIn("#harbor", page)
        self.assertNotIn(".obsidian", page)
        self.assertNotIn("old.md", page)
        status, tagged = _get(self.base, "/ui/vault?tag=planning", cookie)
        self.assertIn("Plan", tagged)
        self.assertNotIn("Pier story", tagged)
        status, note = _get(self.base, "/ui/vault/note?path=Projects%2FHarbor%2FPlan.md", cookie)
        self.assertEqual(status, 200, note)
        self.assertIn('href="/ui/vault/note?path=Projects%2FHarbor%2FBudget.md"', note)
        self.assertIn('href="/ui/vault/note?path=Clippings%2Fpier-story.md"', note)
        self.assertIn('src="/ui/vault/asset?path=Projects%2FHarbor%2Fdock.png"', note)
        self.assertIn("Skimmed", note)
        csrf = note.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        status, reminded, _cookie = _form(self.base, "/ui/vault/note", {"csrf": csrf, "path": "Projects/Harbor/Plan.md", "op": "remind", "when": "tomorrow"}, cookie=cookie)
        self.assertIn("Reminder set for", reminded)
        self.assertEqual([item["text"] for item in ReminderStore(self.scope.home).open_items()], ["Read carefully: Plan"])
        status, marked, _cookie = _form(self.base, "/ui/vault/note", {"csrf": csrf, "path": "Projects/Harbor/Plan.md", "op": "read"}, cookie=cookie)
        self.assertIn("Marked as read.", marked)
        self.assertEqual(note_depth(self.scope.home, "Projects/Harbor/Plan.md"), "read")
        self.assertEqual(ReminderStore(self.scope.home).open_items(), [])

    def test_a_note_is_used_in_chat_with_its_depth_and_hidden_folders_are_not(self) -> None:
        text, used = memory_context(self.scope, "what is the lantern budget")
        self.assertIn("note:Projects/Harbor/Budget.md", used)
        self.assertIn("4419", text)
        self.assertIn("skimmed, not read with care", text)
        self.assertIn("not confirmed memory", text)
        mark_note(self.plane, self.scope, "Projects/Harbor/Budget.md", "read", "marked")
        text, _used = memory_context(self.scope, "what is the lantern budget")
        self.assertIn("read with care by the user", text)
        text, used = memory_context(self.scope, "cedar")
        self.assertIn("note:Clippings/pier-story.md", used)
        self.assertNotIn("deleted note", text)
        self.assertEqual(memory_context(self.scope, "zebra crossing")[1].count("note:"), 0)

    def test_the_ledger_only_goes_deeper_and_holds_no_text(self) -> None:
        home = self.scope.home
        now = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(depth_of(home, "file:lib-1:item-1"), "")
        self.assertTrue(mark(home, "file:lib-1:item-1", "skimmed", "opened", now))
        self.assertTrue(mark(home, "file:lib-1:item-1", "read", "star", now))
        self.assertFalse(mark(home, "file:lib-1:item-1", "skimmed", "opened", now))
        self.assertFalse(mark(home, "file:lib-1:item-1", "studied", "x", now))
        self.assertEqual(depth_of(home, "file:lib-1:item-1"), "read")
        self.assertNotIn("Lantern", (home / "reading.json").read_text(encoding="utf-8"))
        self.assertIn("Sat", remind_to_read(self.plane, self.scope, "note:x", "Plan", "weekend"))

    def test_notes_can_point_at_an_existing_obsidian_vault(self) -> None:
        import tempfile

        made = tempfile.TemporaryDirectory()
        self.addCleanup(made.cleanup)
        mine = Path(made.name).resolve() / "MyVault"
        (mine / "Daily").mkdir(parents=True)
        (mine / "Daily" / "2026-11-13.md").write_text("Met the harbor master.\n", encoding="utf-8")
        (mine / "Tasks.md").write_text("my own task list\n", encoding="utf-8")
        settings, account = self.plane.settings, self.account["id"]
        for bad in ("relative", "smb://nas/vault", str(self.state), str(mine / "missing")):
            with self.assertRaises(SsiosError):
                set_vault(settings, account, bad)
        self.assertEqual(set_vault(settings, account, str(mine)), mine)
        self.assertEqual(account_vault(settings, account), mine)
        self.assertEqual(sorted(item["path"] for item in list_notes(settings, account)), ["Daily/2026-11-13.md", "Tasks.md"])
        from ssios.goals import manual_goal

        manual_goal(self.scope, title="Call the harbor desk", description="", status="open", goal_id="", now=datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc), peer_ip="127.0.0.1")
        self.assertEqual((mine / "Tasks.md").read_text(encoding="utf-8"), "my own task list\n")
        self.assertIn("Call the harbor desk", (mine / "SSIOS" / "Tasks.md").read_text(encoding="utf-8"))
        mine.rename(mine.with_name("Unplugged"))
        with self.assertRaises(SsiosError):
            list_notes(settings, account)
        self.assertFalse(mine.exists())
        set_vault(settings, account, "")
        self.assertEqual(account_vault(settings, account), self.root)
