"""Search reads stored notes. The embedding bench skips when no local model is reachable."""

from __future__ import annotations

from ssios.library import ensure_personal, import_bytes, update_item
from ssios.library_analyze import ScriptedModel, analyze_item
from ssios.library_embed import embed_bench
from ssios.library_search import search_items
from support import PlaneTest
from test_library import _png


class SearchTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.library_id = ensure_personal(self.plane, self.account["id"])["id"]

    def test_search_finds_a_description_and_the_embed_bench_skips_off_machine(self) -> None:
        red = import_bytes(self.plane, self.account["id"], self.library_id, _png(3), name="red.png", source="picker", peer_ip="127.0.0.1")
        other = import_bytes(self.plane, self.account["id"], self.library_id, _png(4), name="other.png", source="picker", peer_ip="127.0.0.1")
        analyze_item(
            self.plane,
            self.account["id"],
            self.library_id,
            red["id"],
            "127.0.0.1",
            model=ScriptedModel("cedar dock", ["dock"], "PIER"),
        )
        update_item(self.plane, self.account["id"], self.library_id, red["id"], peer_ip="127.0.0.1", star=5)
        found = search_items(self.plane, self.account["id"], self.library_id, "pier")
        self.assertEqual([item["id"] for item in found], [red["id"]])
        self.assertEqual(search_items(self.plane, self.account["id"], self.library_id, "cedar", ext="png", min_star=4), found)
        self.assertEqual(search_items(self.plane, self.account["id"], self.library_id, "cedar", ext="pdf"), [])
        self.assertNotIn(other["id"], [item["id"] for item in found])
        remote = embed_bench("https://example.com")
        self.assertTrue(remote["skipped"])
        self.assertEqual(remote["reason"], "not-loopback")
        self.assertEqual(remote["results"], [])
        down = embed_bench("http://127.0.0.1:9", timeout=1)
        self.assertTrue(down["skipped"])
        self.assertEqual(down["reason"], "ollama-unreachable")
        self.assertIn("library-embed-bench", down["command"])
