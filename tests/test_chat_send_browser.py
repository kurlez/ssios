"""The chat page must attach Send. A plain POST still returns HTML."""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import time
import unittest
import urllib.request
from pathlib import Path

from ssios.chat_store import ChatStore


def _chrome() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _Page:
    """A few DevTools calls against headless Chrome. No extra packages."""

    def __init__(self, binary: str) -> None:
        self.port = _port()
        self.profile = Path(f"/tmp/ssios-chat-browser-{self.port}")
        self.profile.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [
                binary,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--remote-allow-origins=*",
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.errors: list[str] = []
        self._id = 0
        self._sock = None
        self._buf = b""
        deadline = time.monotonic() + 10
        version = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=0.5) as response:
                    version = json.loads(response.read().decode())
                    break
            except Exception:
                time.sleep(0.1)
        if not version:
            self.close()
            raise RuntimeError("Chrome did not open a debugging port")
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/new?about:blank", method="PUT")
            with urllib.request.urlopen(request, timeout=5) as response:
                target = json.loads(response.read().decode())
        except Exception:
            self.close()
            raise
        self._connect(target["webSocketDebuggerUrl"])
        self.call("Runtime.enable")
        self.call("Network.enable")
        self.call("Page.enable")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)

    def call(self, method: str, params: dict | None = None, timeout: float = 10) -> dict:
        self._id += 1
        ident = self._id
        self._send({"id": ident, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._recv(deadline - time.monotonic())
            if message.get("id") == ident:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result") or {}
            self._note(message)
        raise TimeoutError(method)

    def goto(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})
        self.wait("document.readyState === 'complete'")

    def wait(self, expression: str, timeout: float = 8) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.eval(expression)
            if value:
                return
            time.sleep(0.05)
        raise AssertionError(expression)

    def eval(self, expression: str):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if result.get("exceptionDetails"):
            self.errors.append(str(result["exceptionDetails"].get("text") or "evaluate failed"))
            return None
        return (result.get("result") or {}).get("value")

    def _note(self, message: dict) -> None:
        method = message.get("method") or ""
        params = message.get("params") or {}
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            self.errors.append(str(details.get("text") or details.get("exception") or "pageerror"))
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            self.errors.append("console error")

    def _connect(self, ws_url: str) -> None:
        host, path = ws_url.split("://", 1)[1].split("/", 1)
        hostname, port = host.rsplit(":", 1)
        self._sock = socket.create_connection((hostname, int(port)), timeout=5)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        request = (
            f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            data += self._sock.recv(4096)
        head, _, rest = data.partition(b"\r\n\r\n")
        self._buf = rest
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError("websocket upgrade failed")

    def _send(self, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        mask = os.urandom(4)
        header = bytes([0x81, 0x80 | len(raw)]) if len(raw) < 126 else bytes([0x81, 0x80 | 126]) + struct.pack("!H", len(raw))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw))
        self._sock.sendall(header + mask + masked)

    def _recv(self, timeout: float) -> dict:
        self._sock.settimeout(max(0.1, timeout))
        while True:
            frame = self._frame()
            if frame is None:
                continue
            return json.loads(frame)

    def _frame(self) -> str | None:
        while len(self._buf) < 2:
            self._buf += self._sock.recv(4096)
        first, second = self._buf[0], self._buf[1]
        opcode = first & 0x0F
        length = second & 0x7F
        offset = 2
        if length == 126:
            while len(self._buf) < 4:
                self._buf += self._sock.recv(4096)
            length = struct.unpack("!H", self._buf[2:4])[0]
            offset = 4
        elif length == 127:
            while len(self._buf) < 10:
                self._buf += self._sock.recv(4096)
            length = struct.unpack("!Q", self._buf[2:10])[0]
            offset = 10
        while len(self._buf) < offset + length:
            self._buf += self._sock.recv(65536)
        payload = self._buf[offset : offset + length]
        self._buf = self._buf[offset + length :]
        if opcode == 0x9:
            self._sock.sendall(bytes([0x8A, len(payload)]) + payload)
            return None
        if opcode == 0x8:
            raise RuntimeError("socket closed")
        return payload.decode()


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8ffff3f0005fe02fe33d32f0000000049454e44ae426082"
)
BMP = bytes.fromhex(
    "424d3a00000000000000360000002800000001000000010000000100180000000000"
    "04000000000000000000000000000000000000000000ff00"
)


