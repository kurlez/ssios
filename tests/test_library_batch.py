"""Batch edits apply per item and a view-only member cannot tag."""

from __future__ import annotations

import shutil

from ssios.errors import SsiosError
from ssios.library import add_folder, create_library, ensure_personal, get_item, import_bytes
from ssios.library_analyze import ScriptedModel
from ssios.library_batch import batch_apply, zip_items
from ssios.library_share import share_library
from support import PlaneTest
from test_library import _png
from test_ui import _form, _get


class BatchApplyTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.plane.accounts.add_admin("owner", "correct-horse-battery")
        self.ada = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.library_id = create_library(self.plane, self.owner["id"], "Studio", "127.0.0.1")["id"]
        self.one = import_bytes(self.plane, self.owner["id"], self.library_id, _png(1), name="one.png", source="picker", peer_ip="127.0.0.1")
        self.two = import_bytes(self.plane, self.owner["id"], self.library_id, _png(2), name="two.png", source="picker", peer_ip="127.0.0.1")
        self.folder = add_folder(self.plane, self.owner["id"], self.library_id, "Harbor", "127.0.0.1")

    def test_tag_folder_trash_and_download_are_per_item(self) -> None:
        tagged = batch_apply(
            self.plane, self.owner["id"], self.library_id, [self.one["id"], self.two["id"], "nope"], "tag", "127.0.0.1", tag="harbor"
        )
        self.assertTrue(tagged["results"][0]["ok"])
        self.assertTrue(tagged["results"][1]["ok"])
        self.assertFalse(tagged["results"][2]["ok"])
        self.assertIn("harbor", get_item(self.plane, self.owner["id"], self.library_id, self.one["id"])["tags"])
        moved = batch_apply(
            self.plane, self.owner["id"], self.library_id, f"{self.one['id']},{self.two['id']}", "folder", "127.0.0.1", folder_id=self.folder["id"]
        )
        self.assertTrue(all(row["ok"] for row in moved["results"]))
        self.assertIn(self.folder["id"], get_item(self.plane, self.owner["id"], self.library_id, self.two["id"])["folder_ids"])
        trashed = batch_apply(self.plane, self.owner["id"], self.library_id, [self.one["id"]], "trash", "127.0.0.1")
        self.assertTrue(trashed["results"][0]["ok"])
        self.assertTrue(get_item(self.plane, self.owner["id"], self.library_id, self.one["id"])["trashed"])
        purged = batch_apply(self.plane, self.owner["id"], self.library_id, [self.one["id"]], "purge", "127.0.0.1", confirm="delete")
        self.assertTrue(purged["results"][0]["ok"])
        with self.assertRaises(SsiosError):
            get_item(self.plane, self.owner["id"], self.library_id, self.one["id"])
        analyzed = batch_apply(
            self.plane,
            self.owner["id"],
            self.library_id,
            [self.two["id"]],
            "analyze",
            "127.0.0.1",
            model=ScriptedModel("a red tile", ["red"], ""),
        )
        self.assertTrue(analyzed["results"][0]["ok"])
        self.assertEqual(get_item(self.plane, self.owner["id"], self.library_id, self.two["id"])["description"], "a red tile")
        blob = zip_items(self.plane, self.owner["id"], self.library_id, [self.two["id"]], "127.0.0.1")
        self.assertTrue(blob.startswith(b"PK"))
        share_library(self.plane, self.owner["id"], self.library_id, "ada", "view", "127.0.0.1")
        with self.assertRaises(SsiosError) as denied:
            batch_apply(self.plane, self.ada["id"], self.library_id, [self.two["id"]], "tag", "127.0.0.1", tag="nope")
        self.assertEqual(denied.exception.code, "library_denied")
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("library.batch_tag", audit)
        self.assertIn("library.batch_folder", audit)
        self.assertIn("library.batch_trash", audit)
        self.assertIn("library.batch_purge", audit)
        self.assertIn("library.batch_analyze", audit)
        self.assertIn("library.batch_download", audit)
        self.assertNotIn("one.png", audit)
        self.assertNotIn("harbor", audit)


class BatchSelectTest(PlaneTest):
    def test_ctrl_click_shows_the_bar_without_widening_the_page(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        if not shutil.which("google-chrome") and not shutil.which("chromium"):
            self.skipTest("Chrome is not installed")
        account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.plane.skills.set_account(account["id"], language="zh")
        library_id = ensure_personal(self.plane, account["id"])["id"]
        for index in range(3):
            import_bytes(self.plane, account["id"], library_id, _png(index + 1), name=f"tile-{index}.png", source="picker", peer_ip="127.0.0.1")
        status, _page, cookie = _form(self.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200)
        token = cookie.split(";", 1)[0].split("=", 1)[1]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context()
            context.add_cookies([{"name": "ssios_session", "value": token, "url": self.base}])
            page = context.new_page()
            page.set_viewport_size({"width": 1280, "height": 900})
            page.goto(self.base + "/ui/library", wait_until="domcontentloaded")
            page.wait_for_selector(".lib-card")
            cards = page.locator(".lib-card")
            cards.nth(0).click(modifiers=["Control"])
            cards.nth(2).click(modifiers=["Control"])
            page.wait_for_selector("#lib-batch")
            self.assertIn("已选择 2 项", page.inner_text("#lib-batch"))
            self.assertIn("添加标签", page.inner_text("#lib-batch"))
            page.locator("#lib-batch [data-batch=tag]").click()
            page.wait_for_selector("#lib-tag-dialog[open]")
            fits = page.evaluate(
                """() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1
                  && document.querySelector('.page').scrollWidth <= document.querySelector('.page').clientWidth + 1"""
            )
            self.assertTrue(fits)
            page.set_viewport_size({"width": 1024, "height": 900})
            fits = page.evaluate(
                """() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1
                  && document.querySelector('.page').scrollWidth <= document.querySelector('.page').clientWidth + 1"""
            )
            self.assertTrue(fits)
            browser.close()
