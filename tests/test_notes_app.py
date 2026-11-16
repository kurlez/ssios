"""Notes as an Obsidian vault: links, backlinks, search, outline, editing, renames, trash, and the page routes."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime
from pathlib import Path

from ssios import notes_edit
from ssios.errors import SsiosError
from ssios.notes_index import Resolver, VaultView, links_in, outline, parse_query, section, slug
from ssios.vault import account_vault
from support import PlaneTest
from test_library import _png
from test_ui import _form, _get

PASSWORD = "correct-horse-battery"
WHEN = datetime(2026, 11, 16, 9, 5)


class VaultCase(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.plane.accounts.add_admin("owner", PASSWORD)
        self.account = next(item for item in self.plane.accounts.public_list() if item["username"] == "owner")
        self.account_id = self.account["id"]
        self.root = account_vault(self.plane.settings, self.account_id)
        self.write(".obsidian/app.json", json.dumps({"attachmentFolderPath": "Attachments", "newFileLocation": "folder", "newFileFolderPath": "Inbox"}))
        self.write(".obsidian/daily-notes.json", json.dumps({"folder": "Daily", "format": "YYYY-MM-DD dddd", "template": "Templates/Day"}))
        self.write(".obsidian/templates.json", json.dumps({"folder": "Templates"}))
        self.write(".obsidian/bookmarks.json", json.dumps({"items": [{"type": "group", "items": [{"type": "file", "path": "Projects/Harbor/Budget.md"}]}]}))
        self.write("Templates/Day.md", "# {{title}}\n\nCreated {{date:YYYY/MM/DD}} at {{time}}.\n")
        self.write("Projects/Harbor/Plan.md", "---\naliases: [Harbor plan]\ntags:\n  - planning\n---\n# Plan\n\nSee [[Budget]], [[Budget#Costs|the costs]], and [[Missing note]].\n\n## Steps\n\nFirst step ^first\n\n```\n[[Not a link]]\n```\n")
        self.write("Projects/Harbor/Budget.md", "# Budget\n\n## Costs\n\nThe lantern costs 4419 dollars. Back to [plan](Plan.md).\n\n## Later\n\nNothing.\n")
        self.write("Archive/Budget.md", "Old budget. The Harbor plan is mentioned here without a link.\n")
        self.write("Journal.md", "Talked about the Plan today. ![[Plan#Steps]] #daily\n")
        self.write("pics/dock.png", _png(3))
        self.obsidian_before = self._snapshot(self.root / ".obsidian")

    def write(self, rel: str, content) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")

    @staticmethod
    def _snapshot(folder: Path) -> dict:
        return {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in folder.rglob("*")}

    def tearDown(self) -> None:
        # Nothing in this file may write Obsidian's own settings.
        self.assertEqual(self._snapshot(self.root / ".obsidian"), self.obsidian_before)
        super().tearDown()

    def view(self) -> VaultView:
        return VaultView(self.plane.settings, self.account_id)


class IndexTest(VaultCase):
    def test_links_resolve_like_obsidian_and_code_is_not_a_link(self) -> None:
        view = self.view()
        resolver = view.resolver
        self.assertEqual(resolver.resolve("Projects/Harbor/Plan.md", "Budget"), "Projects/Harbor/Budget.md")
        # Two notes are named Budget. Beside the current note wins, then the one nearest the vault root.
        self.assertEqual(resolver.resolve("Journal.md", "Budget"), "Archive/Budget.md")
        self.assertEqual(resolver.resolve("Journal.md", "Archive/Budget"), "Archive/Budget.md")
        self.assertEqual(resolver.resolve("Journal.md", "Harbor plan"), "Projects/Harbor/Plan.md")
        self.assertEqual(resolver.resolve("Projects/Harbor/Budget.md", "Plan.md"), "Projects/Harbor/Plan.md")
        self.assertEqual(resolver.resolve("Journal.md", "dock.png"), "pics/dock.png")
        self.assertEqual(resolver.resolve("Journal.md", "../../etc/passwd"), "")
        self.assertEqual(resolver.resolve("Journal.md", "Missing note"), "")
        plan = (self.root / "Projects/Harbor/Plan.md").read_text(encoding="utf-8")
        self.assertEqual([link["target"] for link in links_in(plan)], ["Budget", "Budget", "Missing note"])

    def test_backlinks_mentions_outgoing_outline_and_sections(self) -> None:
        view = self.view()
        back = view.backlinks("Projects/Harbor/Plan.md")
        self.assertEqual([row["path"] for row in back], ["Projects/Harbor/Budget.md", "Journal.md"])
        self.assertIn("Back to [plan](Plan.md).", back[0]["contexts"][0])
        mentions = view.unlinked("Projects/Harbor/Plan.md", {row["path"] for row in back})
        self.assertEqual([row["path"] for row in mentions], ["Archive/Budget.md"])
        text = (self.root / "Projects/Harbor/Plan.md").read_text(encoding="utf-8")
        outgoing = view.outgoing("Projects/Harbor/Plan.md", text)
        self.assertEqual([(row["path"], row["target"]) for row in outgoing], [("Projects/Harbor/Budget.md", "Budget"), ("", "Missing note")])
        self.assertEqual([(row["level"], row["anchor"]) for row in outline(text)], [(1, "plan"), (2, "steps")])
        budget = (self.root / "Projects/Harbor/Budget.md").read_text(encoding="utf-8")
        self.assertEqual(section(budget, "Costs"), "## Costs\n\nThe lantern costs 4419 dollars. Back to [plan](Plan.md).\n")
        self.assertEqual(section(text, "^first"), "First step ^first")
        self.assertEqual(section(text, "Nowhere"), "")
        self.assertEqual(slug("Costs & **Fees**"), "costs--fees")
        graph = view.graph()
        titles = {node["path"] for node in graph["nodes"]}
        self.assertIn("Journal.md", titles)
        self.assertEqual(len(graph["edges"]), 3)

    def test_search_words_phrases_exclusions_and_filters(self) -> None:
        view = self.view()
        self.assertEqual(parse_query('lantern "costs 4419" -old tag:planning path:projects file:plan #x'), {
            "words": ["lantern"], "phrases": ["costs 4419"], "not": ["old"], "tag": ["planning", "x"], "path": ["projects"], "file": ["plan"],
        })
        found = view.search("lantern")
        self.assertEqual([row["path"] for row in found["results"]], ["Projects/Harbor/Budget.md"])
        self.assertIn("<mark>lantern</mark>", found["results"][0]["snippets"][0])
        self.assertEqual([row["path"] for row in view.search("budget -old")["results"]], ["Projects/Harbor/Budget.md", "Projects/Harbor/Plan.md"])
        self.assertEqual([row["path"] for row in view.search("tag:planning")["results"]], ["Projects/Harbor/Plan.md"])
        self.assertEqual([row["path"] for row in view.search("path:archive budget")["results"]], ["Archive/Budget.md"])
        self.assertEqual(view.search('"no such phrase"')["results"], [])
        self.write("Escape.md", "<script>alert(1)</script> lantern")
        snippet = self.view().search("lantern")["results"]
        joined = " ".join(" ".join(row["snippets"]) for row in snippet)
        self.assertNotIn("<script>", joined)
        self.assertIn("&lt;script&gt;", joined)


class EditTest(VaultCase):
    def test_save_detects_a_change_on_disk_and_keeps_line_endings(self) -> None:
        settings = self.plane.settings
        text = (self.root / "Journal.md").read_text(encoding="utf-8")
        base = notes_edit.content_hash(text)
        new_base = notes_edit.write_note(settings, self.account_id, "Journal.md", text + "More.\r\n", base)
        self.assertEqual((self.root / "Journal.md").read_text(encoding="utf-8"), text + "More.\n")
        with self.assertRaises(SsiosError) as stale:
            notes_edit.write_note(settings, self.account_id, "Journal.md", "lost", base)
        self.assertEqual(stale.exception.code, "note_conflict")
        self.assertIn("More.", stale.exception.current)
        notes_edit.write_note(settings, self.account_id, "Journal.md", "kept", new_base)
        self.write("Windows.md", "a\r\nb\r\n")
        windows = (self.root / "Windows.md").read_bytes().decode()
        notes_edit.write_note(settings, self.account_id, "Windows.md", "a\nb\nc\n", notes_edit.content_hash(windows))
        self.assertEqual((self.root / "Windows.md").read_bytes(), b"a\r\nb\r\nc\r\n")
        self.assertEqual([path.name for path in self.root.rglob(".*.tmp")], [])
        for bad in (".obsidian/app.md", "../escape.md", "Projects/../.trash/x.md", "x.txt"):
            with self.assertRaises(SsiosError):
                notes_edit.write_note(settings, self.account_id, bad, "x", "")
        with self.assertRaises(SsiosError):
            notes_edit.write_note(settings, self.account_id, "Journal.md", "x", "")

    def test_new_daily_and_template_notes_follow_obsidian_settings(self) -> None:
        settings = self.plane.settings
        self.assertEqual(notes_edit.new_note_folder(settings, self.account_id, "Journal.md"), "Inbox")
        rel = notes_edit.free_path(settings, self.account_id, "Inbox", "Idea: one?")
        self.assertEqual(rel, "Inbox/Idea one.md")
        notes_edit.create_note(settings, self.account_id, rel)
        self.assertEqual(notes_edit.free_path(settings, self.account_id, "Inbox", "Idea one"), "Inbox/Idea one 1.md")
        daily, created = notes_edit.daily_note(settings, self.account_id, WHEN)
        self.assertEqual((daily, created), ("Daily/2026-11-16 Monday.md", True))
        self.assertEqual((self.root / daily).read_text(encoding="utf-8"), "# 2026-11-16 Monday\n\nCreated 2026/11/16 at 09:05.\n")
        self.assertEqual(notes_edit.daily_note(settings, self.account_id, WHEN), (daily, False))
        self.assertEqual(notes_edit.templates(settings, self.account_id), ["Templates/Day.md"])
        self.assertTrue(notes_edit.template_text(settings, self.account_id, "Templates/Day.md", "Trip", WHEN).startswith("# Trip"))
        with self.assertRaises(SsiosError):
            notes_edit.template_text(settings, self.account_id, "Journal.md", "x", WHEN)

    def test_rename_updates_links_everywhere_and_trash_keeps_the_file(self) -> None:
        settings = self.plane.settings
        result = notes_edit.rename_note(settings, self.account_id, "Projects/Harbor/Budget.md", "Finance/Harbor budget.md")
        self.assertEqual(result["path"], "Finance/Harbor budget.md")
        self.assertEqual(result["updated"], ["Finance/Harbor budget.md", "Projects/Harbor/Plan.md"])
        plan = (self.root / "Projects/Harbor/Plan.md").read_text(encoding="utf-8")
        self.assertIn("See [[Harbor budget]], [[Harbor budget#Costs|the costs]], and [[Missing note]].", plan)
        self.assertIn("[[Not a link]]", plan)
        moved = (self.root / "Finance/Harbor budget.md").read_text(encoding="utf-8")
        self.assertIn("Back to [plan](../Projects/Harbor/Plan.md).", moved)
        self.assertEqual(self.view().resolver.resolve("Finance/Harbor budget.md", "../Projects/Harbor/Plan.md"), "Projects/Harbor/Plan.md")
        self.assertFalse((self.root / "Projects/Harbor/Budget.md").exists())
        notes_edit.rename_note(settings, self.account_id, "Projects/Harbor/Plan.md", "Projects/Harbor/Plan v2.md")
        self.assertIn("Back to [plan](../Projects/Harbor/Plan%20v2.md).", (self.root / "Finance/Harbor budget.md").read_text(encoding="utf-8"))
        self.assertIn("![[Plan v2#Steps]]", (self.root / "Journal.md").read_text(encoding="utf-8"))
        with self.assertRaises(SsiosError):
            notes_edit.rename_note(settings, self.account_id, "Journal.md", "Finance/Harbor budget.md")
        where = notes_edit.trash_note(settings, self.account_id, "Journal.md")
        self.assertEqual(where, ".trash/Journal.md")
        self.assertTrue((self.root / ".trash/Journal.md").is_file())
        self.write("Journal.md", "again")
        self.assertEqual(notes_edit.trash_note(settings, self.account_id, "Journal.md"), ".trash/Journal 1.md")

    def test_pasted_images_go_to_the_attachment_folder_and_bookmarks_merge(self) -> None:
        settings = self.plane.settings
        rel = notes_edit.save_attachment(settings, self.account_id, "Journal.md", _png(5), WHEN)
        self.assertEqual(rel, "Attachments/Pasted image 20261116090500.png")
        self.assertEqual(notes_edit.save_attachment(settings, self.account_id, "Journal.md", _png(5), WHEN), "Attachments/Pasted image 20261116090500 1.png")
        with self.assertRaises(SsiosError):
            notes_edit.save_attachment(settings, self.account_id, "Journal.md", b"MZ\x90\x00not an image", WHEN)
        self.assertEqual(notes_edit.decode_image("data:image/png;base64," + base64.b64encode(b"abc").decode()), b"abc")
        self.assertEqual([row["path"] for row in notes_edit.bookmarks(settings, self.account_id)], ["Projects/Harbor/Budget.md"])
        self.assertTrue(notes_edit.toggle_bookmark(settings, self.account_id, "Journal.md"))
        self.assertEqual([(row["path"], row["source"]) for row in notes_edit.bookmarks(settings, self.account_id)], [("Journal.md", "ssios"), ("Projects/Harbor/Budget.md", "obsidian")])
        self.assertFalse(notes_edit.toggle_bookmark(settings, self.account_id, "Journal.md"))


class PageTest(VaultCase):
    def setUp(self) -> None:
        super().setUp()
        status, _page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": PASSWORD})
        self.cookie = cookie.split(";", 1)[0]

    def csrf(self, page: str) -> str:
        return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]

    def post_json(self, path: str, payload: dict) -> tuple[int, str]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(), method="POST", headers={"Cookie": self.cookie, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_three_panes_with_backlinks_outline_embeds_and_unresolved_links(self) -> None:
        status, page = _get(self.base, "/ui/vault/note?path=Projects%2FHarbor%2FPlan.md", self.cookie)
        self.assertEqual(status, 200, page)
        for part in ('class="vault-side"', 'class="vault-main"', 'class="vault-info"', 'data-folder="Projects/Harbor"', "Backlinks <span class=\"count\">2</span>", "Unlinked mentions <span class=\"count\">1</span>", 'href="#steps"', 'class="local-graph"'):
            self.assertIn(part, page)
        self.assertIn('href="/ui/vault/note?path=Projects%2FHarbor%2FBudget.md#costs"', page)
        self.assertIn('class="unresolved" href="/ui/vault/note?path=Inbox%2FMissing%20note.md&amp;missing=1"', page)
        self.assertIn('<p id="block-first">First step</p>', page)
        self.assertIn('<a class="chip" href="/ui/vault?view=tags&amp;tag=planning">#planning</a>', page)
        status, journal = _get(self.base, "/ui/vault/note?path=Journal.md", self.cookie)
        self.assertIn('<div class="embed"><div class="embed-title"><a href="/ui/vault/note?path=Projects%2FHarbor%2FPlan.md#steps">Plan › Steps</a></div><h2 id="steps">Steps</h2>', journal)
        status, missing = _get(self.base, "/ui/vault/note?path=Inbox%2FMissing%20note.md&missing=1", self.cookie)
        self.assertIn("This note does not exist yet.", missing)
        status, made, _cookie = _form(self.base, "/ui/vault/note", {"csrf": self.csrf(missing), "op": "create-missing", "path": "Inbox/Missing note.md"}, cookie=self.cookie)
        self.assertEqual(status, 200, made)
        self.assertTrue((self.root / "Inbox/Missing note.md").is_file())
        self.assertIn('id="note-editor"', made)

    def test_switcher_index_search_graph_and_bookmark_views(self) -> None:
        status, body = _get(self.base, "/ui/vault/index.json", self.cookie)
        rows = json.loads(body)
        self.assertIn({"p": "Projects/Harbor/Plan.md", "t": "Plan", "a": ["Harbor plan"]}, rows)
        self.assertIn("pics/dock.png", [row["p"] for row in rows])
        self.assertFalse(any(row["p"].startswith((".obsidian", ".trash")) for row in rows))
        status, found = _get(self.base, "/ui/vault?view=search&q=lantern", self.cookie)
        self.assertIn("1 notes match.", found)
        self.assertIn("<mark>lantern</mark>", found)
        status, graph = _get(self.base, "/ui/vault?view=graph", self.cookie)
        payload = json.loads(graph.split('id="graph-data">', 1)[1].split("</script>", 1)[0])
        self.assertEqual(len(payload["edges"]), 3)
        status, marks = _get(self.base, "/ui/vault?view=bookmarks", self.cookie)
        self.assertIn("from Obsidian", marks)

    def test_edit_preview_save_conflict_attach_and_csrf(self) -> None:
        status, page = _get(self.base, "/ui/vault/note?path=Journal.md&mode=edit", self.cookie)
        self.assertIn('id="note-editor"', page)
        csrf = self.csrf(page)
        base = page.split('name="base" value="', 1)[1].split('"', 1)[0]
        status, markup = self.post_json("/ui/vault/preview", {"csrf": csrf, "path": "Journal.md", "text": "# Hi\n\n**bold** [[Harbor/Budget]]"})
        self.assertEqual(status, 200)
        self.assertIn('<h1 id="hi">Hi</h1>', markup)
        self.assertIn('href="/ui/vault/note?path=Projects%2FHarbor%2FBudget.md"', markup)
        status, reply = self.post_json("/ui/vault/note", {"csrf": csrf, "op": "save", "ajax": "1", "path": "Journal.md", "base": base, "text": "Saved by AJAX\n"})
        self.assertEqual(status, 200, reply)
        newer = json.loads(reply)["base"]
        status, reply = self.post_json("/ui/vault/note", {"csrf": csrf, "op": "save", "ajax": "1", "path": "Journal.md", "base": base, "text": "stale\n"})
        self.assertEqual(status, 409)
        self.assertTrue(json.loads(reply)["conflict"])
        status, conflict, _cookie = _form(self.base, "/ui/vault/note", {"csrf": csrf, "op": "save", "path": "Journal.md", "base": base, "text": "mine\n"}, cookie=self.cookie)
        self.assertEqual(status, 409)
        self.assertIn("This note changed on disk after you opened it.", conflict)
        self.assertIn("Saved by AJAX", conflict)
        self.assertEqual((self.root / "Journal.md").read_text(encoding="utf-8"), "Saved by AJAX\n")
        status, done, _cookie = _form(self.base, "/ui/vault/note", {"csrf": csrf, "op": "save", "path": "Journal.md", "base": newer, "text": "Final\n"}, cookie=self.cookie)
        self.assertEqual(status, 200, done)
        self.assertEqual((self.root / "Journal.md").read_text(encoding="utf-8"), "Final\n")
        image = "data:image/png;base64," + base64.b64encode(_png(6)).decode()
        status, reply = self.post_json("/ui/vault/note", {"csrf": csrf, "op": "attach", "path": "Journal.md", "data": image})
        self.assertEqual(json.loads(reply)["embed"].startswith("![[Pasted image "), True)
        status, refused = self.post_json("/ui/vault/note", {"csrf": "wrong", "op": "save", "ajax": "1", "path": "Journal.md", "base": "", "text": "x"})
        self.assertNotEqual(status, 200)
        self.assertEqual((self.root / "Journal.md").read_text(encoding="utf-8"), "Final\n")
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("vault.write", audit)
        self.assertNotIn("Journal", audit)

    def test_rename_trash_daily_and_new_from_the_page(self) -> None:
        status, page = _get(self.base, "/ui/vault/note?path=Archive%2FBudget.md", self.cookie)
        csrf = self.csrf(page)
        status, renamed, _cookie = _form(self.base, "/ui/vault/note", {"csrf": csrf, "op": "rename", "path": "Archive/Budget.md", "new_path": "Archive/Old budget"}, cookie=self.cookie)
        self.assertEqual(status, 200, renamed)
        self.assertTrue((self.root / "Archive/Old budget.md").is_file())
        status, refused, _cookie = _form(self.base, "/ui/vault/note", {"csrf": csrf, "op": "trash", "path": "Archive/Old budget.md"}, cookie=self.cookie)
        self.assertEqual(status, 400)
        self.assertTrue((self.root / "Archive/Old budget.md").is_file())
        status, trashed, _cookie = _form(self.base, "/ui/vault/note", {"csrf": csrf, "op": "trash", "path": "Archive/Old budget.md", "confirm": "1"}, cookie=self.cookie)
        self.assertIn("Moved to .trash/Archive/Old budget.md", trashed)
        status, daily, _cookie = _form(self.base, "/ui/vault/note", {"csrf": csrf, "op": "daily"}, cookie=self.cookie)
        self.assertIn('id="note-editor"', daily)
        self.assertEqual(len(list((self.root / "Daily").glob("*.md"))), 1)
        status, made, _cookie = _form(self.base, "/ui/vault/note", {"csrf": csrf, "op": "create", "name": "Trip", "folder": "Inbox", "template": "Templates/Day.md"}, cookie=self.cookie)
        self.assertTrue((self.root / "Inbox/Trip.md").read_text(encoding="utf-8").startswith("# Trip"))
        status, bad, _cookie = _form(self.base, "/ui/vault/note", {"csrf": csrf, "op": "create", "name": "x", "folder": ".obsidian"}, cookie=self.cookie)
        self.assertEqual(status, 400)
        self.assertFalse(any(path.name == "x.md" for path in (self.root / ".obsidian").iterdir()))


class RoundTwoTest(VaultCase):
    def test_search_or_and_obsidian_link_settings(self) -> None:
        view = self.view()
        self.assertEqual(sorted(row["path"] for row in view.search("lantern OR mentioned")["results"]), ["Archive/Budget.md", "Projects/Harbor/Budget.md"])
        settings = self.plane.settings
        resolver = view.resolver
        shortest = {"format": "shortest", "markdown": False, "update": True}
        self.assertEqual(notes_edit.link_text("Projects/Harbor/Plan.md", "Journal.md", resolver, shortest), "[[Plan]]")
        self.assertEqual(notes_edit.link_text("Projects/Harbor/Budget.md", "Journal.md", resolver, shortest), "[[Projects/Harbor/Budget]]")
        self.assertEqual(notes_edit.link_text("Projects/Harbor/Plan.md", "Archive/Budget.md", resolver, dict(shortest, format="relative")), "[[../Projects/Harbor/Plan]]")
        self.assertEqual(notes_edit.link_text("pics/dock.png", "Projects/Harbor/Plan.md", resolver, dict(shortest, markdown=True), embed=True), "![](../../pics/dock.png)")
        self.assertEqual(notes_edit.link_settings(settings, self.account_id), shortest)
        self.write(".obsidian/app.json", json.dumps({"newLinkFormat": "absolute", "useMarkdownLinks": True, "alwaysUpdateLinks": False}))
        self.obsidian_before = self._snapshot(self.root / ".obsidian")
        self.assertEqual(notes_edit.link_settings(settings, self.account_id), {"format": "absolute", "markdown": True, "update": False})

    def test_link_a_mention_and_rename_without_updating_links(self) -> None:
        settings = self.plane.settings
        self.assertTrue(notes_edit.link_mention(settings, self.account_id, "Archive/Budget.md", "Projects/Harbor/Plan.md"))
        self.assertEqual((self.root / "Archive/Budget.md").read_text(encoding="utf-8"), "Old budget. The [[Plan|Harbor plan]] is mentioned here without a link.\n")
        self.assertFalse(notes_edit.link_mention(settings, self.account_id, "Archive/Budget.md", "Projects/Harbor/Plan.md"))
        before = (self.root / "Journal.md").read_text(encoding="utf-8")
        result = notes_edit.rename_note(settings, self.account_id, "Projects/Harbor/Plan.md", "Projects/Harbor/Plan two.md", update_links=False)
        self.assertEqual(result["updated"], [])
        self.assertEqual((self.root / "Journal.md").read_text(encoding="utf-8"), before)

    def test_inline_footnotes_list_block_ids_and_mermaid_markup(self) -> None:
        from ssios.markdown_render import render

        out = render("- item ^li\n\nText^[inline] and[^a].\n\n[^a]: defined\n\n```mermaid\ngraph TD; A-->B\n```\n", lambda url: "/ui/vault/note?path=" + url)
        self.assertIn('<li id="block-li">item</li>', out)
        self.assertIn('<a href="#fn-1" id="fnref-1">1</a>', out)
        self.assertIn('<li id="fn-1">inline ', out)
        self.assertIn('<li id="fn-2">defined ', out)
        self.assertIn('<pre class="mermaid">graph TD; A--&gt;B</pre>', out)


class RoundTwoPageTest(PageTest):
    def test_mermaid_is_served_and_moves_by_drag_answer_in_json(self) -> None:
        import urllib.request

        request = urllib.request.Request(self.base + "/ui/vendor/mermaid/mermaid.min.js", headers={"Cookie": self.cookie})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertTrue(response.headers.get("Content-Type", "").startswith("text/javascript"))
            self.assertGreater(len(response.read()), 1_000_000)
        status, page = _get(self.base, "/ui/vault/note?path=Archive%2FBudget.md", self.cookie)
        csrf = self.csrf(page)
        self.assertIn('value="link-mention"', page.split('Unlinked mentions', 1)[0] + _get(self.base, "/ui/vault/note?path=Projects%2FHarbor%2FPlan.md", self.cookie)[1])
        status, reply = self.post_json("/ui/vault/note", {"csrf": csrf, "op": "rename", "ajax": "1", "path": "Archive/Budget.md", "new_path": "Projects/Old budget", "update_links": "1"})
        self.assertEqual(json.loads(reply), {"ok": True, "path": "Projects/Old budget.md", "updated": 0})
        status, edit = _get(self.base, "/ui/vault/note?path=Journal.md&mode=edit", self.cookie)
        self.assertIn('data-link-format="shortest" data-markdown="0"', edit)
        self.assertIn("/ui/vendor/mermaid/mermaid.min.js", edit)


class ExcludedAndPeekTest(PageTest):
    def test_excluded_files_leave_search_graph_switcher_and_mentions(self) -> None:
        self.write(".obsidian/app.json", json.dumps({"userIgnoreFilters": ["Archive/", "/^Jour/"]}))
        self.obsidian_before = self._snapshot(self.root / ".obsidian")
        view = self.view()
        self.assertFalse(view.shown("Archive/Budget.md"))
        self.assertFalse(view.shown("Journal.md"))
        self.assertTrue(view.shown("Projects/Harbor/Plan.md"))
        self.assertEqual([row["path"] for row in view.search("budget")["results"]], ["Projects/Harbor/Budget.md", "Projects/Harbor/Plan.md"])
        self.assertNotIn("Journal.md", [node["path"] for node in view.graph()["nodes"]])
        self.assertEqual(view.unlinked("Projects/Harbor/Plan.md", set()), [])
        status, body = _get(self.base, "/ui/vault/index.json", self.cookie)
        listed = [row["p"] for row in json.loads(body)]
        self.assertNotIn("Archive/Budget.md", listed)
        status, page = _get(self.base, "/ui/vault", self.cookie)
        self.assertIn('data-path="Archive/Budget.md"', page)

    def test_hover_preview_shows_the_start_or_one_section(self) -> None:
        status, whole = _get(self.base, "/ui/vault/peek?path=Projects%2FHarbor%2FBudget.md", self.cookie)
        self.assertEqual(status, 200)
        self.assertIn('<h1 id="budget">Budget</h1>', whole)
        status, part = _get(self.base, "/ui/vault/peek?path=Projects%2FHarbor%2FBudget.md&h=costs", self.cookie)
        self.assertIn("4419", part)
        self.assertNotIn("Nothing.", part)
        import urllib.error

        with self.assertRaises(urllib.error.HTTPError) as refused:
            _get(self.base, "/ui/vault/peek?path=..%2Fsecrets.md", self.cookie)
        self.assertEqual(refused.exception.code, 400)
