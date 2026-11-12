"""A model that prints a tool call as text still runs the tool, and the blob is not the answer."""

from __future__ import annotations

import json
import unittest

from ssios.codex_guest import items_from_ollama_chat, render_codex_sse, responses_to_chat
from ssios.google_connector import GoogleConnector, GoogleEndpoints
from ssios.mcp_host import text_only_tool_calls
from ssios.secret_store import MemorySecretStore
from support import PlaneTest
from test_ui import _form, _get


PASSWORD = "correct-horse-battery"
BLOB = '```json\n{ "name": "gmail.search", "arguments": { "query": "inbox" } }\n```'


class ToolTextTest(unittest.TestCase):
    def test_text_and_native_tool_calls_both_become_gmail_search(self) -> None:
        parsed = text_only_tool_calls(BLOB)
        self.assertEqual(parsed, [{"name": "gmail.search", "arguments": {"q": "in:inbox"}}])
        self.assertIsNone(text_only_tool_calls("The inbox is quiet.\n" + BLOB))
        native = (
            b'{"message":{"role":"assistant","content":"","tool_calls":[{"function":{"name":"gmail_search","arguments":{"q":"in:inbox"}}}]},"done":true,"eval_count":3}\n'
        )
        items, _tokens = items_from_ollama_chat(native)
        self.assertEqual(items[0]["name"], "gmail.search")
        text_items, _tokens = items_from_ollama_chat(
            b'{"message":{"role":"assistant","content":' + json.dumps(BLOB).encode() + b'},"done":true,"eval_count":2}\n'
        )
        self.assertEqual(text_items[0]["name"], "gmail.search")
        rendered = render_codex_sse(items)
        self.assertIn(b'"name": "gmail.search"', rendered)
        self.assertNotIn(b"gmail_search", rendered)
        chat = responses_to_chat(
            {
                "model": "qwen2.5-coder:7b",
                "input": "summarize mail",
                "tools": [{"type": "function", "name": "gmail.search", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}],
            },
            16384,
            256,
        )
        self.assertEqual(chat["tools"][0]["function"]["name"], "gmail.search")
        self.assertIn("num_ctx", chat["options"])


class ToolTextChatTest(PlaneTest):
    def test_a_text_tool_call_runs_and_the_host_summary_is_the_answer(self) -> None:
        self.plane.accounts.add_admin("owner", PASSWORD)
        status, page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": PASSWORD})
        self.assertEqual(status, 200, page)
        cookie = cookie.split(";", 1)[0]
        self.plane._google = GoogleConnector(self.plane, MemorySecretStore(), GoogleEndpoints())
        self.plane.llm.report_script = ["Harbor Desk said the studio plan is on the desk. [1]"]
        seen = []

        def read_now(session, *, project_id, tool, arguments, limits, peer_ip="127.0.0.1"):
            del session, limits, peer_ip
            seen.append((tool, arguments.get("q")))
            row = {"from": "Harbor Desk", "subject": "Studio plan", "date": "Nov 10", "snippet": "The studio plan is on the desk."}
            return {"tool": tool, "text": "Note || mail-header " + json.dumps(row), "arguments": arguments, "item_count": 1, "project_id": project_id}

        self.plane.google.read_now = read_now

        class _Guest:
            orchestrator_ready = True

            def run(self, plane, request, on_status):
                del plane, request
                on_status({"phase": "booting", "backend": "macos-vz"})
                on_status({"phase": "running", "backend": "macos-vz"})
                on_status({"phase": "isolation", "checks": {"nic": ["lo"], "outbound": False, "marker": True, "sandbox_denied": True, "outside_write": False}})
                return {"text": BLOB, "isolated": True, "boot_ms": 1, "backend": "macos-vz", "checks": {}}

        self.plane.chat_guest = _Guest()
        _status, chat = _get(self.base, "/ui/chat", cookie)
        csrf = chat.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        status, page, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": csrf, "message": "帮我总结一下最近的邮件"},
            cookie=cookie,
        )
        self.assertEqual(status, 200, page)
        self.assertIn("Harbor Desk said the studio plan is on the desk.", page)
        self.assertIn("did not produce an answer", page)
        self.assertNotIn("Sources (none)", page)
        self.assertNotIn('"query": "inbox"', page)
        self.assertNotIn("```json", page)
        self.assertIn(("gmail.search", "in:inbox"), seen)
