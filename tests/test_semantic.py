"""Meaning search: off without a model, keyword results first, vectors only on disk."""

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from ssios.account_memory import memory_context, set_write_mode
from ssios.chat import iter_turn
from ssios.errors import SsiosError
from ssios.library import _library_root, ensure_personal, import_bytes, update_item
from ssios.library_analyze import ScriptedModel, analyze_item
from ssios.library_search import search_items
from ssios.scope import AccountScope
from ssios.semantic import OllamaEmbedder, SemanticIndex
from support import PlaneTest
from test_library import _png

_CONCEPTS = (
    ("boat", "ship", "vessel", "sail", "船"),
    ("invoice", "bill", "receipt", "发票"),
    ("lamp", "light", "lantern", "灯"),
)


class _Concepts:
    """A stand-in embedder. Words for the same idea share one axis."""

    model = "concept-stub"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail = False

    def __call__(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise SsiosError("embed_unavailable", "down", 503)
        self.calls.append(list(texts))
        made = []
        for text in texts:
            folded = text.casefold()
            made.append([1.0 if any(word in folded for word in group) else 0.0 for group in _CONCEPTS] + [0.05])
        return made


class IndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.embedder = _Concepts()
        self.path = Path(self.tmp.name) / "index" / "one.sqlite3"
        self.index = SemanticIndex(self.path, self.embedder, "concept-stub")

    def test_rank_caches_vectors_and_follows_changed_and_removed_text(self) -> None:
        docs = {"a": "a sailing boat at the pier", "b": "the electricity invoice for March", "c": "   "}
        self.assertEqual([key for key, _score in self.index.rank("s", docs, "ship")], ["a"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotIn(b"sailing", self.path.read_bytes())
        embedded = sum(len(batch) for batch in self.embedder.calls)
        self.assertEqual(embedded, 3)
        self.index.rank("s", docs, "ship")
        self.assertEqual(sum(len(batch) for batch in self.embedder.calls), embedded + 1)
        docs["b"] = "a cargo vessel"
        del docs["a"]
        found = self.index.rank("s", docs, "ship")
        self.assertEqual([key for key, _score in found], ["b"])
        self.assertEqual(self.index.rank("other", {"a": "a boat"}, "receipt"), [])

    def test_a_model_that_is_down_returns_nothing_and_a_remote_host_is_refused(self) -> None:
        self.embedder.fail = True
        self.assertEqual(self.index.rank("s", {"a": "a boat"}, "ship"), [])
        with self.assertRaises(SsiosError) as caught:
            OllamaEmbedder("https://example.com", "bge-m3")(["text"])
        self.assertEqual(caught.exception.code, "embed_unavailable")
        with self.assertRaises(SsiosError):
            OllamaEmbedder("http://127.0.0.1:9", "bge-m3", timeout=1)(["text"])


class BenchQualityTest(unittest.TestCase):
    def test_quality_suggests_a_threshold_only_when_right_beats_wrong(self) -> None:
        from ssios.library_embed import QUALITY_DOCS, QUALITY_QUERIES, retrieval_quality

        def exact(texts):
            source = QUALITY_DOCS if texts[0] in QUALITY_DOCS else QUALITY_QUERIES
            return [[1.0 if source.index(text) == axis else 0.2 for axis in range(len(source))] for text in texts]

        good = retrieval_quality(exact)
        self.assertEqual(good["top1"], "6/6")
        self.assertGreater(good["lowest_right"], good["suggested_min_score"])
        self.assertLess(good["highest_wrong"], good["suggested_min_score"])
        flat = retrieval_quality(lambda texts: [[1.0, 1.0] for _text in texts])
        self.assertNotIn("suggested_min_score", flat)


class FilesAndMemoryTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.library_id = ensure_personal(self.plane, self.account["id"])["id"]

    def _item(self, seed: int, name: str, description: str) -> dict:
        item = import_bytes(self.plane, self.account["id"], self.library_id, _png(seed), name=name, source="picker", peer_ip="127.0.0.1")
        analyze_item(self.plane, self.account["id"], self.library_id, item["id"], "127.0.0.1", model=ScriptedModel(description, [], ""))
        return item

    def test_files_search_adds_meaning_after_keyword_and_is_off_without_a_model(self) -> None:
        boat = self._item(3, "one.png", "a sailing boat at the pier")
        named = self._item(4, "ship-list.png", "a table of numbers")
        self._item(5, "three.png", "the electricity invoice")
        account = self.account["id"]
        self.assertEqual([item["id"] for item in search_items(self.plane, account, self.library_id, "ship")], [named["id"]])
        self.plane.embedder = _Concepts()
        found = search_items(self.plane, account, self.library_id, "ship")
        self.assertEqual([item["id"] for item in found], [named["id"], boat["id"]])
        self.assertEqual(search_items(self.plane, account, self.library_id, "ship", ext="pdf"), [])
        from ssios.library_page import library_page

        page = library_page(self.plane, {"account": self.account, "csrf": "c"}, {"library": self.library_id, "q": "ship"})
        self.assertIn("one.png", page)
        self.assertIn("ship-list.png", page)
        self.assertNotIn("three.png", page)
        index = _library_root(self.plane, account, self.library_id) / "index.sqlite3"
        self.assertTrue(index.is_file())
        self.assertNotIn(b"sailing", index.read_bytes())
        update_item(self.plane, account, self.library_id, boat["id"], peer_ip="127.0.0.1", trash=True)
        self.assertEqual([item["id"] for item in search_items(self.plane, account, self.library_id, "ship")], [named["id"]])
        self.plane.embedder.fail = True
        self.assertEqual([item["id"] for item in search_items(self.plane, account, self.library_id, "ship")], [named["id"]])

    def test_a_topic_note_is_recalled_by_meaning_and_stops_after_forget(self) -> None:
        scope = AccountScope(self.plane, self.account)
        set_write_mode(scope.home, "auto")
        events = list(iter_turn(self.plane, self.account, "remember the harbor desk lamp", "auto", "127.0.0.1"))
        self.assertTrue(any(event.get("event") == "done" for event in events), events)
        _text, used = memory_context(scope, "lantern")
        self.assertFalse(any(item.startswith("topics/") for item in used))
        self.plane.embedder = _Concepts()
        text, used = memory_context(scope, "lantern")
        topics = [item for item in used if item.startswith("topics/")]
        self.assertEqual(len(topics), 1, used)
        self.assertIn("lamp", text.casefold())
        from ssios.mcp_host import call_tool

        listed = call_tool(self.plane, self.account, "memory.search", {"q": "lantern"})
        self.assertIn(topics[0], listed["text"])
        from ssios.chat import apply_memory_change

        apply_memory_change(scope, [], "127.0.0.1", forget_path=topics[0])
        _text, used = memory_context(scope, "lantern")
        self.assertFalse(any(item.startswith("topics/") for item in used), used)
