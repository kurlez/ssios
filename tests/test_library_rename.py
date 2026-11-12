"""Renaming changes the display name only, and the page does not scroll sideways."""

from __future__ import annotations

import shutil

from ssios.errors import SsiosError
from ssios.library import ensure_personal, get_item, import_bytes, item_bytes, rename_item
from support import PlaneTest
from test_library import _png
from test_ui import _form, _get

LONG_CJK = "图片_20261107224447_109_930日历扫描件.jpg"
LONG_ASCII = "harbor_desk_scan_20261107_109_930_final_export_copy.png"


class RenameTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.plane.skills.set_account(self.account["id"], language="zh")
        self.library_id = ensure_personal(self.plane, self.account["id"])["id"]

    def test_rename_keeps_the_bytes_and_refuses_a_clash(self) -> None:
        first = import_bytes(self.plane, self.account["id"], self.library_id, _png(3), name="notes.pdf", source="picker", peer_ip="127.0.0.1")
        other = import_bytes(self.plane, self.account["id"], self.library_id, _png(4), name="taken.png", source="picker", peer_ip="127.0.0.1")
        before = item_bytes(self.plane, self.account["id"], self.library_id, first["id"])[0]
        renamed = rename_item(self.plane, self.account["id"], self.library_id, first["id"], "notes", "127.0.0.1")
        self.assertEqual(renamed["name"], "notes.pdf")
        self.assertEqual(item_bytes(self.plane, self.account["id"], self.library_id, first["id"])[0], before)
        explicit = rename_item(self.plane, self.account["id"], self.library_id, first["id"], "sheet.txt", "127.0.0.1")
        self.assertEqual(explicit["name"], "sheet.txt")
        self.assertEqual(get_item(self.plane, self.account["id"], self.library_id, first["id"])["sha256"], first["sha256"])
        with self.assertRaises(SsiosError) as invalid:
            rename_item(self.plane, self.account["id"], self.library_id, first["id"], "a/b.png", "127.0.0.1")
        self.assertEqual(invalid.exception.code, "library_rename")
        self.assertIn("路径", invalid.exception.detail)
        with self.assertRaises(SsiosError) as empty:
            rename_item(self.plane, self.account["id"], self.library_id, first["id"], "  ", "127.0.0.1")
        self.assertEqual(empty.exception.code, "library_rename")
        with self.assertRaises(SsiosError) as clash:
            rename_item(self.plane, self.account["id"], self.library_id, first["id"], "taken.png", "127.0.0.1")
        self.assertEqual(clash.exception.code, "library_rename_clash")
        self.assertEqual(get_item(self.plane, self.account["id"], self.library_id, first["id"])["name"], "sheet.txt")
        self.assertEqual(get_item(self.plane, self.account["id"], self.library_id, other["id"])["name"], "taken.png")
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("library.rename", audit)
        self.assertNotIn("sheet.txt", audit)
        self.assertNotIn("taken.png", audit)


class LibraryFitTest(PlaneTest):
    def test_long_names_do_not_widen_the_page(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        if not shutil.which("google-chrome") and not shutil.which("chromium"):
            self.skipTest("Chrome is not installed")
        account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.plane.skills.set_account(account["id"], language="zh")
        library_id = ensure_personal(self.plane, account["id"])["id"]
        saved = import_bytes(self.plane, account["id"], library_id, _png(8), name=LONG_CJK, source="picker", peer_ip="127.0.0.1")
        import_bytes(self.plane, account["id"], library_id, _png(9), name=LONG_ASCII, source="picker", peer_ip="127.0.0.1")
        status, _page, cookie = _form(self.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200)
        token = cookie.split(";", 1)[0].split("=", 1)[1]
        url = f"/ui/library?library={library_id}&item={saved['id']}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context()
            context.add_cookies([{"name": "ssios_session", "value": token, "url": self.base}])
            page = context.new_page()
            for width in (900, 1024, 1280, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                page.goto(self.base + url, wait_until="domcontentloaded")
                page.wait_for_selector(".lib-inspector")
                fits = page.evaluate(
                    """() => {
                      const doc = document.documentElement;
                      const pane = document.querySelector(".page");
                      return doc.scrollWidth <= doc.clientWidth + 1 && pane.scrollWidth <= pane.clientWidth + 1;
                    }"""
                )
                self.assertTrue(fits, width)
            page.click(".lib-rename-edit")
            page.wait_for_selector(".lib-rename-input")
            selected = page.evaluate("() => { const el = document.querySelector('.lib-rename-input'); return [el.selectionStart, el.selectionEnd, el.value]; }")
            self.assertEqual(selected[0], 0)
            self.assertLess(selected[1], len(LONG_CJK))
            self.assertTrue(str(selected[2]).endswith(".jpg"))
            page.fill(".lib-rename-input", "日历")
            page.keyboard.press("Enter")
            page.wait_for_selector(".lib-rename-text")
            self.assertIn("日历.jpg", page.inner_text(".lib-rename"))
            self.assertIn("日历.jpg", page.inner_text("#library-grid"))
            browser.close()
        status, shown = _get(self.base, url, cookie.split(";", 1)[0])
        self.assertEqual(status, 200, shown)
        self.assertIn("日历.jpg", shown)
