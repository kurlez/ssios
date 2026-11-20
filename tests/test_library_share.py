"""A shared library keeps a private one private, and recall stops when membership ends."""

from __future__ import annotations

from ssios.errors import SsiosError
from ssios.library import add_folder, create_library, ensure_personal, get_item, import_bytes, item_bytes, update_item
from ssios.library_analyze import ScriptedModel, analyze_item
from ssios.library_share import share_library, unshare_library
from ssios.scope import AccountScope
from support import PlaneTest
from test_library import _png
from test_ui import _form, _get


class ShareTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.plane.accounts.add_admin("owner", "correct-horse-battery")
        self.ada = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.bob = self.plane.accounts.add_member("bob", "another-horse-stable")
        self.personal = ensure_personal(self.plane, self.owner["id"])["id"]
        self.studio = create_library(self.plane, self.owner["id"], "Studio", "127.0.0.1")["id"]

    def test_permissions_badges_and_revoked_recall(self) -> None:
        with self.assertRaises(SsiosError) as private:
            share_library(self.plane, self.owner["id"], self.personal, "ada", "view", "127.0.0.1")
        self.assertEqual(private.exception.code, "library_denied")
        saved = import_bytes(self.plane, self.owner["id"], self.studio, _png(6), name="tile.png", source="picker", peer_ip="127.0.0.1")
        phrase = "cedar4419"
        analyze_item(self.plane, self.owner["id"], self.studio, saved["id"], "127.0.0.1", model=ScriptedModel(phrase, ["cedar"], "DOCK"))
        share_library(self.plane, self.owner["id"], self.studio, "ada", "view", "127.0.0.1")
        _body, mime = item_bytes(self.plane, self.ada["id"], self.studio, saved["id"])
        self.assertEqual(mime, "image/png")
        with self.assertRaises(SsiosError) as denied:
            import_bytes(self.plane, self.ada["id"], self.studio, _png(7), name="extra.png", source="picker", peer_ip="127.0.0.1")
        self.assertEqual(denied.exception.code, "library_denied")
        with self.assertRaises(SsiosError):
            update_item(self.plane, self.ada["id"], self.studio, saved["id"], peer_ip="127.0.0.1", trash=True)
        ada = AccountScope(self.plane, self.ada)
        from ssios.reading import mark_file

        # Being given access is not reading. The note reaches her memory when she opens the file.
        with self.assertRaises(SsiosError):
            ada.memory().read_file(ada.project_id(), f"topics/library/{saved['id']}.md", include_untrusted=True)
        mark_file(self.plane, self.ada, self.studio, saved["id"], "skimmed", "opened")
        mark_file(self.plane, self.owner, self.studio, saved["id"], "skimmed", "opened")
        note = ada.memory().read_file(ada.project_id(), f"topics/library/{saved['id']}.md", include_untrusted=False)
        self.assertIn("Added by: owner", note["content"])
        self.assertIn(phrase, note["content"])
        self.plane.skills.set_account(self.ada["id"], language="zh")
        status, _page, cookie = _form(self.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200)
        status, page = _get(self.base, f"/ui/library?library={self.studio}", cookie.split(";", 1)[0])
        self.assertIn("由 owner 共享", page)
        self.assertIn('title="owner"', page)
        self.plane.skills.set_account(self.owner["id"], language="zh")
        status, _page, owner_cookie = _form(self.base, "/ui/login", {"username": "owner", "password": "correct-horse-battery"})
        status, owner_page = _get(self.base, f"/ui/library?library={self.studio}", owner_cookie.split(";", 1)[0])
        self.assertIn(">共享<", owner_page)
        self.assertIn("ada", owner_page)
        share_library(self.plane, self.owner["id"], self.studio, "ada", "add", "127.0.0.1")
        hers = import_bytes(self.plane, self.ada["id"], self.studio, _png(8), name="ada.png", source="picker", peer_ip="127.0.0.1")
        update_item(self.plane, self.ada["id"], self.studio, hers["id"], peer_ip="127.0.0.1", trash=True)
        self.assertTrue(get_item(self.plane, self.ada["id"], self.studio, hers["id"])["trashed"])
        with self.assertRaises(SsiosError):
            add_folder(self.plane, self.ada["id"], self.studio, "Pier", "127.0.0.1")
        share_library(self.plane, self.owner["id"], self.studio, "ada", "organize", "127.0.0.1")
        add_folder(self.plane, self.ada["id"], self.studio, "Pier", "127.0.0.1")
        with self.assertRaises(SsiosError):
            get_item(self.plane, self.bob["id"], self.personal, saved["id"])
        unshare_library(self.plane, self.owner["id"], self.studio, "ada", "127.0.0.1")
        with self.assertRaises(SsiosError) as missing:
            ada.memory().read_file(ada.project_id(), f"topics/library/{saved['id']}.md", include_untrusted=True)
        self.assertEqual(missing.exception.code, "not_found")
        self.assertEqual(ada.memory().search(ada.project_id(), phrase, include_untrusted=True)["hits"], [])
        owner = AccountScope(self.plane, self.owner)
        kept = owner.memory().read_file(owner.project_id(), f"topics/library/{saved['id']}.md", include_untrusted=False)
        self.assertIn(phrase, kept["content"])
        with self.assertRaises(SsiosError):
            item_bytes(self.plane, self.ada["id"], self.studio, saved["id"])
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("library.share", audit)
        self.assertIn("view", audit)
        self.assertIn("revoked", audit)
        self.assertNotIn(phrase, audit)
        self.assertNotIn("tile.png", audit)
