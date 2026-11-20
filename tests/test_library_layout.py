"""The Files page is one language, and a smart folder is not an imported file."""

from __future__ import annotations

import urllib.request

from ssios.library import ensure_personal, import_bytes
from support import PlaneTest
from test_ui import _form, _get


def _fields(page: str, op: str) -> dict:
    chunk = page.split(f'name="op" value="{op}"', 1)[0]
    return {
        "csrf": chunk.rsplit('name="csrf" value="', 1)[1].split('"', 1)[0],
        "library": chunk.rsplit('name="library" value="', 1)[1].split('"', 1)[0],
        "op": op,
    }


class LibraryLayoutTest(PlaneTest):
    def test_chinese_labels_and_the_smart_folder_form(self) -> None:
        account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.plane.skills.set_account(account["id"], language="zh")
        status, page, cookie = _form(self.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200, page)
        cookie = cookie.split(";", 1)[0]
        status, library = _get(self.base, "/ui/library", cookie)
        self.assertEqual(status, 200, library)
        self.assertIn("<h1>文件</h1>", library)
        self.assertEqual(library.count("<h1>"), 1)
        self.assertIn("全部", library)
        self.assertIn("未分类", library)
        self.assertIn("未加标签", library)
        self.assertIn("回收站", library)
        self.assertIn("文件夹", library)
        self.assertIn("导入", library)
        self.assertIn("把文件拖到这里", library)
        self.assertIn("没有文件。", library)
        self.assertNotIn(">Add folder<", library)
        self.assertNotIn(">Import<", library)
        self.assertNotIn(">Save rating<", library)
        self.assertNotIn("Error code:", library)
        saved = _form(self.base, "/ui/library", {**_fields(library, "smart"), "name": "港口", "query": "tag:港口 AND star>=3"}, cookie=cookie)
        self.assertEqual(saved[0], 200, saved[1])
        self.assertIn("港口", saved[1])
        self.assertIn("已保存智能文件夹。", saved[1])
        self.assertNotIn("压缩包", saved[1])
        self.assertNotIn("Error code:", saved[1])
        status, again = _get(self.base, "/ui/library", cookie)
        english = _form(self.base, "/ui/library", {**_fields(again, "smart"), "name": "Harbor", "query": "tag:harbor AND star>=3"}, cookie=cookie)
        self.assertEqual(english[0], 200, english[1])
        self.assertIn("Harbor", english[1])
        empty = _form(self.base, "/ui/library", {**_fields(again, "smart"), "name": "", "query": ""}, cookie=cookie)
        self.assertEqual(empty[0], 200, empty[1])
        self.assertIn("请填写智能文件夹的名称。", empty[1])
        self.assertIn('class="library"', empty[1])
        self.assertNotIn("压缩包", empty[1])
        self.assertNotIn("Error code:", empty[1])
        bad = _form(self.base, "/ui/library", {**_fields(again, "smart"), "name": "空条件", "query": "???"}, cookie=cookie)
        self.assertEqual(bad[0], 200, bad[1])
        self.assertIn("这个条件无效", bad[1])
        self.assertIn("tag:名称", bad[1])
        self.assertNotIn("压缩包", bad[1])
        folder = _form(self.base, "/ui/library", {**_fields(again, "folder"), "name": ""}, cookie=cookie)
        self.assertEqual(folder[0], 200, folder[1])
        self.assertIn("请填写文件夹名称。", folder[1])
        self.assertNotIn("压缩包", folder[1])
        watched = _form(
            self.base,
            "/ui/library",
            {**_fields(again, "watch"), "path": "smb://nas/photos", "enabled": "1"},
            cookie=cookie,
        )
        self.assertEqual(watched[0], 200, watched[1])
        self.assertIn("这个文件夹不在这台电脑上。", watched[1])
        self.assertIn('class="library"', watched[1])
        self.assertNotIn("Error code:", watched[1])

    def test_a_pdf_preview_is_an_image(self) -> None:
        account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.plane.skills.set_account(account["id"], language="zh")
        library_id = ensure_personal(self.plane, account["id"])["id"]
        saved = import_bytes(
            self.plane,
            account["id"],
            library_id,
            b"%PDF-1.4\n1 0 obj\nendobj\n",
            name="notes.pdf",
            source="picker",
            peer_ip="127.0.0.1",
        )
        status, _page, cookie = _form(self.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200)
        cookie = cookie.split(";", 1)[0]
        request = urllib.request.Request(
            self.base + f"/ui/library/thumb?library={library_id}&item={saved['id']}",
            headers={"Cookie": cookie},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
        self.assertTrue(content_type.startswith("image/"), content_type)
        self.assertTrue(body.startswith(b"\x89PNG"))
        status, page = _get(self.base, f"/ui/library?library={library_id}&item={saved['id']}", cookie)
        self.assertEqual(status, 200, page)
        self.assertIn("/ui/library/thumb?", page)
        self.assertIn('data-tile="PDF"', page)
        self.assertNotIn("<iframe", page)
        self.assertNotIn('src="/ui/library/raw?', page)
        self.assertIn("星级", page)
        self.assertIn("PDF", page)
        self.assertIn("个版本", page)
        self.assertNotIn(" bytes", page)


class PdfRenderLimitTest(PlaneTest):
    def test_a_pdf_that_cannot_be_drawn_is_tried_once_and_the_tools_share_one_time_limit(self) -> None:
        import subprocess

        import ssios.library_thumb as thumb

        account = self.plane.accounts.add_member("ada", "other-horse-stable")
        library_id = ensure_personal(self.plane, account["id"])["id"]
        saved = import_bytes(self.plane, account["id"], library_id, b"%PDF-1.4\n1 0 obj\nendobj\n", name="notes.pdf", source="picker", peer_ip="127.0.0.1")
        calls = []
        real_render = thumb._render_pdf
        thumb._render_pdf = lambda raw: calls.append(1) or b""
        self.addCleanup(setattr, thumb, "_render_pdf", real_render)
        first = thumb.preview_png(self.plane, account["id"], library_id, saved)
        second = thumb.preview_png(self.plane, account["id"], library_id, saved)
        self.assertEqual(len(calls), 1)
        self.assertTrue(first.startswith(b"\x89PNG"))
        self.assertEqual(first, second)
        thumb._render_pdf = real_render
        waits = []

        def slow(command, **kwargs):
            waits.append(kwargs["timeout"])
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        real_which, real_run = thumb.shutil.which, thumb.subprocess.run
        thumb.shutil.which = lambda name: "/usr/bin/" + name
        thumb.subprocess.run = slow
        self.addCleanup(setattr, thumb.shutil, "which", real_which)
        self.addCleanup(setattr, thumb.subprocess, "run", real_run)
        self.assertEqual(thumb._render_pdf(b"%PDF-1.4\n"), b"")
        self.assertEqual(len(waits), 4)
        self.assertTrue(all(0 < wait <= thumb.RENDER_SECONDS for wait in waits), waits)