class ChatSendBrowserTest(unittest.TestCase):
    def setUp(self) -> None:
        binary = _chrome()
        if not binary:
            self.skipTest("headless Chrome is not installed")
        from support import PlaneTest
        from test_ui import _form

        self.case = PlaneTest()
        self.case.setUp()
        self.addCleanup(self.case._stop)
        self.addCleanup(self.case.tmp.cleanup)
        self._form = _form
        self.case.plane.accounts.add_member("ada", "other-horse-stable")
        status, _page, cookie = _form(self.case.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200)
        self.cookie = cookie.split(";", 1)[0].split("=", 1)[1]
        self.ada = next(item for item in self.case.plane.accounts.public_list() if item["username"] == "ada")
        self.page = _Page(binary)
        self.addCleanup(self.page.close)
        self.page.call(
            "Network.setCookie",
            {"name": "ssios_session", "value": self.cookie, "url": self.case.base},
        )

    def _open(self, path: str) -> None:
        self.page.goto(self.case.base + path)
        self.assertEqual(self.page.errors, [])

    def _type_and_send(self, text: str, *, enter: bool = False) -> None:
        literal = json.dumps(text)
        self.page.eval(
            "var box = document.querySelector('textarea[name=message]');"
            f"box.value = {literal};"
            "box.dispatchEvent(new Event('input', {bubbles: true}));"
            "true"
        )
        if enter:
            self.page.eval(
                "document.querySelector('textarea[name=message]').dispatchEvent("
                "new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); true"
            )
        else:
            self.page.eval("document.querySelector('button.send').click(); true")

    def test_plain_post_renders_html(self) -> None:
        from test_ui import _get

        page = _get(self.case.base, "/ui/chat", "ssios_session=" + self.cookie)[1]
        csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        status, body, _cookie = self._form(
            self.case.base,
            "/ui/chat/send",
            {"csrf": csrf, "message": "Hello from the form", "override": "auto"},
            cookie="ssios_session=" + self.cookie,
            extra={"Accept": "text/html"},
        )
        self.assertEqual(status, 200, body[:300])
        self.assertIn("<html", body)
        self.assertIn("Synthetic local answer", body)
        self.assertFalse(body.lstrip().startswith("event:"))
        conv = body.split('class="conversation">', 1)[1].split("<", 1)[0]
        shown = _get(self.case.base, "/ui/chat?conversation=" + conv, "ssios_session=" + self.cookie)[1]
        self.assertIn("Hello from the form", shown)

    def test_send_click_enter_and_paste_have_no_page_errors(self) -> None:
        store = ChatStore(self.case.plane.accounts.home(self.ada["id"]))
        prior = store.create("Harbor desk notes")
        store.append(prior["id"], {"role": "user", "text": "Harbor desk notes"})
        store.append(prior["id"], {"role": "assistant", "text": "The synthetic desk plan is ready."})
        self._open("/ui/chat?conversation=" + prior["id"])
        self.page.wait("document.body.innerText.includes('Harbor desk notes')")

        self._open("/ui/chat")
        self._type_and_send("Hello desk")
        self.page.wait("document.body.innerText.includes('Hello desk') && document.body.innerText.includes('Synthetic local answer')")
        self.assertEqual(self.page.errors, [])

        self._type_and_send("Second desk note", enter=True)
        self.page.wait("document.body.innerText.includes('Second desk note')")
        self.assertEqual(self.page.errors, [])

        payload = json.dumps(list(PNG))
        ready = self.page.eval(
            "var bytes = new Uint8Array(" + payload + ");"
            "var file = new File([bytes], 'desk.png', {type: 'image/png'});"
            "var data = new DataTransfer();"
            "data.items.add(file);"
            "var paste = new Event('paste', {bubbles: true});"
            "Object.defineProperty(paste, 'clipboardData', {value: data});"
            "document.querySelector('textarea[name=message]').dispatchEvent(paste);"
            "(document.getElementById('file-note') || {}).textContent || ''"
        )
        self.assertIn("1 file ready", ready or "")
        self._type_and_send("See the desk photo")
        self.page.wait("document.body.innerText.includes('See the desk photo') && document.body.innerText.includes('Synthetic caption of the attached image')")
        self.assertEqual(self.page.errors, [])

    def test_clipboard_items_attach_images_and_text_paste_still_works(self) -> None:
        from ssios.files import FileLibrary

        self._open("/ui/chat")
        payload = json.dumps(list(PNG))
        text_paste = self.page.eval(
            "var box = document.querySelector('textarea[name=message]');"
            "box.value = 'keep this';"
            "var data = {files: [], items: [{kind: 'string', type: 'text/plain', getAsFile: function () { return null; }}]};"
            "var paste = new Event('paste', {bubbles: true, cancelable: true});"
            "Object.defineProperty(paste, 'clipboardData', {value: data});"
            "box.dispatchEvent(paste);"
            "JSON.stringify({prevented: paste.defaultPrevented, value: box.value, thumbs: document.querySelectorAll('#paste-preview img').length})"
        )
        text_state = json.loads(text_paste)
        self.assertFalse(text_state["prevented"])
        self.assertEqual(text_state["value"], "keep this")
        self.assertEqual(text_state["thumbs"], 0)
        pasted = self.page.eval(
            "var bytes = new Uint8Array(" + payload + ");"
            "function imageFile(name) { return new File([bytes], name, {type: 'image/png'}); }"
            "function pasteItems(files) {"
            "  var items = files.map(function (file) { return {kind: 'file', type: 'image/png', getAsFile: function () { return file; }}; });"
            "  var data = {files: [], items: items};"
            "  var paste = new Event('paste', {bubbles: true, cancelable: true});"
            "  Object.defineProperty(paste, 'clipboardData', {value: data});"
            "  document.querySelector('textarea[name=message]').dispatchEvent(paste);"
            "}"
            "pasteItems([imageFile('shot.png'), imageFile('browser.png')]);"
            "JSON.stringify({prevented: true, thumbs: document.querySelectorAll('#paste-preview img').length, removes: document.querySelectorAll('.paste-x').length, note: (document.getElementById('file-note') || {}).textContent || ''})"
        )
        state = json.loads(pasted)
        self.assertEqual(state["thumbs"], 2)
        self.assertEqual(state["removes"], 2)
        self.assertIn("2 file ready", state["note"])
        self._shot("clipboard-preview-1280.png")
        self.page.eval("document.querySelector('.paste-x').click(); true")
        left = self.page.eval("document.querySelectorAll('#paste-preview img').length")
        self.assertEqual(left, 1)
        self.page.eval(
            "var box = document.querySelector('textarea[name=message]');"
            "box.value = 'Pasted screenshot';"
            "box.dispatchEvent(new Event('input', {bubbles: true}));"
            "document.querySelector('button.send').click(); true"
        )
        self.page.wait("document.body.innerText.includes('Pasted screenshot') && document.querySelectorAll('.msg.user .thumb').length >= 1")
        self.page.eval("document.querySelector('.msg.user .thumb').click(); true")
        self.page.wait("document.getElementById('image-lightbox') && !document.getElementById('image-lightbox').hidden")
        self.assertEqual(self.page.errors, [])
        self._shot("clipboard-sent-1280.png")
        rows = FileLibrary(self.case.plane).list(self.ada["id"], role="image")
        self.assertTrue(rows)
        self.assertTrue(all(item["url"].startswith("/ui/files/file-") for item in rows))

    def test_bmp_and_empty_type_paste_do_not_insert_a_file_url(self) -> None:
        self._open("/ui/chat")
        png = json.dumps(list(PNG))
        bmp = json.dumps(list(BMP))
        script = (
            "var pngBytes = new Uint8Array(" + png + ");"
            "var bmpBytes = new Uint8Array(" + bmp + ");"
            "var html = '<html><body><img src=\"file:///C:/Users/Public/snip.png\"></body></html>';"
            "function paste(items, files) {"
            "  var data = {files: files || [], items: items, getData: function (kind) { return kind === 'text/html' ? html : ''; }};"
            "  var event = new Event('paste', {bubbles: true, cancelable: true});"
            "  Object.defineProperty(event, 'clipboardData', {value: data});"
            "  document.querySelector('textarea[name=message]').dispatchEvent(event);"
            "  return event.defaultPrevented;"
            "}"
            "var empty = new File([pngBytes], 'snip.png', {type: ''});"
            "var bmpFile = new File([bmpBytes], 'snip.bmp', {type: 'image/bmp'});"
            "var prevented = paste(["
            "  {kind: 'string', type: 'text/html', getAsFile: function () { return null; }},"
            "  {kind: 'file', type: '', getAsFile: function () { return empty; }}"
            "], []);"
            "paste([{kind: 'file', type: 'image/bmp', getAsFile: function () { return bmpFile; }}], []);"
            "JSON.stringify({prevented: prevented, value: document.querySelector('textarea[name=message]').value})"
        )
        state = json.loads(self.page.eval(script))
        self.assertTrue(state["prevented"])
        self.assertNotIn("file:///", state["value"])
        self.page.wait("document.querySelectorAll('#paste-preview img').length >= 2 && Array.prototype.every.call(document.querySelectorAll('#paste-preview img'), function (img) { return img.naturalWidth > 0; })")
        self.page.eval(
            "var box = document.querySelector('textarea[name=message]');"
            "box.value = 'Pasted from the clipboard';"
            "box.dispatchEvent(new Event('input', {bubbles: true}));"
            "document.querySelector('button.send').click(); true"
        )
        self.page.wait(
            "document.body.innerText.includes('Synthetic caption of the attached image')"
            " && document.querySelector('.msg.user .thumb')"
            " && document.querySelector('.msg.user .thumb').getAttribute('src').indexOf('/ui/files/') === 0"
            " && document.querySelector('.msg.user .thumb').src.indexOf('localhost') < 0"
            " && document.querySelector('.msg.user .thumb').src.indexOf('file:') < 0"
        )
        self.page.eval(
            "window.__served = '';"
            "var src = document.querySelector('.msg.user .thumb').getAttribute('src');"
            "fetch(src, {credentials: 'same-origin'}).then(function (response) {"
            "  return response.arrayBuffer().then(function (buf) {"
            "    var bytes = new Uint8Array(buf);"
            "    window.__served = JSON.stringify({status: response.status, type: response.headers.get('content-type'), png: bytes[0] === 137 && bytes[1] === 80});"
            "  });"
            "}); true"
        )
        self.page.wait("window.__served")
        body = json.loads(self.page.eval("window.__served"))
        self.assertEqual(body["status"], 200)
        self.assertEqual(body["type"], "image/png")
        self.assertTrue(body["png"])
        self.assertEqual(self.page.errors, [])

    def test_a_failed_guest_clears_the_writing_indicator(self) -> None:
        import ssios.chat_guest as guests

        original = guests.chat_guest_report
        guests.chat_guest_report = lambda settings: {"ok": True, "detail": "files are present"}
        self.addCleanup(lambda: setattr(guests, "chat_guest_report", original))
        from ssios.google_connector import GoogleConnector, GoogleEndpoints
        from ssios.secret_store import MemorySecretStore

        self.case.plane._google = GoogleConnector(self.case.plane, MemorySecretStore(), GoogleEndpoints())
        self.case.plane.llm.report_script = ["Harbor Desk said the studio plan is on the desk. [1]"]

        def read_now(session, *, project_id, tool, arguments, limits, peer_ip="127.0.0.1"):
            del session, limits, peer_ip
            row = {"from": "Harbor Desk", "subject": "Studio plan", "date": "Nov 10", "snippet": "The studio plan is on the desk."}
            return {
                "tool": tool,
                "text": "Note || mail-header " + json.dumps(row),
                "arguments": arguments,
                "item_count": 1,
                "project_id": project_id,
            }

        self.case.plane.google.read_now = read_now
        self._open("/ui/chat")
        self._type_and_send("帮我总结一下最近的邮件")
        self.page.wait(
            "document.querySelectorAll('.typing').length === 0"
            " && document.body.innerText.includes('Harbor Desk said the studio plan is on the desk.')"
            " && document.body.innerText.includes('Run ssios vm-diagnose for the details.')"
            " && !document.body.innerText.includes('Assistant is writing')"
            " && !document.body.innerText.includes('AttributeError')",
            timeout=20,
        )
        self.assertEqual(self.page.errors, [])

    def test_checks_resolve_and_a_text_tool_call_is_not_the_answer(self) -> None:
        from ssios.google_connector import GoogleConnector, GoogleEndpoints
        from ssios.secret_store import MemorySecretStore

        self.case.plane._google = GoogleConnector(self.case.plane, MemorySecretStore(), GoogleEndpoints())
        self.case.plane.llm.report_script = ["Harbor Desk said the studio plan is on the desk. [1]"]

        def read_now(session, *, project_id, tool, arguments, limits, peer_ip="127.0.0.1"):
            del session, limits, peer_ip
            row = {"from": "Harbor Desk", "subject": "Studio plan", "date": "Nov 10", "snippet": "The studio plan is on the desk."}
            return {"tool": tool, "text": "Note || mail-header " + __import__("json").dumps(row), "arguments": arguments, "item_count": 1, "project_id": project_id}

        self.case.plane.google.read_now = read_now

        class _Guest:
            orchestrator_ready = True

            def run(self, plane, request, on_status):
                del plane, request
                on_status({"phase": "booting", "backend": "macos-vz"})
                on_status({"phase": "running", "backend": "macos-vz"})
                on_status({"phase": "isolation", "checks": {"nic": ["lo"], "outbound": False, "marker": True, "sandbox_denied": True, "outside_write": False}})
                return {
                    "text": '```json\n{ "name": "gmail.search", "arguments": { "query": "inbox" } }\n```',
                    "isolated": True,
                    "boot_ms": 1,
                    "backend": "macos-vz",
                    "checks": {},
                }

        self.case.plane.chat_guest = _Guest()
        self._open("/ui/chat")
        self._type_and_send("帮我总结一下最近的邮件")
        self.page.wait(
            "document.querySelectorAll('.typing').length === 0"
            " && document.querySelector('.steps [data-step=checks]')"
            " && document.querySelector('.steps [data-step=checks]').className === 'done'"
            " && document.body.innerText.includes('Harbor Desk said the studio plan')"
            " && !document.body.innerText.includes('Sources (none)')"
            " && !document.body.innerText.includes('Assistant is writing')",
            timeout=20,
        )
        self.assertEqual(self.page.errors, [])

    def _shot(self, name: str) -> None:
        import base64

        result = self.page.call("Page.captureScreenshot", {"format": "png"})
        target = Path("/opt/cursor/artifacts") / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(result["data"]))
